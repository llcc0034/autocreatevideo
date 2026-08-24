# API

## 唯一契约

仓库根 [openapi.yaml](../openapi.yaml) 是唯一机器可读契约。运行时 `GET /api/schema/` 从同一契约动态返回 schema；文档、客户端和校验工具不得引用或维护其他副本。

路由前缀为 `/api/v1/`。本文只解释调用边界；字段、状态码、枚举和必填项以根 OpenAPI 为准。

## 认证与敏感信息

受保护端点使用 JWT。provider credential 通过 SecretBinding 关联；秘密创建与轮换只接受超级管理员的 write-only `secret_value`，读取仅返回 configured 与生命周期元数据，不能据此宣称远端调用 usable。客户端不得记录或显示原始密钥、密文、nonce 或 key id；本文也不提供密钥示例。

首次临时密码登录时，仅允许 `GET /api/v1/auth/me/` 与 `POST /api/v1/auth/password/change/`。改密成功后旧 access/refresh token 立即失效，客户端必须重新登录。

超级管理员专用治理端点包括 `/api/v1/access/users/`、用户的 `role`、`status`、`temporary-password` actions、`/api/v1/access/audit-events/`、`/api/v1/secret-bindings/`、`POST /api/v1/secret-bindings/{id}/rotate/`、`POST /api/v1/secret-bindings/{id}/status/` 与 `/api/v1/secret-audit-events/`。SecretBinding 只允许创建、轮换和启停，没有修改秘密正文或删除入口；SecretVersion 与密钥审计记录只读且不可变。普通成员和 platform administrator 对秘密治理端点均无权限；平台管理员仍可管理不含秘密材料的平台 catalog/config，并可只读查询 `/api/v1/platform-lifecycle-audit-events/`。

平台 catalog/config 生命周期写操作追加 `PlatformLifecycleAuditEvent`，记录操作者、动作、对象标识、稳定原因代码、变更前后 hash 与扁平安全摘要；不保存 secret、endpoint、path 或原始 JSON。客户端可用 `X-Audit-Reason-Code` 提交稳定原因代码；未提交时服务端写入对应动作的稳定系统原因代码。审计 collection 支持 actor、action、object_type、object_id 与 reason_code 过滤，不提供 update/delete。

对状态变更请求，应使用 OpenAPI 声明的幂等/条件字段。冲突、等待人工决策、外部响应不确定、预算不足和上传契约缺失会返回显式错误或阻断状态，不会伪装成功。

## ProductionRun：唯一新生产入口

- `GET/POST /api/v1/production-runs/`
- `GET /api/v1/production-runs/{id}/`
- `POST /api/v1/production-runs/{id}/approve/`
- `POST /api/v1/production-runs/{id}/select-route/`
- `POST /api/v1/production-runs/{id}/authorize-cost/`
- `POST /api/v1/production-runs/{id}/continue/`
- `POST /api/v1/production-runs/{id}/select-artifact/`
- `POST /api/v1/production-runs/{id}/retry/`
- `POST /api/v1/production-runs/{id}/resolve-uncertain/`
- `POST /api/v1/production-runs/{id}/cancel/`
- `GET /api/v1/generation-attempts/`

新生产必须从 collection POST 创建，并按服务端返回的当前 gate/action 推进。不得从 task、assembly、release 或已删除的旧生成路由发起旁路生产。

创建模式包括：

- existing project：不提交剧本输入，只允许已有 Episode 的项目进入下游生产；
- imported script：先通过 `ScriptSource → ScriptRevision → confirm` 固化用户导入原文，再用含 `script_input/confirmed_revision` 的 published recipe 创建运行；recipe 可在其后声明 `script_structure/ai → script_review/review(required)`，把非规范原文结构化为可解析的分集分镜候选；
- AI script：提交故事描述、集数、每集镜数和单集时长，进入 `script_generate/ai → script_review/review(required)`；确认后由服务端确定性生成 Bible，再进行 Bible 审核。

