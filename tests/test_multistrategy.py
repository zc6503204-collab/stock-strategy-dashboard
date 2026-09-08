import asyncio
from copy import deepcopy
from datetime import datetime,timedelta,timezone

import pytest
from fastapi.testclient import TestClient

from app.models import Bar, Quote, Signal, stamp, now
from app.decisions import recommend
from app.service import Dashboard
from app.research import ResearchEngine
from app.simulation import Simulation
from app.store import Store
from app.strategy_registry import StrategyRegistry, DEFINITIONS
from app.calendars import calendar,open_time


def test_registry_seeds_all_strategies_per_market_and_validates(tmp_path):
    registry=StrategyRegistry(Store(tmp_path/'registry.db'))
    assert {r['strategy'] for r in registry.list('CN')}=={key for key,row in DEFINITIONS.items() if 'CN' in row['markets']}
    assert {r['strategy'] for r in registry.list('US')}==set(DEFINITIONS)
    with pytest.raises(ValueError,match='慢速均线'):
        registry.save('trend_pullback','CN',{'daily_fast':40,'daily_slow':30})
    with pytest.raises(ValueError,match='未知参数'):
        registry.save('breakout','CN',{'secret':1})


def test_parameter_versions_are_immutable_and_rollback_creates_revision(tmp_path):
    registry=StrategyRegistry(Store(tmp_path/'registry.db'))
    original=registry.current('breakout','US')
    changed=registry.save('breakout','US',{'rvol_min':1.8},reason='测试')
    assert changed['version']!=original['version'] and registry.config('breakout','US')['rvol_min']==1.8
    restored=registry.rollback('breakout','US',original['version'])
    assert restored['version'] not in [original['version'],changed['version']]
    assert restored['parameters']==original['parameters']
    versions=registry.versions('breakout','US')
    assert versions[0]['parent_version']==changed['version'] and len(versions)==3


def test_rollback_ignores_retired_parameters_from_historical_revision(tmp_path):
    registry=StrategyRegistry(Store(tmp_path/'registry.db'))
    old=registry.revisions['US']['trend_pullback'][0]
    old['parameters']['retired_parameter']=99
    restored=registry.rollback('trend_pullback','US',old['version'])
    assert 'retired_parameter' not in restored['parameters']


def test_config_change_cancels_only_matching_pending_and_keeps_position_version(tmp_path):
    dashboard=Dashboard(tmp_path);t=now()
    keep=Signal('keep','AAPL.US','pullback','longbridge',t,101,100,'normal',{},'实验 2.0.0')
    cancel=Signal('cancel','MSFT.US','breakout','longbridge',t,101,100,'normal',{},'实验 2.0.0')
    dashboard.sim.queue(keep);dashboard.sim.queue(cancel)
    dashboard.sim.state['accounts']['US']['positions']['NVDA.US']={'id':'position','symbol':'NVDA.US','source':'longbridge',
        'strategy':'breakout','version':'旧持仓版本','remaining':1,'mark':100,'risk_group':'normal',
        'entry':100,'entry_time':'2026-09-05T14:00:00Z','initial_stop':95,'stop':95,'target':110,'partial':False}
    dashboard.save_strategy_config('breakout','US',{'rvol_min':1.7})
    assert 'cancel' not in dashboard.sim.state['pending'] and 'keep' in dashboard.sim.state['pending']
    assert dashboard.sim.state['accounts']['US']['positions']['NVDA.US']['version']=='旧持仓版本'


def test_old_version_signal_is_not_reintroduced_after_config_change(tmp_path):
    dashboard=Dashboard(tmp_path);t=now();active=dashboard.registry.current('breakout','US')['version']
    dashboard.register_signal(Signal('old','AAPL.US','breakout','longbridge',t,101,100,'normal',{},'old-version'))
    dashboard.register_signal(Signal('current','AAPL.US','breakout','longbridge',t,101,100,'normal',{},active))
    assert [row['id'] for row in dashboard.store.signals()]==['current']


