from functools import lru_cache
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import exchange_calendars as xc
import pandas as pd
from .models import UTC

@lru_cache
def calendar(market):
    # XSHG includes mainland holidays and lunch; same session dates for SZ.
    return xc.get_calendar('XNYS' if market == 'US' else 'XSHG', start='2018-01-01', end='2026-12-31')

def local_date(t, market):
    return t.astimezone(ZoneInfo('America/New_York' if market == 'US' else 'Asia/Shanghai')).date()

def is_open(t, market):
    try:
        cal = calendar(market)
        minute = pd.Timestamp(t).floor('min')
        if not cal.is_open_on_minute(minute, ignore_breaks=False):
            return False
        day = local_date(t, market)
        break_start=cal.session_break_start(str(day))
        break_end=cal.session_break_end(str(day))
        if not pd.isna(break_start) and break_start.to_pydatetime()<=t<break_end.to_pydatetime():return False
        return t < cal.session_close(str(day)).to_pydatetime()
    except (ValueError, KeyError):
        return False

def close_time(day, market):
    return calendar(market).session_close(str(day)).to_pydatetime()

def session_count(start, end, market):
    return len(calendar(market).sessions_in_range(str(start),str(end)))

def adjacent(previous, current, market):
    """No missing tradable 5m interval (lunch, nights and holidays are permitted)."""
    t = previous + timedelta(minutes=5)
    while t < current:
        if is_open(t,market):
            return False
        t += timedelta(minutes=5)
    return current > previous

def limit_ratio(symbol, risk_group, day):
    if symbol.endswith('.US'):
        return None
    if symbol.startswith(('300','301')):
        return .20 if str(day) >= '2020-08-24' else (.05 if risk_group == 'st' else .10)
    return .05 if risk_group == 'st' and str(day) < '2026-07-06' else .10

def price_limits(previous_close, symbol, risk_group, day):
    from decimal import Decimal, ROUND_HALF_UP
    r = limit_ratio(symbol,risk_group,day)
    if r is None or previous_close is None:
        return None,None
    p = Decimal(str(previous_close));r = Decimal(str(r))
    return float((p*(1+r)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),float((p*(1-r)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))

def open_time(day, market):
    return calendar(market).session_open(str(day)).to_pydatetime()

def phase(t,market):
    if is_open(t,market):return '交易中'
    day=local_date(t,market)
    try:
        if not calendar(market).is_session(str(day)):return '休市'
        if t<open_time(day,market):
            local=t.astimezone(ZoneInfo('Asia/Shanghai' if market=='CN' else 'America/New_York'))
            return '集合竞价' if market=='CN' and (local.hour,local.minute)>=(9,15) else '盘前准备'
        if t<close_time(day,market):return '午间休市'
    except (ValueError,KeyError):pass
    return '已收盘'

def monitoring_window(t,market):
    try:
        day=local_date(t,market)
        if not calendar(market).is_session(str(day)):return False
        return open_time(day,market)-timedelta(minutes=15)<=t<=close_time(day,market)+timedelta(minutes=10)
    except (ValueError,KeyError):return False
