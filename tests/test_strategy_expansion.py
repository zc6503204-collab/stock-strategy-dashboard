from datetime import datetime,timedelta,timezone

from fastapi.testclient import TestClient

from app.calendars import calendar,close_time,open_time
from app.models import Bar,Quote,Signal
from app.research import ResearchEngine
from app.service import Dashboard
from app.simulation import Simulation
from app.strategy_registry import StrategyRegistry,DEFINITIONS


def daily_fixture(symbol='AAPL.US',count=260):
    market='US' if symbol.endswith('.US') else 'CN'
    sessions=list(calendar(market).sessions_in_range('2025-08-01','2026-09-07'))[-count:]
    daily=[];benchmark=[]
    closes=[100+i*.1 for i in range(count)]
    for i,(session,price) in enumerate(zip(sessions,closes)):
        spread=.2 if i>=count-5 else .5 if i>=count-25 else 1.
        moment=open_time(session.date(),market)
        daily.append(Bar(symbol,'longbridge',moment,price-.05,price+spread,price-spread,price,1_000_000,20_000_000))
        base=500+i*.02
        benchmark.append(Bar('SPY.US' if market=='US' else '000300.SH','longbridge',moment,base,base+.2,base-.2,base+.01,2_000_000,1_000_000_000))
    return daily,benchmark


def test_registry_exposes_horizon_market_and_exit_metadata(tmp_path):
    registry=StrategyRegistry()
    cn={row['strategy']:row for row in registry.list('CN')}
    us={row['strategy']:row for row in registry.list('US')}
    assert 'orb20_us' not in cn and 'orb20_us' in us
    assert cn['vcp_swing']['horizon']=='swing' and cn['vcp_swing']['max_hold_sessions']==10
    assert us['orb20_us']['exit_policy']['type']=='orb_fixed'
    assert all(row['enabled'] for row in us.values())


def test_trend_rsi_requires_prior_strength_then_current_pullback():
    engine=ResearchEngine();daily,benchmark=daily_fixture(count=70)
    prices=[100+i*.4 for i in range(65)]
    for move in [1,1,-1,-2,-2]:prices.append(prices[-1]+move)
    for index,(bar,price) in enumerate(zip(daily,prices)):
        bar.open=price-.1;bar.high=price+.2;bar.low=price-.2;bar.close=price
        bar.volume=500_000 if index>=65 else 1_000_000
    engine.prepare('AAPL.US',daily,[],benchmark)
    factors=engine._daily('AAPL.US','trend_rsi_pullback')
    assert factors['eligible']
    assert 45<=factors['rsi']<=60 and factors['rsi_recent_peak']>=65
    daily[-1].close=daily[-1].open=daily[-1].high=daily[-1].low=prices[-1]-8
    engine.prepare('AAPL.US',daily,[],benchmark)
    assert not engine._daily('AAPL.US','trend_rsi_pullback')['eligible']


def test_vcp_requires_260_bars_and_cross_section_rank():
    engine=ResearchEngine();daily,benchmark=daily_fixture(count=259)
    engine.prepare('AAPL.US',daily,[],benchmark,{'percentile':90,'coverage':20})
    assert not engine._daily('AAPL.US','vcp_swing')['eligible']
    daily,benchmark=daily_fixture(count=260)
    engine.prepare('AAPL.US',daily,[],benchmark,{'percentile':90,'coverage':20})
    factors=engine._daily('AAPL.US','vcp_swing')
    assert factors['eligible'] and factors['rank_coverage']==20
    engine.set_relative_rank('AAPL.US',50,20)
    assert not engine._daily('AAPL.US','vcp_swing')['eligible']


