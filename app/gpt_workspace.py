"""Pure helpers for the copy/paste GPT stock-selection workbench.

Nothing in this module calls OpenAI, ChatGPT, a browser, or a broker.  It only
formats an allow-listed package and parses the explicitly marked JSON returned
by the user.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlparse

from .calendars import is_open, phase

SCHEMA_VERSION='SHORTLIST_RESULT_V1'
CHATGPT_URL='https://chatgpt.com/'
LONGBRIDGE_APP_URL='https://chatgpt.com/apps/longbridge/asdk_app_6a2baf2fad748191812393c3e00308ef'
BEGIN_MARKER='SHORTLIST_RESULT_V1_BEGIN'
END_MARKER='SHORTLIST_RESULT_V1_END'
MAX_RESPONSE_BYTES=100*1024
VERDICTS={'买','等','不买'}


def resolve_mode(requested,market,t):
    if requested=='single_stock':return 'single_stock'
    if requested in ('premarket','intraday'):return requested
    if requested!='auto':raise ValueError('未知的GPT研究模式')
    if is_open(t,market) or phase(t,market)=='午间休市':return 'intraday'
    return 'premarket'


def valid_chatgpt_url(value):
    value=str(value or '').strip()
    if not value:return None
    try:parsed=urlparse(value)
    except ValueError:raise ValueError('研究对话链接格式不正确')
    if parsed.scheme!='https' or parsed.hostname!='chatgpt.com' or parsed.username or parsed.password:
        raise ValueError('研究对话链接只允许使用 https://chatgpt.com 地址')
    return value


def response_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _fallback_lines(candidates):
    if not candidates:return ['- 当前没有可用的本地后备候选；这不等于全市场没有机会。']
    lines=[]
    for row in candidates[:20]:
        lines.append(
            f"- {row.get('rank')}. {row.get('name') or row.get('symbol')} ({row.get('symbol')})"
            f"｜行业 {row.get('industry') or '行业未知'}｜本地结论 {row.get('decision') or '等待'}"
            f"｜参考价 {row.get('price') if row.get('price') is not None else '未提供'}"
            f"｜时间 {row.get('price_time') or row.get('candidate_updated_at') or '未提供'}"
            f"｜不追价 {row.get('no_chase_line') if row.get('no_chase_line') is not None else '待核验'}"
            f"｜失效 {row.get('invalidation_condition') if row.get('invalidation_condition') is not None else '待核验'}"
        )
    return lines


def result_contract(package_id,market,trade_date,mode):
    return {
        'schema_version':SCHEMA_VERSION,
        'package_id':package_id,
        'market':market,
        'trade_date':trade_date,
        'market_phase':mode,
        'generated_at':'ISO-8601时间',
        'tools_used':[{'name':'longbridge工具名','as_of':'ISO-8601时间'}],
        'candidates':[{
            'symbol':'600000.SH' if market=='CN' else 'AAPL.US','name':'股票名称','industry':'行业或行业未知',
            'verdict':'买|等|不买','current_price':0.0,'price_time':'ISO-8601时间',
            'facts':['已由数据或一手来源证实的事实'],'inferences':['模型推断'],
            'thesis':'核心逻辑','bear_case':['反方风险'],'catalysts':['催化剂'],
            'confirmation_condition':'确认条件','confirmation_price':0.0,
            'no_chase_price':0.0,'invalidation_price':0.0,'position_cap_pct':0.0,
            'horizon':'0-3交易日','risks':['执行与事件风险'],
        }],
        'no_recommendation_reason':None,
        'data_gaps':[],
        'unavailable_tools':[],
    }


def build_package_prompt(*,package_id,market,trade_date,mode,generated_at,expires_at,
                         candidates,workspace,account=None,symbol=None,parent=None,question=None):
    market_name='A股' if market=='CN' else '美股'
    phase_name={'premarket':'盘前/下一交易日','intraday':'盘中','single_stock':'单股'}[mode]
    local_phase=workspace.get('meta',{}).get('phase','未知')
    decision=workspace.get('decision',{})
    lines=[
        '@longbridge',
        '',
        '# SHORTLIST GPT 全市场选股任务',
        '',
        '你正在接收一个由用户手工复制的研究任务。不要把本地后备池当成全市场扫描结果。',
        f'- 研究包：{package_id}',f'- 市场：{market_name} ({market})',f'- 交易日：{trade_date}',
        f'- 模式：{phase_name}；本地市场阶段：{local_phase}',f'- 生成：{generated_at}',f'- 失效：{expires_at}',
    ]
    if symbol:lines.append(f'- 指定股票：{symbol}')
    if question:lines += ['', '## 本次问题', question]
    if parent:
        lines += ['', '## 上一轮结构化结论与本地复核',json.dumps(parent,ensure_ascii=False,indent=2)]
    lines += [
        '', '## 必须先完成的 Longbridge 研究',
        '1. 先调用 Longbridge 的全市场筛选、异动榜/热度、行业轮动和新闻工具进行发现，再对入围股票深研；不要只在下方本地后备池中挑选。',
        '2. 逐只核对最新行情、公告/监管文件、新闻、财务和基本面；记录实际工具名及每项数据时间。',
    ]
    if mode=='intraday':
        lines.append('3. 盘中额外检查实时价格、成交额、分时/VWAP、量能、资金流、最新公告与相对市场强弱。')
    elif mode=='premarket':
        lines.append('3. 盘前重点检查隔夜信息、开盘前公告、行业轮动、催化剂有效性和可能的跳空风险。')
    else:
        lines.append('3. 对指定股票与同业进行比较，检查最新行情、技术结构、公告、财务、新闻和催化剂。')
    lines += [
        '4. 最终精选0–3只。没有合格股票时明确写“本轮不推荐买入”，不得凑数。',
        '5. Longbridge工具不可用、事实无法核对或行情过期时，只能给“等/不买”。',
        '6. 分开写已证实事实与模型推断；不得把推断冒充事实。',
        '', '## 本地规则现状（不是GPT结论）',
        f"- 今日结论：{decision.get('headline') or '尚无'}",
        f"- 原因：{decision.get('reason') or '尚无'}",
        '', '## 本地量化后备池（最多20只，仅在数据工具不可用时辅助，不得冒充全市场扫描）',
        *_fallback_lines(candidates),
    ]
    if account and account.get('included'):
        lines += ['', '## 用户本次明确勾选的脱敏账户摘要',json.dumps({
            'summaries':account.get('summaries',[]),'positions':account.get('positions',[]),
            'omitted_positions':account.get('omitted_positions',0)},ensure_ascii=False,indent=2)]
    else:
        lines += ['', '## 账户边界','- 本次未包含账户数据。不得推测持仓、资产、购买力或可买金额。']
    if market=='CN':
        lines += ['', '## A股固定检查','- T+1、涨跌停无法成交、停牌、跳空、ST/*ST和新股风险必须逐项检查。']
    lines += [
        '', '## 输出要求',
        '- 先给可读的比较表与结论；每只写核心逻辑、证实事实、推断、催化剂、反方风险、买/等/不买、确认条件、不追价线、失效线、仓位上限和0–3交易日计划。',
        '- 不要下单、改单、撤单，不要调用交易、最大可买量等工具，不要索要账户号、密钥、令牌或任何凭证。',
        '- 最后必须原样使用下面两个标记包住一个有效JSON；标记外可写分析，标记内只能写JSON，候选最多3只。',
        '', BEGIN_MARKER,'```json',json.dumps(result_contract(package_id,market,trade_date,mode),ensure_ascii=False,indent=2),'```',END_MARKER,
    ]
    return '\n'.join(lines)


def extract_result(text):
    if not isinstance(text,str):raise ValueError('请粘贴 ChatGPT 的完整文字答案')
    if len(text.encode('utf-8'))>MAX_RESPONSE_BYTES:raise ValueError('粘贴内容超过100KB，请删减后重试')
    start=text.find(BEGIN_MARKER);end=text.find(END_MARKER,start+len(BEGIN_MARKER))
    if start<0 or end<0:raise ValueError('没有找到完整的 SHORTLIST_RESULT_V1 结构化标记')
    block=text[start+len(BEGIN_MARKER):end].strip()
    block=re.sub(r'^```(?:json)?\s*','',block,flags=re.I)
    block=re.sub(r'\s*```$','',block).strip()
    try:data=json.loads(block)
    except json.JSONDecodeError as exc:raise ValueError(f'结构化JSON无法解析：第{exc.lineno}行附近')
    if not isinstance(data,dict):raise ValueError('结构化结果必须是JSON对象')
    return data


def _iso(value,label):
    if not isinstance(value,str) or not value.strip():raise ValueError(f'{label}必须提供ISO时间')
    try:return datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:raise ValueError(f'{label}不是有效ISO时间')


def validate_result(data,expected):
    if data.get('schema_version')!=SCHEMA_VERSION:raise ValueError('结构化版本不是 SHORTLIST_RESULT_V1')
    for key in ('package_id','market','trade_date'):
        if data.get(key)!=expected[key]:raise ValueError(f'{key}与当前研究包不一致')
    if data.get('market_phase')!=expected['mode']:raise ValueError('market_phase与当前研究包不一致')
    _iso(data.get('generated_at'),'generated_at')
    tools=data.get('tools_used',[])
    if not isinstance(tools,list):raise ValueError('tools_used必须是数组')
    for tool in tools:
        if not isinstance(tool,dict) or not isinstance(tool.get('name'),str):raise ValueError('tools_used字段格式不正确')
        _iso(tool.get('as_of'),'工具时间')
    candidates=data.get('candidates')
    if not isinstance(candidates,list):raise ValueError('candidates必须是数组')
    if len(candidates)>3:raise ValueError('GPT候选不能超过3只')
    seen=set();clean=[]
    allowed_suffix=('SH','SZ') if expected['market']=='CN' else ('US',)
    for index,row in enumerate(candidates,1):
        if not isinstance(row,dict):raise ValueError(f'第{index}只候选格式不正确')
        symbol=str(row.get('symbol','')).upper().strip()
        if not re.fullmatch(r'[A-Z0-9.\-]{1,16}\.(US|SH|SZ)',symbol) or symbol.split('.')[-1] not in allowed_suffix:
            raise ValueError(f'第{index}只候选代码与市场不匹配')
        if symbol in seen:raise ValueError('候选股票不能重复')
        seen.add(symbol)
        verdict=row.get('verdict')
        if verdict not in VERDICTS:raise ValueError(f'{symbol}的结论必须是买、等或不买')
        price=row.get('current_price')
        if not isinstance(price,(int,float)) or isinstance(price,bool) or price<=0:raise ValueError(f'{symbol}缺少有效当前价格')
        _iso(row.get('price_time'),f'{symbol}价格时间')
        numeric={}
        for key in ('confirmation_price','no_chase_price','invalidation_price','position_cap_pct'):
            if key not in row:raise ValueError(f'{symbol}缺少{key}')
            value=row.get(key)
            if value is not None and (not isinstance(value,(int,float)) or isinstance(value,bool) or value<0):
                raise ValueError(f'{symbol}的{key}格式不正确')
            numeric[key]=value
        if numeric['position_cap_pct'] is not None and numeric['position_cap_pct']>100:
            raise ValueError(f'{symbol}的仓位上限不能超过100%')
        def strings(key):
            value=row.get(key,[])
            if not isinstance(value,list) or any(not isinstance(item,str) for item in value):raise ValueError(f'{symbol}的{key}必须是文字数组')
            return [item[:1000] for item in value[:20]]
        clean.append({
            'symbol':symbol,'name':str(row.get('name') or symbol)[:100],
            'industry':str(row.get('industry') or '行业未知')[:100],'verdict':verdict,
            'current_price':float(price),'price_time':row['price_time'],
            'facts':strings('facts'),'inferences':strings('inferences'),
            'thesis':str(row.get('thesis') or '')[:3000],
            'bear_case':strings('bear_case'),'catalysts':strings('catalysts'),
            'confirmation_condition':str(row.get('confirmation_condition') or '')[:1000],
            **numeric,'horizon':str(row.get('horizon') or '0-3交易日')[:100],
            'risks':strings('risks'),
        })
    no_reason=data.get('no_recommendation_reason')
    if not clean and not isinstance(no_reason,str):raise ValueError('零候选时必须说明不推荐原因')
    for key in ('data_gaps','unavailable_tools'):
        if not isinstance(data.get(key,[]),list) or any(not isinstance(item,str) for item in data.get(key,[])):
            raise ValueError(f'{key}必须是文字数组')
    return {
        'schema_version':SCHEMA_VERSION,'package_id':expected['package_id'],'market':expected['market'],
        'trade_date':expected['trade_date'],'market_phase':expected['mode'],'generated_at':data['generated_at'],
        'tools_used':tools[:30],'candidates':clean,'no_recommendation_reason':str(no_reason)[:2000] if no_reason else None,
        'data_gaps':[x[:1000] for x in data.get('data_gaps',[])[:30]],
        'unavailable_tools':[x[:300] for x in data.get('unavailable_tools',[])[:30]],
    }
