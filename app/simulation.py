"""Long-only paper execution with portfolio and strategy-isolated shadow ledgers."""
from datetime import timedelta
from math import floor,sqrt

from .models import Signal,symbol_market,now,stamp
from .calendars import local_date,session_count,close_time
from .risk import size_entry

DEFAULTS={'initial_cash':100000.,'risk_per_trade':.0025,'max_positions':3,'max_swing_positions':1,
          'fee_rate':.0005,'slippage':.001,'high_risk_slippage':.003,'participation':.01}


def _position_defaults(position):
    position.setdefault('horizon','short');position.setdefault('max_hold_sessions',3)
    position.setdefault('exit_policy',{'type':'risk_partial','target_r':2,'trail':'intraday_3bar'})
    position.setdefault('daily_lows',[]);position.setdefault('session_day',None);position.setdefault('session_low',None)
    position.setdefault('lows',[]);position.setdefault('partial',False);position.setdefault('pending_exit',None)
    position.setdefault('observation_gap',False);position.setdefault('exits',[]);position.setdefault('fees',0.)
    position.setdefault('slippage_cost',0.);position.setdefault('realized',0.)
    return position


def _metrics(trades,failures=()):
    trades=sorted(trades,key=lambda t:t.get('exit_time',''))
    values=[float(t.get('net_pnl',0)) for t in trades];wins=[v for v in values if v>0];losses=[-v for v in values if v<0]
    count=len(values);rate=len(wins)/count if count else None
    if count:
        z=1.96;denom=1+z*z/count;center=(rate+z*z/(2*count))/denom
        margin=z*sqrt((rate*(1-rate)+z*z/(4*count))/count)/denom
        interval=[max(0.,center-margin),min(1.,center+margin)]
    else:interval=[None,None]
    streak=longest=0;equity=peak=100000.;drawdown=0.
    for value in values:
        streak=streak+1 if value<0 else 0;longest=max(longest,streak)
        equity+=value;peak=max(peak,equity);drawdown=max(drawdown,(peak-equity)/peak if peak else 0)
    regimes={}
    for trade in trades:
        regime=trade.get('market_regime') or trade.get('evidence',{}).get('market_regime') or '未分类'
        bucket=regimes.setdefault(regime,{'count':0,'wins':0,'net_pnl':0.})
        bucket['count']+=1;bucket['wins']+=int(trade.get('net_pnl',0)>0);bucket['net_pnl']+=trade.get('net_pnl',0)
    for bucket in regimes.values():bucket['win_rate']=bucket['wins']/bucket['count'] if bucket['count'] else None
    return {'count':count,'net_pnl':sum(values),'win_rate':rate,'win_rate_interval':interval,
            'profit_factor':sum(wins)/sum(losses) if losses else None,
            'payoff':(sum(wins)/len(wins))/(sum(losses)/len(losses)) if wins and losses else None,
            'expectancy':sum(values)/count if count else None,'max_drawdown':drawdown*100,'loss_streak':longest,
            'double_cost_pnl':sum(t.get('double_cost_pnl',t.get('net_pnl',0)) for t in trades),
            'avg_holding':sum(t.get('holding_sessions',0) for t in trades)/count if count else None,
            'failed_fills':len(list(failures)),'enough':count>=30,
            'stability':'样本相对稳定' if count>=100 else '可初步比较' if count>=30 else '样本不足',
            'risk_groups':{g:{'count':sum(t.get('risk_group')==g for t in trades),'net_pnl':sum(t.get('net_pnl',0) for t in trades if t.get('risk_group')==g)} for g in ['normal','smallcap','st']},
            'market_regimes':regimes}


