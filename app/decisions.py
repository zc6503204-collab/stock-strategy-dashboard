"""Current actionable decisions, distinct from immutable historical signals."""
from collections import defaultdict
from copy import deepcopy
from datetime import timedelta
from .models import Signal,stamp,symbol_market
from .calendars import is_open,local_date,calendar,close_time,session_count
from .risk import size_entry
from .research import VERSION

def fresh_quote(q,t,source=None):
    return bool(q and q.market_time and q.quality=='realtime' and (not source or q.source==source)
                and 0<=(t-q.market_time).total_seconds()<=30 and 0<=(t-q.received_at).total_seconds()<=30)

def book_ok(q,t,side='buy'):
    return bool(q and q.depth_time and 0<=(t-q.depth_time).total_seconds()<=15
                and q.bid and q.ask and 0<q.bid<=q.ask and (q.ask_size if side=='buy' else q.bid_size)
                and (q.ask_size if side=='buy' else q.bid_size)>0)

def check(signal,q,validation,account,cfg,t,active_versions=None):
    valid_minutes=int(signal.evidence.get('signal_minutes',5))
    base={'signal_id':signal.id,'symbol':signal.symbol,'strategy':signal.strategy,'version':signal.version,
          'source':signal.source,'signal_time':signal.time.isoformat(),'valid_until':(signal.time+timedelta(minutes=valid_minutes)).isoformat(),
          'entry_min':signal.trigger,'entry_max':signal.trigger+.25*(signal.trigger-signal.stop),'stop':signal.stop,
          'target':signal.evidence.get('target_price',signal.trigger+2*(signal.trigger-signal.stop)),'risk_group':signal.risk_group,
          'horizon':signal.evidence.get('horizon','short'),'max_hold_sessions':signal.evidence.get('max_hold_sessions',3),
          'exit_policy':signal.evidence.get('exit_policy',{}),'research_reference':signal.evidence.get('research_reference'),
          'relative_volume':signal.evidence.get('relative_volume',0),'relative_strength':signal.evidence.get('relative_strength',0),
          'forward_quality':signal.evidence.get('forward_quality',0),
          'state':'wait','action':'继续等待','qty':0,'reason':'等待当前交易条件'}
    def reject(reason,state='wait'):
        return {**base,'state':state,'action':'暂不买入' if state=='avoid' else '继续等待','reason':reason}
    m=symbol_market(signal.symbol)
    expected=(active_versions or {}).get((m,signal.strategy),VERSION)
    if signal.version!=expected:return reject('旧版历史信号，不作为当前买入建议','avoid')
    if not signal.time<=t<signal.time+timedelta(minutes=valid_minutes):return reject('买点已过期，等待新信号','avoid')
    if not is_open(t,m):return reject('当前不是正常交易时段')
    if not validation.get('ready') or not validation.get('eligible') or validation.get('source')!=signal.source:
        return reject('行情数据或交易范围尚未通过核验')
    if not fresh_quote(q,t,signal.source):return reject('等待已验证的同源新报价')
    base.update(current_price=q.price,quote_time=q.market_time.isoformat(),depth_time=q.depth_time.isoformat() if q.depth_time else None)
    if q.trade_status!='Normal':return reject('停牌或交易状态尚未确认','avoid')
    if not book_ok(q,t):return reject('等待15秒内有效买卖盘')
    if q.price<signal.trigger or q.ask<signal.trigger:return reject('价格回到触发价以下，等待重新确认')
    if q.price>base['entry_max'] or q.ask>base['entry_max']:return reject('已超过不追价上限','avoid')
    if q.limit_up and (q.price>=q.limit_up or q.ask>=q.limit_up):return reject('触及涨停，不能确认可买入','avoid')
    slip=cfg['high_risk_slippage'] if signal.risk_group!='normal' else cfg['slippage']
    price=q.ask*(1+slip)
    if price>base['entry_max']:return reject('计入滑点后超过不追价上限','avoid')
    liquidity=min(float(signal.evidence.get('bar',{}).get('volume',0))*cfg['participation'],q.ask_size)
    sizing=size_entry(signal,price,account,cfg,liquidity)
    if not sizing['ok']:return reject(sizing['reason'])
    return {**base,**sizing,'state':'buy','action':'可考虑买入','reason':'趋势、放量、盘中确认、现价、盘口和资金均通过',
            'earliest_sell':earliest_sell(t,m)}

