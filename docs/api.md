# API

## 唯一契约

仓库根 [openapi.yaml](../openapi.yaml) 是唯一机器可读契约。运行时 `GET /api/schema/` 从同一契约动态返回 schema；文档、客户端和校验工具不得引用或维护其他副本。

路由前缀为 `/api/v1/`。本文只解释调用边界；字段、状态码、枚举和必填项以根 OpenAPI 为准。

### 项目创建时的服务端默认值

`POST /api/v1/projects/` 由已登录用户在“新建连续剧”页面调用，并将项目保存到当前用户的 `SeriesProject`。语言、画幅和宽高由页面从创建策略读取后提交；API 也允许省略这些字段及 `fps`、`continuity_seed`、`default_shot_duration_ms`，由同一服务端创建策略在校验阶段补齐，不使用模型字段或前端硬编码默认值。省略的宽高按所选画幅选项补齐；连续性种子按策略指定的显式值或每项目随机方式生成。显式提交的非法值仍返回字段级 400，策略缺失或无效则阻断创建，不写入半成品。成功返回 201 和已持久化的完整项目；创建本身不会调用模型，导入剧本与 ProductionRun 是后续独立步骤。PATCH 省略这些字段时保留原项目事实，不重新套用当前创建策略。页面保留服务端字段错误，不能仅显示通用 HTTP 400。

`script.structure` 实例烟测失败时，证据中的 `contract_error_code` 记录受限的原因分类（例如输出预算耗尽、无效 JSON 或结构合同不符），不保存供应商响应正文。历史未记录该分类的失败不能在读取时补猜原因。

