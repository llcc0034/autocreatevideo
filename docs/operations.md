# Operations

本文是当前代码的运行与运维入口。服务管理使用仓库脚本，Schema 使用 Django migrations，机器 API 契约使用根 `openapi.yaml`；不要维护第二套命令或副本。

## 1. 前置条件

工作流执行统一要求冻结 Projector。缺少引用的旧版本不能继续执行，重建使用“工作流 → 扫描/已有候选 → 生成合同 → 提升草稿 → 验收与发布”，不得通过修改旧 SHA 或复制旧通过状态恢复生产。清理旧数据前必须明确对象与依赖范围，并确认没有在途生成或烟测；代码升级本身不自动删除业务数据。

未上线 SQLite 环境的一次性历史清理使用 `manage.py purge_incompatible_workflows`，不是日常删除入口。定向清理加 `--workflow-version <版本 UUID>`（可重复），默认仅预览；未知 UUID、空范围、依赖触及范围外工作流或共享上游、活动任务均阻断。执行要求 `--execute`、精确 `--expected-plan-sha256` 和 `--confirmation`，默认还要求正式备份的 `--backup-manifest`。仅在操作者明确要求不可恢复删除时，可在精确范围下改用 `--irreversible-without-backup`，不能与备份参数并用，也不能无范围使用。可重复传入 `--obsolete-projector-version <版本 UUID>`，同时清理指定的已移除引擎版本及其合同草稿；仍被范围外工作流引用、引擎仍受支持或 UUID 不存在都会阻断，保留共享模型、素材和原始候选。锁内复核范围、外键和 JSON 残留，失败整批回滚。删除计划内历史及产物数据库记录，不删除物理媒体文件。

切换前先复核替代模型版本的 `conditioning_contract`、文件与实例证明、提示词绑定和候选 Projector 匹配。动态规则只存在于旧工作流而不在模型版本中时，需创建新的模型能力版本；不能修改已发布模型，也不能从旧工作流静默补齐运行配置。

目标机器需要能够运行项目内安装/解析的 Python、Node.js、FFmpeg 和 PostgreSQL。ComfyUI 或 async REST provider 是可选执行器，但未完成 provider、deployment、workflow 和 smoke 配置时不能承担生产任务。

运行期业务配置保存在数据库的版本化 ConfigRevision 中。provider 密钥等敏感值由超级管理员写入 SecretBinding，材料以 AES-256-GCM 密文和不可变 SecretVersion 保存；API、页面、日志与审计正文只展示 configured/usable 状态。数据库解密密钥环必须放在部署 secret manager 或受保护环境文件中，不能写入数据库，也不能与数据库备份打包在一起。

本机默认的 `development`、SQLite、localhost HTTP 只用于开发，不构成生产安全基线。生产主机必须显式设置 `AUTOPROMPT_DEPLOYMENT_PROFILE=production`；启动会 fail closed 校验 `DJANGO_DEBUG=false`、不少于 50 字符且非占位的 Django secret、独立且不少于 32 UTF-8 bytes 的 JWT signing key、PostgreSQL 及不少于 20 字符的非占位密码、真实且无通配符的 allowed hosts、与主机匹配的 HTTPS CORS/CSRF origins、SSL redirect、secure cookies 和至少 3600 秒 HSTS。基础服务健康只说明进程与基础设施可用；Provider/Deployment/Workflow/Smoke/Recipe 任一能力证据不完整时，生成 Readiness 仍应 blocked。

建议阈值与安全硬门必须分开处理：输入/输出建议量、建议字数和提示词通道预警只记录 warning，交给人工评估，不阻断也不消费失败重试；权限、受管秘密、真实容量、响应截断、合同结构、目标 hash、新鲜 Smoke、预算/授权、媒体探测和不确定提交仍是硬门，不能为了让主流程显示成功而降级。

## 2. 首次安装

在仓库根目录运行：

```powershell
.\scripts\setup.ps1
```

setup 安装/修复项目运行时、安装后端和前端依赖、生成独立数据库加密密钥环、执行数据库迁移、创建首个超级管理员、初始化资产库依赖的系统风格并导入受版本管理的资产库。系统风格在普通安装与演示安装中都会初始化；只有显式传入 `-WithDemoProject` 才创建演示账号和示例项目。首次创建时终端只显示一次随机临时密码；该账号首次登录必须先改密。workflow catalog 不由 seed 数据或启动流程覆盖。

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

## 3. Migration 流程

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

日常启动/重启：

```powershell
.\scripts\restart.ps1
```

静态与契约检查：

```powershell
.\scripts\check.ps1
```

`restart.ps1` 是本地正式启动/重启入口。它会设置新的 boot identity、执行迁移（除非明确使用维护用跳过参数）、启动 API/前端与 generation、text-generation、async-media、assembly、platform-maintenance worker，并按配置处理 ComfyUI。boot identity 生成不等于 restart-required 配置已激活。仓库当前没有独立的本地停止脚本；停止进程应使用部署环境的服务管理器，不得引用不存在的命令。

当 DynamicVRAM 已启用、ComfyUI 仍因桌面进程占用而在采样临时张量分配阶段 OOM 时，可在确认队列无 running/pending prompt 后，用同一正式入口增加额外显存余量并重启 GPU 进程：

```powershell
.\scripts\restart.ps1 -RestartComfyUI -ComfyUIVramHeadroomGB 1.5
```

该参数只透传 ComfyUI 的 `--vram-headroom`，默认值为 `0`，不改变常规启动行为；它不会降低分辨率、帧数、采样步数或替换工作流。数值越大，权重卸载越激进、速度可能越慢。每次调整后都必须创建新的 smoke attempt，以实际终态和输出探测为准。

需要让同一 ComfyUI GPU 在不同模型任务之间显式交接资源时，在对应 Deployment 的非秘密 `runtime_config` 中配置严格合同 `pre_submit_resource_release={"schema_version":1,"enabled":true,"unload_models":true,"free_memory":true}`。Settings 提供四个类型化字段；四项必须完整，不使用隐藏默认值。generation worker 只在新任务完成本地预检后、冻结 submission token 与 `POST /prompt` 之前调用一次 ComfyUI `POST /free`。已提交任务的恢复、`poll`、`collect` 和历史请求核对不调用 `/free`，避免卸载正在恢复的远端任务。该端点改变进程级 GPU 状态，因此启用合同的部署要求 `TASK_MAX_CONCURRENT=1`；并发不满足、HTTP 拒绝或释放结果无法确认时均在 `/prompt` 前明确失败，不自动重试。成功/失败记录策略 hash、动作、时间、状态和稳定错误码，不记录敏感正文。

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

三者允许 system 与 project 修订，发布后由同一个配置解析器生效；装配把完整 profile 与 SHA-256 纳入来源签名，分发把它纳入 release identity，参考媒体则在 `GenerationTask.capability_snapshot` 中冻结并在每次暂存/重试时复验，配置变化不会静默复用旧衍生素材。对象 schema 的 `security_ceiling_paths` 声明每个嵌套资源字段对应的绝对上限；通用配置发布器和冻结快照解析器均校验这些关系，不由媒体服务另存第二份范围。

所有入库视频/音频共有的实际字节 FFprobe 使用 `artifact.media_probe_timeout_seconds`，它是 system 普通配置并由 `security.runtime.max_timeout_seconds` 提供外层绝对上限；旧 `TASK_DISTRIBUTION_TIMEOUT_SECONDS` 和隐含 `ASSEMBLY_PROBE_TIMEOUT_SECONDS` 不再作为业务事实来源。

