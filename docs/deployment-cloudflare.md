# Cloudflare 公网部署说明

本文将应用发布到 `https://credit.1832104.xyz`。使用子域名可以保留根域名 `1832104.xyz` 的现有网站和配置。

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
