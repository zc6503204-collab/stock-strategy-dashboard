import asyncio
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest
from app.autoresearch import AutoResearch, complete_daily, daily_cutoff, rotate_monitoring, IMPLEMENTATION
from app.models import Bar, Quote, stamp
from app.calendars import calendar, local_date
from app.service import Dashboard


def pick(s,score=80,**extra):
    return dict(symbol=s,name=s,market='CN',decision='重点观察',score=score,distance_to_high_pct=0,
                name_verified=True,data_quality='complete',candidate_strategies=['trend_pullback'],**extra)


def bars_for(symbol,through='2026-09-18',count=270):
    days=calendar('CN').sessions_in_range('2024-01-01',through)[-count:]
    return [Bar(symbol,'longbridge',d.to_pydatetime().replace(hour=1,minute=30,tzinfo=timezone.utc),
                10+i*.02,10.1+i*.02,9.9+i*.02,10+i*.02,20000000,3e8) for i,d in enumerate(days)]


def test_complete_daily_uses_session_close_not_five_minute_bar_end():
    t=stamp('2026-09-21T06:00:00Z');bars=bars_for('600001.SH','2026-09-21')
    assert str(local_date(complete_daily(bars,t)[-1].start,'CN'))=='2026-09-18'
    assert str(local_date(complete_daily(bars,t+timedelta(hours=2))[-1].start,'CN'))=='2026-09-21'
    assert str(daily_cutoff(stamp('2026-09-20T12:00:00Z')))=='2026-09-18'


def test_rotation_requires_two_distinct_rounds_and_preserves_protected():
    rows=[pick('600001.SH',75),pick('600002.SH',81),pick('600003.SH',90)]
    votes={}
    a,_=rotate_monitoring(['600001.SH'],rows,['600003.SH'],votes,'1',2)
    assert a==['600003.SH','600001.SH']
    a,_=rotate_monitoring(a,rows,['600003.SH'],votes,'1',2)
    assert a==['600003.SH','600001.SH']
    a,changes=rotate_monitoring(a,rows,['600003.SH'],votes,'2',2)
    assert a==['600003.SH','600002.SH']
    assert any(c['state']=='被替换' and c['symbol']=='600001.SH' for c in changes)


def test_rotation_hard_reject_immediate_and_broken_streak_resets():
    votes={};rows=[pick('600001.SH',75),pick('600002.SH',81)]
    rotate_monitoring(['600001.SH'],rows,[],votes,'1',1)
    rows[1]['score']=77
    rotate_monitoring(['600001.SH'],rows,[],votes,'2',1)
    rows[1]['score']=81
    a,_=rotate_monitoring(['600001.SH'],rows,[],votes,'3',1)
    assert a==['600001.SH']
    rows[0]['decision']='暂不参与'
    a,changes=rotate_monitoring(a,rows,[],votes,'3',1)
    assert a==['600002.SH'] and changes[0]['state']=='已失效'


def test_universe_pagination_resume_and_duplicate_not_complete(tmp_path):
    d=Dashboard(tmp_path);calls=[]
    async def page(n,count):
        calls.append(n)
        if n==1 and calls==[0,1]:raise RuntimeError('throttled')
        return {'items':[{'symbol':f'60000{n+1}.SH','name':'样本'}],'page':n,'total':2}
    d.lb.universe_page=page
    async def run():
        with pytest.raises(RuntimeError):await d.autoresearch.universe('2026-09-21')
        assert not d.autoresearch.status['universe_complete']
        result=await d.autoresearch.universe('2026-09-21')
        assert result['complete'] and len(result['rows'])==2 and calls==[0,1,1]
        async def duplicate(n,count):return {'items':[{'symbol':'600001.SH'}],'page':n,'total':2}
        d.lb.universe_page=duplicate
        with pytest.raises(ValueError,match='重复'):await d.autoresearch.universe('2026-09-22')
        assert not d.autoresearch.status['universe_complete']
        assert d.store.get('cn_universe')['date']=='2026-09-21'
    asyncio.run(run())


def test_missing_page_and_changed_total_do_not_complete(tmp_path):
    d=Dashboard(tmp_path)
    async def page(n,count):return {'items':[{'symbol':f'60000{n+1}.SH'}],'page':n,'total':2 if n==0 else 3}
    d.lb.universe_page=page
    with pytest.raises(ValueError,match='总数变化'):asyncio.run(d.autoresearch.universe('2026-09-21'))
    assert not d.store.get('cn_universe',{}).get('complete')