与上述普通 profile 分离的 `security.media.duration_verification_tolerance_max_ms`、`security.media.audio_bitrate_max_kbps`、`security.media.audio_sample_rate_max_hz`、`security.media.processing_argument_max_characters` 和 `security.delivery.disclosure_text_max_characters` 属于安全边界，只允许 system scope 且只有超级管理员可修改。超时继续受 `security.runtime.max_timeout_seconds` 约束，披露像素值受 `security.media.max_dimension` 约束。codec/preset/pixel-format 的合法参数形态、FFmpeg 参数分隔、RGBA 的 0–255 和 opacity 的 0–1 是协议/格式不变量，不是可运营建议值。

运行期任务参数从同一配置中心调整，不修改 `.env` 或 Python 常量：`runtime.task_worker_poll_seconds`、`runtime.task_heartbeat_seconds`、`runtime.task_idle_timeout_seconds` 与 `runtime.task_stale_recovery_interval_seconds` 同时服务 generation、text、async-media 和 assembly；`runtime.worker_stale_recovery_minutes` 是 generation/text Worker 未显式传入 `--recover-minutes` 时使用的版本化回收阈值，命令行参数仅作本次运行的显式覆盖；`runtime.asset_text_submission_grace_seconds` 是文本素材任务已冻结请求超时之外的提交宽限，`runtime.asset_uncertain_reconcile_batch_size` 是 ComfyUI 不确定素材任务的每轮对账数量，分别受运行时安全上限保护。`runtime.gpu_probe_timeout_seconds` 控制本机 `nvidia-smi` 能力探测超时，探测快照记录生效值和配置 SHA，并受 `security.runtime.max_timeout_seconds` 外层上限保护。`runtime.deferred_generation_retry_backoff_seconds` 在 Deferred request 创建时冻结，后续失败不会读取新的 live 值。`workflow.smoke_poll_interval_seconds` 是受安全间隔上限约束的真实 smoke 轮询间隔。`delivery.assembly_max_attempts` 在 AssemblyJob 入队时冻结，且不得超过 superadmin-only `security.delivery.assembly_max_attempts`。`production.project_creation_policy` v2 是正式项目创建入口的 system 级闭合合同，提供语言、画幅选项、FPS、默认镜头媒介类型、表单建议容量与连续性种子策略；尺寸、镜头时长和音频开关分别读取 `production.default_resolution_width/height`、`production.default_shot_duration_ms` 与 `production.default_include_audio`，不在对象合同中复制第二份数值。`api.default_page_size` 控制运营列表默认分页；`workflow.node_metadata_default_page_size` 与 `workflow.node_metadata_input_preview_limit` 控制 ComfyUI 节点浏览展示，调用方请求仍不得越过 superadmin-only `security.api.max_page_size` 与 `security.api.max_search_chars`。`runtime.host_probe_heartbeat_seconds`、`runtime.host_probe_lease_seconds`、`runtime.host_probe_stale_seconds`、`runtime.host_probe_refresh_buffer_seconds` 控制主机探测；`runtime.platform_maintenance_poll_seconds` 与 `runtime.platform_maintenance_lease_seconds` 控制维护 Worker；`runtime.readiness_worker_max_age_seconds`、`runtime.readiness_min_disk_free_bytes` 及 `runtime.readiness_generation_worker_required`、`runtime.readiness_text_generation_worker_required`、`runtime.readiness_assembly_worker_required` 控制 readiness 判定。公开分发开关 `security.delivery.public_distribution_enabled` 位于超级管理员安全边界分类；`runtime.distribution_lock_stale_seconds` 是受 `security.runtime.max_timeout_seconds` 约束的 system 配置。清单版本 `shot-public-v1` 是代码协议不变量，不能由环境或普通配置改写。心跳必须小于租约、租约不得大于 stale，配置缺失或关系无效会明确阻断 Worker，而不是回退旧值。`TASK_MAX_CONCURRENT` 仍由目标主机部署环境设置，因为它是启动数据库配置中心前就必须确定的物理进程/GPU 上限；修改后需要重启对应 Worker。

对白摄入绝对包络位于“安全边界”分类的 `security.dialogue.max_entries`、`security.dialogue.speaker_id_max_characters`、`security.dialogue.text_max_characters`、`security.dialogue.delivery_max_characters`，只有超级管理员可改。普通运营建议分别位于 `production.series_concept_resource_advisory`（剧本候选输入/输出估算和超量预警，warning-only）与 `production.script_prop_alias_policy`（确定性道具简称推导，可按项目覆盖）。视频分辨率和帧规则不在这里复制数值：应编辑/导入下一版 WorkflowTemplateVersion 的类型化参数合同并重新完成 validation、真实 smoke 与 publish，历史已发布版本保持原事实。

`production.canonical_name_policy` 是 system/project 的版本化 canonical-English 合同与 glossary 建议批量策略：名称长度、词数仍由合同和 `security.contract_complexity.*` 硬校验；实体数、含视觉候选实体数和推理项数只作为 warning 建议，超出不会直接失败。`production.canonical_context_policy` 配置专名与视觉候选上下文中的术语项数、描述、视觉锚点和单个术语字符建议值；超过建议会把实际值、建议值、受影响项数、策略和配置解析 SHA 写入响应及翻译审计，但服务端仍把完整 Bible 描述、视觉锚点和全量 glossary 送入装配，不做静默切片。Provider 翻译按已发布 `ModelProfileVersion.specification.prompt_normalization.max_items_per_batch` 保持原顺序确定性分批；最终请求若超过现有 `security.snapshot.text_provider_request_max_bytes` 等安全字节包络才会在提交前明确失败。实体与运行项的真正绝对上限仍是超级管理员维护的 `security.canonical_glossary.*`。翻译请求、人工确认同步与 smoke/uncertain 人工审计说明分别受 `security.translation.*`、`security.audit_note.*` 限制。媒体能力合同的模式、规则和说明文本容量受 `security.contract_complexity.*` 限制，字段形状、标识符、配对关系、正时长和时间顺序仍是协议语义，不可由配置放宽。

配置值需要引用平台资源时，在 `value_schema.resource_ref` 中声明 resource、capability、`allow_empty` 和 `require_ready`。页面通过 `GET /api/v1/config-definitions/{id}/resource-options/` 读取服务端审核的选项，并显示不可选原因；不提供自由输入 UUID 的旁路。当前已登记的资源引用是 `prompt.normalize` 的 model deployment。相对 `based_on_revision` 新增或真实变更非空引用时，服务端会在创建、发布及需要重启的激活阶段重新验证 deployment 存在、模型能力、可路由状态和当前有效 operation smoke；不依赖浏览器旧选项。原样继承的引用只做不可变合同与 checksum 校验，不会因瞬时 readiness 变化阻断无关配置修改；实际消费者在使用前仍重新检查 live route。历史 revision 中的空值显式解析为 `unconfigured`，会阻断依赖该路由的新生产，不会回退到环境变量、默认 deployment 或旧缓存。

所有设置页新增/编辑弹窗统一遵守[填写表单最小输入标准](form-input-standard.md)：可枚举值必须读取服务端 `options`，系统字段只读或隐藏，真正的业务事实才保留输入。读取目录前先清空旧值；失败后不能继续使用旧目录，也不能自动退化为自由文本。服务端未在 `custom_entry_fields` 授权时，页面不得显示新目录值入口。

