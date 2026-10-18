# Operations

本文描述当前运行与运维合同，日常服务管理只使用`scripts/restart.ps1`，Schema只通过Django迁移。当前状态和剩余验收见[统一工作包](optimization/records/execution-unification/2026-09-25.md#current-work-packages)，本文不维护第二份切换进度。

## 当前运行边界

- 统一实例以及后续系统资源策略已有实际切换记录，最近记录见计划25.202；不能继续按“运行库0144、源码0169、R7尚未切换”操作。每次维护前重新核对实际Schema、配置修订、在途/未知任务，不根据文档日期猜测现场状态。
- 不兼容对象清理是明确范围的一次性维护，不是日常业务入口。旧0144切换参数及已执行删除仅在计划第21、25节追溯，不在当前环境重放；新清理须重新预览、核定摘要与依赖、备份及确认。
- 已批准的开发旧Run/配方限定清理不以模拟费用结算为前提；未知实付与原预留值不在预览中改写。取消/退役状态、在途或结果未知调用、共享引用、准备领取、分发状态及确切范围摘要仍须核对，不能据此扩大删除对象。
- 新建项目不选择默认视频输入模式；配方使用冻结的执行实例，无配方在具体生成动作中选择。没有视频实例仍可管理项目内容，不代表已经具备生成准入。
- 字段/名称翻译沿项目原审核面板创建精确实例的异步请求，在“查看翻译任务”恢复准备并明确采用。保存Bible不自动外呼；不确定结果先人工核对，不重试为成功。
- 预算、调用次数、平台输出额度只在当前系统资源策略配置。超额保留任务等待确认；确认不代替外发授权，不改写冻结请求，也不自动派发或重跑。业务成本与未知费用继续保留真实来源。

### 媒体恢复与领取安全

项目工作台“查看分发副本 / 恢复处理”查看原任务的本地后处理；修复存储、字体、媒体配置等原因后选择“仅重新处理副本”，不要为副本失败重新生成视频。原generation worker处理，不启动第二个交付worker。`runtime.task_heartbeat_seconds`须小于`runtime.distribution_lock_stale_seconds`，以最近心跳判断陈旧，不以长转码耗时判定。中断明确失败，不自动重排；已发布副本损坏不静默覆盖，下载/封存仍核验真实字节。

Comfy任务必须关联唯一主调用和Preparation。上传恢复核验逐使用回执与原文件；取消、领取变更或时限到期后不继续上传/写回。恢复只查询原提交身份，不因远端队列为空就清除未知状态，不向新地址重放。迟到回执不能接管新领取；未确认取消保持取消中。远端成功但产物校验失败仍是平台失败，不伪装可采用结果。

原任务详情中的“执行检查记录”按需读取节点检查/资源释放事实，不按轮询制造记录。记录随原任务核定闭包清理，未启用释放不伪造通过；清理后仅可从相应备份恢复。节点时限仍见[监测合同](execution-monitoring-contract.md)，不通过心跳延长总时限。

### 内容、装配与封存

0234为AssemblyJob新增可空的`consumer_stage`和成对的`consumer_input_sha256`：新人工/配方装配在创建时保存真实消费步骤与输入版本，历史无记录保持空，不能从当前任务链接或素材生成Stage回填。升级不删业务记录或文件；回退仅删除新增字段，若已有新任务会失去该追溯信息，必须先备份并按实际对象核对，不能把隔离空字段回退测试当作现用数据可无损回退。现用应用情况只认详细设计18节检查点。

人工装配响应未知时，保留原请求键及正文，并保护弹窗关闭、路由离开和父级刷新；浏览器关闭/刷新给出未决提醒（不提供离页后的持久草稿恢复）。重新读取任务只读当前事实，不等于确认命令回执，读取失败保留并标示旧内容、禁止新意图。首次请求明确拒绝可以解除该未执行意图；一旦结果未知，后续鉴权、权限或输入拒绝不能否定此前执行，只有原请求确定回执才解除未决状态。重装仍只走原装配队列，不重新调用媒体模型。

人工制作沿原ProductionRun明确审核剧本、Bible、参考主图、分镜与媒体。候选成功、保存准备和复制文件不等于批准；改选依据、重复请求和未知响应按原动作回执与影响复核处理。当前已知步骤状态/任务可见性缺陷见OPT-MANUAL-UX-001，不能靠重新生成修正显示。

装配只读明确采用并校验的受管来源，不自动取最新生成结果；首次装配冻结来源、顺序与配置。封存要求真实ProductionRun和当前清单合同。9月16日成片记录不是本轮最终效果或ReleaseBundle验收，仍须用户明确确认。

## 1. 前置条件

目标机器需要能够运行项目内安装/解析的 Python、Node.js、FFmpeg 和 PostgreSQL。ComfyUI 或 async REST provider 是可选执行器，但未完成 provider、deployment、workflow 和 smoke 配置时不能承担生产任务。

运行期业务配置保存在数据库的版本化 ConfigRevision 中。provider 密钥等敏感值由超级管理员写入 SecretBinding，材料以 AES-256-GCM 密文和不可变 SecretVersion 保存；API、页面、日志与审计正文只展示 configured/usable 状态。数据库解密密钥环必须放在部署 secret manager 或受保护环境文件中，不能写入数据库，也不能与数据库备份打包在一起。

本机默认的 `development`、SQLite、localhost HTTP 只用于开发，不构成生产安全基线。生产主机必须显式设置 `AUTOPROMPT_DEPLOYMENT_PROFILE=production`；启动会 fail closed 校验 `DJANGO_DEBUG=false`、不少于 50 字符且非占位的 Django secret、独立且不少于 32 UTF-8 bytes 的 JWT signing key、PostgreSQL 及不少于 20 字符的非占位密码、真实且无通配符的 allowed hosts、与主机匹配的 HTTPS CORS/CSRF origins、SSL redirect、secure cookies 和至少 3600 秒 HSTS。基础服务健康只说明进程与基础设施可用；Provider/Deployment/Workflow/Smoke/Recipe 任一能力证据不完整时，生成 Readiness 仍应 blocked。

建议阈值与安全硬门必须分开处理：输入/输出建议量、建议字数和提示词通道预警只记录 warning，交给人工评估，不阻断也不消费失败重试；权限、受管秘密、真实容量、合同结构、目标 hash、新鲜 Smoke、外发授权、媒体探测和不确定提交仍需独立核验；业务预算超额进入人工确认，生产文本截断保留响应待人工处理，不能为了让主流程显示成功而降级。

执行准备的字段正文与“本镜字段一键确认并采用”/多镜复核中，质量建议统一标为“内容提醒（不阻断采用）”。正文保留原值，不自动截断、扩写或选用新修订；人工模式按明确确认操作，自动策略按冻结配置记录policy批准，不冒充人审。字段保存的硬错误保留本页草稿与原采用，须处理具体原因后重读；文件缺失、真实接口容量或映射错误不会因质量提醒可忽略而被绕过。字段采用不等于请求已提交或媒体成功。

## 2. 首次安装

在仓库根目录运行：

```powershell
.\scripts\setup.ps1
```

setup 安装/修复项目运行时、安装后端和前端依赖、生成独立数据库加密密钥环、执行数据库迁移、创建首个超级管理员并初始化系统风格。默认不导入素材、不创建演示项目或执行历史。只有显式传入 `-ImportAssetLibrary` 才导入版本管理的素材清单，`-WithDemoProject` 才创建演示账号和项目；不会在日常重启时重新导入已经删除的素材。首次创建管理员时终端只显示一次随机临时密码；该账号首次登录必须先改密。workflow catalog 不由 seed 数据或启动流程覆盖。

新机器使用干净源码、独立环境文件和空数据库，运行 `scripts/setup.ps1 -Fresh`。该参数在迁移后、账号/风格初始化前只读检查业务历史，发现历史则拒绝继续，不删除旧库；不能与 `-WithDemoProject` 同用。不要复制旧 `.env`、SQLite及其日志、数据库卷、`backend/media`、`.runtime`、`.artifacts`、`backups`、日志或 ComfyUI 输出。已有素材可在新安装中独立上传/显式导入，资产复制不等于配置或生成历史迁移；新机器的执行目标、路径、凭据和能力证据需按实际环境配置。

需要本地演示项目时必须显式选择：

```powershell
.\scripts\setup.ps1 -WithDemoProject
```

或：

```powershell
Push-Location backend
.venv\Scripts\python.exe manage.py seed_data --with-demo-project
Pop-Location
```

不要在生产启动流程中运行 demo seed。可选本地翻译模型也必须通过 setup 参数或专用安装脚本显式安装；这与生产媒体模型部署是不同职责。

视觉风格的日常增删改查通过“风格素材库→管理视觉风格”完成。`seed_data` 只补充缺失预置，不覆盖已有说明、视觉提示词或启停状态，也不按内置清单停用额外共享风格。0174 migration 精确修正原始模板简介并保护已引用风格，迁移不会调用模型或修改已冻结请求。

题材选项在同一弹窗的“管理题材分类”维护，管理员可新增、改名、编辑说明/顺序、启停和删除无引用项；无需改代码或重启。0175迁移初始化目录并将原分类关联到目录，不修改风格提示词。迁移不可逆，回退使用现有备份恢复流程；未知旧代码会明确阻断，必须先核实。`seed_data` 不恢复已删除/停用的目录项，若待补预置需要的分类已不可用则事务失败，不部分初始化。分类只是辅助归类，不是运行配置，也不触发模型调用。

## 3. Migration 流程

### 业务历史清理与素材批量删除

- 超级管理员进入“系统设置 → 凭据与审计 → 业务历史清理”，先选范围，再点“暂停后台检查并预览清理”。默认删除所有用户的项目、Bible/分镜、剧本修订、Run/阶段/准备/生成任务、审核、装配/发行、Smoke及平台审计历史；保留账号、凭据、系统配置、字段/模型/部署/工作流/执行实例/策略/配方和素材库。默认不清理系统维护历史、不等待或暂停主机探测。
- “同时清理系统维护历史”默认不勾选。页面直接列出四类：主机探测任务及进度/结果；CPU、内存、GPU、磁盘、FFmpeg/ComfyUI能力快照；模型文件存在性、大小、校验摘要等可用性观测；系统级模型/工作流推荐快照、候选和采纳/覆盖/拒绝记录。项目相关的推荐仍随业务清理。勾选后等待已有探测结束，清除模型文件的核验引用与状态，下次能力判断需重新探测；不删主机/模型/部署配置或模型文件。
- 确认前先按[备份恢复手册](backup-restore.md)备份。输入“清空所选历史并保留素材”后才提交；服务器锁内复核所选范围、预览摘要、在途任务、未知提交和保留配置的真实引用。范围变化返回409且无删除；有范围内活动执行或配置直接引用历史对象时，先在原入口结束/解决任务或处理具体配置，不能靠清理伪造远端取消。响应未知先读取列表与任务状态核对，不盲重发。
- 后台对受阻准备的例行检查仅推进下次检查时间或短暂领取，不会使清理预览范围反复失效。提交时仍重新检查当前领取与执行状态；检查尚未释放领取权时暂不清理，业务内容、配置或记录成员真正改变时须重新预览。
- 预览窗口暂停新的准备自动检查5分钟；仅勾选系统维护历史时暂停新增定时主机探测，已经领取/排队的探测继续按原流程结束。范围与暂停令牌绑定，不能用业务预览清理维护数据。阻挡时每10秒只读刷新，不自动确认或执行清理。页面显示明确阶段、操作等待时长、最近/下次核对、真实阻挡状态/心跳和暂停倒计时（不是完成时间），删除接口不提供逐条进度，不伪造百分比。各请求30秒超时且立即反馈等待。
- 关闭窗口、清理成功或到期恢复检查；浏览器意外退出时最迟到期恢复。其他窗口显示占用剩余时间并只读检查解除，不抢占或释放别人的暂停；关闭被拒窗口无需等待原窗口。恢复响应未知时允许关闭，但明确标注未确认，保留原令牌/范围供重新核对。清理响应未知禁止重发，先读事实核实后再整页刷新。所选范围内手动新增任务/探测仍会形成真实阻挡。
- 阻挡项显示名称、原因和“查看任务 / 查看探测 / 查看配置”链接；在新窗口定位确切记录，UUID收进“记录标识”。主机探测链接读取指定历史探测，不替换为最新记录。素材任务的“取消任务”位于任务详情顶部，与“刷新此任务”同层，保留确认弹窗和原服务端状态/权限保护，不再藏于素材诊断。
- 素材文件和库内主图/变体关系保留；与已删除Bible、生成记录的关系显式解除，来源仍为原生成来源并标注“历史已清理”，不改为上传或伪造审核。新项目可直接使用这些已有素材。旧Smoke、探测、容量证据删除，模型文件证明和工作流校验/Smoke状态重置，不能继续当作当前可调用证明。工作流配置及不可变导入定义快照保持原值，快照内的当时库存注记不充当当前主机证明；实际输入仍引用历史对象时继续阻止清理。
- 素材库勾选素材后点“批量删除”，每批1–200项。单条/批量共用权限、引用和文件检查，任一项不允许删除则整批不删除。普通用户仅能删除自己的素材；超级管理员可删除全局素材。已引用素材须先处理引用。成功后只删除不再共用的受管上传文件，不删除外部引用或本地库路径的原文件。
- 历史清理不删除素材文件、本机备份、日志或ComfyUI目录，也不执行模型、重启或自动导入。它不是磁盘擦除工具。整页刷新可清除前端旧列表；已删除记录只可通过清理前备份恢复。
- Schema0235新增`AssetReferenceVersion.origin_history_cleared`，仅正式清理writer写入。未清理前仍要求确切生成来源；清理后允许保留生成素材的库内关系而无原任务FK。迁移本身不清理任何历史。执行清理后不能无损降级回要求原生成FK的旧Schema，恢复应使用配套备份与代码版本。

### Schema 升级

每次部署先在目标版本和目标数据库上审阅：

```powershell
Push-Location backend
.venv\Scripts\python.exe manage.py migrate --plan
.venv\Scripts\python.exe manage.py migrate --check
Pop-Location
```

`migrate --check` 表明有待应用 migration 时，应先备份并按变更窗口执行：

```powershell
Push-Location backend
.venv\Scripts\python.exe manage.py migrate
.venv\Scripts\python.exe manage.py migrate --check
Pop-Location
```

不得用 ORM 读侧兼容、手工建表或静默 fallback 代替 migration。本文不记录某一开发数据库的临时应用状态。

## 4. 启动与状态

开发前端的 `/api` 代理必须把应用 `OPTIONS` 转交 Django：`vite.config.ts` 使用 `cors.preflightContinue=true`，保留 Vite 原有来源限制，不将角色等元数据请求提前结束为空 `204`。选项读取失败应检查原响应；不得在页面补一套固定选项或放宽跨域权限。配置保存可能触发 Vite 自动重载，这不代表后端代码或数据库迁移已加载。

日常启动/重启：

```powershell
.\scripts\restart.ps1
```

开发回归与契约检查（默认包含完整测试）：

```powershell
.\scripts\check.ps1
```

后端专项回归也使用同一检查入口；仅选择明确的模块、类或用例：

```powershell
.\scripts\check.ps1 -BackendTestLabels studio.tests.test_execution_media_processing
```

`-BackendTestLabels` 接受 `studio.tests` 下的点分名称数组，空值、路径和命令选项会被拒绝。该模式只运行所选 Django 测试，先经现有解析器加载项目 FFmpeg/FFprobe 到当前进程环境，并统一测试签名配置及 UTF-8 输出；不会修改系统 PATH、启动服务或调用真实模型。结果明确标为专项，不包含前端、OpenAPI、迁移漂移或完整发布检查。不带参数仍执行全部检查。直接启动 `manage.py test` 不会替当前 shell 初始化媒体运行时，媒体样本编码／探测可能在进入业务逻辑前报“找不到文件”；不要删测试、改用假探测或据此判定供应商失败。

两种检查范围均在运行 Django 测试前读取 Django 已解析的数据库配置，只报告后端类别及行锁能力，不输出连接地址或凭据。SQLite 模式明确提醒行锁并发用例会跳过；本地通过不是 PostgreSQL 并发验收。目标数据库验收可在同一入口显式添加 `-RequirePostgreSql`：若不是支持行锁的 PostgreSQL 后端、配置读取失败或证据格式无效，立即非零退出，不进入 Django 测试建库步骤；不会自动安装、迁移或切换数据库。

标准Django测试入口使用`IsolatedMediaDiscoverRunner`，为本次测试分配临时`MEDIA_ROOT`和本地文件存储，退出时清理并恢复原配置；真实媒体文件检查仍可使用文件路径，单项fixture可在自己的临时目录内进一步隔离。该保护也覆盖直接`manage.py test`，不影响业务服务的正式存储，不把测试请求文件写入现用媒体目录，也不改变数据库测试／PostgreSQL延期边界。

`-RequirePostgreSql` 是检查范围约束，不是迁移命令，也不是连接成功证明。仅在目标环境已获授权时，由操作者在该测试进程安全注入隔离测试用 `DATABASE_URL`；不要修改现用开发库配置。Django 后续仍须实际连接并执行测试，测试账号应只拥有隔离测试环境权限。

`restart.ps1` 是本地正式启动/重启入口。它会设置新的 boot identity、执行迁移（除非明确使用维护用跳过参数）、启动 API/前端与 generation、text-generation、async-media、assembly、platform-maintenance worker，并按配置处理 ComfyUI。boot identity 生成不等于 restart-required 配置已激活。仓库当前没有独立的本地停止脚本；停止进程应使用部署环境的服务管理器，不得引用不存在的命令。

启动就绪探测的单次请求使用 `StartupTimeoutSeconds` 剩余预算，不会每3秒丢弃正在核对配方/实例合同的请求再重复发起。总启动截止时间、真实非2xx失败判定及失败后的本次进程回滚不变；不能通过改用 liveness 或忽略 readiness 来冒充就绪。

当 DynamicVRAM 已启用、ComfyUI 仍因桌面进程占用而在采样临时张量分配阶段 OOM 时，可在确认队列无 running/pending prompt 后，用同一正式入口增加额外显存余量并重启 GPU 进程：

```powershell
.\scripts\restart.ps1 -RestartComfyUI -ComfyUIVramHeadroomGB 1.5
```

该参数只透传 ComfyUI 的 `--vram-headroom`，默认值为 `0`，不改变常规启动行为；它不会降低分辨率、帧数、采样步数或替换工作流。数值越大，权重卸载越激进、速度可能越慢。每次调整后都必须创建新的 smoke attempt，以实际终态和输出探测为准。

需要让同一 ComfyUI GPU 在不同模型任务之间显式交接资源时，在对应 Deployment 的非秘密 `runtime_config` 中配置严格合同 `pre_submit_resource_release={"schema_version":1,"enabled":true,"unload_models":true,"free_memory":true}`。Settings 提供四个类型化字段；四项必须完整，不使用隐藏默认值。generation worker 只在新任务完成本地预检后、冻结 submission token 与 `POST /prompt` 之前调用一次 ComfyUI `POST /free`。已提交任务的恢复、`poll`、`collect` 和历史请求核对不调用 `/free`，避免卸载正在恢复的远端任务。该端点改变进程级 GPU 状态，因此启用合同的部署要求 `TASK_MAX_CONCURRENT=1`；并发不满足、HTTP 拒绝或释放结果无法确认时均在 `/prompt` 前明确失败。配方后续是否重试按本轮冻结次数与准入检查决定，原worker不会就地循环重复提交。成功/失败记录策略 hash、动作、时间、状态和稳定错误码，不记录敏感正文。

Provider/Deployment 执行合同不使用运行时隐藏默认值。文本端点必须显式提供 `chat_path`、`timeout_seconds` 与 `capability_ttl_seconds`；ComfyUI Deployment 必须显式提供 `base_url`、`timeout_seconds`、`poll_interval_seconds`、`max_poll_seconds` 与 `capability_ttl_seconds`；受限 async REST 还必须完整声明各操作路径、响应字段、幂等请求头、轮询预算与终态。终态每类项数、单个终态/返回状态字符数和产物主机白名单项数分别受 `security.provider.async_rest_terminal_status_max_items`、`security.provider.async_rest_terminal_status_max_characters` 与 `security.provider.async_rest_artifact_host_max_items` 保护；三者都位于“安全边界”，只有超级管理员可修改，远端返回状态超限会在持久化前明确失败而不是截断。ProductionRecipe 的每个已配置 `routing` 也必须显式声明 `variant_count`；其数值随 recipe/route 快照冻结，缺失不会按单候选回退。数值绝对上限读取系统设置中的当前安全边界，前端不复制一份固定范围。0117 仅把升级前曾由代码补齐的现用值物化到 Provider/Deployment 当前配置，不改写历史 Smoke、任务或审计快照。

修改上述 Deployment 配置会改变执行目标 hash，相关 Workflow 的旧 Live validation 与真实 Smoke 证据必须失效；重新 Live validate、创建新的真实 Smoke attempt 并发布后，才能用于新生产。显存释放只补足“切换任务前没有释放进程级模型/缓存”的隔离缺口，不能降低当前工作负载自身的峰值显存。排查 OOM 时必须同时核对分辨率、帧数、时长、采样步数与节点临时张量：例如旧的 5 秒 / 124 帧任务比目标 1 秒 / 22 帧任务显著放大压力，不能把其 OOM 单一归因于缺少 `/free`。最终验收应使用目标 1 秒 / 22 帧的新任务，而不是重放旧长任务。

不要为 demo seed、workflow 覆盖或旧生产入口添加 restart 分支。

## 5. 五层配置与 boot activation

有效配置按以下顺序解析：

1. model version default；
2. workflow version default；
3. active system revision；
4. active project revision；
5. active run override。

后层覆盖前层。ProductionRun 创建时冻结最终值、来源、revision 与 hash。环境变量只保留数据库连接、Django 启动秘密、数据库加密密钥环、主机路径、进程硬上限和网络安全边界等数据库启动前职责，不作为业务配置的隐式回退层。

运维流程：

1. 创建并校验 config revision。
2. publish revision。
3. 对无需重启的定义按 activation policy 生效。
4. restart-required revision 保持 pending。
5. 启动新 boot。
6. 在新 boot 上显式调用 activation。
7. 用 resolved config 和审计记录核对有效值。
8. 新建 ProductionRun，确认 snapshot 固化预期 revision。

不要把 pending 显示为 active，也不要在 restart 脚本中静默激活。回滚同样通过新 revision/activation 完成，保留历史审计。

### 类型化配置与资源依赖

Settings 的默认值页面按 `ConfigDefinition.value_type` 和 `value_schema` 渲染布尔、枚举、数值、文本、资源下拉和受限 JSON 编辑器，并展示描述、影响、默认值、单位、范围、必填、人工复核和重启影响。普通用户不需要用 JSON 手工填写已有类型化字段；高级 JSON 只用于尚未拆分的封闭结构合同。

媒体处理不再从 Python 常量或超时环境变量取得业务值。系统设置中的三个普通、可版本化对象合同分别是：

- `delivery.assembly_media_profile`：逐集/全剧母版的 video codec、preset、CRF、pixel format、audio codec/bitrate/sample rate、输入探测超时和视频 AI 披露样式/边距；
- `delivery.distribution_media_profile`：公开分发副本的编码、探测/渲染超时以及视频和图片披露框样式；
- `reference.media_preprocessing_profile`：动态参考视频/音频的探测、CFR 与音频规范化编码、超时和产物时长验证容差。

三者允许 system 与 project 修订，发布后由同一个配置解析器生效；装配把完整 profile 与 SHA-256 纳入来源签名，分发把它纳入 release identity，参考媒体则在同次 ExecutionPreparation 的已解析输入中冻结 `media_processing_profile`，并在每次暂存/重试时复验。配置变化不会静默复用旧衍生素材，也不从最新配置补齐缺失快照。对象 schema 的 `security_ceiling_paths` 声明每个嵌套资源字段对应的绝对上限；通用配置发布器和冻结快照解析器均校验这些关系，不由媒体服务另存第二份范围。

所有入库视频/音频共有的实际字节 FFprobe 使用 `artifact.media_probe_timeout_seconds`，它是 system 普通配置并由 `security.runtime.max_timeout_seconds` 提供外层绝对上限；旧 `TASK_DISTRIBUTION_TIMEOUT_SECONDS` 和隐含 `ASSEMBLY_PROBE_TIMEOUT_SECONDS` 不再作为业务事实来源。

主机探测模式在“系统设置 → 默认值与限制 → runtime”中通过配置修订维护，运行主机页提供跳转，不维护第二份设置：

| 配置 | 默认值／范围 | 生效与影响 |
| --- | --- | --- |
| `runtime.host_probe_mode` | `automatic`；可选自动／手动 | 发布后影响新定时调度；手动不自动入队，保留“探测所选主机”按钮；不取消已有探测 |
| `runtime.capability_ttl_seconds` | 2592000秒（30天）；30–31536000秒 | 自动模式的新主机快照有效期；不影响部署或组合Smoke |
| `runtime.host_probe_manual_validity_days` | 36500天（约100年）；1–36500天 | 手动模式的新主机快照长期有效期；其他模块继续按普通 `expires_at` 判断 |

模式和期限不改写历史快照。切换手动后点击一次探测，正常完成才产生长期结果；切回自动时按当前自动周期重新安排刷新，不等待上一份百年快照到期。自动刷新继续保留耗时提前量与去重；配置变更不重启服务，正在运行的探测按开始时的配置完成。缺失／失败事实、文件不匹配和服务错误不因期限较长而变成成功；机器或文件改变时应主动重新探测。0238升级为系统配置创建后继revision，将自动期设为30天并加入手动模式设置，保留旧配置、快照、Run、产物与部署Smoke。

与上述普通 profile 分离的 `security.media.duration_verification_tolerance_max_ms`、`security.media.audio_bitrate_max_kbps`、`security.media.audio_sample_rate_max_hz`、`security.media.processing_argument_max_characters` 和 `security.delivery.disclosure_text_max_characters` 属于安全边界，只允许 system scope 且只有超级管理员可修改。超时继续受 `security.runtime.max_timeout_seconds` 约束，披露像素值受 `security.media.max_dimension` 约束。codec/preset/pixel-format 的合法参数形态、FFmpeg 参数分隔、RGBA 的 0–255 和 opacity 的 0–1 是协议/格式不变量，不是可运营建议值。

运行期任务参数从同一配置中心调整，不修改 `.env` 或 Python 常量：`runtime.task_worker_poll_seconds`、`runtime.task_heartbeat_seconds`、`runtime.task_idle_timeout_seconds` 与 `runtime.task_stale_recovery_interval_seconds` 同时服务 generation、text、async-media 和 assembly；`runtime.worker_stale_recovery_minutes` 是 generation/text Worker 未显式传入 `--recover-minutes` 时使用的版本化回收阈值，命令行参数仅作本次运行的显式覆盖；`runtime.asset_text_submission_grace_seconds` 是文本素材任务已冻结请求超时之外的提交宽限，`runtime.asset_uncertain_reconcile_batch_size` 是 ComfyUI 不确定素材任务的每轮对账数量，分别受运行时安全上限保护。`runtime.gpu_probe_timeout_seconds` 控制本机 `nvidia-smi` 能力探测超时，探测快照记录生效值和配置 SHA，并受 `security.runtime.max_timeout_seconds` 外层上限保护。旧延迟续接请求已停用；其历史配置键纳入R5/R7精确清理，不再控制任何新任务。`workflow.smoke_poll_interval_seconds` 是受安全间隔上限约束的真实 smoke 轮询间隔。`delivery.assembly_max_attempts` 在 AssemblyJob 入队时冻结，且不得超过 superadmin-only `security.delivery.assembly_max_attempts`。`production.project_creation_policy` v2 是正式项目创建入口的 system 级闭合合同，提供语言、画幅选项、FPS、默认镜头媒介类型、表单建议容量与连续性种子策略；尺寸、镜头时长和音频开关分别读取 `production.default_resolution_width/height`、`production.default_shot_duration_ms` 与 `production.default_include_audio`，不在对象合同中复制第二份数值。`api.default_page_size` 控制运营列表默认分页；`workflow.node_metadata_default_page_size` 与 `workflow.node_metadata_input_preview_limit` 控制 ComfyUI 节点浏览展示，调用方请求仍不得越过 superadmin-only `security.api.max_page_size` 与 `security.api.max_search_chars`。`runtime.host_probe_heartbeat_seconds`、`runtime.host_probe_lease_seconds`、`runtime.host_probe_stale_seconds`、`runtime.host_probe_refresh_buffer_seconds` 控制主机探测；`runtime.platform_maintenance_poll_seconds` 与 `runtime.platform_maintenance_lease_seconds` 控制维护 Worker；`runtime.readiness_worker_max_age_seconds`、`runtime.readiness_min_disk_free_bytes` 及 `runtime.readiness_generation_worker_required`、`runtime.readiness_text_generation_worker_required`、`runtime.readiness_assembly_worker_required` 控制 readiness 判定。公开分发开关 `security.delivery.public_distribution_enabled` 位于超级管理员安全边界分类；`runtime.distribution_lock_stale_seconds` 是受 `security.runtime.max_timeout_seconds` 约束的 system 配置。清单版本 `shot-public-v1` 是代码协议不变量，不能由环境或普通配置改写。心跳必须小于租约、租约不得大于 stale，配置缺失或关系无效会明确阻断 Worker，而不是回退旧值。`TASK_MAX_CONCURRENT` 仍由目标主机部署环境设置，因为它是启动数据库配置中心前就必须确定的物理进程/GPU 上限；修改后需要重启对应 Worker。

既有对白摄入包络位于“安全边界”分类的 `security.dialogue.max_entries`、`security.dialogue.speaker_id_max_characters`、`security.dialogue.text_max_characters`、`security.dialogue.delivery_max_characters`，只有超级管理员可改。新剧本字段/规则在字段维护的确切输出版本配置，请求输出额度沿系统`execution.resources.review_policy.output_length`；规范稿自动切段和道具简称推导已移除。0227迁移将`production.script_auto_split_trigger_chars`、`production.script_auto_split_target_chars`及`production.script_prop_alias_policy`标为停用/非必需，并为受影响的启用系统/项目配置创建不含旧键的后继版本；原配置值、草稿、Run冻结快照和历史请求保留，不伪造历史迁移。能力页面不再要求或展示切段阈值；本轮只编写并隔离验证迁移，未应用现用库。0214迁移退役已无读取方的`production.series_concept_resource_advisory`，从启用配置移除旧键并另存配置版本，不转换历史剧本/请求；现用执行状态以详细设计检查点为准，不能将迁移文件存在视为已经应用。视频分辨率和帧规则不在这里复制数值：应编辑/导入下一版 WorkflowTemplateVersion 的类型化参数合同并重新完成 validation、真实 smoke 与 publish，历史已发布版本保持原事实。

`production.canonical_name_policy` 的名称长度、词数仍用于明确确认正式英文术语；确认回执记录实际策略和配置SHA。生成候选采用精确执行实例及原Job/Preparation，单次条目数量和请求字节由实际模型合同及当前统一请求配置校验，不自动拆批、截断或写术语。原同步链的批次建议成员、`production.canonical_context_policy` 与 `security.canonical_glossary.*` 已无该业务消费者，失效配置目录与数据引用列入P5清理，不将旧目录显示值描述为新链路生效参数。翻译请求与 smoke/uncertain 人工审计说明分别受现有条目/文本边界及 `security.audit_note.*` 限制。旧Shot译文批量回写已退役，`security.translation.max_promotions_per_request` 不再有消费者；仅留历史配置记录，不作为当前生效限制。普通确认只审核候选，不改分镜原文、对白或其毫秒，分镜页不再提供译文回填，也不因无关术语表冲突禁止保存；项目工作台已移除名称译表编辑与名称候选确认入口，原名称生产不依赖英文词表。媒体能力合同的模式、规则和说明文本容量受 `security.contract_complexity.*` 限制，字段形状、标识符、配对关系、正时长和时间顺序仍是协议语义，不可由配置放宽。

配置推荐使用 `value_schema.resource_ref` 声明 `resource=execution_instance_version`、精确执行 `capability` 与 `allow_empty`，不再引用裸 Deployment 或配置 `require_ready`。页面通过 `GET /api/v1/config-definitions/{id}/resource-options/` 的标准分页读取完整候选，显示精确版本和不可选原因；保存新增/变更推荐时服务端复核当前发布合同。可推荐不等于烟测通过，也不要求为保存推荐先运行模型。`production.prompt_normalization_execution_instance_version_id` 的空值表示未推荐；用户明确选择后，实际任务只使用自身冻结的实例和准备，worker 不自动读取推荐替换目标。原样继承只核对不可变配置合同和摘要。切换前应清理旧 AI 默认键及不兼容人工修订/引用；0166 只自动移除无人工/业务引用的安装种子，保留有效配置值并建立有完整摘要的新系统基线，不转换旧部署 UUID、不补造历史验收。 0167移除已退役的提示词Binding表和仅属于它的生命周期审计，不将旧规则展开为新实例；旧Smoke/生成尝试仍携带绑定时，必须先纳入R7精确清理。Schema逆向迁移不恢复被删除数据。

所有设置页新增/编辑弹窗统一遵守[填写表单最小输入标准](form-input-standard.md)：可枚举值必须读取服务端 `options`，系统字段只读或隐藏，真正的业务事实才保留输入。读取目录前先清空旧值；失败后不能继续使用旧目录，也不能自动退化为自由文本。服务端未在 `custom_entry_fields` 授权时，页面不得显示新目录值入口。

通用注册表单的数字输入（如模型静默时限、运行实例优先级、配置定义数值范围）区分明确清空与未完成的指数／符号。未完成或非有限数字保留原配置，显示字段错误并阻止保存；修改其他字段不会清除该错误。可补全数字，或选择“撤销未完成输入（保留原配置）”。明确清空可选字段表示移除该声明，清空必填字段显示缺值，不因无关字段修改自动重新填入默认值；0仍是有效数值，整数、范围等规则继续按同一合同校验。切换资源或字段类型后不继承不适用的数字草稿；该保护不表示所有高级JSON或专用子表单均已完成同类验收。

通用注册表单的高级JSON发生语法或合同校验错误时，保留未通过的文本和错误，修改其他字段不会将其静默清除。若其对应的同一对象已在结构化表单中修改，页面阻止旧JSON覆盖新值；选择“撤销此 JSON 草稿并载入当前配置”后，以当前表单值重新编辑。撤销不保存业务数据，也不回退其他字段。资源／记录切换，或字段隐藏、锁定、变为非JSON类型时，移除不适用的编辑草稿。已表单化的模型合同仍不可通过JSON绕过专用表单。

配方主调用与字段辅助统一按Run冻结的 `production.task_max_attempts` 自动重试：这是每个原请求／辅助批次的总尝试次数，包含首次，1表示不自动重试；不是阶段所有候选次数之和。等待间隔读取同一冻结快照的 `execution.preparation.recheck_interval_seconds`，原维护worker到期检查，不保证精确启动时间。按原准入检查通过后才派发，成功调用与已采用字段不重做；声明了备用路线时，先耗尽当前路线次数再按冻结顺序回退。当前次数、下一次最早时间、准入阻断与耗尽原因在运行和执行准备中展示。

次数耗尽转人工处理：主调用通过“重试当前阶段”预览确切失败集合，辅助字段通过执行准备详情处理原响应、填写字段或明确重试失败批次。人工确认可追加一次新尝试，不重置已消耗的自动次数。装配仍由原队列按冻结的 `delivery.assembly_max_attempts` 重试，最终失败转人工，成功集保留。无配方主调用仍沿原工作台“重试此失败主调用”，本次不为其引入隐式自动重发。

本地调用明确失败且没有产物时释放原预留，不因缺少服务商账单卡住，也不补造实际费用为0。生产文本主调用与字段辅助若以`execution_request_invalid`明确结束，且提交起始/登记、远端ID/状态、领取、媒体任务、响应/结果、采用字段和产物均不存在，预留已释放，可按原冻结次数重试；actual_cost仍保留未知，不写成0。仅缺少远端回执不能证明未提交。外部已提交调用仍须费用明确，远端结果未知、取消、已有响应待修正或归档失败均不得盲目重发；使用原状态核对／响应处理入口。提交前校验失败在原任务error_message中保留校验环节和脱敏原因，超长说明明确标记展示上限；不伪造模型响应，旧失败缺失的原因不反向猜测补写。旧 `production.prompt_field_retry_limit` 已停用，0237仅为现用配置创建去除旧键的后继revision，旧Run冻结快照与历史revision不改写。重试命令丢回执时只核对同一请求和幂等键，不新发命令；隔离协议测试不等于真实远端恢复。

同页对原运行的定时轮询不会清除“核对同一重试请求”。取得原命令回执后，继续查看原准备的实际排队/成功/失败状态；命令回执不是调用成功。浏览器整页重载或离开准备后不提供未核实命令的持久恢复，不因此宣称后台任务已撤销。

独立素材任务已留存失败正文时，可在任务中心“输入 / 输出与记录”→“准备与调用”→“模型返回与处理记录”编辑并明确保存人工修订。包含NUL控制字符的正文会被拒绝，保留草稿供修正，不会自动清洗后伪装保存成功。保存后关闭重开仍分别查看模型原文、人工修订和实际确认人；任务完成不代表模型原响应变得有效，也不表示已采用。未知、取消、已有后继等状态仍按原规则只读，不以此入口重发模型。

## 6. Provider、deployment 与 workflow 上线

风格素材库的文本、图片、视频和音频统一选择精确执行实例版本。目录按真实输出合同筛选，基础资源与组合Smoke分别核验；目录可选不等于本次请求已获执行许可。配置、空目录排障、费用和用户验收统一见[素材生成方案配置与验证](asset-generation-model-configuration.md)。

### Provider

- 建立 provider profile。
- 由超级管理员创建或轮换 SecretBinding，并把受管凭据绑定到 Provider 或 Deployment；平台管理员不能替换秘密绑定。
- 在目标环境验证授权、配额和 endpoint。
- 只展示 configured 与 owner lifecycle；configured 不等于可调用，停用的 Provider、Deployment 或 SecretBinding 都必须显示 blocked。

### Model profile、artifact 与 deployment

- 模型创建、修改、后继版本保存及发布／退役分别显示服务端写入回执和随后目录读取状态。写入成功但目录或独立证据读取失败时，不显示“列表已重新读取”，使用“仅重新读取”恢复，不能据此重复保存或发布；模型保存回执包含精确版本及ID。请求中的重复提交被阻止；真正写入失败保留表单／确认原因及错误。切换账号、权限、分区或模型上下文后，迟到回执不关闭新表单或向新分区显示成功。目录读取完成不等于独立Smoke／就绪证据通过；发布仍受服务端原门禁约束。
- 新建模型只填写新的 `profile_key` 和确实需要人工确认的模型事实；Provider、主模态、能力、提示词策略族和文件角色从 `GET /api/v1/model-profile-versions/options/` 的服务端目录选择。新模型可在选择器中显式选择由合法 `profile_key` 派生的同名新策略族，其他自定义文本会被拒绝。该目录不可用时 Settings 失败关闭，不回退自由文本。
- 版本号及模型规格结构版本不由操作员填写。首次模型由服务端固定分配 `v1`；“新建下一版本”只接受当前最高 published/retired 版本，先校验源规格完整性，再在事务中按该族包括 retired 在内的最高历史版本递增。最高版本已是草稿时必须继续编辑；旧源或并发请求返回冲突。`profile_key` 与版本号创建后不可通过普通编辑修改。
- 模型草稿／后继版本的“尺寸、帧率与时长”通过固定字段维护原 `media_constraints`；字段、整数范围与数量限制来自后端目录。尺寸上下界与倍数、帧率列表、时长成对范围无需JSON；0不是未声明。完整JSON可核对媒体约束，但修改须回到表单；其他扩展表单化仍属待办。不保存、不发布即可取消草稿编辑，不改已发布模型与历史请求。模型物理约束不由Projector放宽，也不与系统预算提醒混用。
- 编辑这些已有数字时，未完成的指数或符号输入不会当作留空删除原约束；页面标出错误并禁用保存。补全数字，或选择“撤销未完成数字输入（保留原配置）”恢复对应原值；修改其他字段不会消除未修正的输入错误。明确清空分辨率或时长字段仍表示取消该项声明，最终还须通过成对范围及其余合同校验。
- “原生剧本输入与输出合同”的时长、请求等待时限、原稿容量和字符上限，以及“异步媒体请求合同与 Smoke 样例”的字符数／数值上下界，采用相同的未完成数字保护。撤销只丢弃该字段未完成的输入，不回退其他参数。明确清空 REST 可选边界才移除声明；剧本必填数值及 prompt 最大字符数清空后仍阻止保存，0、整数和范围按原合同校验。字段移除或 REST 字段类型切换后，不继承不适用的错误草稿；目录缺失或只读时不能修改。这是模型规格的编辑校验，不创建任务，也不更改历史请求或 Smoke 结果。
- “文本执行协议与参数范围”的数值边界、允许值列表及对齐规则共用上述保护。删除列表前一行后，未完成输入仍跟随原来的条目；撤销恢复该条原配置，不恢复被明确删除的其他条目。明确清空允许值／步长／余数会保留缺值项并阻止保存，不自动当作0或移除规则；要删除请使用对应“移除”按钮。步长须为正整数，余数、0及允许值交集继续由原合同校验。各数字输入和逐项撤销均有包含能力、参数及行号的可访问名称，窄屏按钮换行不覆盖相邻输入。
- 模型素材数量、用途、预处理、配对与互斥规则的未完成数字保留原值，子规则错误阻止整个模型草稿保存。逐项撤销不回退其他修改；明确清空保留缺值并按原合同校验，不自动等价0。删除模式或规则后错误仍跟随原条目，重新配置已移除的预处理不继承旧错。复制合同失败保留当前输入和错误，仅成功替换才清除被替换草稿；迟到响应不覆盖新选择。这只保护编辑状态，不代表模型能力或Smoke已经验收。
- 节点等待时限的编辑上限也从模型选项目录读取，不依赖全局生产准入聚合；无关实例配置错误不应使模型编辑失败。目录读取失败时用“重新读取模型选项”重试，不写死或跳过安全上限。
- 模型卡片默认只用一行汇总能力与文件证据，需要时再展开；运行实例操作在可用宽度内横向排列和自动换行。视觉折叠不改变文件核验、发布、Probe、Smoke、停用或退役门禁。
- 草稿模型文件的库存目录、库存节点类型和输入名是高级文本字段，直接填写文本，无需JSON引号；字段只读时不可编辑。它们只声明受控定位信息，不移动文件或绕过发布／目录证明。
- 本地 Comfy 模型按 Provider → draft model profile/声明 artifact → 启用并 probe host 至 active，由工作流 discover 返回的同 Provider active loopback 内部 source Deployment 完成草稿 artifact attestation 与完整文件闭包 → publish profile → 建立目标 deployment → 再 probe host 形成该 deployment availability → artifact verify → deployment probe/smoke 的顺序登记。扫描前不要求管理员手选 source Deployment。远端模型没有本地文件时明确为 N/A；不能先发布再证明文件，也不能在没有主机事实时把 declared 文件伪装为 available。
- 审核来源、hash、许可和用途。
- 先把目标 host 探测为 active，再建立 active deployment；建立后再次探测，形成该 deployment 与文件的最新 availability 快照。
- 日常 Settings/API 探测只负责创建持久 `RuntimeHostProbeAttempt`（HTTP `202`）；platform-maintenance worker 领取任务、持续心跳并写入终态。页面刷新后从 attempt collection 恢复，不得因 12 秒客户端超时把仍在后台执行的 SHA 误报为失败。
- running 探测的取消先持久化为 `cancel_requested`，在 8 MiB 文件分块检查点安全停止；worker 崩溃后由陈旧 claim 恢复。若快照已经与 attempt 原子关联，恢复流程收敛为 succeeded，不重复扫描大文件。
- `manage.py probe_runtime_hosts --due` 是人工维护/故障诊断的同步命令；正常运营只使用 Settings/API 异步入口，不与 active attempt 并发执行。
- 对本地模型文件使用最新、未过期的 RuntimeHost snapshot 执行 artifact verify；远端文本模型没有本地 artifact 时明确标为 N/A，不伪造文件。
- 执行 probe 和 smoke-test。文本部署分别选择 `script.generate`、`script.structure`、`prompt.normalize` 与 `visual_rules.generate`；每次调用都先返回 HTTP `202` 的持久 `ModelDeploymentSmokeAttempt`，Settings 从 attempt collection 恢复并轮询终态，不受前端短超时影响。worker 在落库后只跨越一次提交边界：`script.generate` 提交最小一集一镜请求，其余三项使用各自严格合同；公开证据只保留 hash/费用/错误分类。外部部署未配置确定 pricing 时不会排队提交。`queued` 可取消，进入 `submitting` 后若状态不确定必须由管理员核对 Provider 控制台/账单并追加消歧证据，系统不会自动重放付费请求。
- 基础资源烟测从明确Deployment/Workflow进入，选择该资源支持的能力与提示词来源；选择策略时直接引用精确发布版本，不读取Binding或优先级默认。ComfyUI基础烟测不代替策略组合验收。执行实例的组合烟测使用该版本已冻结的策略或人工通道及辅助配置，不再次挑选另一策略；A组合通过不能为B组合放行。

- 基础资源烟测的“更换能力与提示词来源”若已有输入，先以站内确认说明清空范围；返回或Escape保留原表单，批准只清除下方输入、素材与授权并返回选择，原选项保留供修改，不自动重读或调用模型。资源与执行实例共用的“重新读取输入合同”也需确认已有输入；读取失败保留原值并禁止提交，只有成功读取才重置输入、来源上下文及授权。初始必填错误不算用户修改，已输入但未完成的内容仍受保护。
- Smoke提交响应未知时，表单保留同一请求体与幂等标识；重试明确拒绝也不能抹去此前的未知结果，只有原请求回执确认后才解除。核对期间父面板禁止关闭/收起或更换资源另建任务；账号或对象变化、页面卸载后的迟到响应不作为当前成功回执。此保护限当前面板，不提供浏览器刷新/关闭后的持久恢复，也不取消已经提交的任务；不要以刷新页面代替“重试确认同一请求”。
- 基础与组合烟测沿原Attempt根创建统一Preparation，冻结本次合同、输入、参数及身份，返回持久任务回执。重复请求核对原幂等意图，不重抽种子或重发远端调用；准备期间目标发生变化明确拒绝。字段生成、审核、估价、费用授权和提交边界各自按实际准备状态推进，不能把HTTP受理、配置发布或历史证据显示成真实烟测成功。
- “系统设置 → 模型 → 运行实例 → 真实烟测”关闭有修改的基础资源烟测表单时，站内确认框显示本次部署及丢弃范围；“返回继续填写”或Escape保留选择和输入，只有“确认放弃并关闭”才丢弃当前未提交表单。已创建的准备、任务和烟测记录不会因此删除或取消，也不调用模型；提交/读取忙时禁止关闭，确认时再次核对部署和页面上下文。此说明不替代任务中心的真实取消操作。
- SQLite 短暂写锁冲突时，worker 领取任务按配置的轮询间隔等待重试；初始心跳写入失败则由心跳循环重试，真实写入前仍显示缺失/陈旧。非锁冲突错误继续报错；不会把模型调用、远端提交或产物生成放入数据库重试，以免重复执行和计费。若 worker 已异常退出，通过唯一入口 `scripts/restart.ps1` 恢复，勿手动重放已提交或提交状态不确定的任务。
- 未成功前保持不可路由。

draft profile/artifact 可以编辑；只有仍为 `declared`、从未核验且没有主机观测、工作流或配方引用的草稿 artifact 可以删除。published profile 不能覆盖，只能先停用引用它的 deployment、清理 current workflow 约束后 retire，变更通过下一版本完成。Deployment 必须先 disabled：完全没有 probe/smoke、host availability 或其他引用的误建空记录可以删除；已有证据的记录应在替换 draft/published 配方和 current workflow 引用后退役，retired 为不可逆只读终态。

#### 模型文件路径调整与注册表清理

模型文件注册与物理文件管理是两个职责。Settings/API 只维护相对定位、预期大小与 SHA-256，不会替用户移动或删除磁盘文件。路径调整按以下三类处理：

1. **只迁移 ComfyUI 安装根，文件相对位置和内容不变。** 先由运维人员移动/恢复实际文件，再把 `backend/.env` 的 `COMFYUI_OUTPUT_DIR` 改为新 ComfyUI 根下的 `output`（平台据此使用同级 `models` 目录）；若 ComfyUI 地址也变化，同步修改 Provider/Deployment 的受控端点配置。使用唯一日常入口 `scripts/restart.ps1` 重启，然后重新执行 RuntimeHost probe 与 artifact verify。旧快照保留为历史；端点或 Deployment 合同变化会改变 target SHA，必须重跑对应 Deployment smoke 以及 Workflow Live validate/smoke 后再用于新生产。
2. **相对目录、文件名、大小或 SHA-256 变化。** 所属版本仍为 draft 时，在模型文件编辑器更新声明；保存只更新注册记录，不移动文件，并自动把状态恢复为 `declared`、清空当前核验指针。随后重新做草稿文件证明/主机探测并按正式发布顺序收敛。所属版本已经 published/retired 时，不改旧记录：新建更高 ModelProfileVersion 和新的 artifact 闭包，发布并建立新 Deployment，完成 probe/verify/smoke；引用该模型的 Workflow 也新建版本并完成 Live validate/smoke/publish，替换 draft/published 配方与 current 工作流后再停用、退役旧 Deployment 和旧模型版本。
3. **文件缺失或路径配置未同步。** 新 probe 必须写出 `missing`、`incompatible` 或 `unknown`，页面以最新且未过期的精确 size/SHA 事实阻断核验；不得猜测新位置、回退旧快照或仅把历史 artifact 顶层状态当作当前可用。先修正物理文件或受控根，再重新 probe/verify。

模型页按 `profile_key` 显示模型目录，版本内分开查看能力合同、文件证明与运行部署（ModelDeployment）；执行实例在独立的“执行实例”分区配置，不与底层部署混称。默认隐藏 retired 模型版本及其子记录。运行部署停用后删除前须读取统一影响清单，存在有效历史或阻断引用时不能直接删除。Provider、模型能力版本、Deployment、模型文件、Workflow、Recipe、ConfigDefinition、Prompt 字段/Profile/规则及执行实例复用统一影响审查，不展示原始JSON后直接尝试删除。这里是日常注册管理边界；本次已授权的不兼容数据清理使用本页开头的限定维护流程，不通过日常DELETE放开历史级联。

影响弹窗把依赖分为四组：可逐项勾选的安全草稿、随目标存在的自有草稿结构、必须替换或单独复核的阻断引用、绝不删除的运行/审计历史。默认不勾选任何依赖；存在阻断或历史时删除按钮禁用，并显示具体对象名称、版本、状态、UUID、关系和配方 JSONPath。操作者必须选择审计原因并输入服务端返回的完整目标短语；页面只向统一的 `registry-deletion-execute` 提交，资源 detail API 没有直删入口。提交期间禁止关闭；服务端在锁齐依赖及其父边界后再次重算影响，引用或对象内容漂移时拒绝提交并要求重读，不会部分删除。配方、精确执行实例及配置引用的写入与删除共享服务端事务 guard，持锁后重读依赖及完整内容摘要。已冻结配方和ProductionRunPlan按声明的精确实例路径识别引用，字面输入中出现的UUID不作为选路关联；Preparation及原任务、证据通过真实外键列出，不从旧绑定key推断。任何删除动作都只删除数据库中的可清理注册记录，不触碰本地模型文件。

删除复核与当前目标、账号/权限和页面上下文绑定，迟到的清单或回执不会写入另一对象。重新读取依赖失败时保留旧清单及人工输入，并标明失效、禁用提交；成功读取后清空勾选、原因和确认短语，必须重新复核。提交期间禁改输入、重复提交及关闭；只有 `deleted=true` 且回执目标类型/ID与请求一致才确认成功。网络中断、5xx或完成回执异常显示“结果尚未确认”，不推断回滚，不自动重发；请关闭复核窗口后到原资源目录与“凭据与审计”的生命周期审计核对。只读重读不证明未知请求失败，也不解锁当前窗口。Settings登记的成功删除回执在目录刷新失败时仍保留，目录明确报错；只读重试不再次删除。

平台不会自动下载模型。catalog 中存在 H3、SDXL 或其他系列只表示可配置/待许可审阅，不是已安装证明。

### PromptProfile

参考文案需要单元原文时，先在原策略编辑页选择“结构化输入”的确切版本，再在“单元原文路径映射”勾选所需位置、选择目录中的文本路径。可全部不勾选；同一自定义字段可被不同策略分别映射，不按字段名猜含义。更换结构保留旧路径并报错，需明确修正。缺键只用结构已登记默认，仍缺失或非文本拒绝执行，原始输入独立保留。实例单元表单填写 `structured_input`，不再填写固定 `source_facts`。旧已发布合同需配置后继草稿，不能改写历史调用。通用示例仍只验证排版，不证明真实渲染或模型效果。

在“系统设置 → 提示词策略”维护策略及字段规则；在“系统设置 → 执行实例”中，选择策略组装方式后固定一个精确已发布策略版本。执行实例详情及关联目录可继续查看该策略配置。底层运行部署可被多个实例复用，不再通过模型族、active Binding 或 priority 为本次调用自动选择策略；原提示词运行时Binding入口已移除。

PromptProfile 是提示词可配置语义装配的唯一主路径，应在 workflow 和 ProductionRecipe 之前完成治理：

1. 在字段目录中审阅 key、类型、分类、描述、影响、默认值、枚举和必填性。
2. 为 model family/model version、operation 和 mode 建立 draft Profile。普通直接装配选择 `direct_v1`；需要组合已注册确定性能力时选择 `declarative_v1`，再按字段规则选择 `projection.output` 与代码原语。字段、channel、顺序、标签、分隔符、缺省处理和长度都在页面配置。
3. 编辑弹窗右侧会自动带入服务端通用示例并直接开始预览；只有需要验证特定内容或空值时才修改。页面以 400ms 防抖把当前未保存的完整规则快照和本次示例提交到只读 preview，并展示服务端实际组合出的各通道提示词。通用示例不读取生产默认值，用户修改不保存，preview 不调用模型；它只验证最终排版，不代替声明式原语的真实项目上下文验收或 deployment/workflow smoke。
4. 审核官方参考、合同 hash 与系统指令来源后发布。只允许管理员默认值进入 `system_instruction`，项目或请求文本不得提升为系统指令。
5. 在执行实例草稿中显式引用已发布策略，核对执行能力与提示词用途、输入映射和参数合同后发布实例。需要不同策略的业务用途建立不同实例版本，不复制底层运行部署。素材库、无配方制作与配方只选择精确执行实例；每次Preparation冻结实际输入并按实例配置处理提示词。

字段身份和定义版本统一在系统设置“字段维护”管理，类型/状态目录来自定义版本的 `options` API；operation、mode、投影器和规则选项来自 PromptProfile 的 `options` API，不再下发重复的字典选项。新字段的展示排序与启用状态、新 Profile 的 `version=1` 与 `needs_review` 由服务端维护；页面不提交这些技术字段。已发布/退役 Profile 只能提交精确合同 SHA-256 创建后继草稿，不能从“新增”窗口重复填写同一 Profile Key。

字段列表并列显示稳定标识和中文显示名称。名称复用定义版本已有的 `label`：优先显示最新可用定义；没有可用定义时展示最新现存版本并明确标注草稿或停用，不推断中文翻译。到“定义版本”维护“显示名称（建议中文，与业务页面一致）”；已发布版本仍按原后继草稿流程修改。目录名称只用于辨认，不自动替换业务表单绑定的精确版本。未设置中文名、没有定义及读取失败分别提示，失败可重读。

删除字段不是隐藏输入框的开关：项目原有输入框由项目字段和保存逻辑决定，不会因删除同名公共字段自动消失；动态表单由所引用的结构或字段规则生成。调整显示范围应修改对应使用配置。删除先查看引用影响；结构引用或冻结规则引用会阻断删除，可删除的草稿依赖须在删除预览中明确选择，不把“停用”当作删除或自动解除引用。

输入/输出形状在字段维护的“输入／输出结构”统一编辑：新建草稿或从确切版本建立草稿，逐节点选字段版本并配置层级、属性名、使用约束和明确缺键默认。保存后才能检查和试填该版本；启用操作先保存当前编辑，失败保留草稿。已启用/停用版本只读，消费者不会自动换版。节点约束来自同一编译器目录，结构样例不写入任务。

“引用影响”复用能力关联总览：点击字段的定义版本/结构/策略数量，可继续查看来源、业务输出、执行实例和配方，再点“查看配置”打开确切版本。含停用和分页外版本，不自动改选最新；关联数量不代表调用或验收通过。这里只查询当前保存的配置，不做所有历史请求扫描，不保存映射或引用副本，不触发模型。读取失败/记录不存在应重新核对，不能按零引用处理；删除仍走单独的删除预览和确认。

“字段映射预览”选择确切来源/目标结构（含可编译草稿），粘贴原始JSON，从同源路径目录配置对象/列表/逐项/固定值/稳定key查找，点击后查看原始值、来源解析值、目标结果、默认和取值来源及路径错误。修改输入使旧结果失效，失败保留原文与映射，不自动修复JSON或猜测key；未完成数字不会提交先前值。此处只读试配，不保存第二份映射、不创建任务或调用模型；正式映射仍归配方/实例/策略配置。引用导航已接入原关联总览；M1-10模块用户验收仍待完成，不将这些页面视为整个M1已验收。

`declarative_v1` 落实“引擎原语写在代码里，业务组合规则写在页面里”。代码 registry 只声明原语 key、确定性实现、所属 engine 和可消费的 contribution selector；每条 `PromptProfileFieldRule` 使用 `source_selector=projection.output` 与 `projection_primitive` 把一个原语结果映射到所选业务字段。字段名称和数量、position、channel、label_style、separator、required/omit_if_empty，以及 `server_locked`、`model_generated`、`translate_only`、`passthrough` 和 final/contribution 分工全部保存在不可变 Profile schema v6 数据中并进入合同 hash。H3 六段只是六条规则选择六个 `reference_context.*` 原语后的配置结果，不是生产主逻辑中的位置或字段名分支；其他模型可以重排、改名、删减或复用这些原语而不修改主编排。

设置页的日常主路径不要求手写投影 JSON，也不允许手写 generation source 路径：Profile 面板用结构化控件维护当前 engine options，字段规则按“字段内容如何获得”和“最终提示词如何排版”两组业务问题配置。`model_generated` / `translate_only` 的“主要内容来源”选择服务端执行读者目录或 `request.structured_input` 确切结构路径（逐单元贡献只用本单元路径），选中后就近显示读取内容、适用条件和作用范围；`server_locked` / `passthrough` 自动跟随最终提示词取值，不重复配置。低频的额外参考信息、语言、长度和 validators 按需展开。保存与实时 preview 复用同一服务端校验和装配逻辑，页面隐藏高级项不代表省略合同。

运维修改已发布策略时使用“创建后继草稿”：页面向 `clone-successor` action 提交来源合同 SHA-256，服务端从原生支持的精确 published/retired 版本复制配置与规则，自动分配下一版本并回到 `needs_review`。来源变化、不是不可变版本或投影不受支持时零写入失败。发布新策略后，通过执行实例后继草稿显式引用，不自动替换既有实例或已冻结Preparation中的策略。

“管理内容来源版本”中的已保存读取规则随版本固定；当前来源目录只用于新建或明确更换读取位置。修改业务名称、发布或创建后继不自动换规则。若详情／保存报告合同无法核验，应停止使用并定位原记录与备份，不可用重新保存、更新摘要或当前目录覆盖历史来消除错误。来源读者的结构物化只走Django迁移并验证原摘要；先核对精确范围和备份，失败时保持原事实，不手工改成成功。

需要“每次生成自行填写”的业务输入时，从来源目录的“本次请求输入”选择已有文字位置并命名，发布后在策略规则选择精确来源版本；实例自动派生同名业务表单，不必再建接线。任务创建时填写并确认，不会自动读取项目。主题／风格可明确预览项目副本再编辑；若要求必须使用项目保存事实，应选择“项目事实／视觉风格事实”对应读者而非请求文字。来源名称不增加新协议；文字时长／画幅描述也不替代模型运行参数。

`classified_reference_context_ir_v1` 已不属于当前支持的投影，不提供旧策略运行兼容。不能靠重新挂接它来升级历史；不兼容策略及核定关联按维护清单删除，使用当前 `declarative_v1` 配置需要的规则。原生兼容的其他策略仍可复用，不以版本号或模型名决定保留。只有新增外部协议、现有原语无法表达的确定性算法、conditioning adapter 或安全 validator kind，才需要增加并审查代码注册项。

策略的操作用途与实例的模型/工作流执行能力分别核对，二者不因名称相近而自动等同；例如提示词用途 `video.generate` 不替代精确视频执行能力。未选择实例、未发布引用、输入合同不匹配或摘要漂移均明确阻断。策略草稿不代表生产能力，任何入口都不会回退通用模板或默认Binding。人工完整通道和不使用提示词的方式按实例合同分别校验，不强制附加策略，也不隐式调用AI改写。

参考策略的“逐单元辅助上下文”用于辅助字段生成，不是最终提示词。独立烟测中，`context.*` 对象的属性名必须与 `reference_context.units.ordered_units` 每项的 `unit_key` 一一对应，属性值填写该单元对应的风格、镜头或关联实体事实；不能直接把事实对象放在最外层，也不能用空对象冒充已经逐单元提供。名称来自受控上下文目录，填写测试事实不代表项目已审核。生产中的真实上下文沿已配置来源读取并冻结，仅选择标识不会自动查询项目、采用或封存。

### Workflow

已发布工作流证据过期时，不改旧记录或重新发布。通过扫描区“用当前已发布 Projector 生成后继合同”显式重建原模板后继；即使来源图和结构不变，也由 `successor_of` 区分重建与普通去重。显式后继可在同来源兼容候选中选择修正后的模型版本，变更选择会清除旧预览并要求重新生成合同；普通已匹配导入仍锁定原模型。旧版本的绑定和证据不改写，模型精确上下文与来源/模态约束照常生效。前驱须为原模板最高版本，新草稿重新进行 Live Validate/Smoke。响应未知先回读版本，原请求保留同一前驱与合同草稿；刷新或重新扫描后不能把刚创建的后继当作原请求前驱继续重试。

模型上下文下的 saved-workflow discover 仍扫描全部受控来源，页面只按当前精确模型的 candidate、contract draft、WorkflowTemplateVersion provenance 和服务端 actionability 派生主列表；文件名、同族和相似图不是关联证据。扫描会冻结发起时的页面加载代次与 `model_profile_version`；扫描期间切换或清除模型后，旧成功、旧失败及 preview/证明恢复结果全部丢弃，原物理请求结束并释放 busy 前不接受第二次扫描。

ComfyUI saved workflow 上线使用五层主路径：

1. **发现并纳管来源**：在 Settings 核对 `workflow.comfyui_saved_workflow_directory`，扫描全部受控来源作用域，再显式以 `import_kind=candidate` 保存合法文件。candidate 只要求目录、JSON、大小和敏感字段检查通过；不要求目标执行 Deployment、published 模型或 projector。内部 source Deployment 仅负责读取受控目录，不授予生产执行权。
2. **生成执行合同草稿**：对精确 candidate 运行 preview；未选主输出时只返回候选，选定主输出后持久化/复用不可变 `ComfyWorkflowContractDraft`。参数、素材槽、模型依赖、projector identity、blockers 和 deferred Live Validate requirements 都冻结在草稿 projection 中；未解释项保留为 `candidate_only`，不删除来源。
3. **提升与 Live Validate**：仅 `ready_for_executable_import` 草稿可提升为 needs_review `WorkflowTemplateVersion`。提升只消费冻结草稿，不重新读取本地文件或 `object_info`；Live Validate 才要求 active 目标 Deployment、published 模型、新鲜 artifact、实时节点 Schema 和 adapter/projector 一致。
4. **真实 Smoke**：在工作流版本验收页为本次 attempt 选择具体参数和 Asset fixture，在真实 executor 上执行，并审阅 attempt、compiled graph、输出、媒体探测和 target evidence。
5. **发布与生产路由**：上述合同、Validate 和 Smoke 证据都通过且 fresh 后显式 publish；Recipe/ProductionRun 只使用已发布版本。candidate 与 contract draft 不能进入 Validate、Smoke 或生产路由。

刷新页面后仍应从详情 API 看到 validated deployment、validation/smoke evidence 与合同 hash。已发布工作流不可原地更新；需要调整时导入新版本，完成 validate/smoke 后发布替代版，再 retire 旧版。不要依赖固定 Comfy node ID，也不要由 seed 或 restart 覆盖 catalog。

ComfyUI 动态选项按实时 `/object_info` 的已选分支逐层展开，V3 子项保留 `parent.child` 精确名称，旧式 `formats` 子项保留其顶层名称。画布导入、API 合同校验及参数识别共用此展开逻辑；嵌套枚举、必填和数值范围同样校验，总展开量使用已有 `security.comfy.contract.max_dynamic_widgets` 配置，未知分支、重复名称或超限均拒绝。导出不补造隐藏旧字段或缺失的动态值；位置式画布无法无损表示缺少中间可选控件时明确拒绝，不能用默认值替代历史输入。升级导致节点端口名称变化时仍需新保存 revision 和后继工作流，不自动改写旧版本。

工作流的枚举同时含字符串和数值等不同标量类型时，不自动把所有选项转成字符串。如果 Projector 已明确声明固定值 `const`，执行参数层先核对该值属于原始枚举，再按其真实 JSON 类型收窄为单值合同；只有默认值不算固定，仍拒绝混合类型。原工作流枚举保留，来源层及固定值可核对；无效固定值、冲突默认值及包含非标量的枚举均报错。可用 Projector 后继明确保持原任务使用的固定参数，再导入新的工作流版本，不改写历史版本。

无量化进度的长节点不得依靠重复 `running` 心跳无限续期。模型版本通过类型化字段声明通用 `execution_monitoring`；工作流专属节点规则只允许注册 projector 从 ComfyUI 源派生并在验收页只读展示，不接受人工节点 ID/JSON。新的调用准备将有效监测合同/hash 随执行定义冻结进 ExecutionPreparation；原Task仅沿实际主Attempt读取同次准备，不复制监测快照、不读取GeneratedWorkflow。执行时只在节点切换时设置一次 idle 窗口，且永不延长 hard deadline。配置字段、优先级、下一工作流版本迁移示例和无节点遥测边界见 [长节点执行监控合同](execution-monitoring-contract.md)。

执行实例改造的源码路径中，素材库统一选择已发布的精确执行实例版本，通过其定义读取图片、视频、音频或文本输入；API/ComfyUI资源由实例引用，调用方不再拼接工作流和部署。素材选择、人工通道与参数保存在Preparation，再进入原素材队列。完整部署切换仍以[改造计划](optimization/execution-instance-unification-plan.md)的P4/P5验证为准，不能将源码接入描述为真实服务已经验收。

完整的candidate、合同草稿、提升及逐版本Validate/Smoke/Publish见[工作流导入手册](comfyui-saved-workflow-validation.md)。合同生成响应丢失时，从导入区域“核对未确认的工作流导入”点击“只读核对原请求”，按[原合同恢复步骤](comfyui-saved-workflow-validation.md#33-合同生成响应丢失后的只读恢复)处理；需要匹配后台与迁移，不能把未上线或未找到回执当作允许重复生成。模型上下文只过滤全局扫描结果，不按名称推断关联。导入阶段不选择本次参数或Asset；详情workflow_input_schema只读展示冻结合同，不合成Smoke样例。原资源Smoke与执行实例均从共用输入合同创建Preparation，选择明确受管素材并保留实际顺序与配对。

通用多素材使用`generic.references/v1`，分组定义包含有序槽位、顺序端口或逐项合并及数量来源；分类参考使用`generic.classified/v1`。二者复用共用输入解析、Asset上传权限与校验，不按H3或其他模型名开独立请求入口。更新代码后须完成计划中的迁移、正式服务重启和验收，不能把前端构建通过当作运行服务已切换。

`WorkflowProjectorVersion` 的管理属于超级管理员版本化目录，不是普通 ConfigRevision。发布新 current projector 只影响后续新草稿；历史 candidate、contract draft、WorkflowTemplateVersion 和运行证据保持不可变。变更图族、节点角色、参数策略、模型文件角色、素材拓扑或 adapter 后，必须形成新的 projector 版本，并从同一不可变 candidate 生成新的 draft/version；只有 saved workflow 来源字节变化时才纳管新的 candidate revision。误建的最高、未发布且未被合同草稿引用的版本可从设置页二次确认后“放弃草稿”，动作写入生命周期审计；已发布或被引用版本不能删除。不能用重启或配置发布重解释历史合同。

方案目录每个版本均可直接“查看 vN 配置”，无需先创建草稿；只读窗口允许切换配置分区和复制 JSON，不提供保存。参数策略的真实处理、Generic 固定槽与 H3 Ref2VA 动态数量的区别见导入手册 4.6–4.7；尤其不能把 Generic 的 `dynamic` 声明误认为已有动态节点编译能力，也不能把 `source_locked` 当作统一运行时不可覆盖锁。

Projector 方案族、版本和表单选项必须全部读取成功才更新目录。任一读取失败时，同一账号与模型上下文保留上次完整结果并明确标记“上次读取／尚未更新”；首次读取失败表示未知，不显示为“没有版本”。若后继草稿创建或草稿保存已经得到服务端成功响应，但随后目录刷新失败，单独保留该响应的版本并明确区分“写入已完成”和“目录尚未更新”，不当作写入失败或重复创建。完整重读成功前，新建、编辑保存、发布、停用、放弃、退役和后继草稿等相关写操作不可用，仍可只读核对旧配置。后继草稿编辑框内可直接“重新读取 Projector 目录”，不清空表单；保存本身被拒绝则保留输入和原始错误。切换账号、权限、分区或模型上下文后不沿用旧目录，迟到写入回执不重新打开旧编辑框。页面的“重新读取”只查询，不自动补建 v1 或重复先前写操作。

新建Projector表单切换初始化方式、已有模板或执行引擎时，若已有未保存配置或未完成数字／JSON输入，会先显示站内确认，说明将替换的图结构、参数、素材和产物类型；方案族key、名称和说明保留。确认前下拉仍显示当前实际版本／引擎；返回或Escape不丢弃输入。关闭同样说明只丢弃当前未提交表单，已创建的方案族或版本不会被删除或发布。重复操作、忙碌以及账号／分区／上下文变化后的旧确认不能继续执行；这不提供刷新或离开页面后的草稿持久恢复，也不等于已有版本的保存／发布验收。

已有Projector草稿的编辑窗口也保护未保存配置、未完成数字和未应用JSON：关闭时确认具体方案族及版本，返回或Escape保留输入；明确放弃仅关闭当前编辑，不删除服务端已有草稿。未改动或只读窗口直接关闭。高级JSON离开分区时单独确认，只丢弃尚未应用的文本，参数表单及其未完成数字不变。切换“兜底方案”若存在识别条件，会说明将清空必需节点、可选节点与关键输入条件；确认前复选框仍显示原值，取消不改条件，批准也仅调整本地表单，仍需另行保存。重复点击、忙碌及版本／身份／内容变化后的旧确认不能执行，不代表刷新后的持久草稿恢复或真实保存／发布已验收。

Projector作者中的动态素材组数量、模板／合并输出索引、分类素材数量及参数范围会保留未完成数字的原配置，并显示逐项撤销入口。切换已访问分区不清除该草稿或解除保存阻断；删除前一条参数／素材组后，错误仍跟随原条目。明确清空必填数量／索引是缺值，不当作0；可选参数范围明确清空才表示继承节点／引擎合同。更换数量来源或接线方式只清除不再适用的草稿。只有明确应用合法高级JSON时才整体替换子表单草稿；以上不代表工作流已生成或真实验收通过。“创建后继草稿”会立即落库，取消其编辑窗口只放弃未保存字段，不删除已经创建的版本。

同一数字草稿保护也用于匹配优先级、节点数量／范围、必要连接索引以及关键输入期望值和固定参数值。固定值／期望值选择“整数”时，不截断小数、未完成指数或超出安全整数范围的数值；选择“小数”后，即使当前值是0，也保留本次编辑选择，完整的科学计数法按实际数值转换。数值清空不会隐式改成null，需明确选“空值”；必填优先级／数量／索引清空则显示缺值。修改其他字段、节点重命名或切换分区不清除待修正错误，删除条目、切换值类型／治理策略只清理对应草稿。界面不再将异常原值静默显示为0；服务端原有结构与业务约束仍是保存、发布的最终门禁。

需要在 ComfyUI 中查看已保存工作流时，使用 Settings 的“下载 ComfyUI 导入文件”。系统固定返回 `open_mode=download_import`，不声称支持 direct-open。validated、published 和 retired 版本只能使用记录证据的精确 `validated_deployment`；没有该证据的 draft 必须显式选择兼容 active ComfyUI deployment。部署停用、provider/model 绑定不一致、validation target hash 漂移或请求伪造其他 deployment 均在请求 ComfyUI 之前阻断。导出只读 `object_info` 并生成 UI canvas JSON，不 submit、poll 或创建 smoke/生产任务。

### ProductionRecipe

“创建后继草稿”会立即创建服务端草稿，页面保留回执中的配方族、版本和ID，再单独刷新目录。目录或编辑选项读取失败时不重复创建、不用旧数据打开编辑器；使用“仅重新读取”恢复目录后，找到该ID继续编辑。完整读取成功才自动打开这次创建的精确版本；切换账号、分区或离开页面后的迟到响应不会重新打开旧窗口。取消编辑只放弃未保存字段，不删除已经创建的草稿。

配方草稿只接受schema3，通过服务端阶段目录、精确执行实例版本及输入/参数合同编辑；不展开为旧Deployment/Workflow/Binding路线。独立规范化生成/审核已移出阶段目录，目标提示词由每次执行准备统一生成、翻译、确认、采用和组装。源字段未确认或配置不完整时明确阻断，不把旧审核结果迁入新准备。已冻结版本不覆盖，缺失的新阶段适配也不回退旧路径；正式发布/创建按当前精确合同校验，不再以开发阶段名称统一阻断；开发验证与真实库切换状态见改造计划第0.1节与最近记录。

分镜素材沿结构化剧本、已采用Bible和本轮需求声明，再按本轮配方来源规则选定；不是“每镜必须重生成实体图”或“所有Bible实体都要出镜”的全局规则。

1. 在本轮制作台打开“选择素材（单镜 / 多镜）”；人工入口为“选择素材 / 上传（单镜 / 多镜）”。逐镜核对各使用项的名称、设定修订、确切素材版本或独立Asset，不读取旧Shot素材池或Bible默认主图补齐。
2. 配方冻结上游、已有、上传来源及自动/人工选择方式。自动匹配缺失或多义时按原因人工接管，不把未满足的依赖标为成功；上游结果未知不能重发调用试探。库内及上传来源复用正式素材入口，不要求重新生成图片。
3. 出镜人物、逐行发声、场景和道具保持明确关系；画外音不自动出镜，原对白与毫秒区间不改变。业务用途是可选创作描述，每次调用独立声明输入槽位与来源映射，仅接口要求时提供额外用途；不能按自由文字猜首帧或自动把外观参考变成首帧。
4. 保存后重新读取本轮来源/选择摘要，按配置审核和准备。多镜保存逐镜核对回执及冲突，未勾选项不改变；原已执行请求绑定只读，后续改选不改写旧请求、费用或审核。

未配置、待选择、失效或来源漂移须按页面提示处理；不能通过删除已有Bible、清空项目、手填不可见素材ID或重试未知请求规避。配方配置变化以新版本明确生效，不覆盖历史版本。当前模块开发与现用环境切换状态见[详细设计检查点](optimization/module-development-detailed-design-2026-09-28.md)。

### ConfigDefinition 与 ConfigRevision

设置页只提交人工修改的 dirty keys。恢复 model/workflow/default 的继承关系使用 `unset_keys`；禁止把 resolved 全量值写成 system override。ConfigDefinition 已被 revision 使用后只允许安全元数据与启停变更，结构合同不可重写。

新增/编辑 ConfigDefinition 先读取 `config-definitions/options`；配置类别由当前设置分区固定，类型、作用域、默认来源、域、单位和影响模块从服务端目录选择，只有服务端明确授权的开放目录才显示高级登记。配置键与类别创建后不可修改；新定义固定启用且无需人工确认。已有 revision 时页面按响应中的 `has_values/mutable_fields` 锁定结构字段，并只提交相对打开快照的差异 PATCH，避免重复字段造成伪版本冲突。

发布 revision 前核对 old source → new source、restart impact 与秘密绑定变化。旧基线草稿、第二个 pending 和无权变更秘密的请求会被服务端显式拒绝；失败时保留当前 active revision，不得在前端伪装成功。

## 7. RuntimeHost probe 与 recommend

注册 host 后执行 probe，并审阅：

- CPU、内存、GPU/加速器与可用显存；
- 已安装运行时和执行器；
- deployment 与 workflow 兼容性；
- 快照时间和错误。

recommend 依据当前主机与资源证据生成建议。接受或覆盖 recommendation 只创建可复核的默认参数配置草稿；不再派生配方或写入 Deployment/Workflow 直连路线，也不自动选择提示词策略或拆分执行实例。执行实例及 schema3 配方必须通过各自正式配置入口创建。它不会下载模型、自动部署、发布工作流或激活配置。迁移0169会阻断仍有旧直连路线推荐或推荐派生配方的数据库，须先在R7清理精确旧依赖，不能把旧推荐改写为新实例。

提交 recommendation decision 时，服务端会重新验证 host snapshot 的有效期、catalog/config hash 和候选当前兼容性。收到 `recommendation_stale` 时重新 probe/recommend，禁止沿用旧草案或仅由前端放行。

目前没有跨节点/多 GPU scheduler。可以启动多个 worker，但 worker 的并发是进程局部参数；运维必须按目标机器容量设置并观察，不得将它描述为全局 GPU 调度。

## 8. 素材库操作

字段选材入口为媒体字段的“选择已有素材”或“选择素材 / 上传本地文件”。“全部素材”保留独立图片、视频和音频；“按系列与版本选择”复用关系浏览，先查询系列、切换主图/变体，再翻页预览确切版本，最后点“选择此版本用于本项”。本项类型及用途来自字段/实例配置；用途属于本次使用，不是素材的全局属性。该弹窗不提供改名、关联、删除或审核入口，也不自动选中最新版本。

选定时重新读取确切版本及原Asset；失效、返回身份不符、停用或类型不符合时保留错误，不替换另一张图。成功只回填现有字段的Asset ID（用途、顺序、使用项标识不变），不更改系列主图或其他输入。上传来源允许时，本地文件先沿正式Asset入口入库，再回填返回的ID；库内来源和上传来源仍由所属策略控制。读取/上传期间字段条件改变则不覆盖新选择，已成功上传的素材保留在库中。结构化媒体字段也使用相同控件，不接收临时路径。执行时仍由原服务核验文件和模型要求，选材成功不等于生成成功。

关系浏览入口为“风格素材库 → 主图 / 变体关系”：按名称、媒体、角色及Bible关联状态查询，查看父主图、同组变体和版本。各列表分别分页，外层筛选不隐藏详情中的其他版本。选择仅改变预览，不改变生产引用或审核采用；读取失败保留确切版本并允许重读，不自动换图。可打开原素材、原任务及确切的生成依据主图（不一定是最新版）。独立图片/视频/音频仍在“全部素材”浏览。查询失败标明数量未取得，不伪称零条。现用环境尚未因此验收。

选中系列后，“编辑名称 / Bible 关联”可修改任意语言引用名；按项目、实体或引用名搜索设定并分页选择，也可明确选“不关联 Bible”。这只改系列元信息，不改原文件、Asset身份、设定修订或旧请求。有版本固定引用原设定时，改绑会显示需处理的关联，不自动覆盖。保存失败保留输入；结果不明先“重新读取系列”核对，不自动重试。

“删除系列关系…”移除指定系列及其版本关系；“解除此版本关联…”只移除当前确切版本。确认弹窗先展示目标、关联数、保留的素材数及子变体/生成依据依赖。仍有依赖则先在关系视图处理，再重读影响；取消不会删除。解除不会删除原Asset、共享文件、Bible、任务或产物，但原关系ID和来源不能恢复。要真正删除原素材，请到“全部素材 → 素材详情 → 删除”；服务端显示仍存在的关系引用，保留其他Asset共用的上传文件。删除前核对范围，不把已解除关系当成已删文件；这些操作不以模拟费用核实为前提。本轮仅隔离验证，没有实际删除现用业务数据或文件。

主图/变体的手动入库入口为“风格素材库 → 添加主图 / 变体素材”。先查询并选择已有系列，或创建主图系列；创建变体时必须明确选择一个父主图系列，名称可用任意语言，不按名称自动绑定Bible。随后通过同一个“选择素材 / 上传本地文件”控件选择图片、视频或音频，确认显示的目标和素材后点“加入所选系列”。此过程不需要剧本或ProductionRun，不调用模型；上传的变体只是关联，不代表它曾参考主图生成。

上传沿原素材入库接口完成。若上传失败，不会新增空版本；上传已成功而关联失败，页面保留原素材并允许只重试关联，无需重新上传。创建系列结果不确定时切换“加入已有系列”，按名称查询核对后再选中，避免重复创建。关联时发现文件缺失、变化或类型错误会明确报错；不要把系列存在当成文件永远可用。基础关系管理与通用选材见本节；主图/变体生成已复用原素材生成表单、精确实例输入和任务入口，版本声明必须明确角色、可选设定及变体实际使用的确切主图，不增加第二套生成按钮或队列。代码与隔离验收不等于现用切换或真实模型验收，最新范围见详细设计第18节。

配方生成主图/变体完成配置的选择后，自动登记到同一“主图 / 变体关系”。首次从原归档物化Asset允许一次复制；后继登记、浏览及原Bible采用复用原Asset和版本，不再复制文件或另建影子关系。关系详情展示生成来源、确切设定及生成依据主图，配方点“查看原生产调用”，独立生成点“素材生成任务”。详情中的“产物状态”只描述产物，实际调用成功/失败与返回正文到原任务查看；登记不代表Bible已人工审核。

图片失败且尚无产物时，在生产运行阶段或准备列表点“查看本阶段原调用”，任务中心按该Run/Stage列出实际调用，再打开目标调用查看原失败返回；不会猜测最新Attempt或用文本修订处理图片。没有实际返回正文时如实显示缺失，不补造回执。重试前先核对任务实际状态与结果；远端结果未知时不可借此链接重新提交。

运营用户在“风格素材库”统一管理图片、视频、音频和剧本。先切换到页面上的目标类型，再从该类型下点击“新增素材”；对话框沿用当前类型，不再重复选择素材类型，只需选择“本地添加”或“模型生成”：

- 本地图片仅接受 JPEG/PNG/WebP；视频与音频必须由 FFprobe 验证、包含对应媒体流并能可靠确定正时长，入库阶段不套用平台统一的 2–15 秒限制。素材用于生产时，服务端在冻结具体 Workflow 后，按其 `input_contract.conditioning_contract` 中所选模式的 `preprocessing` 合同校验时长、帧率与预处理要求。剧本接受 UTF-8 TXT/Markdown/Fountain 或 DOCX，并规范化为 Markdown。
- 模型生成页面统一展示符合任务和输出类型的精确执行实例版本；版本固定运行部署、能力/模式及适用Workflow和提示词方式。可见、已发布、组合验收通过与本次输入可执行分别展示，不能凭单项历史烟测把整个模型标为通过。
- “文本模型生成提示词”创建独立持久任务；成功正文仍需人工编辑/采用，再显式创建素材生成任务，不会串联自动扣费。
- 外部模型逐任务勾选外发授权，预算和次数提醒统一在系统资源策略配置，超额等待人工确认。`uncertain` 表示远端提交状态需人工核验，不能直接重试；staff 必须核对 Provider 控制台、request id 与账单后，通过素材任务的 `resolve-uncertain` 追加不可变结论，网络重放必须复用同一幂等键。页面会展示 request id 摘要、提交/取消时间和取消意图。确认远端成功的媒体任务只恢复原 request 的 collect，费用证据冲突会再次阻断；任务已经取消时只记录费用、绝不入库晚到产物。明确 `failed` 或 `cancelled` 才可新建重试任务。
- 页面刷新后从 `asset-generation-jobs` 恢复 queued/submitting/submitted/running/cancel_requested 等状态。text-generation worker 处理文本候选和剧本，async-media worker 处理异步 API 图片、视频和音频，generation worker 在共享 GPU 互斥门内处理 ComfyUI 素材任务。
- 下载前重新读取 Asset 详情以刷新短期 `download_url`。只有受管文件可以下载；纯远端 reference URL 不伪装为已归档文件。

当页面没有可选实例时，按[素材生成方案配置与验证](asset-generation-model-configuration.md)检查实际组合是否已创建并发布、输出类型是否匹配和权限是否允许。读取本版本输入合同及当前准入原因，核对底层资源、精确组合证据和有效配置；不按名称、列表首项或旧缓存补一个目标。

选择执行实例后，页面按其输入合同展示逻辑槽位、业务事实、字段和参数。请求提交精确实例ID/定义摘要及inputs、parameters、manual_channels等当前字段，不提交节点值、本地路径或裸Workflow/Deployment。策略组装与人工完整通道分别处理，不重复组装；数量、用途、排序、配对和预处理使用同一组规范化绑定事实。原Preparation核验、冻结并授权后交给已有worker；ComfyUI暂存和提交使用原持久回执，远端状态不确定时不能自动重放。成功必须取得唯一主输出、完成媒体与字节校验后才物化Asset。

如果所选实例的输入合同读取失败，点击“重试读取所选实例输入合同”恢复同一版本；“刷新目录”仅重读可选版本列表。重试保留素材名称等基本信息，仍核对版本ID、定义摘要、能力及提示词模式；不会改选其他实例或覆盖已加载的输入草稿。已有提交结果未知时，只能用原幂等请求确认结果，不能借合同重读重建请求。

独立素材入口没有ProductionRun的审核授权上下文。若本次启用的输入需要“已审核色彩脚本”“已审核连续性说明”等生产审核输出，页面显示实际字段名称并阻止创建；应在“连续剧项目”的对应制作步骤使用该实例，或明确选择适用于独立素材的实例。仅可选输入允许取消提供；必填审核来源不能用手写正文、复制审核内容或自动读取整份剧本代替。目录可选和组合Smoke通过不表示该实例适用于所有业务入口。

素材生成与ProductionRun复用同一执行准备和已有底层执行器，但素材任务不创建、不选择也不读取ProductionRecipe。不兼容旧任务随核定闭包清理；当前重试只读取原生实例/准备合同，不恢复旧请求格式或推断遗失输入。

生成图片按人物/场景/道具/通用分类供现有 Bible 与素材选择入口使用，生成视频和音频直接满足现有 Storyboard 参考素材合同。剧本素材在 ProductionRun 页面导入后只形成项目级 preview revision，仍需人工确认。素材生成不创建 Episode、Shot 或交付，也不替代 ProductionRun。

独立素材创建请求首次明确被拒绝时，可修改输入并重新核对后提交；超时、冲突、限流或响应丢失保留原请求。一旦已有未知结果，后续重试即使被拒绝也不能证明首次未创建任务：继续保留原标识和冻结输入，仅核对同一请求，不自动重试。关闭后的迟到回执不再触发页面成功事件；关闭或刷新不是撤销后台任务，也不提供未确认命令的持久恢复。取消更换实例时，下拉选择和原输入同时保留。

独立素材任务或烟测／素材父任务的辅助调用因缺少精确实例烟测而阻断时，在该原调用“准备与调用 → 用相同输入做一次新烟测”读取、复核后执行。新烟测单独记账，不复制外发授权。后台发现仍无完成证据且准备／系统策略未变化时，独立扫描和父任务推进均只安排下一次检查，保留原准备修订、最后完整评估及其时间，不反复在字段检查和阻断之间切换。新证据、策略／准备修改或人工继续仍触发完整准入；父任务的人工继续同样要求其辅助准备重新核对。烟测成功不等于原素材或辅助任务已成功，应查看原任务实际调用和产物，不新建重复请求。

声明式参考策略在分配辅助字段任务前，先用该精确策略的正式渲染器校验输入事实。参考素材的“类别＋用途”必须匹配该策略已配置的规则；人物／场景／道具是否允许空用途由策略决定，不能把通用分类自动猜为道具，也不能将未知用途回退为默认用途。校验拒绝时先修正输入，不先付费生成字段。最终组装仍会重检；若输入或策略合同不成立，原准备明确显示“请求组装未通过校验”，保留字段和真实调用事实，停止自动重查，待人工核对后继续。已知策略字段的语言、必填及内容校验错误同时显示安全字段标识和错误类型，不暴露原始异常、正文或凭据。已保存的原始输入不会被后台改写，技术失败不能抹去已成功的辅助调用或未知费用。

`preserve_source` 对未改变的直通/服务器锁定字段，以该字段冻结来源与装配值的类型和值完全相同作为保留原文的证据，不把项目语言误当所有上游来源语言。比如中文项目可以原样使用已审核的英文影视规则。默认值、投影改写、模型生成/翻译/人工替换不适用此证明，仍按明确语言上下文校验；固定语言、必填、长度和其他校验不放宽。不重写准备来源、已发布策略或历史审核；修复后的未派发准备经正式“检查并继续此任务”恢复。

## 9. ProductionRun 操作

新建连续剧先选择制作方式，再选择剧本来源：

- 无配方人工制作：可稍后填写剧本，也可导入并确认修订；创建项目后同时创建人工 ProductionRun，进入该轮逐步制作，不自动调用模型或采用内容。
- 配方自动生产：选择来源匹配的已发布配方，导入剧本或填写 AI 创意、集数、每集镜数和时长，明确外发授权，查看当前系统资源提醒；创建完成后自动推进到需要输入、审核或处理错误的位置。
- 导入脚本：粘贴或上传后检查 ScriptSource 预览并明确确认修订。已确认且具有确切结构化详情的修订可沿 script_input/confirmed_revision 及本轮Bible批准创建声明分镜；仅确认原文不会补造结构、按字符拆镜或名称匹配，缺结构时先在正式输入页配置输出合同并保存结构化修订；明确配置 script_structure/ai 时仍调用指定实例并审核真实候选，不因内容看似规范跳过调用。

项目页“继续本轮”恢复原配置与事实；“全部制作记录”可选择另一轮，“新建配方生产/无配方制作”才创建独立运行。已采用剧本读取精确修订，不另存项目母稿。项目或导入已创建但后续失败时页面保留确认回执，沿原幂等键重试，不重新建项目；项目创建回执未知时先查列表。

配方装配失败在原运行页使用“重试当前阶段”，确认框列出集号、原装配任务ID、错误及消费输入SHA。仅明确失败集入队，成功及在途集不重跑、选片不改变，不重新生成镜头素材。若提交响应丢失，先重新读取运行与原装配任务；不要以新失败目标代替旧确认。来源、任务集合或输入SHA变化后必须重新预览，未记录旧消费来源不能靠当前选择补造；缺少逐集Link时也不会借重试额外入队。任一集创建或最终事务失败，新任务与新concat整批回滚，原失败任务及文件仍可查。人工制作仍通过原人工命令回执恢复，不改为配方入口。

配方启动/文本完成回调与维护 worker 共用原执行器。维护周期和租约仍由 `runtime.platform_maintenance_poll_seconds`、`runtime.platform_maintenance_lease_seconds` 配置；日志 `recipe-runs checked=N` 表示检查了多少运行，不代表模型成功数。0171 只增加运行清单索引。重启前检查活跃配方及模型任务：queued/running 配方将在服务恢复后继续；待输入、待审核、阻断及人工运行不自动恢复。阶段前异常显示 `production_auto_advance_failed`，应查看服务日志和原运行，不应通过重复新建绕过问题。运行前检查：

- 项目和内容事实正确；
- confirmed script 可用，或选择 AI script；
- AI script 和确定性 Bible 均完成人工审核；
- 需要参考图的配方含 `bible_image_generation/bible_image_review`，并已配置可用的精确图片执行实例与其提示词策略；
- workflow 已 validate、real smoke、publish；
- deployment/host 可用；
- resolved config 与 run snapshot 可追踪；
- route 与成本币种/授权明确；
- 受管存储可写。

配方生产入口显示所选schema3配方中各阶段冻结的精确实例版本、摘要与输入配置，不另选整集/逐镜工作流或PromptProfile Binding。选路方式和素材来源由配方声明，在对应步骤解析；不会把旧顶层参考视频数组自动分配给每个镜头。无人值守仍需明确确认配方审核策略。完整配方按合同发布和创建，列表配置摘要不代表本次执行准入或真实Smoke通过；实际服务切换情况以改造计划当前记录为准。

创建页区分配方读取失败、没有可见已发布配方、尚未选择、所选版本已不可用和所选配方模式无效。空目录时可到系统设置配置并发布配方，或明确切换无配方人工制作；不自动切换制作方式或绕过创建校验。视频输入模式目录只核验可见视频候选，不把无关文本／图片实例当作视频依赖；但配方实际引用的所有实例仍须逐项通过完整合同核验。

同一项目创建页的“重新读取”更新配方目录和当前能力合同，不重置已编辑的集数／镜数／时长、选集或主动清空的配方选择。首轮成功读取才初始化选择及默认参数；能力读取失败后可重试初始化，但不会用新默认值覆盖已有草稿。已选配方不再可见时明确阻止创建，不能自动换成目录中的另一份；切换项目会清除上一项目的创作输入、配置覆盖、来源确认及外部调用授权。页面草稿保留不代替提交时的服务端权限、合同和资源核对。

### Bible 参考图审核与逐镜绑定

文本 Bible 只定义人物、场景和道具的语义事实，不等于已经具备一致性图片。Ref2VA 的正式顺序是：

1. `bible_image_generation` 使用本 Run 冻结的图片模型、route 和 Bible PromptProfile 为各实体生成模型候选。
2. 人工/辅助运行到达 `bible_image_review` 后，可直接选择模型候选，也可从素材库选择同类型图片，或混合选择两类来源。素材库选择会冻结页面看到的 SHA-256；文件或来源变化时必须刷新后重审。
3. 每个实体至少选一张、模型与素材库合计最多 32 张，并且只能指定一个主锚点。主锚点可以来自模型候选，也可以来自素材库，但必须属于已选集合；同一最终 Asset 不能同时用于多个实体。
4. 对模型结果不满意时，在当前实体点击“使用冻结 Bible 模型重新生成”。该操作只在人工/辅助运行的 `bible_image_review/awaiting_approval` 可用，每次给所选实体追加一个候选，旧候选不覆盖；每实体同一 Run 最多 8 个。未知费用会重新要求授权，不能沿用首次生成的未知费用许可。
   支持种子时，新候选首次准备按冻结合同的合法范围/枚举/对齐抽取一次，排除本阶段该实体先前已用的种子，并保存实际值和来源。刷新、同一操作重放及失败重试不重抽；普通默认值可由此明确新候选请求替代，固定种子或步骤锁定不绕过。有限域缺失、锁定或合法值用尽会在调用前明确失败，需在正式配置入口调整新版本，而非修改历史任务或发布合同。不支持种子时不虚构参数；新种子不保证画面质量或明显差异。
   新候选生成期间，尚未作决定的图片审核输入/输出恢复为未冻结状态；生成完成后重新冻结包含全部候选的前置合同，再进入人工审核。此过程不清理配置或批准记录，不把空对象当作已冻结输入；已产生审核决定的阶段不能重置。若已生成图片但审核恢复报合同不一致，不要再次生成图片，应保留现场并修复对应状态后，通过原运行“重试当前阶段”复核。
5. 图片审核通过后，在 `storyboard_review` 的“分镜审核与逐镜参考素材”按集、镜头展开。完全空白的逐镜绑定按明确人物、场景、道具关系带入上游已批准的唯一主图；已有绑定（包括明确空列表）优先，不猜选其他候选、首尾帧、视频或音频。每项显示缩略图、素材名和短ID，可以改选或取消；图片缺失、摘要/MIME变化或重复引用会明确提示并阻止审核，不自动替换。轮询和“刷新素材”保留本页修改与展开状态；草稿未提交前不写入审核事实，不承诺浏览器重载后恢复未提交修改。
6. 展开“查看本轮路线的输入数量与来源要求”核对当前精确实例合同与配方来源组；图片、视频、音频是否必需及数量/用途按合同判定，不按 Ref2VA 等模式名强制添加。通过本阶段正式审核才保存完整逐镜引用；`媒体主执行准备` 再次检查审核快照、Shot关系、Asset身份和完整性，缺失或越权引用保持 blocked。未映射项可保留登记，但不偷偷加入其他输入槽位。

“查看逐镜输入映射”中的预览是只读检查，解释上游来源如何进入该阶段、镜头、候选的输入槽位和上下文；不创建任务或调用模型。分镜尚未审核时只能查看冻结的来源设置，不能把页面草稿显示为已批准的执行输入；审核后可选择阶段、镜头和候选预览最终映射。正常轮询不折叠面板、不清空选择；真正审核或选路变化会使旧结果失效并提示重读，已有执行准备的实际输入仍以原准备详情为准。

若媒体阶段尚无执行准备，原对白没有voiceover而参考输入要求该标记，可展开“对白缺少画外音标记？核对受控输入修订”，先预览精确修订，再勾选规则确认并记录。这是明确写入操作：缺失标记按普通台词false转换，显式画外音不变，新增唯一审计；原剧本、审核、RunPlan及历史产物不变，不自动重试或生成。应用后重新预览镜头，查看修订ID、实际补值来源证据及生效设置。已有准备/调用不能用此入口改输入；映射通过后仍应核对剩余预算与准入，再决定重试当前阶段。

全自动运行不开放人工“重新生成”或素材库选图。策略要求每个实体恰好一个本 Run 当前模型候选，自动把它物化并设为锚点；候选为零、超过一个、字节/provenance 不完整，或 Shot 未使用全部已物化 Bible 实体时均阻断，等待修正配方、内容或运行输入后新建合规 Run。

所选 published recipe 必须已经通过完整 content → storyboard → prompt → media → selection → assembly → delivery 闭包校验；不要以缺阶段的“部分配方”启动正式 run。

运行中只在 ProductionRun 执行 approve、route selection、cost authorization、continue、artifact selection、retry、resolve-uncertain、cancel 或有序 fallback。短剧 Tasks、Deferred requests、Assemblies、ReleaseBundles 完全只读，没有任务级写例外。独立的素材 `AssetGenerationJob` 不属于上述 Task 投影：其取消、失败/已取消重试和 staff 人工消歧在“任务中心 → 素材操作与媒体诊断”提供，成功产物或提示词候选返回素材库；不得把这些动作套用到短剧 Task。

“任务中心”（`/workflows`）统一按全部、项目生产、素材生成、Smoke 验收筛选；可叠加项目、精确实例版本、操作能力、状态、创建时间和“仅关联异常”。列表、计数和翻页由服务端执行，不下载全部任务后在浏览器拼接。列表行代表真实根准备或原生诊断记录，调用次数在详情单独显示，不将两种数量混用。设置中的实例版本“执行历史”、烟测“查看输入、输出与烟测历史”均进入同一列表；Smoke 仍仅管理员可见。

详情的“刷新此任务”读取期间保留键盘焦点，但不接受重复激活；读取失败会保留上次结果并明确显示尚未更新。弹框内 Tab／Shift+Tab 只在可见控件之间循环；Escape 仍受忙碌及未保存内容的关闭保护。输出校验标题、原错误码、说明和警告纵向展示、允许长标识换行，不隐藏失败或把执行成功解释为自动采用。

任务成功表示对应执行完成，不代表已经采用或封存；模型也不保证严格遵循提示词。核对“输入与实际提示词”的冻结值和实际接线，预览产物以确认文件可读取／可播放、可供后续流程使用；不将画面、动作、声音的创作质量或提示词遵循度作为本轮技术验收门槛。需要采用时仍从本轮生产审核入口操作，技术验收通过不会自动生成采用决定。产物卡片的公开副本状态（已验证、开发母版、失败、待生成）互斥；“已保存到素材库”是另一独立事实，不能遮蔽副本失败，也不代表已选为生产主图或已封存。披露标签由`delivery.ai_label_text`与原执行模型身份组装，修改当前配置不会纠正既有副本清单；不得以标签中的自由文字推断真实执行模型。

模型运行部署和能力关联与验收中心共用的“能力烟测”，在存在最近任务时显示原任务标识、创建／完成时间及已记录诊断码；“查看最近任务输入、输出与记录”按精确任务标识进入任务中心，不重新提交。最近任务可能仍在执行或与上方验收快照不是同一次，不用快照ID、部署ID或策略ID猜测任务。没有任务／缺少标识时不生成历史链接，不能以组合Smoke冒充部署资源Smoke；原任务不可见或没有对应准备时按现有权限／历史事实显示空结果，不改查其他任务。

人工工作台的“步骤开放”仅表示可继续生成候选；旁边分别展示主／辅助调用的真实状态和次数。使用“刷新制作状态”重新读取，或“查看已有准备与结果”进入本轮记录；这些动作不会重新生成。“新建…候选”才创建新的准备。剧本来源 ID、已确认修订 ID、采用决定 ID 是不同对象；已采用的相同修订不能重复点击采用。已经规范化且通过审核的 AI 剧本不必再执行一次结构化。

已有项目在“人工制作台 → 剧本 → 导入／编辑项目剧本（不调用模型）”打开项目剧本管理（`/projects/:projectId/production?view=scripts`）。此视图与配方导入共用同一导入和修订入口，不要求选配方，不读取视频生产配置，也不创建运行。粘贴 JSON 时格式选“纯文本”，生成服务端预览后在“JSON 结构化结果”显式选择已发布输出版本并解析；原稿和自定义字段原值保留。输出目录分页读取，目录失败明确报错，不自动选择版本。“按字段编辑并另存 → 另存预览修订并解析”使用本修订冻结的字段合同，创建新预览而不改旧稿；这些动作不调用模型、不自动确认或采用。自由文本直接 JSON 解析会报格式错误；需要 AI 时回到原制作台明确选择结构化实例。确认剧本和本轮采用仍是分别发起的操作。

项目“新建一轮制作 → 按配方生产”使用已确认剧本时，目录不会自动选用旧版本：在“已有版本”明确选择并读取正文，或先粘贴／上传／导入素材库剧本生成预览并确认。切换来源、修改正文或格式、换文件或素材会立即清除本次预览与选择，须重新预览确认；不改写已保存的剧本和历史采用。详情读取失败可点“重新读取所选剧本”，不能依靠目录中的“已确认”标签绕过正文核对。创建运行仅提交当前可见的已确认版本；已发出但结果不确定的创建仍保留原冻结请求，不因修改表单生成另一意图。

Bible 场景／人物／道具参考图：进入“Bible 与影视规则 → 新建设定参考图生成候选”，明确制作对象并选择声明主体输入的策略组装实例。“本次生成主体描述”为空时自动带入本轮采用剧本所对应的已审核 Bible 描述，不用另写整段提示词，也不把完整剧本塞入请求。可直接审核、修改或补充本次副本；“查看引用原文”用于对照，“使用本轮主图共同身份”与“使用本轮主图基础外观”分别读取原审核中的确切主图设定，先预览、确认后才替换现有内容，不自动拼成一段。页面显示对象、Bible 审核修订和主图设定修订，保存时冻结原文、来源和实际输入；不修改剧本／原设定，不覆盖旧准备。缺主图、空白设定、读取失败、缺审核或审核变化会明确提示，不回退到示例或过期内容。取消勾选的可选字段不参与提交；启用的已审核字段仍须确认本轮来源。点击“保存生成准备（不调用模型）”后继续核对字段与实际请求，尚未提交执行不代表已经生成图片。 此处描述开发代码入口，M3现用切换未完成；旧视觉设定建议及旧凭据不兼容，新配置需显式选择上述字段来源，不能把目录更新视为已经发布业务配置。

“人工制作 · 创建执行准备”面板打开期间暂停整页运行状态轮询和整页重新读取，避免网络失败卸载正在填写的表单；打开前发出的整页读取回执也不会替换该上下文。面板内的来源预览、准备读取和用户明确提交仍正常执行，保存／执行时仍由服务端核对权限、版本及准入，不因暂停刷新放宽校验。切换制作步骤、归属类型、对象或执行实例时，如已有草稿会先确认；取消后下拉框显示、正文和来源引用均保留原上下文，确认后才清空旧草稿并读取新上下文，不自动选择另一实例或创建任务。关闭面板后恢复整页轮询。来源读取失败时已填写正文与原引用保留，恢复连接后可点击原来源按钮重试；浏览器刷新／关闭页面并不承诺保留未保存草稿。

“选择参考图与唯一主图”面板保留打开时的审核动作依据。关闭或按Escape后，可在同一制作页点“继续参考图与主图审核”，恢复所选图片、主图、说明及原待核对请求；重开不自动读取或提交，会显示原读取时间及恢复提示。审核打开或仍有未提交／待核对内容时，整页读取与Run轮询暂停，页面明确提示上次状态；面板关闭且无待处理内容才恢复，避免整页读取失败卸载草稿。面板内的主动读取仍可用，服务端提交仍核对当前权限和版本。主动“重新读取候选与当前关系”在有未提交选择／说明时先确认，取消不改变草稿；读取失败保留上次结果，仅供核对并禁止审核写入，完整重读后才恢复。首次请求明确被拒绝时允许重新读取；响应未知时保留原请求和幂等标识，暂停候选刷新，只能明确点击“重试同一审核请求”核对，重试仍是提交原命令而非只读查询。此前未知请求的重试被拒绝也不能证明首次没有写入。页面内离开和浏览器关闭／刷新会提醒未保存风险，但浏览器刷新或离开后的未保存审核／原请求不持久保存；未知结果请先在原页核对，不另建请求。切换制作轮／项目／冻结计划不把旧回执带入新上下文。

影视规则候选审核中展开“本次冻结的输出合同”可查看原生产输出版本ID与SHA；逐字段展开“响应 → 审核 → 下游来源”核对响应位置、格式、转换及来源标识。字段来自原请求，不随当前配置改动；名称里的“已审核”只表示批准后可用的来源，不代表候选已采用。创建下游准备时，“读取本轮已审核规则”也展示原生产输出合同。未审核、凭据错配或合同缺失不能确认引用；历史缺合同结果仅供查看。上述读取均不运行模型、不替代人工判断。

分镜页“分镜与独立字段”只编辑标题、生成类型、时序、原对白及明确关系；动作、运镜、声景、配乐等从原结构化字段按需查看，并在“独立字段 · 人工修订”另存，不再填写固定narrative等旧列。没有结构化来源时先在正式剧本页填写/解析并创建分镜，或使用本次执行合同允许的人工输入；不能把缺来源显示为空成功。字段另存不代表审核或已发送给模型，已有请求保持原冻结值。旧接口参数返回400；正式现用模块试用须以M5-09切换记录为准，本段只有开发与隔离验证。

剧本／影视规则候选清单刷新失败时，保留本页已读正文、审核说明和未提交的字段编辑，明确标为上次内容并暂停提交；第一次读取失败不显示“没有候选”。点刷新重读，只有生成步骤、候选ID、内容与manifest摘要及状态均一致时才保留原选择与勾选；候选变化须重新选择核对，说明仍保留。人工审核依据（当前剧本或规则审核修订等）变化时，即使候选不变也会取消旧勾选，保留草稿并提示重新审核。详情读取失败或身份不符时不提供可提交候选，请先刷新清单再选择；相同内容摘要不能代替候选ID。人工审核请求结果未确认时禁用清单刷新，仅可明确“重试同一审核请求”核对原命令，不能清除后另行采用。此保护只覆盖当前已挂载面板的候选读取，不承诺浏览器刷新、离开页面或整页重新加载后的草稿持久恢复；读取和恢复均不代表采用。

新文本 Smoke 即使格式校验失败，也会在“输出与校验”展示原始返回和具体诊断；历史未保存正文的记录仍明确缺失，不能补造。响应文件与原 Smoke 数据库结果共同备份、保留，不应单独清理 `execution-responses/`；文件缺失或摘要错误会显式报错。正文只通过有权限的任务详情脱敏读取，不能直接发布文件地址。修正配置后须明确新建烟测；失败结果不会自动重跑或被人工修改为通过。

完整冻结请求存储在同一正式文件存储的私有`execution-requests/`，Preparation及生产Attempt仅持有同一对象manifest；正文按需验真，不走公共媒体URL。`execution_request_storage_invalid`表示请求尚未冻结／提交，检查正式存储可写、可回读及生效的`security.snapshot.stage_max_bytes`后，回到原执行准备“继续”；不要为规避存储错误裁短已确认字段或重新调用辅助模型。冻结后文件丢失／损坏须恢复与原manifest一致的文件，不能从当前页面或策略重建历史请求。备份、保留及0194切换边界见[备份与恢复](backup-restore.md#冻结请求存储维护)。

详情默认打开“输入与实际提示词”：分别查看提交输入、解析后的实际参数及来源、按角色／通道展示的最终提示词。最终内容只读自该次冻结请求，不使用当前策略重新组装；尚未冻结或记录校验失败时明确提示。超过 64 KiB 的字段按需加载完整脱敏正文，不截断冒充完整输出。“输出与校验”区分调用／格式通过与人工预期；“准备与调用”查看原主／辅助调用，并恢复原准备的正式操作。历史调用可单独查看输出，人工补全结果保留确认来源；拒绝候选仍可审计，不因此变成可采用内容。实际费用未知不显示为零。

运行请求不再叠加固定64KiB／256KiB上限：0240移除准备intent/resolved_inputs的固定大小约束，应用层与请求文件共用现有`security.snapshot.stage_max_bytes`配置。配置定义、审核及结果合同边界不随之取消。新辅助请求把同单元重复资料写入`shared_facts`并显式引用；正文和规则不截短，不强制拆成多次调用。部署前使用正式备份入口，在无在途调用的窗口沿`restart.ps1 -SkipComfyUI`迁移并加载平台；不重启ComfyUI。迁移不转换旧批次、不改既有摘要、不自动清除阻断或重发任务。旧批次保留只读原始消息，新执行必须重新通过正式准备流程；撤销迁移前若已有超过旧边界的准备，应恢复匹配备份／代码或明确处理数据，不能为回滚裁剪事实。此调整不是恢复已取消的整套准备存储重构。

生产运行详情及取消／重试确认中的“已记录实际”是服务端已确认金额的小计，不是全部调用的最终账单；小计为0不表示免费或全部结算。未知费用仍需到原调用详情核对，不能用预估、承诺金额或token用量替代实际账单；页面不重新计算或修改已有费用事实。

“准备与调用 → 提交证据”显示当前选中调用的本地边界时间、提交登记时间和隐藏正文后的请求标识状态。登记时间可能由恢复或人工核对写入，标识可能在发送前分配；它们不是网络发送时间或供应商收件回执。“尚无记录”也不证明从未调用。展开“这些记录能证明什么？”查看边界说明；字段时间倒序或摘要异常单独提示，不改原任务状态、不自动重试。此区不含实际HTTP正文，不能据此确认Smoke、费用结算或人工采用。

“来源 → 策略字段 → 通道 → 冻结请求”沿原显式来源接线展示。比对层以每项路径／说明为准：文本消息、ComfyUI已编译语义输入或REST供应商适配前的标准体；“与冻结请求对应层一致”不代表物理节点／供应商重映射已验证，更不是送达、Smoke或人工采用证明。媒体声明／身份不完整显示不可核对，声明位置缺值或正文不同显示不一致；不会按相同正文搜索节点或重新组装历史。未知适配器不会隐藏已有完整字段来源。只有已有策略编译审计可提供策略追溯，人工通道仍明确没有策略装配记录。

已冻结聊天任务可展开“请求协议与配置归属（只读说明）”，查看该协议的system／user职责及配置位置。影视规则区分平台输出格式、策略系统通道和user内策略／结构化字段；剧本结构化明确全文属于本次业务输入，规范化明确系统通道作为user数据，辅助字段明确规则来自父策略而非辅助实例再次配置。说明按冻结协议身份匹配，未知版本不猜测；它是规则说明，不是原文校验、真实Smoke或送达证据。修改配置须使用新版本及新任务，不改写旧请求，也不能以今天的规则断言旧任务未发送全文。

在“系统设置 → 执行实例 → 管理业务输出版本”配置页，先选三项基础响应格式，可按需启用“已审核镜头指导”，再填写“响应字段位置”；例如`visual.palette`表示对象字段，`rules.0.summary`表示rules数组第1项的summary，不是读取项目或文件。最多4层，对象字段每层1–64字符；数组下标从0连续完整声明，不留空或混用对象字段，不按返回内容搜索或重排。不需嵌套或改名时使用“使用所选格式的字段名”。页面同时显示响应位置→审核业务字段→`reviewed_visual_rules.*`，只有人工采用后才能引用。重名、父子位置冲突、保留字、非规范／断档下标、通配符或不完整映射会阻止保存／预检，原输入保留；样例中间对象不接受额外／缺失字段，数组不接受额外／缺失项、null或对象替代。同类型正文仍需人工检查是否对应正确槽位。新建草稿可展开无调用样例预检，已有版本需先保存后按精确修订校验。修改位置不改变内容类型；启用镜头指导后必须返回并审核完整非空英文文字，只存入本轮审核结果，可通过 reviewed_visual_rules.camera_guidance 引用，不写回项目三项规则，已发布版本只能创建后继并由新执行组合明确选择。格式通过不等于真实Smoke、模型质量或人工审核通过。

业务输出的“解析与处理规则”中，剧本语义先展示“响应结构 → 业务含义 → 下游使用”，字段标题和映射方式来自原目录，精确路径及嵌套规则按需展开；这只解释配置，不表示已有解析或模型结果。公共结构下钻返回后保留本页草稿。

语义映射存在未完成的局部数值输入时，已展示或在途的旧预览都会失效；更换响应结构前先说明局部输入影响，取消保持原内容，确认不自动转换已有映射。影视规则的“清除此草稿的质量建议”和覆盖已填样例正文改为页内影响提示，继续编辑不改原值。新建议短语须添加或清空后保存，避免只保存旧列表。质量建议、允许取值、响应位置和公共结构各自按原职责维护，不会因本页操作改写已发布版本或历史结果。

共用字段映射编辑器中，切换已有取值方式、移除字段／列表项或更换会清除逐条映射的列表来源，先在当前字段内显示影响提示。“继续编辑”保留原下拉选项、路径及嵌套内容，明确确认后才应用；待确认期间不能保存、发布输出或预览旧装配映射。来源目录或配置在确认前变化时取消该待确认操作，不把旧选择应用到新配置。这些是前端草稿操作，不修改公共结构或已发布版本，也不执行生产任务。

最终提示词逐段及合计展示 Unicode 码点数、UTF-8 字节数，仅计有权查看的安全过滤后正文，不含角色、JSON 包装及传输开销，不代表原始请求大小、token 数或费用。合计保留每段，不因相同内容扣减。整段非空展示文本完全相同时列出对应角色／通道供检查；比较包含空白且不归一化，不检测局部重复或语义相似。安全过滤或有意复用均可能产生相同正文，不能据此判定重复调用或删除内容；页面不修改请求与状态。明确空段计零、无记录明确说明，正文待完整加载时不计算或显示为零。

任务详情与列表按当前可见状态刷新，编辑、提交或加载完整正文时避免覆盖用户操作；刷新失败显式保留“上次读取”标识。ComfyUI 节点进度仍是采样遥测，不代表整个生产轮次已完成。查看和刷新均不发起生成、重试或采用；付费／外部真实烟测须另行明确发起。

schema3 Recipe 的自动推进遵循本轮冻结 RunPlan 和真实前序产物：worker 在文本、媒体或装配终态事实提交后幂等续接，遇到必需审核、明确路线选择、成本授权或候选选择时等待正式操作。无人值守只执行已登记且冻结摘要的审核策略，创建时须明确授权；媒体选片策略仅在每组有唯一、来源和字节证据均有效的候选时写入选择与 ApprovalDecision，两者同事务，失败一并回滚。动态素材数量与类型以精确实例合同为准，不把项目全局参考广播给所有镜头。提示词的语言、长度、占位符及保护文本约束来自精确策略字段和本次请求；没有额外的全局英文门禁，也不因缺字段隐式调用模型。异常、费用和远端不确定状态保持显式阻断，策略决定不能伪装人工批准，后置推进异常不改写已持久的 worker 事实。

每次外部调用仍需显式外发授权及可核对计价。预算和调用次数来自当前系统资源策略，超额或币种提醒未配置时保留准备并等待人工确认，不直接判定失败。确认不提交模型，另点继续才重新检查；实付超出估价时保留结果和账目，提示人工核对。未授权、未知计价、币种冲突和提交不确定不因确认业务额度而绕过。排查时通过 ProductionRun、StageRun、GenerationAttempt、route/cost event、PromptProfile 冻结引用和 artifact provenance 对账；不将密钥、完整提示词、远端响应或签名 URL 复制到日志/审计正文。

Comfy任务在“输入与实际提示词 → 工作流接线与媒体回执”查看“提示词／参数 → 声明节点端口 → 原请求核对”。页面只读取历史引用的精确工作流及其冻结Projector版本并核验合同摘要，列出语义Key、节点类及node/input；一对多绑定逐项展示，值仍在原参数／提示词区。标量范围不含媒体／模型文件和内部输出路径。版本缺失或摘要变化显示不可核对，动态新增／替换节点不猜测，原媒体用途与暂存回执仍保留。接线来自模型关联的精确工作流版本随导入冻结的“语义绑定”，不是任务页可编辑配置；结构变更须重新导入新版本，不能修改原历史来消除不一致。端口匹配仅证明原请求值对应，不证明节点执行、GPU送达或Smoke通过。

受限REST任务在同一输入页展开“REST 标准字段 → 供应商字段（非送达证明）”：查看标准字段对应哪个供应商JSON顶层字段，以及按原值属于进入正文、缺失／null省略或未配置映射。0、false和明确空字符串不按缺值处理，实际请求校验仍须满足。映射只在精确部署配置摘要与原合同一致时显示；缺失／漂移／校验失败均说明无法核对，不拿当前配置补写历史，也不隐藏原参数和提示词。配置位置是模型运行实例传输配置的request_field_map，覆盖服务连接配置；任务页只读，不提供第二份编辑入口。请求头与凭据不展示，生产派发幂等标识不等于准备ID，不能把映射一致当成已发送、送达或烟测通过。

原生影视规则与剧本任务可在同一输入页展开“业务输入 → user 消息（原记录核对）”。影视规则列出正／负向通道；剧本生成还列梗概／生成要求、风格、集数、镜数及时长；结构化列剧本全文、来源身份、行号、原文时序与已配置规划。每行给出原冻结计划和最终user JSON位置，区分一致、不一致、部分可核对及缺少证据；额外未解释字段单独计数，请继续查看原消息。旧消息若确实发送字段审计副本会单独列出，不静默去重。此处不重新读取项目、填默认值或调用模型；system平台协议正文仍须查看原始消息，不能把业务输入对应一致当成完整协议或送达验收。

独立规范化在此处还核对完整条目批次、保护词转换、返回骨架、策略标识／版本和策略数据。原始保护词在发给模型的消息中成为服务器占位符，来源路径会注明转换；所有条目的顺序、request_id、语言、正文与上下文均参与核对，并非只看首项。返回骨架中的空translation是待填模板，真正输出仍在“输出与验证”查看。策略系统约束在此协议中作为user数据，不是另一条system指令。缺少原通道或版本不补默认；不支持的上下文、损坏或超出只读遍历深度的记录明确不可核对，不重跑原任务。

上述四种原生文本任务及字段生成／翻译辅助任务还可展开“system 消息分段来源（组装记录核对）”。新请求显示平台协议、输出合同／允许值／建议、策略系统通道及可选规划各自的位置和来源；整条及分段摘要对应才显示一致。字符范围从1计数、包含段标签及换行，不是Token数，脱敏正文可能改变可见长度。旧请求未保存布局时显示“来源无法核对”，不会重跑、补写记录或按今天的代码反推。此处没有第二套提示词编辑入口：业务规则仍在各自版本配置维护，平台安全／传输格式不能在追溯页覆盖；字段生成／翻译显示平台固定返回协议和字段消费约束，具体规则请在同次user批次的field_contract查看，不是另一份system策略。其他协议未记录布局，不能外推已经支持。

### 声明的预处理与实际记录

任务中心展开一项素材使用，在“上传前处理 → 声明参数与原处理结果”分别查看冻结处理声明、适配器记录的有效值及处理后探测值。时长按毫秒、帧率按fps、帧数按帧显示；0、否、缺字段和未提供确定值不会混为一项。这里只读取原记录，不在查看历史时重新探测或调用模型。

合同来自原执行组合冻结的模型素材模式，编码参数来自同次冻结的`reference.media_preprocessing_profile`；任务页不提供第二份编辑配置。内嵌原音明确显示共用哪次视频处理，不算独立转换／上传。字节摘要相同／不同仅说明原来源与提交字节的关系；回执缺失、元数据版本或摘要无法核对时明确提示，不用声明值补齐。图节点内部算法、模型送达、Smoke和人工采用仍需各自证据。

### 逐次素材使用的图路径

在任务中心“输入与实际提示词 → 工作流接线与媒体回执”展开一项素材使用，再展开“素材入口 → 声明消费端口”。同一素材的多次使用分别显示；视频内嵌音频保留自己的声明目标和实际输出索引，不与视频用途合并。可查看原入口、目标节点输入和按序列出的原图连接。

“原图连通”仅证明一条最短路径存在，不代表全部转换算法、执行顺序、已上传或送达；“原图未连通”不是模型执行失败结论；“目标无法核对”表示缺少精确语义合同，不能拿当前版本补充历史。共用完整原图仍可展开查看。任务页只读，不修改历史接线、费用、审核或任务结果。

### 媒体路径节点的参数差异

在同一接线与回执区域展开“完整冻结图节点与连接”，再展开具体节点，可查看冻结请求的端口值、准确graph位置，以及它相对精确工作流原图是相同、变化、新增还是不可比较。无参考素材的文生图同样可查；上游节点及未连通节点不会因没有媒体入口而隐藏，节点存在不代表已执行。固定值在工作流导入时冻结；绑定参数／动态接线由原模型、Projector与执行组合共同编译。任务页只读，修改应通过相应新版本完成。

0、false和真实空值分别展示；“敏感或路径内容已隐藏”不是空值，“本次素材占位引用”应结合上方素材身份与回执查看。节点类型替换或原基线缺失时明确无法比较，不用当前节点目录推测历史。这里仍不是节点内部算法、模型收到的请求或Smoke成功证明。

## 10. 媒体执行与上传边界

T2I/T2V/T2A 的目标环境验收必须覆盖 submit、poll、结果获取、managed archive 与 provenance。仅请求校验或 mock 不能作为 real smoke。

I2V/reference image 需要 provider-specific upload contract。当前标准执行器没有该契约；遇到 `provider_upload_contract_missing` 应保持 blocked，登记 provider 上传/引用格式后再开发和验证。禁止丢弃参考图、发送纯文本任务或回退旧执行器并显示成功。

ComfyUI素材任务与ProductionRun使用相同的实例逻辑输入映射：固定/动态素材按当前合同、实际用途和配对事实解析到冻结工作流端口。模型模式由精确实例固定，不由素材请求另选旧CONDITIONING_MODE。两者复用Asset授权、编译、staging和preprocessing，但分别保存自身准备及任务事实。async REST仍受已登记上传/引用协议限制；两条路径都不接受客户端本地路径、上传名或任意节点值。

## 11. 受管产物与逐集交付

远端输出进入装配前必须：

- 通过地址、大小和媒体类型限制；
- 完成下载/读取；
- 写入 managed storage；
- 建立 generated artifact、attempt 和 provenance；
- 完成可播放性/结构校验。

归档时还会从冻结 task/attempt/stage、route、workflow 和 configuration 事实白名单固化 `comparison_parameters`。操作员比较候选参数时只读该字段；manifest 的任务 ID、对象键、输出索引和 hash 仅用于身份/provenance。

装配每次生成一个 episode master，并记录 source items。ReleaseBundle 汇总逐集 master 并执行 verify。系统不生成整季单条长视频；缺一集、来源不完整或 artifact 不可验证都必须阻断交付。

连续剧项目默认先展示**制作版本列表**：一个项目可以有多个独立 ProductionRun，列表显示固定版本标识、创建/结束时间、制作方式、状态与成果。即使只有一轮，也需显式选择；制作版本不等于配方版本。现有合同没有制作版本自定义名称字段，不将列表顺序伪装成 v1/v2。

选择版本后，按“剧本、视觉与术语、Bible 与参考素材、剧集与分镜、媒体与选片、装配与交付、任务与记录”查看该轮内容。剧本下分“创作输入与原文／结构化剧本”，视觉与术语下分“全剧视觉规则／统一名称（Canonical）”，不再把这些二级内容与制作环节混列。人工导入读取原稿；AI 起稿区分创作要求与已采用剧本的实际冻结提示词。结构化剧本以人物、场景、道具、动作、对白和镜头引用展示；其他已配置字段按结构标签只读展示，不默认倾倒 JSON。分镜按保存的剧本来源关联，展开可核对原请求及已选视频；缺失、冲突或不可访问会明确提示，不按名称或当前项目值补齐历史。

“统一名称”展示本轮保存的实体/主图/变体参考名称；未冻结的项目英文 canonical 术语表不会冒充该轮词表。项目工作台沿用封面、风格标题区、标签与卡片，在同一页面展示制作版本，不再另设“项目公共设定”整页入口。

统一操作入口为 **生产工作台**，复用原项目输入页的渐变标题、横向栏目和卡片样式。人工工作台、配方工作台、只读版本共用一份七栏目目录，统一名称、顺序与内容归属，不复制生产逻辑。人工版本保留逐步导入、生成、采用、审核；配方版本展示对应阶段与产物，按冻结配置推进，仅在配置要求或异常时提供处理操作，未知阶段可在“任务与记录”查看。没有对应阶段或产物时明确提示，不为补齐栏目创建任务或强制 AI。多版本先显式选择要继续的版本，不按名称或列表顺序猜测。

生产工作台与“查看本版本内容”共用渐变页头、字体、横向栏目和内容卡片，页头分别标明“可操作”与“只读”。只读版本页保留原轮次的输入、剧本、素材和成片查看，不出现项目输入编辑、上传或保存入口；需要操作时通过“进入生产工作台”切换到同一 Run 的对应栏目，返回仍保持版本与一级栏目。媒体记录按需读取原文件，候选状态不冒充已选用；任务记录只展示保存状态及原因。工作台按制作方式保留适用操作，不把当前编辑值当作历史快照。

原视觉规则、正文翻译候选、Bible 登记、主图/变体、参考图绑定和剧集/分镜编辑继续复用原保存能力。保存当前项目输入不会自动调用模型、采用或审核，也不改写历史 Run 输入与成果。栏目切换保留未保存草稿；编辑或保存期间暂停制作操作与自动刷新，保存失败保留草稿并在页面提示。返回版本内容、剧本导入管理及手动分镜往返均保留明确 Run；离开未保存内容前提示确认。

“视觉与术语”展示**原名称与设定**：人物、场景、道具保留原名，可查找并按准确实体打开现有主图／变体设定。这里是项目当前输入，不是本轮冻结名称；本轮名称仍在只读版本中核对。名称和参考素材按实体、设定版本及实际输入位置关联，不以英文译名为前提。工作台不提供名称译表编辑、缺英文名待办、名称候选生成或写表确认；已有词表和历史候选不删除，也不自动套入本轮。字段流向面板不再将旧名称词表列为当前可编辑字段。**正文翻译候选（按需使用）**保留独立的描述正文处理能力，首次展开才读取记录；确认只更新候选，不改原文或表示本轮已采用。生成仍须显式选择执行实例，展开页面不会调用模型；旧翻译的术语约束报错如实显示，不引导用户去已删除的名称入口。

在“生产工作台 → 视觉与术语 → 全剧视觉规则”展开“字段定义与流向”，可按字段核对原业务输出定义、上游生成与采用、保存位置及冻结配方中的显式下游绑定。字段定义从本轮原产物读取，不用当前目录补齐历史；色彩脚本、负面提示词、连续性摘要区分未保存编辑、项目保存值和本轮采用值，值相同也不推断最后写入者相同。原输出的额外字段只标注本轮采用记录，不伪装成项目基础字段。下游绑定仅表示配置关系；实际读入内容及提示词通过本轮原任务入口核对。来源不完整、读取失败、人工制作没有冻结配方映射时明确说明，不按名称猜测接线；面板本身不会保存、生成或采用。

已封存运行的播放与导出入口：连续剧项目 → 制作版本 → **装配与交付 → 封存成片与交付清单 → 查看成片与导出**；原运行详情也复用同一交付组件。人工与配方运行共用能力。按需实时核验原发布包后，可逐集播放/刷新确切母版、下载母版，或导出完整签名清单 JSON；自动保存未收到时使用页面保留的本次清单下载链接。JSON 保留服务器完整验证文档，并非可见行截取、整包视频归档或第三方发布。

每次播放、下载或导出前重读原核验接口；文件为空、丢失、来源或 SHA 不符时停止展示可交付详情，不取旧文件替代。播放失败明确显示错误，刷新仍指向原母版。文件展示含确切剧集/产物 ID、存储对象键、字节数与 SHA；不把清单数组顺序猜作集号。“已请求下载”只表示发起保存，不能代替磁盘保存成功。配方最终成功断言同样实时复核文件和完整覆盖；分段响应绑定同一已打开文件，截断不会静默成为短成功。接口不创建新装配、媒体生成或采用记录；现用与隔离验收边界见详细设计第18节。

人工封存前的“预览本集成片”和“下载本集成片核对”与封存后交付共用母版身份、状态、非空文件、SHA及正式播放/下载地址校验；人工清单另核对记录的 manifest SHA 和字节数。每次下载重新读取确切产物，不复用上次预览的签名链接；读取失败撤去旧预览并取消本次人工确认，允许显式重读原产物。两种制作模式仍共用原装配与封存服务，独立素材保留原 Asset/采用来源，不因此生成配方或模型调用。

## 12. 健康检查与日志

基础健康检查：

- `GET /api/v1/health/`
- PostgreSQL 可达，数据库任务 claim/lease 可读写；
- 所有 worker 有近期 heartbeat/lease 活动；
- managed storage 可读写；
- provider/deployment/workflow readiness 为当前状态。

运行日志位于项目运行目录配置的位置。排障时保留 ProductionRun ID、stage、attempt、external request ID、artifact ID、worker 和时间戳；先遮蔽 credential、签名 URL 和敏感地址。

常见状态：

- waiting review：完成人工审核；
- blocked：按错误码修复依赖；
- uncertain：核对 provider 后用 run 的 resolve-uncertain；
- workflow smoke uncertain：在工作流版本验收页核对 ComfyUI history 与稳定 request id 后，通过 attempt 的 `resolve-uncertain` 追加终态结论；未消歧禁止重试。人工确认成功也不等于 smoke passed，必须新建 attempt 重新收集并校验。
- failed/retryable：在 ProductionRun 恢复；
- Deferred 历史：只读核对所属 ProductionRun，并在该 run 上执行恢复或取消；
- pending restart activation：新 boot 后显式 activate；
- workflow needs_review：绑定、validate、real smoke、publish。

## 13. 备份与恢复

备份必须在一致时间点覆盖：

- PostgreSQL；
- managed media/artifact storage；
- 部署所需的非敏感配置和版本清单；
- workflow/model provenance 与许可记录。

恢复时先恢复数据库和媒体，再执行 `migrate --plan`、`migrate --check`，核对 artifact 引用、逐集 master 与 ReleaseBundle。恢复演练应在隔离环境定期进行；只备份数据库而不备份受管媒体不是可恢复状态。详细步骤见 [backup-restore.md](backup-restore.md)。

## 14. 不保留旧资源额度合同的开发环境切换

业务预算、调用次数及文本输出长度只在系统设置的 `execution.resources.review_policy` 中维护。模型、执行实例、配方和 ProductionRun 不再保存可覆盖它的额度设置。修改配置后走原配置修订的核对、发布、启用流程；已冻结请求不被改写。超额暂停后续提交并等待明确人工确认，成功结果和真实账目不会因此改成失败。

对已有不兼容配置，不提供运行期兼容或双读。已获授权的本地 SQLite 清理沿既有维护命令和唯一 `scripts/restart.ps1` 入口执行：

1. 用 `scripts/backup.ps1` 备份数据库并核对 manifest。仅数据库备份只能恢复本次数据库清理，不是完整媒体灾备；本次清理不删除任何物理媒体文件。
2. 在 backend 目录用 `.venv/Scripts/python.exe manage.py purge_incompatible_workflows --resource-policy-cutover` 只读预览。逐项核对待删模型、运行实例、执行版本、配方和关联运行/任务/验收记录、ScriptRevision 来源解关联、受保护内容及 `plan_sha256`。进行中调用禁止清理；uncertain 记录作为未消歧事实留在备份和清理报告中，不伪称远端成功或失败。
3. 审核文件仅包含 `backup_manifest`（绝对路径）、`operator`（现有管理用户）、`plan_sha256` 与 `confirmation=REPLACE_REVIEWED_RESOURCE_CONFIG_PRESERVE_CONTENT`。执行 `scripts/restart.ps1 -SkipComfyUI -ResourceCutoverReview <审核文件绝对路径>`：先复核范围，再停平台写入服务、应用迁移、事务清理并重建，再启动服务；不重启 GPU。
4. 清理逐行验证项目、剧集、分镜、Bible、素材、剧本及风格未改变；只允许预览中的剧本来源任务外键解关联。旧配置删除可从备份恢复。新模型及执行实例通过当前配置校验重建，配方回到草稿；不复制 Probe/Smoke 或审批成功证据，不调用供应商。
5. 核对健康/就绪、系统配置来源、创建入口和保留内容。新的资源及实例必须在正式设置页重新完成需要的真实验收，不能把重建成功说成已具备生产验收证据。

### 14.1 已核对废弃配置的精确删除（SQLite 一次性维护）

用户明确要求物理删除不兼容/被替代的配置时，复用 `purge_incompatible_workflows --registry-configuration-plan <JSON绝对路径>`。这是开发环境一次性修复命令，不替代页面日常配置管理，不自动发现或批量选中“校验失败”的版本，也不要求把目标先退役。

- JSON 仅含 `schema_version: 1` 和 `targets`；每项必须明确 `model`、UUID `id` 和已核对的 `reason`。允许模型/部署/权重登记、工作流/Projector、执行实例、策略、配方、字段/结构、输入来源/业务输出及导入配置。空目录也须明确指定，不自动上溯或删除共享依赖。
- 默认只读预览。只扩展目标的自有绑定、输入/参数/合同、策略规则、结构节点与所删部署的文件可用性映射；所有保留对象的外键（包括可置空关系）及执行性 JSON 引用均阻挡。失效下游可明确纳入本次范围，有效上游按引用保留，不改写发布合同。
- 素材文件及原来源说明原样保留。素材的生成实例/部署/工作流编号只是历史说明，不是再次使用素材的执行依赖；导入报告的 `successor_of`、主机快照中已观测的部署编号也原样保留。未知 JSON 引用仍阻挡；不会将旧素材改称由新配置生成。
- 历史清理导致的 `validated_deployment`/Smoke 证明丢失，不是判定配置废弃的依据。缺少当前验证证据的有效配置应保留并明确显示待校验，不能补造通过事实。
- 先用 `scripts/backup.ps1 -BackupName <本次唯一名称> -SkipMedia -SkipComfyUIOutput` 创建 SQLite 备份；执行前刷新预览。仅当 `can_execute=true`，再附 `--execute --operator <现有管理员> --expected-plan-sha256 <本次摘要> --confirmation DELETE_REVIEWED_OBSOLETE_CONFIGURATION_KEEP_CONTENT --backup-manifest <绝对路径>`。本分支与其他清理/切换参数及无备份参数互斥。
- writer 在同一 SQLite 写事务内重新核对范围、引用、备份内容与摘要；错误整体回滚。涉及模型/部署时，正在运行的模型目录探测须先自然结束，防止旧观测在清理后回写；仅清理策略等无关配置不等待主机探测。未知结果先回读，不重复执行原清单。
- 不删除媒体/模型/导入源文件，不操作 ComfyUI、不生成、不迁移、不重启服务。删除后比较保留配置、项目、素材及关系的逐行摘要并检查外键；从清理前数据库备份可恢复本次删除，备份不包含媒体则不能称为完整媒体灾备。

## 15. 发布前验证

```powershell
.\scripts\check.ps1

Push-Location frontend
npm run test:manual
npm run typecheck
npm run build
Pop-Location
```

随后在目标环境完成真实 provider/GPU E2E：授权、deployment probe、workflow real smoke、异步 API 与 ComfyUI 两类独立素材 T2I/T2V/T2A/reference-to-video submit/poll/archive、图片/视频/音频/剧本各自入库与下载/短剧复用、短剧媒体生成、episode master、ReleaseBundle verify 和恢复演练。ComfyUI 输入验收至少覆盖全部标量类型、多个固定媒体槽、单/多 `CONDITIONING_MODE`、多图/多视频/音频用途与配对、required/optional、越权或文件漂移阻断，以及历史 v2 快照 retry 拒绝。每个案例必须记录实际任务和 Asset 证据；仓库测试或文档更新只能证明合同，不足以证明目标基础设施生产就绪。
