# 配置、命令与运行

本文给出 AutoPrompt Studio 的当前运行约束和可重复操作。命令均假设 PowerShell 已位于仓库根目录；示例不依赖固定盘符。生产验收必须在目标主机、真实域名、真实存储和实际 GPU 环境执行，命令未返回成功时不得把对应检查记为通过。

## 前置条件

本地开发需要：

- Windows PowerShell、Python 3.11、Node.js 22 与 npm。
- `ffmpeg.exe` 和 `ffprobe.exe` 均可从 `Path` 找到。
- 可访问的 ComfyUI；由仓库脚本管理便携版时，通过 `AUTOPROMPT_COMFYUI_ROOT` 指定其根目录。
- 足够容纳 Django 媒体、ComfyUI 输出、模型、临时文件和备份的磁盘空间。
- SQLite 只用于单机开发。生产部署使用 PostgreSQL。

标准生产部署还需要 Docker Engine、Docker Compose、真实 HTTPS 域名、可信 TLS 反向代理、PostgreSQL 备份工具与受控备份存储。ComfyUI 作为宿主机独立进程运行，Compose 通过 `host.docker.internal:8188` 访问它。

## 配置来源

配置文件按运行方式分开管理：

| 运行方式 | 配置来源 | 用途 |
| --- | --- | --- |
| 本地 Django | `backend/.env`，模板为 `backend/.env.example` | 数据库、Django、媒体、ComfyUI、队列、Qwen、DeepSeek、装配与发行 |
| Docker Compose | 根目录 `.env`，模板为 `.env.example` | Compose 插值和生产 Django 配置 |
| 本地 Vue | `frontend/.env`，模板为 `frontend/.env.example` | API 基础路径和 mock 开关 |
| ComfyUI 能力审计工具 | `config/*.example.json` 的部署副本 | ComfyUI 启动方式、模型与节点探测 |

进程环境变量优先于 dotenv 文件。密钥只放在后端进程或组织的密钥注入系统中，不提交到版本库，不写入工单、终端共享记录或前端环境。生产配置至少需要独立的 Django 密钥、PostgreSQL 密码、真实 host、精确的 HTTPS CORS/CSRF 源、Secure Cookie 和 HSTS。远程服务密钥仅在启用相应服务时配置。

路径配置遵循以下规则：

- 本地 `COMFYUI_OUTPUT_DIR` 指向 ComfyUI 的真实输出目录。
- Docker 的 `COMFYUI_OUTPUT_HOST_DIR` 是宿主机绝对路径，容器内固定读取 `/comfyui-output`。
- `MEDIA_ROOT`、ComfyUI 输出目录、备份目录不得互相重叠。
- 同一台机器上的 API、生成 worker 与本地 Qwen 必须解析到同一个 `LOCAL_QWEN_LOCK_PATH`。

## 初始化及其副作用

本地初始化命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

`setup.ps1` 会执行以下写操作：

1. 创建 `backend/.venv` 并安装后端与工具依赖。
2. 在 `backend/.env` 不存在时从示例创建配置，并生成独立 Django 密钥；已有文件不被覆盖。
3. 应用数据库迁移。
4. 运行 `seed_data`，使系统风格和工作流模板与命令定义保持一致。
5. 从 `assets/library/manifest.json` 导入素材元数据。
6. 通过 `npm ci` 安装前端依赖。

可选参数：

```powershell
# 安装 CPU 版本地 Qwen 运行时与模型
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -WithLocalTranslationModel

# 仅用于隔离演示环境；会创建演示账号和演示项目
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -WithDemoProject
```

演示账号密码只在账号创建时输出一次，不应进入共享日志。生产环境不创建或保留演示账号，管理员使用 `manage.py createsuperuser` 或组织认可的身份流程创建。

初始化包含数据库写入。接入已有数据库时，先完成下节的备份与迁移计划核对，不直接运行初始化脚本。

## 数据库迁移前备份

`setup.ps1`、`restart.ps1` 和生产容器的 backend bootstrap 都会应用迁移。只要目标数据库含有需要保留的数据，启动这些入口前必须先生成可恢复备份。

```powershell
# 核对备份范围，不写入备份目录
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1 -DryRun

# 创建数据库、媒体和 ComfyUI 输出备份
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1

# 查看待应用迁移
Push-Location .\backend
try {
  .\.venv\Scripts\python manage.py migrate --plan
} finally {
  Pop-Location
}
```