def earliest_sell(t,market):
    d=local_date(t,market)
    if market=='US':return str(d)
    try:return str(calendar(market).next_session(str(d)).date())
    except Exception:return '交易日历待更新'

def rank_key(r):
    return (-r.get('forward_quality',0),-r.get('relative_volume',0),-r.get('relative_strength',0),r.get('cost_ratio',999),r['symbol'],r['strategy'])


def shadow_recommend(signals,quotes,validation,simulation,t,active_versions=None):
    """Evaluate every strategy signal against execution checks without portfolio competition."""
    latest={}
    for raw in signals:
        key=(raw['symbol'],raw['strategy'])
        if key not in latest or stamp(raw['time'])>stamp(latest[key]['time']):latest[key]=raw
    cfg=simulation['params'];rows=[]
    for raw in latest.values():
        signal=Signal.load(raw);market=symbol_market(signal.symbol)
        expected=(active_versions or {}).get((market,signal.strategy),VERSION)
        if signal.version!=expected:continue
        account={'cash':float(cfg.get('initial_cash',100000.)),'positions':{}}
        rows.append(check(signal,quotes.get(signal.symbol),validation.get(signal.symbol,{}),account,cfg,t,active_versions))
    return sorted(rows,key=rank_key)

def recommend(signals,quotes,validation,simulation,t,real_holdings=(),active_versions=None):
    cfg=simulation['params'];accounts=deepcopy(simulation['accounts']);rows=[]
    real_symbols={p['symbol'] for p in real_holdings if p.get('quantity',0)>0}
    real_high_risk={p['symbol'] for p in real_holdings
                    if p.get('quantity',0)>0 and p.get('risk_group') not in (None,'normal','pending')}
    latest={}
    for raw in signals:
        key=(raw['symbol'],raw['strategy'])
        if key not in latest or stamp(raw['time'])>stamp(latest[key]['time']):latest[key]=raw
    reserved={}
    for pending in simulation.get('pending',{}).values():
        approval=pending.get('evidence',{}).get('approval')
        if approval:
            a=accounts[symbol_market(pending['symbol'])]
            if pending['symbol'] not in a['positions']:
                a['cash']-=approval['cash_required']
                a['positions'][pending['symbol']]={'remaining':approval['qty'],'mark':approval['price'],'risk_group':pending['risk_group']}
                reserved[pending['id']]=approval
    def available(s):
        a=deepcopy(accounts[symbol_market(s.symbol)])
        if s.id in reserved:
            a['cash']+=reserved[s.id]['cash_required'];a['positions'].pop(s.symbol,None)
        return a
    for raw in latest.values():
        s=Signal.load(raw)
        expected=(active_versions or {}).get((symbol_market(s.symbol),s.strategy),VERSION)
        if s.version!=expected:continue
        if s.time>t or t-s.time>timedelta(hours=1):continue
        rows.append(check(s,quotes.get(s.symbol),validation.get(s.symbol,{}),available(s),cfg,t,active_versions))
    rows.sort(key=rank_key);seen=set();counts=defaultdict(int)
    output=[];lookup={r['id']:r for r in signals}
    for row in rows:
        s=Signal.load(lookup[row['signal_id']]);m=symbol_market(s.symbol)
        if s.symbol in seen:
            existing=next((x for x in output if x['symbol']==s.symbol and x['state']=='buy'),None)
            if existing is None:continue
            if row['state']=='buy' and s.strategy not in existing['strategies']:existing['strategies'].append(s.strategy)
            continue
        row=check(s,quotes.get(s.symbol),validation.get(s.symbol,{}),available(s),cfg,t,active_versions)
        row['strategies']=[s.strategy]
        if row['state']=='buy' and s.symbol in real_symbols:
            row.update(state='avoid',action='暂不买入',qty=0,
                       reason='已有真实持仓；如需加仓请单独制定加仓计划')
        elif row['state']=='buy' and s.risk_group!='normal' and real_high_risk:
            row.update(state='avoid',action='暂不买入',qty=0,
                       reason='真实高风险持仓名额已占用')
        if row['state']=='buy':
            key=(m,s.strategy)
            if counts[key]>=3:continue
            row['rank']=counts[key]+1;counts[key]+=1
            seen.add(s.symbol)
            # Reserve capacity in the same global ordering used by paper admission.
            if s.id not in reserved:
                accounts[m]['cash']-=row['cash_required']
                accounts[m]['positions'][s.symbol]={'risk_group':s.risk_group,'remaining':row['qty'],'mark':row['price']}
        output.append(row)
    return output

