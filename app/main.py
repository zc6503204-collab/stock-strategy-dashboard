import asyncio,json
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel,Field
from .service import Dashboard
from .providers import ROOT

dashboard=Dashboard()

@asynccontextmanager
async def lifespan(app):
    await dashboard.start()
    yield
    await dashboard.stop()

app=FastAPI(title='短线观察台 · 本地模拟研究',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1','localhost'])

@app.middleware('http')
async def local_only(request:Request,call_next):
    if request.method not in ['GET','HEAD','OPTIONS']:
        if request.headers.get('x-dashboard-local')!='1':
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail':'仅接受本地看板操作'},status_code=403)
        origin=request.headers.get('origin')
        if origin and origin not in ['http://127.0.0.1:8765','http://localhost:8765']:
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail':'来源不允许'},status_code=403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Cache-Control']='no-store'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'sha256-vzr6U6Yv8Vz+BRc+9/HgtZvUqecsKaEvnfervwwT014='; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    return response

@app.get('/')
async def index():return FileResponse(ROOT/'web/index.html')

app.mount('/assets',StaticFiles(directory=str(ROOT/'web')),name='assets')

@app.get('/api/state')
async def state():return dashboard.snapshot()

@app.get('/api/alerts')
async def alerts(limit:int=100,before:str|None=None):
    return dashboard.alerts.list(max(1,min(limit,300)),before)

class AlertRead(BaseModel):id:str=Field(max_length=200)

@app.post('/api/alerts/read')
async def read_alert(body:AlertRead):
    dashboard.alerts.read(body.id);dashboard.broadcast();return {'ok':True}

class Notifications(BaseModel):
    enabled:bool=True
    desktop:bool=True
    sound:bool=True

@app.post('/api/notifications')
async def notifications(body:Notifications):
    dashboard.alerts.configure(body.model_dump());dashboard.broadcast();return dashboard.alerts.status()

@app.post('/api/notifications/test')
async def test_notifications():
    result=await dashboard.alerts.native('request')
    dashboard.alerts.permission=result.get('status','unknown')
    from .models import now
    dashboard.alerts.emit('test:'+now().isoformat(),'短线观察台 · 提醒测试','这是本机规则提醒，不调用AI，也不代表买卖信号。','test')
    return {'ok':True,'permission':dashboard.alerts.permission}

class Monitor(BaseModel):running:bool

@app.post('/api/monitor')
async def monitor(body:Monitor):
    dashboard.monitor_running=body.running;dashboard.store.set('monitor_running',body.running)
    for symbol in dashboard.tracked():dashboard.suspend(symbol,'监测暂停' if not body.running else '恢复监测，重新核验行情')
    dashboard.first_poll=True;dashboard.evaluate_decisions();dashboard.broadcast();return {'ok':True}

@app.get('/api/events')
async def events(request:Request):
    async def stream():
        event=asyncio.Event();dashboard.listeners.add(event)
        try:
            yield 'data: ready\n\n'
            while not await request.is_disconnected():
                try:await asyncio.wait_for(event.wait(),20)
                except asyncio.TimeoutError:pass
                event.clear();yield 'data: update\n\n'
        finally:dashboard.listeners.discard(event)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

@app.get('/api/bars/{symbol}')
async def bars(symbol:str,source:str='longbridge'):
    if source not in ['longbridge','ibkr']:raise HTTPException(400,'未知数据源')
    return dashboard.store.bars(symbol,source,180)

@app.get('/api/lookup')
async def lookup(q:str=''):
    term=q.strip().upper()
    if len(term)<2:return []
    pool={s:r.get('name',s) for s,r in dashboard.security_map.items()}
    pool.update({r['symbol']:r.get('name',r['symbol']) for r in dashboard.candidates})
    pool.update({r['symbol']:r.get('name',r['symbol']) for r in dashboard.real.list()})
    return [{'symbol':s,'name':name} for s,name in pool.items()
            if (term in s or term in str(name).upper()) and dashboard.allowed_security({'symbol':s,'name':name})][:15]

class Watch(BaseModel):symbol:str=Field(max_length=20)
class Settings(BaseModel):
    monitor_limit:int=Field(default=12,ge=1,le=40)
    simulation_enabled:bool=True
    ib_port:int=Field(default=7497,ge=1024,le=65535)