新建连续剧页面的“手动 / 导入剧本 / AI 描述”是上述正式 API 的编排入口，不是新的旁路。导入和 AI 模式必须创建真实 ProductionRun：导入先保存并确认原文，再提交 `script_revision_id`；AI 模式提交 `idea` 等生成参数。页面可按 published recipe 自动连续调用 `continue` 推进 auto 阶段，但遇到 required 审核、外部调用授权、预算、route 选择、产物选择或其他服务端 blocker 时必须暂停并展示恢复动作，不能把“项目已创建”显示为生产已完成。

`script.structure` 的输入始终绑定完整 confirmed revision，不允许截断后外发。只有与正式 renderer 输出逐字节一致的规范脚本可走确定性的 `no_submission` 审核候选；其他文本必须使用显式声明 `script.structure` 能力、受控 chat transport、当前有效 operation smoke 和唯一 active PromptProfile binding 的 route。远端结果必须覆盖全部来源行并通过严格 IR 校验后才能落为 preview revision；原 confirmed revision 在 `script_review` 批准前保持不变。

运行会固化 project、content、configuration、recipe、workflow、route 和 cost 相关快照。后续项目配置变化不回写既有 run。

`POST /api/v1/production-runs/{id}/approve/` 在批准 `script_review` 时可提交 `adopt_generated_metadata=true`。该开关只采用冻结候选中的 title/logline；默认 false，不会改写项目元数据。如果运行创建后对应项目字段已被人工修改，服务端保留并发编辑并在批准证据中记录 `concurrent_edit`，不会用旧候选覆盖。该字段不能用于拒绝决定或其他审核阶段；脚本正文仍以冻结 preview/候选及其 hash 为准。

创建请求可提交版本化的 `workflow_selection`。`mode=uniform` 使用 `route_index` 为全部选中镜头冻结同一条媒体 route；`mode=per_shot` 使用 `shot_routes[{shot_id,route_index}]` 完整覆盖所选 Episode 的每个 Shot。索引只能引用本次 published recipe 的 `media_generation` routes；服务端会拒绝重复、缺失、跨项目或未选 Episode 的 Shot，并重新校验 workflow 的 published/current/smoke 状态、输出类型、H3 mode、deployment 绑定，以及实际 ModelProfileVersion/operation/mode 是否只有一个可用的 active PromptProfile binding。AI 或导入剧本在 Shot 尚未物化时首版只允许 `uniform`。解析后的逐镜头安全投影及 hash 同时冻结在 ProductionRun 与媒体 StageRun 输入；PromptArtifact 另外冻结 route index、精确模型版本、PromptProfile 版本/绑定/checksum，重试不得切换到其他 route 或重新解析到新策略。

创建页通过 `GET /api/v1/production-recipes/{id}/media-route-options/` 读取所选配方当前的安全 route 目录。响应只包含工作流/部署标签、输出类型、H3 模式和结构化依赖缺口，不返回 Provider 或 deployment 配置；页面据此禁用未发布、非 current、缺少 smoke、绑定漂移、缺少唯一 PromptProfile binding 或与镜头类型不兼容的选项，创建接口仍会独立复验，不能把预检当作执行授权。新建连续剧的 AI/导入入口也使用该目录并只写入 `uniform` 选择；不含 `media_generation` 的纯脚本配方不提交 `workflow_selection`。

`generation-attempts` 是按运行所有者隔离的只读安全投影；管理员可在 ProductionRun 被 `uncertain_remote_response` 或 `actual_cost_reconciliation_required` 阻断时，把投影中的 `attempt_id` 提交给 `resolve-uncertain`。响应只包含 request/result/provider request id 的 hash、管理员可见的短 suffix 和状态/费用元数据，不返回原始 provider request id、请求 payload、响应正文或错误正文。人工消歧只追加不可变 resolution；`confirmed_remote_submitted` 仅恢复异步媒体轮询，其他结论也不得展示为生成或 smoke 通过。

## 脚本与内容事实

