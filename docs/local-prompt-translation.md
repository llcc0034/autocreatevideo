# 提示词翻译、扩写与人工审核

## 模型边界

平台实现只接受 `local_qwen` 与 `deepseek_api` 两种 provider。本地模型按其真实模型身份标识，
不会标记为 Qwen-MT：

- 本地候选翻译：`provider=local_qwen`，`model=Qwen/Qwen3-4B-GGUF`；
- 可选远程候选：`provider=deepseek_api`，通过 DeepSeek 官方 HTTPS API 调用；
- 模型文件来自 ModelScope 官方 Qwen 组织，使用 `Qwen3-4B-Q4_K_M.gguf`；
- 任一通道的翻译或扩写都只是候选，必须人工确认，确认前不会进入 MiniMax H3 正式提示词。

官方参考：

- Qwen-MT 介绍：https://qwenlm.github.io/blog/qwen-mt/
- Qwen-MT API：https://docs.qwencloud.com/developer-guides/text-generation/qwen-mt
- Qwen Transformers/ModelScope 下载说明：https://qwen.readthedocs.io/zh-cn/latest/inference/transformers.html

## 可选外部 DeepSeek API

DeepSeek 是按次显式选择的外部通道，适用于翻译、完整创作草稿和逐镜高细节扩写。默认通道仍是
`local_qwen`；远程请求失败时会原样报告失败，**不会静默切换到本地 Qwen**，避免用户误判实际
模型、数据边界或结果来源。

启用前必须理解数据边界：被处理的提示词，以及生成该候选所需的项目、剧本、Bible、连续性和
对白上下文，会发送给外部 DeepSeek API。不得发送无权外发、受保密约束或含不必要个人信息的
内容。能力接口和浏览器不会获得 `DEEPSEEK_API_KEY`；密钥只允许保存在后端 secret manager 或
未提交的环境文件中。任何曾出现在聊天、日志、工单或截图中的密钥都应立即在供应商控制台撤销，
并签发新密钥，不能继续投入使用。

本地开发在未提交的 `backend/.env` 中配置，Docker 生产部署则通过根目录未提交的 `.env` 或
编排平台 secret 注入：

```dotenv
DEEPSEEK_ENABLED=true
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=replace-with-a-new-rotated-secret
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_TIMEOUT_SECONDS=180
DEEPSEEK_MAX_RETRIES=1
DEEPSEEK_MAX_TOKENS=16384
DEEPSEEK_CREATIVE_MIN_SHOTS=1
DEEPSEEK_CREATIVE_RECOMMENDED_MAX_SHOTS=4
DEEPSEEK_CREATIVE_BATCH_SHOTS=1
DEEPSEEK_CREATIVE_MAX_DIALOGUE_PER_SHOT=2
DEEPSEEK_CREATIVE_MAX_EPISODE_TITLE_CHARS=80
DEEPSEEK_CREATIVE_MAX_DIALOGUE_TEXT_CHARS=240
DEEPSEEK_CREATIVE_MAX_DIALOGUE_DELIVERY_CHARS=100
DEEPSEEK_CREATIVE_MAX_DIALOGUE_DELIVERY_EN_CHARS=150
DEEPSEEK_CREATIVE_MIN_SHOT_DURATION_MS=5000
DEEPSEEK_CREATIVE_MAX_SHOT_DURATION_MS=15000
DEEPSEEK_CREATIVE_OUTPUT_MIN_TOKENS=1024
DEEPSEEK_CREATIVE_OUTPUT_BASE_TOKENS=768
DEEPSEEK_CREATIVE_OUTPUT_TOKENS_PER_SHOT=384
DEEPSEEK_CREATIVE_OUTPUT_TOKENS_PER_DIALOGUE=128
DEEPSEEK_CREATIVE_OUTPUT_DIALOGUE_CHAR_THRESHOLD=32
DEEPSEEK_CREATIVE_MAX_SOURCE_CHARS=24000
DEEPSEEK_CREATIVE_MAX_REGENERATION_CONTEXT_CHARS=96000
DEEPSEEK_CREATIVE_MAX_INPUT_CHARS=120000
DEEPSEEK_CREATIVE_OUTPUT_HEADROOM_PER_SHOT=384

# 默认仍保持本地；界面可对每次操作单独选择 DeepSeek。
PROMPT_TRANSLATION_DEFAULT_PROVIDER=local_qwen
PROMPT_CREATIVE_DEFAULT_PROVIDER=local_qwen
```