class ManualHolding(BaseModel):
    id:str|None=Field(default=None,max_length=80)
    symbol:str=Field(max_length=20)
    name:str|None=Field(default=None,max_length=80)
    quantity:float=Field(gt=0)
    cost:float=Field(gt=0)
    entry_date:str|None=Field(default=None,max_length=10)
    stop:float|None=Field(default=None,gt=0)
    target:float|None=Field(default=None,gt=0)
    note:str=Field(default='',max_length=300)

class HoldingPlan(BaseModel):
    id:str=Field(max_length=80)
    entry_date:str|None=Field(default=None,max_length=10)
    stop:float|None=Field(default=None,gt=0)
    target:float|None=Field(default=None,gt=0)
    note:str=Field(default='',max_length=300)

class HoldingId(BaseModel):id:str=Field(max_length=80)
class HoldingSync(BaseModel):source:str='all'

@app.post('/api/watch')
async def watch(body:Watch):
    try:await dashboard.add_watch(body.symbol)
    except ValueError as e:raise HTTPException(400,str(e))
    return {'ok':True}

@app.post('/api/watch/remove')
async def unwatch(body:Watch):
    try:await dashboard.remove_watch(body.symbol)
    except ValueError as e:raise HTTPException(400,str(e))
    return {'ok':True}

class StrategyConfig(BaseModel):
    market:str
    parameters:dict[str,float|int]={}
    enabled:bool|None=None
    reason:str=Field(default='',max_length=200)

class StrategyRollback(BaseModel):
    market:str
    version:str=Field(max_length=100)

@app.get('/api/strategies')
async def strategies(market:str='CN'):
    try:return dashboard.registry.list(market,dashboard.strategy_performance(market))
    except (ValueError,KeyError) as e:raise HTTPException(400,str(e))

@app.post('/api/strategies/{strategy}/config')
async def strategy_config(strategy:str,body:StrategyConfig):
    try:return dashboard.save_strategy_config(strategy,body.market,body.parameters,body.enabled,body.reason)
    except ValueError as e:raise HTTPException(400,str(e))

@app.get('/api/strategies/{strategy}/versions')
async def strategy_versions(strategy:str,market:str='CN'):
    try:return dashboard.registry.versions(strategy,market)
    except ValueError as e:raise HTTPException(400,str(e))

@app.post('/api/strategies/{strategy}/rollback')
async def strategy_rollback(strategy:str,body:StrategyRollback):
    try:return dashboard.rollback_strategy(strategy,body.market,body.version)
    except ValueError as e:raise HTTPException(400,str(e))

@app.get('/api/strategies/{strategy}/recommendations')
async def strategy_recommendations(strategy:str,market:str='CN'):
    if strategy not in dashboard.registry.revisions.get(market,{}):raise HTTPException(400,'未知策略或市场')
    return dashboard.strategy_recommendations(market).get(strategy)

@app.get('/api/strategies/{strategy}/performance')
async def strategy_performance(strategy:str,market:str='CN',version:str|None=None):
    try:return dashboard.strategy_performance_detail(market,strategy,version)
    except ValueError as e:raise HTTPException(400,str(e))

class AnalyzeHolding(BaseModel):
    quantity:float|None=Field(default=None,gt=0)
    cost:float|None=Field(default=None,gt=0)
    available:float|None=Field(default=None,ge=0)
    entry_date:str|None=Field(default=None,max_length=10)
    stop:float|None=Field(default=None,gt=0)
    target:float|None=Field(default=None,gt=0)

class AnalyzeRequest(BaseModel):
    symbol:str=Field(max_length=20)
    holding:AnalyzeHolding|None=None

@app.post('/api/analyze')
async def analyze(body:AnalyzeRequest):
    try:return await dashboard.analyze_stock(body.model_dump(exclude_none=True))
    except ValueError as e:raise HTTPException(400,str(e))

@app.post('/api/analyze/add-monitoring')
async def analyze_add_monitoring(body:Watch):
    try:return await dashboard.add_watch(body.symbol)
    except ValueError as e:raise HTTPException(400,str(e))