文本合同有限重试使用两个互不替代的类型化键，范围均为 0–5、默认均为 2，数值表示首次调用之外允许的自动尝试次数：`production.text_contract_retry_limit` 只处理 `script.generate`、`script.structure` 与 `visual_rules.generate` 已收到确定响应后的已知安全合同拒绝；`production.prompt_field_retry_limit` 只处理 prompt.normalize 的已知逐字段合同失败。内容过滤、uncertain、执行前门禁、Smoke/配置漂移和审计缺失不自动重放；耗尽对应上限后转人工确认。

## 6. Provider、deployment 与 workflow 上线

风格素材库的文本、图片、视频和音频有独立的生成方案目录与真实烟测门禁。文本/剧本只使用 `script.generate` Deployment；媒体可使用 fresh operation smoke 的受限 `async_rest` Deployment，也可使用完成 import → Live validate → real Workflow smoke → publish 的 current ComfyUI Workflow。两类证据不能互相替代。Provider、模型规格、Workflow 门禁、费用/白名单、空列表排障和现场验收统一见 [素材生成方案配置与验证](asset-generation-model-configuration.md)。

### Provider

- 建立 provider profile。
- 由超级管理员创建或轮换 SecretBinding，并把受管凭据绑定到 Provider 或 Deployment；平台管理员不能替换秘密绑定。
- 在目标环境验证授权、配额和 endpoint。
- 只展示 configured 与 owner lifecycle；configured 不等于可调用，停用的 Provider、Deployment 或 SecretBinding 都必须显示 blocked。

### Model profile、artifact 与 deployment

- 新建模型只填写新的 `profile_key` 和确实需要人工确认的模型事实；Provider、主模态、能力、提示词策略族和文件角色从 `GET /api/v1/model-profile-versions/options/` 的服务端目录选择。新模型可在选择器中显式选择由合法 `profile_key` 派生的同名新策略族，其他自定义文本会被拒绝。该目录不可用时 Settings 失败关闭，不回退自由文本。
- 版本号及模型规格结构版本不由操作员填写。首次模型由服务端固定分配 `v1`；“新建下一版本”只接受当前最高 published/retired 版本，先校验源规格完整性，再在事务中按该族包括 retired 在内的最高历史版本递增。最高版本已是草稿时必须继续编辑；旧源或并发请求返回冲突。`profile_key` 与版本号创建后不可通过普通编辑修改。
- 模型卡片默认只用一行汇总能力与文件证据，需要时再展开；运行实例操作在可用宽度内横向排列和自动换行。视觉折叠不改变文件核验、发布、Probe、Smoke、停用或退役门禁。
- 本地 Comfy 模型按 Provider → draft model profile/声明 artifact → 启用并 probe host 至 active，由工作流 discover 返回的同 Provider active loopback 内部 source Deployment 完成草稿 artifact attestation 与完整文件闭包 → publish profile → 建立目标 deployment → 再 probe host 形成该 deployment availability → artifact verify → deployment probe/smoke 的顺序登记。扫描前不要求管理员手选 source Deployment。远端模型没有本地文件时明确为 N/A；不能先发布再证明文件，也不能在没有主机事实时把 declared 文件伪装为 available。
- 审核来源、hash、许可和用途。
- 先把目标 host 探测为 active，再建立 active deployment；建立后再次探测，形成该 deployment 与文件的最新 availability 快照。
- 日常 Settings/API 探测只负责创建持久 `RuntimeHostProbeAttempt`（HTTP `202`）；platform-maintenance worker 领取任务、持续心跳并写入终态。页面刷新后从 attempt collection 恢复，不得因 12 秒客户端超时把仍在后台执行的 SHA 误报为失败。
- running 探测的取消先持久化为 `cancel_requested`，在 8 MiB 文件分块检查点安全停止；worker 崩溃后由陈旧 claim 恢复。若快照已经与 attempt 原子关联，恢复流程收敛为 succeeded，不重复扫描大文件。
- `manage.py probe_runtime_hosts --due` 是人工维护/故障诊断的同步命令；正常运营只使用 Settings/API 异步入口，不与 active attempt 并发执行。
- 对本地模型文件使用最新、未过期的 RuntimeHost snapshot 执行 artifact verify；远端文本模型没有本地 artifact 时明确标为 N/A，不伪造文件。
- 执行 probe 和 smoke-test。文本部署分别选择 `script.generate`、`script.structure`、`prompt.normalize` 与 `visual_rules.generate`；每次调用都先返回 HTTP `202` 的持久 `ModelDeploymentSmokeAttempt`，Settings 从 attempt collection 恢复并轮询终态，不受前端短超时影响。worker 在落库后只跨越一次提交边界：`script.generate` 提交最小一集一镜请求，其余三项使用各自严格合同；公开证据只保留 hash/费用/错误分类。外部部署未配置确定 pricing 时不会排队提交。`queued` 可取消，进入 `submitting` 后若状态不确定必须由管理员核对 Provider 控制台/账单并追加消歧证据，系统不会自动重放付费请求。
- 四种文本能力均需在烟测确认框选择“本次烟测使用的提示词策略”，每次只验证一条启用绑定的已发布版本；缺少适用绑定时先到提示词策略页配置。请求、任务与结果冻结相同的策略版本和合同 SHA，验收页分别显示各策略结果；不能用 A 策略的通过结果为 B 策略放行。历史未冻结策略的记录只说明当时结果，不会自动追认为当前策略验收。最小测试素材和 JSON 输出协议属于代码中的测试夹具与传输合同，业务提示词由所选策略装配，不把夹具当成后台隐藏策略。
- 烟测创建先在写事务外完成合同校验、请求组装与估价，再在短事务内重新核对实例目标 SHA、提示词绑定与重复任务，冻结请求并返回 `202`。准备期间配置发生变化会拒绝提交，不能写入过期快照。`prompt.normalize` 仍覆盖模型声明的输入容量边界：固定内容只组装一次，以实际 messages 字节差计算惰性 ASCII 填充并再次校验；不会为了提速缩小验收样本。安全边界读取只在本次准备/提交范围内复用，下次请求重新读取配置。
- SQLite 短暂写锁冲突时，worker 领取任务按配置的轮询间隔等待重试；初始心跳写入失败则由心跳循环重试，真实写入前仍显示缺失/陈旧。非锁冲突错误继续报错；不会把模型调用、远端提交或产物生成放入数据库重试，以免重复执行和计费。若 worker 已异常退出，通过唯一入口 `scripts/restart.ps1` 恢复，勿手动重放已提交或提交状态不确定的任务。
- 未成功前保持不可路由。

draft profile/artifact 可以编辑；只有仍为 `declared`、从未核验且没有主机观测、工作流或配方引用的草稿 artifact 可以删除。published profile 不能覆盖，只能先停用引用它的 deployment、清理 current workflow 约束后 retire，变更通过下一版本完成。Deployment 必须先 disabled：完全没有 probe/smoke、host availability 或其他引用的误建空记录可以删除；已有证据的记录应在替换 draft/published 配方和 current workflow 引用后退役，retired 为不可逆只读终态。

#### 模型文件路径调整与注册表清理

