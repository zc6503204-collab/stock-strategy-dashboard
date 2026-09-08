from datetime import timedelta
from app.models import Bar,stamp
from app.selection import evaluate,candidate_pool

def daily(step=.1,amount=2e8):
    t=stamp('2026-06-01T01:30Z')
    return [Bar('600001.SH','longbridge',t+timedelta(days=i),10+i*step,11+i*step,9+i*step,10+i*step,10000000,amount) for i in range(65)]

def row():return {'symbol':'600001.SH','name':'测试股票','market_cap':2e10}

def test_ranking_is_not_an_entry_signal():
    r=evaluate(row(),daily())
    assert r['decision']=='重点观察' and r['trend'] and r['stacked']
    assert r['structure_low']<r['close'] and '5分钟' in r['entry_rule']
    assert r['source']=='longbridge' and r['as_of']

def test_low_liquidity_or_falling_trend_rejected():
    assert evaluate(row(),daily(amount=1e6))['decision']=='暂不参与'
    assert evaluate(row(),daily(step=-.02))['decision']=='暂不参与'

def test_price_extension_cannot_be_top_pick():
    bars=daily();b=bars[-1];b.close=b.open=b.high=22
    assert evaluate(row(),bars)['decision']=='等确认'

def test_st_label_and_exact_prior_high():
    bars=daily();r=evaluate(dict(row(),name='*ST测试'),bars)
    assert r['risk_group']=='st'
    assert r['breakout_reference']==max(b.high for b in bars[-21:-1])

def test_auction_reset_retains_researched_names_with_bounded_unique_pool():
    retained=[{'symbol':f'old{i}'} for i in range(40)]
    current=[{'symbol':f'new{i}'} for i in range(65)]+[{'symbol':'old0'}]
    pool=candidate_pool(current,retained,{'old30'})
    symbols=[r['symbol'] for r in pool]
    assert symbols[0]=='old30' and 'old0' in symbols and 'new0' in symbols
    assert len(symbols)==len(set(symbols))==80