@app.post('/api/scan')
async def scan():
    if not dashboard.scanning:
        task=asyncio.create_task(dashboard.scan(force_strategy=True));dashboard.tasks.append(task)
    return {'ok':True}

@app.post('/api/settings')
async def settings(body:Settings):
    if body.monitor_limit<len(dashboard.tracked()):raise HTTPException(400,'请先减少监测股票，再降低名额')
    if not body.simulation_enabled:
        for s in dashboard.tracked():dashboard.sim.cancel_pending(s,'已暂停模拟新开仓')
    dashboard.settings=body.model_dump();dashboard.store.set('settings',dashboard.settings);dashboard.broadcast()
    return {'ok':True}

@app.post('/api/real-holdings/manual')
async def real_holding_manual(body:ManualHolding):
    try:row=dashboard.real.upsert_manual(body.model_dump())
    except ValueError as e:raise HTTPException(400,str(e))
    dashboard.candidate(row['symbol'])['name']=row['name'];dashboard.first_poll=True
    dashboard.evaluate_decisions();dashboard.broadcast();return {'ok':True,'id':row['id']}

@app.post('/api/real-holdings/plan')
async def real_holding_plan(body:HoldingPlan):
    try:dashboard.real.set_plan(body.id,body.stop,body.target,body.entry_date,body.note)
    except ValueError as e:raise HTTPException(400,str(e))
    dashboard.evaluate_decisions();dashboard.broadcast();return {'ok':True}

@app.post('/api/real-holdings/remove')
async def real_holding_remove(body:HoldingId):
    try:dashboard.real.remove_manual(body.id)
    except ValueError as e:raise HTTPException(400,str(e))
    dashboard.evaluate_decisions();dashboard.broadcast();return {'ok':True}

@app.post('/api/real-holdings/sync')
async def real_holding_sync(body:HoldingSync):
    if body.source not in ['all','longbridge','ibkr']:raise HTTPException(400,'未知持仓来源')
    sources=['longbridge','ibkr'] if body.source=='all' else [body.source]
    await dashboard.sync_real_holdings(sources)
    return {'ok':True,'status':dashboard.holding_sync}

@app.post('/api/connect/{provider}')
async def connect(provider:str):
    if provider=='longbridge':
        if dashboard.lb.ctx:
            task=asyncio.create_task(dashboard.verify_longbridge_access());dashboard.tasks.append(task)
        else:await dashboard.lb.authorize()
    elif provider=='ibkr':
        dashboard.ib_auto_connect=True;dashboard.store.set('ib_auto_connect',True)
        await dashboard.connect_ib_available()
    elif provider=='lingxi':
        quotes=await dashboard.lingxi.quotes(dashboard.tracked()[:3] or ['600519.SH'])
        for q in quotes:dashboard.accept_quote(q)
    else:raise HTTPException(400,'未知数据源')
    dashboard.broadcast();return {'ok':True}

@app.post('/api/benchmark')
async def benchmark():
    task=asyncio.create_task(dashboard.benchmark());dashboard.tasks.append(task)
    return {'ok':True}

class Replay(BaseModel):
    symbol:str=Field(max_length=20)
    source:str='longbridge'
    strategy:str|None=None

@app.post('/api/replay')
async def replay(body:Replay):
    try:return dashboard.replay(body.symbol,body.source,body.strategy)
    except ValueError as e:raise HTTPException(400,str(e))

@app.get('/api/replay')
async def last_replay():return dashboard.store.get('last_replay')


class Screen(BaseModel):
    query:str=Field(min_length=2,max_length=300)

@app.post('/api/screen')
async def screen(body:Screen):
    try:return await dashboard.screen(body.query)
    except ValueError as e:raise HTTPException(400,str(e))

@app.get('/api/screen')
async def last_screen():return dashboard.store.get('last_screen')

@app.post('/api/selection')
async def selection():
    dashboard.schedule_selection();return {'ok':True}

class SelectionPriority(BaseModel):market:str='CN'

@app.post('/api/selection/prioritize')
async def prioritize(body:SelectionPriority):
    try:await dashboard.prioritize_selection(body.market)
    except ValueError as e:raise HTTPException(400,str(e))
    return {'ok':True}