模型文件注册与物理文件管理是两个职责。Settings/API 只维护相对定位、预期大小与 SHA-256，不会替用户移动或删除磁盘文件。路径调整按以下三类处理：

1. **只迁移 ComfyUI 安装根，文件相对位置和内容不变。** 先由运维人员移动/恢复实际文件，再把 `backend/.env` 的 `COMFYUI_OUTPUT_DIR` 改为新 ComfyUI 根下的 `output`（平台据此使用同级 `models` 目录）；若 ComfyUI 地址也变化，同步修改 Provider/Deployment 的受控端点配置。使用唯一日常入口 `scripts/restart.ps1` 重启，然后重新执行 RuntimeHost probe 与 artifact verify。旧快照保留为历史；端点或 Deployment 合同变化会改变 target SHA，必须重跑对应 Deployment smoke 以及 Workflow Live validate/smoke 后再用于新生产。
2. **相对目录、文件名、大小或 SHA-256 变化。** 所属版本仍为 draft 时，在模型文件编辑器更新声明；保存只更新注册记录，不移动文件，并自动把状态恢复为 `declared`、清空当前核验指针。随后重新做草稿文件证明/主机探测并按正式发布顺序收敛。所属版本已经 published/retired 时，不改旧记录：新建更高 ModelProfileVersion 和新的 artifact 闭包，发布并建立新 Deployment，完成 probe/verify/smoke；引用该模型的 Workflow 也新建版本并完成 Live validate/smoke/publish，替换 draft/published 配方与 current 工作流后再停用、退役旧 Deployment 和旧模型版本。
3. **文件缺失或路径配置未同步。** 新 probe 必须写出 `missing`、`incompatible` 或 `unknown`，页面以最新且未过期的精确 size/SHA 事实阻断核验；不得猜测新位置、回退旧快照或仅把历史 artifact 顶层状态当作当前可用。先修正物理文件或受控根，再重新 probe/verify。

页面清理规则与上述审计边界一致：模型页按 `profile_key` 显示模型卡片，每个版本先显示一行“能力 + 文件与证明”紧凑摘要，信息详情按需展开；“运行实例（Model Deployment）”保留独立折叠区，其操作按钮随可用宽度横向排列并自动换行。默认隐藏 retired 模型版本及其子记录，需要查历史时显式开启“显示已退役/停用记录”。草稿空声明显示“删除草稿声明”；active/error 运行实例先“停用”，disabled 记录点击删除后必须先读取统一影响清单，只有完整清单确认没有历史或阻断引用时才能提交；需要留存事实的记录使用“退役保留历史”。Provider、模型能力版本、Deployment、模型文件、Workflow、Recipe、ConfigDefinition 及 Prompt 字段/Profile/规则/Binding 全部复用同一弹窗，不再展示目标原始 JSON 后直接尝试删除。

影响弹窗把依赖分为四组：可逐项勾选的安全草稿、随目标存在的自有草稿结构、必须替换或单独复核的阻断引用、绝不删除的运行/审计历史。默认不勾选任何依赖；存在阻断或历史时删除按钮禁用，并显示具体对象名称、版本、状态、UUID、关系和配方 JSONPath。操作者必须选择审计原因并输入服务端返回的完整目标短语；页面只向统一的 `registry-deletion-execute` 提交，资源 detail API 没有直删入口。提交期间禁止关闭；服务端在锁齐依赖及其父边界后再次重算影响，任何引用或对象内容漂移都会清空选择并要求重读，不会部分删除。配方、配置和 Prompt Binding 历史写入与删除共享服务端事务 guard，并按 guard → Binding 行锁顺序在持锁后重读引用身份；ProductionRun 输入快照、PromptArtifact、GenerationAttempt 和 Deployment Smoke request snapshot 命中 Binding ID/key 时都会作为必须保留的历史列出，Binding key 创建后不可改名。任何删除动作都只删除数据库中的可清理注册记录，不触碰本地模型文件。

平台不会自动下载模型。catalog 中存在 H3、SDXL 或其他系列只表示可配置/待许可审阅，不是已安装证明。

### PromptProfile

从“系统设置 → 模型 → 选中版本 → 运行实例 → 提示词策略关联”可在实例上下文查看完整策略关系；模型版本标题也提供相同入口。面板区分精确版本绑定与模型族通用绑定，展示具体 PromptProfile 名称、版本、状态、合同 SHA、operation/mode，可继续进入“管理提示词关联”。新绑定固定当前精确模型版本并选择已发布策略；运行实例继承其模型版本关系，不维护第二套 Binding，也不自动替本次运行选策略。管理页可返回原实例；需要编辑影响其他模型的族级规则时，显式进入“查看全部提示词策略”。

PromptProfile 是提示词可配置语义装配的唯一主路径，应在 workflow 和 ProductionRecipe 之前完成治理：

1. 在字段目录中审阅 key、类型、分类、描述、影响、默认值、枚举和必填性。
2. 为 model family/model version、operation 和 mode 建立 draft Profile。普通直接装配选择 `direct_v1`；需要组合已注册确定性能力时选择 `declarative_v1`，再按字段规则选择 `projection.output` 与代码原语。字段、channel、顺序、标签、分隔符、缺省处理和长度都在页面配置。
3. 编辑弹窗右侧会自动带入服务端通用示例并直接开始预览；只有需要验证特定内容或空值时才修改。页面以 400ms 防抖把当前未保存的完整规则快照和本次示例提交到只读 preview，并展示服务端实际组合出的各通道提示词。通用示例不读取生产默认值，用户修改不保存，preview 不调用模型；它只验证最终排版，不代替声明式原语的真实项目上下文验收或 deployment/workflow smoke。
4. 审核官方参考、合同 hash 与系统指令来源后发布。只允许管理员默认值进入 `system_instruction`，项目或请求文本不得提升为系统指令。
5. 创建 active binding；可以为同一模型配置多个策略。文本/预览默认 resolver 的实际 model version/mode 与 priority 必须解析为唯一最佳结果；媒体与 Bible ProductionRun 则从全部兼容 binding 中显式选择 binding ID，priority 只作目录信息。

字段值类型、分类、operation、mode、投影器、规则选项和系统默认均来自 PromptProfile/PromptField 的 `options` API。新字段的展示排序与启用状态、新 Profile 的 `version=1` 与 `needs_review` 由服务端维护；页面不提交这些技术字段。已发布/退役 Profile 只能提交精确合同 SHA-256 创建后继草稿，不能从“新增”窗口重复填写同一 Profile Key。

`declarative_v1` 落实“引擎原语写在代码里，业务组合规则写在页面里”。代码 registry 只声明原语 key、确定性实现、所属 engine 和可消费的 contribution selector；每条 `PromptProfileFieldRule` 使用 `source_selector=projection.output` 与 `projection_primitive` 把一个原语结果映射到所选业务字段。字段名称和数量、position、channel、label_style、separator、required/omit_if_empty，以及 `server_locked`、`model_generated`、`translate_only`、`passthrough` 和 final/contribution 分工全部保存在不可变 Profile schema v6 数据中并进入合同 hash。H3 六段只是六条规则选择六个 `reference_context.*` 原语后的配置结果，不是生产主逻辑中的位置或字段名分支；其他模型可以重排、改名、删减或复用这些原语而不修改主编排。

