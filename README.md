# Media Asset Production

面向单人或早期团队的媒体资产与内容生产平台。系统把脚本、Bible、镜头、生成任务、托管产物、逐集母版和发布包收敛到一条可审计的生产链路。

## 当前实现

唯一的新生产入口是 \`ProductionRun\`：

1. 从已确认脚本创建，或选择 AI 脚本模式。
2. AI 模式执行“脚本生成 → 人工审核 → 确定性生成 Bible → 人工审核”。
3. 锁定 storyboard、路由、成本和工作流快照后提交生产。
4. 图片与视频任务通过已验证的语义 Comfy workflow 或 async REST T2I/T2V 执行器运行。
5. 远端结果归档为 managed artifact；每集生成一个 episode master，最后形成 \`ReleaseBundle\`。

\`tasks\`、\`deferred-generation-requests\`、\`assemblies\`、\`releases\` 都是完全只读的诊断投影。创建、重试、取消和有序 fallback 只在所属 \`ProductionRun\` 上执行。

平台还实现了：

- 语义工作流：按 input/output semantic 解析节点，不依赖固定节点 ID；工作流必须 validate，并以真实 smoke 证据发布。
- 五层配置：model default、workflow default、system revision、project revision、run override；运行创建时冻结完整来源与 hash，restart-required 变更在新 boot 显式激活。
- 主机能力探测与推荐：probe 以持久后台 attempt 入队并由 platform-maintenance worker 形成快照，Settings 可恢复、轮询和取消；recommend 生成草案，decide 时服务端重新校验主机快照、目录、配置和候选兼容性，陈旧推荐会被拒绝。
- 受管产物与 provenance：外部输出必须归档、校验并绑定生成尝试后才进入装配；\`comparison_parameters\` 在归档时从冻结运行事实白名单生成，参数差异不读取 manifest 身份/hash 字段。
- 配方发布门禁：published recipe 必须具备完整 content → storyboard → prompt → media → selection → assembly → delivery 闭包。
- 逐集交付：交付单位是 episode master；\`ReleaseBundle\` 聚合逐集母版，不生成整季单条长视频。
- 权限与凭据治理：超级管理员独占“权限管理”和受管凭据写入；平台管理员负责非敏感平台配置，普通成员只访问自己的业务数据。数据库中的敏感值使用独立密钥环执行 AES-256-GCM 加密。

## 诚实边界

- async REST 当前只接受标准 T2I/T2V 文本输入。I2V 或 reference image 需要 provider-specific 上传契约，系统会以 \`provider_upload_contract_missing\` 阻断。
- 没有跨节点或多 GPU scheduler；worker 并发是进程级配置，不代表全局资源调度。
- 系统不会自动下载模型，也不会自动激活或发布推荐配置。
- bootstrap workflow catalog 初始为 inactive draft、\`needs_review\`；必须完成节点绑定、校验和真实 smoke 才能发布。
- 仓库测试覆盖契约与模拟失败场景，但不构成真实外部 provider 或 GPU 端到端成功证据。

## 架构

\`\`\`mermaid
flowchart LR
    UI[Vue operator UI] --> API[Django REST API]
    API --> PR[ProductionRun]
    PR --> TQ[Text / media queues]
    TQ --> EX[Semantic Comfy or async REST executors]
    EX --> MA[Managed artifacts]
    MA --> EM[Episode masters]
    EM --> RB[ReleaseBundle]
    API --> DB[(PostgreSQL)]
    API --> FS[(Managed media storage)]
\`\`\`

后端为 Django REST Framework，前端为 Vue 3 + TypeScript。PostgreSQL（开发环境可用 SQLite）保存业务事实、队列状态与运行审计，worker 直接领取持久化任务行；媒体文件保存在受管存储中。详细设计见 [架构文档](docs/architecture.md)。

## 首次安装与启动

Windows PowerShell：

\`\`\`powershell
.\scripts\setup.ps1
.\scripts\restart.ps1
\`\`\`

\`setup.ps1\` 会安装依赖、生成独立的数据库加密密钥环、执行迁移、创建首个超级管理员并导入仓库内的素材库。新建账号的随机临时密码只在终端显示一次，首次登录只能查看当前账号并修改密码，完成修改后才可进入系统。workflow catalog 只由版本化 migration 初始化，不由 seed 数据或启动流程覆盖。只有显式需要演示项目时才运行：

\`\`\`powershell
Push-Location backend
.venv\Scripts\python.exe manage.py seed_data --with-demo-project
Pop-Location
\`\`\`

之后日常启动和重启统一使用 \`scripts/restart.ps1\`。仓库当前没有独立的本地停止脚本；运行手册见 [docs/operations.md](docs/operations.md)。

## 开发入口

- 前端：\`http://127.0.0.1:5173/\`
- 后端健康检查：\`http://127.0.0.1:8000/api/v1/health/\`
- 唯一机器可读 API 契约：[openapi.yaml](openapi.yaml)
- 动态 schema：\`GET /api/schema/\`，与根 \`openapi.yaml\` 同源
- 页面内生产手册：前端导航“生产手册”

根 \`openapi.yaml\` 是唯一提交到仓库的机器契约；不要维护副本。

## 运行前检查

\`\`\`powershell
.\scripts\check.ps1
Push-Location backend
.venv\Scripts\python.exe manage.py migrate --plan
.venv\Scripts\python.exe manage.py migrate --check
Pop-Location
\`\`\`

进入真实生产前还必须逐项完成受管凭据绑定、deployment、workflow validation、真实 smoke、主机探测、配置激活和备份恢复演练。敏感值写入数据库前以 AES-256-GCM 加密，普通读取只展示“已配置/未配置”；解密密钥环必须保留在数据库之外，并与数据库备份分开托管。

## 文档

- [API](docs/api.md)
- [架构](docs/architecture.md)
- [MVP 边界](docs/mvp-requirements.md)
- [运行与运维](docs/operations.md)
- [生产操作员手册](docs/production-operator-manual.md)
- [系统设置新手图文指南](docs/system-settings-beginner-guide.md)
- [生产就绪检查](docs/production-readiness.md)
- [备份与恢复](docs/backup-restore.md)
- [生产部署](docs/production-deployment.md)

## 许可

平台代码与外部模型、工作流、节点包的许可相互独立。catalog 中出现某个 adapter、模型系列或工作流模板，只表示可配置项，不表示已安装、已授权、已验证或适合商业使用。
