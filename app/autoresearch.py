"""Resumable CN discovery, independent from ranking lists and execution feeds."""
import asyncio
from collections import Counter
from zoneinfo import ZoneInfo
from datetime import timedelta
from uuid import uuid4

from .models import now, stamp, Bar, symbol_market, in_scope
from .calendars import calendar, local_date, close_time, is_open, phase
from .selection import evaluate, ranking_key, priority_class, DECISION_ORDER
from .research import ResearchEngine, daily_factors
from .strategy_registry import DEFINITIONS

IMPLEMENTATION='dynamic_discovery_v2'

def daily_cutoff(t, market='CN'):
    day = local_date(t, market)
    cal = calendar(market)
    if cal.is_session(str(day)) and t >= close_time(day, market) + timedelta(minutes=5):
        return day
    return cal.date_to_session(str(day), direction='previous').date() if not cal.is_session(str(day)) else cal.previous_session(str(day)).date()


def complete_daily(bars, t, market='CN'):
    cutoff = daily_cutoff(t, market)
    return [b for b in bars if local_date(b.start, market) <= cutoff and b.valid()]


def rotate_monitoring(current, ranked, protected, votes, round_id, limit=12):
    """Two independent rounds for a >=5-point improvement; hard rejects leave now."""
    lookup = {r['symbol']: r for r in ranked}
    eligible = [r for r in sorted(ranked, key=ranking_key)
                if r.get('decision') != '暂不参与' and r.get('data_quality') != 'missing'
                and r.get('name_verified', False) and r.get('candidate_strategies')]
    valid = {r['symbol']: r for r in eligible}
    pinned = list(dict.fromkeys(protected))
    kept = [s for s in current if s in valid and s not in pinned][:max(0, limit-len(pinned))]
    changes = []
    for s in current:
        if s not in pinned and s not in valid:
            changes.append({'symbol':s, 'state':'已失效' if lookup.get(s,{}).get('decision')=='暂不参与' else '待恢复',
                            'reason':lookup.get(s,{}).get('reason','本轮未通过候选资格复核')})
    active_votes = set()
    for row in eligible:
        s = row['symbol']
        if s in pinned or s in kept: continue
        if len(pinned)+len(kept) < limit:
            kept.append(s); changes.append({'symbol':s,'state':'新入选','reason':'本轮策略复核通过，进入空闲监测名额'}); continue
        if not kept: break
        weakest = max(kept, key=lambda x:ranking_key(valid[x]))
        old = valid[weakest]
        better_class = priority_class(row) < priority_class(old)
        better_score = priority_class(row)==priority_class(old) and row['score'] >= old['score']+5
        key = s+'|'+weakest
        if not (better_class or better_score): continue
        active_votes.add(key)
        vote = votes.setdefault(key, {'count':0,'round':None})
        if vote['round'] != round_id:
            vote.update(count=vote['count']+1, round=round_id)
        if better_class or vote['count'] >= 2:
            kept[kept.index(weakest)] = s
            changes.extend([{'symbol':weakest,'state':'被替换','reason':'更优候选 '+s+' 通过替换门槛'},
                            {'symbol':s,'state':'新入选','reason':'优先级提高' if better_class else '连续两轮领先至少5分'}])
            votes.pop(key,None)
    for key in list(votes):
        if key not in active_votes: votes.pop(key,None)
    return (pinned+kept)[:limit], changes


