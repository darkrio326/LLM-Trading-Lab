# XTP Pro test-environment integration

M2 第一阶段只接入中泰证券官方 XTP Pro **股票测试环境**。它不接真实资金账户，
不解除 `VERIFICATION_LOCK`，不改变 strategy semantics，也不修改或调用
`PaperExecutionEngine`。在 Owner 另行授权测试订单之前，运行时配置固定
`allow_writes=false`，只允许登录、行情订阅和 query/reconcile。

## Runtime 与凭据边界

- 凭据文件默认位于 `~/.config/llm-trading-lab/xtp-pro-test.json`；也可仅通过
  `ZHIXING_XTP_PRO_CONFIG` 指向另一份 owner-only private file。
- 凭据文件和行情配置权限必须为 `0600` 或更严格，且配置文件、行情配置、
  SDK 目录和 SDK runtime/log 目录都位于仓库之外。
- 私密文件包含的字段仅限 `environment`、`allow_writes`、`sdk_library_dir`、
  `runtime_dir`、`client_id`、`software_key`、`local_ip`、`socket_type`、
  `timeout_seconds`、`trader`、`quote` 与 `market_data_probe`。本仓库不提供
  带值模板，不通过命令行参数传递这些值。
- `trader` 和 `quote` 内的 endpoint、port、username、password，以及 quote
  config path 都只在进程内传给官方 SDK。应用日志、Git、round archive、
  execution journal、PR 和 Project Gate evidence 不记录它们。
- 官方 SDK 的 fatal-only runtime 输出保留在 private runtime 目录；公开 smoke
  只输出阶段成功/失败和异常类型，不输出账户快照、XTP id、session id 或错误原文。

配置加载器只接受 `environment="test"`、`allow_writes=false` 和 TCP socket。
这不是 live unlock；真实账户与真实 BUY/SELL/CANCEL 仍没有授权通路。

## 官方环境

官方 XTP Pro Python API 当前仓库版本为 1.2.1，预编译 Linux binding 使用
Python 3.9.13 与 Boost 1.80，并要求 Linux x86_64。SDK 和原生库不 vendoring
进本仓库；运行时从仓库外的官方 SDK 目录加载 `vnxtpxtrader`、
`vnxtpxquote` 及其对应 native libraries。

官方 Git 仓库的 Linux binary 目录不包含其动态依赖的 Boost 1.80 shared
libraries；private runtime 必须另行提供 exact 1.80 runtime。不得把系统可得的
其他 Boost 版本改 SONAME 后冒充兼容依赖，ABI import 失败必须按 SDK unavailable
报告。

宿主机不是该组合时，应使用隔离的 Linux/amd64 Python 3.9 runtime；不能把
“源码可导入”或“容器可启动”冒充为 test login 成功。

## BrokerAdapter mapping

`XtpProBroker.provider = "xtp_pro"`。通用 contract 返回不含资金账号、股东账号、
PBU、endpoint 或 session 的 `BrokerOrder`、`BrokerTrade`、`BrokerAsset` 和
`BrokerPosition`：

| XTP fact | Broker-neutral semantics |
| --- | --- |
| `XTP_ORDER_STATUS_INIT` + insert submitted/accepted | `accepted_submitted` |
| `XTP_ORDER_STATUS_NOTRADEQUEUEING` | `accepted_submitted` |
| `XTP_ORDER_STATUS_REJECTED` 或 insert rejected | `rejected`，terminal |
| `XTP_ORDER_STATUS_PARTTRADEDQUEUEING` | `partially_filled`，non-terminal |
| `XTP_ORDER_STATUS_PARTTRADEDNOTQUEUEING` | `partially_filled`，terminal remainder |
| `XTP_ORDER_STATUS_ALLTRADED` | `filled`，terminal |
| `XTP_ORDER_STATUS_CANCELED` | `cancelled`，terminal；保留累计成交数量 |
| `XTP_ORDER_STATUS_UNKNOWN`、未知组合或歧义 | `reconcile_required` |

XTP 的 `OnOrderEvent` 不推送部分成交状态，因此 adapter 也使用
`qty_traded` 与 trade records 建立 `partially_filled`，不能只依赖 order callback。
cancel submitted/rejected 不把原委托误标成已撤；cancel accepted 但尚未取得
`CANCELED` order status 时保持 `reconcile_required`。

## Durable identity 与 reconciliation

```text
instruction_code (durable journal idempotency key)
  -> deterministic uint32 order_client_id
  -> order_xtp_id (primary broker order reference; trading-day scoped)
  -> order_local_id / order_exch_id / order_cancel_xtp_id
  -> exec_id, or market + report_index (trade reference)
```

`order_client_id` 是 `instruction_code` 的稳定、非零 32-bit hash，只用于定位候选；
execution journal 在明确成功后持久保存 `instruction_code -> order_xtp_id`。当请求可能
已到达 XTP 但本地没有明确结果时，上层保持 `SUBMITTED_UNKNOWN` 或
`RECONCILE_REQUIRED`，adapter 只能按 client id 加 market/symbol/side/qty/price
查询订单，再按 exact `order_xtp_id` 查询成交。零候选、多候选或字段不一致都保持
reconciliation required，绝不据此再次下单。

`insertOrder` 和 `cancelOrder` 都只有一次 SDK 调用。调用后的 exception/timeout
统一标记 `submitted_unknown=True`；只有官方同步返回 `0` 才作为明确未发送。

## 验证分层

确定性 adapter tests 不加载 SDK、不读取 private config、不联网，只使用
`FakeTransport`：

```bash
cd source/backend
PYTHONDONTWRITEBYTECODE=1 python -m tests.xtp_pro_adapter
```

官方测试环境 smoke 与 CI 分开，且只读：

```bash
cd source/backend
PYTHONDONTWRITEBYTECODE=1 python -m tests.xtp_pro_test_environment
```

第二条命令依次验证 native SDK/Python、trader test login、asset、positions、orders、
exact order（账户已有委托时）、trades 和 depth market data。它不调用
`place_order` / `cancel_order`；输出中的 `financial_write_calls` 固定为 `0`。
