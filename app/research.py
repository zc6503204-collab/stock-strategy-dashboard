"""Versioned, deterministic multi-strategy research using completed bars only."""
from collections import defaultdict
from datetime import timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from .models import Signal, symbol_market
from .calendars import local_date, open_time, is_open
from .strategy_registry import StrategyRegistry, DEFINITIONS

VERSION = '实验 2.0.0'
BENCHMARKS = {'CN': '000300.SH', 'US': 'SPY.US'}


def prefix(day, through, market):
    t = open_time(day, market);result = []
    while t <= through:
        if is_open(t, market):result.append(t)
        t += timedelta(minutes=5)
    return result


def _atr(bars, period):
    if len(bars) < period + 1:return None
    rows = bars[-period:];previous = bars[-period-1:-1]
    return sum(max(b.high-b.low, abs(b.high-p.close), abs(b.low-p.close)) for b,p in zip(rows,previous))/period


def daily_factors(symbol, bars, benchmark, fast=20, slow=None, liquidity_min=None, rs_min=0):
    required=max(25,fast+5,(slow or 0)+5)
    if len(bars)<required or len(benchmark)<21:return {'eligible':False,'reason':'等待股票和基准完整日线'}
    market=symbol_market(symbol);dates={local_date(b.start,market):b for b in benchmark}
    if any(local_date(b.start,market) not in dates for b in bars[-21:]):
        return {'eligible':False,'reason':'股票与基准日线日期不一致'}
    closes=[b.close for b in bars];recent=bars[-20:]
    mean=sum(closes[-fast:])/fast;old=sum(closes[-fast-5:-5])/fast
    trend=closes[-1]>mean>old
    slow_value=sum(closes[-slow:])/slow if slow else None
    if slow:trend=trend and mean>slow_value
    amount=sum(b.turnover or 0 for b in recent)/20
    atr=_atr(bars,20)
    first,last=(dates[local_date(b.start,market)] for b in [bars[-21],bars[-1]])
    rs=(closes[-1]/closes[-21]-last.close/first.close)*100
    threshold=liquidity_min if liquidity_min is not None else (1e8 if market=='CN' else 5e6)
    liquid=amount>=threshold;eligible=trend and liquid and rs>rs_min
    return {'eligible':eligible,'trend':trend,'relative_strength':rs,'atr':atr,
            'average_turnover':amount,'ma_fast':mean,'ma_slow':slow_value,
            'reason':'日线与相对强弱通过' if eligible else '等待趋势向上、流动性及跑赢基准同时成立'}


def _ema(values, period):
    if not values:return None
    alpha=2/(period+1);value=values[0]
    for item in values[1:]:value=alpha*item+(1-alpha)*value
    return value


