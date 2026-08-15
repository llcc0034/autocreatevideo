# 本地 API

本文档记录 AutoPrompt Studio 当前业务页面直接使用的本地 REST 接口。完整机器可读 Schema 以根目录 `openapi.yaml`（与 `backend/openapi.yaml` 同步）为准；开发环境可在 `/api/docs/` 查看。本文只补充 OpenAPI 不便表达的调用顺序、副作用与失败处理。

## 鉴权与共同边界

- 基础路径：`/api/v1/`。
- 除登录、注册能力探测、健康检查和签名媒体读取外，接口使用 JWT Bearer 登录态。
- 普通用户只能读写自己的 Project 和 Asset；跨租户对象按接口语义返回 `400` 或 `404`，不能通过 UUID 绕过租户过滤。
- Asset 上传沿用 `asset_upload` 限流、租户总配额、实际文件格式/MIME/尺寸/像素校验。签名媒体 URL 默认有效 900 秒，过期后调用方应重新读取 Project 获取新 URL。

## 确定性拆解全剧母稿

页面“完整剧本与自动拆解”调用 `POST /api/v1/projects/{project_id}/breakdown-script/`。该接口不调用 Qwen 或 DeepSeek，而是按固定语法识别集、镜头、动作、对白和 Bible 名称，直接创建正式 Episode 与 Shot。

```text
第1集：雨夜来客
镜头1【时长=8秒】【首帧连续=否】【尾帧连续=是】：雨夜客栈外。
[0-3秒] 动作：沈砚推门进入。
[3-6秒] 沈砚（in a low, restrained voice）：谁在等我？
[6-8秒] 动作：他以稳定姿态停在柜台前。
```

- 镜头时长支持秒、毫秒和 `MM:SS.mmm`，写入 `Shot.duration_ms`。
- 动作时间段规范化后保存在既有 `Shot.narrative` 中；每行以相对镜头起点的 `[开始s–结束s]` 开头，继续走现有翻译审核与 H3 导演描述链路。
- 对白正文保持原文，时间段以整数毫秒写入每条 `dialogue.start_ms/end_ms`；H3 把时间说明放在 `<d>` 外。
- `requires_first_frame_continuity` 和 `requires_last_frame_continuity` 只表示首尾状态连续要求，不代表 `reference_bindings` 已有真实首帧或尾帧素材。
- 未写镜头时长或连续要求时使用请求中的拆解默认值；未写动作/对白时间时使用 `0–镜头总时长`。无效、倒序或越界的母稿指令会进入成功响应 `warnings[]` 并回退默认，不会中止其他有效镜头。

请求中的 `default_shot_duration_ms`、`default_first_frame_continuity`、`default_last_frame_continuity` 可覆盖本次缺省行为；页面默认值来自 `GET /api/v1/capabilities/production/`，不复制固定数值。成功返回 `201`，包含 `method`、`warnings`、剧集/分镜结果、计数及 `cleared` 清理摘要。

`replace_existing=true` 表示按当前剧本和当前 Bible 重建正式剧集。若现有剧集已经产生可清理的生产派生数据，首次请求返回 `409 script_rebuild_confirmation_required` 及待清理数量；用户明确确认后，以同一请求增加 `discard_existing_production=true`，服务端在一个事务中清理旧剧集/分镜、提示词与工作流、终态生成任务、组装记录、创作与逐镜扩写草稿、草稿发行候选和已失效翻译，再创建新剧集。项目、当前 Bible、素材、风格、全剧视觉规则、术语表及磁盘上的历史输出文件保留。

存在运行中或远端状态未决的生成任务、运行中的组装/整集扩写批次，或已封存、已发行、已替代的发行候选时，接口返回 `409 script_rebuild_blocked`，不执行任何清理或重建。

## 生成结构化分镜创作草稿

### 接口卡片

| 项目 | 内容 |
| --- | --- |
| 生成 | `POST /api/v1/projects/{project_id}/creative-drafts/generate/` |
| 通道 | `local_qwen` 或 `deepseek_api`；显式选择，不做静默回退 |
| 请求 | `episode_id`、`provider`、`target_shot_count`、`creativity`、可选 `variation_note` 与 `extended_generation_confirmed` |
| 成功 | `201`，返回隔离的 `EpisodeCreativeDraft`；不会直接修改正式 Shot |
| 页面入口 | 连续剧工作室 → 剧集与分镜 → 结构化双语分镜草稿 |