def test_unranked_stock_enters_research_and_monitoring(tmp_path,monkeypatch):
    fixed=stamp('2026-09-21T01:15:00Z')
    monkeypatch.setattr('app.autoresearch.now',lambda:fixed)
    monkeypatch.setattr('app.service.now',lambda:fixed)
    d=Dashboard(tmp_path)
    async def page(n,count):
        return {'items':[{'symbol':'600001.SH','name':'未上榜样本','industry':'工业','marketcap':2e10}], 'page':n,'total':1}
    async def daily(symbol,*args,**kw):
        rows=bars_for(symbol)
        if symbol=='000300.SH':
            for b in rows:b.open=b.close=10;b.high=10.1;b.low=9.9
        return rows
    async def refs(symbols):return {s:{'name':'未上榜样本','total_shares':2e9} for s in symbols}
    async def forbidden(*args,**kw):raise AssertionError('discovery must not read rankings')
    d.lb.universe_page=page;d.lb.bars=daily;d.lb.reference_info=refs;d.lingxi.rank=forbidden;d.lb.cli_scan=forbidden
    asyncio.run(d.autoresearch.scan())
    assert d.autoresearch.status['state']=='complete'
    assert d.selection[0]['symbol']=='600001.SH' and d.watch==['600001.SH']
    assert d.selection[0]['candidate_origin']=='universe'
    assert d.store.research_list('candidate')[0]['symbol']=='600001.SH'
    assert d.autoresearch.diagnostics()['enumerated']==1


def test_scan_failure_keeps_history_but_blocks_old_candidate(tmp_path,monkeypatch):
    fixed=stamp('2026-09-21T01:15:00Z');monkeypatch.setattr('app.service.now',lambda:fixed)
    d=Dashboard(tmp_path);d.selection=[pick('600001.SH')];d.watch=['600001.SH']
    d.daily_core={'CN':{'rows':list(d.selection),'stale':False}}
    async def fail(*a,**k):raise RuntimeError('no permission')
    d.lb.universe_page=fail
    asyncio.run(d.autoresearch.scan())
    assert d.daily_core['CN']['stale'] and d.selection[0]['data_quality']=='missing'
    assert not d.watch and d.autoresearch.status['state']=='error'


def test_resume_daily_cache_avoids_repeated_history_and_updates_close(tmp_path):
    d=Dashboard(tmp_path);calls=[]
    async def daily(symbol,period,count,**kw):calls.append(count);return bars_for(symbol,'2026-09-21')
    d.lb.bars=daily
    async def run():
        await d.autoresearch.daily('600001.SH',stamp('2026-09-21T02:00Z'))
        await d.autoresearch.daily('600001.SH',stamp('2026-09-21T03:00Z'))
        assert calls==[270]
        bars=await d.autoresearch.daily('600001.SH',stamp('2026-09-21T07:10Z'))
        assert calls==[270,25] and str(local_date(bars[-1].start,'CN'))=='2026-09-21'
    asyncio.run(run())


def test_background_history_does_not_hold_quote_lock(monkeypatch):
    from app.providers import Longbridge
    lb=Longbridge();started=asyncio.Event();release=asyncio.Event()
    async def slow(*a,**kw):started.set();await release.wait();return []
    monkeypatch.setattr('app.providers.command',slow)
    lb.ctx=SimpleNamespace(quote=lambda syms:[])
    async def run():
        task=asyncio.create_task(lb.bars('600001.SH','day',270,force_cli=True))
        await started.wait()
        assert await asyncio.wait_for(lb.quotes(['600001.SH']),.1)==[]
        release.set();await task
    asyncio.run(run())


def test_coalescing_manual_requests_no_duplicate_job(tmp_path):
    d=Dashboard(tmp_path);done=asyncio.Event();calls=[]
    async def scan(force=False):calls.append(force);await done.wait()
    d.autoresearch.scan=scan
    async def run():
        a=d.autoresearch.request();b=d.autoresearch.request(force=True)
        assert a is b and d.autoresearch.pending
        done.set();await a
        assert calls==[False]
    asyncio.run(run())


