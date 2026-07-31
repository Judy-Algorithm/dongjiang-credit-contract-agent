# Cloudflare 公网部署说明

本文将应用发布到 `https://credit.1832104.xyz`。使用子域名可以保留根域名 `1832104.xyz` 的现有网站和配置。

## 当前推荐：本机部署 + Tunnel

如果暂时不使用 VPS，最简单的公网演示方式是让项目继续运行在本机，再由 Cloudflare Tunnel 建立出站连接。电脑需要持续开机、联网且不能进入睡眠；电脑关机或网络断开时，网站会暂时不可用。

本机 Windows 的服务地址是 `http://127.0.0.1:8765`。仓库提供 `scripts/run-local-cloudflare-tunnel.ps1`，直接启动本机的 Cloudflared，不需要向路由器开放端口或使用 Docker。

### 本机配置步骤

1. 在 Cloudflare Dashboard 进入 **Zero Trust → Networks → Tunnels → Create a tunnel**，选择 Docker，名称填写 `dongjiang-local`。
2. 在 Tunnel 的 **Public Hostname** 添加：
   - Subdomain：`credit`
   - Domain：`1832104.xyz`
   - Type：`HTTP`
   - URL：`http://127.0.0.1:8765`
3. 复制 Tunnel Token，并在仓库根目录执行：

```powershell
Copy-Item deploy/.env.local-tunnel.example deploy/.env.local-tunnel
notepad deploy/.env.local-tunnel
```

4. 确认本地应用正在运行：

```powershell
python -m dongjiang_agent.cli serve --host 127.0.0.1 --port 8765
```

5. 另开一个 PowerShell 窗口启动公网入口：

```powershell
.\scripts\run-local-cloudflare-tunnel.ps1
```

6. 访问 `https://credit.1832104.xyz/api/health`，应返回 `ok: true`，再打开首页完成登录。

`deploy/.env.local-tunnel` 只保存 Tunnel Token，并已加入 `.gitignore`。不要把 Token 写进脚本、截图或 GitHub。

### 配置注册验证和找回密码邮件

注册邮箱验证、注册审核结果和找回密码共用系统 SMTP 发件账号。以 163 邮箱为例，在邮箱设置中开启 SMTP 并生成客户端授权密码，然后在本机项目根目录的 `.env` 中填写：

```text
DONGJIANG_SMTP_HOST=smtp.163.com
DONGJIANG_SMTP_PORT=465
DONGJIANG_SMTP_USERNAME=你的163邮箱
DONGJIANG_SMTP_PASSWORD=邮箱生成的客户端授权密码
DONGJIANG_SMTP_FROM=你的163邮箱
DONGJIANG_SMTP_USE_SSL=true
DONGJIANG_SMTP_STARTTLS=false
DONGJIANG_PUBLIC_URL=https://credit.1832104.xyz
DONGJIANG_SLA_MONITOR_ENABLED=true
DONGJIANG_SLA_SCAN_INTERVAL_SECONDS=900
```

`DONGJIANG_SMTP_PASSWORD` 不是邮箱网页登录密码。`DONGJIANG_PUBLIC_URL` 用于生成通知邮件中的案件处理链接，应填写实际 HTTPS 域名。修改 `.env` 后必须重启本地 Python 服务，不能把授权密码提交到 Git。

时效扫描默认每900秒执行一次。也可以通过管理员“时效运营”页面手动扫描，或执行 `python -m dongjiang_agent.cli sla-sweep`。正式上线前应先根据东江制度调整 `dongjiang_agent/config/sla_policy.json`。

### Cloudflare Access

建议在 **Zero Trust → Access → Applications → Add an application → Self-hosted** 中保护 `credit.1832104.xyz`，只允许你和队员的邮箱访问。这样公网入口不会暴露给陌生人，即使应用本身还有登录页，也多一层身份验证。

GitHub Pages 不适合直接部署本项目。它只能托管静态 HTML/CSS/JavaScript，不能运行 Python Web 服务、SQLite 工作流、案件归档和模型调用；GitHub 仓库继续作为代码版本库即可。

## 1. 部署架构与前提

```text
浏览器 → Cloudflare HTTPS / Access → Cloudflare Tunnel → app:8765
                                                     ├─ data/（案件和账号）
                                                     └─ output/（报告）
```

Cloudflare 不负责运行 Python 服务或持久化案件文件。必须准备一台持续在线、已经安装 Docker 的主机：比赛演示可以使用当前 Windows 电脑；长期运行更推荐香港或新加坡的 Linux VPS。主机关机后网站会下线。

生产编排不会向主机或公网映射 `8765` 端口。应用只加入内部网络，Tunnel 同时加入内部网络和可出站网络：它可以访问应用并连接 Cloudflare，但外部无法绕过 Tunnel 直连应用。

## 2. 准备生产环境变量

在仓库根目录执行：

