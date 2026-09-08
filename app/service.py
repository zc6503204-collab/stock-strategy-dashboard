import asyncio,json,time,re,statistics
from pathlib import Path
from datetime import timedelta
from .models import now,stamp,symbol_market,in_scope,Bar,Signal
from .providers import ROOT,Lingxi,Longbridge,IBKR,number,canonical
from .calendars import is_open,local_date,adjacent,price_limits,phase
from .strategy import Strategies
from .research import ResearchEngine,VERSION,BENCHMARKS,daily_factors
from .decisions import recommend,holding_plan,fresh_quote
from .alerts import Alerts
from .calendars import calendar,monitoring_window,close_time
from .simulation import Simulation
from .store import Store
from .selection import evaluate as evaluate_selection,candidate_pool
from .holdings import RealHoldings,real_holding_plan
from .strategy_registry import StrategyRegistry,DEFINITIONS

def _atr_pct(bars):
    if len(bars)<21 or not bars[-1].close:return 999.
    recent=bars[-20:]
    value=sum(max(b.high-b.low,abs(b.high-bars[-21+i].close),abs(b.low-bars[-21+i].close)) for i,b in enumerate(recent))/20
    return value/bars[-1].close*100

class Dashboard:
    def __init__(self,root=ROOT):
        self.root=root;self.store=Store(root/'.local/dashboard.sqlite3')
        self.lingxi=Lingxi();self.lb=Longbridge();self.ib=IBKR()
        self.providers={'lingxi':self.lingxi,'longbridge':self.lb,'ibkr':self.ib}
        self.registry=StrategyRegistry(self.store);self.strategies=ResearchEngine(self.registry);self.sim=Simulation(self.store)
        self.real=RealHoldings(self.store)
        self.alerts=Alerts(self.store,root);self.books={};self.decisions=[];self.exit_plans=[];self.real_plans=[]
        self.research_ready={};self.research_progress={};self.benchmark_daily={};self.last_live=0.;self.last_research=0.
        self.started_at=time.monotonic()
        self.monitor_running=self.store.get("monitor_running",True);self.runtime_ok=None
        self.manual_watch=self.store.get("manual_watch",[]);self.live_task=None;self.access_task=None;self.access_pending=False;self.ib_connect_task=None
        self.candidates=self.store.get('candidates',[]);self.quotes={};self.comparisons={};self.details={}
        self.settings=self.store.get('settings',{'monitor_limit':12,'simulation_enabled':True,'ib_port':7497})
        self.watch=self.store.get('watch',[]);self.scanning=False;self.last_scan=self.store.get('last_scan')
        self.market_stats=self.store.get('market_stats',{});self.market_stats_time=self.store.get('market_stats_time')
        self.tasks=[];self.alive=True;self.listeners=set();self.fetch_lock=asyncio.Lock();self.last_cycle=time.monotonic()
        self.last_bars_cycle=0.;self.last_quote_cycle=0.;self.last_scan_cycle=0.;self.validation={};self.first_poll=True
        self.lb.on_quote=self.accept_quote;self.lb.on_bar=self.accept_bar
        self.sdk_checked=None
        self.ib_auto_connect=self.store.get('ib_auto_connect',False);self.last_ib_attempt=0.
        self.selection=self.store.get('selection',[]);self.selection_running=False
        self.selection_progress={'done':0,'total':0};self.selection_errors=[];self.selection_task=None
        self.holding_sync=self.store.get('holding_sync',{
            'longbridge':{'state':'waiting','message':'等待首次同步'},
            'ibkr':{'state':'waiting','message':'等待本地接口连接'}})
        self.last_holdings_sync=0.;self.holdings_task=None
        # Static security reference prevents leveraged ETFs and similarly named products entering a stock-only pool.
        p=root/'.local/vendor/gtht/lingxi-realtimemarketdata-skill/stock_code_name.json'
        try:self.security_map={canonical(r['code']):r for r in json.loads(p.read_text())['items']}
        except (FileNotFoundError,KeyError):self.security_map={}

    def broadcast(self):
        for event in self.listeners:event.set()

    async def start(self):
        import os
        (self.root/'.local/server.pid').write_text(str(os.getpid()))
        await self.lb.connect_cached()
        if self.store.get('engine_version')!=VERSION:
            for sid,raw in list(self.sim.state['pending'].items()):
                self.sim.fail(raw['symbol'],'升级新版，取消旧待买信号',now());del self.sim.state['pending'][sid]
            self.sim.save();self.store.set('engine_version',VERSION)
        self.tasks=[asyncio.create_task(self.run()),asyncio.create_task(self.live_loop()),
                    asyncio.create_task(self.research_loop()),asyncio.create_task(self.holdings_loop()),
                    asyncio.create_task(self.alerts.worker())]

    async def stop(self):
        self.alive=False
        authorization=getattr(self.lb,'authorization_task',None)
        if authorization:
            authorization.cancel()
            await asyncio.gather(authorization,return_exceptions=True)
        for t in self.tasks:t.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        if self.ib.ib:self.ib.ib.disconnect()

    def tracked(self):
        held=[s for a in self.sim.state['accounts'].values() for s in a['positions']]
        return list(dict.fromkeys(self.real.symbols()+held+self.watch))[:self.settings['monitor_limit']]

    async def run(self):
        while self.alive:
            try:
                if not self.monitor_running:
                    await asyncio.sleep(1);continue
                tick=time.monotonic()
                if tick-self.last_cycle>90:
                    for s in self.tracked():self.suspend(s,'运行中断／休眠，重新补齐数据')
                    self.first_poll=True
                self.last_cycle=tick
                active=any(is_open(now(),m) for m in ['CN','US'])
                quote_window=active or phase(now(),'CN')=='集合竞价'
                preparing=[m for m in ['CN','US'] if monitoring_window(now(),m)]
                if self.ib_auto_connect and tick-self.last_ib_attempt>30 and (not self.ib.ib or not self.ib.ib.isConnected()):
                    self.last_ib_attempt=tick
                    if not self.ib_connect_task or self.ib_connect_task.done():
                        self.ib_connect_task=asyncio.create_task(self.connect_ib_available())
                        self.tasks.append(self.ib_connect_task)
                if self.lb.ctx and self.sdk_checked is not self.lb.ctx:
                    self.sdk_checked=self.lb.ctx
                    self.access_pending=True;self.first_poll=True
                if self.first_poll:
                    if not self.scanning:
                        task=asyncio.create_task(self.scan());self.tasks.append(task)
                    self.last_scan_cycle=time.monotonic()
                elif active and tick-self.last_scan_cycle>300:
                    await self.scan();self.last_scan_cycle=time.monotonic()
                elif preparing:
                    marker=self.store.get('premarket_scan_marker',{})
                    due=[m for m in preparing if marker.get(m)!=str(local_date(now(),m))]
                    if due and not self.scanning:
                        marker.update({m:str(local_date(now(),m)) for m in due})
                        self.store.set('premarket_scan_marker',marker)
                        task=asyncio.create_task(self.scan());self.tasks.append(task)
                if self.first_poll or any(s not in self.details for s in self.tracked()) or (active and tick-self.last_bars_cycle>60):
                    await self.refresh_bars();self.last_bars_cycle=time.monotonic()
                    if self.lb.ctx:
                        ok=await self.lb.healthcheck()
                        if not ok:
                            for s in self.tracked():self.suspend(s,'长桥连接失联，等待补齐行情')
                    if self.access_pending and (not self.access_task or self.access_task.done()):
                        self.access_pending=False
                        self.access_task=asyncio.create_task(self.lb.verify_access())
                        self.tasks.append(self.access_task)
                self.first_poll=False;self.runtime_ok=now().isoformat();self.store.set('service_heartbeat',self.runtime_ok);self.broadcast()
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.store.event('system',{'message':'数据刷新未完成，已暂停本轮新信号','error_type':type(exc).__name__})
            self.last_cycle=time.monotonic()
            await asyncio.sleep(5)

    async def connect_ib_available(self):
        for port in dict.fromkeys([self.settings['ib_port'],4001,4002,7496,7497]):
            try:
                _,writer=await asyncio.wait_for(asyncio.open_connection('127.0.0.1',port),.5)
                writer.close();await writer.wait_closed()
            except (OSError,asyncio.TimeoutError):continue
            await self.ib.connect(port)
            if self.ib.ib and self.ib.ib.isConnected():
                self.settings['ib_port']=port;self.store.set('settings',self.settings)
                quotes=await self.ib.quotes(list(dict.fromkeys(['AAPL.US']+[s for s in self.tracked() if s.endswith('.US')]))[:8])
                for q in quotes:self.accept_quote(q)
                self.ib.status['verified_symbols']=sorted(self.ib.verified)
                await self.sync_real_holdings(['ibkr'])
                self.first_poll=True;self.broadcast();return True
        self.ib.fail('等待IB Gateway／TWS登录并开启只读API；每30秒自动重试本机端口')
        self.broadcast();return False

    @staticmethod
    def is_st(symbol,name):
        return symbol_market(symbol)=='CN' and bool(re.match(r'^\*?ST',name.upper().strip()))

    def allowed_security(self,row):
        symbol=row['symbol']
        if not in_scope(symbol):return False
        name=row.get('name','').upper()
        if any(term in name for term in ['ETF','ETN','WARRANT','ACQUISITION','认股权','权证','C/WTS','WTS ','UNITS','退市','退整理']):return False
        code=symbol.split('.')[0]
        if symbol.endswith('.US') and len(code)>=5 and re.search(r'(W[A-Z]?|R|U)$',code):return False
        if symbol.endswith('.US'):return True
        ref=self.security_map.get(symbol)
        return bool(ref and str(ref.get('证券类型'))=='1')

    async def scan(self):
        if self.scanning:return
        self.scanning=True;self.broadcast()
        try:
            rows=[]
            # One serialized request per provider, bounded candidate universe, no full-market claim.
            for order in [10,2]:
                found,stats=await self.lingxi.rank(order)
                rows.extend(found)
                if stats:self.market_stats=stats
                if found:self.market_stats_time=found[0].get('market_time')
            try:
                us=await self.lb.cli_scan('US')
                for r in us.get('items',[]):
                    rows.append({'symbol':r['symbol'],'name':r.get('name',r['symbol']),'price':number(r.get('prevclose')),
                     'change_pct':number(r.get('prevchg')),'market_cap':number(r.get('marketcap')),'source':'longbridge',
                     'reason':'美股涨幅候选 · 待流动性核验','market_time':None,'scope':'长桥美股筛选返回前40只'})
            except Exception:
                if not self.lb.ctx:self.lb.status.update(message='持续行情待授权；美股筛选接口本次未返回')
            unique={}
            for r in rows:
                if not self.allowed_security(r):continue
                group='st' if self.is_st(r['symbol'],r['name']) else 'pending'
                r['risk_group']=group;r['market']=symbol_market(r['symbol'])
                r['eligible']=False;r['eligibility_reason']='等待日线、流动性与交易状态核验'
                if r['symbol'] not in unique:unique[r['symbol']]=r
            if unique:
                self.candidates=list(unique.values())
                # Stable monitoring slots: do not silently evict a user-selected stock on rank changes.
                old_watch=list(self.watch)
                self.watch=[s for s in self.watch if self.allowed_security({'symbol':s,'name':self.security_map.get(s,{}).get('name','')})]
                if not self.watch:
                    cn=[r['symbol'] for r in self.candidates if r['market']=='CN'][:6]
                    us=[r['symbol'] for r in self.candidates if r['market']=='US'][:6]
                    self.watch=(cn+us)[:self.settings['monitor_limit']]
                    self.store.set('watch',self.watch)
                elif old_watch!=self.watch:
                    missing=[r['symbol'] for r in self.candidates if r['market']=='US' and r['symbol'] not in self.watch]
                    self.watch=(self.watch+missing)[:self.settings['monitor_limit']]
                    self.store.set('watch',self.watch)
                self.last_scan=now().isoformat()
                self.store.set('candidates',self.candidates);self.store.set('last_scan',self.last_scan)
                self.store.set('market_stats',self.market_stats);self.store.set('market_stats_time',self.market_stats_time)
        finally:self.scanning=False;self.broadcast()
        self.schedule_selection()

    def schedule_selection(self):
        if self.selection_task and not self.selection_task.done():return
        self.selection_task=asyncio.create_task(self.refresh_selection())
        self.tasks.append(self.selection_task)

    async def refresh_selection(self):
        self.selection_running=True;self.selection_errors=[]
        today=str(local_date(now(),'CN'))
        saved=self.store.get('selection_pool',{})
        retained=saved.get('rows',[]) if saved.get('date')==today else []
        # Auction ranking resets must not silently discard the morning's researched candidates.
        preferred={r['symbol'] for r in self.selection if r['decision']=='重点观察'} | set(self.tracked())
        rows=candidate_pool([r for r in self.candidates if r.get('market')=='CN'],[r for r in retained if r.get('market')=='CN'],preferred)
        rows += [r for r in self.candidates if r.get('market')=='US'][:40]
        self.store.set('selection_pool',{'date':today,'rows':rows})
        existing={r['symbol'] for r in self.candidates}
        self.candidates.extend(r for r in rows if r['symbol'] not in existing)
        self.selection_progress={'done':0,'total':len(rows)};self.broadcast()
        previous={r['symbol']:r for r in self.selection}
        target={r['symbol'] for r in rows};processed=set();results=[]
        try:
            try:
                references=await self.lb.reference_info([r['symbol'] for r in rows])
            except Exception:references={}
            for row in rows:
                try:
                    reference=references.get(row['symbol'])
                    if reference:
                        row['name']=reference['name']
                        if row.get('price') and reference.get('total_shares'):row['market_cap']=row['price']*reference['total_shares']
                    elif row.get('name')==row['symbol']:
                        row['name']=self.security_map.get(row['symbol'],{}).get('name',row['symbol'])
                    if not self.allowed_security(row):raise ValueError('不在选股范围')
                    key='selection_daily:'+row['symbol']
                    cached=self.store.get(key)
                    if cached and cached.get('fetched_date')==str(local_date(now(),symbol_market(row['symbol']))) and len(cached.get('bars',[]))>=125:
                        daily=[Bar.load(b) for b in cached['bars']]
                    else:
                        daily=await self.lb.bars(row['symbol'],'day',130)
                        daily=[b for b in daily if local_date(b.start,symbol_market(row['symbol']))<local_date(now(),symbol_market(row['symbol']))]
                        self.store.set(key,{'fetched_date':str(local_date(now(),symbol_market(row['symbol']))),'bars':[b.dump() for b in daily]})
                    if reference and reference.get('total_shares') and daily:row['market_cap']=daily[-1].close*reference['total_shares']
                    item=evaluate_selection(row,daily)
                    f=daily_factors(row['symbol'],daily,self.benchmark_daily.get(symbol_market(row['symbol']),[]))
                    item.update(relative_strength=f.get('relative_strength'),research_reason=f['reason'])
                    if not f.get('eligible') and item['decision']=='重点观察':item.update(decision='等确认',reason=f['reason'])
                    item['name_verified']=bool(reference)
                    if not reference and item['decision']=='重点观察':
                        item['decision']='等确认';item['reason']='证券名称和当前风险标记待核验，暂不进入重点监测'
                    results.append(item)
                except Exception:
                    self.selection_errors.append({'symbol':row['symbol'],'reason':'完整日线或成交量口径未通过核验'})
                processed.add(row['symbol'])
                self.selection_progress['done']+=1
                visible=results+[r for symbol,r in previous.items() if symbol in target and symbol not in processed]
                self.selection=sorted(visible,key=lambda r:({'重点观察':0,'等确认':1,'暂不参与':2}[r['decision']],-r['score'],abs(r['distance_to_high_pct'])))
                self.broadcast()
                await asyncio.sleep(.1)
            self.selection=sorted(results,key=lambda r:({'重点观察':0,'等确认':1,'暂不参与':2}[r['decision']],-r['score'],abs(r['distance_to_high_pct'])))
            self.store.set('selection',self.selection);self.store.set('selection_updated',now().isoformat())
            by_symbol={r['symbol']:r for r in rows}
            self.store.set('selection_pool',{'date':today,'rows':[by_symbol[r['symbol']] for r in self.selection]})
            self.allocate_monitoring()
        finally:self.selection_running=False;self.broadcast()

    async def prioritize_selection(self,market='CN'):
        if market not in ['CN','US','ALL']:raise ValueError('未知市场')
        preferred=[r['symbol'] for r in self.selection if r['decision']=='重点观察' and (market=='ALL' or r['market']==market)][:8]
        if not preferred:raise ValueError('尚无通过趋势和流动性核验的重点候选')
        held=[s for a in self.sim.state['accounts'].values() for s in a['positions']]
        self.store.set('watch_before_priority',self.watch)
        existing=[s for s in self.watch if market=='ALL' or symbol_market(s)==market]
        self.watch=list(dict.fromkeys(self.real.symbols()+held+preferred+self.manual_watch+existing))[:self.settings['monitor_limit']]
        self.store.set('watch',self.watch)
        if self.lb.ctx:
            for q in await self.lb.quotes(self.tracked()):self.accept_quote(q)
        await self.refresh_bars()
        self.broadcast()

    def candidate(self,symbol):
        row=next((r for r in self.candidates if r['symbol']==symbol),None)
        if row is None:
            row={'symbol':symbol,'name':self.security_map.get(symbol,{}).get('name',symbol),'market':symbol_market(symbol),'market_cap':None,'risk_group':'pending'}
            self.candidates.append(row)
        return row

    async def add_watch(self,symbol):
        symbol=symbol.upper().strip()
        if not re.fullmatch(r'[A-Z0-9.\-]{1,16}\.(US|SH|SZ)',symbol) or not self.allowed_security({'symbol':symbol,'name':self.security_map.get(symbol,{}).get('name','')}):
            raise ValueError('请输入码表内的美股、沪深主板或创业板股票，例如 AAPL.US、300750.SZ')
        if symbol not in self.watch:
            if len(self.tracked())>=self.settings['monitor_limit']:raise ValueError('监测名额已满，请先移除一只或增加名额')
            self.watch.append(symbol);self.store.set('watch',self.watch)
            self.manual_watch=list(dict.fromkeys(self.manual_watch+[symbol]));self.store.set('manual_watch',self.manual_watch)
        q=await self.lingxi.quotes([symbol])
        for item in q:self.accept_quote(item)
        await self.refresh_bars([symbol]);self.broadcast()

    async def remove_watch(self,symbol):
        if any(symbol in a['positions'] for a in self.sim.state['accounts'].values()):raise ValueError('模拟持仓仍需监测，退出后再移除')
        if symbol in self.real.symbols():raise ValueError('真实持仓仍需监测；请先在券商端处理或删除手工持仓记录')
        self.watch=[s for s in self.watch if s!=symbol];self.store.set('watch',self.watch)
        self.manual_watch=[s for s in self.manual_watch if s!=symbol];self.store.set('manual_watch',self.manual_watch)
        self.sim.cancel_pending(symbol,'已移出监测');self.broadcast()

    def accept_quote(self,q):
        key=(q.symbol,q.source);previous=self.comparisons.get(key)
        if previous and q.market_time and previous.market_time and q.market_time<previous.market_time:return
        book=self.books.get(q.symbol)
        if book and book.get('source')==q.source:
            for k in ['bid','ask','bid_size','ask_size','depth_time']:setattr(q,k,book[k])
        if symbol_market(q.symbol)=='CN':
            meta=self.validation.get(q.symbol,{})
            q.limit_up,q.limit_down=price_limits(meta.get('previous_close'),q.symbol,meta.get('risk_group','pending'),local_date(now(),'CN'))
        self.comparisons[key]=q
        selected=self.quotes.get(q.symbol)
        # Keep one source explicit; supplemental snapshots do not overwrite a healthy strategy source.
        if not selected or selected.source==q.source or q.source==self.validation.get(q.symbol,{}).get('source') or (now()-selected.received_at).total_seconds()>30:
            self.quotes[q.symbol]=q
        self.evaluate_decisions()
        self.broadcast()

    def suspend(self,symbol,reason):
        detail=self.details.setdefault(symbol,{})
        if detail.get('status')!='paused' or detail.get('reason')!=reason:
            self.store.event('data',{'symbol':symbol,'message':reason})
        detail.update(status='paused',reason=reason)
        self.validation.setdefault(symbol,{})['ready']=False
        self.sim.mark_unobservable(symbol,reason)

    def apply_limits(self,b,group):
        if symbol_market(b.symbol)=='US':return True
        history=self.strategies.history.get(b.symbol,[])
        day=local_date(b.start,'CN')
        previous=[r for r in history if local_date(r.start,'CN')<day]
        prevclose=previous[-1].close if previous else self.validation.get(b.symbol,{}).get('previous_close')
        if not prevclose:return False
        b.limit_up,b.limit_down=price_limits(prevclose,b.symbol,group,day)
        return True

    def accept_bar(self,b):
        v=self.validation.get(b.symbol,{})
        if not b.final or not b.valid() or b.end>now() or not is_open(b.start,symbol_market(b.symbol)):return
        if b.source!=v.get('source'):return
        h=self.strategies.history.get(b.symbol,[])
        if h and b.start<=h[-1].start:return
        if h and not adjacent(h[-1].start,b.start,symbol_market(b.symbol)):
            self.suspend(b.symbol,'发现交易时段K线缺口，暂停并重新补齐');return
        if not self.apply_limits(b,v.get('risk_group','pending')):self.suspend(b.symbol,'缺少前收盘价，无法验证涨跌停');return
        self.store.bar(b)
        fresh=0<=(now()-b.end).total_seconds()<90
        # Do not execute historical next bars during warm-up or reconnection.
        eligible=bool(v.get('ready') and v.get('eligible') and fresh)
        if fresh and v.get('ready'):
            self.sim.process(b,allow_entries=eligible and self.settings['simulation_enabled'])
        signals=self.strategies.update(b,v.get('risk_group','pending'))
        detail=self.details.setdefault(b.symbol,{})
        detail.update(status='monitoring' if eligible else 'waiting',reason='等待完整K线触发' if eligible else v.get('reason','数据未通过验证'),preview=self.strategies.preview(b.symbol),last_bar=b.start.isoformat(),source=b.source)
        if eligible:
            for s in signals:
                self.register_signal(s)
        self.evaluate_decisions()
        self.broadcast()

    async def refresh_bars(self,symbols=None):
        async with self.fetch_lock:
            for symbol in symbols or self.tracked():
                try:
                    meta=self.candidate(symbol);v=self.validation.setdefault(symbol,{})
                    if v.get('daily_checked')!=str(local_date(now(),symbol_market(symbol))):
                        cache_key='selection_daily:'+symbol
                        cached=self.store.get(cache_key,{})
                        if cached.get('fetched_date')==str(local_date(now(),symbol_market(symbol))) and len(cached.get('bars',[]))>=125:
                            days=[Bar.load(b) for b in cached['bars']]
                        else:
                            days=await self.lb.bars(symbol,'day',130)
                            self.store.set(cache_key,{'fetched_date':str(local_date(now(),symbol_market(symbol))),'bars':[b.dump() for b in days]})
                        completed=[b for b in days if local_date(b.start,symbol_market(symbol))<local_date(now(),symbol_market(symbol))]
                        if len(completed)<21:raise ValueError('不足21个完整交易日，暂不产生交易信号')
                        recent=completed[-20:];amount=sum(b.turnover or 0 for b in recent)/20
                        atr=sum(max(b.high-b.low,abs(b.high-completed[-21+i].close),abs(b.low-completed[-21+i].close)) for i,b in enumerate(recent))/20
                        atr_pct=atr/recent[-1].close*100
                        cap=meta.get('market_cap') or (self.quotes.get(symbol).market_cap if self.quotes.get(symbol) else None)
                        st=self.is_st(symbol,meta.get('name',''))
                        small=cap is not None and cap<(2e9 if symbol_market(symbol)=='US' else 1e10)
                        group='st' if st else 'smallcap' if (small or atr_pct>=5) else 'normal'
                        enough=amount>=(5e6 if symbol_market(symbol)=='US' else 1e8)
                        split=any(abs(completed[i].close/completed[i-1].close-1)>.45 for i in range(1,len(completed)))
                        known=cap is not None and cap>0
                        v.update(daily_checked=str(local_date(now(),symbol_market(symbol))),eligible=enough and known and not split,risk_group=group,atr_pct=atr_pct,
                                 average_turnover=amount,previous_close=recent[-1].close,
                                 reason='流动性通过' if enough and known and not split else '市值缺失／流动性不足／疑似除权，暂停信号')
                        meta.update(risk_group=group,eligible=v['eligible'],eligibility_reason=v['reason'])
                    source='longbridge'
                    try:bars=await self.lb.bars(symbol)
                    except Exception:
                        if symbol.endswith('.US') and symbol in self.ib.verified:
                            bars=await self.ib.bars(symbol);source='ibkr'
                        else:raise
                    bars=[b for b in bars if b.final and b.valid() and is_open(b.start,symbol_market(symbol))]
                    if len(bars)<21:raise ValueError('完整5分钟K线不足')
                    position=next((a['positions'][symbol] for a in self.sim.state['accounts'].values() if symbol in a['positions']),None)
                    prior_source=v.get('source') or (position or {}).get('source')
                    changed=prior_source is not None and prior_source!=source
                    history=self.strategies.history.get(symbol,[])
                    reset=changed or not history or not v.get('ready')
                    if reset:
                        if changed or v.get('source'):
                            self.sim.mark_unobservable(symbol,'数据重建，模拟持仓待下一可成交K线退出')
                        self.strategies.reset(symbol);self.sim.cancel_pending(symbol,'初始化／重建时取消未成交信号')
                        # Warm from a contiguous tail only, avoiding false volume/trend calculations across a feed gap.
                        tail=[]
                        for b in bars:
                            if tail and not adjacent(tail[-1].start,b.start,symbol_market(symbol)):tail=[]
                            tail.append(b)
                        if len(tail)<21:raise ValueError('连续K线不足21根')
                        for b in tail:
                            self.strategies.update(b,v['risk_group']);self.store.bar(b)
                        if changed:
                            for account in self.sim.state['accounts'].values():
                                if symbol in account['positions']:
                                    p=account['positions'][symbol]
                                    p.setdefault('entry_source',p['source']);p['source']=source
                                    p['pending_exit']='切换行情源并补齐后，下一可成交K线退出'
                                    p['observation_gap']=True
                            self.sim.save()
                        v.update(source=source,ready=True)
                        self.details[symbol]={'status':'monitoring' if is_open(now(),symbol_market(symbol)) else 'closed','reason':'历史数据预热完成；等待新增完整K线',
                                              'preview':self.strategies.preview(symbol),'last_bar':tail[-1].start.isoformat(),'source':source}
                        if changed:self.store.event('data',{'symbol':symbol,'message':f'行情源设为{source}，历史预热完成；不补做旧信号'})
                    else:
                        for b in bars:self.accept_bar(b)
                    self.validation[symbol]=v
                except Exception as exc:
                    reason=str(exc) if isinstance(exc,(ValueError,RuntimeError)) else 'K线暂不可用，等待数据权限或网络恢复'
                    self.details[symbol]={'status':'waiting','reason':reason,'source':'longbridge','preview':{'ready':False}}
                    self.validation.setdefault(symbol,{})['ready']=False
                self.broadcast()
            if self.lb.ctx:
                try:await self.lb.subscribe([s for s in self.tracked() if self.validation.get(s,{}).get('ready')])
                except Exception:self.lb.fail('订阅未完成，继续按限频查询并暂停未验证标的')

    async def benchmark(self):
        cn=[s for s in self.tracked() if s.endswith(('.SH','.SZ'))]
        us=[s for s in self.tracked() if s.endswith('.US')]
        st=[r['symbol'] for r in self.candidates if r.get('risk_group')=='st']
        symbols=list(dict.fromkeys(cn[:1]+st[:1]+us[:2]))
        for provider in [self.lingxi,self.ib]:
            requested=[s for s in symbols if provider.name!='ibkr' or s.endswith('.US')]
            t=time.monotonic();quotes=await provider.quotes(requested)
            row={'request_ms':round((time.monotonic()-t)*1000),'requested':len(requested),'returned':len(quotes),
                 'during_session':any(is_open(now(),symbol_market(s)) for s in symbols),
                 'samples':[{'symbol':q.symbol,'market_time':q.market_time.isoformat() if q.market_time else None,
                             'bid_ask_complete':bool(q.bid and q.ask and q.bid>0 and q.ask>=q.bid),'quality':q.quality} for q in quotes]}
            self.store.benchmark(provider.name,row)
            for q in quotes:self.accept_quote(q)
        if self.lb.ctx:
            t=time.monotonic();ok=await self.lb.healthcheck()
            self.store.benchmark('longbridge',{'request_ms':round((time.monotonic()-t)*1000),'operation':'订阅连接检查，非行情延迟排名','ok':ok})
        self.broadcast()

    def snapshot(self):
        rows={r['symbol']:dict(r) for r in self.candidates}
        for s in self.tracked():rows.setdefault(s,self.candidate(s))
        for s,r in rows.items():
            q=self.quotes.get(s);r['quote']=q.dump() if q else None
            setup=next((x for x in self.selection if x['symbol']==s),None)
            if setup and not q and not r.get('price'):
                r.update(price=setup['close'],source=setup['source'],price_label='日线收盘参考',market_time=setup['as_of']+'T15:00:00+08:00',change_pct=None)
            r['monitored']=s in self.tracked();r['detail']=self.details.get(s,{'status':'candidate','reason':'加入监测后验证买点条件'})
            v=self.validation.get(s,{})
            if v:r.update(risk_group=v.get('risk_group',r.get('risk_group','pending')),eligible=v.get('eligible',False),eligibility_reason=v.get('reason',''),atr_pct=v.get('atr_pct'),average_turnover=v.get('average_turnover'))
            r['market']=symbol_market(s)
        real_symbols=self.real.symbols();tracked=set(self.tracked())
        alerts=self.alerts.list(30);simulation=self.sim.summary()
        workspaces={m:self.market_workspace(m,rows,simulation,alerts) for m in ['CN','US']}
        return {'time':now().isoformat(),'version':VERSION,'workspace_version':'工作台 3.0','settings':self.settings,
                'decisions':self.decisions,'exit_plans':self.exit_plans,'real_holdings':self.real.list(),'real_plans':self.real_plans,
                'holding_sync':self.holding_sync,'premarket':{m:self.premarket(m) for m in ['CN','US']},'alerts':alerts,
                'notification_settings':self.alerts.status(),'monitor':{'running':self.monitor_running,'last_ok':self.runtime_ok,'ai_calls':0,'research':self.research_progress,
                    'window':{m:monitoring_window(now(),m) for m in ['CN','US']},'uncovered':max(0,len(self.selection)-len(tracked)),
                    'uncovered_holdings':sum(s not in tracked for s in real_symbols)},
                'daily_reports':self.store.get('daily_reports',[])[:10],
                'markets':{m:{'open':is_open(now(),m),'phase':phase(now(),m)} for m in ['CN','US']},
                'calendar_valid_until':'2026-12-31','candidates':list(rows.values()),'watch':self.tracked(),
                'coverage':{'returned_candidates':len(self.candidates),'monitored':len(self.tracked()),'ready':sum(bool(v.get('ready')) for s,v in self.validation.items() if s in self.tracked()),'last_scan':self.last_scan,'full_market':False},
                'providers':{k:dict(v.status,auth_url=self.lb.auth_url if k=='longbridge' else None) for k,v in self.providers.items()},
                'scanning':self.scanning,'market_stats':self.market_stats,'market_stats_time':self.market_stats_time,
                'selection':self.selection,'selection_running':self.selection_running,'selection_progress':self.selection_progress,
                'selection_errors':self.selection_errors,'selection_updated':self.store.get('selection_updated'),
                'signals':self.store.signals(),'simulation':simulation,'failures':self.sim.state['failures'][-60:],
                'pending':list(self.sim.state['pending'].values()),'events':self.store.events(),'benchmarks':self.store.benchmarks(),
                'workspaces':workspaces}

    def strategy_performance(self,market):
        result={}
        for strategy in DEFINITIONS:
            version=self.registry.current(strategy,market)['version']
            result[strategy]=self.sim.summary(version)[market]['strategies'][strategy]
        return result

    def strategy_recommendations(self,market,rows=None,decisions=None,premarket=None,simulation=None):
        rows=rows or {r['symbol']:dict(r) for r in self.candidates}
        decisions=decisions if decisions is not None else [r for r in self.decisions if symbol_market(r['symbol'])==market]
        premarket=premarket if premarket is not None else self.premarket(market)
        output={}
        for strategy,definition in DEFINITIONS.items():
            if not self.registry.enabled(strategy,market):
                output[strategy]={'strategy':strategy,'name':definition['name'],'enabled':False,
                                  'version':self.registry.current(strategy,market)['version'],
                                  'primary':None,'backups':[],'rows':[],'waiting':[],'reason':'该策略已停用'}
                continue
            config=self.registry.config(strategy,market);items=[];seen=set()
            for decision in decisions:
                strategies=decision.get('strategies') or [decision.get('strategy')]
                if strategy not in strategies:continue
                row=dict(decision,strategy=strategy,strategy_name=definition['name'],slot='buy',
                         conditions={'盘中信号':True,'行情盘口':decision.get('state')=='buy','资金风控':decision.get('state')=='buy'})
                items.append(row);seen.add(row['symbol'])
            for setup in premarket:
                if setup['symbol'] in seen:continue
                score=float(setup.get('score') or 0);conditions={
                    '趋势向上':bool(setup.get('trend')),'跑赢基准':(setup.get('relative_strength') or -999)>config.get('relative_strength_min',0),
                    '成交活跃':(setup.get('average_turnover') or 0)>=config.get('liquidity_min',0)}
                reason=setup.get('reason','等待盘中确认');trigger=setup.get('breakout_reference')
                if strategy=='pullback':
                    reason='等待先形成有效突破，再观察首次回踩';score-=3
                elif strategy=='trend_pullback':
                    conditions.update({'均线多头':bool(setup.get('stacked')) and setup.get('close',0)>setup.get('ma60',float('inf')),
                                       '不过度延伸':(setup.get('extension_atr') or 999)<=3})
                    score+=(8 if conditions['均线多头'] else -12)+(5 if conditions['不过度延伸'] else -10)
                    trigger=setup.get('ma20');reason='等待盘中回踩VWAP或五分钟EMA后重新转强'
                elif strategy=='volatility_breakout':
                    contraction=setup.get('atr_contraction')
                    conditions.update({'波动收缩':contraction is not None and contraction<=config['contraction_max'],
                                       '靠近20日高点':abs(setup.get('distance_to_high_pct') or 999)<=config['near_high_pct']})
                    score+=(12 if conditions['波动收缩'] else -15)+(8 if conditions['靠近20日高点'] else -10)
                    trigger=setup.get('high10');reason='等待ATR收缩并放量突破整理区'
                score=max(0,min(100,round(score,1)))
                items.append({'symbol':setup['symbol'],'name':setup.get('name',setup['symbol']),'strategy':strategy,
                              'strategy_name':definition['name'],'version':self.registry.current(strategy,market)['version'],
                              'state':'watch','slot':'watch','action':'观察备选','score':score,'reason':reason,
                              'trigger':trigger,'stop':setup.get('structure_low'),'risk_group':setup.get('risk_group','pending'),
                              'source':setup.get('source'),'as_of':setup.get('as_of'),'conditions':conditions,
                              'quote':rows.get(setup['symbol'],{}).get('quote')})
            items.sort(key=lambda r:(0 if r.get('state')=='buy' else 1,-r.get('score',0),r['symbol']))
            selected=items[:3]
            output[strategy]={'strategy':strategy,'name':definition['name'],'enabled':self.registry.enabled(strategy,market),
                              'version':self.registry.current(strategy,market)['version'],
                              'primary':selected[0] if selected else None,'backups':selected[1:3],'rows':selected,
                              'waiting':items[3:10]}
        return output

    def save_strategy_config(self,strategy,market,parameters=None,enabled=None,reason=''):
        old=self.registry.current(strategy,market)
        row=self.registry.save(strategy,market,parameters,enabled,reason)
        if row['version']!=old['version']:
            self.sim.cancel_strategy(strategy,market,'策略参数已更新，取消旧版本待买信号')
            for symbol in [s for s in self.tracked() if symbol_market(s)==market]:
                self.research_ready.pop(symbol,None);self.strategies.reset(symbol)
                self.details.setdefault(symbol,{})['reason']='策略参数已更新，正在按新版本重新预热'
            self.store.event('strategy',{'message':f"{DEFINITIONS[strategy]['name']}已启用新参数版本",'strategy':strategy,'market':market,'version':row['version']})
            self.queue_replay_readiness(row)
            self.first_poll=True;self.evaluate_decisions();self.broadcast()
        return row

    def rollback_strategy(self,strategy,market,version):
        old=self.registry.current(strategy,market);row=self.registry.rollback(strategy,market,version)
        if row['version']!=old['version']:
            self.sim.cancel_strategy(strategy,market,'策略已恢复历史参数，取消旧版本待买信号')
            for symbol in [s for s in self.tracked() if symbol_market(s)==market]:
                self.research_ready.pop(symbol,None);self.strategies.reset(symbol)
                self.details.setdefault(symbol,{})['reason']='策略已恢复历史参数，正在重新预热'
            self.store.event('strategy',{'message':f"{DEFINITIONS[strategy]['name']}已恢复历史参数",'strategy':strategy,'market':market,'version':row['version']})
            self.queue_replay_readiness(row)
            self.first_poll=True;self.evaluate_decisions();self.broadcast()
        return row

    def queue_replay_readiness(self,row):
        """Audit point-in-time history in the background before any exploratory replay."""
        strategy,market,version=row['strategy'],row['market'],row['version']
        self.registry.set_replay(strategy,market,version,{
            'state':'checking','label':'历史探索结果','message':'正在检查逐时点日线、基准和盘中历史是否完整'})
        async def audit():
            await asyncio.sleep(0)
            symbols=[r['symbol'] for r in self.selection if r.get('market')==market][:10]
            complete=0;details=[];day=local_date(now(),market)
            try:sessions=list(calendar(market).sessions_in_range(str(day-timedelta(days=45)),str(day-timedelta(days=1))))[-14:]
            except Exception:sessions=[]
            benchmark=len(self.store.get(f'benchmark_daily:{market}:{day}',[]))
            for symbol in symbols:
                daily=len(self.store.get('selection_daily:'+symbol,{}).get('bars',[]))
                history=sum(bool(self.store.get(f'research_bars:{symbol}:{session.date()}')) for session in sessions)
                ready=daily>=65 and history>=14 and benchmark>=65
                complete+=int(ready);details.append({'symbol':symbol,'daily_bars':daily,'intraday_sessions':history,'ready':ready})
            message=(f'已有{complete}只股票具备完整逐时点历史，尚需同源基准盘中历史后再运行'
                     if complete else '本地逐时点历史尚不完整；为避免未来数据泄漏，暂不生成回放收益')
            self.registry.set_replay(strategy,market,version,{
                'state':'insufficient_data','label':'历史探索结果','message':message,
                'checked_at':now().isoformat(),'symbols_checked':len(symbols),'complete_symbols':complete,
                'requirements':{'daily_bars':65,'prior_intraday_sessions':14,'same_source_benchmark':True},
                'details':details})
            self.broadcast()
        try:
            task=asyncio.get_running_loop().create_task(audit());self.tasks.append(task)
        except RuntimeError:
            # Direct unit-level configuration changes have no running service loop.
            pass

    async def analyze_stock(self,data):
        symbol=str(data.get('symbol','')).upper().strip()
        if not re.fullmatch(r'[A-Z0-9.\-]{1,16}\.(US|SH|SZ)',symbol):
            pool={s:r.get('name',s) for s,r in self.security_map.items()}
            pool.update({r['symbol']:r.get('name',r['symbol']) for r in self.candidates})
            pool.update({r['symbol']:r.get('name',r['symbol']) for r in self.real.list()})
            exact=[s for s,name in pool.items() if str(name).upper()==symbol]
            partial=[s for s,name in pool.items() if symbol and symbol in str(name).upper()]
            matches=exact or partial
            if len(matches)==1:symbol=matches[0]
            elif len(matches)>1:raise ValueError('名称匹配到多只股票，请从提示中选择具体代码')
        market=symbol_market(symbol)
        if not re.fullmatch(r'[A-Z0-9.\-]{1,16}\.(US|SH|SZ)',symbol) or not self.allowed_security({'symbol':symbol,'name':self.security_map.get(symbol,{}).get('name','')}):
            raise ValueError('请输入码表内的美股、沪深主板或创业板股票代码')
        candidate=next((r for r in self.candidates if r['symbol']==symbol),{})
        cached=symbol in self.strategies.context and bool(self.strategies.history.get(symbol))
        day=local_date(now(),market)
        selection_cache=self.store.get('selection_daily:'+symbol,{})
        daily=[Bar.load(b) for b in selection_cache.get('bars',[]) if local_date(stamp(b['start']),market)<day]
        benchmark_daily=list(self.benchmark_daily.get(market,[]))
        if not benchmark_daily:
            saved=self.store.get(f'benchmark_daily:{market}:{day}',[])
            benchmark_daily=[Bar.load(b) for b in saved]
        stored=[Bar.load(b) for b in self.store.bars(symbol,'longbridge')]
        current_bars=[b for b in stored if b.final and b.end<=now() and is_open(b.start,market) and local_date(b.start,market)==day]
        previous={b.start:b for b in stored if b.final and is_open(b.start,market) and local_date(b.start,market)<day}
        try:sessions=list(calendar(market).sessions_in_range(str(day-timedelta(days=45)),str(day-timedelta(days=1))))[-14:]
        except Exception:sessions=[]
        for session in sessions:
            for raw in self.store.get(f'research_bars:{symbol}:{session.date()}',[]):
                bar=Bar.load(raw)
                if bar.final and bar.valid():previous.setdefault(bar.start,bar)
        historical=[previous[k] for k in sorted(previous)]
        benchmark_intraday=list(self.strategies.benchmarks.get(market,[]))
        quote=self.quotes.get(symbol)
        references={}

        # A one-off analysis must remain responsive. Fill only missing pieces in
        # parallel and keep an honest WAIT result when a provider is slow.
        if self.lb.ctx:
            jobs={}
            if len(daily)<25:jobs['daily']=asyncio.create_task(asyncio.wait_for(self.lb.bars(symbol,'day',130),8))
            if len(current_bars)<2:jobs['intraday']=asyncio.create_task(asyncio.wait_for(self.lb.bars(symbol,'5m',1000),8))
            if len(benchmark_daily)<21:jobs['benchmark_daily']=asyncio.create_task(asyncio.wait_for(self.lb.bars(BENCHMARKS[market],'day',130),8))
            if not benchmark_intraday:jobs['benchmark_intraday']=asyncio.create_task(asyncio.wait_for(self.lb.bars(BENCHMARKS[market],'5m',1000),8))
            if not candidate.get('name'):jobs['reference']=asyncio.create_task(asyncio.wait_for(self.lb.reference_info([symbol]),5))
            if not quote or (now()-quote.received_at).total_seconds()>30:
                jobs['quote']=asyncio.create_task(asyncio.wait_for(self.lb.quotes([symbol]),5))
            if jobs:
                values=await asyncio.gather(*jobs.values(),return_exceptions=True)
                fetched=dict(zip(jobs,values))
                if isinstance(fetched.get('daily'),list):
                    daily=[b for b in fetched['daily'] if local_date(b.start,market)<day]
                    if daily:self.store.set('selection_daily:'+symbol,{'fetched_date':str(day),'bars':[b.dump() for b in daily]})
                if isinstance(fetched.get('intraday'),list):
                    batch=[b for b in fetched['intraday'] if b.final and b.end<=now() and is_open(b.start,market)]
                    for bar in batch:self.store.bar(bar)
                    current_bars=[b for b in batch if local_date(b.start,market)==day]
                    historical=sorted({b.start:b for b in historical+batch if local_date(b.start,market)<day}.values(),key=lambda b:b.start)
                if isinstance(fetched.get('benchmark_daily'),list):
                    benchmark_daily=[b for b in fetched['benchmark_daily'] if local_date(b.start,market)<day]
                    if benchmark_daily:self.store.set(f'benchmark_daily:{market}:{day}',[b.dump() for b in benchmark_daily])
                if isinstance(fetched.get('benchmark_intraday'),list):
                    benchmark_intraday=[b for b in fetched['benchmark_intraday'] if b.final and b.end<=now() and is_open(b.start,market)]
                if isinstance(fetched.get('reference'),dict):references=fetched['reference']
                if isinstance(fetched.get('quote'),list) and fetched['quote']:quote=fetched['quote'][0]
        name=references.get(symbol,{}).get('name') or candidate.get('name') or self.security_map.get(symbol,{}).get('name') or symbol
        if cached:
            context=self.strategies.context[symbol];daily=context.get('daily_bars',[]);benchmark_daily=context.get('benchmark_daily',[])
            intraday=self.strategies.history[symbol];benchmark_intraday=self.strategies.benchmarks.get(market,[])
            signals=[Signal.load(r) for r in self.store.signals()
                     if r['symbol']==symbol and r.get('version')==self.registry.active_versions().get((market,r.get('strategy')))]
            preview=self.strategies.preview(symbol).get('strategies',{})
        else:
            intraday=sorted(current_bars,key=lambda b:b.start);signals=[];preview={}
            if daily and benchmark_daily:
                engine=ResearchEngine(self.registry);engine.prepare(symbol,daily,historical,benchmark_daily);engine.set_benchmark(market,benchmark_intraday)
                risk_group='st' if self.is_st(symbol,name) else 'smallcap' if (_atr_pct(daily)>=5) else 'normal'
                for bar in intraday:signals.extend(engine.update(bar,risk_group))
                preview=engine.preview(symbol).get('strategies',{})
        risk_group='st' if self.is_st(symbol,name) else 'smallcap' if (_atr_pct(daily)>=5) else 'normal'
        if quote and is_open(now(),market):
            try:
                book=await asyncio.wait_for(self.lb.depth(symbol),3)
                if book and book.get('source')==quote.source:
                    for key in ['bid','ask','bid_size','ask_size','depth_time']:setattr(quote,key,book[key])
            except Exception:pass
        base=self.registry.config('breakout',market)
        factors=daily_factors(symbol,daily,benchmark_daily,liquidity_min=base['liquidity_min'],rs_min=base['relative_strength_min'])
        cap=references.get(symbol,{}).get('total_shares');cap=cap*daily[-1].close if cap and daily else candidate.get('market_cap')
        existing_validation=self.validation.get(symbol,{}) if cached else {}
        data_ready=len(daily)>=25 and len(benchmark_daily)>=21 and len(intraday)>=2
        source=intraday[-1].source if intraday else daily[-1].source if daily else 'longbridge'
        validation={'ready':data_ready,'eligible':bool(existing_validation.get('eligible',factors.get('eligible') and cap)),
                    'source':source,'risk_group':risk_group,
                    'atr_pct':_atr_pct(daily),'average_turnover':factors.get('average_turnover'),
                    'reason':'数据通过' if data_ready and factors.get('eligible') and cap else
                             '等待完整日线、五分钟K线和基准同步' if not data_ready else '市值、趋势、流动性或相对强弱尚未全部通过'}
        stored_holdings=self.real.list();stored_holding=next((p for p in stored_holdings if p['symbol']==symbol and p.get('quantity',0)>0),None)
        temporary=list(stored_holdings);holding_input=data.get('holding') or {};holding_origin='saved' if stored_holding else None
        if holding_input.get('quantity') and holding_input.get('cost'):
            temporary=[p for p in temporary if p['symbol']!=symbol]
            temporary.append({'id':'analysis:'+symbol,'symbol':symbol,'name':name,'market':market,'source':'manual',
                              'quantity':float(holding_input['quantity']),'available':holding_input.get('available'),
                              'cost':float(holding_input['cost']),'currency':'USD' if market=='US' else 'CNY',
                              'entry_date':holding_input.get('entry_date'),'stop':holding_input.get('stop'),
                              'target':holding_input.get('target'),'note':'临时分析','updated_at':now().isoformat()})
            holding_origin='temporary'
        decisions=recommend([s.dump() for s in signals],{symbol:quote} if quote else {},{symbol:validation},
                            self.sim.state,now(),temporary,self.registry.active_versions())
        per_strategy=[]
        for strategy,definition in DEFINITIONS.items():
            decision=next((r for r in decisions if r['strategy']==strategy or strategy in r.get('strategies',[])),None)
            view=preview.get(strategy,{'status':'wait','ready':False,'reason':validation['reason']})
            state='符合' if decision and decision.get('state')=='buy' else '不适用' if view.get('status')=='disabled' else '等待'
            per_strategy.append({'strategy':strategy,'name':definition['name'],'version':self.registry.current(strategy,market)['version'],
                                 'state':state,'reason':decision.get('reason') if decision else view.get('reason'),
                                 'conditions':view,'decision':decision,'enabled':self.registry.enabled(strategy,market)})
        entered=next((p for p in temporary if p['symbol']==symbol and p.get('quantity',0)>0),None)
        holding_plan_row=real_holding_plan(entered,quote,now(),validation) if entered else None
        buy=next((r for r in decisions if r.get('state')=='buy'),None)
        if holding_plan_row:final={'status':'MANAGE','action':holding_plan_row['action'],'reason':holding_plan_row['reason']}
        elif buy:final={'status':'BUY','action':'可考虑买入','reason':buy['reason'],'decision':buy}
        elif not is_open(now(),market):
            final={'status':'WAIT','action':'继续等待','reason':'当前休市；开盘后用新的完整K线、报价和盘口重新判断'}
        elif not validation['ready']:
            final={'status':'WAIT','action':'继续等待','reason':validation['reason']}
        elif not fresh_quote(quote,now(),source):
            final={'status':'WAIT','action':'暂不买入','reason':'实时报价超过30秒或权限未确认，统一风控已否决买入'}
        else:
            reasons=[r['reason'] for r in per_strategy if r.get('reason')]
            final={'status':'WAIT','action':'继续等待',
                   'reason':reasons[0] if reasons else validation['reason']}
        return {'symbol':symbol,'name':name,'market':market,'monitored':symbol in self.tracked(),
                'quote':quote.dump() if quote else None,'validation':validation,'strategies':per_strategy,
                'holding':holding_plan_row,'holding_origin':holding_origin,'final':final,'analyzed_at':now().isoformat(),'source':source,
                'note':'临时分析不会自动写入持仓或占用持续监测名额'}

    def market_workspace(self,market,rows,simulation,alerts):
        """One self-contained market payload for the decision-first UI."""
        suffix=lambda symbol:symbol_market(symbol)==market
        tracked=[s for s in self.tracked() if suffix(s)]
        candidates=[dict(r) for s,r in rows.items() if suffix(s)]
        premarket=self.premarket(market)
        decisions=[dict(r) for r in self.decisions if suffix(r['symbol'])]
        buys=[r for r in decisions if r.get('state')=='buy']
        primary=buys[0] if buys else None
        backups=buys[1:3]
        used={r['symbol'] for r in buys[:3]}
        watching=[]
        for row in premarket:
            if row['symbol'] in used:continue
            detail=self.details.get(row['symbol'],{})
            progress=self.research_progress.get(row['symbol'],{})
            watching.append({
                'symbol':row['symbol'],'name':row.get('name',row['symbol']),'risk_group':row.get('risk_group','pending'),
                'score':row.get('score'),'strategy':None,'state':'watch','action':'观察备选',
                'reason':detail.get('preview',{}).get('reason') or progress.get('reason') or row.get('reason','等待盘中确认'),
                'trigger':row.get('breakout_reference'),'stop':row.get('structure_low'),
                'quote':rows.get(row['symbol'],{}).get('quote'),'source':row.get('source'),'as_of':row.get('as_of')})
            if len(watching)>=3:break
        t=now()
        planned_real={p['id']:p for p in self.real_plans}
        real=[]
        for position in self.real.list():
            if not suffix(position['symbol']):continue
            plan=planned_real.get(position['id']) or real_holding_plan(
                position,self.quotes.get(position['symbol']),t,self.validation.get(position['symbol'],{}))
            real.append(dict(plan))
        planned_paper={p['position_id']:p for p in self.exit_plans}
        paper=[]
        for position in simulation[market]['positions']:
            plan=planned_paper.get(position['id']) or holding_plan(
                position,self.quotes.get(position['symbol']),t,self.validation.get(position['symbol'],{}))
            paper.append(dict(plan))
        actionable=[dict(p,holding_type='real') for p in real if p.get('event') or p.get('action') in ['需要设置保护价','行情待恢复','退出条件已触发','暂时无法卖出']]
        actionable += [dict(p,holding_type='paper') for p in paper if p.get('event') or p.get('blocked')]
        # Only real-position actions take over the buy decision page. Paper exits
        # remain available in the separate simulation tab.
        urgent=any(p.get('holding_type')=='real' for p in actionable)
        if urgent:status='MANAGE';headline='先处理持仓风险'
        elif primary:status='BUY';headline=f"当前首选：{primary.get('name',primary['symbol'])}"
        else:status='WAIT';headline='今天暂不买'
        market_alerts=[a for a in alerts if a.get('symbol') and suffix(a['symbol'])]
        market_quotes=[self.quotes[s] for s in tracked if s in self.quotes]
        latest=max((q.market_time for q in market_quotes if q.market_time),default=None)
        ready=sum(bool(self.validation.get(s,{}).get('ready')) for s in tracked)
        fresh=sum(fresh_quote(self.quotes.get(s),now()) for s in tracked)
        uncovered=sum(s not in tracked for s in self.real.symbols() if suffix(s))
        open_now=is_open(now(),market)
        if not self.monitor_running:health_state='paused';health_text='盯盘已暂停'
        elif not open_now:health_state='closed';health_text='休市，保留最近有效数据'
        elif not tracked or ready==0:health_state='error';health_text='行情中断或尚未预热'
        elif fresh<len(tracked):health_state='delayed';health_text=f'{len(tracked)-fresh}只报价待更新'
        else:health_state='healthy';health_text='行情正常'
        issues=[]
        if uncovered:issues.append(f'{uncovered}只真实持仓未覆盖')
        if open_now and fresh<len(tracked):issues.append('部分报价超过30秒')
        if not self.monitor_running:issues.append('后台盯盘已暂停')
        recommendations=self.strategy_recommendations(market,rows,decisions,premarket,simulation)
        if primary:primary['matched_strategies']=primary.get('strategies',[primary.get('strategy')])
        for row in backups:row['matched_strategies']=row.get('strategies',[row.get('strategy')])
        return {
            'meta':{'market':market,'name':'A股' if market=='CN' else '美股','currency':'CNY' if market=='CN' else 'USD',
                    'currency_symbol':'¥' if market=='CN' else '$','benchmark':'沪深300' if market=='CN' else 'SPY',
                    'timezone':'Asia/Shanghai' if market=='CN' else 'America/New_York','phase':phase(now(),market),'open':open_now},
            'health':{'state':health_state,'text':health_text,'tracked':len(tracked),'ready':ready,'fresh':fresh,
                      'uncovered_holdings':uncovered,'data_as_of':latest.isoformat() if latest else self.store.get('selection_updated'),
                      'issues':issues},
            'decision':{'status':status,'headline':headline,'primary':primary,'backups':backups,'watching':watching,
                        'reason':'有持仓退出条件需要先处理' if urgent else '买点已通过完整K线、现价、盘口与资金检查' if primary else '尚无股票同时通过盘中触发、实时行情、盘口和风险检查'},
            'premarket':premarket,'candidates':candidates,
            'holdings':{'real':real,'paper':paper,'actionable':actionable[:6]},
            'alerts':market_alerts,'simulation':simulation[market],
            'breadth':dict(self.market_stats,as_of=self.market_stats_time,available=True) if market=='CN' else {'available':False,'message':'美股市场广度尚未接入'},
            'sources':{'primary':'longbridge','supplemental':'lingxi' if market=='CN' else 'ibkr'},
            'strategies':self.registry.list(market,self.strategy_performance(market)),
            'strategy_recommendations':recommendations,
        }

    def replay(self,symbol,source,strategy=None):
        if strategy and strategy not in DEFINITIONS:raise ValueError('未知策略')
        requested_strategy=strategy
        if requested_strategy in ('trend_pullback','volatility_breakout'):
            raise ValueError('该策略需要逐日可见的日线、基准和盘中历史；本地样本尚未完整，不生成可能含未来信息的回放结果')
        bars=[Bar.load(b) for b in self.store.bars(symbol,source)]
        if len(bars)<30:raise ValueError('本地完整K线不足30根，先添加监测并等待数据')
        engine=Strategies();sim=Simulation();v=self.validation.get(symbol,{})
        group=v.get('risk_group','pending')
        # Without point-in-time risk flags, listing status and limit prices, A-share history cannot be certified.
        if symbol_market(symbol)=='CN' and any(b.limit_up is None or b.limit_down is None for b in bars):
            raise ValueError('A股历史缺少当时涨跌停价／风险标记；不使用当前ST状态代替，先积累完整前向记录')
        if not v.get('eligible'):raise ValueError('该标的尚未通过流动性、价格口径和市值核验')
        cut=max(21,int(len(bars)*.7));prev=None
        for i,b in enumerate(bars):
            if prev and not adjacent(prev.start,b.start,symbol_market(symbol)):
                engine.reset(symbol);sim.mark_unobservable(symbol,'回放数据缺口')
            sim.process(b,allow_entries=i>=cut)
            for signal in engine.update(b,group):
                if i>=cut and (not requested_strategy or signal.strategy==requested_strategy):sim.queue(signal)
            prev=b
        result=sim.summary()
        if requested_strategy:
            for market in ['CN','US']:
                result[market]['strategies']={requested_strategy:result[market]['strategies'].get(requested_strategy,{'count':0,'net_pnl':0,'win_rate':None,'payoff':None,'loss_streak':0,'double_cost_pnl':0,'enough':False,'avg_holding':None,'risk_groups':{}})}
        report={'created_at':now().isoformat(),'symbol':symbol,'source':source,'strategy':requested_strategy,'bars':len(bars),'evaluation_bars':len(bars)-cut,
                'label':'当前候选单股历史回放；前70%预热、后30%固定参数检验。不是无偏全市场回测。',
                'result':result,'failures':sim.state['failures'],'version':self.registry.current(requested_strategy,symbol_market(symbol))['version'] if requested_strategy else '实验 1.0.0'}
        self.store.set('last_replay',report)
        return report

    async def screen(self,query):
        data=await self.lingxi.call('screen',query)
        if not data:raise ValueError('灵犀条件选股暂未返回，请稍后重试')
        raw=data.get('text') if isinstance(data,dict) else None
        if not isinstance(raw,str):raw=json.dumps(data,ensure_ascii=False,indent=2)
        codes=re.findall(r'\b(\d{6})\.?(SH|SZ)\b',raw)
        matches=[]
        for code,market in codes:
            symbol=code+'.'+market
            row={'symbol':symbol,'name':self.security_map.get(symbol,{}).get('name',symbol)}
            if self.allowed_security(row) and row not in matches:matches.append(row)
        result={'query':query,'text':raw[:60000],'candidates':matches,'source':'lingxi','received_at':now().isoformat()}
        self.store.set('last_screen',result)
        return result

    def allocate_monitoring(self):
        markets=[m for m in ['CN','US'] if monitoring_window(now(),m)]
        if not markets:return
        held=[s for a in self.sim.state['accounts'].values() for s in a['positions']]
        pending=[s['symbol'] for s in self.sim.state['pending'].values()]
        picks=[r['symbol'] for r in self.selection if r.get('market') in markets and r['decision']=='重点观察']
        fallback=[r['symbol'] for r in self.selection if r.get('market') in markets and r['decision']!='暂不参与']
        desired=list(dict.fromkeys(self.real.symbols()+held+pending+self.manual_watch+picks+fallback+[s for s in self.watch if symbol_market(s) in markets]))[:self.settings['monitor_limit']]
        if desired and desired!=self.watch:
            self.watch=desired;self.store.set('watch',desired);self.first_poll=True

    async def sync_real_holdings(self,sources=('longbridge','ibkr')):
        """Refresh read-only broker positions. Failures retain the last good snapshot."""
        for source in sources:
            provider=self.providers[source]
            try:
                rows=await provider.account_positions()
                synced=self.real.replace_synced(source,rows)
                for row in synced:
                    meta=self.candidate(row['symbol'])
                    meta['name']=row.get('name') or meta.get('name',row['symbol'])
                self.holding_sync[source]={'state':'connected','message':f'已同步 {len(synced)} 只多头股票持仓',
                                           'updated_at':now().isoformat(),'count':len(synced)}
            except Exception as exc:
                current=sum(r.get('source')==source for r in self.real.list())
                self.holding_sync[source]={'state':'waiting','message':'本次同步未完成，保留上次结果' if current else str(exc),
                                           'updated_at':now().isoformat(),'count':current}
        self.store.set('holding_sync',self.holding_sync)
        self.last_holdings_sync=time.monotonic();self.first_poll=True
        self.allocate_monitoring();self.evaluate_decisions();self.broadcast()

    async def holdings_loop(self):
        await asyncio.sleep(3)
        while self.alive:
            try:
                await self.sync_real_holdings(['longbridge'])
                if self.ib.ib and self.ib.ib.isConnected():await self.sync_real_holdings(['ibkr'])
            except asyncio.CancelledError:raise
            except Exception as exc:self.store.event('holdings',{'message':'真实持仓同步未完成','error_type':type(exc).__name__})
            await asyncio.sleep(300)

    def premarket(self,market):
        current=local_date(now(),market);cal=calendar(market)
        try:
            if cal.is_session(str(current)) and phase(now(),market)!='已收盘':target=current
            elif cal.is_session(str(current)):target=cal.next_session(str(current)).date()
            else:target=cal.date_to_session(str(current),direction='next').date()
        except Exception:target=current
        rows=[dict(r) for r in self.selection if r.get('market')==market and r.get('decision')!='暂不参与']
        rows.sort(key=lambda r:({'重点观察':0,'等确认':1}.get(r.get('decision'),2),-r.get('score',0),abs(r.get('distance_to_high_pct',999))))
        result=[]
        for index,row in enumerate(rows[:10],1):
            result.append({**row,'rank':index,'trade_date':str(target),'entry_status':'等待盘中确认',
                           'monitored':row['symbol'] in self.tracked(),
                           'quote_time':self.quotes.get(row['symbol']).market_time.isoformat() if self.quotes.get(row['symbol']) and self.quotes[row['symbol']].market_time else None})
        return result

    def register_signal(self,signal):
        if not signal.time<=now()<signal.time+timedelta(seconds=90):return
        market=symbol_market(signal.symbol)
        if signal.version!=self.registry.active_versions().get((market,signal.strategy)):
            # The isolated legacy continuity harness uses the pre-registry engine.
            # The running dashboard always uses ResearchEngine and never revives old signals.
            if type(self.strategies) is Strategies and signal.version=='实验 1.0.0' and self.store.signal(signal) and self.settings['simulation_enabled']:
                self.sim.queue(signal)
            return
        self.store.signal(signal)

    def evaluate_decisions(self):
        if not hasattr(self,'alerts'):return
        from copy import deepcopy
        t=now();state=deepcopy(self.sim.state)
        raws=self.store.signals()
        names={r['symbol']:r.get('name',r['symbol']) for r in self.candidates}
        real_rows=self.real.list()
        for p in real_rows:
            p['risk_group']=self.validation.get(p['symbol'],{}).get('risk_group',p.get('risk_group','pending'))
            names[p['symbol']]=p.get('name') or names.get(p['symbol'],p['symbol'])
        result=recommend(raws,self.quotes,self.validation,state,t,real_rows,self.registry.active_versions()) if self.monitor_running else []
        for r in result:
            r['name']=names.get(r['symbol'],r['symbol'])
            if r['signal_id'] in self.sim.state['pending']:
                r['simulation_status']='模拟计划已记录，等待下一根K线复核成交'
            elif r['state']=='buy':
                self.alerts.emit('buy:'+r['signal_id'],r['name']+' · 新买点',
                    f"{DEFINITIONS.get(r['strategy'],{}).get('name',r['strategy'])}确认；参考区间 {r['entry_min']:.2f}–{r['entry_max']:.2f}，模拟 {r['qty']} 股；止损 {r['stop']:.2f}，止盈 {r['target']:.2f}。",'buy',r['symbol'],r)
                if self.settings['simulation_enabled']:
                    raw=next(x for x in raws if x['id']==r['signal_id']);signal=Signal.load(raw)
                    signal.evidence={**signal.evidence,'approval':{'time':t.isoformat(),'price':r['price'],'qty':r['qty'],'cash_required':r['cash_required'],
                                        'quote_time':r['quote_time'],'depth_time':r['depth_time']}}
                    self.sim.queue(signal)
        old={r['signal_id']:r for r in self.decisions}
        new={r['signal_id']:r for r in result}
        for key,r in old.items():
            if r['state']=='buy' and (key not in new or new[key]['state'] not in ['buy','pending']):
                self.alerts.emit('invalid:'+key,r['name']+' · 买点失效',new.get(key,{}).get('reason','信号已过期或监测暂停'),'invalid',r['symbol'],new.get(key,r))
        for r in result:
            if (old.get(r['signal_id'],{}).get('state'),old.get(r['signal_id'],{}).get('reason'))!=(r['state'],r['reason']):
                self.store.event('decision',r)
        self.decisions=result
        plans=[]
        for a in self.sim.state['accounts'].values():
            for p in a['positions'].values():
                q=self.quotes.get(p['symbol']);plan=holding_plan(p,q,t,self.validation.get(p['symbol'],{}))
                plan['name']=names.get(p['symbol'],p['symbol']);plans.append(plan)
                if not self.monitor_running:continue
                if plan['event']:
                    self.alerts.emit(f"exit:{p['id']}:{plan['event']}",plan['name']+' · '+plan['action'],
                        f"{plan['reason']}；参考价 {plan['price']:.2f}，计划处理 {plan['qty']} 股。触价提醒不代表已成交。",'exit',p['symbol'],plan)
                    if plan['event'] in ['stop','trailing_stop'] and plan['quote_fresh'] and not p.get('pending_exit'):
                        p['pending_exit']='触价止损已触发，等待可成交K线';self.sim.save()
                # Old last trades are not connection failures. Check the service receipt and feed readiness separately.
                lost=not self.validation.get(p['symbol'],{}).get('ready') or not q or (t-q.received_at).total_seconds()>90
                if is_open(t,symbol_market(p['symbol'])) and lost and time.monotonic()-self.started_at>90:
                    self.alerts.emit(f"gap:{p['id']}:{local_date(t,symbol_market(p['symbol']))}",plan['name']+' · 持仓行情中断',
                                     '持仓监测数据不完整，已暂停新买点；请核对行情连接。','data',p['symbol'],plan)
        self.exit_plans=plans
        real_plans=[]
        for p in real_rows:
            q=self.quotes.get(p['symbol'])
            plan=real_holding_plan(p,q,t,self.validation.get(p['symbol'],{}))
            plan['name']=names.get(p['symbol'],p['symbol']);real_plans.append(plan)
            if not self.monitor_running:continue
            if plan['event']:
                self.alerts.emit(f"real:{p['id']}:{plan['event']}",plan['name']+' · 真实持仓'+plan['action'],
                    f"{plan['reason']}；参考价 {plan['price']:.2f}，持有 {plan['quantity']:g} 股。请自行核对并决定是否下单。",
                    'exit',p['symbol'],plan)
            lost=not q or (t-q.received_at).total_seconds()>90 or not self.validation.get(p['symbol'],{}).get('ready')
            if is_open(t,symbol_market(p['symbol'])) and lost and time.monotonic()-self.started_at>90:
                self.alerts.emit(f"real-gap:{p['id']}:{local_date(t,symbol_market(p['symbol']))}",plan['name']+' · 真实持仓行情中断',
                    '真实持仓行情不完整，已暂停触价判断；请核对行情连接。','data',p['symbol'],plan)
        self.real_plans=real_plans

    async def live_loop(self):
        tick=time.monotonic();next_poll=0.
        while self.alive:
            try:
                current=time.monotonic()
                if current-tick>90:
                    for s in self.tracked():self.suspend(s,'休眠／运行中断，正在补齐行情')
                tick=current
                if self.monitor_running:
                    if current>=next_poll and self.lb.ctx and (not self.live_task or self.live_task.done()):
                        self.live_task=asyncio.create_task(self.refresh_live());self.tasks.append(self.live_task);next_poll=current+15
                    for s in self.tracked():
                        if isinstance(self.strategies,ResearchEngine) and self.validation.get(s,{}).get('ready'):
                            for signal in self.strategies.evaluate(s,self.validation[s].get('risk_group','pending')):self.register_signal(signal)
                            if s in self.details:
                                self.details[s]['preview']=self.strategies.preview(s)
                                self.details[s]['reason']=self.strategies.preview(s).get('reason','等待确认')
                    self.evaluate_decisions();self.close_reports()
                self.tasks=[x for x in self.tasks if not x.done()]
                self.broadcast()
            except asyncio.CancelledError:raise
            except Exception as exc:self.store.event('monitor',{'message':'本轮决策检查未完成','error_type':type(exc).__name__})
            await asyncio.sleep(1)

    async def refresh_live(self):
        active=[s for s in self.tracked() if monitoring_window(now(),symbol_market(s))]
        if not active:return
        try:
            for q in await self.lb.quotes(active):self.accept_quote(q)
            # Books are queried only for active holdings or currently valid technical signals.
            needed=set(s for a in self.sim.state['accounts'].values() for s in a['positions'])
            needed.update(self.real.symbols())
            active_versions=self.registry.active_versions()
            needed.update(r['symbol'] for r in self.store.signals()
                          if r.get('version')==active_versions.get((symbol_market(r['symbol']),r.get('strategy')))
                          and stamp(r['time'])<=now()<stamp(r['time'])+timedelta(minutes=int(r.get('evidence',{}).get('signal_minutes',5))))
            for symbol in [s for s in active if s in needed]:
                try:
                    book=await self.lb.depth(symbol)
                    if book:
                        self.books[symbol]=book
                        q=self.quotes.get(symbol)
                        if q and q.source==book['source']:
                            for k in ['bid','ask','bid_size','ask_size','depth_time']:setattr(q,k,book[k])
                except Exception:pass
            for market in ['CN','US']:
                if is_open(now(),market):
                    try:
                        bars=await self.lb.bars(BENCHMARKS[market])
                        self.strategies.set_benchmark(market,[b for b in bars if b.final and b.end<=now()])
                    except Exception:pass
            self.evaluate_decisions();self.runtime_ok=now().isoformat()
        except Exception as exc:self.store.event('monitor',{'message':'行情轮询暂未完成，等待重试','error_type':type(exc).__name__})

    async def research_loop(self):
        # Let holdings and the small live watch set restore before bulk historical research.
        await asyncio.sleep(20)
        while self.alive:
            try:
                if self.monitor_running and self.lb.ctx:
                    await self.prepare_research()
            except asyncio.CancelledError:raise
            except Exception as exc:self.store.event('research',{'message':'研究数据补齐未完成','error_type':type(exc).__name__})
            await asyncio.sleep(30)

    async def prepare_research(self):
        for market,symbol in BENCHMARKS.items():
            day=local_date(now(),market);key=f'benchmark_daily:{market}:{day}'
            cached=self.store.get(key)
            if not cached:
                try:
                    bars=[b for b in await self.lb.bars(symbol,'day',130) if local_date(b.start,market)<day]
                    if len(bars)<25:continue
                    cached=[b.dump() for b in bars];self.store.set(key,cached)
                except Exception:
                    self.research_progress[market]={'state':'waiting','reason':'基准日线暂不可用'};continue
            self.benchmark_daily[market]=[Bar.load(b) for b in cached]
        for symbol in self.tracked():
            market=symbol_market(symbol);day=local_date(now(),market)
            if self.research_ready.get(symbol)==str(day):continue
            if market not in self.benchmark_daily:continue
            self.research_progress[symbol]={'state':'loading','days':0,'required':14}
            try:
                refs=await self.lb.reference_info([symbol]);meta=self.candidate(symbol)
                if symbol in refs:meta.update(name=refs[symbol]['name'])
                cached=self.store.get('selection_daily:'+symbol,{})
                daily=[Bar.load(b) for b in cached.get('bars',[])] if cached.get('fetched_date')==str(day) and len(cached.get('bars',[]))>=125 else []
                if not daily:
                    daily=[b for b in await self.lb.bars(symbol,'day',130) if local_date(b.start,market)<day]
                    self.store.set('selection_daily:'+symbol,{'fetched_date':str(day),'bars':[b.dump() for b in daily]})
                if symbol in refs and refs[symbol].get('total_shares') and daily:
                    meta['market_cap']=refs[symbol]['total_shares']*daily[-1].close
                    self.validation.setdefault(symbol,{}).pop('daily_checked',None)
                # Current subscription permits recent candles but denies date-history (quota 0).
                # Use recent history first and accumulate immutable per-day caches across sessions.
                batch=await self.lb.bars(symbol,'5m',1000)
                groups={}
                for bar in batch:
                    d=local_date(bar.start,market)
                    if d<day and is_open(bar.start,market):groups.setdefault(d,[]).append(bar)
                for d,items in groups.items():
                    key=f'research_bars:{symbol}:{d}';old=self.store.get(key,[])
                    merged={b['start']:b for b in old}
                    for bar in items:merged.setdefault(bar.start.isoformat(),bar.dump())
                    self.store.set(key,[merged[k] for k in sorted(merged)])
                sessions=list(calendar(market).sessions_in_range(str(day-timedelta(days=45)),str(day-timedelta(days=1))))[-14:]
                history=[];days=0
                for session in sessions:
                    d=session.date();key=f'research_bars:{symbol}:{d}'
                    saved=self.store.get(key)
                    if not saved:
                        self.research_progress[symbol].update(state='waiting',days=days,reason='最近1000根尚不足14日；已缓存，后续交易日自动补齐')
                        continue
                    history.extend(Bar.load(b) for b in saved);days+=1
                    self.research_progress[symbol].update(days=days)
                self.broadcast()
                self.strategies.prepare(symbol,daily,history,self.benchmark_daily[market])
                self.research_ready[symbol]=str(day)
                f=self.strategies.context[symbol]['daily']
                self.research_progress[symbol]={'state':'ready' if days==14 and f.get('eligible') else 'filtered' if not f.get('eligible') else 'waiting','days':days,'required':14,
                    'reason':f['reason'] if days==14 else '最近历史不足14日；已缓存，后续交易日自动补齐'}
                self.first_poll=True
            except Exception as exc:
                self.research_progress[symbol]={'state':'waiting','reason':'完整历史或成交量口径待核验','error_type':type(exc).__name__}
        # Re-evaluate cached candidate daily data once benchmark data becomes available.
        for r in self.selection:
            m=symbol_market(r['symbol']);cached=self.store.get('selection_daily:'+r['symbol'],{})
            if cached.get('bars') and m in self.benchmark_daily:
                f=daily_factors(r['symbol'],[Bar.load(b) for b in cached['bars']],self.benchmark_daily[m])
                if len(cached['bars'])>=65:
                    refreshed=evaluate_selection(self.candidate(r['symbol']),[Bar.load(b) for b in cached['bars']])
                    r.update(refreshed)
                    if (not f.get('eligible') or not r.get('name_verified')) and r['decision']=='重点观察':
                        r.update(decision='等确认',reason=f['reason'] if not f.get('eligible') else '证券名称和风险标记待核验')
                r.update(relative_strength=f.get('relative_strength'),research_reason=f['reason'])
        self.selection.sort(key=lambda r:({'重点观察':0,'等确认':1,'暂不参与':2}[r['decision']],-r['score'],abs(r['distance_to_high_pct'])))
        self.store.set('selection',self.selection)
        self.store.set('selection_updated',now().isoformat())
        self.allocate_monitoring()

    def close_reports(self):
        t=now()
        for market in ['CN','US']:
            d=local_date(t,market)
            try:
                if not calendar(market).is_session(str(d)) or t<close_time(d,market)+timedelta(minutes=2):continue
            except Exception:continue
            key=f'close_report:{market}:{d}'
            if self.store.get(key):continue
            account=self.sim.state['accounts'][market]
            events=[a for a in self.alerts.list(300) if a.get('symbol') and symbol_market(a['symbol'])==market and local_date(stamp(a['time']),market)==d]
            report={'market':market,'date':str(d),'generated_at':t.isoformat(),'alerts':len(events),
                    'closed_trades':sum(local_date(stamp(p['exit_time']),market)==d for p in account['trades']),
                    'open_positions':len(account['positions']),'failures':sum(symbol_market(f['symbol'])==market and local_date(stamp(f['time']),market)==d for f in self.sim.state['failures']),
                    'message':'规则模板自动整理；未调用AI','risks':[p for p in self.exit_plans if symbol_market(p['symbol'])==market]}
            self.store.set(key,report);self.store.set('daily_reports',([report]+self.store.get('daily_reports',[]))[:60])
