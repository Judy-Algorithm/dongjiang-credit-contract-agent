# 东江一体化信审与合同评审 Agent

面向火鸟黑客松「AI 智能信审与合同评审 Agent」命题的专用实现仓库：  
[https://github.com/Judy-Algorithm/dongjiang-credit-contract-agent](https://github.com/Judy-Algorithm/dongjiang-credit-contract-agent)

本项目不是通用聊天机器人，而是一条**可审计、可恢复、带人工门禁**的制造业风控工作流：

```text
信用资料解析 → 本地脱敏 → 信用评估 → 人工审批生效
→ 合同门禁 → 合同规则审查 → 信用交叉校验
→ 风险分级 → 人工/特批路由 → 报告与系统回写
```

| 项目 | 说明 |
|------|------|
| 运行时 | Python 3.11+（Docker 镜像使用 3.12） |
| 工作流 | LangGraph 1.x + SQLite Checkpointer |
| Web | 标准库 HTTP 服务，默认端口 `8765` |
| 持久化 | 本地 `data/`、`output/`（SQLite + JSON 归档） |

---

## 功能概览

- **信审**：多格式资料解析、可解释评分、TKP/TKM 政策、额度占用与逾期锁定、审批后授信生效。
- **合同**：授信生效后门禁开放；制度规则审查；Word 修订痕迹 / 清洁版；多语言脱敏翻译工作台。
- **Agent 治理**：Plan 2.0 冻结与哈希校验、节点幂等缓存、独立核验、Agent 运维与 SLA/分析报表。
- **安全**：本地 Vault 可逆脱敏；外部模型仅接收脱敏文本；PBKDF2 密码、CSRF、会话 Cookie。
- **集成**：OA / CRM / SAP 端口抽象；比赛演示可设 `DONGJIANG_INTEGRATION_MODE=mock`。

更完整的业务规则与 API 说明见文末[文档索引](#文档索引)。

---

## 仓库结构

```text
dongjiang_agent/
├── workflow/       Harness、LangGraph 图、动态计划、人工中断与恢复
├── ingestion/      PDF/DOCX/XLSX/图片解析与证据坐标
├── credit/         信用模型与评分子 Agent
├── contract/       合同审查、修订、多语言
├── security/       认证、脱敏、邮件
├── web/            REST API 与前端静态资源
├── config/         credit_policy / contract_rules / sla（JSON，可改配置不改代码）
├── operations/     SLA、分析、Benchmark、Agent 运维
└── integrations/   企业系统回写端口与 Mock

deploy/             生产与本机 Tunnel 环境变量模板（勿提交真实密钥）
compose.production.yml   生产：app + cloudflared
Dockerfile            含 Tesseract 五语 OCR 依赖
scripts/              run_web.sh、评测与演示脚本
docs/                 架构、部署补充、用户手册等
```

---

## 快速开始（本地开发）

### 1. 克隆与依赖

```bash
git clone https://github.com/Judy-Algorithm/dongjiang-credit-contract-agent.git
cd dongjiang-credit-contract-agent

python3 -m pip install -e .
# 可选：PDF/Excel/OCR 完整能力
python3 -m pip install -e '.[documents]'
```

### 2. 环境变量

```bash
cp .env.example .env
# 编辑 .env，至少配置文本模型（若需 AI 辅助则另开 DONGJIANG_AI_ASSISTANCE_ENABLED）
```

本地 HTTP 开发请保持：

```text
DONGJIANG_COOKIE_SECURE=false
```

检查配置（不打印密钥）：

```bash
python3 scripts/check_hkgai_config.py
```

### 3. 启动 Web

```bash
python3 -m dongjiang_agent.cli serve --host 127.0.0.1 --port 8765
# 或
./scripts/run_web.sh
```

浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。**首次启动**在页面创建管理员账号。

健康检查：

```bash
curl -s http://127.0.0.1:8765/api/health
# 期望：{"ok": true, "service": "dongjiang-credit-contract-agent"}
```

### 4. 测试与演示数据

```bash
python3 -m unittest discover -s tests -v
python3 scripts/prepare_demo_cases.py    # 三条固定演示路径，可重复执行
python3 scripts/evaluate.py --cases /path/to/cases.json
```

---

## 环境变量参考

| 变量 | 用途 | 本地开发 | HTTPS 公网 |
|------|------|----------|------------|
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | HKGAI 等 OpenAI 兼容文本模型 | 按需 | 必填（若开 AI） |
| `DONGJIANG_AI_ASSISTANCE_ENABLED` | 脱敏合同 AI 辅助审查 | 通常 `false` | 按需 `true` |
| `DONGJIANG_COOKIE_SECURE` | 仅 HTTPS 下发 Secure Cookie | **`false`** | **`true`** |
| `DONGJIANG_PUBLIC_URL` | 邮件/通知中的案件链接 | 可留空 | 填公网 HTTPS 根 URL |
| `DONGJIANG_ENV_FILE_OVERRIDE` | 本机 `.env` 是否覆盖已有环境变量 | 本机长期进程可 `true` | 容器生产保持 `false` |
| `DONGJIANG_INTEGRATION_MODE` | 设为 `mock` 启用比赛 Mock OA | 演示可选 | **生产必须留空** |
| `DONGJIANG_SMTP_*` | 注册验证、审核、找回密码邮件 | 可选 | 公网注册强烈建议配置 |
| `DONGJIANG_OA_*` / `CRM_*` / `SAP_*` | 企业回写 | 可空（记 not_configured） | 联调后填写 |
| `CLOUDFLARE_TUNNEL_TOKEN` | Cloudflare Tunnel | 见部署章节 | 生产 Compose 必填 |
| `HKGAI_SPEECH_*` / `TOOLHUB_*` / `AGENTHUB_*` | 语音与工具服务 | 预留 | 按接入进度填写 |

完整列表见 [.env.example](.env.example) 与 [deploy/.env.production.example](deploy/.env.production.example)。  
**切勿**将 `.env`、`deploy/.env.production`、`deploy/.env.local-tunnel` 或真实案件数据提交到 Git。

---

## 部署指南

GitHub Pages **不能**托管本项目（无 Python 后端、无 SQLite/本地归档）。公网访问请使用 **Cloudflare Tunnel**，或内网/VPN 直连应用端口。

### 部署方式怎么选

| 方式 | 适用场景 | 公网 | 主机要求 |
|------|----------|------|----------|
| [A. 纯本地](#a-纯本地开发--内网) | 开发、答辩现场局域网 | 否 | 本机 Python |
| [B. 本机 + Tunnel](#b-本机应用--cloudflare-tunnel) | 比赛演示、无 VPS | 是（Tunnel） | 本机常开、勿睡眠 |
| [C. Docker + Tunnel](#c-docker-compose--cloudflare-tunnel推荐-vps) | 稳定公网、Linux VPS | 是 | Docker + 磁盘持久化 |
| [D. 仅 Docker 本地](#d-仅-docker-本地无公网) | 与生产同镜像联调 | 否 | Docker |

推荐架构（公网）：

```text
浏览器 → Cloudflare HTTPS（可选 Access）→ cloudflared Tunnel → app:8765
                                              ├─ ./data  （案件、用户、执行缓存）
                                              └─ ./output（报告）
```

生产 Compose **不会**把 `8765` 映射到主机公网 IP；仅 Tunnel 在 `private` + `edge` 网络中访问 `app`。

---

### A. 纯本地（开发 / 内网）

1. 按[快速开始](#快速开始本地开发)安装并 `serve`。
2. 防火墙仅允许可信网段访问 `8765`（若需局域网演示）。
3. `DONGJIANG_COOKIE_SECURE=false`，`DONGJIANG_PUBLIC_URL` 可留空。

---

### B. 本机应用 + Cloudflare Tunnel

适合 Windows/macOS 笔记本直接演示：**应用跑在本机**，Tunnel 出站连接 Cloudflare，**无需**路由器端口映射。

#### B.1 在 Cloudflare 创建 Tunnel

1. [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) → **Networks → Tunnels → Create a tunnel**。
2. 连接器类型选 **Cloudflared**（本机安装 cloudflared，不必 Docker）。
3. 添加 **Public Hostname**（示例）：
   - Subdomain：`credit`
   - Domain：你的域名（如 `1832104.xyz`）
   - Service type：`HTTP`
   - URL：`http://127.0.0.1:8765`
4. 复制 Tunnel **Token**。

#### B.2 本机配置 Token

```bash
cp deploy/.env.local-tunnel.example deploy/.env.local-tunnel
# 编辑 deploy/.env.local-tunnel，填入 CLOUDFLARE_TUNNEL_TOKEN=
```

Windows 也可：

```powershell
Copy-Item deploy\.env.local-tunnel.example deploy\.env.local-tunnel
notepad deploy\.env.local-tunnel
```

#### B.3 启动应用与 Tunnel

终端 1 — 应用（项目根目录）：

```bash
python3 -m dongjiang_agent.cli serve --host 127.0.0.1 --port 8765
```

终端 2 — Tunnel：

- **Windows**：`.\scripts\run-local-cloudflare-tunnel.ps1`（会先探测本机 `/api/health`）
- **macOS / Linux**（需已安装 `cloudflared`）：

```bash
export $(grep -v '^#' deploy/.env.local-tunnel | xargs)
cloudflared tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN"
```

#### B.4 公网环境变量（本机 `.env`）

HTTPS 访问时必须：

```text
DONGJIANG_COOKIE_SECURE=true
DONGJIANG_PUBLIC_URL=https://credit.你的域名
```

注册/找回密码需配置 `DONGJIANG_SMTP_*`（163/QQ 等使用**客户端授权码**，不是登录密码）。修改 `.env` 后**重启** Python 服务。

#### B.5 可选：Cloudflare Access

Zero Trust → **Access → Applications → Self-hosted**，保护同一 hostname，仅允许队员邮箱，避免公网裸奔登录页。

更细的 Tunnel 说明见 [docs/deployment-cloudflare.md](docs/deployment-cloudflare.md)。

---

### C. Docker Compose + Cloudflare Tunnel（推荐 VPS）

适合 Linux 云主机 7×24 运行。镜像已包含 Tesseract（中/英/越/日/西）。

#### C.1 主机准备

```bash
git clone https://github.com/Judy-Algorithm/dongjiang-credit-contract-agent.git
cd dongjiang-credit-contract-agent

mkdir -p data output
# Linux：容器内 UID/GID 与目录属主一致，避免写盘权限错误
export APP_UID=$(id -u) APP_GID=$(id -g)
```

安装 [Docker Engine](https://docs.docker.com/engine/install/) 与 Compose 插件。

#### C.2 生产环境文件

```bash
cp deploy/.env.production.example deploy/.env.production
```

编辑 `deploy/.env.production`，**至少**完成：

1. `CLOUDFLARE_TUNNEL_TOKEN` — 来自 Cloudflare Tunnel（Docker 连接器页复制的 token）。
2. `OPENAI_*` — 文本模型（启用 AI 时 `DONGJIANG_AI_ASSISTANCE_ENABLED=true`）。
3. `DONGJIANG_COOKIE_SECURE=true`
4. `DONGJIANG_PUBLIC_URL=https://credit.你的域名`
5. `APP_UID` / `APP_GID` — 与 `data`、`output` 目录属主一致（Linux 必填）。
6. 按需：`DONGJIANG_SMTP_*`、OA/CRM/SAP、`HKGAI_*`。

在 Cloudflare Tunnel 的 Public Hostname 中，服务 URL 必须为 **`http://app:8765`**（Compose 服务名 `app`，不是 `127.0.0.1`）。

#### C.3 构建与启动

```bash
docker compose --env-file deploy/.env.production -f compose.production.yml config --quiet
docker compose --env-file deploy/.env.production -f compose.production.yml up -d --build
docker compose --env-file deploy/.env.production -f compose.production.yml ps
docker compose --env-file deploy/.env.production -f compose.production.yml logs --tail 100 app cloudflared
```

`app` 通过 healthcheck 就绪后，`cloudflared` 才会启动（见 `compose.production.yml`）。

#### C.4 验收

```bash
curl -s https://credit.你的域名/api/health
```

期望 JSON：`{"ok": true, "service": "dongjiang-credit-contract-agent"}`。  
浏览器打开同一域名，创建管理员并登录。

#### C.5 更新与停止

```bash
git pull
docker compose --env-file deploy/.env.production -f compose.production.yml up -d --build
```

```bash
docker compose --env-file deploy/.env.production -f compose.production.yml down
# 不要使用 down -v；data/ 与 output/ 在主机上保留
```

---

### D. 仅 Docker 本地（无公网）

不启动 `cloudflared`，仅验证生产镜像：

```bash
docker build -t dongjiang-agent:local .
docker run --rm -p 8765:8765 \
  -v "$(pwd)/data:/app/data" -v "$(pwd)/output:/app/output" \
  --env-file .env \
  dongjiang-agent:local
```

访问 [http://127.0.0.1:8765](http://127.0.0.1:8765)。本地 HTTP 时 `.env` 中 `DONGJIANG_COOKIE_SECURE=false`。

---

## 部署后验收清单

- [ ] `GET /api/health` 返回 `ok: true`
- [ ] HTTPS 站点下登录会话正常（`DONGJIANG_COOKIE_SECURE=true`）
- [ ] 已创建管理员；公开注册流程（若启用）能收邮件
- [ ] `python3 scripts/check_hkgai_config.py` 或管理端「探测文本模型」通过（若使用 AI）
- [ ] `data/`、`output/` 挂载/目录可写；重启容器后案件仍在
- [ ] Cloudflare Access（若启用）策略仅含授权身份
- [ ] 生产未设置 `DONGJIANG_INTEGRATION_MODE=mock`（除非明确演示）

---

## 数据持久化与备份

运行时写入（均在 `.gitignore` 中，**禁止提交**）：

| 路径 | 内容 |
|------|------|
| `data/auth/` | 用户、会话、审计、通知（SQLite） |
| `data/cases/`、`data/vault/` | 案件状态与脱敏映射 |
| `data/archive/`、`data/evidence/` | 原始资料与附件 |
| `data/executions/` | 节点幂等执行缓存 |
| `data/integrations/` | OA/CRM/SAP 调用记录 |
| `output/` | HTML/JSON 报告与评测结果 |

**备份**（建议每日，副本存异地）：

```bash
# Compose 环境可先停 app 再拷贝，保证 SQLite 一致
docker compose --env-file deploy/.env.production -f compose.production.yml stop app
tar czf backup-$(date +%F).tar.gz data output
docker compose --env-file deploy/.env.production -f compose.production.yml start app
```

恢复：停服务 → 用备份覆盖 `data/`、`output/` → 再启动。

---

## 运维 CLI

```bash
python3 -m dongjiang_agent.cli sla-sweep              # SLA 扫描（也可由定时任务/页面触发）
python3 -m dongjiang_agent.cli lifecycle-sweep        # 长期无订单客户授信清零策略
python3 -m dongjiang_agent.cli workflow-status --case-id DJ-XXXX
python3 -m dongjiang_agent.cli workflow-resume --case-id DJ-XXXX --decision decision.json
```

容器内将 `python3 -m dongjiang_agent.cli` 换为 `docker compose ... exec app python -m dongjiang_agent.cli`。

---

## 常见问题

| 现象 | 处理 |
|------|------|
| Cloudflare **1033** | Tunnel 未连接：检查 token、`cloudflared` 日志 |
| **502 Bad Gateway** | Public Hostname 应为 `http://app:8765`（Compose）或本机 `127.0.0.1:8765`；确认 `app` healthy |
| 登录后跳回登录页 | 必须用 HTTPS 且 `DONGJIANG_COOKIE_SECURE=true` |
| AI 不可用 | 检查 `OPENAI_*`、`DONGJIANG_AI_ASSISTANCE_ENABLED`；管理端看模型健康 |
| 重启后数据丢失 | 确认从仓库根目录启动 Compose，`./data`、`./output` 卷挂载成功 |
| OCR 失败 | 本机安装 Tesseract 与语言包，或使用官方 Dockerfile（已装五语） |

---

## 文档索引

| 文档 | 说明 |
|------|------|
| [docs/deployment-cloudflare.md](docs/deployment-cloudflare.md) | Cloudflare Tunnel / Access 逐步补充 |
| [docs/user-manual.md](docs/user-manual.md) | 业务操作说明书 |
| [docs/architecture.md](docs/architecture.md) | 架构与动态工作流 |
| [docs/langgraph-harness-design.md](docs/langgraph-harness-design.md) | Harness 与 API 边界 |
| [docs/demo-cases.md](docs/demo-cases.md) | 典型案例 |
| [docs/requirements-matrix.md](docs/requirements-matrix.md) | 命题实现矩阵 |

---

## 安全边界

1. 原始合同与信用资料在本地解析；外发 AI 仅脱敏副本。  
2. Agent **不自动**批准授信或签署合同；人工审批与特批路由由规则与角色控制。  
3. 生产必须 HTTPS + `DONGJIANG_COOKIE_SECURE=true`；建议叠加 Cloudflare Access。  
4. 密钥、Tunnel Token、SMTP 授权码、案件数据不得进入 Git 或截图。

---

## 免责声明

本仓库为黑客松技术原型，**不构成**法律意见、审计意见或东江集团正式信用政策。上线前须完成法务、财务、信息安全与数据合规评审。
