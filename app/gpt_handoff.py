"""Pure, reusable formatter for ChatGPT research handoffs.

This module never calls a model or broker.  It accepts already allow-listed local
data so a future Responses API integration can reuse the same boundary.
"""
from __future__ import annotations

CHATGPT_URL='https://chatgpt.com/'
LONGBRIDGE_APP_URL='https://chatgpt.com/apps/longbridge/asdk_app_6a2baf2fad748191812393c3e00308ef'

def _value(value):
    if value is None:return '未提供'
    if isinstance(value,bool):return '是' if value else '否'
    if isinstance(value,float):return f'{value:,.4f}'.rstrip('0').rstrip('.')
    return str(value)

def _lines(mapping,labels):
    return [f'- {label}：{_value(mapping.get(key))}' for key,label in labels if mapping.get(key) is not None]

def build_context(*,question,market,generated_at,workspace,stock=None,account=None,
                  included_sources=None,warnings=None,mode='research',candidates=None,scan_meta=None):
    included_sources=included_sources or []
    warnings=warnings or []
    candidates=(candidates or [])[:10]
    account=account or {'included':False,'summaries':[],'positions':[],'omitted_positions':0}
    decision=workspace.get('decision',{})
    session=decision.get('session_summary',{})
    sections=[
        '@longbridge\n',
        '# SHORTLIST 研究交接',
        '',
        '## 我的问题',
        question,
        '',
        '## 本地看板规则结论',
        f'- 市场：{"A股" if market=="CN" else "美股"}',
        f'- 市场阶段：{_value(workspace.get("meta",{}).get("phase"))}',
        f'- 当前结论：{_value(decision.get("headline"))}',
        f'- 结论原因：{_value(decision.get("reason"))}',
        f'- 已检查 / 当前可买：{_value(session.get("evaluated_count"))} / {_value(session.get("buyable_now"))}',
    ]
    primary=decision.get('primary')
    if primary:
        sections += _lines(primary,(
            ('symbol','首选代码'),('name','首选名称'),('reason','规则依据'),
            ('current_price','现价'),('entry_min','参考区间下限'),('entry_max','不追价上限'),
            ('stop','失效/保护参考'),('target','第一目标'),('quote_time','行情时间')))
    if stock:
        final=stock.get('final',{})
        quote=stock.get('quote') or {}
        technical=stock.get('technical') or {}
        sections += ['', '## 单股本地快照',
                     f'- 股票：{_value(stock.get("name"))} ({_value(stock.get("symbol"))})',
                     f'- 规则结论：{_value(final.get("action"))} — {_value(final.get("reason"))}',
                     f'- 现价 / 行情时间：{_value(quote.get("price"))} / {_value(quote.get("market_time"))}',
                     f'- 数据状态：{_value(stock.get("data_status"))}；数据源：{_value(stock.get("source"))}',
                     f'- MA20 / MA60 / ATR%：{_value(technical.get("ma20"))} / {_value(technical.get("ma60"))} / {_value(technical.get("atr_pct"))}',
                     f'- 阻塞条件：{" ；".join(map(str,stock.get("blocked_conditions",[]))) or "无"}']
    if mode=='market_scan':
        meta=scan_meta or {}
        sections += ['', '## 本地全市场初筛候选（最多10只）',
                     f'- 对应交易日：{_value(meta.get("trade_date"))}',
                     f'- 候选池刷新：{_value(meta.get("refreshed_at"))}；状态：{"过期快照" if meta.get("stale") else "本轮有效"}',
                     f'- 本地可交接 / 已带入 / 省略：{_value(meta.get("candidate_count"))} / {len(candidates)} / {_value(meta.get("omitted_count",0))}',
                     '- 以下只是本地规则初筛，不是买入推荐；必须刷新外部事实并允许最终零推荐。']
        if not candidates:sections.append('- 本轮没有通过本地规则的候选。')
        for row in candidates:
            sections += ['', f'### {row.get("rank")}. {row.get("name") or row.get("symbol")} ({row.get("symbol")})',
                         f'- 行业：{_value(row.get("industry"))}；本地准备度：{_value(row.get("score"))}；本地结论：{_value(row.get("decision"))}',
                         f'- 入池依据：{_value(row.get("strategy_source"))}；候选类型：{_value(row.get("pool_role"))}',
                         f'- 价格 / 来源 / 时间：{_value(row.get("price"))} / {_value(row.get("price_source"))} / {_value(row.get("price_time"))}',
                         f'- 确认条件：{_value(row.get("confirmation_condition"))}',
                         f'- 不追价线：{_value(row.get("no_chase_line"))}；失效条件：跌破 {_value(row.get("invalidation_condition"))}',
                         f'- 风险组：{_value(row.get("risk_group"))}；候选更新时间：{_value(row.get("candidate_updated_at"))}']
    sections += ['', '## 券商账户数据']
    if not account.get('included'):
        sections.append('- 本次未包含。不得推测我的资产、持仓或可买金额。')
    else:
        for summary in account.get('summaries',[]):
            sections.append(f'### {summary.get("source_label",summary.get("source"))}摘要（{summary.get("updated_at","时间未提供")}{"，过期快照" if summary.get("stale") else ""}）')
            for balance in summary.get('balances',[]):
                values=' ；'.join(f'{k}={_value(v)}' for k,v in balance.items() if k!='currency')
                sections.append(f'- {balance.get("currency") or "基准币种"}：{values or "未提供数值"}')
        sections.append('### 持仓（按估算市值降序，同一股票按券商分开）')
        for row in account.get('positions',[]):
            sections.append(f'- [{row.get("source_label",row.get("source"))}] {row.get("name") or row.get("symbol")} ({row.get("symbol")})：数量 {_value(row.get("quantity"))}，成本 {_value(row.get("cost"))}，估算市值 {_value(row.get("market_value"))}，更新 {_value(row.get("updated_at"))}{"，过期快照" if row.get("stale") else ""}')
        if account.get('omitted_positions'):
            sections.append(f'- 另有 {account["omitted_positions"]} 只持仓因40只上限未带入。')
    sections += ['', '## 数据来源与时间']
    sections += [f'- {row.get("label",row.get("source"))}：{row.get("as_of") or "时间未提供"}{"（过期）" if row.get("stale") else ""}' for row in included_sources]
    if warnings:
        sections += ['', '## 已知限制']+[f'- {warning}' for warning in warnings]
    sections += ['', '## 请在 ChatGPT 继续完成']
    if mode=='market_scan':
        sections += [
            '1. 如已连接 Longbridge App，请使用 `@longbridge` 逐只刷新上述候选的最新行情、新闻、公告/监管文件、财务数据和分析师预期；如未连接，请使用普通联网研究并优先引用一手来源。',
            '2. 先制作10只以内的横向比较表，再最多保留3只；如果没有股票同时通过事实核验、价格位置和风险条件，必须明确写“本轮不推荐买入”，不得凑数。',
            '3. 固定分开四类内容：本地看板规则结论；带时间戳的券商数据；ChatGPT/Longbridge 刷新的外部研究；模型推断。对每个关键数字标注时间和来源，不把推断写成事实。',
            '4. 对最终保留股票逐只输出：核心依据；利多与反方风险；`买／等／不买`；确认条件；不追价线；失效条件；仓位上限。',
        ]
    else:
        sections += [
            '1. 如已连接 Longbridge App，请使用 `@longbridge` 刷新最新行情、新闻、公告/监管文件、财务数据和分析师预期；如未连接，请使用普通联网研究并优先引用一手来源。',
            '2. 固定分开四类内容：本地看板规则结论；带时间戳的券商数据；ChatGPT/Longbridge 刷新的外部研究；模型推断。对每个关键数字标注时间和来源，不把推断写成事实。',
            '3. 请按此结构输出：总体结论；核心依据；利多与反方风险；`买／等／不买`；确认条件；不追价线；失效条件；仓位上限。',
        ]
    next_step=5 if mode=='market_scan' else 4
    if market=='CN':
        sections.append(f'{next_step}. A股必须单列 T+1、涨跌停无法成交、停牌和跳空导致止损无法按计划执行的风险。')
        next_step+=1
    sections.append(f'{next_step}. 仅做研究与条件式建议；不要下单、改单、撤单，不要索要账户号、密钥、令牌或其他凭证。')
    prompt='\n'.join(sections)
    preview={'question':question,'market':market,'mode':mode,'local_conclusion':decision.get('headline'),
             'symbol':stock.get('symbol') if stock else None,'account':account,
             'candidates':candidates,'scan':scan_meta,'included_sources':included_sources,'warnings':warnings}
    return {'prompt':prompt,'preview':preview,'generated_at':generated_at,
            'included_sources':included_sources,'warnings':warnings,
            'account_included':bool(account.get('included')),
            'chatgpt_url':LONGBRIDGE_APP_URL if mode=='market_scan' else CHATGPT_URL,
            'longbridge_app_url':LONGBRIDGE_APP_URL}
