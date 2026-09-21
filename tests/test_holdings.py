import asyncio
from datetime import timedelta

import pytest

from app.holdings import RealHoldings, real_holding_plan
from app.models import Quote, Signal, now, stamp
from app.store import Store
from app.decisions import recommend
from app.research import VERSION
from app.simulation import Simulation
from app.service import Dashboard


def test_manual_holding_persists_and_computes_two_r(tmp_path):
    store=Store(tmp_path/'holdings.db');book=RealHoldings(store)
    row=book.upsert_manual({'symbol':'AAPL.US','quantity':12,'cost':100,'entry_date':'2026-09-04','stop':95})
    assert row['target']==110 and RealHoldings(store).list()[0]['id']==row['id']
    row=book.upsert_manual({**row,'quantity':10,'cost':101,'stop':96,'target':112})
    assert row['quantity']==10 and row['target']==112
    book.remove_manual(row['id']);assert book.list()==[]


def test_synced_refresh_preserves_user_exit_plan(tmp_path):
    book=RealHoldings(Store(tmp_path/'holdings.db'))
    book.replace_synced('longbridge',[{'symbol':'AAPL.US','name':'Apple','quantity':5,'available':5,'cost':100,'currency':'USD'}])
    book.set_plan('longbridge:AAPL.US',95,None,'2026-09-01','核心仓')
    book.replace_synced('longbridge',[{'symbol':'AAPL.US','name':'Apple','quantity':6,'available':6,'cost':101,'currency':'USD'}])
    row=book.list()[0]
    assert row['quantity']==6 and row['stop']==95 and row['target']==110 and row['note']=='核心仓'
    with pytest.raises(ValueError):book.remove_manual(row['id'])
    book.set_plan(row['id'],105,None,None,'移动保护')
    assert book.list()[0]['stop']==105


def test_real_holding_plan_requires_live_session_and_respects_t1(monkeypatch):
    t=stamp('2026-09-08T02:00:00Z')
    p={'id':'manual:x','symbol':'600001.SH','name':'样本','source':'manual','quantity':200,
       'available':0,'cost':101,'entry_date':'2026-09-08','stop':100,'target':103}
    q=Quote('600001.SH','longbridge','样本',99,t,t,quality='realtime',session='regular',
            depth_time=t,bid=98.99,ask=99.01,bid_size=1000,ask_size=1000,trade_status='Normal')
    row=real_holding_plan(p,q,t,{'ready':True,'risk_group':'normal'})
    assert row['event']=='stop' and row['action']=='暂时无法卖出'
    closed=stamp('2026-09-06T02:00:00Z');q.market_time=q.received_at=q.depth_time=closed
    assert real_holding_plan(p,q,closed,{})['event'] is None


def test_real_position_blocks_duplicate_and_high_risk_buy():
    t=stamp('2026-09-08T13:40:05Z')
    def sig(symbol,group):
        return Signal(symbol,symbol,'breakout','longbridge',t-timedelta(seconds=5),101,99,group,
                      {'relative_volume':2,'relative_strength':3,'atr':2,'bar':{'volume':1000000}},VERSION)
    def quote(symbol):
        return Quote(symbol,'longbridge',symbol,101.02,t,t,quality='realtime',session='regular',depth_time=t,
                     bid=101,ask=101.02,bid_size=10000,ask_size=10000,trade_status='Normal')
    signals=[sig('AAPL.US','normal').dump(),sig('AMD.US','smallcap').dump()]
    validation={s:{'ready':True,'eligible':True,'source':'longbridge'} for s in ['AAPL.US','AMD.US']}
    real=[{'symbol':'AAPL.US','quantity':1,'risk_group':'normal'},
          {'symbol':'RISK.US','quantity':1,'risk_group':'smallcap'}]
    rows=recommend(signals,{s:quote(s) for s in validation},validation,Simulation().state,t,real)
    assert any(r['symbol']=='AAPL.US' and '真实持仓' in r['reason'] for r in rows)
    assert any(r['symbol']=='AMD.US' and '高风险' in r['reason'] for r in rows)


