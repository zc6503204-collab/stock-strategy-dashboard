from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

UTC = timezone.utc
VERSION = '实验 1.0.0'

def now() -> datetime:
    return datetime.now(UTC)

def stamp(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, (float, int)) or str(value).replace('.', '', 1).isdigit():
        n = float(value)
        return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, UTC)
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

def symbol_market(symbol: str) -> str:
    return 'US' if symbol.endswith('.US') else 'CN'

def in_scope(symbol: str) -> bool:
    code, market = symbol.rsplit('.', 1)
    if market == 'US':
        return bool(code) and len(code) <= 12
    return ((market == 'SH' and code.startswith(('600', '601', '603', '605')))
            or (market == 'SZ' and code.startswith(('000', '001', '002', '003', '300', '301')))) and len(code) == 6

@dataclass
class Bar:
    symbol: str
    source: str
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: Optional[float] = None
    final: bool = True
    halted: bool = False
    limit_up: Optional[float] = None
    limit_down: Optional[float] = None

    @property
    def end(self):
        from datetime import timedelta
        return self.start + timedelta(minutes=5)

    def valid(self):
        import math
        return (all(math.isfinite(v) for v in [self.open,self.high,self.low,self.close,self.volume])
                and 0 < self.low <= min(self.open,self.close) <= max(self.open,self.close) <= self.high and self.volume >= 0)

    def dump(self):
        return {**asdict(self), 'start': self.start.isoformat()}

    @classmethod
    def load(cls, data):
        return cls(**{**data, 'start': stamp(data['start'])})

@dataclass
class Quote:
    symbol: str
    source: str
    name: str
    price: float
    market_time: Optional[datetime]
    received_at: datetime
    change_pct: Optional[float] = None
    volume: Optional[float] = None
    turnover: Optional[float] = None
    market_cap: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    quality: str = 'unverified'
    session: str = 'unknown'
    depth_time: Optional[datetime] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    trade_status: str = 'Unknown'
    limit_up: Optional[float] = None
    limit_down: Optional[float] = None

    def dump(self):
        return {**asdict(self), 'depth_time': self.depth_time.isoformat() if self.depth_time else None, 'market_time': self.market_time.isoformat() if self.market_time else None,
                'received_at': self.received_at.isoformat()}

@dataclass
class Signal:
    id: str
    symbol: str
    strategy: str
    source: str
    time: datetime
    trigger: float
    stop: float
    risk_group: str
    evidence: dict
    version: str = VERSION

    def dump(self):
        fraction=float(self.evidence.get('no_chase_risk_fraction',.25))
        target=self.evidence.get('target_price',self.trigger+2*(self.trigger-self.stop))
        return {**asdict(self), 'time': self.time.isoformat(), 'no_chase': self.trigger + fraction * (self.trigger-self.stop),
                'target': target,'horizon':self.evidence.get('horizon','short'),
                'max_hold_sessions':self.evidence.get('max_hold_sessions',3),
                'exit_policy':self.evidence.get('exit_policy',{})}

    @classmethod
    def load(cls, d):
        return cls(**{k: stamp(v) if k == 'time' else v for k,v in d.items() if k in cls.__dataclass_fields__})
