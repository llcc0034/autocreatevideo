# 生产部署安全

本页描述仓库内可静态验证的生产边界。它不代替真实 Docker 主机、TLS 域名、GPU、备份存储和组织网络的上线验收。

## 强制边界

- 本机 PowerShell 开发链路使用 `AUTOPROMPT_DEPLOYMENT_PROFILE=development`，只监听 `127.0.0.1`，可使用 SQLite 和 HTTP。
- 标准 `docker-compose.yml` 固定使用 `production`。Django 在导入 settings 时就会验证：`DEBUG=false`、非 placeholder 的 50+ 字符 Django 密钥、20+ 字符 PostgreSQL 密码、PostgreSQL 引擎、真实 host、精确 HTTPS CORS/CSRF 源、SSL 重定向、Secure Cookie 和至少 3,600 秒 HSTS。任一项不满足都会在迁移前退出。
- `backend`、`worker`、`assembly-worker`、`readiness` 和 `frontend` 均使用只读根文件系统、`no-new-privileges`、删除全部 Linux capabilities 及受限 `/tmp` tmpfs。Django/Gunicorn 使用 UID/GID 10001，Nginx 使用非 root `nginx` 用户和 8080 端口。
- PostgreSQL 不发布宿主机端口。Nginx 仅绑定 `127.0.0.1:${FRONTEND_PORT}`，不是 LAN/公网入口。
- API 只校验或入队持久任务：Gunicorn 超时 120 秒，Nginx 超时 125 秒。GPU 生成和 FFmpeg 装配均由 worker 持续执行，不得通过多小时 HTTP 请求执行。
- 本地 Qwen 创作草稿与逐镜扩写目前使用同步 HTTP 请求，服务端运行上限可超过标准入口的 120/125 秒。标准 Compose profile 只支持能在代理超时内完成的这类请求；长耗时输入应在本地开发入口使用，或在将其改造成持久异步任务后再纳入生产部署。不得通过放宽全部 API 的代理超时规避这一边界。
- `/assets/library/` 仅作为 Nginx `internal` 原创素材挂载，不接受浏览器直连；素材预览必须经过签名 `/api/v1/media/` 接口。Vite 的 `/assets/*.js|css` 来自前端构建目录，不会被素材挂载覆盖。Django `collectstatic` 通过独立命名卷以只读方式供 Nginx 读取。
- 后端镜像把仓库内 MiniMax H3 许可证副本复制到 `/app/licenses/minimax-h3/LICENSE-2026-08-09.txt`，并把其要求的 `NOTICE` 复制到 `/app/NOTICE`。分发对应材料的自定义镜像、离线安装包和源码发行物应保留这两个第三方许可文件。

## 外部 TLS 反向代理

Compose 内的 Nginx 不终止 TLS。公网边界必须是组织管理的 HTTPS 反向代理，它只连接宿主机回环地址 `127.0.0.1:8080`，并且：

1. 覆盖而不是追加客户端提供的 `Host`、`X-Forwarded-For` 和 `X-Forwarded-Proto`。
2. 只对已成功完成 TLS 的请求传递 `X-Forwarded-Proto: https`。
3. 把 HTTP 公网入口永久重定向到相同的 HTTPS 域名。
4. 将管理后台限制在组织 VPN/允许列表，并在边界保留访问日志。
5. 只有所有子域都已 HTTPS 化后才启用 `SECURE_HSTS_INCLUDE_SUBDOMAINS=true`；只有确认接受浏览器 preload 的长期影响后才启用 `SECURE_HSTS_PRELOAD=true`。

## 配置与启动

```powershell
# 在仓库根目录执行
Copy-Item .env.example .env
# 替换全部 example.invalid / replace-with placeholder。
# POSTGRES_PASSWORD 使用 URL-safe 随机字符，因为它会进入 DATABASE_URL userinfo。
.\backend\.venv\Scripts\python .\scripts\validate_production_deployment.py
# 只校验，不把插值后的密钥打印到终端或 CI 日志
docker compose config --quiet
docker compose up --build -d
docker compose ps
```

健康验收：

- `backend` 容器验证 `/api/v1/health/live/`。
- `readiness` 容器验证 `/api/v1/health/ready/`，其中包含数据库、迁移、存储、ComfyUI、生成 worker 和装配 worker。
- `frontend` 容器验证 `/healthz`，并在 readiness 通过后才启动。
- 由真实 HTTPS 域名验证首页、`/assets/*.js`、`/api/v1/health/ready/`、登录、上传和一次低成本生成；同时确认原始 `/assets/library/...` 直连返回 404，而素材 API 签名预览可用。不要把容器内 HTTP 成功误当成 TLS 验收。

## 本机无法完成的验收

当验收机未安装 Docker 时，`scripts/validate_production_deployment.py` 可验证 Compose 环境传递、容器权限、非 root Dockerfile、健康检查、Nginx 路由/超时/TLS 传递和 env profile；`settings` 专项测试验证 production 配置。以下项必须在真实环境完成：

- `docker compose config --quiet`、完整镜像构建、非 root 命名卷写入、容器重启和健康转换。
- 真实证书链、HTTP 重定向、Secure Cookie、HSTS 和代理头覆盖。
- PostgreSQL 备份/恢复演练、容量告警和密钥轮换。
- 真实 GPU/ComfyUI 长时稳定性、worker 中断恢复和 FFmpeg 装配验收。
