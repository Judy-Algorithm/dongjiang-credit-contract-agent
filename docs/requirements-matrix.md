# 命题要求与实现矩阵

更新时间：2026-07-30

## 已可演示

| 命题要求 | 当前实现 | 验证方式 |
|---|---|---|
| 信审与合同评审联动 | 合同审查强制读取 `CreditAssessment` | 示例合同同时显示信用分、额度、账期与合同结论 |
| 新/存量客户 | 新客户关闭合作维度并归一化；存量客户计入历史合作 | `test_credit_model.py` |
| TKP/TKM差异规则 | TKP超过Net 90天、TKM超过40%尾款或180天尾款账期进入市场总监特批；保留终端项目统一账期例外 | `test_contract_review.py` |
| 批量/多格式解析 | TXT/CSV/DOCX/PDF/XLSX/PNG/JPG；OCR按本机能力降级 | Web 多文件上传、`DocumentExtractor` |
| 第三方评级报告上传 | 本地报告文本提取，识别主体评级与展望 | `CreditFactExtractor` |
| 多评级归一化与冲突处理 | 机构别名、评级、展望、日期统一；保守取值、过期告警、重大冲突人工复核 | `test_multiple_agency_ratings_use_conservative_result_and_keep_conflict` |
| 缺失财务字段处理 | 缺失指标退出计算并在有效权重内归一化；同时计算资料覆盖率，单一优质维度不得直接形成有效授信 | `test_partial_financial_metrics_are_renormalized`、`test_single_favorable_dimension_requires_supplement` |
| 敏感信息脱敏与还原 | 客户/供应商、金额、价格、账号、电话、邮箱、技术参数可逆映射 | `test_security_and_workflow.py` |
| 合同完整性检查 | 主体、标的、付款、违约、IP、保密、终止、争议解决 | 风险清单 |
| 信用交叉校验 | 合同额度、账期、TKM尾款与批准政策交叉核对 | 规则 `CREDIT-*`、`TKP-*`、`TKM-*` |
| 动态额度占用 | 当前未收款与在手订单合计占用授信，合同按剩余可用额度判断 | `test_credit_control_calculates_occupied_and_available_credit` |
| 逾期锁定和特别放行 | 当前逾期超过30天锁定合同入口；有授权附件才可放行 | `test_current_overdue_requires_evidenced_special_release` |
| 特批证据归档 | 合同特批/特别放行附件持久化并记录SHA-256、审批人和时间 | `test_special_approval_requires_director_or_ceo` |
| 法律风险 | 覆盖底线表中的连带责任、排他、无责取消、质保双限、关联主体外溢、间接损失、VMI/JIT、持续供货等风险 | `contract_rules.json`、真实比赛合同样本矩阵 |
| 风险分级和审批路由 | 通过/人工复核/特批/阻断四态 | Web 结论卡片与 Trace |
| CRM/OA预留端口 | `CRMPort`、`OAPort`、Webhook 示例适配器 | `integrations/ports.py` |
| 可视化后台 | 多文件上传、案件统计、案件列表和详情回看 | `scripts/run_web.sh`、`test_web_api.py` |
| 文件证据定位 | PDF页码、DOCX段落、XLSX工作表/单元格、文本行号；风险卡片联动受控原文预览 | `test_document_locations.py`、`test_contract_risk_exposes_line_location_and_controlled_preview` |
| 合同修订闭环 | 风险逐项采用建议/人工修改/保留说明；生成Word修订稿与清洁稿；清洁稿重新审查并保留版本链 | `test_contract_revisions.py`、`test_contract_revision_generates_downloads_and_resubmits_clean_version` |
| 脱敏AI辅助审查 | 显式开启后，仅发送本地可逆脱敏合同；要求结构化JSON，模型发现独立展示且不参与制度裁决；失败自动回退规则链 | `test_contract_ai_assistant.py` |
| AI证据定位 | AI发现保留文档ID、片段ID和坐标，可在受控查看器中定位对应原文 | `test_structured_findings_are_normalized_and_located`、浏览器验收 |
| 失败回写运维 | 管理员集中查看失败OA/CRM/SAP回写；仅重试指定失败目标，沿用幂等键并持久化重试历史和安全审计 | `test_admin_can_list_and_retry_failed_writeback` |
| 安全自助注册与审核 | 注册前邮箱验证码；账号待审核停用；管理员批准/拒绝并分配角色；审核结果站内信和邮件通知 | `test_verified_registration_requires_admin_review`、注册审核页 |
| 业务通知中心 | 注册申请、审核结果、案件流转和失败回写通知；按用户隔离，支持单条/全部已读与业务跳转 | `test_notifications_are_scoped_to_recipient_and_support_read_all`、通知中心页 |
| 审批时效运营 | 节点级可配置 SLA、临期/逾期展示、去重站内信/邮件提醒、管理员瓶颈看板 | `test_sla_operations.py`、时效运营页 |
| 流程历史分析 | 按Trace重建节点时长；统计案件办结率、平均/P95时长、节点SLA达标率、趋势和责任队列；导出CSV/三工作表Excel | `test_analytics_operations.py`、管理分析页 |
| 固定演示路径 | 幂等生成资料补件、TKP 120天特批、无责取消修订三条合成案件 | `python scripts/prepare_demo_cases.py` |
| 结果可量化 | 决策回归准确率、平均/P95耗时 | `python3 scripts/evaluate.py` |
| 可审计 | 案件 Trace、政策版本、证据和规则 ID | `audit-result.json` |

## 已有接口、需真实环境联调

| 能力 | 当前边界 | 需要东江提供 |
|---|---|---|
| 泛微 OA 流程 | 已有提交端口，未绑定真实表单字段 | 测试地址、鉴权、流程 ID、字段字典、回调规范 |
| CRM 信审回写 | 已有读客户/写决策端口 | 客户主键、额度/账期字段、接口鉴权 |
| 第三方评级自动抓取 | 支持上传报告；未做生产级站点采集 | 数据授权、站点许可或企业订阅接口 |
| OCR | 有 Tesseract 路径和失败提示 | 生产 OCR 服务或内网模型 |
| LLM 增强 | 仅允许脱敏文本；规则链不依赖 LLM | 企业批准模型、网关地址、数据处理协议 |

## 比赛前仍需补强

当前已补齐TKM业务子类型、首期采购款豁免、总信用额、一年无订单自动失活、OA审批链数据、批准范围/有效期、HTTP回写适配器、原件案件级归档、内部用户权限和文件证据定位。真实企业联调仍需东江提供测试地址、鉴权、流程ID和字段字典。
5. 原 Word 合同已支持段落级格式保留和修订痕迹；后续继续补表格内局部文字修订、条款级多语言及结构化LLM抽取。
