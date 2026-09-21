'use strict';

const $=selector=>document.querySelector(selector);
const $$=selector=>[...document.querySelectorAll(selector)];
const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const marketOf=symbol=>String(symbol||'').endsWith('.US')?'US':'CN';
const validMarkets=['CN','US'];
const validViews=['today','premarket','holdings','review','strategies','analyze','gpt','strategy','system'];
const query=new URLSearchParams(location.search);
let state=null;
const savedMarket=readLocal('shortlist-market','CN');
let currentMarket=validMarkets.includes(query.get('market'))?query.get('market'):(validMarkets.includes(savedMarket)?savedMarket:'CN');
let currentView=validViews.includes(query.get('view'))?query.get('view'):'today';
let holdingKind='real',riskFilter='all',strategyHorizon='all',selectedSymbol=null,selectedStrategy='breakout',analysisResult=null,chartBars=[],chartLoadedAt=0,refreshTimer=null,stateRefreshing=false,stateRefreshQueued=false;
let analysisRefreshing=false,lastAnalysisRefresh=0,refreshFallbackTimer=null;
let gptBundle=null,gptRun=null,gptMode='auto';
const researchComparisons={};
let manualHistory={at:0,rows:[],loading:false},executionContext=null;

function readLocal(key,fallback){try{return localStorage.getItem(key)||fallback}catch{return fallback}}
function writeLocal(key,value){try{localStorage.setItem(key,value)}catch{}}
function readSession(key,fallback=null){try{return JSON.parse(sessionStorage.getItem(key))??fallback}catch{return fallback}}
function writeSession(key,value){try{sessionStorage.setItem(key,JSON.stringify(value))}catch{}}
function num(value,digits=2){return value==null||!Number.isFinite(Number(value))?'—':Number(value).toLocaleString('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits})}
function compact(value){if(value==null||!Number.isFinite(Number(value)))return '—';const n=Number(value);return Math.abs(n)>=1e8?`${num(n/1e8)}亿`:Math.abs(n)>=1e4?`${num(n/1e4)}万`:num(n)}
function pct(value){return value==null||!Number.isFinite(Number(value))?'—':`${Number(value)>0?'+':''}${num(value)}%`}
function tone(value){return Number(value)>0?'up':Number(value)<0?'down':''}
function money(value,market=currentMarket){const sign=market==='CN'?'¥':'$';return value==null||!Number.isFinite(Number(value))?'—':`${sign}${num(value)}`}
function source(value){return ({longbridge:'长桥',ibkr:'盈透',lingxi:'灵犀',manual:'手工录入'}[value]||value||'待核验')}
function strategyName(value){return ({first_pullback:'强势首次回踩',breakout:'开盘区间突破',pullback:'首次回调再启动',trend_pullback:'趋势回踩再启动',volatility_breakout:'波动收缩突破',trend_rsi_pullback:'趋势RSI回踩',vcp_swing:'VCP波动收缩突破',orb20_us:'美股20分钟开盘突破'}[value]||value||'等待盘中形态')}
function horizonName(value){return ({intraday:'日内',short:'短线1–3日',swing:'波段5–10日'}[value]||(value?'其他周期':'多策略观察'))}
function exitRule(row){const policy=row?.exit_policy||{};if(policy.type==='orb_fixed')return `开盘区间固定止损与止盈，最迟收盘前${policy.flat_minutes_before_close||10}分钟退出，不隔夜。`;if(policy.trail==='daily_3low')return `达到2倍风险后减半，余仓按完整日线3日低点上移保护，最迟第${row.max_hold_sessions||10}个交易日退出。`;return `达到2倍风险后减半，余仓按完整3根5分钟K线低点上移保护，最迟第${row?.max_hold_sessions||3}个交易日退出。`}
function rateInterval(row){const range=row?.win_rate_interval;if(!range||range[0]==null)return '—';return `${pct(range[0]*100)}–${pct(range[1]*100)}`}
function strategyNames(row){const items=row?.strategies?.length?row.strategies:[row?.strategy];return items.filter(Boolean).map(strategyName).join(' ＋ ')||'等待盘中形态'}
function riskName(value){return ({normal:'普通股',smallcap:'高波动小盘',st:'ST / *ST',pending:'待核验'}[value]||'待核验')}
function time(value,zone='Asia/Shanghai',withDate=true){if(!value)return '时间未提供';try{return new Intl.DateTimeFormat('zh-CN',{timeZone:zone,month:withDate?'2-digit':undefined,day:withDate?'2-digit':undefined,hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value))}catch{return '时间无效'}}
function dateOnly(value,zone='Asia/Shanghai'){if(!value)return '—';try{return new Intl.DateTimeFormat('zh-CN',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value))}catch{return '—'}}
function toast(message){const node=$('#toast');node.textContent=message;node.style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>node.style.display='none',4200)}
async function api(path,body,signal){const options=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Local':'1'},body:JSON.stringify(body)};if(signal)options.signal=signal;const response=await fetch(path,options);let data={};try{data=await response.json()}catch{}if(!response.ok)throw new Error(typeof data.detail==='object'?(data.detail.message||'操作未完成'):(data.detail||'操作未完成'));return data}
async function action(path,body={},message='已完成'){try{await api(path,body);toast(message);await refresh();return true}catch(error){toast(error.message);return false}}
function workspace(){return state?.workspaces?.[currentMarket]}
function updateUrl(){const url=new URL(location.href);url.searchParams.set('market',currentMarket);url.searchParams.set('view',currentView);history.replaceState(null,'',url)}
function showView(view){currentView=validViews.includes(view)?view:'today';$$('.page').forEach(node=>node.classList.toggle('hidden',node.id!==currentView));$$('[data-view]').forEach(node=>node.classList.toggle('active',node.dataset.view===currentView));updateUrl();if(state)renderCurrent()}
function setMarket(market){if(!validMarkets.includes(market))return;currentMarket=market;writeLocal('shortlist-market',market);document.documentElement.dataset.market=market;$$('[data-workspace]').forEach(node=>node.classList.toggle('selected',node.dataset.workspace===market));selectedSymbol=null;chartBars=[];chartLoadedAt=0;clearGptPreview();updateUrl();if(state)render()}
function scheduleRefresh(immediate=false){
  if(document.visibilityState==='hidden')return;
  if(stateRefreshing){stateRefreshQueued=true;return}
  if(immediate===true){clearTimeout(refreshTimer);refreshTimer=null;void refresh();return}
  // Keep the first deadline: frequent pushes must never postpone a refresh.
  if(refreshTimer!==null)return;
  refreshTimer=setTimeout(()=>{refreshTimer=null;if(document.visibilityState!=='hidden')void refresh()},500);
}
function armRefreshFallback(){
  clearTimeout(refreshFallbackTimer);
  refreshFallbackTimer=setTimeout(()=>{
    refreshFallbackTimer=null;
    if(document.visibilityState!=='hidden'&&!stateRefreshing&&refreshTimer===null)scheduleRefresh(true);
    armRefreshFallback();
  },document.visibilityState==='hidden'?30000:5000);
}
function resumeStateUpdates(){
  armRefreshFallback();
  if(document.visibilityState!=='hidden')scheduleRefresh(true);
  else{clearTimeout(refreshTimer);refreshTimer=null;stateRefreshQueued=false}
}
function stateDisconnected(message){
  document.body.classList.add('disconnected');
  if(state&&currentView==='today')renderToday();
  $('#market-phase').textContent='本地状态待恢复';$('#market-clock').textContent=message;
}
function renderStateUpdate(){
  const editing=document.activeElement?.matches('input,textarea,select,[contenteditable="true"]');
  if(editing&&['strategies','system','gpt'].includes(currentView)){
    const w=workspace();if(w)renderShell(w);return;
  }
  render();
}
async function refresh(){
  if(stateRefreshing){stateRefreshQueued=true;return}
  clearTimeout(refreshTimer);refreshTimer=null;stateRefreshing=true;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),8000);
  try{
    const next=await api('/api/state',undefined,controller.signal);if(!next.workspaces)throw new Error('后台正在更新，请稍后刷新');
    state=next;document.body.classList.remove('disconnected');
    if(analysisResult)analysisResult.monitored=state.watch.includes(analysisResult.symbol);
    renderStateUpdate();refreshMonitoredAnalysis();
  }
  catch(error){stateDisconnected(error.name==='AbortError'?'状态读取超时，正在自动重试':error.message)}
  finally{
    clearTimeout(timeout);stateRefreshing=false;armRefreshFallback();
    if(stateRefreshQueued){stateRefreshQueued=false;scheduleRefresh()}
  }
}

function render(){const w=workspace();if(!w)return;renderShell(w);renderCurrent()}
function renderCurrent(){if(currentView==='today')renderToday();if(currentView==='premarket')renderPremarket();if(currentView==='holdings')renderHoldings();if(currentView==='review')renderReview();if(currentView==='strategies')renderStrategyCenter();if(currentView==='analyze')renderAnalyze();if(currentView==='gpt')renderGpt();if(currentView==='strategy')renderStrategy();if(currentView==='system')renderSystem()}
function renderShell(w){
  document.documentElement.dataset.market=currentMarket;
  $('#sidebar-market').textContent=`${w.meta.name}工作区`;
  $$('[data-workspace]').forEach(node=>node.classList.toggle('selected',node.dataset.workspace===currentMarket));
  const ny=time(state.time,'America/New_York',false),cn=time(state.time,'Asia/Shanghai',false);
  $('#market-phase').textContent=`${w.meta.name} · ${w.meta.phase}`;
  $('#market-clock').textContent=currentMarket==='US'?`美东 ${ny} · 北京 ${cn}`:`北京 ${cn} · 基准 ${w.meta.benchmark}`;
  ['today','premarket','holdings','review','strategies','analyze','gpt','strategy'].forEach(id=>{const node=$(`#${id}-kicker`);if(node)node.textContent=`${w.meta.name} · ${{today:'今日决策',premarket:'动态研究池',holdings:'持仓管理',review:'复盘研究',strategies:'策略中心',analyze:'单股分析',gpt:'GPT 选股',strategy:'策略说明'}[id]}`});
  $('#lingxi-panel').classList.toggle('hidden',currentMarket!=='CN');
}
function healthStrip(w){const h=w.health,age=w.meta.open&&h.quote_age_seconds!=null?` · ${num(h.quote_age_seconds,0)}秒前`:'';return `<div class="health-item"><span>行情状态</span><b class="health-state ${esc(h.state)}">${esc(h.text)}</b><small>${esc(h.transport_label||'等待连接')}</small></div><div class="health-item"><span>监测覆盖</span><b>${h.tracked}只 / 已预热${h.ready}只</b><small>推送订阅 ${h.active_subscriptions??0} / 名额 ${h.monitor_limit??state.settings.monitor_limit}</small></div><div class="health-item"><span>行情截至</span><b>${esc(time(h.data_as_of,w.meta.timezone))}${age}</b><small>最新完整5分钟 ${esc(time(h.last_bar_at,w.meta.timezone))}</small></div><div class="health-item"><span>基准与时区</span><b>${esc(w.meta.benchmark)} · ${currentMarket==='CN'?'北京时间':'美东时间'}</b><small>日常盯盘不调用AI</small></div>`}

