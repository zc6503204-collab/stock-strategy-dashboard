"""Deterministic close-of-bar signals. No LLM or quote from another source enters a decision."""
from hashlib import sha256
from .models import Signal, symbol_market
from .calendars import local_date,open_time

class Strategies:
    def __init__(self):
        self.history={}
        self.pullbacks={}

    def reset(self,symbol):
        self.history.pop(symbol,None);self.pullbacks.pop(symbol,None)

    def update(self,bar,risk_group='normal'):
        if not bar.final or not bar.valid():return []
        hist=self.history.setdefault(bar.symbol,[])
        if hist and (bar.source!=hist[-1].source or bar.start<=hist[-1].start):return []
        signals=[]
        previous=hist[-1] if hist else None
        day=local_date(bar.start,symbol_market(bar.symbol))
        today=[b for b in hist if local_date(b.start,symbol_market(b.symbol))==day]+[bar]
        vol=sum(b.volume for b in today)
        # Typical-price VWAP is an explicit OHLCV approximation, not an exchange VWAP.
        vwap=sum(((b.high+b.low+b.close)/3)*b.volume for b in today)/vol if vol and today[0].start==open_time(day,symbol_market(bar.symbol)) else None
        base=self.pullbacks.get(bar.symbol)
        if base:
            base['age']+=1
            if base['age']>6 or day!=base['day'] or bar.close<base['level'] or (base['touched'] and bar.low<base['low']):
                self.pullbacks.pop(bar.symbol,None)
            elif base['touched'] and previous and bar.close>previous.high:
                if bar.close>base['low'] and vwap and bar.close>vwap:
                    signals.append(self.signal(bar,'pullback',bar.close,base['low'],risk_group,dict(level=base['level'],vwap=vwap,age=base['age'])))
                self.pullbacks.pop(bar.symbol,None)
            elif bar.low<=base['level']*1.003 and bar.close>=base['level']:
                base['touched']=True;base['low']=bar.low
        if len(hist)>=21:
            window=hist[-20:]
            mean=sum(b.close for b in window)/20
            prior_mean=sum(b.close for b in hist[-21:-1])/20
            ceiling=max(b.high for b in window)
            avgvol=sum(b.volume for b in window)/20
            stop=min(b.low for b in hist[-3:]+[bar])
            if (mean>prior_mean and vwap and bar.close>vwap and bar.close>ceiling
                and avgvol>0 and bar.volume>=1.5*avgvol and stop<bar.close):
                signals.append(self.signal(bar,'breakout',bar.close,stop,risk_group,dict(level=ceiling,vwap=vwap,volume_ratio=bar.volume/avgvol,ma20=mean,prior_ma20=prior_mean)))
                self.pullbacks[bar.symbol]=dict(level=ceiling,low=bar.low,day=day,age=0,touched=False)
        hist.append(bar)
        del hist[:-500]
        return signals

    @staticmethod
    def signal(bar,strategy,trigger,stop,group,evidence):
        sid=sha256(f'{bar.symbol}|{bar.source}|{bar.start.isoformat()}|{strategy}|1.0.0'.encode()).hexdigest()[:24]
        return Signal(sid,bar.symbol,strategy,bar.source,bar.end,trigger,stop,group,
                      {**evidence,'bar':bar.dump(),'vwap_method':'5分钟OHLCV典型价格近似VWAP'})

    def preview(self,symbol):
        hist=self.history.get(symbol,[])
        if len(hist)<21:return {'ready':False,'reason':'不足21根完整5分钟K线'}
        w=hist[-20:]
        return {'ready':True,'level':max(b.high for b in w),'stop':min(b.low for b in hist[-3:]),
                'source':hist[-1].source,'bars':len(hist),'last_bar':hist[-1].start.isoformat()}