`DEEPSEEK_ENABLED=true` 与非空 API key 缺一不可。若希望默认选择远程通道，可把对应默认值改成
`deepseek_api`；这只改变初始选择，不改变逐次选择、人工审核和失败不回退规则。

## 安装与资源隔离

首次安装约下载 2.50 GB，并校验官方文件大小与 SHA-256：

- 文件大小：`2,497,280,256` bytes；
- SHA-256：`7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`；
- 独立推理运行时：`llama-cpp-python==0.3.34` CPU wheel。

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\download_local_translation_model.ps1
```

也可在首次项目初始化时显式安装：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -WithLocalTranslationModel
```

模型位于 `.runtime/models/Qwen/Qwen3-4B-GGUF`，推理依赖位于独立的
`.runtime/qwen-python`，不会污染 Django 或 ComfyUI 的 Python 环境。默认使用 CPU。
API 每次翻译启动一个 disposable 子进程；批量翻译在同一进程中只加载一次模型，所有字段完成后
进程立即退出，操作系统随即回收 RAM。生成视频期间没有常驻翻译模型，也不会占用 ComfyUI 显存。

翻译、结构化创作和 ComfyUI 生成 worker 共享 `.runtime/locks/local-qwen.lock` 跨进程非阻塞锁。
无论请求来自 Django、worker 还是不同操作系统进程，同一时刻只允许本地 Qwen 或一个视频生成任务
占用机器资源。第二个 Qwen 请求直接返回 HTTP 409，而不是同时加载两个约 5 GB 的模型副本；生成
worker 必须先取得同一把锁才会领取队列任务，并持有到 ComfyUI 返回终态。Qwen 已启动后到达的新视频
任务会保持 queued 并短轮询，视频已开始后到达的 Qwen 请求则返回 409，因此不存在“双方都已通过启动前
检查后再并发占用内存”的窗口。视频任务排队时，Qwen 的数据库前置门禁仍会提前返回 409。

CPU、字段长度、批量大小和上下文都会影响延迟与峰值内存。目标环境应在允许真实项目使用前
分别基准测试单字段、批量、推荐镜数内创作、超过推荐镜数的确认任务和逐镜扩写，并据此设置容量与超时告警。

## 审核状态和生产规则

每个字段保存独立翻译记录：

- `draft_translation`：模型候选，等待人工审核；
- `confirmed`：人工确认，且源文哈希仍匹配时才可进入 compiler；
- `needs_retranslation`：人工要求重新翻译；
- `failed`：首次翻译失败，没有可用候选。

重新翻译会生成不同但不改变事实的新英文候选。只有成功生成候选后才撤销旧确认；若运行时失败且
源文未变，系统保留原 `confirmed` 译文，只记录 `runtime_metadata.last_attempt_error`。每次成功候选、
人工编辑、确认、撤销和要求重译都会留下不可变 revision 记录。任何源字段修改都会使不匹配的确认立即失效，
H3 英文门禁会重新阻止生产，直到新源文对应的译文被确认。

每个候选还会保存 `runtime_metadata.translation_context_hash`。术语表、Bible 名称、稳定 speaker ID
或场景/道具 slug 发生变化时，旧确认即使原文字段未改也不会再进入 compiler，确认接口会要求重新翻译。

`<d>...</d>`、`<lyrics>...</lyrics>`、`<screen_text>...</screen_text>` 会替换为不可读
占位符后再送入本地模型。返回时必须保持每个块的原文、数量和顺序完全一致。对白只翻译
`shot.dialogue.{index}.delivery`（0-based）；`dialogue.text` 永不翻译。

## 页面操作

