# OPT-SETTINGS-UI-002 ComfyUI 模型版本的工作流上下文入口

> 历史方案／登记依据，不是当前施工清单；取消或延期事项不会因本次整理恢复。

[总索引](../README.md) · [当前续接](../current.md) · [按日期查证据](../records/README.md)

<!-- source-section 746e659974da -->
<a id="opt-settings-ui-002-comfyui-模型版本的工作流上下文入口"></a>

## OPT-SETTINGS-UI-002 ComfyUI 模型版本的工作流上下文入口

<a id="实施状态2026-09-04"></a>

### 实施状态（2026-09-04）

- 状态为 `已完成`。模型页进入现有工作流中心的精确上下文入口已经落地，不复制扫描器、合同草稿、Validate 或 Smoke 状态机；candidate/draft/actionability 接口、页面、乱序恢复回归、类型检查、生产构建和正式桌面浏览器只读验收均已通过。
- `WorkflowTemplateVersion.model_profile_version` 仍是可执行工作流到模型版本的权威关系；来源阶段的关系改由精确 candidate/draft UUID 与冻结 provenance 表达，不使用文件名、模型族或前端本地映射。
- 工作流分区校验 URL 中的 `model_profile_version` UUID，完整分页读取已纳管版本，并在全局 discover 结果上按 candidate/draft/version 证据过滤。页面显示服务端 `admission_tier`、`actionability`、blockers 和 recovery actions，不再维护 `eligible/review/contract_blocked` 客户端平行分类。
- 上下文横幅持续显示模型 Key、版本、Provider、完整 UUID、直接关联数量和生命周期引导；一个模型版本关联 0、1 或多个工作流时，均在同一列表展示版本、Live Validate、Smoke 和证据状态。
- URL 上下文和扫描都有请求代次保护；切换或清除模型后，旧扫描、preview 和证明恢复结果不得回填当前页面。点击模型入口本身不会扫描、纳管 candidate、生成 draft、Validate、Smoke、发布或生成。

<a id="已落实方案-1"></a>

### 已落实方案

1. 在精确模型版本的操作区增加工作流上下文入口；判断条件使用该版本 Provider 的注册驱动类型，不按名称猜测。
2. ComfyUI 草稿显示“从工作流提取并证明”，已发布版本显示“管理工作流关联”，已退役版本只允许“查看历史工作流”；非 ComfyUI Provider 明确为不适用。
3. 入口跳转到现有“系统设置 → 工作流”分区，并携带精确 `model_profile_version` UUID 与操作意图；目标页持续显示当前模型 `profile_key`、版本号和精确 ID，并允许用户清除上下文查看全部。
4. 查看、扫描、candidate 纳管、合同草稿、模型文件证明、模型发布、版本提升、Live Validate、Workflow Smoke 和发布继续复用现有工作流中心及服务端合同。
5. saved-workflow 主列表保留 discover 原始事实，并按精确 Provider、candidate source、contract draft、model profile/version provenance 派生当前上下文；服务端 `admission_tier/actionability` 是动作准入唯一口径。
6. 一个模型版本关联多个工作流版本时展示完整列表及生命周期、校验和 Smoke 状态；数据量增长后沿用服务端过滤与分页，不只过滤当前页。

<a id="实现范围"></a>

### 实现范围

1. 扩展现有前端工作流列表读取方法，使其透传服务端已经支持的 `model_profile_version` 精确过滤参数；不新增重复列表 API。
2. 在模型版本操作区增加条件入口，并让工作流分区解析、校验、保留和清除 URL 中的精确模型版本上下文；刷新页面不得丢失或静默改选。
3. 复用 candidate/contract draft API、模型文件证明和 batch promotion；可预设当前模型上下文，但不能绕过 candidate、主输出、projector、结构 SHA、Live Validate 或发布门禁。
4. 原始 `savedWorkflowItems` 仍是一次 discover 的完整事实；过滤只负责展示，动作是否可用读取服务端 actionability/recovery actions。
5. 补充扫描总数、当前模型精确来源/草稿/版本、其他扫描结果和折叠状态；零文件显示明确空状态，不新增模型—工作流绑定表。

