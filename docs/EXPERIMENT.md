# LLM-Trading-Lab 实验边界

## 项目身份与目的

LLM-Trading-Lab 是从 [`mivus1128/zhixing`](https://github.com/mivus1128/zhixing) fork 出来的独立实验项目，保留 upstream attribution。项目要复刻并验证 Zhixing 当前的 LLM-driven trading hypothesis：在不重新设计策略的前提下，观察它能否在受控的小额实验中持续产生盈利。

M0 只建立执行安全基线，不是收益实验本身。M0 通过也只表示 **SAFE FOR SIMULATION**，不证明策略有盈利能力，也不表示已获准进行真钱交易。

## 第一阶段资金政策

默认 `ExperimentPolicyConfig`：

| 字段 | 数值 |
|---|---:|
| `initial_bankroll_cny` | 1000 CNY |
| `max_deployed_capital_cny` | 900 CNY |
| `min_cash_reserve_cny` | 100 CNY |
| `max_single_order_cny` | 600 CNY |
| `max_single_symbol_exposure_cny` | 600 CNY |
| `max_drawdown_pct` | 25% |

禁止 leverage、margin 和 short selling。实验净值小于或等于 750 CNY 后，新的 BUY 一律 REJECT；SELL 和 CANCEL 仍可用于退出风险，但 SELL 不能形成卖空。

Policy 只有 `PASS` 与 `REJECT` 两种结论。它不修改模型订单，不自动缩量，也不自动改价。例如模型提出 `BUY 1000 @ 4.20` 时，durable fact 保留完整原单并 REJECT；不能把它改写成 600 CNY 以内的另一笔订单。

M0 的 policy 输入是通用 `ExperimentSnapshot`，不依赖任何券商响应类型。当前只用 FakeBroker 和 simulation 验证。真实账户中的实验资金隔离、成交回报、在途委托与净值 reconciliation 必须在后续 broker integration 中单独设计；M0 不把某个真实券商总账户余额冒充为已完成的实验账本。

## Strategy fidelity

M0 不修改以下策略语义：

- `prompts.SYSTEM_PROMPT` 与 `prompts.OUTPUT_SPEC`；
- indicators、行情采集和技术指标语义；
- 六轮调度策略；
- LLM 的 buy / sell / hold / cancel 格式；
- LLM 决定 qty 与 limit_price 的能力；
- 历史判断机制。

本项目不会用 MA、RSI、MACD、市场趋势或本地涨跌停预测二次否决 LLM。正数、有限数和可转换性检查属于 execution integrity；实验 bankroll、回撤、杠杆和卖空边界属于 Experiment Policy。二者都不同于重新设计 trading strategy。

## BrokerAdapter 可插拔原则

所有写操作都以 `execution.BrokerAdapter` 为边界。通用层只依赖：

- 非机密 `provider` identity；
- `place_order(ValidatedOrder)`；
- `cancel_order(order_reference)`。

`broker_provider = "eastmoney"` 只说明某条 execution fact 实际使用了 upstream 当前 adapter。Eastmoney 不是本项目唯一或长期固定券商；Eastmoney URL、页面、Selenium 和会话细节不得进入 experiment policy、journal 或核心 execution contract。

M0 不实现 GuosenBroker，不接入国信、iQuant、GTrade 或任何其他真实券商。后续 broker integration 必须作为独立范围设计，并重新取得 Owner 对凭据、真实账户和财务 mutation 的明确授权。

## Execution safety 与状态机

manual confirm 与 scheduler / unattended 共用同一个 execution coordinator：

```text
GuardReport
  -> PREPARED (durable proposed order)
  -> Experiment Policy PASS / REJECT
  -> AUTHORIZED
  -> EXECUTING (fsync before BrokerAdapter write)
  -> SUBMITTED
     | SUBMITTED_UNKNOWN
     | REJECTED
     | RECONCILE_REQUIRED
```

`instruction_code` 是 execution journal 的幂等键。`record_id` / `event_id` 只标识 append-only event，不参与“是否已经执行过”的唯一判断。

journal 位于既有 `archive/_execution/journal/` 边界内。每次状态迁移写一份不可变 JSON fact，并 fsync 文件和目录；同一 instruction 的检查、状态迁移和 BrokerAdapter 调用在跨进程文件锁内串行。round archive 记录轮次/调度事实，execution journal 记录外部 side-effect facts，两者不互相冒充。

恢复规则：

- `SUBMITTED`、`SUBMITTED_UNKNOWN`、`REJECTED`、`SIMULATED`、`RECONCILE_REQUIRED` 都是 `instruction_code` 的 replay-blocking terminal state，不自动 replay；历史 simulation instruction 即使未来解除 live lock，也不能被 promote 为真钱订单；
- 恢复时看见 `EXECUTING`，说明 broker 可能已收到请求但本地没有明确 receipt，必须追加 `RECONCILE_REQUIRED`，禁止自动重试；
- broker success 后、round archive 前崩溃时，自动轮次用 slot-stable `strategy_id` 重建相同 `instruction_code`，journal 会阻止再次提交；
- SIMULATION 和源码 M0 verification lock 永远不调用 BrokerAdapter 写操作；
- 该设计声称的是 **AT-MOST-ONCE AUTOMATIC SUBMISSION + REPLAY SAFETY**，不是理论意义上的 exactly-once。

durable facts 记录 strategy、instruction、object、完整 proposed order、model/provider/confidence、policy 结果与原因、authorization kind、execution state、非机密 broker provider、order reference/receipt 和三个时间戳。不得记录券商账户明文、密码、API Key、Cookie、browser session 或 credential。

## M0 当前边界

允许：

- FakeBroker / TestBroker；
- SIMULATION；
- backend smoke、frontend check、公开内容扫描；
- 文档、commit、push 与 Draft PR。

禁止：

- 配置或使用真实东方财富、国信或其他券商账户；
- 真实 BUY / SELL / CANCEL；
- unattended live trading；
- live deployment；
- 修改 LLM strategy semantics；
- 收益优化、智能选股或多模型 leaderboard。

真实 1,000 CNY 实验需要后续 Owner 单独授权，并先完成所选 broker 的独立 integration、实验资金隔离与 reconciliation 设计。
