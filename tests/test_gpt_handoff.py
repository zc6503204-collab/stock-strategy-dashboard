import asyncio
import json
from datetime import datetime,timedelta,timezone
from fastapi.testclient import TestClient

from app.service import Dashboard
from app.gpt_workspace import BEGIN_MARKER,END_MARKER,valid_chatgpt_url


def test_default_context_never_calls_or_includes_account(tmp_path):
    dashboard=Dashboard(tmp_path);calls=[]
    async def forbidden():
        calls.append('called');raise AssertionError('account method must not be called')
    dashboard.lb.account_summary=forbidden;dashboard.lb.account_positions=forbidden
    dashboard.ib.account_summary=forbidden;dashboard.ib.account_positions=forbidden
    result=asyncio.run(dashboard.gpt_context({
        'question':'复核今天的市场结论','market':'US','symbol':None,
        'include_account':False,'refresh':True}))
    assert not calls and not result['account_included']
    assert '本次未包含' in result['prompt']
    assert '持仓（按估算市值' not in result['prompt']
    stored=' '.join(str(row) for row in dashboard.store.events())
    assert '复核今天的市场结论' not in stored


def test_opt_in_merges_brokers_and_preserves_duplicate_source_boundary(tmp_path):
    dashboard=Dashboard(tmp_path)
    async def lb_summary():return {'source':'longbridge','balances':[{'currency':'USD','net_assets':1000,'cash':200}],'updated_at':'2026-09-14T01:00:00Z'}
    async def ib_summary():return {'source':'ibkr','balances':[{'currency':'BASE','net_assets':3000,'buying_power':5000}],'updated_at':'2026-09-14T01:01:00Z'}
    async def lb_positions():return [{'symbol':'AAPL.US','name':'Apple','quantity':2,'cost':100,'currency':'USD'}]
    async def ib_positions():return [{'symbol':'AAPL.US','name':'Apple','quantity':3,'cost':110,'currency':'USD'}]
    dashboard.lb.account_summary=lb_summary;dashboard.lb.account_positions=lb_positions
    dashboard.ib.account_summary=ib_summary;dashboard.ib.account_positions=ib_positions
    result=asyncio.run(dashboard.gpt_context({
        'question':'诊断账户','market':'US','symbol':None,'include_account':True,'refresh':True}))
    assert result['account_included']
    assert '[长桥] Apple (AAPL.US)' in result['prompt']
    assert '[盈透] Apple (AAPL.US)' in result['prompt']
    assert len(result['preview']['account']['positions'])==2
    assert 'account' not in result['prompt'].lower()


def test_position_limit_warns_and_omits_after_forty(tmp_path):
    dashboard=Dashboard(tmp_path)
    async def summary():return {'source':'longbridge','balances':[{'currency':'USD','cash':20}],'updated_at':'2026-09-14T01:00:00Z'}
    async def positions():return [{'symbol':f'{chr(65+i//26)}{chr(65+i%26)}.US','name':f'S{i}','quantity':1,'cost':i+1,'currency':'USD'} for i in range(45)]
    async def no_summary():raise RuntimeError()
    async def no_positions():raise RuntimeError()
    dashboard.lb.account_summary=summary;dashboard.lb.account_positions=positions
    dashboard.ib.account_summary=no_summary;dashboard.ib.account_positions=no_positions
    result=asyncio.run(dashboard.gpt_context({
        'question':'持仓复盘','market':'US','symbol':None,'include_account':True,'refresh':True}))
    assert len(result['preview']['account']['positions'])==40
    assert result['preview']['account']['omitted_positions']==5
    assert any('已省略5只' in warning for warning in result['warnings'])