1. 打开“系统设置 → 模型与能力”，确认“本地 Qwen 翻译”显示已启用、已配置、模型已下载并已校验。
2. 在“连续剧工作室 → 制作圣经”先填写并保存 canonical 术语表，格式为每行
   `中文术语 = Canonical English`。
   当前人物、场景和道具不能共用同一个英文 canonical。页面按 NFKC、连续空白和大小写归一检测；若出现
   `canonical_target_collision`，系统不会猜译名，并会暂停保存、确认 / 同步和生成。人工修正并保存唯一术语后，
   按“相关译文失效 → 重新翻译生成候选 → 确认译文 → 一键同步已确认英文到正式字段 → 重新生成提示词”恢复。
3. 在制作圣经或具体集的分镜编辑器中找到“本地 Qwen 英文译文人工审核”。点击“一键翻译待处理”，
   也可以只点某个字段的“翻译此字段”。
4. 模型返回的是 `待审核` 候选。逐项核对后可直接修改右侧英文；满意后点击“确认译文并同步正式字段”。
   对分镜 camera、narrative、continuity、soundscape、music 和 dialogue delivery，确认事务会返回权威
   `promoted_fields`，页面只精确合并这些字段到当前 Shot 和本地 store；标题与对白正文不变。不满意时点击
   “重新翻译”，系统会保留中文原文和全部版本记录。
5. 只有显示“已确认”且源文、术语表和身份上下文均未变化的英文才会进入 H3 提示词。任一输入变化后，
   页面会立即显示“源文或上下文已变更”，不匹配的译文只读，必须重新翻译并确认。已确认但尚未同步的镜头条目
   可点击“一键同步已确认英文到正式字段”；撤销审核状态不会回滚已同步英文。
6. 正式字段同步后，输入哈希不匹配的 Prompt Artifact 会退出当前预览；点击“一键重新生成当前提示词”完成 compiler 和语言门禁复检。
   所有高影响中文导演字段处理完后，再创建整集/单镜生产任务。对白正文仍以原语言保留在
   `<d>[Chinese] ...</d>` 中，这是 MiniMax H3 的预期格式，不属于中英混杂错误。

## 术语与人工核对

在 `project.production_settings.translation_glossary` 中配置专名的固定英文，例如：

```json
{
  "玄纹剑": "Xuanwen Sword",
  "凌霄": "Ling Xiao",
  "雨夜客栈": "Rainy Night Inn"
}
```

若未配置，人物名使用稳定 speaker ID（如 `S1`），场景和道具使用 `scene:slug`、`prop:slug`，
避免每集自由翻译产生漂移。人工审核应重点检查人物性别、左右方向、专名、道具部位、景别、机位、
运镜速度和结束构图；4B 本地模型可能把相近概念混淆，不能跳过确认步骤。

运行时还会对常见导演词做生成前约束和生成后硬校验，包括 `中近景 => medium close-up`、
`中景 => medium shot`、`特写 => close-up`、`平视 => eye-level`、`推近 => push in`、`拉远 => pull back`、
`后移 => camera moves/trucks backward`、`三分之四侧脸 => three-quarter profile`、
`三分之四侧身姿态 => three-quarter body pose`、`剑鞘 => scabbard`、
`剑柄/剑把 => hilt`、`剑锋 => blade edge/blade tip/sword tip`。若源文和上下文都未明确
性别，却生成 `he/she/him/her` 等性别代词，该字段会按失败处理，不会把猜测保存为成功候选。

## 双语创作草稿

本地 Qwen 也可根据现有项目、剧集说明、剧本、风格、术语表和人物/场景/道具 Bible 生成
结构化中英双语分镜草稿。它不会直接生成 H3 最终提示词、工作流或参考素材绑定，也不会在生成时
改写正式 `Episode`/`Shot`：

1. 在现有剧集中填写业务目标镜数，选择保守/均衡/大胆创意强度，并可填写变化要求；超过 provider 推荐镜数时阅读耗时/API 用量提示并确认是否继续；
2. 模型输出中英文画面、动作、运镜、连续性、声景、配乐和对白表演说明；对白正文只允许来自剧本的
   精确中文子串；
