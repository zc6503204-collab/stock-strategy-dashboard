from __future__ import annotations
import asyncio,json,os,time,math,threading,sys
from pathlib import Path
from datetime import timedelta
import httpx
from .models import Quote,Bar,now,stamp,symbol_market,in_scope
from .calendars import is_open

ROOT=Path(__file__).resolve().parent.parent

def sdk_stamp(value):
    # The Rust/Python SDK emits naive LOCAL datetimes; CLI ISO timestamps carry UTC.
    # datetime.astimezone interprets a naive value in this machine's timezone.
    from datetime import datetime
    return stamp(value.astimezone()) if isinstance(value,datetime) and value.tzinfo is None else stamp(value)

async def command(*args,cwd=ROOT,timeout=30):
    p=await asyncio.create_subprocess_exec(*map(str,args),cwd=cwd,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:out,err=await asyncio.wait_for(p.communicate(),timeout)
    except (asyncio.TimeoutError,asyncio.CancelledError):
        p.kill();await p.communicate();raise
    if p.returncode:
        # Do not expose vendor output, headers, tokens, account identifiers, or query strings.
        message=err.decode(errors='replace').lower()
        if 'auth' in message or 'token' in message:raise RuntimeError('授权需更新')
        if '429' in message or 'limit' in message:raise RuntimeError('数据配额或频率限制')
        raise RuntimeError('数据请求未完成')
    text=out.decode(errors='replace').strip()
    # CLI may append update notices; decode only the first JSON value.
    for i,c in enumerate(text):
        if c in '[{':
            try:return json.JSONDecoder().raw_decode(text[i:])[0]
            except json.JSONDecodeError:pass
    raise RuntimeError('数据源没有返回结构化结果')

def number(value,default=None):
    try:
        if isinstance(value,dict):value=float(value['value'])/10**int(value.get('exp',0))
        n=float(value)
        return n if math.isfinite(n) else default
    except (ValueError,TypeError,KeyError):return default

def canonical(code):
    for m in ('SH','SZ','US','HK','BJ'):
        if code.startswith(m):return code[len(m):]+'.'+m
    return code

def lingxi_code(symbol):
    code,market=symbol.rsplit('.',1)
    return market+code

def share_volume(volume,turnover,low,high):
    """Validate share/lot scale against same-source notional and traded price range.
    Never silently guess units when the consistency check is inconclusive.
    """
    if not volume:return 0.
    if not turnover:raise ValueError('缺少成交额，成交量单位无法验证')
    candidates=[factor for factor in (1,100) if low*.98<=turnover/(volume*factor)<=high*1.02]
    if len(candidates)!=1:raise ValueError('成交量与成交额口径无法验证')
    return volume*candidates[0]

def normalize_lingxi(data):
    quotes=[]
    for r in data.get('stocks',[]):
        price_scale=10**int(r.get('price_decimal_places',2))
        ratio_scale=10**int(r.get('ratio_decimal_places',4))
        price=number(r.get('last_price'))
        if price is None or price<=0:continue
        sym=canonical(r.get('code',''))
        if not in_scope(sym):continue
        quotes.append(Quote(sym,'lingxi',r.get('name',sym),price/price_scale,
            stamp(r['date_time']) if r.get('date_time') else None,now(),
            number(r.get('change_rate'))/ratio_scale*100 if number(r.get('change_rate')) is not None else None,number(r.get('total_volume')),
            number(r.get('total_amount'),0)/price_scale,number(r.get('total_market_capital'),0)/price_scale,
            quality='snapshot',session='regular' if is_open(now(),symbol_market(sym)) else 'closed'))
    return quotes

class Provider:
    name=''
    def __init__(self):
        self.status={'state':'idle','message':'等待连接','last_ok':None,'latency_ms':None,'stream':False,'authorized':False}
        self.lock=asyncio.Lock()
        self.metrics=[]
    def ok(self,elapsed,message='连接正常'):
        self.status.update(state='connected',message=message,last_ok=now().isoformat(),latency_ms=round(elapsed*1000),authorized=True)
    def fail(self,message='连接暂不可用'):
        self.status.update(state='unavailable',message=message)

class Lingxi(Provider):
    name='lingxi'
    async def call(self,mode,value):
        async with self.lock:
            if not (ROOT/'.local/vendor/gtht/gtht-skill-shared/gtht-entry.json').exists():
                self.fail('尚未配置授权');return {}
            start=time.monotonic()
            try:
                data=await command('node','scripts/lingxi_bridge.cjs',mode,value,timeout=40)
                if isinstance(data,dict) and data.get('error'):raise RuntimeError('灵犀授权或服务需检查')
                self.ok(time.monotonic()-start,'查询已接通 · 持续推送尚未验证')
                return data
            except Exception:
                self.fail('查询失败，请检查灵犀授权或网络');return {}
    async def quotes(self,symbols):
        if not symbols:return []
        return normalize_lingxi(await self.call('quote',','.join(lingxi_code(s) for s in symbols[:40])))
    async def rank(self,order=10):
        data=await self.call('rank',str(order))
        items=[];stats={}
        for group in data.get('items',[]):
            stats=group.get('board_stats_item',{}).get('price_change_stats',{})
            for row in group.get('board_items',[]):
                sym=canonical(row.get('code',''))
                if in_scope(sym):
                    items.append({'symbol':sym,'name':row.get('name',sym),'price':number(row.get('last_price')),
                      'change_pct':number(row.get('price_change_percent'))*100 if number(row.get('price_change_percent')) is not None else None,'market_cap':number(row.get('market_cap')),
                      'turnover':number(row.get('total_amount')),'relative_volume':number(row.get('relative_volume_ratio')),
                      'market_time':stamp(row['date_time']).isoformat() if row.get('date_time') else None,
                      'previous_close':number(row.get('previous_close_price')),'reason':'成交额活跃' if order==10 else '短期涨幅活跃',
                      'source':'lingxi','scope':'A股全市场榜单中按本地主板／创业板范围过滤'})
        return items,stats

class Longbridge(Provider):
    name='longbridge'
    def __init__(self):
        super().__init__();self.ctx=None;self.auth_url=None;self.auth_running=False
        self.loop=None;self.on_quote=None;self.on_bar=None;self.subscribed=set();self.auth_thread=None
        self.units={};self.exchanges={};self.quote_meta={};self.status.update(state='auth_required',message='SDK持续行情需要授权；可先读取现有CLI数据')

    async def cli_scan(self,market):
        async with self.lock:
            # CLI indicator metadata defines marketcap in 100-million currency units.
            return await command('longbridge','screener','filter','prevclose:2:','marketcap:0.5:','--market',market,'--count','40','--format','json',timeout=25)

    async def account_positions(self):
        """Read current long stock positions through the installed CLI."""
        async with self.lock:
            rows=await command('longbridge','positions','--format','json',timeout=20)
        result=[]
        for row in rows if isinstance(rows,list) else []:
            market=str(row.get('market','')).upper()
            raw=str(row.get('symbol','')).upper()
            symbol=raw if '.' in raw else raw+'.'+market
            quantity=number(row.get('quantity'),0)
            cost=number(row.get('cost_price'),0)
            if quantity<=0 or cost<=0 or not in_scope(symbol):continue
            result.append({'symbol':symbol,'name':row.get('name') or symbol,'quantity':quantity,
                           'available':number(row.get('available')),'cost':cost,'currency':row.get('currency')})
        return result

    async def reference_info(self,symbols):
        if not self.ctx:return {}
        result={}
        for start in range(0,len(symbols),30):
            async with self.lock:
                rows=await asyncio.wait_for(asyncio.to_thread(self.ctx.static_info,symbols[start:start+30]),15)
            for r in rows:
                result[r.symbol]={'name':r.name_cn or r.name_en,'total_shares':number(r.total_shares),'exchange':r.exchange}
                self.exchanges[r.symbol]=str(r.exchange)
        return result

    def realtime_entitled(self,symbol):
        descriptions=' '.join(p.get('description','') for p in self.status.get('packages',[]) if not p.get('expires') or stamp(p['expires'])>now()).lower()
        if symbol.endswith(('.SH','.SZ')):return 'a-shares' in descriptions
        return ('us real-time' in descriptions or 'us stocks' in descriptions or ('nasdaq real-time' in descriptions and 'NAS' in self.exchanges.get(symbol,'').upper()))

    async def quotes(self,symbols):
        if not self.ctx or not symbols:return []
        async with self.lock:
            rows=await asyncio.wait_for(asyncio.to_thread(self.ctx.quote,symbols),15)
        result=[]
        for r in rows:
            price=float(r.last_done);prev=number(r.prev_close)
            if price<=0:continue
            status=str(getattr(r,'trade_status','Unknown')).split('.')[-1]
            self.quote_meta[r.symbol]={'previous_close':prev,'trade_status':status}
            result.append(Quote(r.symbol,'longbridge',r.symbol,price,sdk_stamp(r.timestamp),now(),
              change_pct=(price/prev-1)*100 if prev and prev>0 else None,
              quality='realtime' if self.realtime_entitled(r.symbol) else 'subscription_unverified',trade_status=status,
              session='regular' if is_open(now(),symbol_market(r.symbol)) else 'closed'))
        return result

    async def depth(self,symbol):
        if not self.ctx:return None
        async with self.lock:r=await asyncio.wait_for(asyncio.to_thread(self.ctx.depth,symbol),8)
        bid=next((x for x in r.bids if number(x.price,0)>0),None)
        ask=next((x for x in r.asks if number(x.price,0)>0),None)
        if not bid or not ask:return None
        return {'bid':number(bid.price),'ask':number(ask.price),'bid_size':number(bid.volume),
                'ask_size':number(ask.volume),'depth_time':now(),'source':'longbridge'}

    async def history_day(self,symbol,day):
        from longbridge.openapi import Period,AdjustType,TradeSessions
        if not self.ctx:raise ValueError('持续接口未连接')
        async with self.lock:
            rows=await asyncio.wait_for(asyncio.to_thread(self.ctx.history_candlesticks_by_date,symbol,Period.Min_5,AdjustType.NoAdjust,day,day,TradeSessions.Intraday),20)
        result=[]
        for r in rows:
            o,h,l,c=(float(getattr(r,k)) for k in ['open','high','low','close'])
            v=share_volume(float(r.volume),float(r.turnover),l,h)
            result.append(Bar(symbol,'longbridge',sdk_stamp(r.timestamp),o,h,l,c,v,float(r.turnover)))
        return result

    async def bars(self,symbol,period='5m',count=250,force_cli=False):
        async with self.lock:
            if self.ctx and not force_cli:
                from longbridge.openapi import Period,AdjustType
                rows=await asyncio.wait_for(asyncio.to_thread(self.ctx.candlesticks,symbol,Period.Day if period=='day' else Period.Min_5,count,AdjustType.NoAdjust),20)
                data=[{'time':sdk_stamp(r.timestamp),'open':r.open,'high':r.high,'low':r.low,'close':r.close,'volume':r.volume,'turnover':r.turnover} for r in rows]
            else:
                data=await command('longbridge','kline',symbol,'--period',period,'--count',str(count),'--adjust','none','--format','json',timeout=20)
        result=[]
        for r in data:
            t=stamp(r.get('time',r.get('timestamp')))
            o,h,l,c=(float(r[k]) for k in ['open','high','low','close']);v=float(r['volume']);amount=number(r.get('turnover'))
            # Index activity is a weighting proxy, not traded shares or index notional.
            vv=v if symbol=='000300.SH' else share_volume(v,amount,l,h)
            if v:self.units[symbol]=vv/v
            result.append(Bar(symbol,'longbridge',t,o,h,l,c,vv,amount,final=(t+timedelta(minutes=5)<=now())))
        return sorted(result,key=lambda x:x.start)

    async def connect_cached(self):
        if (ROOT/'.local/longbridge-client.json').exists():
            self.auth_running=True;self.auth_mode='cached'
            self.authorization_task=asyncio.create_task(self.interactive_authorize(False))

    async def authorize(self,interactive=True):
        if self.ctx:return
        if self.auth_running and interactive and getattr(self,'auth_mode','')=='cached':
            self.authorization_task.cancel()
            await asyncio.gather(self.authorization_task,return_exceptions=True)
        elif self.auth_running:return
        if interactive:
            self.auth_running=True;self.auth_mode='interactive'
            self.authorization_task=asyncio.create_task(self.interactive_authorize())
            return
        self.auth_running=True;self.loop=asyncio.get_running_loop()
        def work():
            try:
                from longbridge.openapi import OAuthBuilder,Config,QuoteContext,PushCandlestickMode
                p=ROOT/'.local/longbridge-client.json'
                if p.exists():client=json.loads(p.read_text())
                elif interactive:
                    resp=httpx.post('https://openapi.longbridge.com/oauth2/register',json={'client_name':'短线观察台 · 本地只读行情','redirect_uris':['http://localhost:60355/callback'],'grant_types':['authorization_code','refresh_token'],'response_types':['code']},timeout=20)
                    resp.raise_for_status();client=resp.json()
                    if 'client_id' not in client:raise RuntimeError('无法建立授权客户端')
                    p.write_text(json.dumps({'client_id':client['client_id']}));p.chmod(0o600)
                else:return
                def callback(url):
                    if not interactive:raise RuntimeError('需要重新授权')
                    self.auth_url=url;self.status.update(state='auth_required',message='打开授权页面后返回此处，连接会自动完成')
                oauth=OAuthBuilder(client['client_id']).build(callback)
                config=Config.from_oauth(oauth,enable_print_quote_packages=False,push_candlestick_mode=PushCandlestickMode.Confirmed)
                self.ctx=QuoteContext(config)
                self.ctx.set_on_quote(self._quote)
                self.ctx.set_on_candlestick(self._bar)
                self.auth_url=None;self.status.update(state='connected',message='SDK持续连接已建立',stream=True,authorized=True,last_ok=now().isoformat())
            except Exception:
                self.status.update(state='auth_required',message='长桥持续连接未建立，请使用授权按钮重试')
            finally:self.auth_running=False
        self.auth_thread=threading.Thread(target=work,daemon=True);self.auth_thread.start()

    async def interactive_authorize(self,interactive=True):
        p=None
        try:
            client_path=ROOT/'.local/longbridge-client.json'
            if not client_path.exists():
                if not interactive:raise ValueError('authorization missing')
                async with httpx.AsyncClient(timeout=20) as client:
                    resp=await client.post('https://openapi.longbridge.com/oauth2/register',json={'client_name':'短线观察台 · 本地行情','redirect_uris':['http://localhost:60355/callback'],'grant_types':['authorization_code','refresh_token'],'response_types':['code']})
                    resp.raise_for_status();data=resp.json()
                if 'client_id' not in data:raise ValueError('missing client')
                client_path.write_text(json.dumps({'client_id':data['client_id']}));client_path.chmod(0o600)
            p=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'scripts/longbridge_authorize.py'),*([] if interactive else ['--cached']),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,cwd=ROOT)
            deadline=time.monotonic()+(300 if interactive else 10)
            while True:
                line=await asyncio.wait_for(p.stdout.readline(),max(.1,deadline-time.monotonic()))
                if not line:raise RuntimeError('authorization ended')
                try:data=json.loads(line)
                except json.JSONDecodeError:continue
                if data.get('auth_url'):
                    self.auth_url=data['auth_url'];self.status.update(state='auth_required',message='点击打开授权页面；完成后连接自动恢复')
                if data.get('authorized'):
                    await p.wait();self.auth_running=False;self.auth_url=None
                    await self.authorize(False)
                    return
                if data.get('error'):raise RuntimeError('authorization failed')
        except asyncio.CancelledError:raise
        except Exception:
            self.auth_url=None;self.status.update(state='auth_required',message='授权尚未完成，请点击授权按钮重试')
        finally:
            if p and p.returncode is None:p.kill();await p.wait()
            self.auth_running=False

    def _quote(self,symbol,event):
        try:
            meta=self.quote_meta.get(symbol,{})
            q=Quote(symbol,'longbridge',symbol,float(event.last_done),sdk_stamp(event.timestamp),now(),
                    volume=float(event.volume)*self.units.get(symbol,1),turnover=float(event.turnover),
                    change_pct=(float(event.last_done)/meta['previous_close']-1)*100 if meta.get('previous_close') else None,
                    quality='realtime' if self.realtime_entitled(symbol) else 'subscription_unverified',
                    trade_status=str(getattr(event,'trade_status',meta.get('trade_status','Unknown'))).split('.')[-1],
                    session='regular' if is_open(now(),symbol_market(symbol)) else 'closed')
            if self.on_quote and self.loop:self.loop.call_soon_threadsafe(self.on_quote,q)
        except Exception:pass

    def _bar(self,symbol,event):
        try:
            r=event.candlestick
            if not getattr(event,'is_confirmed',False):return
            v=share_volume(float(r.volume),float(r.turnover),float(r.low),float(r.high))
            b=Bar(symbol,'longbridge',sdk_stamp(r.timestamp),float(r.open),float(r.high),float(r.low),float(r.close),v,float(r.turnover))
            if self.on_bar and self.loop:self.loop.call_soon_threadsafe(self.on_bar,b)
        except Exception:pass

    async def subscribe(self,symbols):
        if not self.ctx:return
        from longbridge.openapi import SubType,Period,TradeSessions
        target=set(symbols)
        async with self.lock:
            for s in self.subscribed-target:
                await asyncio.to_thread(self.ctx.unsubscribe,[s],[SubType.Quote])
                await asyncio.to_thread(self.ctx.unsubscribe_candlesticks,s,Period.Min_5)
                self.subscribed.remove(s)
            for s in target-self.subscribed:
                await asyncio.to_thread(self.ctx.subscribe,[s],[SubType.Quote])
                await asyncio.to_thread(self.ctx.subscribe_candlesticks,s,Period.Min_5,TradeSessions.Intraday)
                self.subscribed.add(s)

    async def healthcheck(self):
        if not self.ctx:return False
        start=time.monotonic()
        try:
            async with self.lock:
                await asyncio.wait_for(asyncio.to_thread(self.ctx.subscriptions),12)
            self.ok(time.monotonic()-start,'SDK连接与订阅检查正常');return True
        except Exception:
            self.fail('持续连接失联，暂停新信号');return False

    async def verify_access(self):
        if not self.ctx:return
        from longbridge.openapi import SubType,Period,TradeSessions
        samples=[];access={};packages=[]
        try:
            async with self.lock:
                packages=await asyncio.wait_for(asyncio.to_thread(self.ctx.quote_package_details),12)
            self.status['packages']=[{'name':p.name,'description':p.description,'expires':sdk_stamp(p.end_at).isoformat()} for p in packages]
        except Exception:self.status['packages']=[]
        for market,symbol in [('US','AAPL.US'),('SH','600519.SH'),('SZ','300750.SZ')]:
            item={'symbol':symbol,'quote':False,'depth':False,'subscription':False,'bars':False}
            async with self.lock:
                try:
                    quotes=await asyncio.wait_for(asyncio.to_thread(self.ctx.quote,[symbol]),12)
                    if quotes:
                        r=quotes[0];item['quote']=True
                        q=Quote(symbol,'longbridge',symbol,float(r.last_done),sdk_stamp(r.timestamp),now(),quality='subscription_unverified',session='closed' if not is_open(now(),symbol_market(symbol)) else 'regular')
                        samples.append(q)
                except Exception:pass
                try:
                    depth=await asyncio.wait_for(asyncio.to_thread(self.ctx.depth,symbol),12)
                    bid=next((number(r.price) for r in depth.bids if number(r.price,0)>0),None)
                    ask=next((number(r.price) for r in depth.asks if number(r.price,0)>0),None)
                    item['depth']=bool(bid and ask and ask>=bid)
                    if samples and samples[-1].symbol==symbol:samples[-1].bid=bid;samples[-1].ask=ask
                except Exception:pass
                try:
                    await asyncio.wait_for(asyncio.to_thread(self.ctx.subscribe,[symbol],[SubType.Quote]),12)
                    item['subscription']=True
                    if symbol not in self.subscribed:await asyncio.to_thread(self.ctx.unsubscribe,[symbol],[SubType.Quote])
                except Exception:pass
                try:
                    bars=await asyncio.wait_for(asyncio.to_thread(self.ctx.subscribe_candlesticks,symbol,Period.Min_5,TradeSessions.Intraday),12)
                    item['bars']=bool(bars)
                    if symbol not in self.subscribed:await asyncio.to_thread(self.ctx.unsubscribe_candlesticks,symbol,Period.Min_5)
                except Exception:pass
            access[market]=item
        self.status['market_access']=access
        self.status['verified_at']=now().isoformat()
        self.status['message']='SDK已连接；各市场权限与样本见下方，盘中时效待验证'
        for q in samples:
            if self.on_quote:self.on_quote(q)
        return access

