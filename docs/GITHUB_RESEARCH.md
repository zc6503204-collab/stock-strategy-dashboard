# GitHub 选股与交易项目调研：本工作台的取舍

本文件记录架构层面的借鉴。当前工作台没有复制或安装这些仓库的业务代码；每日筛选、信号、仓位与提醒仍由本机确定性规则完成，不调用模型，不提交真实订单。

## 已吸收的做法

| 项目 | 借鉴内容 | 在本工作台中的落地 |
|---|---|---|
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | 把研究、交易判断、风险控制和组合管理分层；保留决策日志 | 盘前候选、盘中触发、资金否决、真实/模拟持仓与提醒分别记录。AI以后只解释候选，不掌握最终风控权 |
| [TradingAgents-AShare](https://github.com/KylinMountain/TradingAgents-AShare) | A股语义、持仓跟踪、定时分析和结构化报告 | A股T+1、ST/高波动分类、真实持仓输入、Mac后台盯盘与收盘摘要 |
| [FinRL-Trading](https://github.com/AI4Finance-Foundation/FinRL-Trading) | 数据、选股、回测、纸面执行采用同一流水线；强调时点数据 | 同一信号结构用于推荐、模拟成交、提醒和复盘；历史记录保留数据时间与规则版本 |
| [Qlib](https://github.com/microsoft/qlib) | 信号、组合策略、执行与回测分层；横截面因子排名 | 日线“准备度”与盘中“可买”拆开；相对强弱、趋势、成交额由代码计算 |
| [LEAN](https://github.com/QuantConnect/Lean) | 策略信号、组合构建、风险和执行模块可替换 | 四种策略统一注册、统一输出信号，再由同一资金与风控层决定是否可买 |
| [PyBroker](https://github.com/edtechre/pybroker) | 按时间顺序回放，明确训练、预热和交易区间 | 只使用完整K线和当时可见数据，信号在下一根K线复核成交，不用当前资料补写历史状态 |
| [QuantStats](https://github.com/ranaroussi/quantstats) | 收益、回撤、连续亏损和风险收益指标 | 按市场、策略和参数版本展示成本后收益、最大回撤、连续亏损、盈亏比及成本加倍结果 |
| [RD-Agent](https://github.com/microsoft/RD-Agent) | 因子提出、实验、淘汰、迭代的研究闭环 | 后续用于离线提出候选因子，再经走步检验；不在盘中自动改规则 |
| [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | 数字计算与语言解释分工 | 均线、ATR、VWAP、量比、成本、仓位和退出价全部由程序计算；AI只做解释和复盘辅助 |
| [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | 模块化投资约束与组合风险检查 | 真实持仓阻止同股重复推荐，高风险持仓占用风险名额 |
| [vectorbt](https://github.com/polakowo/vectorbt) / [Backtrader](https://github.com/mementum/backtrader) | 快速参数实验；佣金、滑点、下一根成交与成交量约束 | 当前模拟使用下一根复核、成本、容量与保守同根止损；以后用独立研究环境做参数稳健性比较 |

## 暂不接入的部分

- 不让多名LLM Agent持续扫描全市场。成本高、速度慢，也会把语言判断混进数值准入。
- 不直接采用强化学习模型或论文收益。先积累逐笔前向模拟，并检查费用、滑点、停牌、涨跌停和数据泄漏。
- 不自动生成目标价。保护价来自技术失效点，止盈价和仓位由风险距离计算；真实持仓缺少保护价时直接提示补录。
- 不自动交易。券商接口保持只读，任何真实买卖都由用户在券商端决定和执行。

## 当前统一流程

`按策略盘前候选 → 日线硬性准入 → 盘中逐策略确认 → 实时行情与盘口复核 → 综合买卖决策 → 用户决定 → 真实持仓登记/只读同步 → 止损止盈盯盘 → 按策略和版本复盘`

盘前分数只比较“是否值得盯”，盘中买点必须同时通过行情时效、同源数据、市场状态、技术触发、追价上限、成本与风险预算。任何一项缺失都显示等待或暂不买入。
