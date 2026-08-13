# 系统架构

## 1. 架构目标

AutoPrompt Studio 使用 Vue 负责制作交互，Django REST API 负责业务边界与持久化，数据库承担任务队列和恢复状态，独立 worker 调用 ComfyUI 与 FFmpeg。架构优先保证租户隔离、人工审核、输入可追溯、单 GPU 串行执行、提交幂等和中断恢复。

```mermaid
flowchart LR
    U["Vue 制作台"] -->|"JWT / REST"| A["Django API"]
    A --> D["SQLite 或 PostgreSQL"]
    A --> M["租户素材与签名媒体"]
    A --> Q["PromptArtifact → GeneratedWorkflow → GenerationTask"]
    Q --> GW["生成 worker"]
    GW --> C["ComfyUI / MiniMax H3 / SDXL"]
    C --> O["镜头产物与来源清单"]
    O --> AQ["AssemblyJob"]
    AQ --> AW["装配 worker"]
    AW --> F["FFmpeg 单集 / 整剧 MP4"]
    L["本地 Qwen"] --> A
    X["显式授权的外部文本服务"] -.-> A
```

## 2. 运行组件

| 组件 | 职责 | 持久状态 |
| --- | --- | --- |
| Vue/Vite 前端 | 项目、Bible、分镜、候选审核、提示词预览、任务监控、产物与装配操作 | 浏览器仅保存交互状态，业务事实以 API 为准 |
| Django REST API | JWT、租户过滤、校验、候选门禁、提示词编译、工作流物化、任务入队、签名媒体和 OpenAPI | Django 模型与文件存储 |
| SQLite/PostgreSQL | 业务对象、不可变生产输入、任务状态、心跳、互斥租约、幂等键、来源选择 | 开发可用 SQLite，生产要求 PostgreSQL |
| 生成 worker | 串行领取 GenerationTask、暂存素材、实时契约预检、提交/轮询/取消/恢复 ComfyUI 任务、封存产物来源 | GenerationTask、ComfyUI prompt ID、暂存映射与输出清单 |
| ComfyUI | 执行 MiniMax H3 视频图和 SDXL 图片图，提供节点/模型枚举、队列、进度、执行记录和输出 | 独立进程及其 input/output 目录 |
| 装配 worker | 领取 AssemblyJob、维护心跳、执行 FFmpeg、原子发布成片、封存成片来源 | AssemblyJob、ffconcat、MP4 与来源清单 |
| 本地 Qwen 子进程 | 翻译、剧集创意、逐镜扩写、视觉规则和素材 Bible 候选 | 候选记录或浏览器待审数据；进程按需启动并退出 |
| Nginx 与外部 TLS 代理 | 前端静态文件、API 转发、内部素材路由、生产 HTTPS 边界 | 无业务状态 |

API 不承载长时间 GPU 或 FFmpeg 进程。浏览器只负责发起操作和读取状态，页面生命周期不影响持久任务。

## 3. 领域模型

### 3.1 租户与共享资源

- `User` 是租户标识。用户业务对象直接保存 `owner`，或通过 `project.owner` 解析归属。
- `Style`、`Asset`、`WorkflowTemplate` 的 `owner=null` 表示全局资源；普通用户只能读取全局资源。
- 普通用户可维护自己的 Style 与 Asset，但只有 staff 可写 WorkflowTemplate；普通用户的可执行范围进一步收敛为已启用的全局系统模板。
- 用户上传路径由服务端生成为 `assets/{owner}/{year}/{month}/{uuid}.{ext}`，不采用客户端文件名作为目录结构。

### 3.2 连续剧聚合

```text
SeriesProject
├─ Episode (project + number 唯一)
│  └─ Shot (episode + number 唯一)
├─ CharacterBible (project + slug、speaker ID 唯一)
├─ SceneBible (project + slug 唯一)
├─ PropBible (project + slug 唯一)
└─ ConsistencyLock (scope + target + key 唯一，带 version)
```

