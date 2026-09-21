import asyncio

from fastapi.testclient import TestClient

from app.service import Dashboard
from app.store import Store


def client_for(dashboard, monkeypatch):
    import app.main as main
    async def noop():pass
    dashboard.start=noop;dashboard.stop=noop
    monkeypatch.setattr(main,'dashboard',dashboard)
    return TestClient(main.app,base_url='http://localhost')


def test_manual_api_never_enables_account_reads_and_keeps_plan(tmp_path,monkeypatch):
    d=Dashboard(tmp_path);calls=[]
    async def forbidden():
        calls.append('account_read')
        raise AssertionError('manual operation read broker account')
    for provider in d.providers.values():
        monkeypatch.setattr(provider,'account_positions',forbidden,raising=False)
        monkeypatch.setattr(provider,'account_summary',forbidden,raising=False)
    headers={'X-Dashboard-Local':'1'}
    with client_for(d,monkeypatch) as client:
        invalid=client.post('/api/real-holdings/manual',json={'symbol':'bad','quantity':100,'cost':10},headers=headers)
        assert invalid.status_code==400
        result=client.post('/api/real-holdings/manual',json={
            'symbol':'600001.SH','quantity':200,'cost':10,'entry_date':'2026-09-08',
            'entry_fee':5,'stop':9,'target':12,'plan_id':'plan:local','strategy':'strong_pullback',
            'strategy_version':'v1','original_plan':{'stop':9,'max_hold_sessions':3}},headers=headers)
        assert result.status_code==200
        row=result.json()['row']
        assert not d.settings.get('account_mode') and not d.settings.get('broker_sync_enabled')
        edited=client.post('/api/real-holdings/manual',json={
            'id':row['id'],'symbol':'600001.SH','quantity':200,'cost':10,'entry_date':'2026-09-08','stop':9.2,'target':12},headers=headers)
        assert edited.status_code==200 and edited.json()['row']['entry_fee']==5
        sold=client.post('/api/real-holdings/sell',json={'id':row['id'],'quantity':100,'price':11,
            'sold_at':'2026-09-09T10:00:00+08:00','fee':2,'execution_id':'one'},headers=headers)
        assert sold.status_code==200 and sold.json()['row']['quantity']==100
        assert sold.json()['row']['realized_pnl']==95.5
        history=client.get('/api/real-holdings/history').json()['rows']
        assert history[0]['plan_id']=='plan:local' and len(history[0]['sell_fills'])==1
        assert not d.settings.get('account_mode') and not d.settings.get('broker_sync_enabled')
        assert calls==[]
    restarted=Dashboard(tmp_path)
    assert restarted.real.list()[0]['quantity']==100
    assert restarted.research_real_symbols()==['600001.SH']
    assert not restarted.settings.get('broker_sync_enabled')


def test_old_account_display_flag_does_not_authorize_background_sync(tmp_path,monkeypatch):
    store=Store(tmp_path/'.local'/'dashboard.sqlite3')
    store.set('settings',{'monitor_limit':12,'simulation_enabled':True,'ib_port':7497,'account_mode':True})
    d=Dashboard(tmp_path);calls=[]
    async def forbidden(*args):calls.append(args)
    async def clock(seconds):
        if seconds>=300:d.alive=False
    d.sync_real_holdings=forbidden
    monkeypatch.setattr('app.service.asyncio.sleep',clock)
    asyncio.run(d.holdings_loop())
    assert calls==[] and not d.settings.get('broker_sync_enabled')


def test_explicit_sync_is_the_only_route_that_enables_broker_reads(tmp_path,monkeypatch):
    d=Dashboard(tmp_path);calls=[]
    async def sync(sources):calls.append(sources)
    d.sync_real_holdings=sync
    headers={'X-Dashboard-Local':'1'}
    with client_for(d,monkeypatch) as client:
        invalid=client.post('/api/real-holdings/sync',json={'source':'unknown'},headers=headers)
        assert invalid.status_code==400 and not d.settings.get('broker_sync_enabled')
        settings=client.post('/api/settings',json={'broker_sync_enabled':True},headers=headers)
        assert settings.status_code==400 and calls==[]
        enabled=client.post('/api/real-holdings/sync',json={'source':'longbridge'},headers=headers)
        assert enabled.status_code==200 and calls==[['longbridge']] and d.settings['broker_sync_enabled']
        disabled=client.post('/api/settings',json={'broker_sync_enabled':False},headers=headers)
        assert disabled.status_code==200 and not d.settings['broker_sync_enabled']