def test_workspace_exposes_isolated_strategy_catalog_and_recommendations(tmp_path):
    dashboard=Dashboard(tmp_path)
    dashboard.selection=[{'symbol':'AAPL.US','name':'Apple','market':'US','decision':'重点观察','score':88,
        'distance_to_high_pct':-1,'risk_group':'normal','source':'longbridge','as_of':'2026-09-04',
        'close':100,'breakout_reference':102,'structure_low':98,'average_turnover':20_000_000,
        'relative_strength':3,'trend':True,'stacked':True,'ma20':99,'ma60':95,'extension_atr':1,
        'atr_contraction':.6,'high10':102,'reason':'等待盘中确认'}]
    snapshot=dashboard.snapshot();us=snapshot['workspaces']['US'];cn=snapshot['workspaces']['CN']
    assert len(us['strategies'])==7 and set(us['strategy_recommendations'])==set(DEFINITIONS)
    assert len(cn['strategies'])==6 and 'orb20_us' not in cn['strategy_recommendations']
    assert us['strategy_recommendations']['trend_pullback']['primary']['symbol']=='AAPL.US'
    assert all(not (row.get('primary') or {}).get('symbol','').endswith('.US') for row in cn['strategy_recommendations'].values())
    dashboard.save_strategy_config('trend_pullback','US',enabled=False)
    disabled=dashboard.strategy_recommendations('US')['trend_pullback']
    assert disabled['enabled'] is False and disabled['primary'] is None and disabled['rows']==[]


def test_strategy_api_save_versions_and_rollback(tmp_path,monkeypatch):
    import app.main as main
    dashboard=Dashboard(tmp_path)
    async def noop():pass
    dashboard.start=noop;dashboard.stop=noop;monkeypatch.setattr(main,'dashboard',dashboard)
    headers={'X-Dashboard-Local':'1'}
    with TestClient(main.app,base_url='http://localhost') as client:
        assert len(client.get('/api/strategies?market=CN').json())==6
        changed=client.post('/api/strategies/breakout/config',headers=headers,json={'market':'CN','parameters':{'rvol_min':1.9}});assert changed.status_code==200
        versions=client.get('/api/strategies/breakout/versions?market=CN').json();assert len(versions)==2
        restored=client.post('/api/strategies/breakout/rollback',headers=headers,json={'market':'CN','version':versions[-1]['version']})
        assert restored.status_code==200 and restored.json()['parameters']['rvol_min']==1.5


def test_summary_includes_all_four_strategies():
    summary=Simulation().summary()
    assert set(DEFINITIONS)<=set(summary['CN']['strategies'])


def test_current_new_strategy_signal_reaches_decision_and_same_stock_is_merged():
    t=datetime(2026,9,8,14,0,tzinfo=timezone.utc)
    quote=Quote('AAPL.US','longbridge','Apple',100.1,t,t,quality='realtime',session='regular',
                bid=100.09,ask=100.1,depth_time=t,bid_size=5000,ask_size=5000,trade_status='Normal')
    evidence={'bar':{'volume':100000},'relative_volume':2,'relative_strength':4,'signal_minutes':5}
    signals=[Signal('trend','AAPL.US','trend_pullback','longbridge',t-timedelta(minutes=2),100,99,'normal',evidence,'trend-v3').dump(),
             Signal('orb','AAPL.US','breakout','longbridge',t-timedelta(minutes=2),100,99,'normal',evidence,'orb-v4').dump()]
    active={('US','trend_pullback'):'trend-v3',('US','breakout'):'orb-v4'}
    rows=recommend(signals,{'AAPL.US':quote},{'AAPL.US':{'ready':True,'eligible':True,'source':'longbridge'}},
                   Simulation().state,t,active_versions=active)
    assert len(rows)==1 and rows[0]['state']=='buy'
    assert set(rows[0]['strategies'])=={'trend_pullback','breakout'}