两种通道共用项目/剧集/Bible/术语、结构化输出和正式应用门禁。DeepSeek 会把本次生成所需上下文发送给外部 API；本地 Qwen 只在本机进程中处理。

页面先读取 `GET /api/v1/capabilities/creative-drafts/`。该接口的 `providers[]` 是镜数和容量策略的唯一权威来源；每行包含 `min_shots`、`recommended_max_shots`、`batch_shots`、每镜对白、剧集标题、对白正文与表演描述长度，镜头时长范围，输出 token 公式及余量，源/重生成/总输入字符预算、超时和就绪状态。每行还公开 14 个 `recommended_{title|narrative|camera|action|continuity|soundscape|music}_{zh|en}_chars` 审核建议，以及 `max_camera_movements`、`max_action_sequence_markers`、`min_script_action_coverage_percent`；它们只生成可确认的审核提示，不是输入或返回数据硬上限。本地 Qwen 行另返回 `context_size`、`min_context_size`、`max_context_size`、`reserved_input_tokens` 与包含清理余量的进程超时。根级只返回共同能力、默认 provider 和 provider 列表，不再复制本地 Qwen 限额。能力响应不包含 API key、模型目录或外部端点。

### 镜数策略与长任务确认

镜数策略来自所选 provider 的能力行，不再由页面写死。默认配置下，本地 Qwen 的推荐值与单次调用镜数均为 4 镜；DeepSeek 的推荐值为 4 镜、单次调用镜数为 1 镜。`recommended_max_shots` 是用户确认阈值，`batch_shots` 是初次生成与整稿重生成的一次模型调用镜数，两者相互独立且都只需不低于 `min_shots`；例如推荐 11、每次调用 4 镜是合法配置。`batch_shots` 不是单集、整剧或业务目标镜数上限，服务端和页面都不得据此缩小用户输入。部署方可通过正式环境配置修改这些值。推荐值只表示预计耗时和外部 API 成本开始明显增加，不表示生成必然失败。

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `target_shot_count` | integer | `4` | 正整数业务目标镜数；provider 的镜数范围是确认建议，不设写死的业务最大值 |
| `extended_generation_confirmed` | boolean | `false` | 当镜数超出建议范围、权威剧本超过单批参考值、对白密度超过建议值，或权威标题/对白长度超过 provider 建议值时必须为 `true`；服务端不会静默缩小镜数、截断剧本或删改对白 |

上述建议被触发但尚未确认时返回 `400`，稳定业务码为 `creative_generation_confirmation_required`。安全上下文会按触发原因公开镜数、批次、源字符数、对白数量/建议容量/实际所需容量及字段字符数；页面应准确显示预计耗时、DeepSeek API 用量及预计批次数可能增加，由用户选择取消或按原业务需求继续。确认后仍保留完整业务镜数、权威剧本和原文对白；不得把这些建议值当成整集业务拒绝条件。带 `shot_keys` 的选镜重生成不具备源文本拆批能力，仍按下文单次协议和可配置预算预检。

确认只批准本次长任务，不绕过真实单批 token、源上下文和重生成上下文预算。服务端会把确认状态及当时的 provider 镜数/批次策略写入草稿 `input_settings`，因此后续修改环境配置不会改写历史生成口径。

### 对白与输出容量预检

原剧本对白正文必须逐字保留。所选 provider 的 `max_dialogue_per_shot` 是默认密度建议，默认 2 句且可通过环境配置调整。若本次权威对白需要更高密度，首次请求返回 `creative_generation_confirmation_required`，并安全公开 `required_dialogue_count`、`dialogue_capacity`、`max_dialogue_per_shot` 与 `effective_max_dialogue_per_shot`；用户确认后，服务端按实际需要扩展本次结构 envelope，同时写入 `dialogue_density_high` 审核项，而不是丢弃结果。

