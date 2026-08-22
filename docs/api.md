# 本地 API

本文档记录 AutoPrompt Studio 当前业务页面直接使用的本地 REST 接口。完整机器可读 Schema 以根目录 `openapi.yaml`（与 `backend/openapi.yaml` 同步）为准；开发环境可在 `/api/docs/` 查看。本文只补充 OpenAPI 不便表达的调用顺序、副作用与失败处理。

## 鉴权与共同边界

- 基础路径：`/api/v1/`。
- 除登录、注册能力探测、健康检查和签名媒体读取外，接口使用 JWT Bearer 登录态。
- 普通用户只能读写自己的 Project 和 Asset；跨租户对象按接口语义返回 `400` 或 `404`，不能通过 UUID 绕过租户过滤。
- Asset 上传沿用 `asset_upload` 限流、租户总配额、实际文件格式/MIME/尺寸/像素校验。签名媒体 URL 默认有效 900 秒，过期后调用方应重新读取 Project 获取新 URL。

## 新建项目前生成故事基础候选

页面“新建连续短剧 → 故事基础”可调用 `POST /api/v1/series-concept-drafts/generate/`，根据一段创作想法一次生成相互一致的项目名称、一句话梗概、角色/场景/道具 Bible 候选和完整剧本候选。该接口是瞬态、只读的提案入口：不会创建或修改 `SeriesProject`、Episode、Shot 或 Bible，也没有自动保存、自动确认或自动拆解的旁路。页面必须把返回内容保存在独立的可编辑候选区；只有用户点击“确认并填入故事基础”后才复制项目名、梗概和完整剧本到表单，最后仍由既有 `POST /api/v1/projects/` 完成唯一正式创建。`candidate.bible` 与 `master_script` 内的 `SCRIPT_BIBLE_V1` 前置块来自同一份结构化 IR，用于人工预览和创建项目后的 Bible 审核，不是单独落库入口。

```json
{
  "idea": "失忆剑客在雨夜客栈追查师门血案，却发现掌柜正是故人。",
  "provider": "local_qwen",
  "episode_count": 3,
  "shots_per_episode": 3,
  "episode_duration_seconds": 60,
  "style_hint": "武侠；冷峻、克制的雨夜氛围",
  "attempt": 1
}
```

- `provider` 只接受 `local_qwen` 或 `deepseek_api`，按本次请求显式选择；任一通道失败都不会静默切换到另一通道。
- `idea` 最多 6000 字符，`style_hint` 最多 2000 字符；`episode_count`、`shots_per_episode` 和 `episode_duration_seconds` 只要求正整数，不设置 4 集、4 镜、12 镜等产品数量上限。严格请求 Serializer 拒绝未知字段。
- 页面复用 `GET /api/v1/capabilities/creative-drafts/` 的 `providers[]` 就绪状态和 DeepSeek 数据外发提示。根级 `series_concept_draft_preferred_provider` 是此操作的配置推荐；每行 provider 的 `series_concept_draft_suitability` 为 `recommended` 或 `experimental`，并带受控的 `series_concept_draft_suitability_reason_code`（`series_concept_strict_structure_recommended` 或 `series_concept_strict_structure_experimental`）和安全展示文案 `series_concept_draft_suitability_notice`。这些字段只帮助人工选择，不能拒绝请求、读取硬件或模型名推断能力，也不会覆盖用户显式选择。每行还公开该操作专用的 `series_concept_draft_timeout_seconds` 与 `series_concept_draft_max_output_tokens`；它们描述当前运行时/模型能力，不是按集数或镜数计算的业务上限。能力响应另公开 `series_concept_draft_*` 支持标志、结构版本、默认单集时长以及生产允许的单镜时长范围；不返回密钥、模型目录或外部端点。
- 每集镜头数乘生产最短单镜时长不得大于 `episode_duration_seconds`；这是所选生产链路的物理可行性校验，不是模型或产品镜数上限。页面默认单集 60 秒，实际默认值以能力响应为准。
- DeepSeek 只接收本次创作想法、所选风格文本和生成规模；本地 Qwen 使用共享资源锁，按需加载并在请求后退出。

模型只返回紧凑的结构化 `bible + episodes/shots` IR，并为每镜建议当前生产范围内的 `duration_ms`；三类 Bible 数组不设数量上限，服务端执行字段、跨类型 NFKC 名称唯一、镜头数量与对白校验。若某集建议总时长超过用户预算，服务端保留每镜生产最短时长并按相对建议确定性压缩，再把 Bible IR 渲染为闭合的 `SCRIPT_BIBLE_V1` 前置块，把剧集 IR 渲染为 `script_breakdown.py` 的权威显式语法。响应示例：

