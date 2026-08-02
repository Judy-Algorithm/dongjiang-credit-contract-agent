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
启动时会自动加载它，且默认不会覆盖部署环境中已经设置的变量。本机长期运行进程若残留旧变量，可显式设置 `DONGJIANG_ENV_FILE_OVERRIDE=true` 让项目 `.env` 优先；容器和正式生产环境保持 `false`。

```text
OPENAI_BASE_URL      HKGAI文本模型OpenAI兼容地址
OPENAI_API_KEY       文本模型API密钥
OPENAI_MODEL         /v1/models返回的模型ID
DONGJIANG_AI_ASSISTANCE_ENABLED  是否启用脱敏合同AI辅助审查（true/false）
DONGJIANG_ORCHESTRATOR_LLM_PLANNING_ENABLED  是否启用主Agent的大模型计划提议层
DONGJIANG_DOCUMENT_TEXT_ENHANCEMENT_ENABLED  是否对低质量已提取文字启用文本模型字段增强
HKGAI_SPEECH_*       语音服务地址和API密钥
HKGAI_TOOLHUB_*      Toolhub服务地址
HKGAI_AGENTHUB_*     Agenthub搜索服务地址
HKGAI_APP_NAME       Toolhub/Agenthub共用App-Name
HKGAI_APP_KEY        Toolhub/Agenthub共用App-Key
```

当前代码已具备文本模型网关，并新增与服务商无关的 `AgentRegistry` 和
`ToolRegistry` 边界：主流程通过本地注册中心发现和调用信用、合同两个子 Agent，
文档解析、信用分析、合同规则和合同 AI 辅助能力通过本地工具目录调用。运行记录会保存
Agent分配来源和工具调用摘要，但不保存资料正文。拿到官方 Agenthub/Toolhub API 文档后，
只需新增远程适配器并切换provider；现有业务节点、受控动态计划和本地降级路径无需重写。
语音能力仍只预留环境变量，尚未进入业务工作流。

合同AI辅助审查默认关闭。设置 `DONGJIANG_AI_ASSISTANCE_ENABLED=true` 后，
系统会把已经本地可逆脱敏的合同文本发送给配置的文本模型，并要求返回结构化JSON。
模型发现与制度规则分栏显示，模型调用失败时自动回退到现有规则链，且不会改变审批结论。

管理员可在“Agent运维”查看文本模型最近状态、调用耗时、成功率和失败类型，并点击“探测文本模型”主动检查 `/models`。健康记录只保存模型 ID、状态、耗时、HTTP 状态和错误类型，不保存 API 密钥、提示词或资料正文。

案件“Agent运行”页支持对信用资料或合同生成 AI 结构化字段候选。每个候选必须通过字段白名单、Schema、置信度和 `document_id + fragment_id` 证据定位核验；只有信用、财务、法务或管理员人工采纳后才会写入业务字段并重新生成冻结计划。生成、拒绝或核验失败均不改变正式字段，采纳也不会自动批准授信或合同。

可以执行以下命令检查配置；输出只包含布尔状态和模型ID，不会显示密钥：

```bash
python3 scripts/check_hkgai_config.py
```

## LangGraph + Harness 工作流

`DongjiangWorkflowHarness` 是CRM、泛微OA、Web和CLI调用的稳定边界，负责案件号、角色权限、信用资料与合同的分阶段暂存、Checkpoint和恢复；LangGraph负责信审/合同子图、条件路由、循环和人工中断。

动态计划采用Plan 2.0执行治理：计划在运行前冻结并校验SHA-256规范摘要，同时快照信用规则、合同规则、模型和提示词版本；节点使用持久幂等键避免重复调用，合同AI失败最多重试一次并回退到制度规则，证据定位不足的发现自动降级剔除。独立核验会审计缺失、越权、重复和证据违规任务，偏差会强制补件或人工复核。前端“Agent运行”页可查看上述状态，但只显示脱敏摘要和截断哈希。

主 Agent 另有案件级动态计划，负责描述资料处理、两个子 Agent 分配、人工审批、
合同收集、结果汇总、企业回写和归档之间的依赖。默认由确定性规划器生成；设置
`DONGJIANG_ORCHESTRATOR_LLM_PLANNING_ENABLED=true` 后，文本模型可以从案件任务白名单
提出计划，但不能删除必需审批、回写和归档任务。模型失败、越权、缺少必需任务或形成
循环依赖时，系统记录降级原因并自动使用确定性计划，不阻断案件。

