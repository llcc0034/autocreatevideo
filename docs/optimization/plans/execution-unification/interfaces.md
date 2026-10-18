# 8. 页面与接口调整范围

> 历史方案／登记依据，不是当前施工清单；取消或延期事项不会因本次整理恢复。

[总索引](../../README.md) · [当前续接](../../current.md) · [按日期查证据](../../records/README.md)

<!-- source-section 860ce71e0508 -->
<a id="8-页面与接口调整范围"></a>

## 8. 页面与接口调整范围

<a id="81-页面"></a>

### 8.1 页面

1. 在现有系统设置中提供执行实例的创建、后继版本、只读配置、复核、发布、启停、烟测和关联查看；保留现有模型／部署资源入口，不另建平行控制台。
2. 表单按“能力和部署 → 工作流（适用时）→ 提示词方式 → 输入及参数 → 配置预览”组织；选项由后端返回。可自动补充的内容标明来源；不自动保存或发布推荐。
3. 素材库仅显示与任务输出合同匹配的执行实例；展示就绪原因和不可用原因。实例切换时清理或重新确认不兼容输入，防止旧表单值串用。
4. 扩展现有合同输入控件：素材库选择、本地上传、实体／镜头用途、动态数量、配对关系、参数范围和最终提示词预览。
5. 配方编辑器将部署／Workflow／策略多选项收敛为实例版本选择，保留阶段输入映射、失败策略和预算设置；不重建整个可视化编排器。
6. 在现有ProductionRunView项目生产工作台增加“配方生产／人工逐步制作”两种模式，SeriesStudioView提供创建入口：直接创建、逐步选择实例、人工内容、字段审核、候选采用、装配与交付；流程合同与接口见第20、23节。Recipe模式保留原功能。ProductionManualView是操作手册，只同步使用说明，不改造成制作台。
7. 本期只处理必要控件和关联展示；全局布局、主题、卡片密度及分块优化仍属于独立视觉待办。

<a id="82-api-调整合同实现状态见第025节"></a>

### 8.2 API 调整合同（实现状态见第0、25节）

| 接口职责 | 最小调整方向 | 副作用与验证要求 |
| --- | --- | --- |
| 执行实例目录及版本生命周期 | 在既有平台 registry 增加第 20 节指定资源 | 管理权限、不可变版本、乐观并发校验、审计 |
| 输入合同与预览 | 返回来源、范围、槽位、依赖及 blocker；预览返回规范化绑定与文本 | 不排队、不调用模型、不自动持久化业务配置 |
| 素材生成创建 | 保留现有任务端点，改为接收精确 execution instance version 及业务输入 | 新协议拒绝独立 deployment／workflow／binding 组合；返回持久任务 |
| Recipe 与 ProductionRun 创建 | 精确实例版本替代阶段执行目标组合；保留现有运行端点 | 版本冻结、幂等、预算、权限、人工审核不变 |
| Smoke、证据及关联查询 | 扩展现有 API 接收／返回执行实例上下文 | 复用 Attempt，不创建平行的 Smoke 任务系统 |
| 清理影响预览与执行 | 复用现有 registry 管理页面/API、摘要及事务锁，允许清单内历史/审计随依赖删除 | 不新建旧配置转换/映射系统；不提供任意 SQL 或无范围的强制删除 |

不新增一个覆盖全部业务状态机的 `/execute-anything` API。统一内部准备与校验，不等于把素材任务、生产运行、烟测揉成一个接口。

实施时同步根 `openapi.yaml`、`docs/api.md`、相关 Serializer、前端类型与调用适配器；本计划不在正式 API 文件中预先发布尚未实现的字段和路径。

<!-- source-section 41c4f8d3ac28 -->
<a id="20-api页面和生产接线"></a>

## 20. API、页面和生产接线

<a id="201-公共规则"></a>

### 20.1 公共规则

以下为统一接口合同；已实现子集以根OpenAPI、Serializer、前端调用及第25节验证记录为准，未完成项见第0节。实施时同步这些入口和docs/api，不把全表标成尚未实现或全部已上线。统一/api/v1前缀及既有错误信封、分页/鉴权/CSRF：
- 管理配置与Smoke需管理员；素材/生产/字段需真实owner权限，跨项目不可借ID越权。
- 配置写expected_revision，版本动作expected_definition_sha256；字段/恢复写expected_preparation_revision，选择写expected_selection_revision。
- 幂等同key异body409，相同body返回原对象及当前真实状态；检查幂等重放先于失效旧revision检查，但仍校验读取权限。
- 配置创建201、修改200；异步首次202+Location、重放200；结构错误400、权限403、不可见404、冲突/冻结409。
- 准入返回configuration_state、evidence_state、capacity_state、warnings、blockers、can_submit。可执行但未认证时can_submit=true且warnings非空，不能显示为验收通过。

<a id="202-注册与执行准备接口"></a>