- `/api/v1/script-sources/`
- `/api/v1/script-revisions/`
- `POST /api/v1/script-revisions/{id}/confirm/`
- `/api/v1/projects/` 及其 OpenAPI 中声明的内容子资源

AI 生产阶段对脚本来源的写入受 ProductionRun lineage 和状态约束。客户端不能绕开审核把生成文本直接写成确认脚本。

## 只读诊断投影

- `GET /api/v1/tasks/`、`GET /api/v1/tasks/{id}/`
- `GET /api/v1/deferred-generation-requests/`、`GET /api/v1/deferred-generation-requests/{id}/`
- `GET /api/v1/assemblies/`、`GET /api/v1/assemblies/{id}/`
- `GET /api/v1/release-bundles/`、`GET /api/v1/release-bundles/{id}/`
- `GET /api/v1/release-bundles/{id}/verify/`

这些资源用于观察 lineage、attempt、source item、装配和交付验证；Task 与 Deferred request 没有 create/retry/cancel 写端点。创建、重试、取消与有序 fallback 必须使用所属 ProductionRun 的正式 action。ReleaseBundle verify 只执行交付诊断，不是生产创建入口。

## Provider、模型与工作流

平台管理端点包括：

- `/api/v1/production-recipes/`
- `/api/v1/production-recipes/{id}/`
- `POST /api/v1/production-recipes/{id}/publish/`
- `POST /api/v1/production-recipes/{id}/retire/`
- `/api/v1/provider-profiles/`
- `/api/v1/model-artifacts/`
- `POST /api/v1/model-artifacts/{id}/verify/`
- `/api/v1/model-profile-versions/`
- `POST /api/v1/model-profile-versions/{id}/publish/`
- `POST /api/v1/model-profile-versions/{id}/retire/`
- `/api/v1/model-deployments/`
- `POST /api/v1/model-deployments/{id}/probe/`
- `POST /api/v1/model-deployments/{id}/smoke-test/`
- `POST /api/v1/model-deployments/{id}/resolve-smoke-uncertain/`
- `GET /api/v1/model-deployments/{id}/evidence/`
- `GET /api/v1/model-deployment-smoke-attempts/`
- `GET /api/v1/model-deployment-smoke-attempts/{id}/`
- `POST /api/v1/model-deployment-smoke-attempts/{id}/cancel/`
- `/api/v1/workflow-templates/`
- `/api/v1/workflow-template-versions/`
- `GET /api/v1/workflow-template-versions/{id}/comfyui-workflow/`
- `POST /api/v1/workflow-template-versions/{id}/validate/`
- `POST /api/v1/workflow-template-versions/{id}/smoke-test/`
- `GET /api/v1/workflow-smoke-attempts/{id}/`
- `POST /api/v1/workflow-smoke-attempts/{id}/cancel/`
- `POST /api/v1/workflow-template-versions/{id}/publish/`
- `POST /api/v1/workflow-template-versions/{id}/retire/`

ProductionRecipe publish 会校验唯一正式 content 入口和完整有序的 content → storyboard → prompt → media → selection → assembly → delivery 闭包；缺阶段、顺序错误或媒体 route 未冻结都会拒绝发布。

注册表使用受状态约束的生命周期，而不是对审计事实开放任意 CRUD：Provider 可新增、编辑和停用，非系统且无引用的停用项才可删除；模型版本与配方只有 draft 可修改或删除，published 只能 retire；工作流版本不支持原地 PATCH，必须导入新版本。Deployment 删除前必须停用，且任何 probe/smoke、主机可用性、工作流、配方、推荐或运行证据都会以 409 阻止删除。

正确模型顺序为 Provider → draft ModelProfileVersion/声明 ModelArtifact → 核对发布 → 启用并 probe RuntimeHost 至 active → ModelDeployment → 再次 host probe 形成 availability → artifact verify → deployment probe/smoke。已发布模型的文件关联不可原地修改。`model-artifacts/{id}/verify/` 只接受未过期且合同 hash 有效的 HostCapabilitySnapshot，并把 snapshot、操作者和时间写为可追溯核验证据。