def test_failed_refresh_marks_cached_account_and_positions_stale(tmp_path):
    dashboard=Dashboard(tmp_path)
    dashboard.account_summaries={'ibkr':{'source':'ibkr','balances':[{'currency':'BASE','net_assets':1000}],
                                         'updated_at':'2026-09-13T01:00:00Z'}}
    dashboard.real.replace_synced('ibkr',[{'symbol':'AAPL.US','name':'Apple','quantity':1,'cost':100,'currency':'USD'}])
    async def unavailable():raise RuntimeError('private vendor detail')
    dashboard.lb.account_summary=unavailable;dashboard.lb.account_positions=unavailable
    dashboard.ib.account_summary=unavailable;dashboard.ib.account_positions=unavailable
    result=asyncio.run(dashboard.gpt_context({
        'question':'复盘持仓','market':'US','symbol':None,'include_account':True,'refresh':True}))
    assert any(row['label']=='盈透账户摘要' and row['stale'] for row in result['included_sources'])
    assert any(row['label']=='盈透持仓快照' and row['stale'] for row in result['included_sources'])
    assert '过期快照' in result['prompt'] and 'private vendor detail' not in result['prompt']


def test_api_validates_market_and_keeps_local_header_boundary(tmp_path,monkeypatch):
    import app.main as main
    dashboard=Dashboard(tmp_path)
    async def noop():pass
    dashboard.start=noop;dashboard.stop=noop
    monkeypatch.setattr(main,'dashboard',dashboard)
    with TestClient(main.app,base_url='http://localhost') as client:
        body={'question':'看看','market':'US','include_account':False,'refresh':False}
        assert client.post('/api/gpt/context',json=body).status_code==403
        response=client.post('/api/gpt/context',json=body,headers={'X-Dashboard-Local':'1'})
        assert response.status_code==200 and response.json()['chatgpt_url']=='https://chatgpt.com/'
        bad=client.post('/api/gpt/context',json={**body,'market':'HK'},headers={'X-Dashboard-Local':'1'})
        assert bad.status_code==400


def test_market_scan_handoff_contains_only_top_ten_and_opens_longbridge(tmp_path):
    dashboard=Dashboard(tmp_path);trade_date=dashboard.candidate_trade_date('US')
    dashboard.daily_core={'US':{'trade_date':trade_date,'updated_at':'2026-09-15T01:00:00Z','stale':False,
                                'rows':[{'symbol':'S00.US'}]}}
    dashboard.selection=[{'symbol':f'S{i:02d}.US','name':f'Stock {i}','market':'US','industry':'软件' if i<3 else f'行业{i}',
                          'decision':'重点观察','score':100-i,'distance_to_high_pct':0,'candidate_strategies':['breakout'],
                          'source':'longbridge','as_of':'2026-09-14','close':100+i,'breakout_reference':101+i,
                          'structure_low':95+i,'risk_group':'normal','reason':'等待盘中确认','pool_role':'daily_core',
                          'candidate_updated_at':'2026-09-15T01:00:00Z'} for i in range(12)]
    result=asyncio.run(dashboard.gpt_context({'question':'扫描并复核','market':'US','symbol':None,
                                              'include_account':False,'refresh':False,'mode':'market_scan'}))
    assert len(result['preview']['candidates'])==10
    assert result['preview']['scan']['candidate_count']==12 and result['preview']['scan']['omitted_count']==2
    assert result['chatgpt_url']==result['longbridge_app_url']
    assert '最多保留3只' in result['prompt'] and '本轮不推荐买入' in result['prompt']
    assert '行业：软件' in result['prompt'] and '候选池刷新：2026-09-15T01:00:00Z' in result['prompt']
    stored=' '.join(str(row) for row in dashboard.store.events())
    assert '扫描并复核' not in stored and 'S00.US' not in stored


def test_market_scan_stale_or_empty_snapshot_is_explicit(tmp_path):
    dashboard=Dashboard(tmp_path)
    dashboard.daily_core={'CN':{'trade_date':'2026-09-01','updated_at':'2026-09-01T01:00:00Z','stale':True,'rows':[]}}
    result=asyncio.run(dashboard.gpt_context({'question':'扫描A股','market':'CN','symbol':None,
                                              'include_account':False,'refresh':False,'mode':'market_scan'}))
    assert result['preview']['scan']['stale'] and not result['preview']['candidates']
    assert any('不是当前交易日' in warning for warning in result['warnings'])
    assert any('不得为了凑数推荐股票' in warning for warning in result['warnings'])


