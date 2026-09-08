"""Shared, deterministic sizing for live proposals and paper fills."""
from math import floor
from .models import symbol_market

def size_entry(signal,price,account,cfg,liquidity):
    market=symbol_market(signal.symbol);lot=100 if market=='CN' else 1
    if signal.symbol in account['positions']:return {'ok':False,'reason':'已有该股模拟持仓'}
    if len(account['positions'])>=cfg['max_positions']:return {'ok':False,'reason':'三个持仓名额已满'}
    if signal.risk_group!='normal' and any(p['risk_group']!='normal' for p in account['positions'].values()):
        return {'ok':False,'reason':'高风险持仓名额已占用'}
    distance=price-signal.stop
    if distance<=0:return {'ok':False,'reason':'当前价格已失守止损价'}
    if market=='CN' and signal.version.startswith('实验 2'):
        atr=signal.evidence.get('atr')
        if not atr or atr<=0:return {'ok':False,'reason':'缺少日线ATR，不能计算隔夜风险仓位'}
        distance=max(distance,atr)
    slip=cfg['high_risk_slippage'] if signal.risk_group!='normal' else cfg['slippage']
    unit=distance+cfg['fee_rate']*(price+signal.stop)+signal.stop*slip
    equity=account['cash']+sum(p['remaining']*p['mark'] for p in account['positions'].values())
    qty=floor(min(equity*cfg['risk_per_trade']/unit,account['cash']/(price*(1+cfg['fee_rate'])),max(0,liquidity))/lot)*lot
    if qty<lot:return {'ok':False,'reason':'风险预算、现金或流动性不足一手'}
    return {'ok':True,'qty':qty,'price':price,'cash_required':qty*price*(1+cfg['fee_rate']),
            'planned_risk':qty*unit,'risk_budget':equity*cfg['risk_per_trade'],'cost_ratio':(unit-distance)/distance,
            'target':price+2*(price-signal.stop),'stop':signal.stop}