function marketPlans(){
  if(!Array.isArray(state?.plans?.[currentMarket])||(currentMarket==='US'&&!state.plans.US.length))return null;
  return state.plans[currentMarket].map(row=>{
    if(row.state!=='buyable')return row;
    const t=Date.now(),signalEnd=Date.parse(row.signal_valid_until),quoteTime=Date.parse(row.quote_time);
    let reason=null,next='recovering',label='待恢复';
    if(!workspace()?.meta.open){reason='当前不是正常交易时段，等待开盘后重新验证';next='execution_check';label='执行核验'}
    else if(document.body.classList.contains('disconnected'))reason='本地服务连接中断，暂停当前买入判断';
    else if(!Number.isFinite(signalEnd)||t>=signalEnd){reason='上一买点已过期，等待新完整K线重新触发';next='waiting_trigger';label='等待触发'}
    else if(!Number.isFinite(quoteTime)||t-quoteTime>30000||quoteTime-t>1000)reason='报价已过期，等待新报价重新核验';
    return reason?{...row,state:next,state_label:label,reason}:row;
  });
}
function usablePlan(row){return !['bought','invalid','expired'].includes(row.state)}
function planChecks(row){const checks=row.checks||state.research_context?.[row.symbol]?.checks||[];return checks.length?`<details class="plan-checks"><summary>逐项条件 · ${checks.filter(c=>c.passed).length}/${checks.length}已通过</summary>${checks.map(c=>`<div><b class="${c.passed?'pass':''}">${c.passed?'✓':'○'} ${esc(c.layer||c.name||'检查')}</b><span>${esc(c.reason||'待核验')}</span></div>`).join('')}</details>`:''}
function planCard(row,compactCard=false){
  const buy=row.state==='buyable',w=workspace();
  return `<article class="decision-card plan-card ${buy?'buyable-plan':''} ${compactCard?'compact-plan':''}"><div class="card-top"><div><span class="kicker">${esc(strategyName(row.strategy))} · ${esc(horizonName(row.horizon))}</span><h2>${esc(row.name||row.symbol)}</h2><small>${esc(row.symbol)} · ${esc(row.strategy_version||row.version||'版本待核验')}</small></div><span class="state-chip ${buy?'buy':''}">${esc(row.state_label||(buy?'当前可买':'等待触发'))}</span></div><span class="trial-label">${row.trial===false?'规则计划':'试运行 · 尚未验证胜率'}</span><p class="plan-reason">${esc(row.reason||'等待下一根完整5分钟K线核验')}</p><div class="plan-price"><b>${money(row.current_price??row.price)}</b><small>行情 ${esc(time(row.quote_time,w.meta.timezone))}</small></div><div class="plan-levels"><div><span>买入区间</span><b>${money(row.entry_min)}–${money(row.entry_max)}</b></div><div><span>禁止追价上限</span><b>${money(row.entry_max)}</b></div><div><span>结构止损</span><b>${money(row.stop)}</b></div><div><span>第一止盈</span><b>${money(row.target)}</b></div><div><span>成本后盈亏比</span><b>${row.net_rr==null?'待核验':`${num(row.net_rr)} : 1`}</b></div><div><span>最长持有</span><b>${num(row.max_hold_sessions,0)}个交易日</b></div></div><div class="plan-validity"><span>买点复核截至 ${esc(time(row.signal_valid_until,w.meta.timezone))}</span><span>结构计划截至 ${esc(time(row.valid_until,w.meta.timezone))}</span></div>${planChecks(row)}<details class="plan-exit"><summary>买后退出规则</summary><p>${esc(exitRule(row))}</p></details><div class="plan-actions"><button class="${buy?'primary':'secondary'}" data-confirm-plan="${esc(row.id)}">登记已实际买入</button><button class="text-button" data-history="${esc(row.symbol)}">候选变化</button></div></article>`;
}
function renderToday(){
  const w=workspace(),d=w.decision,h=w.health,plans=marketPlans(),active=plans?.filter(usablePlan),buys=active?.filter(r=>r.state==='buyable')||[],waiting=active?.filter(r=>r.state!=='buyable')||[];
  $('#health-strip').innerHTML=healthStrip(w);renderAutoResearch(w);
  const s=d.session_summary||{},manual=w.holdings.real||[],urgent=manual.filter(r=>r.event),hasPlans=plans!==null;
  const headline=hasPlans?(urgent.length?'先处理持仓退出':buys.length?`${buys.length}个计划通过当前买入检查`:'当前没有通过全部条件的买点'):d.headline;
  const reason=hasPlans?(urgent.length?'退出条件已触发，请结合实际可卖数量处理。':buys.length?'买入区间与退出价已列出；价格越过上限时等待下一次机会。':waiting[0]?.reason||'系统持续从整个支持范围发现候选，新的机会会自动进入下方计划。'):d.reason;
  $('#decision-hero').innerHTML=`<div><span class="kicker">${esc(w.meta.name)} · ${esc(w.meta.phase)}</span><div class="decision-word">${esc(headline)}</div><p>${esc(reason)}</p><div class="decision-facts"><span>当前可买 ${hasPlans?buys.length:s.buyable_now??0}</span><span>等待确认 ${hasPlans?waiting.length:d.watching.length}</span><span>持仓待处理 ${urgent.length}</span><span>研究池最多80只 · 动态进出</span></div></div><span class="decision-mark ${urgent.length?'manage':''}">${urgent.length?'处理持仓':(hasPlans?buys.length:d.primary)?'当前可买':'等待条件'}</span>`;
  if(hasPlans){
    $('#primary-decision').innerHTML=buys[0]?planCard(buys[0]):`<div class="empty-primary"><div><div class="empty-icon">○</div><h2>暂无当前可买</h2><p>${esc(reason)}。这里最多展示3只，条件不完整时保持空缺。</p><button class="secondary" data-go="premarket">查看动态研究池</button></div></div>`;
    $('#backup-decisions').innerHTML=buys.slice(1,3).map(row=>planCard(row,true)).join('')||'<div class="empty-backup"><b>其余买入位置暂空</b><p>全范围持续发现新机会，不为凑满位置降低条件。</p></div>';
  }else{
    $('#primary-decision').innerHTML=d.primary?primaryCard(d.primary,w):emptyPrimary(d,w);
    $('#backup-decisions').innerHTML=d.backups.slice(0,2).map((row,index)=>backupCard({...row,slotType:'buy'},index+2)).join('')||'<div class="empty-backup">当前暂无更多通过执行检查的计划。</div>';
  }
  const observations=(d.watching||[]).filter(row=>!active?.some(p=>p.symbol===row.symbol)).slice(0,3);
  $('#waiting-plans-count').textContent=waiting.length?`${waiting.length}个计划${waiting.length>6?' · 显示前6个':''}`:`${observations.length}只观察候选`;
  $('#waiting-plans').innerHTML=waiting.length?waiting.slice(0,6).map(row=>planCard(row,true)).join(''):observations.length?observations.map(row=>`<article class="decision-card compact-plan"><span class="kicker">观察候选 · 尚未形成买卖计划</span><h3>${esc(row.name||row.symbol)}</h3><small>${esc(row.symbol)} · ${esc(strategyNames(row))}</small><p class="plan-reason">${esc(row.reason||'等待分钟形态与完整执行条件')}</p><button class="text-button" data-history="${esc(row.symbol)}">查看重新检查的条件</button></article>`).join(''):'<div class="empty">尚未形成待触发的结构计划。完整K线通过策略条件后，会自动出现在这里。</div>';
  $('#holding-actions').innerHTML=manual.length?[...urgent,...manual.filter(r=>!r.event)].slice(0,4).map(row=>holdingActionRow({...row,holding_type:'real'})).join(''):'<div class="empty">登记实际买入后，这里持续跟踪保护价、止盈价和时间退出。<br><button class="text-button" id="add-holding">手工录入已买入股票</button></div>';
  const planReasons=waiting.map(row=>`${row.name||row.symbol}：${row.reason}`),scanReasons=(s.wait_reasons||[]).map(row=>`${row.count}只：${row.reason}`),reasons=[...planReasons,...h.issues,...scanReasons];if(!reasons.length&&!buys.length)reasons.push(reason);
  $('#waiting-reasons').innerHTML=[...new Set(reasons.filter(Boolean))].slice(0,5).map((item,index)=>`<div class="list-row"><div><b>${index===0?'主要原因':'其他原因'}</b></div><div>${esc(item)}</div><span class="action-chip">未通过</span></div>`).join('')||'<div class="empty">当前展示计划的条件完整，仍需在有效价格范围内执行。</div>';
  $('#market-alerts').innerHTML=w.alerts.length?w.alerts.slice(0,6).map(alertRow).join(''):'<div class="empty">当前市场暂无提醒。</div>';
}
function emptyPrimary(d,w){return `<div class="empty-primary"><div><div class="empty-icon">○</div><h2>${esc(d.headline)}</h2><p>${esc(d.reason)}。盘前名单仍会继续观察，但不会为了填满推荐位而显示买入。</p><button class="secondary" data-go="premarket">查看${esc(w.meta.name)}观察名单</button></div></div>`}
function primaryCard(row,w){const price=row.current_price??row.price;return `<article class="decision-card primary-card"><div class="card-top"><div class="stock-title"><span class="kicker">综合首选 · ${esc(strategyNames(row))} · ${esc(horizonName(row.horizon))}</span><h2>${esc(row.name||row.symbol)}</h2><small>${esc(row.symbol)} · ${esc(riskName(row.risk_group))}</small></div><span class="verdict">可考虑买入</span></div><div class="live-price">${money(price)} <small class="quote-time">行情 ${esc(time(row.quote_time,w.meta.timezone))}</small></div><div class="entry-band"><div><span>买入区间</span><b>${money(row.entry_min)}–${money(row.entry_max)}</b></div><div><span>不追价上限</span><b>${money(row.entry_max)}</b></div></div><div class="level-grid"><div><span>初始止损</span><b>${money(row.stop)}</b></div><div><span>第一止盈</span><b>${money(row.target)}</b></div><div><span>建议数量</span><b>${num(row.qty,0)}股</b></div><div><span>占用资金</span><b>${money(row.cash_required)}</b></div><div><span>计划风险</span><b>${money(row.planned_risk)}</b></div><div><span>风险距离</span><b>${money(row.price_risk)}</b></div><div><span>信号失效</span><b>${esc(time(row.valid_until,w.meta.timezone))}</b></div><div><span>计划周期</span><b>${esc(horizonName(row.horizon))}</b></div></div><div class="exit-rule"><b>退出计划：</b>${esc(exitRule(row))}</div><div class="card-foot"><span>${esc(row.reason)}</span><span>${esc(source(row.source))} · ${esc(row.version||state.version)}</span></div></article>`}
function backupCard(row,index){const buy=row.slotType==='buy';const price=row.current_price??row.price??row.quote?.price;return `<article class="decision-card backup-card"><div class="card-top"><div><span class="kicker">备选 ${index} · ${buy?'已通过执行检查':'观察备选'} · ${esc(horizonName(row.horizon))}</span><h3>${esc(row.name||row.symbol)} <small>${esc(row.symbol)}</small></h3></div><span class="state-chip ${buy?'buy':''}">${buy?'可考虑买入':'继续观察'}</span></div><p>${esc(row.reason||'等待盘中确认')}</p><div class="backup-levels"><div>现价<b>${money(price)}</b></div><div>${buy?'买入区间':'突破观察位'}<b>${buy?`${money(row.entry_min)}–${money(row.entry_max)}`:money(row.trigger)}</b></div><div>保护参考<b>${money(row.stop)}</b></div></div></article>`}
function holdingActionRow(row){return `<div class="list-row holding-action"><div><b>${esc(row.name||row.symbol)}</b><small>${esc(row.symbol)} · 剩余${num(row.quantity??row.remaining,0)}股</small></div><div>${esc(row.reason||'等待条件')}<small>现价 ${money(row.price,marketOf(row.symbol))} · 保护 ${money(row.stop,marketOf(row.symbol))} · 止盈 ${money(row.target,marketOf(row.symbol))}</small><small>可卖 ${num(row.available,0)}股 · 行情 ${esc(time(row.quote_time,workspace().meta.timezone))}</small></div><div><span class="action-chip ${row.event?'urgent':''}">${esc(row.action||'继续持有')}</span>${row.source==='manual'?`<button class="text-button" data-sell-holding="${esc(row.id)}">登记实际卖出</button>`:''}</div></div>`}
function alertRow(row){return `<div class="alert-row ${row.read?'read':''}"><time>${esc(time(row.time,workspace().meta.timezone))}</time><b>${esc(row.title)}</b><span>${esc(row.message)}</span>${row.read?'':`<button class="text-button" data-alert-read="${esc(row.id)}">标为已读</button>`}</div>`}

