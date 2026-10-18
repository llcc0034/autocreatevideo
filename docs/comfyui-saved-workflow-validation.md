# ComfyUI 已保存工作流导入、验证与发布手册

本文是 ComfyUI userdata 已保存工作流纳管的正式操作手册。它描述当前已实现的页面和 API 主路径，不把自动测试、旧草稿或历史烟测当作当前真实 GPU 证据。

日常人工操作只使用平台正式页面：

- 导入入口：**设置 → 工作流 → 从 ComfyUI 已保存工作流批量导入**，路由为 `/settings?section=workflows`。
- 集中查看入口：**设置 → 验收与烟测**，路由为 `/settings/acceptance-smoke`；该页只读展示关联、历史结论和当前 freshness，并跳转到既有动作页。
- 版本验收入口：工作流版本表中的“验收与发布”，路由为 `/settings/workflows/{workflow_version_id}`。
- 模型证明与发布：仅当合同草稿或 Live Validate 要求补齐模型证据时进入 **设置 → 模型**；完成后返回工作流页生成新草稿或重新 Validate。

本文档变更没有执行真实 ComfyUI 长 GPU smoke。真实执行必须按第 9 节逐项现场验收，不能把 validate、preview、单元测试或旧 attempt 写成已通过。

## 1. 五层主路径与唯一事实边界

| 阶段 | 持久事实 | 本阶段要求 | 本阶段明确不要求 |
| --- | --- | --- | --- |
| 1. 发现并纳管来源 | immutable `ComfyWorkflowSource(import_kind=candidate)` | 受控目录、JSON 可解析、大小合规、无禁止敏感字段、精确 source scope/path/SHA | 目标模型已发布、目标执行 Deployment、projector、模型文件证明 |
| 2. 生成执行合同草稿 | immutable `ComfyWorkflowContractDraft` | 已纳管 candidate；实时 `object_info`；明确主输出后冻结投影与诊断 | 所有 blocker 已解决、Live Validate 或 GPU 执行 |
| 3. Live Validate | `WorkflowTemplateVersion` validation evidence | 草稿可提升、active 目标 Deployment、published 模型、新鲜 artifact、实时节点 Schema、冻结 projector/adapter 一致 | 具体 Smoke 素材或产物成功 |
| 4. 真实 Smoke | immutable `WorkflowSmokeAttempt` | 本次具体参数、Asset、真实提交、媒体/产物/资源证据 | 改写工作流版本合同 |
| 5. 发布与生产路由 | published/current WorkflowTemplateVersion | 合同、Validate、Smoke 证据完整且 fresh | 从 candidate/draft 直接建立 Recipe 或 ProductionRun |

candidate 和 contract draft 都没有执行权。它们不能进入 Validate、Smoke、Recipe 或 ProductionRun；目录受控只能证明“可以纳管”，不能证明“可以生产执行”。

用于读取本地 userdata 和 `object_info` 的内部 source Deployment 是受控传输代理；它由服务端按 Provider、RuntimeHost、规范化 endpoint 和配置目录形成的来源作用域选择。它不等于后续 Live Validate/Smoke 的目标执行 Deployment。管理员不在扫描前把某个模型实例指定成来源。

模型实例和工作流版本的关系仍按三层证据解释：相同 `ModelProfileVersion` 只是 **Profile 兼容候选**；`WorkflowTemplateVersion.validated_deployment` 精确指向实例才是 **历史 Live Validate 关联**；`WorkflowSmokeAttempt` 同时精确指向版本与实例才是 **某次 Workflow Smoke 的执行关联**。名称相似、同 Provider、默认 Deployment 或优先级都不是关联证据。

## 2. 发现并纳管 candidate

### 2.1 Scan

1. 在“设置 → 工作流”核对 `workflow.comfyui_saved_workflow_directory` 的有效 userdata 相对目录，再点击扫描。目录初始为 `workflows`，可以收窄为受控子目录，不能填主机绝对路径。
2. `GET /api/v1/comfy-workflow-sources/discover/` 只读扫描；任一来源失败都单独显示，不能伪装为空目录。
3. 扫描结果显示 source path、SHA、来源作用域、解析/安全诊断和既有 candidate/draft/version 精确关系。模型上下文只过滤页面，不改变扫描范围，也不能用文件名推断归属。

### 2.2 显式纳管

对需要保留的合法文件执行 `batch-import`，但选择 `import_kind=candidate`。这一步只提交 discover 返回的 source path/SHA 与 source scope 版本/hash，创建或复用不可变 candidate；不选择模板、模型版本或 projector，也不创建工作流版本。

candidate 保存原始来源文档、大小、hash、conversion provenance 和操作者。随后本地文件被覆盖不会改写既有 candidate；重新扫描并纳管后形成新的精确来源 revision。`execution_snapshot` 只登记与任务精确关联的运行来源，`ignored` 记录人工忽略；二者都不能代替 candidate。

## 3. 生成执行合同草稿

preview 必须命中完全一致的 candidate，否则返回 `workflow_candidate_required`。操作顺序如下：

1. 先调用 preview 读取主输出候选；未选择主输出时返回 `actionability=select_primary_output`，不会创建草稿。
2. 从服务端候选中明确选择唯一主输出；可按需要选择当前 draft/published ModelProfileVersion 作为解释上下文。
3. 再次 preview。服务端读取实时 `object_info`，转换 UI canvas、展开子图、裁剪不可达节点，并提取参数、固定素材槽、动态引用、模型 selector/文件依赖和输出合同。
4. 服务端持久化或复用不可变 `ComfyWorkflowContractDraft`，返回 `contract_draft_id/revision`、`admission_tier`、`actionability`、四类结构 SHA、projector identity、`blockers`、`live_validate_requirements` 与 `recovery_actions`。

`candidate_only` 是可审计的待处理草稿，不是导入失败。缺少或歧义的 projector、主输出、模型解释等结构问题进入 `blockers`；目标执行 Deployment、模型发布状态、新鲜模型文件证明和实时节点 Schema 等执行条件进入 `live_validate_requirements`。只有结构已被受审 projector 完整解释、目标模型/主输出与四类 hash 一致且 `actionability=ready_for_executable_import` 时，才能提升为工作流版本。

preview 中的画布当前值和服务端推荐值仅用于理解 Schema。具体 smoke 参数和 Asset 不进入 candidate、draft 或提升请求；它们只属于后续 attempt。

通用固定/动态 Projector 必须在“执行引擎与接线 → 执行能力”明确选择文生图、图片生图等已登记能力。该选择描述这张图能做什么，须与主输出和所选模型版本同时匹配；不能因主输出都是图片就推断两种能力相同。扫描推荐可保留未选择的配置供人工补充，但生成草稿会显示明确阻断，不能提升。补齐并发布 Projector 后，从同一候选生成后继合同；无需修改模型名称，也不会补写已有工作流。

### 3.1 输入图片决定输出尺寸

子图展开与尺寸支持是两道独立校验。Qwen Image Edit 2511 一类工作流可以成功展开子图、识别模型文件，但其尺寸不是显式 `width/height`，必须另有可证明的执行合同。

当前 Generic 编译器可冻结 `source_image_derived`，使用受审原语 `comfy.flux_kontext_image_scale/v1`：从图片主输出反向证明 `SaveImage/SaveImageAdvanced/PreviewImage → [VAEDecode → KSampler/KSamplerAdvanced → VAEEncode] → FluxKontextImageScale → LoadImage`。方括号中的整段可省略，编码和解码须连接同一个 VAE；所有尺寸链边使用第 0 输出。仅存在一个无关的缩放节点不算证据。