def test_us_security_and_premarket_are_visible(tmp_path,monkeypatch):
    d=Dashboard(tmp_path)
    assert d.allowed_security({'symbol':'AAPL.US','name':'Apple'})
    d.selection=[{'symbol':'AAPL.US','name':'Apple','market':'US','decision':'重点观察','score':82,
                  'distance_to_high_pct':-1,'risk_group':'normal','source':'longbridge','as_of':'2026-09-04',
                  'breakout_reference':230,'structure_low':220,'reason':'趋势向上'}]
    monkeypatch.setattr('app.service.now',lambda:stamp('2026-09-07T10:00:00Z'))
    rows=d.premarket('US')
    assert rows[0]['symbol']=='AAPL.US' and rows[0]['trade_date']=='2026-09-08'


def test_longbridge_position_normalization(monkeypatch):
    from app.providers import Longbridge
    async def fake(*args,**kwargs):
        return [{'symbol':'AAPL.US','market':'US','name':'Apple','quantity':'2','available':'2','cost_price':'100','currency':'USD'},
                {'symbol':'OPT.US','market':'US','name':'Option','quantity':'0','available':'0','cost_price':'1','currency':'USD'}]
    monkeypatch.setattr('app.providers.command',fake)
    rows=asyncio.run(Longbridge().account_positions())
    assert rows==[{'symbol':'AAPL.US','name':'Apple','quantity':2.0,'available':2.0,'cost':100.0,'currency':'USD'}]


def test_failed_broker_refresh_keeps_last_good_snapshot(tmp_path):
    d=Dashboard(tmp_path)
    d.real.replace_synced('longbridge',[{'symbol':'AAPL.US','name':'Apple','quantity':2,'available':2,'cost':100,'currency':'USD'}])
    class Broken:
        async def account_positions(self):raise RuntimeError('offline')
    d.providers['longbridge']=Broken()
    asyncio.run(d.sync_real_holdings(['longbridge']))
    assert d.real.list()[0]['symbol']=='AAPL.US'
    assert d.holding_sync['longbridge']['state']=='waiting'


def test_manual_partial_sell_costs_remaining_and_closed_history(tmp_path):
    store=Store(tmp_path/'holdings.db');book=RealHoldings(store)
    original={'entry_min':10,'entry_max':10.25,'stop':9,'target':12,'max_hold_sessions':3}
    row=book.upsert_manual({'symbol':'600001.SH','quantity':400,'cost':10.5,'entry_time':'2026-09-08T10:00:00+08:00',
        'entry_fee':8,'stop':9,'target':12,'plan_id':'plan:1','strategy':'strong_pullback','strategy_version':'trial-1','original_plan':original})
    original['stop']=1
    assert row['original_plan']['stop']==9
    assert row['entry_date']=='2026-09-08' and row['plan_deviations']==['实际买价高于原计划禁止追价上限']
    result=book.record_manual_sell(row['id'],100,12,'2026-09-09T10:00:00+08:00',3,'fill:1')
    assert result['quantity']==300 and result['realized_pnl']==145
    assert result['sell_fills'][0]['entry_fee_allocated']==2
    restarted=RealHoldings(store)
    again=restarted.record_manual_sell(row['id'],100,12,'2026-09-09T10:00:00+08:00',3,'fill:1')
    assert again['quantity']==300 and len(again['sell_fills'])==1
    with pytest.raises(ValueError,match='成交编号'):
        restarted.record_manual_sell(row['id'],200,12,'2026-09-09T10:00:00+08:00',3,'fill:1')
    closed=restarted.record_manual_sell(row['id'],300,11,'2026-09-10T10:00:00+08:00',5,'fill:2')
    assert closed['status']=='closed' and closed['quantity']==0 and closed['realized_pnl']==284
    assert closed['entry_fee_allocated']==8 and restarted.symbols()==[]
    assert RealHoldings(store).list()[0]['sell_fills']==closed['sell_fills']
    with pytest.raises(ValueError,match='保留成交账本'):restarted.remove_manual(row['id'])