class Simulation:
    def __init__(self,store=None,state=None):
        self.store=store
        self.state=state or (store.get('simulation') if store else None) or {
            'accounts':{m:{'cash':100000.,'positions':{},'trades':[],'curve':[]} for m in ['CN','US']},
            'pending':{},'seen':[],'processed':{},'failures':[],'params':dict(DEFAULTS),'shadow':{}}
        self._migrate()

    def _migrate(self):
        self.state.setdefault('accounts',{m:{'cash':100000.,'positions':{},'trades':[],'curve':[]} for m in ['CN','US']})
        self.state.setdefault('pending',{});self.state.setdefault('seen',[]);self.state.setdefault('processed',{})
        self.state.setdefault('failures',[]);self.state.setdefault('shadow',{})
        params=dict(DEFAULTS);params.update(self.state.get('params',{}));self.state['params']=params
        for account in self.state['accounts'].values():
            account.setdefault('cash',100000.);account.setdefault('positions',{});account.setdefault('trades',[]);account.setdefault('curve',[])
            for position in account['positions'].values():_position_defaults(position)
        shadow=self.state['shadow'];shadow.setdefault('pending',{});shadow.setdefault('seen',[]);shadow.setdefault('processed',{})
        shadow.setdefault('books',{});shadow.setdefault('failures',[])
        for book in shadow['books'].values():
            book.setdefault('positions',{});book.setdefault('trades',[]);book.setdefault('curve',[])
            for position in book['positions'].values():_position_defaults(position)

    def save(self):
        if self.store:self.store.set('simulation',self.state)

    def fail(self,symbol,reason,t,strategy=None,version=None,ledger='portfolio'):
        item={'symbol':symbol,'reason':reason,'time':t.isoformat(),'strategy':strategy,'version':version,'ledger':ledger}
        target=self.state['failures'] if ledger=='portfolio' else self.state['shadow']['failures']
        if not target or target[-1]!=item:target.append(item);del target[:-500]

    def queue(self,signal):
        if signal.id in self.state['seen']:return
        self.state['seen'].append(signal.id);self.state['pending'][signal.id]=signal.dump();self.save()

    def queue_shadow(self,signal):
        shadow=self.state['shadow']
        if signal.id in shadow['seen']:return
        shadow['seen'].append(signal.id);shadow['pending'][signal.id]=signal.dump();self.save()

    def cancel_pending(self,symbol,reason):
        for pending,ledger in [(self.state['pending'],'portfolio'),(self.state['shadow']['pending'],'shadow')]:
            for sid,signal in list(pending.items()):
                if signal['symbol']==symbol:
                    self.fail(symbol,reason,now(),signal.get('strategy'),signal.get('version'),ledger);del pending[sid]
        self.save()

    def cancel_strategy(self,strategy,market,reason):
        for pending,ledger in [(self.state['pending'],'portfolio'),(self.state['shadow']['pending'],'shadow')]:
            for sid,signal in list(pending.items()):
                if signal.get('strategy')==strategy and symbol_market(signal['symbol'])==market:
                    self.fail(signal['symbol'],reason,now(),strategy,signal.get('version'),ledger);del pending[sid]
        self.save()

    def mark_unobservable(self,symbol,reason):
        for account in self.state['accounts'].values():
            if symbol in account['positions']:
                account['positions'][symbol]['observation_gap']=True;account['positions'][symbol]['pending_exit']=reason
        for book in self.state['shadow']['books'].values():
            for position in book['positions'].values():
                if position['symbol']==symbol:position['observation_gap']=True;position['pending_exit']=reason
        self.cancel_pending(symbol,reason)

    def process(self,bar,allow_entries=True):
        if not bar.final or not bar.valid():return
        marker=f'{bar.symbol}|{bar.source}';last=self.state['processed'].get(marker)
        if last and bar.start<=stamp(last):return
        self.state['processed'][marker]=bar.start.isoformat();market=symbol_market(bar.symbol);account=self.state['accounts'][market]
        position=account['positions'].get(bar.symbol)
        if position and position['source']==bar.source:self.exit_position(account,position,bar,market,bar.symbol)
        for sid,data in list(self.state['pending'].items()):
            signal=Signal.load(data)
            if signal.symbol!=bar.symbol or signal.source!=bar.source or bar.start<signal.time:continue
            del self.state['pending'][sid]
            if not allow_entries or bar.start!=signal.time:self.fail(signal.symbol,'错过下一根K线／信号暂停，取消买入',bar.start,signal.strategy,signal.version);continue
            if bar.symbol in account['positions']:self.fail(signal.symbol,'已有该股模拟持仓，不重复买入',bar.start,signal.strategy,signal.version);continue
            self.enter(account,signal,bar,market)
        self._mark_portfolio(account,bar);self.process_shadow(bar,allow_entries);self.save()

    def _entry_checks(self,signal,bar,market,ledger):
        cfg=self.state['params']
        if bar.halted or bar.volume<=0 or (bar.limit_up is not None and bar.low>=bar.limit_up-.00001):
            self.fail(signal.symbol,'停牌、无成交或封涨停，未成交',bar.start,signal.strategy,signal.version,ledger);return None
        if bar.open<=signal.stop:
            self.fail(signal.symbol,'开盘已越过止损，取消买入',bar.start,signal.strategy,signal.version,ledger);return None
        approval=signal.evidence.get('approval',{});slip=cfg['high_risk_slippage'] if signal.risk_group!='normal' else cfg['slippage']
        price=bar.open*(1+slip)
        if signal.version!='实验 1.0.0':
            if not approval or not bar.start<=stamp(approval['time'])<bar.end:
                self.fail(signal.symbol,'缺少下一根期间通过现价与盘口检查的计划',bar.start,signal.strategy,signal.version,ledger);return None
            price=approval['price']
        no_chase=signal.trigger+float(signal.evidence.get('no_chase_risk_fraction',.25))*(signal.trigger-signal.stop)
        if price>no_chase:
            self.fail(signal.symbol,'跳空／滑点超过不追价上限',bar.start,signal.strategy,signal.version,ledger);return None
        if price>bar.high or (bar.limit_up and price>bar.limit_up):
            self.fail(signal.symbol,'假设成交价超出K线／涨停价，未成交',bar.start,signal.strategy,signal.version,ledger);return None
        return approval,slip,price

    def enter(self,account,signal,bar,market):
        checked=self._entry_checks(signal,bar,market,'portfolio')
        if not checked:return
        approval,slip,price=checked;cfg=self.state['params']
        if len(account['positions'])>=cfg['max_positions']:
            self.fail(signal.symbol,'已达三个持仓上限',bar.start,signal.strategy,signal.version);return
        if signal.evidence.get('horizon')=='swing' and sum(p.get('horizon')=='swing' for p in account['positions'].values())>=cfg['max_swing_positions']:
            self.fail(signal.symbol,'波段持仓名额已占用',bar.start,signal.strategy,signal.version);return
        if signal.risk_group!='normal' and any(p['risk_group']!='normal' for p in account['positions'].values()):
            self.fail(signal.symbol,'高波动／ST持仓名额已占用',bar.start,signal.strategy,signal.version);return
        lot=100 if market=='CN' else 1;unit=price-signal.stop+cfg['fee_rate']*(price+signal.stop)+signal.stop*slip
        equity=account['cash']+sum(p['remaining']*p['mark'] for p in account['positions'].values())
        qty=floor(min(equity*cfg['risk_per_trade']/unit,account['cash']/(price*(1+cfg['fee_rate'])),bar.volume*cfg['participation'])/lot)*lot
        sizing=None
        if approval:
            sizing=size_entry(signal,price,account,cfg,min(approval['qty'],bar.volume*cfg['participation']))
            if not sizing['ok']:self.fail(signal.symbol,sizing['reason'],bar.start,signal.strategy,signal.version);return
            qty=sizing['qty']
        if qty<lot:self.fail(signal.symbol,'风险预算、现金或成交量不足一手',bar.start,signal.strategy,signal.version);return
        fee=qty*price*cfg['fee_rate'];account['cash']-=qty*price+fee
        position=self._new_position(signal,bar,price,qty,sizing['planned_risk'] if sizing else qty*unit,fee,qty*(price-bar.open))
        account['positions'][signal.symbol]=position;self.exit_position(account,position,bar,market,signal.symbol)

    def _new_position(self,signal,bar,price,qty,planned_risk,fee,slippage_cost):
        policy=dict(signal.evidence.get('exit_policy') or {'type':'risk_partial','target_r':2,'trail':'intraday_3bar'})
        stop=signal.stop
        if policy.get('type')=='orb_fixed' and signal.evidence.get('opening_range'):
            width=float(signal.evidence['opening_range']);stop=price-float(policy.get('stop_range',.5))*width
            target=price+float(policy.get('target_range',.75))*width
        else:target=float(signal.evidence.get('target_price',price+float(policy.get('target_r',2))*(price-stop)))
        return _position_defaults({'id':signal.id,'symbol':signal.symbol,'source':signal.source,'strategy':signal.strategy,
            'risk_group':signal.risk_group,'version':signal.version,'entry':price,
            'entry_time':signal.evidence.get('approval',{}).get('time',bar.start.isoformat()),'qty':qty,'remaining':qty,
            'stop':stop,'planned_risk':planned_risk,'initial_stop':stop,'target':target,
            'fees':fee,'slippage_cost':slippage_cost,'realized':0.,'mark':price,'exits':[],
            'pending_exit':None,'observation_gap':False,'horizon':signal.evidence.get('horizon','short'),
            'max_hold_sessions':int(signal.evidence.get('max_hold_sessions',3)),'exit_policy':policy,
            'market_regime':signal.evidence.get('market_regime','未分类'),'evidence':signal.evidence})

    def process_shadow(self,bar,allow_entries=True):
        shadow=self.state['shadow'];marker=f'{bar.symbol}|{bar.source}';last=shadow['processed'].get(marker)
        if last and bar.start<=stamp(last):return
        shadow['processed'][marker]=bar.start.isoformat();market=symbol_market(bar.symbol)
        for book in shadow['books'].values():
            for key,position in list(book['positions'].items()):
                if position['symbol']==bar.symbol and position['source']==bar.source:self.exit_position(book,position,bar,market,key)
        for sid,data in list(shadow['pending'].items()):
            signal=Signal.load(data)
            if signal.symbol!=bar.symbol or signal.source!=bar.source or bar.start<signal.time:continue
            del shadow['pending'][sid]
            if not allow_entries or bar.start!=signal.time:self.fail(signal.symbol,'错过下一根K线／信号暂停，取消影子买入',bar.start,signal.strategy,signal.version,'shadow');continue
            book_id=f'{market}|{signal.strategy}|{signal.version}';book=shadow['books'].setdefault(book_id,{'market':market,'strategy':signal.strategy,'version':signal.version,'positions':{},'trades':[],'curve':[]})
            if any(p['symbol']==signal.symbol for p in book['positions'].values()):
                self.fail(signal.symbol,'同策略已有该股影子持仓',bar.start,signal.strategy,signal.version,'shadow');continue
            checked=self._entry_checks(signal,bar,market,'shadow')
            if not checked:continue
            approval,slip,price=checked;lot=100 if market=='CN' else 1;capacity=floor(bar.volume*self.state['params']['participation']/lot)*lot
            qty=min(int(approval.get('qty',0)),capacity) if approval else 0;qty=floor(qty/lot)*lot
            if qty<lot:self.fail(signal.symbol,'影子成交量容量不足',bar.start,signal.strategy,signal.version,'shadow');continue
            fee=qty*price*self.state['params']['fee_rate'];position=self._new_position(signal,bar,price,qty,approval.get('planned_risk',qty*(price-signal.stop)),fee,qty*(price-bar.open))
            book['positions'][sid]=position;self.exit_position(book,position,bar,market,sid)
        for book in shadow['books'].values():self._mark_shadow(book,bar)

    def _roll_daily_trail(self,position,bar,market):
        if not position.get('partial') or position.get('exit_policy',{}).get('trail')!='daily_3low':return
        day=str(local_date(bar.start,market));prior=position.get('session_day')
        if prior and prior!=day and position.get('session_low') is not None:
            position['daily_lows'].append(position['session_low']);position['daily_lows']=position['daily_lows'][-3:]
            if len(position['daily_lows'])==3:position['stop']=max(position['stop'],min(position['daily_lows']))
            position['session_low']=bar.low
        else:position['session_low']=bar.low if position.get('session_low') is None else min(position['session_low'],bar.low)
        position['session_day']=day

    def exit_position(self,account,position,bar,market,key):
        position=_position_defaults(position);position['mark']=bar.close;self._roll_daily_trail(position,bar,market)
        day=local_date(bar.start,market);entry_day=local_date(stamp(position['entry_time']),market);held=session_count(entry_day,day,market)
        policy=position.get('exit_policy',{});kind=policy.get('type','risk_partial');maximum=int(position.get('max_hold_sessions',3))
        due=held>maximum or (held==maximum and bar.end>=close_time(day,market))
        if kind=='orb_fixed':due=bar.end>=close_time(day,market)-timedelta(minutes=int(policy.get('flat_minutes_before_close',10)))
        reason=position.get('pending_exit');exit_price=bar.open if reason else None;qty=position['remaining']
        if bar.low<=position['stop']:reason='止损／跟踪止损';exit_price=min(bar.open,position['stop'])
        elif bar.high>=position['target'] and not position.get('partial'):
            reason='固定区间止盈' if kind=='orb_fixed' else '2R分批止盈';exit_price=max(bar.open,position['target'])
            if kind!='orb_fixed':
                lot=100 if market=='CN' else 1;qty=floor(position['remaining']/2/lot)*lot or position['remaining']
        elif due and not reason:reason='日内收盘前退出' if kind=='orb_fixed' else '第三交易日到期' if maximum==3 else f'第{maximum}交易日到期';exit_price=bar.close
        if reason:
            blocked=None
            if market=='CN' and day<=entry_day:blocked='T+1，当日新买股份不可卖'
            elif bar.halted or bar.volume<=0:blocked='停牌／无成交，待恢复'
            elif bar.limit_down is not None and bar.high<=bar.limit_down+.00001:blocked='封跌停，无法确认卖出'
            if blocked:
                if reason not in ('2R分批止盈','固定区间止盈'):position['pending_exit']=reason
                position['status']=blocked
            else:
                cfg=self.state['params'];lot=100 if market=='CN' else 1;capacity=floor(bar.volume*cfg['participation']/lot)*lot;qty=min(qty,capacity)
                if qty<=0:
                    if reason not in ('2R分批止盈','固定区间止盈'):position['pending_exit']=reason
                    position['status']='本根成交量不足，待退出'
                else:
                    slip=cfg['high_risk_slippage'] if position['risk_group']!='normal' else cfg['slippage']
                    execution=max(bar.low,exit_price*(1-slip));execution=max(bar.limit_down,execution) if bar.limit_down else execution
                    fee=qty*execution*cfg['fee_rate'];account['cash']=account.get('cash',0)+qty*execution-fee
                    position['remaining']-=qty;position['realized']+=qty*(execution-position['entry']);position['fees']+=fee
                    position['slippage_cost']+=qty*(exit_price-execution);position['exits'].append({'time':bar.end.isoformat(),'price':execution,'qty':qty,'reason':reason})
                    if reason=='2R分批止盈':
                        position['partial']=True;position['pending_exit']=None;position['lows']=[];position['daily_lows']=[]
                        position['session_day']=str(day);position['session_low']=bar.low;position['trailing_after']=bar.end.isoformat()
                    elif position['remaining']:position['pending_exit']=reason
                    else:position['pending_exit']=None
                    position['status']='持有' if position['pending_exit'] is None else '待继续退出'
                    if position['remaining']==0:
                        position['net_pnl']=position['realized']-position['fees'];position['gross_before_costs']=position['net_pnl']+position['fees']+position['slippage_cost']
                        position['double_cost_pnl']=position['net_pnl']-position['fees']-position['slippage_cost'];position['exit_time']=bar.end.isoformat()
                        position['holding_sessions']=held;account['trades'].append(position.copy());del account['positions'][key];return
        if position.get('partial') and policy.get('trail')!='daily_3low' and bar.start>=stamp(position.get('trailing_after',position['entry_time'])):
            position['lows'].append(bar.low);position['lows']=position['lows'][-3:]
            if len(position['lows'])==3:position['stop']=max(position['stop'],min(position['lows']))

    def _mark_portfolio(self,account,bar):
        equity=account['cash']+sum(p['remaining']*p['mark'] for p in account['positions'].values())
        point={'time':bar.end.isoformat(),'equity':round(equity,2)};curve=account['curve']
        if curve and curve[-1]['time']==point['time']:curve[-1]=point
        else:curve.append(point)

    def _mark_shadow(self,book,bar):
        if not any(p['symbol']==bar.symbol for p in book['positions'].values()):return
        closed=sum(t.get('net_pnl',0) for t in book['trades']);open_pnl=sum((p['mark']-p['entry'])*p['remaining']-p['fees'] for p in book['positions'].values())
        point={'time':bar.end.isoformat(),'equity':round(100000+closed+open_pnl,2)};curve=book['curve']
        if curve and curve[-1]['time']==point['time']:curve[-1]=point
        else:curve.append(point)

    def summary(self,version=None):
        result={}
        for market,account in self.state['accounts'].items():
            equity=account['cash']+sum(p['remaining']*p['mark'] for p in account['positions'].values());peak=100000.;dd=0.
            for point in sorted(account['curve'],key=lambda x:x['time']):peak=max(peak,point['equity']);dd=max(dd,(peak-point['equity'])/peak)
            ids=['breakout','pullback','trend_pullback','volatility_breakout','trend_rsi_pullback','vcp_swing','orb20_us']
            groups={}
            for strategy in ids:
                trades=[t for t in account['trades'] if t.get('strategy')==strategy and (version is None or t.get('version','实验 1.0.0')==version)]
                failures=[f for f in self.state['failures'] if f.get('strategy')==strategy and symbol_market(f.get('symbol',''))==market
                          and (version is None or f.get('version')==version)]
                groups[strategy]=_metrics(trades,failures)
            horizons={h:_metrics([t for t in account['trades'] if t.get('horizon','short')==h]) for h in ['intraday','short','swing']}
            result[market]={'cash':account['cash'],'equity':equity,'return_pct':(equity/100000-1)*100,'max_drawdown':dd*100,
                'positions':list(account['positions'].values()),'trades':account['trades'][-100:],'strategies':groups,
                'by_horizon':horizons,'curve':account['curve'][-500:]}
        return result

    def shadow_summary(self,market=None):
        result={m:{} for m in ['CN','US'] if market in (None,m)}
        for book in self.state['shadow']['books'].values():
            if market and book['market']!=market:continue
            failures=[f for f in self.state['shadow']['failures'] if f.get('strategy')==book['strategy'] and f.get('version')==book['version']
                      and symbol_market(f.get('symbol',''))==book['market']]
            result[book['market']].setdefault(book['strategy'],{})[book['version']]={**_metrics(book['trades'],failures),'positions':list(book['positions'].values())}
        return result

    def performance(self,market,strategy,version):
        portfolio=self.summary(version)[market]['strategies'].get(strategy,_metrics([]))
        shadow=self.shadow_summary(market).get(market,{}).get(strategy,{}).get(version,_metrics([]))
        return {**shadow,'shadow':shadow,'portfolio':portfolio}
