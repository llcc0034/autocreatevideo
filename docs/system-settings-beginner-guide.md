<!-- 此文件由 frontend/src/content/systemSettingsGuide.json 生成，请勿手工维护正文。 -->
# 系统设置新手图文指南

> 从看懂页面到完成一次可审计配置，按按钮、成功标志和故障恢复逐步说明

版本：1.3.0　更新日期：2026-09-01　页面入口：`/manual/settings`

## 如何使用本指南

适用对象：

- 第一次接触 AutoPrompt 的平台管理员
- 需要配置模型、工作流、提示词策略或运行主机的运营人员
- 只想复核已有配置、不希望误触高风险操作的审核人员

权限边界：

- 普通登录用户可以阅读本指南，但真正进入“系统设置”并修改配置需要平台管理员（staff）权限。
- 创建、轮换或停用受管凭据（SecretBinding）需要超级管理员（superuser）权限。
- 安全边界（Security Boundaries）对平台管理员只读；只有超级管理员可以新增定义、修改值、发布或激活相关配置修订。
- 系统设置的 Provider、Model、Deployment、Workflow、Recipe、PromptProfile、SecretBinding、默认值和安全边界列表共用同一搜索规则；已退役或停用记录默认隐藏，页面会显示隐藏数量，需要复核历史时再主动开启显示。
- 所有新增/编辑表单只要求填写有业务意义的字段；受控值从服务端 options 目录选择，版本、合同 Schema、排序及新记录生命周期等技术字段只读或由服务端分配。目录缺失或无效时停止打开和提交，不回退自由文本；仅 custom_entry_fields 明确授权时显示高级登记入口。
- 看见按钮不代表一定有权限；最终权限、依赖和状态都由服务端复核，页面不会伪装成功。

### 状态不是一回事

- **启用（active）**：对象允许参与选择，但不代表已完成真实调用验证。
- **已发布（published）**：版本已冻结，不能原地覆盖；后续变更要新建下一版。
- **已校验（validated）**：结构和绑定已通过在线校验，不等于真实生成已经成功。
- **烟测通过（smoke passed）**：用最小真实请求执行成功；证据仍要检查是否过期（fresh）。
- **待消歧（uncertain）**：远端是否接收请求无法确认。先查服务商控制台和账单，不能自动或人工直接重复提交。
- **建议警告（advisory warning）**：结果偏离推荐目标，但事实完整且仍可执行；记录到审核页供人工评估，不阻断主流程，也不消费失败重试次数。
- **待人工确认（awaiting approval）**：某个 required review 正在等待人工决定。只有 script_normalize_review 且 review_origin=human_after_validation_retry_exhausted 时，才表示字段严格合同失败已耗尽有限重试。
- **阻断（blocked）**：依赖、权限、预算或证据不完整，必须先处理页面给出的原因代码。

![系统设置八个分区总览](../frontend/public/help/system-settings/00-overview.jpg)

_图 0：系统设置总览。八个分区从“可执行资源”到“安全治理”组成一条闭环，不应只配置其中一张卡片。_

## 三分钟配置顺序

1. **先确认权限**：使用 staff 登录；只有处理秘密凭据时才切换到 superuser。
2. **先看运行主机**：选择目标主机并确认最近探测快照有效，避免在离线主机上继续配置。
3. **配置模型闭环**：Provider → 草稿模型版本/文件 → 发布 → Deployment → Probe → Smoke。
4. **配置工作流闭环**：扫描 → 纳管 candidate → 生成合同草稿 → 提升版本 → 在线校验 → 真实烟测 → 发布；candidate/draft 不能直接生产。
5. **配置提示词与路由**：发布 PromptProfile 和 Binding，再复核 Production Recipe 的阶段与精确路由。
6. **核对默认值**：只改确实需要的键，分别核对通用文本响应重试 production.text_contract_retry_limit 与提示词字段重试 production.prompt_field_retry_limit，并发布配置 Revision。
7. **复核安全边界**：确认真实工作流参数没有超过绝对上限。staff 只读复核；确需变更时由 superuser 单独审核、发布并重新验收。

> 探测（Probe）只确认健康与能力；真实烟测（Smoke Test）会提交真实低成本请求并可能产生费用。两者不能互相替代。

## 核心组件调用关系与推荐配置顺序

从上到下按推荐顺序配置。阶段内的组件可以并行准备；真正的调用依赖以图下方的“关键关系”文字为准。

- 必需：正式 ProductionRun 的主链路必须具备
- 按需：仅远端鉴权、本地执行或对应媒体阶段需要
- Probe 与 Smoke 是两个独立门禁，不能互相替代
- 纯配置接入（Configuration-only）：复用现有驱动能力、事实来源和已登记原语，只新增 schema v6 版本化资源
- 原语开发（Primitive-required）：只有出现现有原语无法解释的新外部协议、新确定性投影或新可执行校验时才需要代码

```mermaid
flowchart TD
  subgraph stage_1["01. 确认权限与运行基础"]
    direction LR
    node_platform_access["平台管理权限<br/>staff / superuser<br/>必需组件"]
    node_secret_binding["受管凭据<br/>SecretBinding<br/>按需组件"]
    node_runtime_host["运行主机<br/>RuntimeHost<br/>按需组件"]
  end
  subgraph stage_2["02. 完成模型能力闭环"]
    direction LR
    node_provider["服务提供方<br/>Provider<br/>必需组件"]
    node_model_profile["模型版本与能力合同<br/>ModelProfileVersion / Capability Contract<br/>必需组件"]
    node_model_artifact["模型文件证据<br/>ModelArtifact<br/>按需组件"]
    node_deployment["运行实例与 Smoke 证据<br/>Deployment / Probe / Smoke<br/>必需组件"]
  end
  subgraph stage_3["03. 并行准备生产合同"]
    direction LR
    node_workflow_candidate["工作流来源与合同草稿<br/>Candidate / Contract Draft<br/>必需组件"]
    node_workflow_projector["工作流合同方案版本<br/>WorkflowProjectorVersion<br/>必需组件"]
    node_workflow["工作流版本<br/>WorkflowVersion<br/>必需组件"]
    node_reference_contract["条件输入与动态参考合同<br/>Conditioning Contract / Dynamic Reference Topology<br/>按需组件"]
    node_controlled_adapter["引擎原语注册<br/>Projection Primitive Registry<br/>按需组件"]
    node_prompt_contract["模型无关提示词合同<br/>Model-neutral Prompt Contract<br/>必需组件"]
    node_prompt_binding["提示词运行时绑定<br/>PromptProfile Binding<br/>必需组件"]
    node_config_revision["生效配置修订<br/>Effective Configuration / Revision<br/>必需组件"]
    node_security_boundaries["安全边界<br/>Security Boundaries<br/>必需组件"]
  end
  subgraph stage_4["04. 冻结阶段与精确路由"]
    direction LR
    node_recipe["生产配方<br/>Production Recipe<br/>必需组件"]
  end
  subgraph stage_5["05. 执行就绪检查并准备项目输入"]
    direction LR
    node_readiness["生产就绪检查<br/>Readiness<br/>必需组件"]
    node_project_inputs["项目、剧本、分镜与引用素材<br/>Project / Script / Shot / Reference Asset<br/>必需组件"]
    node_reference_binding["显式引用绑定<br/>Explicit Reference Binding<br/>按需组件"]
  end
  subgraph stage_6["06. 创建运行并形成交付"]
    direction LR
    node_production_run["正式生产运行<br/>ProductionRun<br/>必需组件"]
    node_execution["阶段运行与生成尝试<br/>StageRun / GenerationAttempt<br/>必需组件"]
    node_layer_one_facts["已确认内容事实<br/>Reviewed Content Facts<br/>必需组件"]
    node_layer_two_fields["目标字段内容生成<br/>Prompt Field Generation<br/>必需组件"]
    node_prompt_validation["容量分批、字段校验、建议审计与有限重试<br/>Capacity Batching / Validation / Advisory Audit / Finite Retry<br/>必需组件"]
    node_human_confirmation["重试耗尽后人工确认<br/>Human Confirmation after Exhaustion<br/>按需组件"]
    node_source_projection["受控来源投影<br/>Controlled Source Projection<br/>必需组件"]
    node_prompt_profile_assembly["最终提示词确定性装配<br/>Deterministic Final Prompt Assembly<br/>必需组件"]
    node_reference_validation["引用合同严格校验<br/>Reference Contract Validation<br/>按需组件"]
    node_reference_preprocessing["引用素材预处理<br/>Reference Media Preprocessing<br/>按需组件"]
    node_video_generation["工作流媒体生成<br/>Workflow Media Generation<br/>必需组件"]
    node_delivery["产物、装配与交付包<br/>Artifact / Assembly / ReleaseBundle<br/>必需组件"]
  end
  node_platform_access -->|staff 才可维护平台配置| node_provider
  node_secret_binding -.->|按需：远端服务引用受管凭据| node_provider
  node_secret_binding -.->|按需：部署可覆盖 Provider 的凭据引用| node_deployment
  node_provider -->|选择连接驱动与服务来源| node_model_profile
  node_model_profile -.->|按需：本地模型补齐文件闭包| node_model_artifact
  node_runtime_host -.->|按需：按同一份新鲜主机快照证明文件| node_model_artifact
  node_model_profile -->|只部署已发布模型版本| node_deployment
  node_runtime_host -.->|按需：本地部署绑定 active 主机| node_deployment
  node_deployment -->|用于在线校验与真实烟测| node_workflow
  node_workflow -.->|按需：按模式冻结用途、配对和预处理数据| node_reference_contract
  node_model_profile -->|能力合同限定可匹配的 operation、model family 与 mode| node_prompt_contract
  node_controlled_adapter -.->|按需：declarative_v1 的 FieldRule 只能选择已登记代码原语| node_prompt_contract
  node_prompt_contract -->|先发布不可变三层合同，再建立运行时绑定| node_prompt_binding
  node_model_profile -->|按精确模型或模型族绑定| node_prompt_binding
  node_deployment -->|冻结阶段的精确执行路由| node_recipe
  node_workflow -->|冻结已发布工作流版本| node_recipe
  node_deployment -->|检查路由能力与新鲜烟测证据| node_readiness
  node_workflow -->|检查已发布工作流证据闭包| node_readiness
  node_config_revision -->|检查配置、Worker 与工具链依赖| node_readiness
  node_security_boundaries -->|超级管理员通过单独审核的系统修订发布高风险变更| node_config_revision
  node_security_boundaries -->|在执行前核对工作流与请求没有越过绝对上限| node_readiness
  node_recipe -->|按已发布配方收集生产要求| node_readiness
  node_readiness -->|执行前预检；创建时服务端再次复验| node_production_run
  node_prompt_binding -->|显式选择并冻结提示词策略| node_production_run
  node_config_revision -->|解析并冻结最终有效参数| node_production_run
  node_security_boundaries -->|创建前及外部执行边界持续按当前启用安全值失败关闭| node_production_run
  node_project_inputs -->|提供剧本、分镜和已确认引用| node_production_run
  node_reference_contract -.->|按需：页面渲染用途、说明、默认值与数量范围| node_reference_binding
  node_project_inputs -.->|按需：人工从受管素材中明确选择| node_reference_binding
  node_reference_binding -.->|按需：随所选 route 冻结显式 purpose key| node_production_run
  node_production_run -->|创建持久阶段与尝试记录| node_execution
  node_execution -->|生成并冻结已确认内容事实| node_layer_one_facts
  node_prompt_contract -->|提供冻结的逐字段生成与校验合同| node_layer_two_fields
  node_layer_one_facts -->|作为字段生成唯一事实输入| node_layer_two_fields
  node_layer_two_fields -->|对模型响应执行冻结字段合同校验| node_prompt_validation
  node_prompt_validation -.->|按需：硬合同失败且审计完整时有限重试；耗尽后转人工，建议警告不走此分支| node_human_confirmation
  node_layer_one_facts -->|提供已确认的通用事实来源| node_source_projection
  node_prompt_validation -->|final 值直接覆盖目标字段；contribution 交给 FieldRule 选择的受控原语| node_source_projection
  node_human_confirmation -.->|按需：人工补全并通过同一合同后恢复主链路| node_source_projection
  node_controlled_adapter -.->|按需：projection_primitive 只能调用已登记受控实现| node_source_projection
  node_source_projection -->|输出受控装配来源，最终语法仍由 Profile 决定| node_prompt_profile_assembly
  node_prompt_contract -->|提供冻结的字段顺序、通道、标签、分隔和空值规则| node_prompt_profile_assembly
  node_prompt_profile_assembly -->|冻结 PromptArtifact 作为视频输入| node_video_generation
  node_production_run -.->|按需：读取冻结 mode contract 与 SHA-256| node_reference_validation
  node_reference_contract -.->|按需：提供 cardinality、purpose、pairing 与 mutex| node_reference_validation
  node_reference_validation -.->|按需：校验通过后选择已注册 preprocessing adapter| node_reference_preprocessing
  node_reference_preprocessing -.->|按需：输出受控媒体输入，不改变用途语义| node_video_generation
  node_video_generation -->|媒体探测通过后装配并封存| node_delivery
  node_provider -->|受控读取 userdata；不授予执行权| node_workflow_candidate
  node_workflow_candidate -->|只有 ready 合同草稿可提升；不重读来源| node_workflow
  node_workflow_projector -->|可执行草稿和版本冻结精确 published 方案；不重解释历史| node_workflow
```

_关系图 A：核心组件从能力合同、模型无关提示词合同到正式交付的调用关系。节点可跳转到对应设置章节或真实案例；虚线“按需”关系表示只有新协议或对应执行方式才需要。_

### 不支持关系图时，按阶段阅读

#### 1. 确认权限与运行基础

先保证人有权限、服务有凭据、本地主机可连接。
- **平台管理权限（staff / superuser）**：staff 负责平台配置；只有创建、轮换或停用秘密正文时才需要 superuser。
- **受管凭据（SecretBinding）〔按需〕**：远端服务需要鉴权时创建；Provider 与 Deployment 只保存引用，秘密正文不回显。
- **运行主机（RuntimeHost）〔按需〕**：本地 ComfyUI 部署需要 active 主机和新鲜 Probe；纯远端服务可以不绑定主机。
- **完成标志**：staff 可进入设置；所需 SecretBinding 可用；本地 RuntimeHost 最近探测成功。
- **再继续**：基础条件明确后，才登记模型能力。

#### 2. 完成模型能力闭环

先登记连接方式，再冻结真实模型版本，最后建立可执行部署。
- **服务提供方（Provider）**：登记驱动、服务根地址和凭据引用；驱动、支持模态和声明操作从服务端注册目录选择，但仍只是说明性运营标记，不参与执行授权、能力兼容性校验或候选筛选。
- **模型版本与能力合同（ModelProfileVersion / Capability Contract）**：冻结真实 model identifier、媒体类型、业务 capabilities 和可选的通用 execution_monitoring；它与驱动注册能力、Deployment/Workflow 真实证据共同决定哪些 operation/mode 可以执行，Provider 的说明性运营标记不能授予能力。
- **模型文件证据（ModelArtifact）〔按需〕**：本地模型补齐文件角色、大小和 SHA-256；远端模型明确为不适用。
- **运行实例与 Smoke 证据（Deployment / Probe / Smoke）**：把已发布模型绑定到主机或远端服务；实例生命周期与 Smoke 证据分别展示，Probe 查健康能力，Smoke 提交最小真实调用。
- **完成标志**：ModelProfileVersion 已发布，Deployment active，Probe 与真实 Smoke 分别通过且证据 fresh。
- **再继续**：可执行部署就绪后，工作流与提示词策略才有可靠绑定目标。

#### 3. 并行准备生产合同

工作流、提示词策略和默认值可并行准备，但都必须在创建正式运行前发布或生效。
- **工作流来源与合同草稿（Candidate / Contract Draft）**：先把受控目录中的合法 JSON 冻结成不可执行 candidate，再用实时 `object_info` 生成不可变合同草稿。candidate 不要求目标 Deployment、published 模型或 projector；未解释项以 blocker 保留。
- **工作流合同方案版本（WorkflowProjectorVersion）**：candidate 纳管阶段不要求 Projector；要形成可提升的执行合同则必须唯一冻结一个 published 版本（generic fallback 也属于 Projector）。超级管理员版本化维护节点角色/连接、参数策略、模型文件角色和素材拓扑；Engine 自动锁定注册 adapter。具体 H3 T2VA/FL2VA/Ref2VA 是版本数据，发布新方案不会改写历史工作流。
- **工作流版本（WorkflowVersion）**：只有 `ready_for_executable_import` 草稿可提升；提升冻结节点图、参数 Schema、素材槽、主输出、模型和精确 projector identity，不重读本地源。随后依次完成 Live Validate、真实 Smoke、Publish。
- **条件输入与动态参考合同（Conditioning Contract / Dynamic Reference Topology）〔按需〕**：模型能力版本声明条件模式、用途、基数、pairing、mutex 和 preprocessing；冻结的 published projector 将其与实时节点动态插槽一致性核对后写入草稿/版本。页面只读，不接受人工填写 key、节点 ID 或 JSON。
- **引擎原语注册（Projection Primitive Registry）〔按需〕**：服务端代码只登记已实现、已测试的确定性原语；页面 FieldRule 选择原语并决定业务字段、顺序、格式和通道。配置页不能上传脚本、模板或自定义执行器。
- **模型无关提示词合同（Model-neutral Prompt Contract）**：PromptProfile 合同 schema v6 冻结已确认事实引用、逐字段内容生成/校验、source_projection_key、字段级 projection_primitive、受限 source_projection_config，以及最终提示词的顺序、通道、标签、分隔和长度合同。H3 六段是 declarative_v1 的可复制 Profile 实例，不是主编排硬编码。
- **提示词运行时绑定（PromptProfile Binding）**：发布 Profile 后，按能力合同中的 operation、model family、mode 和可选精确模型版本建立 Binding；Binding 不会给模型增加未声明能力。
- **生效配置修订（Effective Configuration / Revision）**：解析系统、项目和请求层默认值；每次变更形成不可变 Revision。
- **安全边界（Security Boundaries）**：保存仅 system 作用域的绝对资源上限。staff 只读，只有 superuser 可变更；命中时失败关闭，且不会被项目、Run、模型或工作流默认值放宽。
- **完成标志**：Workflow 已校验/烟测/发布；PromptProfile 与 Binding 已发布；Effective Configuration 可解释。
- **再继续**：工作流与精确执行路由写入 Recipe；Prompt Binding、有效配置和项目输入留到 ProductionRun 创建时共同冻结。

#### 4. 冻结阶段与精确路由

把阶段顺序、已验证的精确 Deployment/Workflow 路由和人工/自动门禁写入版本化配方；Recipe 不负责冻结项目输入或本次运行的有效配置。
- **生产配方（Production Recipe）**：冻结阶段顺序、执行模式、审批方式、变体数及精确 Deployment/Workflow 路由；不包含尚未创建的 Run 输入快照。
- **完成标志**：Production Recipe 已发布，阶段从 script_input 到 delivery 闭环，精确 route 可追溯。
- **再继续**：配方发布后，再用真实项目输入创建一次运行。

#### 5. 执行就绪检查并准备项目输入