项目方当前只提供文本模型，因此文档兜底采用“本地解析质量门禁 + 文本增强”，不是
多模态识图：Word/PDF/XLSX结构、扫描页OCR和证据坐标仍由本地工具产生。只有本地已经
提取出文字但质量较低时，系统才可把脱敏文字交给文本模型补充结构化字段，且每个字段
必须重新定位到原始片段。图片或扫描PDF完全没有OCR文字时，文本模型无法继续处理，
系统会明确标记为需人工检查，不会伪造解析结果。

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
- `POST /api/cases/{case_id}/structured-extractions`
- `POST /api/cases/{case_id}/structured-extraction-actions`
- `POST /api/cases/{case_id}/mock-enterprise-approval`（管理员、仅 Mock 模式）
- `POST /api/cases/{case_id}/revisions`
- `GET /api/cases/{case_id}/revisions/{revision_id}/redline|clean`
- `POST /api/cases/{case_id}/revisions/{revision_id}/submit`
- `GET /api/operations/writebacks`（管理员）
- `GET /api/operations/agents`（管理员，跨案件Agent执行健康）
- `POST /api/operations/model/probe`（管理员，文本模型主动探测）
- `GET /api/operations/analytics?days=30`（管理员）
- `GET /api/operations/analytics/export.csv|xlsx?days=30`（管理员）
- `POST /api/cases/{case_id}/writeback-retries`（管理员，仅重试失败目标）

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
- `dongjiang_agent/config/sla_policy.json`

## 文件支持

- 原生：TXT、Markdown、CSV、JSON、HTML。
- OOXML：DOCX、XLSX。
- PDF：逐页优先提取原生文本；无文本页由 `pypdfium2` 渲染并调用 Tesseract OCR，证据保留页码、OCR 标记和平均置信度；不可用时再尝试本机 `pdftotext` 并给出明确降级提示。
- 图片：检测到本机 Tesseract 时执行 OCR。默认支持中、英、越、日、西语，可用 `DONGJIANG_OCR_LANGUAGES` 调整；缺少引擎或语言包时不会伪造解析成功。
- DOCX 表格：按表格号、行、列解析单元格，记录横向/纵向合并跨度，风险证据可定位到具体单元格；表格内整条款可生成保留原表格结构的清洁稿与 Word 修订痕迹稿。

Web页面包含登录、邮箱验证码注册申请、邮箱找回密码、注册审核、通知中心、时效运营、Agent运维、管理分析、我的待办、案件列表、发起信审、七页签案件工作台、案件处理、用户管理和安全审计。首次启动需在页面创建管理员；公开注册必须先验证邮箱，账号创建后处于待审核和停用状态，由管理员在“注册审核”页批准、拒绝并分配角色，审核结果通过站内通知和邮件送达。通知中心还接收案件流转、临期/逾期、Agent异常和失败回写提醒，点击后可进入对应业务页面。案件列表和详情显示当前待办截止时间，管理员可在“时效运营”页查看逾期分布和节点积压，在“Agent运维”页集中查看计划完整性、执行偏差、节点失败/降级、重试和缓存复用，并对异常执行确认、分派、受控候选重跑和关闭，在“管理分析”页查看案件办结率、节点SLA达标率、趋势和责任队列积压，并下载CSV或三工作表Excel报表。管理员可以补录邮箱、调整角色、停用账号或重置密码。销售、信用管理、财务、法务、市场总监、集团管理层和管理员按照真实登录身份执行工作流权限。新案件自动归属发起销售，管理员可以改派负责人。新解析文件支持 PDF 页码、DOCX 段落、XLSX 工作表/单元格和文本行号定位，风险卡片可在受控查看器中打开对应原文。信用资料和合同使用不同入口，合同只能在授信审批生效后上传。上传限制为单文件15MB、单次请求30MB。

合同处于“等待修改”时，销售可以逐项采用标准建议、人工修改或保留并填写理由。系统不会覆盖原合同，而是生成带 Word 修订痕迹版和清洁版；清洁版可以直接重新送入既有规则审查，修订决策、文件摘要和复审结果按案件保留。当前自动修订支持具有行号、段落号或 Word 表格单元格坐标的 TXT、Markdown 和 DOCX；PDF 及需要任意 run 级局部格式修改的合同保留人工上传新版本通道。

