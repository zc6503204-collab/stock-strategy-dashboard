"""Read-only broker holdings plus manually entered real positions.

The dashboard never submits orders.  Broker rows and user-entered lots share one
small, persistent schema so the monitoring engine can calculate exits without
guessing whether a paper trade represents a real position.
"""
from __future__ import annotations

from datetime import date
from uuid import uuid4

from .calendars import is_open, local_date
from .decisions import book_ok, fresh_quote
from .models import in_scope, now, symbol_market


def _positive(value, name, optional=False):
    if value in (None, '') and optional:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name}必须是有效数字')
    if value <= 0:
        raise ValueError(f'{name}必须大于0')
    return value


def _entry_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValueError('买入日期格式不正确')


class RealHoldings:
    def __init__(self, store):
        self.store = store
        self.rows = store.get('real_holdings', [])

    def save(self):
        self.store.set('real_holdings', self.rows)

    def list(self):
        return [dict(row) for row in self.rows]

    def symbols(self):
        return list(dict.fromkeys(row['symbol'] for row in self.rows if row.get('quantity', 0) > 0))

    def upsert_manual(self, data):
        symbol = str(data.get('symbol', '')).upper().strip()
        if not in_scope(symbol):
            raise ValueError('请输入美股、沪深主板或创业板代码，例如 AAPL.US、300750.SZ')
        row_id = data.get('id') or 'manual:' + uuid4().hex
        existing = next((r for r in self.rows if r['id'] == row_id), None)
        if existing and existing.get('source') != 'manual':
            raise ValueError('券商同步持仓只能修改保护计划')
        cost = _positive(data.get('cost'), '买入均价')
        stop = _positive(data.get('stop'), '保护价', True)
        target = _positive(data.get('target'), '止盈价', True)
        if not existing and stop is not None and stop >= cost:
            raise ValueError('初始保护价需低于买入均价')
        if stop is not None and stop < cost and target is None:
            target = cost + 2 * (cost - stop)
        row = {
            'id': row_id, 'symbol': symbol, 'name': data.get('name') or symbol,
            'market': symbol_market(symbol), 'source': 'manual',
            'quantity': _positive(data.get('quantity'), '持仓数量'), 'available': None,
            'cost': cost, 'currency': 'USD' if symbol_market(symbol) == 'US' else 'CNY',
            'entry_date': _entry_date(data.get('entry_date')), 'stop': stop, 'target': target,
            'note': str(data.get('note') or '')[:300], 'updated_at': now().isoformat(),
        }
        if existing:
            self.rows[self.rows.index(existing)] = row
        else:
            self.rows.append(row)
        self.save()
        return row

    def set_plan(self, row_id, stop=None, target=None, entry_date=None, note=''):
        row = next((r for r in self.rows if r['id'] == row_id), None)
        if not row:
            raise ValueError('真实持仓记录不存在')
        stop = _positive(stop, '保护价', True)
        target = _positive(target, '止盈价', True)
        if stop is not None and stop < row['cost'] and target is None:
            target = row['cost'] + 2 * (row['cost'] - stop)
        row.update(stop=stop, target=target, entry_date=_entry_date(entry_date),
                   note=str(note or '')[:300], updated_at=now().isoformat())
        self.save()
        return row

    def remove_manual(self, row_id):
        row = next((r for r in self.rows if r['id'] == row_id), None)
        if not row:
            raise ValueError('真实持仓记录不存在')
        if row.get('source') != 'manual':
            raise ValueError('券商持仓请在券商端处理，再重新同步')
        self.rows.remove(row)
        self.save()

    def replace_synced(self, source, rows):
        preserved = {r['id']: r for r in self.rows if r.get('source') == source}
        others = [r for r in self.rows if r.get('source') != source]
        synced = []
        for raw in rows:
            symbol = str(raw.get('symbol', '')).upper().strip()
            quantity = _positive(raw.get('quantity'), '持仓数量')
            cost = _positive(raw.get('cost'), '持仓成本')
            if not in_scope(symbol):
                continue
            row_id = f'{source}:{symbol}'
            old = preserved.get(row_id, {})
            synced.append({
                'id': row_id, 'symbol': symbol, 'name': raw.get('name') or old.get('name') or symbol,
                'market': symbol_market(symbol), 'source': source, 'quantity': quantity,
                'available': raw.get('available'), 'cost': cost,
                'currency': raw.get('currency') or ('USD' if symbol_market(symbol) == 'US' else 'CNY'),
                'entry_date': old.get('entry_date'), 'stop': old.get('stop'), 'target': old.get('target'),
                'note': old.get('note', ''), 'updated_at': now().isoformat(),
            })
        self.rows = others + synced
        self.save()
        return synced


def real_holding_plan(position, quote, t, validation):
    market = symbol_market(position['symbol'])
    mark = quote.price if quote and quote.price else position['cost']
    fresh = fresh_quote(quote, t)
    open_now = is_open(t, market)
    stop, target = position.get('stop'), position.get('target')
    action, reason, event = '继续持有', '未触及已设置的退出条件', None

    if stop is None:
        action, reason = '需要设置保护价', '尚未登记止损保护价，无法形成完整盯盘计划'
    elif not open_now:
        action, reason = '休市观察', '开盘后使用有效实时行情重新核验止损止盈'
    elif not fresh:
        action, reason = '行情待恢复', '报价超过30秒或实时权限未确认，暂停触价判断'
    elif mark <= stop:
        action, reason, event = '止损退出', '有效行情已触及保护价', 'stop'
    elif target is not None and mark >= target:
        action, reason, event = '减仓止盈', '有效行情已达到登记的止盈参考价', 'take_profit'

    blocked = False
    if event and market == 'CN':
        today = str(local_date(t, market))
        same_day = position.get('entry_date') == today
        unavailable = position.get('available') is not None and float(position['available']) <= 0
        if same_day or unavailable:
            action, blocked = '暂时无法卖出', True
            reason = 'A股当日可卖数量不足或为当天新买；退出条件已记录，下一可卖时段再处理'
    if event and not book_ok(quote, t, 'sell'):
        action, blocked = '退出条件已触发', True
        reason = '价格条件已触发，但买盘深度不足15秒内有效，不能假设已成交'
    pnl = (mark - position['cost']) * position['quantity']
    return {
        **position, 'price': mark, 'market_value': mark * position['quantity'],
        'unrealized_pnl': pnl, 'unrealized_pct': (mark / position['cost'] - 1) * 100,
        'action': action, 'reason': reason, 'event': event, 'blocked': blocked,
        'quote_time': quote.market_time.isoformat() if quote and quote.market_time else None,
        'quote_fresh': fresh, 'data_ready': bool(validation.get('ready')),
        'risk_group': validation.get('risk_group', position.get('risk_group', 'pending')),
    }