- Project 保存生产规格、风格、连续性种子、术语表和视觉规则。
- Episode 保存剧本单元和跨集连续性边界。
- Shot 保存生产最小单元。`media_type` 决定视频或图片链路，`h3_mode` 与 `reference_bindings` 决定视频输入语义。
- Bible 的 anchor/reference assets 是身份锚点；锁定的 Bible 只能通过显式解锁后修改。
- `sync_consistency_locks` 从 Project、Style 和 Bible 生成版本化快照，并维护稳定的 Picture/Subject 槽位。提示词保存该快照，生产不会在执行时重新猜测身份。

### 3.3 候选模型

- `PromptFieldTranslation` 以 scope、字段、源文哈希、上下文哈希、候选译文、状态和修订组织逐字段翻译。
- `EpisodeCreativeDraft` 保存整集分镜候选、上下文快照、校验结果、确认者和应用者。
- `ShotDetailDraft` 保存单镜高细节候选、相邻连续性边界、相关 Bible 快照、确认者和应用者。
- 全剧视觉规则候选由请求返回；应用接口使用上下文指纹和生成指纹复核后写入允许字段。
- 素材 Bible 候选由素材的文字元数据生成；返回的 provenance 包含素材 checksum、元数据哈希和更新时间，正式保存时再次复核。

候选生成与正式写入分离。确认和应用使用数据库事务、行锁、`expected_revision` 或上下文指纹，避免过期页面覆盖已更新内容。

## 4. H3 模式与模板路由

### 4.1 有效输入模式

`Shot.H3Mode` 定义 T2VA、I2VA、L2VA、FL2VA 和 Ref2VA。

- T2VA：`reference_bindings` 必须为空。
- I2VA：必须且只能有 `first_frame_asset_id`。
- L2VA：必须且只能有 `last_frame_asset_id`。
- FL2VA：允许首帧、尾帧或两者；执行时按实际槽位计算有效模式，无图为 T2VA，单首帧为 I2VA，单尾帧为 L2VA，双帧为 FL2VA；双帧不能引用同一素材。
- Ref2VA：不使用首尾帧字段；分类图片、参考视频、独立音频和视频配对音频分别限量，素材 ID 必须唯一并属于用户可访问范围。

`resolve_group_reference_bindings` 把语义绑定转换为有序记录。图片使用连续 `<Picture N>`，视频和音频使用连续索引；工作流只为实际记录创建 `LoadImage`、`LoadVideo → GetVideoComponents` 或 `LoadAudio` 节点。

### 4.2 五份视频模板

| slug | `workflow_mode` | 源工作流 | 成熟度 |
| --- | --- | --- | --- |
| `minimax-h3-base-video` | `text_to_video` | `video_minimax_h3_i2v.json` 的安全审核 API 等价图 | 默认 |
| `minimax-h3-first-last-video` | `first_last_to_video` | `video_minimax_h3_i2v.json` 的安全审核 API 等价图 | 默认 |
| `minimax-h3-reference-video` | `reference_to_video` | `video_minimax_h3_r2v.json` 的安全审核 API 等价图 | 默认 |
| `minimax-h3-r2v-t8-dual-clock-4step` | `reference_to_video` | `video_minimax_h3_r2v_t8_dual_clock_4step.json` 的安全审核 API 等价图 | 实验；未通过 GPU 端到端生成验收 |
| `minimax-h3-r2v-turbo-lora` | `reference_to_video` | `video_minimax_h3_r2v_turbo_lora.json` 的安全审核 API 等价图 | 实验；未通过 GPU 端到端生成验收 |

模板声明允许模式、模型、节点、参数范围、参考策略和输出类型。路由优先使用请求中显式选择且兼容的模板，再选择相应模式的默认模板；不兼容、未启用或不可执行时立即失败，不做隐式降级。

图片镜头使用独立的 `minimax-reference-image` 系统模板和固定 SDXL Base 1.0 checkpoint。该图是文本与 Bible 锚点条件的文生图，不连接 Bible 图片像素。

## 5. 生产对象链

### 5.1 PromptArtifact

PromptArtifact 是提示词编译结果，保存：

- Project、Episode、可选 Shot 与创建者；
- 视频/图片类型、H3 请求模式、有效条件模式、是否多镜头与音频开关；
- 实际 `prompt_text`、负面提示词、结构化负载和连续性快照；
- 分镜 ID、时长覆盖、参考绑定覆盖、编译器/输入指纹版本、输入指纹；
- 递增修订号和 `SHA-256(prompt_text)` 内容哈希。

