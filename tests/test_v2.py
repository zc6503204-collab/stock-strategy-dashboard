import asyncio
from datetime import timedelta
from copy import deepcopy
from app.models import Bar,Quote,Signal,stamp
from app.research import ResearchEngine,VERSION,prefix
from app.decisions import recommend,check,holding_plan
from app.simulation import Simulation
from app.calendars import calendar,open_time,local_date,monitoring_window
from app.store import Store
from app.alerts import Alerts

T=stamp('2026-09-08T13:40:05Z')
def quote(symbol='AAPL.US',t=T,price=101.02):
 return Quote(symbol,'longbridge',symbol,price,t,t,quality='realtime',session='regular',depth_time=t,
              bid=price-.02,ask=price,bid_size=10000,ask_size=10000,trade_status='Normal')
def signal(symbol='AAPL.US',strategy='breakout',sid='s',group='normal'):
 return Signal(sid,symbol,strategy,'longbridge',T-timedelta(seconds=5),101,100,group,
               {'relative_volume':2,'relative_strength':3,'atr':2,'bar':{'volume':1000000}},VERSION)
def valid():return {'ready':True,'eligible':True,'source':'longbridge'}

def test_entry_requires_price_book_permissions_and_freshness():
 s=signal();a=Simulation().state['accounts']['US'];cfg=Simulation().state['params']
 assert check(s,quote(),valid(),a,cfg,T)['state']=='buy'
 for mutation,reason in [({'price':104,'ask':104},'不追价'),({'depth_time':T-timedelta(seconds=16)},'买卖盘'),
                        ({'market_time':T-timedelta(seconds=31)},'新报价'),({'trade_status':'Halted'},'停牌'),
                        ({'quality':'delayed'},'新报价'),({'source':'ibkr'},'同源'),({'ask':None},'买卖盘')]:
  q=quote()
  for k,v in mutation.items():setattr(q,k,v)
  r=check(s,q,valid(),a,cfg,T);assert r['state']!='buy' and reason in r['reason']
 assert check(s,quote(),valid(),a,cfg,T+timedelta(minutes=5))['state']=='avoid'
 assert check(s,quote(),valid(),a,cfg,T-timedelta(seconds=6))['state']=='avoid'

def test_shared_cash_high_risk_and_duplicate_allocation():
 state=Simulation().state
 sigs=[signal('AAPL.US',sid='a',group='smallcap'),signal('AAPL.US','pullback','b','smallcap'),signal('AMD.US',sid='c',group='smallcap')]
 for s in sigs:s.stop=99
 q={s.symbol:quote(s.symbol) for s in sigs};v={s.symbol:valid() for s in sigs}
 rows=recommend([s.dump() for s in sigs],q,v,state,T)
 buys=[r for r in rows if r['state']=='buy']
 assert len(buys)==1 and buys[0]['strategies']==['breakout','pullback']
 assert any('高风险' in r['reason'] for r in rows if r['symbol']=='AMD.US')
 state['accounts']['US']['cash']=1
 assert not any(r['state']=='buy' for r in recommend([s.dump() for s in sigs],q,v,state,T))

def test_pending_plan_stays_visible_without_double_reserving_cash():
 state=Simulation().state;s=signal(sid='pending');raw=s.dump()
 raw['evidence']['approval']={'time':T.isoformat(),'price':101.02,'qty':100,'cash_required':10110}
 state['pending'][s.id]=deepcopy(raw)
 rows=recommend([raw],{s.symbol:quote()},{s.symbol:valid()},state,T)
 assert len(rows)==1 and rows[0]['state']=='buy' and rows[0]['qty']>0
 # A different high-risk candidate still sees the first plan's reserved slot.
 s.risk_group='smallcap';state['pending'][s.id]['risk_group']='smallcap'
 other=signal('AMD.US',sid='other',group='smallcap');other.stop=99
 rows=recommend([state['pending'][s.id],other.dump()],
                {'AAPL.US':quote(),'AMD.US':quote('AMD.US')},
                {'AAPL.US':valid(),'AMD.US':valid()},state,T)
 assert not any(r['state']=='buy' and r['symbol']=='AMD.US' for r in rows)