工作流导入返回的 `suggested_input_contract.resolution_strategy` 可含已注册的 `source_image_derived` 尺寸路径证据；运行宽高由受管图片推导，不作为用户覆盖参数。扫描 `match_reason_codes` 是非冗余候选诊断摘要，完整候选原因仍保留；`review_workflow_contract` 恢复动作不表示缺少模型。具体支持范围和执行证据见 [输入图片决定输出尺寸](comfyui-saved-workflow-validation.md#31-输入图片决定输出尺寸)。

### 工作流执行准入

所有可执行 ComfyUI 工作流（含固定槽、通用多素材及 H3）均要求精确冻结 Projector 引用。候选纳管仍不要求匹配；提升时必须匹配，后续在线校验、发布准入与执行快照编译也会验证同一身份。没有 `contract_projector`、adapter 不一致或版本 SHA 漂移时显式阻断，不存在只凭专用 compiler 即可执行的入口。缺少引用的版本必须从候选新建替代版本；不得给原版本补挂 current Projector 或沿用历史验收。原有管理员权限、错误 envelope、无引用候选的 `candidate_only` 状态及导入 API 均保持不变。

### 工作流图片尺寸配置

- 目录：`GET /api/v1/workflow-projector-versions/options/` 的 `image_resolution` 返回只读算法标识、模式、原生尺寸、可选预设及技术上限；不访问 ComfyUI、不写库。
- 写入：现有 Projector 草稿 `PATCH /api/v1/workflow-projector-versions/{id}/`，在 `definition.projection.config.image_resolution` 提交 `adapter` 与 `sizes: [{width,height}]`，单位 px。省略该配置表示跟随原节点；空数组、null、重复尺寸和越界值均不表示默认值，而是 HTTP 400。仅 `generic.fixed` 支持；完整 Definition 仍接受同一服务器校验、版本哈希和生命周期审计。
- 读取：Projector 详情返回原配置；工作流版本详情的 `input_contract.resolution_strategy` 返回本版本冻结的规则与尺寸链。配置发布不修改已有工作流。Live Validate 会检查所有配置尺寸的执行图，缺少 ImageScale 或节点不接受配置时 HTTP 400；不会提交生成。
- 权限与状态：沿用超级管理员 Projector 读写及发布权限，未授权返回 401/403；已发布版本不可原地修改。无效 PATCH 不保存部分配置；前端显示服务端错误。
- 页面入口、实际编译行为和边界见 [查看规则与配置目标尺寸](comfyui-saved-workflow-validation.md#32-查看规则与配置目标尺寸)。配置与执行图、产物校验、错误分支由回归测试覆盖；真实模型运行需对新工作流版本进行 Smoke。

### 能力关联总览与列表摘要

- **入口**：设置一级导航“能力关联与验收” → “能力与关联总览”，沿用 `/settings/acceptance-smoke?view=relations`，不新增配置编辑器。各模型版本、实例、提示词策略/绑定、Projector 版本和配方列表使用同一摘要组件；工作流保留精确关联名称，增加全貌与配方反向引用入口。
- **查询**：`GET /api/v1/registry-relations/`，仅 staff；未登录/未授权返回 401/403，不支持写入。必填 `kind=model|deployment|workflow|prompt|binding|projector|recipe`。可选 `ids` 为逗号分隔的 1–50 个 UUID；`search` 最多 160 字；`page` 从 1 起；`page_size` 最大 50，默认沿用平台配置并受安全上限约束。总览每页 25，嵌入摘要每批最多 50、最多两个请求并行。
- **逐层查看**：同时传 `source_kind`、`source_id`、`relation`，`kind` 必须是该分组的目标类型。例如先查 `?kind=model&ids={model_version_id}`，再采用返回分组 URL 的 `source_kind=model&source_id={model_version_id}&relation=bindings&kind=binding` 查提示词绑定。分组计数与明细共享同一查询条件；不是按名称猜测关联。
- **响应**：`count / next / previous / results` 分页；`generated_at` 是本次读取时间，`context_label` 表示来源与关系。每行返回精确 `id / kind / label / status`、`capabilities` 声明摘要、`groups[{key,label,kind,count,url}]`、`notices` 和 `config_url / overview_url / acceptance_url`。不返回节点图、完整规格、Projector Definition、配方 Definition、凭据或运行参数。缺失的可选关系计数为 0；读取失败不能用 0 替代。精确 `ids` 对象不存在返回空结果；来源不存在返回 404；参数不完整、未知关系或超过批量/分页限制返回 400。
- **语义**：实例的 `validated` 数只是记录为校验目标的工作流数，不是当前有效验收数；`candidates` 只是同模型、未以此实例为校验目标的工作流。提示词精确绑定与模型族通用绑定包含停用项，按 operation/mode 展示，不宣称某次运行已选中。Projector 关系按冻结版本 ID 和摘要定位，不替换成 current；完整身份与执行合同有效性仍在工作流详情及验收中核对。文本及 async REST 不统一要求 Workflow / Projector。
- **配方**：复用注册表引用字段白名单，在配方 JSON 的明确 UUID/key 字段及其数组中检索，并支持反向查看；普通说明里的 UUID 不算引用。计数是唯一目标对象数，重复阶段引用不重复计数。继承、运行时解析与最终选中 Route 不在这里猜测，仍查原配方审核详情和 ProductionRun。
- **下一步与副作用**：缺少实例/工作流/提示词绑定、缺少模型族及缺失 Projector 记录均有说明；原对象配置入口携带 `relation_context` 返回上下文，精确配方/绑定会定位到对应项，缺失对象显式报错。有效性与下一步仍进入已有 Live Validate / Smoke 验收投影；本 API 只读持久化事实，不调用 Provider/ComfyUI、不执行校验、发布、生产，不新增关联事实表。配置页沿用原写入权限。
- **性能与验证边界**：分页摘要在一条带相关计数子查询的 SQL 中读取；关系筛选额外读取一个来源对象，无逐行网络请求。FK 条件复用现有索引，配方的显式引用检索扫描小型配方注册表（不扫描 ProductionRun 历史），应随注册表规模观察成本。SQLite 的精确计数/分页、权限、空关系、历史引用和前端过期响应隔离由回归测试覆盖；PostgreSQL JSONPath 实现须在对应部署库实测，不以本地 SQLite 测试代替生产数据库验收。

### 工作流关联配置查询

关联按钮使用“提示词绑定”“适用模型”“校验工作流”“引用配方”等简短名称；详细语义在关联总览的“这些关联有什么区别？”中说明。模型版本与实例的常用提示词、验收入口各保留一次，其余关系统一进入“更多关联”，不再平铺重复计数。

- **入口与权限**：现有 `GET /api/v1/workflow-template-versions/`（支持 `template`、`model_profile_version` 等原有筛选、`page` / `page_size` 分页）及 `GET /api/v1/workflow-template-versions/{id}/` 返回 `configuration_links`。沿用 staff 登录权限；未认证/未授权为 401/403，详情不存在为 404。列表页面按每页 50 条读取。
- **响应口径**：`model` 为精确绑定的模型版本及该版本全部实例数，`validated_deployment` 仅表示持久化的校验目标，未校验时为 null，不选择默认实例。`prompt_bindings` 是模型的当前关联目录：精确模型绑定及相同 `specification.model_family` 的族级绑定。`total` 统计全部绑定，`active_total` 统计 `is_active=true` 的绑定；`preview` 仅预览启用项，最多三条（精确模型优先，其次优先级及 key）。数据库先筛选启用项再排名，不能从全部绑定的前三条中再次筛选，否则会漏掉启用项。预览仍携带用途、模式和策略版本状态，启用绑定不等于该策略已发布或某次任务实际选用；实际选择以 ProductionRun 冻结记录为准。
- **Projector 来源**：`projector` 只根据工作流 `input_contract.contract_projector` 的版本 ID、key、版本号、SHA、引擎和操作身份关联，绝不按 current 版本补齐。`state=not_recorded` 表示没有记录，`invalid` 表示引用缺失/不一致，`linked` 表示冻结身份与对应发布/退役版本元数据一致；这不是重新进行合同校验或验收。已退役及非当前版本可追溯查看。
- **失败与边界**：缺少模型返回 `model_unbound`；模型未声明族时返回 `family_missing`，只保留可确定的精确绑定。接口/关联响应异常由页面明确报错并允许刷新，不伪装为“没有关联”。查询只读本地数据库，按页批量获取有界摘要，不访问 ComfyUI、不创建实例/绑定、不校验、Smoke 或发布，不记录新的生产事实。
- **页面跳转**：工作流列表以三行摘要显示模型实例、启用提示词绑定和引用 Projector，不平铺详细配置；多条启用绑定显示数量并可展开。“更多关联”打开独立弹窗，展示完整数量、停用绑定数量、历史实例及冻结方案版本。停用或退役对象在列表保留状态提示，异常引用和目录不完整持续可见，不替换历史目标。模型实例链接定位精确版本与实例；策略链接定位指定版本，弹窗“查看全部”保留模型上下文并携带 `include_inactive=1`，在原提示词页显示全部绑定（含停用项）。Projector 使用 `section=workflows&projector_version={version_id}` 只读打开冻结配置，可进入方案族管理后继草稿。`workflow_context` 保留来源工作流的返回入口。跳转无写操作，仍遵守原权限和发布不可变约束。
- **验证**：后端回归覆盖分页/详情一致性、权限、旧版本追溯、异常引用、无校验目标、多绑定和恒定批量查询；前端测试覆盖链接上下文、边界状态、只读配置加载及过期响应隔离。机器字段定义仍以根 OpenAPI 为准。

## 认证与敏感信息

受保护端点使用 JWT。provider credential 通过 SecretBinding 关联；秘密创建与轮换只接受超级管理员的 write-only `secret_value`，读取仅返回 configured 与生命周期元数据，不能据此宣称远端调用 usable。客户端不得记录或显示原始密钥、密文、nonce 或 key id；本文也不提供密钥示例。

首次临时密码登录时，仅允许 `GET /api/v1/auth/me/` 与 `POST /api/v1/auth/password/change/`。改密成功后旧 access/refresh token 立即失效，客户端必须重新登录。

超级管理员专用治理端点包括 `/api/v1/access/users/`、用户的 `role`、`status`、`temporary-password` actions、`/api/v1/access/audit-events/`、`/api/v1/secret-bindings/`、`POST /api/v1/secret-bindings/{id}/rotate/`、`POST /api/v1/secret-bindings/{id}/status/` 与 `/api/v1/secret-audit-events/`。SecretBinding 只允许创建、轮换和启停，没有修改秘密正文或删除入口；SecretVersion 与密钥审计记录只读且不可变。普通成员和 platform administrator 对秘密治理端点均无权限；平台管理员仍可管理不含秘密材料的平台 catalog/config，并可只读查询 `/api/v1/platform-lifecycle-audit-events/`。

平台 catalog/config 生命周期写操作追加 `PlatformLifecycleAuditEvent`，记录操作者、动作、对象标识、稳定原因代码、变更前后 hash 与扁平安全摘要；不保存 secret、endpoint、path 或原始 JSON。客户端可用 `X-Audit-Reason-Code` 提交稳定原因代码；未提交时服务端写入对应动作的稳定系统原因代码。审计 collection 支持 actor、action、object_type、object_id 与 reason_code 过滤，不提供 update/delete。

对状态变更请求，应使用 OpenAPI 声明的幂等/条件字段。冲突、等待人工决策、外部响应不确定、预算不足和上传契约缺失会返回显式错误或阻断状态，不会伪装成功。

## 素材库：本地入库、独立生成与下载

- `GET/POST /api/v1/assets/`
- `GET/PATCH/DELETE /api/v1/assets/{id}/`
- `GET /api/v1/asset-generation-jobs/options/?job_type={prompt|asset}&media_type={image|video|audio|script}`
- `GET/POST /api/v1/asset-generation-jobs/`
- `GET /api/v1/asset-generation-jobs/{id}/`
- `POST /api/v1/asset-generation-jobs/{id}/cancel/`
- `POST /api/v1/asset-generation-jobs/{id}/retry/`
- `POST /api/v1/asset-generation-jobs/{id}/resolve-uncertain/`（仅 staff）

素材库以受管 `Asset` 统一承载图片、视频、音频与剧本。图片可标记为人物、场景、道具或通用参考；视频和音频按短剧引用合同入库为 `reference_video`、`reference_audio`；剧本统一规范为 UTF-8 Markdown `script`。本地上传只接受 OpenAPI 与服务端校验器声明的格式；参考视频和音频入库时只探测并记录可验证的源媒体事实。素材生成任务选定 Workflow 和 `CONDITIONING_MODE` 时，以及素材随后进入 ProductionRun 时，服务端都会按该不可变版本的 `conditioning_contract.modes[].dynamic_reference_topology.preprocessing` 冻结合同校验时长、帧率、音轨和预处理要求。剧本支持 TXT、Markdown、Fountain 与 DOCX，PDF 明确拒绝。

`name`、`description`、`prompt_hint` 与 `style` 表达素材本身的业务信息；`source_info` 只表达“如何进入素材库”，由服务端写入并在 Asset API 中只读。正式 Asset POST 必须在上传文件、受管路径和外部引用地址中恰好选择一种，分别记录“用户本地上传”“服务端受管路径登记”或“外部引用地址登记”；客户端提交 `source_info` 会被显式拒绝。独立模型/Workflow 生成、延迟连续生成、素材清单导入和 ProductionRun Bible 审核也分别记录对应来源，不得把任务 UUID 或运行说明写入名称、基础描述或视觉锚点。`source_info` 是面向用户的来源摘要，不是机器血缘权威；`AssetGenerationJob.output_asset`、GeneratedArtifact/ProductionRun 关系和 Asset `metadata` 中的冻结 ID/hash 才用于复验与审计。历史素材（包括已退役且权威任务关系已不存在的旧候选）没有可复验来源时返回空值，页面明确显示“来源未记录”，不会仅凭客户端可写 metadata 猜测或伪装来源。

`metadata.fixture_scope=workflow_smoke` 标识只供工作流真实烟测使用的技术素材。普通 `GET /api/v1/assets/`、项目新建、分镜和 ProductionRun 生产选择器不会返回它；即使绕过页面提交，分镜绑定与全自动 Run 冻结也会显式拒绝。只有 staff 的 `GET /api/v1/assets/workflow-smoke-candidates/` 会继续返回并实时复核这类素材。此规则不修改既有 Shot、Run 或任务审计事实。

`options` 返回安全生成方案 `routes[]`，而不是全部模型目录。每项以 `target_kind=deployment|comfy_workflow` 和 `target_id` 给出唯一可提交目标，并同时返回实际 Deployment、operation、负面提示词能力和 smoke evidence ID。Deployment 方案返回封闭 `request_schema` 与默认值；ComfyUI 方案返回该不可变版本的 `workflow_input_schema`，其中包含全部可编辑标量参数、固定多媒体槽、单模式动态引用及按 `CONDITIONING_MODE` 索引的 `dynamic_reference_modes`。Deployment 方案要求已发布/启用、匹配 capability/driver 且具有 fresh 真实 operation smoke；ComfyUI 方案要求 current/published、output kind 匹配、精确 Live validation 与 succeeded 真实 Workflow smoke evidence，并绑定当前 active loopback ComfyUI Deployment。Workflow 的 `smoke_expires_at` 为 `null`，可用性由版本、validation/smoke/fixture/compiled graph/target 证据完整性和漂移复验决定。

创建请求必须显式提交 `execution_kind`。`deployment` 必须且只能配 `model_deployment_id`；`comfy_workflow` 必须且只能配 `workflow_template_version_id`，且只允许 `job_type=asset` 的 image/video/audio。客户端不得同时提交两种目标，也不得把 Workflow 与另一个 Deployment 自行拼配。异步 API Deployment 的图片、视频、音频 operation 分别为 `image.text_to_image`、`video.text_to_video`、`audio.text_to_audio`；ComfyUI 使用 Workflow 不可变版本声明并经能力校验的精确 operation，例如参考生视频可为 `video.reference_to_video`，不能被降级成纯文本生视频。剧本生成和“文本模型生成提示词”只使用受控文本 Deployment 的 `script.generate`。提示词任务只产生可编辑的 `result_text`，用户采用并确认后再创建素材生成任务；素材任务成功后直接物化一个受管 Asset。页面刷新后从 job collection 恢复任务，不以 Toast 或客户端超时推断终态。

ComfyUI 创建请求使用 `workflow_inputs={schema_version:2,values,assets,references}`。`values` 按 `workflow_input_schema.parameters` 提交 string/integer/number/boolean/enum 等全部公开标量；数值除 min/max/multiple 外还会按需返回 `alignment_modulus` 与 `alignment_remainder`，例如双时钟帧数必须满足 `5 + 17k`，前后端使用同一合同拒绝范围内但未对齐的值。存在多个 conditioning mode 时显式提交 `CONDITIONING_MODE`；`assets` 以 `{semantic_key,asset_id}` 为每个固定图片、视频或音频 Load 槽绑定一个受管素材；`references` 以 `pictures/videos/audios` 提交合同允许的多图、多视频、音频、用途以及内嵌/外部音轨配对意图。页面和客户端只提交 Asset ID 与业务选择，不提交本地路径、checksum、ComfyUI 节点值或上传名。`prompt.*`、`model.*`、`output.prefix` 与 `generation.batch_size` 仍由服务端管理，其中 batch size 固定为 1；固定素材必须通过 `assets`，不能伪装为标量或高级 JSON。

“新增素材 → 模型生成”的工作流输入同时支持从素材库选择和上传本地图片、视频或音频。快捷上传仍复用 `POST /api/v1/assets/`：图片以通用 `reference` 入库，视频、音频分别以 `reference_video`、`reference_audio` 入库；服务端返回 Asset 后，页面将该 Asset 加入当前候选并把 ID 自动绑定到用户点击的固定槽、动态图片、动态视频、独立音频或视频外配音轨。上传成功即表示素材已经正式入库；如果随后发现不符合当前 Workflow 的时长、帧率或类型合同，页面明确显示“已入库但未绑定”。如果生成任务创建报错，页面保留上传成功状态并单独显示任务错误；素材始终保留供复用或人工删除，不自动回滚，也不把文件、Blob 或客户端路径塞进 `workflow_inputs`。Workflow Smoke 的技术 fixture 入口不开放该普通素材快捷上传能力。

素材任务与 Workflow smoke、短剧生产复用同一套版本化参数规范化、Asset owner/global 授权、semantic compiler、媒体 staging 和 conditioning/preprocessing adapter，但会建立独立的 `AssetGenerationJob` 事实，不要求也不读取 `ProductionRecipe`。创建时服务端冻结用户输入请求、Asset facts、编译后的 bindings、conditioning mode、预处理合同及其 hash，同时冻结版本、validated Deployment、validation/smoke/target hash、编译图、唯一主输出、每任务输出前缀和以 job UUID 表示的 submission token；generation worker 执行前再次复验。只有 loopback Deployment 且 API/worker 可读取同一 `COMFYUI_OUTPUT_DIR` 时可创建，远程 ComfyUI 不会因 Workflow passed 而绕过产物下载合同。

任务状态为 `queued → submitting → submitted/running → succeeded|failed|cancelled|uncertain`，取消中使用 `cancel_requested`。创建支持 `Idempotency-Key`；同键同请求返回既有任务，不同请求显式拒绝。外部模型必须逐任务提交 `allow_external_calls=true` 与预算上限，服务端在排队前校验可确定费率；提交边界不确定时进入 `uncertain` 并禁止自动重放。`retry` 只接受明确失败或已取消任务。历史 ComfyUI 素材任务的请求快照 schema version 小于 3 时缺少完整 `workflow_inputs` 冻结意图，服务端会明确拒绝自动重试，用户必须从当前 Workflow 重新创建任务。

`resolve-uncertain` 只接受 `uncertain` 任务并追加不可变管理员证据，结论为 `confirmed_not_submitted`、`confirmed_remote_failed`、`confirmed_remote_cancelled` 或 `confirmed_remote_succeeded`。该 action 也支持 `Idempotency-Key`；响应丢失后同键同请求返回既有 resolution，任务详情返回 `latest_resolution` 供刷新后追溯。媒体远端结论必须关联与既有事实一致的 `provider_request_id` 或服务端已冻结的摘要；确认未提交时任务不得已有 request id 摘要或 submitted 时间。ComfyUI 使用 job UUID 作为稳定 submission token，恢复任务会先查询同一 token；远端 missing 不能证明未提交，仍保留 `uncertain` 等待 staff。超长 provider id 不会被截断用于轮询，只保存 SHA-256 并进入人工核验。可提交实际费用与币种，省略时沿用上一轮有效人工证据；后续 collect 金额如与人工证据冲突会重新进入对账状态，绝不静默覆盖。

任务详情公开 `submission_started_at`、`cancel_requested_at`、`cancel_submitted_at`、`ignore_late_result` 和 provider request id 摘要，不公开原始远端 id。取消意图一经记录不会因人工消歧丢失：远端晚到成功只恢复原 request 的费用 collect，产物不下载入库；文本响应或仅有 request id 摘要的结果无法安全恢复时明确关闭后，才允许用户创建新的显式意图。

异步 API 产物只有在受限下载和媒体校验后才物化；ComfyUI 产物只有从受控 `COMFYUI_OUTPUT_DIR` 中冻结输出前缀下取得唯一、类型匹配的持久 Save 主输出并完成大小、checksum 与媒体探测后才物化。Asset 详情中的 `preview_url` 与 `download_url` 是短期签名地址；客户端下载前应重新读取详情，`download_url` 以附件响应返回受管文件。只有远端 `reference_url` 而没有受管文件的条目不提供下载链接。人物/场景/道具图片以及参考视频、参考音频可由现有短剧工作台选择；剧本素材必须通过 `POST /api/v1/script-sources/` 的 `asset_id` 输入复制为目标项目的 preview revision，并继续走人工确认门，不能覆盖或绕过已确认剧本。

独立素材生成只写素材库和自身审计状态，不创建 Episode、Shot、ProductionRun、装配或交付事实；因此它不是短剧生产旁路。

## ProductionRun：短剧生产唯一入口

- `GET/POST /api/v1/production-runs/`
- `GET /api/v1/production-runs/{id}/`
- `POST /api/v1/production-runs/{id}/approve/`
- `POST /api/v1/production-runs/{id}/regenerate-bible-images/`
- `POST /api/v1/production-runs/{id}/select-route/`
- `POST /api/v1/production-runs/{id}/authorize-cost/`
- `POST /api/v1/production-runs/{id}/continue/`
- `POST /api/v1/production-runs/{id}/select-artifact/`
- `POST /api/v1/production-runs/{id}/retry/`
- `POST /api/v1/production-runs/{id}/resolve-uncertain/`
- `POST /api/v1/production-runs/{id}/cancel/`
- `GET /api/v1/generation-attempts/`

新的短剧生产必须从 collection POST 创建，并按服务端返回的当前 gate/action 推进。不得从 task、assembly、release、素材生成任务或已删除的旧生成路由发起旁路生产。

`GET /api/v1/capabilities/production/` 返回当前队列计数及 `production.recommended_max_pending`、`production.recommended_max_active_per_user` 两个配置建议值。其 `queue_guidance_enforcement` 固定为 `advisory`；超出建议值时只在 `queue_advisories[]` 返回结构化 warning，并在新建媒体任务的冻结 `capability_snapshot.queue_guidance` 中留痕，不返回 409，也不要求确认。响应的 `effective_limits` 同时投影当前 active system Revision 中已经实际生效的 route、集数、每集镜头、总镜头、生成任务、参考绑定、Bible 候选/绑定/重生成实体、对白、执行监控、提示词审核快照和审计说明安全上限；客户端应以该投影生成提示，不得内置同名上限。`creation_defaults` 则唯一投影 `production.project_creation_policy` 的项目尺寸、FPS 与新运行集数/镜数/时长/生成任务默认值；缺失或非法时能力接口返回 409，客户端不得回退本地默认值。外发授权、费用预算、ProductionRun 的 `max_generation_tasks`、运行主机实际并发和执行完整性合同仍是独立硬约束。

创建模式包括：

- existing project：不提交剧本输入，只允许已有 Episode 的项目进入下游生产；
- imported script：先通过 `ScriptSource → ScriptRevision → confirm` 固化用户导入原文，再用含 `script_input/confirmed_revision` 的 published recipe 创建运行；recipe 可在其后声明 `script_structure/ai → script_review/review(required)`，把非规范原文结构化为可解析的分集分镜候选；
- AI script：提交故事描述、集数、每集镜数和单集时长；schema v1 的人工/辅助配方进入 `script_generate/ai → script_review/review(required)`，确认后由服务端确定性生成 Bible，再进行 Bible 审核。schema v2 的无人值守配方则使用下述注册策略审核，不伪造人工确认。

新建连续剧页面的“手动 / 导入剧本 / AI 描述”是上述正式 API 的编排入口，不是新的旁路。导入和 AI 模式必须创建真实 ProductionRun：导入先保存并确认原文，再提交 `script_revision_id`；AI 模式提交 `idea` 等生成参数。页面可按 published recipe 自动连续调用 `continue` 推进 auto 阶段，但遇到 required 审核、外部调用授权、预算、route 选择、产物选择或其他服务端 blocker 时必须暂停并展示恢复动作，不能把“项目已创建”显示为生产已完成。

ProductionRecipe schema v2 可显式声明 `interaction_mode=unattended`。基础闭包为 `script_generate → script_review(policy) → bible_materialize → bible_review(policy) → storyboard → script_normalize → script_normalize_review(policy) → bible_image_generation → bible_image_review(policy) → storyboard_review(policy) → prompt_compile → media_generation → artifact_selection(policy) → assembly → delivery`；也可在 `bible_review` 与 `storyboard` 之间成对加入 `visual_rules_generate → visual_rules_review(policy)`。基础闭包的六个 review 必须分别使用服务端注册的 `decision_policy`；`script_normalize` 必须使用具备 `prompt.normalize` 能力且已有当前真实烟测的固定单一 Deployment route，Bible 图片与媒体 route 也只能使用配方冻结的兼容 route，媒体产物仍只允许单候选策略。AI 新剧本的 unattended 创建只接受人物、场景、道具 Bible 均为空的真正新项目；项目已有任一 Bible 实体时在创建任何 Run/审核事实前返回 `unattended_existing_bible_requires_new_project`，要求另建空项目，避免把既有项目无关实体混入本 Run 的全自动闭包。创建请求必须提交 `unattended_confirmed=true`；`reference_inputs` 的数量、可用用途、用途级数量、互斥与音频配对规则，只由所选 route 中当前模式的冻结 `conditioning_contract.modes[].inputs` 及 `dynamic_reference_topology` 决定。客户端必须为每项显式提交安全 `purpose` 与 `use_embedded_audio`，服务端不猜默认用途；没有 topology 或用途未声明时 fail closed。服务端按合同允许的数量逐条重新读取并冻结 owner 的受管 `reference_video` 文件，复验路径边界、MIME 与字节 SHA-256；时长、帧率、格式转换和音轨对齐由同一 mode 的 `dynamic_reference_topology.preprocessing` 受控 adapter 执行，而不由入库层或通用主逻辑写死。客户端传入的路径、checksum 或未声明用途都不是授权。

无人值守审核写入 `ApprovalDecision.actor=recipe_policy`、注册 policy 及其 hash，`decided_by` 必须为空；人工 approve action 不能写入该运行。脚本、文本 Bible、提示词标准化、Bible 图片、逐镜参考绑定和单候选产物只有在各自冻结事实满足策略时才自动推进。`bible_image_review` 使用 `select_single_bible_candidate_per_entity_v1`，要求每个实体恰好存在一个来自本 Run、本生成阶段的当前模型候选；策略会把该候选物化为受管 Asset 并设为实体锚点，不会在无人值守状态下猜选素材库图片或接受人工追加生成。`storyboard_review` 使用 `bind_frozen_reference_inputs_by_contract_v2`，必须把每个 Shot 关系中的人物、场景、道具精确绑定到本 Run 已批准的实体锚点，并把本 Run 按冻结工作流合同校验的全部动态参考视频逐条原样绑定到每个视频 Shot；数量、用途、互斥与音轨配对继续由合同裁决，合同允许零条且用户未选择时写入空视频列表；任一已物化 Bible 实体未出现在任何 Shot 关系中会以 `unattended_storyboard_bible_entities_unused` 阻断。`script_normalize_review` 必须显式使用 `accept_all_valid_frozen_prompt_translations_v2`；旧 v1 policy 不会在原 hash 下改变语义。v2 只接受同一成功 attempt 的完整冻结候选，逐项复验 request id、source/context hash、英文执行文本与 canonical identity。名称 provisional 必须精确等于当前实体的 Character `stable_speaker_id`、`scene:{slug}` 或 `prop:{slug}`；策略优先使用项目 glossary，否则从同 scope visual identity 派生唯一短 canonical，无法安全派生时使用确定性分类标签，并将映射、派生方式、替换次数和 overrides hash 冻结到审批 evidence。全部候选只做大小写敏感、完整 token 边界、单次替换；权威值不匹配、未知/扩展 token、大小写漂移或替换后残留均 fail closed。候选缺失、重复或空白分别使用明确的 unattended blocker，源数据、上下文、执行文本或 canonical identity 校验失败使用 `unattended_prompt_normalization_validation_failed`，物化与策略审批在同一事务内回滚且不写入审批事实。策略异常、费用未知、外发未授权、预算不足、远端不确定或媒体校验失败都保持 `blocked/uncertain`，不会回退人工身份、旧素材或模拟成功。

`script.structure` 的输入始终绑定完整 confirmed revision，不允许截断后外发。只有与正式 renderer 输出逐字节一致的规范脚本可走确定性的 `no_submission` 审核候选；其他文本必须使用显式声明 `script.structure` 能力、受控 chat transport、当前有效 operation smoke 和唯一 active PromptProfile binding 的 route。远端结果必须覆盖全部来源行并通过严格 IR 校验后才能落为 preview revision；原 confirmed revision 在 `script_review` 批准前保持不变。

运行会固化 project、content、configuration、recipe、workflow、route 和 cost 相关快照。后续项目配置变化不回写既有 run。

`POST /api/v1/production-runs/{id}/approve/` 在批准 `script_review` 时可提交 `adopt_generated_metadata=true`。该开关只采用冻结候选中的 title/logline；默认 false，不会改写项目元数据。如果运行创建后对应项目字段已被人工修改，服务端保留并发编辑并在批准证据中记录 `concurrent_edit`，不会用旧候选覆盖。该字段不能用于拒绝决定或其他审核阶段；脚本正文仍以冻结 preview/候选及其 hash 为准。

创建请求使用 `workflow_selection.schema_version=2`。`mode=uniform` 通过 `route_index` 冻结整集媒体 route，并用 `prompt_profile_selections[{operation,mode,prompt_profile_binding_id}]` 为本次实际媒体合同逐项显式选策略；`mode=per_shot` 则使用 `shot_routes[{shot_id,route_index,prompt_profile_binding_id}]` 完整覆盖所选 Episode 的每个 Shot。索引只能引用本次 published recipe 的 `media_generation` routes；服务端会拒绝重复、缺失、额外合同、跨项目或未选 Episode 的 Shot，并逐项复验所选 binding 与 workflow、deployment、实际 ModelProfileVersion、operation、mode、输出类型及 H3 mode 的兼容性。AI 或导入剧本在 Shot 尚未物化时首版只允许 `uniform`，并按项目默认视频 mode 提交唯一未来合同。解析后的逐镜头安全投影、精确 binding/Profile/contract/model 引用及 hash 同时冻结在 ProductionRun、媒体 StageRun 输入、PromptArtifact 与 GenerationAttempt；重试只读这些冻结事实，不按后来 active/priority 变化重新选策略。

创建页通过 `GET /api/v1/production-recipes/{id}/media-route-options/` 读取 `schema_version=2` 的安全目录。每条媒体 route 除工作流/部署标签、输出类型、H3 模式和结构化依赖缺口外，还按 `operation+mode` 返回 `prompt_profile_requirements[].choices[]`；每个 choice 都含可提交的 binding ID/key、不可变 Profile 版本、contract SHA-256、精确/族级 scope 与仅供目录展示的 priority。含 Bible 参考图阶段时，顶层 `bible_image_generation` 返回所有 Bible routes 共同兼容的显式选择集合。响应不返回 Provider 或 deployment 配置；页面必须在创建前要求用户逐项选择，但创建接口仍会独立复验，不能把目录预检当作执行授权。新建连续剧的 AI/导入入口也使用该目录并只写入 `uniform` 选择；不含 `media_generation` 的纯脚本配方不提交 `workflow_selection`。

含正式 Bible 参考图的配方只接受 `bible_materialize → bible_review → storyboard → script_normalize → script_normalize_review → bible_image_generation → bible_image_review → storyboard_review → prompt_compile` 的顺序。旧顺序的 published recipe 不能创建新 Run；已经冻结旧顺序且尚未结束的 Run 也会 fail closed，必须以新配方创建新 Run，不能从 Bible 审核直接跳入 prompt compile。

批准 `bible_image_review` 时，请求必须同时提交 `expected_source_snapshot_sha256` 与 `bible_image_bindings[]`。每个实体可只选模型候选、只选素材库图片，或混合选择，精确结构如下：

```json
{
  "expected_source_snapshot_sha256": "64 位小写十六进制 SHA-256",
  "bible_image_bindings": [
    {
      "entity_type": "character",
      "entity_id": "实体 UUID",
      "selected_artifact_ids": ["GeneratedArtifact UUID"],
      "selected_library_assets": [
        {
          "asset_id": "Asset UUID",
          "expected_sha256": "64 位小写十六进制 SHA-256"
        }
      ],
      "anchor_artifact_id": null,
      "anchor_library_asset_id": "Asset UUID"
    }
  ]
}
```

`selected_artifact_ids` 与 `selected_library_assets` 至少一侧非空，合计最多 32 张且各自不得重复；`anchor_artifact_id` 与 `anchor_library_asset_id` 必须且只能有一个非空，并且属于对应选择集合。素材库 Asset 必须是项目所有者或全局范围内的 active 图片，类别须与 character/scene/prop 相符，项目封面不可选择；客户端提交的 `expected_sha256` 必须与服务端重新读取的真实文件 checksum 一致，否则返回 `bible_library_asset_snapshot_stale`。同一最终 Asset 不能绑定到多个 Bible 实体。该 Run 的模型候选集由本次新候选和同项目、同实体、来源仍 current 的正式历史 GeneratedArtifact 合并而成；来源 stale、字节或 provenance 不完整的历史版本只读且不可选择。批准会复用已物化版本，并把尚未物化的模型版本复制为项目租户所有的不可变 Asset；审核输出只冻结本次明确选择的 Asset 集合，不会把实体当前所有 reference assets 猜成已审核版本。

模型候选物化为 Asset 时，`name`、`description`、`prompt_hint` 分别精确取自当前审核实体的名称、基础描述和 `visual_identity`，`style` 取自项目，`source_info` 单独记录 ProductionRun 与候选编号。任一业务字段为空会阻断整个审核事务，不能以来源文字或默认占位符代替。升级迁移只有在 Asset → GeneratedArtifact → ProductionRun → 项目/实体、冻结 manifest、checksum 和 owner 全链一致时，才修复仍保持旧自动占位模式的字段；非法、缺失或关系不一致的 metadata 保持原值和空来源。人工修改过的名称或描述按字段保留，空视觉锚点可从当前实体补齐；修复记录可逆迁移审计并更新 Asset 时间戳，使依赖旧素材快照的草稿显式变为 stale，而不会静默维持旧 hash。

`POST /api/v1/production-runs/{id}/regenerate-bible-images/` 只允许项目所有者或 staff 在人工/辅助运行处于 `bible_image_review/awaiting_approval`、且尚无审核决定时调用。它使用本 Run 已冻结的 Bible 模型、route、PromptProfile 和执行合同，为指定实体各追加一个新候选；旧候选、已物化 Asset 与正式 Bible 绑定都不会被覆盖。请求为：

```json
{
  "request_id": "本次操作的 UUID",
  "expected_source_snapshot_sha256": "页面当前候选快照的 64 位小写 SHA-256",
  "entities": [
    { "entity_type": "character", "entity_id": "实体 UUID" }
  ]
}
```

`entities` 为 1..32 个不重复的 character/scene/prop 实体。成功返回更新后的 ProductionRun 详情；同一 `request_id` 与同一请求可安全重放且不会重复创建候选，同一 ID 改变请求内容返回 409 `bible_regeneration_idempotency_conflict`。页面快照、候选、实体语义来源或状态已变化时返回明确的 409 冲突；每个实体在同一 Run 最多保留 8 个模型候选。重新生成可能产生新的外部费用，未知费用会打开一份新的成本授权门，不复用首次生成的未知费用授权。全自动运行禁止调用该接口；如需人工重生或从素材库选图，应使用人工/辅助运行。

批准后 `storyboard_review` 是进入 `prompt_compile` 前唯一的逐镜参考绑定门。请求必须提交同一 source hash 和完整 `shot_reference_bindings[]`：I2VA 使用精确 `first_frame_asset_id`；每个 Ref2VA Shot 必须至少关联并绑定一张本 Run 已批准的实体图，同时把其 Shot 关系中的全部角色、场景和道具逐实体提交精确 `entity_id + asset_id`，并可绑定最多 3 个参考视频（每个选择内嵌音轨或一个外部配套音频，二者互斥）及最多 3 个独立音频；FL2VA 允许零张、仅首帧、仅尾帧或首尾双图，双图不能是同一 Asset。实体图片必须来自本 Run 对相应实体已批准的 Bible 集合。动态参考视频会作为完整视频条件被模型读取；`purpose` 仅表达希望侧重的动作/镜头意图，不提供模型级内容隔离，也不能替代人物、场景或道具 Bible 锚点。服务端在一个事务中校验整批并同时写入 `Shot.reference_bindings` 与派生的 reference-assets 关系；任一镜头失败时零审批、零部分写入。`prompt_compile` 会再次核对 Bible 图片生成/审核快照、每镜至少一个实体图、Shot 实体关系、精确 Asset 绑定与批准集合，分别以 `ref2va_bible_images_required`、`ref2va_bible_binding_snapshot_invalid`、`ref2va_shot_bible_image_missing`、`ref2va_entity_image_bindings_incomplete` 或 `ref2va_entity_image_not_approved` 阻断不完整输入。下游 PromptArtifact、工作流 staged inputs 和上传字节都按所选 Asset ID、SHA-256 和大小精确复验。

含 Bible image route 的 Run 创建请求必须提交 `bible_image_prompt_profile_binding_id`。服务端逐 route 复验该 binding 与实际模型、`image.generate` 默认 mode 以及 Bible required/positive-source 合同兼容，并把精确 binding/Profile/contract/ModelProfileVersion/operation/mode 冻结到 Run、Bible Stage routing/input、PromptArtifact 与 GenerationAttempt；运行期间修改 active/priority 不会使既有 Run 改选其他策略。所选 Profile 必须对 character、scene、prop 三类实体都可满足 required 字段，并具有至少一个必定非空的 positive source 或非空 default，否则创建或阶段推进会在外发前 fail-closed。

`GET /api/v1/projects/{id}/bible-image-candidates/` 是只读历史投影。每个版本返回 artifact/run/stage/attempt/task、模型与 PromptProfile provenance、source-current 状态、当前 binding role、可选的 materialized Asset，以及实际文件的 MIME、大小和 SHA-256 完整性结果。已物化但当前 unbound 的历史仍保留；文件缺失、篡改或合同损坏时 `integrity_status` 显式失败且预览/下载 URL 为空，客户端不得使用 Run artifact 的旧 URL 绕过该状态。不存在项目级 promotion/select 写旁路，历史版本只能在新的正式 `bible_image_review` 中重新选择。

`generation-attempts` 是按项目租户（`production_run.project.owner`）隔离的只读安全投影；`ProductionRun.owner` 仅表示发起操作的 actor/audit 身份。管理员可在 ProductionRun 被 `uncertain_remote_response` 或 `actual_cost_reconciliation_required` 阻断时，把投影中的 `attempt_id` 提交给 `resolve-uncertain`。响应只包含 request/result/provider request id 的 hash、管理员可见的短 suffix 和状态/费用元数据，不返回原始 provider request id、请求 payload、响应正文或错误正文。人工消歧只追加不可变 resolution；`confirmed_remote_submitted` 仅恢复异步媒体轮询，其他结论也不得展示为生成或 smoke 通过。

## 脚本与内容事实

- `/api/v1/script-sources/`
- `/api/v1/script-revisions/`
- `POST /api/v1/script-revisions/{id}/confirm/`
- `/api/v1/projects/` 及其 OpenAPI 中声明的内容子资源

AI 生产阶段对脚本来源的写入受 ProductionRun lineage 和状态约束。客户端不能绕开审核把生成文本直接写成确认脚本。

## 只读诊断投影

- `GET /api/v1/tasks/`、`GET /api/v1/tasks/{id}/`
- `GET /api/v1/tasks/{id}/input-prompt/`
- `GET /api/v1/generation-attempts/`、`GET /api/v1/generation-attempts/{id}/`
- `GET /api/v1/generated-artifacts/`、`GET /api/v1/generated-artifacts/{id}/`
- `GET /api/v1/prompts/`、`GET /api/v1/prompts/{id}/`
- `GET /api/v1/deferred-generation-requests/`、`GET /api/v1/deferred-generation-requests/{id}/`
- `GET /api/v1/assemblies/`、`GET /api/v1/assemblies/{id}/`
- `GET /api/v1/release-bundles/`、`GET /api/v1/release-bundles/{id}/`
- `GET /api/v1/release-bundles/{id}/verify/`

这些资源用于观察 lineage、attempt、冻结提示词、产物、source item、装配和交付验证；Task 与 Deferred request 没有 create/retry/cancel 写端点。创建、重试、取消与有序 fallback 必须使用所属 ProductionRun 的正式 action。ReleaseBundle verify 只执行交付诊断，不是生产创建入口。`generation-attempts` 按 `stage_run` 过滤，`generated-artifacts` 可按 `production_run`、`stage_run` 或 `generation_task` 过滤；Task collection 没有 `production_run` 查询参数，客户端只能读取任务投影中经验证的 `production_run_id`，不得按标题、Episode 或时间猜归属。

Task 安全投影通过 `production_run_id` 和 `production_variant_number` 暴露创建任务时冻结的整集归属。`production_run_id` 只接受任务快照中的有效 UUID；快照缺失或无效时返回 `null`，服务端和客户端都不得按 Episode、时间或标题猜测所属运行。`production_variant_number` 只读取冻结 `execution_identity.variant_number` 的正整数。详情中的 `input_parameters` 只投影经双层 hash 校验的当前 `GeneratedWorkflow` 与 `GenerationTask` 冻结执行合同：`reference_assets` 仅表示实际连入执行图的 `asset_bindings`，`continuity_assets` 仅表示随任务冻结但未连入执行图的一致性素材；模式名不会触发搬运或补齐。分辨率、提示词、帧率、时长和音频也只读取冻结语义与 `execution_metadata`。对于只把帧数和 FPS 绑定进执行图的通用工作流，请求时长与音频开关仍以同一任务创建时明确冻结的 snapshot 字段为事实；服务端校验 frames/FPS 对应的 `effective_frames` 与 `effective_duration_ms`，不会从其他记录补值。任一合同缺失或漂移时，详情和 `input-prompt` 仍显式失败并要求从 ProductionRun 新建任务，不会从 PromptArtifact、项目默认值或历史字段推断；安全投影和输出均不暴露内部路径或其他运行配置。

`GET /api/v1/tasks/` 使用独立的列表安全投影，避免一条已退役记录让整页审计事实不可读。每行新增 `input_contract_status` 与 `input_contract_code`：当前合同可验证时返回 `available`、空原因码及原有安全 `input_parameters`；历史 Schema 不受支持时返回 `unavailable / legacy_schema_unsupported / null`；Schema 已声明但冻结合同缺失或损坏时返回 `unavailable / frozen_contract_invalid / null`。不可用行仍保留数据库中的真实任务状态、时间和归属，不构造空素材、提示词、时钟或其他运行参数；任务页以中文 / English 双语明确显示合同不可用。该列表投影只用于读取审计，绝不恢复旧执行、重试、工作流导出或提示词读取路径。

任务诊断页只使用两种互斥的整集分组身份：存在 `batch_id` 的历史任务按精确 legacy batch 分组；否则仅在 `production_run_id` 与 `episode_id` 同时存在时按精确运行与分集组合分组。缺少上述完整身份的任务保持独立。父组按每个生产单元的最新审计尝试聚合状态与完成数，历史重试仍作为子记录完整展示；ProductionRun 单元键是 `shot_id + production_variant_number`，legacy 单元键是 `batch_index`。父级筛选和计数使用聚合后的整集状态，不把子任务数当成整集数，也不把 ComfyUI 当前节点采样百分比平均成整集完成度。

## Provider、模型与工作流

平台管理端点包括：

- `/api/v1/production-recipes/`
- `/api/v1/production-recipes/{id}/`
- `GET /api/v1/production-recipes/options/`
- `POST /api/v1/production-recipes/{id}/clone-successor/`
- `POST /api/v1/production-recipes/{id}/publish/`
- `POST /api/v1/production-recipes/{id}/retire/`
- `/api/v1/provider-profiles/`
- `/api/v1/model-artifacts/`
- `POST /api/v1/model-artifacts/{id}/verify/`
- `/api/v1/model-profile-versions/`
- `GET /api/v1/model-profile-versions/options/`
- `POST /api/v1/model-profile-versions/{id}/draft-next/`
- `POST /api/v1/model-profile-versions/{id}/publish/`
- `POST /api/v1/model-profile-versions/{id}/retire/`
- `/api/v1/model-deployments/`
- `POST /api/v1/model-deployments/{id}/retire/`
- `POST /api/v1/model-deployments/{id}/probe/`
- `POST /api/v1/model-deployments/{id}/smoke-test/`
- `POST /api/v1/model-deployments/{id}/resolve-smoke-uncertain/`
- `GET /api/v1/model-deployments/{id}/evidence/`
- `GET /api/v1/acceptance-smoke/`
- `GET /api/v1/comfy-workflow-sources/discover/`
- `POST /api/v1/model-deployments/{id}/saved-workflows/preview/`
- `POST /api/v1/model-deployments/{id}/saved-workflows/attest-model-artifacts/`
- `POST /api/v1/model-deployments/{id}/saved-workflows/batch-import/`
- `GET /api/v1/assets/workflow-smoke-candidates/`
- `GET /api/v1/model-deployment-smoke-attempts/`
- `GET /api/v1/model-deployment-smoke-attempts/{id}/`
- `POST /api/v1/model-deployment-smoke-attempts/{id}/cancel/`
- `/api/v1/workflow-templates/`
- `GET /api/v1/workflow-template-versions/`（集合不开放 POST；版本只能由 saved-workflow contract draft 的 batch promotion 创建）
- `GET /api/v1/workflow-template-versions/{id}/comfyui-workflow/`
- `POST /api/v1/workflow-template-versions/{id}/validate/`
- `POST /api/v1/workflow-template-versions/{id}/smoke-test/`
- `GET /api/v1/workflow-smoke-attempts/`
- `GET /api/v1/workflow-smoke-attempts/{id}/`
- `POST /api/v1/workflow-smoke-attempts/{id}/cancel/`
- `POST /api/v1/workflow-smoke-attempts/{id}/resolve-uncertain/`
- `POST /api/v1/workflow-template-versions/{id}/publish/`
- `POST /api/v1/workflow-template-versions/{id}/retire/`

ProductionRecipe publish 会校验唯一正式 content 入口和完整有序的 content → storyboard → prompt → media → selection → assembly → delivery 闭包；缺阶段、顺序错误、媒体 route 未冻结，或 route 引用的 Provider + Deployment 合并执行合同不完整都会拒绝发布。新建 ProductionRun 也会执行同一准入校验；合同无效时在任何费用承诺或远端提交前返回 `400 deployment_execution_contract_invalid`，错误响应不回显底层端点、配置值或密钥。

注册表使用受状态约束的生命周期，而不是对审计事实开放任意 CRUD：Provider 可新增、编辑和停用，非系统且无引用的停用项才可删除；模型版本与配方只有 draft 可修改或删除，published 只能 retire；工作流版本不支持原地 PATCH，必须导入新版本。新模型 `POST /model-profile-versions/` 的写入合同不包含 `version`，服务端固定创建 `v1`；`specification.schema_version` 也由服务端写入当前支持值，客户端应省略，提交不同值会被拒绝。已存在的 `profile_key` 必须改用精确源版本的 `draft-next` action。该 action 只接受当前最高 published/retired 版本，先校验源合同的大小与 SHA-256，再在事务中锁定模型族锚点，并按全部历史（包括 retired）分配下一号；当前最高版本已是 draft 时要求继续编辑，旧源或并发请求返回 409，普通 PATCH 不能改变 `profile_key` 或 `version`。`GET /model-profile-versions/options/` 返回当前规格结构版本、注册驱动及其模态/传输操作、模型模态、Artifact 角色、受控业务能力与已登记提示词策略族，供正式页面构造单选/多选；新模型可在选择器中显式选择由合法 `profile_key` 派生的同名新策略族，其他自定义文本会被拒绝；目录缺失不能回退为任意文本。ModelArtifact 仅在所属模型版本仍为 draft、状态仍为 `declared`、从未核验且不存在 RuntimeModelAvailability、工作流或配方引用时允许删除；绑定 published/retired 模型版本的记录是不可变证据。Deployment 必须先停用：完全没有 probe/smoke、主机可用性、工作流、配方、推荐或运行证据的误建空记录才可删除；需要保留证据的停用记录在替换 current 工作流与 draft/published 配方引用后使用 `retire`，进入不可逆只读终态。项目一旦存在正式 ProductionRun，`DELETE /api/v1/projects/{id}/` 同样返回 `409 protected_resource`，要求归档而不是级联删除；ComfyUI/async 候选、已物化 Asset、任务、签名 manifest 与审核历史必须继续可审计。

Settings 中 11 类平台注册资源的删除统一使用服务端影响复核，不能先发送无正文 DELETE 再从一句错误猜依赖。客户端先调用 `POST /api/v1/registry-deletion-preview/`，提交 `resource_type + object_id`；响应完整返回目标状态、所有依赖对象、关系说明、配方 JSONPath、`selectable_delete / automatic_delete / blocking_reference / preserved_history` 分类、精确计数、确认短语与 `impact_sha256`。可选草稿依赖默认不选；任务、ProductionRun、Smoke、快照、RuntimeModelAvailability、推荐/人工决策、ConfigRevision、PromptArtifact、Comfy 来源及 published/retired 版本永不成为可选级联项。

唯一执行入口是 `POST /api/v1/registry-deletion-execute/`；11 类资源 detail API 不再开放可绕过复核的 DELETE。正文包含 `resource_type`、`object_id`、`expected_impact_sha256`、全部已人工确认的 `selected_dependency_keys` 和服务端返回的完整 `confirmation_phrase`，稳定审计原因只通过 `X-Audit-Reason-Code` 传递。服务端先锁定目标并发现依赖，再锁齐可变依赖、自动子项及其生命周期父边界，最后第二次重算完整影响并核对目标及依赖的安全状态指纹；正式 Recipe/ConfigValue 写入，以及把 Prompt Binding ID/key 冻结进 ProductionRun、PromptArtifact、GenerationAttempt 和 Deployment Smoke request snapshot 的写入，共享相同事务 guard，并按 guard → Binding 行锁顺序在持锁后重读、复验引用身份。PromptProfileBinding 的 `key` 创建后不可变，避免历史事实因改 key 脱链。引用或对象内容漂移返回 `409 deletion_impact_changed` 并且零删除，漏选依赖返回 `409 dependency_confirmation_required`，伪造或选择不可删除事实返回 409。成功返回 HTTP 200 的目标与显式删除依赖摘要；属于 11 类注册对象的目标、人工选择项和自动子项写生命周期审计，其他组合草稿结构仍在结果中列明。该合同只删除平台注册记录，从不移动或删除磁盘模型/媒体文件。

本地 Comfy 模型的正确准备顺序为 Provider → draft ModelProfileVersion/声明 ModelArtifact → 启用并 probe RuntimeHost 至 active，由工作流 discover 返回的同 Provider active loopback 内部 source Deployment 完成草稿 artifact attestation 与完整文件闭包 → 发布 ModelProfileVersion → 建立目标 ModelDeployment → RuntimeHost Probe 形成该 deployment 的 availability → artifact verify。完成准备后，ComfyUI 工作流的正式验收链路是 **RuntimeHost Probe → Live Validate → Workflow Smoke**；Deployment Probe/Smoke 不能替代某个不可变工作流版本的实时结构校验和真实 Workflow Smoke。`async_rest` 不执行 ComfyUI 工作流，使用 **Deployment Probe → Deployment Smoke** 验证其部署 operation；文本驱动仅对已声明并受支持的 operation 执行 **Deployment Smoke**，不提供 Deployment Probe。管理员不在扫描前手选 source Deployment。远端模型没有本地文件时明确为 N/A，不伪造 artifact。ModelDeployment 写入使用唯一权威合同校验，同时核对 Provider/Deployment JSON 完整性、敏感字段和驱动必填项。Provider 启用或更改 driver/config 时会在同一事务锁定并重验所有 active Deployment；任一合同失效则整个变更回滚，而停用 Provider 仍允许以便运维修复。已发布模型的文件关联不可原地修改。`model-artifacts/{id}/verify/` 只接受未过期且合同 hash 有效的 HostCapabilitySnapshot，并把 snapshot、操作者和时间写为可追溯核验证据。

ModelArtifact 的 `filename` 是受控 models 根目录下的相对文件名，`inventory_folder` 是受控库存目录；API 不保存可随机器变化的绝对路径，也不会移动或删除物理模型文件。修改 draft artifact 的定位字段、SHA-256 或大小时，服务端原子恢复为 `declared` 并清空当前 `verified_*` 指针，既有 RuntimeModelAvailability 仍保留为历史观测；新就绪判断必须等待新声明对应的新鲜主机快照。published/retired artifact 的相对定位或字节发生变化时必须创建更高 ModelProfileVersion。仅 ComfyUI 安装根变化而相对定位与字节不变时，不改 artifact；按运维文档更新受控根/端点、重启并重新执行 host probe、artifact verify，以及受 target 漂移影响的 deployment/workflow 验证。

Deployment 真实烟测的受控 operation 包括 `image.text_to_image`、`video.text_to_video`、`audio.text_to_audio`、`prompt.normalize`、`script.generate`、`script.structure` 与 `visual_rules.generate`。`script.generate` 只接受在 `specification.capabilities` 中精确声明该能力、包含完整 `series_concept` 策略且使用受控 chat transport 的已发布文本模型；烟测提交最小的一集一镜请求，并用正式剧本合同严格校验结果。`script.structure` 使用独立能力声明、输入上限、严格结构化合同与 operation smoke；`script.generate` 的证据不能替代它。`visual_rules.generate` 从所选 ModelProfileVersion 的 `specification.visual_rules` 冻结三项硬容量及可选 `quality_advisories`；颜色数量、负面约束数量/单项长度和候选文本长度偏离显式建议时仍返回完整候选，只写入 `warnings` 与 `advisory_audit`。`recommended_negative_clauses` 也只从该冻结策略读取，存在时合并为可人工移除的候选并审计移除项；未配置的建议不启用隐含默认或代码清单。结构、允许枚举、唯一性、可打印英文/控制字符、来源回显及 Provider JSON/请求绝对上限仍硬失败。外部调用必须先有可确定 pricing。七类 operation 都先把冻结请求和目标 hash 写入同一 `ModelDeploymentSmokeAttempt`，再由 worker 跨越一次提交边界；公开响应与最终 evidence 只返回请求、响应、结果和 request id 的 hash、费用元数据与错误分类，不返回冻结请求、提示词、模型正文或原始 provider request id。

工作流版本以 semantic bindings 对接 Comfy 图，不依赖固定节点号。bootstrap 版本为 `needs_review` 草案；import、Live validate、真实 smoke、核对 publish 和 retire 是不同动作。详情端点持久返回验证目标、合同 hash、validation evidence 与 smoke evidence；API 不会自动下载模型或把 catalog 条目标成已部署。满足精确证据门禁的 current/published image/video/audio Workflow 会由素材生成 `options` 作为 `comfy_workflow` route 返回，这不等于其历史 smoke 可替代 Deployment smoke，也不让 Workflow 绕过素材任务自身的冻结、幂等、取消、uncertain 和产物入库状态机。

文本 Smoke 的四个 operation（`prompt.normalize`、`script.generate`、`script.structure`、`visual_rules.generate`）必须同时提交 `prompt_profile_binding_id`。候选来自验收查询的 `deployment_smoke.operations[].prompt_profiles[]`，只包含适用的启用绑定与已发布策略；页面不自动选择第一项。服务端复核模型、operation、mode 和版本，冻结策略引用、合同 SHA 与装配后的请求；worker 不按后来优先级重新选策略。同能力已有其他策略的活动任务时拒绝复用。媒体 Smoke 仍使用其 `smoke_inputs` 合同，不接受文本策略参数。

### 验收与烟测集中查询

`GET /api/v1/acceptance-smoke/` 是 staff 平台管理员使用的唯一集中查询接口。它只读取已持久化的模型、工作流、Probe、Live Validate 和 Smoke 事实，不调用 Provider/ComfyUI，不创建 attempt，不修复或改写历史状态，也不把读取时发现的漂移回写成新的验收结论。对应正式页面为 `/settings/acceptance-smoke`；页面负责集中展示、筛选和解释证据，只跳转到既有唯一动作表单：ComfyUI 的 Live Validate、Workflow Smoke 与发布仍在 `/settings/workflows/{workflow_version_id}`，`async_rest` 的 Deployment Probe/Smoke 与文本 Deployment Smoke 仍在“设置 → 模型”的实例操作区。集中页不得复制 fixture 编辑器或直接建立第二套提交路径。

查询参数如下：

| 参数 | 约束与含义 |
| --- | --- |
| `subject` | 必填，枚举为 `workflow` 或 `deployment`；决定结果以工作流版本还是模型实例为主对象 |
| `search` | 可选；按服务端声明的安全名称和标识字段筛选，不改变关联判定 |
| `deployment_id` | 可选 UUID；筛选与该实例有关的精确验收、执行或兼容结果 |
| `workflow_id` | 可选 UUID，且仅允许与 `subject=workflow` 同时使用；用于工作流详情精确读取当前 freshness，其他组合返回 400，不得静默忽略 |
| `page` | 可选，正整数页码；格式无效返回 400，请求不存在的页沿用 DRF 标准 404 分页错误 |
| `page_size` | 可选，服务端每页最多返回 200 条；大于 200 时钳制为 200，格式无效返回 400，不得绕过服务端分页 |

成功响应使用标准 `count / next / previous / results` 分页合同和稳定排序。客户端必须跟随 `next` 读取所需页面，不能把首屏结果当成全集；无匹配项返回空 `results`，权限不足、查询无效或事实无法安全投影时显示明确错误，不能伪装为空数据。接口仅允许 staff 访问：匿名和非 staff 分别按认证与权限合同失败；非法 `subject`、UUID、`workflow_id`/`subject` 组合或其他非分页查询参数返回 400，无效页码沿用 DRF 的 404。

每条结果必须区分以下三层证据，不能按名称相似、默认实例、优先级或同一 Provider 猜关联：

1. **Profile 兼容**：工作流版本的 `model_profile_version` 与 Deployment 的 profile 精确一致，只证明两者可作为候选；不代表已经验收或执行。
2. **精确验收**：工作流版本的 `validated_deployment` 精确指向该 Deployment，证明历史 Live Validate 对这个实例执行过。
3. **精确执行**：持久 `WorkflowSmokeAttempt` 同时精确指向工作流版本和 Deployment，证明某次 Workflow Smoke 确实以该实例为目标；attempt 终态和证据完整性仍须单独判断。

历史结论与当前 freshness 是两个维度。历史 `validation passed` 或 `smoke passed` 保留其当时的不可变审计语义；接口在读取时用当前 Provider、Deployment、ModelProfileVersion、Artifact、工作流结构与 target hash 复验并单独返回 `fresh`、`drifted`、`not_run` 或明确不可判定状态。因配置、artifact、结构或 target 变化而得到 `drifted` 时，页面必须同时显示“历史曾通过”和“当前已漂移”，并引导重新 Live Validate/Workflow Smoke；不得把历史 passed 改写为 failed，也不得把旧 passed 当成当前可发布证据。Deployment Smoke 与 Workflow Smoke 始终分别展示，二者不能互相替代。

模型页的运行实例直接展示适用提示词策略；ComfyUI 实例同时按精确 `deployment_id` 分页读取对应工作流的在线校验与烟测结果，不用直接 Smoke 的“不适用”代替工作流结论。烟测通过仅表示该工作流版本在此实例的当前证据有效，不表示所有同模型候选均已通过，也不代替发布门禁。其他候选与历史记录按需展开。提示词绑定按所属模型版本／模型族列出，默认展示启用且策略已发布的项，全部绑定保留管理入口；同版本实例共享适用范围，实际任务仍按操作、模式和所选绑定校验并冻结，并非实例与策略一对一。此展示只读取现有 API，不提交烟测、不改写关系或证据。

可直接调用的模型实例按 `deployment_smoke.operations[]` 逐能力展示，不再使用实例级最后一条 Smoke 代表所有能力。模型页与验收中心共用展示组件；`有效通过 x/y 项` 仅统计当前适用能力，最近任务排队、执行、失败和证据有效期是独立维度。读取失败或数据缺失必须显示不可判断，不能退回历史 `passed`。

| 字段 | 类型与口径 |
| --- | --- |
| `evidence_state` | 字符串：`fresh` 当前有效、`expired` 到期、`drifted` 目标配置改变、`failed` 失败且无有效通过、`missing` 无记录、`unverified` 有记录但无有效证据、`unavailable` 合同不可核验、`uncertain` 提交待确认、`not_applicable` 当前不适用。服务端从持久事实投影，不回写历史 |
| `latest_snapshot` | 对象或 null；按该能力的最新烟测快照，包含 `captured_at`、`expires_at`、历史 `status` 和当前 `target_matches`，过期历史不因另一能力新烟测而消失 |
| `fresh_passed_snapshot` | 对象或 null；符合当前目标、未过期、已真实提交且覆盖该能力的通过证据。可能早于最新失败或执行中的尝试，二者并列展示 |
| `latest_attempt` | 对象或 null；该能力最近任务，包含状态与开始/结束时间；任务成功不自动等于证据当前有效 |
| `prompt_profiles` | 数组；文本能力的当前适用策略，每项含冻结身份 `prompt_profile`、`evidence_state`、`latest_snapshot`、`fresh_passed_snapshot`。非文本能力为空；每个绑定及精确策略版本分别验收 |

Attempt 和 Snapshot 的安全摘要返回 `prompt_profile`（绑定、策略版本、名称与合同 SHA，不包含提示词正文）。未冻结策略的旧记录返回 null，仍可查看历史状态，但不能作为当前策略组合的有效证据。能力级有效通过只表示至少一个策略组合有效，不表示所有策略均已通过；生产门禁必须匹配实际选用策略的绑定与版本，不借用其他策略的通过结果。变更绑定所指版本不会追认旧证据。

证据有效期仍由 Provider/实例运行配置的 `capability_ttl_seconds` 在生成快照时冻结；前端仅在服务端 `expires_at` 到达时将有效状态降为过期，不自行配置 TTL 或推断通过。按能力历史通过分页范围内的快照窗口查询批量读取，不逐实例查询，不调用外部执行器。

使用 `deployment_id` 查看 Workflow 时，`validation.fresh` 和 `smoke.fresh` 只允许归属于该筛选实例：若历史 `validated_deployment_id` 或 `smoke.evidence_deployment_id` 指向另一个实例，响应保留历史 hash/状态，但返回 `not_current_for_selected_deployment` 和稳定原因码，不得借用另一个实例的绿色状态。存在未消歧的 Workflow Smoke attempt 时，新的 Workflow Smoke 动作必须禁用，并返回指向既有 `workflow-smoke-attempts/{id}/resolve-uncertain/` 合同的处理动作。Deployment 反向汇总中的“仅 Profile 兼容”只计算既没有精确验收、也没有该实例精确 Smoke 历史的 Workflow 去重集合。

集中响应只在当前生效的 `workflow.max_graph_bytes`、Workflow/Profile 完整性和 execution target 本地预检均通过后把 Live Validate 动作标记为 `ready`；这些预检不调用 Provider。结构或 target 已确定损坏时返回 `workflow_validation_precheck_failed` 并禁用该动作，不能先显示“可执行”再由正式动作确定性失败。

Deployment 行分别返回模型合同状态和当前 target 状态。模型规格损坏使用 `model_profile_contract_invalid`，实例 target 无法核验使用 `deployment_target_invalid`；相关 Probe/Smoke 动作 fail closed。已有健康或 Smoke 快照仍作为历史摘要返回，但 `target_matches=false`、`fresh=false`，不得把合同损坏静默显示为“未配置”或“没有历史”。

媒体 ModelProfileVersion 的 `specification.execution_monitoring` 通过类型化字段声明默认/节点类型静默窗口；WorkflowTemplateVersion 如确需当前 graph 的节点 ID 覆盖，只能由已注册 workflow-family projector 从 ComfyUI 源确定性派生，API 和版本页都不接受人工 JSON。ProductionRun 合并后把合同/hash 冻结到路由、GenerationAttempt、GeneratedWorkflow 和 GenerationTask；worker 要求 GeneratedWorkflow/GenerationTask 双层合同完整且一致，任一层缺失或漂移都在远端提交前 fail closed，不再存在 legacy 回退。字段 Schema、优先级、deadline 语义和新版本迁移见 [长节点执行监控合同](execution-monitoring-contract.md)。

### ComfyUI 已保存工作流纳管

该主路径分为五层，权限与副作用不能混用：

1. **发现并纳管来源**：`discover` 只读扫描；staff 显式以 `import_kind=candidate` 纳管合法文件后，创建不可变 `ComfyWorkflowSource`。candidate 准入只检查受控目录、JSON 可解析、大小边界和敏感字段，不要求目标模型已发布、目标执行 Deployment 可用或 projector 已匹配。用于读取本地目录的内部 source Deployment 只是受控传输代理，不是目标执行授权。
2. **生成执行合同草稿**：preview 只接受已经纳管的精确 candidate。没有主输出选择时返回候选；明确选择主输出后，服务端读取实时 `object_info`，持久化或复用不可变 `ComfyWorkflowContractDraft`。响应以 `admission_tier`、`actionability`、`blockers`、`live_validate_requirements` 和 `recovery_actions` 区分“仅候选”与“可提升”；无法解释的部分不会删除来源候选。
3. **Live Validate**：只有 `actionability=ready_for_executable_import` 的草稿可提升为 `WorkflowTemplateVersion`。版本进入 Live Validate 时才要求匹配的 active 目标 Deployment、published ModelProfileVersion、新鲜模型文件证明、实时节点 Schema 以及冻结 projector/adapter 身份一致。
4. **真实 Smoke**：版本按 `smoke_fixture_schema` 接受具体参数和 Asset，创建持久 `WorkflowSmokeAttempt`，验证真实产物、媒体流和目标容量。
5. **发布与生产路由**：合同、Live Validate 和真实 Smoke 证据均通过且仍 fresh 后才能 publish；Recipe 与 ProductionRun 只接受已发布版本。candidate/contract draft 不具备执行权，不能进入 Validate、Smoke 或生产路由。

Settings 携带 `model_profile_version` 上下文时，底层 `discover` 仍扫描全部受控来源；URL 上下文只过滤和解释列表，不改变来源扫描或生成关联事实。精确关联必须来自 candidate/draft/version 的 UUID 与冻结 provenance，不能按文件名、同模型族、默认 Deployment 或相似名称推断。切换或清除上下文后，旧请求结果不得回填当前列表。

| Method 与 Path | 请求与用途 | 成功响应与副作用 |
| --- | --- | --- |
| `GET /api/v1/comfy-workflow-sources/discover/` | 按系统配置 `workflow.comfyui_saved_workflow_directory` 扫描全部 active ComfyUI userdata 来源作用域，并在服务端发现/匹配 | 200；返回每个来源的成功或失败、source SHA、分类、匹配状态、候选、精确恢复动作、既有 provenance、`contract_upgrade_required`，以及包含有效目录和扫描/批处理预算的 `effective_limits`；部分来源失败不会伪装为空目录，不写数据、不提交 GPU |
| `POST /api/v1/model-deployments/{id}/saved-workflows/batch-import/`，`import_kind=candidate` | 对 discover 结果显式纳管来源；只提交 source path/SHA、来源作用域版本/hash 和 candidate 类型 | 200；逐项创建或复用不可变 candidate provenance。不会创建 WorkflowTemplateVersion，不要求目标模型、projector 或目标执行 Deployment，不提交 GPU |
| `POST /api/v1/model-deployments/{id}/saved-workflows/preview/` | 基于精确 candidate 提交 source、SHA、来源作用域版本/hash、可选唯一 canvas primary 与可选目标 profile | 200；未选主输出只返回选择信息；选定主输出后创建/复用不可变合同草稿，并返回 draft ID/revision、投影 SHA、projector 身份、参数/素材/模型依赖、blockers、deferred Live Validate 要求与 actionability。该调用不是纯只读 |
| `GET /api/v1/comfy-workflow-contract-drafts/`、`GET /api/v1/comfy-workflow-contract-drafts/{id}/` | staff 审计精确 candidate 的合同草稿 | 200；只读返回 `projection_summary`（节点/依赖数量、结构摘要、blockers、Live Validate 前置项和恢复动作）、projection schema/size/SHA、主输出、模型、projector 版本、admission tier 和 actionability；不返回可执行图正文，也不提供 PATCH/DELETE |
| `POST /api/v1/model-deployments/{id}/saved-workflows/attest-model-artifacts/` | 使用 discover 返回的来源作用域与当前转换审阅事实，为同 Provider draft profile 建立模型证明 | 202 + `Location`；创建或复用 RuntimeHostProbeAttempt，不提交 GPU prompt |
| `POST /api/v1/model-deployments/{id}/saved-workflows/batch-import/`，`import_kind=template_version` | 提交精确 contract draft、模型版本、主输出、四类草稿审阅 hash 及新/既有模板目标 | 200；只校验并消费冻结 draft，创建/复用 needs_review WorkflowTemplateVersion 和正式 provenance；不重新读取本地源文件或 `object_info`，不接收具体参数/Asset |
| `GET/POST /api/v1/workflow-projectors/`、`GET/PATCH/PUT /api/v1/workflow-projectors/{id}/` | superuser 管理稳定 projector family | 200/201；key 创建后不可修改；停用 family 只影响新投影，不改写历史草稿或版本 |
| `GET/POST /api/v1/workflow-projector-versions/`、`GET/PATCH/PUT /api/v1/workflow-projector-versions/{id}/` | superuser 管理 projector 草稿版本 | 200/201；仅 draft 可修改，服务端校验 definition 并计算 schema/size/SHA；published/retired 不可覆盖 |
| `GET /api/v1/workflow-projector-versions/options/` | superuser 读取 Projector 结构化编辑合同 | 200；返回 definition/editor schema 版本、表单分区与字段帮助、枚举、数量上限，以及每个 Engine 的说明、唯一 adapter contract、projection kind、锁定字段、专项字段和 `creation_template`。后者包含 driver/operation/output_kind 与不含业务规则的 definition 空白骨架，独立于已有版本；不是通过校验的合同。无副作用；权限不足返回 403。客户端遇到未知 Schema/Engine、缺失骨架或身份不一致时失败关闭，不能登记或自由拼接任意 engine/adapter key |
| `POST /api/v1/workflow-projector-versions/recommendation/` | superuser 基于任一已纳管 candidate、来源作用域/SHA、唯一 canvas 主输出和人工确认的 output kind 请求 Projector 建议；候选即使已由 fallback 匹配或已提升为工作流版本也可使用 | 200；重新校验不可变 candidate，读取实时 `object_info`，返回 `persistence=unsaved_draft` 的 `generic.fixed/v1` 或经结构证据确认的 `generic.references/v1` definition、节点/输入安全目录、证据 SHA 与人工复核警告；不创建 family/version，不发布、不提交 GPU，也不返回 source path、提示词、素材名或模型文件字面值 |
| `POST /api/v1/workflow-projector-versions/{id}/draft-next/`、`publish/`、`retire/` | superuser 创建后继草稿并管理不可变生命周期 | 200/201；发布后成为该 family 的 current published 版本；后继版本不会改写历史 draft/WorkflowTemplateVersion |
| `POST /api/v1/workflow-projector-versions/{id}/discard/` | superuser 放弃误建或不再采用的 Projector 草稿 | 204；只允许删除该 family 的最高、未发布、非 current 且未被合同草稿引用的版本，并记录 `discard` 生命周期审计；published/current/retired 和历史引用保持不可变。非最高或已有引用返回 409 |
| `GET /api/v1/assets/workflow-smoke-candidates/` | 工作流版本验收页读取每次 fixture 可选的受管 Asset，包括普通生产列表刻意隐藏的 `fixture_scope=workflow_smoke` 技术素材 | 200 分页；返回资格和非敏感原因，不改变版本 |
| `POST /api/v1/workflow-template-versions/{id}/validate/` | body 为 `deployment_id`，校验实时结构、模型和主输出 | 200 时更新 validation evidence，并使旧 smoke evidence 失效；模型主机证明不可用时返回 400 稳定原因码与 `details.recovery={state,title,summary,steps,actions}`，页面据当前探测状态引导等待、主机探测、模型文件核对或重新校验；不要求具体 Asset |
| `POST /api/v1/workflow-template-versions/{id}/smoke-test/` | 仅接受 `validated + validation passed` 版本；body 为 `deployment_id` 和 `fixture={schema_version:2,values,assets[],references?}` | 202 + `Location`；冻结参数值、动态引用、Asset facts 与 fixture hash；生命周期或 fixture 不满足时返回 400；同版本已有不同 fixture 的 active attempt 时返回 409；两种失败都不创建新 attempt |
| `POST /api/v1/workflow-smoke-attempts/{id}/resolve-uncertain/` | 仅 staff；body 为三种终态 `outcome`、核验说明和可选只写 `provider_request_id` | 201 首次追加不可变结论；200 完全相同重放；冲突为 409 |
| `POST /api/v1/workflow-template-versions/{id}/publish/` | 复验 validation/smoke/fixture/compiled graph/target 证据 | 200；发布不可变当前版本 |

discover 只读各工作流库，不返回 canvas 正文、不写候选/版本、不提交 prompt；实际项数、字节与同步时间边界由当前安全配置控制，页面只使用响应中的 `effective_limits`。candidate 纳管与 `execution_snapshot`/`ignored` 使用同一 batch 外壳但语义互斥；candidate 保存精确原始文档和来源 hash，永远没有 `workflow_version`。preview 必须带 discover 的 `expected_sha256`、`expected_source_scope_schema_version` 与 `expected_source_scope_sha256`，并且服务端必须找到完全一致的 candidate，否则返回 `workflow_candidate_required`。来源作用域漂移返回 `source_scope_changed`；candidate 自身不被覆盖，重新扫描后纳管新的 revision。

明确选择一个服务端候选主输出后，preview 重读 `/object_info`，完成 UI canvas → API graph、子图展开、可达剪枝和结构审计，并持久化草稿。`configurable_parameters` 只用于审阅 parameter semantic、type/range/options、selection group 和精确 API node/input target；具体值不进入草稿。普通固定素材槽 required 为 `1..1`、optional 为 `0..1`；动态 topology 只来自冻结的 published projector。没有唯一 projector、模型证明或目标 Deployment 时，草稿可保持 `candidate_only` 并记录 blocker/Live Validate 要求；只有 `ready_for_executable_import`、无结构 blocker、精确 projector/model/primary 与四类 SHA 全部一致时才允许提升。

preview 发现目标 draft model profile 缺少模型声明时，可调用 `attest-model-artifacts`。该 action 只接受当前 source、两个转换审阅 hash、唯一 canvas primary 和同 Provider draft profile；模型 filename、size 和 SHA 不接受客户端提交。来源必须是本机 loopback ComfyUI 且 RuntimeHost 的 Comfy scope 都是本地。服务端重算完整模型闭包，整批成功后才原子写入 artifact size/SHA/verified snapshot；失败只保留诊断 snapshot。调用方按 `Location` 轮询，不能把部分证明当作可用模型。

正式提升的 batch 不重新扫描目录、重读本地 JSON 或再次请求 `object_info`。服务端锁定并校验 contract draft 的 projection schema/size/SHA、candidate 身份、主输出、模型版本、projector version/definition SHA/engine identity 和四类审阅 hash，再建立版本；草稿不匹配返回 `workflow_contract_draft_mismatch`，仍有阻断项返回 `workflow_contract_blocked`，且零写入。具体参数值和 Asset ID 不属于提升请求。响应可以是 `complete/partial/failed`，HTTP 200 仍须检查 `failed_count` 和逐项 `code`。

`WorkflowProjectorVersion` 是独立版本化领域对象，不是普通 ConfigRevision JSON。definition 描述 driver/operation/output、节点角色与关键连接、可选节点、参数策略、模型 selector 角色、素材槽/动态引用及 adapter；代码只注册受审 engine、adapter 与安全原语。新投影只选择 active family 的 current published 版本，并把 version ID/number、definition SHA 和 engine identity 冻结进草稿与工作流输入合同。发布 projector 后继版本不会重解释历史候选、草稿或 WorkflowTemplateVersion；要采用新规则，可基于同一不可变 candidate 生成新草稿并提升新版本，只有来源字节变化时才纳管新的 candidate revision。未发布的最高版本如果尚无合同草稿引用，可通过受控 `discard` 动作放弃；这不是删除历史版本。

通用动态素材的 options 增加 `definition_editor.choices.asset_group_wirings`（`ordered_ports/reduce`）；新引擎的 `projection_fields` 使用 `asset_group_list` 控件，固定 compiler/output/dynamic 为 `generic.comfy/v2`、`comfy.primary/v1`、`generic.asset_groups/v1`。`projection.config.groups` 包含 key、source_role、branch_roles、output_role/output_index、target_roles、input_names、wiring、reducer_role/reducer_inputs/reducer_output_index、minimum_items/maximum_items、conditioning_mode/model_input；字段必须完整，未知字段/算法、越界数量或不兼容接线均拒绝。数量为模型来源时本地 min/max 必须为 null。

参数范围的正式管理入口是 **系统设置 → 工作流 → Projector 草稿 → 参数与素材 → 参数策略 → 运行参数范围与默认值**。`parameter_policies[]` 保持原有五个必需字段，可增加 `constraints` 对象，允许 `min`、`max`、`default`、`multiple_of`、`alignment_modulus`、`alignment_remainder`。仅 `runtime/object_info` 策略可声明非空范围，固定值和来源锁定不因此解锁。省略或空对象继承节点／引擎合同；填写后只允许收窄，不能放宽实时节点边界或既有对齐规则。小数步长以十进制从 0 起算，例如 LoRA `min=0,max=1,multiple_of=0.05,default=0.3`；整数帧数可声明 `alignment_modulus=17,alignment_remainder=5`。生成合同会验证类型、交集和默认值，默认值不合法时返回 `workflow_projector_parameter_range_invalid` blocker，不截断或替换成成功值。最终范围写入 `input_contract.parameters`，素材生成、Smoke 和生产编译复用同一数值校验；分类参考引擎的 `frame_policy` 在生成时从同一帧数定义派生。已发布版本不原地改写，采用新范围须发布 Projector 后重新生成、验收工作流。

分类参考使用 `generic.classified/v1`（compiler `generic.comfy/v3`）。Projector 草稿 API 的 `definition.projection.config` 配置 capability、conditioning_mode、target_role、prompt_template_keys、static_semantics 与 dynamic_groups；四类组为 images/videos/video_audios/audios。每组选择 mode_input，声明按序 input_names 或 prefix（二选一），数量由对应 asset_slots.contract_source 确定。model_profile 来源不填写本地 min/max；workflow 来源显式配置 minimum_items/maximum_items，不能超出模型合同和实时节点容量。页面可读取模型版本的能力与模式辅助填写，生成合同时再冻结精确版本。端口必须唯一且不能覆盖静态输入，配套音轨按所属视频序号接入；Live Validate 探测最小、最大及稀疏音轨情况。不满足约束会产生 blocker，不能提升或提交 GPU。

三个 Generic 引擎共用参数治理：fixed 生成 default+const，运行解析对所有类型拒绝不同值；必选单值参数通过 fixture Schema 的 options 表示，前端只读展示。source_locked 表示来源默认优先，不是禁止显式覆盖。object_info.step 不推导从 0 起算的整数格点；应使用显式 multiple_of 或模数/余数配置。已发布旧合同不会自动补写或改变。

数量来源选项 `contract_source=workflow` 的页面名称为 **Projector 中配置**，实际读取 `projection.config.groups[].minimum_items/maximum_items`，随后校验真实接线端口和节点能力；它不是自动推测画布中可任选的素材数。`contract_source=model_profile` 名称仍为 **模型版本合同**，按 `conditioning_mode/model_input` 读取所选模型版本的 `specification.conditioning_contract.modes[].inputs`，不借用其他模型的范围。

模型数量合同的正式管理入口是 **系统设置 → 模型 → 草稿版本（或新建下一版本）→ 模型素材数量合同**。`GET /model-profile-versions/options/` 新增只读 `conditioning_contract` 编辑目录：`schema_version/adapter_key/input_kinds/input_slots/maximum_items/maximum_modes/identifier_pattern/mode_template`；权限沿用模型注册表 options，缺失目录时表单失败关闭。表单自动补齐系统字段和零数量槽位，也可通过已有 `GET /model-profile-versions/{id}/` 读取并明确复制完整素材合同；复制不修改其他规格，不自动推测模型能力。模式形态、首尾帧／参考图片／视频／音频的最少和最多数量通过结构化字段编辑，`0..0` 不支持、`N..N` 固定、`min<max` 动态；图片总数仍独立校验，不能超过各图片槽上限之和。数量表单保留已有复杂 `dynamic_reference_topology`；视频和音频的用途、预处理、配对规则仍在同一完整规格中复核，不得仅提高数量绕过其约束。保存沿用模型版本 POST/PATCH 的 `specification`，发布仍走既有完整合同校验；非法基数、未声明拓扑或发布态修改按原接口返回 400，错误不得伪装为已保存。

生产配方 route 冻结的是明确的执行目标、版本合同及选择规则，不是把区间直接传给 ComfyUI。每个生成任务在提交前解析出具体值并校验范围、冻结执行快照；种子可以按任务／变体确定，帧数可以按已冻结规则由镜头时长计算。因此“每次执行有确定参数”不等于“整条路由下所有镜头必须手填同一组常数”。

新 Workflow 的 `smoke_fixture_schema` / `workflow_input_schema` 在 schema_version=2 下投影 `asset_groups=[{key,media_kind,min_items,max_items,semantic_keys,wiring}]`；无分组时为空数组。semantic_keys 是现有 asset_slots 的有序子集，运行请求不接受图、分组或节点描述，仍只提交 `values` 与 `assets=[{semantic_key,asset_id}]`。组内必须选择连续前缀并满足范围，未知参数、缺失必填项、越权 Asset 和类型/摘要漂移按现有校验返回 400/权限错误，不执行 GPU。固定预处理参数不投影为可编辑值。实际 GPU 验收独立于推荐、草稿及 Live Validate，必须逐新 Workflow 版本执行真实 Smoke。

正式页面可对任一已纳管且已确定唯一主输出的 candidate 调用 recommendation action，包括存在 `workflow_projector_*` 阻断、已由 Generic fallback 自动匹配或已经提升为工作流版本的候选。它把该 candidate 的可达节点精确数量、主输出映射、唯一节点角色、可证明连接、受控模型 selector、参数目标和素材槽填入**本地未保存表单**。单份候选不能证明 optional node，也不能替管理员决定图族长期 key/name、业务产物语义、关键 discriminator 或发布优先级；响应固定 `requires_human_confirmation=true`，任何保存仍只创建 draft，发布继续执行完整冲突校验。已发布专用方案不会改写既有工作流；管理员必须从同一 candidate 显式生成后继合同草稿，并提升到原模板的新版本。编辑器中的“执行引擎与接线”展示 compiler/output/dynamic Adapter 与 projection kind：它们说明平台用哪段已审核代码编译合同、取回产物和处理动态输入，不是 ComfyUI 工作流业务字段；`generic.fixed` 无额外 projection 配置是正常状态。

上述管理 API 仅 staff 可读取候选与合同草稿；projector family/version 的写入与生命周期动作仅 superuser。稳定错误包括 `workflow_candidate_required`、`workflow_contract_draft_mismatch`、`workflow_contract_blocked`、`workflow_projector_catalog_unavailable`、`workflow_projector_ambiguous`、`source_changed` 与 `source_scope_changed`；客户端应展示服务端 `detail/blockers/recovery_actions`，不得把失败降级为未匹配或空结果。详细页面顺序、错误恢复和人工验收见 [ComfyUI 已保存工作流导入、验证与发布手册](comfyui-saved-workflow-validation.md)。

WorkflowTemplateVersion 详情返回 v2 `smoke_fixture_schema={available,reason_code,parameters,asset_slots,dynamic_references,dynamic_reference_modes}`。即使版本合同无效，详情也保持同一个 v2 响应形状：`available=false`、`reason_code=workflow_fixture_contract_invalid`、两个清单为空、`dynamic_references=null`、`dynamic_reference_modes={}`；客户端据此显示失败关闭状态，不得回退旧 Schema 或默认输入。具体参数和 Asset 只由工作流版本验收页按该 Schema 组装每次 fixture；required 槽各绑定一个合格 Asset，optional 槽可省略，无槽时 `assets=[]`。动态媒体仍使用 `references={pictures,videos,audios}`；只有一个 conditioning mode 时 `dynamic_references` 返回该模式投影，多个模式时客户端先通过 `values.CONDITIONING_MODE` 选择 `dynamic_reference_modes[mode]`。图片、视频、音频的数量，视频格式、时长、帧率、用途、互斥与音轨配对，以及宽高、帧数和 steps 都必须读取当前 WorkflowTemplateVersion 的 fixture schema、条件输入合同与参数 Schema，不存在 API 级 H3 默认值。一次版本化验收 attempt 中冻结的 0.2MP、帧率或 steps 只证明该精确 workflow/fixture/target，不得套用到新版本。烟测素材候选只调用 staff 专用的 `assets/workflow-smoke-candidates` 分页 action；素材生成页面则从普通 `GET /api/v1/assets/` 读取当前 owner/global 受管素材，两条路径都由服务端复核权限、路径、MIME、大小与 SHA-256，且不公开本地路径。WorkflowSmokeAttempt 详情只返回 fixture schema version、规范化大小、hash 和 Asset binding facts，不公开原始 fixture 参数值。普通 fixture 校验失败返回 400 `invalid`，原因位于 `details.fixture`，且不会创建 attempt；同 workflow/deployment 的不同 active fixture 返回 409 `workflow_smoke_fixture_conflict`。

已保存工作流的 `comfyui-workflow` action 只从明确绑定的 ComfyUI deployment 读取 `object_info`，并复用正式 API graph → UI canvas 转换器生成可导入 JSON；它不会 submit、poll 或创建 smoke/生产任务。validated、published 与 retired 版本只能使用持久化 `validated_deployment`，请求中的 `deployment_id` 如与证据不一致会被拒绝；没有 `validated_deployment` 的 draft 必须传入显式 `deployment_id`，且该部署须为 active、ComfyUI 驱动并与工作流已有 Provider/模型版本绑定兼容，禁止回退全局或默认 ComfyUI。停用部署、非 published 模型、绑定不一致或 validation target hash 漂移都会在网络访问前失败。元数据响应包含实际 `deployment_id`、`deployment_name`、`deployment_source`，并固定声明 `direct_open_supported=false`、`direct_open_url=null`、`open_mode=download_import`；`?download=true&deployment_id={uuid}` 返回附件。浏览器客户端必须明确提示“下载后导入”，不得伪造跨页面直接打开能力。读取 `object_info` 或转换失败时返回显式 4xx/503，且不回传远端原始诊断正文。

工作流 smoke 创建返回 `202 WorkflowSmokeAttempt`，客户端轮询 `queued/submitting/running/cancel_requested` 到 `succeeded/failed/cancelled/uncertain`。fixture hash 相同的 active 请求复用原 attempt；不同 fixture 返回冲突。`submission_uncertain`/`remote_state_uncertain` 表示提交边界可能已被远端接受，未消歧时任何 fixture 都禁止重放。staff 只能通过 attempt 的 `resolve-uncertain` 追加 `confirmed_not_submitted`、`confirmed_remote_failed` 或 `confirmed_remote_succeeded`；原始 request id 只写不回显，并且远端终态必须与该 attempt 的稳定 submission token 一致。人工结论不修改旧 attempt、不生成 passed evidence；即使确认远端成功，也仍需新 attempt 重新收集并校验后才可发布。ComfyUI 明确失败且 history 含标准 `execution_error` 时，`error_message` 会保存服务端脱敏后的失败节点、异常类别、具体原因与处理建议；节点类型以本次提交的冻结 graph 为准。原始 `status.messages`、traceback、当前输入输出和 request id 不进入 API；结构未知时明确提示到 ComfyUI History 继续定位。OOM 仍使用稳定分类 `provider_smoke_failed`，但可从具体错误说明识别显存原因。发布只接受重新读取后与当前版本、成功 attempt、fixture、compiled graph 和 validated target 完全一致的证据。

所有 Deployment 烟测也统一返回 `202 ModelDeploymentSmokeAttempt`：文本任务按 `queued → submitting → succeeded/failed/uncertain` 推进，async media 使用 `queued/submitting/submitted/cancel_requested`。它们的 `resolve-smoke-uncertain` 只适用于 Deployment attempt，不能用于 WorkflowSmokeAttempt。

上述接口必须通过候选准入、草稿持久化、projector 生命周期、promotion 不重读来源及 Live Validate/Smoke 门禁的仓库契约测试；本文档变更未执行真实长 GPU smoke。目标环境仍须逐版本记录 attempt、fixture hash、真实提交、输出字节校验、耗时及 OOM/uncertain 核对，不能以自动测试或 validate 替代。

### PromptProfile 配置面

模型页“提示词策略关联”复用 `PromptProfileBinding`，不新增 Deployment 到策略的独立关系。`GET /api/v1/prompt-profile-bindings/` 支持既有 `model_family` 与 `model_profile_version` 等筛选并保留分页；响应增加只读 `prompt_profile_name`、`prompt_profile_version_number`、`prompt_profile_status`，与既有策略 ID/key/合同 SHA 一起展示精确策略版本。关联详情只按实际模型版本及其声明的 family 展示绑定记录，不把列表顺序当成本次运行选择结果。实际 operation/mode、版本发布状态与执行兼容性仍由服务端校验。

前端 `/settings?section=prompts&model_profile_version={id}` 使用现有模型 detail API 核对上下文，可附加 `prompt_profile_version` 定位策略及 `deployment` 保留返回实例。此入口新增 Binding 时固定精确模型版本；族级绑定在该上下文仅展示，需从全局提示词管理入口编辑。查询参数只提供导航范围，不替代服务端权限和写入校验。

- `/api/v1/prompt-field-definitions/`
- `GET /api/v1/prompt-field-definitions/options/`
- `/api/v1/prompt-profile-versions/`
- `/api/v1/prompt-profile-field-rules/`
- `/api/v1/prompt-profile-bindings/`
- `GET /api/v1/prompt-profile-versions/options/`
- `POST /api/v1/prompt-profile-versions/{id}/clone-successor/`
- `POST /api/v1/prompt-profile-versions/{id}/preview/`
- `POST /api/v1/prompt-profile-versions/{id}/publish/`
- `POST /api/v1/prompt-profile-versions/{id}/retire/`

这三个管理目录与 ProductionRecipe 共用[填写表单最小输入标准](form-input-standard.md)。Recipe/Profile 的首版号和后继版本号、Recipe 结构版本、PromptField 展示排序都不属于请求字段；服务端在共享注册表锁内分配。各 `options` 响应同时给出受控目录、系统默认和 `custom_entry_fields` 授权；客户端不得把缺失目录转换为空数组后继续自由输入。两个 `clone-successor` action 都要求精确来源 SHA-256，来源过期、已有草稿或正文与持久摘要不一致返回稳定 `409` 且零写入。

PromptProfile 是唯一的类型化提示词合同，不接受可执行模板。每条字段规则同时回答两个业务问题：“字段内容如何获得”和“最终提示词如何排版”。最终排版由 `source_selector`、可选 `projection_primitive`、`channel`、`position`、`label_style`、`separator`、`required`、`omit_if_empty` 与 `max_chars` 决定；字段内容规则使用下列通用字段：

`GET /api/v1/prompt-profile-versions/options/` 的 `effective_limits` 返回字段文本、validators 和 context selectors 的当前安全上限。字段规则 API 自身不再复制发布时的旧静态 maximum；写入与运行时统一由相同 active security boundary 校验，因此管理员调高后不会继续被旧 OpenAPI 常量拦截，调低后立即生效。

| API 字段 | 类型与范围 | 含义 |
| --- | --- | --- |
| `generation_mode` | `server_locked`、`model_generated`、`translate_only`、`passthrough` | 谁负责取得或生成字段。新建规则必须显式提交，不能靠缺省猜测；当前字段级 `model_generated` / `translate_only` 执行器只接入 `image.generate` 与 `video.generate` ProductionRun，其他 operation 不能保存这两种方式。 |
| `generation_source_selector` | `model_generated` / `translate_only`：必须取 `generation_source_selectors[].value`；`server_locked` / `passthrough`：必须等于本规则 `source_selector` | 生成目标字段时首先读取的主要业务事实，不是文件地址或开放点路径。当前写目录与生产字段事实采集共用同一服务端 registry；语法合法但目录未登记的值也会拒绝。 |
| `generation_context_selectors` | 最多 8 项的受控枚举数组；允许空数组 | 声明生成字段内容时除主要内容来源外还能参考哪些已确认事实。空数组表示只使用主要内容来源；相邻镜头和全剧范围必须显式选择。 |
| `source_selector` | 受控来源枚举 | `direct_v1` 读取通用事实；`declarative_v1` 的原语输出规则固定使用 `projection.output`。历史 H3 专属 selector 只用于读取冻结旧合同。 |
| `projection_primitive` | 空字符串或 `source_projection_primitives[].value` | 仅 `source_selector=projection.output` 时必填；声明代码中已注册的确定性原语。其他 source selector 必须为空。原语结果写入本规则所选 `field_definition.key`，因此业务字段映射不写死在代码中。 |
| `generation_required` | boolean | 字段内容是否必须生成成功；与最终拼装的 `required` 分开记录。 |
| `language_policy` | `inherit`、`preserve_source`、`any`、BCP 47 标签或 `bilingual:<tag>+<tag>` | 输出语言约束；`translate_only` 不能使用 `preserve_source` 或 `any`。 |
| `length_unit` | `none`、`characters`、`english_words`、`items` | 长度计量方式。`none` 要求上下限都为空；其他单位至少设置一个边界。 |
| `generation_min` / `generation_max` | null 或 1–30000 整数 | 生成长度边界；同时存在时 min 不得大于 max。 |
| `generation_validator_keys` | 最多 16 项的受控枚举数组 | 可组合 `non_empty`、`no_control_characters`、`no_unresolved_placeholders`、`single_line`、`valid_json`、`json_object`、`json_array`、`unique_items`，并以互斥的 `protected_tokens_preserve_count`（受保护内容保持来源次数）或 `protected_tokens_at_least_once`（受保护内容至少一次）声明引用计数策略。未显式选择计数策略时仍严格保持来源次数；未知、重复或组合冲突均拒绝。 |

Profile 级 `channel_limits` 是最终提示词通道合同，键只能使用受控 channel，值包含必填 `max_chars`（1–30000）与可选 `warn_at_chars`（1–`max_chars`）。未配置的通道表示该 Profile 不限制最终长度，不存在代码内隐藏默认值。装配超过 `max_chars` 以 `prompt_profile_channel_too_long` 阻断；达到预警阈值仍返回结果，并在 preview 的 `warnings` 与 `channel_audit` 中显式标记。

`GET /api/v1/prompt-profile-versions/options/` 返回已有 operation、模型族、模式、装配来源、通道、标签和分隔符建议，并返回 `generation_modes`、`generation_source_selectors`、`language_policies`、`length_units`、`generation_validators` 与 `generation_context_selectors`。

`generation_source_selectors` 是 `model_generated` / `translate_only` 当前写入的唯一主要内容来源目录，也是生产 prompt-normalization 实际可以物化的 35 项静态字段事实。每项包含稳定 `value`、业务 `label`、`scope_type`、分组标识与分组名、`description`（读取什么）、`availability`（什么时候可用）、`scope`（对白和镜头范围）、歧义/基数元数据、状态以及适用的 `generation_modes`。目录按项目事实、人物 Bible、场景 Bible、道具 Bible 与当前镜头分组；例如 `style.visual_prompt`、`character.visual_identity`、`scene.lighting`、`prop.continuity_state`、`shot.narrative`。页面必须直接使用该目录，不得维护副本、允许自由文本或把历史值临时混入 active options。人物或道具来源同时命中多个当前镜头关联实体时仍由生产门禁以歧义拒绝，不会自动拼接。目录合同不主动注入独立对白正文；若作者已经把台词写入所选自由文本字段，原文字段仍会被读取。动态 `shot.dialogue.<index>.delivery` 不属于当前 Profile 静态下拉目录。

每个 `generation_context_selectors` 候选包含 `value`、中英双语 `label` 和 `description`；这是额外上下文的唯一当前目录。页面不得维护第二套枚举或在目录缺失时静默回退。当前受控值只有 `project.core`、`project.style`、`target_shot.core`、`target_shot.dialogue_metadata`、`target_shot.dialogue_authored_text`、`target_shot.linked_bible`、`adjacent_shots.core`、`all_shots.core`。`target_shot.dialogue_metadata` 只提供当前镜头说话人、语气、起止时间和画外音标记，不含对白正文；只有显式选择 `target_shot.dialogue_authored_text` 才向字段生成模型提供已经确认的当前镜头对白原文。`adjacent_shots.core` 与 `all_shots.core` 都必须逐规则显式开启，且其核心事实仍不包含其他镜头的对白正文。

同一 options 响应的 `source_projection_keys` 是当前可写目录，只包含 `direct_v1` 与 `declarative_v1`。`source_projection_primitives` 是代码原语目录，每项返回 `value`、双语 `label`、`description`、`engine_key`、可消费的 `contribution_source_selectors` 和只读的 `rendering_template_keys`（可能使用的文案模板键）；页面据此显示可选项、字段模板对应关系并校验 contribution，不维护模型专属副本。模板对应目录不是执行轨迹，也不作为版本配置或模板默认值写入合同；实际模板选择仍由本次素材、音频与贡献条件决定。当前 `reference_context_ir_v1` 引擎注册六个原语：`reference_context.subject_definitions`、`reference_context.summary`、`reference_context.retention_analysis`、`reference_context.detailed_description`、`reference_context.overall_soundscape`、`reference_context.non_diegetic_music`。新增模型可以按自己的字段定义、数量、顺序、标签和通道复用其中任意原语；只有平台还不能执行的新确定性算法才需要增加代码原语。

策略详情的“内容生成与模板配置”读取所选版本已保存的 `source_projection_config`，展示模板标识、分隔符、列表前缀，并按需展开模板正文和其他原语配置。字段规则的“查看相关模板”通过上述目录定位同一只读配置弹框；未登记原语、未保存模板、留空和格式异常分别显示，不从新版本或教学示例补值。历史版本未保存显式模板时只展示其原始规则，不还原当时的内置文案。查看不会生成草稿、写入配置、调用模型或改变发布/绑定状态。

`operations` 是便于录入的建议目录，不是创建或执行授权：Profile 草稿可首次登记符合 `^[a-z][a-z0-9_.-]{1,99}$` 的新 operation key；发布时服务端必须找到内置受控 operation registry、同模型族已发布 ModelProfileVersion 的精确 `specification.capabilities`，或已注册 Provider driver 的精确 operation capability，否则以 `prompt_profile_operation_evidence_missing` 拒绝。Provider 页面可编辑的 `modalities` / `operations` 是说明、展示和人工审核用的运营标记；服务端不读取这两个字段做执行授权、能力兼容性校验或候选筛选，字段内容也不会加载脚本、函数或模板。非内置 operation 的 active Binding 必须选择精确 published ModelProfileVersion 且该版本必须声明相同 capability；模型族泛化 Binding 会被拒绝。即使 Profile 已发布，缺少 Deployment/Workflow、新鲜真实 Smoke 或对应运行实现时仍 fail closed，不会生成可执行 route。

语言选项是常用建议，API 仍接受符合上述安全语法的其他 BCP 47 标签。创建字段规则时 `generation_mode`、`generation_source_selector`、`generation_context_selectors`、`generation_required`、`language_policy`、`length_unit` 和 `generation_validator_keys` 七个合同成员必须显式提交；`projection.output` 规则还必须提交非空 `projection_primitive`。对于 model-generated/translate-only，服务端还会核对 generation source 是否属于同一 options registry；对于 server-locked/passthrough，则核对其与 `source_selector` 相等。context 与 validator 数组都可以为空，PATCH 可只提交需要修改的字段，但服务端会把旧值与新值合并后验证完整组合。

新建、克隆或经管理 API 编辑、发布的 Profile 使用合同 schema v6：它在 schema v5 的逐字段 `generation_context_selectors` 基础上，把每条规则的 `projection_primitive` 一并纳入冻结合同；字段内容规则、来源投影、最终提示词排版、Profile 级 `channel_limits` 与版本化 `source_projection_config` 全部进入规范 JSON、大小和 SHA-256，修改任何一项都会改变合同摘要。schema v1–v5 只用于验证已经冻结的历史审计事实，不作为新建规则的缺省入口。

`declarative_v1` 至少需要一条 `source_selector=projection.output` 字段规则，当前执行要求 `source_projection_config.schema_version=5`。`primitive_options` 的 engine key 必须与字段规则实际使用的原语引擎精确一致；`reference_context_ir_v1.rendering` 明确保存 `templates`、`separator`、`list_prefix`、`prompt_template_key`、`reference_rules`、`task_separator` 和 `unreferenced_task`。模板字段和允许的占位符从 PromptProfile options 的 `reference_rendering_fields` 读取，编辑器提供文本框和占位符按钮；只支持简单的 `{字段名}` 插值，禁止表达式、属性访问、索引、格式指令、函数和脚本。参考声明、逐镜描述、对白、风格开场、空素材说明及音频关闭说明都来自所选版本配置，不读取 H3 内置文案。素材标签来自实际绑定，模式及数量来自冻结模型合同，视频帧数来自冻结 Workflow frame policy；缺少合同会拒绝编译，不按模型名补默认值。已审核逐镜贡献使用 `reviewed_shot` 模板，贡献或对白缺少对应占位符时拒绝，不静默丢弃。可选 `quality_advisories` 只产生建议，不放宽结构和身份校验。`direct_v1` 不接受 `projection.output`、原语或 contribution。

`reference_rules` 是有界列表，每项精确包含文本 `category`、`purpose`、`task`、`relationship`、`usage`；类别与用途组合不可重复，空用途只匹配未声明用途，并非通配。任务名称、保留方式与使用说明由此配置决定；未知组合拒绝执行，不根据模型名推测。普通素材及配套音频都进入声明、保留关系、用途摘要与相应镜头；配套音频使用自己的用途/说明和父视频镜头范围，不继承视频主体。`task_types` 按实际用途规则稳定去重并由 `task_separator` 连接；无素材时采用显式 `unreferenced_task`。`reference_usage` 模板将实际标签和使用说明同时提供给摘要与逐镜模板，审核后的模型段落不会取代这些确定性关系。首镜头使用 `shot_opening`，后续镜头使用 `shot_transition`；`timecode` 是此前镜头有效时长累计的 `MM:SS.mmm`，具体标记文案仍随版本冻结。配置表单编辑后需保存草稿、复核与发布，不原地修改已发布内容。当前闭合 schema 由来源配置校验器负责结构限制，文本叶子继续接受规范化安全校验；不为该配置放宽任意上下文的嵌套限制。

提示词编译器版本为 `11`，旧产物显示为需重新编译，不会伪装为当前结果。生产语言门禁仅依据当前待改写字段、硬阻断字段及明确配置的门禁模式；历史 `optimizer.status=optimized` 不再豁免。新结果不再输出旧优化器状态，分镜页不再展示已退役优化器；改写统一走 `prompt.normalize` 审核。主体命名复用项目术语表和稳定身份标识；保存字段再次作为运行级声音描述传入时使用精确对应的已确认译文，新增描述仍受语言校验。

`reference_rendering_fields` 同时返回只读教学元数据：`description`（套用场景）、`assembly`（原语与字段去向）、`requirement`（必填/条件必需/留空含义）、`placeholder_help`（与允许占位符同键的中文名称、来源说明及虚构 `example` 值），以及 `example.template/result`。示例结果由正式单次标量替换函数生成，仅用于说明语法；不会写入 Profile、合同 SHA、生产默认值或任务，不代表当前用户模板的预览，也不代表验收通过。编辑器显示中文插入按钮，支持光标处插入；说明与示例按需展开。缺少帮助目录时明确提示更新服务并重新读取，不回退到前端内置目录。实际值为空时不自动隐藏周围的文字；双花括号用于输出字面花括号，替换值不递归展开。字段排版与通道仍通过已有最终提示词预览检查；该预览使用示例字段值，真实参考模板替换需结合实际项目和镜头验收。

提示词规范化合同校验、冻结批次规划以及单次生产创建/推进/审批在操作内复用现有安全边界解析快照，避免对每个嵌套文本重复读取整份配置。该快照不跨操作或外部执行任务保留；下一次校验重新解析当前配置，解析失败不会回退到先前结果，文本/深度/字段数量等校验仍逐项执行。

声明式历史 schema 1–4 仅保留不可变合同的读取和摘要核验，不再调用旧编译器执行；合法的 `direct_v1` 静态配置不受此声明式限制。升级草稿须补齐用途规则及新增模板，在提示词策略页面复核文案与字段规则、发布并显式选择新绑定，再重新验收；不修改已发布内容、自动激活绑定、转移烟测证据或以旧策略作为执行回退。

图片候选缩放的安全边界由统一 ConfigDefinition 管理：`security.workflow.image_resolution_min_dimension`、`max_dimension`、`max_pixels`、`max_candidates`（后面三项同一前缀）。在安全边界页面配置，options 返回实际生效限制，提交与执行使用同一校验。初始化值分别为 64、8192、16777216、32；不是运行期缺配置时的默认值。16 像素对齐、原生 Kontext 候选尺寸和 Lanczos/居中裁切属于已注册适配器的协议语义，不能通过提高安全边界改变。具体目标尺寸仍在 Projector 配置并冻结到工作流版本。

`classified_reference_context_ir_v1` 及 `request.h3_ref2va.*` selector 仅供已冻结的旧 Profile、ProductionRun 和审计事实读取，不再执行旧编译，但不出现在当前可写 projector 目录中，也不能用于新建、编辑后发布或克隆后继版本。新 H3 使用 `declarative_v1 + projection.output + projection_primitive`；其他需要同类确定性转换的模型也可按自己的字段规则复用这些原语。这样引擎原语在代码中受审，业务字段组合在页面和版本化数据库合同中维护。

`preview` 使用与生产相同的确定性装配器，返回 `channels` 中的实际最终提示词文本；页面不得用 JSON 或浏览器端第二套拼装逻辑代替。编辑草稿时可提交 `expected_base_contract_sha256` 与完整 `candidate`：候选包含 Profile 的模型族、operation、mode、来源投影设置、通道限制及完整 `field_rules` 快照。服务端在内存中校验并计算候选合同 SHA-256，不保存候选、示例输入或合同字段；基础合同已变化时返回 HTTP 409。未提交候选时按已保存合同预览，且同样不会刷新或写入数据库。

`PromptFieldDefinition.preview_example` 是服务端只读返回的通用预览示例。当前内置字段使用中性、无品牌的语义示例，未知或未来字段按 `value_type` 返回类型正确的通用值；它不读取 `default_value`，不进入 PromptProfile 合同或 SHA-256，也不会被 ProductionRun、字段生成或来源投影使用。页面只把它作为本次预览 `sources` 的初始值，用户修改或主动清空后以用户输入为准，并可显式恢复服务端示例。该字段不属于创建或修改 PromptFieldDefinition 的请求合同。

对 `declarative_v1`，`sources.projection.output.<field_definition.key>` 表示原语已经得到的示例字段内容。响应固定声明 `preview_scope=sample_final_assembly`，并用 `executed_stages` 明确 `field_generation=false`、是否运行了静态来源投影以及 `final_assembly=true`。因此该接口能真实反映未保存的字段顺序、标签、分隔符、通道、必填/空值与长度规则，但不会调用文本模型，也不会假装已经用真实项目/镜头执行参考上下文原语；原语参数仍需在真实上下文中验收。语义校验失败返回稳定错误，不由 preview 自行重试。`system_instruction` 只能使用 `definition.default`，项目或请求内容不得提升为系统指令。

只有 `review_state=ready` 且每条“字段内容 + 最终排版”规则组合完整的 draft 可发布；published/retired 版本及其字段合同不可原地修改。具体模型要求的字段名称、顺序、语言、长度和校验组合属于已发布 Profile 版本数据，不进入通用 API 的模型特例分支。Binding 只能引用 published Profile，并按 operation、model family、mode 和可选精确模型版本声明适用范围；priority 仅参与仍采用默认 resolver 的文本/预览路径排序，最高候选并列时该 resolver 必须显式失败。ProductionRun 的媒体/Bible 路径不使用 priority 自动选择，而是接收并复验显式 binding ID。发布、退役和 CRUD 均使用 staff 配置权限与生命周期审计，生产事实冻结 Profile 版本、Binding ID/key 和合同 SHA-256。

`script.generate`、`script.structure` 与 `prompt.normalize` 仍要求默认 resolver 得到唯一 active PromptProfile binding；缺失、并列或与实际模型版本/模式不一致时显式阻断。`image.generate`、每个 `video.generate` mode 与 Bible image 则要求至少有一个兼容 choice，并在 ProductionRun 创建时由用户显式提交 binding ID；缺失、额外、冲突或不兼容选择同样 fail-closed。两类路径都不会回退到通用模板；preview 也不能替代 published Profile、binding 或真实 deployment/workflow smoke。

#### 创建 PromptProfile 后继草稿

| 项目 | 合同 |
| --- | --- |
| Method / Path | `POST /api/v1/prompt-profile-versions/{id}/clone-successor/` |
| 权限与入口 | 登录且具有 staff 配置权限；API 接受 `direct_v1`/`declarative_v1` 的不可变来源，系统设置当前在已发布/已退役的 `declarative_v1` Profile 显示“创建后继草稿” |
| Body | `expected_source_contract_sha256`：必填 64 位小写 SHA-256；`name`：必填 1–160 字符 |
| 成功响应 | HTTP `201`，返回同一 `profile_key` 下按全部历史版本递增的新 draft；`review_state=needs_review`、合同 schema v6，并精确复制来源 Profile 的配置与全部字段规则 |
| 数据副作用 | 新增 `PromptProfileVersion`、全部字段规则和 `clone_successor` 生命周期审计，并计算新合同摘要；不修改来源版本、Binding、ProductionRun 或外部服务，不自动发布或激活 |
| 失败处理 | 来源 hash 过期、来源不是 published/retired、来源使用只读 legacy projector、名称无效或并发变化时返回显式 4xx，且不创建部分草稿 |

调用方必须先读取来源版本并把当时显示的 `contract_sha256` 原样提交；响应丢失后先按同一 `profile_key` 重新读取版本列表，不能无条件重复创建。该 action 不接受客户端指定版本号，也不把原 Binding 自动改指向新草稿。修改并预览后仍需人工把 `review_state` 设为 ready、发布，并创建或切换新的 Binding；历史 Binding 必须继续指向原不可变 Profile，保证旧 ProductionRun 可复验。

## prompt.normalize 与翻译审核

- `GET /api/v1/projects/{project_id}/capabilities/prompt-normalization/`
- `GET /api/v1/projects/{id}/translations/`；可选 query `episode`、`shot` 必须是 UUID，`status` 必须是正式译文状态枚举。非法 query 返回 `400`，不会进入 ORM 查询或返回服务端内部错误；同时提供 `shot` 时按镜头范围过滤，否则可按 `episode` 过滤。
- `POST /api/v1/projects/{id}/translations/translate/`
- `POST /api/v1/translations/{id}/retranslate/`
- `POST /api/v1/model-deployments/{id}/smoke-test/`，请求 `operation=prompt.normalize` 与显式 `prompt_profile_binding_id`

capability 返回动态 deployment UUID、provider/model 标识、readiness、原因码和 smoke 有效期，不返回 credential reference、密钥或 endpoint。项目默认值可以为空；客户端不得把列表第一项当成隐式默认。

翻译与重译请求必须显式提交 `model_deployment_id`。standalone 翻译请求没有 `allow_external_calls` 或 `budget_limit` 字段；当前仅 loopback 部署可用，外部部署以 `external_durable_attempt_required` 显式不可用。外部 deployment 的真实 smoke 使用上述持久单提交 attempt，ProductionRun 的 prompt normalization 也使用自己的 durable GenerationAttempt、外发授权与预算门禁；二者都不会回退到无持久边界的 standalone 调用。

模型版本只有在 `specification.capabilities` 精确声明 `prompt.normalize` 且 driver 实现该操作时才能发布。`operations`、`protocols` 等兼容字段不能替代正式声明。真实 smoke 与正式调用使用相同的 request id、受保护 token 和目标 English 校验，中文回显不能形成 passed snapshot。

## async REST 媒体契约

async REST executor 支持标准 T2I/T2V/T2A：

1. submit；
2. 记录外部 request；
3. poll；
4. 获取结果；
5. 归档为 managed artifact；
6. 绑定 attempt/provenance。

标准 schema 是封闭、文本输入契约。I2V/reference image 在没有 provider-specific upload contract 时返回 `provider_upload_contract_missing` 并阻断。调用方不得删除 reference 参数后重试为文本任务。

真实 smoke 必须包含真实 submission 证据；仅验证配置、构造请求或 mock response 不算成功。

## 受管产物

- `GET /api/v1/generated-artifacts/`
- `GET /api/v1/generated-artifacts/{id}/`

ProductionRun 的 artifact selection 动作只接受当前 lineage 中可验证的候选。远端 URL 不是 managed artifact；完成归档、媒体校验和 provenance 绑定后才可选入装配。

每个 GeneratedArtifact 的 `comparison_parameters` 在归档时由服务端从冻结 task/attempt/stage、route、workflow 与 configuration 事实的公开白名单生成。参数比较只使用该字段；`manifest_metadata` 的 task/output/object identity 与 hash 不属于生成参数。

## 配置、boot activation 与主机

- `/api/v1/config-definitions/`
- `GET /api/v1/config-definitions/options/`
- `GET /api/v1/config-definitions/{id}/resource-options/`
- `/api/v1/config-revisions/`
- `GET /api/v1/config-revisions/resolved/`
- `POST /api/v1/config-revisions/{id}/publish/`
- `POST /api/v1/config-revisions/{id}/activate/`
- `/api/v1/projects/{project_id}/config/`
- `/api/v1/runtime-hosts/`
- `POST /api/v1/runtime-hosts/{id}/probe/`
- `GET /api/v1/runtime-host-probe-attempts/`
- `GET /api/v1/runtime-host-probe-attempts/{id}/`
- `POST /api/v1/runtime-host-probe-attempts/{id}/cancel/`
- `GET /api/v1/runtime-hosts/{id}/latest-snapshot/`
- `POST /api/v1/runtime-hosts/{id}/status/`
- `GET /api/v1/runtime-hosts/{id}/readiness/`
- `POST /api/v1/runtime-hosts/{id}/recommend/`
- `GET /api/v1/runtime-hosts/node-metadata/`
- `GET /api/v1/recommendations/`
- `GET /api/v1/recommendations/{id}/`
- `POST /api/v1/recommendations/{id}/decide/`

`config-definitions/options` 是新增/编辑定义的目录事实源，返回配置类别、值类型、作用域、默认来源、域、单位、影响模块、系统默认和 `custom_entry_fields`。当前设置分区固定新定义的配置类别；配置键与类别创建后不可修改。定义响应额外返回只读 `has_values` 与 `mutable_fields`；已有 ConfigValue 时结构字段不可改变，客户端只发送相对打开快照的真实差异 PATCH。服务端按规范化后的真实值判断变化，相同结构字段不会制造伪冲突；未知目录项仍被拒绝。

resolved config 按 model default → workflow default → active system revision → active project revision → active run override 合并，并返回每项来源。设置页面和客户端只能提交 dirty keys；恢复继承使用 `unset_keys`，不得把合并后的 model/workflow 默认值整体复制成 system override。ProductionRun 创建时冻结不可变 snapshot；restart-required revision 必须在新 boot 显式 activate。

`ConfigDefinition.configuration_class=security_boundary` 表示仅由当前 active system Revision 提供的安全绝对上限。此类定义必须同时满足 `domain=security`、`allowed_scopes=system`、无默认来源、required、非 secret 且 active；缺失、分类错误、来源错误、hash 漂移或请求越界均失败关闭。platform administrator 可读取定义和值，但只有 super administrator 可以新增或修改安全定义、在 Revision 中显式 set/unset 安全值，以及发布或激活 `changes_security_boundaries=true` 的 Revision。Project、Run、Model、Workflow 和历史冻结快照均不能放宽当前安全边界。

安全值由业务提交、解析、下载或媒体处理边界实时解析。工作流 Smoke Fixture 的字节数、参数项和物理素材数，延迟生成抽帧时限，工作流输出前缀长度，以及提示词事实树的嵌套深度、字段数和键长度均使用 `security.*` 定义；媒体 FPS/时长、阶段快照、Bible 候选数、单实体 Bible 参考图数、单次 Bible 重生成实体数、Provider usage、PromptProfile 字段/validators/context selectors、Comfy timeout 与异步轮询也不得再叠加代码常量。队列建议、自动分段、任务健康阈值、AI 披露文本和显存建议档位属于可调业务配置，不在 `value_schema` 复制任意业务 maximum/maxItems/maxLength；其真正资源风险应由独立 `security.*` 边界负责。API 返回的 `changes_security_boundaries` 是服务端只读分类事实；缺失或未知时客户端必须失败关闭。安全边界错误使用 `security_boundary_definition_missing`、`security_boundary_definition_invalid`、`security_boundary_source_invalid`、`security_boundary_value_invalid` 或 `security_boundary_exceeded` 等稳定 code，不允许客户端改用本地默认值继续提交。

`ConfigDefinition.value_schema.resource_ref` 是服务端所有的类型化资源依赖合同。有权的 platform administrator 使用 `resource-options` 读取安全选项；响应每项只包含 `value`、安全 label、resource/capability、state、`reason_code` 和 `is_selectable`，不返回 provider endpoint、凭据或内部诊断正文。当前注册的组合是 `resource=model_deployment`、`capability=prompt.normalize`；未注册资源/能力或无效 schema 返回显式 4xx，不产生修改副作用。配置 revision 相对 `based_on_revision` 新增或真实变更非空引用时，会在创建、发布及需要重启的激活阶段重新验证资源存在、声明 capability、route readiness 和当前有效 operation smoke，所以客户端旧下拉结果不是授权。原样继承的引用仍接受类型、Schema、存储合同与整版 checksum 校验，但不会因为发布后的瞬时 Smoke 过期而阻断无关配置变更；实际消费者使用前继续执行 live route 门禁。历史空值保留为显式 `unconfigured`，并阻断依赖该配置的生产；不回退到环境变量、默认 deployment 或旧缓存。

ConfigRevision publish 会在锁内检查 `based_on_revision`，旧草稿返回 `stale_config_base`；同 scope 已有待激活版本时拒绝第二个 pending。普通 platform administrator 可以原样携带已有 SecretVersion，但增加、替换或移除秘密绑定必须由 super administrator 在 publish 与 activate 时再次授权。

RuntimeHost 由基础设施登记，collection 保持只读。`status` 只负责启用/停用；启用后先进入 unavailable，必须重新 probe 才能 active。仍有 active deployment 时不能停用，已禁用主机不能 probe，active deployment 也不能绑定非 active 主机。

`POST runtime-hosts/{id}/probe/` 只创建或复用该主机唯一 active 的 `RuntimeHostProbeAttempt`，返回 `202` 和 attempt detail 的 `Location`，不会在 HTTP 请求内执行模型文件 SHA。状态为 `queued → running → succeeded|failed`，取消时为 `cancel_requested → cancelled`；queued 可立即取消，running 在受控文件分块检查点停止。Settings 必须从 attempt collection 恢复任务、轮询详情并等待服务端终态，不能用客户端超时推断失败。platform-maintenance worker 会领取 queued/recovered attempt、持久化心跳、回收陈旧 claim，并通过 snapshot 的一对一 attempt 关系处理“快照已提交、终态未回写”的进程崩溃。attempt `succeeded` 只表示不可变快照已完整提交，不代表快照的 `probe_status` 或 readiness 为通过。同步 `probe_runtime_hosts` 只保留为受控运维命令，不是日常页面/API 旁路。

`latest-snapshot` 的 `model_availability` 会向已授权 platform administrator 返回 `observed_size_bytes`、完整 `observed_sha256` 与 `observation_source`，用于审核并创建下一版不可变 artifact 声明；响应不返回本机文件路径。name-only 远程库存仍不能作为受信 size/hash 证据。

recommend 返回可审核的建议/草案。decide 时服务端复验 host snapshot 是否有效、catalog/config hash 是否漂移，以及 deployment/workflow/候选是否仍兼容；陈旧推荐返回明确错误并要求重新 probe/recommend。接受建议不等于下载模型、部署模型、发布 workflow 或激活配置。

`runtime-hosts/node-metadata` 使用唯一分页 v2 合同：查询参数为必填 `deployment_id`、可选 `search`、`offset>=0` 与 `1<=limit<=200`；响应包含 `total_count`、`offset`、`limit`、`next_offset`、完整过滤排序目录的 `catalog_sha256` 和当前 `items`。客户端必须读取全部页，并在 total/hash 漂移、重复 class type、空页或 next offset 不前进时 fail closed；不再支持旧 `count/truncated` 合同。

## 错误语义

DRF 验证、权限、未找到和冲突响应统一使用 `{"error":{"status_code":400,"code":"invalid","details":...}}` envelope；调用方不得同时兼容另一套扁平 `code/detail` 形状。注册表删除预览/执行、其他业务 DELETE、publish 与 activate 操作的 400/403/404/409 以根 OpenAPI 声明为准。

需要操作员介入的状态应保持显式，例如：

- 内容或 Bible 未审核；
- workflow `needs_review`、validation/smoke 缺失；
- provider 未授权或 deployment 不可用；
- `provider_upload_contract_missing`；
- 成本币种/授权不满足；
- 外部响应不确定，需要 `resolve-uncertain`；
- artifact 归档或媒体校验失败；
- recommendation snapshot/catalog/config/candidate 已陈旧，需要重新 probe/recommend；
- episode master 或 ReleaseBundle 不完整；
- restart-required revision 尚未激活。

调用方应展示服务端错误码、关联 ID 与恢复动作，不应回退到旧路由、模拟结果或默认值。

`workflow_inputs` 包含未注册字段、缺少 required 参数/素材槽/`CONDITIONING_MODE`、用途或配对不符合所选模式，或引用 Asset 越权、停用、MIME/checksum/受管路径漂移时，创建与执行复验都在远端提交前 fail closed。客户端不得删除失败字段后降级提交，也不得以旧 smoke fixture、浏览器缓存或本地路径补值。

## 契约校验

```powershell
.\scripts\check.ps1
```

`check.ps1` 会把动态 schema 生成到系统临时文件，完成 Spectacular 校验后，将 UTF-8 BOM 和换行符规范化，再与根 `openapi.yaml` 做逐字节比较。任何漂移都会明确失败，且检查过程不会覆盖根契约；审阅 API 改动后必须显式重新生成并提交根契约。检查同时拒绝重新出现的 `backend/openapi.yaml`，避免形成第二份机器契约。

如需检查动态 schema，可在测试环境启动后比较 `GET /api/schema/` 与根契约的路由集合。不得把临时生成文件提交为第二份契约。