def test_trend_pullback_engine_emits_current_registry_version():
    symbol='AAPL.US';market='US';day=datetime(2026,9,8,tzinfo=timezone.utc).date()
    sessions=list(calendar(market).sessions_in_range('2026-05-01','2026-09-07'))[-70:]
    daily=[];benchmark_daily=[]
    for i,session in enumerate(sessions):
        t=open_time(session.date(),market);price=100+i
        daily.append(Bar(symbol,'longbridge',t,price,price+1,price-1,price+.5,1_000_000,20_000_000))
        benchmark_daily.append(Bar('SPY.US','longbridge',t,500+i*.2,501+i*.2,499+i*.2,500.1+i*.2,2_000_000,1_000_000_000))
    engine=ResearchEngine();engine.prepare(symbol,daily,[],benchmark_daily)
    start=open_time(day,market);bars=[];bench=[]
    for i in range(19):
        price=170+i*.05
        bars.append(Bar(symbol,'longbridge',start+timedelta(minutes=5*i),price,price+.12,price-.1,price+.02,1000,170000))
        bp=500+i*.05;bench.append(Bar('SPY.US','longbridge',bars[-1].start,bp,bp+.2,bp-.1,bp+.1,2000,1_000_000))
    bars.append(Bar(symbol,'longbridge',start+timedelta(minutes=95),171,171.3,170,171.2,1000,171200))
    bars.append(Bar(symbol,'longbridge',start+timedelta(minutes=100),171.25,172.1,171.2,172,2000,344000))
    for b in bars[19:]:
        bp=501;btime=b.start;bench.append(Bar('SPY.US','longbridge',btime,bp,bp+.3,bp-.1,bp+.2,2000,1_002_000))
    engine.set_benchmark(market,bench)
    signals=[]
    for bar in bars:signals.extend(engine.update(bar))
    trend=[s for s in signals if s.strategy=='trend_pullback']
    assert len(trend)==1 and trend[0].version==engine.registry.current('trend_pullback','US')['version']


def test_volatility_contraction_breakout_emits_signal_with_same_time_volume():
    symbol='AAPL.US';market='US';day=datetime(2026,9,8,tzinfo=timezone.utc).date()
    sessions=list(calendar(market).sessions_in_range('2026-05-01','2026-09-07'))[-70:]
    daily=[];benchmark_daily=[]
    for i,session in enumerate(sessions):
        t=open_time(session.date(),market);price=100+i
        spread=.25 if i>=65 else 2
        daily.append(Bar(symbol,'longbridge',t,price,price+spread,price-spread,price+.1,1_000_000,20_000_000))
        benchmark_daily.append(Bar('SPY.US','longbridge',t,500+i*.2,501+i*.2,499+i*.2,500.1+i*.2,2_000_000,1_000_000_000))
    historical=[]
    for session in sessions[-14:]:
        t=open_time(session.date(),market)
        historical.extend([Bar(symbol,'longbridge',t,168.5,169,168,168.7,100,16870),
                           Bar(symbol,'longbridge',t+timedelta(minutes=5),168.7,169,168.5,168.8,100,16880)])
    engine=ResearchEngine();engine.prepare(symbol,daily,historical,benchmark_daily)
    level=max(b.high for b in daily[-10:]);start=open_time(day,market)
    bars=[Bar(symbol,'longbridge',start,level-.4,level-.1,level-.5,level-.2,200,34000),
          Bar(symbol,'longbridge',start+timedelta(minutes=5),level-.1,level+.4,level-.2,level+.2,200,34000)]
    bench=[Bar('SPY.US','longbridge',b.start,500,501,499.9,500.8,2000,1_000_000) for b in bars]
    engine.set_benchmark(market,bench)
    signals=[]
    for bar in bars:signals.extend(engine.update(bar))
    contraction=[s for s in signals if s.strategy=='volatility_breakout']
    assert len(contraction)==1 and contraction[0].evidence['relative_volume']>=1.5