def test_market_scan_rejects_symbol_and_unknown_mode(tmp_path):
    dashboard=Dashboard(tmp_path)
    for body in [
        {'question':'扫描','market':'US','symbol':'AAPL.US','mode':'market_scan','refresh':False},
        {'question':'扫描','market':'US','symbol':None,'mode':'other','refresh':False},
    ]:
        try:asyncio.run(dashboard.gpt_context(body))
        except ValueError:pass
        else:raise AssertionError('invalid mode combination must fail')


def test_market_scan_refreshes_requested_market_before_building_context(tmp_path):
    dashboard=Dashboard(tmp_path);calls=[]
    async def scan(force_strategy=False,rebuild_markets=None,wait_selection=False):
        calls.append((force_strategy,rebuild_markets,wait_selection))
        trade_date=dashboard.candidate_trade_date('US')
        dashboard.daily_core['US']={'trade_date':trade_date,'updated_at':'2026-09-15T02:00:00Z','stale':False,'rows':[]}
    dashboard.scan=scan
    result=asyncio.run(dashboard.gpt_context({'question':'刷新后扫描','market':'US','symbol':None,
                                              'include_account':False,'refresh':True,'mode':'market_scan'}))
    assert calls==[(False,['US'],True)]
    assert result['preview']['scan']['refreshed_at']=='2026-09-15T02:00:00Z'


def package_result(package,candidates=None,**changes):
    current=datetime.now(timezone.utc).isoformat()
    body={'schema_version':'SHORTLIST_RESULT_V1','package_id':package['package_id'],
          'market':package['preview']['market'],'trade_date':package['preview']['trade_date'],
          'market_phase':package['mode_resolved'],'generated_at':current,
          'tools_used':[{'name':'longbridge.quote','as_of':current}],
          'candidates':candidates or [],'no_recommendation_reason':'本轮没有合格股票' if not candidates else None,
          'data_gaps':[],'unavailable_tools':[]}
    body.update(changes)
    return f'可读分析\n{BEGIN_MARKER}\n```json\n{json.dumps(body,ensure_ascii=False)}\n```\n{END_MARKER}'


def gpt_candidate(symbol='AAPL.US',verdict='买'):
    return {'symbol':symbol,'name':'Apple','industry':'硬件','verdict':verdict,'current_price':100,
            'price_time':datetime.now(timezone.utc).isoformat(),'facts':['行情已核对'],'inferences':['可能延续'],
            'thesis':'相对强势','bear_case':['波动风险'],'catalysts':['新品'],'confirmation_condition':'站稳100',
            'confirmation_price':100,'no_chase_price':105,'invalidation_price':95,'position_cap_pct':8,
            'horizon':'0-3交易日','risks':['跳空']}


def test_new_package_is_click_driven_full_market_task_and_account_defaults_off(tmp_path):
    dashboard=Dashboard(tmp_path);calls=[]
    async def forbidden():calls.append(True);raise AssertionError('account must stay off')
    dashboard.lb.account_summary=forbidden;dashboard.lb.account_positions=forbidden
    result=asyncio.run(dashboard.gpt_package({'market':'US','mode':'premarket','symbol':None,'include_account':False}))
    assert not calls and result['mode_resolved']=='premarket'
    assert result['preview']['fallback_count']<=20
    assert result['prompt'].startswith('@longbridge')
    assert '先调用 Longbridge 的全市场筛选' in result['prompt']
    assert '不得冒充全市场扫描' in result['prompt']
    assert BEGIN_MARKER in result['prompt'] and END_MARKER in result['prompt']
    assert '本次未包含账户数据' in result['prompt']


