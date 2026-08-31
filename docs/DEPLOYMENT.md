# 部署指南

## 前置条件

- Linux 或 macOS Docker 主机。只有 upstream `collector` profile 会启动 Chromium；M2 experiment profile 不需要 browser。
- Docker Engine。
- Docker Compose v2，命令形式为 `docker compose`。
- 首次启动时可以访问容器镜像仓库。

项目不要求在宿主机安装 Python、Node.js、Nginx 或 Chromium。

## M2 Experiment #1 runtime

Experiment #1 使用固定 Compose identity `llm-trading-lab-exp1`、独立的 `runtime`/`archives` 命名卷和不含 browser 的 `experiment` profile：

```bash
bash scripts/start.sh experiment
docker compose -f deploy/compose.yaml --profile experiment exec -T api \
  python -m zhixing.activation prepare \
  --archive-root /opt/zhixing/data/archives \
  --runtime-dir /opt/zhixing/data/runtime
```

`prepare` 只在私有 runtime 为空时写入 Owner 固定的三只 T+1 ETF 和六个时点；已有配置漂移时 fail closed，不覆盖。随后在本机工作台仅配置模型字段：

- provider：`DeepSeek`
- protocol：`openai_chat`
- exact model identifier：endpoint 实际提供的 DeepSeek V4 Pro identifier（必须明确为 V4 Pro，不能是 Flash）
- endpoint 与 API key：仅保存到私有 `runtime` 卷

不要把 endpoint 或 Key 写进 `.env`、Git、命令行参数、archive 或日志。也不要填写任何券商、验证码或 browser credential。

准备状态可只读检查：

```bash
docker compose -f deploy/compose.yaml --profile experiment exec -T api \
  python -m zhixing.activation status \
  --archive-root /opt/zhixing/data/archives \
  --runtime-dir /opt/zhixing/data/runtime
```

在交易日、三只 ETF 都有当日行情时执行唯一 activation 入口：

```bash
bash scripts/activate_experiment.sh
```

该脚本在写入 start fact 前依次确认 worktree clean、local/origin main baseline、完整 `scripts/verify.sh`、Compose identity、browser 未启动、named volumes、初始 `GET /api/experiment` 以及 API/Web recreate 后账本投影不变。随后才会用真实 DeepSeek V4 Pro endpoint 验证 JSON contract、model echo（若 endpoint 返回）和三标的正式规模整轮 inference；整轮超过 900 秒会返回 `MODEL_LATENCY_BLOCKING`。任一项失败都不会形成 `EXPERIMENT_STARTED`。

启动成功后，`EXPERIMENT_STARTED`、1,000 CNY 初始 NAV、exact model identifier、model echo、三标的 universe、六轮 schedule、fee/benchmark 假设和 verification lock 会作为 append-only ledger fact 保存。DeepSeek V4 Flash 不会自动 fallback；实验开始后的 model/endpoint/credential 变更由 API 拒绝，直到 Owner 另行授权并同步追加 `MODEL_REGIME_CHANGED` fact。

可以独立复核容器重建不改变 ledger projection：

```bash
bash scripts/check_experiment_runtime.sh
```

此 runtime 始终使用 `MarketCollector`、`AuthorizationKind.SIMULATION` 和 `broker_provider=None`。它不是 production deployment，也不授权真实交易。

## 一键启动

在仓库根目录运行：

```bash
bash scripts/start.sh
```

默认启动完整服务。脚本会：

1. 构建 API 和 Web 镜像。
2. 创建私有 `runtime` 与 `archives` Docker 卷。
3. 初始化卷权限。
4. 启动 Selenium Chromium、轮次驱动、API 和网页。
5. 在 Compose 支持时等待健康检查通过。

只启动 API 和网页：

```bash
bash scripts/start.sh web
```

## 部署参数

所有参数都有安全默认值，不创建 `.env` 也能启动。需要调整时：