```json
{
  "source_fingerprint": "64位sha256",
  "candidate": {
    "title": "听雨不归人",
    "logline": "……",
    "bible": {
      "characters": [
        {
          "name": "沈砚",
          "description": "失忆剑客",
          "visual_identity_en": "young Chinese swordsman, rain-soaked dark robe"
        }
      ],
      "scenes": [
        {
          "name": "雨夜客栈",
          "description": "关键调查地点",
          "visual_identity_en": "ancient roadside inn at night, heavy rain"
        }
      ],
      "props": []
    },
    "master_script": "【SCRIPT_BIBLE_V1】\n【人物】沈砚｜失忆剑客｜young Chinese swordsman, rain-soaked dark robe\n【场景】雨夜客栈｜关键调查地点｜ancient roadside inn at night, heavy rain\n【/SCRIPT_BIBLE_V1】\n\n第1集：雨夜来客\n镜头1【时长=8秒】【首帧连续=否】【尾帧连续=是】：……"
  },
  "format": {
    "schema_version": 2,
    "template_id": "deterministic-script-breakdown-v3",
    "compatible": true,
    "episode_count": 3,
    "shot_count": 9,
    "episode_duration_seconds": 60,
    "total_duration_ms": 71000,
    "shot_durations_ms": [[8000, 6000, 9000], [5000, 10000, 7000], [12000, 6000, 8000]]
  },
  "resource_estimate": {
    "total_shots": 9,
    "estimated_input_chars": 2100,
    "estimated_output_tokens": 2312,
    "configured_output_token_hint": 3072,
    "output_hint_utilization_percent": 76
  },
  "warnings": ["候选包含对白；创建项目后请先人工确认并应用 SCRIPT_BIBLE_V1 人物候选……"],
  "runtime": {
    "provider": "local_qwen",
    "model": "Qwen/Qwen3-4B-GGUF",
    "transport": "local_process",
    "elapsed_ms": 1234,
    "review_required": true,
    "persisted": false
  }
}
```

`resource_estimate` 是所选模型的提示，不是业务门禁：即使 `output_hint_utilization_percent` 超过 100%，服务端仍把完整规模作为一次业务/API 提交，不截断、不按集或镜头自动分批；本地 llama.cpp 不设置人为输出 token 上限，仅受部署配置的实际上下文容量约束。DeepSeek 为该操作发送模型能力配置 `DEEPSEEK_SERIES_CONCEPT_MAX_TOKENS`（当前 V4 Flash 示例为 384000）并使用独立长超时 `DEEPSEEK_SERIES_CONCEPT_TIMEOUT_SECONDS`（默认 900 秒）；二者都不根据集数、镜数或估算 token 动态缩小，也不在调用前形成业务拒绝。本地首轮输出若无法解码为 JSON 或完整结构校验失败，运行时最多额外执行一次携带原始完整请求的整份结构修复，仍重新生成全剧而不是补丁或分段。修复成功和确定性空白恢复分别通过 `warnings[]` 中包含 `series_concept_structure_repaired`、`series_concept_structure_whitespace_recovered` 对应文案向人工显式提示；两轮的 `prompt_tokens`、`completion_tokens`、`total_tokens` 会聚合为同一份安全运行诊断 usage。实际能否完成由所选模型、上下文、运行时、硬件或远端服务决定。一般内容质量提示、无对白、时长压缩、母稿超过当前拆解参考预算或资源估算较高以 `200 + warnings[]` 返回，不阻止用户编辑和采用。`400` 只表示输入无效或单集时长物理不可行，`409` 表示本地共享资源占用，`503` 表示所选通道未启用、未配置或运行时不可用，`502` 表示模型加载、推理、JSON 解码、结构校验或上游调用没有获得可安全展示的候选；DeepSeek 返回 `finish_reason=length` 时使用明确错误码 `series_concept_provider_output_truncated`，不能伪装成一般 JSON 错误。失败不会清空页面上一次成功候选，也不阻止用户手工填写并创建项目。

`502`/`503` 的 `error.details` 包含默认展示的安全 `detail` 和 `diagnostic` 摘要。摘要稳定提供 `diagnostic_id`、provider、错误码、失败阶段，并在上游提供时附带 `finish_reason` 与数值 usage。`diagnostic.details_available=true` 时页面可让用户主动展开 `details`：这里只允许尝试次数、耗时、HTTP 状态、当前连续剧模型输出能力/超时、校验路径，以及输出字符数、UTF-8 字节数、SHA-256、JSON 解码位置和脱敏 JSON 结构轮廓。服务端不会保存或返回创作想法、风格文本、Authorization、密钥、外部响应正文或模型原始文本；普通日志行也只写摘要，结构化日志通过同一 `diagnostic_id` 关联可展开的安全字段。

这里的 `master_script` 是后续确定性拆解的全剧母稿，不是直接提交给 MiniMax H3 的单镜成品提示词，因此不套用 H3 每个文本输入 7000 字符的上限。后续拆解服务使用 `SCRIPT_BREAKDOWN_MAX_SOURCE_CHARS`（默认 1,000,000）作为单次输入预算；候选母稿超过该参考值时仍完整返回，只增加人工核对和调整拆解配置的警告，不截断、不分批。后续按镜头编译出的 H3 提示词才单独执行 7000 字符校验，三个口径不得混用。

### 从母稿审核并新增 Bible

完整母稿可选地以前置闭合块声明实体，每行格式固定为 `【人物|场景|道具】名称｜描述｜visual_identity_en`：

```text
【SCRIPT_BIBLE_V1】
【人物】沈砚｜失忆剑客｜young Chinese swordsman, rain-soaked dark robe
【场景】雨夜客栈｜关键调查地点｜ancient roadside inn at night, heavy rain
【道具】残缺玉佩｜身份线索｜broken white jade pendant with a red cord
【/SCRIPT_BIBLE_V1】
```