function renderPremarket(){
  const w=workspace(),rows=w.premarket.filter(row=>{const q=$('#premarket-search').value.trim().toUpperCase();return (!q||row.symbol.includes(q)||String(row.name).toUpperCase().includes(q))&&(riskFilter==='all'||row.risk_group===riskFilter)}),coverage=state.coverage||{},candidates=coverage.candidates_by_market?.[currentMarket]??w.candidates.length,strategyCandidates=coverage.strategy_candidates_by_market?.[currentMarket]??0,supplements=coverage.supplement_candidates_by_market?.[currentMarket]??candidates,pool=coverage.research_pool_by_market?.[currentMarket]??candidates,checked=coverage.selection_by_market?.[currentMarket]??state.selection.filter(row=>row.market===currentMarket).length,poolStatus=coverage.pool_status_by_market?.[currentMarket]||{};
  const freshness=poolStatus.stale?'过期快照':`交易日 ${poolStatus.trade_date||'待生成'}`,freshnessDetail=poolStatus.error||`${time(poolStatus.updated_at,w.meta.timezone)}更新`;
  $('#premarket-summary').innerHTML=`<div class="health-item"><span>每日核心池</span><b>${poolStatus.core_count??strategyCandidates}只</b><small class="${poolStatus.stale?'stale-text':''}">${esc(freshness)} · ${esc(freshnessDetail)}</small></div><div class="health-item"><span>盘中异动补充</span><b>${poolStatus.supplement_count??supplements}只</b><small>只在本交易日参与重排</small></div><div class="health-item"><span>深度核验 / 已评估</span><b>${pool}只 / ${checked}只</b></div><div class="health-item"><span>盘前入围 / 持续盯盘</span><b>${w.premarket.length}只 / ${w.health.tracked}只</b></div>`;
  if(currentMarket==='CN'){const r=w.research||{};$('#premarket-summary').innerHTML=`<div class="health-item"><span>证券库分页</span><b>${num(r.enumerated||0,0)} / ${num(r.universe_total||0,0)}</b><small>供应商范围，不等于研究完成</small></div><div class="health-item"><span>日线复核 / 支持范围</span><b>${num(r.checked||0,0)} / ${num(r.supported||0,0)}</b><small>缺日线 ${num(r.missing||0,0)} 只</small></div><div class="health-item"><span>有效候选 / 数据待恢复</span><b>${Math.max(0,(r.research_pool||0)-(r.pool_data_missing||0))} / ${r.pool_data_missing||0}</b><small>最多80只，池外持续发现和替换</small></div><div class="health-item"><span>盘前入围 / 持续监测</span><b>${w.premarket.length} / ${w.health.tracked}</b><small>保护名额 ${r.protected_slots||0} 个</small></div>`}
  const screenErrors=coverage.strategy_screen?.errors?.length||0;
  $('#premarket-scope').innerHTML=currentMarket==='CN'?`<b>自动研究范围：</b><span>${esc(w.research?.scope||'沪深主板和创业板')}。分页证券库 → 逐股日线与策略检查 → 最多80只研究池 → 最多12只实时监测。</span><small>已检查 ${num(w.research?.checked||0,0)} / ${num(w.research?.supported||0,0)}；缺日线 ${num(w.research?.missing||0,0)}；盘中每5分钟从全范围重新发现，每30分钟复核完整资格。${esc(w.research?.reason||'等待首次自动扫描')}。榜单不是入选条件。</small>`:`<b>美股候选范围：</b><span>长桥筛选返回候选，经同源日线核验后展示；不代表全部美股已完成复核。</span>`;
  $('#refresh-premarket').disabled=Boolean(state.selection_running||state.scanning);$('#refresh-premarket').textContent=state.selection_running?`双市场日线复核 ${state.selection_progress.done}/${state.selection_progress.total}`:state.scanning?'灵犀按策略筛选中…':'重新运行全市场策略筛选';
  if(currentMarket==='CN'){$('#refresh-premarket').disabled=Boolean(w.research?.running);$('#refresh-premarket').textContent=w.research?.running?'自动研究进行中…':'补充运行范围筛选'}
  $('#premarket-count').textContent=`显示 ${rows.length} / ${w.premarket.length}`;
  if(!rows.some(row=>row.symbol===selectedSymbol)){selectedSymbol=rows[0]?.symbol||null;chartBars=[];chartLoadedAt=0}
  $('#premarket-rows').innerHTML=rows.length?rows.map(row=>`<tr data-symbol="${esc(row.symbol)}" class="${row.symbol===selectedSymbol?'selected':''}"><td><div class="symbol-cell"><span class="rank">${row.rank}</span><div><b>${esc(row.name)}</b><small>${esc(row.symbol)} · ${row.pool_role==='daily_core'?'策略复核候选':row.pool_role==='intraday_supplement'?'盘中补充':'上次有效快照'}</small></div></div></td><td>${esc(row.industry||'行业未知')}</td><td><span class="score">${num(row.score,0)}</span></td><td>${money(row.close)}</td><td>${money(row.breakout_reference)}</td><td>${money(row.structure_low)}</td><td>${compact(row.average_turnover)}</td><td class="${tone(row.distance_to_high_pct)}">${pct(row.distance_to_high_pct)}</td><td><span class="risk-chip ${esc(row.risk_group)}">${esc(riskName(row.risk_group))}</span></td><td class="wait-cell">${esc(row.reason)}</td></tr>`).join(''):'<tr><td colspan="10"><div class="empty">当前没有符合条件的研究候选，系统会继续从支持范围发现。</div></td></tr>';
  renderStockDetail();renderBreadth();
  if(selectedSymbol&&Date.now()-chartLoadedAt>15000)loadChart(selectedSymbol);
}
async function loadChart(symbol){const row=workspace().premarket.find(item=>item.symbol===symbol);chartLoadedAt=Date.now();try{const bars=await api(`/api/bars/${encodeURIComponent(symbol)}?source=${encodeURIComponent(row?.source||'longbridge')}`);if(symbol===selectedSymbol){chartBars=bars;renderStockDetail()}}catch{if(symbol===selectedSymbol){chartBars=[];renderStockDetail()}}}
function renderStockDetail(){const w=workspace(),row=w.premarket.find(item=>item.symbol===selectedSymbol);if(!row){$('#stock-detail').innerHTML='<div class="empty">选择一只股票查看完整依据。</div>';return}const candidate=w.candidates.find(item=>item.symbol===row.symbol),q=candidate?.quote,price=q?.price??row.close,change=q?.change_pct,dataTime=q?.market_time?time(q.market_time,w.meta.timezone):row.as_of||'日期未提供',strategyOrigin=row.candidate_strategies?.length?row.candidate_strategies.map(strategyName).join('、'):row.candidate_origin==='universe'?'分页证券库逐股复核':'成交额／涨幅异动补充';$('#stock-detail').innerHTML=`<div class="detail-header"><span class="kicker">排名 ${row.rank} · ${esc(riskName(row.risk_group))}</span><h2>${esc(row.name)}</h2><small>${esc(row.symbol)}</small><div class="detail-price">${money(price)} <small class="${tone(change)}">${pct(change)}</small></div><div class="detail-meta">${q?'当前报价':'最近完整日线收盘'} · ${esc(source(q?.source||row.source))}<br>数据时间 ${esc(dataTime)}</div></div><div class="chart">${chart(chartBars,w.meta.timezone)}</div><div class="detail-body"><div class="detail-row"><span>入池来源</span><b>${esc(strategyOrigin)}</b></div><div class="detail-row"><span>20日均线</span><b>${money(row.ma20)}</b></div><div class="detail-row"><span>20日相对强弱</span><b>${pct(row.relative_strength)}</b></div><div class="detail-row"><span>平均成交额</span><b>${compact(row.average_turnover)}</b></div><div class="detail-row"><span>日线量比</span><b>${num(row.volume_ratio)}倍</b></div><div class="detail-row"><span>ATR / 价格</span><b>${pct(row.atr_pct)}</b></div><div class="detail-note"><b>等待条件：</b>${esc(row.reason)}<br>观察位不是买入价，仍需盘中完整5分钟K线确认。</div><div class="detail-actions"><button class="secondary" data-history="${esc(row.symbol)}">跟踪历史</button><button class="secondary" data-watch="${esc(row.symbol)}" data-remove="${row.monitored?'1':'0'}">${row.monitored?'移出监测':'加入监测'}</button><button class="secondary" data-ask-gpt="stock" data-symbol="${esc(row.symbol)}">问 GPT</button></div></div>`}
function chart(bars,zone){if(!bars.length)return '<div class="empty">5分钟K线尚未就绪<br><small>不会用示例图代替真实行情</small></div>';const data=bars.slice(-50),width=360,height=155,pad=20,low=Math.min(...data.map(row=>row.low)),high=Math.max(...data.map(row=>row.high)),range=high-low||1,x=index=>pad+index*(width-pad*2)/data.length,y=value=>12+(high-value)/range*(height-35);let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="最近完整5分钟K线">`;for(let index=0;index<3;index++){const value=low+range*index/2;svg+=`<line x1="16" x2="344" y1="${y(value)}" y2="${y(value)}" stroke="#e7eae4"/><text class="chart-label" x="18" y="${y(value)-4}">${num(value)}</text>`}const barWidth=Math.max(1.6,(width-pad*2)/data.length*.58);data.forEach((row,index)=>{const color=row.close>=row.open?'#b34a3e':'#2e7a59';svg+=`<line x1="${x(index)}" x2="${x(index)}" y1="${y(row.high)}" y2="${y(row.low)}" stroke="${color}"/><rect x="${x(index)-barWidth/2}" y="${Math.min(y(row.open),y(row.close))}" width="${barWidth}" height="${Math.max(1,Math.abs(y(row.open)-y(row.close)))}" fill="${color}"/>`});return svg+`<text class="chart-label" x="18" y="153">${esc(time(data[0].start,zone))}</text><text class="chart-label" x="342" y="153" text-anchor="end">5分钟 · ${esc(source(data[0].source))}</text></svg>`}
function renderBreadth(){const w=workspace(),b=w.breadth;$('#breadth-title').textContent=currentMarket==='CN'?'A股市场广度':'美股市场广度';if(!b.available){$('#market-breadth').innerHTML=`<div class="empty">${esc(b.message||'当前市场广度未接入')}，不会复用另一市场的统计。</div>`;return}$('#market-breadth').innerHTML=`<div class="breadth-grid"><div><span>上涨家数</span><b class="up">${num(b.up_count,0)}</b></div><div><span>下跌家数</span><b class="down">${num(b.down_count,0)}</b></div><div><span>涨停家数</span><b>${num(b.up_limit_count,0)}</b></div><div><span>跌停家数</span><b>${num(b.down_limit_count,0)}</b></div></div>`}

function renderHoldings(){
  const w=workspace(),real=w.holdings.real,paper=w.holdings.paper;renderManualHistory();$('#stop-broker-sync').classList.toggle('hidden',!state.settings.broker_sync_enabled);
  $('#holding-summary').innerHTML=`<div class="health-item"><span>真实持仓</span><b>${real.length}只</b><small>手工登记独立于账户同步</small></div><div class="health-item"><span>模拟持仓</span><b>${paper.length}只</b></div><div class="health-item"><span>需处理动作</span><b>${w.holdings.actionable.length}项</b></div><div class="health-item"><span>未纳入监测</span><b>${w.health.uncovered_holdings}只</b></div>`;
  $$('[data-holding-kind]').forEach(node=>node.classList.toggle('selected',node.dataset.holdingKind===holdingKind));
  const rows=holdingKind==='real'?real:paper;
  if(!rows.length){$('#holdings-content').innerHTML=`<div class="empty">本市场暂无${holdingKind==='real'?'真实':'模拟'}持仓。</div>`;return}
  if(holdingKind==='real')$('#holdings-content').innerHTML=`<table class="holdings-table"><thead><tr><th>股票 / 来源</th><th>现价 / 成本</th><th>剩余 / 可卖</th><th>浮动 / 已实现盈亏</th><th>保护 / 止盈</th><th>当前动作</th><th>行情时间</th><th>成交登记</th></tr></thead><tbody>${rows.map(row=>`<tr><td><b>${esc(row.name||row.symbol)}</b><small>${esc(row.symbol)} · ${esc(source(row.source))}</small><small>${esc(strategyName(row.strategy))}</small></td><td>${money(row.price)}<small>成本 ${money(row.cost)}</small></td><td>${num(row.quantity,0)}股<small>可卖 ${num(row.available,0)}股</small></td><td class="pnl ${tone(row.unrealized_pnl)}">${money(row.unrealized_pnl)}<small class="${tone(row.realized_pnl)}">已实现 ${money(row.realized_pnl??0)}</small></td><td>${money(row.stop)}<small>止盈 ${money(row.target)}</small></td><td><span class="action-chip ${row.event?'urgent':''}">${esc(row.action)}</span><small>${esc(row.reason)}</small></td><td>${esc(time(row.quote_time,w.meta.timezone))}</td><td><button class="row-action" data-edit-holding="${esc(row.id)}">编辑计划</button>${row.source==='manual'?`<button class="row-action" data-sell-holding="${esc(row.id)}">登记实际卖出</button>`:''}</td></tr>`).join('')}</tbody></table>`;
  else $('#holdings-content').innerHTML=`<table class="holdings-table"><thead><tr><th>股票 / 策略</th><th>周期</th><th>现价</th><th>买入价</th><th>剩余数量</th><th>保护价</th><th>第一止盈</th><th>当前动作</th><th>行情时间</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${esc(row.name||row.symbol)}<small>${esc(strategyName(row.strategy))}</small></td><td>${esc(horizonName(row.horizon))}</td><td>${money(row.price)}</td><td>${money(row.entry)}</td><td>${num(row.remaining,0)}股</td><td>${money(row.stop)}</td><td>${money(row.target)}</td><td>${esc(row.action)}<small>${esc(row.reason)}</small></td><td>${esc(time(row.quote_time,w.meta.timezone))}</td></tr>`).join('')}</tbody></table>`;
}
function renderManualHistory(){
  $('.manual-ledger').classList.toggle('hidden',holdingKind!=='real');
  if(!manualHistory.loading&&Date.now()-manualHistory.at>30000){manualHistory.loading=true;manualHistory.at=Date.now();api('/api/real-holdings/history').then(data=>{manualHistory.rows=data.rows||[];manualHistory.error=null}).catch(error=>{manualHistory.error=error.message}).finally(()=>{manualHistory.loading=false;if(currentView==='holdings')renderManualHistory()})}
  const rows=manualHistory.rows.filter(row=>marketOf(row.symbol)===currentMarket&&(row.sell_fills?.length||row.status==='closed'));
  $('#manual-history').innerHTML=manualHistory.error?`<p class="form-hint">${esc(manualHistory.error)}</p>`:rows.map(row=>`<details class="manual-history-row"><summary>${esc(row.name||row.symbol)} · ${row.status==='closed'?'已清仓':`剩余${num(row.quantity,0)}股`} · 已实现净盈亏 ${money(row.realized_pnl)}</summary><p class="form-hint">${esc(strategyName(row.strategy))} · ${esc(row.strategy_version||'手工计划')} · 买入 ${money(row.cost)} · 买入费用 ${money(row.entry_fee??0)}</p>${(row.sell_fills||[]).map(fill=>`<div class="manual-fill"><span>${esc(time(fill.time,workspace().meta.timezone))}</span><span>卖出 ${num(fill.quantity,0)}股 × ${money(fill.price)}</span><span>费用 ${money(fill.fee)} · 分摊买入费 ${money(fill.entry_fee_allocated)}</span><b class="${tone(fill.net_pnl)}">${money(fill.net_pnl)}</b></div>`).join('')}</details>`).join('')||'<p class="form-hint">暂无实际卖出记录。模拟交易在复盘页独立统计。</p>';
}
function localDateTimeInput(value){const d=value?new Date(value):new Date();return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16)}
function openExecution(kind,row){
  executionContext={kind,row,executionId:`manual:${Date.now()}:${Math.random().toString(16).slice(2)}`};
  const buy=kind==='buy',market=marketOf(row.symbol);
  $('#execution-title').textContent=buy?'登记已实际买入':'登记已实际卖出';$('#execution-submit').textContent=buy?'确认记录已实际买入':'确认记录已实际卖出';
  $('#execution-summary').innerHTML=`<b>${esc(row.name||row.symbol)} · ${esc(row.symbol)}</b><p>${buy?`${esc(strategyName(row.strategy))} · ${esc(row.strategy_version||'')} · ${esc(row.state_label||row.state)}`:`剩余 ${num(row.quantity,0)}股 · 当前可卖 ${num(row.available,0)}股 · 成本 ${money(row.cost,market)}`}</p><div class="plan-levels"><div><span>${buy?'原计划买入区间':'当前保护价'}</span><b>${buy?`${money(row.entry_min,market)}–${money(row.entry_max,market)}`:money(row.stop,market)}</b></div><div><span>${buy?'原计划止损 / 止盈':'当前止盈价'}</span><b>${buy?`${money(row.stop,market)} / ${money(row.target,market)}`:money(row.target,market)}</b></div></div>${buy&&row.state!=='buyable'?'<p class="stale-text">本计划当前未通过全部买入条件；仅用于补记你已经完成的实际成交。</p>':''}`;
  $('#execution-quantity').value=buy?(row.qty||''):(row.qty||row.quantity||'');$('#execution-quantity').step=market==='CN'?'1':'any';$('#execution-quantity').min=market==='CN'?'1':'0.0001';if(buy)$('#execution-quantity').removeAttribute('max');else $('#execution-quantity').max=row.quantity;
  $('#execution-price').value=buy?(row.current_price??row.price??''):(row.price??'');$('#execution-time').value=localDateTimeInput();$('#execution-fee').value=0;$('#execution-note').value='';$('#execution-feedback').textContent='';$('#execution-dialog').showModal();
}
async function saveExecution(event){
  event.preventDefault();if(!executionContext)return;
  const {kind,row,executionId}=executionContext,button=$('#execution-submit'),date=new Date($('#execution-time').value);
  if(!Number.isFinite(date.getTime())){$('#execution-feedback').textContent='请填写有效成交时间';return}
  const quantity=Number($('#execution-quantity').value),price=Number($('#execution-price').value),fee=Number($('#execution-fee').value),note=$('#execution-note').value.trim(),timestamp=date.toISOString();button.disabled=true;$('#execution-feedback').textContent='正在保存本机成交记录…';
  try{if(kind==='buy')await api(`/api/plans/${encodeURIComponent(row.id)}/confirm-buy`,{quantity,cost:price,entry_time:timestamp,entry_fee:fee,note});else await api('/api/real-holdings/sell',{id:row.id,quantity,price,sold_at:timestamp,fee,execution_id:executionId,note});manualHistory.at=0;$('#execution-dialog').close();toast(kind==='buy'?'已记录实际买入，开始跟踪退出条件':'已记录实际卖出并更新剩余持仓');await refresh()}
  catch(error){$('#execution-feedback').textContent=error.message}finally{button.disabled=false}
}
function openHolding(row=null){const dialog=$('#holding-dialog'),synced=row&&row.source!=='manual';$('#holding-dialog-title').textContent=row?'编辑退出计划':'手工录入';$('#holding-id').value=row?.id||'';$('#holding-source').value=row?.source||'manual';$('#holding-symbol').value=row?.symbol||'';$('#holding-name').value=row?.name||'';$('#holding-quantity').value=row?.quantity??'';$('#holding-cost').value=row?.cost??'';$('#holding-date').value=row?.entry_date||'';$('#holding-stop').value=row?.stop??'';$('#holding-target').value=row?.target??'';$('#holding-note').value=row?.note||'';['holding-symbol','holding-name','holding-quantity','holding-cost'].forEach(id=>{$(`#${id}`).disabled=Boolean(synced||row?.sell_fills?.length)});$('#holding-date').disabled=Boolean(row?.entry_time||row?.sell_fills?.length);$('#remove-holding').classList.toggle('hidden',!row||row.source!=='manual'||Boolean(row.sell_fills?.length));dialog.showModal()}