设置页的日常主路径不要求手写投影 JSON，也不允许手写 generation source 路径：Profile 面板用结构化控件维护当前 engine options，字段规则按“字段内容如何获得”和“最终提示词如何排版”两组业务问题配置。`model_generated` / `translate_only` 的“主要内容来源”从服务端 `generation_source_selectors` 分组目录选择，选中后就近显示读取内容、适用条件和作用范围；`server_locked` / `passthrough` 自动跟随最终提示词取值，不重复配置。低频的额外参考信息、语言、长度和 validators 按需展开。保存与实时 preview 复用同一服务端校验和装配逻辑，页面隐藏高级项不代表省略合同。

运维修改已发布策略时优先使用“创建后继草稿”：页面向 `clone-successor` action 提交来源合同 SHA-256，服务端只从精确 published/retired 版本复制配置与全部规则，自动分配下一版本并回到 `needs_review`。来源在确认后变化、不是不可变版本或属于 legacy projector 时零写入失败；新草稿不会自动替换 Binding。修改、预览和发布后新建或切换 Binding，旧 Binding 仍指向旧 Profile 供历史运行复验。

`classified_reference_context_ir_v1` 是历史运行兼容 projector：运行时仍可读取并编译已经冻结的旧 Profile，但当前 options 不再提供它，新建、修改后发布和 clone successor 都不能选择它。不要删除或重定向旧 Profile/Binding 来“升级”历史数据；新 H3 策略使用 `declarative_v1`。只有新增外部协议、现有原语无法表达的确定性算法、conditioning adapter 或安全 validator kind，才需要增加并审查代码注册项。

`script.generate`、`script.structure`、`prompt.normalize`、`visual_rules.generate`、`image.generate` 和每个 `video.generate` mode 都要单独核对。前四个文本 operation 缺少唯一默认 binding 或出现同优先级多解时阻断；媒体/Bible operation 没有兼容 choice、用户未显式选择、选择与模型/模式不兼容时阻断。seed 生成的 `needs_review` draft、未发布引用与合同漂移都不是生产能力，任何路径都不会回退到通用模板。变更已发布策略时新建版本并重新绑定；历史运行只读冻结的 binding ID/Profile/contract，不受后来 active/priority 变化影响。

### Workflow

模型上下文下的 saved-workflow discover 仍扫描全部受控来源，页面只按当前精确模型的 candidate、contract draft、WorkflowTemplateVersion provenance 和服务端 actionability 派生主列表；文件名、同族和相似图不是关联证据。扫描会冻结发起时的页面加载代次与 `model_profile_version`；扫描期间切换或清除模型后，旧成功、旧失败及 preview/证明恢复结果全部丢弃，原物理请求结束并释放 busy 前不接受第二次扫描。

ComfyUI saved workflow 上线使用五层主路径：

1. **发现并纳管来源**：在 Settings 核对 `workflow.comfyui_saved_workflow_directory`，扫描全部受控来源作用域，再显式以 `import_kind=candidate` 保存合法文件。candidate 只要求目录、JSON、大小和敏感字段检查通过；不要求目标执行 Deployment、published 模型或 projector。内部 source Deployment 仅负责读取受控目录，不授予生产执行权。
2. **生成执行合同草稿**：对精确 candidate 运行 preview；未选主输出时只返回候选，选定主输出后持久化/复用不可变 `ComfyWorkflowContractDraft`。参数、素材槽、模型依赖、projector identity、blockers 和 deferred Live Validate requirements 都冻结在草稿 projection 中；未解释项保留为 `candidate_only`，不删除来源。
3. **提升与 Live Validate**：仅 `ready_for_executable_import` 草稿可提升为 needs_review `WorkflowTemplateVersion`。提升只消费冻结草稿，不重新读取本地文件或 `object_info`；Live Validate 才要求 active 目标 Deployment、published 模型、新鲜 artifact、实时节点 Schema 和 adapter/projector 一致。
4. **真实 Smoke**：在工作流版本验收页为本次 attempt 选择具体参数和 Asset fixture，在真实 executor 上执行，并审阅 attempt、compiled graph、输出、媒体探测和 target evidence。
5. **发布与生产路由**：上述合同、Validate 和 Smoke 证据都通过且 fresh 后显式 publish；Recipe/ProductionRun 只使用已发布版本。candidate 与 contract draft 不能进入 Validate、Smoke 或生产路由。

刷新页面后仍应从详情 API 看到 validated deployment、validation/smoke evidence 与合同 hash。已发布工作流不可原地更新；需要调整时导入新版本，完成 validate/smoke 后发布替代版，再 retire 旧版。不要依赖固定 Comfy node ID，也不要由 seed 或 restart 覆盖 catalog。

无量化进度的长节点不得依靠重复 `running` 心跳无限续期。模型版本通过类型化字段声明通用 `execution_monitoring`；工作流专属节点规则只允许注册 projector 从 ComfyUI 源派生并在验收页只读展示，不接受人工节点 ID/JSON。ProductionRun 会把有效合同/hash 冻结到 GeneratedWorkflow 和 GenerationTask 两层；worker 要求双层完整且一致，不接受 legacy 回退。执行时只在节点切换时设置一次 idle 窗口，且永不延长 hard deadline。配置字段、优先级、下一工作流版本迁移示例和无节点遥测边界见 [长节点执行监控合同](execution-monitoring-contract.md)。

current/published 的 image/video/audio Workflow 在精确 validation/smoke evidence、output kind、持久 Save 主输出和执行权限全部通过后，会进入素材库的 `comfy_workflow` 生成方案目录。固定或动态素材输入不会让版本被排除；options 会返回 `workflow_input_schema`，素材库创建任务时必须提交满足 required 参数、固定槽和所选 conditioning mode 的 `workflow_inputs`。素材库只提交 Workflow version ID，服务端解析并冻结其 validated Deployment；不得由页面或运维人员另配 Deployment。当前只允许 loopback ComfyUI 与 API/worker 可访问的同一 `COMFYUI_OUTPUT_DIR`，远程 ComfyUI 缺少受控产物下载合同时不会进入目录。

完整的 candidate、contract draft、提升、逐版本 Validate/Smoke/Publish 与错误恢复见 [ComfyUI 已保存工作流导入、验证与发布手册](comfyui-saved-workflow-validation.md)。模型上下文只过滤和解释全局扫描结果；不得按文件名、模型族或相似名称自动归属。导入阶段不选择具体参数或 Asset；这些值只在工作流版本验收页创建 Smoke attempt，或素材库/ProductionRun 创建各自任务时冻结。`smoke_fixture_schema` 与 `workflow_input_schema` 复用同一参数/素材合同内核，但生产输入不读取 smoke-only 默认值。普通 generic 工作流的每个 Load 节点形成 optional `0..1` 或 required `1..1` 固定槽；动态 `references.pictures/videos/audios` 仅由冻结的 published `WorkflowProjectorVersion` 投影，不由管理员手填。

通用多素材使用新的 `generic.references/v1` 草稿；入口、顺序端口/逐项合并、数量来源及边界见上述导入手册的“通用动态素材配置流程”。`asset_groups` 描述组内有序槽，仍复用 `assets` 请求、Asset 上传权限与校验，不另建文件通道；当前 H3 的专用 `references` 请求保持原样。更新代码后须通过正式服务管理入口激活后端，再生成和验收新版本；不能把前端构建通过当作正在运行的后端已经支持。

