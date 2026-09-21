import asyncio
from datetime import timedelta
from fastapi.testclient import TestClient
from app.service import Dashboard
from app.models import Bar,Quote,now,stamp
from test_engine import bar,breakout_bars

def test_feed_gap_cancels_entry_and_cross_source_is_ignored(tmp_path,monkeypatch):
    d=Dashboard(tmp_path);bs=breakout_bars()
    from app.strategy import Strategies
    d.strategies=Strategies()  # Preserve legacy strategy continuity regression separately from v2 admission tests.
    d.validation['AAPL.US']={'source':'longbridge','ready':True,'eligible':True,'risk_group':'normal'}
    for b in bs[:-1]:d.strategies.update(b)
    monkeypatch.setattr('app.service.now',lambda:bs[-1].end+timedelta(seconds=10))
    d.accept_bar(bs[-1]);assert len(d.sim.state['pending'])==1
    d.accept_bar(bs[-1]);assert len(d.store.signals())==1
    other=bar(bs[-1].end);other.source='ibkr';d.accept_bar(other)
    assert len(d.strategies.history['AAPL.US'])==23
    missed=bar(bs[-1].end+timedelta(minutes=5))
    monkeypatch.setattr('app.service.now',lambda:missed.end+timedelta(seconds=10))
    d.accept_bar(missed)
    assert not d.sim.state['pending'] and not d.validation['AAPL.US']['ready']

def test_old_and_future_bars_never_create_live_orders(tmp_path,monkeypatch):
    for lag in [-30,3600]:
        d=Dashboard(tmp_path/str(lag));bs=breakout_bars()
        d.validation['AAPL.US']={'source':'longbridge','ready':True,'eligible':True,'risk_group':'normal'}
        for b in bs[:-1]:d.strategies.update(b)
        monkeypatch.setattr('app.service.now',lambda:bs[-1].end+timedelta(seconds=lag))
        d.accept_bar(bs[-1]);assert not d.store.signals() and not d.sim.state['pending']

def test_out_of_order_quote_does_not_overwrite(tmp_path):
    d=Dashboard(tmp_path);t=now()
    q=Quote('AAPL.US','lingxi','Apple',100,t,t);d.accept_quote(q)
    old=Quote('AAPL.US','lingxi','Apple',90,t-timedelta(seconds=10),t);d.accept_quote(old)
    assert d.quotes['AAPL.US'].price==100

def test_lunch_reconnect_refreshes_intraday_benchmark(tmp_path,monkeypatch):
    d=Dashboard(tmp_path)
    midday=stamp('2026-09-10T04:00:00Z')
    monkeypatch.setattr('app.service.now',lambda:midday)
    d.watch=['002839.SZ'];d.validation['002839.SZ']={'ready':True,'source':'longbridge'}
    d.lb.ctx=object();requested=[];received=[]
    async def quotes(symbols):return []
    async def bars(symbol,*args,**kwargs):
        requested.append(symbol)
        return [Bar(symbol,'longbridge',stamp('2026-09-10T03:25:00Z'),100,101,99,100,1000,100000)]
    d.lb.quotes=quotes;d.lb.bars=bars
    d.strategies.set_benchmark=lambda market,rows:received.append((market,rows))
    asyncio.run(d.refresh_live())
    assert requested==['000300.SH']
    assert received and received[0][0]=='CN' and received[0][1][0].symbol=='000300.SH'

def test_source_query_text_preserved_and_scope_filtered(tmp_path):
    d=Dashboard(tmp_path)
    d.security_map={s:{'name':'样本','证券类型':'1'} for s in ['300300.SZ','600001.SH','688001.SH']}
    async def result(*args):return {'text':'日期20260904 | 300300.SZ | 600001SH | 688001.SH'}
    d.lingxi.call=result
    r=asyncio.run(d.screen('均线向上'))
    assert [x['symbol'] for x in r['candidates']]==['300300.SZ','600001.SH']
    assert '20260904' in r['text']