class IBKR(Provider):
    name='ibkr'
    def __init__(self):
        super().__init__();self.ib=None;self.contracts={};self.verified=set()
        self.status.update(state='not_configured',message='现有Codex连接可查询；本地TWS／Gateway接口待连接')

    async def connect(self,port=7497):
        if self.ib and self.ib.isConnected():return
        from ib_async import IB,StartupFetch
        self.ib=IB()
        try:
            await self.ib.connectAsync('127.0.0.1',port=port,clientId=91,timeout=8,readonly=True,fetchFields=StartupFetch(0))
            self.contracts.clear();self.verified.clear()
            self.status.update(state='connected',message='本地只读连接成功；需逐股票验证实时权限',authorized=True,port=port,last_ok=now().isoformat())
        except Exception:
            self.ib=None;self.fail('未连接本地TWS／Gateway，请开启只读API后重试')

    async def contract(self,symbol):
        from ib_async import Stock
        if symbol not in self.contracts:
            rows=await self.ib.qualifyContractsAsync(Stock(symbol.split('.')[0],'SMART','USD'))
            if not rows:raise RuntimeError('无法确定证券')
            self.contracts[symbol]=rows[0]
        return self.contracts[symbol]

    async def quotes(self,symbols):
        if not self.ib or not self.ib.isConnected():return []
        result=[];start=time.monotonic()
        try:
            for symbol in symbols:
                if not symbol.endswith('.US'):continue
                self.verified.discard(symbol)
                c=await self.contract(symbol)
                # Snapshot flag false = streaming request, no regulatory snapshot charge.
                ticker=self.ib.reqMktData(c,'',False,False)
                try:
                    await asyncio.sleep(1.2)
                    last=number(ticker.last)
                    realtime=ticker.marketDataType==1 and last is not None and last>0
                    if realtime:self.verified.add(symbol)
                    if last and last>0:
                        result.append(Quote(symbol,'ibkr',symbol,last,stamp(ticker.time) if ticker.time else None,now(),
                         volume=number(ticker.volume),bid=number(ticker.bid),ask=number(ticker.ask),
                         quality='realtime' if realtime else 'delayed',session='regular'))
                finally:self.ib.cancelMktData(c)
            self.ok(time.monotonic()-start,'本地行情查询完成，实时权限逐标的确认')
        except Exception:
            self.verified.clear();self.fail('盈透行情查询失败／权限不足')
        return result

    async def account_positions(self):
        """Read current long U.S. stock positions from the local read-only session."""
        if not self.ib or not self.ib.isConnected():
            raise RuntimeError('盈透本地接口尚未连接')
        try:
            rows=await asyncio.wait_for(self.ib.reqPositionsAsync(),8)
        except Exception:
            rows=self.ib.positions()
            if not rows:raise RuntimeError('盈透持仓请求超时，保留上次同步结果')
        result=[]
        for row in rows:
            contract=getattr(row,'contract',None)
            quantity=number(getattr(row,'position',None),0)
            cost=number(getattr(row,'avgCost',None),0)
            if not contract or getattr(contract,'secType','')!='STK' or quantity<=0 or cost<=0:continue
            if str(getattr(contract,'currency','USD')).upper()!='USD':continue
            symbol=str(getattr(contract,'symbol','')).upper()+'.US'
            if not in_scope(symbol):continue
            result.append({'symbol':symbol,'name':symbol,'quantity':quantity,'available':None,
                           'cost':cost,'currency':'USD'})
        return result

    async def bars(self,symbol):
        if symbol not in self.verified:raise RuntimeError('备用源实时权限未验证')
        c=await self.contract(symbol)
        rows=await self.ib.reqHistoricalDataAsync(c,endDateTime='',durationStr='3 D',barSizeSetting='5 mins',whatToShow='TRADES',useRTH=True,formatDate=2,timeout=15)
        return [Bar(symbol,'ibkr',stamp(r.date),r.open,r.high,r.low,r.close,float(r.volume),float(r.average)*float(r.volume),final=stamp(r.date)+timedelta(minutes=5)<=now()) for r in rows]