```powershell
New-Item -ItemType Directory -Force data, output | Out-Null
Copy-Item deploy/.env.production.example deploy/.env.production
notepad deploy/.env.production
```

Linux 主机使用 `mkdir -p data output`。请以运行 Docker 的用户创建这两个目录，并将 `deploy/.env.production` 中的 `APP_UID`、`APP_GID` 分别设为 `id -u`、`id -g` 的结果，避免容器写入时遇到权限问题。

填写文本模型、语音模型、Toolhub / Agenthub 凭证，保持：

```text
DONGJIANG_COOKIE_SECURE=true
```

`deploy/.env.production`、`.env`、案件数据、SQLite 数据库和输出报告均已被 Git 忽略。不要把任何真实密钥粘贴进 Compose、Dockerfile 或提交记录。

## 3. 在 Cloudflare 创建 Tunnel

1. 登录 Cloudflare Dashboard，进入 **Zero Trust → Networks → Tunnels**。
2. 选择 **Create a tunnel → Cloudflared**，名称可填 `dongjiang-credit-agent`。
3. 在连接器安装页选择 Docker，复制命令中 `--token` 后面的 Tunnel Token。
4. 把 Token 写入 `deploy/.env.production` 的 `CLOUDFLARE_TUNNEL_TOKEN`。
5. 在 Tunnel 的 **Public Hostname** 中添加：
   - Subdomain：`credit`
   - Domain：`1832104.xyz`
   - Type：`HTTP`
   - URL：`app:8765`

Public Hostname 会自动创建 Cloudflare DNS 记录，不需要开放路由器端口，也不要手动把域名解析到主机公网 IP。

## 4. 启动与检查

在仓库根目录执行：

```powershell
docker compose --env-file deploy/.env.production -f compose.production.yml config --quiet
docker compose --env-file deploy/.env.production -f compose.production.yml up -d --build
docker compose --env-file deploy/.env.production -f compose.production.yml ps
docker compose --env-file deploy/.env.production -f compose.production.yml logs --tail 100 app cloudflared
```

等待两个服务正常后访问：

```text
https://credit.1832104.xyz/api/health
```

预期返回：

```json
{"ok": true, "service": "dongjiang-credit-contract-agent"}
```

首次打开主页时，应用会引导创建第一个管理员账号。请使用独立的强密码，不要沿用模型 API 密钥或 Cloudflare 密码。

更新代码后重新部署：

```powershell
git pull
docker compose --env-file deploy/.env.production -f compose.production.yml up -d --build
```

停止服务：

```powershell
docker compose --env-file deploy/.env.production -f compose.production.yml down
```

`down` 不会删除主机上的 `data/` 和 `output/`。不要执行带 `-v` 的删除命令。

## 5. 添加 Cloudflare Access（强烈推荐）

应用自身具备登录、失败锁定、CSRF、HttpOnly 和 SameSite 防护，但公网演示仍建议增加外层身份验证：

1. **Zero Trust → Access → Applications → Add an application**。
2. 选择 **Self-hosted**，域名填写 `credit.1832104.xyz`。
3. 新建 Allow 策略，仅包含你和队员的邮箱，或指定的邮箱域名。
4. Session Duration 比赛期间可设为 24 小时。

Access 生效后，访问者先通过 Cloudflare 邮箱验证码或身份提供商验证，再进入应用登录页。若评委需要公开访问，可在比赛演示窗口临时加入评委邮箱，不建议把整个站点永久公开。

## 6. 数据备份与恢复

至少每天备份以下目录，并把副本放在另一台设备或对象存储中：

```text
data/auth
data/cases
data/workflow
data/archive
data/evidence
data/revisions
data/integrations
output
```

备份前可短暂停止服务以获得一致的 SQLite 快照：

```powershell
docker compose --env-file deploy/.env.production -f compose.production.yml stop app
# 使用可信的备份工具复制 data/ 和 output/
docker compose --env-file deploy/.env.production -f compose.production.yml start app
```

恢复时先停止服务，用备份替换对应目录，再启动服务。

## 7. 常见故障

- **域名显示 1033**：Tunnel 未连接。检查 `cloudflared` 日志及 Token 是否完整。
- **502 Bad Gateway**：确认 Public Hostname 的服务是 `http://app:8765`，并检查 `app` 是否 healthy。
- **登录后仍回到登录页**：生产环境必须通过 HTTPS 访问，且 `DONGJIANG_COOKIE_SECURE=true`。
- **AI 功能不可用**：检查 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL` 和 `DONGJIANG_AI_ASSISTANCE_ENABLED=true`，不要在工单或截图中暴露密钥。
- **重启后数据消失**：必须从仓库根目录启动 Compose，确认 `./data:/app/data` 与 `./output:/app/output` 挂载成功。

当前 Python 标准库 Web Server 适合比赛演示和低流量访问。若转为正式企业生产，应进一步迁移到成熟的 WSGI/ASGI 服务，并补充集中日志、并发/上传限制、监控告警和自动化异地备份。
