import asyncio
from datetime import timedelta

from app.autoresearch import AutoResearch
from app.models import Quote, stamp
from app.research import ResearchEngine
from app.service import Dashboard
from app.discovery import intraday_priority
from app.selection import ranking_key
from test_autoresearch import bars_for, pick


def fixture(tmp_path,monkeypatch,count=3):
    clock=[stamp('2026-09-21T02:00:00Z')]
    for module in ('app.autoresearch','app.discovery','app.service'):
        monkeypatch.setattr(module+'.now',lambda:clock[0])
    d=Dashboard(tmp_path);d.lb.ctx=object()
    rows=[{'symbol':f'600{i:03}.SH','name':f'样本{i}','industry':'工业','marketcap':2e10} for i in range(1,count+1)]
    d.store.set('cn_universe',{'date':'2026-09-21','complete':True,'total':count,'rows':rows})
    for symbol in ['000300.SH']+[r['symbol'] for r in rows]:
        bars=bars_for(symbol,count=65)
        if symbol=='000300.SH':
            for b in bars:b.open=b.close=10;b.high=10.1;b.low=9.9
        d.store.set('selection_daily:'+symbol,{'cutoff':'2026-09-18','bars':[b.dump() for b in bars]})
    async def quotes(symbols,**kw):
        return [Quote(s,'longbridge',s,11.28,clock[0],clock[0],quality='realtime',trade_status='Normal',volume=1e7,turnover=1e8) for s in symbols]
    async def refs(symbols):return {s:{'name':s,'total_shares':2e9} for s in symbols}
    async def validate(row):return {**row,'setup_stage':'watch','setup_checked_at':clock[0].isoformat(),'setup_ready':True,'name_verified':True}
    async def no_history(*a,**kw):raise AssertionError('light discovery must never fetch historical bars')
    d.lb.quotes=quotes;d.lb.reference_info=refs;d.lb.bars=no_history;d.validate_research_candidate=validate
    d.refresh_research_context=lambda:None
    return d,rows,clock


async def drain(d):
    task=d.autoresearch.discovery.validation_task
    if task:await task


def test_outside_eighty_not_on_rankings_can_enter_research_and_live_pool(tmp_path,monkeypatch):
    d,rows,clock=fixture(tmp_path,monkeypatch,97);a=d.autoresearch
    seen=[]
    async def validate(row):
        seen.append(row['symbol'])
        return {**row,'setup_stage':'confirmed' if row['symbol']=='600097.SH' else 'watch',
                'setup_checked_at':clock[0].isoformat(),'setup_ready':True,'name_verified':True}
    d.validate_research_candidate=validate
    for r in rows[:80]:
        item=pick(r['symbol'],100,checked_session='2026-09-21',reason='原候选',setup_stage='watch')
        a.evaluations[r['symbol']]=item;d.selection.append(dict(item))
    d.watch=[r['symbol'] for r in rows[:12]]
    async def run():
        await a.refresh_pool();await drain(d)
        assert set(seen)=={r['symbol'] for r in rows}
        assert len(d.selection)==80 and '600097.SH' in [r['symbol'] for r in d.selection]
        assert '600097.SH' in d.watch and len(d.watch)<=12
        assert len(a.evaluations)==97
        coverage=a.discovery.diagnostics()
        assert coverage['quote_coverage']=={'checked':97,'fresh_now':97,'total':97,'missing':0,'unquoted':0,'complete':True}
        assert coverage['signal_coverage']['checked']==97
        assert coverage['round_changes']['outside_discovered']==17
        assert coverage['round_changes']['new_entries']>=1
    asyncio.run(run())


def test_cold_history_is_independent_and_missing_history_stays_in_scope(tmp_path,monkeypatch):
    d,rows,clock=fixture(tmp_path,monkeypatch,3);a=d.autoresearch
    d.store.set('selection_daily:600003.SH',{})
    release=asyncio.Event()
    async def cold():await release.wait()
    async def run():
        a.task=asyncio.create_task(cold())
        await asyncio.wait_for(a.refresh_pool(),2);await drain(d)
        assert not a.task.done()
        assert a.discovery.status['quote_checked']==3 and a.discovery.status['daily_checked']==2
        assert a.discovery.status['daily_missing']==1 and a.discovery.status['state']=='partial'
        assert '600003.SH' in a.evaluations and a.evaluations['600003.SH']['data_quality']=='missing'
        release.set();await a.task
    asyncio.run(run())


