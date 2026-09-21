"""Whole-universe quote discovery and a separate, fair minute-validation queue.

The 80-name presentation pool and 12 live books never bound discovery. This
worker reads daily caches only; a slow history request cannot hold its queue.
"""
import asyncio
from collections import OrderedDict
from uuid import uuid4
from zoneinfo import ZoneInfo

from .models import Bar, now, stamp, in_scope
from .calendars import local_date, is_open
from .research import ResearchEngine
from .selection import priority_class


def fresh_discovery_quote(q, t):
    return bool(q and q.market_time and q.quality=='realtime' and q.price>0
                and 0<=(t-q.market_time).total_seconds()<=300
                and 0<=(t-q.received_at).total_seconds()<=300)


def intraday_priority(row,q,t):
    """A coarse workload priority only; never a same-time-volume or buy signal."""
    local=t.astimezone(ZoneInfo('Asia/Shanghai'));minute=local.hour*60+local.minute+local.second/60
    elapsed=max(0,min(minute,690)-570)+max(0,min(minute,900)-780)
    volume=q.volume;amount=q.turnover
    gap=volume is None or amount is None or volume<0 or amount<0
    vr=ar=None
    if not gap and elapsed>=5:
        if row.get('average_volume',0)>0:vr=volume/(row['average_volume']*elapsed/240)
        if row.get('average_turnover',0)>0:ar=amount/(row['average_turnover']*elapsed/240)
    distances=[]
    for strategy in row.get('candidate_strategies',[]):
        level=row.get('strategy_context',{}).get(strategy,{}).get('level')
        if not level and strategy=='vcp_swing':level=row.get('high10')
        level=level or row.get('breakout_reference')
        if level and level>0:distances.append((abs(q.price/level-1),strategy,level,(q.price/level-1)*100))
    best=min(distances) if distances else None
    distance=best[3] if best else row.get('intraday_distance_to_high_pct',999)
    near=abs(distance)<=3
    expanding=vr is not None and vr>=1.2 and ar is not None and ar>=1.2
    row.update(intraday_volume=volume,intraday_turnover=amount,volume_data_missing=gap,
               coarse_volume_ratio=vr,coarse_turnover_ratio=ar,coarse_elapsed_minutes=round(elapsed,2),
               coarse_volume_method='累计量额除以20日日均量额及已交易分钟占240分钟比例；仅轻筛估算，非同时间量比',
               intraday_distance_to_trigger_pct=distance,discovery_trigger_strategy=best[1] if best else None,
               discovery_priority=int(near)+int(expanding),
               discovery_stage='forming' if near and expanding else 'watch')
    return gap


