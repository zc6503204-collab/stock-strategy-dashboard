from datetime import datetime,timedelta,timezone

from app.models import Quote, now
from app.service import Dashboard


def _selection(symbol, market, rank):
    return {
        'symbol': symbol, 'name': f'样本{rank}', 'market': market,
        'decision': '重点观察', 'score': 100-rank, 'distance_to_high_pct': -rank,
        'risk_group': 'normal', 'source': 'longbridge', 'as_of': '2026-09-04',
        'close': 100+rank, 'breakout_reference': 102+rank, 'structure_low': 98,
        'average_turnover': 200_000_000, 'reason': '等待盘中完整5分钟确认',
    }


def _buy(symbol, rank):
    return {
        'signal_id': f's{rank}', 'symbol': symbol, 'name': symbol,
        'strategy': 'breakout', 'strategies': ['breakout'], 'state': 'buy',
        'action': '可考虑买入', 'reason': '全部条件通过', 'entry_min': 100,
        'entry_max': 101, 'stop': 96, 'target': 108, 'qty': 10,
        'cash_required': 1010, 'planned_risk': 40, 'risk_group': 'normal',
    }


def test_workspaces_are_isolated_and_limit_decision_slots(tmp_path, monkeypatch):
    fixed = now()
    monkeypatch.setattr('app.service.now', lambda: fixed)
    dashboard = Dashboard(tmp_path)
    dashboard.selection = [
        _selection('600001.SH', 'CN', 1), _selection('000001.SZ', 'CN', 2),
        _selection('AAPL.US', 'US', 1),
    ]
    dashboard.candidates = [
        {'symbol': '600001.SH', 'name': '沪市样本', 'market': 'CN', 'source': 'longbridge'},
        {'symbol': 'AAPL.US', 'name': 'Apple', 'market': 'US', 'source': 'longbridge'},
    ]
    dashboard.decisions = [
        _buy('600001.SH', 1), _buy('000001.SZ', 2),
        _buy('300001.SZ', 3), _buy('600002.SH', 4), _buy('AAPL.US', 5),
    ]
    snapshot = dashboard.snapshot()
    assert snapshot['coverage']['full_market'] is False
    assert snapshot['coverage']['scope_label'] == '灵犀全市场策略初筛，本地仅复核返回候选'
    assert snapshot['coverage']['candidates_by_market'] == {'CN': 1, 'US': 1}
    assert snapshot['coverage']['strategy_candidates_by_market'] == {'CN': 0, 'US': 0}
    assert snapshot['coverage']['supplement_candidates_by_market'] == {'CN': 1, 'US': 1}
    assert snapshot['coverage']['research_pool_by_market'] == {'CN': 0, 'US': 0}
    assert snapshot['coverage']['selection_by_market'] == {'CN': 2, 'US': 1}
    cn, us = snapshot['workspaces']['CN'], snapshot['workspaces']['US']
    assert cn['meta']['currency'] == 'CNY' and cn['meta']['benchmark'] == '沪深300'
    assert us['meta']['currency'] == 'USD' and us['meta']['benchmark'] == 'SPY'
    assert cn['decision']['primary']['symbol'] == '600001.SH'
    assert len(cn['decision']['backups']) == 2
    assert us['decision']['primary']['symbol'] == 'AAPL.US'
    assert all(item['market'] == 'CN' for item in cn['candidates'])
    assert all(item['market'] == 'US' for item in us['candidates'])
    assert cn['breadth']['available'] is True
    assert us['breadth']['available'] is False


def test_workspace_holding_action_overrides_buy_and_fallback_is_renderable(tmp_path, monkeypatch):
    fixed = now()
    monkeypatch.setattr('app.service.now', lambda: fixed)
    dashboard = Dashboard(tmp_path)
    dashboard.real.upsert_manual({
        'symbol': 'AAPL.US', 'name': 'Apple', 'quantity': 2, 'cost': 100,
        'entry_date': '2026-09-01', 'stop': None, 'target': None,
    })
    dashboard.watch = ['AAPL.US']
    dashboard.quotes['AAPL.US'] = Quote(
        'AAPL.US', 'longbridge', 'Apple', 102, fixed-timedelta(seconds=5), fixed,
        quality='realtime', session='regular', trade_status='Normal',
    )
    dashboard.decisions = [_buy('MSFT.US', 1)]
    us = dashboard.snapshot()['workspaces']['US']
    assert len(us['holdings']['real']) == 1
    assert us['holdings']['real'][0]['action'] == '需要设置保护价'
    assert us['decision']['status'] == 'MANAGE'
    assert us['decision']['headline'] == '先处理持仓风险'

def test_premarket_prefers_strategy_matches_within_same_decision(tmp_path):
    dashboard=Dashboard(tmp_path)
    generic=_selection('600001.SH','CN',1);generic['score']=100
    matched=_selection('000001.SZ','CN',2);matched.update(score=70,candidate_strategies=['trend_pullback'])
    dashboard.selection=[generic,matched]
    assert dashboard.premarket('CN')[0]['symbol']=='000001.SZ'


def test_lunch_health_keeps_last_quote_without_reporting_feed_failure(tmp_path,monkeypatch):
    fixed=datetime(2026,9,8,4,0,tzinfo=timezone.utc)  # 12:00 Beijing time.
    monkeypatch.setattr('app.service.now',lambda:fixed)
    dashboard=Dashboard(tmp_path);symbol='600001.SH';dashboard.watch=[symbol]
    dashboard.validation[symbol]={'ready':True,'eligible':True,'source':'longbridge'}
    dashboard.lb.status.update(stream=True,state='connected')
    dashboard.lb.subscribed.add(symbol)
    dashboard.quotes[symbol]=Quote(symbol,'longbridge','样本',10,fixed-timedelta(minutes=30),fixed-timedelta(minutes=30),quality='realtime')
    dashboard.details[symbol]={'last_bar':(fixed-timedelta(minutes=35)).isoformat()}
    health=dashboard.snapshot()['workspaces']['CN']['health']
    assert health['state']=='closed' and '午间休市' in health['text']
    assert health['last_quote_at'] and health['last_bar_at']
    assert health['active_subscriptions']==1 and health['monitor_limit']==12
    assert health['quote_age_seconds']==1800
