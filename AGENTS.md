# AGENTS.md — 公开仓库协作约束

## 沟通与编码

- 默认使用简体中文说明问题，代码标识符和协议名称保持原样。
- 文本文件使用 UTF-8；部署脚本使用 LF 换行。
- 修改前先阅读 `README.md`、`docs/ARCHITECTURE.md`、`docs/contracts.md` 和 `docs/PRIVACY.md`。

## 隐私红线

- 绝不提交真实账号、密码、Key、Cookie、浏览器 profile、账户快照、策略归档或运行日志。
- 示例数据必须明显为虚构内容；网址优先使用保留域名 `.invalid`。
- 不得把本机用户名、绝对路径、服务器地址或内部运维记录写入仓库。
- 提交前运行 `python scripts/check_public_tree.py`。

## 核心行为边界

本仓库是独立实验项目 **LLM-Trading-Lab**，fork 自 `mivus1128/zhixing`；保留 upstream attribution，但后续治理、实验结论和发布身份均属于本仓库。

- `execution.BrokerAdapter` 是唯一通用券商写边界。核心 execution、policy、journal、idempotency 与 replay safety 必须保持 broker-agnostic；不得把 Eastmoney 页面、URL、Selenium 细节或字段写进通用层。Eastmoney 只是 upstream 当前 adapter，不是固定券商。
- 券商登录、验证码识别、交易执行、策略生成和自动轮次属于核心行为。修改这些语义必须有 Owner 对 exact scope 的明确授权。
- 未经 Owner 单独明确授权，不得接入或操作真实券商账户，不得执行真实 BUY/SELL/CANCEL，不得开启 unattended live trading，也不得解除源码 verification lock。
- 未经 Owner 明确授权，不得修改 prompt、指标、行情语义、六轮调度、LLM 决策格式、模型 qty/limit_price 权限或历史判断等 strategy semantics。
- 不得把新券商预先猜进 contract。后续 broker integration 必须单独设计、评审和授权；不要大规模重命名 `zhixing` Python package。

部署、文档、前端和不改变上述核心语义的数据展示改动可以正常进行。对边界有疑问时，先向项目所有者确认。

## 验证

- 后端：在 `source/backend` 运行 `PYTHONDONTWRITEBYTECODE=1 python -m tests.smoke`。
- 前端：在 `source/frontend` 运行 `npm ci && npm run check`。
- 发布：确认 `source/frontend/dist` 与 `frontend-dist` 逐文件一致，并检查 `docker compose -f deploy/compose.yaml --profile collector config`。