class Discovery:
    def __init__(self, research):
        self.a=research;self.d=research.d;self.store=research.store
        self.index=self.store.get('research_evaluation_index',{})
        self.status=self.store.get('discovery_status',{})
        if self.status.get('state')=='running':
            self.status.update(state='interrupted',reason='全范围发现上次中断，恢复后重新获取本轮报价')
        self.queue=OrderedDict()
        for symbol in self.store.get('research_validation_queue',[]):
            if symbol in self.index:self.queue[symbol]=dict(self.index[symbol])
        self.validation_task=None;self.active_symbol=None
        self.enqueued_at=self.store.get('research_validation_enqueued',{})
        self.active_started_at=None
        saved_changes=self.store.get('discovery_round_changes',{})
        self.round_changes={k:set(saved_changes.get(k,[])) for k in ('outside_discovered','new_entries','replacements','missing')}
        self.initial_pool=set()
        self.quote_times={};self.last_slot=None

    def scope(self,t):
        day=str(local_date(t,'CN'));universe=self.store.get('cn_universe',{})
        checkpoint=self.store.get('cn_universe_checkpoint',{})
        if checkpoint.get('date')==day and universe.get('date')!=day:
            # New-day pagination may still be running. Continue discovering
            # across every previously known security, adding newly seen ones;
            # membership coverage remains explicitly incomplete.
            known={r['symbol']:r for r in universe.get('rows',[])}
            known.update({r['symbol']:r for r in checkpoint.get('rows',[])})
            universe={**checkpoint,'rows':list(known.values()),'complete':False}
        rows=[];invalid=False;candidate_date=self.d.candidate_trade_date('CN')
        for raw in universe.get('rows',[]):
            symbol=raw.get('symbol','');name=raw.get('name') or symbol
            try:supported=bool(symbol and in_scope(symbol))
            except (ValueError,TypeError):invalid=True;continue
            if not supported or any(x in name.upper() for x in ('ETF','ETN','退市','退整理')):continue
            rows.append({'symbol':symbol,'name':name,'industry':raw.get('industry'),'market':'CN',
                         'market_cap':raw.get('marketcap'),'source':'longbridge','candidate_origin':'universe',
                         'scope':'沪深主板和创业板分页证券库','pool_role':'daily_core',
                         'candidate_date':candidate_date,'candidate_updated_at':t.isoformat()})
        return rows, bool(universe.get('complete') and universe.get('date')==day and not invalid)

    def persist(self):
        self.store.set('research_evaluation_index',self.index)
        self.store.set('research_validation_queue',list(dict.fromkeys(([self.active_symbol] if self.active_symbol else [])+list(self.queue))))
        self.store.set('research_validation_enqueued',{**self.enqueued_at,**({self.active_symbol:self.active_started_at} if self.active_symbol else {})})
        self.store.set('discovery_status',self.status)
        self.store.set('discovery_round_changes',{k:list(v) for k,v in self.round_changes.items()})

    def merge_daily(self, rows):
        """A background history result must not roll back a newer quote/Setup."""
        for item in rows:
            symbol=item['symbol'];old=self.index.get(symbol,{})
            merged=dict(item)
            same=(old.get('checked_session')==item.get('checked_session')
                  and old.get('as_of')==item.get('as_of')
                  and old.get('strategy_versions')==item.get('strategy_versions'))
            if same and not item.get('hard_veto'):
                for k,v in old.items():
                    if k.startswith(('setup_','discovery_','coarse_','intraday_')) or k in ('plan_id','plan','signal_id','quote_as_of','observed_price','volume_data_missing'):
                        merged[k]=v
                if old.get('quote_as_of') and old.get('discovery_round_id'):
                    for k in ('decision','score','reason','data_quality','name_verified'):
                        if k in old:merged[k]=old[k]
            if old.get('name_verified') and old.get('checked_session')==item.get('checked_session'):
                merged.update(name_verified=True,name=old['name'])
            self.index[symbol]=merged

    def enqueue(self,row):
        symbol=row['symbol']
        if row.get('decision')=='暂不参与' or row.get('data_quality')=='missing':return
        # Replacing a queued snapshot preserves its FIFO position: a busy pool
        # cannot perpetually jump ahead of an outside candidate.
        self.queue[symbol]=dict(row)
        self.enqueued_at.setdefault(symbol,now().isoformat())
        self.start_validation()

    def start_validation(self):
        if self.queue and (not self.validation_task or self.validation_task.done()):
            self.validation_task=asyncio.create_task(self.validate())
            self.d.tasks.append(self.validation_task)

    async def validate(self):
        while self.queue and self.d.alive and self.d.monitor_running:
            t=now();pool={r['symbol'] for r in self.d.selection if r.get('market')=='CN'}
            if not is_open(t,'CN'):break
            ready=[s for s in self.queue if fresh_discovery_quote(self.a.discovery_quotes.get(s),t)]
            # Preserve stale work for the next quote round; never busy-loop or
            # let a restored queue validate against yesterday's market snapshot.
            if not ready:break
            def priority(symbol):
                row=self.queue[symbol];queued=self.enqueued_at.get(symbol,t.isoformat())
                age=(t-stamp(queued)).total_seconds()
                # After one five-minute cycle, oldest work takes precedence over
                # newly arriving attractive names. Otherwise warm the pool and
                # near-trigger outside candidates first.
                return (0,queued) if age>=300 else (1,priority_class(row),-row.get('discovery_priority',0),symbol not in pool,
                    abs(row.get('intraday_distance_to_trigger_pct',row.get('intraday_distance_to_high_pct',row.get('distance_to_high_pct',999)))),queued,symbol)
            symbol=min(ready,key=priority);row=self.queue.pop(symbol)
            self.active_symbol=symbol;self.active_started_at=self.enqueued_at.pop(symbol,t.isoformat())
            current=self.index.get(symbol,{})
            if current.get('decision')=='暂不参与' or current.get('data_quality')=='missing' or current.get('checked_session')!=str(local_date(now(),'CN')):
                continue
            try:
                result=await self.d.validate_research_candidate(dict(current))
            except asyncio.CancelledError:
                self.queue.setdefault(symbol,row);self.enqueued_at.setdefault(symbol,self.active_started_at);self.persist();raise
            except Exception as exc:
                result={'setup_stage':'unavailable','setup_ready':False,'setup_checked_at':now().isoformat(),
                        'setup_reason':'分钟验证暂未完成','setup_error_type':type(exc).__name__}
            latest=self.index.get(symbol,{})
            if not is_open(now(),'CN') or not fresh_discovery_quote(self.a.discovery_quotes.get(symbol),now()):
                if latest.get('decision')!='暂不参与':
                    latest.update(setup_stage='unavailable',setup_ready=False,
                                  setup_reason='交易时段已结束或报价已过期，恢复报价后重新验证')
                    self.queue.setdefault(symbol,dict(latest))
                    self.enqueued_at.setdefault(symbol,self.active_started_at)
                continue
            same=(latest.get('checked_session')==current.get('checked_session')
                  and latest.get('strategy_versions')==current.get('strategy_versions'))
            if (result or {}).get('setup_checked_at') and latest.get('setup_checked_at'):
                same=same and stamp(result['setup_checked_at'])>=stamp(latest['setup_checked_at'])
            if same and latest.get('decision')!='暂不参与':
                # Keep the latest broad-scan quote and daily decision. Only the
                # validation-owned fields may be merged from an awaited result.
                for k,v in (result or {}).items():
                    if k.startswith('setup_') or k in ('plan_id','plan','signal_id','name_verified','name','risk_group'):
                        latest[k]=v
                if latest.get('setup_stage')=='invalid':
                    latest.update(decision='暂不参与',reason=latest.get('setup_reason','当前Setup失效'))
                self.store.research_save('validation',uuid4().hex,
                    {'market':'CN','date':str(local_date(now(),'CN')),**latest})
                await self.a.publish([],partial=True,source='validation')
            self.active_symbol=None;self.active_started_at=None
            self.store.set('research_validation_queue',list(self.queue))
            await asyncio.sleep(0)
        self.active_symbol=None;self.active_started_at=None
        self.persist();self.d.broadcast()

    async def run(self):
        from .autoresearch import daily_cutoff, complete_daily, IMPLEMENTATION
        t=now();day=str(local_date(t,'CN'));cutoff=str(daily_cutoff(t))
        round_id=uuid4().hex;rows,universe_complete=self.scope(t)
        self.quote_times={}
        self.a.discovery_quotes={}
        self.round_changes={k:set() for k in ('outside_discovered','new_entries','replacements','missing')}
        self.initial_pool={r['symbol'] for r in self.d.selection if r.get('market')=='CN'}
        self.status={'round_id':round_id,'date':day,'started_at':t.isoformat(),'state':'running',
                     'implementation':IMPLEMENTATION,
                     'supported':len(rows),'universe_complete':universe_complete,'quote_checked':0,
                     'quote_missing':0,'daily_checked':0,'daily_missing':0,'attempted':0,
                     'volume_missing':0,
                     'reason':'全支持范围按当前行情重新发现候选，历史补齐独立运行'}
        self.store.set('discovery_status',self.status);self.d.broadcast()
        benchmark_cache=self.store.get('selection_daily:000300.SH',{})
        benchmark=complete_daily([Bar.load(b) for b in benchmark_cache.get('bars',[])],t)
        if benchmark_cache.get('cutoff')!=cutoff or not benchmark or str(local_date(benchmark[-1].start,'CN'))!=cutoff:benchmark=[]
        if benchmark:self.d.benchmark_daily['CN']=benchmark
        engine=ResearchEngine(self.d.registry)
        seen=set()
        try:
            for offset in range(0,len(rows),100):
                if not self.d.alive or not self.d.monitor_running:raise asyncio.CancelledError()
                if str(local_date(now(),'CN'))!=day or not is_open(now(),'CN'):
                    raise ValueError('交易时段已变化，下一轮重新核验行情')
                batch=rows[offset:offset+100]
                try:quotes=await self.d.lb.quotes([r['symbol'] for r in batch],background=True)
                except Exception:quotes=[]
                if str(local_date(now(),'CN'))!=day or not is_open(now(),'CN'):
                    raise ValueError('交易时段已变化，下一轮重新核验行情')
                lookup={q.symbol:q for q in quotes}
                for raw in batch:
                    symbol=raw['symbol'];seen.add(symbol);q=lookup.get(symbol)
                    old=self.index.get(symbol,{})
                    cache=self.store.get('selection_daily:'+symbol,{})
                    bars=complete_daily([Bar.load(b) for b in cache.get('bars',[])],t)
                    valid_quote=fresh_discovery_quote(q,now())
                    self.status['attempted']+=1
                    self.status['quote_checked' if valid_quote else 'quote_missing']+=1
                    if valid_quote:
                        self.quote_times[symbol]=q.market_time.isoformat()
                        self.a.discovery_quotes[symbol]=q
                        if q.volume is None or q.turnover is None or q.volume<0 or q.turnover<0:
                            self.status['volume_missing']+=1
                    try:
                        if (cache.get('cutoff')!=cutoff or not bars or
                            str(local_date(bars[-1].start,'CN'))!=cutoff or not benchmark):raise ValueError('完整日线待补齐')
                        rank={'percentile':old.get('rs_percentile'),'coverage':old.get('rs_rank_coverage',0)}
                        item=self.a.evaluate_daily(raw,bars,benchmark,engine,t,rank)
                        self.status['daily_checked']+=1
                        if old.get('name_verified') and old.get('checked_session')==day:item.update(name_verified=True,name=old['name'])
                        if old.get('strategy_versions')==item.get('strategy_versions') and old.get('checked_session')==day:
                            item.update({k:v for k,v in old.items() if k.startswith('setup_') or k in ('plan','plan_id','signal_id')})
                        if valid_quote:
                            self.a.apply_price(item,q)
                            intraday_priority(item,q,now())
                        else:item.update(data_quality='missing',reason='本轮全范围报价缺失或超过五分钟，等待恢复',setup_ready=False,setup_stage='unavailable')
                    except Exception:
                        self.status['daily_missing']+=1
                        item={**old,**raw,'checked_session':day,'data_quality':'missing','score':old.get('score',0),
                              'decision':'等确认','candidate_strategies':[],'setup_ready':False,'setup_stage':'unavailable',
                              'reason':'完整日线或同源基准待补齐，仍保留在全范围发现中'}
                    finally:engine.context.pop(symbol,None)
                    item.update(discovery_round_id=round_id,discovery_checked_at=now().isoformat())
                    self.index[symbol]=item
                    if item.get('data_quality')=='missing' or item.get('volume_data_missing'):self.round_changes['missing'].add(symbol)
                    if valid_quote and item.get('candidate_strategies') and item.get('decision')!='暂不参与':
                        if symbol not in self.initial_pool:
                            self.round_changes['outside_discovered'].add(symbol)
                            self.status['last_outside_at']=now().isoformat()
                        self.enqueue(item)
                    if self.status['attempted']%20==0:await asyncio.sleep(0)
                # Quote failures remain in the global index; they are never
                # counted as screened successfully or silently removed.
                await self.a.publish([],partial=True,source='discovery')
                self.status['heartbeat']=now().isoformat()
                self.store.set('discovery_status',self.status);self.d.broadcast()
                await asyncio.sleep(.05)
            if universe_complete:
                for symbol in list(self.index):
                    if symbol not in seen:
                        self.index[symbol].update(decision='暂不参与',reason='已不在本交易日支持证券范围',setup_stage='invalid',setup_ready=False)
            age_stale=sum(not fresh_discovery_quote(q,now()) for q in self.a.discovery_quotes.values())
            complete=bool(rows and universe_complete and not self.status['quote_missing'] and not self.status['daily_missing']
                          and not self.status['volume_missing'] and not age_stale)
            self.status.update(state='complete' if complete else 'partial',ended_at=now().isoformat(),
                               quote_expired_during_round=age_stale,
                               reason='全范围本轮发现完成' if complete else '本轮报价、日线或证券范围存在缺口，不能声称全覆盖')
            refresh=getattr(self.d,'refresh_research_context',None)
            if refresh:refresh()
            await self.a.publish([],partial=not complete,source='discovery')
        except asyncio.CancelledError:
            self.status.update(state='interrupted',reason='全范围发现中断，保留已检查记录，恢复后重新取价');raise
        except Exception as exc:
            self.status.update(state='interrupted',reason=str(exc) if isinstance(exc,ValueError) else '全范围发现暂未完成')
        finally:
            self.status['heartbeat']=now().isoformat()
            self.store.research_save('discovery',round_id,{'market':'CN',**self.status})
            self.persist();self.d.broadcast()

    def diagnostics(self):
        t=now();day=str(local_date(t,'CN'))
        status=dict(self.status) if self.status.get('date')==day else {}
        supported=status.get('supported') or len(self.scope(t)[0])
        defaults={'state':'waiting','reason':'等待本轮行情发现','quote_checked':0,'quote_missing':0,
                  'daily_checked':0,'daily_missing':0,'volume_missing':0,'attempted':0}
        status={**defaults,**status,'supported':supported}
        quotes=sum(fresh_discovery_quote(q,t) for q in self.a.discovery_quotes.values())
        if status['state']=='complete' and quotes<supported:
            status.update(state='waiting',reason='最近一轮已完成，当前报价待重新获取或已过期')
        minute_rows=[r for r in self.index.values() if r.get('checked_session')==day and r.get('setup_checked_at')
                     and 0<=(t-stamp(r['setup_checked_at'])).total_seconds()<=360 and r.get('setup_ready')]
        waiting=[r for r in self.index.values() if r.get('checked_session')==day and r.get('candidate_strategies') and r.get('decision')!='暂不参与']
        live=[s for s in self.d.tracked() if s.endswith(('.SH','.SZ'))]
        from .decisions import fresh_quote, book_ok
        executable=sum(fresh_quote(self.d.quotes.get(s),t) and book_ok(self.d.quotes.get(s),t) for s in live)
        delayed=bool(self.status.get('started_at') and self.status.get('state')=='running' and (t-stamp(self.status['started_at'])).total_seconds()>300)
        times=list(self.enqueued_at.values())+([self.active_started_at] if self.active_started_at else [])
        oldest=max((max(0,(t-stamp(x)).total_seconds()) for x in times if x),default=0)
        return {**status,'schedule_delayed':delayed,
                'round_changes':{k:len(v) if self.status.get('date')==day else 0 for k,v in self.round_changes.items()},'oldest_wait_seconds':round(oldest),
                'quote_coverage':{'checked':status['quote_checked'],'fresh_now':quotes,'total':supported,'missing':status['quote_missing'],
                                  'unquoted':max(0,supported-status['attempted']),
                                  'complete':bool(supported and quotes==supported and status.get('universe_complete'))},
                'signal_coverage':{'checked':len(minute_rows),'eligible':len(waiting),'total':supported},
                'execution_coverage':{'ready':executable,'monitored':len(live),'limit':12},
                'validation_backlog':len(self.queue)+(1 if self.active_symbol else 0),
                'validation_active':self.active_symbol,'index_size':len(self.index)}