3. 服务端再次严格校验 JSON schema、项目 Bible ID、时长、单一运镜、动作节拍、数字、左右、否定、
   术语表和英文残留中文；失败草稿仍可人工修复，但不能确认或应用；
4. “确认草稿”只记录审核状态，不写正式分镜；只有“应用”才事务性写入；
5. `chinese_only` 只应用中文导演字段，英文继续走逐字段翻译审核；
6. `bilingual_confirmed` 必须显式勾选“已人工审核英文”，应用时同步创建 confirmed translation overlay；
7. 可整稿重新生成，也可按 `client_key` 只重新生成一个镜头；未选择镜头保持不变，每次生成形成独立不可变版本；
8. 项目、剧集、Bible、术语表或正式镜头变化后，不匹配的草稿 context hash 立即失效，不能确认或应用。

创作镜数分为“推荐阈值”和“单次调用镜数”。默认本地 4B CPU 为推荐 4 镜、每次调用 4 镜，DeepSeek 为推荐 4 镜、每次调用 1 镜；推荐阈值只控制用户确认，`QWEN_CREATIVE_BATCH_SHOTS` / `DEEPSEEK_CREATIVE_BATCH_SHOTS` 用于初次整稿与整稿重生成的内部模型调用规划，二者相互独立且都只需不低于最小镜数。它们都不是单集或整剧总镜数上限。确认后的 11 镜请求仍保留 11 镜，不会被压缩为 4 镜。初次整稿与不带 `shot_keys` 的整稿重生成先保留显式“镜头 N”或空行段落边界，再按配置逐次调用；DeepSeek 默认每个镜头独立请求，并只携带该镜权威对白 manifest，后一镜只接收前一镜末尾的已校验连续性事实。每次返回先在本次调用范围内确定性对齐对白，再合并全稿；少返回镜头、镜号异常或后续调用失败时保留已获得的可解析镜头供人工确认。带 `shot_keys` 的选镜重生成仍走携带完整旧草稿的单次协议。

output tokens 按镜头数、必须保留的原文对白和 provider 余量动态计算。默认本地 Qwen 输出上限 3072，DeepSeek 传输上限 16384；每镜对白默认 2 句是可配置的审核建议。实际所需密度更高时先由用户确认，再按本次有效密度继续并标记审核，不会删除原文对白。初次整稿和整稿重生成会缩小内部批次以满足真实预算；只有调用模型前无法形成批次或完全没有可物化返回时才明确失败。选镜重生成预算不足会在调用模型前返回所需量和配置量，不会把 DeepSeek 请求静默截到较小 token 值；应调整 provider 配置或选用具备足够能力的通道，而不是降低业务总镜数。11 镜加 13 条常规短对白在默认 DeepSeek 配置范围内，但仍应以当前剧本预检结果为准。

源剧本、重生成草稿和总体输入分别使用 provider 的显式字符预算。权威剧本超过单批 `*_MAX_SOURCE_CHARS` 参考值时先要求用户确认，确认后的初次整稿与整稿重生成再按连续源片段自动分批，并逐批校验真实输入；不会截断权威剧本。仅选镜重生成按完整 `previous_draft` 上下文校验，超出其单次协议预算时会明确要求调整配置或选用具备足够能力的通道。字符预算不构成单集/整剧镜数上限。本地 Qwen 创作进程仍默认只使用 CPU、按需加载并在
请求完成后退出。若草稿要求 I2VA/FL2VA/L2VA 却没有正式首尾帧绑定，应用时会安全降级为 T2VA；
Ref2VA 只有在能从所选 Bible 找到无冲突的分类锚点素材时才会自动绑定，否则同样降级为 T2VA。

本地 Qwen 上下文边界由 `QWEN_CREATIVE_MIN_CONTEXT_SIZE`、`QWEN_CREATIVE_CONTEXT_SIZE`、`QWEN_CREATIVE_MAX_CONTEXT_SIZE` 与 `QWEN_CREATIVE_RESERVED_INPUT_TOKENS` 共同约束；子进程结束后的父进程清理余量由 `QWEN_CREATIVE_PROCESS_CLEANUP_GRACE_SECONDS` 控制。默认分别为 8192、8192、32768、2048 与 60 秒，均可通过环境配置调整，运行时和页面不保留第二套固定常量。