Deployment 真实烟测的受控 operation 包括 `image.text_to_image`、`video.text_to_video`、`prompt.normalize`、`script.generate` 与 `script.structure`。`script.generate` 只接受在 `specification.capabilities` 中精确声明该能力、包含完整 `series_concept` 策略且使用受控 chat transport 的已发布文本模型；烟测提交最小的一集一镜请求，并用正式剧本合同严格校验结果。`script.structure` 使用独立能力声明、输入上限、严格结构化合同与 operation smoke；`script.generate` 的证据不能替代它。外部调用必须先有可确定 pricing。五类 operation 都先把冻结请求和目标 hash 写入同一 `ModelDeploymentSmokeAttempt`，再由 worker 跨越一次提交边界；公开响应与最终 evidence 只返回请求、响应、结果和 request id 的 hash、费用元数据与错误分类，不返回冻结请求、提示词、模型正文或原始 provider request id。

工作流版本以 semantic bindings 对接 Comfy 图，不依赖固定节点号。bootstrap 版本为 `needs_review` 草案；validate、真实 smoke、核对 publish 和 retire 是不同动作。详情端点持久返回验证目标、合同 hash、validation evidence 与 smoke evidence；API 不会自动下载模型或把 catalog 条目标成已部署。

已保存工作流的 `comfyui-workflow` action 只从明确绑定的 ComfyUI deployment 读取 `object_info`，并复用正式 API graph → UI canvas 转换器生成可导入 JSON；它不会 submit、poll 或创建 smoke/生产任务。validated、published 与 retired 版本只能使用持久化 `validated_deployment`，请求中的 `deployment_id` 如与证据不一致会被拒绝；没有 `validated_deployment` 的 draft 必须传入显式 `deployment_id`，且该部署须为 active、ComfyUI 驱动并与工作流已有 Provider/模型版本绑定兼容，禁止回退全局或默认 ComfyUI。停用部署、非 published 模型、绑定不一致或 validation target hash 漂移都会在网络访问前失败。元数据响应包含实际 `deployment_id`、`deployment_name`、`deployment_source`，并固定声明 `direct_open_supported=false`、`direct_open_url=null`、`open_mode=download_import`；`?download=true&deployment_id={uuid}` 返回附件。浏览器客户端必须明确提示“下载后导入”，不得伪造跨页面直接打开能力。读取 `object_info` 或转换失败时返回显式 4xx/503，且不回传远端原始诊断正文。

工作流 smoke 创建返回 `202` 和持久化 `WorkflowSmokeAttempt`，客户端必须按 attempt 详情轮询 `queued/running/cancel_requested`，并支持恢复读取与显式取消；不得把创建请求当作同步长请求。只有服务端终态才能结束轮询，发布仍以 WorkflowTemplateVersion 上重新读取的持久化 smoke evidence 为准。所有 Deployment 烟测也统一返回 `202 ModelDeploymentSmokeAttempt`：文本任务按 `queued → submitting → succeeded/failed/uncertain` 推进，async media 继续使用 `queued/submitting/submitted/cancel_requested`；客户端从 collection 恢复未完成任务、轮询详情并读取 `succeeded/failed/cancelled/uncertain` 终态。重复创建同 deployment/operation 的 active attempt 返回原 attempt，不再次付费提交。排队态可复用 attempt cancel API；文本请求进入 `submitting` 后不能伪装撤回。worker 在单提交边界后丢失响应时只会转为 `uncertain`，不会自动重放；`resolve-smoke-uncertain` 对上述 operation 的 submitting/uncertain attempt 只追加管理员消歧证据，消歧后必须重新执行新烟测，不能沿用 resolution 伪装 passed snapshot。

### PromptProfile 配置面