`WorkflowProjectorVersion` 的管理属于超级管理员版本化目录，不是普通 ConfigRevision。发布新 current projector 只影响后续新草稿；历史 candidate、contract draft、WorkflowTemplateVersion 和运行证据保持不可变。变更图族、节点角色、参数策略、模型文件角色、素材拓扑或 adapter 后，必须形成新的 projector 版本，并从同一不可变 candidate 生成新的 draft/version；只有 saved workflow 来源字节变化时才纳管新的 candidate revision。误建的最高、未发布且未被合同草稿引用的版本可从设置页二次确认后“放弃草稿”，动作写入生命周期审计；已发布或被引用版本不能删除。不能用重启或配置发布重解释历史合同。

方案目录每个版本均可直接“查看 vN 配置”，无需先创建草稿；只读窗口允许切换配置分区和复制 JSON，不提供保存。参数策略的真实处理、Generic 固定槽与 H3 Ref2VA 动态数量的区别见导入手册 4.6–4.7；尤其不能把 Generic 的 `dynamic` 声明误认为已有动态节点编译能力，也不能把 `source_locked` 当作统一运行时不可覆盖锁。

需要在 ComfyUI 中查看已保存工作流时，使用 Settings 的“下载 ComfyUI 导入文件”。系统固定返回 `open_mode=download_import`，不声称支持 direct-open。validated、published 和 retired 版本只能使用记录证据的精确 `validated_deployment`；没有该证据的 draft 必须显式选择兼容 active ComfyUI deployment。部署停用、provider/model 绑定不一致、validation target hash 漂移或请求伪造其他 deployment 均在请求 ComfyUI 之前阻断。导出只读 `object_info` 并生成 UI canvas JSON，不 submit、poll 或创建 smoke/生产任务。

### ProductionRecipe

draft 配方使用服务端 `options` 提供的阶段、执行方式、审核门禁、路由策略和标准预设进行结构化编辑；普通表单不开放完整 definition JSON 写入口。结构版本与首版号由服务端维护，已有配方族只能从精确 published/retired 版本及 SHA-256 创建后继草稿。发布前在确认窗口核对 schema、interaction mode、script mode、deployment/workflow UUID、阶段顺序与交付闭包。schema v1 保留人工/辅助模式；schema v2 的 `interaction_mode=unattended` 必须使用服务端注册 policy、单 route、单媒体候选和完整无人值守闭包。Ref2VA 基础闭包的唯一合法顺序为 `script_generate/ai → script_review/review → bible_materialize/deterministic → bible_review/review → storyboard/deterministic → script_normalize/ai → script_normalize_review/review → bible_image_generation/existing_pipeline → bible_image_review/review → storyboard_review/review → prompt_compile/existing_pipeline → media_generation/existing_pipeline → artifact_selection/review → assembly/existing_pipeline → delivery/existing_pipeline`；如启用全剧影视规则，只能在 `bible_review` 与 `storyboard` 之间成对加入 `visual_rules_generate/ai → visual_rules_review/review`。基础闭包的六项策略依次为 `approve_single_frozen_script_v1`、`approve_frozen_bible_v1`、`accept_all_valid_frozen_prompt_translations_v2`、`select_single_bible_candidate_per_entity_v1`、`bind_frozen_reference_inputs_by_contract_v2`、`accept_single_artifact_per_group_v1`；所有非 review 阶段均为 `approval=auto`。`storyboard_review` 的通用策略会把 Run 创建时按冻结工作流合同校验过的全部动态参考视频逐条复制到每个视频 Shot，原样保留素材、用途和内嵌音轨选择；允许数量、用途、互斥和配对规则仍只读取该 WorkflowTemplateVersion 的 `conditioning_contract`，`minimum_items=0` 时可保持空列表。`script_normalize` 的 fixed route 选择具备 `prompt.normalize` 能力且已通过当前真实烟测的文本 Deployment。标准化策略会将名称候选中的权威 provisional identity 按同 scope visual identity、已配置 glossary 或确定性安全标签生成唯一 canonical，并在所有冻结候选中做大小写敏感的单次精确替换；旧配方不会静默获得新语义，必须新建并发布下一版本。published 配方不可覆盖，只能创建下一版本或 retire；历史 ProductionRun 继续引用原不可变版本。

新建 Ref2VA 配方前必须把 Bible 图片生产依赖配置完整：

1. 为 `bible_image_generation` 配置输出图片的 Deployment 或 ComfyUI Workflow route，并完成该目标要求的真实 smoke、Live validation 与 publish 门禁；动态参考视频的媒体 Workflow 不能代替图片 route。
2. 发布适用于 `image.generate` 的 Bible PromptProfile，并建立与所有 Bible 图片 route 共同兼容的 active binding；创建 Run 时显式选择 `bible_image_prompt_profile_binding_id`。
3. 配方同时包含 `bible_image_generation` 与 `bible_image_review`，并把 `storyboard_review` 放在图片审核之后、`prompt_compile` 之前。
4. 在项目 Shot 关系中明确挂接实际出现的人物、场景和道具。无人值守策略不会从脚本文字猜测遗漏的关系；任一已物化 Bible 实体未被任何 Shot 使用都会阻断。

缺少任一项时不应通过更换动态视频、手填 Asset ID 或重试媒体阶段规避。缺少 Bible 图片阶段的历史 Ref2VA 配方可以继续作为历史审计事实，但不再满足当前新 Run 的创建/执行闭包；应创建新的配方版本，不能原地覆盖已发布版本。

AI 新剧本的全自动运行只允许从人物、场景、道具 Bible 均为空的新项目开始。若项目已经存在任一 Bible 实体，创建会在写入 Run 或审核事实前以 `unattended_existing_bible_requires_new_project` 拒绝；请新建空项目后重新提交，不能把既有 Bible 临时隐藏、清空关系或由策略猜测为本 Run 输入。这样全自动后续“项目全部 Bible 实体都必须被 Shot 使用”的范围，才与本 Run 从脚本新物化的实体范围一致。

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

recommend 依据当前事实生成 draft。接受 recommendation 只创建 reviewable recipe/config draft。它不会下载模型、自动部署、publish workflow 或 activate 配置。

提交 recommendation decision 时，服务端会重新验证 host snapshot 的有效期、catalog/config hash 和候选当前兼容性。收到 `recommendation_stale` 时重新 probe/recommend，禁止沿用旧草案或仅由前端放行。

目前没有跨节点/多 GPU scheduler。可以启动多个 worker，但 worker 的并发是进程局部参数；运维必须按目标机器容量设置并观察，不得将它描述为全局 GPU 调度。

## 8. 素材库操作

运营用户在“风格素材库”统一管理图片、视频、音频和剧本。先切换到页面上的目标类型，再从该类型下点击“新增素材”；对话框沿用当前类型，不再重复选择素材类型，只需选择“本地添加”或“模型生成”：

