# 命题要求与实现矩阵

更新时间：2026-07-29

## 已可演示

| 命题要求 | 当前实现 | 验证方式 |
|---|---|---|
| 信审与合同评审联动 | 合同审查强制读取 `CreditAssessment` | 示例合同同时显示信用分、额度、账期与合同结论 |
| 新/存量客户 | 新客户关闭合作维度并归一化；存量客户计入历史合作 | `test_credit_model.py` |
| TKP/TKM差异规则 | TKP 90天；TKM 40%尾款、180天尾款账期 | `test_contract_review.py` |
| 批量/多格式解析 | TXT/CSV/DOCX/PDF/XLSX/PNG/JPG；OCR按本机能力降级 | Web 多文件上传、`DocumentExtractor` |
| 第三方评级报告上传 | 本地报告文本提取，识别主体评级与展望 | `CreditFactExtractor` |
| 多评级归一化与冲突处理 | 机构别名、评级、展望、日期统一；保守取值、过期告警、重大冲突人工复核 | `test_multiple_agency_ratings_use_conservative_result_and_keep_conflict` |
| 缺失财务字段处理 | 缺失指标退出计算并在有效权重内归一化，不按0分惩罚 | `test_partial_financial_metrics_are_renormalized` |
| 敏感信息脱敏与还原 | 客户/供应商、金额、价格、账号、电话、邮箱、技术参数可逆映射 | `test_security_and_workflow.py` |
| 合同完整性检查 | 主体、标的、付款、违约、IP、保密、终止、争议解决 | 风险清单 |
| 信用交叉校验 | 合同额度、账期、TKM尾款与批准政策交叉核对 | 规则 `CREDIT-*`、`TKP-*`、`TKM-*` |
| 法律风险 | 无限责任、单方解除、IP转让、境外管辖、高违约金 | `contract_rules.json` |
| 风险分级和审批路由 | 通过/人工复核/特批/阻断四态 | Web 结论卡片与 Trace |
| CRM/OA预留端口 | `CRMPort`、`OAPort`、Webhook 示例适配器 | `integrations/ports.py` |
| 可视化后台 | 多文件上传、案件统计、案件列表和详情回看 | `scripts/run_web.sh`、`test_web_api.py` |
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

1. 原 Word 合同保真修订、接受/拒绝建议及带修订/清洁版 DOCX 导出。
2. 英文、越南文、日文、西班牙文的条款级翻译和译文同步更新。
3. 多份附件在线切换、页码/坐标级高亮定位。
4. 使用真实公开制造企业报告构造至少 20 个验证案例，并由财务/法务双人标注。
5. 接入一个可现场演示的 OA/CRM Mock 服务，展示提交与回写闭环。