服务端还会按“镜数 + 必须保留的原文对白 + provider 输出余量”计算真实输出预算。初次整稿 `generate` 和不带 `shot_keys` 的整稿 `regenerate` 会按 `batch_shots` 及真实预算，把剧本沿连续原文边界规划成一个或多个 provider 请求；不会缩小业务目标镜数。每批只处理自己的连续源片段，后一批携带前一批末镜的已校验连续性事实。所有可解析镜头都会保留并重新编号：对白缺失、多余或乱序时，服务端用权威 manifest 确定性回填并产生 `dialogue_manifest_repaired`；批次少返回镜头、镜号异常或后续批次失败时，已返回镜头以 `needs_review` 草稿保存，并产生相应风险项。只有尚未获得任何可物化镜头、正文为空/JSON 不可解析、上下文漂移或批次计划在调用模型前无法成立时才明确失败。

带 `shot_keys` 的选镜 `regenerate` 为了携带完整 `previous_draft` 并精确合并，仍沿用单次模型协议，不自动分批。其预算超过 provider token 配置时，在 HTTP 请求前返回 `creative_output_budget_exceeded` 或传输层 `deepseek_output_budget_exceeded`，并公开安全容量信息，不会把预算静默截到 `DEEPSEEK_MAX_TOKENS`。管理员应调整配置或选用具备足够能力的通道，不能要求用户减少单集/整剧的业务总镜数。默认 DeepSeek `DEEPSEEK_MAX_TOKENS=16384`，足以覆盖 11 镜及 13 条常规短对白的当前协议预算；更长对白仍以实际预检结果为准。后续 H3 媒体任务始终逐镜执行，与创作草稿批次相互独立。

## 整集逐镜高细节扩写与原子应用

### 接口卡片

| 用途 | Method 与 Path | 副作用 |
| --- | --- | --- |
| 创建/列出批次 | `POST/GET /api/v1/projects/{project_id}/shot-detail-draft-batches/` | POST 只冻结输入并入队，不在 HTTP 请求内调用模型 |
| 查看批次和逐镜对比 | `GET /api/v1/shot-detail-draft-batches/{batch_id}/` | 只读；`items[].before_snapshot` 是入队时事实，`items[].draft` 是候选或失败记录；应用完成后同时返回数据库最终 `applied_shots`，供客户端在应用响应丢失后恢复 |
| 整集应用 | `POST /api/v1/shot-detail-draft-batches/{batch_id}/apply/` | 统一预检后在一个数据库事务内确认并应用全部视频镜头；响应的 `applied_shots` 是数据库最终镜头值，`translation_written` 表示是否已写入确认英文 |

接口使用 JWT 和 Project 租户范围。`request_id` 是 UUID 幂等键；同一用户、项目、剧集和客户端请求重放返回原批次 `200`，首次入队返回 `202`，不同范围或请求复用同一键返回 `409 shot_detail_batch_request_id_conflict`。部署期间 provider policy 变化不会改变既有请求的幂等身份；原批次继续按其冻结的 `policy_snapshot` 执行。批次保存实际 resolved provider policy、全镜源清单指纹、逐镜源指纹、重试次数和错误，不保存密钥或外部端点。

### 创建请求、确认与容量口径

```json
{
  "episode_id": "00000000-0000-0000-0000-000000000000",
  "request_id": "00000000-0000-0000-0000-000000000001",
  "provider": "deepseek_api",
  "variation_note": "加强雨水在衣料和地面的层次",
  "confirmation_acknowledged": false
}
```

`GET /api/v1/capabilities/creative-drafts/` 的 `shot_high_detail_policies.{provider}` 是整集扩写预算和推荐镜数的权威来源；其中 provider 专用 `output_tokens`、`context_max_chars` 与 `recommended_max_shots` 已合并公共策略。`temporarily_busy=true` 或 `runtime_probe_error_code=shared_resource_busy` 只表示共享资源正在使用，仍允许入队，worker 稍后重试；只有 disabled、未配置、模型缺失或校验失败才返回 `503 shot_detail_provider_not_ready`。

当视频镜头数超过所选 provider 的 `recommended_max_shots`，服务端在创建任何批次数据前返回 `409 shot_detail_batch_confirmation_required`，并给出总镜数、视频镜数、推荐值和 `business_volume_limited=false`。调用方展示时间与费用提示后，可保持全部业务镜头不变并以 `confirmation_acknowledged=true` 重发。该推荐值不是单集、100 集短剧或整剧能力上限；非视频镜头会生成显式 `skipped` item，不会被伪装为视频扩写成功。

