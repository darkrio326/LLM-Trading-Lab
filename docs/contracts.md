# HTTP 契约概要

本文只记录公开部署需要依赖的稳定边界，不包含生产配置、内部运维历史或任何个人数据。后端实现与 smoke 测试是字段级行为的最终依据。

## 1. 通用原则

- API 路径统一位于 `/api/`。
- Web 网关同源转发 API，前端不需要单独配置后端地址。
- JSON 成功响应使用 `ok: true`，失败响应使用 `ok: false`，空结果与失败必须可区分。
- 密钥、资金账号和交易密码不得通过读取接口返回明文。
- 归档与账户数据来自私有 Docker 卷，不进入前端构建产物。

## 2. HTTP 响应契约

成功响应的基本形状：

```json
{
  "ok": true,
  "data": {}
}
```

失败响应的基本形状：

```json
{
  "ok": false,
  "error": {
    "code": "ERROR_CODE",
    "message": "可读错误说明"
  }
}
```

调用方必须先判断 `ok`，不能以 HTTP 200、空数组、空对象或零值代替业务成功判断。

## 3. 公开接口组

- `/api/status`：系统、调度、采集和运行状态。
- `/api/runs`：历史轮次列表、详情与比较。
- `/api/objects`：研究标的维护。
- `/api/account`：账户快照读取。
- `/api/experiment`：M1/M2 isolated synthetic account 的只读、可重建摘要。
- `/api/usage`：模型用量聚合。
- 运行设置接口：调度、模型、验证码、券商连接和运行模式配置。

具体请求字段由 `source/frontend/src/api/` 的类型与 HTTP 客户端定义；后端 smoke 测试覆盖成功、空态、错误态、脱敏和写入校验。

### 3.1 Experiment summary

`GET /api/experiment` 不接受写入参数，不连接券商，也不读取 `/api/account` 的真实账户快照。其 `data` 至少包含：

- `initial_cash`、`current_cash`、`available_cash` 与 `nav`；
- `realized_pnl`、`unrealized_pnl`、`market_value`、`cumulative_fees` 与 `turnover`；
- `high_water_mark`、`drawdown_pct` 与 `max_drawdown_pct`；
- `positions` 与 `open_synthetic_orders`；
- `last_mark_time`、`experiment_start_time` 与 `benchmarks`。
- `activation_state`、`experiment_metadata` 与 `current_model_regime`。

M2 新建但尚未 activation 的 archive 返回 1,000 CNY 初始空态、`activation_state="NOT_STARTED"`，时间和 metadata 字段可以为 `null`，且不会因为 daemon 到达 slot 自动初始化。只有启动硬门禁全部通过后，activation coordinator 才追加唯一的 `EXPERIMENT_STARTED` fact；后续重启从该 fact 重建相同实验。实验开始后，通用模型设置接口不得静默改变 model regime。

## 4. 验证码配置边界

- `识别方式` 只允许 `vision`、`ttshitu` 或 `chaojiying`。
- 主识别服务包含 `接口地址`、`模型`、`识别方式` 和 `密钥`；`备用识别` 是相同四字段对象组成的有序数组。
- GET 返回的主密钥与备用密钥都是脱敏值，前端不得把它们回填到可提交输入框。
- PUT 中原位置、原身份的备用项可以用空密钥表示保留；新增或更换识别方式、地址、模型时必须提交新密钥。
- 当前后端把空 `备用识别` 数组解释为“不修改已有备用链路”，因此公开前端只允许移除尚未保存的草稿项，不提供已保存项的重排或删除。

## 5. 网络边界

Compose 不向宿主机发布 API 和 Selenium 端口。外部请求只能先进入 `web`，默认网页端口也只绑定到 `127.0.0.1`。若通过反向代理提供访问，应由部署者在外层配置身份认证与 TLS。