def holding_plan(p,q,t,validation):
    m=symbol_market(p['symbol']);d=local_date(t,m);entryday=local_date(stamp(p['entry_time']),m)
    eligible_sell=m=='US' or d>entryday
    fresh=fresh_quote(q,t,p['source']);price=q.price if fresh else p['mark']
    action='继续持有';reason='未触及退出条件';event=None;qty=p['remaining']
    try:
        sessions=session_count(entryday,d,m);maximum=int(p.get('max_hold_sessions',3));policy=p.get('exit_policy',{})
        due=(t>=close_time(d,m)-timedelta(minutes=int(policy.get('flat_minutes_before_close',10)))) if policy.get('type')=='orb_fixed' else sessions>=maximum and (sessions>maximum or t>=close_time(d,m)-timedelta(minutes=10))
    except Exception:due=False
    if p.get('pending_exit') or (fresh and price<=p['stop']):
        action='止损退出';reason=p.get('pending_exit') or ('跟踪保护价已触及' if p.get('partial') else '初始止损价已触及')
        event='trailing_stop' if p.get('partial') else 'stop'
        if p.get('pending_exit') and '止损' not in p['pending_exit'] and '保护价' not in p['pending_exit']:
            event='time_exit' if '到期' in p['pending_exit'] or '第三' in p['pending_exit'] else 'pending_exit'
            action='到期退出' if event=='time_exit' else '待退出'
    elif due:
        action='到期退出';reason='日内策略接近收盘，按计划退出' if p.get('exit_policy',{}).get('type')=='orb_fixed' else f"已到第{p.get('max_hold_sessions',3)}交易日退出准备时间";event='time_exit'
    elif fresh and price>=p['target'] and not p.get('partial'):
        fixed=p.get('exit_policy',{}).get('type')=='orb_fixed';action='止盈退出' if fixed else '减仓止盈'
        reason='达到开盘区间固定止盈目标' if fixed else '达到两倍初始价格风险的止盈目标';event='take_profit'
        if not fixed:
            lot=100 if m=='CN' else 1;qty=(p['remaining']//(2*lot))*lot or p['remaining']
    blocked=None
    if event and not eligible_sell:blocked='T+1：当日新买股份不能卖出'
    elif event and not is_open(t,m):blocked='当前不在正常交易时段，等待下一可卖时段'
    elif event and (not fresh or not book_ok(q,t,'sell')):blocked='退出条件已触发，等待有效报价和买卖盘'
    elif event and (q.trade_status!='Normal' or (q.limit_down and q.bid<=q.limit_down)):blocked='停牌或跌停，无法确认卖出'
    if blocked:action='暂时无法卖出';reason=blocked+'；'+reason
    if not fresh and not event:reason='报价待更新，不能据旧价判断继续持有是否安全'
    return {'position_id':p['id'],'symbol':p['symbol'],'strategy':p['strategy'],'version':p.get('version','实验 1.0.0'),
            'horizon':p.get('horizon','short'),'max_hold_sessions':p.get('max_hold_sessions',3),'exit_policy':p.get('exit_policy',{}),
            'source':p['source'],'risk_group':p['risk_group'],'action':action,'reason':reason,'event':event,
            'qty':qty if event else 0,'remaining':p['remaining'],'entry':p['entry'],'price':price,
            'initial_stop':p['initial_stop'],'stop':p['stop'],'target':p['target'],
            'price_risk':p['entry']-p['initial_stop'],'earliest_sell':earliest_sell(stamp(p['entry_time']),m),
            'quote_time':q.market_time.isoformat() if q and q.market_time else None,'quote_fresh':fresh,
            'blocked':bool(blocked),'data_ready':validation.get('ready',False)}