def test_two_trading_days_schedule_reports_without_manual_action(tmp_path,monkeypatch):
    d=Dashboard(tmp_path);calls=[];clock=[None]
    monkeypatch.setattr('app.autoresearch.now',lambda:clock[0])
    def request(force=False):
        calls.append(clock[0]);d.autoresearch.status={'date':str(local_date(clock[0],'CN')),'state':'complete','checked':100,'supported':100}
    d.autoresearch.request=request
    async def pool():pass
    d.autoresearch.refresh_pool=pool
    async def run():
        for day in ['2026-09-21','2026-09-22']:
            for at in ['01:10','01:20','02:00','02:30','07:10']:
                clock[0]=stamp(day+'T'+at+'Z');await d.autoresearch.tick(clock[0]);await asyncio.sleep(0)
            # Same schedule doesn't enqueue a new run or duplicate report.
            await d.autoresearch.tick(clock[0])
        assert len(d.store.research_list('report'))==4
        assert len(calls)==8
        assert all(r['scan_complete'] for r in d.store.research_list('report'))
    asyncio.run(run())


def test_report_no_trades_explains_missing_data_and_notifications_not_replayed(tmp_path,monkeypatch):
    d=Dashboard(tmp_path);t=stamp('2026-09-21T10:00Z')
    monkeypatch.setattr('app.autoresearch.now',lambda:t)
    d.autoresearch.status={'state':'partial','date':'2026-09-21','checked':20,'supported':100,'missing':80}
    d.autoresearch.report_if_due(t);d.autoresearch.report_if_due(t)
    report=d.store.research_list('report')[0]
    assert report['closed_trades']==0 and report['coverage']['missing']==80
    assert not report['scan_complete'] and not d.alerts.list()


def test_manual_confirmation_tracks_local_holding_without_enabling_broker_context(tmp_path):
    d=Dashboard(tmp_path)
    d.real.upsert_manual({'symbol':'600001.SH','quantity':100,'cost':10})
    d.real.rows.append({'symbol':'600002.SH','source':'longbridge','quantity':100,'cost':10})
    assert [r['symbol'] for r in d.research_real_holdings()]==['600001.SH']
    assert d.protected_monitoring()==['600001.SH']
    assert not d.settings.get('account_mode',False)
    d.settings['account_mode']=True
    assert d.protected_monitoring()==['600001.SH','600002.SH']


def test_new_day_old_candidates_cannot_reenter_on_quote_alone(tmp_path,monkeypatch):
    t=stamp('2026-09-22T02:00Z');monkeypatch.setattr('app.service.now',lambda:t)
    monkeypatch.setattr('app.autoresearch.now',lambda:t)
    d=Dashboard(tmp_path);d.autoresearch.status={'date':'2026-09-22','state':'running'}
    d.selection=[pick('600001.SH',checked_session='2026-09-21',structure_low=9)]
    d.watch=['600001.SH'];d.autoresearch.expire_previous_day(t)
    assert not d.watch and not d.research_entry_allowed('600001.SH')
    d.lb.ctx=object()
    async def quotes(*a,**k):return [Quote('600001.SH','longbridge','样本',10,t,t,quality='realtime',trade_status='Normal')]
    d.lb.quotes=quotes
    asyncio.run(d.autoresearch.refresh_pool())
    assert d.selection[0]['data_quality']=='missing' and not d.research_entry_allowed('600001.SH')
    d.selection[0].update(checked_session='2026-09-22',data_quality='complete',setup_ready=True,setup_checked_at=t.isoformat())
    assert d.research_entry_allowed('600001.SH')


def test_live_depth_updates_without_a_history_response(tmp_path):
    d=Dashboard(tmp_path);t=stamp('2026-09-22T02:00Z')
    d.quotes['600001.SH']=Quote('600001.SH','longbridge','样本',10,t,t)
    calls=[];d.evaluate_decisions=lambda:calls.append(True)
    d.accept_depth({'symbol':'600001.SH','source':'longbridge','bid':9.99,'ask':10.01,'bid_size':1000,'ask_size':2000,'depth_time':t})
    assert d.quotes['600001.SH'].ask==10.01 and d.quotes['600001.SH'].depth_time==t and calls


