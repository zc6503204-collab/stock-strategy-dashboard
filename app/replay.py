"""Point-in-time technical replay with the live research engine, never fake P&L.

Current classifications, risk flags and provider permissions are deliberately
absent from this interface. Only historical snapshots may audit those gates.
"""
from collections import Counter
from copy import deepcopy
from math import floor

from .calendars import is_open,local_date
from .models import Bar,now,stamp,symbol_market
from .research import ResearchEngine,BENCHMARKS
from .strategy_registry import StrategyRegistry,DEFINITIONS


def _bars(rows,symbol,source,as_of,intraday=False):
    selected={}
    rejected=0
    for raw in rows:
        try:
            b=raw if isinstance(raw,Bar) else Bar.load(raw)
            if b.symbol!=symbol or b.source!=source or not b.final or not b.valid():
                rejected+=1;continue
            if b.end>as_of:continue
            if intraday and not is_open(b.start,symbol_market(symbol)):continue
            # Do not use corrected duplicates to rewrite the first observation.
            selected.setdefault(b.start,b)
        except (ValueError,TypeError,KeyError,AttributeError):rejected+=1
    return [selected[k] for k in sorted(selected)],rejected


def _snapshots(rows,as_of):
    result=[]
    for row in rows:
        try:t=stamp(row.get('as_of') or row.get('checked_at'))
        except (ValueError,TypeError):continue
        if t<=as_of:result.append((t,row))
    return sorted(result,key=lambda x:x[0])


def _snapshot_gaps(snapshot,at,bar,market):
    missing=[]
    time,row=snapshot if snapshot else (None,{})
    timely=time is not None and 0<=(at-time).total_seconds()<=300
    chain=row.get('research_chain',{})
    if not timely or not isinstance(chain,dict) or not chain.get('checks'):
        missing.append('当时市场、行业和公告研究链快照')
    if not row.get('risk_group') or row.get('risk_group')=='pending':missing.append('当时风险标记')
    v=row.get('validation',{})
    if not timely or not v.get('ready') or v.get('source')!=bar.source:missing.append('当时证券及行情核验')
    if market=='CN' and (bar.limit_up is None or bar.limit_down is None):missing.append('当时涨跌停价格')
    q=row.get('quote',{})
    try:
        quote_ok=(timely and q.get('source')==bar.source and q.get('quality')=='realtime'
                  and 0<=(at-stamp(q.get('market_time'))).total_seconds()<=30
                  and 0<=(at-stamp(q.get('received_at'))).total_seconds()<=30
                  and 0<=(at-stamp(q.get('depth_time'))).total_seconds()<=15
                  and q.get('bid_size',0)>0 and q.get('ask_size',0)>0
                  and 0<q.get('bid',0)<=q.get('ask',0))
    except (ValueError,TypeError):quote_ok=False
    if not quote_ok:missing.append('当时可成交报价与盘口')
    return missing


