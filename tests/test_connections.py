import asyncio
from datetime import datetime,timedelta
from types import SimpleNamespace as Obj
from app.models import stamp
from app.providers import Longbridge,sdk_stamp
from app.service import Dashboard

def test_sdk_local_datetime_is_converted_to_utc():
    utc=stamp('2026-09-04T06:55:00Z')
    sdk_local=datetime.fromtimestamp(utc.timestamp())
    assert sdk_stamp(sdk_local)==utc
    assert sdk_stamp(utc)==utc

def test_market_verification_records_partial_access():
    p=Longbridge();received=[];p.on_quote=received.append
    class Context:
        def quote_package_details(self):return []
        def quote(self,symbols):return [Obj(symbol=symbols[0],last_done=100,timestamp=datetime.now())]
        def depth(self,symbol):
            if symbol.endswith('.US'):raise RuntimeError('unavailable')
            return Obj(bids=[Obj(price=99)],asks=[Obj(price=101)])
        def subscribe(self,*args):pass
        def unsubscribe(self,*args):pass
        def subscribe_candlesticks(self,*args):return [1]
        def unsubscribe_candlesticks(self,*args):pass
    p.ctx=Context();result=asyncio.run(p.verify_access())
    assert result['US']['quote'] and result['US']['bars'] and not result['US']['depth']
    assert result['SH']['depth'] and len(received)==3

def test_auto_connect_persists_actual_gateway_port(tmp_path,monkeypatch):
    d=Dashboard(tmp_path)
    class Writer:
        def close(self):pass
        async def wait_closed(self):pass
    async def probe(host,port):
        assert host=='127.0.0.1'
        if port!=4001:raise ConnectionRefusedError()
        return None,Writer()
    async def connect(port):d.ib.ib=Obj(isConnected=lambda:True)
    async def quotes(symbols):return []
    monkeypatch.setattr('app.service.asyncio.open_connection',probe)
    d.ib.connect=connect;d.ib.quotes=quotes
    assert asyncio.run(d.connect_ib_available())
    assert d.store.get('settings')['ib_port']==4001 and d.first_poll

def test_no_listener_never_claims_connected(tmp_path,monkeypatch):
    d=Dashboard(tmp_path)
    async def probe(*args):raise ConnectionRefusedError()
    monkeypatch.setattr('app.service.asyncio.open_connection',probe)
    assert not asyncio.run(d.connect_ib_available())
    assert d.ib.status['state']=='unavailable'
