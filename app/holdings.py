"""Read-only broker holdings plus manually entered real positions.

The dashboard never submits orders.  Broker rows and user-entered lots share one
small, persistent schema so the monitoring engine can calculate exits without
guessing whether a paper trade represents a real position.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from math import isfinite
from uuid import uuid4
from zoneinfo import ZoneInfo

from .calendars import calendar, is_open, local_date, session_count
from .decisions import book_ok, fresh_quote
from .models import in_scope, now, stamp, symbol_market


def _positive(value, name, optional=False):
    if value in (None, '') and optional:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name}必须是有效数字')
    if not isfinite(value) or value <= 0:
        raise ValueError(f'{name}必须大于0')
    return value


def _entry_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValueError('买入日期格式不正确')


def _fee(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        raise ValueError('费用必须是有效数字')
    if not isfinite(value) or value < 0:
        raise ValueError('费用不能为负数或无效数字')
    return value


def _execution_time(value, name):
    if value in (None, ''):
        return None
    try:
        result = stamp(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f'{name}格式不正确')
    if result > now() + timedelta(minutes=1):
        raise ValueError(f'{name}不能在未来')
    return result


def sellable_quantity(position, t):
    """Local manual lots never use an account query to determine T+1."""
    quantity = max(0., float(position.get('quantity', 0)))
    if symbol_market(position['symbol']) == 'US':
        return quantity
    entry = position.get('entry_date')
    if position.get('source') == 'manual':
        return quantity if entry and entry < str(local_date(t, 'CN')) else 0.
    available = position.get('available')
    return min(quantity, max(0., float(available))) if available is not None else None


def _next_sell_date(position):
    entry = position.get('entry_date')
    if not entry:
        return None
    if symbol_market(position['symbol']) == 'US':
        return entry
    try:
        cal = calendar('CN')
        day = cal.date_to_session(entry, direction='previous')
        return str(cal.next_session(day).date())
    except (ValueError, KeyError):
        return None


class RealHoldings:
    def __init__(self, store):
        self.store = store
        self.rows = store.get('real_holdings', [])

    def save(self):
        self.store.set('real_holdings', self.rows)

    def list(self):
        rows = deepcopy(self.rows)
        for row in rows:
            row.setdefault('status', 'open' if row.get('quantity', 0) > 0 else 'closed')
            row.setdefault('sell_fills', [])
            row.setdefault('realized_pnl', 0.)
            if row.get('source') == 'manual':
                row['available'] = sellable_quantity(row, now())
                row['earliest_sell_date'] = _next_sell_date(row)
        return rows

    def symbols(self):
        return list(dict.fromkeys(row['symbol'] for row in self.rows if row.get('quantity', 0) > 0))

    def upsert_manual(self, data):
        symbol = str(data.get('symbol', '')).upper().strip()
        if '.' not in symbol or not in_scope(symbol):
            raise ValueError('请输入美股、沪深主板或创业板代码，例如 AAPL.US、300750.SZ')
        row_id = data.get('id') or 'manual:' + uuid4().hex
        existing = next((r for r in self.rows if r['id'] == row_id), None)
        if existing and existing.get('source') != 'manual':
            raise ValueError('券商同步持仓只能修改保护计划')
        cost = _positive(data.get('cost'), '买入均价')
        quantity = _positive(data.get('quantity'), '持仓数量')
        if symbol_market(symbol) == 'CN' and not quantity.is_integer():
            raise ValueError('A股持仓数量必须为整数股')
        entry_time = _execution_time(data.get('entry_time') or (existing or {}).get('entry_time'), '买入时间')
        entry_date = _entry_date(data.get('entry_date'))
        if entry_time:
            time_date = str(local_date(entry_time, symbol_market(symbol)))
            if entry_date and entry_date != time_date:
                raise ValueError('买入日期与买入时间不一致')
            entry_date = time_date
        if entry_date and entry_date > str(local_date(now(), symbol_market(symbol))):
            raise ValueError('买入日期不能在未来')
        if existing and existing.get('sell_fills'):
            if (symbol != existing['symbol'] or cost != existing['cost'] or quantity != existing['quantity']
                    or entry_date != existing.get('entry_date')):
                raise ValueError('已有卖出记录，不能改写原成交；减仓请登记实际卖出，加仓请新增一笔买入')
        stop = _positive(data.get('stop'), '保护价', True)
        target = _positive(data.get('target'), '止盈价', True)
        original = deepcopy((existing or {}).get('original_plan') or data.get('original_plan') or {})
        if not isinstance(original, dict):
            raise ValueError('原始买卖计划格式不正确')
        if not existing and not original and stop is not None and stop >= cost:
            raise ValueError('初始保护价需低于买入均价')
        if stop is not None and stop < cost and target is None:
            target = cost + 2 * (cost - stop)
        row = {
            **deepcopy(existing or {}),
            'id': row_id, 'symbol': symbol, 'name': data.get('name') or symbol,
            'market': symbol_market(symbol), 'source': 'manual',
            'quantity': quantity, 'available': None,
            'cost': cost, 'currency': 'USD' if symbol_market(symbol) == 'US' else 'CNY',
            'entry_date': entry_date, 'entry_time': entry_time.isoformat() if entry_time else None,
            'stop': stop, 'target': target,
            'note': str(data.get('note') or '')[:300], 'updated_at': now().isoformat(),
        }
        # Plan identity and initial risk are immutable even if execution differs.
        for key in ('plan_id', 'strategy', 'strategy_version'):
            row[key] = (existing or {}).get(key) or data.get(key)
        row['original_plan'] = original
        row.setdefault('sell_fills', [])
        row.setdefault('realized_pnl', 0.)
        row.setdefault('initial_quantity', quantity)
        row['status'] = 'open'
        if not existing or not existing.get('sell_fills'):
            row['initial_quantity'] = quantity
            row['entry_fee'] = _fee(data.get('entry_fee', row.get('entry_fee', 0)))
        row.setdefault('entry_fee_allocated', 0.)
        row.setdefault('plan_deviations', [])
        if original and not existing:
            lower = original.get('entry_min', original.get('trigger'))
            upper = original.get('entry_max', original.get('no_chase'))
            if lower is not None and cost < float(lower):
                row['plan_deviations'].append('实际买价低于原计划买入区间')
            if upper is not None and cost > float(upper):
                row['plan_deviations'].append('实际买价高于原计划禁止追价上限')
            original_stop = original.get('stop')
            if original_stop is not None and cost <= float(original_stop):
                row['plan_deviations'].append('实际买价已低于原计划保护价，需立即复核退出条件')
        if existing:
            self.rows[self.rows.index(existing)] = row
        else:
            self.rows.append(row)
        self.save()
        return deepcopy(row)

    def record_manual_sell(self, row_id, quantity, price, sold_at, fee=0, execution_id=None, note=''):
        """Record an already executed manual fill; this never places an order."""
        existing = next((r for r in self.rows if r['id'] == row_id), None)
        if not existing or existing.get('source') != 'manual':
            raise ValueError('只能登记本地手动持仓的实际卖出')
        quantity = _positive(quantity, '卖出数量')
        price = _positive(price, '实际卖出价')
        fee = _fee(fee)
        executed = _execution_time(sold_at, '卖出时间')
        if executed is None:
            raise ValueError('请填写实际卖出时间')
        previous = next((f for f in existing.get('sell_fills', []) if execution_id and f['id'] == execution_id), None)
        if previous:
            if any(previous[k] != v for k, v in {'quantity': quantity, 'price': price, 'fee': fee, 'time': executed.isoformat()}.items()):
                raise ValueError('相同成交编号已有不同记录')
            return deepcopy(existing)
        if quantity > existing['quantity']:
            raise ValueError('卖出数量超过剩余持仓')
        market = symbol_market(existing['symbol'])
        if market == 'CN' and not quantity.is_integer():
            raise ValueError('A股卖出数量必须为整数股')
        if existing.get('entry_time') and executed < stamp(existing['entry_time']):
            raise ValueError('卖出时间不能早于买入时间')
        if existing.get('entry_date') and str(local_date(executed, market)) < existing['entry_date']:
            raise ValueError('卖出日期不能早于买入日期')
        if market == 'CN':
            if not existing.get('entry_date'):
                raise ValueError('请先补充买入日期以核验A股T+1')
            if sellable_quantity(existing, executed) < quantity:
                raise ValueError('A股T+1：买入当天不可卖出')
        try:
            if not calendar(market).is_session(str(local_date(executed, market))):
                raise ValueError('卖出日期不是交易日')
        except (KeyError, OverflowError):
            raise ValueError('卖出日期超出交易日历范围')
        row = deepcopy(existing)
        remaining = row['quantity'] - quantity
        row.setdefault('initial_quantity', row['quantity'])
        row.setdefault('entry_fee', 0.)
        row.setdefault('entry_fee_allocated', 0.)
        allocated = (row['entry_fee'] - row['entry_fee_allocated']) if remaining == 0 else row['entry_fee'] * quantity / row['initial_quantity']
        gross = (price - row['cost']) * quantity
        net = gross - fee - allocated
        row.setdefault('sell_fills', []).append({
            'id': execution_id or 'manual-fill:' + uuid4().hex, 'time': executed.isoformat(),
            'quantity': quantity, 'price': price, 'fee': fee, 'entry_fee_allocated': allocated,
            'gross_pnl': gross, 'net_pnl': net, 'note': str(note or '')[:300],
            'recorded_at': now().isoformat(),
        })
        row['entry_fee_allocated'] += allocated
        row['realized_pnl'] = row.get('realized_pnl', 0.) + net
        row['quantity'] = remaining
        row['status'] = 'closed' if remaining == 0 else 'open'
        row['updated_at'] = now().isoformat()
        if remaining == 0:
            row['closed_at'] = max(fill['time'] for fill in row['sell_fills'])
        self.rows[self.rows.index(existing)] = row
        self.save()
        return deepcopy(row)

    def set_plan(self, row_id, stop=None, target=None, entry_date=None, note=''):
        row = next((r for r in self.rows if r['id'] == row_id), None)
        if not row:
            raise ValueError('真实持仓记录不存在')
        entry_date = _entry_date(entry_date)
        if row.get('source') == 'manual' and row.get('sell_fills') and entry_date != row.get('entry_date'):
            raise ValueError('已有卖出记录，不能改写买入日期')
        if row.get('entry_time') and entry_date != str(local_date(stamp(row['entry_time']), symbol_market(row['symbol']))):
            raise ValueError('买入日期与已登记的实际买入时间不一致')
        stop = _positive(stop, '保护价', True)
        target = _positive(target, '止盈价', True)
        if stop is not None and stop < row['cost'] and target is None:
            target = row['cost'] + 2 * (row['cost'] - stop)
        row.update(stop=stop, target=target, entry_date=entry_date,
                   note=str(note or '')[:300], updated_at=now().isoformat())
        self.save()
        return row

    def remove_manual(self, row_id):
        row = next((r for r in self.rows if r['id'] == row_id), None)
        if not row:
            raise ValueError('真实持仓记录不存在')
        if row.get('source') != 'manual':
            raise ValueError('券商持仓请在券商端处理，再重新同步')
        if row.get('sell_fills'):
            raise ValueError('已有实际卖出记录，需保留成交账本；已清仓记录不占监测名额')
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
    original = position.get('original_plan') or {}
    maximum = original.get('max_hold_sessions', position.get('max_hold_sessions'))
    due = False
    if maximum and position.get('entry_date'):
        try:
            sessions = session_count(position['entry_date'], local_date(t, market), market)
            local = t.astimezone(ZoneInfo('Asia/Shanghai' if market == 'CN' else 'America/New_York'))
            deadline = (14, 50) if market == 'CN' else (15, 50)
            due = sessions > int(maximum) or (sessions == int(maximum) and (local.hour, local.minute) >= deadline)
        except (ValueError, KeyError):
            pass
    sold_at_target = sum(f['quantity'] for f in position.get('sell_fills', []) if target is not None and f['price'] >= target)
    partial_taken = sold_at_target >= float(position.get('initial_quantity', position['quantity'])) / 2

    if stop is None:
        action, reason = '需要设置保护价', '尚未登记止损保护价，无法形成完整盯盘计划'
    elif not open_now:
        action, reason = '休市观察', '开盘后使用有效实时行情重新核验止损止盈'
    elif not fresh:
        action, reason = '行情待恢复', '报价超过30秒或实时权限未确认，暂停触价判断'
    elif mark <= stop:
        action, reason, event = '止损退出', '有效行情已触及保护价', 'stop'
    elif due:
        action, reason, event = '到期退出', f'已到第{maximum}个交易日计划退出时间', 'time_exit'
    elif target is not None and mark >= target and not partial_taken:
        action, reason, event = '减仓止盈', '有效行情已达到登记的止盈参考价', 'take_profit'

    blocked = False
    available = sellable_quantity(position, t)
    if event and market == 'CN':
        today = str(local_date(t, market))
        same_day = position.get('entry_date') == today
        unavailable = available is not None and available <= 0
        if same_day or unavailable:
            action, blocked = '暂时无法卖出', True
            reason = ('缺少买入日期，无法核验A股T+1可卖数量' if not position.get('entry_date') and position.get('source') == 'manual'
                      else 'A股当日可卖数量不足或为当天新买；退出条件已记录，下一可卖时段再处理')
    if event and not blocked and not book_ok(quote, t, 'sell'):
        action, blocked = '退出条件已触发', True
        reason = '价格条件已触发，但买盘深度不足15秒内有效，不能假设已成交'
    if event and not blocked and (quote.trade_status != 'Normal' or (quote.limit_down and quote.bid <= quote.limit_down)):
        action, blocked = '暂时无法卖出', True
        reason = '退出条件已触发，但停牌、跌停或交易状态未确认，不能假设已成交'
    pnl = (mark - position['cost']) * position['quantity']
    unallocated = position.get('entry_fee', 0) - position.get('entry_fee_allocated', 0)
    exit_qty = position['quantity'] if event else 0
    if event == 'take_profit':
        lot = 100 if market == 'CN' else 1
        exit_qty = (exit_qty // (2 * lot)) * lot or exit_qty
    return {
        **position, 'price': mark, 'market_value': mark * position['quantity'],
        'unrealized_pnl': pnl, 'unrealized_pct': (mark / position['cost'] - 1) * 100,
        'unrealized_after_entry_fee': pnl - unallocated,
        'available': available, 'earliest_sell_date': _next_sell_date(position),
        'qty': exit_qty, 'max_hold_sessions': maximum, 'partial_taken': partial_taken,
        'action': action, 'reason': reason, 'event': event, 'blocked': blocked,
        'quote_time': quote.market_time.isoformat() if quote and quote.market_time else None,
        'quote_fresh': fresh, 'data_ready': bool(validation.get('ready')),
        'risk_group': validation.get('risk_group', position.get('risk_group', 'pending')),
    }