def test_single_stock_analysis_uses_cached_data_without_mutating_holdings_or_monitoring(tmp_path):
    dashboard=Dashboard(tmp_path);symbol='AAPL.US';start=datetime(2026,6,1,tzinfo=timezone.utc)
    daily=[];benchmark=[]
    for i in range(70):
        daily.append(Bar(symbol,'longbridge',start+timedelta(days=i),100+i*.4,101+i*.4,99+i*.4,100.5+i*.4,1_000_000,20_000_000))
        benchmark.append(Bar('SPY.US','longbridge',start+timedelta(days=i),500+i,501+i,499+i,500.5+i,2_000_000,1_000_000_000))
    dashboard.strategies.context[symbol]={'daily_bars':daily,'benchmark_daily':benchmark,'daily':{},'sessions':{},'source':'longbridge'}
    dashboard.strategies.history[symbol]=[Bar(symbol,'longbridge',now()-timedelta(minutes=10),127,128,126,127.5,10000,1_000_000)]
    before_watch=list(dashboard.watch);before_holdings=deepcopy(dashboard.real.list())
    result=asyncio.run(dashboard.analyze_stock({'symbol':symbol,'holding':{'quantity':10,'cost':120,'stop':114,'target':132}}))
    assert result['symbol']==symbol and len(result['strategies'])==7
    assert result['holding_origin']=='temporary' and result['holding']['quantity']==10
    assert dashboard.watch==before_watch and dashboard.real.list()==before_holdings
    assert '不会自动写入' in result['note']
    assert result['mode']=='snapshot' and result['data_status']=='partial'
    assert result['data_profile']['daily']['count']==70
    assert result['data_profile']['same_time_volume']['sessions']==0
    assert result['available_assessments']['daily_trend']
    assert any('14日同时间' in reason for reason in result['blocked_conditions'])


def test_chinese_name_analysis_returns_cached_partial_evidence_instead_of_no_data(tmp_path):
    dashboard=Dashboard(tmp_path);symbol='600206.SH';start=now()-timedelta(days=90)
    dashboard.security_map[symbol]={'code':'600206.SH','name':'有研新材','证券类型':'1'}
    daily=[];benchmark=[]
    for i in range(70):
        moment=start+timedelta(days=i);price=10+i*.05
        daily.append(Bar(symbol,'longbridge',moment,price,price+.2,price-.2,price+.1,2_000_000,30_000_000))
        benchmark.append(Bar('000300.SH','longbridge',moment,4000+i,4002+i,3998+i,4001+i,2_000_000,1_000_000_000))
    dashboard.strategies.context[symbol]={'daily_bars':daily,'benchmark_daily':benchmark,'daily':{},'sessions':{},'source':'longbridge'}
    dashboard.strategies.history[symbol]=[Bar(symbol,'longbridge',now()-timedelta(minutes=10),13,13.2,12.9,13.1,10000,131000)]
    async def no_quote(symbols):return []
    dashboard.lingxi.quotes=no_quote
    result=asyncio.run(dashboard.analyze_stock({'symbol':'有研新材'}))
    assert result['symbol']==symbol and result['name']=='有研新材'
    assert result['data_status']=='partial' and result['data_profile']['daily']['count']==70
    assert result['technical']['ma20'] is not None and len(result['strategies'])==7
    assert next(row for row in result['strategies'] if row['strategy']=='orb20_us')['state']=='不适用'
    assert result['final']['status']=='WAIT' and dashboard.tracked()==[]


def test_add_monitoring_reports_capacity_and_does_not_evict_existing_symbols(tmp_path):
    dashboard=Dashboard(tmp_path);dashboard.settings['monitor_limit']=1;dashboard.watch=['MSFT.US']
    with pytest.raises(ValueError,match='1/1'):
        asyncio.run(dashboard.add_watch('AAPL.US'))
    assert dashboard.watch==['MSFT.US']

    dashboard.watch=[]
    async def warm(symbols=None):
        dashboard.validation['AAPL.US']={'ready':True,'eligible':True,'source':'longbridge'}
        dashboard.lb.subscribed.add('AAPL.US')
    dashboard.refresh_bars=warm;dashboard.lb.status['stream']=True
    result=asyncio.run(dashboard.add_watch('AAPL.US'))
    assert result['monitored'] and result['warm'] and result['transport']=='push'
    assert result['capacity']=={'used':1,'limit':1,'available':0,'symbols':['AAPL.US']}