### 异步进度与部分生成失败

现有 generation worker 始终先领取 ComfyUI 媒体任务；没有可领取媒体任务时才串行领取一个高细节 item。每镜复用单镜 `ShotDetailDraft` 生成服务，状态为 `pending → running → succeeded | failed | stale`。模型返回可编辑候选时即使带审核项也保留为 `succeeded`；无法使用的输出、超时和 provider 错误按配置重试，并保存每次失败草稿。worker 中断后按 claim/heartbeat 恢复；已经提交 draft 但尚未更新 item 的情况通过 draft 关联恢复为成功，避免重复丢失结果。

批次可为 `completed_with_errors`，成功候选和失败镜头都保留给逐镜对比；失败不会回退到旧缓存、模拟候选或另一 provider。`policy_snapshot`、`pending_count`、`succeeded_count`、`failed_count`、`stale_count` 和 `skipped_count` 是当前进度口径。

### 整集原子应用

应用请求必须提交批次最新 revision，并逐项提交批次内全部非 skipped 视频镜头：

```json
{
  "expected_batch_revision": 7,
  "items": [{
    "item_id": "00000000-0000-0000-0000-000000000010",
    "draft_id": "00000000-0000-0000-0000-000000000011",
    "expected_revision": 1
  }],
  "language_mode": "bilingual_confirmed",
  "english_review_acknowledged": true,
  "review_acknowledged": true
}
```

服务端先锁定批次、剧集、全部 item/draft/shot，再统一校验：生成必须全部成功、item/draft 必须匹配、源指纹和 revision 必须当前、候选不得有 `requires_edit`。`draft` 与 `confirmed` 候选都可由此入口一键确认并应用。全部预检通过后才写 `Shot.detailed_description`、翻译审核和审计；所有镜头写完后再以最终相邻镜头状态重算每个 context hash。任何镜头预检或写入失败都会回滚整集，零镜头写入；不提供部分应用旁路。成功后批次 `apply_status=complete`。

主要错误：`409 shot_detail_batch_active_conflict`（同集已有活动批次）、`409 shot_detail_batch_not_fully_generated`（存在未完成/失败/陈旧镜头）、`409 shot_detail_batch_revision_conflict` 或单镜 revision/context 冲突、`400 shot_detail_requires_edit`/审核确认缺失、`503 shot_detail_provider_not_ready`。调用方必须保留对比弹窗和用户编辑，不得把失败响应显示为已应用。

当前后端验证覆盖：推荐镜数只确认不截断、幂等重放、共享资源忙入队、逐镜成功/失败保留、ComfyUI 优先、真实 runtime request 对首尾帧 boolean 和对白 `start_ms/end_ms` 的规范化，以及相邻镜头整集应用后的最终 context 与任一 revision 失败零写。

## 创建单集、选区或整剧生产任务

### 能力与请求

页面在提交前读取 `GET /api/v1/capabilities/production/`。返回的 `recommended_max_pending` 与 `recommended_max_active_per_user` 是当前队列的确认阈值，`max_concurrent` 是 worker 实际并发，另有当前全局/当前用户活跃任务数和任务超时。`video_shot_min_duration_ms` 与 `video_shot_max_duration_ms` 是正式保存镜头和 H3 生产共用的当前时长范围，页面必须动态读取，不得复制固定秒数。`business_volume_limited=false` 明确表示队列数字不是项目集数、单集镜数或整剧任务总量上限。

生产入口为 `POST /api/v1/projects/{id}/build-production/`。省略 `episode_ids` 表示按项目当前全部剧集构建；也可传一个或多个剧集，或用 `shot_ids` 进一步选择镜头。`whole_episode=true` 只接受一个剧集，并为该集每个已保存镜头创建一个可独立重试和审计的任务。所有模式都支持：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `episode_ids` | UUID[] | `[]` | 空数组表示当前项目全部剧集；不是固定前 N 集 |
| `shot_ids` | UUID[] | `[]` | 可选镜头子集；必须属于当前项目与所选剧集 |
| `whole_episode` | boolean | `false` | 整集一镜一任务模式；必须只选一个剧集 |
| `enqueue` | boolean | `true` | 是否创建持久生产任务 |
| `extended_generation_confirmed` | boolean | `false` | 仅在队列推荐阈值提示后由用户显式确认；不会提高并发或改变单镜参数 |
| `idempotency_key` | string | `""` | `enqueue=true` 且解析后包含多个生产单元时必填；单生产单元可省略。相同输入复用同一完整批次 |

