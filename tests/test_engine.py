from datetime import timedelta,date
import copy
import pytest
from app.models import Bar,Signal,stamp
from app.calendars import is_open,adjacent,price_limits
from app.providers import share_volume,normalize_lingxi
from app.strategy import Strategies
from app.simulation import Simulation
from app.store import Store
from app.service import Dashboard

def bar(t='2026-09-01T14:00:00Z',symbol='AAPL.US',**kw):
    return Bar(symbol,'longbridge',stamp(t),**({'open':100.,'high':101.,'low':99.5,'close':100.5,'volume':100000.}|kw))

def signal(b,**kw):
    return Signal('test',b.symbol,'breakout',b.source,b.start,**({'trigger':100.,'stop':99.,'risk_group':'normal','evidence':{}}|kw))

def entered(**kw):
    b=bar(**kw);s=Simulation();s.queue(signal(b));s.process(b)
    return s,b

def test_time_offsets():
    assert stamp('2026-09-01T09:30:00-04:00')==stamp('2026-09-01T13:30:00Z')
    assert stamp(1788269400000).tzinfo is not None

def test_calendar_lunch_and_holiday():
    assert not is_open(stamp('2026-09-06T02:00Z'),'CN')
    assert not is_open(stamp('2026-09-07T14:00Z'),'US') # Labor Day
    assert not is_open(stamp('2026-09-01T03:30Z'),'CN')
    assert not is_open(stamp('2026-09-01T07:00Z'),'CN')
    assert adjacent(stamp('2026-09-01T03:25Z'),stamp('2026-09-01T05:00Z'),'CN')
    assert not adjacent(stamp('2026-09-01T02:00Z'),stamp('2026-09-01T02:10Z'),'CN')
    assert not is_open(stamp('2027-01-04T14:00Z'),'US')

def test_historical_limits_and_rounding():
    assert price_limits(10,'600001.SH','st',date(2026,7,3))==(10.5,9.5)
    assert price_limits(10,'600001.SH','st',date(2026,7,6))==(11.,9.)
    assert price_limits(10,'300001.SZ','st',date(2026,9,1))==(12.,8.)

def test_volume_units_fail_closed():
    assert share_volume(100,100000,9.9,10.1)==10000
    assert share_volume(100,1000,9.9,10.1)==100
    with pytest.raises(ValueError):share_volume(100,None,9.9,10.1)
    with pytest.raises(ValueError):share_volume(100,5000,9.9,10.1)

def test_st_is_a_chinese_security_flag():
    assert Dashboard.is_st('600001.SH','*ST测试')
    assert not Dashboard.is_st('TITN.US','TITAN INDUSTRIES')

def breakout_bars(start='2026-09-01T13:30Z'):
    t=stamp(start)
    bars=[bar(t+timedelta(minutes=5*i),open=100+i*.05,close=100+i*.05,low=99.9+i*.05,high=100.1+i*.05,volume=1000) for i in range(22)]
    bars.append(bar(t+timedelta(minutes=110),open=101.1,close=102,high=102.1,low=101.05,volume=2000))
    return bars

def test_final_breakout_and_deduplication():
    s=Strategies();bs=breakout_bars()
    for b in bs[:-1]:assert not s.update(b)
    unfinished=copy.copy(bs[-1]);unfinished.final=False
    assert not s.update(unfinished)
    result=s.update(bs[-1]);assert len(result)==1 and result[0].strategy=='breakout'
    assert result[0].time==bs[-1].end
    assert not s.update(bs[-1])
    alien=bar(bs[-1].end);alien.source='ibkr'
    assert not s.update(alien)

def test_partial_day_cannot_claim_day_vwap():
    s=Strategies()
    assert not [r for b in breakout_bars('2026-09-01T14:00Z') for r in s.update(b)]

def test_first_pullback_and_invalidation():
    s=Strategies();bs=breakout_bars()
    for b in bs:s.update(b)
    touch=bar(bs[-1].end,open=101.6,high=101.8,low=101.1,close=101.4,volume=1000)
    assert not s.update(touch)
    restart=bar(touch.end,open=101.5,high=102,low=101.3,close=101.9,volume=1000)
    assert any(x.strategy=='pullback' for x in s.update(restart))
    s=Strategies()
    for b in bs:s.update(b)
    s.update(touch)
    s.update(bar(touch.end,open=101.3,high=102,low=101,close=101.9,volume=1000))
    assert 'AAPL.US' not in s.pullbacks