def test_us_orb20_only_signals_inside_entry_window_with_fixed_target():
    daily,benchmark_daily=daily_fixture(count=70);symbol='AAPL.US';market='US'
    prior_sessions=list(calendar(market).sessions_in_range('2026-08-01','2026-09-07'))[-14:]
    historical=[]
    for session in prior_sessions:
        start=open_time(session.date(),market)
        for index in range(25):
            historical.append(Bar(symbol,'longbridge',start+timedelta(minutes=5*index),100,100.2,99.8,100,100,10_000))
    engine=ResearchEngine();engine.prepare(symbol,daily,historical,benchmark_daily)
    day=datetime(2026,9,8,tzinfo=timezone.utc).date();start=open_time(day,market)
    bars=[];bench=[]
    for index in range(7):
        close=100.1 if index<6 else 100.8
        high=100.4 if index<6 else 100.9
        volume=100 if index<6 else 500
        bars.append(Bar(symbol,'longbridge',start+timedelta(minutes=5*index),100,high,99.9,close,volume,volume*close))
        bench.append(Bar('SPY.US','longbridge',bars[-1].start,500,501,499.8,500.8,1000,500_800))
    engine.set_benchmark(market,bench);signals=[]
    for bar in bars:signals.extend(engine.update(bar))
    found=[signal for signal in signals if signal.strategy=='orb20_us']
    assert len(found)==1 and found[0].time==bars[-1].end
    assert found[0].evidence['exit_policy']['type']=='orb_fixed'
    assert found[0].evidence['target_price']>found[0].trigger
    late=[]
    for index in range(7,25):
        close=100.8 if index==24 else 100.1
        bar=Bar(symbol,'longbridge',start+timedelta(minutes=5*index),100,100.9 if index==24 else 100.3,99.9,close,500,50_000)
        bars.append(bar);bench.append(Bar('SPY.US','longbridge',bar.start,500,501,499.8,500.8,1000,500_800))
        engine.set_benchmark(market,bench);late.extend(engine.update(bar))
    assert not [signal for signal in late if signal.strategy=='orb20_us']


def test_shadow_ledger_records_strategy_without_using_portfolio_cash():
    simulation=Simulation();start=datetime(2026,9,8,14,0,tzinfo=timezone.utc)
    evidence={'approval':{'time':start.isoformat(),'price':100.,'qty':10,'planned_risk':10.,'cash_required':1001.},
              'horizon':'intraday','max_hold_sessions':1,'target_price':101.5,
              'exit_policy':{'type':'orb_fixed','flat_minutes_before_close':10}}
    signal=Signal('shadow-one','AAPL.US','orb20_us','longbridge',start,100,99,'normal',evidence,'orb-test')
    simulation.queue_shadow(signal)
    simulation.process(Bar('AAPL.US','longbridge',start,100,100.5,99.5,100.2,10000,1_000_000))
    assert simulation.state['accounts']['US']['positions']=={}
    book=simulation.state['shadow']['books']['US|orb20_us|orb-test']
    assert 'shadow-one' in book['positions']
    simulation.process(Bar('AAPL.US','longbridge',start+timedelta(minutes=5),101,102,100.8,101.8,10000,1_000_000))
    metrics=simulation.performance('US','orb20_us','orb-test')['shadow']
    assert metrics['count']==1 and metrics['win_rate']==1 and metrics['win_rate_interval'][0]<1


def test_swing_slot_is_shared_only_by_portfolio():
    from app.risk import size_entry
    simulation=Simulation();account=simulation.state['accounts']['US']
    account['positions']['MSFT.US']={'remaining':1,'mark':100,'risk_group':'normal','horizon':'swing'}
    signal=Signal('vcp','AAPL.US','vcp_swing','longbridge',datetime.now(timezone.utc),100,98,'normal',
                  {'horizon':'swing','atr':2},'vcp-test')
    result=size_entry(signal,100,account,simulation.state['params'],10000)
    assert not result['ok'] and '波段' in result['reason']


def test_performance_api_returns_three_separate_evidence_lanes(tmp_path,monkeypatch):
    import app.main as main
    dashboard=Dashboard(tmp_path)
    async def noop():pass
    dashboard.start=noop;dashboard.stop=noop;monkeypatch.setattr(main,'dashboard',dashboard)
    with TestClient(main.app,base_url='http://localhost') as client:
        result=client.get('/api/strategies/vcp_swing/performance?market=US').json()
    assert result['horizon']=='swing'
    assert set(['forward_shadow','portfolio','historical'])<=set(result)


def test_orb20_position_is_closed_at_1550_new_york():
    simulation=Simulation();account=simulation.state['accounts']['US']
    session=datetime(2026,9,8,tzinfo=timezone.utc).date();closing=close_time(session,'US')
    account['positions']['AAPL.US']={
        'id':'orb-close','symbol':'AAPL.US','source':'longbridge','strategy':'orb20_us','version':'orb-test',
        'risk_group':'normal','entry':100.,'entry_time':open_time(session,'US').isoformat(),'qty':10,'remaining':10,
        'stop':98.,'initial_stop':98.,'target':105.,'planned_risk':20.,'mark':100.,'horizon':'intraday',
        'max_hold_sessions':1,'exit_policy':{'type':'orb_fixed','flat_minutes_before_close':10},
    }
    bar=Bar('AAPL.US','longbridge',closing-timedelta(minutes=15),100,100.4,99.8,100.2,10000,1_002_000)
    simulation.process(bar)
    assert 'AAPL.US' not in account['positions']
    assert account['trades'][-1]['exits'][-1]['reason']=='日内收盘前退出'