`GET /api/v1/projects/{id}/script-bible-candidates/` 严格解析当前 `master_script`，返回整份母稿的 `source_fingerprint`、全部候选、逐行 `diagnostics`、正文出现次数与证据、推荐状态，以及当前 Bible 的 `existing` / `confirmed` / `matched_bible_id` / `conflict_code`。`source_fingerprint` 只用于 apply 时防止陈旧页面覆盖新母稿，不作为单条候选的确认身份。GET 只读且不持久化候选；无块时返回空候选和 `has_block=false`。BOM、CRLF 和块内空行会规范化处理；缺少闭合、嵌套块、正文伪标记、字段换行/控制字符/保留分隔符、跨三类 NFKC 重名均返回显式 `400`，不会猜测或截断原文。

`POST /api/v1/projects/{id}/script-bible-candidates/apply/` 是唯一只增不删的确认入口。请求必须带 GET 返回的指纹和至少一个人工审核选择；`candidate_id` 只定位原候选，最终名称、描述和英文视觉锚点使用用户可编辑的提交值：

```json
{
  "source_fingerprint": "64位sha256",
  "selections": [
    {
      "candidate_id": "64位sha256",
      "reviewed": true,
      "name": "沈砚（青年）",
      "description": "失忆剑客",
      "visual_identity_en": "young Chinese swordsman, rain-soaked dark robe"
    }
  ]
}
```

服务端在一个事务中锁定 Project 与三类现有 Bible、重算整份母稿指纹、重新解析并校验编辑值、检查跨类型 NFKC 唯一性，然后分配稳定 slug 与下一可用 `S-id`，创建选中项并同步连续性锁。响应的 `created` 和 `already_exists` 均按 `characters/scenes/props` 返回完整 Bible 行，并给出请求、创建和已存在计数。相同声明候选与相同人工编辑内容重放为 `already_exists`；人工编辑后的正式名称可以与模型原候选不同。确认身份由内部候选 ID 加仅基于实体类型与原声明规范字段的声明指纹确定，不包含剧集正文；因此只修改正文标点、集内容或在确认后合法编辑正式 Bible 字段都不会撤销确认。声明字段变化会形成新候选或显式 provenance 冲突。`metadata.script_bible_v1.source_fingerprint` 仅保留首次确认母稿的审计事实，不参与身份匹配。

`metadata.script_bible_v1` 同时保存人工审核快照与其 SHA-256；每次候选分类都会重新计算快照哈希并严格比对，不能因格式宽松或当前 Bible 字段后来变化而跳过。该保留成员只能由上述 apply 入口写入；人物、场景、道具的通用创建/更新、全量 `sync-bibles` 和 Django 管理后台均拒绝注入、替换或删除它。相同候选以不同审核内容重放、同名异类型/异内容、来源指纹过期、无效 provenance 或并发唯一键冲突返回 `409`，整批零写入；现有未选 Bible 永不删除。

apply 与 breakdown 的请求校验失败统一返回全局包络 `{"error":{"status_code":400,"code":"invalid","details":...}}`；`details` 保留 DRF 的字段级错误对象或列表，不保证存在业务错误使用的 `detail` 字段。业务冲突继续返回相同顶层包络及对应的 `409` 业务码。

## 确定性拆解全剧母稿

页面“完整剧本与自动拆解”调用 `POST /api/v1/projects/{id}/breakdown-script/`。该接口不调用 Qwen 或 DeepSeek，而是按固定语法识别集、镜头、动作、对白和 Bible 名称，直接创建正式 Episode 与 Shot。

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

拆解总是先用同一个 `SCRIPT_BIBLE_V1` parser 处理完整原文，只把块后的 body 交给集/镜解析器，Project 的 `master_script` 永久保留完整原文。块内仍有未确认候选时默认返回 `409 script_bible_confirmation_required` 和当前指纹、待确认候选，且不清理旧剧集、不创建新剧集；用户可以先调用 apply，或在明确人工确认跳过后把请求字段 `continue_without_script_bible` 设为 `true`。跳过只影响本次拆解，不创建、不修改 Bible，并在成功 `warnings[]` 中如实记录。无块的历史母稿不受此门禁影响；非法块始终 `400`，不能通过跳过参数绕过。

若用户在 apply 时改过实体名称，拆解入口会仅从“已确认且无冲突”的 provenance 构造原声明名到当前 Bible 行的可信别名映射，并显式交给唯一拆解服务；因此母稿正文仍使用原人物、场景或道具名时，人物对白、镜头人物、场景和道具仍绑定到人工编辑后的实际 Bible ID。未确认或冲突候选绝不提供别名；名称重叠继续采用最长非重叠匹配，显式 `stable_speaker_id` 对对白解析始终高于同文本名称或别名。

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

同一集同时只能有一个草稿处于 `applied` 状态。用户应用新的已确认草稿后，服务端会在同一事务内把该集此前的 `applied` 草稿改回 `confirmed`，清空其当前应用标记；正式镜头仍通过 Shot 自身的 `metadata.creative_draft_id` 保留来源审计。`placement=replace` 是默认主路径：如果当前集还没有提示词、生成任务或组装审计，服务端会直接替换正式分镜；如果已有生产派生记录，服务端不会覆盖旧 Shot，而是创建同一 Episode 下的新分镜版本并设为当前版本。`placement=append` 只在用户明确选择追加时使用。