def replay_signals(symbol,source,*,registry,bars,daily,benchmark_daily,benchmark_intraday,
                   strategy=None,context_history=(),as_of=None):
    """Replay fixed current strategy versions; report signal counts, no returns.

    inputs accept Bars or serialized Bars. context_history is an optional list
    of saved {as_of/checked_at, research_chain, validation, quote, risk_group,
    relative_rank}. Snapshot timestamps after each event are never available to
    that event. A current relative-strength rank cannot fill historical VCP gaps.
    """
    as_of=as_of or now();market=symbol_market(symbol)
    if strategy and (strategy not in DEFINITIONS or market not in DEFINITIONS[strategy]['markets']):
        raise ValueError('未知策略或策略不适用于当前市场')
    frozen=StrategyRegistry()
    frozen.revisions=deepcopy(registry.revisions);frozen.active=deepcopy(registry.active)
    applicable=[k for k,d in DEFINITIONS.items() if market in d['markets'] and (not strategy or k==strategy)]
    # An explicitly selected replay isolates that strategy. Its version,
    # parameters and enabled flag remain those used by the live engine.
    if strategy:
        for k in frozen.active[market]:
            if k!=strategy:
                version=frozen.active[market][k]
                next(r for r in frozen.revisions[market][k] if r['version']==version)['enabled']=False
    minute,rejected=_bars(bars,symbol,source,as_of,True)
    daily_rows,bad_daily=_bars(daily,symbol,source,as_of)
    benchmark=BENCHMARKS[market]
    benchmark_days,bad_benchmark=_bars(benchmark_daily,benchmark,source,as_of)
    benchmark_minutes,bad_minutes=_bars(benchmark_intraday,benchmark,source,as_of,True)
    snapshots=_snapshots(context_history,as_of);snapshot_i=-1
    sessions=sorted({local_date(b.start,market) for b in minute})
    split=max(1,floor(len(sessions)*.7)) if sessions else 0
    purge=max((DEFINITIONS[k]['max_hold_sessions'] for k in applicable),default=0)
    validation_days=set(sessions[split+purge:])
    engine=ResearchEngine(frozen);previous=[];signals=[];gaps=Counter();reasons=Counter()
    previous_day=None;contexts_complete=0;daily_ready=0;benchmark_ready=0;validation_counts=Counter()
    for b in minute:
        at=b.end;day=local_date(b.start,market)
        while snapshot_i+1<len(snapshots) and snapshots[snapshot_i+1][0]<=at:snapshot_i+=1
        snapshot=snapshots[snapshot_i] if snapshot_i>=0 else None
        row=snapshot[1] if snapshot else {}
        rank=row.get('relative_rank',{}) if snapshot and local_date(snapshot[0],market)==day else {}
        if day!=previous_day:
            # No current/future day's final OHLCV may leak into an intraday
            # decision, even when a today's cache supplied the input list.
            visible_daily=[x for x in daily_rows if local_date(x.start,market)<day]
            visible_benchmark=[x for x in benchmark_days if local_date(x.start,market)<day]
            engine.prepare(symbol,visible_daily,previous,visible_benchmark,rank)
            previous_day=day
        else:engine.set_relative_rank(symbol,rank.get('percentile'),rank.get('coverage',0))
        engine.set_benchmark(market,[x for x in benchmark_minutes if x.end<=at])
        needed=max((DEFINITIONS[k]['data_requirements']['daily_bars'] for k in applicable),default=25)
        if len(engine.context[symbol]['daily_bars'])>=needed:daily_ready+=1
        else:gaps['逐时点策略日线预热不足']+=1
        if any(x.start==b.start for x in benchmark_minutes):benchmark_ready+=1
        else:gaps['同源基准分钟线缺口']+=1
        missing=_snapshot_gaps(snapshot,at,b,market)
        if not missing:contexts_complete+=1
        gaps.update(missing)
        group=row.get('risk_group','pending') if snapshot and snapshot[0]<=at else 'pending'
        found=engine.update(b,group)
        for signal in found:
            signals.append(signal.dump())
            if day in validation_days:validation_counts[signal.strategy]+=1
        preview=engine.preview(symbol)
        for key in applicable:
            view=preview.get('strategies',{}).get(key)
            if view and not view.get('ready'):reasons[f'{key}: {view.get("reason","策略数据不足")}']+=1
        previous.append(b)
    insufficient=not minute or bool(gaps) or daily_ready<len(minute) or benchmark_ready<len(minute)
    counts=Counter(s['strategy'] for s in signals)
    versions={k:frozen.current(k,market)['version'] for k in applicable}
    return {'created_at':as_of.isoformat(),'symbol':symbol,'source':source,'strategy':strategy,
            'engine':'ResearchEngine','versions':versions,'version':versions.get(strategy) if strategy else None,
            'state':'insufficient_data' if insufficient else 'signal_exploration',
            'label':'同引擎逐时点技术信号探索；不计算未经历史成交证据支持的收益',
            'message':'历史市场、板块、事件、风险标记或盘口证据不完整，仅展示可重放技术信号' if insufficient
                      else '固定参数技术信号已重放；本接口不把信号命中当作成交或盈利',
            'result':{},'failures':[],'profit_computed':False,'bars':len(minute),
            'evaluation_bars':sum(local_date(b.start,market) in validation_days for b in minute),
            'signal_count':len(signals),'signal_counts':{k:counts[k] for k in applicable},
            'validation_signal_counts':{k:validation_counts[k] for k in applicable},
            'signals':signals[-100:],'signals_truncated':len(signals)>100,
            'coverage':{'bars':len(minute),'daily_ready_bars':daily_ready,'benchmark_bars':benchmark_ready,
                        'complete_context_bars':contexts_complete,'rejected_rows':rejected+bad_daily+bad_benchmark+bad_minutes},
            'missing':[{'reason':k,'bars':v} for k,v in sorted(gaps.items())],
            'strategy_blockers':[{'reason':k,'bars':v} for k,v in reasons.most_common(15)],
            'validation':{'method':'固定参数、按时间前70%预热及后30%留出；边界剔除最长持有期',
                          'training_end':str(sessions[split-1]) if sessions else None,
                          'validation_start':str(sessions[split+purge]) if split+purge<len(sessions) else None,
                          'purge_sessions':purge,'purged_dates':[str(d) for d in sessions[split:split+purge]],
                          'validation_sessions':len(validation_days),'max_parameter_trials':9,
                          'parameter_trials':0,'optimization_performed':False},
            'limitations':['当前提供的单股历史不能证明完整历史证券范围覆盖，不能视为无偏全市场回测',
                           '日线缓存的调整口径并非逐时点快照，技术探索不替代对除权与修订数据的审计',
                           '未用当今行业、公告、ST或涨跌停规则回填历史；没有推算成交收益']}