确认备份目录中不存在 `.incomplete`，并用 `restore.ps1 -DryRun` 校验清单、文件长度和 SHA-256。数据库、媒体和 ComfyUI 输出需要同一业务时间点时，应先停止写入服务或使用组织的协调快照机制。

Compose 的 PostgreSQL 不发布宿主机端口。备份程序必须通过受控网络访问数据库，或在数据库容器内使用兼容版本的 `pg_dump` 生成可校验备份；不得为了备份临时开放公网数据库端口。未取得有效备份时不要启动会执行迁移的新 backend 容器。

## Seed 与素材导入

在已完成迁移的环境中执行：

```powershell
Push-Location .\backend
try {
  .\.venv\Scripts\python manage.py seed_data
  .\.venv\Scripts\python manage.py import_asset_library `
    --manifest ..\assets\library\manifest.json
} finally {
  Pop-Location
}
```

`seed_data` 更新系统风格和工作流模板，并把命令定义之外的系统风格设为不启用。`import_asset_library` 按 manifest 更新素材记录，要求每条素材引用的风格处于启用状态且文件可读取。仅在明确要求数据库素材集合严格跟随 manifest 时使用 `--deactivate-missing`。

`restart.ps1` 默认运行 `seed_data`，但不导入素材 manifest。素材文件或 manifest 调整后应单独执行导入命令。任一命令非零退出都表示同步未完成。

## 本地启动与停止

由脚本管理 ComfyUI 时先提供便携版根目录：

```powershell
$ComfyUIRoot = Read-Host '请输入 ComfyUI 便携版根目录'
$env:AUTOPROMPT_COMFYUI_ROOT = (Resolve-Path -LiteralPath $ComfyUIRoot).Path
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restart.ps1 -OpenBrowser
```

也可以双击 `restart-all.cmd`。启动顺序为数据库迁移、seed、装配记录恢复、ComfyUI 探测、Django API、生成 worker、装配 worker 和 Vue。脚本仅监听回环地址，运行状态写入 `.runtime/services.json`，标准输出和错误输出写入 `.runtime/logs/`。只有 `/api/v1/health/ready/` 返回成功，脚本才报告就绪。

常用选项：

- `-SkipComfyUI`：不启动本地便携版进程，使用 `COMFYUI_BASE_URL` 指向的外部管理实例；最终 readiness 仍会验证该实例。
- `-RestartComfyUI`：队列为空且健康可核对时重启 ComfyUI。
- `-ForceComfyUIRestart`：允许在队列状态不可核对或存在活动任务时中断 ComfyUI，必须与 `-RestartComfyUI` 一起使用。
- `-SkipMigrate`：仅在迁移已由受控步骤完成时使用。
- `-SkipSeedData`：仅用于定位 seed 或模板问题。
- `-ForcePortTakeover`：仅在核对端口进程确属本工作区后使用。

脚本拒绝终止未登记且无法确认归属的端口进程。不要用进程名或端口号批量结束 Python、Node 或 ComfyUI。

手工启动时使用四个终端，并确保 ComfyUI 已在 `127.0.0.1:8188` 提供服务：

```powershell
# 终端 1：API
Set-Location .\backend
.\.venv\Scripts\python manage.py runserver 127.0.0.1:8000

# 终端 2：生成 worker
Set-Location .\backend
.\.venv\Scripts\python manage.py run_generation_worker

# 终端 3：装配 worker
Set-Location .\backend
.\.venv\Scripts\python manage.py run_assembly_worker

# 终端 4：前端
Set-Location .\frontend
npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

手工运行的服务在各终端使用 `Ctrl+C` 停止。由 `restart.ps1` 启动的隐藏进程以 `.runtime/services.json` 为识别依据；计划停机时应逐项核对 PID、进程启动时间、可执行文件路径和命令行，只停止属于该工作区的 API、两个 worker、Vue 与按计划纳入停机范围的 ComfyUI。仓库没有独立的本地 stop 脚本。

## API、前端、workers 与 ComfyUI

本地默认入口：

- 前端：`http://127.0.0.1:5173/`
- API 文档：`http://127.0.0.1:8000/api/docs/`
- ComfyUI：`http://127.0.0.1:8188/`

生产任务写入数据库队列。生成 worker 负责领取任务、向 ComfyUI 提交、轮询、心跳、取消协调、结果收集和失联任务核对；同一单 GPU 配置只运行一个生成 worker。装配 worker 独立执行 FFmpeg 单集或项目装配，并通过临时文件和原子发布避免暴露不完整成片。