API 只读。生产构建可复用完全匹配的记录；任一输入、连续性、参考绑定、编译器版本或内容哈希不匹配时创建新修订。提示词优化器配置不会在后台改写记录。

### 5.2 GeneratedWorkflow

GeneratedWorkflow 将一个 PromptArtifact 注入一个版本化 WorkflowTemplate，保存确定的 API-format `workflow_json`、`injected_values` 和内容哈希。该记录同样通过只读 API 暴露。

工作流生成阶段用于预览和规划，可导出 API JSON 或转换为 ComfyUI UI 画布。同步到 ComfyUI 工作流库是显式操作，只保存画布，不入队执行。

### 5.3 GenerationTask

GenerationTask 引用 GeneratedWorkflow，并冻结执行参数：

- backend、model profile、模板版本、seed；
- 视频有效帧数和时长；
- 分辨率档位、尺寸倍数、宽高、实际 H3 条件模式和用户模板参数；
- 参考素材 ID/checksum/输入槽摘要；
- 批次、幂等、重试和实时契约预检快照；
- 状态、心跳、尝试次数、提交 token、ComfyUI prompt ID、进度和错误。

任务输入在创建后不变。状态与运行遥测可更新。重试创建新的 GenerationTask，并用 `retry_of` 指向来源任务。

### 5.4 Output、Assembly 与 Release

- Output 不是独立数据库表，而是 GenerationTask 的 `outputs` 列表。每项保存 ComfyUI 文件名、子目录、媒体类别、来源清单、SHA-256，以及可选的素材提升或受管发行状态。
- `persist_task_provenance` 在任务成功前封存任务、模板、Prompt 哈希、输入媒体哈希和输出哈希；封存失败时任务不能标记成功。
- 图片 Output 可复制到用户 Asset，重复请求返回同一提升结果，不修改 ComfyUI 母版。
- `AssemblyJob` 选择成功视频任务，保存镜头顺序、输出路径、ffconcat、FFmpeg 参数、源签名、状态和领取令牌。
- `ReleaseBundle` 通过持久 ID 显式选择镜头产物与装配母版；封存后不可修改选择。

## 6. 生成时序与契约预检

1. API 在项目行锁内同步 ConsistencyLock，编译或复用 PromptArtifact。
2. API 依据媒体类型、H3 模式和显式选择解析 WorkflowTemplate，物化 GeneratedWorkflow。
3. 语言门禁、队列容量和整集幂等检查通过后，API 创建 GenerationTask。
4. 生成 worker 取得本地 Qwen 共享资源锁，再通过数据库互斥领取一条任务。
5. worker 对每个参考素材重新核对 owner、授权快照、允许根目录和 checksum；视频统一暂存为 CFR 24，配对音频按有效视频时长归一化。
6. worker 将每次成功上传的 token → ComfyUI 路径立即写入 `staged_inputs`，重启后可按内容哈希验证并复用。
7. worker 对完整执行图调用实时 `/object_info` 与模型枚举，校验节点类、必需/自增长输入、输出槽和类型、连线、数值范围、widget/模型枚举及环路。
8. 预检通过后生成规范 UUID，先持久化为 submission token，再用同一 ID POST `/prompt`。
9. worker 使用 WebSocket 读取可用的节点进度，同时以 HTTP queue/history 作为终态依据。
10. 成功后提取输出、封存来源清单并更新任务终态；取消、明确失败、遥测不可用和连接不确定分别保存可区分状态。

实时模板可见性检查和执行图预检都不提交 GPU 任务，也不构成 GPU 端到端验收。T8 Dual-Clock 与 Turbo LoRA 的架构状态保持实验。

## 7. 并发、幂等与恢复

### 7.1 领取与容量

- `WorkerLease` 为生成容量、生成领取、装配入队和装配领取提供数据库互斥。
- 生成任务按优先级和入队时间领取，`TASK_MAX_CONCURRENT` 默认约束单 GPU 串行执行；API 同时限制全局待处理量和单用户活动任务量。
- 本地 Qwen 与生成 worker 共享文件锁。Qwen 占用资源时生成任务保持 queued；生成任务持锁时本地 Qwen 请求返回资源忙。

### 7.2 整集幂等