先用 Readiness 汇总当前系统闭环，再准备剧本、分镜和明确用途的引用资产。
- **生产就绪检查（Readiness）**：按全部仍处于 published 的 Recipe 汇总部署、工作流、烟测、Worker、工具链和配置依赖；blocked 时先在配方与路由处理聚合原因。
- **项目、剧本、分镜与引用素材（Project / Script / Shot / Reference Asset）**：业务事实与生成输入的来源；系统不会猜测素材用途，也不会把空字段伪装成有效提示词。
- **显式引用绑定（Explicit Reference Binding）〔按需〕**：人工按当前 mode 合同选择素材与 purpose key；Shot 只保存安全标识符，不能自行解释或补默认用途。
- **完成标志**：目标配方 Readiness=ready；Project、Script、Episode、Shot 与 Reference Asset 数据完整，引用用途已人工确认。
- **再继续**：输入和所有系统合同同时就绪后，才创建 ProductionRun。

#### 6. 创建运行并形成交付

运行开始时冻结版本、路由、输入、参数和预算；先形成已确认内容事实，再按 PromptProfile schema v6 的逐字段来源与显式上下文合同取得或生成目标字段，declarative_v1 按每条 FieldRule 选择的代码原语产生 projection.output，最后按页面配置的顺序、格式和通道确定性装配，随后进入媒体与交付。
- **正式生产运行（ProductionRun）**：创建时共同冻结已发布 Recipe、Prompt Binding、最终有效配置、项目输入、外发授权、费用上限和媒体执行监控合同；通用文本响应重试与提示词字段重试次数也随运行冻结。
- **阶段运行与生成尝试（StageRun / GenerationAttempt）**：持久记录 queued、running、blocked、uncertain、awaiting approval、completed 等状态及恢复动作。
- **已确认内容事实（Reviewed Content Facts）**：生成并确认剧本、视觉规则、逐镜头叙事、Bible、对白、旁白/配音与声景；新 Run 的 script_breakdown 会显式物化 detailed_description、sound_effects 和 music，字段内容生成不会替空字段编造内容。
- **目标字段内容生成（Prompt Field Generation）**：field_definition.key/profile_field_key 是目标字段；generation_source_selector 从服务端 generation_source_selectors 目录选择主要内容来源，generation_context_selectors 是逐字段显式勾选的额外已确认信息。空数组只使用主要内容来源；相邻镜头和全剧不会隐式注入。generation_mode、generation_instruction、output semantics、语言、长度和受控 validators 全部读取冻结 PromptProfile，不在主逻辑写模型专属段名。
- **容量分批、字段校验、建议审计与有限重试（Capacity Batching / Validation / Advisory Audit / Finite Retry）**：prompt.normalize 先读取模型版本的四项必填容量、必填但可为空对象的 chat_request_options，以及可选 input_byte_warning_threshold 和 output_token_warning_threshold，再按 generation_mode 与原 request 顺序贪心分批；每批单独建立 GenerationAttempt，成功批不重跑。输入字节、字段长度、通道长度或完成 token 超过建议阈值只形成 warning，继续进入审核与装配，不消费失败重试。prompt.normalize 的受控字段合同失败按 production.prompt_field_retry_limit 处理；script.generate、script.structure 与 visual_rules.generate 已收到确定响应但被安全审计判为截断、缺少最终文本或不符合受控 JSON 外壳时，按 production.text_contract_retry_limit 处理。两类配置都只允许有限重试，且不把建议警告计为失败。
- **重试耗尽后人工确认（Human Confirmation after Exhaustion）〔按需〕**：对应硬合同的有限重试耗尽后转人工；失败输出不成为候选。prompt.normalize 的人工确认页提供全部冻结字段的空白候选，人工必须逐项补全并再次通过同一合同；通用文本响应失败则保留明确阻断原因，等待人工确认后决定是否修改输入、模型或策略再继续。
- **受控来源投影（Controlled Source Projection）**：Profile 通过 source_projection_key 选择 direct_v1 或 declarative_v1。direct_v1 直接使用通用事实；declarative_v1 读取每条 FieldRule 的 projection_primitive 并产生 projection.output。最终字段、顺序、标签、分隔符和通道仍由 Profile 页面规则决定。
- **最终提示词确定性装配（Deterministic Final Prompt Assembly）**：source_selector 指向装配来源；field_definition.key 是稳定目标字段。按冻结 Profile 配置的 position、channel、label_style、separator、max_chars 与空值策略组装 PromptArtifact。
- **引用合同严格校验（Reference Contract Validation）〔按需〕**：从冻结 mode contract 校验数量、用途、配对和互斥；缺合同或未知用途直接停止，不按模型名称猜测。
- **引用素材预处理（Reference Media Preprocessing）〔按需〕**：仅调用冻结合同注册的 video、paired audio、standalone audio adapter；素材入库只保存源事实，不预先套用 H3 CFR。
- **工作流媒体生成（Workflow Media Generation）**：把冻结的 PromptArtifact 和已批准引用交给所选兼容 Deployment/Workflow；H3 Ref2VA 是本指南案例，不是主链路固定后端。全自动与人工/辅助页面都读取冻结 Workflow 输入合同并由服务端复验素材数量。
- **产物、装配与交付包（Artifact / Assembly / ReleaseBundle）**：校验媒体后登记受管产物，装配分集母版并封存交付包。
- **完成标志**：已确认事实、字段来源/目标/生成/校验、source projection 和最终 PromptArtifact 均可追溯；ProductionRun 完成且 ReleaseBundle 可审计。
- **再继续**：完成后从 Run 快照和产物证据复盘；需要变更时新建版本或新 Run，不改写历史。

### 关键调用关系

- **平台管理权限 → 服务提供方**：staff 才可维护平台配置
- **受管凭据 → 服务提供方**〔按需〕：远端服务引用受管凭据
- **受管凭据 → 运行实例与 Smoke 证据**〔按需〕：部署可覆盖 Provider 的凭据引用
- **服务提供方 → 模型版本与能力合同**：选择连接驱动与服务来源
- **模型版本与能力合同 → 模型文件证据**〔按需〕：本地模型补齐文件闭包
- **运行主机 → 模型文件证据**〔按需〕：按同一份新鲜主机快照证明文件
- **模型版本与能力合同 → 运行实例与 Smoke 证据**：只部署已发布模型版本
- **运行主机 → 运行实例与 Smoke 证据**〔按需〕：本地部署绑定 active 主机
- **运行实例与 Smoke 证据 → 工作流版本**：用于在线校验与真实烟测
- **工作流版本 → 条件输入与动态参考合同**〔按需〕：按模式冻结用途、配对和预处理数据
- **模型版本与能力合同 → 模型无关提示词合同**：能力合同限定可匹配的 operation、model family 与 mode
- **引擎原语注册 → 模型无关提示词合同**〔按需〕：declarative_v1 的 FieldRule 只能选择已登记代码原语
- **模型无关提示词合同 → 提示词运行时绑定**：先发布不可变三层合同，再建立运行时绑定
- **模型版本与能力合同 → 提示词运行时绑定**：按精确模型或模型族绑定
- **运行实例与 Smoke 证据 → 生产配方**：冻结阶段的精确执行路由
- **工作流版本 → 生产配方**：冻结已发布工作流版本
- **运行实例与 Smoke 证据 → 生产就绪检查**：检查路由能力与新鲜烟测证据
- **工作流版本 → 生产就绪检查**：检查已发布工作流证据闭包
- **生效配置修订 → 生产就绪检查**：检查配置、Worker 与工具链依赖
- **安全边界 → 生效配置修订**：超级管理员通过单独审核的系统修订发布高风险变更
- **安全边界 → 生产就绪检查**：在执行前核对工作流与请求没有越过绝对上限
- **生产配方 → 生产就绪检查**：按已发布配方收集生产要求
- **生产就绪检查 → 正式生产运行**：执行前预检；创建时服务端再次复验
- **提示词运行时绑定 → 正式生产运行**：显式选择并冻结提示词策略
- **生效配置修订 → 正式生产运行**：解析并冻结最终有效参数
- **安全边界 → 正式生产运行**：创建前及外部执行边界持续按当前启用安全值失败关闭
- **项目、剧本、分镜与引用素材 → 正式生产运行**：提供剧本、分镜和已确认引用
- **条件输入与动态参考合同 → 显式引用绑定**〔按需〕：页面渲染用途、说明、默认值与数量范围
- **项目、剧本、分镜与引用素材 → 显式引用绑定**〔按需〕：人工从受管素材中明确选择
- **显式引用绑定 → 正式生产运行**〔按需〕：随所选 route 冻结显式 purpose key
- **正式生产运行 → 阶段运行与生成尝试**：创建持久阶段与尝试记录
- **阶段运行与生成尝试 → 已确认内容事实**：生成并冻结已确认内容事实
- **模型无关提示词合同 → 目标字段内容生成**：提供冻结的逐字段生成与校验合同
- **已确认内容事实 → 目标字段内容生成**：作为字段生成唯一事实输入
- **目标字段内容生成 → 容量分批、字段校验、建议审计与有限重试**：对模型响应执行冻结字段合同校验
- **容量分批、字段校验、建议审计与有限重试 → 重试耗尽后人工确认**〔按需〕：硬合同失败且审计完整时有限重试；耗尽后转人工，建议警告不走此分支
- **已确认内容事实 → 受控来源投影**：提供已确认的通用事实来源
- **容量分批、字段校验、建议审计与有限重试 → 受控来源投影**：final 值直接覆盖目标字段；contribution 交给 FieldRule 选择的受控原语
- **重试耗尽后人工确认 → 受控来源投影**〔按需〕：人工补全并通过同一合同后恢复主链路
- **引擎原语注册 → 受控来源投影**〔按需〕：projection_primitive 只能调用已登记受控实现
- **受控来源投影 → 最终提示词确定性装配**：输出受控装配来源，最终语法仍由 Profile 决定
- **模型无关提示词合同 → 最终提示词确定性装配**：提供冻结的字段顺序、通道、标签、分隔和空值规则
- **最终提示词确定性装配 → 工作流媒体生成**：冻结 PromptArtifact 作为视频输入
- **正式生产运行 → 引用合同严格校验**〔按需〕：读取冻结 mode contract 与 SHA-256
- **条件输入与动态参考合同 → 引用合同严格校验**〔按需〕：提供 cardinality、purpose、pairing 与 mutex
- **引用合同严格校验 → 引用素材预处理**〔按需〕：校验通过后选择已注册 preprocessing adapter
- **引用素材预处理 → 工作流媒体生成**〔按需〕：输出受控媒体输入，不改变用途语义
- **工作流媒体生成 → 产物、装配与交付包**：媒体探测通过后装配并封存
- **服务提供方 → 工作流来源与合同草稿**：受控读取 userdata；不授予执行权
- **工作流来源与合同草稿 → 工作流版本**：只有 ready 合同草稿可提升；不重读来源
- **工作流合同方案版本 → 工作流版本**：可执行草稿和版本冻结精确 published 方案；不重解释历史

### 阅读提示

- 这是推荐配置顺序，不表示所有组件只能串行创建；工作流、提示词策略、配置修订和项目素材可以并行准备。
- Recipe 只冻结阶段与精确执行路由；真正创建 ProductionRun 时，已发布 Recipe、Prompt Binding、最终有效配置和项目输入才会共同冻结到运行快照。
- 安全边界不属于可被 Project/Run 覆盖的建议值：只有 superuser 可修改，运行创建前和外部提交边界会按当前启用值重新检查，收紧后也保护已排队任务。
- Readiness 是汇总检查结果，不是执行器；正式创建 ProductionRun 时，服务端仍会重新核对依赖。
- 修改已发布版本、默认值或优先级不会静默改写历史 Run；要让新配置生效，应创建新版本或新的 ProductionRun。
- 有限重试分为两条独立策略：production.prompt_field_retry_limit 只处理 script_normalize 的两类已知字段响应校验失败，production.text_contract_retry_limit 只处理 script.generate、script.structure 与 visual_rules.generate 的已知安全响应拒绝；两者都要求完整审计，耗尽后转人工。内容过滤、uncertain、执行前门禁、审计缺失、Provider Smoke 失效和配置漂移不走自动重试。
- 能力合同（Capability Contract）先回答“模型能不能执行该 operation/mode”，提示词合同再回答“字段从哪里来、怎样生成校验、怎样装配”；PromptProfile 或 Provider 说明文本不能越权增加模型能力。
- 纯配置接入包括：新增 ModelProfileVersion/Deployment/Workflow、PromptFieldDefinition、PromptProfile rules、Binding、Recipe 和配置 Revision；前提是复用现有驱动 capability、事实来源及 direct_v1/已注册投影。
- 只有全新的 Provider 请求/响应协议、现有 projection primitive 无法表达的确定性转换，或新的可执行 validator 才需要开发、审查并登记代码原语；不要在 ProductionRun 主编排复制模型名分支，配置也不能执行脚本或任意代码。

## 关系图 B：从空项目到交付

这张图只描述一次短剧生产中的业务先后关系。先冻结已确认内容事实；再按 PromptProfile schema v6 的逐字段来源与显式上下文合同取得或生成目标字段；来源投影按 FieldRule 选择的原语产生装配值；最后由同一 PromptProfile 确定性装配。不要跳过文本 Bible、唯一实体锚点或 Shot 绑定，让视频模型自行猜测一致性。

```mermaid
flowchart TD
  node_production_empty_project["01. 新建空项目<br/>Empty Project"]
  node_production_ai_script["02. 生成已确认叙事事实<br/>Reviewed Narrative Facts"]
  node_production_text_bible["03. 生成文本 Bible<br/>Text Consistency Bible"]
  node_production_bible_candidates["04. 生成 Bible 候选图<br/>z-image-turbo Bible Candidates"]
  node_production_entity_anchors["05. 确认唯一实体锚点<br/>Canonical Entity Anchors"]
  node_production_shot_bindings["06. 绑定 Shot 实体与工作流素材<br/>Shot Bindings + Workflow Inputs"]
  node_production_prompt_field_generation["07. 生成目标字段内容<br/>Prompt Field Generation"]
  node_production_prompt_field_validation["08. 按容量分批、区分建议与硬校验并有限重试<br/>Capacity Batching / Advisory vs Required Validation / Finite Retry"]
  node_production_prompt_human_confirmation["09. 重试耗尽后人工确认<br/>Human Confirmation after Exhaustion"]
  node_production_prompt_compile["10. 来源投影与最终提示词装配<br/>Source Projection + Final Prompt Assembly"]
  node_production_h3_ref2va["11. H3 动态参考生视频<br/>H3 Dynamic Ref2VA"]
  node_production_artifact_selection["12. 选片<br/>Artifact Selection"]
  node_production_assembly["13. 装配分集母版<br/>Episode Assembly"]
  node_production_release_bundle["14. 封存交付包<br/>ReleaseBundle"]
  node_production_empty_project -->|读取已确认事实| node_production_ai_script
  node_production_ai_script -->|读取已确认事实| node_production_text_bible
  node_production_text_bible -->|读取已确认事实| node_production_bible_candidates
  node_production_bible_candidates -->|读取已确认事实| node_production_entity_anchors
  node_production_entity_anchors -->|读取已确认事实| node_production_shot_bindings
  node_production_shot_bindings -->|读取已确认事实| node_production_prompt_field_generation
  node_production_prompt_field_generation -->|读取已确认事实| node_production_prompt_field_validation
  node_production_prompt_field_validation -->|读取已确认事实| node_production_prompt_human_confirmation
  node_production_prompt_human_confirmation -->|读取已确认事实| node_production_prompt_compile
  node_production_prompt_compile -->|读取已确认事实| node_production_h3_ref2va
  node_production_h3_ref2va -->|读取已确认事实| node_production_artifact_selection
  node_production_artifact_selection -->|读取已确认事实| node_production_assembly
  node_production_assembly -->|读取已确认事实| node_production_release_bundle
```

_关系图 B：从空项目到 ReleaseBundle 的正式生产顺序。箭头表示事实依赖，不表示可以用测试动态视频替代剧情素材。_

### 不支持关系图时，按生产步骤阅读

1. **新建空项目（Empty Project）**：建立项目范围与生产目标；此时可以是 0 集、0 分镜。AI 创建必须填写集数、每集镜数和单集时长；导入剧本只有选中的配方含 script_structure/ai 时才显示并要求同一组结构化计划。
2. **生成已确认叙事事实（Reviewed Narrative Facts）**：从项目创意生成，或把已确认导入原文结构化为剧本、视觉规则、分集、逐镜头叙事、对白、旁白/配音和声景；服务端先把单集总时长预算与全部冻结文本 route 的镜头时长策略求交并冻结 structured_shot_duration_contract，冲突在任何模型调用前拒绝。新 Run 的 script_breakdown 同时显式物化 detailed_description、sound_effects 和 music，后续模型不得擅自补造缺失事实。
3. **生成文本 Bible（Text Consistency Bible）**：从剧本提取人物、场景、道具和影视规则；先确认文字实体，再生成图片。
4. **生成 Bible 候选图（z-image-turbo Bible Candidates）**：用 z-image-turbo 生成实体候选图；可用 Bible 选择的模型重新生成，也可从素材库选择。
5. **确认唯一实体锚点（Canonical Entity Anchors）**：每个人物、场景和道具各确认一张正式参考图，形成跨镜头共享的唯一视觉锚点。
6. **绑定 Shot 实体与工作流素材（Shot Bindings + Workflow Inputs）**：每个 Shot 显式绑定同一组实体锚点。动态视频合同来自 input_contract.conditioning_contract.modes[].dynamic_reference_topology.videos，并按当前 conditioning_mode 选中唯一模式：minimum_items=0 可留空，maximum_items=0 禁止选择，其他组合按 minimum_items..maximum_items 接受完整集合。人工、辅助和全自动三条线都执行冻结 route 的同一 0..N 合同，不再额外限制为最多一条。
7. **生成目标字段内容（Prompt Field Generation）**：field_definition.key/profile_field_key 定义目标字段。依次配置 generation mode、generation source、generation_context_selectors、generation_instruction、output semantics、required、language、length 和受控 validators。dialogue_metadata 不含对白原文；需要原文时显式选择 dialogue_authored_text，相邻/全剧上下文也必须显式开启。模型专属规则来自冻结 Profile 数据。
8. **按容量分批、区分建议与硬校验并有限重试（Capacity Batching / Advisory vs Required Validation / Finite Retry）**：服务端按模型版本 specification.prompt_normalization 的四项必填容量、必填但可为 {}的 chat_request_options，以及可选 input_byte_warning_threshold 与 output_token_warning_threshold 冻结批次计划。输入字节、length_advisory、warn_at_chars 和输出 token 建议阈值只形成可审计 warning，主流程继续；真实硬容量、required、JSON 形状、语言、受保护内容、未解析引用、Provider 截断/过滤等仍是硬合同。prompt.normalize 的硬失败批只在唯一 KNOWN_RESPONSE_REJECTED 审计完整时消费 production.prompt_field_retry_limit；script.generate、script.structure 与 visual_rules.generate 的受控确定响应拒绝消费 production.text_contract_retry_limit。两类策略都只重试失败批或当前失败阶段，重试耗尽进入人工确认；建议警告既不重试也不阻断。
9. **重试耗尽后人工确认（Human Confirmation after Exhaustion）**：对应硬合同失败耗尽上限后停止重试；失败输出不会伪装成成功候选。prompt.normalize 需要人工补全全部冻结字段并再次通过同一合同；其他文本阶段等待人工确认后再决定修改输入、模型或策略。首次即通过时自动跳过本步骤。
10. **来源投影与最终提示词装配（Source Projection + Final Prompt Assembly）**：先按 source_projection_key 选择 direct_v1 或 declarative_v1。声明式策略从每条 FieldRule 读取已注册 projection_primitive，产生 projection.output；再按冻结 Profile 的 source_selector、channel、position、label_style、separator、字段 max_chars、空值规则和 channel_limits 组装并校验 PromptArtifact。
11. **H3 动态参考生视频（H3 Dynamic Ref2VA）**：使用本次明确选择并冻结的 H3 动态 Ref2VA 双时钟 Workflow；尺寸、帧率、帧数和 steps 全部读取该 Workflow 合同。视频基数读取冻结的 minimum_items/maximum_items，因此合同允许 0 条时可以只使用 Bible 图片锚点；提示词读取本次冻结的 schema v6 Profile，按 declarative_v1 + projection.output + 六个 reference_context.* 原语形成六段。历史 0.2MP 参数只属于对应版本化验收案例，不是这里的默认值。
12. **选片（Artifact Selection）**：按镜头复核叙事、实体一致性、运动质量和媒体探测结果，选择正式变体。
13. **装配分集母版（Episode Assembly）**：按 Shot 顺序装配所选视频、音轨与时长，形成可探测的 Episode Master。
14. **封存交付包（ReleaseBundle）**：将母版、清单、校验值与运行快照封存为可审计交付事实。

