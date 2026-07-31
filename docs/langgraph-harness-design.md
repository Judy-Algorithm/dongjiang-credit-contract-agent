# LangGraph 与 Harness 落地设计

## 1. 职责边界

Harness 是CRM、泛微OA、Web和CLI共同调用的稳定入口，负责案件号、调用身份、角色权限、信用资料与合同的分阶段暂存、Checkpoint数据库和暂停/恢复。LangGraph只负责节点、分支、子图和循环。

```text
CRM / OA / Web / CLI
        ↓
DongjiangWorkflowHarness
        ↓
LangGraph parent workflow
   ├── credit_workflow：计划 → 动态扇出 → 汇总 → 评分 → 独立核验
   └── contract_workflow：计划 → 按合同扇出 → 汇总 → 独立核验
        ↓
现有解析、评分、脱敏、合同规则和报告模块
```

## 2. 代码位置

```text
dongjiang_agent/workflow/
├── state.py      案件共享状态
├── codec.py      领域对象与Checkpoint字典转换
├── nodes.py      业务节点与人工interrupt
├── graph.py      父图、信审子图、合同子图和路由
├── dynamic.py    任务白名单、结构化计划、依赖校验和案件级任务选择
└── harness.py    身份、权限、暂存、启动、查询和恢复
```

## 3. 主流程

```text
create_case
→ ingest_credit_documents
→ check_credit_cache
    ├─ 命中 → contract_gate
    └─ 未命中 → credit_workflow（动态分析计划 + Send并行扇出 + 独立核验）
                  → interrupt(credit_approval)
                      ├─ 批准/调整后批准 → activate_credit
                      ├─ 补资料 → interrupt(credit_supplement) → 重新计算
                      └─ 拒绝 → finalize
→ contract_gate（仅接受有效授信）
→ 无合同：interrupt(contract_upload)
→ 上传合同：ingest_contract_documents → contract_workflow（按合同和工具可用性动态展开）
→ decide
    ├─ block → interrupt(sales_revision)
    ├─ special_approval → interrupt(manager_approval)
    ├─ manual_review → interrupt(finance_legal_review)
    └─ pass → finalize
→ finalize → 报告、案件库、CRM/OA待回写数据
```

## 4. 数据安全

粘贴的合同正文由Harness写入本地临时收件区，Graph状态只接收文件路径。信用资料由`ingest_credit_documents`处理，合同由`ingest_contract_documents`处理；两个入口会拒绝错误类型的文件。解析节点完成本地可逆脱敏并删除临时原文，Checkpoint只保存结构化事实、脱敏后的条款以及Vault路径。

Agent运行记录只包含任务ID、类型、状态、耗时、执行器和受控摘要；不得写入客户名称、合同全文、账号、金额原文、密钥或脱敏映射。

## 5. 动态计划安全边界

1. 计划只能选择白名单任务，不能携带任意代码。
2. 计划最多48个任务，任务ID必须唯一，依赖必须存在且不能形成环。
3. AI合同任务只处理已经本地脱敏的文本，结果始终保持辅助属性。
4. 确定性信用评分器、合同制度规则、人工Interrupt和OA/CRM/SAP回写门禁保持固定。
5. 信用和合同结果在进入人工审批前都要经过独立核验节点。

## 6. 人工审批与角色

| Interrupt | 允许角色 |
|---|---|
| `credit_approval` | credit、finance |
| `credit_supplement` | sales、finance |
| `contract_upload` | sales |
| `sales_revision` | sales |
| `manager_approval` | director、ceo |
| `finance_legal_review` | finance、legal |

OA审批回调必须使用同一个`case_id`，Harness把它映射为LangGraph的`thread_id`，然后通过`Command(resume=...)`恢复。

## 7. 本地运行

```bash
python3 -m pip install -e .

python3 -m dongjiang_agent.cli audit --case /path/to/your-case.json

python3 -m dongjiang_agent.cli workflow-status --case-id DJ-XXXXXXXXXX
```

也可以使用API：

```text
GET  /api/cases
POST /api/cases
GET  /api/cases/{case_id}
POST /api/cases/{case_id}/credit-documents
POST /api/cases/{case_id}/credit-actions
POST /api/cases/{case_id}/contracts
POST /api/cases/{case_id}/contract-actions
```