def test_partial_history_publish_preserves_not_yet_processed_names(tmp_path,monkeypatch):
    d,rows,clock=fixture(tmp_path,monkeypatch,3);a=d.autoresearch
    d.selection=[pick(r['symbol'],80,checked_session='2026-09-21',reason='仍待本轮检查') for r in rows]
    a.status={'run_id':'new-history','date':'2026-09-21'}
    async def run():
        await a.publish([dict(d.selection[0],run_id='new-history')],partial=True)
        assert {r['symbol'] for r in d.selection}=={r['symbol'] for r in rows}
        assert next(r for r in d.selection if r['symbol']=='600003.SH')['pool_review_pending']
        assert not any(r['state']=='已失效' for r in d.store.research_list('change'))
    asyncio.run(run())


def test_quote_failure_and_expiry_cannot_report_complete(tmp_path,monkeypatch):
    d,rows,clock=fixture(tmp_path,monkeypatch,3);a=d.autoresearch
    async def quotes(symbols,**kw):
        return [Quote('600001.SH','longbridge','样本',11.28,clock[0],clock[0],quality='realtime',trade_status='Normal'),
                Quote('600002.SH','longbridge','样本',11.28,clock[0]-timedelta(minutes=6),clock[0],quality='realtime',trade_status='Normal')]
    d.lb.quotes=quotes
    async def run():
        await a.refresh_pool();await drain(d)
        status=a.discovery.diagnostics()
        assert status['state']=='partial' and status['quote_coverage']['missing']==2
        assert status['signal_coverage']['checked']==1
        assert all(a.evaluations[s]['data_quality']=='missing' for s in ('600002.SH','600003.SH'))
    asyncio.run(run())


def test_pending_validation_is_deduplicated_and_restart_restores_queue(tmp_path,monkeypatch):
    d,rows,clock=fixture(tmp_path,monkeypatch,3);a=d.autoresearch
    entered=asyncio.Event();release=asyncio.Event();calls=[]
    async def validate(row):
        calls.append(row['symbol']);entered.set();await release.wait()
        return {**row,'setup_stage':'forming','setup_ready':True,'setup_checked_at':clock[0].isoformat()}
    d.validate_research_candidate=validate
    async def run():
        await a.refresh_pool();await entered.wait()
        for _ in range(3):a.discovery.enqueue(a.evaluations['600003.SH'])
        assert list(a.discovery.queue)==['600002.SH','600003.SH']
        a.discovery.persist()
        restored=AutoResearch(d)
        assert set(restored.discovery.queue)=={'600001.SH','600002.SH','600003.SH'}
        release.set();await drain(d)
        assert calls==['600001.SH','600002.SH','600003.SH']
    asyncio.run(run())


def test_slow_validation_cannot_revive_newly_invalidated_candidate(tmp_path,monkeypatch):
    d,rows,clock=fixture(tmp_path,monkeypatch,1);a=d.autoresearch
    entered=asyncio.Event();release=asyncio.Event()
    async def validate(row):
        entered.set();await release.wait()
        return {**row,'setup_stage':'confirmed','setup_ready':True,'setup_checked_at':clock[0].isoformat()}
    d.validate_research_candidate=validate
    async def run():
        await a.refresh_pool();await entered.wait()
        a.evaluations['600001.SH'].update(decision='暂不参与',setup_stage='invalid',reason='结构失效')
        release.set();await drain(d)
        assert a.evaluations['600001.SH']['setup_stage']=='invalid'
    asyncio.run(run())