<a id="边界与暂不做事项"></a>

### 边界与暂不做事项

- 唯一关联事实继续是 `WorkflowTemplateVersion.model_profile_version`；Artifact 使用关系继续来自 `WorkflowModelRequirement.artifact`，不得增加前端本地映射、模型字段 JSON 或第二套绑定表。
- 已存在的不可变工作流版本不能原地改挂到另一个模型版本；需要变更时必须从新 revision 导入新的工作流版本。
- candidate 准入不要求目标模型已发布、目标执行 Deployment 或 projector；生成草稿后再按 blocker 补齐解释和执行证据。已发布模型发现新文件依赖时仍须新建下一模型版本。
- 模型页入口不携带或静默选择 `validated_deployment`。扫描来源继续按 ComfyUI source scope 处理，Live Validate 与 Smoke 后续仍显式使用精确 Deployment。
- 点击入口不自动扫描 ComfyUI、不自动选择或批量导入工作流，也不触发 Probe、Validate、Smoke、发布或真实生成。
- URL 中的模型上下文不缩小 discover 的物理目录扫描范围；它只派生主列表和只读诊断。文件名、同模型族和相似名称不作为相关性依据。
- candidate/draft 只表示来源与投影证据，不等于已关联可执行版本；必须继续完成主输出、projector、模型证明、目标 Deployment、Live Validate、Smoke 与发布。既有 published 版本保持不可变。
- 本项不顺带重构模型 selector 目录、Artifact 唯一约束、模型 Profile 拆分或历史数据，这些问题另行评估和登记。

<a id="验收条件-2"></a>

### 验收条件

- ComfyUI 草稿、已发布、已退役和非 ComfyUI 四类版本显示符合上述状态语义的入口；Provider 目录读取失败时失败关闭。
- 路由上下文只使用精确 `ModelProfileVersion.id`；版本不存在、已失效、Provider 不匹配或候选不兼容时显示具体原因，不自动切换其他版本。
- 相同 `profile_key` 的版本 A 与版本 B 不混用工作流；0、1、多个关联工作流及分页状态均可完整查看。
- 未纳管 candidate 不能 preview；未选择主输出不创建 draft；`candidate_only` draft 不能提升；已发布模型缺少所需 Artifact 时不能原地追加声明。
- 多个 Deployment 或多个 source scope 不会被合并为含糊默认目标；导入上下文不会提前冻结最终验证 Deployment。
- 浏览器恢复的旧导入草稿若与 URL 目标版本冲突，必须重新确认或重新预览，不能静默覆盖。
- 模型上下文扫描后，主列表不平铺其他 Provider、其他精确模型或无 provenance 的相似名称；扫描总数等于当前上下文结果与其他扫描结果之和，展开/折叠不改变恒等式或请求载荷。
- exact candidate、contract draft 与既有 workflow version 均显示自己的对象 ID、actionability 和后续动作；零文件有明确空状态，清除上下文后恢复全量展示。
- 扫描进行中切换或清除模型上下文时，旧请求的成功、失败及 restore 中途返回均被丢弃，不重建列表、证明、轮询或浏览器草稿；原请求结束后 busy 必须释放，才允许在新上下文重新扫描。
- 相关前端测试、类型检查、生产构建和系统设置浏览器验收通过；浏览器验收只检查导航、筛选与状态，不执行真实导入、模型证明、Probe、Validate、Smoke 或发布。

<a id="历史验收与重新验证要求"></a>

### 历史验收与重新验证要求

- 2026-09-02 的 45/45、10/10 和本地 `1 / 15` 结果只证明旧客户端分类方案，已被 candidate/draft/actionability 合同取代，不作为当前验收结论。
- 当前版本必须重新覆盖精确模型上下文、candidate 纳管、preview 持久 draft、candidate-only/ready 动作门禁、多工作流分页、乱序请求保护、类型检查、生产构建与浏览器只读验收；真实 Probe/Validate/Smoke/发布仍需目标环境人工执行。