def test_manual_sell_checks_t1_over_sell_dates_and_finite_numbers(tmp_path):
    book=RealHoldings(Store(tmp_path/'holdings.db'))
    row=book.upsert_manual({'symbol':'600001.SH','quantity':200,'cost':10,'entry_date':'2026-09-08','stop':9})
    for qty,price,time,fee,reason in [
        (100,11,'2026-09-08T14:00:00+08:00',0,r'T\+1'),
        (201,11,'2026-09-09T14:00:00+08:00',0,'超过'),
        (100,11,'2026-09-12T14:00:00+08:00',0,'交易日'),
        (100,float('nan'),'2026-09-09T14:00:00+08:00',0,'大于0'),
        (100,11,'2026-09-09T14:00:00+08:00',-1,'费用'),
    ]:
        with pytest.raises(ValueError,match=reason):book.record_manual_sell(row['id'],qty,price,time,fee)
    assert book.list()[0]['quantity']==200 and not book.list()[0]['sell_fills']
    for value in (float('nan'),float('inf')):
        with pytest.raises(ValueError):book.upsert_manual({'symbol':'AAPL.US','quantity':1,'cost':value})
    with pytest.raises(ValueError):book.upsert_manual({'symbol':'bad','quantity':1,'cost':1})


def test_manual_plan_identity_and_fills_cannot_be_rewritten(tmp_path):
    book=RealHoldings(Store(tmp_path/'holdings.db'))
    row=book.upsert_manual({'symbol':'600001.SH','quantity':200,'cost':10,'entry_date':'2026-09-08','stop':9,
        'plan_id':'original','strategy_version':'v1','original_plan':{'stop':9,'target':12},'entry_fee':5})
    edited=book.upsert_manual({**row,'stop':10.2,'plan_id':'replacement','original_plan':{'stop':8}})
    assert edited['plan_id']=='original' and edited['original_plan']=={'stop':9,'target':12}
    sold=book.record_manual_sell(row['id'],100,12,'2026-09-09T10:00:00+08:00')
    with pytest.raises(ValueError,match='不能改写'):book.upsert_manual({**sold,'quantity':200})
    with pytest.raises(ValueError,match='不能改写'):book.set_plan(row['id'],10,12,'2026-09-07')
    book.set_plan(row['id'],10.5,13,'2026-09-08')
    assert book.list()[0]['original_plan']['stop']==9
    returned=book.list();returned[0]['original_plan']['stop']=1
    assert book.list()[0]['original_plan']['stop']==9


def test_legacy_manual_lots_keep_local_t1_and_time_exit(tmp_path):
    store=Store(tmp_path/'holdings.db')
    old={'id':'manual:legacy','symbol':'600001.SH','name':'样本','source':'manual','quantity':200,
         'available':None,'cost':10,'entry_date':'2026-09-08','stop':9,'target':12,
         'original_plan':{'max_hold_sessions':3}}
    store.set('real_holdings',[old]);book=RealHoldings(store)
    t=stamp('2026-09-10T06:50:00Z')
    q=Quote('600001.SH','longbridge','样本',10,t,t,quality='realtime',session='regular',depth_time=t,
            bid=9.99,ask=10.01,bid_size=1000,ask_size=1000,trade_status='Normal')
    plan=real_holding_plan(book.list()[0],q,t,{'ready':True})
    assert plan['event']=='time_exit' and plan['available']==200 and plan['qty']==200
    q.limit_down=9.99
    blocked=real_holding_plan(book.list()[0],q,t,{'ready':True})
    assert blocked['blocked'] and '跌停' in blocked['reason']