def test_valid_import_new_symbol_is_locally_gated_and_deduplicated(tmp_path):
    dashboard=Dashboard(tmp_path)
    package=asyncio.run(dashboard.gpt_package({'market':'US','mode':'intraday','symbol':None,'include_account':False}))
    async def analyzed(_):
        return {'symbol':'AAPL.US','name':'Apple','market':'US','analyzed_at':'2026-09-15T08:01:00+00:00',
                'source':'longbridge','data_status':'full','quote':{'price':101,'market_time':'2026-09-15T08:01:00+00:00','trade_status':'Normal'},
                'available_assessments':{'execution':True},
                'final':{'status':'BUY','action':'可考虑买入','reason':'本地通过','decision':{'cash_required':5000}}}
    dashboard.analyze_stock=analyzed
    text=package_result(package,[gpt_candidate()])
    first=asyncio.run(dashboard.gpt_import({'package_id':package['package_id'],'response_text':text}))
    second=asyncio.run(dashboard.gpt_import({'package_id':package['package_id'],'response_text':text}))
    assert first['status']=='validated' and first['run_id']==second['run_id']
    assert first['local_validation'][0]['display_status']=='当前可考虑买入'
    assert first['local_validation'][0]['position_cap_pct']==5
    assert dashboard.active_gpt_symbols(['US'])==['AAPL.US']


def test_import_rejects_broken_wrong_market_duplicates_and_over_three(tmp_path):
    dashboard=Dashboard(tmp_path)
    package=asyncio.run(dashboard.gpt_package({'market':'US','mode':'premarket','symbol':None,'include_account':False}))
    missing_risk=gpt_candidate();missing_risk.pop('no_chase_price')
    bad_texts=[
        '没有结构化结果',
        package_result(package,[gpt_candidate('600000.SH')]),
        package_result(package,[gpt_candidate(),gpt_candidate()]),
        package_result(package,[gpt_candidate(f'S{i}.US') for i in range(4)]),
        package_result(package,[missing_risk]),
    ]
    for text in bad_texts:
        result=asyncio.run(dashboard.gpt_import({'package_id':package['package_id'],'response_text':text}))
        assert result['status']=='needs_correction' and not result['candidates']
    assert not dashboard.active_gpt_symbols(['US'])


def test_zero_recommendation_is_valid_and_does_not_activate_symbols(tmp_path):
    dashboard=Dashboard(tmp_path)
    package=asyncio.run(dashboard.gpt_package({'market':'US','mode':'premarket','symbol':None,'include_account':False}))
    result=asyncio.run(dashboard.gpt_import({'package_id':package['package_id'],'response_text':package_result(package)}))
    assert result['status']=='validated' and result['candidates']==[] and result['local_validation']==[]
    assert dashboard.active_gpt_symbols(['US'])==[]


def test_gpt_buy_cannot_bypass_no_chase_or_missing_execution(tmp_path):
    dashboard=Dashboard(tmp_path)
    async def over_chase(_):
        return {'symbol':'AAPL.US','name':'Apple','source':'longbridge','data_status':'full','analyzed_at':'2026-09-15T08:01:00+00:00',
                'quote':{'price':106,'market_time':'2026-09-15T08:01:00+00:00','trade_status':'Normal'},
                'available_assessments':{'execution':True},'final':{'status':'BUY','decision':{'cash_required':1000}}}
    dashboard.analyze_stock=over_chase
    blocked=asyncio.run(dashboard.validate_gpt_candidate(gpt_candidate()))
    assert blocked['display_status']=='暂不参与' and '不追价线' in blocked['reason']
    async def no_depth(_):
        return {'symbol':'AAPL.US','name':'Apple','source':'longbridge','data_status':'partial','analyzed_at':'2026-09-15T08:01:00+00:00',
                'quote':{'price':101,'market_time':'2026-09-15T08:01:00+00:00','trade_status':'Normal'},
                'available_assessments':{'execution':False},'final':{'status':'WAIT','reason':'等待盘口'}}
    dashboard.analyze_stock=no_depth
    waiting=asyncio.run(dashboard.validate_gpt_candidate(gpt_candidate()))
    assert waiting['display_status']=='GPT精选·等确认'


