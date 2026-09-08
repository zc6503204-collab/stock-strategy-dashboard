"""Explainable daily setup ranking, separate from confirmed 5-minute entry signals."""
from .calendars import local_date
from .models import symbol_market

VERSION='选股 1.0'

def _ema(values,period):
    alpha=2/(period+1);result=values[0]
    for value in values[1:]:result=alpha*value+(1-alpha)*result
    return result

def _rsi(values,period=14):
    if len(values)<period+1:return None
    gains=[max(0.,values[i]-values[i-1]) for i in range(1,len(values))]
    losses=[max(0.,values[i-1]-values[i]) for i in range(1,len(values))]
    gain=sum(gains[:period])/period;loss=sum(losses[:period])/period
    for i in range(period,len(gains)):
        gain=(gain*(period-1)+gains[i])/period;loss=(loss*(period-1)+losses[i])/period
    return 100. if loss==0 and gain else 50. if loss==0 else 100-100/(1+gain/loss)

def candidate_pool(current,retained,preferred,limit=80):
    ordered=[r for r in retained if r['symbol'] in preferred]+retained[:20]+current+retained
    unique={}
    for row in ordered:unique.setdefault(row['symbol'],row)
    return list(unique.values())[:limit]

def evaluate(row,bars):
    if len(bars)<65:raise ValueError('不足65根完整日线')
    closes=[b.close for b in bars];b=bars[-1]
    ma=lambda n:sum(closes[-n:])/n
    m5,m10,m20,m60=(ma(n) for n in [5,10,20,60])
    old20=sum(closes[-25:-5])/20
    amount=sum(x.turnover or 0 for x in bars[-20:])/20
    atr=sum(max(x.high-x.low,abs(x.high-bars[-21+i].close),abs(x.low-bars[-21+i].close)) for i,x in enumerate(bars[-20:]))/20
    atr5=sum(max(x.high-x.low,abs(x.high-bars[-6+i].close),abs(x.low-bars[-6+i].close)) for i,x in enumerate(bars[-5:]))/5
    ceiling=max(x.high for x in bars[-21:-1]);stop=min(x.low for x in bars[-3:])
    avg_volume=sum(x.volume for x in bars[-21:-1])/20
    ratio=b.volume/avg_volume if avg_volume else 0
    distance=(b.close/ceiling-1)*100
    extension=(b.close-m20)/atr if atr else 999
    trend=b.close>m20 and m20>old20
    stacked=m5>m10>m20
    liquid=amount>=(1e8 if symbol_market(b.symbol)=='CN' else 5e6)
    stretched=extension>3 or (b.close/m20-1)>.18
    abnormal=any(abs(closes[i]/closes[i-1]-1)>.45 for i in range(1,len(closes)))
    near=-3<=distance<=3
    ema10,ema20,ema50=(_ema(closes[-120:],n) for n in [10,20,50])
    higher_high_low=bool(len(bars)>=40 and max(x.high for x in bars[-20:])>=max(x.high for x in bars[-40:-20]) and min(x.low for x in bars[-20:])>min(x.low for x in bars[-40:-20]))
    range5=sum(x.high-x.low for x in bars[-5:])/5;range20=sum(x.high-x.low for x in bars[-25:-5])/20
    score=(25 if trend else 0)+(15 if stacked else 0)+(10 if b.close>m60 else 0)+(15 if liquid else 0)+(15 if near else 0)+(10 if 1.2<=ratio<=4 else 0)+(10 if not stretched else 0)
    if abnormal:decision='暂不参与';reason='疑似除权或异常跳变，先核对价格口径'
    elif not liquid:decision='暂不参与';reason='20日平均成交额不足，退出流动性存疑'
    elif not trend:decision='暂不参与';reason='尚未站稳上行的20日均线'
    elif stretched:decision='等确认';reason='离20日均线偏远，等回调，不因涨幅追入'
    elif score>=70:decision='重点观察';reason='趋势向上且成交活跃，等待盘中完整5分钟确认'
    else:decision='等确认';reason='趋势尚可，位置或量能还需改善'
    return {'symbol':row['symbol'],'name':row['name'],'market':symbol_market(b.symbol),
      'score':score,'decision':decision,'reason':reason,'source':b.source,
      'as_of':str(local_date(b.start,symbol_market(b.symbol))),
      'close':b.close,'previous_high':b.high,'breakout_reference':ceiling,'structure_low':stop,
      'ma5':m5,'ma10':m10,'ma20':m20,'ma60':m60,'ma20_rising':m20>old20,
      'trend':trend,'stacked':stacked,'average_turnover':amount,'volume_ratio':ratio,
      'distance_to_high_pct':distance,'extension_atr':extension,'atr_pct':atr/b.close*100,
      'atr5':atr5,'atr20':atr,'atr_contraction':atr5/atr if atr else None,
      'high10':max(x.high for x in bars[-10:]),'high20':ceiling,'high60':max(x.high for x in bars[-60:]),
      'ema10':ema10,'ema20':ema20,'ema50':ema50,'higher_high_low':higher_high_low,
      'range_contraction':range5/range20 if range20 else None,'volume_contraction':sum(x.volume for x in bars[-5:])/5/(sum(x.volume for x in bars[-20:])/20) if sum(x.volume for x in bars[-20:]) else None,
      'rsi14':_rsi(closes,14),'rsi_recent_peak':max((_rsi(closes[:cut],14) for cut in range(len(closes)-5,len(closes)) if _rsi(closes[:cut],14) is not None),default=None),
      'return5_pct':(closes[-1]/closes[-6]-1)*100,
      'risk_group':'st' if symbol_market(b.symbol)=='CN' and row['name'].upper().lstrip('*').startswith('ST') else 'smallcap' if atr/b.close>=.05 or (row.get('market_cap') and row['market_cap']<(1e10 if symbol_market(b.symbol)=='CN' else 2e9)) else 'normal',
      'conditions':{'trend':trend,'stacked_ma':stacked,'above_ma60':b.close>m60,'liquid':liquid,'near_high':near,'volume':1.2<=ratio<=4,'not_extended':not stretched},
      'version':VERSION,'entry_rule':'仅在本地5分钟策略确认后形成计划；日线观察位不是买入委托'}