案件“合同审查”页提供多语言工作台，支持中文、英文、越南语、日语和西班牙语。系统只将带稳定片段 ID 的脱敏文本发送给已配置的文本模型，严格校验片段数量和脱敏令牌；译稿逐段绑定原文，销售、财务、法务或管理员必须逐段核对、填写复核说明后，才能生成并下载案件级双语 Word。翻译不覆盖原合同、不改变风险结论，也不参与自动批准。

AI 辅助发现引用到原文片段时，可点击证据位置，在受控查看器中打开相应页码、段落、单元格或文本行；模型结果仍不参与制度规则裁决。管理员导航中的“回写运维”集中显示 OA、CRM 和 SAP 的失败回写。重试只调用失败的目标系统，沿用原案件和阶段的幂等键，不会再次调用已经成功的系统；每次尝试写入案件 Trace、安全审计和集成调用历史。

Agent异常处置采用独立运维闭环。只有分析阶段白名单节点允许“候选重跑”；评分、业务决策、独立核验、审批和回写节点禁止直接重跑。候选任务在案件状态的隔离副本中重新执行分析、汇总和独立核验，结果仅作为异常处置证据，不覆盖正式授信或合同结论，也不改变审批状态和业务待办。处置动作写入案件 Trace 和安全审计；运维接口不会返回处理备注历史、节点原始输入输出、合同正文、完整哈希或密钥。

后台会按照受控异常指纹自动发现计划完整性失败、执行/独立核验偏差、节点失败和连续分析降级，并为新的异常事实自动开单。同一异常指纹只开一次；关闭后如果异常事实没有变化，不会重复开单。比赛建议时效为严重异常2小时响应、24小时解决，需要关注异常4小时响应、48小时解决，配置位于 `dongjiang_agent/config/agent_operations_policy.json`，标记为比赛建议而非东江正式制度。管理员可在“Agent运维”点击“立即扫描”，后台也沿用15分钟运营扫描周期生成去重提醒。

比赛演示可在 `.env` 设置 `DONGJIANG_INTEGRATION_MODE=mock`。管理员随后可在等待信用审批的案件“系统回写”页点击“运行 Mock OA 审批”，系统会生成带 `mock: true` 标记的完整 OA 审批链、批准范围、有效期和证据 ID，并沿用正式工作流激活授信、执行 OA/CRM/SAP 幂等回写。Mock 记录保存到已忽略的 `data/mock-enterprise/`，只含允许字段摘要；它证明端到端编排可运行，不代表泛微 OA、真实 CRM 或 SAP 已完成联调。生产环境必须清空该模式并配置真实接口。

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
- `data/auth/auth.sqlite`：本地用户、密码摘要、会话、安全审计、注册验证码摘要和站内通知；不保存明文密码、明文验证码或会话令牌。
- `data/revisions/DJ-*/REV-*`：合同修订决策、带修订痕迹版、清洁版和重新审查结果。
- `data/executions/DJ-*/PLAN-*/*.json`：按冻结计划和幂等键保存的节点执行结果，用于重启后安全复用。
- `output/DJ-*/audit-result.json`：机器可读结果。
- `output/DJ-*/audit-report.html`：人工审阅报告。
- `output/evaluation-report.json`：量化回归结果。

`data/cases`、`data/vault`、`data/auth`、`data/executions` 和 `output` 已加入 `.gitignore`，不得提交真实企业数据、身份数据或节点执行结果。

## 公网部署

项目提供基于 Docker Compose 与 Cloudflare Tunnel 的部署配置，推荐使用 `credit.1832104.xyz`，不需要向公网开放应用端口。完整的 Tunnel、Cloudflare Access、启动、备份和故障排查步骤见 [Cloudflare 公网部署说明](docs/deployment-cloudflare.md)。

暂不使用 VPS 时，可直接采用“本机应用 + Cloudflare Tunnel”模式；运行 `scripts/run-local-cloudflare-tunnel.ps1` 即可把本机 `127.0.0.1:8765` 映射到自定义域名。GitHub Pages 仅适合静态站点，不能替代本项目的 Python 后端和本地数据存储。

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
