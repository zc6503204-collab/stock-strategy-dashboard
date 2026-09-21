"""Deterministic, tick-aligned plan prices; no market or account side effects."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import isfinite

from .models import symbol_market


def tick_price(value, tick=.01, *, up=False):
    unit=Decimal(str(tick))
    if not isfinite(float(value)) or not isfinite(float(tick)) or unit<=0:
        raise ValueError('价格或最小报价单位无效')
    mode=ROUND_CEILING if up else ROUND_FLOOR
    return float((Decimal(str(value))/unit).to_integral_value(rounding=mode)*unit)


def _costs(signal,cfg,entry,stop,target,qty,tick):
    slip=float(cfg.get('high_risk_slippage',.003) if signal.risk_group!='normal' else cfg.get('slippage',.001))
    fee=float(cfg.get('fee_rate',.0005));minimum=float(cfg.get('minimum_fee',0.))
    # fee_rate is the existing platform's aggregate fee assumption. Do not add
    # an invented tax on top; explicit additional rates are opt-in assumptions.
    sell_extra=float(cfg.get('sell_tax_rate',0.))+float(cfg.get('transfer_fee_rate',0.))
    buy_extra=float(cfg.get('transfer_fee_rate',0.))
    if any(not isfinite(v) or v<0 for v in (slip,fee,minimum,sell_extra,buy_extra)) or slip>=1:
        raise ValueError('交易成本参数无效')
    target_fill=tick_price(target*(1-slip),tick)
    stop_fill=tick_price(stop*(1-slip),tick)
    buy_fee=max(entry*qty*fee,minimum)/qty+entry*buy_extra
    target_fee=max(target_fill*qty*fee,minimum)/qty+target_fill*sell_extra
    stop_fee=max(stop_fill*qty*fee,minimum)/qty+stop_fill*sell_extra
    reward=target_fill-entry-buy_fee-target_fee
    risk=entry-stop_fill+buy_fee+stop_fee
    return {'net_reward':reward,'net_risk':risk,'net_rr':reward/risk if risk>0 else None,
            'entry_fee_per_share':buy_fee,'target_fee_per_share':target_fee,'stop_fee_per_share':stop_fee,
            'target_fill':target_fill,'stop_fill':stop_fill,'slippage':slip,'fee_rate':fee,
            'quantity_assumption':qty,'basis':'平台费用与滑点假设；实际费率以用户核验为准'}


def execution_range(signal,cfg,quote=None,qty=None):
    """Return final fill interval, not a misleading raw trigger/no-chase spread.

    A missing quote computes a feasible structural interval only. Freshness,
    execution status, book size and the 5-minute signal TTL remain caller gates.
    Quote prices are never silently rounded down to make an entry fit.
    """
    market=symbol_market(signal.symbol);e=signal.evidence
    tick=float(e.get('price_tick',.01 if market=='CN' or signal.trigger>=1 else .0001))
    result={'entry_min':None,'entry_max':None,'stop':None,'target':None,'net_rr':None,
            'executable':False,'reason':'无法形成可成交价格区间','price_tick':tick,
            'quote_max':None,'estimated_fill':None,'costs':None}
    try:
        stop=tick_price(float(e.get('structural_stop',signal.stop)),tick)
        trigger=tick_price(signal.trigger,tick,up=True)
        target=tick_price(float(e.get('target_price',signal.trigger+2*(signal.trigger-stop))),tick)
        fraction=float(e.get('no_chase_risk_fraction',.25));min_rr=float(e.get('min_net_rr',1.5))
        slip=float(cfg.get('high_risk_slippage',.003) if signal.risk_group!='normal' else cfg.get('slippage',.001))
        qty=max(1,float(qty or (100 if market=='CN' else 1)))
        result.update(stop=stop,target=target,trigger=trigger,min_net_rr=min_rr)
        if not all(isfinite(x) for x in (fraction,min_rr,slip,qty)) or fraction<0 or min_rr<=0 or not 0<=slip<1:
            raise ValueError('计划参数无效')
        if not 0<stop<trigger<target:
            result['reason']='结构止损、触发价或目标价无效';return result
        entry_min=tick_price(trigger*(1+slip),tick,up=True)
        raw_max=tick_price(signal.trigger+fraction*(signal.trigger-stop),tick)
        if quote and quote.limit_up:
            raw_max=min(raw_max,tick_price(quote.limit_up-tick,tick))
        result.update(entry_min=entry_min,no_chase=raw_max)
        low=int(round(entry_min/tick));high=int(round(raw_max/tick));best=None
        # RR falls monotonically with entry price. Binary search makes explicit
        # minimum-commission assumptions safe without a float-grid linear scan.
        while low<=high:
            mid=(low+high)//2;price=float(Decimal(mid)*Decimal(str(tick)))
            costs=_costs(signal,cfg,price,stop,target,qty,tick)
            if costs['net_rr'] is not None and costs['net_rr']>=min_rr:
                best=price;low=mid+1
            else:high=mid-1
        if best is None:
            result['costs']=_costs(signal,cfg,entry_min,stop,target,qty,tick)
            result['net_rr']=result['costs']['net_rr']
            result['reason']=('计入最小报价单位和滑点后，没有可成交价格区间' if entry_min>raw_max
                              else f'计入费用和滑点后收益风险比不足{min_rr:g}，没有可成交价格区间')
            return result
        quote_max=tick_price(best/(1+slip),tick)
        result.update(entry_max=best,quote_max=quote_max)
        if quote_max<trigger:
            result['reason']='盘口价格取整后没有可成交价格区间';return result
        fill=best
        if quote is not None:
            if quote.ask is None or not isfinite(float(quote.ask)) or quote.ask<=0 or not isfinite(float(quote.price)) or quote.price<=0:
                result['reason']='等待有效卖一报价';return result
            if quote.ask<trigger or quote.price<trigger:
                result['reason']='价格回到触发价以下，等待重新确认';return result
            fill=tick_price(tick_price(quote.ask,tick,up=True)*(1+slip),tick,up=True)
            result['estimated_fill']=fill
            if fill>best or quote.price>raw_max:
                result['reason']='计入费用和滑点后已超过不追价上限';return result
        costs=_costs(signal,cfg,fill,stop,target,qty,tick)
        result.update(net_rr=costs['net_rr'],costs=costs,executable=True,
                      reason='可成交区间通过最小报价单位、滑点及成本后收益风险比核验')
        return result
    except (TypeError,ValueError,OverflowError,ArithmeticError):
        result['reason']='价格或交易成本参数无效';return result
