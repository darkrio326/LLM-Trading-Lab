# LLM-Trading-Lab（知行 Zhixing 独立实验 fork）

本仓库 fork 自 [`mivus1128/zhixing`](https://github.com/mivus1128/zhixing)，用于复刻并验证其 LLM-driven trading 方法。M2 将 M1 的 1,000 CNY isolated synthetic account 建立为显式、硬门禁的长期实验 runtime；只有真实公开行情、DeepSeek V4 Pro endpoint、三标的整轮 inference 和 durable volume 全部通过后，才会追加唯一的 `EXPERIMENT_STARTED` fact。源码 verification lock 保持生效。当前另有独立、只读的 XTP Pro 官方股票测试环境 integration；它不接真实资金账户、不提交测试订单，也不授权 unattended live trading 或 production deployment。实验边界与归因见 [docs/EXPERIMENT.md](docs/EXPERIMENT.md)，XTP 边界见 [docs/XTP_PRO.md](docs/XTP_PRO.md)。

知行是第三代自托管交易研究与自动化工作台。项目包含 Python 后端、React 前端、独立浏览器容器和 Docker Compose 部署配置。

当前构建号为 `3.260817.00`。它采用“代次.日期.构建”的项目编号，不是语义化版本号。

## 公开版包含什么

- `source/backend/`：后端源码与自检用例。
- `source/frontend/`：React + TypeScript 前端源码和锁定依赖。
- `frontend-dist/`：可直接部署的网页产物；由仓库中的前端源码可重复构建。
- `deploy/`：API、轮次驱动、浏览器与 Web 网关的容器配置。
- `scripts/`：启动、停止、状态检查和公开内容扫描脚本。

公开仓库不包含任何资金账号、交易密码、API Key、Cookie、浏览器登录态、策略归档、账户快照或服务器日志。首次启动会得到一个空白运行环境，个人配置需要在网页的“运行设置”中填写。

## 快速启动

适用于装有 Docker Engine 和 Docker Compose v2 的 Linux 主机：

```bash
cd <仓库目录>
bash scripts/start.sh
```

脚本默认构建并启动 `api`、`daemon`、`browser` 和 `web` 服务。M2 的 `daemon` 只使用真实公开行情与 `ExperimentLedger`，不构造券商会话；`browser` 是 upstream compose 中保留的服务，不是 isolated simulation runtime 的资金或执行事实来源。启动完成后，在部署机器上打开：

```text
http://127.0.0.1:18765
```

只想先查看空白工作台、不启动采集轮次和浏览器时：

```bash
bash scripts/start.sh web
```

Experiment #1 使用不启动 browser 的独立 profile：

```bash
bash scripts/start.sh experiment
```

这条命令只建立 runtime；它不会绕过启动门禁或自动写入 `EXPERIMENT_STARTED`。完整 preparation、模型私密配置与 activation 流程见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

默认只监听本机回环地址，不会直接暴露到公网。部署在远程服务器时，从自己的电脑建立 SSH 隧道：

```bash
ssh -L 18765:127.0.0.1:18765 user@server
```

然后仍访问 `http://127.0.0.1:18765`。

## 首次配置

进入“运行设置”后按页面提示填写：

1. 在“交易对象”页添加需要研究或运行的标的；公开版不会预置个人清单。
2. 模型服务的接口地址、协议、模型名称和 Key。
3. M2 会把调度固定为 09:35、10:00、11:15、13:15、14:00、14:45（Asia/Shanghai）；daemon 始终以 `AuthorizationKind.SIMULATION` 更新 synthetic account，运行模式不会解除真实交易锁。
4. upstream 验证码与券商连接设置仍保留于界面，但 M2 experiment runtime 不使用它们；**不得在这里录入真实账号、交易密码或其他券商 credential**。XTP Pro test credential 只使用仓库外的 private runtime config，见 [docs/XTP_PRO.md](docs/XTP_PRO.md)。

这些值保存在 Docker 的私有 `runtime` 卷中，不写入源码目录，也不通过 `.env` 提交。重新构建镜像不会自动删除它们。

验证码备用服务按显示顺序与已保存密钥对应。公开界面允许追加备用服务和修改原位置配置，但不会重排或删除已保存项目；更换某一项的识别方式、地址或模型时，需要同时输入该项的新密钥。

## 常用命令

```bash
# 查看服务状态
bash scripts/status.sh

# 查看日志
bash scripts/logs.sh

# 停止容器但保留运行数据
bash scripts/stop.sh

# 运行公开内容扫描、后端自检和前端构建校验；有 Docker 时也校验 Compose
bash scripts/verify.sh
```

部署参数可通过根目录 `.env` 调整；先复制 [.env.example](.env.example)，其中只允许放非机密的容器参数。完整部署说明见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)，数据边界见 [docs/PRIVACY.md](docs/PRIVACY.md)。

## 开发

核心后端与确定性测试仅使用 Python 标准库：

```bash
cd source/backend
PYTHONDONTWRITEBYTECODE=1 python -m tests.smoke
PYTHONDONTWRITEBYTECODE=1 python -m tests.xtp_pro_adapter
```

credential-gated 的 XTP Pro 测试环境网络 smoke 不属于 CI，必须在官方支持的
Linux x86_64 / Python 3.9 runtime 中单独执行；它不会提交订单。

前端使用锁定版本的 Node 依赖：

```bash
cd source/frontend
npm ci
npm run check
```

`npm run check` 会生成 `source/frontend/dist/`。发布前，该目录应与根目录 `frontend-dist/` 逐文件一致。

## 许可证

本项目以 [Apache License 2.0](LICENSE) 发布。自动化交易具有实际资金风险，使用者需要自行确认券商规则、接口许可和适用法律，并对运行结果负责。
