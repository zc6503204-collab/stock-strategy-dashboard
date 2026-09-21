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


def first_pullback_fixture():
    symbol='600000.SH';daily,benchmark=daily_fixture(symbol,count=70)
    for b in daily:b.turnover=200_000_000
    for b,(o,h,l,c,v) in zip(daily[-3:],[(107,113,106.9,112,2_000_000),
            (110,111,107,107.2,500_000),(107.2,108,106.8,107,500_000)]):
        b.open=o;b.high=h;b.low=l;b.close=c;b.volume=v
    closing=close_time(daily[-1].start.date(),'CN');history=[]
    for i in range(30):
        history.append(Bar(symbol,'longbridge',closing-timedelta(minutes=5*(30-i)),107.1,107.2,107,107.1,1000,107100))
    engine=ResearchEngine();engine.prepare(symbol,daily,history,benchmark)
    start=open_time(datetime(2026,9,8).date(),'CN')
    today=[Bar(symbol,'longbridge',start,107.1,107.3,106.9,107.1,1000,107100),
           Bar(symbol,'longbridge',start+timedelta(minutes=5),107.1,108.1,107.05,108,1600,172800)]
    benchmark_today=[Bar('000300.SH','longbridge',b.start,500,501,499.9,500.8,1000,500800) for b in today]
    engine.set_benchmark('CN',benchmark_today)
    return engine,symbol,daily,benchmark,history,today


def test_first_pullback_confirms_in_early_morning_without_fourteen_day_baseline():
    engine,symbol,daily,benchmark,history,today=first_pullback_fixture()
    factors=engine._daily(symbol,'first_pullback')
    assert factors['eligible'] and factors['pullback_sessions']==2 and factors['volume_contraction']<=.8
    assert engine.update(today[0])==[]
    signals=engine.update(today[1]);found=next(s for s in signals if s.strategy=='first_pullback')
    assert found.time==open_time(datetime(2026,9,8).date(),'CN')+timedelta(minutes=10)
    assert found.stop==106.79 and found.evidence['structural_stop']==106.79
    assert found.evidence['setup']=='S04' and found.evidence['target_price']>found.trigger
    assert found.evidence['entry_deadline'].startswith('2026-09-08T06:45:00')
    assert found.evidence['signal_minutes']==5
    preview=engine.preview(symbol)
    assert preview['ready'] and preview['strategies']['first_pullback']['ready']
    assert not preview['strategies']['vcp_swing']['ready']
    assert not preview['strategies']['breakout']['ready']
    # Rechecking the same structure preserves its identity and does not depend
    # on transient quote time, while each 5m signal still has its own expiry.
    again=next(s for s in engine.evaluate(symbol) if s.strategy=='first_pullback')
    assert again.evidence['structure_id']==found.evidence['structure_id']


def test_first_pullback_rejects_second_leg_heavy_volume_and_broken_support():
    engine,symbol,daily,benchmark,history,today=first_pullback_fixture()
    daily[-1].volume=2_000_000
    engine.prepare(symbol,daily,history,benchmark)
    assert not engine._daily(symbol,'first_pullback')['eligible']
    daily[-1].volume=500_000;daily[-1].close=105;daily[-1].low=104.9
    engine.prepare(symbol,daily,history,benchmark)
    assert not engine._daily(symbol,'first_pullback')['eligible']
    engine,symbol,daily,benchmark,history,today=first_pullback_fixture()
    daily[-2].high=107.5;daily[-1].high=109;daily[-1].close=108.8
    engine.prepare(symbol,daily,history,benchmark)
    assert not engine._daily(symbol,'first_pullback')['eligible']
    engine,symbol,daily,benchmark,history,today=first_pullback_fixture()
    today[0].low=106.5
    for b in today:assert not [s for s in engine.update(b) if s.strategy=='first_pullback']
    assert engine.preview(symbol)['strategies']['first_pullback']['status']=='invalid'


def test_prior_session_ema_warmup_reuses_only_same_source_contiguous_completed_bars():
    engine,symbol,daily,benchmark,history,today=first_pullback_fixture()
    for b in today:engine.update(b)
    view=engine.preview(symbol)['strategies']['trend_pullback']
    assert view['ready']  # 09:40, instead of waiting for today's 21st bar.
    history[-1].source='other'
    engine.prepare(symbol,daily,history,benchmark)
    engine.evaluate(symbol)
    assert not engine.preview(symbol)['strategies']['trend_pullback']['ready']
    history[-1].source='longbridge';history[-1].final=False
    engine.prepare(symbol,daily,history,benchmark)
    engine.evaluate(symbol)
    assert not engine.preview(symbol)['strategies']['trend_pullback']['ready']