def test_strategy_candidate_screen_merges_strategy_matches_and_uses_daily_cache(tmp_path):
    d=Dashboard(tmp_path)
    d.security_map={s:{'name':s,'证券类型':'1'} for s in ['600001.SH','300300.SZ']}
    calls=[]
    async def result(mode,query):
        calls.append((mode,query))
        extra='300300.SZ' if 'RSI14' in query else ''
        return {'text':f'600001.SH {extra}'}
    d.lingxi.call=result
    first,status=asyncio.run(d.strategy_candidate_scan())
    second,_=asyncio.run(d.strategy_candidate_scan())
    by_symbol={row['symbol']:row for row in first}
    assert len(calls)==4 and second==first and not status['errors']
    assert set(by_symbol['600001.SH']['candidate_strategies'])=={'breakout','pullback','trend_pullback','trend_rsi_pullback','volatility_breakout','vcp_swing'}
    assert by_symbol['300300.SZ']['candidate_strategies']==['trend_rsi_pullback']
    assert all(row['candidate_origin']=='strategy' for row in first)

def test_cn_premarket_scan_forces_fresh_strategy_screen_before_marking_done(tmp_path,monkeypatch):
    d=Dashboard(tmp_path);fixed=stamp('2026-09-10T01:05:00Z')
    monkeypatch.setattr('app.service.now',lambda:fixed);calls=[]
    async def scan(force_strategy=False,rebuild_markets=None,wait_selection=False):
        calls.append((force_strategy,rebuild_markets,wait_selection))
        d.daily_core['CN']={'trade_date':'2026-09-10','updated_at':fixed.isoformat(),'rows':[],'stale':False}
    d.scan=scan
    asyncio.run(d.run_premarket_scan(['CN']))
    assert calls==[(True,['CN'],True)]
    assert d.store.get('premarket_scan_marker')['CN']=='2026-09-10'

def test_failed_daily_rebuild_keeps_snapshot_and_does_not_mark_complete(tmp_path,monkeypatch):
    d=Dashboard(tmp_path);fixed=stamp('2026-09-10T01:05:00Z')
    monkeypatch.setattr('app.service.now',lambda:fixed)
    async def scan(force_strategy=False,rebuild_markets=None,wait_selection=False):
        d.daily_core['CN']={'trade_date':'2026-09-09','updated_at':'2026-09-09T01:00:00Z',
                            'rows':[{'symbol':'600001.SH'}],'stale':True}
    d.scan=scan
    asyncio.run(d.run_premarket_scan(['CN']))
    assert 'CN' not in d.store.get('premarket_scan_marker',{})

def test_legacy_scan_keeps_us_separate_and_delegates_cn_discovery(tmp_path,monkeypatch):
    d=Dashboard(tmp_path);requested=[]
    d.autoresearch.request=lambda force=False:requested.append(force)
    d.daily_core={'CN':{'rows':[{'symbol':'600001.SH','name':'分页样本','industry':'银行','market':'CN',
                                'candidate_origin':'universe','pool_role':'daily_core'}]}}
    async def cli_scan(market):
        assert market=='US'
        return {'items':[{'symbol':'AAPL.US','name':'Apple','industry':'消费电子','prevclose':200}]}
    async def forbidden(*args,**kwargs):raise AssertionError('CN must not fall back to ranking lists')
    d.lb.cli_scan=cli_scan;d.lingxi.rank=forbidden;d.schedule_selection=lambda:None
    rebuilt=asyncio.run(d.scan(force_strategy=True,rebuild_markets=['CN','US']))
    rows={r['symbol']:r for r in d.candidates}
    assert rebuilt=={'US'} and requested==[True]
    assert rows['600001.SH']['industry']=='银行' and rows['600001.SH']['candidate_origin']=='universe'
    assert d.daily_core['US']['rows'][0]['industry']=='消费电子'

