from datetime import timedelta

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