function renderReview(){const w=workspace(),a=w.simulation;renderResearchReview(w);$('#toggle-simulation').textContent=state.settings.simulation_enabled?'暂停模拟新开仓':'恢复模拟新开仓';$('#review-account').innerHTML=`<section class="account-card"><div><span>${esc(w.meta.name)}组合模拟权益</span><strong>${money(a.equity)}</strong></div><div><span>组合收益</span><strong class="${tone(a.return_pct)}">${pct(a.return_pct)}</strong></div><div><span>组合最大回撤</span><strong>${pct(-a.max_drawdown)}</strong></div><div><span>可用现金</span><strong>${money(a.cash)}</strong></div></section>`;const strategies=w.strategies||[];$('#strategy-metrics').innerHTML=`<div class="metric-list wide-metrics">${strategies.map(item=>{const row=item.performance?.shadow||item.performance||{};return `<div class="metric"><div><b>${esc(item.name)}</b><small>${esc(horizonName(item.horizon))} · ${esc(row.stability||'样本不足')}</small></div><div>结束<b>${row.count??0}</b></div><div>胜率<b>${row.win_rate==null?'—':pct(row.win_rate*100)}</b><small>95% ${rateInterval(row)}</small></div><div>利润因子<b>${num(row.profit_factor)}</b></div><div>期望/笔<b class="${tone(row.expectancy)}">${money(row.expectancy)}</b></div><div>最大回撤<b>${row.max_drawdown==null?'—':pct(-row.max_drawdown)}</b></div></div>`}).join('')}</div>`;const failures=state.failures.filter(row=>marketOf(row.symbol)===currentMarket).slice(-12).reverse();$('#market-failures').innerHTML=failures.length?failures.map(row=>`<div class="event-row"><time>${esc(time(row.time,w.meta.timezone))}</time><b>${esc(row.symbol)}</b><span>${esc(row.reason)}</span></div>`).join(''):'<div class="empty">本市场暂无未成交或取消记录。</div>';$('#market-trades').innerHTML=tradeTable(a.trades,w);const select=$('#replay-symbol');if(select){const old=select.value;select.innerHTML=state.watch.filter(symbol=>marketOf(symbol)===currentMarket).map(symbol=>`<option>${esc(symbol)}</option>`).join('');if([...select.options].some(option=>option.value===old))select.value=old}}
function tradeTable(rows,w){if(!rows.length)return '<div class="empty">暂无已结束交易，继续积累前向样本。</div>';return `<div class="table-wrap"><table><thead><tr><th>股票</th><th>策略 / 分类</th><th>买入时间</th><th>退出时间</th><th>净盈亏</th><th>持有交易日</th></tr></thead><tbody>${rows.slice(-40).reverse().map(row=>`<tr><td>${esc(row.symbol)}</td><td>${esc(strategyName(row.strategy))}<small>${esc(riskName(row.risk_group))}</small></td><td>${esc(time(row.entry_time,w.meta.timezone))}</td><td>${esc(time(row.exit_time,w.meta.timezone))}</td><td class="${tone(row.net_pnl)}">${money(row.net_pnl)}</td><td>${num(row.holding_sessions,0)}</td></tr>`).join('')}</tbody></table></div>`}

function renderStrategyCenter(){
  const w=workspace(),all=w.strategies||[],items=all.filter(row=>strategyHorizon==='all'||row.horizon===strategyHorizon);if(!items.length){$('#strategy-cards').innerHTML='<div class="empty">当前市场没有这一周期的策略。</div>';$('#strategy-workbench').innerHTML='';return}
  $$('[data-horizon]').forEach(node=>node.classList.toggle('selected',node.dataset.horizon===strategyHorizon));
  if(!items.some(row=>row.strategy===selectedStrategy))selectedStrategy=items[0].strategy;
  $('#strategy-cards').innerHTML=items.map(row=>{const p=row.performance?.shadow||row.performance||{};return `<button class="strategy-summary ${row.strategy===selectedStrategy?'selected':''}" data-select-strategy="${esc(row.strategy)}"><span class="kicker">${esc(horizonName(row.horizon))} · ${row.enabled?'运行中':'已停用'}</span><h2>${esc(row.name)}</h2><p>${esc(row.summary)}</p><div class="strategy-mini-metrics"><span><b>${p.count??0}</b><small>影子样本</small></span><span><b>${p.win_rate==null?'—':pct(p.win_rate*100)}</b><small>胜率</small></span><span><b>${num(p.profit_factor)}</b><small>利润因子</small></span></div><small class="confidence">95%胜率区间 ${rateInterval(p)} · ${esc(p.stability||'样本不足')} · 日线要求${row.data_requirements?.daily_bars||'—'}根</small><i class="${row.enabled?'on':''}"></i></button>`}).join('');
  const row=items.find(item=>item.strategy===selectedStrategy),rec=w.strategy_recommendations?.[selectedStrategy]||{rows:[]};
  const candidates=rec.rows?.length?rec.rows.map((candidate,index)=>strategyCandidate(candidate,index)).join(''):'<div class="empty">当前没有符合该策略盘前条件的股票。</div>';
  const waiting=rec.waiting?.length?`<div class="strategy-waiting"><b>继续等待</b><p>${rec.waiting.map(item=>`${esc(item.name||item.symbol)}：${esc(item.reason)}`).join('<br>')}</p></div>`:'';
  $('#strategy-workbench').innerHTML=`<div class="strategy-head"><div><span class="kicker">${esc(w.meta.name)} · ${esc(row.category)} · ${esc(horizonName(row.horizon))}</span><h2>${esc(row.name)}</h2><p>${esc(row.summary)}</p><small>${esc(row.research_reference)} · 当前版本 ${esc(row.version)}</small></div><div class="title-actions"><button class="secondary" data-show-versions="${esc(row.strategy)}">版本记录</button><button class="${row.enabled?'danger-button':'primary'}" data-strategy-toggle="${esc(row.strategy)}" data-enabled="${row.enabled?'0':'1'}">${row.enabled?'停用策略':'启用策略'}</button></div></div><div class="strategy-columns"><section><div class="section-label"><b>该策略今日名单</b><span>首选1只 · 备选2只</span></div><div class="strategy-candidates">${candidates}${waiting}</div><div class="strategy-waiting"><b>退出规则</b><p>${esc(exitRule(row))}</p></div></section><section><div class="section-label"><b>当前参数</b><span>保存后立即生成新版本</span></div><form id="strategy-config-form" data-strategy-id="${esc(row.strategy)}"><div class="parameter-grid">${row.fields.map(field=>`<label>${esc(field.label)}<input data-param="${esc(field.key)}" type="number" value="${esc(field.value)}" min="${esc(field.min)}" max="${esc(field.max)}" step="${field.type==='int'?'1':'any'}"><small>${num(field.min)}–${compact(field.max)}</small></label>`).join('')}</div><label class="change-reason">修改说明<input id="strategy-change-reason" maxlength="200" placeholder="例如：降低成交量门槛做观察实验"></label><button class="primary">保存并立即生效</button><p class="form-hint">新版本会取消该策略旧待买信号；已有持仓的保护计划不变，影子与组合前向样本按版本隔离。</p></form></section></div>`;
}
function strategyCandidate(row,index){const conditions=Object.entries(row.conditions||{});return `<article class="strategy-pick"><div class="card-top"><div><span class="kicker">${index===0?'策略首选':`备选 ${index}`}</span><h3>${esc(row.name||row.symbol)}</h3><small>${esc(row.symbol)} · ${esc(riskName(row.risk_group))}</small></div><span class="state-chip ${row.state==='buy'?'buy':''}">${row.state==='buy'?'可考虑买入':'继续观察'}</span></div><p>${esc(row.reason)}</p><div class="pick-levels"><span>准备度<b>${num(row.score,0)}</b></span><span>观察位<b>${money(row.trigger)}</b></span><span>保护参考<b>${money(row.stop)}</b></span></div><div class="condition-chips">${conditions.map(([key,value])=>`<span class="${value?'pass':'wait'}">${value?'✓':'○'} ${esc(key)}</span>`).join('')}</div></article>`}

