"""Long-only paper execution. Conservative intrabar order, T+1, liquidity and persistence."""
from math import floor
from .models import Signal, symbol_market, now, stamp
from .calendars import local_date,session_count,close_time
from .risk import size_entry

DEFAULTS={'initial_cash':100000.,'risk_per_trade':.0025,'max_positions':3,
          'fee_rate':.0005,'slippage':.001,'high_risk_slippage':.003,'participation':.01}

class Simulation:
    def __init__(self,store=None,state=None):
        self.store=store
        self.state=state or (store.get('simulation') if store else None) or {
            'accounts':{m:{'cash':100000.,'positions':{},'trades':[],'curve':[]} for m in ['CN','US']},
            'pending':{},'seen':[],'processed':{},'failures':[],'params':dict(DEFAULTS)}

    def save(self):
        if self.store:self.store.set('simulation',self.state)

    def fail(self,symbol,reason,t):
        item={'symbol':symbol,'reason':reason,'time':t.isoformat()}
        if not self.state['failures'] or self.state['failures'][-1]!=item:
            self.state['failures'].append(item)
            self.state['failures']=self.state['failures'][-500:]

    def queue(self,signal):
        if signal.id in self.state['seen']:return
        self.state['seen'].append(signal.id)
        self.state['pending'][signal.id]=signal.dump()
        self.save()

    def cancel_pending(self,symbol,reason):
        for sid,s in list(self.state['pending'].items()):
            if s['symbol']==symbol:
                self.fail(symbol,reason,now());del self.state['pending'][sid]
        self.save()

    def cancel_strategy(self,strategy,market,reason):
        for sid,s in list(self.state['pending'].items()):
            if s.get('strategy')==strategy and symbol_market(s['symbol'])==market:
                self.fail(s['symbol'],reason,now());del self.state['pending'][sid]
        self.save()

    def mark_unobservable(self,symbol,reason):
        for a in self.state['accounts'].values():
            if symbol in a['positions']:
                a['positions'][symbol]['observation_gap']=True
                a['positions'][symbol]['pending_exit']=reason
        self.cancel_pending(symbol,reason)

    def process(self,bar,allow_entries=True):
        if not bar.final or not bar.valid():return
        marker=f'{bar.symbol}|{bar.source}'
        last=self.state['processed'].get(marker)
        if last and bar.start<=stamp(last):return
        self.state['processed'][marker]=bar.start.isoformat()
        market=symbol_market(bar.symbol);account=self.state['accounts'][market]
        p=account['positions'].get(bar.symbol)
        if p and p['source']==bar.source:self.exit_position(account,p,bar,market)
        for sid,d in list(self.state['pending'].items()):
            s=Signal.load(d)
            if s.symbol!=bar.symbol or s.source!=bar.source or bar.start<s.time:continue
            del self.state['pending'][sid]
            # A next-bar entry does not remain a standing order through a missing bar or overnight.
            if not allow_entries or bar.start!=s.time:
                self.fail(s.symbol,'错过下一根K线／信号暂停，取消买入',bar.start);continue
            if bar.symbol in account['positions']:
                self.fail(s.symbol,'已有该股模拟持仓，不重复买入',bar.start);continue
            self.enter(account,s,bar,market)
        self.mark(account,bar)
        self.save()

    def enter(self,a,s,b,market):
        cfg=self.state['params']
        if b.halted or b.volume<=0 or (b.limit_up is not None and b.low>=b.limit_up-.00001):
            self.fail(s.symbol,'停牌、无成交或封涨停，未成交',b.start);return
        if b.open<=s.stop:
            self.fail(s.symbol,'开盘已越过止损，取消买入',b.start);return
        slip=cfg['high_risk_slippage'] if s.risk_group!='normal' else cfg['slippage']
        price=b.open*(1+slip)
        approval=s.evidence.get('approval',{})
        if s.version!='实验 1.0.0':
            if not approval or not b.start<=stamp(approval['time'])<b.end:
                self.fail(s.symbol,'缺少下一根期间通过现价与盘口检查的计划',b.start);return
            price=approval['price']
        if price>s.trigger+.25*(s.trigger-s.stop):
            self.fail(s.symbol,'跳空／滑点超过不追价上限',b.start);return
        if price>b.high or (b.limit_up and price>b.limit_up):
            self.fail(s.symbol,'假设成交价超出K线／涨停价，未成交',b.start);return
        if len(a['positions'])>=cfg['max_positions']:
            self.fail(s.symbol,'已达三个持仓上限',b.start);return
        if s.risk_group!='normal' and any(p['risk_group']!='normal' for p in a['positions'].values()):
            self.fail(s.symbol,'高波动／ST持仓名额已占用',b.start);return
        equity=a['cash']+sum(p['remaining']*p['mark'] for p in a['positions'].values())
        lot=100 if market=='CN' else 1
        # Budget includes estimated round-trip fees and exit slippage.
        unit_risk=price-s.stop+cfg['fee_rate']*(price+s.stop)+s.stop*slip
        qty=floor(min(equity*cfg['risk_per_trade']/unit_risk,a['cash']/(price*(1+cfg['fee_rate'])),b.volume*cfg['participation'])/lot)*lot
        sizing=None
        if approval:
            capacity=min(s.evidence['approval']['qty'],b.volume*cfg['participation'])
            sizing=size_entry(s,price,a,cfg,capacity)
            if not sizing['ok']:self.fail(s.symbol,sizing['reason'],b.start);return
            qty=sizing['qty']
        if qty<lot:
            self.fail(s.symbol,'风险预算、现金或成交量不足一手',b.start);return
        fee=qty*price*cfg['fee_rate']
        a['cash']-=qty*price+fee
        p={'id':s.id,'symbol':s.symbol,'source':s.source,'strategy':s.strategy,'risk_group':s.risk_group,'version':s.version,
           'entry':price,'entry_time':approval['time'] if approval else b.start.isoformat(),'qty':qty,'remaining':qty,'stop':s.stop,
           'planned_risk':sizing['planned_risk'] if sizing else qty*unit_risk,
           'initial_stop':s.stop,'target':price+2*(price-s.stop),'partial':False,'lows':[],
           'fees':fee,'slippage_cost':qty*(price-b.open),'realized':0.,'mark':price,'exits':[],
           'pending_exit':None,'observation_gap':False}
        a['positions'][s.symbol]=p
        self.exit_position(a,p,b,market)

    def exit_position(self,a,p,b,market):
        p['mark']=b.close
        day=local_date(b.start,market);entryday=local_date(stamp(p['entry_time']),market)
        held_sessions=session_count(entryday,day,market)
        due=held_sessions>3 or (held_sessions==3 and b.end>=close_time(day,market))
        reason=p.get('pending_exit')
        exit_price=b.open if reason else None
        qty=p['remaining']
        if b.low<=p['stop']:
            reason='止损／跟踪止损';exit_price=min(b.open,p['stop'])
        elif due and not reason:
            reason='第三交易日到期';exit_price=b.open if held_sessions>3 else b.close
        elif not reason and b.high>=p['target'] and not p['partial']:
            reason='2R分批止盈';exit_price=max(b.open,p['target'])
            lot=100 if market=='CN' else 1
            qty=floor(p['remaining']/2/lot)*lot
            if qty==0:qty=p['remaining']  # One-lot position cannot be split into valid buy lots.
        if reason:
            blocked=None
            if market=='CN' and day<=entryday:blocked='T+1，当日新买股份不可卖'
            elif b.halted or b.volume<=0:blocked='停牌／无成交，待恢复'
            elif b.limit_down is not None and b.high<=b.limit_down+.00001:blocked='封跌停，无法确认卖出'
            if blocked:
                # Profit taking is not a permanent exit instruction; loss/time exits remain pending.
                if reason!='2R分批止盈':p['pending_exit']=reason
                p['status']=blocked
            else:
                cfg=self.state['params'];lot=100 if market=='CN' else 1
                capacity=floor(b.volume*cfg['participation']/lot)*lot
                qty=min(qty,capacity)
                if qty<=0:
                    if reason!='2R分批止盈':p['pending_exit']=reason
                    p['status']='本根成交量不足，待退出'
                else:
                    slip=cfg['high_risk_slippage'] if p['risk_group']!='normal' else cfg['slippage']
                    execution=max(b.low,exit_price*(1-slip))
                    if b.limit_down:execution=max(b.limit_down,execution)
                    fee=qty*execution*cfg['fee_rate']
                    a['cash']+=qty*execution-fee;p['remaining']-=qty
                    p['realized']+=qty*(execution-p['entry']);p['fees']+=fee
                    p['slippage_cost']+=qty*(exit_price-execution)
                    p['exits'].append({'time':b.end.isoformat(),'price':execution,'qty':qty,'reason':reason})
                    if reason=='2R分批止盈':
                        p['partial']=True;p['pending_exit']=None;p['lows']=[];p['trailing_after']=b.end.isoformat()
                    elif p['remaining']:p['pending_exit']=reason
                    else:p['pending_exit']=None
                    p['status']='持有' if p['pending_exit'] is None else '待继续退出'
                    if p['remaining']==0:
                        p['net_pnl']=p['realized']-p['fees']
                        p['gross_before_costs']=p['net_pnl']+p['fees']+p['slippage_cost']
                        p['double_cost_pnl']=p['net_pnl']-p['fees']-p['slippage_cost']
                        p['exit_time']=b.end.isoformat();p['holding_sessions']=session_count(entryday,day,market)
                        a['trades'].append(p.copy());del a['positions'][p['symbol']];return
        if p['partial'] and b.start>=stamp(p.get('trailing_after',p['entry_time'])):
            p['lows'].append(b.low);p['lows']=p['lows'][-3:]
        if p['partial'] and len(p['lows'])==3:
            # Only updates AFTER evaluating this bar, so this bar's low cannot be a retroactive stop.
            p['stop']=max(p['stop'],min(p['lows']))

    def mark(self,a,b):
        eq=a['cash']+sum(p['remaining']*p['mark'] for p in a['positions'].values())
        point={'time':b.end.isoformat(),'equity':round(eq,2)}
        curve=a['curve']
        if curve and curve[-1]['time']==point['time']:curve[-1]=point
        else:curve.append(point)

    def summary(self,version=None):
        result={}
        for market,a in self.state['accounts'].items():
            equity=a['cash']+sum(p['remaining']*p['mark'] for p in a['positions'].values())
            peak=100000.;dd=0.
            for x in sorted(a['curve'],key=lambda x:x['time']):
                peak=max(peak,x['equity']);dd=max(dd,(peak-x['equity'])/peak)
            groups={}
            strategy_ids=['breakout','pullback','trend_pullback','volatility_breakout']
            strategy_ids+=sorted({t.get('strategy') for t in a['trades'] if t.get('strategy')} - set(strategy_ids))
            for strategy in strategy_ids:
                ts=[t for t in a['trades'] if t['strategy']==strategy and (version is None or t.get('version','实验 1.0.0')==version)]
                wins=[t['net_pnl'] for t in ts if t['net_pnl']>0];losses=[-t['net_pnl'] for t in ts if t['net_pnl']<0]
                streak=longest=0
                for t in ts:
                    streak=streak+1 if t['net_pnl']<0 else 0;longest=max(longest,streak)
                groups[strategy]={'count':len(ts),'net_pnl':sum(t['net_pnl'] for t in ts),'win_rate':len(wins)/len(ts) if ts else None,
                   'payoff':(sum(wins)/len(wins))/(sum(losses)/len(losses)) if wins and losses else None,
                   'loss_streak':longest,'double_cost_pnl':sum(t['double_cost_pnl'] for t in ts),'enough':len(ts)>=30,
                   'avg_holding':sum(t['holding_sessions'] for t in ts)/len(ts) if ts else None,
                   'risk_groups':{g:{'count':sum(t['risk_group']==g for t in ts),'net_pnl':sum(t['net_pnl'] for t in ts if t['risk_group']==g)} for g in ['normal','smallcap','st']}}
            result[market]={'cash':a['cash'],'equity':equity,'return_pct':(equity/100000-1)*100,'max_drawdown':dd*100,
                            'positions':list(a['positions'].values()),'trades':a['trades'][-100:],'strategies':groups,'curve':a['curve'][-500:]}
        return result