- `resolution_strategy` 保存 adapter、主输出、尺寸链、缩放节点、来源节点与唯一必填 `asset_semantic_keys`。来源路径由图结构证明，不按模型名选择；配置目标尺寸时，额外冻结 Projector 声明的 `sizes`。
- `required_dimension_multiple=16` 来自该原语的输出尺寸集合，不是任意兜底值。`image.width/height` 不进入可编辑参数；不能用项目宽高、静态值或运行时覆盖取代图片事实。
- 每次编译读取授权 Asset 的真实字节并核验 checksum，按 ComfyUI `LoadImage` 的 EXIF 朝向取得尺寸，按候选尺寸与原图宽高比的差值求值；等距离时按 `(距离, 宽, 高)` 排序。跟随原节点时保留 `FluxKontextImageScale`；配置尺寸时只在执行图将已证明的缩放节点替换为标准 `ImageScale`（Lanczos、center），不修改来源图或历史工作流版本。
- 具体来源 Asset ID、checksum、方向修正后的输入宽高与计算出的输出宽高保存在当次 `output_contract` schema v2 的 `resolution` 中，同时宽高进入已哈希的解析语义。Workflow Version 本身不保存具体素材。
- Smoke、独立素材生成、ProductionRun 产物归档均核对真实输出图片宽高；不一致即失败。节点/VAE 实现发生变化仍需重新 Live Validate 和真实 Smoke，不将导入通过等同于运行通过。

当前边界：仅支持静态单帧图片和上述确定路径；未知缩放器、尺寸分支、视频尺寸、`aspect_megapixels` 及动态素材组对尺寸链的改写仍明确阻断。旧的不完整 `source_image_derived` 合同不会自动取得执行资格，也不会修改已发布版本；从原 candidate 重新生成合同草稿，依次提升、Live Validate、Smoke、发布。

扫描顶层诊断使用候选模型中非冗余的阻断集合。如果正确模型只剩尺寸阻断，不再混入其他不匹配模型的“类型不兼容/缺文件”，也不引导重复登记模型；展开匹配诊断仍能查看每个候选的完整失败原因，匹配与提升标准不变。

### 3.2 查看规则与配置目标尺寸

- 查看入口：导入第 2 步合同草稿、工作流「详情与证据」、工作流「验收与发布」中的「查看尺寸规则与来源」。展示规则标识、候选尺寸、来源素材槽、子图展开后的 API 节点尺寸链以及冻结 JSON；缺少规则目录或规则未知时明确报错，不使用前端默认尺寸。
- 配置入口：目标工作流生成专用 Projector 建议，或该方案的后继草稿 →「执行引擎与接线」→「图片输出尺寸」。仅 `generic.fixed` 支持这一配置；选择「跟随原节点规则」不写覆盖项，选择「配置目标尺寸」可多选服务端预设或添加自定义宽高。
- 一组尺寸表示固定输出；多组尺寸按输入图片比例选择，不是运行时任意覆盖宽高。每边为 64–8192 内的 16 倍数，总像素不超过 16777216，最多 32 组，不允许空组、重复、非整数或未知字段。缩放算法和安全上限由后端注册，尺寸列表由版本化配置管理。
- 唯一写入位置是 `WorkflowProjectorVersion.definition.projection.config.image_resolution`，结构为 `{"adapter":"comfy.image_scale_candidates/v1","sizes":[{"width":1024,"height":576},{"width":512,"height":512}]}`。草稿保存后仍需发布，并重新生成工作流合同、提升新版本、Live Validate、Smoke、发布。现有工作流不跟随 Projector 最新配置漂移。
- 建议维护专用方案，不直接改变通用兜底方案的业务范围。配置了目标尺寸但来源图不满足受支持的尺寸链时，导入阻断，不能静默忽略配置。动态素材组和分类参考引擎不因此自动获得可改写图片尺寸链的能力。
- Live Validate 对每组配置尺寸使用同一编译函数构造 `ImageScale` 探测图，以目标部署实时 `object_info` 校验节点、宽高范围、插值和裁切选项；缺少节点或任一尺寸不支持都失败，不提交 GPU。通过后仍需真实 Smoke 验证模型可运行性与实际产物。
- 运行 `output_contract.resolution` 记录实际 Asset、输入尺寸、最终尺寸以及所用尺寸列表；三条产物收集路径统一核验。规则配置不宣称所有模型、显存环境都能运行任意合法尺寸。

### 3.3 合同生成响应丢失后的只读恢复