function renderAnalyze(){
  if(analysisResult&&analysisResult.market===currentMarket){$('#analyze-result').innerHTML=analysisHtml(analysisResult);return}
  $('#analyze-result').innerHTML='<div class="empty-primary"><div><div class="empty-icon">◎</div><h2>等待输入股票</h2><p>系统先返回可靠的部分判断；只有行情、基准、量能与风控全部通过后，才可能显示买入。</p></div></div>'
}
function analysisHtml(result){
  const w=workspace(),q=result.quote,final=result.final,p=result.data_profile||{},capacity=result.monitoring||{};
  const modeClass=result.mode==='monitoring'?'live':'snapshot',full=capacity.used>=capacity.limit;
  const monitorLabel=result.monitored?`已在持续监测 · ${esc(result.transport_label)}`:full?`监测名额已满 ${capacity.used}/${capacity.limit}`:'＋ 加入盘中监测';
  return `<section class="analysis-hero ${String(final.status).toLowerCase()}"><div><div class="analysis-badges"><span class="mode-chip ${modeClass}">${esc(result.transport_label||'即时快照')}</span><span class="data-chip ${esc(result.data_status)}">${esc(dataStatusName(result.data_status))}</span><span class="mode-chip">${esc(result.market_phase)}</span></div><span class="kicker">${esc(result.name)} · ${esc(result.symbol)}</span><h2>${esc(final.action)}</h2><p>${esc(final.reason)}</p></div><div class="analysis-price"><b>${money(q?.price)}</b><small>${q?`行情 ${esc(time(q.market_time,w.meta.timezone))}<br>本机接收 ${esc(time(q.received_at,w.meta.timezone))}`:'行情未取得'}</small></div></section>
  <div class="analysis-actions"><button class="secondary" data-analysis-monitor="${esc(result.symbol)}" ${result.monitored||full?'disabled':''}>${monitorLabel}</button>${result.holding_origin==='saved'?'<span class="action-chip">已结合已保存或同步持仓</span>':'<button class="secondary" data-analysis-save-holding="1">保存为真实持仓</button>'}<button class="secondary" data-ask-gpt="stock" data-symbol="${esc(result.symbol)}">问 GPT</button><span class="analysis-refresh">下次策略确认 ${esc(time(result.next_confirmation_at,w.meta.timezone))}</span></div>
  ${full&&!result.monitored?`<div class="capacity-note"><b>当前12个监测名额已用完。</b><span>${(capacity.symbols||[]).map(esc).join('、')}</span><button class="text-button" data-go="system">调整名额</button></div>`:''}
  ${analysisDataProfile(result)}${holdingAnalysis(result.holding)}
  <div class="analysis-strategies">${result.strategies.map(row=>`<article class="panel analysis-strategy"><div class="panel-head"><div><span class="kicker">${esc(row.version||'当前市场不适用')} · ${esc(horizonName(row.horizon))}</span><h2>${esc(row.name)}</h2></div><span class="state-chip ${row.state==='符合'?'buy':''}">${esc(row.state)}</span></div><div class="analysis-body"><p>${esc(row.reason)}</p>${analysisLevels(row)}${requirementList(row)}</div></article>`).join('')}</div><p class="analysis-note">${esc(result.note)} · 策略K线来源：${esc(source(result.source))} · 日常监测不调用AI</p>`
}
function dataStatusName(value){return ({full:'数据完整',partial:'可做部分判断',warming:'数据预热中',stale:'行情延迟',unavailable:'数据不可用'}[value]||'待核验')}
function age(value){return value==null?'—':Number(value)<1?'刚刚':`${num(value,0)}秒前`}
function analysisDataProfile(result){
  const p=result.data_profile||{},d=p.daily||{},i=p.intraday||{},v=p.same_time_volume||{},b=p.benchmark||{},q=p.quote||{},depth=p.depth||{},t=result.technical||{};
  const cards=[['日线',`${d.count??0}根`,d.start?`${dateOnly(d.start,workspace().meta.timezone)} 至 ${dateOnly(d.end,workspace().meta.timezone)}`:'尚未取得'],['今日5分钟',`${i.count??0}根`,i.last_bar_at?`最新完整K线 ${time(i.last_bar_at,workspace().meta.timezone)} · ${age(i.age_seconds)}`:'尚未形成'],['同时间量能',`${v.sessions??0}/${v.required??14}日`,(v.sessions??0)>=(v.required??14)?'可用于量比确认':'突破类策略保持等待'],['同源基准',b.synced?'已同步':'待同步',`${esc(b.symbol||'—')} · 盘中${b.intraday_count??0}根`],['报价',q.available?(q.fresh?'实时':q.timely?'新鲜，权限待核验':'行情延迟'):'未取得',`${esc(source(q.source))} · 行情${age(q.market_age_seconds)} · 接收${age(q.receive_age_seconds)}`],['买卖盘',depth.available?(depth.fresh?'实时':depth.timely?'新鲜，等待权限':'已过期'):'未取得',depth.time?`${time(depth.time,workspace().meta.timezone)} · ${age(depth.age_seconds)}`:'只影响买入确认']];
  const blocked=(result.blocked_conditions||[]).map(row=>`<li>${esc(row)}</li>`).join('');
  return `<section class="panel analysis-data"><div class="panel-head"><div><span class="kicker">数据完整度</span><h2>${esc(dataStatusName(result.data_status))}</h2></div><span class="state-chip ${result.data_status==='full'?'buy':''}">${result.mode==='monitoring'?'持续更新':'本次快照'}</span></div><div class="data-profile-grid">${cards.map(row=>`<div><span>${row[0]}</span><b>${row[1]}</b><small>${row[2]}</small></div>`).join('')}</div><div class="technical-strip"><div><span>最近日线收盘</span><b>${money(t.last_close)}</b></div><div><span>MA20 / MA60</span><b>${money(t.ma20)} / ${money(t.ma60)}</b></div><div><span>10日观察位</span><b>${money(t.high10)}</b></div><div><span>20日相对强弱</span><b>${pct(t.relative_strength)}</b></div><div><span>ATR / 价格</span><b>${pct(t.atr_pct)}</b></div></div>${blocked?`<div class="blocked-box"><b>买入仍被以下条件拦截</b><ul>${blocked}</ul></div>`:'<div class="ready-box">数据条件已完整，继续等待策略形态或执行检查。</div>'}</section>`
}
function conditionName(value){return ({eligible:'策略盘前条件',trend:'趋势向上',ready:'盘中形态数据',market_ok:'市场环境'}[value]||value)}
function requirementList(row){const requirements=Object.entries(row.requirements||{}),conditions=Object.entries(row.conditions||{}).filter(([,value])=>typeof value==='boolean');return `<div class="condition-list">${[...requirements,...conditions].filter(([key],index,all)=>all.findIndex(([other])=>other===key)===index).map(([key,value])=>`<div><span>${esc(conditionName(key))}</span><b class="${value?'up':'muted'}">${value?'通过':'等待'}</b></div>`).join('')}</div>`}
function holdingAnalysis(row){if(!row)return '';return `<section class="panel analysis-holding"><div class="panel-head"><div><span class="kicker">真实持仓处理</span><h2>${esc(row.action)}</h2></div><span class="state-chip ${row.event?'urgent':''}">${num(row.quantity,0)}股</span></div><div class="analysis-levels"><span>现价<b>${money(row.price)}</b></span><span>买入均价<b>${money(row.cost)}</b></span><span>浮动盈亏<b class="${tone(row.unrealized_pnl)}">${money(row.unrealized_pnl)}</b></span><span>保护 / 止盈<b>${money(row.stop)} / ${money(row.target)}</b></span></div><p>${esc(row.reason)}</p></section>`}
function analysisLevels(row){const d=row.decision,c=row.conditions||{};if(d)return `<div class="analysis-levels"><span>买入区间<b>${money(d.entry_min)}–${money(d.entry_max)}</b></span><span>止损<b>${money(d.stop)}</b></span><span>第一止盈<b>${money(d.target)}</b></span><span>建议数量<b>${num(d.qty,0)}股</b></span></div>`;const dataComplete=Object.values(row.requirements||{}).every(Boolean),status=c.ready?'已具备盘中判断':dataComplete?'数据齐全，条件未过':'继续补数据';return `<div class="analysis-levels"><span>观察位<b>${money(c.level)}</b></span><span>保护参考<b>${money(c.stop)}</b></span><span>相对量能<b>${c.relative_volume==null?'—':num(c.relative_volume)+'倍'}</b></span><span>状态<b>${status}</b></span></div>`}

function renderStrategy(){const w=workspace(),cn=currentMarket==='CN';$('#strategy-content').innerHTML=`<div class="strategy-flow"><article class="flow-step"><span>01 · 盘前</span><h3>分周期筛选</h3><p>日内、1–3日短线和5–10日波段分别计算，不用同一持有期解释所有机会。</p></article><article class="flow-step"><span>02 · 数据</span><h3>同源且完整</h3><p>长桥K线和成交量作为主源；各策略分别检查所需历史；某个策略预热不足不会阻塞其他策略。</p></article><article class="flow-step"><span>03 · 确认</span><h3>完整K线触发</h3><p>趋势、RSI回踩、VCP或开盘区间均需完整5分钟K线与${esc(w.meta.benchmark)}过滤。</p></article><article class="flow-step"><span>04 · 执行检查</span><h3>价格与盘口</h3><p>报价30秒内、买卖盘15秒内，不超过不追价上限，再计算数量。</p></article><article class="flow-step"><span>05 · 复盘</span><h3>双账本评价</h3><p>影子模拟衡量策略本身，组合模拟衡量资金与仓位竞争后的实际结果。</p></article></div><div class="two-columns"><section class="panel rule-card"><h2>${esc(w.meta.name)}策略组</h2><ul><li><b>短线：</b>开盘区间突破、首次回调、趋势回踩、波动收缩和趋势RSI回踩，最长3个交易日。</li><li><b>波段：</b>VCP要求260根日线、EMA多头、相对强势前30%及双重波动收缩，最长10个交易日。</li>${cn?'': '<li><b>日内：</b>20分钟开盘区间突破仅在美东10:00至11:30入场，15:50前退出。</li>'}<li>已有真实持仓、行情过期、盘口缺失、追价过高或风险名额不足时不会进入综合首选。</li></ul></section><section class="panel rule-card"><h2>${esc(w.meta.name)}退出与统计</h2><ul><li>短线与波段默认在2倍价格风险减半；短线用3根5分钟低点，VCP用完整日线3日低点跟踪。</li><li>${cn?'A股继续执行T+1、涨跌停和100股手数约束。':'美股日内策略使用固定区间止损止盈，其他策略按对应周期退出。'}</li><li>不足30笔标记样本不足，30笔后可初步比较，100笔后才标记样本相对稳定。</li><li>胜率同时展示95%区间、利润因子、每笔期望、回撤和成本加倍结果。</li></ul></section></div><section class="panel rule-card"><h2>页面结论</h2><ul><li><b>可考虑买入：</b>策略信号、行情、盘口、资金和持仓约束全部通过。</li><li><b>观察备选：</b>具备部分趋势或形态条件，仍缺盘中确认或数据基线。</li><li><b>今天暂不买：</b>没有股票满足全部条件，系统不会用低质量股票补满位置。</li><li>券商接口继续只读，没有真实下单、撤单或改单入口。</li></ul></section>`}