def test_only_latest_signal_per_symbol_and_strategy_is_current():
 old=signal(sid='old');old.time-=timedelta(seconds=20)
 new=signal(sid='new')
 rows=recommend([old.dump(),new.dump()],{'AAPL.US':quote()},{'AAPL.US':valid()},Simulation().state,T)
 assert [r['signal_id'] for r in rows]==['new']

def test_atr_floor_and_no_missing_atr_for_cn():
 from app.risk import size_entry
 s=signal('600001.SH');sim=Simulation();a=sim.state['accounts']['CN'];cfg=sim.state['params']
 r=size_entry(s,101.1,a,cfg,100000)
 assert r['ok'] and r['qty']==100 and r['planned_risk']<=250
 s.evidence.pop('atr');assert not size_entry(s,101.1,a,cfg,100000)['ok']

def research_fixture(market='US'):
 symbol='600001.SH' if market=='CN' else 'AAPL.US';day=stamp('2026-09-08T00:00Z').date()
 start=open_time(day,market);n=3 if market=='CN' else 1
 def b(t,c=100,h=100.5,l=99.5,o=100,v=100):return Bar(symbol,'longbridge',t,o,h,l,c,v,c*v)
 prior=list(calendar(market).sessions_in_range(str(day-timedelta(days=40)),str(day-timedelta(days=1))))[-14:]
 sessions={}
 for session in prior:
  st=open_time(session.date(),market)
  sessions[session.date()]={st+timedelta(minutes=5*i):b(st+timedelta(minutes=5*i)) for i in range(15)}
 engine=ResearchEngine();engine.context[symbol]={'daily':{'eligible':True,'atr':2,'relative_strength':4},'sessions':sessions,'source':'longbridge'}
 today=[b(start+timedelta(minutes=5*i),c=100.2,v=200) for i in range(n)]
 today += [b(start+timedelta(minutes=5*n),c=101,h=101.1,v=300)]
 benchmark=[Bar('000300.SH' if market=='CN' else 'SPY.US','longbridge',x.start,100,102,99,101,100,10000) for x in today]
 engine.set_benchmark(market,benchmark)
 return engine,symbol,today,benchmark

def test_opening_range_and_rvol_both_markets():
 for market in ['CN','US']:
  engine,symbol,bars,benchmark=research_fixture(market)
  for b in bars[:-1]:assert not engine.update(b)
  signals=engine.update(bars[-1]);assert len(signals)==1
  s=signals[0];assert s.version==VERSION and s.strategy=='breakout' and s.evidence['relative_volume']>=1.5
  assert s.time==open_time(local_date(bars[0].start,market),market)+timedelta(minutes=20 if market=='CN' else 10)
  assert not engine.update(bars[-1])

def test_missing_same_time_day_or_benchmark_blocks_and_benchmark_late_retries():
 engine,symbol,bars,benchmark=research_fixture()
 engine.set_benchmark('US',benchmark[:-1])
 for b in bars:assert not engine.update(b)
 engine.set_benchmark('US',benchmark)
 assert len(engine.evaluate(symbol))==1
 d=sorted(engine.context[symbol]['sessions'])[0]
 engine.context[symbol]['sessions'][d].pop(next(iter(engine.context[symbol]['sessions'][d])))
 assert not engine.evaluate(symbol)

def test_pullback_and_failed_pullback_do_not_use_future_bars():
 for fail in [False,True]:
  e,symbol,bars,bm=research_fixture()
  for b in bars:e.update(b)
  t=bars[-1].end
  touch=Bar(symbol,'longbridge',t,100.8,100.9,100.49,100.7,200,20100)
  nextbar=Bar(symbol,'longbridge',t+timedelta(minutes=5),100.7,101.5,100.48 if fail else 100.6,101.2,300,30300)
  e.set_benchmark('US',bm+[Bar('SPY.US','longbridge',x.start,100,102,99,101,100,10000) for x in [touch,nextbar]])
  assert not e.update(touch)
  signals=e.update(nextbar)
  assert bool([s for s in signals if s.strategy=='pullback']) is (not fail)