def test_strategy_daily_eligibility_is_not_overridden_by_generic_trend(tmp_path,monkeypatch):
    d,rows,clock=fixture(tmp_path,monkeypatch,1)
    import app.autoresearch as module
    actual=module.evaluate
    def generic(row,bars):return dict(actual(row,bars),decision='暂不参与',reason='尚未站稳上行的20日均线',hard_veto=False)
    monkeypatch.setattr(module,'evaluate',generic)
    engine=ResearchEngine(d.registry);bars=bars_for('600001.SH',count=65);benchmark=bars_for('000300.SH',count=65)
    monkeypatch.setattr(engine,'_daily',lambda symbol,strategy:{'eligible':strategy=='trend_rsi_pullback'})
    row=d.autoresearch.evaluate_daily({'symbol':'600001.SH','name':'样本'},bars,benchmark,engine,clock[0])
    assert row['candidate_strategies']==['trend_rsi_pullback'] and row['decision']=='等确认'


def test_new_day_incomplete_enumeration_still_scans_previously_known_outside_names(tmp_path,monkeypatch):
    d,rows,clock=fixture(tmp_path,monkeypatch,3);a=d.autoresearch
    old=d.store.get('cn_universe');old['date']='2026-09-18';d.store.set('cn_universe',old)
    d.store.set('cn_universe_checkpoint',{'date':'2026-09-21','page':1,'total':4,
        'rows':[{'symbol':'600004.SH','name':'新证券','industry':'工业'}]})
    async def run():
        await a.refresh_pool();await drain(d)
        assert a.discovery.status['supported']==4 and a.discovery.status['quote_checked']==4
        assert not a.discovery.status['universe_complete'] and a.discovery.status['state']=='partial'
        assert set(a.evaluations)=={'600001.SH','600002.SH','600003.SH','600004.SH'}
    asyncio.run(run())


def test_validation_aging_prevents_new_pool_candidates_starving_outside_work(tmp_path,monkeypatch):
    d,rows,clock=fixture(tmp_path,monkeypatch,2);a=d.autoresearch;calls=[]
    first=pick('600001.SH',100,checked_session='2026-09-21',setup_stage='confirmed',reason='池内')
    outside=pick('600002.SH',70,checked_session='2026-09-21',setup_stage='watch',reason='池外')
    d.selection=[first];a.evaluations.update({first['symbol']:first,outside['symbol']:outside})
    a.discovery_quotes={s:Quote(s,'longbridge',s,10,clock[0],clock[0],quality='realtime') for s in a.evaluations}
    async def validate(row):
        calls.append(row['symbol']);return {'setup_stage':'watch','setup_ready':True,'setup_checked_at':clock[0].isoformat()}
    d.validate_research_candidate=validate
    async def run():
        a.discovery.enqueue(first);a.discovery.enqueue(outside)
        a.discovery.enqueued_at[outside['symbol']]=(clock[0]-timedelta(minutes=6)).isoformat()
        assert a.discovery.diagnostics()['oldest_wait_seconds']==360
        await drain(d)
        assert calls[0]=='600002.SH'
    asyncio.run(run())


def test_each_strategy_uses_its_own_structure_stop_for_dynamic_admission():
    t=stamp('2026-09-21T02:00Z')
    row=pick('600001.SH',80,structure_low=10,close=11,breakout_reference=11,atr20=1,
             structural_stops={'trend_pullback':10,'vcp_swing':9},daily_score=80)
    row['candidate_strategies']=['trend_pullback','vcp_swing']
    q=Quote('600001.SH','longbridge','样本',9.5,t,t,quality='realtime',trade_status='Normal')
    AutoResearch.apply_price(row,q)
    assert row['decision']!='暂不参与' and row['candidate_strategies']==['vcp_swing']
    assert row['strategy_invalidations']['trend_pullback']['stop']==10
    q.price=8.9;AutoResearch.apply_price(row,q)
    assert row['decision']=='暂不参与' and row['setup_stage']=='invalid'