def test_vcp_position_exits_on_tenth_session_close():
    simulation=Simulation();account=simulation.state['accounts']['US']
    sessions=list(calendar('US').sessions_in_range('2026-08-20','2026-09-08'))[-10:]
    first=sessions[0].date();last=sessions[-1].date();closing=close_time(last,'US')
    account['positions']['AAPL.US']={
        'id':'vcp-close','symbol':'AAPL.US','source':'longbridge','strategy':'vcp_swing','version':'vcp-test',
        'risk_group':'normal','entry':100.,'entry_time':open_time(first,'US').isoformat(),'qty':10,'remaining':10,
        'stop':90.,'initial_stop':90.,'target':130.,'planned_risk':100.,'mark':100.,'horizon':'swing',
        'max_hold_sessions':10,'exit_policy':{'type':'risk_partial','target_r':2,'trail':'daily_3low'},
    }
    bar=Bar('AAPL.US','longbridge',closing-timedelta(minutes=5),101,101.5,100.5,101.2,10000,1_012_000)
    simulation.process(bar)
    assert 'AAPL.US' not in account['positions']
    assert account['trades'][-1]['exits'][-1]['reason']=='第10交易日到期'


def test_same_symbol_uses_one_portfolio_position_but_two_shadow_books():
    simulation=Simulation();start=datetime(2026,9,8,14,0,tzinfo=timezone.utc)
    approval={'time':start.isoformat(),'price':100.,'qty':10,'planned_risk':20.,'cash_required':1001.}
    common={'approval':approval,'horizon':'short','max_hold_sessions':3,
            'exit_policy':{'type':'risk_partial','target_r':2,'trail':'intraday_3bar'}}
    first=Signal('multi-breakout','AAPL.US','breakout','longbridge',start,100,98,'normal',dict(common),'breakout-test')
    second=Signal('multi-rsi','AAPL.US','trend_rsi_pullback','longbridge',start,100,98,'normal',dict(common),'rsi-test')
    for signal in (first,second):
        simulation.queue(signal);simulation.queue_shadow(signal)
    simulation.process(Bar('AAPL.US','longbridge',start,100,100.3,99.5,100.1,10000,1_001_000))
    assert list(simulation.state['accounts']['US']['positions'])==['AAPL.US']
    assert 'multi-breakout' in simulation.state['shadow']['books']['US|breakout|breakout-test']['positions']
    assert 'multi-rsi' in simulation.state['shadow']['books']['US|trend_rsi_pullback|rsi-test']['positions']
    assert any('已有该股模拟持仓' in row['reason'] for row in simulation.state['failures'])


def test_workspace_strategy_groups_are_market_isolated(tmp_path):
    dashboard=Dashboard(tmp_path)
    state=dashboard.snapshot()
    cn=state['workspaces']['CN']['strategy_groups'];us=state['workspaces']['US']['strategy_groups']
    assert all(row['strategy']!='orb20_us' for group in cn.values() for row in group['strategies'])
    assert any(row['strategy']=='orb20_us' for row in us['intraday']['strategies'])
    assert all(row['markets']==['US'] for row in us['intraday']['strategies'])


def test_a_share_new_short_strategy_always_requires_atr_for_sizing():
    from app.risk import size_entry
    simulation=Simulation();account=simulation.state['accounts']['CN']
    signal=Signal('cn-rsi','600206.SH','trend_rsi_pullback','longbridge',datetime.now(timezone.utc),10,9.8,'normal',
                  {'horizon':'short'},'TREND_RSI_PULLBACK 1.0.0')
    result=size_entry(signal,10,account,simulation.state['params'],10000)
    assert not result['ok'] and 'ATR' in result['reason']


def test_failed_fill_metrics_do_not_cross_markets():
    simulation=Simulation()
    t=datetime(2026,9,8,14,0,tzinfo=timezone.utc)
    simulation.fail('600206.SH','A股失败',t,'breakout','shared-version')
    simulation.fail('AAPL.US','美股失败',t,'breakout','shared-version')
    assert simulation.performance('CN','breakout','shared-version')['portfolio']['failed_fills']==1
    assert simulation.performance('US','breakout','shared-version')['portfolio']['failed_fills']==1