- `/api/v1/prompt-field-definitions/`
- `/api/v1/prompt-profile-versions/`
- `/api/v1/prompt-profile-field-rules/`
- `/api/v1/prompt-profile-bindings/`
- `GET /api/v1/prompt-profile-versions/options/`
- `POST /api/v1/prompt-profile-versions/{id}/preview/`
- `POST /api/v1/prompt-profile-versions/{id}/publish/`
- `POST /api/v1/prompt-profile-versions/{id}/retire/`

PromptProfile 是字段字典、白名单来源选择器、输出通道和确定性装配顺序组成的数据合同，不接受可执行模板。管理 UI 应使用 `options` 返回的 operation、模型族、模式、来源、通道、标签和分隔符构建类型化表单；高级 JSON 不能成为唯一入口。`preview` 只接受 Profile 已声明的 source selector，并使用与生产相同的装配器，不调用模型。`system_instruction` 只能使用 `definition.default`，项目或请求内容不得提升为系统指令。

只有 `review_state=ready` 且规则完整的 draft 可发布；published/retired 版本及其字段合同不可原地修改。Binding 只能引用 published Profile，并可按 operation、model family、mode、精确模型版本和 priority 解析；最高候选并列时必须显式失败。发布、退役和 CRUD 均使用 staff 配置权限与生命周期审计，生产事实冻结 Profile 版本、Binding key 和合同 SHA-256。

生产 route 对 `script.generate`、`script.structure`、`prompt.normalize`、`image.generate` 和 `video.generate` 均要求解析到唯一 active PromptProfile binding；缺失、并列或与实际模型版本/模式不一致时，recipe 发布、运行创建或阶段提交会显式阻断，不会回退到通用模板。预览可用于核对装配结果，但不能替代 published Profile、binding 或真实 deployment/workflow smoke。

## prompt.normalize 与翻译审核

- `GET /api/v1/projects/{project_id}/capabilities/prompt-normalization/`
- `POST /api/v1/projects/{id}/translations/translate/`
- `POST /api/v1/translations/{id}/retranslate/`
- `POST /api/v1/model-deployments/{id}/smoke-test/`，请求 `operation=prompt.normalize`

capability 返回动态 deployment UUID、provider/model 标识、readiness、原因码和 smoke 有效期，不返回 credential reference、密钥或 endpoint。项目默认值可以为空；客户端不得把列表第一项当成隐式默认。

翻译与重译请求必须显式提交 `model_deployment_id`。standalone 翻译请求没有 `allow_external_calls` 或 `budget_limit` 字段；当前仅 loopback 部署可用，外部部署以 `external_durable_attempt_required` 显式不可用。外部 deployment 的真实 smoke 使用上述持久单提交 attempt，ProductionRun 的 prompt normalization 也使用自己的 durable GenerationAttempt、外发授权与预算门禁；二者都不会回退到无持久边界的 standalone 调用。

模型版本只有在 `specification.capabilities` 精确声明 `prompt.normalize` 且 driver 实现该操作时才能发布。`operations`、`protocols` 等兼容字段不能替代正式声明。真实 smoke 与正式调用使用相同的 request id、受保护 token 和目标 English 校验，中文回显不能形成 passed snapshot。

## async REST 媒体契约

async REST executor 支持标准 T2I/T2V：

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

resolved config 按 model default → workflow default → active system revision → active project revision → active run override 合并，并返回每项来源。设置页面和客户端只能提交 dirty keys；恢复继承使用 `unset_keys`，不得把合并后的 model/workflow 默认值整体复制成 system override。ProductionRun 创建时冻结不可变 snapshot；restart-required revision 必须在新 boot 显式 activate。

`ConfigDefinition.value_schema.resource_ref` 是服务端所有的类型化资源依赖合同。有权的 platform administrator 使用 `resource-options` 读取安全选项；响应每项只包含 `value`、安全 label、resource/capability、state、`reason_code` 和 `is_selectable`，不返回 provider endpoint、凭据或内部诊断正文。当前注册的组合是 `resource=model_deployment`、`capability=prompt.normalize`；未注册资源/能力或无效 schema 返回显式 4xx，不产生修改副作用。配置 revision 发布非空引用时会重新验证资源存在、声明 capability、route readiness 和当前有效 operation smoke，所以客户端旧下拉结果不是授权。历史空值保留为显式 `unconfigured`，并阻断依赖该配置的生产；不回退到环境变量、默认 deployment 或旧缓存。