function renderSystem(){
  const descriptions={longbridge:'持续行情、候选筛选与历史K线。App内可见行情不等于接口权限，按市场逐项核验。',ibkr:'美股行情核验、历史补充与备用。通过本机TWS或Gateway只读连接。',lingxi:'A股榜单、行情查询和自然语言筛选补充；未验证持续推送前不作为主源。'};
  $('#provider-cards').innerHTML=Object.entries(state.providers).map(([id,row])=>`<article class="panel provider"><h2>${esc(source(id))}<span class="action-chip ${row.state==='connected'?'':'urgent'}">${row.state==='connected'?'已接通':'待连接'}</span></h2><p>${esc(descriptions[id])}</p><small>${esc(row.message||'等待检查')}</small><small>最近成功 ${esc(time(row.last_ok))}${row.latency_ms!=null?` · 请求 ${num(row.latency_ms,0)}ms`:''}</small>${row.market_access?`<div class="provider-access">${Object.entries(row.market_access).map(([market,access])=>`<small><b>${esc(market)}</b> · 报价${access.quote?'已验证':'待验证'} · 买卖盘${access.depth?'已验证':'待验证'} · 推送${access.subscription?'已验证':'待验证'}</small>`).join('')}</div>`:''}<button class="secondary" data-connect="${id}">${id==='longbridge'?'检查长桥权限':id==='ibkr'?'连接盈透本地接口':'检查灵犀'}</button>${row.auth_url?`<a class="text-button" target="_blank" rel="noreferrer" href="${esc(row.auth_url)}">打开长桥授权页</a>`:''}</article>`).join('')+`<article class="panel provider"><h2>同花顺<span class="action-chip">人工入口</span></h2><p>普通手机或电脑客户端用于人工看盘和核对。目前不作为程序行情源。</p><small>以后取得正式数据接口权限，再作为独立数据源接入。</small></article>`;
  renderNotifications();if(document.activeElement!==$('#monitor-limit'))$('#monitor-limit').value=state.settings.monitor_limit;if(document.activeElement!==$('#ib-port'))$('#ib-port').value=state.settings.ib_port;$('#monitor-toggle').textContent=state.monitor.running?'暂停盯盘':'恢复盯盘';$('#benchmarks').innerHTML=state.benchmarks.length?state.benchmarks.slice(0,14).map(row=>`<div class="event-row"><time>${esc(time(row.time))}</time><b>${esc(source(row.source))}</b><span>${row.operation?esc(row.operation):`请求 ${num(row.request_ms,0)}ms · 返回 ${row.returned}/${row.requested}个样本${row.during_session?'':' · 休市样本'}`}</span></div>`).join(''):'<div class="empty">暂无连接检查记录。</div>';$('#system-events').innerHTML=state.events.length?state.events.slice(0,24).map(row=>`<div class="event-row"><time>${esc(time(row.time))}</time><b>${esc(row.symbol||'系统')}</b><span>${esc(row.message)}</span></div>`).join(''):'<div class="empty">暂无异常记录。</div>';
}
function renderNotifications(){const n=state.notification_settings,permission=({authorized:'已授权',provisional:'临时授权',denied:'已拒绝',not_determined:'尚未选择',unknown:'检查中',unavailable:'不可用'}[n.permission]||n.permission);$('#notification-settings').innerHTML=`<div class="toggle-row"><span>看板提醒</span><input type="checkbox" id="notify-enabled" ${n.enabled?'checked':''}></div><div class="toggle-row"><span>Mac通知中心</span><input type="checkbox" id="notify-desktop" ${n.desktop?'checked':''}></div><div class="toggle-row"><span>触发提示音</span><input type="checkbox" id="notify-sound" ${n.sound?'checked':''}></div><div class="toggle-row"><span>系统权限</span><b>${esc(permission||'检查中')}</b></div><div class="toggle-row"><button class="secondary" id="save-notifications">保存提醒设置</button><button class="text-button" id="test-notification">测试Mac通知</button></div>`}

function analysisRequest(){
  const quantity=$('#analyze-quantity').value,cost=$('#analyze-cost').value;
  const holding=quantity&&cost?{quantity:Number(quantity),cost:Number(cost),entry_date:$('#analyze-date').value||null,available:$('#analyze-available').value?Number($('#analyze-available').value):null,stop:$('#analyze-stop').value?Number($('#analyze-stop').value):null,target:$('#analyze-target').value?Number($('#analyze-target').value):null}:null;
  return {symbol:$('#analyze-symbol').value.trim().toUpperCase(),holding}
}
function saveAnalysisSession(request){writeSession('stock-analysis-session',{request,result:analysisResult})}
function restoreAnalysisSession(){
  const saved=readSession('stock-analysis-session');if(!saved?.request?.symbol||!saved.result)return;
  analysisResult=saved.result;const request=saved.request,h=request.holding||{};
  $('#analyze-symbol').value=request.symbol;$('#analyze-quantity').value=h.quantity??'';$('#analyze-cost').value=h.cost??'';$('#analyze-date').value=h.entry_date||'';$('#analyze-available').value=h.available??'';$('#analyze-stop').value=h.stop??'';$('#analyze-target').value=h.target??''
}
async function runAnalysis(silent=false){
  if(analysisRefreshing)return;const request=analysisRequest();if(!request.symbol)return;
  analysisRefreshing=true;const button=$('#analyze-form .primary');if(button)button.disabled=true;
  if(!silent)$('#analyze-result').innerHTML='<div class="empty">正在优先读取本地行情，并补充日线、基准、五分钟K线和买卖盘…</div>';
  try{
    analysisResult=await api('/api/analyze',request);lastAnalysisRefresh=Date.now();saveAnalysisSession(request);
    if(analysisResult.market!==currentMarket)setMarket(analysisResult.market);
    const h=analysisResult.holding;if(h&&!request.holding){$('#analyze-quantity').value=h.quantity??'';$('#analyze-cost').value=h.cost??'';$('#analyze-date').value=h.entry_date||'';$('#analyze-available').value=h.available??'';$('#analyze-stop').value=h.stop??'';$('#analyze-target').value=h.target??''}
    renderAnalyze()
  }catch(error){if(!silent){analysisResult=null;$('#analyze-result').innerHTML=`<div class="empty">${esc(error.message)}</div>`;toast(error.message)}}
  finally{analysisRefreshing=false;if(button)button.disabled=false}
}
function refreshMonitoredAnalysis(){
  if(currentView!=='analyze'||!analysisResult?.monitored||!state?.markets?.[analysisResult.market]?.open||analysisRefreshing)return;
  if(Date.now()-lastAnalysisRefresh>=15000)runAnalysis(true)
}

function renderGpt(){
  if(gptMode==='auto')$('#gpt-mode-hint').textContent=`当前${workspace()?.meta?.phase||'阶段待确认'} · 生成时自动选择模板`;
  const saved=state?.gpt?.[currentMarket]?.conversation_url||gptBundle?.conversation_url||'';
  $('#gpt-conversation-url').value=saved;
  const savedLink=$('#open-saved-conversation');savedLink.classList.toggle('hidden',!saved);if(saved)savedLink.href=saved;
  if(gptBundle){
    const preview=gptBundle.preview||{};
    $('#gpt-preview-title').textContent=`${preview.market_name||currentMarket} · ${modeName(gptBundle.mode_resolved)}任务`;
    $('#gpt-preview-meta').textContent=`后备池 ${preview.fallback_count||0}只 · ${preview.account_included?'已含账户':'未含账户'} · ${time(gptBundle.generated_at)}`;
    $('#gpt-prompt').value=gptBundle.prompt||'';
    $('#gpt-warnings').innerHTML=(gptBundle.warnings||[]).map(row=>`<div class="gpt-warning">${esc(row)}</div>`).join('');
    $('#copy-open-chatgpt').disabled=!gptBundle.prompt;$('#copy-gpt').disabled=!gptBundle.prompt;$('#import-gpt-result').disabled=!$('#gpt-response').value.trim();
  }
  const latest=gptRun||(state?.gpt?.[currentMarket]?.runs||[]).find(row=>row.active)||(state?.gpt?.[currentMarket]?.runs||[])[0];
  if(latest&&!gptRun)gptRun=latest;
  renderGptRun(latest);
  setGptStep(latest?3:$('#gpt-response').value.trim()?2:gptBundle?1:0);
}
function modeName(value){return ({auto:'自动识别',premarket:'盘前/下一交易日',intraday:'盘中',single_stock:'单股'}[value]||value||'自动识别')}
function setGptStep(index){$$('.gpt-steps span').forEach((node,i)=>node.classList.toggle('active',i===index))}
function clearGptPreview(){gptBundle=null;gptRun=null;const prompt=$('#gpt-prompt');if(!prompt)return;prompt.value='';$('#gpt-account').checked=false;$('#gpt-preview-title').textContent='尚未生成任务';$('#gpt-preview-meta').textContent='';$('#gpt-warnings').innerHTML='';$('#copy-open-chatgpt').disabled=true;$('#copy-gpt').disabled=true;$('#import-gpt-result').disabled=true;$('#gpt-response').value='';$('#gpt-import-message').textContent='无结构化区块或字段错误时，不会激活任何候选，原文会保留供你修改。';setGptStep(0)}
function setGptMode(mode){
  gptMode=['auto','premarket','intraday','single_stock'].includes(mode)?mode:'auto';
  $$('[data-gpt-mode]').forEach(node=>node.classList.toggle('selected',node.dataset.gptMode===gptMode));
  $('#gpt-symbol-label').classList.toggle('hidden',gptMode!=='single_stock');
  $('#gpt-mode-hint').textContent=gptMode==='auto'?`当前${workspace()?.meta?.phase||'阶段待确认'} · 生成时自动选择模板`:modeName(gptMode);
}
function openGptResearch(kind='market',symbol=null){
  const chosen=symbol||(kind==='analysis'?analysisResult?.symbol:null)||(kind==='selected'?selectedSymbol:null);
  $('#gpt-symbol').value=chosen||'';
  setGptMode(chosen?'single_stock':'auto');
  $('#gpt-account').checked=false;
  clearGptPreview();if(chosen)$('#gpt-symbol').value=chosen;showView('gpt');
}
async function generateGptContext(){
  const button=$('#generate-gpt-context');button.disabled=true;button.textContent='正在整理全市场研究任务…';
  try{
    gptBundle=await api('/api/gpt/package',{market:currentMarket,mode:gptMode,symbol:gptMode==='single_stock'?$('#gpt-symbol').value.trim().toUpperCase()||null:null,include_account:$('#gpt-account').checked});
    gptRun=null;renderGpt();toast('GPT扫描任务已生成，请预览后复制');
  }catch(error){toast(error.message)}finally{button.disabled=false;button.textContent='生成GPT全市场扫描任务'}
}
async function copyGptPrompt(openChat=false){
  if(!gptBundle?.prompt)return;
  const opened=openChat?window.open('about:blank','_blank'):null;
  let copied=false;
  try{await navigator.clipboard.writeText(gptBundle.prompt);copied=true}catch{
    const field=$('#gpt-prompt');field.focus();field.select();try{copied=document.execCommand('copy')}catch{}
  }
  if(openChat&&opened)opened.location.href=gptBundle.chatgpt_url;
  if(openChat&&!opened)toast(copied?'已复制；浏览器拦截了 ChatGPT 新窗口，请手工打开':'新窗口和剪贴板均被拦截，请手工选中复制');
  else toast(copied?'已复制到剪贴板':'剪贴板不可用，请手工选中复制');
}
function renderGptRun(run){
  const cards=$('#gpt-result-cards'),form=$('#gpt-followup-form');
  if(!run){cards.innerHTML='<div class="empty">导入后会分别显示：GPT精选·等确认、当前可考虑、暂不参与。</div>';$('#gpt-run-meta').textContent='尚未导入';form.classList.add('hidden');return}
  $('#gpt-run-meta').textContent=`${run.status==='validated'?'已完成本地复核':run.status==='expired'?'已失效':'需要修正'} · ${time(run.imported_at)}`;
  if(run.status==='needs_correction'){
    cards.innerHTML=`<div class="gpt-correction"><b>结构化结果需要修正</b><p>${esc((run.warnings||[]).join('；'))}</p><small>粘贴原文仍保留在上方，请在ChatGPT答案末尾补齐标记和JSON后再次导入。</small></div>`;form.classList.add('hidden');return
  }
  const local=new Map((run.local_validation||[]).map(row=>[row.symbol,row]));
  if(!(run.candidates||[]).length){cards.innerHTML='<div class="empty-primary"><div><div class="empty-icon">○</div><h2>本轮不推荐买入</h2><p>GPT返回零候选，本机不会为了填满名额激活股票。</p></div></div>'}
  else cards.innerHTML=run.candidates.map(candidate=>{const check=local.get(candidate.symbol)||{},status=check.display_status||'GPT精选·等确认';return `<article class="gpt-result-card ${check.state||'wait'}"><div class="card-top"><div><span class="kicker">${esc(candidate.industry||'行业未知')} · GPT ${esc(candidate.verdict)}</span><h3>${esc(candidate.name||candidate.symbol)} <small>${esc(candidate.symbol)}</small></h3></div><span class="state-chip ${check.state==='buy'?'buy':''}">${esc(status)}</span></div><p>${esc(candidate.thesis||'未提供核心逻辑')}</p><div class="gpt-levels"><span>GPT价<b>${money(candidate.current_price)}</b></span><span>本地价<b>${money(check.local_price)}</b></span><span>不追价<b>${money(candidate.no_chase_price)}</b></span><span>失效线<b>${money(candidate.invalidation_price)}</b></span><span>仓位上限<b>${check.position_cap_pct==null?'待本地确认':`${num(check.position_cap_pct)}%`}</b></span></div><div class="detail-note"><b>本地结论：</b>${esc(check.reason||'等待本地核验')}<br><b>确认条件：</b>${esc(candidate.confirmation_condition||'未提供')}</div></article>`}).join('');
  form.classList.toggle('hidden',run.status!=='validated'||!run.active);
}
async function importGptResult(){
  if(!gptBundle?.package_id)return toast('请先生成当前研究任务');
  const button=$('#import-gpt-result');button.disabled=true;button.textContent='正在刷新本地行情并复核…';
  try{gptRun=await api('/api/gpt/import',{package_id:gptBundle.package_id,response_text:$('#gpt-response').value});renderGptRun(gptRun);$('#gpt-import-message').textContent=gptRun.status==='validated'?'已解析结构化结论，并完成逐只本地复核。':'未激活候选：请按提示修正结构化JSON后再次导入。';toast(gptRun.status==='validated'?'GPT结果已完成本地复核':'结构化结果需要修正');await refresh()}
  catch(error){$('#gpt-import-message').textContent=error.message;toast(error.message)}finally{button.disabled=!$('#gpt-response').value.trim();button.textContent='导入并进行本地复核'}
}
async function saveGptConversation(){try{const result=await api('/api/gpt/conversation',{market:currentMarket,url:$('#gpt-conversation-url').value.trim()});toast(result.conversation_url?'研究对话链接已保存在本机':'研究对话链接已清除');await refresh()}catch(error){toast(error.message)}}
async function generateGptFollowup(){
  const question=$('#gpt-followup-question').value.trim();if(!gptRun?.run_id||!question)return;
  const button=$('#gpt-followup-form button');button.disabled=true;
  try{gptBundle=await api('/api/gpt/followup-package',{run_id:gptRun.run_id,question});$('#gpt-prompt').value=gptBundle.prompt;renderGpt();toast('增量追问任务已生成，请复制到原对话')}
  catch(error){toast(error.message)}finally{button.disabled=false}
}