整集生产把项目、剧集、连续性快照、顺序分镜、参考绑定、分辨率、模板选择和参数规范化后计算请求指纹。`idempotency_key + request_fingerprint` 唯一标识批次：

- 相同键与相同指纹返回同一完整子任务集合；
- 相同键与不同指纹返回冲突；
- 批次不完整时拒绝复用并要求新的键；
- 整集批次固定为一镜一子任务，便于独立重试和审计。

### 7.3 ComfyUI 提交恢复

- submission token 在 POST 前持久化，避免 API 返回丢失导致无法识别远端任务。
- 已有 token 的任务先查询 ComfyUI history 与 queue；存在时接续同一 prompt，不再次 POST。
- POST 响应不确定、远端健康不可确认或 token 无法核实时，任务进入失败/降级状态并保留 token。队列的 unresolved-token 断路器阻止继续领取，避免同一 GPU 上存在不可见任务时叠加工作。
- 失联领取在没有 token 时可安全重新排队；有 token 且远端存在时恢复；有 token 但远端明确不存在时停止自动重投，由人工核对后决定重试。
- 重试前必须证明远端明确失败、明确取消或在健康 ComfyUI 中不存在；远端仍在执行或已有成功结果时拒绝克隆任务。

### 7.4 FFmpeg 装配恢复

- 装配源签名包含 owner、Project、Episode、顺序 Shot、任务 ID、相对输出路径、裁切窗口、输入 SHA-256 和编码/标识参数。
- 相同源签名复用 queued、running 或文件与来源清单均有效的 succeeded 作业。
- assembly worker 用领取 UUID 写入 `.part-{token}`，FFmpeg 成功且文件非空后以 `os.replace` 原子发布。
- worker 定期更新心跳。失联作业若最终文件及来源清单有效则标记成功；否则在尝试上限内重新排队，达到上限后失败。领取令牌对应的临时文件会被清理。

## 8. 安全架构

### 8.1 身份与对象授权

- DRF 默认使用 JWT 和 `IsAuthenticated`。
- queryset 先按 owner 或 `project.owner` 收敛，object permission 再允许 owner 或 staff。
- 全局对象只在安全方法中向普通用户开放；系统模板写入只允许 staff，模板执行只允许 staff 或已启用的全局系统模板。
- 注册、上传和通用请求分别受限流策略控制。

### 8.2 文件与路径

- 图片通过 Pillow 验证实际格式、MIME、尺寸和像素数；视频/音频通过 ffprobe 验证流、MIME、扩展名和 2–15 秒时长。
- 非 staff 不能设置 `local_path`。staff 路径也必须位于 `ASSET_LIBRARY_ROOT` 或 `MEDIA_ROOT`。
- 任务参考输入只接受全局素材或任务 owner 的上传；绝对路径在读取前解析，并限制到素材库或 `media/assets/{owner}`。
- ComfyUI Output 使用服务端保存的 filename/subfolder 在配置输出根下解析；Assembly 使用 `media/assemblies/{owner}/{project}`；所有读取均复核 `is_relative_to`。
- 装配输出名只允许无目录的安全 `.mp4` 文件名；FFmpeg 使用参数数组启动，不经过 shell 拼接。

### 8.3 签名媒体与来源完整性

浏览器 `<img>`、`<video>` 不携带 Bearer header，因此 API 生成短期签名 token。token 包含 purpose、object ID、owner ID 和按用途需要的 output index；媒体端点匿名可达，但必须同时通过：

1. Django 签名与有效期校验；
2. purpose 与对象类型校验；
3. owner、对象终态和 output index 校验；
4. 允许根目录与文件存在性校验；
5. 来源清单签名、产物 SHA-256 和相关源文件 SHA-256 校验。

任一校验失败统一返回不可用。响应使用 `private, no-store`、`nosniff` 和按用途限定的下载策略；生产中的 ComfyUI 母版只预览，公开下载使用受管发行副本。

### 8.4 ComfyUI 执行边界

- `COMFYUI_BASE_URL` 必须是 HTTP(S)。默认仅允许 localhost 或 IP loopback，远端地址要求显式 `COMFYUI_ALLOW_NON_LOOPBACK=true` 并由网络策略保护。
- 非 staff 不能把任意保存图变成执行路径。系统模板使用服务端节点白名单；扩展节点只通过管理员审核后的 `COMFYUI_EXTRA_ALLOWED_NODE_TYPES` 加入。
- 模板构建先验证无危险节点、无未知变量、图结构和模式契约，执行前再用目标 ComfyUI 的实时能力验证。

