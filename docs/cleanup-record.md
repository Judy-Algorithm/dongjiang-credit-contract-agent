# 遗留通用 Agent 基座裁剪记录

## 裁剪结果

原通用 Agent 仓库约 900 个业务文件、30MB，包含通用 CLI、消息平台、Skills 市场、RL 训练、Benchmark、语音/媒体工具、网站、ACP、Cron、十余种 Gateway 等。

比赛运行时已经移除：

- `gateway/`：Telegram、Discord、Slack、飞书、钉钉等消息通道。
- `skills/`、`optional-skills/`、`plugins/`：与合同信用审计无关的通用技能市场。
- `environments/`、`tinker-atropos/`、`batch_runner.py`：RL、数据生成与 Benchmark。
- `website/`、`landingpage/`：原通用 Agent 产品官网。
- `acp_adapter/`、`integrations/`：IDE/ACP 桥接。
- `cron/`、语音、媒体、智能家居、社交工具。
- 原 Tk Desktop、Electron Desktop 和 3 万行 Desktop 聚合实现。
- 原通用 Agent 循环、工具注册和模型供应商适配层。
- 旧版本发布说明、打包脚本和原项目测试。

## 保留并重新实现的基座思想

- Request/Context/Result 式的显式业务契约。
- 工作流编排与工具执行分离。
- 结构化事件、Trace ID 和错误可观测性。
- 可选模型增强、确定性工具优先。
- 外部系统通过端口/适配器接入。

## 当前状态

旧通用基座代码和运行时缓存已从业务项目移除。当前仓库只保留东江信审、合同评审、数据安全、系统集成、报告与演示所需模块。

2026-07进一步完成业务编排收敛：

- 删除与LangGraph重复的同步`DongjiangAuditAgent`流程。
- CLI、Web、Demo和评测统一调用`DongjiangWorkflowHarness`。
- 案件级风险优先级统一由`domain/decision.py`维护。
- 领域对象转换统一由`domain/codec.py`维护。
- Web上传解码和文件限制统一为单一实现。