## 逐镜高细节扩写

已保存的**视频**镜头可使用独立的 `shot_high_detail` 提案，它不重用剧集创作草稿，也不会直接改写正式镜头。每次只读取：

- 当前镜头的正式叙事、动作、运镜、时长、已绑定参考和已锁定的对白；
- 只有上一、下一个镜头的连续性边界；
- 当前镜头绑定的人物、场景、道具 Bible，以及项目级风格、色彩和 canonical 术语。

它绝不将整个剧本、不相关的镜头或不相关 Bible 传入 Qwen。本地 Qwen 按需加载、用后退出，仍与 ComfyUI 视频任务共享 `LOCAL_QWEN_LOCK_PATH`；视频在运行时会返回 HTTP 409，不会抢占显存。

1. 选中一个正式视频镜头，点击“逐镜高细节扩写”。可填写变体提示（仅允许调整构图、光线、表演细节等非事实变化）；
2. 服务端生成独立候选 `detail_zh` 和 `detail_en`，分别上限为 **500 中文字符** 和 **750 英文字符**。中文候选必须含中文正文；英文候选只允许安全的拉丁英文导演文字，拒绝中/日/韩/西里尔文、双向控制符、零宽字符和其他不可见控制字符；
3. 在候选审阅区可修改两段文字并保存。保存、确认和应用都会复验字段长度、数字、S-ID、左右方向及当前镜头相关 canonical 英文术语；
4. 点击“确认候选”后再选择应用方式：
   - `bilingual_confirmed` 需显式勾选“已人工核对英文”，将 `detail_en` 写入正式 `Shot.detailed_description`，并写入已确认译文审计记录；
   - `chinese_only` 先将 `detail_zh` 写入正式字段，然后在现有“本地 Qwen 译文人工审核”中处理唯一的 `shot.detailed_description` 条目。在译文确认并提升前，H3 语言门禁会阻止生产；
5. 应用后，H3 编译器仅使用正式 `Shot.detailed_description` 一次，不将候选草稿、中文对白或高细节文字写入 negative prompt 或 SDXL 图像提示词。编译器仅复用 `prompt_compiler_version=8` 且输入指纹完全匹配的提示词产物；
6. 应用完成的草稿立即以“应用后正式镜头”作为新上下文基线，所以立即显示为当前。后续手工修改该镜头、相邻镜头的连续性、相关 Bible 或 canonical 术语后，候选会失效，必须重新生成。

异常恢复：

- **409 资源忙**：等待 ComfyUI 视频任务结束后重试；不要并行点击翻译、创作或高细节扩写。
- **上下文已变更 / 409**：重新载入镜头后重新生成；不要试图将过期候选覆盖新的 Bible 或连续性。
- **术语、左右、数字或 S-ID 校验失败**：修改候选中英文字段使两者一致，再保存、确认；如果 canonical 术语冲突，先修改 Bible 中的唯一英文名称。
- **英文未核对**：要么勾选人工审核并以双语应用，要么选择中文应用，随后在翻译审核面板确认英文。

## API

- `GET /api/v1/capabilities/prompt-translation/`
- `GET /api/v1/projects/{project_id}/translations/`
- `POST /api/v1/projects/{project_id}/translations/translate/`
- `POST /api/v1/translations/{id}/retranslate/`
- `POST /api/v1/translations/{id}/confirm/`
- `POST /api/v1/translations/{id}/request-retranslation/`
- `POST /api/v1/translations/{id}/revoke/`
- `GET /api/v1/capabilities/creative-drafts/`
- `GET /api/v1/projects/{project_id}/creative-drafts/?episode={episode_id}`
- `POST /api/v1/projects/{project_id}/creative-drafts/generate/`
- `GET/PATCH /api/v1/creative-drafts/{id}/`
- `POST /api/v1/creative-drafts/{id}/regenerate/`
- `POST /api/v1/creative-drafts/{id}/confirm/`
- `POST /api/v1/creative-drafts/{id}/apply/`
- `GET /api/v1/projects/{project_id}/shot-detail-drafts/?shot={shot_id}`
- `POST /api/v1/projects/{project_id}/shot-detail-drafts/generate/` — `{ "shot_id": "...", "variation_note": "..." }`
- `GET/PATCH /api/v1/shot-detail-drafts/{id}/` — PATCH 必须包含 `expected_revision` 和完整的 `{ schema_version, detail_zh, detail_en }`
- `POST /api/v1/shot-detail-drafts/{id}/regenerate/`
- `POST /api/v1/shot-detail-drafts/{id}/confirm/`
- `POST /api/v1/shot-detail-drafts/{id}/apply/` — 返回 `{ draft, applied_shot, translation_written }`