## 9. 外部网络与信任边界

| 边界 | 默认 | 显式启用后的数据流 |
| --- | --- | --- |
| ComfyUI | loopback | 可连接受控远端 HTTP(S) ComfyUI；工作流、暂存输入和任务元数据发送到该节点 |
| 本地 Qwen | 本地一次性子进程 | 无候选内容外发；模型安装需要单独下载模型与运行时 |
| DeepSeek API | 关闭且不选择 | 按次选择后，经 HTTPS 发送完成翻译/创意/扩写所需的项目、剧本、Bible、连续性或提示词上下文 |
| H3 Prompt Optimizer | 关闭 | 配置 HTTPS、API key 与非 loopback 授权后发送导演描述；对白、歌词和屏显文字以不可读占位符传输 |
| MiniMax Partner API 节点 | 不作为平台回退 | 平台不在本地失败时自动调用；采用外部付费链路需要独立、明确的产品授权与实现 |
| Web 公网入口 | 应用只监听受控地址 | 生产由可信 TLS 反向代理覆盖 Host、X-Forwarded-For 和 X-Forwarded-Proto 后转发到回环 Nginx |

外部服务密钥只从后端环境或 secret 注入，不返回浏览器、不写入 PromptArtifact、任务能力快照或来源清单。外部通道失败必须保留实际 provider 和错误，不能切换到未选择的通道。

## 10. 部署与可观测性

- 本地开发由 Django、生成 worker、装配 worker、Vue 和独立 ComfyUI 组成，入口绑定 `127.0.0.1`。
- Docker 生产由 PostgreSQL、Django/Gunicorn、生成 worker、装配 worker、readiness 和非 root Nginx/Vue 组成；ComfyUI 作为受控宿主机或私有网络进程。
- 容器使用只读根文件系统、非 root 用户、`no-new-privileges` 和删除 Linux capabilities；Nginx 只发布回环端口。
- `/api/v1/health/live/` 表示 API 进程存活；`/api/v1/health/ready/` 汇总数据库、迁移、存储、ComfyUI、生成 worker 和装配 worker。
- worker 持久化 heartbeat、claim token、远端状态、进度来源和错误。运行日志进入 `.runtime/logs/`，生产监控应采集队列深度、任务耗时、重试、失联 token、GPU/RAM、磁盘、装配失败和签名媒体拒绝。

## 11. 架构不变量

以下条件是实现和验收必须保持的不变量：

1. 正式 Project/Episode/Shot/Bible 只能由用户编辑或经人工审核后的显式应用写入；模型候选不能直接进入生产事实。
2. 任何 GenerationTask 都能追溯到一个 PromptArtifact、一个 GeneratedWorkflow、一个模板版本和一组确定的输入快照。
3. PromptArtifact 与 GeneratedWorkflow 不在后台改写；输入不匹配时建立新记录。
4. 普通用户不能执行任意 ComfyUI 图，也不能读取跨租户素材、任务或产物。
5. 参考素材语义必须来自显式绑定；未选输入不接线，服务端路径不从提示词或客户端路径猜测。
6. GPU 提交前必须通过语言门禁、模板兼容性、路径授权、素材哈希与实时 ComfyUI 契约校验。
7. 一个 submission token 最多对应一次 `/prompt` 提交；不确定状态必须停止自动重投。
8. 重试创建新任务，不能覆盖来源任务；成功产物在来源清单封存前不能标记成功。
9. 装配只能使用覆盖目标镜头的成功视频任务，并通过临时文件与原子替换发布最终 MP4。
10. 媒体访问必须同时满足短期签名、owner、允许目录与内容完整性校验。
11. 远端 ComfyUI、DeepSeek 和提示词优化器均需要显式授权；系统不执行未选择的外部回退。
12. `minimax-h3-r2v-t8-dual-clock-4step` 与 `minimax-h3-r2v-turbo-lora` 保持实验标识，且不得以节点/模型可见性代替 GPU 端到端生成验收。