def test_live_volume_expansion_changes_discovery_rank_without_creating_buy_signal():
    t=stamp('2026-09-21T02:00Z')
    quiet=pick('600001.SH',90,average_volume=2400000,average_turnover=24000000,breakout_reference=10)
    active=pick('600002.SH',80,average_volume=2400000,average_turnover=24000000,breakout_reference=10)
    for row,volume in ((quiet,150000),(active,600000)):
        q=Quote(row['symbol'],'longbridge','样本',10,t,t,quality='realtime',volume=volume,turnover=volume*10)
        assert not intraday_priority(row,q,t)
    assert active['coarse_volume_ratio']==2 and active['discovery_stage']=='forming'
    assert ranking_key(active)<ranking_key(quiet)
    assert 'setup_ready' not in active and active['decision']=='重点观察'
    q.volume=None
    assert intraday_priority(active,q,t) and active['coarse_volume_ratio'] is None


def test_before_first_discovery_reports_known_scope_and_unquoted_gap(tmp_path,monkeypatch):
    d,rows,clock=fixture(tmp_path,monkeypatch,3);a=d.autoresearch
    a.evaluations['600001.SH']=pick('600001.SH',80,checked_session='2026-09-21')
    status=a.discovery.diagnostics()
    assert status['state']=='waiting' and status['reason']=='等待本轮行情发现'
    assert status['supported']==3 and status['daily_checked']==status['daily_missing']==status['volume_missing']==0
    assert status['quote_coverage']=={'checked':0,'fresh_now':0,'total':3,'missing':0,'unquoted':3,'complete':False}
    assert status['signal_coverage']['total']==3 and status['signal_coverage']['eligible']==1
    a.discovery.status={'date':'2026-09-18','state':'complete','supported':100,'quote_checked':100}
    a.discovery.round_changes['outside_discovered'].add('600001.SH')
    status=a.discovery.diagnostics()
    assert status['state']=='waiting' and status['supported']==3
    assert status['round_changes']['outside_discovered']==0
    assert a.discovery.round_changes['outside_discovered']=={'600001.SH'}


def test_expired_quote_queue_waits_without_busy_loop_then_resumes(tmp_path,monkeypatch):
    d,rows,clock=fixture(tmp_path,monkeypatch,1);a=d.autoresearch;calls=[]
    row=pick('600001.SH',80,checked_session='2026-09-21')
    a.evaluations[row['symbol']]=row
    async def validate(item):
        calls.append(item['symbol']);return {'setup_stage':'watch','setup_ready':True,'setup_checked_at':clock[0].isoformat()}
    d.validate_research_candidate=validate
    async def run():
        # Receiving an old snapshot now cannot make it fresh.
        a.discovery_quotes[row['symbol']]=Quote(row['symbol'],'longbridge','样本',10,clock[0]-timedelta(minutes=6),clock[0],quality='realtime')
        a.discovery.enqueue(row);await asyncio.wait_for(drain(d),1)
        assert not calls and list(a.discovery.queue)==[row['symbol']]
        await a.refresh_pool();await drain(d)
        assert calls==[row['symbol']] and not a.discovery.queue
        clock[0]+=timedelta(minutes=6)
        status=a.discovery.diagnostics()
        assert status['state']=='waiting' and status['quote_coverage']['fresh_now']==0 and not status['quote_coverage']['complete']
    asyncio.run(run())


def test_inflight_validation_after_close_cannot_confirm_or_drain_queue(tmp_path,monkeypatch):
    d,rows,clock=fixture(tmp_path,monkeypatch,2);a=d.autoresearch;calls=[]
    entered=asyncio.Event();release=asyncio.Event()
    async def validate(row):
        calls.append(row['symbol']);entered.set();await release.wait()
        return {'setup_stage':'confirmed','setup_ready':True,'setup_checked_at':clock[0].isoformat()}
    d.validate_research_candidate=validate
    async def run():
        await a.refresh_pool();await entered.wait()
        clock[0]=stamp('2026-09-21T07:01:00Z');release.set();await drain(d)
        assert len(calls)==1 and len(a.discovery.queue)==2
        assert a.evaluations[calls[0]]['setup_stage']=='unavailable' and not a.evaluations[calls[0]]['setup_ready']
    asyncio.run(run())