### 生产边界提醒

- 原生 Ref2VA 会读取完整参考视频；purpose 只向提示词装配表达希望侧重的意图，不构成模型级内容隔离。正式生产必须选择与本剧同一故事宇宙、且人物、场景、道具不与目标镜头冲突的素材。三条生产线均按 conditioning_mode 读取冻结工作流 input_contract.conditioning_contract.modes[].dynamic_reference_topology.videos；人工/辅助逐镜页按所选 route 显示允许范围并在页面与服务端双重校验。
- metadata.fixture_scope=workflow_smoke 的测试视频只用于工作流版本验收页的真实烟测候选；普通素材列表、项目新建、分镜与 ProductionRun 选择器均不显示，服务端也拒绝把它冻结为生产输入。
- 某次版本化 0.2MP 验收案例中的图片和视频均使用 352×608 / 4 steps；这些数值只证明对应的精确 Workflow、fixture、target 与 attempt。实际操作必须分别读取本次选择并冻结的视频 Workflow 和 Bible 图片 Workflow，两套发布、校验、真实 Smoke 与路由证据不能互相复用。
- 共享实体锚点负责人物、场景、道具一致性；shot.narrative 是服务器组装 detailed_description 时使用的上游镜头事实，两者不能互相替代。
- 有限重试分为两条独立策略：production.prompt_field_retry_limit 只处理 script_normalize 的两类已知字段响应校验失败，production.text_contract_retry_limit 只处理 script.generate、script.structure 与 visual_rules.generate 的已知安全响应拒绝；两者都要求完整审计，耗尽后转人工。内容过滤、uncertain、执行前门禁、审计缺失、Provider Smoke 失效和配置漂移不走自动重试。
- H3 六段只是 declarative_v1 + projection.output + 六个 reference_context.* 原语的页面配置示例，不是通用主逻辑的固定分支；复用已有原语的新模型只需新增 schema v6 Profile/FieldRule/Binding。

## 模型（Models）

以模型为中心登记调用来源、能力版本、所需文件，以及最终由哪些运行实例执行。

设置深链：`/settings?section=models`

![系统设置按模型聚合的版本、文件证明和运行实例界面](../frontend/public/help/system-settings/01-models.jpg)

_图 1：模型分区。Provider 在独立弹窗中管理；左侧模型目录独立滚动，右侧只显示当前模型与精确版本，并在能力合同、文件与证明、运行实例三个页签间切换。_

### 这个部分具体做什么

- “能力合同”页签直接展示所选版本 `specification` 中的声明能力、模型族、素材模式与数量表，以及已声明的媒体规格、文本容量和执行监控限制。“用途、预处理与配对规则”可展开查看；其他扩展字段也按层级展示，“查看完整 JSON”保留原始规格核对入口。0–0 表示不支持，非零的相同上下限表示固定数量，不同上下限表示可变；图片总数约束各图片槽合计，不是额外素材槽。页面不补缺失字段或默认范围，也不把能力声明当成烟测结论；工作流实际可用范围仍以其冻结合同为准。此处只读，修改已发布合同需创建新模型版本。
- 服务提供方（Provider）登记调用驱动、基础地址和受管凭据引用；驱动、支持模态和声明操作从服务端注册目录选择，仍只是说明、展示和人工审核用的运营标记，不参与服务端执行授权、能力兼容性校验或候选筛选。
- 模型能力版本（Model Profile Version）冻结真实模型标识、媒体类型、操作能力和规格合同；它是模型侧的能力合同（Capability Contract），后续 Workflow、PromptProfile Binding 和 Recipe 都必须与其相容。
- 精确声明 prompt.normalize 的文本模型版本必须在 specification.prompt_normalization 冻结 max_input_bytes、max_output_tokens、max_items_per_batch、timeout_seconds 四项必填硬容量，并显式冻结 chat_request_options（模型无额外选项时填空对象 {}）；每个 max_input_bytes 只覆盖所属 operation 的完整冻结输入，不能跨 operation 复用；可选 input_byte_warning_threshold 与 output_token_warning_threshold 分别是输入字节和完成 token 的建议阈值，超过只告警，不拆批、不重试、不阻断完整响应。
- 精确声明 visual_rules.generate 的文本模型版本必须在 specification.visual_rules 冻结 max_input_bytes、max_output_tokens 和 timeout_seconds 三项硬合同，并显式冻结 chat_request_options（可为 {}）；可独立配置输入/输出建议阈值与 quality_advisories。quality_advisories 推荐主色数量、负面短语数量/长度、连续性摘要等内容长度及 recommended_negative_clauses；数量/长度命中只写候选 warning 与 advisory audit，建议短语只合并到可人工移除的候选。省略对象或字段时，不启用隐含建议或代码默认清单。它与 prompt_normalization 是两个按 operation 分离的规格对象，不能把一项的请求选项、容量或建议静默套到另一项。
- 媒体模型可在规格合同中声明执行监控（execution_monitoring）的默认静默时限和节点类型覆盖；模型层不能填写具体工作流节点 ID。
- 新建或编辑模型时，“特定节点等待时限（高级）”默认折叠，通常无需设置。需要时选择同一服务提供方下的启用 ComfyUI 实例读取节点目录，再选择节点类型并填写分钟数；目录来源只供查询，不给新模型建立运行实例关联。页面同时显示将保存的整数秒，规则仍写入同一 `specification.execution_monitoring.node_type_overrides`，不改变任务总超时。已有规则不因目录缺失或读取失败而清空；没有目录时不能添加新类型，未完成或重复规则不能保存。条数与时长上限取自当前服务端安全配置。移除全部规则后沿用生效的默认等待时限，0 不表示无限等待；完整模型规格 JSON 仍可用于高级核对并与表单同步。
- 模型文件（Model Artifact）记录本地模型文件的角色、相对文件名、大小和 SHA-256 证据；标准角色可留空库存选择器并由平台解析默认目录，自定义 ComfyUI 模型目录才需要显式节点选择器；远端模型可以明确为不适用。注册表不保存机器绝对路径，也不会移动或删除磁盘文件。
- 运行实例（Model Deployment）把已发布模型版本绑定到运行主机或远端服务，并保存探测与真实烟测证据。页面把它收进所属模型版本，但不会合并后台对象或生命周期。active/error 可停用；无证据和引用的 disabled 空记录可删除，其他 disabled 记录应在替换活动引用后退役保留历史。
- 直接调用的运行实例按每个能力展示烟测状态、证据时间和到期时间，并汇总有效通过项数。通过且有效、已过期、配置已变更、失败和未烟测分别展示；最近任务排队或执行中不会覆盖仍然有效的证据。可点击重新读取能力烟测更新状态。ComfyUI 实例继续按关联工作流分别验收。
- ComfyUI 部署可以配置通用的“新提交前资源释放”合同：只在全新的 POST /prompt 前调用 /free，不按模型名称分支；恢复、轮询和收集旧请求不会触发。

### 操作前准备

- 准备 Provider 的正式基础地址；不要把 Token、密码或 Secret 粘贴到普通文本或高级 JSON。
- 本地 ComfyUI 模型先确认运行主机已登记并可探测；远端文本或异步接口准备好对应 SecretBinding。
- 先确认注册驱动实际支持的 capabilities，再在 ModelProfileVersion 声明业务能力，例如剧本生成（script.generate）、剧本结构化（script.structure）、提示词规范化（prompt.normalize）、影视规则生成（visual_rules.generate）或参考生视频（video.reference_to_video）。

### 逐步操作

#### 1. 新增服务连接

- **怎么做**：点击“管理服务连接”，再点“新增服务连接”，填写唯一键、名称、基础地址和超时；驱动类型使用服务端单选，“支持模态 / Supported modalities”和“声明操作 / Declared operations”使用该注册驱动给出的多选项。需要凭据时只选择受管凭据绑定（SecretBinding）引用。运营标记不会被当成执行授权。
- **成功标志**：服务连接区出现新 Provider，状态为启用（active），详情中不出现秘密正文；驱动、模态和传输操作没有自由文本旁路。
- **卡住时**：若模型选项目录读取失败，新增/编辑会失败关闭而不是回退自由输入；重新读取后再操作。若提示基础地址、请求映射或终态缺失，按字段旁说明补齐。

#### 2. 建立草稿模型版本

- **怎么做**：点击页面主按钮“新建模型”，填写新的模型版本族标识（Profile Key）和真实 model identifier；Provider、主模态、能力列表和提示词策略族均从服务端目录选择。新模型可在选择器中显式选择由合法 Profile Key 派生的同名新策略族，其他自定义文本会被拒绝。版本号及模型规格结构版本由服务端分配，不填写。已有模型只能从当前最高 published/retired 版本点“新建下一版本”；服务端校验源合同完整性后，按该族全部历史（包括退役版本）原子分配下一号。最高版本已是草稿时直接继续编辑。能力多选加入 prompt.normalize 或 visual_rules.generate 后，页面才显示各自的类型化容量合同。
- **成功标志**：模型卡片内出现由服务端分配编号的草稿版本；模型版本族和版本号锁定，完整 specification 的大小与 SHA-256 一致。发布后调整合同必须再次新建下一版本并对新 target 重跑真实 Smoke。
- **卡住时**：选项目录、Provider 或必填能力合同缺失时页面失败关闭；旧源或并发创建由服务端返回冲突，刷新后读取实际版本，不要人工改号。建议阈值越界、选项含敏感/保留字段或真实硬合同失败时按服务端错误修正。

#### 3. 声明并证明本地模型文件

- **怎么做**：在左侧模型目录选择目标模型，再在右侧选择草稿版本并切换到“文件与证明”页签，点击“新增文件声明”；文件角色从服务端列表选择，再填写逻辑名称、相对文件名、预期 SHA-256 和字节数。使用 checkpoints、diffusion_models、text_encoders、vae、loras 等标准角色目录时，把库存目录（Inventory folder）、库存节点类型（Inventory node type）和库存节点输入名（Inventory input name）三个选择器全部留空；只有自定义 ComfyUI models 子目录才同时填写真实 object_info 节点选择器。随后在已保存工作流审阅卡片中点击“证明所需模型文件”。
- **成功标志**：证明任务只创建一次并进入 queued/running，完成后该版本的文件区显示 available，且 size/SHA 与同一份主机快照一致；标准角色留空不会被误判为目录不一致。
- **卡住时**：三个库存字段不能只填一个；自定义目录必须同时有 inventory_folder、inventory_node_type 和 inventory_input_name，三个字段要成对成组填写。若提示主机已有探测任务，等待当前任务结束，不要重复点击；大型模型可达几十 GB，证明可能需要数分钟。

#### 4. 发布模型版本

- **怎么做**：在草稿版本点击“核对并发布”。本地模型必须先形成完整文件闭包；远端模型不要伪造本地文件。
- **成功标志**：状态变为已发布（published）；编辑按钮消失，只能退役或新建下一版。
- **卡住时**：发布失败通常是能力合同、配置默认值、文件角色或 SHA/大小证据不完整；按服务端错误回到草稿修正。

#### 5. 新建运行实例

- **怎么做**：在左侧模型目录选择目标模型，再在右侧选择已发布版本并切换到“运行实例”页签，点击“新增运行实例”；模型版本会精确预选。本地模型选择 active 运行主机，远端模型可不选主机；设置优先级和运行时配置。
- **成功标志**：运行实例出现在所属版本内，实例状态为启用（active），但“Smoke 证据”仍可能显示未执行。
- **卡住时**：若模型版本不是 published、主机未 active 或凭据不可用，先处理依赖，不能用默认第一项替代。

#### 6. 维护路径与清理历史

- **怎么做**：模型目录与右侧详情分别滚动；右侧只显示一个精确版本，并用能力合同、文件与证明、运行实例三个页签切换。搜索只缩小左侧目录，点击命中模型后才定位到实际命中的版本和页签，不会在输入时静默切换对象。draft 文件声明改变相对路径/库存选择器/大小/SHA 后重新探测和核验；published 文件新建更高模型版本。先停用实例，仅无证据/引用的误建空记录可删除，其余记录应退役保留历史。
- **成功标志**：连续切换模型、版本和页签不会增加主页面高度；旧观测、Smoke 和生产引用仍可审计，页面清理不会移动或删除真实模型文件。
- **卡住时**：删除返回冲突表示已有核验、主机观测或引用，不要清数据库绕过；按提示改走新版本、替换引用或退役流程。

#### 7. 按需配置 ComfyUI 新提交前资源释放

- **怎么做**：仅对需要在同一 GPU 上切换模型的 ComfyUI Deployment，填写四个类型化字段：合同版本=1、启用=true、卸载已加载模型=true、回收空闲显存=true。四项必须一次填完整；不要把它写到模型名称或工作流专用代码中。保存后 Deployment target SHA 会变化。
- **成功标志**：新任务通过校验后、冻结 submission token 与调用 POST /prompt 前，worker 先调用一次 POST /free；成功或失败形成受限审计。已提交任务的恢复、poll、collect 不调用 /free。
- **卡住时**：该操作会改变 ComfyUI 进程的全局 GPU 状态，因此 TASK_MAX_CONCURRENT 必须为 1；否则系统在任何 /free 或 /prompt 前明确失败。/free 被拒绝或响应无法确认时也 fail closed，不会自动重复提交。修改合同后必须重新 Live validate、真实 Smoke 并发布 Workflow。

#### 8. 区分实例状态、探测与真实烟测

- **怎么做**：在具体运行实例上先点“探测（Probe）”确认健康和能力，再点“真实烟测（Smoke）”提交真实请求。页面分别显示实例生命周期和“Smoke 证据”，不能因 active 就推断 Smoke 已通过。script.generate、script.structure、prompt.normalize 与 visual_rules.generate 是四项独立文本 operation；prompt.normalize 会按当前已发布生成字段 Profile 组装代表性完整合同，visual_rules.generate 会按自己的严格 JSON 输出合同烟测。真实硬容量、批次数量、结构、引用和 Provider 终止原因属于硬验证，输入/输出建议阈值与建议长度只进入警告审计。任何 ModelProfileVersion 规格、凭据或 Deployment 目标变化都会改变 target SHA。
- **成功标志**：Probe 形成能力快照；四项文本 operation 的 Smoke 分别通过并绑定当前 target SHA。页面显示本次选择的已发布 ModelProfileVersion，并按 operation 读取 prompt_normalization、visual_rules 等各自冻结的硬容量、建议阈值和 chat_request_options；即使属于同一模型版本，也不能假定不同 operation 的规格或选项相同。
- **卡住时**：uncertain 先人工消歧；超过真实硬容量、结构、未声明引用、截断或内容过滤等硬失败需修复源合同或模型配置。不要把建议范围越界当成失败，也不要复用旧 hash 证据。

### 通用 prompt.normalize 容量与 Chat Completions 选项实例 / Generic Prompt Normalization Options

```json
{
  "prompt_normalization": {
    "max_input_bytes": 65536,
    "input_byte_warning_threshold": 16000,
    "max_output_tokens": 131072,
    "output_token_warning_threshold": 32768,
    "max_items_per_batch": 1,
    "timeout_seconds": 600,
    "chat_request_options": {
      "thinking": { "type": "enabled" },
      "reasoning_effort": "low"
    }
  }
}
```

### 当前 visual_rules.generate 容量与 Chat Completions 选项实例 / Current Visual Rules Options

```json
{
  "visual_rules": {
    "max_input_bytes": 65536,
    "input_byte_warning_threshold": 16000,
    "max_output_tokens": 131072,
    "output_token_warning_threshold": 32768,
    "timeout_seconds": 600,
    "quality_advisories": {
      "schema_version": 1,
      "base_colors_min_items": 1,
      "base_colors_max_items": 3,
      "negative_constraints_min_items": 0,
      "negative_constraints_max_items": 32,
      "negative_constraint_max_chars": 120,
      "recommended_negative_clauses": [
        "identity drift",
        "face drift",
        "age drift",
        "hairstyle change",
        "wardrobe change",
        "prop mutation",
        "inconsistent architecture",
        "duplicate person",
        "extra limbs",
        "extra fingers",
        "deformed hands",
        "readable text",
        "subtitles",
        "watermark",
        "logo",
        "flicker",
        "jitter",
        "camera teleport",
        "low detail"
      ],
      "continuity_summary_max_chars": 3000,
      "negative_prompt_max_chars": 8000,
      "color_script_max_chars": 2000
    },
    "chat_request_options": {
      "thinking": { "type": "disabled" }
    }
  }
}
```

### ComfyUI 新提交前资源释放合同 / Pre-submit Resource Release

```json
{
  "pre_submit_resource_release": {
    "schema_version": 1,
    "enabled": true,
    "unload_models": true,
    "free_memory": true
  }
}
```

### 字段怎么理解