def test_followup_contains_previous_result_and_latest_price_change(tmp_path):
    dashboard=Dashboard(tmp_path)
    package=asyncio.run(dashboard.gpt_package({'market':'US','mode':'premarket','symbol':None,'include_account':False}))
    async def analyzed(_):
        return {'symbol':'AAPL.US','name':'Apple','source':'longbridge','data_status':'partial','analyzed_at':'2026-09-15T08:01:00+00:00',
                'quote':{'price':102,'market_time':'2026-09-15T08:01:00+00:00','trade_status':'Normal'},
                'available_assessments':{'execution':False},'final':{'status':'WAIT','reason':'等待确认'}}
    dashboard.analyze_stock=analyzed
    run=asyncio.run(dashboard.gpt_import({'package_id':package['package_id'],'response_text':package_result(package,[gpt_candidate()])}))
    follow=asyncio.run(dashboard.gpt_followup_package({'run_id':run['run_id'],'question':'价格变化后是否仍成立？'}))
    assert '价格变化后是否仍成立？' in follow['prompt'] and 'latest_local_validation' in follow['prompt']
    assert follow['preview']['parent_run_id']==run['run_id']


def test_chatgpt_conversation_url_is_strictly_local_and_host_limited(tmp_path):
    dashboard=Dashboard(tmp_path)
    saved=dashboard.save_gpt_conversation('CN','https://chatgpt.com/c/abc')
    assert saved['conversation_url']=='https://chatgpt.com/c/abc'
    for url in ('http://chatgpt.com/c/abc','https://evil.example/?next=chatgpt.com','https://chatgpt.com.evil.example/c/abc'):
        try:valid_chatgpt_url(url)
        except ValueError:pass
        else:raise AssertionError('non-chatgpt URL must fail')


def test_expired_cleanup_removes_raw_prompt_and_response_but_keeps_structured_review(tmp_path,monkeypatch):
    import app.service as service
    base=datetime(2026,9,15,8,tzinfo=timezone.utc)
    monkeypatch.setattr(service,'now',lambda:base)
    dashboard=Dashboard(tmp_path)
    package=asyncio.run(dashboard.gpt_package({'market':'US','mode':'premarket','symbol':None,'include_account':False}))
    async def analyzed(_):raise RuntimeError()
    dashboard.analyze_stock=analyzed
    run=asyncio.run(dashboard.gpt_import({'package_id':package['package_id'],'response_text':package_result(package,[gpt_candidate()])}))
    monkeypatch.setattr(service,'now',lambda:datetime.fromisoformat(package['expires_at'])+timedelta(minutes=1))
    dashboard.cleanup_gpt_data()
    stored_run=next(row for row in dashboard.gpt_runs if row['run_id']==run['run_id'])
    stored_package=next(row for row in dashboard.gpt_packages if row['package_id']==package['package_id'])
    assert stored_run['status']=='expired' and stored_run['response_text'] is None and stored_run['candidates']
    assert stored_package['prompt'] is None and not stored_package['include_account']


def test_new_gpt_endpoints_keep_local_header_and_do_not_echo_raw_answer(tmp_path,monkeypatch):
    import app.main as main
    dashboard=Dashboard(tmp_path)
    async def noop():pass
    dashboard.start=noop;dashboard.stop=noop
    monkeypatch.setattr(main,'dashboard',dashboard)
    headers={'X-Dashboard-Local':'1'}
    with TestClient(main.app,base_url='http://localhost') as client:
        assert client.post('/api/gpt/package',json={'market':'US','mode':'premarket'}).status_code==403
        package=client.post('/api/gpt/package',json={'market':'US','mode':'premarket'},headers=headers).json()
        pasted=package_result(package)
        imported=client.post('/api/gpt/import',json={'package_id':package['package_id'],'response_text':pasted},headers=headers)
        assert imported.status_code==200 and imported.json()['status']=='validated'
        assert 'response_text' not in imported.json()
        runs=client.get('/api/gpt/runs?market=US').json()
        assert runs['runs'][0]['run_id']==imported.json()['run_id'] and 'response_text' not in runs['runs'][0]
        bad_url=client.post('/api/gpt/conversation',json={'market':'US','url':'https://example.com/c/1'},headers=headers)
        assert bad_url.status_code==400
