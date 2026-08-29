# LLM-Trading-Lab 实验边界

## 项目身份与目的

LLM-Trading-Lab 是从 [`mivus1128/zhixing`](https://github.com/mivus1128/zhixing) fork 出来的独立实验项目，保留 upstream attribution。项目要复刻并验证 Zhixing 当前的 LLM-driven trading hypothesis：在不重新设计策略的前提下，观察它能否在受控的小额实验中持续产生盈利。

M0 只建立执行安全基线，不是收益实验本身。M0 通过也只表示 **SAFE FOR SIMULATION**，不证明策略有盈利能力，也不表示已获准进行真钱交易。M1 在该基线上建立可持续运行的 isolated paper-trading runtime，用来产生可长期复盘的 synthetic account facts；M2 只负责将它配置为经过硬门禁、可长期运行的 Experiment #1。三者都不证明盈利能力，不授权真钱。

## M2 Experiment #1 activation contract

固定实验身份：

- `experiment_id = llm-trading-lab-exp1`
- display name = `Experiment #1 — Zhixing Reproduction`
- baseline main = `323e0b2dfddea0ce6008c231f88edfd7b08c647e`
- initial synthetic NAV / CASH benchmark = 1,000 CNY
- buy-and-hold benchmark = `SH_510300`

固定 tradable universe 只有以下三项，不自动扩展：

| object_id | 名称 | asset_type | lot_size | turnover_mode |
|---|---|---|---:|---|
| `SH_510300` | 沪深300ETF | ETF | 100 | T+1 |
| `SZ_159915` | 创业板ETF | ETF | 100 | T+1 |
| `SH_512880` | 证券ETF | ETF | 100 | T+1 |

六轮仍为 09:35、10:00、11:15、13:15、14:00、14:45（Asia/Shanghai），沿用原有 scheduler 的 deterministic jitter/window 和“错过不补跑”语义。M2 activation 发生在交易日中途时，只从之后第一个合法 slot 开始。

### Model regime

Experiment #1 初始 model regime 固定为 DeepSeek V4 Pro：

- provider = `DeepSeek`
- model family = `DeepSeek V4`
- variant = `Pro`
- protocol = `openai_chat`
- exact model identifier = runtime endpoint 实际接受并回显核对的 V4 Pro identifier

启动前必须用真实 endpoint 完成一个 JSON contract smoke，并用与正式实验相同的三份原始 prompt/context 串行完成一整轮 inference。若 endpoint 返回 model echo，它必须与 requested exact identifier 一致并进入启动 metadata；完整三标的一轮 wall-clock operational target 为 900 秒，超过时返回 `MODEL_LATENCY_BLOCKING`。没有 endpoint/name/Key 时保持 `READY_BUT_MODEL_UNCONFIGURED`，不伪造结果。

DeepSeek V4 Flash 只是 Owner 可在未来另行授权的候选，不是自动 fallback。任何收益表现都不能触发换模。实验开始后通用设置接口不能静默切换 model/endpoint/credential；未来 Owner-authorized switch 必须在同一 ledger 追加 `MODEL_REGIME_CHANGED`，记录 from/to exact model、timestamp、reason 与 `nav_at_switch`，且不重置现金、持仓或 NAV。daemon 会核对私有 model config 与 durable current regime；不一致时停止轮次。

### Explicit start and durable runtime

未启动的 M2 ledger 只读返回 1,000 CNY、空持仓和 `activation_state=NOT_STARTED`，daemon 到达 slot 也不能自动初始化。唯一 activation 入口必须先通过：

- clean worktree 与 exact local/origin main baseline；
- 完整 `scripts/verify.sh`、`VERIFICATION_LOCK=True`；
- `AuthorizationKind.SIMULATION`、`broker_provider=None`、无 broker credential；
- exact universe、six-slot schedule 与 Asia/Shanghai；
- 1,000 CNY initial cash/NAV、空持仓/委托；
- 三项当日有效真实公开行情；
- DeepSeek V4 Pro JSON/echo/三标的一轮 latency probes；
- `runtime`/`archives` Docker named volumes、初始 `GET /api/experiment` 与 API/Web recreate 后相同 ledger projection。

全部通过后才追加唯一的 `EXPERIMENT_STARTED` append-only fact，记录 experiment identity、start timestamp、main SHA、非机密 model identity/echo、universe、schedule、initial NAV、fee assumptions、benchmark、simulation mode、verification lock 与 storage proof。Key、endpoint、broker/browser/cookie/session 不进入该 fact。重启只重建已有实验，不生成新 start。

M2 使用不包含 browser 的 `experiment` Compose profile。daemon 为 `MarketCollector`，API 固定 `broker_provider=None`；`runtime` 卷保存私有模型/catalog/schedule，`archives` 卷保存 round archive、M0 execution journal 和 M1 ledger。容器重建不得重置 synthetic account。

## M1 isolated simulation runtime

M1 的资金事实权威是 `ExperimentLedger`，而不是任何真实券商账户摘要。它以 1,000 CNY 现金、空持仓和 1,000 CNY 初始 NAV 开始，将不可变事件写入私有 archive 下的 `_experiment/ledger/`。每个事件都携带完整 account/order state，因此重启时可仅从 durable facts 重建；不存在另一份可变余额作为第二事实源。

账户派生状态包括：

- current/available cash；
- positions（total/sellable/pending settlement）、average cost、market value；
- realized/unrealized P&L；
- NAV、high-water mark、current/max drawdown；
- cumulative fees 与 turnover；
- open synthetic orders、last mark time 和 experiment start time。

`instruction_code` 仍是一笔 synthetic order 的幂等键。该订单可以从 OPEN 进入后续 terminal state，但每个状态迁移只追加一份完整 durable fact，成交对账户的 delta 只应用一次。同进程 retry、进程重启后 retry 与并发 retry 只返回已有结果，不会再扣现金、再增持仓或再减持仓。单个成交事件同时 durable 记录订单状态和完整 resulting account state，避免在“订单成交”与“账户更新”之间形成可重放窗口。

### Synthetic order state machine

`PaperExecutionEngine`/`SimulationVenueRules` 是独立的 paper venue，不实现、不冒充也不调用 `BrokerAdapter`。

```text
proposed order
  -> REJECTED
  -> OPEN -> FILLED
          -> CANCELLED
          -> EXPIRED
```

- BUY 只支持 catalog 明确标记为可交易、市场为 SH/SZ、资产类型为股票/ETF 且 lot-size 合法的标的；数量必须是 catalog 买入交易单位的整数倍。信息不足的品种 fail closed，不猜测。
- SELL 不得超过 synthetic sellable position；OPEN SELL reservation 也只占用 sellable qty，不会把待交收持仓当成可卖持仓，也不会形成 short position。
- BUY 必须有足够 synthetic available cash 覆盖成交金额和模拟费用。OPEN BUY 按模型 limit price 加费用预留现金；OPEN SELL 预留可卖数量。
- CANCEL 只能关闭存在且仍为 OPEN 的 synthetic order。
- venue 不会修改模型的 qty 或 limit price。不可成交就是 OPEN、REJECTED 或 EXPIRED，不会自动缩量或改价。

### Settlement / turnover assumptions

回转制度是 `SimulationVenueRules` 的市场机制，不属于 `ExperimentPolicy`。catalog 的 `turnover_mode` 只接受 `T+1` 或 `T+0`：

- 为兼容旧 catalog，股票和 ETF 缺失该字段时均保守按 `T+1`；不能仅凭 `asset_type=ETF` 推断 T+0。
- 只有 catalog 明确声明 `turnover_mode="T+0"` 的 ETF，BUY fill 后才立即增加 sellable qty。
- `T+1` BUY fill 当日只增加 total qty，并把该 lot 记入带成交日和 `instruction_code` 的 pending settlement；模型会看到总持仓、可用数量和冻结数量，例如“持有 100、可用 0、冻结 100”。
- settlement 不靠自然日定时猜测。仅当后续一个真实 simulation round 的 observation 日期被现有交易日历确认是有效交易日时，ledger 才追加一次 `SETTLEMENT_RELEASED` fact，将此前交易日的 pending qty 转为 sellable。周末、休市日和同日重启不会提前解冻。

每个持仓事实保存 `qty`、`sellable_qty`、`pending_settlement_qty` 和 `pending_settlements`；完整状态随 append-only event 持久化，所以重启可重建相同的 total/sellable/pending 状态。`ExperimentPolicy` 的 position/exposure 始终按 total qty 计算，SELL 可成交性则只按扣除 OPEN SELL reservation 后的 sellable qty 计算。

### Deterministic fill assumptions

M1 不模拟订单簿深度。每轮仅使用该轮已采集、当日有效且大于零的 quote `last_price` 作为 market reference：

- BUY limit >= reference 时，以 reference 一次性全部成交；
- SELL limit <= reference 时，以 reference 一次性全部成交；
- 当前不 marketable 时保持 OPEN，后续轮次用新的当时 quote 重新判断；
- DAY order 到当地时间 15:00 仍未成交则 EXPIRED。

该模型不使用未来价格，不做部分成交、滑点或流动性深度推测。同一 `strategy_id` 的重试会复用首次 durable market observation，不会用 retry 时已经更新的价格回填原时点。这些都是实验假设，不是对真实成交的声明。

### Fee assumptions

`SimulationFeeModel` 通过 `SimulationFeeConfig` 集中配置，不将费率散落在撮合逻辑里。M1 默认假设为：

| 项目 | 股票 | ETF |
|---|---:|---:|
| commission | 0.03% | 0.03% |
| minimum commission | 5 CNY | 5 CNY |
| sell-side tax | 0.05% | 0 |
| sell-side fee | 0.001% | 0 |

买入费用计入 average cost，卖出费用从 proceeds 扣除并进入 realized P&L。费用是可调整的实验假设，不代表 Eastmoney、Guosen 或任何其他券商的实际费率。

### Mark-to-market, model context and benchmarks

每轮在调用 LLM 前，paper engine 会先处理到期 settlement，再用该轮可获得的 quote 更新持仓市值、未实现盈亏、NAV、high-water mark 和 drawdown。然后将 ledger 中的现金、总持仓、可用/冻结数量、成本、市值和当日 synthetic activity 映射到现有 account/context contract。这保证模型下一轮看到自己前面的模拟交易结果，而不是每轮重置为 1,000 CNY。prompt 文本不变。

`ExperimentPolicy` 的 net equity、available cash、deployed capital、symbol exposure 和 position qty 同样来自 ledger projection；`Store.account()` 中是否存在旧的真实账户快照不影响 M1 策略上下文、policy 或账本。

benchmark 只是比较事实，不会进入策略决策：

- CASH benchmark 始终为 1,000 CNY；
- 可通过 `SimulationBenchmarkConfig.buy_and_hold_symbol` 选择一个 catalog 内标的。第一个可用 quote 确定基准单位数，后续只用当轮 quote mark。

`GET /api/experiment` 提供上述 synthetic account 的只读摘要。M1 不建设完整绩效 dashboard。

### Isolation from real brokers

M1 daemon 的 `MarketCollector` 复用 upstream 现有 quotes、indicators 和 macro 语义，但类型上没有 broker session、captcha solver、login 或 `execution_broker`。daemon 构造 `Runner` 时固定使用 `AuthorizationKind.SIMULATION`、`broker_provider=None` 和 `PaperExecutionEngine`。API 服务同样不构造 broker provider。

M0 execution journal 仍会为原始模型 instruction 留下 replay-blocking `SIMULATED` fact，但 synthetic fill 只由 `ExperimentLedger` 产生。`BrokerAdapter` 仍只表示未来经单独授权的真实外部券商 side effect boundary。`VERIFICATION_LOCK` 保持 `True`；M1 的 BUY、SELL、CANCEL 都不会调用 `BrokerAdapter.place_order`/`cancel_order`。

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

Policy 输入是通用 `ExperimentSnapshot`，不依赖任何券商响应类型。M1 的 snapshot 只从 `ExperimentLedger` 重建；FakeBroker 仍只用于验证 M0 的真实券商写边界不会被触发。真实账户中的实验资金隔离、成交回报、在途委托与净值 reconciliation 必须在后续 broker integration 中单独设计；M1 不把某个真实券商总账户余额冒充为实验账本。

## Strategy fidelity

M0/M1/M2 不修改以下策略语义：

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
     |-> SIMULATED (M1 另由 ExperimentLedger 记录 synthetic order/fill)
     `-> EXECUTING (fsync before BrokerAdapter write)
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

## M0/M1 invariants 与 M2 当前边界

允许：

- FakeBroker / TestBroker；
- isolated synthetic account 上的 SIMULATION；
- 真实公开行情采集、固定 DeepSeek V4 Pro 的原始 LLM 决策与六轮调度；
- backend smoke、frontend check、公开内容扫描；
- 文档、commit、push 与 Draft PR。

禁止：

- 配置或使用真实东方财富、国信或其他券商账户；
- 真实 BUY / SELL / CANCEL；
- unattended live trading；
- live deployment；
- 修改 LLM strategy semantics；
- 收益优化、智能选股或多模型 leaderboard。

M2 只有在 activation hard gates 全部通过并写入 `EXPERIMENT_STARTED` 后，才可持续产生 synthetic 1,000 CNY paper-trading facts；在此之前必须明确保持未启动或 BLOCKED。真实 1,000 CNY 实验需要后续 Owner 单独授权，并先完成所选 broker 的独立 integration、实验资金隔离与 reconciliation 设计。