每个草稿镜头除 `h3_mode` 与 `include_audio` 外，还保存 `requires_first_frame_continuity` 和 `requires_last_frame_continuity`。这两个字段来自模型对剧本指令、剧集默认设置和镜头进出状态的结构化判断，应用草稿时会写入正式 Shot，并在分镜编辑器显示为“首帧承接上一镜 / 尾帧衔接下一镜”。它们只表示连续性意图，不代表已经存在 `reference_bindings.first_frame_asset_id` 或 `last_frame_asset_id`；真实首尾帧素材仍必须通过人工绑定或延迟连续生成请求物化。

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

分镜编辑器通过 `POST /api/v1/episodes/{episode_id}/sync-shots/` 保存当前明确选择的分镜版本。请求必须同时提交 `shot_version_key` 和该版本的完整 `shots` 快照；服务端只更新、排序或删除这个版本的镜头，并把它设为当前版本，不会把同一剧集的其他版本视为本次快照的一部分。客户端不得修改 `creative_version_*` 保留元数据；已有整集高细节扩写历史的正式镜头不能通过该入口硬删除，以免丢失批次审计记录。需要改变这类版本结构时应生成新的分镜版本进行比较。

### 接口卡片

| 用途 | Method 与 Path | 副作用 |
| --- | --- | --- |
| 创建/列出批次 | `POST/GET /api/v1/projects/{project_id}/shot-detail-draft-batches/` | POST 只冻结所选分镜版本的输入并入队，不在 HTTP 请求内调用模型；GET 必须同时提供 `episode` 与 `shot_version_key`，且只返回该版本记录 |
| 查看批次和逐镜对比 | `GET /api/v1/shot-detail-draft-batches/{batch_id}/` | 只读；`items[].before_snapshot` 是入队时事实，`items[].draft` 是候选或失败记录；应用完成后同时返回数据库最终 `applied_shots`，供客户端在应用响应丢失后恢复 |
| 整集应用 | `POST /api/v1/shot-detail-draft-batches/{batch_id}/apply/` | 统一预检后在一个数据库事务内确认并应用全部视频镜头；响应的 `applied_shots` 是数据库最终镜头值，`translation_written` 表示是否已写入确认英文 |

接口使用 JWT 和 Project 租户范围。`shot_version_key` 是必填的分镜版本边界；创建只冻结该版本的完整镜头集合，相邻镜头上下文也只在同一版本内解析。当前版本没有批次时返回空分页，不会恢复或展示同一剧集其他版本的最新批次。`request_id` 是 UUID 幂等键；同一用户、项目、剧集、分镜版本和客户端请求重放返回原批次 `200`，首次入队返回 `202`，不同范围或分镜版本复用同一键返回 `409 shot_detail_batch_request_id_conflict`。部署期间 provider policy 变化不会改变既有请求的幂等身份；原批次继续按其冻结的 `policy_snapshot` 执行。批次保存版本标识/标签、实际 resolved provider policy、全镜源清单指纹、逐镜源指纹、重试次数和错误，不保存密钥或外部端点。

历史数据迁移只把能够由完整 items 唯一证明归属的批次标记为 `resolved`。混合版本、items 不完整或无法证明归属的批次保留原生成/应用状态与计数，但标记为 `legacy_mixed`、`legacy_partial` 或 `legacy_unknown`，仅供审计；它们不会进入版本专属列表、worker 或整集应用路径。

### 创建请求、确认与容量口径