def test_runtime_requires_actual_full_days_and_detects_sleep(tmp_path):
    d=Dashboard(tmp_path)
    for day in ['2026-09-21','2026-09-22']:
        base=stamp(day+'T01:10Z')
        for i in range(361):d.autoresearch.record_runtime(base+timedelta(minutes=i))
        d.store.research_save('run',day,{'date':day,'implementation':IMPLEMENTATION,'universe_complete':True,'checked':100,'ended_at':day+'T07:10Z'})
        d.store.research_save('discovery',day,{'date':day,'implementation':IMPLEMENTATION,'universe_complete':True,'supported':100,'quote_checked':100,'quote_missing':0,'ended_at':day+'T07:00Z'})
        for kind in ['premarket','close']:d.store.research_save('report',day+kind,{'date':day,'kind':kind,'implementation':IMPLEMENTATION})
    assert d.autoresearch.runtime_validation()['state']=='verified'
    assert len(d.autoresearch.runtime_validation()['verified_days'])==2
    assert d.autoresearch.runtime_validation()['operational_two_days']
    assert d.autoresearch.runtime_validation()['full_coverage']['state']=='incomplete'
    other=Dashboard(tmp_path/'gap')
    for at in ['01:10','01:31','02:00','07:10']:
        other.autoresearch.record_runtime(stamp('2026-09-21T'+at+'Z'))
    assert other.store.research_get('runtime','2026-09-21')['gap']
    assert other.autoresearch.runtime_validation()['state']=='collecting'


def test_research_read_apis_do_not_schedule_or_read_accounts(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main
    d=Dashboard(tmp_path)
    async def noop():pass
    d.start=noop;d.stop=noop
    def forbidden(*a,**kw):raise AssertionError('read API must not run external work')
    d.autoresearch.request=forbidden;d.lb.account_positions=forbidden
    d.store.research_save('candidate','test',{'market':'CN','date':'2026-09-21','symbol':'600001.SH','state':'等确认','strategy_versions':{'breakout':'test'}})
    monkeypatch.setattr(main,'dashboard',d)
    with TestClient(main.app,base_url='http://localhost') as c:
        assert c.get('/api/research/candidates/600001.SH').json()['checks'][0]['strategy_versions']=={'breakout':'test'}
        assert c.get('/api/research/days?market=CN&date=2026-09-21').json()=={'runs':[],'reports':[]}
        result=c.get('/api/research/comparison?market=CN').json()
        assert result['strategies'] and all(r['signal_to_fill_rate'] is None for r in result['strategies'])
        assert c.get('/api/research/days?market=BAD').status_code==400
        assert c.get('/api/research/candidates/invalid').status_code==400


def test_comparison_counts_structures_not_repeat_confirmations(tmp_path):
    from app.models import Signal
    d=Dashboard(tmp_path);strategy='trend_pullback';version=d.registry.current(strategy,'CN')['version']
    d.strategy_performance_detail=lambda market,strategy,version=None:{'version':version or d.registry.current(strategy,market)['version']}
    for i in range(3):
        signal=Signal(str(i),'600001.SH',strategy,'longbridge',stamp('2026-09-21T02:00Z')+timedelta(minutes=5*i),10,9,'normal',{'structure_id':'same'},version)
        d.store.signal(signal)
    d.sim.state['accounts']['CN']['trades']=[{'id':'0','strategy':strategy,'version':version,'evidence':{'structure_id':'same'}}]
    row=next(r for r in d.autoresearch.comparison()['strategies'] if r['strategy']==strategy)
    assert row['raw_signal_count']==3 and row['signal_count']==1 and row['filled_count']==1
    assert row['signal_to_fill_rate']==1 and row['correlated_sample']['reconfirmations']==2


def test_interrupted_history_resumes_after_twenty_without_starting_quote_discovery(tmp_path,monkeypatch):
    d=Dashboard(tmp_path);t=stamp('2026-09-21T12:30Z');calls=[]
    monkeypatch.setattr('app.autoresearch.now',lambda:t)
    d.autoresearch.status={'state':'interrupted','checked':10,'supported':100}
    d.autoresearch.request=lambda force=False:calls.append(force)
    asyncio.run(d.autoresearch.tick(t))
    assert calls==[False] and d.autoresearch.pool_task is None


def test_runtime_completion_local_notification_only_once(tmp_path,monkeypatch):
    d=Dashboard(tmp_path);t=stamp('2026-09-21T12:30Z')
    monkeypatch.setattr('app.autoresearch.now',lambda:t)
    d.autoresearch.runtime_validation=lambda:{'operational_two_days':True,'full_coverage':{'state':'incomplete'}}
    async def run():
        await d.autoresearch.tick(t);await d.autoresearch.tick(t+timedelta(minutes=1))
    asyncio.run(run())
    alerts=[a for a in d.alerts.list() if a['id'].startswith('runtime_verified_notification:')]
    assert len(alerts)==1 and d.store.get('runtime_verified_notification:'+IMPLEMENTATION)