ComfyUI 的 `COMFYUI_BASE_URL` 默认限制在 loopback。允许非回环地址需要显式启用 `COMFYUI_ALLOW_NON_LOOPBACK`，并在网络层限制来源。API、worker 与 ComfyUI 必须对同一输出目录达成一致。重启 GPU 进程前先查询 `/queue`；无法确认队列状态时按活动任务处理。

## Qwen 与 DeepSeek

本地 Qwen 安装命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\download_local_translation_model.ps1 -Runtime cpu
```

模型、校验清单和隔离运行时位于 `.runtime/`。Qwen 翻译、创意请求与生成 worker 共享一个非阻塞资源锁；锁被占用时，生成 worker 保持任务排队，Qwen 请求返回资源忙，不允许绕过锁并发占用同一机器内存。`restart.ps1` 在模型缺失时仍可启动工作台，但本地翻译和创意能力不可用。

标准 Compose 没有把宿主机 `.runtime` 挂载到 backend 容器。选择 `local_qwen` 前必须在容器内提供匹配的模型、Python 运行时和共享锁路径，并完成独立 smoke test；否则使用显式配置的可用提供方。

DeepSeek 为可选远程通道：

- 设置 `DEEPSEEK_ENABLED=true` 时必须提供 `DEEPSEEK_API_KEY`。
- `DEEPSEEK_BASE_URL`、模型、超时、重试和 token 上限由对应环境变量控制。
- 选择 DeepSeek 会向外部服务发送完成操作所需的提示词和项目上下文。
- 远程失败会直接报告，不会静默切换到本地 Qwen。
- 标准 Compose 只把 DeepSeek 密钥注入 backend，不注入生成 worker、装配 worker 或前端；本地进程共用 `backend/.env`，应按包含密钥的后端进程统一保护。

`PROMPT_TRANSLATION_DEFAULT_PROVIDER` 和 `PROMPT_CREATIVE_DEFAULT_PROVIDER` 只接受 `local_qwen` 或 `deepseek_api`。远程同步请求还受 Gunicorn 120 秒与 Nginx 125 秒限制；提供方客户端超时不是可用的 HTTP 等待时长证明。

H3 提示词优化器是独立的可选外部通道。保持 `H3_PROMPT_OPTIMIZER_ENABLED=false` 可阻止该通道外发；启用非回环地址时需要 HTTPS、密钥和显式的非回环许可。

## 队列、装配与发行

长耗时 GPU 与 FFmpeg 工作都由持久 worker 执行，浏览器只负责创建任务和读取状态。操作要求如下：

- 通过 UI 或 API 创建、取消和重试任务，不直接修改队列表。
- `TASK_MAX_PENDING`、`TASK_MAX_ACTIVE_PER_USER` 和 `TASK_MAX_CONCURRENT` 控制容量；单 GPU 配置保持 `TASK_MAX_CONCURRENT=1`。
- 仅对具有明确终态的任务执行重试；重试创建引用同一不可变工作流的新任务记录。
- worker 启动时核对失联任务，并以持久 Prompt ID 与 ComfyUI 队列和结果接口为依据处理。
- 装配 API 返回持久 job；通过 job 状态确认成功，不以 HTTP 请求持续时间判断 FFmpeg 是否完成。
- 单镜公开副本由受管分发流程生成，并包含配置的 AI 标识和完整性信息。不要把私有 ComfyUI 路径或原始媒体路径当作发行地址。

发行候选通过 `release-bundles` 工作流管理：先创建草稿并确认精确的分镜产物与装配成片选择，再封存清单；封存会校验产物、受管公开副本、哈希和签名。标记发行需要 staff 权限，并在操作时再次实时校验。封存或发行清单引用的任务、素材和成片属于发行证据，不得通过文件系统或数据库手工删除。

## Health、readiness 与 heartbeat

| 端点 | 含义 | 成功条件 |
| --- | --- | --- |
| `/api/v1/health/`、`/api/v1/health/live/` | Django 进程存活 | API 可处理基本请求 |
| `/api/v1/health/ready/` | 接收业务流量的依赖条件 | 所有必需检查均通过；否则返回 503 |
| `/api/v1/comfyui/health/` | 登录用户查看 ComfyUI 状态 | ComfyUI 健康或可判定为降级 |
| `/healthz` | 生产前端容器存活 | Nginx 可响应 |

readiness 检查包括：数据库连接、未应用迁移、媒体目录可写与剩余空间、FFmpeg/FFprobe 和发行标识工具链、ComfyUI 输出目录、生成与装配 worker 心跳、ComfyUI CUDA 设备，以及核心工作流所需节点、模型和接口契约。实验工作流能力以独立字段报告，不应把节点可见性当作 GPU 端到端生成成功。

两个 worker 把进程心跳写入数据库，默认最大允许间隔由 `READINESS_WORKER_MAX_AGE_SECONDS` 控制。任务和装配 job 另有执行心跳，用于停滞判断与领取恢复。readiness 通过只表示依赖满足，不替代登录、上传、低成本生成、装配、发行和 TLS 端到端验收。

## 日志与运行状态

本地脚本日志位于 `.runtime/logs/`，文件名包含启动批次和服务名；服务 PID、启动时间和日志路径记录在 `.runtime/services.json`。外部管理的 ComfyUI 使用其自身控制台或服务日志。

Docker 运行状态和日志：

```powershell
docker compose ps
docker compose logs --tail 200 backend worker assembly-worker readiness frontend db
```

排障时保留时间范围、任务 ID、job ID、HTTP 状态、稳定错误码和脱敏后的依赖状态。不要收集 JWT、签名媒体 token、数据库连接密码、完整提示词、项目私有内容或外部 API Key。

## Docker、TLS 与卷

生产配置与启动：

```powershell
Copy-Item .env.example .env
# 在受控编辑器中替换所有 placeholder、域名、可信源、密钥和宿主机 ComfyUI 输出目录。
.\backend\.venv\Scripts\python .\scripts\validate_production_deployment.py
docker compose config --quiet
docker compose up --build -d
docker compose ps
```

backend bootstrap 会执行生产配置门、Django deployment check、数据库迁移、seed、素材导入和 `collectstatic`。因此现有数据库必须在 `docker compose up` 前完成备份。

Compose 包含 `db`、`backend`、`worker`、`assembly-worker`、`readiness` 和 `frontend`。应用容器使用只读根文件系统、`no-new-privileges`、空 capabilities 和受限 tmpfs。持久数据位置为：

- `postgres-data`：PostgreSQL 数据；
- `backend-media`：Django 私有媒体与受管分发产物；
- `backend-static`：可由 `collectstatic` 重建的静态文件；
- `COMFYUI_OUTPUT_HOST_DIR`：宿主机 ComfyUI 输出，只读挂载到应用容器；
- `assets/`：仓库素材，只读挂载。

停止容器：

```powershell
docker compose stop
```

需要移除容器和网络时可在完成备份后运行 `docker compose down`；该命令默认保留命名卷。不要使用 `docker compose down -v`，除非删除数据库和媒体卷就是明确、已授权且已有可恢复备份的操作目标。

前端仅发布到宿主机 `127.0.0.1:${FRONTEND_PORT}`。可信 TLS 反向代理负责公网 HTTPS，并覆盖 `Host`、`X-Forwarded-For` 和 `X-Forwarded-Proto`；只有 TLS 已完成的请求可以传递 `X-Forwarded-Proto: https`。管理入口应限制在组织 VPN 或允许列表。详细边界见 [生产部署安全](./production-deployment.md)。

## 备份与恢复

备份覆盖 SQLite 或 PostgreSQL、Django 媒体、ComfyUI 输出和可选配置。默认不包含模型权重、Python/Node 依赖、缓存或源代码。

```powershell
# 查看计划
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1 -DryRun