```json
{
  "episode_id": "00000000-0000-0000-0000-000000000000",
  "shot_version_key": "creative-draft:00000000-0000-0000-0000-000000000002",
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

## 生成并人工确认 Bible 实体图片候选

`GET /api/v1/projects/{id}/bible-image-candidates/` 返回项目三类 Bible 实体、项目级参考图提示词配置 `prompt_config`、当前 `source_fingerprint`、anchor/reference assets，以及每个实体全部已创建的 `PromptArtifact → GeneratedWorkflow → GenerationTask` 候选历史。每个实体行包含 `current_prompt_inputs`，逐项列出本次若重新生成会采用的正向/负向来源字段，例如实体视觉锚点、结构化 Bible 字段、项目风格、系统构图规则、固定负向规则和质量后缀；历史候选的 `prompt_inputs` 则冻结当时实际采用的字段值和最终 prompt 文本。`candidate_history` 按新到旧排列并保留每次重新生成的独立候选；`latest_candidate` 必须等于 `candidate_history[0]`，仅用于快速展示最新状态，不代表旧候选被覆盖。指纹只由本次提示词真正采用的源值/确认译文及 revision/hash、相关 canonical 碰撞输入、编译器版本、画面规格、项目级提示词配置、字段覆盖值和额外导演要求生成；未参与编译或碰撞判断的无关实体正文不会使候选失效。每条候选的 `source_current` 表示该冻结输入是否仍与当前编译结果一致；`source_current=false` 是审核风险提示，不表示图片无效，也不会删除历史候选。`output_media`、`promoted_assets`、`review_required` 和 `persisted_as_asset` 反映当前审核状态。GET 只查询既有事实，不创建候选、任务或 Asset。

`POST /api/v1/projects/{id}/bible-image-candidates/` 创建一个待人工审核的图片候选。请求字段唯一为：

```json
{
  "entity_type": "character",
  "entity_id": "00000000-0000-0000-0000-000000000001",
  "idempotency_key": "bible-image-character-1-v1",
  "extra_direction": "neutral expression",
  "prompt_overrides": {
    "project.style_prompt": "cinematic neo-wuxia, expressive ink-wash atmosphere",
    "system.composition.character": "neutral full-body identity reference, unobstructed face"
  },
  "seed": 42,
  "priority": 0,
  "max_attempts": 3,
  "extended_generation_confirmed": false
}
```

`entity_type` 只接受 `character | scene | prop`，`idempotency_key` 最长 120 字符；其余字段可省略。`prompt_overrides` 是本次生成对 `current_prompt_inputs` 中字段键的人工覆盖，键名只允许字母、数字、点、下划线和短横线，单值最长 4,000 字符；它不修改项目配置或 Bible 正式字段，但会进入请求指纹、候选历史和审计快照。调用方不能提交 template、model 或 workflow。服务端只选择最新启用的全局 system `minimax-reference-image`，并严格核对 ComfyUI、image、SDXL 固定模型、`text_to_image`、`sdxl_txt2img_bible_v1` 和无像素参考输入契约；缺失或不合规返回 `503 bible_image_candidate_template_unavailable`，不回退到其他模板。

项目的 `production_settings.bible_image_prompt_config` 是参考图提示词默认配置，创建项目时随所选风格写入默认值，之后可在页面中编辑并随项目保存。当前字段包含 `style_prompt`、`style_negative_prompt`、三类 `composition_rules`、三类 `negative_rules` 和 `quality_suffix`。人物提示词默认使用单主体中性全身或 3/4 身份图，场景为无人的 wide establishing view，道具为单件隔离 3/4 产品图；这些只是可配置默认值，最终以保存后的项目配置和本次 `prompt_overrides` 为准。视觉字段缺失、中文导演文本、词数偏高、字段不足或 canonical target 碰撞只进入 `quality.issues/warnings`；`advisory_only=true`、`would_block_enqueue=false`，不会伪装成已优化，也不会因内容质量提示阻止入队。结构、租户、模板、编译、幂等和队列错误仍显式失败。

新请求在同一事务内锁定 Project、目标 Bible 和系统模板，分配 Prompt revision，并物化三件套。成功返回 `201` 与固定 ID、`review_required=true`、`persisted_as_asset=false`；每次使用新的 `idempotency_key` 重新生成都会追加一条候选历史，旧图片仍保留给用户对比确认。相同 key 与相同指纹返回原链当前最新任务 ID（重试后即 retry task）和 `200 idempotent_replay=true`，同 key 不同指纹返回 `409 bible_image_candidate_idempotency_conflict`，残缺或歧义链返回 `409 bible_image_candidate_incomplete_chain`。候选 Prompt/Workflow 不能通过通用 workflow/task 创建入口再次物化，旁路请求返回 `409 bible_image_candidate_reserved_pipeline`。候选没有独立数据库表，也不直接改 anchor/reference assets。

worker 成功并封存 output provenance 后，调用既有 `POST /api/v1/tasks/{id}/promote-output-to-asset/`，请求最少只需 `{"output_index": 0}`。对 `generation_purpose=bible_image_candidate` 的任务，服务端从冻结的 `bible_target` 推导分类和实体；若客户端仍提供 `reference_category/entity_id`，必须精确一致，否则返回 `409 bible_image_candidate_target_conflict`。签名 manifest、任务 capability snapshot 与 Prompt request options 中的 purpose、request fingerprint 和 target 必须逐字一致；任一不一致返回 `409 bible_image_candidate_provenance_mismatch`。缺少生成时封存 provenance 返回 `409 bible_image_candidate_provenance_missing`，实体已删除返回 `409 bible_image_candidate_target_unavailable`。若候选的冻结来源已不是当前 Bible/译文/视觉规则，默认返回 `409 bible_image_candidate_source_stale`，响应 `details.can_accept_stale=true`；前端在用户二次确认后可重发 `{"output_index": 0, "accept_stale_bible_image_candidate": true}`，服务端允许把该历史候选作为参考图加入。成功只把新 Asset 添加到 `reference_assets`，不替换 anchor；metadata 记录实体/来源/请求指纹、task/output、Prompt/Workflow、冻结模板版本与契约 hash、artifact/manifest hash、`source_current`、`current_source_fingerprint`、`stale_source_accepted` 和确认人/时间。重复提升返回原 Asset；数据库事务失败时删除刚复制的外部文件，避免孤儿素材。普通图片任务的原提升语义不变。

正式任务列表 `GET /api/v1/tasks/`、任务详情 `GET /api/v1/tasks/{id}/` 以及 `POST /api/v1/tasks/{id}/retry/` 的任务响应统一提供两个安全只读投影：`generation_purpose` 仅为 `bible_image_candidate` 或 `null`；`bible_target` 仅为 `null` 或 `{schema_version: 1, reference_category, entity_id, entity_name, source_fingerprint}`，其中分类只接受 `character | scene | prop`，指纹为 64 位小写 SHA-256。两字段只读取该 GenerationTask 已冻结的 snapshot，响应不公开完整 `capability_snapshot`、本地路径、幂等 key 或其他内部执行字段。若任务已标记为候选但冻结 target 缺失或不合法，仍返回 `generation_purpose: "bible_image_candidate"` 与 `bible_target: null`；调用方必须将其显示为不可审核的候选异常，不能降级为普通任务或猜测绑定目标。

## 创建延迟首尾帧连续生成请求

`POST /api/v1/deferred-generation-requests/` 用于把一个已有视频任务的声明 `primary_video` 输出衔接到另一个镜头的首帧或尾帧槽位。请求只创建可审计的 `DeferredGenerationRequest` 和临时提示词预览；此阶段不会创建新的 `PromptArtifact`、`GeneratedWorkflow` 或 `GenerationTask`，也不会修改目标 Shot 的 `reference_bindings`、`reference_assets` 或 Bible。

请求体是 closed object，唯一字段为：

```json
{
  "project_id": "00000000-0000-0000-0000-000000000001",
  "source_task_id": "00000000-0000-0000-0000-000000000002",
  "target_shot_id": "00000000-0000-0000-0000-000000000003",
  "frame_slot": "last_frame",
  "idempotency_key": "deferred-shot-12-to-shot-13-last-frame",
  "priority": 0,
  "max_attempts": 3,
  "extended_generation_confirmed": false
}
```

`frame_slot` 只接受 `first_frame | last_frame`。服务端固定从 source task 的冻结 `output_contract.primary_video` 解析视频输出，客户端不得传 `output_index`、template、model、workflow、reference asset 或临时文件路径。`first_frame` 表示“目标镜头开场状态”，其来源始终是 source task 成功视频的尾帧：I2VA/FL2VA 会把派生图作为严格 `first_frame_asset_id` 使用；启用 `requires_first_frame_continuity=true` 的 Ref2VA 会把派生图作为软 `first_frame_reference_asset_id`，在提示词和工作流里占用 `<Picture 1>`，同时仍可继续使用人物、场景、道具、参考视频和音频。该软首帧会占用 Ref2VA 的 9 张 Picture 容量之一，因此普通人物/场景/道具图片最多再占 8 张。`last_frame` 仍只能衔接到 L2VA/FL2VA 目标镜头。项目、source task、target shot、owner 与模板必须一致。相同 `project_id + idempotency_key` 且请求指纹一致返回原记录 `200`，不同指纹返回 `409 deferred_generation_idempotency_conflict`；同一目标镜头/槽位已有活动请求返回 `409 deferred_generation_active_conflict`。

`GET /api/v1/deferred-generation-requests/` 支持 `project`、`source_task`、`target_shot`、`status`、`frame_slot` 过滤并返回分页列表。列表不含完整临时提示词；`GET /api/v1/deferred-generation-requests/{id}/` 才返回 `provisional_prompt_text`、`provisional_negative_prompt` 与 `provisional_prompt_sha256`。响应字段包括 `status`、`target_template_id/hash`、`target_input_fingerprint`、`target_workflow_overrides`、`resolved_source_output_index`、`source_artifact_sha256`、`source_manifest_sha256`、`derived_asset_id`、`materialized_task_id`、`materialized_prompt_artifact_id`、`materialized_workflow_id`、`cancel_allowed`、`retry_allowed=false` 与安全错误摘要。

`POST /api/v1/deferred-generation-requests/{id}/cancel/` 只接受空 JSON 对象，仅当状态仍为 `preparing_prompt | waiting_upstream` 且 worker 未 claim 时可取消；其他状态返回 `409 deferred_generation_cancel_conflict`。后端 worker 会在 source task 成功并验证签名 manifest 后自动抽取所需源帧、创建派生 reference Asset，并用临时绑定物化正式下游 `PromptArtifact → GeneratedWorkflow → GenerationTask`；目标 Shot 本身的 `reference_bindings`、`reference_assets` 和 Bible 不会被这个过程改写。失败、stale 或容量确认需求会写入 Deferred 行的安全错误摘要，不能由前端或外部脚本模拟成功。

## 创建单集、选区或整剧生产任务

### 能力与请求

页面在提交前读取 `GET /api/v1/capabilities/production/`。返回的 `recommended_max_pending` 与 `recommended_max_active_per_user` 是当前队列的确认阈值，`max_concurrent` 是 worker 实际并发，另有当前全局/当前用户活跃任务数和任务超时。`video_shot_min_duration_ms` 与 `video_shot_max_duration_ms` 是正式保存镜头和 H3 生产共用的当前时长范围，页面必须动态读取，不得复制固定秒数。`business_volume_limited=false` 明确表示队列数字不是项目集数、单集镜数或整剧任务总量上限。

生产入口为 `POST /api/v1/projects/{id}/build-production/`。省略 `episode_ids` 表示按项目当前全部剧集构建；也可传一个或多个剧集，或用 `shot_ids` 进一步选择镜头。`whole_episode=true` 只接受一个剧集，并且必须显式提交 `shot_version_key`；服务端只解析该分镜版本，按版本内镜号冻结其有序、精确的 `batch_shot_ids`，不会把同一剧集保留的初始版或其他历史版本混入本次批次。每个冻结镜头对应一个可独立重试和审计的槽位。所有模式都支持：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `episode_ids` | UUID[] | `[]` | 空数组表示当前项目全部剧集；不是固定前 N 集 |
| `shot_ids` | UUID[] | `[]` | 可选镜头子集；必须属于当前项目与所选剧集 |
| `whole_episode` | boolean | `false` | 整集一镜一任务模式；必须只选一个剧集 |
| `shot_version_key` | string | `""` | `whole_episode=true` 时必填；所选分镜版本的稳定标识，只生成该版本的镜头 |
| `enqueue` | boolean | `true` | 是否创建持久生产任务 |
| `extended_generation_confirmed` | boolean | `false` | 仅在队列推荐阈值提示后由用户显式确认；不会提高并发或改变单镜参数 |
| `idempotency_key` | string | `""` | `enqueue=true` 且解析后包含多个生产单元时必填；单生产单元可省略。相同输入复用同一完整批次 |

### 队列确认与批次追踪

如果“当前活跃任务 + 本次待创建任务”超过任一推荐阈值，且请求未确认，服务端在创建 Prompt、Workflow 或 Task 前返回 `409`，稳定码为 `production_queue_confirmation_required`。`error.details` 包含 `requested_task_count`、`active_task_count`、`user_active_task_count`、`recommended_max_pending`、`recommended_max_active_per_user` 与 `max_concurrent`。页面必须展示任务数量和预计排队时间增加风险，由用户取消或再次以 `extended_generation_confirmed=true` 提交；不得自动确认，也不得把业务范围静默截断为阈值以内。

确认后的请求仍为每个可立即执行的生产单元创建完整任务集合。若某镜头启用了 `requires_first_frame_continuity`，且同一分镜版本内的上一视频镜头启用了 `requires_last_frame_continuity`，生产入口会优先创建或复用上一镜视频任务，并为当前镜头登记 `DeferredGenerationRequest`；连续性查找不会越过分镜版本边界。当前镜头不会在缺少上一镜尾帧时直接排队。响应会同时返回即时 `task_ids`、`deferred_generation_request_ids`，并在对应 `production_units[]` 中标记 `workflow_mode="deferred_first_frame_continuity"`、`waiting_for_deferred_first_frame_source` 或 `waiting_for_deferred_first_frame_chain`；只有实际登记 Deferred 时才返回 `counts.deferred_generation_requests`。Deferred 会冻结目标镜头在本次请求中匹配到的模板和 `target_workflow_overrides`，包括视频分辨率档位、尺寸倍数、实际宽高，以及目标模板公开的同名工作流参数；worker 后续物化下游任务时使用这些冻结值，不回落到项目默认尺寸或默认模板。整集页面按“剧集 + 分镜版本”保存会话内持久的 `idempotency_key`；网络响应丢失后会以同一键和请求参数重放，切换分镜版本不会复用旧版本的键。服务端在项目行锁内、写入任何 Prompt/Workflow/Task 前计算最终剧集、分镜版本、有序镜头清单、连续性、模板与参数指纹：同键同指纹返回原完整批次（`200`），同键不同指纹返回 `409 production_idempotency_conflict`，不会重复创建任务。`201` 正常响应和 `200` 幂等重放使用同一响应 Schema，并公开 `batch_id`、`batch_kind`、`batch_size`、`shot_version_key`、`shot_version_label`、`batch_shot_ids`、`batch_slots`、`task_ids` 与 `production_units`：多集为 `series`，单集选区为 `selection`，整集一镜一任务为 `episode`。整集响应的 `batch_size` 等于冻结版本的镜头总量，`batch_shot_ids` 是该批次不可变的槽位顺序；`task_ids` 只包含已经物化的真实生产任务。前端可用 `production_units[]` 先展示本次提交中的等待镜头，并以 `batch_slots[]` 作为批次每个槽位的权威状态。未提供幂等键且只有一个生产单元时 `batch_id` / `batch_kind` 可以为空，此时 `batch_size` 仍表示本次即时生产任务数。任务详情中的批次索引必须为 `1..batch_size`；延迟物化出的下游任务会继承原 `batch_id`、版本标识、冻结镜头清单与下一镜 `batch_index`。可用 `GET /api/v1/tasks/?batch_id=...` 查询已经物化的同批次任务；任务详情和列表响应也会返回 `batch_slots[]`，使刷新页面后仍能看到已物化、等待 Deferred、已取消、被上游取消或尚未创建的槽位状态。历史上缺少版本清单或已混合多个版本的批次保持原始 `batch_size`、任务和审计事实，不会被自动归到当前版本、缩减镜头数或作为新版本批次重放；用户需要为当前版本新建独立批次。

`batch_slots[]` 是读侧事实，不创建、取消或重试任何任务。每个槽位至少包含 `batch_index`、`batch_size`、`slot_status`、`shot_id`/`shot_number`/`shot_title`、以及安全 `message`；有真实任务时包含 `task_id` 与 `task_status`，等待延迟连续性时包含 `deferred_generation_request_id` 与 `deferred_generation_status`。`slot_status="waiting_deferred"` 才表示有真实 Deferred 正在等待或物化；`slot_status="not_created"` 表示当前没有 active Deferred，也没有真实 `GenerationTask`，页面应显示“后续链路已中断/等待重试恢复”，不得仅凭 `batch_size` 和已加载任务数补成“排队中”。`slot_status="cancelled_by_upstream"` 表示上游取消已切断后续链路；对最新 retry 任务成功后，worker 会基于新的 task id 创建新的 Deferred，后续刷新会以新的 `batch_slots` 覆盖旧虚拟占位。

连续整集可能形成链式等待：第一个可直接生成的上一镜会创建即时 `GenerationTask`，下一镜创建真实 `DeferredGenerationRequest`，再往后的镜头先以 `workflow_mode="waiting_for_deferred_first_frame_source"` 或 `workflow_mode="waiting_for_deferred_first_frame_chain"` 记录在本次响应里，等前一镜 Deferred 物化并生成成功后由 worker 逐镜自动登记下一条 Deferred。`deferred_generation_request_ids` 只包含本次已经实际登记的 Deferred，不包含尚无 source task 的链式等待镜头。尾帧抽取完成后会产生一个派生参考图 Asset，并封存在 Deferred 记录和下游物化任务的参考输入中；任务详情 `GET /api/v1/tasks/{id}/` 会把该派生帧追加到上游视频任务的 `output_media` 图片列表，供页面在“本任务原始产物”中展示；这个过程不会直接修改 Shot 绑定。

单任务入口 `POST /api/v1/tasks/`、任务取消 `POST /api/v1/tasks/{id}/cancel/` 和失败/取消任务的 `POST /api/v1/tasks/{id}/retry/` 使用同一队列事实。取消一个未终态任务时，服务端会沿 Deferred 首尾帧依赖链自动取消仍未物化的后续 `DeferredGenerationRequest`，以及已经物化但尚未终态的后续 `GenerationTask`；响应附带 `downstream_cancellations` 摘要。该级联只停止当前依赖链，不删除历史审计记录，也不取消已经成功完成的独立任务。后续如果对被取消的源任务执行 retry，新任务成功后 worker 会按新的 task id 重新登记下一镜 Deferred，并恢复后续连续生成链路。

单任务创建和 retry 的请求均包含默认 `false` 的 `extended_generation_confirmed`；超过推荐值但未确认时返回相同的 `409 production_queue_confirmation_required` 与安全容量详情，且不会创建新任务。调用方显示提示后可取消，或原样重发并设为 `true`。确认只允许继续排队：单任务创建不会改变生成参数，重试仍复用原任务已物化的工作流与参数，二者都不会提高 worker 并发。任务能力快照记录本次确认，便于审计。

整集或整剧批次中的部分任务失败后，可从任一同批次任务调用 `POST /api/v1/tasks/{id}/resume-batch/`。请求体与 retry 相同，只包含 `extended_generation_confirmed`；服务端按冻结的 `batch_id` 扫描同批次任务，每个 `batch_index` 只取最新一条，已成功、正在排队/运行/取消中、或已经有后续 retry 的任务都会写入 `skipped` 并跳过。只有最新失败或取消且通过原 retry 安全校验的任务才会克隆为新任务，继续使用原已物化工作流与参数，不会重建新的整集批次，也不会重复提交已完成镜头。创建了重试任务返回 `201`，没有新建任务但完成扫描返回 `200`；响应包含 `batch_id`、`batch_kind`、`scanned_task_count`、`retried_task_ids`、`skipped[]` 与新建 `tasks[]`。若恢复数量超过队列建议值且未确认，返回 `409 production_queue_confirmation_required`，调用方可展示成本/等待提示后以同一请求重发确认。

取消整集或整剧批次时调用 `POST /api/v1/tasks/{id}/cancel-batch/`。服务端先从被选中的真实任务解析其有效 `batch_id`，只扫描并处理这个精确批次 ID 下仍未终态的任务；同一剧集、同一镜头的其他历史批次不会被取消、恢复或合并。响应包含 `cancelled_task_ids`、`cancel_requested_task_ids`、`skipped[]`、`downstream_cancellations` 与刷新后的同批次 `tasks[]`。如果某个任务已有 ComfyUI 远端提交，接口只标记 `cancel_requested`，由 worker 后续确认远端状态；没有远端提交的排队任务会直接进入 `cancelled`。旧批次若已经真实排队，会继续作为独立批次显示，用户需要选中该旧批次自己的任务再执行取消。

删除整集或整剧批次历史记录时调用 `DELETE /api/v1/tasks/{id}/delete-batch/`。服务端同样只按被选中任务冻结的精确 `batch_id` 扫描，不会匹配同一剧集或同一镜头的其他历史批次。该操作只允许整个批次都已进入终态，且没有活跃 Deferred 首尾帧连续请求；若仍有排队、运行、取消中任务或处理中连续请求，返回 `400` 并列出阻塞项。删除会同时清理该批次内部终态 Deferred 记录和任务记录，但不删除 ComfyUI/本地磁盘产物，也不会绕过封存、发行或组装审计保护。响应包含 `batch_id`、`batch_kind`、`scanned_task_count`、`deleted_task_ids` 与 `deleted_deferred_generation_request_ids`。

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