- **模型版本族与提示词策略族**：Profile Key 是模型记录跨版本的稳定身份；model_family 是 PromptProfile 匹配使用的策略族。两者不是同一字段。新建下一版本时前者锁定，后者从服务端目录选择；新模型可在选择器中显式选择与合法 Profile Key 同名的新策略族，除此之外不接受自定义文本。
- **版本号与规格结构版本**：两者都由服务端维护：首次创建固定为 v1，下一版按该模型族全部历史原子递增；规格结构版本使用当前受支持值。浏览器只读显示，不接受人工输入。
- **优先级（priority）**：数值越高越适合推荐/默认目录，但不会覆盖已发布 Recipe 中冻结的精确路由。
- **默认部署（is_default）**：用于目录展示与推荐，不代表生产运行可以忽略显式选择。
- **运行时配置（runtime_config）**：空对象表示继承 Provider；只放非秘密的驱动扩展字段。ComfyUI 的 pre_submit_resource_release 是部署级、版本化的通用执行合同，不属于模型名或工作流名。
- **新提交前资源释放（pre_submit_resource_release）**：严格四字段合同：schema_version、enabled、unload_models、free_memory；没有隐藏默认值。启用时至少选择一项动作，停用时两项动作都必须为 false。它只隔离新提交前的 GPU 状态，不能替代合理的分辨率、帧数、时长和工作流容量设计。
- **Provider 支持模态 / 声明操作（Supported Modalities / Declared Operations）**：它们从当前注册驱动目录选择，只是说明、展示和人工审核用的运营标记，不参与服务端执行授权、能力兼容性校验或候选筛选。真实能力来自注册驱动 capabilities、已发布 ModelProfileVersion、Deployment/Workflow 合同和新鲜真实 Smoke；历史任意输入文本不会注册驱动或增加驱动能力，也不会执行代码、生成候选或绕过门禁。
- **能力合同（Capability Contract）**：由注册驱动能力、ModelProfileVersion capabilities、Deployment/Workflow 合同和当前 Probe/Smoke 证据共同形成。它先回答模型能否执行 operation/mode；PromptProfile 只约束提示词内容，不能授予能力。
- **提示词规范化合同（specification.prompt_normalization）**：精确属于一个已发布 ModelProfileVersion；四项硬容量与 chat_request_options 必填，无扩展时选项对象填 {}；input_byte_warning_threshold 和 output_token_warning_threshold 可选且只告警。max_input_bytes/max_output_tokens 仍是真实硬上限，Provider 明确返回 length 代表已截断，仍属硬失败。
- **受控请求选项（chat_request_options）**：必须显式冻结、纯 JSON 的模型版本请求合同；可为 {}，也可表达服务商已支持的非敏感顶层选项，例如 temperature、response_format、thinking.type 与 reasoning_effort。它不能覆盖 model、messages、max_tokens、stream、seed、timeout_seconds、endpoint 或请求包装字段，不能包含凭据；大小和结构上限来自当前安全配置。
- **当前已选 prompt.normalize 合同**：从本次选择的已发布 ModelProfileVersion 读取 prompt_normalization 四项硬容量、必填 chat_request_options 和可选建议阈值，并冻结到运行快照；不要复制退役版本或文档案例中的数值。
- **当前已选 visual_rules.generate 合同**：从本次选择的已发布 ModelProfileVersion 独立读取 visual_rules 的硬容量、必填 chat_request_options、可选建议阈值和 quality_advisories，并冻结到运行快照。quality_advisories 的数量/长度命中只写候选 warnings/advisory audit；recommended_negative_clauses 只在配置存在时合并到候选且人工可移除。省略 quality_advisories 对象或字段就不启用隐含建议或代码默认清单。数组类型、允许枚举、唯一性、可打印英文/控制字符、来源回显与绝对 JSON/请求资源上限仍硬阻断。主逻辑只按 operation 读取冻结规格，不按模型名、版本号或另一 operation 的值推断。
- **执行监控合同（Execution Monitoring）**：纯数据的长节点静默窗口规则。模型层可配默认值和精确 class_type（允许空格/括号）；不会按模型名称写分支，也不会改变任务不可续期的 hard deadline。
- **模型库存选择器（Model Inventory Selector）**：用于证明 ComfyUI 实际能枚举到模型文件。标准角色目录的三个选择器字段全部留空，由平台按角色映射；自定义目录才填写 inventory_folder，并且 inventory_node_type 与 inventory_input_name 必须成对来自真实 object_info，主逻辑不会按模型名称猜测。filename 始终是受控 models 根下的安全相对文件名。
- **Deployment 退役（retired）**：不可逆的只读生命周期终态，用于保留已有 probe、Smoke、可用性和运行证据；它不会继续参与新路由，也不是删除物理模型文件。
- **证据有效（fresh）**：烟测证据仍在有效期内且相关配置未漂移。

### 新手最常见卡点

- **Provider 显示 configured，但调用仍失败**：configured 只表示已绑定凭据；同时检查 SecretBinding active、owner active、版本可解密和最新 Probe/Smoke。
- **在 Provider 的受控操作中输入新 operation，以为能力已经启用**：该输入只是运营说明，不会给驱动增加能力，也不会进入服务端候选筛选。先检查驱动注册能力，再在模型版本声明业务 capability，并通过 Deployment/Workflow 的真实烟测；不要把输入框内容当作可执行证据。
- **想在模型版本或工作流页面填写 Comfy 节点 10**：模型版本不认识具体 graph，工作流版本页也不提供 node_overrides。公共规则只能按受控节点类型选择；确需节点专属规则时，由注册 workflow-family projector 从新的 ComfyUI 源修订确定性派生。
- **模型文件在主机上存在，页面却不能发布**：先重新探测主机，再按同一份新鲜快照核验 Model Artifact；文件存在不等于数据库已有可信证据。
- **移动本地目录后仍显示旧可用状态，或核验没有候选**：旧状态是历史核验，不代表当前主机。只迁移 ComfyUI 根时更新 COMFYUI_OUTPUT_DIR/端点并重启，再 Probe/Verify；相对路径或文件内容改变时，draft 更新声明并重新证明，published 新建更高版本。平台不会猜新路径或回退旧快照。
- **模型文件或部署删除按钮不可用/删除返回冲突**：published/retired 文件和有证据的部署必须保留。文件只删 pristine draft 声明；部署先停用，无证据空记录才删除，其余在替换活动引用后退役。
- **添加 chat_request_options 后仍想复用旧 passed Smoke**：选项已改变 ModelProfileVersion specification SHA 和 Deployment target SHA。保留旧证据做历史审计，但必须在当前版本与目标上重跑所需 operation 的真实 Smoke；不能手工改 fresh 状态。
- **开启提交前资源释放后仍出现 OOM**：先区分工作负载本身与模型残留。资源释放只处理新提交前的模型/空闲显存隔离；过长时长、过多帧、分辨率或工作流临时张量仍可能超过显存。用目标合同的新任务重新实测，不要把旧长任务 OOM 单一归因于未释放。

## 工作流（Workflows）

把 ComfyUI 工作流冻结为版本化结构合同，并用真实素材和参数证明它可以执行。

设置深链：`/settings?section=workflows`

![系统设置工作流分区双语界面](../frontend/public/help/system-settings/02-workflows.jpg)

_图 2：工作流分区。来源 candidate、执行合同草稿、版本提升、在线校验、真实烟测和发布是不同门禁。_

### 这个部分具体做什么

- 扫描只发现受控目录文件；显式纳管后形成不可执行 candidate。candidate 可在目标模型、执行 Deployment 或 projector 尚未就绪时保留来源证据。
- 选定主输出后的 preview 会持久化不可变合同草稿；只有 `ready_for_executable_import` 草稿可提升为工作流版本。提升只消费草稿，不重新读取本地文件或 `object_info`。
- 工作流版本冻结节点图、参数 Schema、素材槽数量和媒体类型、模型要求、主输出和精确 `WorkflowProjectorVersion` identity；冻结合同只读，但版本页提供显式 Live Validate、Smoke 和发布动作。
- 条件输入合同（Conditioning Contract）由模型能力版本与 frozen projector 共同投影。动态参考拓扑保存用途、基数、默认值、配对、互斥和预处理 adapter；页面与服务端读取同一合同，不接受人工 JSON。
- 执行监控合同（execution_monitoring）的公共规则通过模型版本类型化选项维护；确需节点专属规则时只由注册 projector 派生。新 Run 会冻结合并后的合同和 SHA-256。
- 在线校验（Live Validate）核对 Deployment、object_info、节点和语义绑定。
- 真实烟测（Smoke Test）用一次明确的 fixture 绑定真实素材和值并运行，不会改写版本合同。

### 操作前准备

- 确保平台可以通过受控 source scope 读取 ComfyUI userdata；内部 source Deployment 只是目录和 `object_info` 的传输代理。目标执行 Deployment、published 模型与文件证明可在 candidate 之后、Live Validate 之前补齐。
- 工作流文件必须来自系统配置 workflow.comfyui_saved_workflow_directory 指定的 ComfyUI userdata 相对目录；初始值为 workflows，也可收窄到 workflows/team-a 等子目录。这里不能填写 Windows/Linux 绝对路径；原始 UI JSON 请自行保管。
- 准备满足当前条件模式合同的最小烟测素材。在只读验收页选择当前 conditioning_mode，再查看 dynamic_reference_topology 的数量、用途、时长、帧率、配对和互斥；没有合同就停止，不能套用 H3 默认值。下方只读证据示例来自某次 H3 版本化验收；这些数字不是平台默认值。

### 逐步操作

#### 1. 扫描并纳管来源 candidate

- **怎么做**：核对 `userdata/<有效目录>` 和同步预算，执行扫描；再勾选需要保留的合法文件并执行“纳管来源候选”。平台按 Provider、运行主机、规范化 endpoint 与有效目录去重扫描。
- **成功标志**：行内出现精确 candidate source ID、source/document SHA 与“已纳管候选”。这一步只检查受控目录、JSON、大小和敏感字段，不要求模型已发布、目标 Deployment 或 projector。
- **卡住时**：目录/JSON/大小/敏感字段错误先修来源。`source_scope_changed` 重新扫描；本地文件变化后纳管新的 revision，不覆盖旧 candidate。请求 `partial` 时逐项检查 result/code/detail。

#### 2. 生成平台执行合同草稿

- **怎么做**：先对已纳管 candidate 读取主输出候选；从服务端候选中选择唯一主输出后再次 preview。可选择 draft/published 模型版本作为解释上下文，但不能手填节点或 projector。
- **成功标志**：出现 contract draft ID/revision、projection SHA、converted graph/parameter/structural SHA、projector identity、`admission_tier` 与 `actionability`。preview 在选定主输出后会写入/复用不可变 draft，不是纯只读动作。
- **卡住时**：`candidate_only` 不是来源导入失败；按 blockers、`live_validate_requirements` 和 recovery actions 补模型、projector 或运行证据。超级管理员也可以对已经由 fallback 自动匹配、甚至已经提升为工作流版本的候选点击“基于此候选生成专用 Projector 建议”。若结构无法确定图片/视频/音频/文本，先在页面确认产物类型。建议只填入未保存表单，不会发布或执行工作流。未选主输出时不会创建 draft。

#### 3. 提升为工作流版本

- **怎么做**：只有 `ready_for_executable_import` draft 可以选择新模板或追加到原模板，再提交提升。请求携带精确 draft、candidate/source scope/hash、模型、主输出和四类审阅 SHA。
- **成功标志**：结果显示已创建/复用 needs_review 工作流版本及正式 provenance。服务端从冻结 draft 提升，不重新读取本地文件或实时 `object_info`。
- **卡住时**：`workflow_contract_draft_mismatch` 表示 draft 与请求身份/SHA 不一致；`workflow_contract_blocked` 表示仍有结构阻断。不要修改浏览器 JSON，回到 candidate 生成新 draft。HTTP 200 仍逐项检查结果。

#### 4. 新建与调整工作流合同方案

- **怎么做**：普通用户只读查看草稿/工作流冻结的 projector key、version、definition SHA 和 engine identity。超级管理员在“工作流合同方案目录”按工作流识别、输出与模型、节点与连接、参数与素材、执行引擎与接线等分区编辑；候选建议提供节点类型和输入名选项，完整 JSON 只在最后的高级复核区。新图族创建 family + v1 草稿，已有图族调整则创建后继草稿。误建的最高、未发布且未被合同草稿引用的版本可点击“放弃草稿”，经二次确认和审计原因后删除；已发布版本和历史引用不能删除。新建提供三种初始化：从工作流推荐（默认）、复制已有方案、空白新建。空白新建只选择注册引擎，不依赖已有版本，也不夹带旧业务规则；三种方式共用一个编辑器。方式选择不会执行扫描；无候选时点击“前往扫描工作流”，在扫描区显式扫描、纳管并确认主输出，再点击“生成推荐配置”。扫描区的“返回新建方案”保留当前页表单内容，关闭或刷新页面不保留。Engine 自动锁定 adapter 和 projection 类型；切换前确认覆盖未保存的修改。对于已经导入的候选，先生成并调整专用建议、保存并发布方案，再点击“用当前已发布 Projector 生成后继合同”，核对后提升到原模板的新工作流版本。
- **成功标志**：每个新投影精确引用一个 current published projector；旧工作流仍引用原版本。
- **卡住时**：未知/歧义 projector 只形成 `candidate_only` draft。候选建议中的必需节点来自单份快照，optional node、长期语义 key/name、关键 discriminator 和发布优先级仍需人工确认。已有 fallback 命中不是生成专用建议的阻断条件，也不要为了显示推荐入口而停用 fallback。发布新 projector 后，可从同一不可变 candidate 重新生成 draft 并 promote 新版本；只有 saved workflow 字节变化时才重新扫描并纳管新的 candidate revision。旧 draft/version 不会被重解释或覆盖。

#### 5. 配置长节点静默窗口

- **怎么做**：在模型能力版本的类型化执行监控字段中配置公共 default 和节点类型覆盖，例如 SamplerCustomAdvanced=5400 秒；工作流版本验收页只读展示最终冻结结果，不接受节点 ID 或 JSON 手填。
- **成功标志**：字段校验通过，时限为 30–86400 的整数秒；新模型版本发布并被新工作流版本引用后，页面明确展示节点切换与硬时限语义。
- **卡住时**：已发布模型或工作流版本不能原地改；先创建下一模型版本，或为确需节点专属适配的 compiler 增加受审合同实现，再从 ComfyUI 新源修订导入并创建新 Run。

#### 6. 按冻结合同选择本次参考素材

- **怎么做**：打开工作流版本验收页只读核对当前 conditioning mode 的图片、视频、配套音频和独立音频范围。实际生成时，在素材新增、ProductionRun 或本次 Smoke 表单中使用“添加/移除”控件，按 0..N、1..N 或 N..N 范围选择素材；不要修改合同 JSON。
- **成功标志**：页面与服务端按同一冻结 mode 展示并复验实际数量、用途、配对和互斥规则；任务创建后冻结完整节点参数、本次素材绑定、mode contract 与 SHA-256。
- **卡住时**：若现有范围或拓扑不适用，停止提交；创建并发布新的 WorkflowProjectorVersion（只有现有受审原语无法表达时才开发新 engine/adapter）。来源字节未变时直接复用同一 candidate 重新 draft → promote；只有 ComfyUI 工作流源本身变化时才保存新 revision 并重新 scan → candidate。随后完成 Live Validate → Smoke → publish。

#### 7. 核对条件输入合同与动态参考拓扑

- **怎么做**：在只读验收摘要中核对服务端返回的 conditioning mode、dynamic_reference_topology、purpose、默认用途、pairing、mutual exclusion 和 preprocessing；运行页面只提供这些受控选项，不允许输入任意 key、节点 ID 或 adapter。H3 数量与 24fps 仅是对应版本案例，不是平台默认。
- **成功标志**：素材新增、分镜与运行页按 conditioning mode 显示同一用途名称、说明、默认值和范围；ProductionRun 冻结 conditioning contract、选中 mode contract 与 SHA-256，执行器使用注册 adapter。
- **卡住时**：视频 route 缺 conditioning_contract、adapter 未注册或动态拓扑不完整都会直接阻断；回到模型/工作流权威来源创建新版本，不给 Shot 猜默认用途，也不在页面手改高级 JSON。

#### 8. 在线校验

- **怎么做**：打开版本验收与发布页，选择兼容 Deployment，复核只读节点绑定和参数合同后执行在线校验。
- **成功标志**：校验状态为已通过，保存 validated Deployment 和 object_info 证据。
- **卡住时**：若模型路径只是 `/` 与 `\\` 的目录分隔符写法不同，平台会按同一安全相对路径识别，并冻结目标 `object_info` 的唯一精确原值供执行提交，不需要为此修改或重导工作流。真实模型文件名、目录或模型身份不同仍会阻断；这时修正 ComfyUI 源并形成新的 candidate → draft → version。Deployment 变化则重新 Live Validate。不要把旧 hash 当成当前事实。

#### 9. 真实烟测并发布

- **怎么做**：填写当前 smoke fixture 的具体参数与素材，执行真实烟测；成功后再审核发布。
- **成功标志**：smoke=passed、证据 fresh，版本状态 published/current。
- **卡住时**：注意版本参数默认值、单次 fixture、系统有效配置和 ProductionRun 最终冻结值不是同一层；不要因数字不同就直接覆盖版本合同。

### H3 只读验收中的参考素材数量证据 / H3 Read-only Reference Cardinality

```jsonc
// 页面只读证据；由模型合同与注册 projector 派生，不可粘贴或编辑
{
  "total_images": { "minimum_items": 0, "maximum_items": 10 },
  "first_frame": { "minimum_items": 0, "maximum_items": 1 },
  "last_frame": { "minimum_items": 0, "maximum_items": 0 },
  "entity_images": { "minimum_items": 0, "maximum_items": 9 },
  "reference_videos": { "minimum_items": 0, "maximum_items": 3 },
  "paired_video_audios": { "minimum_items": 0, "maximum_items": 3 },
  "reference_audios": { "minimum_items": 0, "maximum_items": 3 }
}
```

### H3 只读验收中的动态参考拓扑证据 / H3 Read-only Dynamic Topology

```jsonc
// 页面只读证据；运行表单仅提供这里声明的用途和数量选项
{
  "schema_version": 1,
  "videos": {
    "minimum_items": 0,
    "maximum_items": 3,
    "default_purpose_key": "motion_camera_reference",
    "purposes": [
      { "key": "motion_camera_reference", "label": "动作与运镜参考 / Motion & Camera Reference", "description": "原生 Ref2VA 读取完整视频；此用途只表达希望侧重动作节奏与镜头运动，不隔离视频内容。", "minimum_items": 0, "maximum_items": 3 },
      { "key": "video_editing", "label": "视频编辑参考 / Video Editing", "description": "参考剪辑结构与时序。", "minimum_items": 0, "maximum_items": 3 },
      { "key": "video_continuation", "label": "视频续写参考 / Video Continuation", "description": "从参考视频末态继续发展。", "minimum_items": 0, "maximum_items": 3 }
    ]
  },
  "audios": {
    "minimum_items": 0,
    "maximum_items": 3,
    "default_purpose_key": "audio_reference",
    "purposes": [
      { "key": "audio_reference", "label": "音频参考 / Audio Reference", "description": "参考声音节奏与声景。", "minimum_items": 0, "maximum_items": 3 },
      { "key": "audio_reuse", "label": "音频复用 / Audio Reuse", "description": "复用已批准音轨。", "minimum_items": 0, "maximum_items": 3 }
    ]
  },
  "preprocessing": {
    "videos": { "adapter_key": "cfr_video_v1", "minimum_duration_ms": 2000, "maximum_duration_ms": 15000, "target_fps": 24, "requires_source_fps": true },
    "paired_video_audios": { "adapter_key": "aligned_audio_v1", "minimum_duration_ms": 2000, "maximum_duration_ms": 15000 },
    "reference_audios": { "adapter_key": "passthrough_audio_v1", "minimum_duration_ms": 2000, "maximum_duration_ms": 15000 }
  },
  "pairing_rules": [{
    "key": "video_audio_pairing",
    "label": "视频与音轨配对 / Video-Audio Pairing",
    "description": "每条视频最多一个内嵌或外配音轨。",
    "source_media_kind": "video",
    "target_media_kind": "audio",
    "source_purpose_keys": ["motion_camera_reference", "video_editing", "video_continuation"],
    "target_purpose_keys": ["audio_reference", "audio_reuse"],
    "default_target_purpose_key": "audio_reference",
    "minimum_targets_per_source": 0,
    "maximum_targets_per_source": 1,
    "allow_embedded_source": true,
    "allow_external_target": true,
    "source_and_external_mutually_exclusive": true
  }],
  "mutual_exclusion_rules": [{
    "key": "editing_or_continuation",
    "label": "编辑与续写互斥 / Editing or Continuation",
    "description": "同一绑定集合不能同时使用编辑和续写语义。",
    "media_kind": "video",
    "purpose_keys": ["video_editing", "video_continuation"],
    "maximum_distinct_purposes": 1
  }]
}
```

### H3 模型类型化执行监控实例 / H3 Typed Execution Monitoring

```json
{
  "schema_version": 1,
  "default_idle_timeout_seconds": 1800,
  "node_type_overrides": { "SamplerCustomAdvanced": 5400 }
}
```

### 字段怎么理解

