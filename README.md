# 东江一体化信审与合同评审 Agent

面向火鸟黑客松“AI 智能信审与合同评审 Agent”命题的精简专用仓库。

它不是通用聊天机器人，而是一条可追踪的制造业风控工作流：

```text
信用资料解析 → 本地脱敏 → 信用评估 → 人工审批生效
→ 合同门禁 → 合同规则审查 → 信用交叉校验
→ 风险分级 → 人工/特批路由 → 报告与系统回写
```

## 立即运行

环境要求：Python 3.11+。正式工作流使用 LangGraph 1.1 和 SQLite Checkpointer。

```bash
cd /Users/zhuzhu/Desktop/黑客松agent/dongjiang-credit-contract-agent
python3 -m pip install -e .

# 本地业务页面
python3 -m dongjiang_agent.cli serve --host 127.0.0.1 --port 8765
# 浏览器打开 http://127.0.0.1:8765

# 离线测试
python3 -m unittest discover -s tests -v

# 使用你准备的案件集执行回归评测
python3 scripts/evaluate.py --cases /path/to/cases.json
```

也可以运行 `./scripts/run_web.sh`。

## HKGAI 服务配置

复制`.env.example`为`.env`并填写凭证。`.env`已加入`.gitignore`，CLI和Web
启动时会自动加载它，且不会覆盖部署环境中已经设置的变量。

```text
OPENAI_BASE_URL      HKGAI文本模型OpenAI兼容地址
OPENAI_API_KEY       文本模型API密钥
OPENAI_MODEL         /v1/models返回的模型ID
HKGAI_SPEECH_*       语音服务地址和API密钥
HKGAI_TOOLHUB_*      Toolhub服务地址
HKGAI_AGENTHUB_*     Agenthub搜索服务地址
HKGAI_APP_NAME       Toolhub/Agenthub共用App-Name
HKGAI_APP_KEY        Toolhub/Agenthub共用App-Key
```

当前代码已具备文本模型网关；语音、Toolhub和Agenthub的环境变量已预留，
对应客户端及工作流节点仍需按具体API文档接入。

可以执行以下命令检查配置；输出只包含布尔状态和模型ID，不会显示密钥：

```bash
python3 scripts/check_hkgai_config.py
```

## LangGraph + Harness 工作流

`DongjiangWorkflowHarness` 是CRM、泛微OA、Web和CLI调用的稳定边界，负责案件号、角色权限、信用资料与合同的分阶段暂存、Checkpoint和恢复；LangGraph负责信审/合同子图、条件路由、循环和人工中断。

模型计算结果不等于正式授信。系统依次区分：

1. `calculated`：模型已给出建议额度、账期和风险等级；
2. `pending_approval`：等待信用或财务人员审批；
3. `effective`：审批后的额度和账期正式生效，合同入口才会开放。

历史有效授信只会按统一社会信用代码或CRM客户编号自动复用；仅有企业名称时重新评估，避免同名主体串用额度。

前端只调用业务接口，内部工作流状态不会暴露给页面：

- `GET /api/cases`
- `POST /api/cases`
- `GET /api/cases/{case_id}`
- `POST /api/cases/{case_id}/credit-documents`
- `POST /api/cases/{case_id}/credit-actions`
- `POST /api/cases/{case_id}/contracts`
- `POST /api/cases/{case_id}/contract-actions`
- `POST /api/cases/{case_id}/revisions`
- `GET /api/cases/{case_id}/revisions/{revision_id}/redline|clean`
- `POST /api/cases/{case_id}/revisions/{revision_id}/submit`

详细设计见[LangGraph与Harness落地设计](docs/langgraph-harness-design.md)。

## 当前业务规则

规则状态是 `PROPOSED_NOT_OFFICIAL`：除企业明确给出的底线外，评分权重属于比赛建议，必须在答辩时如实说明。

- 风险等级：低风险（≥75）、中风险（55–74.99）、高风险（<55）。
- 建议评分维度：财务 45%、外部评级 20%、合作记录 25%、企业基础 10%。
- 新客户没有合作记录时，关闭该维度并对其余有效权重归一化。
- 财务或企业基础字段缺失时，只在实际取得的指标内重新归一权重，不把未知值当成0。
- 多机构主体评级分别保存机构、等级、展望、日期和来源；冲突时默认取保守值，重大冲突转人工复核。
- 最大授信额度：`月度订单额 × 风险等级月份系数`。
- 当前未收款与在手已入单金额共同占用批准额度，合同按剩余可用额度校验。
- 当前未收款逾期超过30天或占用超过批准额度时锁定；须由授权人员上传批准附件后特别放行。
- TKP：低/中/高风险建议账期 90/60/30 天；超过 Net 90 天需核验终端项目统一账期或市场总监特别批准。
- TKM：低风险常规条件为最多 40% 尾款、180 天；中高风险进一步收紧，超出常规条件需市场总监特别信用申请。
- TKM区分汽车及标准业务、精密模具业务；首期采购款豁免、尾期条件和客户总信用额分别记录并批核。
- 超过一年无新订单且无欠款、无在手订单时清零授信并转 `Inactive`；再次申请按新客户并补充历史交易付款记录。
- 超批准额度、超建议账期或超 TKP/TKM 常规条件：进入特批，不把可授权例外误判为绝对阻断。
- 无责取消、关联主体责任外溢、间接损失等不可直接接受的合同底线：原则阻断、退回业务修改。
- 缺关键合同条款：财务/法务人工复核。