此路径需要后台已应用迁移 `0236_comfy_workflow_preview_receipt` 并加载匹配代码；代码／隔离验收不等于现用服务已更新。上线条件见[部署边界](production-deployment.md#合同回执更新的部署边界)。

1. 返回“设置 → 工作流”的导入区域，在“核对未确认的工作流导入”查看原请求。同一浏览器保留的待核对请求跨刷新有效；使用原操作账号，不清除浏览器记录来解除防重。
2. 点击“只读核对原请求”。这里只读取平台保存的原回执，不扫描 ComfyUI、不生成合同、不重发原写入；展开“查看已核对的原合同”核对完整输入约定、参数、素材槽和输出。
3. 原候选已加载且模型、主输出及来源选择仍一致时，页面恢复该合同；候选未加载或选择改变时只展示原合同，保留待核对项，不覆盖当前表单。需要继续原合同，应先加载同一候选并明确恢复原选择，再只读核对；加载来源的扫描本身可能访问主机，不能与无主机访问的回执读取混为一谈。
4. 回读成功只证明原合同保存结果。符合提升条件后，仍由用户明确勾选和提交版本提升，不自动提升、Validate、Smoke 或发布。

404、权限不足、响应损坏或请求身份不一致时保留“未知”，不证明原写入失败，也不解锁重复生成。旧请求没有原回执时不能从相似草稿、最新版本或当前配置补造；升级不会补齐历史回执。回执与合同草稿原子保存于数据库，备份沿[既有数据库备份流程](backup-restore.md)，不另建客户端合同事实源。精确请求键、权限和错误语义见[正式 API](api.md#comfyui-已保存工作流纳管)。

## 4. WorkflowProjectorVersion 配置化边界

`WorkflowProjectorVersion` 是独立版本化领域对象，不是普通 `ConfigRevision` JSON。稳定 `WorkflowProjector` family 下的每个版本可声明：

- Provider driver、operation 和 output kind；
- 节点角色、关键连接、允许的可选节点及数量；
- 参数冻结/开放策略、模型 selector 与文件角色映射；
- 固定素材槽、动态图片/视频/音频、用途、配对、互斥和预处理 adapter；
- 已注册 engine/compiler/output/dynamic adapter key；
- draft/published/retired、版本号、definition schema/size/SHA 和发布审计。

代码只保留受审的通用执行原语、安全验证器和 adapter registry；具体 H3 T2VA、FL2VA、Ref2VA 组合由 published projector 版本描述。新 preview 只使用 active family 的 current published 版本，并把 projector version ID/number、definition SHA 与 engine identity 冻结进合同草稿。发布 projector 后继版本不会重写历史 candidate、draft 或 WorkflowTemplateVersion；采用新规则可从同一不可变 candidate 生成新草稿并提升新版本，只有来源字节变化时才纳管新的 candidate revision。

### 4.1 配置对象与代码边界

| 层 | 保存内容 | 为什么放在这里 | 变更方式 |
| --- | --- | --- | --- |
| `WorkflowProjector` | 稳定 family key、名称、说明和 active 状态 | 作为同一图族的长期身份，不把版本号写进业务 key | key 创建后不改；停用只影响后续新投影 |
| `WorkflowProjectorVersion` | provider/operation/output、engine identity、完整 definition、schema/size/SHA、生命周期和发布审计 | 具体图族组合需要可配置、可审阅、可回溯，不能藏在普通 ConfigRevision 或导入分支 | 只改 draft；published/retired 不可覆盖；从最新版本创建后继草稿；仅最高、未引用的 draft 可受控放弃 |
| definition 数据 | `detector`、`output_nodes`、`model_selectors`、`node_roles`、`required_edges`、`parameter_policies`、`asset_slots`、`adapters`、`projection` | 表达 T2VA/FL2VA/Ref2VA 的节点组合、参数和素材拓扑 | 经过严格 Schema、大小、秘密字段、引用和冲突校验后发布 |
| 代码 registry | 通用 engine、compiler/output/dynamic adapter 和安全验证器 | 这些内容会执行算法或触及安全边界，不能允许普通 JSON 注入任意代码 key | 代码审查、测试和发布；新增 key 后才能被 version options 选择 |

因此“配置化”不是把整段 projector 算法搬进可编辑 JSON。业务组合、节点指纹和映射进入版本数据；真正执行转换的有限原语以及目录、JSON、敏感字段、大小、图安全和 adapter 注册校验继续留在代码中。definition 引用未注册 engine/adapter 时失败关闭，不能回退旧硬编码分支。

### 4.2 选择、冻结与升级

1. preview 按 Provider driver、`operation=workflow.import` 和已知 output kind 读取 active family 的 current published 版本，并逐项复验 definition 的 schema/size/SHA。
2. 先匹配非 fallback detector；没有精确命中时才考虑 fallback。命中集合按版本数据中的 priority 选择最高者；同优先级多命中返回 `workflow_projector_ambiguous`。发布时也会提前拒绝无法证明互斥的同优先级 detector。
3. 选中后按 definition 提取节点角色/连接、主输出、参数策略、模型文件角色、固定素材槽和动态引用，并调用已注册 engine/adapter 生成投影。
4. 草稿冻结 projector version UUID、family key、版本号、definition SHA、engine key/version 和 adapter keys；提升时逐项复验这些身份，不查询“当前最新版”替换它。
5. 后继 projector 发布后只服务新的 preview。旧 candidate 仍保存原始来源，旧 contract draft、WorkflowTemplateVersion、Smoke 和 ProductionRun 继续使用各自冻结的版本事实；需要采用新规则时可对同一 candidate 重新生成 draft 并 promote 新版本。只有工作流源文件本身变化时才重新 scan → candidate。

Projector 是由服务端自动匹配的，不由导入人手选。超级管理员可以对任何已纳管且已确定唯一主输出的 candidate 生成一份候选专用建议，包括已经由 fallback 命中或已经提升过工作流版本的候选。建议只进入本地未保存表单；人工调整并发布专用方案后，页面通过“用当前已发布 Projector 生成后继合同”显式重投影，并只允许追加到原 template。原方案已符合要求、仅需重新取得运行证据时，也可直接使用这一入口，不必为了绕过去重修改来源图或发布相同 Projector。不得停用 Generic fallback 来人为制造未匹配状态。

显式后继提升携带 `successor_of`，页面在发起重建时固定原前驱，不随扫描结果变化；同一草稿和完整请求重放返回原后继。新请求要求前驱是原模板最高版本（包含 draft/validated/published/retired），且存在同路径、同来源作用域的正式登记；已有更高版本时拒绝从旧前驱另开分支。即使结构完全相同也创建新的未校验草稿，旧来源、版本与证据不变。普通导入仍保持原去重行为。提交结果未知时先回读版本；刷新后不可将新扫描版本作为原请求的前驱直接重试。前驱状态不授予运行资格，新后继仍需独立 Live Validate、Smoke 与发布。

后继导入或重投影失败不会覆盖原发布版本及其执行准备。成功建立的新版本仍是草稿，须取得自身精确目标的Validate/Smoke证据；原版本的通过结论不能继承。已发布/退役版本再次发布明确返回400，连同直接调用共用发布服务，都不能因目标漂移把历史版本降为草稿或清空原证据。读取发现历史Smoke过期时保留历史成功事实，另行显示当前不可用；是否重新运行真实Smoke按具体授权执行。

当前迁移提供四个受管 family：`generic.comfy.fixed`、`minimax_h3.dual_clock_t2va`、`minimax_h3.dual_clock_fl2va` 和 `minimax_h3.classified_ref2va`。它们是可审计版本数据，不表示任何模型、节点包或 Deployment 已在目标主机就绪。

普通 generic fixed 合同仍把每个 `LoadImage`、`LoadVideo` 或 `LoadAudio` 节点投影成独立 `0..1`/`1..1` 槽。运行时可变的 `references={pictures,videos,audios}` 只由冻结的 projector 版本开放；浏览器不能提交 topology、compiler 或任意 adapter key。

### 4.3 “工作流合同方案目录”是什么

系统设置中的“工作流合同方案目录（Workflow Projector）”是**数据库中的版本化图结构解释规则目录**，不是扫描本地 JSON 的文件目录，也不是模型实例与工作流的手工绑定表。它接收已经纳管的 saved workflow 图、实时 `object_info`、唯一主输出和可选模型上下文，输出平台能够冻结、校验和执行的语义合同。

| 能力 | 具体作用 | 产出的证据 |
| --- | --- | --- |
| 识别图族 | 用节点类型/数量、可选节点、关键输入值、语义角色和必要连接判断图属于哪个受审结构；不按文件名或模型名猜测 | 精确 Projector family/version；精确命中或 fallback；歧义 blocker |
| 识别主输出 | 将 `SaveImage`、`SaveVideo` 等节点类型映射为图片、视频、音频或文本 | 唯一主输出和 output kind |
| 提取稳定参数 | 将具体节点输入映射为 `image.width`、`video.frames`、`sampling.seed` 等 semantic key，并声明 `fixed/object_info/source_locked/runtime` 策略 | parameter schema、target 和冻结策略 |
| 识别模型文件 | 从 Loader 节点提取文件名，映射 Artifact role、库存目录与 `object_info` selector | 模型依赖、文件角色和后续证明要求 |
| 生成素材合同 | 声明固定素材槽或动态图片、视频、音频引用组，核对模型 conditioning contract | 固定/动态素材槽、媒体类型、基数与合同来源 |
| 连接受审执行器 | 由 Engine 唯一确定 compiler、output 和 dynamic adapter；配置只能引用代码注册能力 | engine identity 与 adapter keys |
| 固化历史 | 将 Projector version UUID、版本号、definition SHA、Engine 与 Adapter 冻结进合同草稿和工作流版本 | 后继版本无法改写的审计链 |

Projector **不负责**保存工作流源 JSON、下载模型、创建 Deployment、执行 Live Validate/Smoke、保存某次运行的参数或 Asset ID，也不会把一个模型实例与所有相似文件自动绑定。一个 Projector 版本可以解释多个结构一致的工作流；一个工作流合同草稿只冻结一个精确 Projector 版本。已导入版本的来源行会保留当时人工确认的 canvas 主输出证据，供后续专用建议和后继合同使用，但不会因此重写既有版本。

### 4.4 Definition 参数如何工作

管理页面默认按下列分区编辑，完整 JSON 只保留在最后的“复核与 JSON”高级区：

| 页面分区 | Definition 字段 | 管理含义 |
| --- | --- | --- |
| 工作流识别 | `schema_version`、`priority`、`detector` | `priority` 只决定多个精确规则的选择顺序；fallback 只能兜底且不能同时声明图指纹 |
| 输出与模型 | `output_nodes`、`model_selectors` | 主输出类型，以及需要模型文件证明的节点输入、文件角色和库存选择器 |
| 节点与连接 | `node_roles`、`required_edges` | 用稳定 role 代替易变节点 ID，并要求关键角色间存在精确连接 |
| 参数与素材 | `parameter_policies`、`asset_slots` | 参数值治理方式，以及固定或动态素材槽、媒体类型和约束来源 |
| 执行引擎与接线 | `adapters`、`projection` | Adapter 与 `projection.kind` 由 Engine 锁定；只有明确开放的版本化 config 可编辑。通用多素材引擎配置模板分支与接线；分类参考引擎配置能力/模式、目标角色、四类媒体端口与数量来源、静态语义及可选提示词模板限制。帧格点在参数策略中统一配置 |
| 复核与 JSON | 完整 definition | 与参数表单使用同一草稿；非法 JSON 不覆盖最后一个有效对象，也不能保存 |

`GET /api/v1/workflow-projector-versions/options/` 是表单的服务端事实源：返回 editor schema、字段分区/帮助、枚举、数量上限，以及每个 Engine 的说明、固定 Adapter 合同、projection kind、锁定字段和专项字段。前端缺少目录、遇到未知 editor schema 或未知 Engine 时失败关闭，不能用本地默认值猜测。服务端保存和发布时仍执行完整字段集合、跨字段关系、大小、敏感字段、注册表和方案冲突校验；前端表单校验不能替代它。

创建新 family 提供三种初始化方式，共用同一结构化编辑器和 family/v1 创建 API：

- **从工作流推荐（默认）**：这是初始化方式选择，不是执行扫描的按钮。没有就绪候选时点击“前往扫描工作流”，在原扫描区显式扫描、纳管候选并确认主输出；扫描区显示“返回新建方案”，当前页往返保留表单，关闭或刷新页面不保留。有候选时先选择，再点击“生成推荐配置”，读取实时节点目录生成待复核建议。页面分别显示尚未扫描、扫描失败、部分来源失败、未发现工作流和候选尚未就绪，不把失败当作空结果。推荐不要求已有 Generic 版本，缺失模型 selector 目录会明确警告，不猜测补全。
- **复制已有方案**：显式选择已有版本，精确复制 Engine、driver、operation、output 和完整 definition；复制后独立，不是运行时继承。
- **空白新建**：显式选择已注册 Engine，使用 options 返回的 `engines[].creation_template`。骨架不查询已有版本，不含节点、模型 selector、参数和素材业务规则；它不是可直接保存或发布的合同，必须补齐必需字段并通过正常完整校验。

更换初始化方式、复制版本或空白引擎前，已编辑的配置需确认覆盖；保留方案名称与说明。已有 family 的规则调整仍使用“创建后继草稿”。误建草稿可以“放弃草稿”，但只允许删除该 family 的最高、未发布、非 current 且无合同草稿引用的版本，并要求确认与审计原因；历史版本不变。Adapter 与 `projection.kind` 由 Engine 锁定；新增业务方案不等于注册新执行算法。

当步骤 2 已生成合同草稿但包含 `workflow_projector_*` blocker 时，超级管理员可以从该行生成方案建议。服务端只接受已纳管 candidate ID、扫描时的 source scope/hash、唯一主输出和页面上人工确认的 output kind；随后复验 candidate 完整性并读取 fresh `object_info`。响应自动补充单份可达图能证明的精确节点数量、主输出、唯一节点角色、连接、已发布目录中的受控模型 selector、参数目标与固定素材槽，同时给编辑器提供节点类型和输入名选项目录。

建议固定为 `persistence=unsaved_draft`、`requires_human_confirmation=true`，不会创建 family/version、不会自动发布，也不会提交 ComfyUI prompt。它不返回 source path、提示词、素材文件名或模型文件字面值。单份候选不能证明 optional node，也不能替管理员决定长期 family key/name、业务语义、关键 `input_equals`、priority 或哪些结构差异属于同一图族；这些字段必须在保存草稿和发布前复核。候选、作用域或主输出变化后必须重新生成推荐；切换到复制或空白方式则清除推荐关联。

### 4.5 如何对接导入候选

1. 扫描和 candidate admission 只证明受控目录、JSON、大小和敏感字段安全，不查询 Projector。
2. 用户从服务端候选中选择唯一主输出后，preview 转换 API graph、展开子图并裁剪到主输出可达范围。
3. 服务端读取 active family 的 current published 目录，先匹配非 fallback，再考虑 fallback；同一层取最高 priority，最高优先级仍有多项则返回歧义，绝不由页面手工挑选绕过。
4. 命中后，Projector 联合实时 `object_info` 提取参数、模型 selector、素材槽、动态引用和输出合同；无法解释的部分写入 blocker，candidate 仍保留。
5. 草稿冻结精确 Projector 与 SHA；promotion 只消费冻结 candidate/draft，不重读本地文件或自动切到最新 Projector。
6. 新 Projector 发布后，若 saved workflow 字节未变，直接对同一 candidate 重新 preview → draft → promote；只有来源字节变化才重新 scan/admit candidate。
7. 提升出的新 WorkflowTemplateVersion 仍须单独完成 Live Validate、真实 Smoke 和发布。Projector 发布成功只代表解释规则可用于新 preview，不代表任何工作流已通过执行验收。

候选建议不是未发布 Projector 的 dry-run：它只证明服务端能从当时的 candidate + `object_info` 形成一个结构合法的初始 definition。草稿保存成功仍不证明它已命中真实工作流。发布前应审阅节点/连接/参数摘要；需要证明真实匹配时，发布后从目标 candidate 重新 preview，并继续完成版本验收。

### 4.6 查看已发布配置与四种参数策略

方案目录中每个版本均有“查看 vN 配置”，包括启用方案的 current published 版本。查看复用同一结构化编辑器，但所有业务输入只读、没有保存按钮；仍可切换分区，在“复核与 JSON”复制完整 definition。版本状态、current 标记和合同 SHA 与所查看版本一起展示；不能把“方案族已启用”理解为其未发布草稿已生效。需要修改时显式创建后继草稿。

从工作流关联进入查看时，页面按 `workflow_context` 读取该工作流，核对冻结的 Projector 版本和 definition SHA，再读取工作流实际绑定的精确模型版本；不使用 URL 中另选的模型代替。动态数量直接展示工作流 `input_contract.classified_references.groups` 或 `asset_groups` 的冻结范围和字段路径，不以当前模型声明重算。上下文缺失、不匹配、读取失败时明确报错并提供重试，不回退到模型预览。

从 Projector 目录直接查看时不要求选择模型。通用分类参考规则的能力与模式以已发布值展示；“选择模型进行对照（可选）”按需展开，区分未加载、加载中、读取失败、一致和不匹配。该对照只检查所选模型的能力/模式及各组数量声明，不代表工作流的在线校验或 Smoke 结论，不保存模型绑定；同组来源详情复用这一次对照读取。具体模型及执行数量在生成工作流合同时冻结，而非在 Projector 目录查看时选择。

以下是 **三个 Generic 引擎共用的参数策略**，不是只根据下拉框名称推断的语义。入口为 [`_apply_generic_projector_configuration`](../backend/studio/services/comfyui_workflow_imports.py)：先用 `node_role` 唯一定位节点，再用 `input_name` 定位现有输入绑定，最后将它映射到 `semantic_key`；无法唯一定位或语义键冲突时返回 blocker。

| 页面策略 / API 值 | 当前实际处理 | 适用示例与边界 |
| --- | --- | --- |
| 固定值 / `fixed` | 来源值必须等于 Projector 的 `value`，否则产生 `workflow_projector_fixed_value_mismatch`，不会静默修改来源图；写入 `default`、`const` 和 `use_source_default=true`，数值类型另设 `min=max=value`，从导入可配置参数清单移除 | 固定步数、FPS、开关或枚举；所有类型在运行时均禁止覆盖为其他值，表单显示只读固定值 |
| 由实时 object_info 约束 / `object_info` | 初始参数投影读取实际节点 Schema 的类型、范围、枚举等，并冻结到新合同；此策略移除 `use_source_default`，保留可配置参数 | 宽、高、帧数等。不是每次输入自动重写已发布合同；Live Validate 仍须核对在线节点能力 |
| 锁定导入来源值 / `source_locked` | 复制本次来源节点值为 `default`，设置 `use_source_default=true`，从导入可配置参数清单移除；不将数值范围缩成一个点 | 保留该来源版本的设置，不要求所有候选具有同一固定值。当前是来源默认优先，不是绝对不可覆盖锁 |
| 每次运行时传入 / `runtime` | Projector 不保存本次运行值；移除 `use_source_default`，由本次请求、内建语义解析或合同默认值取得值，仍经过类型/范围/必填校验 | 提示词、随机种子等。Generic 当前与 `object_info` 使用同一处理分支，并未形成两套不同的运行时约束 |

`source_locked` 是来源默认优先，仍接受通过合同检查的显式覆盖。新合同的 `fixed` 写入 `const`，语义解析器对所有值类型拒绝不同值；运行表单把唯一必选值显示为只读。`detector.input_equals` 对应的已映射参数也固定，不能运行时切换图族。已发布旧合同不会被查询侧补写字段。

固定、通用多素材与分类参考引擎共用上述策略。分类参考的帧格点从 `parameter_policies[].constraints` 派生为 `frame_policy`，没有 H3 的 FPS、步数、LoRA 或帧格点常量。`object_info.step` 仅是编辑增量，不当成从 0 起算的整数倍约束；对齐需显式配置 `multiple_of` 或模数/余数。

### 4.7 素材合同来源与动态数量

“合同来源”决定约束从哪里取得，不决定素材文件从哪里上传。实际素材始终来自受管 Asset，在本次 Smoke 或生产请求中按 ID 选择；不要把本地文件路径写进 Projector。

| 来源 / API 值 | 当前实现 |
| --- | --- |
| Projector 中配置 / `workflow` | `generic.fixed` 派生固定 `0..1`/`1..1` 槽；`generic.references` 从 Projector 动态组的 `minimum_items/maximum_items` 读取范围，验证实际端口后冻结有界动态组 |
| 模型版本合同 / `model_profile` | 通用动态组使用 `conditioning_mode` + `model_input` 指向精确模型版本的 conditioning mode 数量输入；须清空本地 minimum/maximum，不能覆盖模型声明。没有该合同的图片模型使用人工审核后的 workflow 数量范围 |

单独勾选 `dynamic=true` 不会改变执行图。QwenEdit 等非 H3 工作流可以通过 `generic.references/v1` 配置通用接线，不需要增加以模型名称分支的后端编译器；前提是实际节点、模型和图结构能够用受控原语表达。这不是任意模型、任意数量的承诺。

#### 通用动态素材配置流程

1. 在“系统设置 → 工作流”纳管候选并确认主输出，点击“基于此候选生成专用 Projector 建议”。单素材分支连到同类型可选端口时，会推荐通用动态草稿；证据不足则保留固定建议并明确提示。
2. 未自动命中时，选择“空白新建 → 通用动态多素材接线”，补充节点角色与结构；或者在“复制已有方案”中选已有通用动态版本。两种方式都只初始化新草稿，不改变已发布版本。
3. 在“执行引擎与接线 → 动态素材组”选择加载角色、逐项预处理节点、模板输出、目标角色和有序输入名，再选择接线方式及数量来源。组与素材槽属于同一个 Definition，不保存本次运行的 Asset ID。
4. 人工复核后保存/发布 Projector；对同一不可变候选生成后继合同、提升新 Workflow 版本。来源图改变时，先保存并纳管新 candidate revision。
5. 新版本通过 Live Validate 后，在烟测或素材生成表单点击“增加素材 / 减少素材”，按顺序选择素材库条目或上传本地文件。提交仍使用 `assets=[{semantic_key, asset_id}]`，不能跳过中间槽。减少数量不删除素材文件。
6. 在目标 Deployment 真实 Smoke、核对产物后发布。旧 Workflow、H3 Ref2VA、Smoke 和 ProductionRun 不会重新解释。

| 接线方式 | 配置与执行 |
| --- | --- |
| `ordered_ports` 顺序端口 | 如实际 Schema 存在 `image1/image2/image3`，配置 1–3 份；最大数须等于端口数，超过最少数量的端口必须可省略。逐份重复加载/预处理分支，并接到对应端口。目标角色可为 many，把相同顺序接到多个同类型节点；须复核所有目标语义。展开式 autogrow 端口同样须通过实时 Schema 验证 |
| `reduce` 逐项合并 | 选择来源图已有的合并节点、左右输入和输出索引。其两个输入须接同一模板输出，不能吞掉另一条实际分支。运行时 1 份直接连接，N 份产生 N−1 个合并节点；可表达节点原生支持的列表/批次。端口类型必须兼容，列表与批次不能靠改标签转换 |

[`workflow_asset_groups.py`](../backend/studio/services/workflow_asset_groups.py) 校验配置、冻结最大分支、按选择删除不用的分支并重新接线；[`workflow_compiler.py`](../backend/studio/services/workflow_compiler.py) 注册 `generic.comfy/v2`，固定配套 `generic.asset_groups/v1`。逐项预处理与合并节点的标量使用冻结来源默认值，不允许运行时单独修改副本。模型、CLIP、VAE 加载节点应留在组外共享。数量和展开节点总数受既有安全预算约束。

推荐数量只是节点端口上限，不证明模型可用数量或显存充足。多来源、歧义分支、重复模板节点类型、组外消费者或专用时序/音轨配对不会被猜测补全；应调整来源模板、显式配置或增加受审原语。后续模型若能复用现有顺序端口或逐项合并规则，只需配置新 Projector 并验收，无需修改后端代码。

此能力不会安装节点或绕过执行白名单。新节点须已安装并通过服务端安全审核；现有扩展入口为部署环境的 `COMFYUI_EXTRA_ALLOWED_NODE_TYPES`，危险节点禁令仍优先，浏览器和 Projector 不能扩大白名单。

分类参考（包括 H3 Ref2VA）的配置/消费顺序：

1. 在目标**模型能力版本**审阅 `conditioning_contract` 对应 mode 的 `inputs`，包括 `entity_images`、`total_images`、`reference_videos`、`paired_video_audios`、`reference_audios` 的 `minimum_items/maximum_items`，以及用途、配对等拓扑约束；变更已发布模型时创建新版本，不原地扩大旧合同。
2. 新建通用分类参考方案或复制已有版本。四组 `images/videos/video_audios/audios` 对应 image/video/audio/audio；每组选择“Projector 中配置”或“模型版本合同”。前者填写 minimum/maximum 且不能超出模型能力；后者按 `mode_input` 读取所选模式输入合同。端口使用有序名称列表或从 0 编号的完整前缀，二选一。
3. 对同一候选生成新合同：`project_plan` 解析数量来源，冻结 `classified_references` 和收窄后的 conditioning 合同。通用在线校验检查最少、最多以及仅后部视频携带音轨的接线图；端口缺失、类型错误、数量超界或原端口已有连接时阻断。
4. 提升为新 Workflow 版本并 Live Validate。工作流详情的 `workflow_input_schema` 展示冻结的参数和素材声明；实际资源烟测通过 `smoke-contract` 读取共同 Preparation 输入端口。图片数量由总量与分类限制共同约束，视频/音频还读取相应 topology；所有选择仍受物理素材总上限、用途和音轨配对约束。缺少默认值不会自动补测试值，默认值不符合范围或步长时明确报错，不静默缩小尺寸、改种子或对齐帧数。
5. 本次 Smoke 表单按冻结范围增减素材；[`workflow_classified_references.py`](../backend/studio/services/workflow_classified_references.py) 根据规范化引用添加加载节点和连接。图片/视频/音频分别排序，配套音频保持所属视频位置。通过真实 Smoke 并核对媒体后再发布。

例如希望一次选 1–3 张图片：相应模型 mode 的 `entity_images` 应允许 `minimum_items=1, maximum_items=3`，`total_images.maximum_items` 也至少为 3；节点、其他模式约束和总素材预算均须支持。这个数值只是配置例子，不是对当前模型能力的断言。只改 Projector、不生成新合同/工作流版本，旧 Smoke 表单不会变化；只提升工作流也不等于执行验收通过。

## 5. 从合同草稿提升为工作流版本

选择 `actionability=ready_for_executable_import` 的精确 contract draft，再以 `import_kind=template_version` 提交：draft ID、candidate source/scope/hash、唯一主输出、模型版本、四类审阅 SHA，以及新建或既有 WorkflowTemplate 目标。

提升服务只锁定并校验 candidate 与 draft 中已经冻结的文档和 projection；它**不会重新读取本地来源文件，也不会再次请求实时 `object_info`**。projector version/definition SHA/engine identity、模型版本、主输出或任一审阅 SHA 不一致时返回 `workflow_contract_draft_mismatch`；草稿仍有结构 blocker 时返回 `workflow_contract_blocked`，并保持零写入。

成功后创建或复用 needs_review `WorkflowTemplateVersion` 与正式 `ComfyWorkflowSource(import_kind=template_version)` provenance。HTTP 200 仍可能是 `partial`，必须检查 `failed_count` 及每项 `result/code/detail`。提升不接收具体参数值或 Asset，也不提交 GPU。

旧版本或 projector 语义升级都不能原地迁移。保留旧版本审计：来源字节未变时从既有 candidate 重新生成 contract draft → promote；来源字节变化时重新 scan → candidate → contract draft → promote。随后在新版本上执行 Live Validate → fixture → real Smoke → publish。

## 6. 集中查看与工作流版本验收：RuntimeHost Probe → validate → fixture → Workflow Smoke → publish

先在 `/settings/acceptance-smoke` 按工作流或 Deployment 查看关联证据、历史结论、当前 freshness 和最近 attempt。该页只提供筛选、详情与跳转：需要处理 `not_run`、`drifted`、failed、active 或 uncertain 时，进入 `/settings/workflows/{workflow_version_id}` 使用唯一动作表单，不能在集中页重新拼装 fixture 或重复提交任务。

按 Deployment 筛选时，绿色 Live Validate/Workflow Smoke 只属于当前筛选实例；历史证据来自另一实例时会保留历史状态并明确显示“并非当前实例”，不会跨实例复用 freshness。若最近 Workflow Smoke 为未消歧 `uncertain`，集中页会阻断新的 Smoke，并引导进入原工作流版本页完成既有 attempt 的消歧。模型合同或实例 target 无法核验时，集中页保留历史快照但显式标为无效，不能将其解释成“未执行”。

ComfyUI 的正确链路是 **RuntimeHost Probe → Live Validate → Workflow Smoke**。RuntimeHost Probe 提供当前主机能力和模型文件证明，Live Validate 把不可变工作流版本精确绑定到目标 Deployment 并校验实时结构，Workflow Smoke 再以本次冻结 fixture 执行真实媒体任务。`async_rest` 使用 Deployment Probe/Smoke；文本 Deployment 仅对已声明并受支持的 operation 使用 Deployment Smoke，不提供 Deployment Probe。这些实例级动作都不能替代 ComfyUI Workflow 的 Live Validate 或 Workflow Smoke。

### 6.1 Live validate

1. 打开导入成功的版本。
2. 选择与版本 model profile 精确匹配的 active deployment。
3. 点击“校验”。
4. 刷新详情并确认：
   - `validation_state=passed`；
   - `validated_deployment` 为本次选择；
   - validation evidence 和 hash 非空；
   - 页面未显示 graph、model inventory、output 或 target drift。

validate 使用受管素材 token 检查槽位结构，不要求先选择具体 Asset。重新 validate 会清空旧 smoke evidence，因此顺序必须是 validate 在前、smoke 在后。

模型文件路径以安全的 models 相对路径作为平台中立身份，`/` 与 `\\` 仅作为目录分隔符时视为同一文件。Live Validate 仍会从目标 Deployment 的实时 `object_info` 中解析且只接受唯一匹配，并把 ComfyUI 返回的精确原值（包括 Windows 反斜杠）冻结为 validation evidence 的 `submission_filename`；执行前仅用该证据值覆盖对应模型 widget。缺少匹配、出现多个规范化后等价的枚举，或冻结 graph 指向不同模型时均失败关闭。仅分隔符不同不需要重存 ComfyUI 工作流或重新导入版本。

### 6.2 配置本次基础烟测

执行实例改造处于P4隔离验证阶段；真实服务切换状态以[改造计划](optimization/execution-instance-unification-plan.md)为准。工作流详情的 `workflow_input_schema` 仅供查看冻结参数、默认值与素材结构，不再提供烟测样例默认值。创建烟测使用正式 `smoke-options/smoke-contract` 和共用输入表单。

1. 先完成本版本Live Validate；基础工作流烟测只使用其已校验的部署，不能临时换成另一个实例。
2. 明确选择本次能力、模式及人工通道或无提示词模式；基础图不隐含调用提示词策略。要验证策略与工作流组合，应在执行实例版本上创建组合烟测。
3. 按返回合同填写参数、受管素材与人工通道。数量、顺序、配对、尺寸和对齐规则来自精确合同；不按H3或其他模型名推测，不自动填入业务样例。
4. 使用共用受管素材选择或正式本地上传；上传后引用Asset身份。无素材输入时按合同提交空输入，不虚构素材或默认文件。
5. 核对本次输入和授权后创建准备；从“准备与输入”恢复原Preparation，确认所需字段后继续。缺失输入明确等待人工，不自动调用辅助模型。

### 6.3 创建、继续与查看结果

原 `POST smoke-test` 使用明确的Idempotency-Key，首次返回202，同键同请求返回200；响应包含原SmokeAttempt与Preparation，Location指向准备详情。只有准备通过并进入原队列，才开始主烟测计时。

- 原任务包含preparing、queued、submitting、running、cancel_requested及终态；preparing不是远端正在执行。
- 相同幂等请求只恢复原对象；不同请求遇到同一资源的活动或未消歧任务返回409，不通过fixture相同偷偷复用。
- 从“准备与输入”可查看原素材、参数、通道与审核选择；任务和Workflow证据只引用同次准备、结果及摘要，不另存一份业务输入。
- 成功必须有原worker已提交、冻结图匹配以及真实产物校验；过期或配置漂移不能被历史成功覆盖。
- 提交结果不确定时先核对远端任务并在原任务消歧；人工结论不会改成烟测通过，也不刷新TTL。需要重测时明确创建新请求，不自动重发。

基础Deployment/Workflow的验收与执行实例组合验收分别展示，不能相互替代。动态分类参考与通用素材分组共用同一冻结输入和接线协议，不存在模型专用Smoke入口。

### 6.4 Publish

点击“核对并发布”前确认：

1. validation 与 smoke 均为 passed；
2. 两份 evidence hash 有效；
3. smoke evidence指向成功的基础Attempt与原Preparation，请求、编译图、产物及结果摘要完整且未过期；
4. validated deployment 仍 active，Provider、model profile、artifact 与 target hash 未漂移；
5. 本次 evidence 属于当前不可变 execution contract。

发布拒绝后先刷新版本。若 target 或结构漂移导致证据失效，必须重新 validate 和 smoke；禁止补写、搬运或复用旧证据。

## 7. 参数与素材变化处理

| 变化 | 是否重导版本 | 恢复动作 |
| --- | --- | --- |
| 只改变某个允许的 smoke 参数值 | 否 | 等待/取消 active attempt，创建新 fixture attempt |
| 只更换某个槽的合格 Asset | 否 | 重新读取候选并创建新 fixture attempt |
| Asset 字节或 checksum 在 attempt 后漂移 | 否，除非槽结构也变 | 不修改旧 attempt；恢复原字节或选择当前合格 Asset 后创建新 attempt |
| 参数类型、范围、枚举、required、target 变化 | 是 | 新 source revision → candidate → draft → promote，再 validate/smoke |
| 素材槽 media type、input slot、required/cardinality 变化 | 是 | 新 candidate/draft/version；不能用 fixture 绕过 |
| 增删 Load 节点或改变拓扑 | 是 | 保存新的 ComfyUI source revision，重新走五层主路径 |
| projector definition/engine/adapter 变化 | 是 | 发布 projector 后继版本，再从同一不可变 candidate 生成新 draft/version；来源字节变化时才纳管新 candidate，旧版本不重解释 |
| 模型依赖或主输出变化 | 是 | 重新证明/发布模型，从既有或新 candidate 生成新 draft/version 和新证据 |
| 模型路径仅 `/` 与 `\\` 分隔符写法不同 | 否 | 在同一不可变版本上重新 Live Validate；证据冻结目标 `object_info` 的唯一精确枚举值，执行时按该值提交 |
| 仅 deployment target 变化 | 否 | 在同一不可变版本上重新 Live Validate，并创建新的真实 Smoke evidence |

## 8. 错误与恢复

### 8.1 Scan / preview / batch

| 用户可见代码 | 触发 | 恢复 |
| --- | --- | --- |
| `workflow_request_budget_exceeded` | discover 的共享项数、总字节或同步时间超限 | 整次请求返回 HTTP 400，不生成新扫描结果；先在“默认值与限制”收窄 `workflow.comfyui_saved_workflow_directory` 后重扫，仍超时再由超级管理员依据测量复核 `security.comfy.import.scan_max_seconds` |
| `workflow_request_budget_exceeded` | batch 提交项数超过服务端有效上限 | 整次请求返回 HTTP 400；按 discover 的 `effective_limits.batch_max_items` 拆批 |
| `workflow_request_budget_exceeded` | batch 执行过程中触发总字节或同步时间预算 | 已成功提交的 sibling 保留；当前项和剩余项显式 `failed`，稳定来源后分批重试失败项 |
| `workflow_candidate_required` | preview 没有命中完全一致的 immutable candidate | 先以 `import_kind=candidate` 纳管当前 source revision |
| `source_changed` | discover 后本地 source SHA 变化 | 重新 scan 并纳管新 candidate；旧 candidate 保持不可变 |
| `conversion_contract_changed` | live object_info 或转换图变化 | 稳定 ComfyUI 节点环境后重新 preview |
| `source_scope_changed` | discover 后来源 Provider、RuntimeHost 或规范化 ComfyUI endpoint 变化 | 重新全局扫描；不要继续使用旧行的内部 Deployment 或来源 hash |
| `source_deployment_changed` | source deployment/runtime/provider/profile 指纹变化 | 稳定部署后重新 scan/preview |
| `source_already_classified` | 同一来源被改投其他分类、模板或合同 | 使用原分类/模板，或创建新的来源修订 |
| `secret_field_forbidden` | canvas 含明文 credential | 删除明文并改用受管 secret binding |
| `unsafe_source_literal` | 不安全本地路径、上传 literal 或输出路径 | 改为 semantic 槽和受控输出，再保存 source |
| `incompatible_model_profile` | 草稿/提升目标 profile 与来源或主输出不兼容 | 选择正确 profile；发布和文件证明可在 Live Validate 前补齐 |
| `model_dependency_unmanaged` | Live Validate 所需 artifact/inventory/size/hash 证明不完整 | 修复 inventory，重新 attestation 后再 Validate；candidate 仍保留 |
| `workflow_projector_catalog_unavailable` | 受审 projector 目录不可用 | 由超级管理员恢复目录；保持 candidate/draft，不回退硬编码规则 |
| `workflow_projector_ambiguous` | 多个 projector 同时命中且无法唯一解释 | 修正/发布互斥 definition 后重新生成 draft，不按优先级猜测 |
| `workflow_contract_draft_mismatch` | promotion 请求与 draft 的 candidate、projector、model、primary 或 SHA 不一致 | 重新读取精确 draft；需要新语义时重新 preview 生成新 draft |
| `workflow_contract_blocked` | draft 非 ready 或仍有结构 blocker | 按 blockers/recovery actions 修复后生成新 draft |
| `workflow_output_contract_invalid` | 主输出数量、节点类型或 output kind 不匹配 | 选择唯一受支持主输出并重新 preview |
| `workflow_import_failed` | 其他结构或安全合同失败 | 按详情修复；详情被安全投影时查后端日志，不盲重试 |

preview 的 blocker 与 `live_validate_requirements` 分开：结构无法解释时 `actionability=candidate_only`；目标 Deployment、模型发布和 artifact freshness 属于 Live Validate 前置要求，不应反向否定 candidate 来源。客户端显示服务端返回的稳定 code/detail/action，不维护第二套阻断码白名单。

### 8.2 模型证明

- `draft_artifact_attestation_conflict`：已有不同意图的 active probe；轮询或等待现有 attempt。
- `draft_artifact_attestation_failed`：查看 `final_snapshot.details.probe_errors`，修复整个 artifact closure 后创建新 attempt。
- `runtime_host_state_changed`：探测期间 deployment/profile/artifact/path 漂移；稳定后重做。
- `required_config_missing`：补齐正式配置。
- `runtime_host_probe_failed` 或 `probe_snapshot_missing`：检查 platform-maintenance worker 和持久化故障。
- `attestation_request_contract_invalid`：请求快照完整性失败，不能复用该 attempt。

#### 8.2.1 Live Validate 的模型文件证明恢复

Live Validate 不再把模型证明故障显示为 Python 字典或笼统的 `graph` 错误。API 返回稳定原因码，并在 `details.recovery` 中给出当前状态、处理步骤和受控动作；页面将技术原因折叠显示，主区只呈现可执行建议。

| 原因码或当前恢复状态 | 含义 | 页面建议 |
| --- | --- | --- |
| `ready_to_retry` | 上次校验失败后，新主机证明已经完成，且 size/SHA-256 已与模型声明一致 | 保持当前验收部署不变，直接点击“重新校验”；不要改工作流或重新声明模型 |
| `host_probe_running` | 当前主机已有 capability Probe 排队或执行中 | 前往“运行主机”查看进度，等待成功后返回重新校验；不要重复创建 Probe |
| `host_snapshot_stale` / `host_snapshot_not_latest` / `host_probe_failed` | 当前没有可用的新鲜主机证明 | 在“系统设置 → 运行主机”对验收部署绑定的主机执行探测，成功后返回重新校验 |
| `artifact_observation_unverified` / `artifact_profile_mismatch` | 最新探测仍缺少匹配的文件大小/SHA-256，或模型版本绑定不一致 | 先重做主机探测；仍失败时在“模型 → 文件与证明”核对主机文件和声明。已发布模型声明错误必须创建下一版本，再重新导入工作流 |

恢复响应不返回模型绝对路径、文件 SHA-256 或主机敏感配置。主机证明刷新不会自动执行 Live Validate；必须由管理员在版本页显式点击“重新校验”。

### 8.3 Fixture 与 smoke

Live Validate 成功后版本进入 `validated`，验收页随即开放“本次 Smoke 输入”和“创建烟测任务”；`draft` 只用于校验前或证据失效后的状态。Smoke 与发布均不得继续使用 `draft` 作为可操作条件：Smoke 要求 `validated + validation passed`，发布再追加 `smoke passed`。已发布或停用版本不能创建新 Smoke，修改必须导入新 revision。

当当前 conditioning mode 的图片、视频、音频最小数量都为 0，且用户没有选择任何素材时，素材候选读取失败不阻断零参考 Smoke；一旦添加引用或存在必填素材槽，必须成功读取并校验候选素材后才能提交。

| 状态或代码 | 恢复 |
| --- | --- |
| `workflow_input_contract_invalid` | 当前版本的只读输入合同不可核验；检查冻结参数、语义绑定与素材槽结构，不能当作没有输入 |
| HTTP 400 `invalid` 且 `details.fixture` 非空 | 修正未知/缺失参数、联动值、required 槽、MIME、checksum 或素材预算；无 attempt 写入 |
| `workflow_smoke_fixture_conflict` | 同目标已有不同 fixture 的 active attempt；等待或取消后再创建 |
| `cancelled_before_submission` | 未提交远端；确认仍需执行后可创建新 attempt |
| `idle_timeout` / `hard_timeout` | 检查 worker 容量和队列；确认未远端提交后再试 |
| `cancelled` | 先核对远端是否仍执行，再决定新 attempt |
| `submission_uncertain` / `remote_state_uncertain` | 按第 8.4 节人工消歧，禁止直接重复提交 |
| `unresolved_workflow_smoke_submission` | 尚有未消歧的 uncertain attempt；先核对并追加人工结论，不能换 fixture 绕过 |
| `workflow_smoke_failed` | 先查看 attempt 中服务端脱敏后的失败节点、具体原因和处理建议；修复 fixture、运行时或输出后创建新 attempt。只有 history 结构未知时才继续查看 ComfyUI History/日志 |
| `provider_smoke_failed`，且原因提示 Ref2VA 至少需要一个参考媒体输入 | 在“本次 Smoke 输入”中至少添加 1 个合格参考图片、参考视频或独立参考音频，然后创建新的 attempt；不要重放原失败 attempt |
| `provider_smoke_output_invalid`（动态 Ref2VA） | 对照该 attempt 冻结的宽高、帧数、24fps 与有音轨要求检查 FFprobe 输出；确认 T8 AVDecode 和 CreateVideo.audio 连线未被环境节点合同改写，再重新 validate 与 smoke |
| `provider_smoke_failed`，且具体原因显示 `torch.OutOfMemoryError` / CUDA out of memory | 这是运行主机容量不足，不是可通过改素材、降为 T2VA 或降低 4 steps 掩盖的问题。保持动态 Ref2VA 已冻结的 fixture 与 24fps / 4 steps 合同，先独占或迁移到显存足够的 GPU，或使用审核过的低显存工作流版本；在目标主机确认 DynamicVRAM 的启动证据后，重新执行主机探测、live validate，并创建新的 smoke attempt。已提交的失败 attempt 保留作 OOM 审计证据，绝不重复提交它。 |
| `provider_smoke_output_invalid`，且本机提示找不到 `ffprobe` | 在运行 workflow smoke worker 的主机安装 FFmpeg/FFprobe，或把受控 `FFPROBE_BINARY` 配置为该可执行文件；重新探测主机后创建新的 smoke attempt。不得用手填媒体 metadata 替代实际探测。 |
| `参考视频缺少已验证帧率` 或 `参考视频必须是 24fps CFR 素材` | 使用正式资产库上传/规范化为 24fps CFR 后重新选择；不要仅改扩展名或手工改 metadata |

普通 fixture/远端失败会把本次 smoke attempt 标为 failed，但不自动使仍有效的 Live Validate 历史结论失效；实时结构或 target 漂移也不得改写历史 passed，而是把当前 freshness 单独显示为 `drifted` 并阻断继续 Smoke/发布。恢复前必须刷新版本：若 freshness 已漂移，先重新 Live Validate，再创建新的 Workflow Smoke attempt；旧 attempt 始终保留为审计事实。

### 8.4 uncertain 与 OOM 特别边界

`submission_uncertain` 表示平台已进入提交边界，但尚未持久化 provider request ID；ComfyUI 可能已经接收任务。此时：

1. 停止自动或人工重复提交。
2. 按 attempt 时间、workflow/deployment 和服务端日志核对 ComfyUI queue/history/output。
3. 若远端仍运行，等待或在可控情况下取消；不得创建重复 GPU 任务。
4. 确认远端终态后，在版本验收页追加人工消歧结论；重新读取确认 `resolution_required=false` 后，才可按当前 validation 状态创建新的 smoke attempt。

管理员核对 ComfyUI history 后，在版本验收页对该 attempt 追加人工消歧结论。允许的结论只有“确认未提交”“确认远端失败”“确认远端成功”；远端终态必须关联该 attempt 的稳定 request id。未消歧前禁止创建任何新 attempt；相同结论重放幂等，不同结论冲突。人工结论永远不会把旧 attempt 改写为 passed，也不能直接补成发布证据；即使确认远端成功，仍需创建新的真实 smoke attempt 重新收集和校验。

当前没有单独的 OOM error code；远端 OOM 仍分类为 `provider_smoke_failed`。Comfy driver 会从标准 `execution_error` 只投影失败节点、异常类别和脱敏原因，页面可直接显示 CUDA/显存原因与恢复建议；history 结构未知或缺少该事件时才需要查看 ComfyUI History、控制台或日志。提交边界丢失仍可能表现为 `submission_uncertain`。

OOM 恢复：

1. 先按 uncertain 规则排除仍在执行或重复任务。
2. 清理竞争 GPU 工作负载，确认 ComfyUI 健康。
3. DynamicVRAM 已启用但采样临时张量仍因其他进程占用而 OOM 时，可通过 `scripts/restart.ps1 -RestartComfyUI -ComfyUIVramHeadroomGB <GB>` 增加 ComfyUI 的额外显存余量；该参数不改变工作流内容，代价是更激进的权重卸载与更慢的执行。
4. 若只需降低允许的参数，使用编辑器创建新的低成本 fixture attempt。
5. 若需要越过现有 Schema、减少节点或改变拓扑，回 ComfyUI 保存新 source revision 并导入新版本。
6. 只有确认队列无 running/pending prompt 时，才通过正式重启入口替换 GPU 进程；不手工杀进程。参数调整后创建新的 attempt，不重提旧失败 attempt。

## 9. 真实长 GPU 人工验收记录

本节必须在目标机器通过正式页面逐工作流填写。本次文档修改状态为：**未执行真实长 GPU smoke**。

| Workflow/version | Deployment | Validate | Fixture hash | Attempt/终态 | 真实提交 | 输出字节校验 | 时长 | OOM/uncertain 核对 | Publish |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 待现场填写 | 待现场填写 | 未验收 | 待现场填写 | 未运行 | 未验证 | 未验证 | 未测 | 未核对 | 未发布 |

每行至少保存：

- workflow version、validated deployment 和 execution contract hash；
- fixture hash、attempt ID、开始/结束时间和终态；
- `execution_submitted=true`；
- compiled graph hash、输出类型/数量及真实字节校验；
- OOM、取消、超时或 uncertain 的远端核对结论；
- 发布后刷新得到的 `published + is_current=true`。

不要记录凭据、内部绝对路径、原提示词、完整远端响应或敏感 request ID。

## 10. 完成判定

单个 saved workflow 只有同时满足以下条件才可报告可用：

1. fresh discover 的来源成功，并已保存精确 immutable candidate；scope/path/source/document hash 可复验。
2. 选定主输出后的 preview 已持久化 immutable contract draft；其 actionability 为 ready，projector/model/primary 与 projection SHA 完整。
3. promotion 只消费该 draft，已创建或精确复用 needs_review WorkflowTemplateVersion 与正式 provenance。
4. 参数 Schema、固定/动态素材槽、模型依赖和唯一主输出合同完整，并使用同一 active deployment 通过 Live Validate。
5. 在验收页选择并冻结本次 fixture，真实 Smoke attempt 终态 succeeded，输出媒体与资源证据有效。
6. 刷新版本后 validation/smoke evidence 与 attempt、fixture、compiled graph 和 target 一致；显式 publish 后仍为 `published + is_current=true`。

仓库测试、candidate、contract draft、preview、模型证明、Deployment Probe、旧版本或其他 ProductionRun 都不能替代上述逐版本真实证据。