批量翻译为同步请求，最多 100 个字段；前端超时应不低于 360 秒。服务端核对每个 `source_text`
必须等于数据库当前值，过期页面不能提交旧原文。失败不会改写 SeriesProject、Bible、Shot 或对白原文。
选择本地 Qwen 时，若平台存在排队、运行中或取消中的视频生成任务，翻译接口会在加载 2.5 GB
模型前返回 HTTP 409；请等待生成结束后再翻译，以免与 ComfyUI 同时占用约 5 GB RAM。
DeepSeek API 不加载本地模型，因此不获取这把本地资源锁。

本地配置项见 `backend/.env.example` 的 `QWEN_TRANSLATION_*`。默认
`QWEN_TRANSLATION_DEVICE=cpu`，CPU 运行时保持为 `.runtime/qwen-python/Scripts/python.exe`。

### 可选 CUDA Qwen 运行时

CUDA 不是默认项。启用前应在独立目录安装与目标主机驱动、CUDA 和指令集匹配的 `llama-cpp-python` 运行时，例如 `.runtime/qwen-python-cuda/Scripts/python.exe`；它复用同一份 ModelScope 下载的 Qwen GGUF 文件，不重新下载模型，也不污染 Django、CPU Qwen 或 ComfyUI 环境。只有通过目标环境能力探测与上下文烟测的 runtime 才能配置到 `QWEN_*_RUNTIME_PYTHON`，不能将 CPU wheel 作为 CUDA runtime 使用。

在 `.env` 中只对需要加速的能力显式切换，另一项可以继续使用 CPU：

```dotenv
QWEN_GPU_LAYERS=-1
QWEN_TRANSLATION_DEVICE=cuda
QWEN_TRANSLATION_RUNTIME_PYTHON=../.runtime/qwen-python-cuda/Scripts/python.exe
# 如需创意草稿也使用 CUDA，再单独启用：
QWEN_CREATIVE_DEVICE=cuda
QWEN_CREATIVE_RUNTIME_PYTHON=../.runtime/qwen-python-cuda/Scripts/python.exe
```

`QWEN_GPU_LAYERS=-1` 表示尽量卸载全部模型层到 GPU；也可填写正整数控制卸载层数。CUDA 模式会先在这个**配置的 Python runtime** 中调用 `llama_supports_gpu_offload()`，再执行一次“小上下文 + 1 token”模型烟测。两项都成功才显示为可用；运行时缺失、GPU 不支持、驱动/指令集不兼容、烟测崩溃或 `QWEN_GPU_LAYERS=0` 都会明确拒绝请求，绝不悄悄退回 CPU。

能力接口会返回 `cuda_requested`、`gpu_offload_supported`、`gpu_context_smoke_passed`、`gpu_ready`、`gpu_layers` 和 `runtime_probe_error_code`。实际翻译、创作、全剧视觉规则与素材设定草稿仍使用同一把 `LOCAL_QWEN_LOCK_PATH`，与视频生成互斥；必须按目标 GPU 的可用显存为 Qwen 与 ComfyUI 分别完成容量验收，不能并行运行两类 GPU 任务。

创作使用独立的 `QWEN_CREATIVE_*` 资源预算，但可复用同一模型文件和 Python runtime；
`LOCAL_QWEN_LOCK_PATH` 必须指向所有 Django/生成 worker 进程共同可见的同一个本地文件；不能为不同
进程配置不同路径，否则资源互斥失效。