- **object_info**：ComfyUI 当前节点目录与输入定义，用于确认节点类型和默认值。
- **语义绑定（Semantic Binding）**：把业务参数如 width、frames、steps 映射到具体 API 节点输入。
- **执行快照（Execution Snapshot）**：某次真实任务导出的来源证据，不等于可复用模板版本。
- **条件输入合同（Conditioning Contract）**：位于 input_contract.conditioning_contract。schema_version 和 adapter_key 选择已注册解释器，modes[] 用通用字段声明条件模式、输入类型、是否要求实体 Bible、原生音频支持与各输入槽基数；当前 Shot 的 conditioning_mode 必须精确命中其中一项。
- **静默时限（Idle Deadline）**：任务进入新节点时设置一次的最晚无进度时间；同节点 running 心跳和采样百分比不会反复续期。运行时按节点 ID、节点类型、默认值依次解析，但节点 ID 规则只能由注册 projector 派生并在验收页只读展示。
- **硬时限（Hard Deadline）**：任务冻结的不可续期上限。任何节点静默窗口都会被它截断，execution_monitoring 不能延长它。
- **主输出（Primary Output）**：最终交付视频/图片/音频对应的唯一输出节点。
- **动态参考拓扑（Dynamic Reference Topology）**：位于 input_contract.conditioning_contract.modes[].dynamic_reference_topology，是当前条件模式唯一的数据合同：videos/audios 保存双语 purposes 与显式 default_purpose_key；pairing_rules 保存视频到音轨的来源、允许用途、默认目标用途和内嵌/外配互斥；mutual_exclusion_rules 限制不能共存的用途；preprocessing 为三类媒体选择受控 adapter、时长和可选目标 FPS。
- **H3 可复制动态参考实例（H3 Copyable Reference Instance）**：外层 inputs 配 reference_videos=0–3、paired_video_audios=0–3、reference_audios=0–3；videos default=motion_camera_reference，purposes=motion_camera_reference/video_editing/video_continuation；audios default=audio_reference，purposes=audio_reference/audio_reuse；preprocessing videos={adapter_key:cfr_video_v1, minimum_duration_ms:2000, maximum_duration_ms:15000, target_fps:24, requires_source_fps:true}，paired={adapter_key:aligned_audio_v1, 2000–15000}，reference={adapter_key:passthrough_audio_v1, 2000–15000}；pairing 每视频 0–1 个内嵌或外配音轨且二者互斥；video_editing 与 video_continuation 最多同时出现一种。H3 只是实例，新模型替换这些配置而不改主编排。
- **动态参考调用关系（Reference Call Chain）**：Mode Contract → 页面渲染 Purpose/Help/Default → 人工显式 Shot Binding → 服务端 Cardinality/Pairing/Mutex 校验 → ProductionRun 冻结合同与 SHA-256 → 合同指定 Preprocessing Adapter → 所选 Model Adapter → Workflow Graph Inputs。任何一环缺合同都会停止。

### 新手最常见卡点

- **点击“在 ComfyUI 中打开”没有自动跳转**：该功能只下载服务端转换并校验的 UI JSON；把文件拖入 ComfyUI 画布。浏览器不会向另一个页面直接注入。
- **长采样节点仍在 30 分钟被判 idle**：不要原地修改旧任务；开发环境统一清理旧任务/产物必须先采用已确认的清理策略。先在下一模型能力版本选择足够的默认/节点类型窗口；工作流专属节点 ID 规则只能进入新的 WorkflowProjectorVersion。若图源字节未变，复用原 candidate 重新生成草稿；图源确实变化时才保存新的 ComfyUI revision 并重新纳管。随后重新 Live Validate、真实 Smoke 和发布，再让新 Recipe/New Run 使用它；同时确认 hard deadline 更长。验收页只读，不手填节点 ID 或 JSON。
- **看到 56 frames，以为 1 秒实际测试就是 56 帧**：56 帧是单元测试 fixture；本指南历史实测案例的 1 秒业务镜头按该次冻结 Workflow 的 24fps 和 5+17n 规则对齐为 22 帧 / 917ms。

## 提示词策略（Prompt Profiles）

先以受控 registry 或已发布能力合同证明 operation，再用版本化数据分别配置“字段内容如何获得”和“最终提示词如何排版”，最后建立运行时 Binding；发布不等于已有可执行路由。

设置深链：`/settings?section=prompts`

![系统设置提示词策略分区双语界面](../frontend/public/help/system-settings/03-prompts.jpg)

_图 3：提示词策略分区。先建字段字典和规则，再做确定性预览、发布 Profile、创建 Binding。_

### 这个部分具体做什么

- 字段字典的 field_definition.key 是稳定目标字段；运行快照中以 profile_field_key 指向它。值类型、分类等封闭词表从服务端目录选择，显示顺序和初始启用状态由服务端分配；用户只填写确有业务含义的名称、语义和影响说明。
- 每条规则先回答“字段内容如何获得”：model_generated/translate_only 从服务端 generation_source_selectors 目录选择一个主要内容来源，页面按项目、人物 Bible、场景 Bible、道具 Bible和当前镜头分组，并就近说明读取内容、适用条件和作用范围；server_locked/passthrough 自动跟随最终提示词取值，不要求重复选择。generation_context_selectors 再多选额外的项目、目标镜头、Bible、对白或跨镜头已确认信息。空数组表示只使用主要内容来源。
- Profile 级 source_projection_key 决定来源投影方式：普通策略选 direct_v1；需要按字段组合确定性原语时选 declarative_v1。classified_reference_context_ir_v1 只用于重现历史冻结合同，不再出现于新建选项。
- declarative_v1 的每条 FieldRule 以 source_selector=projection.output 读取投影结果，并用 projection_primitive 选择服务端登记的确定性原语。原语是代码能力；字段、原语组合、顺序、标签、分隔符和通道是页面业务规则。
- 同一条规则再回答“最终提示词如何排版”：source_selector 指向最终取值，并通过 position、channel、label_style、separator、max_chars、required 和 omit_if_empty 确定性拼装。
- 模型专属的段落名称、顺序、输入说明和数值限制保存为 Profile 版本数据；主编排只识别通用合同，不执行配置中的脚本、函数、模板或自定义正则。
- Binding 把已发布 Profile 与 operation、model family、mode 或精确模型版本关联。
- Operation 日常从服务端 options 目录选择；目录项只是录入建议，不授予能力。首次登记仅在 custom_entry_fields 明确开放 operation 时通过高级入口进行；发布仍必须有内置受控 operation registry、同模型族已发布 ModelProfileVersion capability 或注册 Provider driver capability 证据，配置值不会被当成脚本执行。
- 非内置 operation 的 active Binding 必须选择精确已发布模型版本并复核同名 capability。Profile 发布、Binding 建立与执行路由就绪是三件事；没有 Deployment/Workflow、真实 Smoke 或运行实现时生产仍 fail closed。
- 新建或经设置页修改的提示词策略（Profile）统一使用合同 schema v6；generation_context_selectors、source_projection_key、source_projection_config、字段级 projection_primitive、字段内容规则和最终排版规则都随版本冻结并参与一致性校验。已发布或已退役版本不原地修改，只能从精确 ID、版本和 SHA-256 先克隆后继草稿再编辑。
- 纯配置接入适用于复用现有驱动 capability、已确认事实来源和已登记原语的新模型；新建字段、schema v6 Profile rules、Binding、Workflow/Recipe 版本即可，不修改 ProductionRun 主编排。只有现有原语无法表达新的确定性协议时才新增代码原语。
- H3 六段是可复制的声明式配置实例：Profile 选 declarative_v1；六条 FieldRule 统一读 projection.output，并分别选择六个 reference_context.* 原语。字段、顺序、标签、分隔符、通道和生成合同全部由页面 FieldRule 保存，主编排本身不识别 H3 或“六段”。

### 操作前准备

- 发布 Profile 前先从受控 operation registry、同模型族已发布 ModelProfileVersion capability 或注册 Provider driver capability 核对 operation 证据；Provider 的说明性运营标记不能作为能力证据，也不参与候选筛选。正式运行前还必须从 Deployment/Workflow 与新鲜真实 Smoke 复核 operation 和 mode 确实可执行；Probe 只确认健康和能力快照，不能替代 Smoke。
- 阅读模型官方输入格式，区分“只是字段/顺序/格式不同”与“现有原语无法表达的新确定性协议”。前者走 declarative_v1 页面配置，后者先新增、测试并登记受控代码原语。
- 列出目标模型要求的段落、每段目标字段及子规则，并确认这些字段都能追溯到已确认事实。
- 先判断能否使用 direct_v1，或在 declarative_v1 中复用现有原语。只有现有原语无法表达全新确定性协议时，才进入代码原语开发与审查，不把脚本塞进配置。
- 逐字段画出 source → destination：generation_source_selector 是从 generation_source_selectors 目录选出的主要内容来源，generation_context_selectors 是生成时可参考的额外信息范围，field_definition.key/profile_field_key 是目标字段，source_selector 是最终提示词取值。再决定生成方式、说明、结果语义、语言、长度和通用校验器；相邻镜头 adjacent_shots.core 与全剧 all_shots.core 必须按字段显式选择。
- 服务端会自动提供一组类型正确的通用示例；只有需要验证特定内容或空值时才修改，以便实时预览 omit-empty、required、默认值、顺序、标签、分隔符和通道行为。
- 系统指令只能来自管理员默认 definition.default，不能把项目或请求文本提升为 system_instruction。

### 逐步操作

#### 1. 维护字段字典

- **怎么做**：填写稳定 key、显示名、含义和影响说明；分类、值类型从服务端目录选择，只有服务端明确开放的目录字段才可登记新值。按业务需要配置默认值与枚举选项，不填写显示顺序或初始启用状态。
- **成功标志**：字段按服务端追加顺序创建并默认启用，可在 Profile 规则中选择；默认值通过类型校验。
- **卡住时**：目录读取不完整时页面会停止新增，不要改用自由文本绕过；枚举默认值必须属于选项，也不要把秘密、Token 或用户自由文本放进 system_instruction。

#### 2. 先核对能力合同与接入类型

- **怎么做**：确认目标 ModelProfileVersion/Deployment/Workflow 已声明并实际验证所需 operation、model family 和 mode。若只改变字段、语言、长度、顺序、标签、分隔或 Binding，选择“纯配置接入”；只有现有原语无法表达新的确定性协议，才先开发、测试并登记代码原语。
- **成功标志**：可以明确回答本次是 configuration-only 还是 primitive-required，并能指出能力证据和所需 projection primitive。
- **卡住时**：不要用 Provider 的说明性运营标记、PromptProfile 名称或 priority 代替能力证据；无匹配 capability/Smoke 时先回模型或工作流分区。

#### 3. 查看已保存版本配置

- **怎么做**：在详情的“内容生成与模板配置”查看当前版本的模板标识、分隔符和列表前缀，按需展开模板正文、其他原语配置和质量建议。字段规则显示内容来源与输出去向，点击“查看相关模板”可在现有只读配置弹框顶部查看对应模板；完整配置仍在下方。列表或详情的“查看配置”可查看全部配置、字段规则、通道限制和合同标识。
- **成功标志**：已发布、已退役与草稿都能查看；使用所选版本已保存的数据，不补入新版默认值，不创建草稿、不执行预览或模型调用。模板可能按条件使用，配置存在不代表实际套用或验收通过。
- **卡住时**：规则读取数量不符、原语未登记或模板缺失时会明确提示，请重新读取或查看完整配置；留空不等于未保存。历史版本未保存显式模板时不还原其内置代码文案。

#### 4. 读懂模板占位符与组装关系

- **怎么做**：在草稿的“参考素材与镜头的文案模板”展开新手步骤。先写普通句子，再点击“素材标签 {label}”等中文按钮在光标处插入动态内容；在每段“查看取值说明与替换示例”中核对来源、必填要求与组装去向。模板输出还需通过字段规则的 projection.output、对应原语、排序和输出通道进入最终提示词。
- **成功标志**：例如 {label}: {subject} 会用真实参考绑定替换成具体标签与主体。教学示例不会自动填入配置或真实任务，也不是当前模板预览。
- **卡住时**：不要改写括号内标识、写脚本或把虚构标签写死；空值不会自动去掉周围固定文字。帮助目录缺失时更新平台并重新读取。保存草稿后用最终提示词预览核对示例字段排版，真实素材替换需结合实际项目和镜头验收；发布后仍需显式切换绑定，不会自动生效。

#### 5. 建立 schema v6 草稿或克隆后继版本

- **怎么做**：新建 Profile 只填写确有业务含义的 key、名称，并从服务端目录选择 model family、operation、mode 和 source_projection_key；版本号、合同 schema 和初始审核状态由服务端固定。要修改已发布或已退役版本时，点击“创建后继草稿”，核对来源 ID、版本、SHA-256 和规则数后由服务端原子复制。
- **成功标志**：首版固定为 v1 且只有草稿可编辑；后继版本精确继承原 Profile 配置与所有 FieldRule，新版本获得自己的合同 hash。
- **卡住时**：任一必需目录缺失时暂停保存，不要选择列表第一项或手写技术值；classified_reference_context_ir_v1 是历史冻结投影器，不允许新建或克隆为新写入路径；来源 hash 变化时重新读取后再决定。

#### 6. 配置字段内容和最终排版

- **怎么做**：选择 field_definition.key 作为目标字段。先在“字段内容如何获得”中选择生成方式；model_generated/translate_only 再从服务端分组下拉目录选择主要内容来源并阅读“读取什么、什么时候可用、作用范围”，server_locked/passthrough 则由页面自动跟随最终提示词取值。按需选择额外参考信息、结果语义、语言、长度和 validators；再在“最终提示词如何排版”中选择 source_selector、projection_primitive、channel、position、label_style、separator、max_chars、required 与 omit_if_empty。
- **成功标志**：规则列表同时显示目标字段、原语、主要内容来源、额外参考信息、生成/结果语义和最终排版摘要；实际技术 key 只作为审计标识展示。
- **卡住时**：不要手写 generation_source_selector 或用自由 JSON 输入上下文 key，也不要自造 projection_primitive。当前草稿只能选择 generation_source_selectors 返回的目录项；contribution 必须被当前原语明确支持；length_advisory 越界只产生 warning。

#### 7. 填写字段生成说明

- **怎么做**：对 model_generated 或 translate_only 填写 generation_instruction，说明目标字段需要包含什么、禁止添加什么，以及应如何引用冻结事实。server_locked、passthrough 不调用模型，不用填写创作指令。
- **成功标志**：文本模型收到每个 request_id 对应的字段目标、来源、说明、结果语义、语言、长度和校验规则，不会被要求一次直接拼出最终多段提示词。
- **卡住时**：instruction 是不可执行说明；不得含模板表达式、脚本、动态函数或秘密。实际可接受内容以当前 schema 与服务端校验结果为准。

#### 8. 复核受控校验组合

- **怎么做**：按字段类型勾选服务端返回的通用 validators。结果必须非空时同时勾选 generation required；JSON object/array 同时选择 valid_json；unique_items 配合 items 长度单位。
- **成功标志**：保存后服务端重新读取同一规则，未知、重复或冲突校验器无法进入草稿事实。
- **卡住时**：不要输入自定义正则或脚本；新校验能力必须先作为通用受控校验器进入平台版本，再供 Profile 选择。

#### 9. 需要 H3 时按配置案例建立六条规则

- **怎么做**：新建 schema v6 Profile，选择 operation=video.generate、mode=Ref2VA、source_projection_key=declarative_v1。六条 FieldRule 的 source_selector 都选 projection.output，projection_primitive 依次选 reference_context.subject_definitions、reference_context.summary、reference_context.retention_analysis、reference_context.detailed_description、reference_context.overall_soundscape、reference_context.non_diegetic_music。前三条用 server_locked/final；后三条用 model_generated/contribution，generation_source_selector 依次是 shot.detailed_description、shot.sound_effects、shot.music。
- **成功标志**：六个业务字段、原语、生成来源、顺序、标签、分隔符和通道均可在页面解释并进入合同 SHA-256；主编排没有 H3 专属字段开关。
- **卡住时**：不要把整段最终提示词塞进一个 instruction，不要在新策略中选历史 classified_reference_context_ir_v1，也不要给 projection.output 填未登记原语。

#### 10. 实时查看最终提示词

- **怎么做**：字段规则或 Profile 编辑弹窗右侧会自动带入服务端通用示例；页面在配置变化 400ms 后把当前未保存的完整候选快照提交给服务端，并直接显示各通道实际组合文本。只有需要验证特定内容或空值时才修改示例，也可以一键恢复。
- **成功标志**：打开后无需填写即可得到实际组合文本。预览不保存示例、不调用模型，只执行与生产相同的最终排版；顺序、标签、分隔符、通道、必填/空值和长度变化会立即反映。页面明确标记字段生成与声明式 H3 原语没有在此执行。
- **卡住时**：先修复左侧配置；若主动清空了必填示例，可以恢复服务端通用示例。不要把上次有效预览当成当前结果，也不要用示例预览替代真实项目/镜头上下文中的来源原语验收。

#### 11. 审核发布并创建 Binding

- **怎么做**：把 review state 设为 ready，发布 Profile；再新建 Binding，选择精确模型或模型族并设置 priority。非内置 operation 必须选择精确已发布模型版本。
- **成功标志**：Profile=published/ready，Binding=active，选择器没有同优先级冲突；这仍不替代 Deployment/Workflow、Smoke 和正式运行路由。
- **卡住时**：operation 无能力证据、非内置 operation 使用模型族泛化 Binding、同一 selector 的最高 priority 并列或运行 route 不完整都会 fail closed；媒体 ProductionRun 仍需显式提交 Binding ID。

### 字段怎么理解