def test_engine_revision_migration_preserves_old_results_and_user_parameters(tmp_path):
    from copy import deepcopy
    from app.store import Store
    store=Store(tmp_path/'revision.db');registry=StrategyRegistry(store)
    old=registry.revisions['CN']['vcp_swing'][0]
    old['version']='vcp_swing 1.0.0';old.pop('engine_revision',None)
    old['parameters']['rvol_min']=1.9;old['replay']={'state':'exploration','count':3}
    registry.active['CN']['vcp_swing']=old['version'];before=deepcopy(old);registry._save()
    migrated=StrategyRegistry(store);current=migrated.current('vcp_swing','CN')
    assert current['version']!=old['version'] and current['parent_version']==old['version']
    assert current['parameters']['rvol_min']==1.9 and current['replay'] is None
    assert migrated.versions('vcp_swing','CN')[-1]==before
    assert len(StrategyRegistry(store).versions('vcp_swing','CN'))==2


def test_vcp_cn_uses_daily_contraction_stop_and_stable_anchor():
    symbol='600000.SH';daily,benchmark=daily_fixture(symbol,count=260)
    for b in daily:b.turnover=200_000_000
    engine=ResearchEngine();engine.prepare(symbol,daily,[],benchmark,{'percentile':90,'coverage':4000})
    factors=engine._daily(symbol,'vcp_swing');assert factors['eligible']
    level=max(b.high for b in daily[-10:]);start=open_time(datetime(2026,9,8).date(),'CN')
    bars=[Bar(symbol,'longbridge',start,level-.2,level-.05,level-.3,level-.1,100,12000),
          Bar(symbol,'longbridge',start+timedelta(minutes=5),level-.1,level+.3,level-.2,level+.2,200,24000)]
    frames=[{'bar':b,'vwap':b.close-.1,'benchmark':Bar('000300.SH','longbridge',b.start,500,502,499,501,100),
             'benchmark_vwap':500} for b in bars]
    found,view=engine._vcp_swing(symbol,bars,frames,[2,2],'normal',factors)
    assert found and found[0].stop==round(min(b.low for b in daily[-5:])-.01,2)
    assert found[0].evidence['max_hold_sessions']==10 and found[0].evidence['exit_policy']['trail']=='daily_3low'
    assert found[0].evidence['structure_id'] and found[0].evidence['structure_anchor']


def test_cn_execution_revision_isolates_old_breakout_paper_version_without_changing_us(tmp_path):
    from copy import deepcopy
    from app.store import Store
    store=Store(tmp_path/'execution.db');registry=StrategyRegistry(store)
    old=registry.revisions['CN']['breakout'][0]
    old['version']='实验 2.0.0';old.pop('execution_revision',None);old['enabled']=False
    old['parameters']['rvol_min']=2.2;old['replay']={'count':8,'net_pnl':123.}
    registry.active['CN']['breakout']=old['version'];before=deepcopy(old)
    old_us=deepcopy(registry.revisions['US']);registry._save()
    migrated=StrategyRegistry(store);current=migrated.current('breakout','CN')
    assert current['execution_revision']=='dynamic-plans-1' and current['version']!=before['version']
    assert current['parameters']==before['parameters'] and current['enabled'] is False
    assert current['replay'] is None and migrated.versions('breakout','CN')[-1]==before
    assert migrated.revisions['US']==old_us
    signal=Signal('old-paper','600000.SH','breakout','longbridge',datetime.now(timezone.utc),10,9,'normal',{},before['version'])
    assert signal.version!=migrated.active_versions()[('CN','breakout')]
    simulation=Simulation();simulation.state['accounts']['CN']['trades']=[
        {'strategy':'breakout','version':before['version'],'net_pnl':123.,'exit_time':signal.time.isoformat()}]
    assert simulation.performance('CN','breakout',before['version'])['portfolio']['count']==1
    assert simulation.performance('CN','breakout',current['version'])['portfolio']['count']==0
    from app.decisions import check
    rejected=check(signal,None,{},simulation.state['accounts']['CN'],simulation.state['params'],signal.time,migrated.active_versions())
    assert rejected['state']=='avoid' and '旧版' in rejected['reason']
    changed=migrated.save('breakout','CN',{'rvol_min':2.3})
    assert changed['execution_revision']==current['execution_revision']
    assert len(StrategyRegistry(store).versions('breakout','CN'))==3