所有参数在以下文件中调整，不需要改代码：

- `dongjiang_agent/config/credit_policy.json`
- `dongjiang_agent/config/contract_rules.json`

## 文件支持

- 原生：TXT、Markdown、CSV、JSON、HTML。
- OOXML：DOCX、XLSX。
- PDF：优先 `pypdf`，否则使用本机 `pdftotext`。
- 图片：检测到本机 Tesseract 时执行 OCR，否则给出明确降级提示。

Web页面包含登录、我的待办、案件列表、发起信审、六页签案件工作台、案件处理、用户管理和安全审计。首次启动需在页面创建管理员；普通账号只能由管理员创建，不开放公开注册。销售、信用管理、财务、法务、市场总监、集团管理层和管理员按照真实登录身份执行工作流权限。新案件自动归属发起销售，管理员可以改派负责人。新解析文件支持 PDF 页码、DOCX 段落、XLSX 工作表/单元格和文本行号定位，风险卡片可在受控查看器中打开对应原文。信用资料和合同使用不同入口，合同只能在授信审批生效后上传。上传限制为单文件15MB、单次请求30MB。

合同处于“等待修改”时，销售可以逐项采用标准建议、人工修改或保留并填写理由。系统不会覆盖原合同，而是生成带 Word 修订痕迹版和清洁版；清洁版可以直接重新送入既有规则审查，修订决策、文件摘要和复审结果按案件保留。当前自动修订支持具有行号或段落号定位的 TXT、Markdown 和 DOCX；PDF 及复杂版式合同保留人工上传新版本通道。

比赛演示可用合成数据一键准备三条固定路径：

```bash
python3 scripts/prepare_demo_cases.py
```

脚本重复执行不会重复创建案件，生成的资料位于已忽略的数据目录。

可选安装：

```bash
python3 -m pip install -e '.[documents]'
```

模型网关使用Python标准库，无需额外安装SDK。外部模型只能通过
`OpenAICompatibleGateway.analyze_redacted()`接收含脱敏令牌的文本；没有脱敏令牌时会拒绝发送。

## 输出

案件状态会持续写入本地案件库；流程完成或关闭后会产生：

- `data/cases/DJ-*.json`：可审计案件状态和 Agent Trace。
- `data/vault/DJ-*.vault.json`：仅本地保存的可逆脱敏映射。
- `data/evidence/DJ-*/*`：例外审批和特别放行附件，按SHA-256归档。
- `data/archive/DJ-*/credit|contract/*`：案件级原始信用资料和合同，按SHA-256归档。
- `data/integrations/DJ-*/*.json`：OA、CRM、SAP每次调用的成功、失败或未配置记录。
- `data/auth/auth.sqlite`：本地用户、密码摘要、会话和安全审计；不保存明文密码或会话令牌。
- `data/revisions/DJ-*/REV-*`：合同修订决策、带修订痕迹版、清洁版和重新审查结果。
- `output/DJ-*/audit-result.json`：机器可读结果。
- `output/DJ-*/audit-report.html`：人工审阅报告。
- `output/evaluation-report.json`：量化回归结果。

`data/cases`、`data/vault`、`data/auth` 和 `output` 已加入 `.gitignore`，不得提交真实企业数据或身份数据。

## 架构与交付状态

- [业务操作说明书](docs/user-manual.md)
- [典型案例集](docs/demo-cases.md)
- [系统架构](docs/architecture.md)
- [LangGraph与Harness落地设计](docs/langgraph-harness-design.md)
- [命题要求实现矩阵](docs/requirements-matrix.md)
- [信用模型设计](docs/scoring-model.md)
- [仓库清理记录](docs/cleanup-record.md)
- [比赛前下一步](docs/next-steps.md)
- [比赛材料差距分析](docs/competition-material-gap-analysis.md)

## 安全边界

1. 原始合同只在本地解析。
2. 客户/供应商、金额、价格、账号、联系方式、技术参数替换为稳定令牌。
3. 外部 AI 只读取脱敏副本。
4. 输出建议恢复令牌时只在本地进行。
5. 最终审批由授权人员确认，Agent 不自动签署合同或扩大授信。
6. 密码使用 PBKDF2-SHA256 加盐摘要；连续5次失败锁定15分钟；Web写操作校验 `HttpOnly` 会话 Cookie 和 CSRF 令牌。
7. HTTPS生产部署必须设置 `DONGJIANG_COOKIE_SECURE=true`，并由反向代理提供TLS和访问控制。

## 免责声明

这是黑客松技术原型，不构成法律意见、审计意见或东江集团正式信用政策。上线前必须完成法务、财务、信息安全和数据合规评审。
