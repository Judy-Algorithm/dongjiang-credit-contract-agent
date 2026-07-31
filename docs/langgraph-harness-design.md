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
├── dynamic.py    任务白名单、Plan 2.0冻结哈希、依赖校验和执行偏差审计
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

Agent运行记录只包含任务ID、类型、状态、耗时、执行器、版本号、短哈希和受控摘要；不得写入客户名称、合同全文、账号、金额原文、完整哈希、密钥或脱敏映射。

## 5. 动态计划安全边界

1. 计划只能选择白名单任务，不能携带任意代码。
2. 计划最多48个任务，任务ID必须唯一，依赖必须存在且不能形成环。
3. AI合同任务只处理已经本地脱敏的文本，结果始终保持辅助属性。
4. 确定性信用评分器、合同制度规则、人工Interrupt和OA/CRM/SAP回写门禁保持固定。
5. 信用和合同结果在进入人工审批前都要经过独立核验节点。

## 6. Plan 2.0 执行治理

1. **冻结计划**：计划创建后记录任务目录和运行时版本快照，并计算规范哈希；派发和执行前必须通过完整性校验。
2. **节点幂等**：幂等键由计划ID、规范哈希、任务ID、任务类型和输入引用确定。节点先查Checkpoint，再查`data/executions`持久缓存。
3. **AI重试和降级**：合同AI辅助最多尝试2次；失败或证据定位率不足时标记`degraded`并回退到制度规则，不影响确定性底线。
4. **证据门槛**：AI发现至少80%具有案件内文档及片段引用，否则未定位发现不进入后续汇总。
5. **偏差审计**：独立核验比较计划与实际执行结果；缺失任务、越权任务、重复结果、证据失败或计划篡改均为`non_conformant`。
6. **安全路由**：信用偏差强制补件；合同偏差至少进入财务法务复核；已有阻断和特批结论保持不变。

Cloudflare Dynamic Workflows当前不参与本地执行。未来若用于公网事件等待或持久重试，也只编排案件引用和受控状态，不能替代上述计划校验、规则引擎、人工审批和回写门禁。

## 7. 人工审批与角色

| Interrupt | 允许角色 |
|---|---|
| `credit_approval` | credit、finance |
| `credit_supplement` | sales、finance |
| `contract_upload` | sales |
| `sales_revision` | sales |
| `manager_approval` | director、ceo |
| `finance_legal_review` | finance、legal |

OA审批回调必须使用同一个`case_id`，Harness把它映射为LangGraph的`thread_id`，然后通过`Command(resume=...)`恢复。

## 8. 本地运行

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