# 创建备份
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1

# 可选配置含密钥，备份目录必须按密钥库保护
$ProjectRoot = (Resolve-Path '.').Path
$ConfigPaths = @(
  (Join-Path $ProjectRoot '.env'),
  (Join-Path $ProjectRoot 'backend\.env'),
  (Join-Path $ProjectRoot 'config')
) | Where-Object { Test-Path -LiteralPath $_ }
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1 `
  -IncludeConfig -ConfigPath $ConfigPaths
```

恢复先做只读预演：

```powershell
$BackupPath = (Resolve-Path '.\backups\autoprompt-backup-<timestamp>-<id>').Path
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore.ps1 `
  -BackupPath $BackupPath -DryRun
```

真实恢复要求：

1. 停止 Django API、生成 worker、装配 worker 和 ComfyUI；恢复脚本不会停止进程。
2. 确认目标数据库、媒体目录、ComfyUI 输出目录和项目根目录均为预期绝对路径。
3. 显式传入 `-ConfirmRestore`。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore.ps1 `
  -BackupPath $BackupPath -ConfirmRestore
```

脚本在写入前创建强制 pre-restore 备份，保留被替换目标的同级 `.pre-restore-*` 副本，并校验清单和文件哈希。恢复完成后运行 `scripts/check.ps1`，启动服务，核对登录、项目与分镜数量、代表性媒体、下载、低成本生成、装配和发行验证；确认数据可用后再按保留策略处置副本。详细参数见 [备份、校验与恢复](./backup-restore.md)。

Docker 命名卷不能直接由本地文件路径参数覆盖。生产 Compose 应为 `postgres-data` 和 `backend-media` 配置组织认可的卷快照或导出流程，并将 ComfyUI 宿主机输出纳入同一恢复点；恢复演练必须使用与生产匹配的 PostgreSQL 版本、卷权限和磁盘容量。

## 安全检查

```powershell
# 素材完整性、部署静态约束、Django、迁移漂移、OpenAPI、测试、前端构建和备份工具测试
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1