def test_daily_core_is_capped_at_80_and_keeps_strategy_candidates_first(tmp_path):
    d=Dashboard(tmp_path)
    market_rows=[{'symbol':f'T{i:03}.US','name':f'市场{i}','candidate_strategies':[]} for i in range(90)]
    strategy_rows=[{'symbol':f'S{i:03}.US','name':f'策略{i}','candidate_strategies':['breakout']} for i in range(3)]
    rows=d.limit_core_rows(market_rows+strategy_rows)
    assert len(rows)==80
    assert [row['symbol'] for row in rows[:3]]==['S000.US','S001.US','S002.US']

def test_saved_oversized_core_is_trimmed_when_dashboard_starts(tmp_path):
    from app.store import Store
    store=Store(tmp_path/'.local/dashboard.sqlite3')
    store.set('daily_candidate_core',{'US':{'trade_date':'2026-09-15','rows':[
        {'symbol':f'T{i:03}.US','name':f'市场{i}','candidate_strategies':[]} for i in range(81)]}})
    dashboard=Dashboard(tmp_path)
    assert len(dashboard.daily_core['US']['rows'])==80
    assert len(dashboard.store.get('daily_candidate_core')['US']['rows'])==80

def test_failed_cn_discovery_preserves_last_valid_core_as_stale(tmp_path):
    d=Dashboard(tmp_path)
    d.daily_core={'CN':{'trade_date':'2026-09-09','rows':[{'symbol':'600001.SH','name':'旧候选'}],'stale':False}}
    async def unavailable(*args,**kwargs):raise RuntimeError('private detail')
    d.lb.universe_page=unavailable
    asyncio.run(d.autoresearch.scan())
    assert d.daily_core['CN']['stale']
    assert d.daily_core['CN']['rows'][0]['symbol']=='600001.SH'
    assert d.autoresearch.status['state']=='error'


def test_manual_full_market_scan_requests_new_universe_without_destroying_history(tmp_path):
    d=Dashboard(tmp_path);requests=[]
    d.autoresearch.request=lambda force=False:requests.append(force)
    d.store.set('selection_pool',{'date':'2026-09-09','rows':[{'symbol':'000001.SZ'}]})
    async def us_scan(market):return {'items':[]}
    d.lb.cli_scan=us_scan;d.schedule_selection=lambda:None
    asyncio.run(d.scan(force_strategy=True))
    assert requests==[True]
    # Old cache isn't evidence of today's completion and isn't silently discarded before a new scan succeeds.
    assert not d.autoresearch.status.get('universe_complete',False)
    assert 'CN' not in d.store.get('premarket_scan_marker',{})

def test_local_api_rejects_cross_origin_and_private_files(tmp_path,monkeypatch):
    import app.main as main
    d=Dashboard(tmp_path)
    async def noop():pass
    d.start=noop;d.stop=noop
    monkeypatch.setattr(main,'dashboard',d)
    with TestClient(main.app,base_url='http://localhost') as c:
        page=c.get('/');assert page.status_code==200
        assert "location.protocol==='file:'" in page.text
        script=c.get('/assets/app.js');assert script.status_code==200
        assert "action('/api/scan',{},'已开始重新筛选" in script.text
        assert "sha256-vzr6U6Yv8Vz+BRc+9/HgtZvUqecsKaEvnfervwwT014=" in page.headers['content-security-policy']
        r=c.get('/api/state');assert r.status_code==200
        assert 'apiKey' not in r.text
        assert c.get('/.local/vendor/gtht/gtht-skill-shared/gtht-entry.json').status_code==404
        assert c.get('/assets/../.local/dashboard.sqlite3').status_code==404
        assert c.post('/api/settings',json={}).status_code==403
        assert c.post('/api/settings',json={},headers={'X-Dashboard-Local':'1','Origin':'https://example.com'}).status_code==403
        assert c.post('/api/settings',json={'monitor_limit':41},headers={'X-Dashboard-Local':'1'}).status_code==422
        assert c.post('/api/settings',json={'simulation_enabled':False},headers={'X-Dashboard-Local':'1'}).status_code==200
