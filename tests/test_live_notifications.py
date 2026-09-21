import asyncio
from app.models import Quote,stamp
from app.service import Dashboard


def test_push_updates_notify_promptly_and_publish_last_quote_in_burst(tmp_path,monkeypatch):
    d=Dashboard(tmp_path);clock=[100.];callbacks=[];notifications=[]
    monkeypatch.setattr('app.service.time.monotonic',lambda:clock[0])
    class Handle:
        cancelled=False
        def cancel(self):self.cancelled=True
    class Loop:
        def call_later(self,delay,callback):
            handle=Handle();callbacks.append((delay,callback,handle));return handle
    monkeypatch.setattr('app.service.asyncio.get_running_loop',lambda:Loop())
    d.evaluate_decisions=lambda:None
    d.broadcast=lambda:notifications.append((clock[0],d.quotes['AAPL.US'].price))
    t=stamp('2026-09-21T14:00:00Z')
    def quote(price):return Quote('AAPL.US','longbridge','Apple',price,t,t,quality='realtime')
    d.accept_quote(quote(100))
    assert notifications==[(100.,100)]
    clock[0]=100.2;d.accept_quote(quote(101))
    clock[0]=100.4;d.accept_quote(quote(102))
    d.accept_depth({'symbol':'AAPL.US','source':'longbridge','depth_time':t,
                    'bid':101.9,'ask':102.,'bid_size':100,'ask_size':100})
    assert len(callbacks)==1 and len(notifications)==1
    assert 0<callbacks[0][0]<=1
    clock[0]=101.;callbacks[0][1]()
    assert notifications==[(100.,100),(101.,102)]
    assert d.quotes['AAPL.US'].bid==101.9
    # A late quote cannot roll the price back or schedule another notification.
    older=quote(99);older.market_time=stamp('2026-09-21T13:59:00Z')
    d.accept_quote(older)
    assert len(callbacks)==1 and d.quotes['AAPL.US'].price==102


def test_service_stop_cancels_pending_market_notification(tmp_path):
    d=Dashboard(tmp_path)
    class Handle:
        cancelled=False
        def cancel(self):self.cancelled=True
    handle=Handle();d.market_broadcast_handle=handle
    asyncio.run(d.stop())
    assert handle.cancelled and d.market_broadcast_handle is None