# Python 与生产前端依赖漏洞审计；需要访问当前漏洞数据源
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\security_check.ps1
```

命令只有在全部子步骤返回零退出码时才算通过。`check.ps1` 不执行真实 GPU 长时生成，也不验证真实 TLS 证书、生产 PostgreSQL 恢复、外部提供方配额或组织网络策略，这些项目必须在目标环境单独验收。

安全基线：

- 生产环境关闭公开注册和演示账号，使用精确 host、CORS、CSRF、代理数量与限流配置。
- ComfyUI 默认只监听 loopback；非回环连接同时受应用允许开关和网络 ACL 约束。
- 上传接受大小、MIME、尺寸、媒体流和租户配额校验；原始媒体目录不直接对外发布。
- 浏览器媒体访问使用短期签名 URL；素材库原始路径在生产 Nginx 中是 internal。
- Qwen 模型下载、DeepSeek、H3 优化器和漏洞审计都具有各自的网络外连边界，按组织策略放行。
- 日志、备份和发行清单按其敏感级别控制访问、加密、保留和销毁。

## 故障排查

| 现象 | 核对项 | 处置 |
| --- | --- | --- |
| readiness 返回 503 | 响应中的 `checks` | 只处理失败项；不要把 liveness 成功当作可接流量 |
| `migrations` 失败 | `manage.py migrate --plan`、数据库连接、备份 | 先取得可恢复备份，再应用迁移 |
| `storage` 或 `comfyui_output` 失败 | 目录存在性、权限、挂载和剩余空间 | 修复目标目录或卷，不把其他目录临时映射成生产数据目录 |
| worker 心跳不新鲜 | worker 日志、进程状态、数据库连接 | 确认同类 worker 数量和配置，安全重启对应 worker |
| ComfyUI 不可用 | `/system_stats`、CUDA 设备、`/object_info`、模型枚举和输出目录 | 修复节点、模型或路径；活动队列未核对时不重启 GPU 进程 |
| 任务持续排队 | 生成 worker 心跳、队列容量、本地 Qwen 锁、ComfyUI `/queue` | 保持持久任务不变，恢复依赖后由 worker 继续领取 |
| 任务状态不确定 | Prompt ID、ComfyUI 队列与结果接口、任务 heartbeat | 使用系统恢复和 API 重试规则，不在数据库中手工重置状态 |
| Qwen 不可用或资源忙 | 模型大小与 SHA-256、运行时路径、设备配置、共享锁 | 修复运行时；锁被正常持有时等待资源释放 |
| DeepSeek 请求失败 | 启用开关、密钥、HTTPS、配额、客户端与代理超时 | 显式报告失败；不切换提供方继续提交 |
| 装配失败 | FFmpeg/FFprobe、输入流、字体、媒体权限、装配 worker 日志 | 修复依赖后通过 job 流程处理，不发布临时文件 |
| 前端容器未启动 | `readiness`、backend 和两个 worker 的容器日志 | 先恢复依赖；前端等待 readiness 健康 |
| 端口被占用 | PID、启动时间、可执行文件路径和命令行 | 只停止确认属于该工作区的进程；不默认使用强制接管 |
| 恢复预演失败 | `.incomplete`、manifest、哈希、数据库类型、绝对目标边界 | 保持目标不动，取得完整备份或修复目标配置后重新预演 |
