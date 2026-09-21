from datetime import timedelta
from app.models import Signal,stamp
from app.plans import TradePlans
from app.store import Store
from app.simulation import DEFAULTS
from app.holdings import RealHoldings

T=stamp('2026-09-18T02:00:00Z')
def signal(sid='one',t=T):
    return Signal(sid,'600001.SH','first_pullback','longbridge',t,10,9,'normal',
                  {'structure_id':'first-structure','target_price':12,'horizon':'short','max_hold_sessions':3,'signal_minutes':5},'test-v1')
def test_same_structure_revises_plan_and_old_buy_does_not_revive(tmp_path):
    store=Store(tmp_path/'plans.db');plans=TradePlans(store)
    row=plans.observe(signal(),{'name':'样本'},DEFAULTS,T)
    assert row['executable_range'] and row['trial']
    meta={'symbol':'600001.SH','name_verified':True,'data_quality':'complete'}
    gate={'600001.SH':{'passed':True,'checks':[]}}
    plans.update([{'signal_id':'one','state':'buy','reason':'通过'}],[meta],gate,T)
    assert plans.list()[0]['state']=='buyable'
    plans.update([],[meta],gate,T+timedelta(minutes=6))
    assert plans.list()[0]['state']=='waiting_trigger'
    next_row=plans.observe(signal('two',T+timedelta(minutes=10)),meta,DEFAULTS,T+timedelta(minutes=10))
    assert next_row['id']==row['id'] and len(plans.rows)==1
    assert next_row['state']=='execution_check'
    resumed=TradePlans(store)
    assert resumed.list()[0]['state']=='recovering'
    resumed.update([],[meta],gate,stamp('2026-09-18T06:45:00Z'))
    assert resumed.rows[row['id']]['state']=='expired'

def test_manual_buy_links_frozen_plan_and_is_idempotent(tmp_path):
    store=Store(tmp_path/'plans.db');plans=TradePlans(store);real=RealHoldings(store)
    row=plans.observe(signal(),{'name':'样本'},DEFAULTS,T)
    body={'quantity':200,'cost':10.02,'entry_time':T.isoformat(),'entry_fee':5}
    first=plans.confirm_buy(row['id'],body,real,T)
    again=plans.confirm_buy(row['id'],body,real,T)
    assert first['id']==again['id'] and len(real.list())==1
    assert first['original_plan']['stop']==9 and first['plan_id']==row['id']
    assert plans.rows[row['id']]['state']=='bought'
    assert not store.get('settings',{}).get('broker_sync_enabled')

def test_plan_buyability_fails_closed_when_context_lost(tmp_path):
    plans=TradePlans(Store(tmp_path/'plans.db'))
    row=plans.observe(signal(),{},DEFAULTS,T)
    plans.update([{'signal_id':'one','state':'buy','reason':'price'}],
                 [{'symbol':'600001.SH','data_quality':'complete'}],
                 {'600001.SH':{'passed':False,'reason':'行业数据不足','checks':[]}},T)
    assert plans.rows[row['id']]['state']=='execution_check'
    assert '行业' in plans.rows[row['id']]['reason']


def test_confirm_endpoint_tracks_manual_holding_without_broker_sync(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.service import Dashboard
    d=Dashboard(tmp_path);monkeypatch.setattr(main,'dashboard',d)
    row=d.plans.observe(signal(),{'name':'样本'},DEFAULTS,T)
    calls=[]
    async def forbidden(*args,**kwargs):calls.append(args);raise AssertionError('account read')
    d.lb.account_positions=forbidden;d.ib.account_positions=forbidden
    client=TestClient(main.app,base_url='http://127.0.0.1:8765')
    response=client.post('/api/plans/'+row['id']+'/confirm-buy',headers={'x-dashboard-local':'1'},
                         json={'quantity':200,'cost':10.02,'entry_time':T.isoformat(),'entry_fee':5})
    assert response.status_code==200,response.text
    assert d.research_real_symbols()==['600001.SH']
    assert not calls and not d.settings.get('broker_sync_enabled')
    assert client.get('/api/plans?market=CN').json()['plans'][0]['state']=='bought'


def test_shadow_drawdown_includes_open_and_partial_realized_profit():
    from app.simulation import Simulation
    from app.models import Bar
    sim=Simulation();book={'positions':{'one':{'symbol':'600001.SH','mark':10,'entry':10,'remaining':100,
          'fees':2,'realized':200}},'trades':[],'curve':[]}
    bar=Bar('600001.SH','longbridge',T,10,11,9,10,1000)
    sim._mark_shadow(book,bar)
    assert book['curve'][-1]['equity']==100198


def test_filled_structure_cannot_create_a_second_trade():
    from app.simulation import Simulation
    sim=Simulation();sim.state['filled_structures']=['first-structure']
    sim.state['shadow']['filled_structures']=['first-structure']
    sim.queue(signal());sim.queue_shadow(signal())
    assert not sim.state['pending'] and not sim.state['shadow']['pending']