class AutoResearch:
    def __init__(self, dashboard):
        self.d = dashboard
        self.store = dashboard.store
        self.task = None
        self.pool_task = None
        self.status = self.store.get('auto_research_status', {})
        if self.status.get('state') in ('running','enumerating'):
            self.status.update(state='interrupted', reason='上次扫描中断，恢复后从有效缓存继续')
        self.votes = {}
        self.last_slot = None
        self.last_pool_slot = None
        self.failed_at = None
        self.last_runtime_minute=None
        self.pending = False
        self.previous_pool={}
        self.publish_lock=asyncio.Lock()
        self.discovery_quotes={}
        from .discovery import Discovery
        self.discovery=Discovery(self)
        self.evaluations=self.discovery.index

    def expire_previous_day(self,t):
        day=str(local_date(t,'CN'))
        changed=False
        for row in self.d.selection:
            if row.get('market')=='CN' and row.get('checked_session')!=day and row.get('data_quality')!='missing':
                row.update(data_quality='missing',reason='新交易日尚未重新通过策略复核')
                changed=True
        for row in self.evaluations.values():
            if row.get('checked_session')!=day:
                row.update(data_quality='missing',setup_ready=False,setup_stage='unavailable',name_verified=False)
        if changed and self.d.daily_core.get('CN'):
            self.d.daily_core['CN']['stale']=True
            self.store.set('daily_candidate_core',self.d.daily_core)
        self.d.allocate_monitoring()

    def save_status(self, **values):
        self.status.update(values, heartbeat=now().isoformat())
        self.store.set('auto_research_status', self.status)
        if self.status.get('run_id'):
            self.store.research_save('run', self.status['run_id'], self.status)
        self.d.broadcast()

    def request(self, force=False):
        if self.task and not self.task.done():
            self.pending = self.pending or force
            return self.task
        self.task = asyncio.create_task(self.scan(force))
        self.d.tasks.append(self.task)
        return self.task

    async def universe(self, day, force=False):
        cached = self.store.get('cn_universe', {})
        if cached.get('date')==day and cached.get('complete') and not force:
            self.save_status(universe_total=cached['total'],enumerated=len(cached['rows']),pages=None)
            return cached
        # Retry an interrupted page sequence, but never inherit yesterday's universe as today's completion.
        checkpoint = self.store.get('cn_universe_checkpoint', {})
        if checkpoint.get('date')!=day or force: checkpoint = {'date':day,'rows':[],'page':0,'total':None}
        rows = {r['symbol']:r for r in checkpoint['rows']}
        page = checkpoint['page']; total = checkpoint['total']
        try:
            while True:
                payload = await self.d.lb.universe_page(page, 100)
                items = payload.get('items', [])
                reported = payload.get('total')
                if not isinstance(reported,int) or reported<1 or payload.get('page')!=page:
                    raise ValueError('分页总数或页号缺失')
                if total is not None and reported!=total:
                    self.store.set('cn_universe_checkpoint', {})
                    raise ValueError('分页期间证券总数变化，下一轮重新枚举')
                total = reported
                if not items or any(not isinstance(r,dict) or not r.get('symbol') for r in items):
                    raise ValueError('分页为空或证券标识缺失')
                if any(r['symbol'] in rows for r in items) or len({r['symbol'] for r in items})!=len(items):
                    self.store.set('cn_universe_checkpoint', {})
                    raise ValueError('分页重复，不能确认覆盖完整')
                rows.update({r['symbol']:r for r in items})
                page += 1
                self.store.set('cn_universe_checkpoint', {'date':day,'rows':list(rows.values()),'page':page,'total':total})
                self.save_status(universe_total=total, enumerated=len(rows), pages=page)
                if len(rows)==total: break
                if len(rows)>total: raise ValueError('分页数量超过声明总数')
                await asyncio.sleep(.6)
            result = {'date':day,'rows':list(rows.values()),'complete':True,'total':total,'updated_at':now().isoformat()}
            self.store.set('cn_universe',result)
            self.store.set('cn_universe_checkpoint',{})
            return result
        except Exception:
            self.save_status(universe_complete=False, enumerated=len(rows), universe_total=total)
            raise

    async def daily(self, symbol, t):
        key='selection_daily:'+symbol
        cached=self.store.get(key,{})
        cutoff=str(daily_cutoff(t, symbol_market(symbol)))
        bars=complete_daily([Bar.load(b) for b in cached.get('bars',[])],t,symbol_market(symbol))
        if cached.get('cutoff')==cutoff and bars and str(local_date(bars[-1].start,symbol_market(symbol)))==cutoff:
            return bars
        # Small incremental request once warmed; full history only on first access or a long gap.
        count=270
        if len(bars)>=260 and (daily_cutoff(t,symbol_market(symbol))-local_date(bars[-1].start,symbol_market(symbol))).days<20:
            count=25
        fresh=await self.d.lb.bars(symbol,'day',count,force_cli=True)
        merged={b.start:b for b in bars}
        merged.update({b.start:b for b in complete_daily(fresh,t,symbol_market(symbol))})
        bars=[merged[k] for k in sorted(merged)][-270:]
        if not bars or str(local_date(bars[-1].start,symbol_market(symbol)))!=cutoff:
            raise ValueError('最近完整交易日日线尚未返回')
        self.store.set(key,{'fetched_date':str(local_date(t,symbol_market(symbol))), 'cutoff':cutoff,
                            'fetched_at':now().isoformat(),'bars':[b.dump() for b in bars]})
        return bars

    def evaluate_daily(self,row,bars,benchmark,engine,t,relative_rank=None):
        """Strategy-specific admission; generic MA20 ranking is not a veto."""
        item=evaluate(row,bars)
        engine.prepare(row['symbol'],bars,[],benchmark,relative_rank)
        strategy_context={k:engine._daily(row['symbol'],k) for k,v in DEFINITIONS.items()
                          if 'CN' in v['markets'] and self.d.registry.enabled(k,'CN')}
        matches=[k for k,v in strategy_context.items() if v.get('eligible')]
        structural_stops={}
        for strategy in matches:
            context=strategy_context[strategy]
            stop=context.get('structural_stop') or context.get('stop')
            if stop is None and strategy=='vcp_swing':
                count=int(self.d.registry.config(strategy,'CN')['final_contraction_days'])
                stop=min(b.low for b in bars[-count:])
            structural_stops[strategy]=stop if stop is not None else item['structure_low']
        f=daily_factors(row['symbol'],bars,benchmark)
        item.update(row,**{k:v for k,v in item.items() if k not in row})
        item.update(candidate_strategies=matches,relative_strength=f.get('relative_strength'),
                    name_verified=False,data_quality='complete',checked_at=now().isoformat(),
                    checked_session=str(local_date(t,'CN')),run_id=self.status.get('run_id'),
                    daily_decision=item['decision'],daily_reason=item['reason'],
                    strategy_versions={k:self.d.registry.current(k,'CN')['version'] for k in matches},
                    setup_stage='watch',setup_ready=False,daily_score=item['score'])
        item.update(strategy_context=strategy_context,structural_stops=structural_stops,
                    daily_candidate_strategies=list(matches))
        if not matches or item.get('hard_veto'):
            item.update(decision='暂不参与',reason=item['reason'] if item.get('hard_veto') else '未匹配已启用策略日线条件')
        elif item['decision']=='暂不参与':
            item.update(decision='等确认',reason='策略日线条件通过，等待对应盘中Setup确认')
        item['evidence_chain']={
            'market':{'state':'partial','benchmark':'000300.SH','cutoff':str(daily_cutoff(t)),'missing':'完整市场情绪状态机'},
            'sector':{'state':'partial','industry':row.get('industry'),'missing':'板块持续性与梯队'},
            'stock':{'state':'checked','relative_strength':f.get('relative_strength'),'conditions':item['conditions']},
            'setup':{'state':'experimental','strategies':matches},
            'entry':{'state':'pending','requirement':'完整五分钟信号、同源新报价与盘口'},
            'risk':{'state':'partial','risk_group':item['risk_group'],'missing':'公告与重大事件核验',
                    'execution_checks':['T+1','涨跌停','停牌','跳空','流动性','成本']}}
        item['rating']='数据不足以形成完整交易评级；仅作实验候选'
        return item

    async def scan(self, force=False):
        t=now(); day=str(local_date(t,'CN')); cutoff=str(daily_cutoff(t))
        self.previous_pool={r['symbol']:dict(r) for r in self.d.selection if r.get('market')=='CN'}
        self.expire_previous_day(t)
        self.status={'run_id':uuid4().hex,'date':day,'market':'CN','started_at':t.isoformat(),
                     'implementation':IMPLEMENTATION,
                     'state':'enumerating','checked':0,'missing':0,'supported':0,'universe_complete':False,
                     'cutoff':cutoff,'reason':'分页更新证券库','errors':[]}
        self.save_status()
        try:
            universe=await self.universe(day,force)
            rows=[]
            for raw in universe['rows']:
                s=raw['symbol']
                if not in_scope(s): continue
                name=raw.get('name') or s
                if any(x in name.upper() for x in ('ETF','ETN','退市','退整理')):continue
                rows.append({'symbol':s,'name':name,'industry':raw.get('industry'), 'market':'CN',
                             'market_cap':raw.get('marketcap'),'source':'longbridge','candidate_origin':'universe',
                             'scope':'沪深主板和创业板分页证券库','pool_role':'daily_core',
                             'candidate_date':self.d.candidate_trade_date('CN'),'candidate_updated_at':t.isoformat()})
            self.save_status(state='running',universe_complete=True,supported=len(rows),reason='逐股检查完整日线与策略条件')
            benchmark=await self.daily('000300.SH',t)
            self.d.benchmark_daily['CN']=benchmark
            engine=ResearchEngine(self.d.registry)
            evaluated=[]; missing=[]; good=[]
            # Cached names first yields useful output quickly; all names still receive one check.
            cache_symbols=set(self.store.daily_cache_symbols(cutoff))
            rows.sort(key=lambda r:(r['symbol'] not in cache_symbols,r['symbol']))
            for index,row in enumerate(rows):
                if not self.d.alive or not self.d.monitor_running: raise asyncio.CancelledError()
                if str(local_date(now(),'CN'))!=day or str(daily_cutoff(now()))!=cutoff:
                    self.pending=True
                    raise ValueError('扫描跨越交易日或收盘，保留历史缓存并按新时点重建')
                try:
                    bars=await self.daily(row['symbol'],t)
                    item=self.evaluate_daily(row,bars,benchmark,engine,t)
                    evaluated.append(item);good.append(item)
                    self.store.research_save('candidate',self.status['run_id']+':'+row['symbol'],
                        {**item,'date':day,'state':item['decision'],'cross_section_pending':True})
                    engine.context.pop(row['symbol'],None)
                except Exception as exc:
                    missing.append(row['symbol'])
                    old=self.evaluations.get(row['symbol'],{})
                    self.evaluations[row['symbol']]={**old,**row,'checked_session':day,'data_quality':'missing',
                        'setup_stage':'unavailable','setup_ready':False,'decision':old.get('decision','等确认'),
                        'score':old.get('score',0),'candidate_strategies':old.get('candidate_strategies',[]),
                        'reason':'本轮完整日线未能复核，等待恢复'}
                    self.store.research_save('candidate',self.status['run_id']+':'+row['symbol'],
                        {**row,'date':day,'run_id':self.status['run_id'],'state':'待恢复','reason':'日线过期、不足或口径未通过核验',
                         'error_type':type(exc).__name__,'checked_at':now().isoformat()})
                self.status.update(checked=len(evaluated),missing=len(missing),attempted=index+1)
                if (index+1)%50==0:
                    await self.publish(evaluated,partial=True)
                    self.save_status()
                await asyncio.sleep(.05)
            # Cross-sectional ranks use the entire successfully checked scope, not the former top 80.
            ranked=sorted(good,key=lambda item:item.get('relative_strength') if item.get('relative_strength') is not None else -999)
            for i,item in enumerate(ranked):
                rank=100*i/max(1,len(ranked)-1)
                cache=self.store.get('selection_daily:'+item['symbol'],{})
                bars=[Bar.load(b) for b in cache.get('bars',[])]
                engine.prepare(item['symbol'],bars,[],benchmark,{'percentile':rank,'coverage':len(ranked)})
                if self.d.registry.enabled('vcp_swing','CN') and engine._daily(item['symbol'],'vcp_swing').get('eligible'):
                    if 'vcp_swing' not in item['candidate_strategies']:item['candidate_strategies'].append('vcp_swing')
                    item['strategy_context']['vcp_swing']=engine._daily(item['symbol'],'vcp_swing')
                    count=int(self.d.registry.config('vcp_swing','CN')['final_contraction_days'])
                    item['structural_stops']['vcp_swing']=min(b.low for b in bars[-count:])
                    item['strategy_versions']['vcp_swing']=self.d.registry.current('vcp_swing','CN')['version']
                    if item['reason']=='未匹配已启用策略日线条件':
                        item.update(decision=item['daily_decision'],reason=item['daily_reason'])
                engine.context.pop(item['symbol'],None)
                if i%20==0:await asyncio.sleep(0)
                item.update(rs_percentile=rank,rs_rank_coverage=len(ranked))
                self.store.research_save('candidate',self.status['run_id']+':'+item['symbol'],
                    {**item,'date':day,'run_id':self.status['run_id'],'state':item['decision']})
            await self.publish(evaluated,partial=bool(missing),finished=True)
            incomplete=bool(missing or any(r.get('data_quality')=='missing' for r in self.d.selection if r.get('market')=='CN'))
            self.failed_at=now() if incomplete else None
            self.save_status(state='partial' if incomplete else 'complete',ended_at=now().isoformat(),
                             reason='部分证券缺日线、报价或身份核验；已检查范围内继续研究' if incomplete else '支持范围本轮复核完成',
                             missing_symbols=missing[:100],candidate_count=len(self.d.daily_core.get('CN',{}).get('rows',[])))
            self.report_if_due(now())
        except asyncio.CancelledError:
            self.save_status(state='interrupted',reason='扫描中断，已保存逐股缓存，恢复后继续');raise
        except Exception as exc:
            self.failed_at=now()
            if self.d.daily_core.get('CN'):
                self.d.daily_core['CN'].update(stale=True,error='本轮证券库或基准数据未完成')
                self.store.set('daily_candidate_core',self.d.daily_core)
            for row in self.d.selection:
                if row.get('market')!='CN':continue
                current=self.evaluations.get(row['symbol'],{})
                independently_checked=(current.get('checked_session')==day and current.get('discovery_round_id')
                    and current.get('data_quality')=='complete' and current.get('quote_as_of')
                    and 0<=(now()-stamp(current['quote_as_of'])).total_seconds()<=300)
                if not independently_checked:row.update(data_quality='missing',reason='本轮范围扫描失败，待恢复')
            self.d.allocate_monitoring()
            self.save_status(state='error',ended_at=now().isoformat(),reason=str(exc) if isinstance(exc,ValueError) else '证券库或基准数据请求失败',errors=[type(exc).__name__])
            self.d.alerts.emit('research-error:'+day,'A股研究数据待恢复',self.status['reason'],'data')
        finally:
            self.d.broadcast()

    @staticmethod
    def apply_price(row,q):
        row.update(observed_price=q.price,quote_as_of=q.market_time.isoformat(),intraday_checked_at=now().isoformat())
        stops=row.get('structural_stops',{})
        strategies=row.get('candidate_strategies',[])
        rejected={s:{'stop':stops.get(s,row['structure_low']),'price':q.price,'checked_at':now().isoformat(),
                     'reason':'当前价格跌破该策略结构失效位'}
                  for s in strategies if q.price<float(stops.get(s,row['structure_low']))}
        row['strategy_invalidations']={**row.get('strategy_invalidations',{}),**rejected}
        row['candidate_strategies']=[s for s in strategies if s not in rejected]
        if q.trade_status!='Normal' or (strategies and not row['candidate_strategies']):
            row.update(decision='暂不参与',reason='交易状态异常或跌破结构失效位',setup_stage='invalid',setup_ready=False);return
        if row['decision']=='暂不参与':return
        distance=(q.price/row['breakout_reference']-1)*100
        old_near=bool(row.get('conditions',{}).get('near_high'))
        row['score']=max(0,min(100,row.get('daily_score',row['score'])+15*(int(-3<=distance<=3)-int(old_near))))
        row['intraday_distance_to_high_pct']=distance
        if q.price>row['close']+3*(row.get('atr20') or 0):
            row.update(decision='等确认',reason='盘中偏离日线结构过远，不追价')
        else:
            row.update(decision='重点观察' if row['score']>=70 else '等确认',
                       reason='策略日线条件有效，当前价格已复核，等待完整五分钟Setup')

    async def publish(self, evaluated, partial=False, finished=False, source='history'):
        async with self.publish_lock:
            previous={r['symbol']:r for r in self.d.selection if r.get('market')=='CN'}
            # A partial historical batch is an update to the full index, never a
            # declaration that all not-yet-visited names have failed.
            for symbol,row in previous.items():self.evaluations.setdefault(symbol,dict(row))
            self.discovery.merge_daily(evaluated)
            day=str(local_date(now(),'CN'))
            round_id=(self.discovery.status.get('round_id') if source!='history' else self.status.get('run_id')) or day
            for row in self.evaluations.values():
                if row.get('checked_session')!=day:
                    row.update(data_quality='missing',setup_ready=False,setup_stage='unavailable')
                elif (is_open(now(),'CN') and row.get('setup_checked_at') and
                      (now()-stamp(row['setup_checked_at'])).total_seconds()>360):
                    row.update(setup_ready=False,setup_stage='watch',setup_reason='本轮完整分钟条件待复核')
                row['pool_review_pending']=bool(partial and
                    (row.get('run_id')!=round_id if source=='history' else row.get('discovery_round_id')!=round_id))
            def eligible():
                return sorted([r for r in self.evaluations.values()
                               if r.get('decision')!='暂不参与' and r.get('candidate_strategies')],
                              key=lambda r:(r.get('data_quality')=='missing',ranking_key(r)))[:80]
            valid=eligible()
            needed=[r['symbol'] for r in valid if not r.get('name_verified')]
            try:refs=await self.d.lb.reference_info(needed) if needed else {}
            except Exception:refs={}
            for symbol in needed:
                row=self.evaluations.get(symbol)
                if row is None:continue
                ref=refs.get(symbol)
                row['name_verified']=bool(ref)
                if ref:
                    row['name']=ref['name'];row['name_verified_session']=day
                    if ref.get('total_shares') and row.get('close'):row['market_cap']=ref['total_shares']*row['close']
                else:row.update(data_quality='missing',reason='证券身份和风险标记待核验')
            valid=eligible();selected={r['symbol'] for r in valid}
            for row in valid:
                before=previous.get(row['symbol'])
                if not before and source!='history' and row.get('discovery_round_id')==self.discovery.status.get('round_id'):
                    self.discovery.round_changes['new_entries'].add(row['symbol'])
                state='待恢复' if row.get('data_quality')=='missing' else '继续跟踪' if before else '新入选'
                changed=not before or (before.get('decision'),before.get('setup_stage'),before.get('data_quality'))!=(row.get('decision'),row.get('setup_stage'),row.get('data_quality'))
                if changed or finished:
                    self.store.research_save('change',round_id+':pool:'+row['symbol'],
                        {**row,'date':day,'state':state,'time':now().isoformat(),'pool_layer':'research',
                         'reason':row.get('setup_reason') or row.get('reason','本轮策略复核通过')})
            for symbol,old in previous.items():
                current=self.evaluations.get(symbol,{})
                if symbol in selected:continue
                invalid=current.get('decision')=='暂不参与'
                if invalid:self.d.sim.cancel_pending(symbol,current.get('reason','当前资格失效'))
                state='已失效' if invalid else '待恢复' if current.get('data_quality')=='missing' else '被替换'
                if state=='被替换' and source!='history':self.discovery.round_changes['replacements'].add(symbol)
                self.store.research_save('change',round_id+':pool:'+symbol,
                    {**old,'date':day,'state':state,'time':now().isoformat(),'pool_layer':'research',
                     'reason':current.get('reason','当前资格失效') if invalid else
                              '数据待恢复，保留全范围复核记录' if state=='待恢复' else '全范围最新Setup及策略排名出现更优候选'})
            self.d.selection=[r for r in self.d.selection if r.get('market')!='CN']+[dict(r) for r in valid]
            self.d.candidates=[r for r in self.d.candidates if r.get('market')!='CN']+[dict(r) for r in valid]
            self.d.daily_core['CN']={'trade_date':self.d.candidate_trade_date('CN'),'updated_at':now().isoformat(),
                                     'rows':[dict(r) for r in valid],'stale':partial,'errors':[], 'run_id':round_id}
            for k,v in [('selection',self.d.selection),('candidates',self.d.candidates),('daily_candidate_core',self.d.daily_core),('selection_updated',now().isoformat())]:self.store.set(k,v)
            if finished:self.discovery.persist()
            self.d.allocate_monitoring();self.d.broadcast()

    async def refresh_pool(self):
        # Kept as the public scheduler entrypoint for compatibility. Discovery
        # covers the full supported universe, not just the displayed 80 names.
        await self.discovery.run()

    def context_summary(self):
        gates=getattr(self.d,'context_gates',{})
        layers={}
        reasons=Counter()
        for row in self.d.selection:
            if row.get('market')!='CN':continue
            gate=gates.get(row['symbol'],{})
            for check in gate.get('checks',[]):
                detail=layers.setdefault(check['layer'],{'checked':0,'blocked':0,'missing':0})
                detail['checked']+=1;detail['blocked']+=int(not check.get('passed'))
                detail['missing']+=int(check.get('data_complete') is False)
                if not check.get('passed'):reasons[check.get('reason','条件尚未通过')]+=1
        return {'layers':layers,'blocked_reasons':[{'reason':k,'candidates':v} for k,v in reasons.most_common()]}

    def diagnostics(self):
        current=[r for r in self.d.selection if r.get('market')=='CN']
        protected=[s for s in self.d.protected_monitoring() if symbol_market(s)=='CN']
        funnel=self.store.research_funnel('CN',str(local_date(now(),'CN')))
        context=self.context_summary()
        return {**self.status,'funnel':funnel,'running':bool(self.task and not self.task.done()),'coalesced':self.pending,
                'discovery':self.discovery.diagnostics(),
                'scope':'沪深主板、创业板；不含科创板、北交所','research_pool':len(current),'pool_data_missing':sum(r.get('data_quality')=='missing' for r in current),
                'io_metrics':dict(self.d.lb.io_metrics),
                'protected_slots':len(protected),'protected_overflow':max(0,len(protected)-min(12,self.d.settings['monitor_limit'])),
                'monitor_limit':min(12,self.d.settings['monitor_limit']),
                'changes':self.store.research_list('change',market='CN',limit=12),
                'reports':self.store.research_list('report',market='CN',limit=4),
                'context':context,
                'chain':{'市场':'指数趋势、VWAP及全范围上涨占比；已核验'+str(context['layers'].get('市场',{}).get('checked',0))+'只，'+str(context['layers'].get('市场',{}).get('blocked',0))+'只未通过',
                         '板块':'行业3/5日相对强度及覆盖率；已核验'+str(context['layers'].get('板块',{}).get('checked',0))+'只，'+str(context['layers'].get('板块',{}).get('blocked',0))+'只未通过',
                         '个股':'同源日线、流动性与相对强弱','Setup':'现有版本化实验策略',
                         '买点':'完整五分钟与实时报价、盘口检查','风险':'公告目录规则筛查、T+1、涨跌停及成本；公告全文重大事项仍待核验'},
                'missing_document_checks':['完整市场情绪状态机','公告全文及重大事项实质核验']+
                    [k+'数据覆盖不足' for k,v in context['layers'].items() if v['missing']]+
                    ([] if context['layers'] else ['市场、行业与公告目录核验尚未完成']),
                'runtime_validation':self.runtime_validation(),'score_label':'日线准备度，非交易评级或胜率'}

    def report_if_due(self,t):
        day=local_date(t,'CN');cal=calendar('CN')
        if not cal.is_session(str(day)):return
        clock=t.astimezone(ZoneInfo('Asia/Shanghai'))
        if (clock.hour,clock.minute)<(9,20):return
        closing=t>=close_time(day,'CN')+timedelta(minutes=10)
        kind='close' if closing else 'premarket'
        key=f'CN:{day}:{kind}'
        old=self.store.research_get('report',key)
        complete=self.status.get('date')==str(day) and self.status.get('state')=='complete'
        if old and old.get('implementation')==IMPLEMENTATION and (kind=='close' or old.get('scan_complete') or not complete):return
        summary=self.store.research_funnel('CN',str(day))
        paper=self.d.sim.state['accounts']['CN']
        entries=[r for r in list(paper['positions'].values())+paper['trades'] if local_date(stamp(r['entry_time']),'CN')==day]
        summary['simulated_fills']=len({r['id'] for r in entries})
        summary['failed_fills']=sum(local_date(stamp(r['time']),'CN')==day and symbol_market(r['symbol'])=='CN' for r in self.d.sim.state['failures'])
        trades=[r for r in self.d.sim.state['accounts']['CN']['trades'] if local_date(stamp(r['exit_time']),'CN')==day]
        picks=[r for r in self.d.selection if r.get('market')=='CN' and r.get('decision')!='暂不参与'][:10]
        plans=[r for r in self.d.plans.list('CN',True) if r.get('date')==str(day)] if hasattr(self.d,'plans') else []
        report={'market':'CN','date':str(day),'kind':kind,'generated_at':t.isoformat(),'scan_complete':complete,
                'implementation':IMPLEMENTATION,'discovery':self.discovery.diagnostics(),
                'plans':{'count':len(plans),'states':dict(Counter(r.get('state') for r in plans))},
                'context':self.context_summary(),
                'coverage':{k:self.status.get(k) for k in ('run_id','state','supported','checked','missing','cutoff')},
                'funnel':summary,'closed_trades':len(trades),'net_pnl':sum(r.get('net_pnl',0) for r in trades),
                'candidates':[{k:r.get(k) for k in ('symbol','name','decision','reason','as_of','candidate_strategies')} for r in picks],
                'next_action':'下一交易日重新核验候选与缺失数据' if closing else '盘中每5分钟全范围发现，每30分钟复核完整资格；独立队列验证池内外分钟Setup',
                'message':'无成交时按信号、执行阻断及数据缺口解释；观察涨幅不计收益'}
        self.store.research_save('report',key,report)
        # Starting after the schedule leaves a report, but not a misleading old actionable notification.
        timely=(clock.hour,clock.minute)<(9,30) if kind=='premarket' else t<close_time(day,'CN')+timedelta(minutes=30)
        if timely:self.d.alerts.emit(key+(':complete' if complete else ':progress'),'A股收盘日报' if closing else 'A股盘前计划',
            f"已检查 {self.status.get('checked',0)} 只，缺数据 {self.status.get('missing',0)} 只；详情见今日研究。",'research')

    def comparison(self,market='CN'):
        signals=self.store.all_signals(market)
        portfolio=self.d.sim.state['accounts'][market]
        fills=list(portfolio['positions'].values())+portfolio['trades']
        def structure(row):return row.get('evidence',{}).get('structure_id') or row.get('structure_id') or 'legacy:'+row['id']
        def version_summary(strategy,version):
            detail=self.d.strategy_performance_detail(market,strategy,version)
            raw=[r for r in signals if r['strategy']==strategy and r.get('version')==version]
            structures={structure(r) for r in raw}
            filled={structure(r) for r in fills if r.get('strategy')==strategy and r.get('version')==version}
            return {'performance':detail,'version':version,'signal_count':len(structures),'raw_signal_count':len(raw),
                    'filled_count':len(filled),'filled_without_signal_count':len(filled-structures),
                    'signal_to_fill_rate':len(filled & structures)/len(structures) if structures else None,
                    'correlated_sample':{'warning':'同一结构的重复确认已去重；同股、同日及同板块样本仍相关，不能视为独立胜率证据',
                                         'reconfirmations':len(raw)-len(structures),
                                         'legacy_without_structure':sum(not r.get('evidence',{}).get('structure_id') for r in raw)}}
        result=[]
        for k,definition in DEFINITIONS.items():
            if market not in definition['markets']:continue
            version=self.d.registry.current(k,market)['version']
            current=version_summary(k,version)
            versions=[]
            for revision in self.d.registry.revisions[market][k]:
                summary=version_summary(k,revision['version'])
                versions.append({**summary.pop('performance'),**summary})
            result.append({'strategy':k,'name':definition['name'],'horizon':definition['horizon'],**current,
                           'versions':versions})
        first=self.d.registry.current('first_pullback','CN') if 'first_pullback' in DEFINITIONS else {}
        discovery=self.discovery.diagnostics()
        return {'market':market,'strategies':result,'coverage':{k:self.status.get(k) for k in ('checked','supported','missing')},
                'minute_coverage':discovery['signal_coverage'] if market=='CN' else None,
                'execution_coverage':discovery['execution_coverage'] if market=='CN' else None,
                'coverage_method':'分钟覆盖为六分钟内完成对应策略检查的股票；执行覆盖为当前监测中具备30秒报价和15秒盘口的股票，均不等于可买数量',
                'research_cards':[
                    {'setup':'S04 强势缩量首次回踩','strategy':'first_pullback','version':first.get('version'),
                     'state':'试运行，尚未验证胜率' if first.get('enabled') else '试运行规则已停用',
                     'environment':'指数趋势、VWAP及上涨占比通过，行业3/5日相对强度居前',
                     'entry':'日线突破后首次1至5日缩量回踩，完整分钟K线转强；仍须公告目录与成本后执行检查',
                     'exit':'按本次结构止损、分批目标和持有期退出；A股遵守T+1',
                     'missing':['公告全文重大事项实质核验','充分的本地样本外及前向成交证据']},
                    {'setup':'S05 急跌后右侧修复','state':'研究假设，未激活','environment':'冰点修复或震荡、指数与板块先企稳',
                     'entry':'停止创新低后收复VWAP及反转结构；不得仅凭超卖入场','exit':'再创新低失效，按T+1可执行性处置',
                     'missing':['完整市场状态历史','致命利空核验','本地样本外结果']}],
                'method':'按策略版本与结构去重，原始信号另列；短线与波段、历史探索与前向模拟分开。样本数量及高胜率均不单独证明盈利能力。'}

    def runtime_validation(self):
        days=self.store.research_list('runtime',market='CN',limit=30)
        verified=[];full=[]
        for r in days:
            if r.get('implementation')!=IMPLEMENTATION:continue
            reports=self.store.research_list('report',market='CN',day=r['date'],limit=5)
            runs=self.store.research_list('run',market='CN',day=r['date'],limit=100)
            discoveries=self.store.research_list('discovery',market='CN',day=r['date'],limit=100)
            has_reports={'premarket','close'}.issubset({p.get('kind') for p in reports if p.get('implementation')==IMPLEMENTATION})
            has_scan=any(p.get('implementation')==IMPLEMENTATION and p.get('universe_complete') and p.get('checked',0)>0 and p.get('ended_at') for p in runs)
            has_discovery=any(p.get('implementation')==IMPLEMENTATION and p.get('universe_complete') and p.get('supported',0)>0
                              and p.get('quote_checked')==p.get('supported') and not p.get('quote_missing') and p.get('ended_at') for p in discoveries)
            if r.get('premarket_seen') and r.get('close_seen') and len(r.get('open_minutes',[]))>=240 and not r.get('gap') and has_reports and has_scan and has_discovery:
                verified.append(r['date'])
                full_history=any(p.get('implementation')==IMPLEMENTATION and p.get('state')=='complete' and p.get('supported',0)>0
                                 and p.get('checked')==p.get('supported') and not p.get('missing') for p in runs)
                full_discovery=any(p.get('implementation')==IMPLEMENTATION and p.get('state')=='complete' and p.get('supported',0)>0
                                   and p.get('daily_checked')==p.get('supported') and not p.get('daily_missing') for p in discoveries)
                if full_history and full_discovery:full.append(r['date'])
        verified=sorted(verified)
        consecutive=any(str(calendar('CN').next_session(a).date())==b for a,b in zip(verified,verified[1:]))
        full=sorted(full)
        full_consecutive=any(str(calendar('CN').next_session(a).date())==b for a,b in zip(full,full[1:]))
        return {'implementation':IMPLEMENTATION,'required_full_days':2,'verified_days':verified,'state':'verified' if consecutive else 'collecting',
                'operational_two_days':consecutive,
                'full_coverage':{'state':'complete' if full_consecutive else 'incomplete','days':full,
                                 'meaning':'支持范围完整日线资格与报价覆盖；分钟验证及执行覆盖另外列示，不代表策略盈利或完整金融验证'},
                'message':'实机运行记录仍在积累；自动测试使用模拟时钟，不算实机验收' if not consecutive else '已有连续两个完整交易日运行及报告记录；数据覆盖与策略有效性分别检验'}

    def record_runtime(self,t):
        day=str(local_date(t,'CN'));local=t.astimezone(ZoneInfo('Asia/Shanghai'));minute=local.hour*60+local.minute
        if self.last_runtime_minute==(day,minute):return False
        self.last_runtime_minute=(day,minute)
        row=self.store.research_get('runtime',day) or {'market':'CN','date':day,'open_minutes':[],'gap':False}
        if row.get('implementation')!=IMPLEMENTATION:
            row={'market':'CN','date':day,'open_minutes':[],'gap':False,'implementation':IMPLEMENTATION}
        if row.get('last_seen') and is_open(t,'CN'):
            prev=stamp(row['last_seen'])
            if is_open(prev,'CN') and (t-prev).total_seconds()>120:row['gap']=True
        if 550<=minute<=560:row['premarket_seen']=True
        if minute>=910:row['close_seen']=True
        if is_open(t,'CN') and minute not in row['open_minutes']:row['open_minutes'].append(minute)
        row['last_seen']=t.isoformat();self.store.research_save('runtime',day,row)
        return True

    async def tick(self,t=None):
        t=t or now();day=local_date(t,'CN');cal=calendar('CN')
        if not self.d.monitor_running:return
        session=cal.is_session(str(day))
        local=t.astimezone(ZoneInfo('Asia/Shanghai'));minute=local.hour*60+local.minute
        recovering=self.status.get('state') in ('interrupted','error')
        if (not session or minute<550 or minute>=1200) and recovering:
            if (not self.task or self.task.done()) and (not self.failed_at or (t-self.failed_at).total_seconds()>=300):
                self.failed_at=t;self.request()
        if not session:return
        new_minute=self.record_runtime(t)
        notified='runtime_verified_notification:'+IMPLEMENTATION
        if new_minute and not self.store.get(notified,False):
            validation=self.runtime_validation()
            if validation['operational_two_days']:
                coverage='完整日线与报价覆盖也已通过' if validation['full_coverage']['state']=='complete' else '完整数据覆盖仍待补齐'
                self.d.alerts.emit(notified,'A股自动研究已连续运行两个完整交易日',
                    coverage+'；运行验收不代表策略胜率或收益已获验证。','research')
                self.store.set(notified,True)
        if minute>=550:self.expire_previous_day(t)
        slot=f'{day}:pre' if 550<=minute<570 else f'{day}:{minute//30}' if is_open(t,'CN') else f'{day}:close' if 905<=minute<1200 else None
        self.status['schedule_delayed']=bool(slot and slot!=self.last_slot and self.task and not self.task.done())
        retry=self.status.get('state') in ('error','partial','interrupted') and (not self.failed_at or (t-self.failed_at).total_seconds()>=300)
        if slot and (slot!=self.last_slot or retry or self.pending) and (not self.task or self.task.done()):
            force=self.pending
            self.last_slot=slot;self.pending=False;self.failed_at=t;self.request(force=force)
        pool_slot=f'{day}:{minute//5}'
        if is_open(t,'CN') and pool_slot!=self.last_pool_slot and (not self.pool_task or self.pool_task.done()):
            self.last_pool_slot=pool_slot;self.pool_task=asyncio.create_task(self.refresh_pool());self.d.tasks.append(self.pool_task)
        if is_open(t,'CN'):self.discovery.start_validation()
        self.report_if_due(t)

    async def loop(self):
        while self.d.alive:
            try:await self.tick()
            except asyncio.CancelledError:raise
            except Exception as exc:self.store.event('research',{'message':'自动研究调度等待恢复','error_type':type(exc).__name__})
            await asyncio.sleep(5)