ConfigRevision publish 会在锁内检查 `based_on_revision`，旧草稿返回 `stale_config_base`；同 scope 已有待激活版本时拒绝第二个 pending。普通 platform administrator 可以原样携带已有 SecretVersion，但增加、替换或移除秘密绑定必须由 super administrator 在 publish 与 activate 时再次授权。

RuntimeHost 由基础设施登记，collection 保持只读。`status` 只负责启用/停用；启用后先进入 unavailable，必须重新 probe 才能 active。仍有 active deployment 时不能停用，已禁用主机不能 probe，active deployment 也不能绑定非 active 主机。

`POST runtime-hosts/{id}/probe/` 只创建或复用该主机唯一 active 的 `RuntimeHostProbeAttempt`，返回 `202` 和 attempt detail 的 `Location`，不会在 HTTP 请求内执行模型文件 SHA。状态为 `queued → running → succeeded|failed`，取消时为 `cancel_requested → cancelled`；queued 可立即取消，running 在受控文件分块检查点停止。Settings 必须从 attempt collection 恢复任务、轮询详情并等待服务端终态，不能用客户端超时推断失败。platform-maintenance worker 会领取 queued/recovered attempt、持久化心跳、回收陈旧 claim，并通过 snapshot 的一对一 attempt 关系处理“快照已提交、终态未回写”的进程崩溃。attempt `succeeded` 只表示不可变快照已完整提交，不代表快照的 `probe_status` 或 readiness 为通过。同步 `probe_runtime_hosts` 只保留为受控运维命令，不是日常页面/API 旁路。

`latest-snapshot` 的 `model_availability` 会向已授权 platform administrator 返回 `observed_size_bytes`、完整 `observed_sha256` 与 `observation_source`，用于审核并创建下一版不可变 artifact 声明；响应不返回本机文件路径。name-only 远程库存仍不能作为受信 size/hash 证据。

recommend 返回可审核的建议/草案。decide 时服务端复验 host snapshot 是否有效、catalog/config hash 是否漂移，以及 deployment/workflow/候选是否仍兼容；陈旧推荐返回明确错误并要求重新 probe/recommend。接受建议不等于下载模型、部署模型、发布 workflow 或激活配置。

`runtime-hosts/node-metadata` 使用唯一分页 v2 合同：查询参数为必填 `deployment_id`、可选 `search`、`offset>=0` 与 `1<=limit<=200`；响应包含 `total_count`、`offset`、`limit`、`next_offset`、完整过滤排序目录的 `catalog_sha256` 和当前 `items`。客户端必须读取全部页，并在 total/hash 漂移、重复 class type、空页或 next offset 不前进时 fail closed；不再支持旧 `count/truncated` 合同。

## 错误语义

DRF 验证、权限、未找到和冲突响应统一使用 `{"error":{"status_code":400,"code":"invalid","details":...}}` envelope；调用方不得同时兼容另一套扁平 `code/detail` 形状。各 DELETE、publish 与 activate 操作的 400/403/404/409 以根 OpenAPI 声明为准。

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

## 契约校验

```powershell
.\scripts\check.ps1
```

`check.ps1` 会把动态 schema 生成到系统临时文件，完成 Spectacular 校验后，将 UTF-8 BOM 和换行符规范化，再与根 `openapi.yaml` 做逐字节比较。任何漂移都会明确失败，且检查过程不会覆盖根契约；审阅 API 改动后必须显式重新生成并提交根契约。检查同时拒绝重新出现的 `backend/openapi.yaml`，避免形成第二份机器契约。

如需检查动态 schema，可在测试环境启动后比较 `GET /api/schema/` 与根契约的路由集合。不得把临时生成文件提交为第二份契约。
