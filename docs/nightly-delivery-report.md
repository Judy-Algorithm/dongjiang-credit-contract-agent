# 交付前夜间检查报告

检查日期：2026-08-03

检查分支：`codex/five-role-dynamic-agents`
基线提交：`9f0b7c8 feat: simplify account access and workflow handoffs`

## 交付结论

本地确定性规则闭环、五角色权限、信审生效门禁、合同联动、人工复核/特批、原文证据定位和报告导出均可运行。项目可用于本地黑客松演示。

尚未完成的部分均属于外部配置或真实企业系统联调，不应在答辩中宣称已上线。

## 本轮已修复

1. 五角色回归测试对齐真实权限：系统管理员不再在测试中代替业务经办人、信用审批人、合同法务或授权审批人执行业务动作。
2. 结构化字段提取测试使用达到质量门禁的可定位资料，不再绕过原文证据要求。
3. 新增 DOCX/XLSX 解压安全门禁：限制 OOXML 内部成员数、单成员大小和总解压大小，防止恶意文档解析耗尽资源。
4. Web JSON 请求拒绝负数 `Content-Length`。
5. 升级文档处理依赖到 `pypdf >= 6.14.2` 和 `Pillow >= 12.3`，清除旧主版本的已知漏洞。
6. 修复知识产权标准修订文本与规则解析器不一致的闭环缺陷：采用系统建议后可通过同一规则的复审，不会重复命中同一风险。
7. 配置检查脚本区分文本模型核心必需项与语音/Toolhub/Agenthub 可选项，避免误报“整个系统不可用”。
8. README 已对齐五角色模型、Mock OA 审批角色和通用安装路径。
9. 增加不会自动注入页面的合成测试资料，可按真实用户顺序手动跑通信审与合同联动。
10. 统一 README、架构文档、演示案例和用户手册中的五角色名称，明确系统管理员不参与业务审批。

## 验证证据

- Python 编译：通过。
- 依赖一致性：`pip check` 通过。
- 已知依赖漏洞：`pip-audit` 未发现已知漏洞（项目自身为本地包，不在 PyPI 审计）。
- 自动化回归：非 Web 149 项、Web/API 35 项，共 184 项通过，0 失败、0 错误、0 跳过。
- 敏感信息扫描：受版本控制文件中无 `.env`、私钥、数据库、运行数据或已知比赛密钥。
- 本地启动：`/api/health`、首页和前端 JS 资源返回正常。
- 湖南钢铁 2025 年报：成功解析 187 页，约 17 万字符。
- 天山材料 2025 年报：成功解析 18 页，约 9700 字符。
- 合成通过路径：信审评分 89.05 / 低风险 → 信用审批生效 → 合同自读取授信 → `Pass`。
- 既有系统测试包已覆盖：标准 TKP、超额度 TKP、31 天逾期锁定与特别放行、法务复核、标准 TKM、TKM 多例外、未生效授信上传合同阻断、越权审批阻断、无证据特批阻断。

## 提交后必须确认的外部项

### 1. 文本模型凭证

当前新工作树没有 `.env`。旧工作树虽有 `.env`，但模型 ID 仍是 `replace-with-model-id`，接口探测返回 HTTP 401，不能作为有效配置复制。

需用有效凭证填写：

```text
OPENAI_BASE_URL=https://test-new-api.hkchat.app/v1
OPENAI_API_KEY=<有效文本模型密钥>
OPENAI_MODEL=</models 返回的真实模型 ID>
DONGJIANG_AI_ASSISTANCE_ENABLED=true
```

然后运行：

```bash
.venv/bin/python scripts/check_hkgai_config.py
```

在没有有效模型配置时，系统会使用本地规则和本地文档解析，不会伪造 AI 成功。

### 2. 公网地址

`https://credit.1832104.xyz/api/health` 当前返回 Cloudflare 530。本机服务正常，但没有运行 `cloudflared`，两个工作树也都没有本地 Tunnel Token 文件。恢复公网地址需要从 Cloudflare Zero Trust 重新取得 Tunnel Token。

### 3. 本机 OCR 语言包

本机 Tesseract 当前只有 `eng/osd/snum`。原生文本 PDF 不受影响，但纯中文扫描页会降级。Docker 部署说明已要求五语包；Mac 本地可在网络正常后安装 `tesseract-lang`。今晚尝试从 Tesseract 官方仓库下载语言数据时，`raw.githubusercontent.com` 连接失败；已删除空目录，不会破坏现有英文 OCR。

### 4. OA / CRM / SAP

真实回写接口仍未配置。黑客松演示可在 `.env` 设置：

```text
DONGJIANG_INTEGRATION_MODE=mock
```

然后由“信用审批人”执行 Mock OA 审批。展示时必须明确说明这是幂等企业系统适配演示，不是已与东江真实 OA/CRM/SAP 完成联调。

## 本地启动命令

```bash
cd /Users/zhuzhu/Desktop/黑客松agent/dongjiang-credit-contract-agent-five-role-dynamic-agents
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[documents]"
.venv/bin/python -m dongjiang_agent.cli serve --host 127.0.0.1 --port 8765
```

若 8765 已占用，先查看当前服务是否就是本项目，或临时换用 8767，不要盲目终止未知进程。

## 3–5 分钟演示顺序

1. 系统管理员登录，简短展示五角色账号，切换到业务经办人。
2. 发起信审，填写客户与本次交易条件，上传 `test-fixtures/synthetic-credit-profile.txt`。
3. 展示财务指标、多机构评级、风险等级、建议额度和账期；强调此时仍未生效。
4. 切换到信用审批人，批准授信；展示合同入口只在此后开放。
5. 切回业务经办人，上传 `test-fixtures/standard-tkp-contract.txt`。
6. 展示合同 Agent 自读取正式授信，交叉校验合同金额、可用额度、60 天账期和法律条款，最终 `Pass`。
7. 用已有超额度或底线条款案件快速展示 `Special Approval` 或 `Block`，最后打开审计报告。

## 提交前命令

```bash
git diff --check
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m pip check
.venv/bin/python -m pip_audit --progress-spinner off
git status --short
```

提交和打包时不要包含 `.env`、`data/`、`output/`、客户原始资料或任何 API/Tunnel 密钥。
