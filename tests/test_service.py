import asyncio
from datetime import timedelta
from fastapi.testclient import TestClient
from app.service import Dashboard
from app.models import Quote,now
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

def test_source_query_text_preserved_and_scope_filtered(tmp_path):
    d=Dashboard(tmp_path)
    d.security_map={s:{'name':'样本','证券类型':'1'} for s in ['300300.SZ','600001.SH','688001.SH']}
    async def result(*args):return {'text':'日期20260904 | 300300.SZ | 600001SH | 688001.SH'}
    d.lingxi.call=result
    r=asyncio.run(d.screen('均线向上'))
    assert [x['symbol'] for x in r['candidates']]==['300300.SZ','600001.SH']
    assert '20260904' in r['text']

def test_local_api_rejects_cross_origin_and_private_files(tmp_path,monkeypatch):
    import app.main as main
    d=Dashboard(tmp_path)
    async def noop():pass
    d.start=noop;d.stop=noop
    monkeypatch.setattr(main,'dashboard',d)
    with TestClient(main.app,base_url='http://localhost') as c:
        r=c.get('/api/state');assert r.status_code==200
        assert 'apiKey' not in r.text
        assert c.get('/.local/vendor/gtht/gtht-skill-shared/gtht-entry.json').status_code==404
        assert c.get('/assets/../.local/dashboard.sqlite3').status_code==404
        assert c.post('/api/settings',json={}).status_code==403
        assert c.post('/api/settings',json={},headers={'X-Dashboard-Local':'1','Origin':'https://example.com'}).status_code==403
        assert c.post('/api/settings',json={'monitor_limit':41},headers={'X-Dashboard-Local':'1'}).status_code==422
        assert c.post('/api/settings',json={'simulation_enabled':False},headers={'X-Dashboard-Local':'1'}).status_code==200
