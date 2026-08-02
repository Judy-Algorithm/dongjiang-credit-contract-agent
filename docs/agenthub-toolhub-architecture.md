# Agenthub / Toolhub 可迁移架构

当前版本采用本地实现，但业务调用边界已经按远程 Hub 设计：

```text
案件主 Agent（LangGraph）
  └─ AgentRegistry.invoke(agent_id, payload)
       ├─ credit_review   → 信用 Dynamic Workflow
       └─ contract_review → 合同 Dynamic Workflow

信用/合同子 Agent
  └─ ToolRegistry.invoke(tool_name, payload)
       ├─ document.extract
       ├─ credit.extract_facts / assess / control / analyze_dimension
       └─ contract.extract_facts / policy_review / ai_review
```

案件主 Agent 会先生成独立的案件级计划，描述两个子 Agent、人工节点和企业回写的依赖。
大模型规划只作为白名单计划提议层；系统会重新补齐并校验全部必需治理任务，非法提议
自动降级为确定性计划。

## 当前运行方式

- `LocalAgentRegistry`：在当前进程运行两个 LangGraph 子图。
- `LocalToolRegistry`：在当前进程调用文档、信用、合同和文本模型能力。
- 主工作流仍拥有正式审批、权限、状态机、持久化和系统回写边界。
- 每次 Agent 分配和工具调用只记录结构化摘要、耗时、状态和provider，不记录正文。

## 未来远程适配

官方接口明确后实现：

- `AgenthubAgentRegistry.discover/invoke`
- `ToolhubToolRegistry.discover/invoke`
- 异步运行需要补充 `get_status/get_result/cancel`
- 文件能力需要确认上传协议、临时URL、大小限制和数据驻留要求
- 写操作工具需要身份透传、审批凭证、幂等键和审计回执

远程 Hub 不可用时应保留本地注册中心作为可配置降级路径。正式授信批准、例外授权、
OA审批完成以及CRM/SAP写回不允许由子 Agent 自主决定。
