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