### 20.2 注册与执行准备接口

| Method / Path | 核心请求 | 结果及调用入口 |
| --- | --- | --- |
| GET, POST /model-execution-instances/；GET, PATCH /.../{id}/ | key/name/description/is_active及revision | 系统设置注册、启停与只读详情 |
| GET, POST /model-execution-instance-versions/；GET, PATCH /.../{id}/ | 精确部署、执行operation/mode、prompt_mode、策略/Workflow及inputs/parameters/auxiliaries | draft创建或原子配置更新，返回派生合同及SHA；提示词用途从策略只读派生 |
| POST /model-execution-instance-versions/{id}/publish/、clone-successor/、retire/ | revision、definition SHA、reason（适用时） | 原生命周期；不自动Smoke |
| GET /model-execution-instance-versions/{id}/input-contract/ | 无 | 动态表单、范围和来源；不改配置 |
| POST /model-execution-instance-versions/{id}/preview/ | inputs、parameters、field_values或manual_channels、subject_facts | 纯预览+缺字段，不保存确认/不调用AI |
| POST /model-execution-instance-versions/{id}/smoke-test/ | fixture、definition SHA、幂等/预算授权；可带reuse_preparation_id及明确字段选择 | 原SmokeAttempt+Preparation，缺字段可awaiting_input |
| GET /model-execution-instance-versions/{id}/evidence/ | 状态/分页 | 精确组合证据，不混入其他能力 |
| POST /asset-generation-jobs/ | execution_instance_version_id、inputs、parameters、field_values或manual_channels、subject_facts、原素材业务字段、幂等/费用 | 根Job及Preparation；缺人工字段也可创建preparing |
| GET /execution-preparations/{id}/ | 无 | 状态、只读intent/最终请求、字段选择、子任务、费用及warnings/blockers |
| POST /execution-preparations/{id}/continue/ | expected_preparation_revision、可选confirm_and_select列表 | 幂等确认推进；不重复子外呼，不改已冻结请求 |
| GET, POST /execution-field-values/ | preparation_id；POST unit_key/field_key/text/revision | 有权修订列表/创建pending |
| POST /execution-field-values/{id}/confirm/、reject/ | expected_source/text SHA、expected_preparation_revision、select_for_execution、选择并发值 | 确认与采用，旧confirmed不自动替换 |
| 原Job/Run/Smoke取消、重试、预算授权、消歧接口 | 保留本身字段，补准备上下文 | 原正式动作；成功解除阻断通知advance，不触发隐式付费重试 |

普通请求不接受deployment_id/workflow_id/binding_id独立组合，不接受parent_preparation、resource scope或自动批准伪造字段。删除走registry影响预览与执行；清单内历史/审计闭包可删除，规则见第11节，不添加无范围的强制DELETE。

字段批量确认在同一准备事务原子执行；确认后不继续时仍可调整未冻结选择。“确认并继续”明确包含最终组装动作，不隐藏在只读预览中。

<a id="203-recipe与默认值"></a>

### 20.3 Recipe与默认值

Recipe只支持schema3，复用原阶段/依赖/审核/预算/fallback业务语义，不保留schema1/2运行或展示解析。AI路线引用execution_instance_version_id及expected_definition_sha256，input_sources和parameter_policies属于路线/步骤，不属于实例。新增显式字段审核合同，不把阶段批准当成字段批准。

每个单元选路后创建自己的Preparation。冻结后重试不改路线；fallback创建新准备并记录StageRouteEvent。多候选、多镜头不能共用Stage级唯一prepared_request。非AI装配/审核/交付不制造虚拟实例。

新recipe Run接受已发布schema3；新manual Run不接收Recipe，合同见第23节。两者调用get_run_execution_plan与同一stage handler。去除handler、options、readiness、字段来源与交付中对run.recipe_version直接必填访问，不以空对象/隐藏Recipe兜底。

指定Deployment的AI默认配置键改为精确实例版本引用；只作为明确选择候选/默认推荐，不在worker里隐式替换。ConfigRevision仍是唯一配置来源。清理旧键和相关不兼容修订/引用，不保留旧名称别名或旧Run展示入口。

<a id="204-错误合同"></a>

### 20.4 错误合同

新增execution_definition_conflict、execution_target_drift、input_mapping_invalid、parameter_locked、field_review_stale、field_selection_conflict、preparation_frozen、manual_action_not_ready、run_plan_source_conflict。沿原信封返回具体path、原因和下一步；不泄露凭据或原始第三方错误正文。已删对象返回普通404，非法Schema/字段返回标准400；不为已移除旧协议新增专用处理器或重定向适配。

execution_evidence_expired和execution_capacity_unknown/uncovered在默认策略下可为warning，策略要求时才为blocker；响应必须带severity及policy来源，不能一边200放行一边用“失败”样式。缺少真实组合通过、已知失败或硬合同错误仍明确拒绝外呼。
