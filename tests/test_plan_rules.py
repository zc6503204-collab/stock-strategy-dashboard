from datetime import timedelta
from decimal import Decimal

from app.models import Quote,Signal,stamp
from app.decisions import check,rank_key
from app.plan_rules import execution_range,tick_price
from app.simulation import Simulation


T=stamp('2026-09-08T02:00:00Z')


def signal(trigger=10,stop=9.5,target=11):
    return Signal('test','600000.SH','first_pullback','longbridge',T-timedelta(seconds=5),trigger,stop,'normal',
                  {'atr':.3,'structural_stop':stop,'target_price':target,'min_net_rr':1.5,
                   'price_tick':.01,'bar':{'volume':100000},'signal_minutes':5,
                   'entry_deadline':'2026-09-08T06:45:00Z'},'test-version')


def quote(price=10):
    return Quote('600000.SH','longbridge','浦发银行',price,T,T,bid=price-.01,ask=price,
                 quality='realtime',depth_time=T,bid_size=10000,ask_size=10000,trade_status='Normal')


def test_price_interval_is_executable_ticks_after_slippage_and_fees():
    cfg=Simulation().state['params'];s=signal();r=execution_range(s,cfg,quote())
    assert r['executable'] and r['estimated_fill']==10.01
    assert 10.01<=r['entry_max']<=10.12 and r['net_rr']>=1.5
    for k in ('entry_min','entry_max','stop','target','quote_max','estimated_fill'):
        assert Decimal(str(r[k]))%Decimal('.01')==0
    last=execution_range(s,cfg,quote(r['quote_max']))
    assert last['executable'] and last['net_rr']>=1.5
    above=execution_range(s,cfg,quote(round(r['quote_max']+.01,2)))
    assert not above['executable'] and '不追价' in above['reason']


def test_empty_tick_interval_is_explicit_and_never_widened():
    r=execution_range(signal(10,9.99,10.02),Simulation().state['params'])
    assert not r['executable'] and r['entry_max'] is None and '没有可成交' in r['reason']


def test_gross_two_r_can_fail_net_rr_and_higher_fees_cannot_improve_result():
    cfg=Simulation().state['params'];s=signal(100,98,104)
    base=execution_range(s,cfg)
    costly=execution_range(s,{**cfg,'fee_rate':.005})
    assert base['executable']
    assert not costly['executable'] and costly['net_rr']<1.5 and '费用' in costly['reason']


def test_minimum_commission_and_actual_quantity_are_accounted_for():
    cfg={**Simulation().state['params'],'minimum_fee':20};s=signal()
    small=execution_range(s,cfg,qty=100);larger=execution_range(s,cfg,qty=1000)
    assert not small['executable'] and larger['executable']


def test_quote_rounding_limit_up_and_invalid_inputs_fail_closed():
    cfg=Simulation().state['params'];s=signal()
    q=quote();q.limit_up=10.01
    assert not execution_range(s,cfg,q)['executable']
    q=quote(10.0001);r=execution_range(s,cfg,q)
    assert r['estimated_fill']==10.03
    assert not execution_range(signal(stop=float('nan')),cfg)['executable']
    assert tick_price(1.235,.01)==1.23 and tick_price(1.235,.01,up=True)==1.24


def test_decision_uses_the_same_range_and_upstream_gate_before_admission():
    s=signal();q=quote();sim=Simulation();cfg=sim.state['params'];account=sim.state['accounts']['CN']
    active={('CN','first_pullback'):s.version}
    validation={'ready':True,'eligible':True,'source':'longbridge','research_chain':{'passed':True}}
    result=check(s,q,validation,account,cfg,T,active)
    plan=execution_range(s,cfg,q)
    assert result['state']=='buy' and result['price']==plan['estimated_fill']
    assert result['entry_max']==plan['entry_max'] and result['target']==plan['target'] and result['net_rr']>=1.5
    validation['research_chain']={'passed':False,'reason':'公告核验缺失'}
    assert check(s,q,validation,account,cfg,T,active)['reason']=='公告核验缺失'
    validation['research_chain']={'passed':True}
    assert '过期' in check(s,q,validation,account,cfg,T+timedelta(minutes=5),active)['reason']


def test_deadline_does_not_extend_current_signal_ttl_and_rank_ignores_fake_quality():
    s=signal();s.time=stamp('2026-09-08T06:44:00Z');q=quote();sim=Simulation()
    validation={'ready':True,'eligible':True,'source':'longbridge'}
    r=check(s,q,validation,sim.state['accounts']['CN'],sim.state['params'],stamp('2026-09-08T06:45:00Z'),
            {('CN','first_pullback'):s.version})
    assert r['state']=='avoid' and '最晚入场' in r['reason']
    common={'relative_volume':2,'relative_strength':3,'cost_ratio':.1,'symbol':'600000.SH','strategy':'first_pullback'}
    assert rank_key({**common,'forward_quality':100})==rank_key({**common,'forward_quality':0})