- 本地图片仅接受 JPEG/PNG/WebP；视频与音频必须由 FFprobe 验证、包含对应媒体流并能可靠确定正时长，入库阶段不套用平台统一的 2–15 秒限制。素材用于生产时，服务端在冻结具体 Workflow 后，按其 `input_contract.conditioning_contract` 中所选模式的 `preprocessing` 合同校验时长、帧率与预处理要求。剧本接受 UTF-8 TXT/Markdown/Fountain 或 DOCX，并规范化为 Markdown。
- 模型生成页面展示两类方案：active/published 且具有 fresh 精确 operation smoke 的 Deployment，以及 current/published、Live validated 且具有精确真实 Workflow smoke evidence 的 ComfyUI Workflow。音频 Deployment 必须声明并烟测 `audio.text_to_audio`；音频 Workflow 的 output kind 和唯一主输出必须为 audio。
- “文本模型生成提示词”创建独立持久任务；成功正文仍需人工编辑/采用，再显式创建素材生成任务，不会串联自动扣费。
- 外部模型逐任务勾选外发授权并填写预算上限。`uncertain` 表示远端提交状态需人工核验，不能直接重试；staff 必须核对 Provider 控制台、request id 与账单后，通过素材任务的 `resolve-uncertain` 追加不可变结论，网络重放必须复用同一幂等键。页面会展示 request id 摘要、提交/取消时间和取消意图。确认远端成功的媒体任务只恢复原 request 的 collect，费用证据冲突会再次阻断；任务已经取消时只记录费用、绝不入库晚到产物。明确 `failed` 或 `cancelled` 才可新建重试任务。
- 页面刷新后从 `asset-generation-jobs` 恢复 queued/submitting/submitted/running/cancel_requested 等状态。text-generation worker 处理文本候选和剧本，async-media worker 处理异步 API 图片、视频和音频，generation worker 在共享 GPU 互斥门内处理 ComfyUI 素材任务。
- 下载前重新读取 Asset 详情以刷新短期 `download_url`。只有受管文件可以下载；纯远端 reference URL 不伪装为已归档文件。

当页面显示“没有可用方案”时，不得把列表第一项、过期 smoke、历史 Workflow 或旧缓存作为隐式回退。按 [素材生成方案配置与验证](asset-generation-model-configuration.md) 分别核对：Deployment 的精确 operation、driver、发布/启用状态、target hash 与 smoke 到期时间；ComfyUI Workflow 的 current/published、validated Deployment、graph/execution/validation/fixture/compiled/target evidence、output kind、持久 Save 节点、权限以及 `workflow_input_schema` 是否能从当前版本完整投影。存在 required 输入本身不是排除条件。

选择 ComfyUI 方案后，页面按 `workflow_input_schema` 动态展示全部可编辑 string/integer/number/boolean/enum 参数、每个固定图片/视频/音频槽，以及按 `CONDITIONING_MODE` 切换的多图、多视频、音频、用途与内嵌/外部音轨配对选项。提交体统一为 `workflow_inputs={schema_version:2,values,assets,references}`；页面只传标量、Asset ID 和业务意图，不传本地路径、checksum、ComfyUI 节点值或高级 JSON。提示词、模型、输出前缀和 batch size 仍由服务端管理，batch size 固定为 1。服务端在排队与执行前使用同一公共验证器复核 required 输入、Asset owner/global 权限、active/MIME/SHA-256、数量、用途、互斥和配对，再经 semantic compiler、staging 与 conditioning/preprocessing adapter 生成冻结图。创建后 generation worker 以 job UUID 作为稳定 submission token，只执行冻结图；ComfyUI 队列/历史 missing 仍是 `uncertain`，不能当作“未提交”后自动重放。成功时必须从任务专属输出前缀取得唯一主输出，完成路径、类型、大小、checksum 与 FFprobe 校验后才物化 Asset。

素材生成复用上述短剧底层工作流能力，但不创建、不选择也不读取 ProductionRecipe。历史 ComfyUI 素材任务的请求快照 schema version 小于 3 时没有完整 `workflow_inputs` 冻结意图，即使状态为 failed/cancelled 也会明确拒绝自动 retry；操作员应从当前发布 Workflow 重新创建任务，不得猜测旧参数或引用素材。

生成图片按人物/场景/道具/通用分类供现有 Bible 与素材选择入口使用，生成视频和音频直接满足现有 Storyboard 参考素材合同。剧本素材在 ProductionRun 页面导入后只形成项目级 preview revision，仍需人工确认。素材生成不创建 Episode、Shot 或交付，也不替代 ProductionRun。

## 9. ProductionRun 操作

新建连续剧页面提供三种内容入口：

- 空项目：只建项目，不伪造脚本或生产成功。
- 导入脚本：粘贴或上传后先检查 `ScriptSource` 服务端预览，再显式确认 revision。规范脚本的 `script.structure` 产生确定性 no-submission 审核候选；非规范原文由已审核 route 转为覆盖全文的 preview revision。两者都在 required `script_review` 人工确认后才取代运行中的脚本事实。
- AI 创建：填写需求、集数、每集镜数和时长，同时选择已发布配方、外发授权、币种和预算。项目创建后用正式 ProductionRun 生成脚本，不经由瞬时草稿旁路。

只有需要执行的内容会创建 ProductionRun，而所有新的短剧生产一律从 ProductionRun 创建。运行前检查：

- 项目和内容事实正确；
- confirmed script 可用，或选择 AI script；
- AI script 和确定性 Bible 均完成人工审核；
- Ref2VA 配方含 `bible_image_generation/bible_image_review`，并已配置可用图片 route 与 Bible PromptProfile binding；
- workflow 已 validate、real smoke、publish；
- deployment/host 可用；
- resolved config 与 run snapshot 可追踪；
- route 与成本币种/授权明确；
- 受管存储可写。

有媒体阶段的运行必须显式选择 workflow route 与 PromptProfile binding。`uniform` 把同一 route 应用到全部选中镜头，并为每个实际 `operation+mode` 提交一条 binding ID；`per_shot` 必须为每个镜头提交 route 与 binding ID，页面可以先将选择批量应用到整集再逐镜修改。含 Bible image stage 时还要单独提交 `bible_image_prompt_profile_binding_id`。服务端检查完整覆盖、Shot 归属、workflow/deployment/model/mode/Profile/contract 兼容性并冻结 hash。AI/导入入口尚无物化 Shot 时只接受按项目默认视频 mode 构造的 `uniform`；无媒体阶段的纯脚本配方不提交 workflow selection。

### Bible 参考图审核与逐镜绑定

文本 Bible 只定义人物、场景和道具的语义事实，不等于已经具备一致性图片。Ref2VA 的正式顺序是：

1. `bible_image_generation` 使用本 Run 冻结的图片模型、route 和 Bible PromptProfile 为各实体生成模型候选。
2. 人工/辅助运行到达 `bible_image_review` 后，可直接选择模型候选，也可从素材库选择同类型图片，或混合选择两类来源。素材库选择会冻结页面看到的 SHA-256；文件或来源变化时必须刷新后重审。
3. 每个实体至少选一张、模型与素材库合计最多 32 张，并且只能指定一个主锚点。主锚点可以来自模型候选，也可以来自素材库，但必须属于已选集合；同一最终 Asset 不能同时用于多个实体。
4. 对模型结果不满意时，在当前实体点击“使用冻结 Bible 模型重新生成”。该操作只在人工/辅助运行的 `bible_image_review/awaiting_approval` 可用，每次给所选实体追加一个候选，旧候选不覆盖；每实体同一 Run 最多 8 个。未知费用会重新要求授权，不能沿用首次生成的未知费用许可。
5. 图片审核通过后，在 `storyboard_review` 按每个 Shot 的实际人物、场景、道具关系绑定该实体已批准的 Asset，并同时选择动态参考视频。每个 Ref2VA Shot 至少要绑定一张本 Run 已批准实体图；动态视频负责动作/镜头条件，实体图片负责身份、外观和空间锚点，两者缺一不可。
6. `prompt_compile` 会再次检查图片生成/审核快照、Shot 关系、Asset ID 和批准集合；缺失或越权引用会保持 blocked，不会退化为仅动态视频生成。