def test_holding_alert_t1_and_stop_before_profit():
 t=stamp('2026-09-08T02:00Z');q=quote('600001.SH',t,99)
 p={'id':'p','symbol':'600001.SH','source':'longbridge','strategy':'breakout','risk_group':'normal','entry':101,'entry_time':t.isoformat(),
    'remaining':200,'stop':100,'initial_stop':100,'target':103,'mark':101,'partial':False}
 r=holding_plan(p,q,t,valid());assert r['event']=='stop' and r['action']=='暂时无法卖出' and 'T+1' in r['reason']
 p['entry_time']='2026-09-07T02:00:00Z';r=holding_plan(p,q,t,valid());assert r['action']=='止损退出'
 p['pending_exit']='先前止损触发';q.price=104
 assert holding_plan(p,q,t,valid())['event']=='stop'
 p.pop('pending_exit');r=holding_plan(p,q,t,valid());assert r['event']=='take_profit' and r['qty']==100

def test_holding_exit_needs_open_market_and_sell_side_book():
 t=stamp('2026-09-08T13:40:05Z');closed_t=stamp('2026-09-07T13:40:05Z')
 q=quote(t=closed_t,price=99)
 p={'id':'p','symbol':'AAPL.US','source':'longbridge','strategy':'breakout','risk_group':'normal','entry':101,
    'entry_time':'2026-09-04T14:00:00Z','remaining':10,'stop':100,'initial_stop':100,'target':103,'mark':101,'partial':False}
 closed=holding_plan(p,q,closed_t,valid())
 assert closed['blocked'] and '交易时段' in closed['reason']
 q=quote(t=t,price=99)
 q.bid_size=0
 waiting=holding_plan(p,q,t,valid())
 assert waiting['blocked'] and '买卖盘' in waiting['reason']
 q.bid_size=100;q.ask_size=0
 assert holding_plan(p,q,t,valid())['action']=='止损退出'

def test_non_price_pending_exit_is_not_labeled_stop():
 t=stamp('2026-09-08T13:40:05Z');q=quote(t=t,price=102)
 p={'id':'p','symbol':'AAPL.US','source':'longbridge','strategy':'breakout','risk_group':'normal','entry':101,
    'entry_time':'2026-09-04T14:00:00Z','remaining':10,'stop':100,'initial_stop':100,'target':105,'mark':101,'partial':False,
    'pending_exit':'切换行情源并补齐后，下一可成交K线退出'}
 r=holding_plan(p,q,t,valid())
 assert r['event']=='pending_exit' and r['action']=='待退出'

def test_alert_dedup_restart_read_and_no_model_calls(tmp_path):
 store=Store(tmp_path/'s.db');a=Alerts(store,tmp_path);a.configure({'desktop':False})
 assert a.emit('x','止损','触价提醒','exit')
 assert not Alerts(store,tmp_path).emit('x','止损','触价提醒','exit')
 a.read('x');r=a.list()[0];assert r['read'] and r['delivery']=='page_only'
 assert a.status()['ai_calls']==0
 # The runtime notification path contains only the native subprocess helper.
 from pathlib import Path
 runtime=(Path(__file__).parents[1]/'app/alerts.py').read_text()
 assert 'openai.' not in runtime and "'codex'" not in runtime and 'httpx' not in runtime

def test_monitor_calendars_dst_lunch_early_close():
 assert monitoring_window(stamp('2026-09-08T01:15Z'),'CN')
 assert not monitoring_window(stamp('2026-09-07T13:30Z'),'US') # Labor Day
 assert monitoring_window(stamp('2026-11-27T18:05Z'),'US') # early close + 5 min
 assert not monitoring_window(stamp('2026-11-27T18:15Z'),'US')
 assert open_time(stamp('2026-11-02T00:00Z').date(),'US').hour==14
 assert open_time(stamp('2026-10-30T00:00Z').date(),'US').hour==13

def test_paper_requires_admission_and_conservative_same_bar_exit():
 sim=Simulation();s=signal();bar=Bar(s.symbol,s.source,s.time,101.1,104,99,102,100000,10200000)
 sim.queue(s);sim.process(bar);assert not sim.state['accounts']['US']['positions'] and sim.state['failures']
 sim=Simulation();s.evidence['approval']={'time':T.isoformat(),'price':101.1,'qty':100}
 sim.queue(s);sim.process(bar)
 trade=sim.state['accounts']['US']['trades'][0]
 assert trade['version']==VERSION and '止损' in trade['exits'][0]['reason']
 assert trade['qty']<=100