- **目标字段（field_definition.key / profile_field_key）**：本条规则要产出的稳定字段，也就是 destination；它不是输入路径。运行中的字段请求用 profile_field_key 冻结该目标。
- **内容生成方式（generation_mode）**：server_locked 表示主逻辑按已确认事实锁定；model_generated 表示调用文本模型生成；translate_only 只翻译；passthrough 原值透传。当前字段级模型处理执行器只接入 image.generate/video.generate ProductionRun；其他 operation 只提供 server_locked/passthrough。model_generated/translate_only 需要显式选择主要内容来源；server_locked/passthrough 自动跟随最终提示词取值。
- **主要内容来源（generation_source_selector）**：不是文件地址、数据库位置或可手写表达式，而是生成当前目标字段时首先读取的一项已确认业务事实。image.generate/video.generate 的 model_generated/translate_only 只能从 options API 的 generation_source_selectors 当前目录选择；目录按项目、人物 Bible、场景 Bible、道具 Bible 和当前镜头分组，每项附带读取内容、适用条件与作用范围。
- **生成时可参考的额外信息（generation_context_selectors）**：在主要内容来源之外逐字段复选的受控已确认信息。空数组表示只提供主要内容来源；dialogue_metadata 不含对白正文，只有 dialogue_authored_text 才提供已确认台词。相邻镜头和全剧不会默认注入，必须显式选择。
- **字段生成说明（generation_instruction）**：随 Profile 版本冻结的不可执行说明，告诉文本模型这个目标字段要包含和禁止什么。model_generated/translate_only 必填；它不能成为模板、脚本或动态代码。
- **生成结果语义（generation_output_semantics）**：final 表示审核值直接成为目标字段；contribution 表示它只是当前 projection_primitive 的输入贡献，由原语结合冻结事实产生完整的 projection.output。contribution 不能在 direct_v1 下悬空，也必须命中原语声明支持的 generation source。
- **来源投影（source_projection_key）**：Profile 级受控选择。direct_v1 直接使用通用事实；declarative_v1 按 FieldRule 选择的原语产生 projection.output。classified_reference_context_ir_v1 只保留给历史冻结合同运行，不是可新建选项。
- **投影原语（projection_primitive）**：声明式 FieldRule 从服务端登记目录选择的确定性代码能力。原语只负责将冻结事实或字段贡献变为结果值；目标字段、顺序、格式和通道都由页面 FieldRule 决定。
- **声明式装配来源（projection.output）**：declarative_v1 的通用装配来源。每条规则按自己的 profile_field_key 读取对应原语结果，不再选择模型专属的 request.h3_ref2va.* selector。
- **纯配置接入（Configuration-only Onboarding）**：复用现有驱动 capability、已确认事实来源和已登记原语时，只新增或发布 ModelProfileVersion、Deployment/Workflow、PromptFieldDefinition、schema v6 PromptProfile rules、Binding、Recipe 与配置 Revision；不修改主编排。
- **Operation 首次登记（First-time Operation Registration）**：PromptProfile 草稿可输入符合 ^[a-z][a-z0-9_.-]{1,99}$ 的新 key；options 只提供建议。发布需要受控 registry、同模型族已发布 ModelProfileVersion capability 或注册 Provider driver capability 证据。配置不执行脚本，且非内置 operation 的 active Binding 必须绑定精确模型。
- **需要新原语（Primitive-required Onboarding）**：只有出现新 Provider 请求/响应协议、现有原语无法表达的新确定性转换，或新可执行 validator 时才需要代码。原语必须范围明确、可测试、经审查并注册；配置不能承载脚本。
- **H3 六段声明式配置案例（H3 Declarative Six-section Example）**：该可复制案例使用合同 schema v6：model_family=minimax_h3、operation=video.generate、mode=Ref2VA、source_projection_key=declarative_v1。六条 FieldRule 的 source_selector 都是 projection.output，projection_primitive 分别是 reference_context.subject_definitions、reference_context.summary、reference_context.retention_analysis、reference_context.detailed_description、reference_context.overall_soundscape、reference_context.non_diegetic_music。前三段为 server_locked/final；后三段为 model_generated/contribution，generation_source_selector 分别是 shot.detailed_description、shot.sound_effects、shot.music，并显式选择 project.style、target_shot.core、target_shot.dialogue_metadata、target_shot.linked_bible。三条 contribution 的建议目标依次为 350–500、1–80、1–60 english_words；positive_prompt 的 max_chars=7000、warn_at_chars=6200。H3 不把已确认对白原文交给字段生成模型，服务器确定性投影/装配会把锁定原文写入最终 <d>。共享参考上下文参数在声明式投影区用字段化控件维护；字段、position 1–6、label_style、separator、channel、max_chars 和字段内容合同全由页面规则确定。实际运行读取本次选择并冻结的 Profile 版本、Binding 与合同 SHA-256，不按模型名称回退到历史规则。
- **最终提示词取值（source_selector）**：最终拼装时读取哪个原始或投影事实；不要和主要内容来源 generation_source_selector 混为一项。
- **语言策略（language_policy）**：支持 inherit、preserve_source、any、BCP 47 标签和 bilingual:<tag>+<tag>；translate_only 必须给出可确定的目标语言。
- **长度单位、边界与等级（length_unit / generation_min / generation_max / length_advisory）**：none、characters、english_words 或 items；min 不得大于 max。勾选 length_advisory 时边界是推荐目标，越界记录 recommended_length_out_of_range 并继续；未标为 advisory 的长度边界才是硬约束。
- **受控校验器（generation_validator_keys）**：只能从服务端列表勾选。protected_tokens_preserve_count 表示受保护内容保持来源出现次数；protected_tokens_at_least_once 表示允许重复但至少出现一次，两者互斥。未选择时仍严格保持来源次数。未知名称、重复项、JSON 形状冲突和不一致组合都会 fail closed。
- **最终提示词排版规则**：position 决定顺序；channel 决定正向、负向、系统指令或结构化通道；label_style 决定字段名样式；separator 决定片段间分隔；max_chars 限制单字段长度。
- **字段为空时省略（omit_if_empty）**：最终提示词取值为空时是否省略片段；它与“字段内容必须生成成功”是两个独立约束。
- **priority**：同一选择范围内的匹配优先级；不会覆盖 ProductionRun 已显式冻结的 Binding。
- **实时最终提示词预览**：服务端提供不读取生产默认值的通用示例，页面自动带入并允许本次修改；只运行最终拼装逻辑，不保存示例、不调用模型、不产生费用，也不伪造字段生成或声明式来源原语结果。编辑时提交未保存候选快照，旧响应不得覆盖新配置。
- **PromptProfile 合同 schema v6**：projection_primitive、generation_context_selectors、字段内容规则、source_projection_config 与最终排版规则全部进入规范 JSON、大小和 SHA-256；任一值变化都必须从精确已发布版本克隆后继草稿，不能静默改写历史合同。

### 新手最常见卡点

- **保存字段规则时提示主要内容来源无效**：重新读取页面后，从服务端分组下拉目录选择业务事实，不要手写 shot.narrative 一类技术路径。若显示“历史未登记来源”，该值只供历史审计，当前草稿必须显式改选。
- **模型看不到对白原文，或意外读取了过多镜头**：检查 generation_context_selectors：dialogue_metadata 不含台词，需要原文时显式勾选 target_shot.dialogue_authored_text；不需要跨镜上下文时不要勾选 adjacent_shots.core/all_shots.core。空数组就是最小范围，不是缺失配置。
- **把主要内容来源和最终提示词取值当成同一项**：先写清目标字段 field_definition.key；再分别确认生成字段内容时首先读什么（generation_source_selector）和最终拼装时取什么（source_selector）。model_generated/translate_only 分开选择；server_locked/passthrough 由页面自动保持一致。
- **contribution 保存或编译后提示没有匹配投影**：contribution 只能用于 model_generated/translate_only，source_selector 必须是 projection.output，且 projection_primitive 必须明确支持当前 generation_source_selector；普通直出字段改用 final，普通事实装配使用 direct_v1。
- **接入新模型时准备修改主编排或复制一套 H3 逻辑**：先用 schema v6 PromptProfile/FieldRule/Binding 表达业务字段、projection primitive、生成说明、顺序和装配格式。复用现有原语时不改代码；只有现有原语无法表达全新确定性协议时才新增受审代码原语。
- **PromptProfile 已发布，但运行提示模型不支持 operation/mode**：PromptProfile 只定义内容合同，不授予模型能力。回到模型/工作流分区核对注册驱动、ModelProfileVersion capability、Deployment/Workflow 和新鲜 Smoke，再创建匹配的 Binding。
- **新 Operation 可以保存草稿，但发布提示 operation evidence missing**：这是预期门禁：options 不是允许列表，草稿登记也不授予能力。先发布同模型族且 specification.capabilities 精确声明该 key 的 ModelProfileVersion，或完成受控 operation registry/Provider driver 注册；Provider operations 说明文本不能替代证据。
- **长度或 validator 组合被拒绝**：none 时清空 min/max；其他单位至少填一个边界；non_empty 配合 generation required；JSON 形状配合 valid_json；unique_items 配合 items。
- **发布按钮不可用**：确认 review state=ready、至少一条规则、operation/family/mode 完整，并逐条保存了字段内容与最终排版规则。

## 配方与路由（Recipes & Routing）

把剧本到交付的阶段顺序、人工门禁和精确 Deployment/Workflow 路由冻结成可发布配方。

设置深链：`/settings?section=routing`

![系统设置配方与路由分区双语界面](../frontend/public/help/system-settings/04-routing.jpg)

_图 4：配方与路由分区。发布前必须核对阶段完整性、路由 ID 和合同指纹。_

### 这个部分具体做什么

- Production Recipe 是一次生产运行的流程合同，定义阶段顺序、执行模式、审批方式、路由和变体数。
- 已发布 Recipe 冻结阶段与精确 Deployment/Workflow 执行路由，不由“默认部署”或优先级静默替换。
- ProductionRun 创建时再把已发布 Recipe、Prompt Binding、最终有效配置和项目输入共同冻结；不要把两层快照混为一谈。
- AI 创建和含 script_structure/ai 的导入配方使用同一 structured_shot_duration_contract：把集数、每集镜数、单集总时长预算与全部冻结文本 route 的镜头时长策略求交；冲突在任何文本模型调用前拒绝。
- 配方生产就绪概览按全部仍处于 published 的不可变版本计算；首屏只显示 ready/blocked 数量，原因按需下钻。较早版本只有退役后才不再计入。

### 操作前准备

- 文本与媒体 Deployment 已真实烟测通过，Workflow 已校验/烟测/发布。
- 明确哪些阶段人工操作、AI 执行或需要审核门禁。
- 先决定做人工、半自动还是使用已有的无人值守配方。

### 逐步操作

#### 1. 先看生产就绪总体情况

- **怎么做**：先核对全部 published 版本数、ready/blocked 数和聚合原因；需要时展开原因并跳到对应配置域。
- **成功标志**：无需滚动逐行阅读即可先得到平台级结论；仍 published 的较早阻断版本不会被静默过滤。
- **卡住时**：总体 blocked 时按聚合原因修复源配置；若较早版本不应继续可选，审核后执行退役，不要只在前端隐藏。

#### 2. 新建 v1 或创建后继草稿

- **怎么做**：建立全新配方族时填写具有业务含义的 recipe key 和名称；版本号与定义 Schema 由服务端固定。已有 Recipe Key 必须从 published/retired 版本卡片创建后继草稿，由服务端核对 definition SHA-256、原子复制并分配下一版本。在结构化编辑器从服务端目录选择阶段、mode、approval 和 routing，不直接编辑完整 definition JSON。
- **成功标志**：全新配方固定为 v1；后继草稿精确复制来源合同并获得连续版本；每个阶段显示中文标签和机器 key，路由指向明确 Deployment/Workflow。
- **卡住时**：目录读取不完整、来源已变化或已有草稿时暂停保存，不要手写技术值、重复登记同一 Recipe Key 或沿用旧目录；fixed 路由必须正好一个 route；脚本生成/结构化/规范化最多一个变体，其他媒体最多 8 个。

#### 3. 审核完整闭环

- **怎么做**：打开“审核详情”，从 script_input 一直核对到 delivery，确认 route IDs、定义 SHA 和所有依赖状态。
- **成功标志**：没有缺失阶段、未烟测依赖或无法到达 delivery 的路径。
- **卡住时**：服务端发布门禁会重新检查内容到交付闭环；不要通过删除失败阶段来规避。

#### 4. 发布或复用无人值守配方

- **怎么做**：普通人工/半自动配方可在结构化编辑器建立。全自动场景优先复核并使用已有已发布 schema v2 unattended 配方。
- **成功标志**：Recipe=published，ProductionRun 创建页可明确选择。
- **卡住时**：当前可视结构化编辑器没有 interaction_mode=unattended、review policy 和 approval=policy 控件，不能向新手承诺只靠可视控件从零建立同等全自动配方。

### 字段怎么理解

- **阶段键（stage key）**：稳定机器值；页面用中文解释，但 API 值不能翻译。
- **固定路由（fixed）**：运行时只能使用一条精确 route。
- **按顺序降级（ordered_fallback）**：按声明顺序尝试候选，不代表可以绕过预算或权限门禁。
- **人工选择（manual_choice）**：创建运行时由人工明确选路由。
- **结构化镜头时长合同（structured_shot_duration_contract）**：仅 AI 创建或包含 script_structure/ai 的导入配方使用。服务端以单集总时长为预算，与全部冻结 route 的 minimum/default/maximum 求交并保存 SHA-256；例如 1 集、1 镜、1 秒会冻结为 1000/1000/1000ms。导入配方没有 script_structure 时页面不显示也不提交这些计划字段。

### 新手最常见卡点

- **在审核详情的只读 JSON 里看见 unattended，就准备复制修改**：先复核已有 published v4。普通配方编辑器不开放完整 definition JSON 写入口；全自动能力缺少结构化控件时应继续使用已审核版本，不能复制技术字段绕过。
- **改了默认 Deployment，但 Run 仍走旧部署**：已发布 Recipe 已冻结精确执行路由；需要从精确 published/retired 版本卡片创建并发布后继草稿，再创建新的 ProductionRun 共同冻结本次 Binding、配置和输入。

## 默认值与限制（Defaults & Limits）

查看最终生效值、只提交真正修改的键，并以不可变 Revision 记录每次变更。

设置深链：`/settings?section=defaults`

![系统设置默认值与限制分区双语界面](../frontend/public/help/system-settings/05-defaults.jpg)

_图 5：默认值与限制分区。每个值同时显示类型、允许作用域、来源和是否需重启。_

### 这个部分具体做什么

- 新增或编辑 ConfigDefinition 时，配置类别、值类型、允许作用域和默认来源使用服务端封闭目录；配置域、单位和影响模块只有在服务端明确授权时才可登记新值。生命周期默认值由服务端固定，已有 Revision 后结构字段锁定，页面只提交实际变化。
- 配置按 model default → workflow default → system → project → run 逐层覆盖。
- 设置页创建 system Revision；ProductionRun 创建时冻结最终值、来源、revision 和 SHA。
- production.text_contract_retry_limit 读取当前有效整数值；只控制 script.generate、script.structure 与 visual_rules.generate 已收到确定响应，但安全审计判为输出截断、缺少最终文本或不符合受控 JSON 外壳时，首次调用之外允许的自动重试次数。0 表示首次此类失败后转人工；可填写的绝对上限由超级管理员维护的 security.production.max_automatic_retries 决定。
- production.prompt_field_retry_limit 读取当前有效整数值；只控制 script_normalize 中 provider_response_invalid 或 prompt_normalize_contract_failed 且唯一 KNOWN_RESPONSE_REJECTED 审计完整的额外尝试。0 表示首次此类失败后转人工；可填写的绝对上限由超级管理员维护的 security.production.max_automatic_retries 决定。retry_feedback 不在这里配置：它只能由服务端从冻结失败审计派生，页面只读显示是否用于下一 attempt 及 SHA-256。
- 两个键互不替代：前者保护上游三项通用文本 operation 的整体响应合同，后者保护 prompt.normalize 的逐字段冻结合同。内容过滤、uncertain、执行前门禁、Smoke/配置漂移和审计缺失都不自动重放。
- 恢复继承会通过 unset_keys 删除 system override，不会把下层值复制一份。

### 操作前准备

- 先记录当前 Effective Configuration 的 SHA，明确本次只改哪些键。
- 检查字段类型、范围、allowed scopes、needs review 和 restart required。
- 分别决定可接受的通用文本整体响应失败与 script_normalize 字段合同失败重试次数；两个值都在新 ProductionRun 创建时冻结，修改系统值不会改写已经开始或已完成的运行。
- 资源选择型字段必须显式选择真实 Deployment/Profile，空值代表未配置，不会回退列表第一项。
- ComfyUI 已保存工作流扫描范围使用 workflow.comfyui_saved_workflow_directory；值是 userdata 相对目录，只允许 workflows 或其安全子目录，保存后无需重启，但目录变化会形成新的来源作用域并要求重新扫描。

### 逐步操作

#### 1. 维护配置定义

- **怎么做**：新增时填写稳定配置键和必要业务说明；配置类别由当前分区锁定，配置域、值类型、单位、影响模块、作用域和默认来源从服务端目录选择，只有 custom_entry_fields 授权项可登记新值。初始启用、审核状态等生命周期字段由服务端固定；编辑时只提交 mutable_fields 允许且实际改变的字段。
- **成功标志**：目录完整时才可保存；已有 Revision 的定义会明确锁定结构字段，并只发送实际差异，未修改字段不会造成版本冲突。
- **卡住时**：目录缺失或过期时停止提交，不要回退为任意文本；需要改变已有 Revision 的结构合同，应停止操作并另行评审迁移。

#### 2. 找到要改的键

- **怎么做**：按 domain 展开，阅读中文说明、类型、作用域、当前来源和影响模块。
- **成功标志**：能说清当前值来自哪一层，以及改动会影响 API、worker、ProductionRun 还是交付。
- **卡住时**：字段缺说明或资源选项读取失败时暂停提交，先修复定义或依赖接口。

#### 3. 设置两类文本合同重试上限

- **怎么做**：找到 production.text_contract_retry_limit 与 production.prompt_field_retry_limit，按业务成本与人工承接能力分别填写非负整数。0 表示首次对应硬合同失败后转人工；当前页面允许的最高值来自超级管理员维护的 security.production.max_automatic_retries，而不是前端固定范围。不要把两个值合并，也不要新建 retry_feedback 配置项或在 JSON 中手填反馈。
- **成功标志**：页面按当前安全边界完成范围校验，变更集中显示两个键的新值；新建 ProductionRun 会把有效值及来源冻结进运行快照。通用文本重试只关联受控失败证据；若 prompt.normalize 某次 length_out_of_range 具有与冻结字段规则一致的安全数值上下文，下一 attempt 会自动关联服务端派生反馈及 hash；正文不可查看或编辑。
- **卡住时**：不要用很大的值掩盖规则或模型问题。内容过滤、pre-submit、provider_request_rejected、uncertain、Smoke/配置漂移、目标合同漂移和审计缺失不计入这两类安全重试；它们会按各自规则阻断、切换 route 或等待消歧。

#### 4. 只修改必要值

- **怎么做**：修改输入后确认页面标记为待改；若要撤销系统覆盖，点“恢复继承”，不要手动抄写下层默认值。
- **成功标志**：顶部待改/待恢复数量准确，未触碰的键不会进入提交。
- **卡住时**：显式 false、0、null 是真实值，不要把它们误判为空；类型或范围错误必须就地修正。

#### 5. 核对并发布变更

- **怎么做**：点击“核对并发布变更”，阅读变更集和审计说明，先创建草稿 Revision，再发布。
- **成功标志**：Revision 状态变为 published/active；Effective SHA 更新。
- **卡住时**：stale base 表示页面基线过期；重新读取后逐项重做，不要覆盖别人或后台刚产生的配置。

#### 6. 处理需重启配置

- **怎么做**：restart_required 项发布后会 pending activation；使用正式 restart.ps1 启动新进程，再由新 boot 激活。
- **成功标志**：新进程激活后状态为 active，读取值来自新 Revision。
- **卡住时**：当前仓库没有独立 stop 脚本；不要虚构或使用旁路停止命令。

### 字段怎么理解

- **dirty keys**：本次确实被人工修改的键，只有它们进入变更集。
- **unset_keys**：要删除当前 system override、恢复继承的键。
- **Revision**：不可变配置修订，保留值、来源、审计和激活状态。
- **Effective Configuration**：所有层级解析后的最终生效配置，不等于任一单层默认值。
- **production.text_contract_retry_limit**：当前有效的非负整数；表示 script.generate、script.structure 与 visual_rules.generate 的已知整体响应合同拒绝允许的额外自动尝试数，总尝试数=1+该值。最高可配值读取 security.production.max_automatic_retries；新 Run 创建后冻结，审计完整且耗尽上限时转人工确认。
- **production.prompt_field_retry_limit**：当前有效的非负整数；只表示 script_normalize 两类已知响应校验失败允许的额外自动尝试数，总尝试数=1+该值。最高可配值读取 security.production.max_automatic_retries；新 Run 创建后冻结，审计完整且耗尽上限时转人工确认。
- **服务端安全重试反馈（retry_feedback）**：不是管理员配置。仅当前一次 KNOWN_RESPONSE_REJECTED 包含可与冻结 item_generation_rule 反校验的 length_out_of_range 时，服务端派生 schema_version、受控 reason_code、数值 validation_context 和 request_index，并以 repair_of_attempt_id 与 retry_feedback_sha256 关联下一 attempt。页面只读显示关系与 hash，不显示或允许编辑反馈正文，更不会把模型原文带回下一次请求。