全自动运行不开放人工“重新生成”或素材库选图。策略要求每个实体恰好一个本 Run 当前模型候选，自动把它物化并设为锚点；候选为零、超过一个、字节/provenance 不完整，或 Shot 未使用全部已物化 Bible 实体时均阻断，等待修正配方、内容或运行输入后新建合规 Run。

所选 published recipe 必须已经通过完整 content → storyboard → prompt → media → selection → assembly → delivery 闭包校验；不要以缺阶段的“部分配方”启动正式 run。

运行中只在 ProductionRun 执行 approve、route selection、cost authorization、continue、artifact selection、retry、resolve-uncertain、cancel 或有序 fallback。短剧 Tasks、Deferred requests、Assemblies、ReleaseBundles 完全只读，没有任务级写例外。独立的素材 `AssetGenerationJob` 不属于上述 Task 投影：其取消、失败/已取消重试和 staff 人工消歧只在“生产工作流”页提供，成功产物或提示词候选返回素材库；不得把这些动作套用到短剧 Task。

“生产工作流”列表先按来源区分素材任务、短剧任务和独立媒体任务，再按状态筛选；`uncertain` 与 `cancelled` 保留独立语义，不伪装成普通失败。同一整集的短剧任务仍折叠为一个父项：正式运行只按冻结的 `production_run_id + episode_id` 合并，历史任务只按精确 `batch_id` 合并；缺少运行身份时保持单项，不能根据相同 Episode 或相近时间补组。父项状态、状态筛选和完成比例以每个镜头变体的最新尝试为准，展开后保留全部历史尝试用于审计。运行中父项默认展开、已完成父项初次加载默认折叠；通过任务深链进入时应展开所属父项。正式父项可进入所属 ProductionRun 继续处理，legacy 父项仅提供只读诊断；素材任务不进入短剧分组。

整集父项的进度只显示“已完成生产单元数 / 总生产单元数”。活跃执行采用不确定进度提示；ComfyUI 上报的是当前节点采样遥测，禁止跨任务求平均后伪装为整集完成百分比。轮询刷新必须保留操作员已经做出的展开/折叠选择，新增终态父项才应用默认折叠。

schema v1 的“自动生产”不会跳过人工门。各 worker 在文本、媒体或装配终态事实提交后幂等触发 auto-advance，状态机自动继续普通阶段；遇到 required review、路由选择、成本授权或候选选择时保持 waiting/blocked，等操作员通过正式 run action 决策。schema v2 的 unattended 运行则只执行已注册并冻结 hash 的 recipe policy；创建页始终要求用户明确确认自动审核，动态参考视频是否必选由所选 WorkflowTemplateVersion 的 `dynamic_reference_topology.videos.min_items` 决定，`min_items=0` 时可不带视频运行。之后脚本、文本 Bible、提示词标准化候选、每实体唯一 Bible 图片候选、逐镜实体锚点、合同允许时的动态视频绑定以及单产物选择可由策略推进。提示词标准化策略不会关闭由 `production.prompt_language_gate` 冻结的通用生产语言门禁：它只物化成功 attempt 的完整冻结执行语言候选；名称占位符必须精确等于 Character `stable_speaker_id`、`scene:{slug}` 或 `prop:{slug}`，再以同 scope visual identity、项目 glossary 或碰撞安全标签生成不超过 8 词/80 字符的 canonical。策略在全部候选中进行大小写敏感、边界完整、单次替换，未知、扩展、大小写漂移或替换后残留的 provisional token 会阻断；canonical 映射、派生方式、替换次数与 overrides hash 一并冻结到 policy evidence。任何空白、重复、漂移或校验失败都会在策略审批事实写入前回滚并阻断。异常、费用或远端不确定状态仍然阻断，且禁止把策略决定记录成用户人工批准。后置推进异常不会修改已持久的 worker 事实。

每次外部调用都必须在 run snapshot 中有显式授权、币种和预算上限。未授权、pricing 不可确定、预算缺失或超额会在提交前阻断。排查时通过 ProductionRun、StageRun、GenerationAttempt、route/cost event、PromptProfile 冻结引用和 artifact provenance 对账；不将密钥、完整提示词、远端响应或签名 URL 复制到日志/审计正文。

## 10. 媒体执行与上传边界

T2I/T2V/T2A 的目标环境验收必须覆盖 submit、poll、结果获取、managed archive 与 provenance。仅请求校验或 mock 不能作为 real smoke。

I2V/reference image 需要 provider-specific upload contract。当前标准执行器没有该契约；遇到 `provider_upload_contract_missing` 应保持 blocked，登记 provider 上传/引用格式后再开发和验证。禁止丢弃参考图、发送纯文本任务或回退旧执行器并显示成功。

workflow-backed AssetGenerationJob 接受工作流正式声明的固定和动态素材输入。固定图片/视频/音频通过 `workflow_inputs.assets` 的 semantic key 绑定；conditioning 合同声明的多图、多视频和音频通过 `workflow_inputs.references` 绑定，并按 `values.CONDITIONING_MODE` 选择准确模式。素材任务与 ProductionRun 复用 Asset 授权、语义编译、上传 staging 和媒体 preprocessing 实现，但分别冻结自己的运行事实。async REST 的 I2V/reference image 仍需要 provider-specific upload contract；ComfyUI 不依赖该远端协议，而是在服务端复核受管 Asset 后通过受控本地 staging 交给冻结图。两条路径都禁止客户端注入路径、上传名或任意节点值。

## 11. 受管产物与逐集交付

远端输出进入装配前必须：

- 通过地址、大小和媒体类型限制；
- 完成下载/读取；
- 写入 managed storage；
- 建立 generated artifact、attempt 和 provenance；
- 完成可播放性/结构校验。

归档时还会从冻结 task/attempt/stage、route、workflow 和 configuration 事实白名单固化 `comparison_parameters`。操作员比较候选参数时只读该字段；manifest 的任务 ID、对象键、输出索引和 hash 仅用于身份/provenance。

装配每次生成一个 episode master，并记录 source items。ReleaseBundle 汇总逐集 master 并执行 verify。系统不生成整季单条长视频；缺一集、来源不完整或 artifact 不可验证都必须阻断交付。

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

## 14. 发布前验证

```powershell
.\scripts\check.ps1

Push-Location frontend
npm run test:manual
npm run typecheck
npm run build
Pop-Location
```

随后在目标环境完成真实 provider/GPU E2E：授权、deployment probe、workflow real smoke、异步 API 与 ComfyUI 两类独立素材 T2I/T2V/T2A/reference-to-video submit/poll/archive、图片/视频/音频/剧本各自入库与下载/短剧复用、短剧媒体生成、episode master、ReleaseBundle verify 和恢复演练。ComfyUI 输入验收至少覆盖全部标量类型、多个固定媒体槽、单/多 `CONDITIONING_MODE`、多图/多视频/音频用途与配对、required/optional、越权或文件漂移阻断，以及历史 v2 快照 retry 拒绝。每个案例必须记录实际任务和 Asset 证据；仓库测试或文档更新只能证明合同，不足以证明目标基础设施生产就绪。
