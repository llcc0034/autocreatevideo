# 23. 无配方人工制作的完整路径

> 历史方案／登记依据，不是当前施工清单；取消或延期事项不会因本次整理恢复。

[总索引](../../README.md) · [当前续接](../../current.md) · [按日期查证据](../../records/README.md)

<!-- source-section 8368e6852e31 -->
<a id="23-无配方人工制作的完整路径"></a>

## 23. 无配方人工制作的完整路径

<a id="231-创建及页面"></a>

### 23.1 创建及页面

项目工作台提供并列“使用配方生产”和“人工逐步制作”。后者直接选择项目/剧集范围、已有内容或从空白开始、交付目标和预算，不展示必选Recipe，不自动创建Recipe，也不预选模型。沿现有ProductionRunView管理同一ProductionRun，SeriesStudioView提供创建入口。ProductionManualView对应`/manual`操作手册，不是制作台；此页面落点修正已于2026-09-06获用户确认。

POST /production-runs/增加严格联合：
- plan_source=recipe：recipe_version_id必填，manual_plan禁止。
- plan_source=manual：recipe_version_id必须缺省/空；manual_plan含受控stage_catalog_key、期望目录SHA、项目/剧集范围、已存在内容修订引用及交付选项；不允许客户端上传任意handler/DAG。
两者均使用原幂等、权限、预算字段，返回同一Run详情URL。新增GET /production-runs/manual-options/返回目录、可用步骤、真实前置缺口及版本；不做模型调用。

manual Run创建后等待明确动作，不自动执行AI、装配或发布。没有剧本/素材是待完成事项，不伪造ScriptRevision；首次AI调用仍需明确实例、输入和费用授权。用户可以直接提供人工剧本或采用现有素材满足前置，不强制先跑AI。

<a id="232-阶段目录与执行动作"></a>

### 23.2 阶段目录与执行动作

将现有阶段职责/输入输出合同从Recipe解析和stage handlers之间提取成唯一受控目录。manual与schema3 Recipe都引用它；目录是已实现业务操作集合，不是隐藏配方。首期复用已有剧本、结构化/Bible/镜头内容、提示词、参考/镜头媒体、审核/选片、装配、交付职责；步骤配置只引用已有受审handler，具体组合/输入来源进入显式计划，不新写模型专用阶段模板或任意DAG作者工具。

POST /production-runs/{id}/manual-actions/：
- 公共字段：action、stage_key、scope/unit_key、expected_run_plan_sha256、expected_selection_revision（影响选片时）、idempotency_key。
- generate：精确execution_instance_version_id、inputs、parameters、field_values或manual_channels、source revisions及本次费用授权。创建该单元Preparation，返回202及其URL。
- adopt：精确已拥有的Asset/Artifact/内容修订及用途，或者人工文本（走现有正式内容修订服务）；类型/权限/版权/前置检查后记录真实来源与既有Decision，返回200/201，不造AI Attempt。
- approve：明确待批准对象/修订及理由，复用现有正式人工审核，不能通用“全部通过”。
- assemble：引用本Run已采用的精确产物与交付范围，沿原AssemblyJob/ProductionAssemblyLink返回原异步任务引用，完成只来自worker事实。
- deliver：原worker完成转码后，人工明确核验并封存ReleaseBundle；只读取已有文件和签名，不编码、不新增交付队列、不对外发布。核验、发布包、人工决定、Run完成和幂等回执同事务；首次返回201与发布包引用，重放返回200并重新核验。此语义经用户于2026-09-10确认，取代把封存误称异步任务的原表述。
- 同key异body409；回执与任务/决策同事务写，重放返回相同引用。非manual Run拒绝此动作入口，继续原Recipe控制API。

manual动作只授权当前步骤；完成后更新可操作事项，不自动选择下一模型。两个镜头可以并行有不同实例；一个镜头多个候选由不同准备区分，原ArtifactSelectionDecision管理采用，不能用“最后完成的产物”替代人工选择。

<a id="233-冻结范围与修改规则"></a>

### 23.3 冻结范围与修改规则

RunPlan冻结项目/剧集边界、阶段目录及交付合同。每个步骤使用明确的当前内容修订及已采用上游；每次Preparation独立冻结。人工维护内容仍经现有ContentRevision/ScriptRevision等正式入口，不直接改准备快照。

更改上游采用结果不会覆盖旧Attempt/Decision。以新Decision修订为当前选择；下游显示其依据已变化，需要明确重做或重新审核采用，不能静默标为仍匹配。UI展示受影响镜头和任务，批量重做仍需明确数量/费用授权。

为复用现有每Run/剧集唯一装配关联，首期一次Run只封存一套正式交付：
- 装配提交前允许重生成/改选，保存修订与影响关系。
- 装配提交时冻结准确选片集；之后需要换片/重剪，使用“新建人工制作轮次”，创建新manual Run并显式引用可复用真实输入，不能覆写原装配成员。
- 已完成Run只读；不把新一轮制作当作原任务网络重试，不复制已通过阶段状态。
该边界必须在页面提示，避免宣称首期提供任意无限次原地剪辑能力。

<a id="234-前置完成与失败"></a>

### 23.4 前置、完成与失败

只有需要的真实前置事实已满足，步骤才可执行；允许通过人工输入/采用满足，不要求每个前置都来自AI。素材来自既有Asset时，GeneratedArtifact记录source=adopted及真实来源，不假称本Run模型生成。

manual模式的“继续”只重新检查/恢复已经授权的准备或确定性在途任务，不自动执行下一AI步骤。未操作阶段显示待处理，不能因缺少Recipe而自动skip。采用既有结果满足的步骤需正式Decision证明，不伪造生成成功。

Run成功需所选交付范围的有效选片、已完成媒体探测/装配、真实ReleaseBundle及必要批准；不以任务队列为空判成功。单镜失败可重试该准备或创建新候选；取消/uncertain/费用均沿原链，部分结果保留真实状态。

<a id="235-最小改动落点与正式文档"></a>

### 23.5 最小改动落点与正式文档

必须修改：ProductionRun模型/Serializer与创建服务、统一RunPlan读取、Recipe解析及人工阶段目录、逐单元prepare调用、Approval/Selection接入、assembly/delivery前置校验，以及ProductionRunView、SeriesStudioView的工作台入口与交互。操作手册只同步说明。P0逐项搜索run.recipe_version与recipe_definition_sha256，收敛到明确plan_source/plan SHA；新人工路径不得通过空Recipe对象兜底。

正式实施时同步根openapi.yaml、docs/api.md、docs/architecture.md、docs/operations.md及相关主文档，说明ProductionRun仍是唯一生产入口，但不再普遍必选Recipe。本次设计不提前把当前主文档改成已经支持。