### 新手最常见卡点

- **把 900ms 写进业务默认值以追求 22 帧**：保持 1000ms 业务事实；该历史验收案例冻结的 H3 Workflow 按 24fps 和 5+17n 对齐到 22 frames / 917ms。其他运行读取各自冻结的 Workflow。
- **页面显示配置已发布，运行仍用旧值**：检查是否 restart_required 尚待新 boot 激活，以及该 ProductionRun 是否早已冻结旧快照。
- **已知文本合同失败后一直重试**：先按 operation 核对运行快照使用的是 production.text_contract_retry_limit 还是 production.prompt_field_retry_limit，再检查错误码和唯一 KNOWN_RESPONSE_REJECTED 审计；达到 1+对应配置值后必须进入人工确认，不能继续创建下一次自动尝试。
- **把执行前失败、uncertain 或审计缺失也当成字段重试**：立即停止重投。pre-submit、provider_request_rejected、Smoke/配置漂移和合同漂移按门禁处理，uncertain 先人工消歧，审计缺失则修复审计链；这些状态不会降级为人工填写字段。

## 安全边界（Security Boundaries）

集中查看平台不可被项目、运行、模型或工作流默认值放宽的绝对资源上限，并由超级管理员单独审核高风险变更。

设置深链：`/settings?section=security`

![系统设置安全边界分区双语界面，包含搜索、默认隐藏退役记录和超级管理员权限提示](../frontend/public/help/system-settings/06-security.jpg)

_图 6：安全边界分区。平台管理员可搜索和只读复核；只有超级管理员能修改、发布或激活，已停用记录默认隐藏。_

### 这个部分具体做什么

- 安全边界（Security Boundaries）保存平台绝对保护值，例如请求、提示词、媒体、工作流和运行任务的最大资源消耗；命中后失败关闭（fail closed）。
- 它与普通建议值不同：项目、ProductionRun、ModelProfileVersion 或 Workflow 默认值都不能放宽安全边界；收紧后的当前启用值也会保护已经排队但尚未外发的任务。
- 页面以 configuration_class=security_boundary、domain=security、allowed_scopes=system、is_required=true、is_secret=false 作为完整合同。缺失或未知类别会单独报错并失败关闭，不会混入普通配置。

### 操作前准备

- 先使用 staff 账号只读核对现值、来源与受影响模块；只有确实需要变更时才切换到 superuser。
- 准备要保护的真实外部上限、当前工作流合同、最小真实 Smoke 和容量证据；不要仅凭某次成功任务抬高上限。
- 记录当前 Effective Configuration SHA、拟修改键、旧值、新值、风险和回滚值；确认普通“建议值”没有误放进安全分类。
- 先用搜索框按配置键、配置域、说明、单位或影响模块定位；退役记录默认不展示，需要复核时再主动勾选。

### 逐步操作

#### 1. 只读定位并确认分类

- **怎么做**：进入“安全边界”，搜索目标键，核对配置类别、security 域、system 作用域、类型、单位、影响模块和当前来源。staff 页面不会显示新增、保存、发布或激活按钮。
- **成功标志**：只看到启用的安全定义；页面明确显示只读权限，未知 configuration_class 会进入红色失败关闭提示。
- **卡住时**：如果记录消失，先清空搜索，再按需勾选“显示已退役/停用记录”；若进入未知分类提示，修复服务端数据合同后重新读取，不能当普通配置继续。

#### 2. 由超级管理员修改单个边界

- **怎么做**：superuser 打开目标定义或值。配置类别、security 域、system 作用域、空默认来源及 required/non-secret 合同由安全分区和服务端锁定，只读核对；新定义生命周期由服务端分配。只填写业务说明、边界约束及服务端 mutable_fields 允许修改的字段。
- **成功标志**：页面本地合同校验通过，顶部待改数只包含人工实际修改的安全键。
- **卡住时**：任一只读合同不符合预期时停止提交并修复服务端事实；不要改成 standard 或手写技术值绕过。

#### 3. 核对、发布并重新验收

- **怎么做**：点击“核对并发布安全边界”，填写高风险审计原因，核对旧值、新值和影响模块，发布 system Revision；随后重新读取 Readiness，并对受影响的 Provider/Workflow 执行最小真实验证。
- **成功标志**：Revision 持久化为已发布/已激活状态，Effective SHA 更新，超限请求仍以 security_boundary_exceeded 明确失败，范围内请求可以正常进入主流程。
- **卡住时**：stale base 时重新读取并重做；需要重启的配置只在正式 restart.ps1 启动的新进程中激活。不要重复提交外部任务验证。

#### 4. 实际案例：复核 0.2MP / 4 steps 动态 Ref2VA

- **怎么做**：以本次选中的 H3 动态 Ref2VA Workflow 为准，逐项核对 352×608 像素、4 steps、参考图片/视频/音频数量、提示词字节和任务时限都没有超过对应 security.* 上限；这里只调整确有平台风险证据的绝对上限，不把 0.2MP 或 4 steps 写成全平台硬编码。
- **成功标志**：工作流合同负责本次运行的精确值，安全边界只提供更外层的不可越过上限；人工、辅助和全自动三条线在相同合同内均可继续。
- **卡住时**：若工作流精确值超过安全边界，先判断是工作流配置错误还是经过审核的容量需求；没有 Smoke、成本和资源证据时不要抬高安全值。

### 字段怎么理解

- **configuration_class=security_boundary**：安全分类的机器值。只有该精确值进入安全分区；缺失、未来未知值和拼写错误均失败关闭。
- **changes_security_boundaries**：服务端按修订基线计算的只读事实。true 的 Revision 只进入安全边界分区，并且只有 superuser 能发布或激活；false 才能进入普通配置修订列表；缺失或未知时失败关闭，不能发布或激活。
- **allowed_scopes=system**：安全边界只由系统级 Revision 管理，不能被 project 或 run 覆盖。
- **失败关闭（fail closed）**：无法读取、无法分类、缺值或超限时停止危险操作并显示原因，不使用普通默认值继续。
- **绝对上限与建议值**：绝对上限保护安全、资源或协议边界并可阻断；业务推荐目标应放在普通可配置项中，只告警、交由人工验证。
- **已停用/退役定义（inactive）**：保留历史识别与审计但不再生效，列表默认隐藏；主动勾选后只读复核或由有权限人员处理。

### 新手最常见卡点

- **为了让一个工作流通过，把所有安全上限都调大**：先核对该 Workflow 的输入合同、真实 Smoke、显存、超时和费用证据，只变更与明确瓶颈对应的单个键，并保留审计与回滚值。
- **staff 看得见输入框，就以为可以提交**：权限目录明确把安全分区写操作限定为 superuser；staff 只读。最终服务端仍会再次拒绝越权修改。
- **configuration_class 缺失时把记录当 standard**：页面现在会把缺失或未知值放入红色失败关闭提示。修复源数据和 API 合同后重新读取，不能通过前端默认值掩盖。

## 运行主机（Runtime Hosts）

先查看所选运行主机的总体结论，再创建持久 Probe、下钻能力快照并基于同一快照生成推荐。

设置深链：`/settings?section=runtime`

![系统设置运行主机分区双语界面](../frontend/public/help/system-settings/06-runtime.jpg)

_图 7：运行主机分区。先看总体情况，再选择主机、Probe 和下钻证据；主机能力结论与 Recommendation 必须属于同一快照。_

### 这个部分具体做什么

- 运行主机由基础设施登记，Settings 只负责选择、启停、探测和查看事实，不提供新建主机按钮。
- Probe 在后台 worker 中执行并持久化，刷新或离开页面不会丢失。
- 能力快照记录 CPU/GPU/内存/磁盘/FFmpeg/ComfyUI 和模型文件 size/SHA，但不暴露本地路径。
- 首屏总体情况只汇总所选主机、最近快照和主机级 capability readiness；平台级配方生产就绪属于“配方与路由”。

### 操作前准备

- 明确选择目标主机；不要依赖列表第一项或上次残留选择。
- 确认主机不是 disabled，并且没有正在进行的 Probe。
- 模型文件多时探测需要时间；一次提交后等待持久任务终态，不要反复点击。

### 逐步操作

#### 1. 先看运行主机总体情况

- **怎么做**：先核对 active 主机数、所选主机、最近能力快照、生效配置基线以及每项主机能力的 ready/blocked 状态。
- **成功标志**：无需先滚动主机与证据明细即可判断当前所选主机是否可用于后续推荐。
- **卡住时**：尚无快照、快照读取失败或 capability 无法判断时保持显式未知状态；不要沿用旧快照伪装当前结论。

#### 2. 选择并探测主机

- **怎么做**：在“事实 · 运行主机”中选择单选项，点击“探测所选主机”。
- **成功标志**：出现持久 Probe ID、进度、worker heartbeat，最终生成 snapshot。
- **卡住时**：404 表示从未有快照，可创建第一份；接口 error 与 404 不同，必须先修接口。

#### 3. 按需核对能力快照

- **怎么做**：检查 CPU/GPU、内存、磁盘、FFmpeg、ComfyUI 版本；需要排障或审计时再展开模型文件观测证据或打开完整快照。
- **成功标志**：probe=succeeded，snapshot 未过期，所需 artifacts 显示 available 且 size/SHA 匹配。
- **卡住时**：文件缺失或 hash 不一致时修复主机文件，再重新 Probe；不要在读侧替换为旧快照。

#### 4. 生成并处理推荐

- **怎么做**：基于当前 snapshot 点击“生成当前快照推荐”；接受/覆盖前再次核对主机、快照和候选证据。
- **成功标志**：推荐有效，接受/覆盖只生成草稿 Revision 或 Recipe，不会自动发布。
- **卡住时**：切换主机、重新 Probe 或配置漂移会使 recommendation stale；重新生成，不要提交旧候选。

### 字段怎么理解

- **Probe**：读取主机事实并形成带时间、可审计的能力快照。
- **主机能力 Readiness**：只针对所选主机与当前 snapshot 判断各 capability 是否有兼容部署；不等于平台级配方生产就绪。
- **Snapshot**：某一时刻冻结的主机事实；有有效期，不能永远当作当前状态。
- **Recommendation**：基于同一快照计算的草稿建议，不会自动下载、发布或激活。

### 新手最常见卡点

- **代理/进程看起来在运行，就认为主机可用**：以当前 Probe、端口、ComfyUI 和模型文件事实为准；进程存在不等于链路已通过。
- **停用主机按钮被服务端拒绝**：仍有 active Deployment 引用。先迁移或停用相关部署，再停用主机。

## 凭据与审计（Credentials & Audit）

安全管理秘密凭据引用并查看配置、生命周期和密钥的不可变审计记录。

设置深链：`/settings?section=governance`

![系统设置凭据与审计分区双语界面](../frontend/public/help/system-settings/07-governance.jpg)

_图 8：凭据与审计分区。截图只展示元数据和状态，绝不展示秘密正文。_

### 这个部分具体做什么

- SecretBinding 的秘密正文只写入、不回显；页面、API 和审计都不会返回明文、密文或 nonce。
- Provider/Deployment 只保存 SecretBinding 引用，避免 Token 进入普通配置、前端或文档。
- 生命周期审计记录 actor、action、object、reason 和 before/after SHA；密钥审计仅 superuser 可读。

### 操作前准备

- 仅在需要创建、轮换或停用凭据时使用 superuser；普通 staff 不需要看到秘密。
- 在密码管理器中准备新秘密，确认唯一 key、显示名、用途和可复核的审计说明。
- 不要在截图、工单、普通文档或聊天中粘贴真实 Token。

### 逐步操作

#### 1. 创建受管凭据

- **怎么做**：点击“新增受管凭据”，填写唯一 key、显示名称、用途、秘密正文和审计说明。
- **成功标志**：保存后只显示 configured、版本号和元数据；输入框清空，秘密正文不回显。
- **卡住时**：审计说明或秘密为空会被拒绝；不要把秘密改放到 Provider 高级 JSON。

#### 2. 绑定到 Provider/Deployment

- **怎么做**：回模型分区编辑对象，在受管凭据选择框中选中该 SecretBinding；保存的只是引用。
- **成功标志**：绑定与生命周期显示“已绑定受管凭据”，但仍需 Probe/Smoke 证明可调用。
- **卡住时**：configured 不等于 usable；检查 binding、owner、版本和健康证据。

#### 3. 轮换或停用

- **怎么做**：轮换时输入新秘密和审计说明；停用前检查所有引用能力。
- **成功标志**：版本号递增或状态停用，并新增不可变密钥审计。
- **卡住时**：停用会让引用能力 fail closed；生产中对象先迁移凭据再停用。

#### 4. 审计追踪

- **怎么做**：按对象类型/动作筛选生命周期审计，打开安全摘要；superuser 可额外查看密钥审计。
- **成功标志**：可以用对象 ID、时间、操作者和 SHA 还原变更，但看不到秘密正文。
- **卡住时**：如果审计缺失，不要把操作当作已完成；重新读取并核对服务端响应。

### 字段怎么理解

- **configured**：存在可解密的当前秘密版本，不代表远端调用已验证。
- **owner**：引用凭据的 Provider 或 Deployment；owner 停用时能力也不可用。
- **fail closed**：条件不满足就明确阻断，不回退旧凭据或空值。
- **before/after SHA**：变更前后安全摘要，用于审计一致性，不包含秘密正文。

### 新手最常见卡点

- **凭据已创建，但 Provider 仍显示未配置**：创建 SecretBinding 不会自动绑定；编辑 Provider/Deployment 并明确选择引用。
- **轮换后旧烟测仍显示 fresh**：重新读取并执行新 Probe/Smoke；凭据变化可能使旧证据失效，不能把旧结果当当前事实。

## 历史案例：动态 Ref2VA 双时钟全自动短剧（PromptProfile v3）

这是一条已完成的历史 v3 链路证据：从 0 集、0 分镜的新项目生成剧本、文本 Bible 和 z-image-turbo Bible 候选图，让 2 个 Shot 共享人物/场景/道具锚点，并显式选择一条动态参考视频。它不证明当前 schema v6 declarative_v1 Profile、projection.output、逐字段原语/上下文、建议审计、容量分批或有限重试已经过联合真实执行，也不能替代任何新运行实际选择并冻结的视频 Workflow 与 Bible 图片 Workflow 证据。

![动态 Ref2VA 双时钟运行详情页面阅读位置示意](../frontend/public/help/system-settings/08-case-result.jpg)

_图 9：历史 v3 运行详情页的阶段、快照、Artifact 与 ReleaseBundle 阅读位置示意。这里只证明该历史 Run 的持久证据，不代表后来选择的 Profile、逐字段上下文、建议审计、容量分批与字段生成合同已经联合实测。_

### 已核实的固定事实

- 2 个 Shot 的业务时长各保持 1000ms；H3 按 24fps 与 5+17n 合法帧规则分别对齐为 22 frames / 917ms。
- H3 Workflow v3：aadbf9be-9a33-4507-a6d2-9629d5c3cfd0；执行使用该历史 Run 已冻结的 H3 Deployment。Deployment UUID 不写入操作手册，现场应从 Run 快照和当前 Settings 读取。
- 该历史 Run 的两份视频 PromptArtifact 实际冻结 PromptProfile v3：minimax_h3.video.ref2va；Binding：minimax-h3-ref2va-dynamic-v3。
- 全自动 Recipe v4：983f2cd4-37eb-4914-869e-1b7b467ca6ae，包含从 script_generate 到 delivery 的 17 个阶段。
- z-image-turbo Workflow v2：a3c0b172-b194-42b5-9b8d-9895957c36b8；执行使用该历史 Run 已冻结的 z-image-turbo Deployment；Bible 图片阶段使用独立 9 steps。
- 动态参考视频 Asset 67069cf0-ae08-483e-be0a-2d443e259ef9 是历史链路烟测素材；原生 Ref2VA 实际会读取其完整画面，因此它不应进入正式生产选择器。历史 Run 保留原审计事实，但新 Run 必须选择同故事且人物、场景、道具不冲突的生产素材。

### 设置页预检

#### 默认值

- production.width=352、production.height=608
- 该历史案例的两个镜头业务时长均为 1000ms
- 该历史 Run 的 script_normalize 快照没有 production.prompt_field_retry_limit，按最新逻辑会 fail closed，不能继续执行或作为有限重试证据；只有新 Run 才冻结当前有效值
- 通用生产提示词语言门禁 production.prompt_language_gate 为 block
- 4 steps 只用于 H3 视频阶段，不能覆盖 z-image-turbo Workflow v2 的 9 steps

#### 安全边界

- 由 staff 先只读复核，再由 superuser 处理确需修改的高风险值
- 352×608、4 steps、参考图片/视频/音频数量、提示词字节与任务时限均未超过当前启用 security.* 绝对上限
- configuration_class、security 域、system 作用域与 required/active 合同完整
- 本次工作流精确参数属于版本化合同，不把历史 0.2MP / 4 steps 写成全平台硬编码

#### 运行主机

- 目标主机 active
- 最近 Probe 成功且 snapshot 未过期
- H3 所需模型文件都有 size/SHA 可用证据

#### 模型部署

- 历史 H3 Deployment 在该 Run 创建时 active 且 smoke fresh；当前状态必须回到 Settings 重新读取
- 历史 z-image-turbo Deployment 在该 Run 创建时 active；当前状态必须回到 Settings 重新读取
- 文本 Deployment 的 script.generate 与 prompt.normalize 烟测有效
- 凭据 configured/usable，但不查看秘密正文

#### 工作流

- 该历史 Run 冻结 H3 v3 与 z-image-turbo v2；它们不是当前新建 Run 的版本选择
- 任何新 Run 都必须读取并冻结本次实际选择的视频 Workflow 与 Bible 图片 Workflow，并分别要求自己的 published/current、Live Validate 与真实 Smoke 证据；不能沿用本历史案例的版本
- H3 capability=video.reference_to_video、conditioning mode=Ref2VA
- 该历史 H3 v3 的双时钟 topology、24fps、4 steps 已冻结，只是本次版本化验收事实
- 动态视频合同来自 input_contract.conditioning_contract.modes[Ref2VA].dynamic_reference_topology.videos；本案例为 minimum_items=0、maximum_items=3
- 该历史 z-image-turbo v2 的 Bible 图片参数和 9 steps 只属于本案例审计；新 Run 读取自己选择的图片 Workflow，不能由本案例推断尺寸或 steps

#### 提示词策略

- 历史 Run 冻结 minimax_h3.video.ref2va v3
- Binding minimax-h3-ref2va-dynamic-v3
- 该 Run 的 script_normalize 请求没有 target_prompt_contract
- 只能用于复核历史 v3 PromptArtifact、引用和媒体证据

#### 生产配方

- 复用 published schema v2 unattended Recipe v4
- 17 个阶段完整
- bible_image_generation 指向 z-image-turbo；media_generation 指向 H3 Deployment + Workflow v3

### 创建真实 Run