class ResearchEngine:
    def __init__(self, registry=None):
        self.registry=registry or StrategyRegistry()
        self.history={};self.context={};self.benchmarks={};self.views={}

    def reset(self,symbol):
        self.history.pop(symbol,None);self.views.pop(symbol,None)

    def prepare(self,symbol,daily,historical,benchmark_daily):
        market=symbol_market(symbol);base=self.registry.config('breakout',market)
        f=daily_factors(symbol,daily,benchmark_daily,liquidity_min=base['liquidity_min'],rs_min=base['relative_strength_min'])
        groups=defaultdict(dict)
        for b in historical:
            if b.final and b.valid() and is_open(b.start,market):groups[local_date(b.start,market)][b.start]=b
        self.context[symbol]={'daily':f,'daily_bars':list(daily),'benchmark_daily':list(benchmark_daily),
                              'sessions':dict(groups),'source':daily[-1].source if daily else None}

    def set_benchmark(self,market,bars):
        self.benchmarks[market]=[b for b in bars if b.final and b.valid() and is_open(b.start,market)]

    def update(self,bar,risk_group='normal'):
        if not bar.final or not bar.valid():return []
        hist=self.history.setdefault(bar.symbol,[])
        if hist and (bar.source!=hist[-1].source or bar.start<=hist[-1].start):return []
        hist.append(bar);del hist[:-6000]
        return self.evaluate(bar.symbol,risk_group)

    def _version(self,strategy,market):return self.registry.current(strategy,market)['version']

    def _signal(self,b,strategy,stop,group,f,extra):
        market=symbol_market(b.symbol);version=self._version(strategy,market)
        sid=sha256(f'{b.symbol}|{b.source}|{b.start.isoformat()}|{strategy}|{version}'.encode()).hexdigest()[:24]
        evidence={**f,**extra,'bar':b.dump(),'vwap_method':'5分钟OHLCV典型价格近似VWAP',
                  'signal_minutes':self.registry.config(strategy,market).get('signal_minutes',5)}
        return Signal(sid,b.symbol,strategy,b.source,b.end,b.close,stop,group,evidence,version)

    def _daily(self,symbol,strategy):
        market=symbol_market(symbol);ctx=self.context[symbol];p=self.registry.config(strategy,market)
        if strategy=='trend_pullback':
            return daily_factors(symbol,ctx.get('daily_bars',[]),ctx.get('benchmark_daily',[]),p['daily_fast'],p['daily_slow'],p['liquidity_min'],p['relative_strength_min'])
        if strategy=='volatility_breakout':
            return daily_factors(symbol,ctx.get('daily_bars',[]),ctx.get('benchmark_daily',[]),p['daily_ma'],None,p['liquidity_min'],p['relative_strength_min'])
        return ctx['daily']

    def evaluate(self,symbol,group='normal'):
        hist=self.history.get(symbol,[]);ctx=self.context.get(symbol,{})
        aggregate={'ready':False,'reason':'正在补齐策略所需数据','source':ctx.get('source'),'strategies':{}}
        self.views[symbol]=aggregate
        if not hist or not ctx:return []
        current=hist[-1];market=symbol_market(symbol);day=local_date(current.start,market)
        if current.source!=ctx.get('source'):
            aggregate['reason']='策略历史与当前行情来源不一致';return []
        today=[b for b in hist if local_date(b.start,market)==day];expected=prefix(day,current.start,market)
        if [b.start for b in today]!=expected:
            aggregate['reason']='缺少今天开盘以来的完整K线';return []
        benchmark={b.start:b for b in self.benchmarks.get(market,[]) if local_date(b.start,market)==day and b.start<=current.start}
        if set(benchmark)!=set(expected) or any(b.source!=current.source for b in benchmark.values()):
            aggregate['reason']='等待同源基准同步至当前完整K线';return []

        volume=notional=bvolume=bnotional=0.;frame=[]
        for b in today:
            volume+=b.volume;notional+=(b.high+b.low+b.close)/3*b.volume
            bb=benchmark[b.start];bvolume+=bb.volume;bnotional+=(bb.high+bb.low+bb.close)/3*bb.volume
            frame.append({'bar':b,'benchmark':bb,'vwap':notional/volume if volume else None,
                          'benchmark_vwap':bnotional/bvolume if bvolume else None})

        sessions=ctx.get('sessions',{});prior=sorted(d for d in sessions if d<day)[-14:];rvol=[]
        if len(prior)==14:
            tz=ZoneInfo('Asia/Shanghai' if market=='CN' else 'America/New_York')
            old_cumulative=[0.]*14;today_cumulative=0.;complete=True
            maps=[{b.start.astimezone(tz).strftime('%H:%M'):b for b in sessions[d].values()} for d in prior]
            for b in today:
                today_cumulative+=b.volume;clock=b.start.astimezone(tz).strftime('%H:%M')
                for j,mapped in enumerate(maps):
                    old=mapped.get(clock)
                    if old is None:complete=False;break
                    old_cumulative[j]+=old.volume
                if not complete:break
                average=sum(old_cumulative)/14;rvol.append(today_cumulative/average if average else 0)
            if not complete:rvol=[]

        signals=[];strategy_views={}
        for strategy in DEFINITIONS:
            if not self.registry.enabled(strategy,market):
                strategy_views[strategy]={'status':'disabled','ready':False,'reason':'该策略已停用'};continue
            daily=self._daily(symbol,strategy)
            if not daily.get('eligible'):
                strategy_views[strategy]={**daily,'status':'wait','ready':False};continue
            if strategy in ('breakout','pullback'):
                found,view=self._opening_strategy(symbol,strategy,today,frame,rvol,group,daily)
            elif strategy=='trend_pullback':
                found,view=self._trend_pullback(symbol,today,frame,group,daily)
            else:
                found,view=self._volatility_breakout(symbol,today,frame,rvol,group,daily)
            signals.extend(found);strategy_views[strategy]=view
        aggregate.update(ready=True,last_bar=current.start.isoformat(),strategies=strategy_views)
        confirmed=[k for k,v in strategy_views.items() if v.get('status')=='signal']
        aggregate['reason']='买点已确认，正在核对现价、盘口和资金' if confirmed else next((v.get('reason') for v in strategy_views.values() if v.get('ready')), '等待策略条件成立')
        preferred=strategy_views.get('breakout',{})
        aggregate.update({k:v for k,v in preferred.items() if k!='strategies'});aggregate['strategies']=strategy_views
        return signals

    def _opening_strategy(self,symbol,strategy,today,frame,rvol,group,daily):
        market=symbol_market(symbol);p=self.registry.config(strategy,market);n=p['opening_bars']
        if len(today)<=n:return [],{**daily,'status':'wait','ready':False,'reason':'等待开盘区间形成及后续完整五分钟K线'}
        if len(rvol)!=len(today):return [],{**daily,'status':'wait','ready':False,'reason':'14日同时间段数据有缺口，继续等待'}
        opening=today[:n];level=max(b.high for b in opening);bullish=opening[-1].close>opening[0].open
        base=None;last=[]
        for i,item in enumerate(frame):
            b=item['bar'];good=bool(item['vwap'] and item['benchmark_vwap'] and b.close>item['vwap'] and item['benchmark'].close>item['benchmark_vwap'] and rvol[i]>=p['rvol_min'])
            found=[]
            if base and strategy=='pullback':
                age=i-base['index'];tol=p['touch_tolerance_pct']/100
                if age>p['pullback_bars'] or b.close<level or (base['touched'] and b.low<base['low']):base=None
                elif base['touched'] and i>0 and b.close>today[i-1].high and good:
                    found.append(self._signal(b,'pullback',base['low'],group,daily,{'relative_volume':rvol[i],'level':level,'vwap':item['vwap'],'benchmark_vwap':item['benchmark_vwap']}));base=None
                elif b.low<=level*(1+tol) and b.close>=level:base={'index':base['index'],'touched':True,'low':b.low}
            crossed=i>=n and today[i-1].close<=level<b.close
            if bullish and crossed and good:
                stop=min(x.low for x in today[max(0,i-p.get('stop_lookback',4)+1):i+1])
                if strategy=='breakout' and stop<b.close:
                    found.append(self._signal(b,'breakout',stop,group,daily,{'relative_volume':rvol[i],'level':level,'vwap':item['vwap'],'benchmark_vwap':item['benchmark_vwap']}))
                if strategy=='pullback':base={'index':i,'touched':False,'low':b.low}
            last=found
        current=frame[-1];status='signal' if last else 'wait'
        reason='买点已确认' if last else '等待开盘区间突破' if strategy=='breakout' else '等待突破后的首次回踩再启动'
        return last,{**daily,'status':status,'ready':True,'reason':reason,'level':level,'stop':min(b.low for b in today[-3:]),
                     'relative_volume':rvol[-1],'vwap':current['vwap'],'benchmark_vwap':current['benchmark_vwap'],'market_ok':current['benchmark'].close>current['benchmark_vwap']}

    def _trend_pullback(self,symbol,today,frame,group,daily):
        market=symbol_market(symbol);p=self.registry.config('trend_pullback',market);period=p['intraday_ema']
        if len(today)<period+1:return [],{**daily,'status':'wait','ready':False,'reason':f'等待至少{period+1}根五分钟K线形成EMA'}
        current=frame[-1];previous=frame[-2];ema_prev=_ema([b.close for b in today[:-1]],period)
        band=max(previous['vwap'] or 0,ema_prev or 0);floor=min(previous['vwap'] or band,ema_prev or band)
        tolerance=p['touch_tolerance_pct']/100
        touched=bool(band and previous['bar'].low<=band*(1+tolerance) and previous['bar'].close>=floor)
        prior_volumes=[b.volume for b in today[max(0,len(today)-22):-2]]
        average=sum(prior_volumes)/len(prior_volumes) if prior_volumes else 0
        ratio=current['bar'].volume/average if average else 0
        market_ok=bool(current['benchmark_vwap'] and current['benchmark'].close>current['benchmark_vwap'])
        confirmed=touched and current['bar'].close>previous['bar'].high and current['bar'].close>(current['vwap'] or current['bar'].close) and market_ok and ratio>=p['confirm_volume_ratio']
        found=[]
        if confirmed:
            stop=min(b.low for b in today[-p['stop_lookback']:])
            if stop<current['bar'].close:found=[self._signal(current['bar'],'trend_pullback',stop,group,daily,{'relative_volume':ratio,'level':band,'vwap':current['vwap'],'benchmark_vwap':current['benchmark_vwap']})]
        return found,{**daily,'status':'signal' if found else 'wait','ready':True,'reason':'买点已确认' if found else '等待回踩VWAP或五分钟EMA后放量转强','level':band,'stop':min(b.low for b in today[-p['stop_lookback']:]),'relative_volume':ratio,'vwap':current['vwap'],'benchmark_vwap':current['benchmark_vwap'],'market_ok':market_ok}

    def _volatility_breakout(self,symbol,today,frame,rvol,group,daily):
        market=symbol_market(symbol);p=self.registry.config('volatility_breakout',market);bars=self.context[symbol].get('daily_bars',[])
        need=max(21,p['atr_long']+1,p['breakout_days'])
        if len(bars)<need:return [],{**daily,'status':'wait','ready':False,'reason':'等待完整日线计算波动收缩'}
        short=_atr(bars,p['atr_short']);long=_atr(bars,p['atr_long']);ratio=short/long if short and long else None
        high20=max(b.high for b in bars[-20:]);distance=(high20-bars[-1].close)/high20*100;level=max(b.high for b in bars[-p['breakout_days']:])
        setup=ratio is not None and ratio<=p['contraction_max'] and distance<=p['near_high_pct']
        if not setup:return [],{**daily,'status':'wait','ready':True,'reason':'等待ATR收缩并靠近20日高点','atr_contraction':ratio,'distance_to_high_pct':distance,'level':level}
        if len(rvol)!=len(today):return [],{**daily,'status':'wait','ready':False,'reason':'14日同时间段数据有缺口，继续等待','atr_contraction':ratio,'level':level}
        current=frame[-1];previous=today[-2] if len(today)>1 else None
        market_ok=bool(current['benchmark_vwap'] and current['benchmark'].close>current['benchmark_vwap'])
        confirmed=bool(previous and previous.close<=level<current['bar'].close and current['bar'].close>(current['vwap'] or current['bar'].close) and market_ok and rvol[-1]>=p['rvol_min'])
        found=[]
        if confirmed:
            stop=min(b.low for b in today[-p['stop_lookback']:])
            if stop<current['bar'].close:found=[self._signal(current['bar'],'volatility_breakout',stop,group,daily,{'relative_volume':rvol[-1],'level':level,'vwap':current['vwap'],'benchmark_vwap':current['benchmark_vwap'],'atr_contraction':ratio})]
        return found,{**daily,'status':'signal' if found else 'wait','ready':True,'reason':'买点已确认' if found else '波动已收缩，等待放量突破整理区','atr_contraction':ratio,'distance_to_high_pct':distance,'level':level,'stop':min(b.low for b in today[-p['stop_lookback']:]),'relative_volume':rvol[-1],'vwap':current['vwap'],'benchmark_vwap':current['benchmark_vwap'],'market_ok':market_ok}

    def preview(self,symbol):return self.views.get(symbol,{'ready':False,'reason':'等待研究数据核验','strategies':{}})