### 队列确认与批次追踪

如果“当前活跃任务 + 本次待创建任务”超过任一推荐阈值，且请求未确认，服务端在创建 Prompt、Workflow 或 Task 前返回 `409`，稳定码为 `production_queue_confirmation_required`。`error.details` 包含 `requested_task_count`、`active_task_count`、`user_active_task_count`、`recommended_max_pending`、`recommended_max_active_per_user` 与 `max_concurrent`。页面必须展示任务数量和预计排队时间增加风险，由用户取消或再次以 `extended_generation_confirmed=true` 提交；不得自动确认，也不得把业务范围静默截断为阈值以内。

确认后的请求仍为每个生产单元创建完整任务集合。整剧页面始终发送会话内持久的 `idempotency_key`；网络响应丢失后会以同一键和请求参数重放。服务端在项目行锁内、写入任何 Prompt/Workflow/Task 前计算最终剧集、镜头、连续性、模板与参数指纹：同键同指纹返回原完整批次（`200`），同键不同指纹返回 `409 production_idempotency_conflict`，不会重复创建任务。`201` 正常响应和 `200` 幂等重放使用同一响应 Schema，并公开 `batch_id`、`batch_kind`、`batch_size`、`task_ids` 与 `production_units`：多集为 `series`，单集选区为 `selection`，整集一镜一任务为 `episode`；未提供幂等键且只有一个生产单元时 `batch_id` / `batch_kind` 可以为空，但 `batch_size` 仍表示本次生产单元数。任务详情中的批次索引必须为 `1..batch_size`，可用 `GET /api/v1/tasks/?batch_id=...` 查询完整批次。

单任务入口 `POST /api/v1/tasks/` 和失败/取消任务的 `POST /api/v1/tasks/{id}/retry/` 使用同一队列推荐策略。两者请求均包含默认 `false` 的 `extended_generation_confirmed`；超过推荐值但未确认时返回相同的 `409 production_queue_confirmation_required` 与安全容量详情，且不会创建新任务。调用方显示提示后可取消，或原样重发并设为 `true`。确认只允许继续排队：单任务创建不会改变生成参数，重试仍复用原任务已物化的工作流与参数，二者都不会提高 worker 并发。任务能力快照记录本次确认，便于审计。

## 上传并绑定连续剧封面

### 接口卡片

| 项目 | 内容 |
| --- | --- |
| 用途 | 为已存在的连续剧上传、绑定、替换或移除展示封面 |
| 上传 | `POST /api/v1/assets/`，`multipart/form-data` |
| 绑定/移除 | `PATCH /api/v1/projects/{project_id}/`，JSON |
| 鉴权 | JWT；Asset 与 Project 必须属于同一当前用户 |
| 页面入口 | 新建连续剧第 04 步；项目工作室页首“上传/更换封面”“移除封面” |
| 代码入口 | `AssetViewSet`、`SeriesProjectViewSet`、`repository.setProjectCover` |
| 当前状态 | 已使用；后端专项测试、OpenAPI、前端类型检查和构建已验证 |

这是一条两步写入链路：前端先创建专用 Asset，上传成功后再把其 UUID 绑定到 Project。若第二步失败，前端会删除刚上传且尚未绑定的专用 Asset；普通项目创建成功不会因后续封面失败而被伪装为创建失败或重复创建。

### 请求

第一步上传：

| 字段 | 类型 | 必填 | 口径 |
| --- | --- | --- | --- |
| `kind` | string | 是 | 固定 `reference` |
| `name` | string | 是 | 项目标题加“封面”的展示名称 |
| `file` | binary | 是 | JPEG、PNG 或静态 WebP；默认最大 10 MiB，并受服务端尺寸/像素/租户配额配置约束；入库前统一方向并剥离 EXIF/GPS |
| `metadata` | JSON string | 是 | `{"usage":"project_cover","project_id":"<project UUID>"}`；Project 必须属于当前用户 |

