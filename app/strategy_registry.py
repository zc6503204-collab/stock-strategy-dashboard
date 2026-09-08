"""Persistent, versioned strategy definitions for both market workspaces."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

from .models import now


DEFINITIONS = {
    'breakout': {
        'name': '开盘区间突破',
        'summary': '开盘区间收阳后，等待完整五分钟K线放量突破。',
        'category': '趋势突破',
        'markets': ['CN', 'US'], 'horizon': 'short', 'max_hold_sessions': 3,
        'exit_policy': {'type': 'risk_partial', 'target_r': 2, 'trail': 'intraday_3bar'},
        'data_requirements': {'daily_bars': 25, 'intraday_baseline_sessions': 14},
        'research_reference': '本地实验规则；开盘区间与同时间成交量框架',
        'parameters': {
            'opening_bars': ('开盘区间K线数', 'int', 1, 6, {'CN': 3, 'US': 1}),
            'rvol_min': ('同时间成交量倍数', 'float', .5, 5., 1.5),
            'stop_lookback': ('止损回看K线数', 'int', 1, 12, 4),
            'signal_minutes': ('信号有效分钟', 'int', 1, 30, 5),
            'liquidity_min': ('20日平均成交额', 'float', 100000., 1e10, {'CN': 1e8, 'US': 5e6}),
            'relative_strength_min': ('20日相对强弱下限', 'float', -20., 50., 0.),
        },
    },
    'pullback': {
        'name': '首次回调再启动',
        'summary': '有效突破后首次回踩突破位，守住后再次转强。',
        'category': '趋势回调',
        'markets': ['CN', 'US'], 'horizon': 'short', 'max_hold_sessions': 3,
        'exit_policy': {'type': 'risk_partial', 'target_r': 2, 'trail': 'intraday_3bar'},
        'data_requirements': {'daily_bars': 25, 'intraday_baseline_sessions': 14},
        'research_reference': '本地实验规则；突破后的首次回踩确认',
        'parameters': {
            'opening_bars': ('开盘区间K线数', 'int', 1, 6, {'CN': 3, 'US': 1}),
            'rvol_min': ('同时间成交量倍数', 'float', .5, 5., 1.5),
            'pullback_bars': ('突破后观察K线数', 'int', 1, 24, 6),
            'touch_tolerance_pct': ('回踩容差百分比', 'float', 0., 3., .3),
            'signal_minutes': ('信号有效分钟', 'int', 1, 30, 5),
            'liquidity_min': ('20日平均成交额', 'float', 100000., 1e10, {'CN': 1e8, 'US': 5e6}),
            'relative_strength_min': ('20日相对强弱下限', 'float', -20., 50., 0.),
        },
    },
    'trend_pullback': {
        'name': '趋势回踩再启动',
        'summary': '日线多头趋势中，回踩VWAP或五分钟EMA后重新转强。',
        'category': '趋势回调',
        'markets': ['CN', 'US'], 'horizon': 'short', 'max_hold_sessions': 3,
        'exit_policy': {'type': 'risk_partial', 'target_r': 2, 'trail': 'intraday_3bar'},
        'data_requirements': {'daily_bars': 65, 'intraday_baseline_sessions': 0},
        'research_reference': '本地实验规则；日线趋势与盘中VWAP/EMA回踩',
        'parameters': {
            'daily_fast': ('日线快速均线', 'int', 5, 60, 20),
            'daily_slow': ('日线慢速均线', 'int', 20, 120, 60),
            'intraday_ema': ('五分钟EMA周期', 'int', 5, 60, 20),
            'touch_tolerance_pct': ('回踩容差百分比', 'float', 0., 3., .3),
            'confirm_volume_ratio': ('确认K线量比', 'float', .5, 5., 1.2),
            'stop_lookback': ('止损回看K线数', 'int', 1, 12, 3),
            'signal_minutes': ('信号有效分钟', 'int', 1, 30, 5),
            'liquidity_min': ('20日平均成交额', 'float', 100000., 1e10, {'CN': 1e8, 'US': 5e6}),
            'relative_strength_min': ('20日相对强弱下限', 'float', -20., 50., 0.),
        },
    },
    'volatility_breakout': {
        'name': '波动收缩突破',
        'summary': '日线波动收缩并靠近高点后，等待盘中放量突破整理区。',
        'category': '收缩突破',
        'markets': ['CN', 'US'], 'horizon': 'short', 'max_hold_sessions': 3,
        'exit_policy': {'type': 'risk_partial', 'target_r': 2, 'trail': 'intraday_3bar'},
        'data_requirements': {'daily_bars': 65, 'intraday_baseline_sessions': 14},
        'research_reference': '本地实验规则；ATR收缩与整理区突破',
        'parameters': {
            'daily_ma': ('日线趋势均线', 'int', 5, 60, 20),
            'atr_short': ('短ATR周期', 'int', 2, 20, 5),
            'atr_long': ('长ATR周期', 'int', 10, 60, 20),
            'contraction_max': ('ATR收缩比例上限', 'float', .2, 1.5, .75),
            'near_high_pct': ('距20日高点上限百分比', 'float', 0., 20., 5.),
            'breakout_days': ('整理区突破天数', 'int', 3, 30, 10),
            'rvol_min': ('同时间成交量倍数', 'float', .5, 5., 1.5),
            'stop_lookback': ('止损回看K线数', 'int', 1, 12, 3),
            'signal_minutes': ('信号有效分钟', 'int', 1, 30, 5),
            'liquidity_min': ('20日平均成交额', 'float', 100000., 1e10, {'CN': 1e8, 'US': 5e6}),
            'relative_strength_min': ('20日相对强弱下限', 'float', -20., 50., 0.),
        },
    },
    'trend_rsi_pullback': {
        'name': '趋势RSI回踩',
        'summary': '日线强势股缩量回到MA20附近，等待盘中重新转强。',
        'category': '趋势回调',
        'markets': ['CN', 'US'], 'horizon': 'short', 'max_hold_sessions': 3,
        'exit_policy': {'type': 'risk_partial', 'target_r': 2, 'trail': 'intraday_3bar'},
        'data_requirements': {'daily_bars': 65, 'intraday_baseline_sessions': 0},
        'research_reference': 'tick-stock-panel RSI中轴与MA20回踩思路的本地重构',
        'parameters': {
            'daily_fast': ('日线快速均线', 'int', 10, 40, 20),
            'daily_slow': ('日线慢速均线', 'int', 40, 120, 60),
            'rsi_period': ('RSI周期', 'int', 5, 30, 14),
            'rsi_peak_min': ('近期RSI强势阈值', 'float', 55., 85., 65.),
            'rsi_current_min': ('当前RSI下限', 'float', 30., 65., 45.),
            'rsi_current_max': ('当前RSI上限', 'float', 40., 75., 60.),
            'ma_distance_pct': ('距MA20上限百分比', 'float', .2, 8., 2.),
            'volume_contraction_max': ('5日/20日均量上限', 'float', .2, 1.5, .8),
            'intraday_ema': ('五分钟EMA周期', 'int', 5, 60, 20),
            'touch_tolerance_pct': ('回踩容差百分比', 'float', 0., 3., .3),
            'confirm_volume_ratio': ('确认K线量比', 'float', .5, 5., 1.2),
            'stop_lookback': ('止损回看K线数', 'int', 1, 12, 3),
            'signal_minutes': ('信号有效分钟', 'int', 1, 30, 5),
            'liquidity_min': ('20日平均成交额', 'float', 100000., 1e10, {'CN': 1e8, 'US': 5e6}),
            'relative_strength_min': ('20日相对强弱下限', 'float', -20., 50., 0.),
        },
    },
    'vcp_swing': {
        'name': 'VCP波动收缩突破',
        'summary': '均线多头、相对强势且波动收缩的股票，等待突破最终整理平台。',
        'category': '波段突破',
        'markets': ['CN', 'US'], 'horizon': 'swing', 'max_hold_sessions': 10,
        'exit_policy': {'type': 'risk_partial', 'target_r': 2, 'trail': 'daily_3low'},
        'data_requirements': {'daily_bars': 260, 'intraday_baseline_sessions': 14},
        'research_reference': 'Qullamaggie breakout scanner质量因子与VCP框架的本地重构',
        'parameters': {
            'ema_fast': ('快速EMA', 'int', 5, 30, 10),
            'ema_mid': ('中速EMA', 'int', 10, 50, 20),
            'ema_slow': ('慢速EMA', 'int', 30, 120, 50),
            'slow_slope_days': ('慢速EMA上行观察日数', 'int', 5, 40, 20),
            'rs_top_pct': ('相对强弱前百分比', 'float', 5., 60., 30.),
            'atr_short': ('短ATR周期', 'int', 2, 20, 5),
            'atr_long': ('长ATR周期', 'int', 10, 60, 20),
            'contraction_max': ('ATR收缩比例上限', 'float', .2, 1.5, .75),
            'range_contraction_max': ('区间收缩比例上限', 'float', .2, 1.5, .75),
            'near_high_pct': ('距60日高点上限百分比', 'float', 0., 20., 5.),
            'breakout_days': ('整理区突破天数', 'int', 5, 30, 10),
            'final_contraction_days': ('最终收缩观察天数', 'int', 3, 10, 5),
            'rvol_min': ('同时间成交量倍数', 'float', .5, 5., 1.5),
            'max_stop_atr': ('最大结构止损ATR倍数', 'float', 1., 6., 3.),
            'signal_minutes': ('信号有效分钟', 'int', 1, 30, 5),
            'liquidity_min': ('20日平均成交额', 'float', 100000., 1e10, {'CN': 1e8, 'US': 5e6}),
        },
    },
    'orb20_us': {
        'name': '美股20分钟开盘突破',
        'summary': '美股开盘20分钟形成区间，只在10:00至11:30确认突破并当日退出。',
        'category': '日内突破',
        'markets': ['US'], 'horizon': 'intraday', 'max_hold_sessions': 1,
        'exit_policy': {'type': 'orb_fixed', 'stop_range': .5, 'target_range': .75, 'flat_minutes_before_close': 10},
        'data_requirements': {'daily_bars': 25, 'intraday_baseline_sessions': 14},
        'research_reference': 'sam-bateman/trading-orb公开参数的本地独立实现',
        'parameters': {
            'opening_minutes': ('开盘区间分钟', 'int', 5, 60, 20),
            'entry_start_minutes': ('开盘后最早入场分钟', 'int', 20, 180, 30),
            'entry_end_minutes': ('开盘后停止入场分钟', 'int', 30, 300, 120),
            'rvol_min': ('同时间成交量倍数', 'float', .5, 5., 1.2),
            'stop_range': ('止损区间倍数', 'float', .1, 2., .5),
            'target_range': ('止盈区间倍数', 'float', .2, 4., .75),
            'exit_minutes_before_close': ('收盘前退出分钟', 'int', 5, 60, 10),
            'signal_minutes': ('信号有效分钟', 'int', 1, 30, 5),
            'liquidity_min': ('20日平均成交额', 'float', 100000., 1e10, 5e6),
            'relative_strength_min': ('20日相对强弱下限', 'float', -20., 50., 0.),
        },
    },
}


def _default_value(spec, market):
    value = spec[4]
    return value[market] if isinstance(value, dict) else value


class StrategyRegistry:
    def __init__(self, store=None):
        self.store = store
        self.revisions = deepcopy(store.get('strategy_revisions', {})) if store else {}
        self.active = deepcopy(store.get('strategy_active', {})) if store else {}
        self._seed()

    def _seed(self):
        changed = False
        for market in ['CN', 'US']:
            self.revisions.setdefault(market, {})
            self.active.setdefault(market, {})
            for strategy, definition in DEFINITIONS.items():
                if market not in definition['markets']:
                    continue
                self.revisions[market].setdefault(strategy, [])
                if not self.revisions[market][strategy]:
                    version = '实验 2.0.0' if strategy in ('breakout', 'pullback') else f'{strategy} 1.0.0'
                    row = {
                        'strategy': strategy, 'market': market, 'version': version,
                        'enabled': True, 'parameters': self.defaults(strategy, market),
                        'created_at': now().isoformat(), 'parent_version': None,
                        'changes': {}, 'reason': '系统默认参数',
                        'validation_status': '前向样本积累中', 'replay': None,
                    }
                    self.revisions[market][strategy].append(row)
                    self.active[market][strategy] = version
                    changed = True
                elif strategy not in self.active[market]:
                    self.active[market][strategy] = self.revisions[market][strategy][-1]['version'];changed = True
        if changed:self._save()

    def _save(self):
        if self.store:
            self.store.set('strategy_revisions', self.revisions)
            self.store.set('strategy_active', self.active)

    def defaults(self, strategy, market):
        if strategy not in DEFINITIONS or market not in DEFINITIONS[strategy]['markets']:
            raise ValueError('该策略不适用于当前市场')
        return {key: _default_value(spec, market) for key, spec in DEFINITIONS[strategy]['parameters'].items()}

    def current(self, strategy, market):
        if strategy not in DEFINITIONS or market not in DEFINITIONS[strategy]['markets']:
            raise ValueError('该策略不适用于当前市场')
        version = self.active[market][strategy]
        return next(deepcopy(r) for r in self.revisions[market][strategy] if r['version'] == version)

    def config(self, strategy, market):
        return self.current(strategy, market)['parameters']

    def enabled(self, strategy, market):
        return bool(self.current(strategy, market)['enabled'])

    def active_versions(self, market=None):
        markets = [market] if market else ['CN', 'US']
        return {(m, s): self.active[m][s] for m in markets for s,d in DEFINITIONS.items() if m in d['markets']}

    def _coerce(self, strategy, market, raw):
        definition = DEFINITIONS[strategy]
        current = self.config(strategy, market)
        base = {key: current.get(key, _default_value(spec, market)) for key, spec in definition['parameters'].items()}
        unknown = set(raw)-set(definition['parameters'])
        if unknown:raise ValueError('包含未知参数：' + '、'.join(sorted(unknown)))
        values = dict(base)
        for key, value in raw.items():
            label, kind, low, high, _ = definition['parameters'][key]
            try:value = int(value) if kind == 'int' else float(value)
            except (TypeError, ValueError):raise ValueError(f'{label}必须是数字')
            if not low <= value <= high:raise ValueError(f'{label}需在{low:g}至{high:g}之间')
            values[key] = value
        if strategy in ('trend_pullback','trend_rsi_pullback') and values['daily_slow'] <= values['daily_fast']:
            raise ValueError('日线慢速均线必须大于快速均线')
        if strategy in ('volatility_breakout','vcp_swing') and values['atr_long'] <= values['atr_short']:
            raise ValueError('长ATR周期必须大于短ATR周期')
        if strategy=='trend_rsi_pullback' and not values['rsi_current_min']<values['rsi_current_max']<values['rsi_peak_min']:
            raise ValueError('RSI参数需满足当前下限＜当前上限＜近期强势阈值')
        if strategy=='vcp_swing' and not values['ema_fast']<values['ema_mid']<values['ema_slow']:
            raise ValueError('EMA周期需满足快速＜中速＜慢速')
        if strategy=='vcp_swing' and values['final_contraction_days']>=values['breakout_days']:
            raise ValueError('最终收缩观察日数必须小于整理区突破天数')
        if strategy=='orb20_us' and values['entry_end_minutes']<=values['entry_start_minutes']:
            raise ValueError('停止入场时间必须晚于最早入场时间')
        return values

    def save(self, strategy, market, parameters=None, enabled=None, reason='用户修改参数'):
        if strategy not in DEFINITIONS:raise ValueError('未知策略')
        if market not in ['CN', 'US'] or market not in DEFINITIONS[strategy]['markets']:raise ValueError('该策略不适用于当前市场')
        old = self.current(strategy, market);values = self._coerce(strategy, market, parameters or {})
        enabled = old['enabled'] if enabled is None else bool(enabled)
        if values == old['parameters'] and enabled == old['enabled']:return old
        serial = f"{market}|{strategy}|{now().isoformat()}|{values}|{enabled}"
        version = f"{strategy}-{market}-{len(self.revisions[market][strategy])+1}-{sha256(serial.encode()).hexdigest()[:6]}"
        changes = {k: {'from': old['parameters'].get(k), 'to': v} for k, v in values.items() if old['parameters'].get(k) != v}
        if enabled != old['enabled']:changes['enabled'] = {'from': old['enabled'], 'to': enabled}
        row = {'strategy': strategy, 'market': market, 'version': version, 'enabled': enabled,
               'parameters': values, 'created_at': now().isoformat(), 'parent_version': old['version'],
               'changes': changes, 'reason': str(reason or '用户修改参数')[:200],
               'validation_status': '前向样本重新积累', 'replay': None}
        self.revisions[market][strategy].append(row);self.active[market][strategy] = version;self._save();return deepcopy(row)

    def rollback(self, strategy, market, version):
        target = next((r for r in self.revisions.get(market, {}).get(strategy, []) if r['version'] == version), None)
        if not target:raise ValueError('找不到要恢复的参数版本')
        parameters=self.defaults(strategy,market)
        parameters.update({k:v for k,v in target['parameters'].items() if k in DEFINITIONS[strategy]['parameters']})
        return self.save(strategy, market, parameters, target['enabled'], f'恢复自 {version}')

    def set_replay(self, strategy, market, version, replay):
        for row in self.revisions.get(market, {}).get(strategy, []):
            if row['version'] == version:row['replay'] = replay;break
        self._save()

    def list(self, market, performance=None):
        if market not in ['CN', 'US']:raise ValueError('未知市场')
        performance = performance or {}
        result=[]
        for strategy, definition in DEFINITIONS.items():
            if market not in definition['markets']:continue
            row=self.current(strategy,market);metrics=performance.get(strategy,{})
            fields=[]
            for key,spec in definition['parameters'].items():
                label,kind,low,high,_=spec
                fields.append({'key':key,'label':label,'type':kind,'min':low,'max':high,'value':row['parameters'][key]})
            exit_policy=deepcopy(definition['exit_policy'])
            if strategy=='orb20_us':exit_policy.update(stop_range=row['parameters']['stop_range'],target_range=row['parameters']['target_range'],flat_minutes_before_close=row['parameters']['exit_minutes_before_close'])
            result.append({**row,'name':definition['name'],'summary':definition['summary'],'category':definition['category'],
                           'markets':definition['markets'],'horizon':definition['horizon'],
                           'max_hold_sessions':definition['max_hold_sessions'],'exit_policy':exit_policy,
                           'data_requirements':deepcopy(definition['data_requirements']),
                           'research_reference':definition['research_reference'],'fields':fields,'performance':metrics,
                           'forward_count':metrics.get('count',0),'enough':metrics.get('count',0)>=30,
                           'stability':'样本相对稳定' if metrics.get('count',0)>=100 else '可初步比较' if metrics.get('count',0)>=30 else '样本不足'})
        return result

    def versions(self, strategy, market):
        if strategy not in DEFINITIONS or market not in DEFINITIONS[strategy]['markets']:raise ValueError('未知策略或市场')
        return list(reversed(deepcopy(self.revisions[market][strategy])))