function bindEvents(){
  document.addEventListener('click',async event=>{
    const historyButton=event.target.closest('[data-history]');if(historyButton){
      $('#research-history-content').textContent='正在读取跟踪记录';$('#research-history-dialog').showModal();
      try{const res=await fetch('/api/research/candidates/'+encodeURIComponent(historyButton.dataset.history));if(!res.ok)throw new Error('读取失败');const data=await res.json();
      $('#research-history-content').innerHTML=[...(data.changes||[]),...(data.checks||[])].sort((a,b)=>String(b.time||b.checked_at).localeCompare(String(a.time||a.checked_at))).slice(0,80).map(r=>`<div class="event-row"><time>${esc(r.date||'')}</time><b>${esc(r.state||r.decision)}</b><span>${esc(r.reason)}<small>${esc(r.time||r.checked_at||'')}</small></span></div>`).join('')||'<p>旧版未保存逐股检查历史；从本次改版开始积累。</p>';}catch(e){$('#research-history-content').textContent='读取失败，请稍后重试'}return;
    }
    const workspaceButton=event.target.closest('[data-workspace]');if(workspaceButton){setMarket(workspaceButton.dataset.workspace);return}
    const viewButton=event.target.closest('[data-view]');if(viewButton){showView(viewButton.dataset.view);return}
    const goButton=event.target.closest('[data-go]');if(goButton){showView(goButton.dataset.go);return}
    const askGpt=event.target.closest('[data-ask-gpt]');if(askGpt){openGptResearch(askGpt.dataset.askGpt,askGpt.dataset.symbol||null);return}
    const gptModeButton=event.target.closest('[data-gpt-mode]');if(gptModeButton){setGptMode(gptModeButton.dataset.gptMode);return}
    const riskButton=event.target.closest('[data-risk]');if(riskButton){riskFilter=riskButton.dataset.risk;$$('[data-risk]').forEach(node=>node.classList.toggle('selected',node===riskButton));renderPremarket();return}
    const horizonButton=event.target.closest('[data-horizon]');if(horizonButton){strategyHorizon=horizonButton.dataset.horizon;selectedStrategy='';renderStrategyCenter();return}
    const symbolRow=event.target.closest('tr[data-symbol]');if(symbolRow){selectedSymbol=symbolRow.dataset.symbol;chartBars=[];chartLoadedAt=0;renderPremarket();return}
    const watch=event.target.closest('[data-watch]');if(watch){watch.disabled=true;await action(watch.dataset.remove==='1'?'/api/watch/remove':'/api/watch',{symbol:watch.dataset.watch},watch.dataset.remove==='1'?'已移出监测':'已加入监测并开始补齐数据');return}
    const kind=event.target.closest('[data-holding-kind]');if(kind){holdingKind=kind.dataset.holdingKind;renderHoldings();return}
    const edit=event.target.closest('[data-edit-holding]');if(edit){const row=workspace().holdings.real.find(item=>item.id===edit.dataset.editHolding);if(row)openHolding(row);return}
    const confirmPlan=event.target.closest('[data-confirm-plan]');if(confirmPlan){const row=marketPlans()?.find(item=>item.id===confirmPlan.dataset.confirmPlan);if(row)openExecution('buy',row);return}
    const sellHolding=event.target.closest('[data-sell-holding]');if(sellHolding){const row=workspace().holdings.real.find(item=>item.id===sellHolding.dataset.sellHolding);if(row)openExecution('sell',row);return}
    const close=event.target.closest('[data-close]');if(close){$(`#${close.dataset.close}`).close();return}
    const connect=event.target.closest('[data-connect]');if(connect){connect.disabled=true;await action(`/api/connect/${connect.dataset.connect}`,{},'已开始检查连接');return}
    const read=event.target.closest('[data-alert-read]');if(read){await action('/api/alerts/read',{id:read.dataset.alertRead},'已标记为已读');return}
    const strategyButton=event.target.closest('[data-select-strategy]');if(strategyButton){selectedStrategy=strategyButton.dataset.selectStrategy;renderStrategyCenter();return}
    const strategyToggle=event.target.closest('[data-strategy-toggle]');if(strategyToggle){strategyToggle.disabled=true;await action(`/api/strategies/${strategyToggle.dataset.strategyToggle}/config`,{market:currentMarket,parameters:{},enabled:strategyToggle.dataset.enabled==='1',reason:strategyToggle.dataset.enabled==='1'?'重新启用策略':'手工停用策略'},'策略状态已生成新版本');return}
    const versions=event.target.closest('[data-show-versions]');if(versions){await openVersions(versions.dataset.showVersions);return}
    const rollback=event.target.closest('[data-rollback]');if(rollback){rollback.disabled=true;if(await action(`/api/strategies/${rollback.dataset.strategy}/rollback`,{market:currentMarket,version:rollback.dataset.rollback},'已恢复参数并生成新版本'))$('#versions-dialog').close();return}
    const analysisMonitor=event.target.closest('[data-analysis-monitor]');if(analysisMonitor&&!analysisResult?.monitored){analysisMonitor.disabled=true;try{const result=await api('/api/analyze/add-monitoring',{symbol:analysisMonitor.dataset.analysisMonitor});toast(result.warm?'已加入持续监测，数据预热完成':'已加入持续监测，正在预热数据');await refresh();await runAnalysis(true)}catch(error){toast(error.message)}finally{analysisMonitor.disabled=false}return}
    const saveAnalysis=event.target.closest('[data-analysis-save-holding]');if(saveAnalysis&&analysisResult){openHolding({symbol:analysisResult.symbol,name:analysisResult.name,source:'manual',quantity:Number($('#analyze-quantity').value)||'',cost:Number($('#analyze-cost').value)||'',entry_date:$('#analyze-date').value,stop:Number($('#analyze-stop').value)||null,target:Number($('#analyze-target').value)||null,note:'由单股分析录入'});return}
    if(event.target.id==='add-holding'){openHolding();return}
    if(event.target.id==='stop-broker-sync'){await action('/api/settings',{...state.settings,broker_sync_enabled:false},'已关闭券商账户同步，手工持仓继续跟踪');return}
    if(event.target.id==='sync-holdings'){event.target.disabled=true;await action('/api/real-holdings/sync',{source:'all'},'持仓同步已完成或已保留上次有效结果');event.target.disabled=false;return}
    if(event.target.id==='refresh-premarket'){event.target.disabled=true;await action('/api/scan',{},'已开始重新筛选；候选会分批完成日线复核');return}
    if(event.target.id==='toggle-simulation'){await action('/api/settings',{...state.settings,simulation_enabled:!state.settings.simulation_enabled},state.settings.simulation_enabled?'已暂停模拟新开仓':'已恢复模拟新开仓');return}
    if(event.target.id==='benchmark'){await action('/api/benchmark',{},'已开始检查数据连接');return}
    if(event.target.id==='monitor-toggle'){await action('/api/monitor',{running:!state.monitor.running},state.monitor.running?'盯盘已暂停':'盯盘已恢复，正在重新核验行情');return}
    if(event.target.id==='save-notifications'){await action('/api/notifications',{enabled:$('#notify-enabled').checked,desktop:$('#notify-desktop').checked,sound:$('#notify-sound').checked},'提醒设置已保存');return}
    if(event.target.id==='test-notification'){await action('/api/notifications/test',{},'已提交测试通知，请查看Mac通知中心');return}
    if(event.target.id==='remove-holding'){const id=$('#holding-id').value;if(id&&await action('/api/real-holdings/remove',{id},'手工持仓记录已删除'))$('#holding-dialog').close();return}
    if(event.target.id==='run-replay'){const symbol=$('#replay-symbol').value;if(!symbol)return toast('当前市场没有可回放的监测股票');event.target.disabled=true;try{const result=await api('/api/replay',{symbol,source:'longbridge'});$('#replay-result').innerHTML=`<b>${esc(result.symbol)}</b> · ${result.evaluation_bars}根评估K线<br>${esc(result.label)}`;toast(result.profit_computed===false?'技术信号复核完成；收益数据不足':'回放完成')}catch(error){toast(error.message)}finally{event.target.disabled=false}return}
    if(event.target.id==='strategy-versions'){await openVersions(selectedStrategy);return}
    if(event.target.id==='copy-open-chatgpt'){await copyGptPrompt(true);return}
    if(event.target.id==='copy-gpt'){await copyGptPrompt(false);return}
    if(event.target.id==='import-gpt-result'){await importGptResult();return}
    if(event.target.id==='clear-gpt-response'){$('#gpt-response').value='';$('#import-gpt-result').disabled=true;return}
    if(event.target.id==='save-gpt-conversation'){await saveGptConversation();return}
  });
  $('#execution-form').addEventListener('submit',saveExecution);
  $('#premarket-search').addEventListener('input',()=>state&&renderPremarket());
  $('#holding-form').addEventListener('submit',async event=>{event.preventDefault();const id=$('#holding-id').value,sourceValue=$('#holding-source').value,payload={id:id||null,symbol:$('#holding-symbol').value.trim().toUpperCase(),name:$('#holding-name').value.trim()||null,quantity:Number($('#holding-quantity').value),cost:Number($('#holding-cost').value),entry_date:$('#holding-date').value||null,stop:$('#holding-stop').value?Number($('#holding-stop').value):null,target:$('#holding-target').value?Number($('#holding-target').value):null,note:$('#holding-note').value.trim()};const path=id&&sourceValue!=='manual'?'/api/real-holdings/plan':'/api/real-holdings/manual';const body=path.endsWith('/plan')?{id,payload,entry_date:payload.entry_date,stop:payload.stop,target:payload.target,note:payload.note}:{...payload};delete body.payload;if(await action(path,body,'真实持仓计划已保存'))$('#holding-dialog').close()});
  $('#screen-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('#screen-result').textContent='正在查询灵犀并筛出股票代码…';try{const result=await api('/api/screen',{query:$('#screen-query').value.trim()});$('#screen-result').innerHTML=`<p>${esc(result.text||'查询已完成')}</p>${(result.candidates||[]).map(row=>`<button class="secondary" data-watch="${esc(row.symbol)}">＋ ${esc(row.symbol)}</button>`).join('')}`;toast('灵犀查询完成')}catch(error){$('#screen-result').textContent=error.message;toast(error.message)}finally{button.disabled=false}});
  $('#settings-form').addEventListener('submit',async event=>{event.preventDefault();await action('/api/settings',{account_mode:!!state.settings.account_mode,broker_sync_enabled:!!state.settings.broker_sync_enabled,monitor_limit:Number($('#monitor-limit').value),simulation_enabled:state.settings.simulation_enabled,ib_port:Number($('#ib-port').value)},'运行设置已保存')});
  $('#strategy-workbench').addEventListener('submit',async event=>{if(event.target.id!=='strategy-config-form')return;event.preventDefault();const form=event.target,parameters={};form.querySelectorAll('[data-param]').forEach(input=>parameters[input.dataset.param]=input.value);const button=event.submitter;button.disabled=true;try{await api(`/api/strategies/${form.dataset.strategyId}/config`,{market:currentMarket,parameters,reason:$('#strategy-change-reason').value});toast('新参数版本已立即生效');await refresh()}catch(error){toast(error.message)}finally{button.disabled=false}});
  $('#analyze-form').addEventListener('submit',async event=>{event.preventDefault();await runAnalysis(false)});
  $('#gpt-form').addEventListener('submit',async event=>{event.preventDefault();await generateGptContext()});
  $('#gpt-response').addEventListener('input',event=>{const ready=Boolean(gptBundle?.package_id&&event.target.value.trim());$('#import-gpt-result').disabled=!ready;setGptStep(ready?2:gptBundle?1:0)});
  $('#gpt-followup-form').addEventListener('submit',async event=>{event.preventDefault();await generateGptFollowup()});
  let lookupTimer;$('#analyze-symbol').addEventListener('input',event=>{clearTimeout(lookupTimer);lookupTimer=setTimeout(async()=>{const q=event.target.value.trim();if(q.length<2)return;try{const rows=await api('/api/lookup?q='+encodeURIComponent(q));$('#analyze-suggestions').innerHTML=rows.map(row=>`<option value="${esc(row.symbol)}">${esc(row.name)}</option>`).join('')}catch{}},220)});
}

async function openVersions(strategy){try{const rows=await api(`/api/strategies/${strategy}/versions?market=${currentMarket}`);$('#versions-title').textContent=`${strategyName(strategy)} · 参数历史`;$('#versions-content').innerHTML=rows.map((row,index)=>`<div class="version-row"><div><b>${esc(row.version)}</b><small>${esc(time(row.created_at))} · ${esc(row.reason)}</small>${row.replay?`<small>${esc(row.replay.label||'历史探索结果')} · ${esc(row.replay.message||row.replay.state)}</small>`:'<small>历史探索结果尚未运行</small>'}</div><span class="state-chip ${index===0?'buy':''}">${index===0?'当前版本':'历史版本'}</span>${index? `<button class="secondary" data-rollback="${esc(row.version)}" data-strategy="${esc(strategy)}">恢复此版</button>`:''}<details><summary>查看参数与差异</summary><pre>${esc(JSON.stringify(row.parameters,null,2))}</pre></details></div>`).join('');$('#versions-dialog').showModal()}catch(error){toast(error.message)}}

document.documentElement.dataset.market=currentMarket;showView(currentView);bindEvents();setGptMode('auto');restoreAnalysisSession();refresh();
document.addEventListener('visibilitychange',resumeStateUpdates);window.addEventListener('online',resumeStateUpdates);armRefreshFallback();
try{const events=new EventSource('/api/events');events.onmessage=()=>scheduleRefresh();events.onerror=()=>stateDisconnected('状态推送中断，正在自动重新连接')}catch{}


function renderAutoResearch(w){
  const panel=$('#auto-research-panel');if(!panel)return;panel.classList.toggle('hidden',currentMarket!=='CN');if(currentMarket!=='CN')return;
  const r=w.research||state.auto_research||{},dis=r.discovery||state.auto_research?.discovery||{},q=dis.quote_coverage||{},signal=dis.signal_coverage||{},execution=dis.execution_coverage||{},changes=dis.round_changes||{},supported=q.total||r.supported||0,checked=r.checked||0,expanded=panel.querySelector('[data-research-details]')?.open;
  const labels={running:'全范围重新发现中',enumerating:'更新证券范围',complete:'本轮覆盖完成',partial:'部分数据待恢复',error:'数据请求失败',interrupted:'中断待恢复'};
  const outside=changes.outside_discovered??dis.outside_discovered,newEntries=changes.new_entries??dis.new_entries,replacements=changes.replacements??dis.replacements;
  panel.innerHTML=`<div class="panel-head"><div><span class="kicker">每天自动 · 盘中持续</span><h2>80只是深度研究上限，名单随行情进出</h2><p>每5分钟从支持范围更新报价和发现池外机会；历史预热独立运行。</p></div><span class="action-chip">${state.monitor.running?'自动运行':'已暂停'}</span></div><div class="discovery-summary"><span>${esc(labels[dis.state]||'等待本轮行情发现')}</span><span>有效研究 ${num(Math.max(0,(r.research_pool||0)-(r.pool_data_missing||0)),0)} / 80只</span><span>实时监测 ${num(execution.monitored??w.health.tracked,0)} / ${num(execution.limit??r.monitor_limit??12,0)}个</span><span>保护名额 ${num(r.protected_slots||0,0)}个</span></div>
  <div class="research-counts"><div>全范围报价覆盖<b>${num(q.checked??0,0)} / ${num(supported,0)}</b><small>当前仍有效 ${num(q.fresh_now??0,0)} · 缺报价 ${num(q.missing??0,0)} · 缺量额 ${num(dis.volume_missing??0,0)}</small></div><div>本轮策略检查<b>${num(dis.daily_checked??0,0)} / ${num(supported,0)}</b><small>本轮缺日线 ${num(dis.daily_missing??0,0)}只 · 历史预热 ${num(checked,0)} / ${num(r.supported||0,0)}</small></div><div>分钟策略验证<b>${num(signal.checked??0,0)} / ${num(signal.eligible??0,0)}</b><small>待验证 ${num(dis.validation_backlog??0,0)}只 · 最久等待 ${num(dis.oldest_wait_seconds??0,0)}秒</small></div></div>
  <div class="discovery-summary"><span>本轮池外发现 ${outside==null?'未记录':num(outside,0)}</span><span>新入选 ${newEntries==null?'未记录':num(newEntries,0)}</span><span>被替换 ${replacements==null?'未记录':num(replacements,0)}</span><span>最近池外发现 ${dis.last_outside_at?esc(time(dis.last_outside_at,w.meta.timezone)):'待记录'}</span></div>
  <p class="form-hint">${esc(dis.reason||r.reason||'等待自动任务启动')} · ${dis.schedule_delayed||r.schedule_delayed?'本轮延迟，重复任务已合并':'按交易时段自动更新'} · 更新 ${esc(time(dis.heartbeat||dis.ended_at||r.updated_at,w.meta.timezone))}</p>
  <details data-research-details ${expanded?'open':''}><summary>扫描详情、候选变化与自动报告</summary><p class="form-hint">${esc(r.scope||'沪深主板和创业板；未覆盖科创板、北交所')}。证券库已读取 ${num(r.enumerated??0,0)} / ${num(r.universe_total??0,0)}，供应商名单不代表全部通过策略检查。盘口就绪 ${num(execution.ready??0,0)}个。</p><p class="form-hint">历史预热独立运行：已检查 ${num(checked,0)} / ${num(r.supported||0,0)}只，缺历史 ${num(r.missing||0,0)}只；与本轮全范围行情筛选分别计数。</p><progress max="${Math.max(1,r.supported||0)}" value="${checked}"></progress><p class="form-hint">${esc(r.runtime_validation?.message||'正在积累实机运行记录')} · 完整交易日 ${r.runtime_validation?.verified_days?.length||0} / 2。</p>${(r.changes||[]).slice(0,8).map(c=>`<div class="event-row"><b>${esc(c.name||c.symbol)}</b><span>${esc(({research:'研究池',monitoring:'监测池'})[c.pool_layer||c.layer||c.pool]||c.pool_layer||c.layer||c.pool||'候选')} · ${esc(c.state)} · ${esc(c.reason)}</span><button class="text-button" data-history="${esc(c.symbol)}">历史</button></div>`).join('')||'<p class="form-hint">尚无候选变化。名单相同时仍会记录重新检查的条件。</p>'}${(r.reports||[]).slice(0,2).map(researchReport).join('')}</details>`;
}
function researchReport(r){const f=r.funnel||{};return `<details class="research-report"><summary>${esc(r.date)} · ${r.kind==='close'?'收盘日报':'盘前计划'} · ${r.scan_complete?'范围复核完成':'覆盖尚不完整'}</summary><p>信号 ${num(f.signals||0,0)}，曾通过执行检查 ${num(f.execution_passed||0,0)}，未记录去向 ${num(f.unrecorded||0,0)}，模拟成交 ${num(f.simulated_fills||0,0)} 笔，未成交/取消 ${num(f.failed_fills||0,0)} 次，模拟结束 ${num(r.closed_trades||0,0)} 笔，成本后盈亏 ${money(r.net_pnl||0)}。</p>${(f.reasons||[]).map(x=>`<div class="list-row"><span>${esc(x.reason)}</span><b>${x.signals} 条信号</b></div>`).join('')}<small>同一信号可能经过多个阻断原因，数量不可直接相加。</small><p>${esc(r.next_action)}</p>${(r.candidates||[]).map(c=>`<div class="event-row"><b>${esc(c.name)}</b><span>${esc(c.reason)}</span><button class="text-button" data-history="${esc(c.symbol)}">历史</button></div>`).join('')}</details>`}
function renderResearchReview(w){
  const panel=$('#research-review');if(!panel)return;panel.classList.toggle('hidden',w.meta.market!=='CN');if(!w.research)return;
  const market=w.meta.market;
  let cache=researchComparisons[market];
  if(!cache||(!cache.loading&&Date.now()-cache.at>30000)){
    cache=researchComparisons[market]={at:Date.now(),loading:true,data:cache?.data};
    api('/api/research/comparison?market='+market).then(data=>{cache.data=data;cache.error=null}).catch(error=>{cache.error=error.message}).finally(()=>{cache.loading=false;if(currentView==='review'&&currentMarket===market)renderResearchReview(workspace())});
  }
  const rows=cache.data?.strategies||[];
  panel.innerHTML=`<div class="panel-head"><div><span class="kicker">研究与交易分开统计</span><h2>每日研究与策略比较</h2><p>1—3日短线与5—10日波段按策略和版本分别比较。每20笔检查一次；不足30笔继续标记样本不足。</p></div></div>
    <p>数据覆盖：${num(w.research.checked||0,0)} / ${num(w.research.supported||0,0)} 只，缺日线 ${num(w.research.missing||0,0)} 只。</p>
    ${cache.error?`<p>${esc(cache.error)}</p>`:''}
    ${rows.map(r=>{const p=r.performance.portfolio||{};return `<details class="research-report"><summary>${esc(r.name)} · ${esc(horizonName(r.horizon))} · ${esc(r.performance.version)}</summary><p>组合信号 ${r.signal_count}，成交 ${r.filled_count}，成交率 ${r.signal_to_fill_rate==null?'—':pct(r.signal_to_fill_rate*100)}；已结束 ${p.count||0} 笔，胜率 ${p.win_rate==null?'—':pct(p.win_rate*100)}。</p><p>平均盈利 ${money(p.average_win)} / 平均亏损 ${money(p.average_loss)}；成本后净收益 ${money(p.net_pnl)}，平均每笔 ${money(p.expectancy)}，最大回撤 ${p.max_drawdown==null?'—':pct(-p.max_drawdown)}。</p><p>前向影子样本 ${r.performance.forward_shadow?.count||0} 笔；${esc(p.stability||'样本不足')}。历史版本 ${r.versions.length} 个，历史探索与前向模拟分开统计。</p></details>`}).join('')||'<p>正在读取策略比较记录。</p>'}
    ${(w.research.reports||[]).map(researchReport).join('')||'<p>等待首份自动报告。</p>'}
    ${(cache.data?.research_cards||[]).map(c=>`<details><summary>${esc(c.setup)} · ${esc(c.state)}</summary><p>环境：${esc(c.environment)}</p><p>入场：${esc(c.entry)}</p><p>退出：${esc(c.exit)}</p><p>待验证：${esc(c.missing.join('、'))}</p></details>`).join('')}
    <p class="form-hint">观察候选的后续上涨不计作收益。研究假设不会自动激活。</p>`;
}
