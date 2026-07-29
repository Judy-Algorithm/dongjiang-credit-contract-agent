# 东江一体化信审与合同评审 Agent 架构

## 1. 设计原则

1. 规则负责底线，模型负责增强：授信、账期和法律底线由版本化规则确定，LLM 不得覆盖硬规则。
2. 数据先脱敏再出边界：原始文件在本地解析，可逆映射保存在案件隔离区；外部模型只接收脱敏副本。
3. 每个结论都有证据：评分维度、合同事实、命中规则、原文片段、修改建议和审批路由均进入案件 Trace。
4. 端口与供应商解耦：业务层只依赖 `CRMPort` 和 `OAPort`，后续替换为东江 CRM、泛微 OA API。
5. 人机协同：Agent 输出通过、复核、特批或阻断建议，最终信用决策和合同修改由授权人员确认。

## 2. 业务链路

```text
本地资料/CRM
  → 多格式解析与文件分类
  → 本地敏感信息识别、脱敏与隔离映射
  → 查询180天内有效信审结果
      ├─ 有：读取批准额度、账期和特殊条件
      └─ 无：财务 + 外评 + 合作记录 + 企业基础 → 东江评分模型
  → TKP/TKM政策计算额度、账期和尾款条件
  → 信用/财务人员审批 → 授信生效
  → 合同门禁开放 → 销售上传合同 → 合同事实提取
  → 完整性 + 信用交叉校验 + 法律风险规则
  → 合并去重、定位原文、给出修改建议
  → 正常审批 / 财务法务复核 / 总监CEO特批 / 原则阻断
  → 报告导出及 OA/CRM 回写
```

## 3. 代码边界

```text
dongjiang_agent/
├── workflow/       Harness、LangGraph父图、信审/合同子图、Interrupt与Checkpoint
├── ingestion/      DOCX/PDF/XLSX/图片/文本提取
├── security/       可逆本地脱敏和隔离 Vault
├── credit/         信用事实提取与可解释评分子 Agent
├── contract/       合同事实提取与规则审查子 Agent
├── config/         版本化信用政策与合同规则
├── persistence/    案件和有效信审缓存
├── integrations/   CRM/OA 端口及 Webhook 适配器
├── llm/            只接受脱敏文本的可选模型增强
├── reporting/      JSON/HTML 审评报告
└── web/            本地业务API与多页面前端
```

CLI、Web、CRM和OA统一通过`workflow/DongjiangWorkflowHarness`启动和恢复，
不再维护第二套同步编排。Harness把`case_id`作为LangGraph `thread_id`，并用
SQLite保存本地Checkpoint。

## 4. 决策状态机

优先级从高到低：

1. 命中硬底线：`block → return_to_owner`
2. 超批准额度、超建议账期或重大法律风险：`special_approval → director_ceo`
3. 关键字段或条款缺失：`manual_review → finance_legal`
4. 未命中风险：`pass → normal`

当前硬底线：

- TKP 账期超过 90 天。
- TKM 尾款比例超过 40%。
- TKM 尾款账期超过 180 天。

这些规则来自企业答复；模型权重和等级阈值属于比赛建议值，不得标注为东江正式制度。
