import asyncio
from datetime import timedelta
from app.service import Dashboard
from app.models import Bar,stamp
from test_strategy_expansion import first_pullback_fixture


def candidate_service(tmp_path,monkeypatch,broken=False):
    engine,symbol,daily,benchmark,history,today=first_pullback_fixture()
    if broken:today[0].low=106.5
    t=today[-1].end+timedelta(seconds=5)
    monkeypatch.setattr('app.service.now',lambda:t)
    d=Dashboard(tmp_path);d.benchmark_daily['CN']=benchmark
    d.store.set('selection_daily:'+symbol,{'cutoff':'2026-09-07','bars':[b.dump() for b in daily]})
    async def reference(symbols):return {symbol:{'name':'测试股票','total_shares':1e9}}
    async def bars(s,*args,**kwargs):return history+today if s==symbol else engine.benchmarks['CN']
    async def filings(s):return [{'id':'notice','title':'董事会决议','publish_at':(t-timedelta(days=1)).isoformat()}]
    d.lb.reference_info=reference;d.lb.bars=bars;d.lb.filings=filings
    row={'symbol':symbol,'name':'测试股票','market':'CN','industry':'测试行业','candidate_origin':'universe','candidate_strategies':['first_pullback'],
         'data_quality':'complete','decision':'等确认','checked_session':'2026-09-08','name_verified':False}
    return d,row,t,history


def test_pool_outsider_uses_real_strategy_and_creates_plan_without_live_slot(tmp_path,monkeypatch):
    d,row,t,history=candidate_service(tmp_path,monkeypatch)
    result=asyncio.run(d.validate_research_candidate(row))
    assert row['symbol'] not in d.watch
    assert result['setup_stage']=='confirmed' and result['setup_ready']
    assert result['plan_id'] in d.plans.rows
    assert d.store.signals() and not d.sim.state['pending']
    assert all(b['limit_up'] is None for b in d.store.bars(row['symbol'],'longbridge') if stamp(b['start'])<stamp('2026-09-08T00:00:00Z'))


def test_intraday_broken_structure_is_invalid_not_forming(tmp_path,monkeypatch):
    d,row,t,history=candidate_service(tmp_path,monkeypatch,broken=True)
    result=asyncio.run(d.validate_research_candidate(row))
    assert result['setup_stage']=='invalid' and '破坏' in result['setup_reason']
    assert not d.plans.rows


def test_stale_minute_validation_blocks_entry_even_with_good_daily_data(tmp_path,monkeypatch):
    d,row,t,_=candidate_service(tmp_path,monkeypatch)
    d.autoresearch.status={'state':'running'}
    row.update(name_verified=True,setup_ready=True,setup_checked_at=(t-timedelta(minutes=7)).isoformat())
    d.selection=[row]
    assert not d.research_entry_allowed(row['symbol'])
    row['setup_checked_at']=t.isoformat()
    assert d.research_entry_allowed(row['symbol'])
    row['setup_ready']=False
    assert not d.research_entry_allowed(row['symbol'])