```bash
cp .env.example .env
```

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `COMPOSE_PROJECT_NAME` | `llm-trading-lab-exp1` | Compose 项目名及数据卷前缀 |
| `ZHIXING_WEB_HOST` | `127.0.0.1` | 网页监听地址 |
| `ZHIXING_WEB_PORT` | `18765` | 网页端口 |
| `ZHIXING_BROWSER_IMAGE` | 已验证的 Chromium 镜像 digest | 浏览器容器 |
| `ZHIXING_BROWSER_MEMORY` | `1g` | 浏览器内存上限 |
| `ZHIXING_BROWSER_SHM_SIZE` | `512m` | Chromium 共享内存 |
| `ZHIXING_API_IMAGE` | `zhixing-api:3.260817.00` | 本地 API 镜像标签 |
| `ZHIXING_WEB_IMAGE` | `zhixing-web:3.260817.00` | 本地 Web 镜像标签 |

`.env` 只用于非机密部署参数。模型 Key、验证码 Key、资金账号和交易密码不要写入 `.env`。M2 只允许在本机网页“运行设置”中填写模型 endpoint/name/provider/protocol/Key；不得配置真实券商或验证码服务。

## 远程服务器访问

网页默认绑定 `127.0.0.1`，外部网络不能直接连接。推荐从本机建立 SSH 隧道：

```bash
ssh -L 18765:127.0.0.1:18765 user@server
```

隧道保持连接时，在本机打开 `http://127.0.0.1:18765`。

如果需要长期通过域名访问，建议保持回环绑定，在同一主机使用带身份认证和 TLS 的反向代理转发。不要把未加认证的工作台直接监听到公网。

## 首次运行设置

以下内容只适用于 upstream 完整 profile，不属于 M2。完整 Compose 中的浏览器服务名为 `browser`。在券商连接设置里填写：

```text
http://browser:4444/wd/hub
```

随后可按 upstream 页面配置。Experiment #1 不使用这条路径，也不得录入真实券商 credential；它的 universe 与 schedule 由 `zhixing.activation prepare` 固定写入。

验证码备用服务按数组位置关联已保存密钥。网页支持追加备用项；修改已有项的识别方式、地址或模型时必须同时填写新密钥，并且不提供已保存项的重排或删除操作，以免空密钥错位沿用。

## 日常运维

```bash
# 状态
bash scripts/status.sh

# 最近 200 行日志
bash scripts/logs.sh

# 只看 API 日志
bash scripts/logs.sh api

# 停止服务，保留数据卷
bash scripts/stop.sh

# 更新源码后重新构建并启动
bash scripts/start.sh
```

日志使用 Docker 的 `json-file` 驱动，并限制为每个容器最多 3 个、每个 10 MiB。

`stop.sh` 会删除容器：`runtime`、`archives` 及其中保存的券商凭据会保留，但浏览器容器里的 Cookie/profile 不持久化，下次启动可能需要重新登录。

## 数据与备份

运行数据位于 Compose 管理的两个命名卷：

- `runtime`：配置、凭据状态、账户快照和运行状态。
- `archives`：策略与执行归档。

卷的实际名称带 `COMPOSE_PROJECT_NAME` 前缀。备份前先运行 `bash scripts/stop.sh`，再使用主机现有的 Docker 卷备份方案同时备份这两个卷。备份文件本身含有个人和交易数据，不得提交到 Git。

`docker compose down` 会保留卷；`docker compose down -v` 会永久删除它们。不要在没有备份时使用 `-v`。

## 排错

```bash
docker compose -f deploy/compose.yaml --profile collector ps
docker compose -f deploy/compose.yaml --profile collector logs --tail=200
docker compose -f deploy/compose.yaml --profile collector config
docker compose -f deploy/compose.yaml --profile experiment ps
docker compose -f deploy/compose.yaml --profile experiment logs --tail=200
docker compose -f deploy/compose.yaml --profile experiment config
```

- `data-init` 显示 `Exited (0)` 是正常状态，它只负责初始化数据卷权限。
- 浏览器长时间不健康时，先检查主机内存和镜像下载状态。
- 网页可打开但没有采集轮次时，确认使用的是默认 `full` 模式，而不是 `web` 模式。
- Experiment #1 没有轮次时，先检查 activation status；`READY_BUT_MODEL_UNCONFIGURED`、`MARKET_DATA_NOT_READY`、`MODEL_ROUTE_MISMATCH` 或 `MODEL_LATENCY_BLOCKING` 都不会自动降级或补跑。
- 修改 `.env` 中的 `COMPOSE_PROJECT_NAME` 会切换到另一组空白数据卷。
