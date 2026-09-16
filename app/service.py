import asyncio,json,time,re,statistics
from uuid import uuid4
from pathlib import Path
from datetime import timedelta
from .models import now,stamp,symbol_market,in_scope,Bar,Signal
from .providers import ROOT,Lingxi,Longbridge,IBKR,number,canonical
from .calendars import is_open,local_date,adjacent,price_limits,phase
from .strategy import Strategies
from .research import ResearchEngine,VERSION,BENCHMARKS,daily_factors
from .decisions import recommend,shadow_recommend,holding_plan,fresh_quote
from .alerts import Alerts
from .calendars import calendar,monitoring_window,close_time
from .simulation import Simulation
from .store import Store
from .selection import evaluate as evaluate_selection,candidate_pool,ranking_key as selection_rank,diversified_rank
from .holdings import RealHoldings,real_holding_plan
from .strategy_registry import StrategyRegistry,DEFINITIONS
from .gpt_handoff import build_context
from .gpt_workspace import (build_package_prompt,extract_result,resolve_mode,response_digest,
                            validate_result,valid_chatgpt_url,LONGBRIDGE_APP_URL)

DAILY_FETCH_COUNT=270
DAILY_CACHE_MIN=260

STRATEGY_SCREEN_GROUPS=(
    {'id':'breakout','strategies':['breakout','pullback'],'name':'突破准备',
     'query':'A股全市场的主板和创业板中，20日平均成交额大于1亿元，20日均线向上，最新价高于20日均线且距离20日最高价不超过5%的股票，按20日涨幅从高到低排序，最多返回40只'},
    {'id':'trend_pullback','strategies':['trend_pullback'],'name':'趋势回踩',
     'query':'A股全市场的主板和创业板中，20日平均成交额大于1亿元，最新价高于60日均线，20日均线高于60日均线且20日均线向上，最新价距离20日均线不超过2%的股票，按20日涨幅从高到低排序，最多返回40只'},
    {'id':'trend_rsi_pullback','strategies':['trend_rsi_pullback'],'name':'RSI回踩',
     'query':'A股全市场的主板和创业板中，20日平均成交额大于1亿元，最新价高于60日均线，20日均线高于60日均线且向上，最近5日RSI14最大值大于等于65，当前RSI14在45到60之间，最新价距离20日均线不超过2%，5日平均成交量小于20日平均成交量的80%，按20日涨幅从高到低排序，最多返回40只'},
    {'id':'volatility_contraction','strategies':['volatility_breakout','vcp_swing'],'name':'波动收缩',
     'query':'A股全市场的主板和创业板中，20日平均成交额大于1亿元，EMA10高于EMA20且EMA20高于EMA50，EMA50向上，ATR5小于ATR20的75%，最新价距离60日最高价不超过5%，最近5日平均振幅小于此前20日平均振幅的75%，按60日涨幅从高到低排序，最多返回40只'},
)

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
        saved_access=self.store.get('longbridge_access',{})
        if saved_access:
            self.lb.status.update({k:v for k,v in saved_access.items() if k in ['market_access','packages','verified_at']})
        self.registry=StrategyRegistry(self.store);self.strategies=ResearchEngine(self.registry);self.sim=Simulation(self.store)
        self.real=RealHoldings(self.store)
        self.alerts=Alerts(self.store,root);self.books={};self.decisions=[];self.exit_plans=[];self.real_plans=[]
        self.research_ready={};self.research_progress={};self.benchmark_daily={};self.last_live=0.;self.last_research=0.
        self.last_decision_eval=0.
        self.started_at=time.monotonic()
        self.monitor_running=self.store.get("monitor_running",True);self.runtime_ok=None
        self.manual_watch=self.store.get("manual_watch",[]);self.live_task=None;self.access_task=None;self.access_pending=False;self.ib_connect_task=None
        self.candidates=self.store.get('candidates',[]);self.quotes={};self.comparisons={};self.details={}
        self.settings=self.store.get('settings',{'monitor_limit':12,'simulation_enabled':True,'ib_port':7497})
        self.watch=self.store.get('watch',[]);self.scanning=False;self.scan_task=None;self.last_scan=self.store.get('last_scan')
        self.daily_core=self.store.get('daily_candidate_core',{})
        core_trimmed=False
        for market,core in self.daily_core.items():
            if len(core.get('rows',[]))>80:
                self.daily_core[market]={**core,'rows':self.limit_core_rows(core['rows'])};core_trimmed=True
        if core_trimmed:self.store.set('daily_candidate_core',self.daily_core)
        self.market_stats=self.store.get('market_stats',{});self.market_stats_time=self.store.get('market_stats_time')
        self.tasks=[];self.alive=True;self.listeners=set();self.fetch_lock=asyncio.Lock();self.last_cycle=time.monotonic()
        self.last_bars_cycle=0.;self.last_quote_cycle=0.;self.last_scan_cycle=0.;self.validation={};self.first_poll=True
        self.lb.on_quote=self.accept_quote;self.lb.on_bar=self.accept_bar
        self.sdk_checked=None
        self.ib_auto_connect=self.store.get('ib_auto_connect',False);self.last_ib_attempt=0.
        self.selection=sorted(self.store.get('selection',[]),key=selection_rank);self.selection_running=False
        self.selection_progress={'done':0,'total':0};self.selection_errors=[];self.selection_task=None
        self.holding_sync=self.store.get('holding_sync',{
            'longbridge':{'state':'waiting','message':'等待首次同步'},
            'ibkr':{'state':'waiting','message':'等待本地接口连接'}})
        self.account_summaries=self.store.get('account_summaries',{})
        self.gpt_packages=self.store.get('gpt_packages',[])
        self.gpt_runs=self.store.get('gpt_runs',[])
        self.gpt_conversations=self.store.get('gpt_conversations',{})
        self.cleanup_gpt_data()
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
        if self.store.get('engine_version')!=VERSION:
            for sid,raw in list(self.sim.state['pending'].items()):
                self.sim.fail(raw['symbol'],'升级新版，取消旧待买信号',now());del self.sim.state['pending'][sid]
            self.sim.save();self.store.set('engine_version',VERSION)
        self.tasks=[asyncio.create_task(self.lb.connect_cached()),asyncio.create_task(self.run()),asyncio.create_task(self.live_loop()),
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
                self.cleanup_gpt_data()
                if tick-self.last_cycle>90:
                    for s in self.tracked():self.suspend(s,'运行中断／休眠，重新补齐数据')
                    self.first_poll=True
                self.last_cycle=tick
                active=any(is_open(now(),m) for m in ['CN','US'])
                quote_window=active or phase(now(),'CN')=='集合竞价'
                preparing=[m for m in ['CN','US'] if monitoring_window(now(),m)]
                # Rotate automatic candidates into the shared monitoring capacity as soon as
                # a market enters its preparation window. Protected holdings/manual watches
                # keep their priority; this avoids waiting for a long daily-selection pass.
                if preparing:self.allocate_monitoring()
                if self.ib_auto_connect and tick-self.last_ib_attempt>30 and (not self.ib.ib or not self.ib.ib.isConnected()):
                    self.last_ib_attempt=tick
                    if not self.ib_connect_task or self.ib_connect_task.done():
                        self.ib_connect_task=asyncio.create_task(self.connect_ib_available())
                        self.tasks.append(self.ib_connect_task)
                if self.lb.ctx and self.sdk_checked is not self.lb.ctx:
                    self.sdk_checked=self.lb.ctx
                    self.access_pending=False;self.first_poll=True
                    if not self.access_task or self.access_task.done():
                        self.access_task=asyncio.create_task(self.verify_longbridge_access())
                        self.tasks.append(self.access_task)
                if self.first_poll:
                    marker=self.store.get('premarket_scan_marker',{})
                    due=[m for m in ['CN','US'] if marker.get(m)!=self.candidate_trade_date(m)
                         or self.daily_core.get(m,{}).get('trade_date')!=self.candidate_trade_date(m)
                         or self.daily_core.get(m,{}).get('stale')]
                    if due and not self.scanning:
                        task=asyncio.create_task(self.run_premarket_scan(due));self.tasks.append(task)
                    elif not self.scanning:self.request_scan()
                    self.last_scan_cycle=time.monotonic()
                elif active and tick-self.last_scan_cycle>300:
                    await self.request_scan();self.last_scan_cycle=time.monotonic()
                elif preparing:
                    marker=self.store.get('premarket_scan_marker',{})
                    due=[m for m in preparing if marker.get(m)!=self.candidate_trade_date(m)]
                    if due and not self.scanning:
                        task=asyncio.create_task(self.run_premarket_scan(due));self.tasks.append(task)
                if self.first_poll or any(s not in self.details for s in self.tracked()) or (active and tick-self.last_bars_cycle>60):
                    await self.refresh_bars();self.last_bars_cycle=time.monotonic()
                    if self.lb.ctx:
                        ok=await self.lb.healthcheck()
                        if not ok:
                            for s in self.tracked():self.suspend(s,'长桥连接失联，等待补齐行情')
                self.first_poll=False;self.runtime_ok=now().isoformat();self.store.set('service_heartbeat',self.runtime_ok);self.broadcast()
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.store.event('system',{'message':'数据刷新未完成，已暂停本轮新信号','error_type':type(exc).__name__})
            self.last_cycle=time.monotonic()
            await asyncio.sleep(5)

    def candidate_trade_date(self,market):
        current=local_date(now(),market);cal=calendar(market)
        try:
            if cal.is_session(str(current)) and phase(now(),market)!='已收盘':return str(current)
            if cal.is_session(str(current)):return str(cal.next_session(str(current)).date())
            return str(cal.date_to_session(str(current),direction='next').date())
        except Exception:return str(current)

    def request_scan(self,force_strategy=False,rebuild_markets=None,wait_selection=False):
        """Coalesce callers onto one running provider scan."""
        if self.scan_task and not self.scan_task.done():return self.scan_task
        self.scan_task=asyncio.create_task(self.scan(force_strategy,rebuild_markets,wait_selection))
        self.tasks.append(self.scan_task)
        return self.scan_task

    async def run_premarket_scan(self,markets):
        """Rebuild due market cores and mark completion only after deep validation."""
        markets=list(dict.fromkeys(markets))
        await self.request_scan(force_strategy='CN' in markets,rebuild_markets=markets,wait_selection=True)
        marker=self.store.get('premarket_scan_marker',{})
        for market in markets:
            core=self.daily_core.get(market,{})
            if core.get('trade_date')==self.candidate_trade_date(market) and not core.get('stale'):
                marker[market]=core['trade_date']
        self.store.set('premarket_scan_marker',marker)
        self.broadcast()

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

    async def verify_longbridge_access(self):
        access=await self.lb.verify_access()
        if access:
            self.store.set('longbridge_access',{
                'market_access':access,'packages':self.lb.status.get('packages',[]),
                'verified_at':self.lb.status.get('verified_at')})
        self.evaluate_decisions();self.broadcast()
        return access

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

    async def strategy_candidate_scan(self,force=False):
        marker=str(local_date(now(),'CN'))
        cached=self.store.get('strategy_candidate_screen',{})
        if not force and cached.get('date')==marker:return cached.get('rows',[]),cached
        rows=[];queries=[];errors=[]
        for group in STRATEGY_SCREEN_GROUPS:
            active=[s for s in group['strategies'] if self.registry.enabled(s,'CN')]
            if not active:continue
            try:
                data=await self.lingxi.call('screen',group['query'])
                raw=data.get('text') if isinstance(data,dict) else None
                if not isinstance(raw,str):raw=json.dumps(data,ensure_ascii=False)
                found=[]
                for code,market in re.findall(r'\b(\d{6})\.?((?:SH|SZ))\b',raw):
                    symbol=code+'.'+market
                    row={'symbol':symbol,'name':self.security_map.get(symbol,{}).get('name',symbol)}
                    if self.allowed_security(row) and symbol not in found:found.append(symbol)
                for symbol in found:
                    rows.append({'symbol':symbol,'name':self.security_map.get(symbol,{}).get('name',symbol),
                        'source':'lingxi','reason':'策略初筛：'+group['name'],'scope':'灵犀按策略条件筛选A股全市场',
                        'candidate_origin':'strategy','candidate_strategies':active,'screen_group':group['id']})
                queries.append({'group':group['id'],'name':group['name'],'strategies':active,'returned':len(found)})
            except Exception as exc:
                errors.append({'group':group['id'],'name':group['name'],'reason':str(exc)[:120]})
        unique={}
        for row in rows:
            current=unique.get(row['symbol'])
            if current:
                current['candidate_strategies']=list(dict.fromkeys(current['candidate_strategies']+row['candidate_strategies']))
                current['reason']='策略初筛：'+'、'.join(DEFINITIONS[s]['name'] for s in current['candidate_strategies'])
            else:unique[row['symbol']]=row
        result={'date':marker,'updated_at':now().isoformat(),'rows':list(unique.values()),'queries':queries,'errors':errors}
        self.store.set('strategy_candidate_screen',result)
        return result['rows'],result

    @staticmethod
    def merge_candidate(unique,row):
        current=unique.get(row['symbol'])
        if not current:unique[row['symbol']]=row;return
        strategies=list(dict.fromkeys(current.get('candidate_strategies',[])+row.get('candidate_strategies',[])))
        if strategies:
            current['candidate_strategies']=strategies
            current['candidate_origin']='strategy'
            current['reason']='策略初筛：'+'、'.join(DEFINITIONS[s]['name'] for s in strategies)
        for key,value in row.items():
            if current.get(key) is None and value is not None:current[key]=value

    @staticmethod
    def longbridge_candidates(payload,role,generated_at,trade_date):
        items=payload.get('items',[]) if isinstance(payload,dict) else []
        rows=[]
        for raw in items:
            if not isinstance(raw,dict) or not raw.get('symbol'):continue
            rows.append({'symbol':str(raw['symbol']).upper(),'name':raw.get('name') or raw['symbol'],
                'industry':str(raw.get('industry') or '').strip() or None,'price':number(raw.get('prevclose')),
                'change_pct':number(raw.get('prevchg')),'market_cap':number(raw.get('marketcap')),
                'source':'longbridge','reason':'长桥全市场活跃候选 · 待本地日线和流动性核验',
                'market_time':None,'scope':'长桥全市场筛选返回最多80只','candidate_origin':'market_screener',
                'candidate_strategies':[],'pool_role':role,'candidate_date':trade_date,
                'candidate_updated_at':generated_at})
        return rows

    def limit_core_rows(self,rows,limit=80):
        """Merge duplicate sources, keep strategy names first, and enforce the research-pool cap."""
        unique={}
        for row in rows:self.merge_candidate(unique,row)
        return candidate_pool(list(unique.values()),[],set(),limit)

    async def scan(self,force_strategy=False,rebuild_markets=None,wait_selection=False):
        if self.scanning:return set()
        self.scanning=True;self.broadcast()
        rebuilt=set();scan_time=now().isoformat()
        try:
            requested=set(rebuild_markets or ())
            if force_strategy and rebuild_markets is None:requested={'CN','US'}
            for market in ('CN','US'):
                core=self.daily_core.get(market,{})
                if core.get('trade_date')!=self.candidate_trade_date(market):requested.add(market)
            rows=[]
            if 'CN' in requested:
                try:
                    strategy_rows,status=await self.strategy_candidate_scan(True)
                    core_rows=[]
                    for row in strategy_rows:
                        core_rows.append({**row,'pool_role':'daily_core','candidate_date':self.candidate_trade_date('CN'),
                                          'candidate_updated_at':scan_time})
                    longbridge_ok=False
                    try:
                        cn=await self.lb.cli_scan('CN')
                        core_rows.extend(self.longbridge_candidates(cn,'daily_core',scan_time,self.candidate_trade_date('CN')))
                        longbridge_ok=True
                    except Exception:pass
                    if not status.get('queries') and not longbridge_ok:raise RuntimeError('A股每日核心池未返回可验证结果')
                    core_rows=self.limit_core_rows(core_rows)
                    self.daily_core['CN']={'trade_date':self.candidate_trade_date('CN'),'updated_at':scan_time,
                                           'rows':core_rows,'stale':False,'errors':status.get('errors',[])}
                    rebuilt.add('CN')
                except Exception:
                    if self.daily_core.get('CN'):
                        self.daily_core['CN']={**self.daily_core['CN'],'stale':True,'error':'A股每日核心池刷新失败'}
            rows.extend(self.daily_core.get('CN',{}).get('rows',[]))
            if is_open(now(),'CN') or phase(now(),'CN')=='集合竞价':
                for order in [10,2]:
                    try:found,stats=await self.lingxi.rank(order)
                    except Exception:found,stats=[],{}
                    for row in found:
                        row.update(candidate_origin='rank_supplement',candidate_strategies=[],pool_role='intraday_supplement',
                                   candidate_date=self.candidate_trade_date('CN'),candidate_updated_at=scan_time)
                    rows.extend(found)
                    if stats:self.market_stats=stats
                    if found:self.market_stats_time=found[0].get('market_time')
            if 'US' in requested:
                try:
                    us=await self.lb.cli_scan('US')
                    core_rows=self.limit_core_rows(
                        self.longbridge_candidates(us,'daily_core',scan_time,self.candidate_trade_date('US')))
                    self.daily_core['US']={'trade_date':self.candidate_trade_date('US'),'updated_at':scan_time,
                                           'rows':core_rows,'stale':False,'errors':[]}
                    rebuilt.add('US')
                except Exception:
                    if self.daily_core.get('US'):
                        self.daily_core['US']={**self.daily_core['US'],'stale':True,'error':'美股每日核心池刷新失败'}
                    if not self.lb.ctx:self.lb.status.update(message='持续行情待授权；美股筛选接口本次未返回')
            rows.extend(self.daily_core.get('US',{}).get('rows',[]))
            if 'US' not in requested and is_open(now(),'US'):
                try:
                    us=await self.lb.cli_scan('US')
                    rows.extend(self.longbridge_candidates(us,'intraday_supplement',scan_time,self.candidate_trade_date('US')))
                except Exception:
                    if not self.lb.ctx:self.lb.status.update(message='持续行情待授权；美股盘中补充本次未返回')
            self.store.set('daily_candidate_core',self.daily_core)
            unique={}
            for r in rows:
                if not self.allowed_security(r):continue
                group='st' if self.is_st(r['symbol'],r['name']) else 'pending'
                r['risk_group']=group;r['market']=symbol_market(r['symbol'])
                r['eligible']=False;r['eligibility_reason']='等待日线、流动性与交易状态核验'
                self.merge_candidate(unique,r)
            if unique or rebuilt:
                if requested:
                    saved_pool=self.store.get('selection_pool',{})
                    if requested=={'CN','US'}:self.store.set('selection_pool',{})
                    else:
                        kept=[r for r in saved_pool.get('rows',[]) if symbol_market(r['symbol']) not in requested]
                        dates={k:v for k,v in saved_pool.get('dates',{}).items() if k not in requested}
                        self.store.set('selection_pool',{'dates':dates,'rows':kept})
                    current_symbols=set(unique)
                    self.selection=[r for r in self.selection if r.get('market') not in requested or r.get('symbol') in current_symbols]
                    self.store.event('selection',{'message':'已重新运行全市场策略筛选，盘前候选池不沿用上一轮结果'})
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
                if self.selection_task and not self.selection_task.done():await self.selection_task
                selection_task=self.schedule_selection()
                if wait_selection and selection_task:await selection_task
            return rebuilt
        finally:self.scanning=False;self.broadcast()

    def schedule_selection(self):
        if self.selection_task and not self.selection_task.done():return self.selection_task
        self.selection_task=asyncio.create_task(self.refresh_selection())
        self.tasks.append(self.selection_task)
        return self.selection_task

    async def refresh_selection(self):
        self.selection_running=True;self.selection_errors=[]
        saved=self.store.get('selection_pool',{})
        saved_dates=saved.get('dates',{})
        if not saved_dates and saved.get('date'):saved_dates={'CN':saved['date']}
        retained=[r for r in saved.get('rows',[]) if saved_dates.get(symbol_market(r['symbol']))==self.candidate_trade_date(symbol_market(r['symbol']))]
        # Keep tracked names and a small continuity set, while leaving most slots for today's strategy screen.
        continuity=[r['symbol'] for r in self.selection if r['decision']=='重点观察'][:8]
        preferred=set(continuity) | set(self.tracked())
        rows=[]
        for market in ('CN','US'):
            rows+=candidate_pool([r for r in self.candidates if r.get('market')==market],
                                 [r for r in retained if symbol_market(r['symbol'])==market],preferred,80)
        existing={r['symbol'] for r in self.candidates}
        self.candidates.extend(r for r in rows if r['symbol'] not in existing)
        self.selection_progress={'done':0,'total':len(rows)};self.broadcast()
        previous={r['symbol']:r for r in self.selection}
        results=[]
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
                    if cached and cached.get('fetched_date')==str(local_date(now(),symbol_market(row['symbol']))) and len(cached.get('bars',[]))>=DAILY_CACHE_MIN:
                        daily=[Bar.load(b) for b in cached['bars']]
                    else:
                        daily=await self.lb.bars(row['symbol'],'day',DAILY_FETCH_COUNT,force_cli=True)
                        daily=[b for b in daily if local_date(b.start,symbol_market(row['symbol']))<local_date(now(),symbol_market(row['symbol']))]
                        self.store.set(key,{'fetched_date':str(local_date(now(),symbol_market(row['symbol']))),'bars':[b.dump() for b in daily]})
                    if reference and reference.get('total_shares') and daily:row['market_cap']=daily[-1].close*reference['total_shares']
                    item=evaluate_selection(row,daily)
                    item.update(candidate_origin=row.get('candidate_origin','retained'),candidate_strategies=row.get('candidate_strategies',[]),
                                pool_role=row.get('pool_role','daily_core'),candidate_date=row.get('candidate_date'),
                                candidate_updated_at=row.get('candidate_updated_at'))
                    f=daily_factors(row['symbol'],daily,self.benchmark_daily.get(symbol_market(row['symbol']),[]))
                    item.update(relative_strength=f.get('relative_strength'),research_reason=f['reason'])
                    if not f.get('eligible') and item['decision']=='重点观察':item.update(decision='等确认',reason=f['reason'])
                    item['name_verified']=bool(reference)
                    if not reference and item['decision']=='重点观察':
                        item['decision']='等确认';item['reason']='证券名称和当前风险标记待核验，暂不进入重点监测'
                    results.append(item)
                except Exception:
                    self.selection_errors.append({'symbol':row['symbol'],'reason':'完整日线或成交量口径未通过核验'})
                self.selection_progress['done']+=1
                if self.selection_progress['done']%10==0 or self.selection_progress['done']==self.selection_progress['total']:self.broadcast()
                await asyncio.sleep(.1)
            for market in ['CN','US']:
                ranked=sorted([r for r in results if r.get('market')==market and r.get('relative_strength') is not None],key=lambda r:r['relative_strength'])
                coverage=len(ranked)
                for index,item in enumerate(ranked):
                    item['rs_percentile']=100. if coverage==1 else round(index/(coverage-1)*100,1)
                    item['rs_rank_coverage']=coverage
                    self.strategies.set_relative_rank(item['symbol'],item['rs_percentile'],coverage)
            attempted={symbol_market(r['symbol']) for r in rows}
            succeeded={r['market'] for r in results}
            failed=attempted-succeeded
            for market in failed:
                if self.daily_core.get(market):
                    self.daily_core[market]={**self.daily_core[market],'stale':True,'error':'每日候选未通过本轮日线数据核验'}
            if failed:
                results.extend(r for r in previous.values() if r.get('market') in failed)
                self.store.set('daily_candidate_core',self.daily_core)
            self.selection=sorted(results,key=selection_rank)
            updated=now().isoformat()
            self.store.set('selection',self.selection);self.store.set('selection_updated',updated)
            updated_by_market=self.store.get('selection_updated_by_market',{})
            updated_by_market.update({market:updated for market in succeeded})
            self.store.set('selection_updated_by_market',updated_by_market)
            by_symbol={r['symbol']:r for r in rows}
            pool_rows=[by_symbol[r['symbol']] for r in self.selection if r['symbol'] in by_symbol and r.get('market') not in failed]
            pool_rows.extend(r for r in saved.get('rows',[]) if symbol_market(r['symbol']) in failed)
            pool_dates={m:self.candidate_trade_date(m) for m in ('CN','US') if m not in failed}
            pool_dates.update({m:d for m,d in saved_dates.items() if m in failed})
            self.store.set('selection_pool',{'dates':pool_dates,'updated_at':updated,'rows':pool_rows})
            self.allocate_monitoring()
        finally:self.selection_running=False;self.broadcast()

    async def prioritize_selection(self,market='CN'):
        if market not in ['CN','US','ALL']:raise ValueError('未知市场')
        preferred=[r['symbol'] for r in self.selection if r['decision']=='重点观察' and (market=='ALL' or r['market']==market)][:8]
        if not preferred:raise ValueError('尚无通过趋势和流动性核验的重点候选')
        held=[s for a in self.sim.state['accounts'].values() for s in a['positions']]
        self.store.set('watch_before_priority',self.watch)
        existing=[s for s in self.watch if market=='ALL' or symbol_market(s)==market]
        self.watch=list(dict.fromkeys(self.real.symbols()+held+self.manual_watch+self.active_gpt_symbols()+preferred+existing))[:self.settings['monitor_limit']]
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
            if len(self.tracked())>=self.settings['monitor_limit']:
                names='、'.join(self.tracked())
                raise ValueError(f"监测名额已满（{len(self.tracked())}/{self.settings['monitor_limit']}）：{names}。请先移除一只或增加名额")
            self.watch.append(symbol);self.store.set('watch',self.watch)
            self.manual_watch=list(dict.fromkeys(self.manual_watch+[symbol]));self.store.set('manual_watch',self.manual_watch)
        quotes=[]
        if self.lb.ctx:
            try:quotes=await self.lb.quotes([symbol])
            except Exception:quotes=[]
        if not quotes and symbol_market(symbol)=='CN':
            try:quotes=await asyncio.wait_for(self.lingxi.quotes([symbol]),5)
            except Exception:quotes=[]
        for item in quotes:self.accept_quote(item)
        await self.refresh_bars([symbol]);self.broadcast()
        monitored=symbol in self.tracked();subscribed=symbol in self.lb.subscribed
        return {'ok':True,'symbol':symbol,'monitored':monitored,
                'warm':bool(self.validation.get(symbol,{}).get('ready')),
                'transport':'push' if subscribed and self.lb.status.get('stream') else 'polling' if monitored and self.lb.ctx else 'unavailable',
                'capacity':{'used':len(self.tracked()),'limit':self.settings['monitor_limit'],
                            'available':max(0,self.settings['monitor_limit']-len(self.tracked())),
                            'symbols':self.tracked()}}

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
        tick=time.monotonic()
        if tick-self.last_decision_eval>=1:
            self.evaluate_decisions();self.last_decision_eval=tick

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
        tick=time.monotonic()
        if tick-self.last_decision_eval>=1:
            self.evaluate_decisions();self.last_decision_eval=tick

    async def refresh_bars(self,symbols=None):
        async with self.fetch_lock:
            for symbol in symbols or self.tracked():
                try:
                    meta=self.candidate(symbol);v=self.validation.setdefault(symbol,{})
                    if v.get('daily_checked')!=str(local_date(now(),symbol_market(symbol))):
                        cache_key='selection_daily:'+symbol
                        cached=self.store.get(cache_key,{})
                        if cached.get('fetched_date')==str(local_date(now(),symbol_market(symbol))) and len(cached.get('bars',[]))>=DAILY_CACHE_MIN:
                            days=[Bar.load(b) for b in cached['bars']]
                        else:
                            days=await self.lb.bars(symbol,'day',DAILY_FETCH_COUNT)
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
        research_pool=self.store.get('selection_pool',{}).get('rows',[])
        strategy_screen=self.store.get('strategy_candidate_screen',{})
        workspaces={m:self.market_workspace(m,rows,simulation,alerts) for m in ['CN','US']}
        gpt={m:self.gpt_list_runs(m) for m in ['CN','US']}
        return {'time':now().isoformat(),'version':VERSION,'workspace_version':'工作台 4.2','settings':self.settings,
                'decisions':self.decisions,'exit_plans':self.exit_plans,'real_holdings':self.real.list(),'real_plans':self.real_plans,
                'holding_sync':self.holding_sync,'premarket':{m:self.premarket(m) for m in ['CN','US']},'alerts':alerts,
                'gpt':gpt,
                'notification_settings':self.alerts.status(),'monitor':{'running':self.monitor_running,'last_ok':self.runtime_ok,'ai_calls':0,'research':self.research_progress,
                    'window':{m:monitoring_window(now(),m) for m in ['CN','US']},'uncovered':max(0,len(self.selection)-len(tracked)),
                    'uncovered_holdings':sum(s not in tracked for s in real_symbols)},
                'daily_reports':self.store.get('daily_reports',[])[:10],
                'markets':{m:{'open':is_open(now(),m),'phase':phase(now(),m)} for m in ['CN','US']},
                'calendar_valid_until':'2026-12-31','candidates':list(rows.values()),'watch':self.tracked(),
                'coverage':{'returned_candidates':len(self.candidates),
                    'candidates_by_market':{m:sum(symbol_market(r['symbol'])==m for r in self.candidates) for m in ['CN','US']},
                    'strategy_candidates_by_market':{m:sum(symbol_market(r['symbol'])==m and bool(r.get('candidate_strategies')) for r in self.candidates) for m in ['CN','US']},
                    'supplement_candidates_by_market':{m:sum(symbol_market(r['symbol'])==m and not r.get('candidate_strategies') for r in self.candidates) for m in ['CN','US']},
                    'research_pool_by_market':{m:sum(symbol_market(r['symbol'])==m for r in research_pool) for m in ['CN','US']},
                    'selection_by_market':{m:sum(r.get('market')==m for r in self.selection) for m in ['CN','US']},
                    'pool_status_by_market':{m:{'trade_date':self.daily_core.get(m,{}).get('trade_date'),
                        'updated_at':self.daily_core.get(m,{}).get('updated_at'),'stale':bool(self.daily_core.get(m,{}).get('stale')),
                        'core_count':len(self.daily_core.get(m,{}).get('rows',[])),
                        'supplement_count':sum(r.get('market')==m and r.get('pool_role')=='intraday_supplement' for r in self.candidates),
                        'error':self.daily_core.get(m,{}).get('error')} for m in ['CN','US']},
                    'monitored':len(self.tracked()),'ready':sum(bool(v.get('ready')) for s,v in self.validation.items() if s in self.tracked()),
                    'last_scan':self.last_scan,'full_market':False,'strategy_universe':'A股全市场','scope_label':'灵犀全市场策略初筛，本地仅复核返回候选',
                    'strategy_screen':{k:strategy_screen.get(k) for k in ['updated_at','queries','errors']}},
                'providers':{k:dict(v.status,auth_url=self.lb.auth_url if k=='longbridge' else None) for k,v in self.providers.items()},
                'scanning':self.scanning,'market_stats':self.market_stats,'market_stats_time':self.market_stats_time,
                'selection':self.selection,'selection_running':self.selection_running,'selection_progress':self.selection_progress,
                'selection_errors':self.selection_errors,'selection_updated':self.store.get('selection_updated'),
                'signals':self.store.signals(),'simulation':simulation,'failures':self.sim.state['failures'][-60:],
                'pending':list(self.sim.state['pending'].values()),'events':self.store.events(),'benchmarks':self.store.benchmarks(),
                'workspaces':workspaces}

    def strategy_performance(self,market):
        result={}
        for strategy,definition in DEFINITIONS.items():
            if market not in definition['markets']:continue
            version=self.registry.current(strategy,market)['version']
            result[strategy]=self.sim.performance(market,strategy,version)
        return result

    def strategy_performance_detail(self,market,strategy,version=None):
        if strategy not in DEFINITIONS or market not in DEFINITIONS[strategy]['markets']:raise ValueError('未知策略或市场')
        revision=self.registry.current(strategy,market) if version is None else next((r for r in self.registry.revisions[market][strategy] if r['version']==version),None)
        if not revision:raise ValueError('找不到策略版本')
        performance=self.sim.performance(market,strategy,revision['version'])
        return {'market':market,'strategy':strategy,'name':DEFINITIONS[strategy]['name'],'version':revision['version'],
                'horizon':DEFINITIONS[strategy]['horizon'],'forward_shadow':performance['shadow'],
                'portfolio':performance['portfolio'],'historical':revision.get('replay') or {
                    'state':'insufficient_data','label':'历史探索结果','message':'本地逐时点历史尚不完整，未生成收益结果'},
                'interpretation':'前向影子模拟评价策略本身；组合模拟包含资金与仓位竞争。'}

    def strategy_recommendations(self,market,rows=None,decisions=None,premarket=None,simulation=None):
        rows=rows or {r['symbol']:dict(r) for r in self.candidates}
        decisions=decisions if decisions is not None else [r for r in self.decisions if symbol_market(r['symbol'])==market]
        premarket=premarket if premarket is not None else self.premarket(market)
        output={}
        for strategy,definition in DEFINITIONS.items():
            if market not in definition['markets']:continue
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
                elif strategy=='trend_rsi_pullback':
                    rsi=setup.get('rsi14');contraction=setup.get('volume_contraction')
                    conditions.update({'MA20高于MA60':bool(setup.get('ma20') and setup.get('ma20')>setup.get('ma60',float('inf')) and setup.get('close',0)>setup.get('ma60',float('inf'))),
                                       '近期RSI强势':setup.get('rsi_recent_peak') is not None and setup['rsi_recent_peak']>=config['rsi_peak_min'],
                                       '靠近MA20':abs((setup.get('close') or 0)/(setup.get('ma20') or 1)-1)*100<=config['ma_distance_pct'],
                                       'RSI回落区间':rsi is not None and config['rsi_current_min']<=rsi<=config['rsi_current_max'],
                                       '缩量回踩':contraction is not None and contraction<=config['volume_contraction_max']})
                    score+=sum(6 if ok else -8 for ok in list(conditions.values())[-5:])
                    trigger=setup.get('ma20');reason='等待RSI强势回落后在VWAP或EMA附近重新转强'
                elif strategy=='vcp_swing':
                    percentile=setup.get('rs_percentile');contraction=setup.get('atr_contraction');range_ratio=setup.get('range_contraction')
                    conditions.update({'EMA多头':bool(setup.get('ema10') and setup.get('ema10')>setup.get('ema20',float('inf'))>setup.get('ema50',float('inf'))),
                                       '相对强势前列':percentile is not None and percentile>=100-config['rs_top_pct'],
                                       '高低点抬升':bool(setup.get('higher_high_low')),
                                       '波动收缩':contraction is not None and contraction<=config['contraction_max'] and range_ratio is not None and range_ratio<=config['range_contraction_max']})
                    score+=sum(8 if ok else -10 for ok in list(conditions.values())[-4:])
                    trigger=setup.get('high10');reason='等待VCP质量、排名与收缩条件完成后放量突破'
                elif strategy=='orb20_us':
                    reason='等待美股10:00至11:30突破20分钟开盘区间';trigger=setup.get('breakout_reference')
                score=max(0,min(100,round(score,1)))
                items.append({'symbol':setup['symbol'],'name':setup.get('name',setup['symbol']),'strategy':strategy,
                              'strategy_name':definition['name'],'version':self.registry.current(strategy,market)['version'],
                              'horizon':definition['horizon'],'max_hold_sessions':definition['max_hold_sessions'],'exit_policy':definition['exit_policy'],
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
                needed=self.analysis_daily_required(strategy,self.registry.config(strategy,market))
                ready=daily>=needed and history>=14 and benchmark>=61
                complete+=int(ready);details.append({'symbol':symbol,'daily_bars':daily,'intraday_sessions':history,'ready':ready})
            message=(f'已有{complete}只股票具备完整逐时点历史，尚需同源基准盘中历史后再运行'
                     if complete else '本地逐时点历史尚不完整；为避免未来数据泄漏，暂不生成回放收益')
            self.registry.set_replay(strategy,market,version,{
                'state':'insufficient_data','label':'历史探索结果','message':message,
                'checked_at':now().isoformat(),'symbols_checked':len(symbols),'complete_symbols':complete,
                'requirements':{'daily_bars':self.analysis_daily_required(strategy,self.registry.config(strategy,market)),'prior_intraday_sessions':14,'same_source_benchmark':True},
                'details':details})
            self.broadcast()
        try:
            task=asyncio.get_running_loop().create_task(audit());self.tasks.append(task)
        except RuntimeError:
            # Direct unit-level configuration changes have no running service loop.
            pass

    def analysis_transport(self,symbol):
        """Describe how this symbol is being refreshed without overstating feed quality."""
        monitored=symbol in self.tracked()
        if not self.monitor_running:return 'stopped','盯盘已暂停'
        if not monitored:return 'snapshot','即时快照'
        if symbol in self.lb.subscribed and self.lb.status.get('stream'):
            return 'push','实时盯盘'
        if self.lb.ctx:return 'polling','15秒查询'
        return 'unavailable','行情不可用'

    @staticmethod
    def analysis_daily_required(strategy,config):
        if strategy=='trend_pullback':return max(25,int(config['daily_slow'])+5)
        if strategy=='trend_rsi_pullback':return max(65,int(config['daily_slow'])+5,int(config['rsi_period'])+7)
        if strategy=='vcp_swing':return 260
        if strategy=='volatility_breakout':return max(25,int(config['atr_long'])+1,int(config['breakout_days']))
        return max(25,int(config.get('daily_ma',20))+5)

    def market_transport(self,market,tracked,ready):
        subscribed=sum(s in self.lb.subscribed for s in tracked)
        if not self.monitor_running:return 'stopped','盯盘已暂停',subscribed
        if not monitoring_window(now(),market):return 'stopped','当前时段不请求新行情',subscribed
        if tracked and subscribed>=ready>0 and self.lb.status.get('stream'):
            return 'push','行情推送',subscribed
        if tracked and self.lb.ctx:return 'polling','15秒查询补充',subscribed
        return 'unavailable','行情连接不可用',subscribed

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
        references={};fetch_errors=[]

        # A one-off analysis must remain responsive. Fill only missing pieces in
        # parallel and keep an honest WAIT result when a provider is slow.
        if self.lb.ctx:
            jobs={}
            applicable=[k for k,d in DEFINITIONS.items() if market in d['markets']]
            daily_needed=max(self.analysis_daily_required(k,self.registry.config(k,market)) for k in applicable)
            if len(daily)<daily_needed:jobs['daily']=asyncio.create_task(asyncio.wait_for(self.lb.bars(symbol,'day',DAILY_FETCH_COUNT),8))
            stale_bar=is_open(now(),market) and (not current_bars or (now()-current_bars[-1].end).total_seconds()>390)
            if len(current_bars)<2 or stale_bar:jobs['intraday']=asyncio.create_task(asyncio.wait_for(self.lb.bars(symbol,'5m',1000),8))
            if len(benchmark_daily)<61:jobs['benchmark_daily']=asyncio.create_task(asyncio.wait_for(self.lb.bars(BENCHMARKS[market],'day',DAILY_FETCH_COUNT),8))
            if not benchmark_intraday:jobs['benchmark_intraday']=asyncio.create_task(asyncio.wait_for(self.lb.bars(BENCHMARKS[market],'5m',1000),8))
            if not candidate.get('name'):jobs['reference']=asyncio.create_task(asyncio.wait_for(self.lb.reference_info([symbol]),5))
            if not quote or (now()-quote.received_at).total_seconds()>30:
                jobs['quote']=asyncio.create_task(asyncio.wait_for(self.lb.quotes([symbol]),5))
            if jobs:
                values=await asyncio.gather(*jobs.values(),return_exceptions=True)
                fetched=dict(zip(jobs,values))
                fetch_errors=[key for key,value in fetched.items() if isinstance(value,Exception)]
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
        # Lingxi may provide a supplemental A-share display quote. It never
        # becomes the source for Longbridge bars or volume-based confirmation.
        if not quote and market=='CN':
            try:
                supplemental=await asyncio.wait_for(self.lingxi.quotes([symbol]),5)
                if supplemental:quote=supplemental[0]
            except Exception:fetch_errors.append('supplemental_quote')
        name=references.get(symbol,{}).get('name') or candidate.get('name') or self.security_map.get(symbol,{}).get('name') or symbol
        if cached and len(daily)>len(self.strategies.context[symbol].get('daily_bars',[])):
            cached=False
        if cached:
            context=self.strategies.context[symbol];daily=context.get('daily_bars',[]);benchmark_daily=context.get('benchmark_daily',[])
            intraday=self.strategies.history[symbol];benchmark_intraday=self.strategies.benchmarks.get(market,[])
            signals=[Signal.load(r) for r in self.store.signals()
                     if r['symbol']==symbol and r.get('version')==self.registry.active_versions().get((market,r.get('strategy')))]
            preview=self.strategies.preview(symbol).get('strategies',{})
        else:
            intraday=sorted(current_bars,key=lambda b:b.start);signals=[];preview={}
            if daily and benchmark_daily:
                setup=next((r for r in self.selection if r['symbol']==symbol),{})
                rank={'percentile':setup.get('rs_percentile'),'coverage':setup.get('rs_rank_coverage',0)}
                engine=ResearchEngine(self.registry);engine.prepare(symbol,daily,historical,benchmark_daily,rank);engine.set_benchmark(market,benchmark_intraday)
                risk_group='st' if self.is_st(symbol,name) else 'smallcap' if (_atr_pct(daily)>=5) else 'normal'
                for bar in intraday:signals.extend(engine.update(bar,risk_group))
                preview=engine.preview(symbol).get('strategies',{})
        risk_group='st' if self.is_st(symbol,name) else 'smallcap' if (_atr_pct(daily)>=5) else 'normal'
        observed_at=now();open_now=is_open(observed_at,market);market_phase=phase(observed_at,market)
        if quote and quote.source=='longbridge' and open_now:
            try:
                book=await asyncio.wait_for(self.lb.depth(symbol),3)
                if book and book.get('source')==quote.source:
                    for key in ['bid','ask','bid_size','ask_size','depth_time']:setattr(quote,key,book[key])
            except Exception:pass
        observed_at=now();open_now=is_open(observed_at,market);market_phase=phase(observed_at,market)
        base=self.registry.config('breakout',market)
        factors=daily_factors(symbol,daily,benchmark_daily,liquidity_min=base['liquidity_min'],rs_min=base['relative_strength_min'])
        cap=references.get(symbol,{}).get('total_shares');cap=cap*daily[-1].close if cap and daily else candidate.get('market_cap')
        if not cap and quote and quote.market_cap:cap=quote.market_cap
        existing_validation=self.validation.get(symbol,{}) if cached else {}
        source=intraday[-1].source if intraday else daily[-1].source if daily else 'longbridge'
        today_bars=sorted([b for b in intraday if b.final and b.end<=observed_at and local_date(b.start,market)==day],key=lambda b:b.start)
        if cached:
            history_groups=self.strategies.context.get(symbol,{}).get('sessions',{})
        else:
            history_groups={}
            for bar in historical:history_groups.setdefault(local_date(bar.start,market),{})[bar.start]=bar
        from zoneinfo import ZoneInfo
        zone=ZoneInfo('Asia/Shanghai' if market=='CN' else 'America/New_York')
        today_clocks={b.start.astimezone(zone).strftime('%H:%M') for b in today_bars}
        baseline_sessions=0
        for session in sessions:
            rows=history_groups.get(session.date(),{})
            clocks={b.start.astimezone(zone).strftime('%H:%M') for b in rows.values()}
            if rows and (not today_clocks or today_clocks.issubset(clocks)):baseline_sessions+=1
        benchmark_today={b.start:b for b in benchmark_intraday
                         if b.final and b.end<=observed_at and local_date(b.start,market)==day}
        benchmark_synced=bool(today_bars) and all(
            b.start in benchmark_today and benchmark_today[b.start].source==b.source for b in today_bars)
        quote_market_age=(observed_at-quote.market_time).total_seconds() if quote and quote.market_time else None
        quote_receive_age=(observed_at-quote.received_at).total_seconds() if quote else None
        depth_age=(observed_at-quote.depth_time).total_seconds() if quote and quote.depth_time else None
        bar_age=(observed_at-today_bars[-1].end).total_seconds() if today_bars else None
        bars_timely=bool(bar_age is not None and -5<=bar_age<=390)
        quote_timely=bool(quote and quote.source==source and quote_market_age is not None and quote_receive_age is not None
                          and 0<=quote_market_age<=30 and 0<=quote_receive_age<=30)
        quote_current=bool(quote_timely and quote.quality=='realtime')
        depth_timely=bool(quote and quote.source==source and depth_age is not None and 0<=depth_age<=15
                          and quote.bid and quote.ask and quote.ask>=quote.bid)
        depth_current=bool(depth_timely and quote_current)
        data_ready=len(daily)>=25 and len(benchmark_daily)>=21 and len(today_bars)>=2
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
                            self.sim.state,observed_at,temporary,self.registry.active_versions())
        per_strategy=[]
        for strategy,definition in DEFINITIONS.items():
            if market not in definition['markets']:
                per_strategy.append({'strategy':strategy,'name':definition['name'],'version':None,'state':'不适用',
                                     'reason':'该策略仅适用于'+('／'.join(definition['markets'])),'conditions':{},'requirements':{},'decision':None,'enabled':False,
                                     'horizon':definition['horizon'],'max_hold_sessions':definition['max_hold_sessions']})
                continue
            config=self.registry.config(strategy,market)
            required_daily=self.analysis_daily_required(strategy,config)
            if strategy=='trend_pullback':
                strategy_daily=daily_factors(symbol,daily,benchmark_daily,config['daily_fast'],config['daily_slow'],config['liquidity_min'],config['relative_strength_min'])
                required_intraday=int(config['intraday_ema'])+1
            elif strategy=='volatility_breakout':
                strategy_daily=daily_factors(symbol,daily,benchmark_daily,config['daily_ma'],None,config['liquidity_min'],config['relative_strength_min'])
                required_intraday=2
            elif strategy=='vcp_swing':
                strategy_daily=preview.get(strategy,{}) or {'eligible':False,'reason':'等待VCP完整数据'}
                required_intraday=2
            elif strategy=='trend_rsi_pullback':
                strategy_daily=preview.get(strategy,{}) or daily_factors(symbol,daily,benchmark_daily,config['daily_fast'],config['daily_slow'],config['liquidity_min'],config['relative_strength_min'])
                required_intraday=int(config['intraday_ema'])+1
            elif strategy in ('breakout','pullback'):
                strategy_daily=factors;required_intraday=int(config['opening_bars'])+1
            else:
                strategy_daily=factors;required_intraday=int(config['opening_minutes'])//5+1
            needs_baseline=strategy in ('breakout','pullback','volatility_breakout','vcp_swing','orb20_us')
            requirements={'日线数据':len(daily)>=required_daily,
                          '今日完整5分钟K线':len(today_bars)>=required_intraday,
                          '最新完整K线':bars_timely,
                          '同源基准盘中同步':benchmark_synced,
                          '实时报价':quote_current,'买卖盘':depth_current}
            if needs_baseline:requirements['14日同时间量能']=baseline_sessions>=14
            decision=next((r for r in decisions if r['strategy']==strategy or strategy in r.get('strategies',[])),None)
            view=preview.get(strategy)
            if not view:
                if len(daily)<required_daily:reason=f'日线数据预热中（{len(daily)}/{required_daily}）'
                elif len(benchmark_daily)<21:reason=f'基准日线预热中（{len(benchmark_daily)}/21）'
                elif len(today_bars)<required_intraday:reason=f'等待今日完整5分钟K线（{len(today_bars)}/{required_intraday}）'
                elif not benchmark_synced:reason='等待同源基准同步至当前完整K线'
                elif needs_baseline and baseline_sessions<14:reason=f'趋势可判断，等待14日同时间成交量基线（{baseline_sessions}/14）'
                else:reason=validation['reason']
                view={**strategy_daily,'status':'wait','ready':False,'reason':reason}
            view={**view,'requirements':requirements}
            state='符合' if decision and decision.get('state')=='buy' else '不适用' if view.get('status')=='disabled' else '等待'
            per_strategy.append({'strategy':strategy,'name':definition['name'],'version':self.registry.current(strategy,market)['version'],
                                 'state':state,'reason':decision.get('reason') if decision else view.get('reason'),
                                 'conditions':view,'requirements':requirements,'decision':decision,
                                 'enabled':self.registry.enabled(strategy,market),'horizon':definition['horizon'],
                                 'max_hold_sessions':definition['max_hold_sessions'],'exit_policy':definition['exit_policy']})
        entered=next((p for p in temporary if p['symbol']==symbol and p.get('quantity',0)>0),None)
        holding_plan_row=real_holding_plan(entered,quote,observed_at,validation) if entered else None
        buy=next((r for r in decisions if r.get('state')=='buy'),None)
        if holding_plan_row:final={'status':'MANAGE','action':holding_plan_row['action'],'reason':holding_plan_row['reason']}
        elif buy:final={'status':'BUY','action':'可考虑买入','reason':buy['reason'],'decision':buy}
        elif not daily and not today_bars and not quote:
            final={'status':'WAIT','action':'数据暂不可用','reason':'当前未取得日线、5分钟K线或行情，请检查长桥权限与网络'}
        elif not open_now:
            final={'status':'WAIT','action':'继续等待','reason':f'当前{market_phase}；保留最近有效数据，开盘后用新的完整K线、报价和盘口重新判断'}
        elif not validation['ready']:
            final={'status':'WAIT','action':'继续等待','reason':validation['reason']}
        elif not fresh_quote(quote,observed_at,source):
            final={'status':'WAIT','action':'暂不买入','reason':'实时报价超过30秒或权限未确认，统一风控已否决买入'}
        else:
            reasons=[r['reason'] for r in per_strategy if r.get('reason')]
            final={'status':'WAIT','action':'继续等待',
                   'reason':reasons[0] if reasons else validation['reason']}
        transport,transport_label=self.analysis_transport(symbol)
        applicable=[k for k,d in DEFINITIONS.items() if market in d['markets']]
        research_full=(len(daily)>=max(self.analysis_daily_required(k,self.registry.config(k,market)) for k in applicable)
                       and len(benchmark_daily)>=21 and len(today_bars)>=2 and benchmark_synced and baseline_sessions>=14)
        if not daily and not today_bars and not quote:data_status='unavailable'
        elif open_now and (not bars_timely or (quote and not quote_timely)):data_status='stale'
        elif open_now and (not quote_current or not depth_current):data_status='warming' if symbol in self.tracked() else 'partial'
        elif research_full:data_status='full'
        elif symbol in self.tracked():data_status='warming'
        else:data_status='partial'
        blocked=[]
        if len(daily)<max(self.analysis_daily_required(k,self.registry.config(k,market)) for k in applicable):
            blocked.append(f"完整策略日线 {len(daily)}/{max(self.analysis_daily_required(k,self.registry.config(k,market)) for k in applicable)}")
        if len(benchmark_daily)<21:blocked.append(f'基准日线 {len(benchmark_daily)}/21')
        if len(today_bars)<2:blocked.append(f'今日完整5分钟K线 {len(today_bars)}/2')
        elif open_now and not bars_timely:blocked.append(f'最新完整5分钟K线已延迟 {round(bar_age/60,1) if bar_age is not None else "—"}分钟')
        if not benchmark_synced:blocked.append('同源基准尚未同步')
        if baseline_sessions<14:blocked.append(f'14日同时间成交量基线 {baseline_sessions}/14')
        if open_now and not quote_timely:blocked.append('报价时间超过30秒')
        elif open_now and quote and quote.quality!='realtime':blocked.append('行情订阅权限尚未确认')
        if open_now and not depth_timely:blocked.append('买卖盘时间超过15秒或数据缺失')
        elif open_now and not quote_current:blocked.append('买卖盘已取得，但需等待行情权限确认')
        if not open_now:blocked.append(f'当前{market_phase}')
        if quote and quote.source!=source:blocked.append(f'{source}策略K线与{quote.source}补充报价不混算')
        technical={'last_close':daily[-1].close if daily else None,
                   'ma20':sum(b.close for b in daily[-20:])/20 if len(daily)>=20 else None,
                   'ma60':sum(b.close for b in daily[-60:])/60 if len(daily)>=60 else None,
                   'high10':max((b.high for b in daily[-10:]),default=None),
                   'high20':max((b.high for b in daily[-20:]),default=None),
                   'structure_low':min((b.low for b in (today_bars[-4:] or daily[-5:])),default=None),
                   'relative_strength':factors.get('relative_strength'),'average_turnover':factors.get('average_turnover'),
                   'atr_pct':_atr_pct(daily) if len(daily)>=21 else None,'trend':factors.get('trend')}
        next_confirmation=(today_bars[-1].end+timedelta(minutes=5)).isoformat() if open_now and bars_timely else None
        profile={'daily':{'count':len(daily),'required':max(self.analysis_daily_required(k,self.registry.config(k,market)) for k in applicable),'start':daily[0].start.isoformat() if daily else None,
                          'end':daily[-1].start.isoformat() if daily else None},
                 'intraday':{'count':len(today_bars),'last_bar_at':today_bars[-1].end.isoformat() if today_bars else None,
                             'age_seconds':round(bar_age,1) if bar_age is not None else None,'timely':bars_timely},
                 'same_time_volume':{'sessions':baseline_sessions,'required':14},
                 'benchmark':{'symbol':BENCHMARKS[market],'daily_count':len(benchmark_daily),
                              'intraday_count':len(benchmark_today),'synced':benchmark_synced},
                 'quote':{'available':bool(quote),'source':quote.source if quote else None,'quality':quote.quality if quote else None,
                          'market_time':quote.market_time.isoformat() if quote and quote.market_time else None,
                          'received_at':quote.received_at.isoformat() if quote else None,
                          'market_age_seconds':round(quote_market_age,1) if quote_market_age is not None else None,
                          'receive_age_seconds':round(quote_receive_age,1) if quote_receive_age is not None else None,
                          'timely':quote_timely,'fresh':quote_current},
                 'depth':{'available':bool(quote and quote.bid and quote.ask),'time':quote.depth_time.isoformat() if quote and quote.depth_time else None,
                          'age_seconds':round(depth_age,1) if depth_age is not None else None,'timely':depth_timely,'fresh':depth_current},
                 'fetch_errors':fetch_errors}
        monitoring={'used':len(self.tracked()),'limit':self.settings['monitor_limit'],
                    'available':max(0,self.settings['monitor_limit']-len(self.tracked())),
                    'symbols':self.tracked()}
        return {'symbol':symbol,'name':name,'market':market,'monitored':symbol in self.tracked(),
                'quote':quote.dump() if quote else None,'validation':validation,'strategies':per_strategy,
                'holding':holding_plan_row,'holding_origin':holding_origin,'final':final,'analyzed_at':observed_at.isoformat(),'source':source,
                'mode':'monitoring' if symbol in self.tracked() else 'snapshot','transport':transport,'transport_label':transport_label,
                'data_status':data_status,'data_profile':profile,'technical':technical,
                'available_assessments':{'daily_trend':len(daily)>=25 and len(benchmark_daily)>=21,
                                         'intraday_structure':len(today_bars)>=2 and benchmark_synced,
                                         'same_time_volume':baseline_sessions>=14,
                                         'execution':quote_current and depth_current and open_now},
                'blocked_conditions':blocked,'refresh_pending':bool(symbol in self.tracked() and self.lb.ctx and data_status in ('warming','stale')),
                'next_confirmation_at':next_confirmation,'market_phase':market_phase,'monitoring':monitoring,
                'note':'已加入持续监测；本机规则会自动更新，不调用AI' if symbol in self.tracked() else '即时快照不会自动写入持仓或占用监测名额；点击加入后才持续跟踪'}

    def cached_stock_context(self,symbol):
        candidate=next((r for r in self.candidates if r.get('symbol')==symbol),{})
        selection=next((r for r in self.selection if r.get('symbol')==symbol),{})
        quote=self.quotes.get(symbol)
        detail=self.details.get(symbol,{})
        decision=next((r for r in self.decisions if r.get('symbol')==symbol),None)
        name=candidate.get('name') or selection.get('name') or self.security_map.get(symbol,{}).get('name') or symbol
        return {'symbol':symbol,'name':name,'market':symbol_market(symbol),'quote':quote.dump() if quote else None,
                'source':detail.get('source') or candidate.get('source') or selection.get('source') or 'longbridge',
                'data_status':detail.get('status','cached'),'technical':selection,
                'blocked_conditions':[detail.get('reason')] if detail.get('reason') else [],
                'final':{'action':decision.get('action','继续等待') if decision else selection.get('decision','继续等待'),
                         'reason':decision.get('reason') if decision else detail.get('reason') or selection.get('reason') or '仅使用本地最近快照'}}

    async def account_context(self,refresh):
        """Only called after a per-request explicit account opt-in."""
        warnings=[];stale_positions=set()
        if refresh:
            for source in ('longbridge','ibkr'):
                provider=self.providers[source]
                try:
                    summary=await provider.account_summary()
                    self.account_summaries[source]=summary
                except Exception:
                    if source in self.account_summaries:
                        self.account_summaries[source]={**self.account_summaries[source],'stale':True}
                        warnings.append(f'{"长桥" if source=="longbridge" else "盈透"}账户摘要刷新失败，使用上次脱敏快照')
                    else:warnings.append(f'{"长桥" if source=="longbridge" else "盈透"}账户摘要不可用，本次已省略')
                try:
                    rows=await provider.account_positions()
                    synced=self.real.replace_synced(source,rows)
                    self.holding_sync[source]={'state':'connected','message':f'已同步 {len(synced)} 只多头股票持仓',
                                               'updated_at':now().isoformat(),'count':len(synced)}
                except Exception:
                    current=sum(r.get('source')==source for r in self.real.list())
                    if current:
                        stale_positions.add(source)
                        warnings.append(f'{"长桥" if source=="longbridge" else "盈透"}持仓刷新失败，使用上次有效快照')
                    else:warnings.append(f'{"长桥" if source=="longbridge" else "盈透"}持仓不可用，本次已省略')
            self.store.set('account_summaries',self.account_summaries)
            self.store.set('holding_sync',self.holding_sync)
        summaries=[]
        for source,row in self.account_summaries.items():
            summaries.append({k:v for k,v in row.items() if k in {'source','balances','updated_at','stale'}}|{'source_label':'长桥' if source=='longbridge' else '盈透'})
        positions=[]
        for row in self.real.list():
            if row.get('source') not in ('longbridge','ibkr'):continue
            quote=self.quotes.get(row['symbol'])
            price=quote.price if quote else row.get('cost')
            clean={k:row.get(k) for k in ('symbol','name','source','quantity','cost','currency','updated_at')}
            clean['source_label']='长桥' if row.get('source')=='longbridge' else '盈透'
            clean['market_value']=round(abs(number(row.get('quantity'),0)*number(price,0)),4)
            clean['stale']=row.get('source') in stale_positions
            positions.append(clean)
        positions.sort(key=lambda row:(-row['market_value'],row['source'],row['symbol']))
        omitted=max(0,len(positions)-40)
        if omitted:warnings.append(f'持仓共{len(positions)}只，仅带入市值前40只，已省略{omitted}只')
        position_sources=[]
        for source in ('longbridge','ibkr'):
            rows=[row for row in positions if row['source']==source]
            if rows:position_sources.append({'source':source,'source_label':'长桥' if source=='longbridge' else '盈透',
                                             'updated_at':max((row.get('updated_at') or '' for row in rows),default=None),
                                             'stale':source in stale_positions})
        return {'included':True,'summaries':summaries,'positions':positions[:40],'omitted_positions':omitted,
                'position_sources':position_sources},warnings

    def gpt_scan_candidates(self,market,limit=10):
        available=[r for r in self.selection if r.get('market')==market and r.get('decision')!='暂不参与']
        bounded=max(1,min(int(limit),20))
        if bounded<=10:rows=self.premarket(market)[:bounded]
        else:
            rows=[]
            for index,row in enumerate(diversified_rank([dict(r) for r in available],bounded),1):
                rows.append({**row,'rank':index,'industry':row.get('industry') or '行业未知'})
        result=[]
        for row in rows:
            quote=self.quotes.get(row['symbol'])
            strategies=[DEFINITIONS[s]['name'] for s in row.get('candidate_strategies',[]) if s in DEFINITIONS]
            result.append({'rank':row['rank'],'symbol':row['symbol'],'name':row.get('name') or row['symbol'],
                'industry':row.get('industry') or '行业未知','score':row.get('score'),'decision':row.get('decision'),
                'strategy_source':'、'.join(strategies) if strategies else '全市场活跃度补充',
                'price':quote.price if quote else row.get('close'),'price_source':quote.source if quote else row.get('source'),
                'price_time':quote.market_time.isoformat() if quote and quote.market_time else row.get('as_of'),
                'confirmation_condition':row.get('reason'),'no_chase_line':row.get('breakout_reference'),
                'invalidation_condition':row.get('structure_low'),'risk_group':row.get('risk_group'),
                'pool_role':row.get('pool_role'),'candidate_updated_at':row.get('candidate_updated_at')})
        core=self.daily_core.get(market,{})
        stale=bool(core.get('stale')) or core.get('trade_date')!=self.candidate_trade_date(market)
        return result,{'market':market,'candidate_count':len(available),'included_count':len(result),
            'omitted_count':max(0,len(available)-len(result)),'trade_date':self.candidate_trade_date(market),
            'refreshed_at':core.get('updated_at'),'stale':stale,'core_count':len(core.get('rows',[])),
            'supplement_count':sum(r.get('market')==market and r.get('pool_role')=='intraday_supplement' for r in self.candidates)}

    async def gpt_context(self,data):
        if not str(data.get('question','')).strip():raise ValueError('请输入要交给 ChatGPT 的研究问题')
        market=str(data.get('market','')).upper()
        if market not in ('CN','US'):raise ValueError('请选择A股或美股市场')
        mode=str(data.get('mode') or 'research')
        if mode not in ('research','market_scan'):raise ValueError('未知的GPT研究模式')
        symbol=str(data.get('symbol') or '').upper().strip() or None
        if mode=='market_scan' and symbol:raise ValueError('全市场扫描不接受单股代码')
        if symbol:
            if not re.fullmatch(r'[A-Z0-9.\-]{1,16}\.(US|SH|SZ)',symbol) or symbol_market(symbol)!=market:
                raise ValueError('股票代码与当前市场不匹配')
            if not self.allowed_security({'symbol':symbol,'name':self.security_map.get(symbol,{}).get('name','')}):
                raise ValueError('该股票不在本机允许的研究范围')
        warnings=[];stock=None;candidates=[];scan_meta=None;refresh_incomplete=False
        if mode=='market_scan' and data.get('refresh',True):
            deadline=time.monotonic()+180
            try:
                for _ in range(2):
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise asyncio.TimeoutError
                    task=self.request_scan(force_strategy=market=='CN',rebuild_markets=[market],wait_selection=True)
                    await asyncio.wait_for(asyncio.shield(task),remaining)
                    core=self.daily_core.get(market,{})
                    if core.get('trade_date')==self.candidate_trade_date(market) and not core.get('stale'):break
            except asyncio.TimeoutError:
                refresh_incomplete=True
                warnings.append('全市场刷新超过180秒，使用本机最近有效候选快照；后台仍会继续完成刷新')
            except Exception:
                refresh_incomplete=True
                warnings.append('全市场刷新未完成，使用本机最近有效候选快照')
        if symbol:
            if data.get('refresh',True):
                try:stock=await self.analyze_stock({'symbol':symbol})
                except Exception:
                    warnings.append('单股数据刷新未完成，使用本地最近有效快照')
            stock=stock or self.cached_stock_context(symbol)
        if mode=='market_scan':
            candidates,scan_meta=self.gpt_scan_candidates(market)
            if refresh_incomplete:scan_meta['stale']=True
            if scan_meta['stale']:warnings.append('候选池不是当前交易日的完整新快照，ChatGPT 必须先刷新并核对后再判断')
            if not candidates:warnings.append('本地规则本轮没有可交接候选，不得为了凑数推荐股票')
            elif len(candidates)<10:warnings.append(f'本地规则本轮仅有{len(candidates)}只候选，已全部带入')
        snapshot=self.snapshot();workspace=snapshot['workspaces'][market]
        account={'included':False,'summaries':[],'positions':[],'omitted_positions':0}
        if data.get('include_account',False):
            account,account_warnings=await self.account_context(bool(data.get('refresh',True)))
            warnings.extend(account_warnings)
        sources=[{'source':'dashboard','label':'SHORTLIST本地规则看板','as_of':snapshot['time'],'stale':False}]
        health=workspace.get('health',{})
        if health.get('data_as_of'):
            sources.append({'source':'market','label':f'{workspace["meta"]["name"]}行情/策略快照','as_of':health['data_as_of'],
                            'stale':health.get('state') in ('delayed','error')})
        if health.get('last_bar_at'):
            sources.append({'source':'longbridge','label':f'{workspace["meta"]["name"]}策略K线','as_of':health['last_bar_at'],
                            'stale':health.get('state') in ('delayed','error')})
        if scan_meta:
            sources.append({'source':'candidate_pool','label':f'{workspace["meta"]["name"]}每日动态候选池',
                            'as_of':scan_meta.get('refreshed_at'),'stale':scan_meta.get('stale',True)})
        if stock and stock.get('quote'):
            sources.append({'source':stock['quote'].get('source'),'label':f'{stock["symbol"]}行情',
                            'as_of':stock['quote'].get('market_time') or stock.get('analyzed_at'),
                            'stale':stock.get('data_status') in ('stale','unavailable')})
        if stock and stock.get('data_profile'):
            profile=stock['data_profile'];bar_time=profile.get('intraday',{}).get('last_bar_at') or profile.get('daily',{}).get('end')
            sources.append({'source':stock.get('source'),'label':f'{stock["symbol"]}策略K线','as_of':bar_time,
                            'stale':stock.get('data_status') in ('stale','unavailable')})
        for summary in account.get('summaries',[]):
            sources.append({'source':summary.get('source'),'label':summary.get('source_label')+'账户摘要',
                            'as_of':summary.get('updated_at'),'stale':bool(summary.get('stale'))})
        for position_source in account.get('position_sources',[]):
            sources.append({'source':position_source.get('source'),'label':position_source.get('source_label')+'持仓快照',
                            'as_of':position_source.get('updated_at'),'stale':bool(position_source.get('stale'))})
        generated=now().isoformat()
        result=build_context(question=data['question'].strip(),market=market,generated_at=generated,
                             workspace=workspace,stock=stock,account=account,included_sources=sources,warnings=warnings,
                             mode=mode,candidates=candidates,scan_meta=scan_meta)
        self.store.event('gpt_handoff',{'message':'已生成GPT研究交接','scope':'stock' if symbol else 'market',
                                        'market':market,'mode':mode,'candidate_count':len(candidates),
                                        'include_account':bool(data.get('include_account',False)),
                                        'stale':bool(scan_meta and scan_meta.get('stale')),'generated_at':generated})
        return result

    def cleanup_gpt_data(self):
        """Remove same-day raw text after package expiry; keep only review-safe fields."""
        t=now();changed=False
        packages=[]
        for row in self.gpt_packages:
            expired=bool(row.get('expires_at') and stamp(row['expires_at'])<=t)
            if expired and row.get('prompt') is not None:
                row={k:row.get(k) for k in ('package_id','market','mode','trade_date','generated_at','expires_at','symbol')}
                row.update(status='expired',prompt=None,preview=None,include_account=False,parent_run_id=None,question=None)
                changed=True
            packages.append(row)
        runs=[]
        for row in self.gpt_runs:
            expired=bool(row.get('expires_at') and stamp(row['expires_at'])<=t)
            if expired and (row.get('response_text') is not None or row.get('status')!='expired'):
                row={k:row.get(k) for k in ('run_id','package_id','market','mode','trade_date','generated_at','expires_at',
                                             'imported_at','version','digest','candidates','local_validation','result')}
                row.update(status='expired',active=False,response_text=None,warnings=['研究结果已在收盘后失效；完整问答已清除'])
                changed=True
            runs.append(row)
        self.gpt_packages=packages[-80:];self.gpt_runs=runs[-160:]
        if changed:
            self.store.set('gpt_packages',self.gpt_packages);self.store.set('gpt_runs',self.gpt_runs)

    def active_gpt_symbols(self,markets=None):
        self.cleanup_gpt_data();markets=set(markets or ('CN','US'))
        active=[]
        for run in reversed(self.gpt_runs):
            if run.get('active') and run.get('status')=='validated' and run.get('market') in markets:
                active.extend(row['symbol'] for row in run.get('candidates',[]) if row.get('verdict')!='不买')
        return list(dict.fromkeys(active))

    def _gpt_package_record(self,package_id):
        return next((row for row in reversed(self.gpt_packages) if row.get('package_id')==package_id),None)

    async def gpt_package(self,data,*,parent=None,question=None,parent_run_id=None):
        market=str(data.get('market','')).upper()
        if market not in ('CN','US'):raise ValueError('请选择A股或美股市场')
        requested=str(data.get('mode') or 'auto')
        mode=resolve_mode(requested,market,now())
        symbol=str(data.get('symbol') or '').upper().strip() or None
        if mode=='single_stock' and not symbol:raise ValueError('单股研究需要股票代码')
        if symbol:
            if not re.fullmatch(r'[A-Z0-9.\-]{1,16}\.(US|SH|SZ)',symbol) or symbol_market(symbol)!=market:
                raise ValueError('股票代码与当前市场不匹配')
            if not self.allowed_security({'symbol':symbol,'name':self.security_map.get(symbol,{}).get('name','')}):
                raise ValueError('该股票不在本机允许的研究范围')
        trade_date=self.candidate_trade_date(market);generated=now().isoformat()
        try:expires=close_time(trade_date,market).isoformat()
        except Exception:expires=(now()+timedelta(hours=12)).isoformat()
        candidates,scan_meta=self.gpt_scan_candidates(market,limit=20)
        if symbol:
            candidates=[row for row in candidates if row['symbol']==symbol] or [{
                'rank':1,'symbol':symbol,'name':self.security_map.get(symbol,{}).get('name') or symbol,
                'industry':'行业未知','decision':'等待本地核验','price':self.quotes.get(symbol).price if self.quotes.get(symbol) else None,
                'price_time':self.quotes.get(symbol).market_time.isoformat() if self.quotes.get(symbol) and self.quotes.get(symbol).market_time else None,
            }]
        account={'included':False,'summaries':[],'positions':[],'omitted_positions':0};warnings=[]
        if data.get('include_account',False):
            account,account_warnings=await self.account_context(True);warnings.extend(account_warnings)
        if scan_meta.get('stale'):warnings.append('本地后备池是过期快照；ChatGPT必须先用Longbridge完成全市场刷新')
        package_id='gptp_'+uuid4().hex
        snapshot=self.snapshot();workspace=snapshot['workspaces'][market]
        prompt=build_package_prompt(package_id=package_id,market=market,trade_date=trade_date,mode=mode,
                                    generated_at=generated,expires_at=expires,candidates=candidates,workspace=workspace,
                                    account=account,symbol=symbol,parent=parent,question=question)
        preview={'market':market,'market_name':workspace['meta']['name'],'mode':mode,'trade_date':trade_date,
                 'market_phase':workspace['meta']['phase'],'fallback_candidates':candidates,'fallback_count':len(candidates),
                 'fallback_stale':bool(scan_meta.get('stale')),'account_included':bool(account.get('included')),
                 'symbol':symbol,'parent_run_id':parent_run_id}
        record={'package_id':package_id,'market':market,'mode':mode,'trade_date':trade_date,'generated_at':generated,
                'expires_at':expires,'symbol':symbol,'include_account':bool(account.get('included')),'prompt':prompt,
                'preview':preview,'status':'ready','parent_run_id':parent_run_id,'question':question}
        self.gpt_packages.append(record);self.gpt_packages=self.gpt_packages[-80:]
        self.store.set('gpt_packages',self.gpt_packages)
        self.store.event('gpt_package',{'message':'已生成GPT选股任务','market':market,'mode':mode,
                                        'candidate_count':len(candidates),'include_account':bool(account.get('included')),
                                        'generated_at':generated})
        return {k:record[k] for k in ('package_id','prompt','preview','generated_at','expires_at')}|{
            'mode_resolved':mode,'chatgpt_url':LONGBRIDGE_APP_URL,'warnings':warnings,
            'conversation_url':self.gpt_conversations.get(market)}

    def _public_gpt_run(self,row):
        return {k:row.get(k) for k in ('run_id','package_id','market','mode','trade_date','generated_at','expires_at',
                                       'imported_at','version','status','candidates','local_validation','warnings','active')}

    def _local_position_cap(self,market,decision):
        if not decision or not decision.get('cash_required'):return None
        account=self.sim.state['accounts'][market]
        equity=account['cash']+sum(p.get('remaining',0)*p.get('mark',0) for p in account['positions'].values())
        return round(decision['cash_required']/equity*100,2) if equity>0 else None

    async def validate_gpt_candidate(self,row,mode=None):
        symbol=row['symbol'];market=symbol_market(symbol)
        try:analysis=await self.analyze_stock({'symbol':symbol})
        except Exception:
            return {'symbol':symbol,'state':'wait','display_status':'GPT精选·等确认','reason':'本地证券、行情或K线刷新未完成',
                    'gpt_verdict':row['verdict'],'local_price':None,'local_price_time':None,'position_cap_pct':None}
        quote=analysis.get('quote') or {};price=quote.get('price');final=analysis.get('final') or {}
        base={'symbol':symbol,'name':analysis.get('name') or row.get('name'),'gpt_verdict':row['verdict'],
              'local_price':price,'local_price_time':quote.get('market_time'),'checked_at':analysis.get('analyzed_at') or now().isoformat(),
              'data_status':analysis.get('data_status'),'source':analysis.get('source'),'position_cap_pct':None}
        def result(state,label,reason):return base|{'state':state,'display_status':label,'reason':reason}
        if row['verdict']=='不买':return result('avoid','暂不参与','GPT结论为不买；本地不会放宽该结论')
        if price is None:return result('wait','GPT精选·等确认','本地没有有效实时报价')
        gpt_age=(now()-stamp(row['price_time'])).total_seconds()
        if mode=='intraday' and (gpt_age<0 or gpt_age>900):
            return result('wait','GPT精选·等确认','GPT盘中行情时间已过期，需先刷新外部研究')
        if quote.get('trade_status')!='Normal':return result('avoid','暂不参与','本地确认停牌或交易状态异常')
        if row.get('invalidation_price') and price<=row['invalidation_price']:
            return result('avoid','暂不参与','本地现价已触及GPT失效线')
        if row.get('no_chase_price') and price>row['no_chase_price']:
            return result('avoid','暂不参与','本地现价已超过GPT不追价线')
        if quote.get('limit_up') and price>=quote['limit_up']:
            return result('avoid','暂不参与','本地确认已触及涨停，不能确认可买')
        if quote.get('limit_down') and price<=quote['limit_down']:
            return result('avoid','暂不参与','本地确认已触及跌停或结构失效')
        if row['verdict']!='买':return result('wait','GPT精选·等确认','GPT结论为等，继续等待确认条件')
        execution=analysis.get('available_assessments',{}).get('execution')
        if not execution:return result('wait','GPT精选·等确认','本地实时报价、盘口或市场时段尚未全部通过')
        if final.get('status')!='BUY' or not final.get('decision'):
            return result('wait','GPT精选·等确认',final.get('reason') or '本地策略尚未出现有效买点')
        local_cap=self._local_position_cap(market,final['decision']);gpt_cap=row.get('position_cap_pct')
        caps=[value for value in (local_cap,gpt_cap) if isinstance(value,(int,float))]
        base['position_cap_pct']=min(caps) if caps else None
        base['local_decision']=final['decision']
        return result('buy','当前可考虑买入','GPT精选且本地行情、盘口、信号与风险检查全部通过')

    async def gpt_import(self,data):
        self.cleanup_gpt_data();package_id=str(data.get('package_id') or '')
        package=self._gpt_package_record(package_id)
        if not package:raise ValueError('找不到这个GPT研究包，请重新生成')
        response_text=data.get('response_text')
        if not isinstance(response_text,str):raise ValueError('请粘贴 ChatGPT 的完整文字答案')
        digest=response_digest(response_text)
        duplicate=next((row for row in reversed(self.gpt_runs) if row.get('package_id')==package_id and row.get('digest')==digest),None)
        if duplicate:return self._public_gpt_run(duplicate)
        imported=now().isoformat();version=1+max((r.get('version',0) for r in self.gpt_runs if r.get('package_id')==package_id),default=0)
        if package.get('status')=='expired' or stamp(package['expires_at'])<=now():
            self.store.event('gpt_import',{'message':'GPT结果已过期','market':package['market'],'mode':package['mode'],'candidate_count':0,'status':'expired','generated_at':imported})
            return {'run_id':None,'status':'expired','candidates':[],'local_validation':[],
                    'warnings':['研究包已在收盘后失效，请重新生成当日任务']}
        base={'run_id':'gptr_'+uuid4().hex,'package_id':package_id,'market':package['market'],'mode':package['mode'],
              'trade_date':package['trade_date'],'generated_at':package['generated_at'],'expires_at':package['expires_at'],
              'imported_at':imported,'version':version,'digest':digest,'response_text':response_text,'active':False}
        try:
            parsed=extract_result(response_text)
            structured=validate_result(parsed,package)
        except ValueError as exc:
            row=base|{'status':'needs_correction','candidates':[],'local_validation':[],'result':None,'warnings':[str(exc)]}
            self.gpt_runs.append(row);self.gpt_runs=self.gpt_runs[-160:];self.store.set('gpt_runs',self.gpt_runs)
            self.store.event('gpt_import',{'message':'GPT结果需要修正','market':package['market'],'mode':package['mode'],
                                           'candidate_count':0,'status':'needs_correction','generated_at':imported})
            return self._public_gpt_run(row)
        if (not structured.get('tools_used') or structured.get('unavailable_tools')):
            for candidate in structured['candidates']:
                if candidate['verdict']=='买':candidate['verdict']='等'
        local=[]
        for candidate in structured['candidates']:local.append(await self.validate_gpt_candidate(candidate,package['mode']))
        for old in self.gpt_runs:
            if old.get('market')==package['market'] and old.get('trade_date')==package['trade_date']:old['active']=False
        warnings=list(structured.get('data_gaps',[]))
        warnings.extend(f'未完成工具：{item}' for item in structured.get('unavailable_tools',[]))
        row=base|{'status':'validated','candidates':structured['candidates'],'local_validation':local,
                  'result':structured,'warnings':warnings,'active':True}
        self.gpt_runs.append(row);self.gpt_runs=self.gpt_runs[-160:];self.store.set('gpt_runs',self.gpt_runs)
        self.allocate_monitoring();self.broadcast()
        self.store.event('gpt_import',{'message':'GPT结果已完成本地复核','market':package['market'],'mode':package['mode'],
                                       'candidate_count':len(structured['candidates']),'status':'validated','generated_at':imported})
        return self._public_gpt_run(row)

    async def gpt_followup_package(self,data):
        self.cleanup_gpt_data();run_id=str(data.get('run_id') or '')
        run=next((row for row in reversed(self.gpt_runs) if row.get('run_id')==run_id),None)
        if not run or run.get('status')!='validated':raise ValueError('找不到可继续追问的有效研究结果')
        if not run.get('active') or stamp(run['expires_at'])<=now():raise ValueError('这轮研究已失效，请重新发起当日扫描')
        question=str(data.get('question') or '').strip()
        if not question:raise ValueError('请输入要继续复核的问题')
        refreshed=[]
        for candidate in run.get('candidates',[]):refreshed.append(await self.validate_gpt_candidate(candidate,run['mode']))
        parent={'previous_result':run.get('result'),'latest_local_validation':refreshed,
                'price_changes':[{'symbol':item['symbol'],'gpt_price':next((c['current_price'] for c in run['candidates'] if c['symbol']==item['symbol']),None),
                                  'local_price':item.get('local_price'),'local_price_time':item.get('local_price_time')}
                                 for item in refreshed]}
        return await self.gpt_package({'market':run['market'],'mode':run['mode'],'symbol':None,'include_account':False},
                                      parent=parent,question=question,parent_run_id=run_id)

    def gpt_list_runs(self,market):
        self.cleanup_gpt_data()
        if market not in ('CN','US'):raise ValueError('请选择A股或美股市场')
        return {'market':market,'conversation_url':self.gpt_conversations.get(market),
                'runs':[self._public_gpt_run(row) for row in reversed(self.gpt_runs) if row.get('market')==market][:30]}

    def save_gpt_conversation(self,market,url):
        if market not in ('CN','US'):raise ValueError('请选择A股或美股市场')
        clean=valid_chatgpt_url(url)
        if clean:self.gpt_conversations[market]=clean
        else:self.gpt_conversations.pop(market,None)
        self.store.set('gpt_conversations',self.gpt_conversations)
        return {'market':market,'conversation_url':clean}

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
        latest_received=max((q.received_at for q in market_quotes),default=None)
        last_bars=[stamp(self.details[s]['last_bar']) for s in tracked if self.details.get(s,{}).get('last_bar')]
        latest_bar=max(last_bars,default=None)
        ready=sum(bool(self.validation.get(s,{}).get('ready')) for s in tracked)
        fresh=sum(fresh_quote(self.quotes.get(s),now()) for s in tracked)
        uncovered=sum(s not in tracked for s in self.real.symbols() if suffix(s))
        current_time=now();open_now=is_open(current_time,market);market_phase=phase(current_time,market)
        session_day=local_date(current_time,market)
        evaluated=[]
        for symbol in tracked:
            last_bar=self.details.get(symbol,{}).get('last_bar')
            if (self.validation.get(symbol,{}).get('ready') and last_bar
                    and local_date(stamp(last_bar),market)==session_day):
                evaluated.append(symbol)
        today_signals=[row for row in self.store.signals()
                       if suffix(row['symbol']) and local_date(stamp(row['time']),market)==session_day]
        today_failures=[row for row in self.sim.state['failures']
                        if suffix(row.get('symbol','')) and local_date(stamp(row['time']),market)==session_day]
        wait_counts={}
        for symbol in evaluated:
            reason=self.details.get(symbol,{}).get('preview',{}).get('reason')
            if reason:wait_counts[reason]=wait_counts.get(reason,0)+1
        wait_summary=[{'reason':reason,'count':count} for reason,count in
                      sorted(wait_counts.items(),key=lambda item:(-item[1],item[0]))[:3]]
        market_selection=sum(r.get('market')==market for r in self.selection)
        scan_running=bool(self.scanning or self.selection_running)
        if primary:
            session_conclusion=f'当前{len(buys)}只通过买点与执行检查'
        elif urgent:
            session_conclusion='已发现真实持仓需要先处理'
        elif evaluated:
            prefix='上午盘' if market_phase=='午间休市' else '今日' if market_phase=='已收盘' else '本轮'
            session_conclusion=f'{prefix}已检查{len(evaluated)}只，{len(today_signals)}只触发过策略信号，当前0只通过全部买入检查'
        elif scan_running:
            session_conclusion='正在自动筛选和预热，尚未形成本轮盘中结论'
        elif premarket:
            session_conclusion=f'已选出{len(premarket)}只盘前观察股，等待本交易日完整五分钟K线'
        else:
            session_conclusion='当前没有可用的盘前观察名单'
        if not urgent and not primary:
            if market_phase=='午间休市' and evaluated:headline='上午盘结论：暂不买'
            elif open_now and scan_running:headline='自动复核中，当前暂不买'
            elif open_now:headline='本轮结论：暂不买'
            elif market_phase=='已收盘' and evaluated:headline='今日结论：无可执行买点'
        transport,transport_label,subscription_count=self.market_transport(market,tracked,ready)
        quote_age=(current_time-latest).total_seconds() if latest else None
        if not self.monitor_running:health_state='paused';health_text='盯盘已暂停'
        elif not open_now:
            health_state='closed'
            health_text='午间休市，保留11:30封盘数据' if market_phase=='午间休市' else f'{market_phase}，保留最近有效数据'
        elif not tracked or ready==0:health_state='error';health_text='行情中断或尚未预热'
        elif fresh<len(tracked):health_state='delayed';health_text=f'{len(tracked)-fresh}只报价待更新'
        else:health_state='healthy';health_text='行情正常'
        issues=[]
        if uncovered:issues.append(f'{uncovered}只真实持仓未覆盖')
        if open_now and fresh<len(tracked):issues.append('部分报价超过30秒')
        if not self.monitor_running:issues.append('后台盯盘已暂停')
        recommendations=self.strategy_recommendations(market,rows,decisions,premarket,simulation)
        catalog=self.registry.list(market,self.strategy_performance(market))
        strategy_groups={h:{'strategies':[r for r in catalog if r['horizon']==h],
                            'recommendations':{k:v for k,v in recommendations.items() if DEFINITIONS[k]['horizon']==h},
                            'portfolio_performance':simulation[market].get('by_horizon',{}).get(h,{})}
                         for h in ['intraday','short','swing']}
        if primary:primary['matched_strategies']=primary.get('strategies',[primary.get('strategy')])
        for row in backups:row['matched_strategies']=row.get('strategies',[row.get('strategy')])
        return {
            'meta':{'market':market,'name':'A股' if market=='CN' else '美股','currency':'CNY' if market=='CN' else 'USD',
                    'currency_symbol':'¥' if market=='CN' else '$','benchmark':'沪深300' if market=='CN' else 'SPY',
                    'timezone':'Asia/Shanghai' if market=='CN' else 'America/New_York','phase':market_phase,'open':open_now},
            'health':{'state':health_state,'text':health_text,'tracked':len(tracked),'ready':ready,'fresh':fresh,
                      'uncovered_holdings':uncovered,'data_as_of':latest.isoformat() if latest else self.store.get('selection_updated'),
                      'issues':issues,'transport':transport,'transport_label':transport_label,
                      'quote_age_seconds':round(quote_age,1) if quote_age is not None else None,
                      'last_quote_at':latest.isoformat() if latest else None,
                      'last_received_at':latest_received.isoformat() if latest_received else None,
                      'last_bar_at':latest_bar.isoformat() if latest_bar else None,
                      'active_subscriptions':subscription_count,'monitor_limit':self.settings['monitor_limit'],
                      'phase_reason':health_text},
            'decision':{'status':status,'headline':headline,'primary':primary,'backups':backups,'watching':watching,
                        'reason':'有持仓退出条件需要先处理' if urgent else '买点已通过完整K线、现价、盘口与资金检查' if primary else session_conclusion,
                        'session_summary':{'state':'complete' if evaluated else 'running' if scan_running else 'waiting',
                            'refreshing':scan_running,
                            'candidate_count':market_selection,'premarket_count':len(premarket),'monitored_count':len(tracked),
                            'evaluated_count':len(evaluated),'signals_today':len(today_signals),'buyable_now':len(buys),
                            'failed_fills_today':len(today_failures),'wait_reasons':wait_summary,
                            'scan_progress':dict(self.selection_progress),'last_scan':self.last_scan}},
            'premarket':premarket,'candidates':candidates,
            'holdings':{'real':real,'paper':paper,'actionable':actionable[:6]},
            'alerts':market_alerts,'simulation':dict(simulation[market],shadow=self.sim.shadow_summary(market).get(market,{})),
            'breadth':dict(self.market_stats,as_of=self.market_stats_time,available=True) if market=='CN' else {'available':False,'message':'美股市场广度尚未接入'},
            'sources':{'primary':'longbridge','supplemental':'lingxi' if market=='CN' else 'ibkr'},
            'strategies':catalog,'strategy_recommendations':recommendations,'strategy_groups':strategy_groups,
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
        pending=[s['symbol'] for s in list(self.sim.state['pending'].values())+list(self.sim.state['shadow']['pending'].values())]
        picks=[r['symbol'] for r in self.selection if r.get('market') in markets and r['decision']=='重点观察']
        swing=[r['symbol'] for r in sorted(self.selection,key=lambda r:(-(r.get('rs_percentile') or 0),r.get('atr_contraction') or 999,r['symbol']))
               if r.get('market') in markets and r.get('decision')!='暂不参与' and r.get('atr_contraction') is not None]
        fallback=[r['symbol'] for r in self.selection if r.get('market') in markets and r['decision']!='暂不参与']
        gpt=self.active_gpt_symbols(markets)
        desired=list(dict.fromkeys(self.real.symbols()+held+self.manual_watch+pending+gpt+picks+swing+fallback+[s for s in self.watch if symbol_market(s) in markets]))[:self.settings['monitor_limit']]
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
        rows=[dict(r) for r in self.selection if r.get('market')==market and r.get('decision')!='暂不参与']
        rows=diversified_rank(rows,10)
        core=self.daily_core.get(market,{})
        result=[]
        for index,row in enumerate(rows,1):
            result.append({**row,'rank':index,'industry':row.get('industry') or '行业未知',
                           'trade_date':self.candidate_trade_date(market),'pool_updated_at':core.get('updated_at'),
                           'pool_stale':bool(core.get('stale')),'entry_status':'等待盘中确认',
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
        performance=self.sim.performance(market,signal.strategy,signal.version)
        signal.evidence={**signal.evidence,'forward_quality':min(1.,performance.get('count',0)/100)}
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
        active=self.registry.active_versions()
        result=recommend(raws,self.quotes,self.validation,state,t,real_rows,active) if self.monitor_running else []
        shadow_rows=shadow_recommend(raws,self.quotes,self.validation,state,t,active) if self.monitor_running else []
        for row in shadow_rows:
            if row['state']!='buy' or row['signal_id'] in self.sim.state['shadow']['pending']:continue
            raw=next(x for x in raws if x['id']==row['signal_id']);signal=Signal.load(raw)
            signal.evidence={**signal.evidence,'approval':{'time':t.isoformat(),'price':row['price'],'qty':row['qty'],
                'planned_risk':row['planned_risk'],'cash_required':row['cash_required'],'quote_time':row['quote_time'],'depth_time':row['depth_time']}}
            self.sim.queue_shadow(signal)
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
                                        'planned_risk':r['planned_risk'],'quote_time':r['quote_time'],'depth_time':r['depth_time']}}
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
        tick=time.monotonic();next_poll=0.;next_evaluation=0.
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
                    if current>=next_evaluation:
                        self.evaluate_decisions();self.close_reports();self.broadcast();next_evaluation=current+5
                self.tasks=[x for x in self.tasks if not x.done()]
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
            # Rebuild the benchmark whenever this market is inside its monitoring
            # window. This also covers an A-share lunch-break wake/reconnect: the
            # stock history may already include the 11:25 bar while the in-memory
            # benchmark still stops before sleep. Waiting for 13:00 would make all
            # candidates look unsynchronised even though the data feed is healthy.
            active_markets={symbol_market(symbol) for symbol in active}
            for market in ['CN','US']:
                if market in active_markets:
                    try:
                        bars=await self.lb.bars(BENCHMARKS[market])
                        self.strategies.set_benchmark(market,[b for b in bars if b.final and b.end<=now()])
                    except Exception as exc:
                        self.store.event('monitor',{'message':f'{market}基准K线暂未补齐，等待重试','error_type':type(exc).__name__})
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
                    bars=[b for b in await self.lb.bars(symbol,'day',DAILY_FETCH_COUNT) if local_date(b.start,market)<day]
                    if len(bars)<61:continue
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
                daily=[Bar.load(b) for b in cached.get('bars',[])] if cached.get('fetched_date')==str(day) and len(cached.get('bars',[]))>=DAILY_CACHE_MIN else []
                if not daily:
                    daily=[b for b in await self.lb.bars(symbol,'day',DAILY_FETCH_COUNT) if local_date(b.start,market)<day]
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
                setup=next((r for r in self.selection if r['symbol']==symbol),{})
                rank={'percentile':setup.get('rs_percentile'),'coverage':setup.get('rs_rank_coverage',0)}
                self.strategies.prepare(symbol,daily,history,self.benchmark_daily[market],rank)
                self.research_ready[symbol]=str(day)
                f=self.strategies.context[symbol]['daily']
                daily_need=max(self.analysis_daily_required(k,self.registry.config(k,market)) for k,d in DEFINITIONS.items() if market in d['markets'])
                self.research_progress[symbol]={'state':'ready' if days==14 and len(daily)>=daily_need and f.get('eligible') else 'filtered' if not f.get('eligible') else 'waiting','days':days,'required':14,
                    'daily_bars':len(daily),'daily_required':daily_need,
                    'reason':f['reason'] if days==14 and len(daily)>=daily_need else f'日线预热 {len(daily)}/{daily_need}' if len(daily)<daily_need else '最近历史不足14日；已缓存，后续交易日自动补齐'}
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
        self.selection.sort(key=selection_rank)
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