第二步绑定：

```json
{
  "cover_asset": "00000000-0000-0000-0000-000000000000"
}
```

移除封面使用同一接口并发送：

```json
{
  "cover_asset": null
}
```

### 响应

上传成功返回 `201` Asset。绑定或移除成功返回 `200` SeriesProject，核心字段为：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `cover_asset` | UUID 或 null | 当前持久封面 Asset；移除后为 null |
| `cover_url` | string | 当前短期签名预览 URL；没有封面时为空字符串，不持久化为项目事实 |
| `updated_at` | datetime | 项目最近更新时间 |

项目列表 `GET /api/v1/projects/` 直接返回这两个封面字段，不逐卡追加 Asset 请求。页面遇到签名 URL 加载失败时最多刷新一次 Project 数据获取新 URL，失败后回退到风格占位封面。

### 错误与失败处理

| 状态 | 场景 | 调用方处理 |
| --- | --- | --- |
| `400` | 跨租户/错项目 Asset、非 reference、停用素材、坏图、MIME/实际格式不符、动画图、配额或尺寸超限 | 保留当前有效封面；展示服务端错误，允许用户重新选择 |
| `401` | 登录态失效 | 清空租户状态并回到登录流程 |
| `404` | Project 或 Asset 不存在/不属于当前租户 | 不显示成功；刷新列表或提示对象已不存在 |
| `429` | 上传限流 | 保留本地待选文件与预览，稍后重试 |
| `5xx` | 服务端或存储异常 | 不改前端权威封面；上传已成功但绑定失败时尝试删除本次专用 Asset |

### 权限、安全与副作用

- 专用封面 Asset 的 `usage` 不可改写，不能原位替换文件；更换时必须上传新 Asset 后重新绑定。
- 专用封面不进入通用素材库，服务端拒绝其绑定到 Bible、Shot 或生成参考输入。
- 项目行与新封面 Asset 在同一事务中加锁。更换或移除时，旧封面数据库清理与 Project 更新同一事务提交；物理文件在提交后删除。
- 只有 `metadata.usage=project_cover` 且无 Project、Bible、Shot 或发行证据引用的专用 Asset 会自动清理。未标记的普通 reference 只解除关联，不自动删除。
- 删除 Project 会遵守活动任务、未终结远端提交和发行证据保护；成功删除后再清理无引用专用封面。

### 验证状态

- `studio.tests.test_series_project_cover_api` 覆盖上传/绑定/列表/签名读取、跨租户与错项目、坏图与动画图、替换/移除/项目删除/Asset 删除、事务回滚、专用素材隔离和列表查询数。
- `studio.tests.test_openapi_schema` 覆盖 `cover_asset` 可写 UUID/null 与 `cover_url` 只读契约。
- 前端 `npm run typecheck`、`npm run test:projects`、`npm run build` 覆盖字段适配、10 MiB/MIME 预校验、回滚路径与页面入口。

## 更正与删除图片素材

### 接口卡片

| 项目 | 内容 |
| --- | --- |
| 更正类别 | `PATCH /api/v1/assets/{asset_id}/`，提交 `kind: character | scene | prop` |
| 删除素材 | `DELETE /api/v1/assets/{asset_id}/` |
| 鉴权 | JWT；仅素材所有者或 staff 可写，全局内置素材只读 |
| 页面入口 | 素材参考详情中的“编辑素材信息”“删除素材” |

用户误选类别时，可以在人物、场景和道具图片之间更正。普通通用图片、音频、视频、生成产物和连续剧专用封面不能通过此接口转换类别；分类图片在更新后仍必须保留基础描述与视觉锚点。

为了避免静默破坏连续性，已被连续剧封面、人物/场景/道具 Bible、分镜参考、排队或运行中的生成任务、封存或历史发行清单引用的素材不能改类或删除。调用方应展示服务端返回的具体引用原因，并要求先解除引用或等待任务终结；失败时不得乐观更新列表。

删除未被引用的自有上传素材返回 `204`。数据库事务提交后会同时清理对应上传文件；若事务回滚，文件保持不变。前端删除前要求输入完整素材名称确认，服务端成功后才从列表移除。
