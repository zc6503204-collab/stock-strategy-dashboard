from copy import deepcopy
from datetime import datetime,timedelta

from app.calendars import calendar,open_time,close_time
from app.models import Bar
from app.replay import replay_signals
from app.strategy_registry import StrategyRegistry


def inputs():
    symbol='600000.SH';days=list(calendar('CN').sessions_in_range('2026-05-01','2026-09-07'))[-70:]
    daily=[];benchmark=[]
    for i,d in enumerate(days):
        t=open_time(d.date(),'CN');price=100+i*.1
        daily.append(Bar(symbol,'longbridge',t,price,price+.2,price-.2,price,1_000_000,200_000_000))
        benchmark.append(Bar('000300.SH','longbridge',t,500,500.1,499.9,500,2_000_000,1_000_000_000))
    for b,(o,h,l,c,v) in zip(daily[-3:],[(107,113,106.9,112,2_000_000),
            (110,111,107,107.2,500_000),(107.2,108,106.8,107,500_000)]):
        b.open=o;b.high=h;b.low=l;b.close=c;b.volume=v
    closing=close_time(days[-1].date(),'CN')
    minute=[Bar(symbol,'longbridge',closing-timedelta(minutes=5*(30-i)),107.1,107.2,107,107.1,1000,107100) for i in range(30)]
    start=open_time(datetime(2026,9,8).date(),'CN')
    minute.extend([Bar(symbol,'longbridge',start,107.1,107.3,106.9,107.1,1000,107100),
                   Bar(symbol,'longbridge',start+timedelta(minutes=5),107.1,108.1,107.05,108,1600,172800)])
    bm=[Bar('000300.SH','longbridge',b.start,500,501,499.9,500.8,1000,500800) for b in minute]
    return {'registry':StrategyRegistry(),'bars':minute,'daily':daily,'benchmark_daily':benchmark,
            'benchmark_intraday':bm,'strategy':'first_pullback','as_of':minute[-1].end}


def test_live_engine_replays_technical_signal_but_missing_context_never_becomes_returns():
    data=inputs();versions=deepcopy(data['registry'].revisions)
    result=replay_signals('600000.SH','longbridge',**data)
    assert result['engine']=='ResearchEngine' and result['signal_counts']['first_pullback']==1
    assert result['signals'][0]['version']==data['registry'].current('first_pullback','CN')['version']
    assert result['state']=='insufficient_data' and result['result']=={} and not result['profit_computed']
    assert any('当时市场' in r['reason'] for r in result['missing'])
    assert any('涨跌停' in r['reason'] for r in result['missing'])
    assert data['registry'].revisions==versions


def test_future_daily_closes_benchmarks_and_context_cannot_rewrite_earlier_signals():
    data=inputs();base=replay_signals('600000.SH','longbridge',**data)
    today=data['bars'][-1].start.replace(hour=1,minute=30)
    data['daily']=data['daily']+[Bar('600000.SH','longbridge',today,2,999,1,999,100_000_000,1e12)]
    data['benchmark_daily']=data['benchmark_daily']+[Bar('000300.SH','longbridge',today,500,1000,400,999,10000,1e9)]
    data['benchmark_intraday']=data['benchmark_intraday']+[Bar('000300.SH','longbridge',data['as_of'],500,999,100,999,10000,1e9)]
    data['context_history']=[{'as_of':(data['as_of']+timedelta(minutes=1)).isoformat(),'risk_group':'normal',
                              'relative_rank':{'percentile':100,'coverage':5000},
                              'research_chain':{'passed':True,'checks':[{'layer':'市场','passed':True}]}}]
    result=replay_signals('600000.SH','longbridge',**data)
    assert result['signal_counts']==base['signal_counts'] and result['signals']==base['signals']
    assert result['coverage']['complete_context_bars']==0


def test_missing_current_benchmark_bar_blocks_signal_instead_of_using_future_bar():
    data=inputs();data['benchmark_intraday']=data['benchmark_intraday'][:-1]
    result=replay_signals('600000.SH','longbridge',**data)
    assert result['signal_count']==0 and result['state']=='insufficient_data'
    assert any('基准分钟线' in r['reason'] for r in result['missing'])


def test_time_split_purges_longest_holding_period_without_parameter_search():
    data=inputs();result=replay_signals('600000.SH','longbridge',**data)
    validation=result['validation']
    assert validation['purge_sessions']==3 and validation['validation_sessions']==0
    assert validation['max_parameter_trials']==9 and validation['parameter_trials']==0
    assert not validation['optimization_performed']


def test_vcp_does_not_borrow_current_cross_section_rank():
    data=inputs();data['strategy']='vcp_swing'
    # A snapshot collected later than replay as_of is deliberately inadmissible.
    data['context_history']=[{'checked_at':(data['as_of']+timedelta(days=1)).isoformat(),
                              'relative_rank':{'percentile':99,'coverage':5000},'risk_group':'normal'}]
    result=replay_signals('600000.SH','longbridge',**data)
    assert result['signal_count']==0 and result['state']=='insufficient_data'
    assert result['validation']['purge_sessions']==10
    assert any('VCP需要260' in r['reason'] for r in result['strategy_blockers'])