1. 进入项目的“一键生产”，从空项目生成 1 集、2 镜剧本；两个镜头分别描述捡起发光怀表和举起怀表看见远处神秘人影。
2. 由剧本生成并确认人物、雨夜废弃车站和发光怀表的文本 Bible；使用 z-image-turbo 生成候选图并为三类实体各确认唯一锚点。
3. 确认两个 Shot 都绑定同一人物、场景和道具锚点；该历史 Run 最终冻结 Recipe v4、H3 Workflow v3 和 PromptProfile v3 Binding。
4. 按 Shot 的 conditioning_mode=Ref2VA 读取 input_contract.conditioning_contract.modes[Ref2VA].dynamic_reference_topology.videos。本案例 minimum_items=0、maximum_items=3，本可留空；若要验证动态参考链路，只能选择未标记 fixture_scope=workflow_smoke、与本剧同故事且人物、场景、道具不冲突的生产素材，再从合同选项中选择 purpose=motion_camera_reference。历史 smoke fixture 只保留在工作流版本验收页候选与历史审计中。
5. 不使用参考视频内嵌音轨；填写外部预算上限并明确外发授权，勾选无人值守确认后创建。
6. 等待服务端状态机完成，不在 queued/running/uncertain 时重复创建；从运行详情查看阶段、费用、Artifact 和 ReleaseBundle。

### 本轮验证状态（CURRENT VERIFICATION STATUS）

- 本轮 ProductionRun：7c06c10b-4dd8-4a19-ac78-7921371bcde7；17/17 阶段成功，8 个生成任务，实际费用 0.03 USD，总耗时约 19 分 27 秒。
- 两个 Shot 共享同一人物、场景和道具锚点；两份视频 PromptArtifact 均冻结 PromptProfile v3，并包含各自不同的镜头叙事、相同的 3 张实体图和 1 条本案例主动选择的动态参考视频。
- 两条 H3 视频均为 352×608、24fps、22 frames、917ms、H.264 + AAC；磁盘文件大小与 SHA-256 和数据库完全一致。
- 分集母版为 768×1344、24fps、48 frames、2.000 秒、H.264 + AAC；ReleaseBundle c5a6ae29-f3a7-4d13-b402-c2945323db5a 已 sealed，签名与在线文件完整性验证均通过。

### 质量与口径提示

- 原生 Ref2VA 会读取动态参考视频的完整画面，purpose 不能把它隔离成纯动作或纯运镜信号；参考内容可能显著影响人物、场景、道具和构图。工作流 minimum_items=0 时优先留空；确需绑定时，只能选择同一故事、且与目标镜头人物、场景、道具不冲突的生产素材。
- Bible 锚点解决人物、场景、道具的一致性；镜头叙事是服务器组装详细描述时使用的上游事实。该历史 v3 Run 不能证明当前 schema v6 declarative_v1 六段 Profile、六个 reference_context.* 原语与逐字段镜头级上下文；必须查看新 Run 自己冻结的 Profile 和真实执行证据。
- 另一次 R5 Run f2cce7ef-ca4f-4928-98dd-d8586e8fbadc 验证过 PromptProfile v4，但没有动态参考视频，也早于当前 H3 逐字段 target_prompt_contract；这些历史 Run 都不能证明 v8、动态视频、容量分批和新逐字段合同的联合真实执行。
- 抽帧显示两镜人物服装与发型、站台结构和发光怀表保持一致；镜头 2 的举表与远处人影符合叙事，镜头 1 却把“拾起”表现成逐步放下，技术链路通过但动作方向质量仍需优化。
- 56 frames 仅是单元测试 fixture；本案例 H3 每个 1 秒业务镜头按合同冻结为 22 frames / 917ms。
- 图 9 只示意运行详情的阅读位置；页面中的其他历史数据不属于 7c06c10b-…。本案例结论以该 Run 持久化的 Stage、PromptArtifact、Artifact 和 ReleaseBundle 证据为准。

## 故障排查

### 页面出现英文机器值

常见原因：该值可能是 API 枚举、能力代码或新状态。

1. 先读中文主标签和括号内机器值。
2. 未知状态会显示“未知状态（raw_code）”，请保留 raw_code 便于排查。
3. 不要修改 API value 来追求中文显示。

### 安全边界出现“无法分类，已失败关闭”

常见原因：服务端返回的 configuration_class 缺失、拼写错误或是前端尚不认识的新合同值。

1. 记录红色提示中的配置键与原始分类值。
2. 不要把记录改成普通配置或依赖前端默认 standard。
3. 修复服务端数据合同并重新读取；分类恢复为 security_boundary 或 standard 后再继续。

### 按钮点击后长时间 running

常见原因：Probe、Smoke 和 ProductionRun 都是服务端持久任务。

1. 查看任务 ID、heartbeat 和 updated time。
2. 只轮询当前任务，不要重复提交。
3. 超过超时再按错误原因处理或安全取消。

### 状态 uncertain

常见原因：请求可能已被远端接收，但本地没有确定响应；这不是普通字段校验失败。

1. 立即停止自动和人工重复提交。
2. 核对 Provider 控制台、请求时间窗和账单。
3. 由管理员追加消歧记录；只有确认未提交或已得到确定终态后，才能按正式恢复动作继续。

### script_normalize 两类已知响应校验连续不通过

常见原因：Provider 已返回内容，但响应未满足冻结字段合同，并分别记录为 provider_response_invalid 或 prompt_normalize_contract_failed。

1. 先核对错误码、失败 batch、尝试序号、唯一 KNOWN_RESPONSE_REJECTED 审计和运行快照中的 production.prompt_field_retry_limit。已成功批次不会重跑。
2. 展开失败与安全修复审计：只接受受控 reason_code、数值 validation_context 和 request_index；若下一 attempt 使用了服务端派生 retry_feedback，页面会只读显示 repair 关系和 SHA-256，不显示模型原文或反馈正文。
3. 0 表示首次此类失败后转人工；2 表示最多 3 次此类尝试。达到上限后 script_normalize_review 才会进入 awaiting approval。
4. 在人工确认页逐项补全全部空白冻结候选并审核；失败输出不会成为候选，人工文本仍须通过本次运行冻结的 PromptProfile 字段合同。

### script.generate、script.structure 或 visual_rules.generate 整体响应合同连续不通过

常见原因：Provider 已返回确定响应，但安全审计证明输出被截断、缺少最终文本，或没有满足该 operation 的受控 JSON 外壳。

1. 核对失败 operation、尝试序号、唯一 KNOWN_RESPONSE_REJECTED 审计和运行快照中的 production.text_contract_retry_limit；不要误看 production.prompt_field_retry_limit。
2. 0 表示首次此类失败后转人工；2 表示最多 3 次此类尝试。只重试当前失败阶段，不回放已经成功的上游或其他 operation。
3. 达到上限后停止自动重试并等待人工确认；先检查真实输入、模型输出能力和已发布 operation 规格，再决定修改配置、模型或内容后继续。
4. 内容过滤、uncertain、执行前门禁、Smoke/配置漂移或审计缺失不属于该策略；必须按各自原因处理，禁止为了“跑通”手动重放付费请求。

### chat_request_options 或输入/输出建议阈值保存、发布或提交前校验失败

常见原因：选项对象缺失、不是可冻结 JSON、包含敏感/保留/传输字段，或建议阈值高于对应硬容量。

1. 按 operation 核对必填合同：prompt_normalization 是四项容量加 chat_request_options，visual_rules 是 max_input_bytes、max_output_tokens、timeout_seconds 加 chat_request_options；无额外选项时也要显式填 {}。
2. input_byte_warning_threshold 和 output_token_warning_threshold 只用于 warning，可省略且不能高于对应硬容量；具体容量、建议阈值和 chat_request_options 必须读取本次选择的已发布 ModelProfileVersion 冻结规格。
3. 配置本身无效属于提交前硬错误；单次完整请求或正常响应超过建议阈值不失败、不重试。

### prompt.normalize 或真实 Smoke 提示容量/输出问题

常见原因：可能是基础输入或单项超过真实硬容量，也可能只是输入字节或输出 token 超过建议阈值。

1. 先看分类：recommended_input_bytes_exceeded 与 recommended_output_tokens_exceeded 仅告警并继续；response_output_budget_exhausted 表示 Provider 已截断，必须硬失败。
2. 核对 max_input_bytes、max_output_tokens、max_items_per_batch、timeout_seconds 四项同版本发布；两个可选建议阈值不得高于各自硬上限。
3. 普通多项输入会确定性拆批；只有基础输入/单项超过 max_input_bytes 或 Provider 截断才需发布新模型版本或缩减真实合同，不能把建议阈值越界误当失败。

### 字段重试没有开始而是直接阻断

常见原因：可能是 pre-submit、provider_request_rejected、Provider Smoke/配置或目标合同漂移、审计缺失等非重试条件。

1. 查看阻断原因代码，区分两类已知响应失败与其他执行、合同或审计问题。
2. 返回对应 Provider、Deployment、配置、冻结合同或审计链修复源问题；uncertain 必须先消歧。
3. 不要提高 production.text_contract_retry_limit 或 production.prompt_field_retry_limit 来绕过问题；两项配置都不能绕过执行前门禁、漂移或审计完整性检查。

### 新 PromptProfile 发布或编译时提示 source projection 或 primitive 未注册、配置不完整或 contribution 未消费

常见原因：source_projection_key 不是可写的 direct_v1/declarative_v1，declarative_v1 规则没有使用 projection.output 或已登记 projection_primitive，或 contribution 的 generation source 不在该原语支持列表内。

1. 普通直出字段选 direct_v1 和 final；需要原语组合时选 declarative_v1，并将规则 source_selector 设为 projection.output。
2. 从服务端返回的目录选 projection_primitive，逐字段核对 target field、generation source、position、format 和 channel；不要输入自造名称或脚本。
3. 若目标协议现有原语无法表达，停止发布，由开发侧新增、测试并登记受控原语后，再创建 schema v6 Profile 版本；不要在主编排加模型名分支。

### 动态参考视频没选时创建或审核按钮不可用，或页面禁止选择

常见原因：页面先按 Shot 的 conditioning_mode 选中冻结 Workflow 的 input_contract.conditioning_contract.modes[]，再读取 dynamic_reference_topology.videos 的 minimum_items/maximum_items，不再使用固定必选或最多一条规则。

1. 打开当前 Shot 所选工作流版本，核对 conditioning_contract 中当前 mode 的 dynamic_reference_topology.videos 基数。
2. minimum_items=0 可留空，maximum_items=0 禁止选择；其他组合按 minimum_items..maximum_items 选择完整集合，人工、辅助和全自动均执行同一合同。
3. 动态参考不是 motion-only：只按该范围选择同故事、且人物、场景、道具不与目标镜头冲突的生产素材；页面和服务端会复验数量、用途、配对、互斥与 Asset 快照。

### Comfy 长节点没有量化进度并在固定 idle 时间失败

常见原因：冻结执行监控合同没有覆盖该节点类型，或节点遥测缺失时无法命中受控规则。旧任务如果缺少 GeneratedWorkflow/GenerationTask 双层合同会直接 fail closed，不再使用 legacy 回退。

1. 在下一模型能力版本的类型化字段配置公共 default/node type；节点类型使用精确 class_type，所有值必须是 30–86400 的整数秒。确需工作流节点 ID 专属规则时由开发侧扩展受审 compiler/导入合同，不在版本验收页手填 JSON。
2. 重新扫描并纳管 candidate，生成 draft、提升版本、Live Validate、真实 Smoke、发布，再创建新 Recipe/New Run；不手工修改历史快照。
3. 核对任务 hard deadline 大于所需窗口；若 WebSocket 不报告节点切换，应使用足以覆盖整次远端执行的 default_idle_timeout_seconds。

### 发布失败

常见原因：草稿依赖、证据、Schema 或内容到交付闭环不完整。

1. 打开服务端错误详情。
2. 返回产生问题的 Provider/Model/Workflow/Recipe 源对象修正。
3. 重新校验和烟测后再发布，不在展示层兜底。

### 改配置后 Run 没变化

常见原因：Run 可能已冻结旧 snapshot，或 Revision 尚待新 boot 激活。

1. 核对 Run 的 config revision/SHA。
2. 检查 restart_required 与 activation state。
3. 需要新参数时创建新的 ProductionRun，不改写历史运行。

## 最终检查单

- [ ] 我能区分 active、published、validated、smoke passed 和 fresh。
- [ ] 我没有把 Token、密码或 Secret 放进普通字段、JSON、截图或文档。
- [ ] 我已核对安全边界分类、system 作用域和当前来源；知道 staff 只读、只有 superuser 可修改，也不会把建议值当成绝对阻断上限。
- [ ] 我已用注册驱动、ModelProfileVersion capability、Deployment/Workflow 与新鲜真实 Smoke 核对能力合同，没有把 Provider 说明文本或 PromptProfile 当成能力授权或候选来源；Probe 只用于健康和能力快照检查。
- [ ] 我已核对 prompt_normalization 的四项必填硬容量、必填 chat_request_options 与可选 input_byte_warning_threshold/output_token_warning_threshold；知道建议阈值只告警，而超过硬容量或 Provider 截断仍硬失败。
- [ ] 我已核对 visual_rules 的三项必填硬合同、必填 chat_request_options 与可选质量建议，并确认它们来自本次选择的已发布 ModelProfileVersion，而非退役版本或文档示例。
- [ ] 模型、工作流、提示词 Binding、Recipe 和 Effective Configuration 指向同一目标链路。
- [ ] Probe、Smoke、Readiness 和 Recommendation 都基于当前事实，没有复用过期快照；ModelProfileVersion 或 chat_request_options 变更后已对当前 target SHA 重跑所需真实 Smoke。
- [ ] 我已核对 production.text_contract_retry_limit；知道它只处理 script.generate、script.structure 与 visual_rules.generate 的已知整体响应合同拒绝，内容过滤、uncertain 和执行前失败不消费该重试。
- [ ] 我已核对 production.prompt_field_retry_limit；知道它只处理两类已知响应校验失败且要求唯一 KNOWN_RESPONSE_REJECTED 审计，其他失败不消费该重试。
- [ ] 我知道 retry_feedback 不是系统设置项，只能由服务端从受控 reason_code 与数值 validation_context 派生；页面只读显示下一 attempt 的 repair 关系和 hash，不显示模型原文或允许编辑反馈。
- [ ] 我能区分目标字段 field_definition.key/profile_field_key、主要内容来源 generation_source_selector 和最终提示词取值 source_selector，并已核对 generation_instruction 与 output semantics。
- [ ] 我已判断本次接入是纯配置（configuration-only）还是需要新原语（primitive-required），并已选择 direct_v1 或 declarative_v1；声明式规则的 projection.output 与 projection_primitive 已逐字段核对。我知道 H3 六段只是可复制配置案例，新模型复用现有原语时不应修改主编排。
- [ ] 我知道三条生产线都按 conditioning_mode 读取 input_contract.conditioning_contract.modes[].dynamic_reference_topology.videos 判断动态视频基数；人工/辅助逐镜页会显示并复验所选 route 的 0–3 条范围，且不会用无关视频绕过素材门禁。
- [ ] 我已为无量化进度的长节点核对 execution_monitoring：模型版本维护默认值/节点类型，节点 ID 规则来自注册 projector 且验收页只读；idle 只在节点切换时续期一次，hard deadline 始终不续期。
- [ ] 我知道 Recipe 冻结阶段与精确执行路由；ProductionRun 再共同冻结 Recipe、Prompt Binding、有效配置、项目输入、参数和费用。
- [ ] 遇到 uncertain 时我会先消歧，不会直接重复提交。

## 常用术语速查

- **服务提供方（Provider）**：登记调用驱动、地址和受管凭据引用；支持模态/声明操作只是运营说明，不参与服务端执行授权或候选筛选。
- **能力合同（Capability Contract）**：由驱动注册能力、ModelProfileVersion capabilities、Deployment/Workflow 合同与新鲜真实 Smoke 共同证明模型能执行哪些 operation/mode；Probe 只确认健康和能力快照，说明文本和 PromptProfile 不能授予能力。
- **条件输入合同（Conditioning Contract）**：位于 input_contract.conditioning_contract 的版本化数据合同；按 modes[] 声明每种条件输入模式、输入槽、实体 Bible 与原生音频要求，并由 Shot 的 conditioning_mode 精确选择。
- **动态参考拓扑（Dynamic Reference Topology）**：位于条件模式内，声明动态视频/音频用途、基数、明确默认用途、配对、互斥和受控预处理 adapter；页面与服务端读取同一份冻结数据。
- **模型部署（Deployment）**：把已发布模型版本绑定到可执行主机或远端服务。
- **工作流（Workflow）**：描述 ComfyUI 节点图、参数合同和输出。
- **执行监控合同（Execution Monitoring Contract）**：模型版本用类型化选项维护公共长节点静默窗口；工作流专属节点规则只由注册 projector 派生并在验收页只读展示。ProductionRun 与 GenerationTask 冻结有效合同和 SHA-256。
- **静默时限（Idle Deadline）**：进入新节点时设置一次的无活动上限；重复 running 心跳或同节点采样进度不会无限续期。
- **硬时限（Hard Deadline）**：任务冻结且永不续期的绝对执行上限；所有 idle 窗口都受其限制。
- **模型无关提示词合同（Model-neutral Prompt Contract）**：保存已确认内容事实的引用，声明目标字段如何取得、生成和校验，再按冻结顺序、通道、标签与分隔确定性形成 PromptArtifact。
- **提示词策略（PromptProfile）**：提示词合同的版本化配置载体，保存字段内容合同、source_projection_key、最终装配规则和通道长度边界；不负责声明模型执行能力。
- **目标字段（Destination Field / profile_field_key）**：一条规则最终要产生的稳定业务字段；由 field_definition.key 定义，不等于主要内容来源或最终装配来源。
- **来源投影（Source Projection）**：由 PromptProfile 的 source_projection_key 选择的受控确定性来源转换。direct_v1 不额外转换；declarative_v1 按字段规则产生 projection.output；历史 classified_reference_context_ir_v1 只用于冻结运行。
- **投影原语（Projection Primitive）**：平台代码中已登记、范围明确并经过测试的确定性转换能力。FieldRule 在页面选 projection_primitive 并决定目标字段、顺序、格式和通道；配置本身不执行代码。
- **提示词产物（PromptArtifact）**：目标字段通过冻结合同后，由同一 PromptProfile 确定性组装形成的下一阶段输入。
- **绑定规则（Binding）**：把提示词策略与精确模型版本、操作和模式关联。
- **生产配方（Production Recipe）**：冻结从剧本到交付的阶段顺序、审核门禁和精确执行路由。
- **生产运行（ProductionRun）**：一次可审计、可恢复的正式短剧生产；创建时共同冻结 Recipe、Prompt Binding、有效配置和项目输入。
- **主机探测（Probe）**：读取运行主机当前能力并形成带时间的事实快照。
- **真实烟测（Smoke Test）**：用最小真实请求确认部署或工作流可以执行。
- **配置修订（Revision）**：不可变的配置变更版本。
- **安全边界（Security Boundary）**：仅 system 作用域、只有 superuser 可修改的绝对保护值；缺失、未知或超限时失败关闭，不能被项目、Run、模型或工作流默认值放宽。
- **就绪检查（Readiness）**：配方与路由中的平台级结论按全部 published Recipe 汇总生产闭环；运行主机中的主机级结论只针对所选 snapshot 的 capability，二者不能混用。
- **冻结快照（Snapshot）**：在运行开始时固化、后续不能静默改写的事实。

---

待优化计划与当前能力分开维护。Provider 能力声明字段的收敛计划见 [`OPT-SETTINGS-001`](optimization/README.md)。

正文唯一来源：`frontend/src/content/systemSettingsGuide.json`。网页路由 `/manual/settings` 与本 Markdown 使用同一份结构化数据。