def test_next_bar_only_and_restart_dedup():
    s,b=entered();a=s.state['accounts']['US']
    assert len(a['positions'])==1 and a['cash']>=0
    prior=copy.deepcopy(s.state)
    s.process(b);assert s.state==prior
    recovered=Simulation(state=copy.deepcopy(s.state));recovered.process(b)
    assert recovered.state==s.state
    late=Simulation();late.queue(signal(b));late.process(bar(b.end))
    assert not late.state['accounts']['US']['positions']

def test_gap_no_chase_and_locked_limit():
    for b in [bar(open=102,high=103,low=101,close=102),bar(open=98,high=101,low=97),bar(low=100,open=100,close=100,high=100,limit_up=100)]:
        s=Simulation();s.queue(signal(b));s.process(b)
        assert not s.state['accounts']['US']['positions']
        assert s.state['failures']

def test_same_bar_stop_before_target():
    s,b=entered(low=98,high=104)
    a=s.state['accounts']['US']
    assert not a['positions'] and a['trades'][0]['net_pnl']<0
    assert a['trades'][0]['exits'][0]['reason']=='止损／跟踪止损'

def test_china_t1_and_limit_down_pending():
    s,b=entered(t='2026-09-01T02:00Z',symbol='600001.SH',low=98,high=104)
    a=s.state['accounts']['CN'];p=a['positions'][b.symbol]
    assert p['pending_exit'] and p['remaining']%100==0 and not a['trades']
    s.process(bar('2026-09-02T01:30Z',symbol=b.symbol,open=90,high=90,low=90,close=90,limit_down=90))
    assert b.symbol in a['positions']
    s.process(bar('2026-09-02T01:35Z',symbol=b.symbol,open=91,high=92,low=90,close=91,limit_down=90))
    assert not a['positions'] and a['trades'][0]['net_pnl']<0

def test_trailing_requires_three_subsequent_complete_bars():
    s,b=entered();a=s.state['accounts']['US'];p=a['positions'][b.symbol]
    s.process(bar(b.end,open=102,high=103,low=101.5,close=102.8))
    assert p['partial'] and p['lows']==[] and p['stop']==99
    for i in range(2):
        s.process(bar(b.end+timedelta(minutes=5*(i+1)),open=103,high=104,low=102,close=103))
        assert p['stop']==99
    s.process(bar(b.end+timedelta(minutes=15),open=103,high=104,low=102,close=103))
    assert p['stop']==102

def test_overdue_after_halt_exits_at_next_open():
    s,b=entered();a=s.state['accounts']['US']
    s.process(bar('2026-09-04T13:30Z',open=101,high=102,low=100,close=101.5))
    assert not a['positions'] and a['trades'][0]['exits'][0]['reason']=='第三交易日到期'

def test_risk_slot_and_three_position_limit():
    s=Simulation()
    for i in range(4):
        b=bar(symbol=f'T{i}.US')
        sig=signal(b);sig.id=f'id{i}';s.queue(sig);s.process(b)
    assert len(s.state['accounts']['US']['positions'])==3
    s=Simulation()
    for i in range(2):
        b=bar(symbol=f'T{i}.US',high=102);sig=signal(b,risk_group='smallcap',stop=95);sig.id=f'id{i}'
        s.queue(sig);s.process(b)
    assert len(s.state['accounts']['US']['positions'])==1

def test_store_immutable_signal_evidence(tmp_path):
    st=Store(tmp_path/'test.db');b=bar();st.bar(b);original=b.dump();b.close=100.8;st.bar(b)
    assert st.bars(b.symbol,b.source)==[original]
    sig=signal(b);assert st.signal(sig);assert not st.signal(sig)

def test_pause_cancels_pending_and_retains_exposure():
    s,b=entered();s.mark_unobservable(b.symbol,'连接中断')
    assert s.state['accounts']['US']['positions'][b.symbol]['observation_gap']
    assert not s.state['pending']

def test_cost_sensitivity_is_worse_than_net():
    s,b=entered(low=98,high=104);r=s.summary()['US']['strategies']['breakout']
    assert r['double_cost_pnl']<r['net_pnl'] and r['count']==1 and not r['enough']
