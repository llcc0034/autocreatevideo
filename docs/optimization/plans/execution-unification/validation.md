# 7. 验收、容量与错误处理

> 历史方案／登记依据，不是当前施工清单；取消或延期事项不会因本次整理恢复。

[总索引](../../README.md) · [当前续接](../../current.md) · [按日期查证据](../../records/README.md)

<!-- source-section f0c2cfd5e070 -->
<a id="7-验收容量与错误处理"></a>

## 7. 验收、容量与错误处理

- 实例就绪条件必须包含精确 operation／mode、主执行目标、策略、提示词方式、参数／槽位合同及需要的辅助能力证据；任意另一项能力通过不能授权当前实例。
- 基础 Workflow Smoke 可能只证明图可运行，不证明策略组装及上游字段生成链通过。页面分开显示基础执行证据与组合链路覆盖情况，缺少覆盖时明确提示。
- 保留证据时间、到期时间、最近任务状态、配置变化原因及跳转。失效状态由现有证据事实计算，不新增一个可人工填写的“已通过”布尔值。
- 原生可用的resource证据不补造instance身份；需旧格式解析的历史证据清理。组合证据按精确配置身份匹配，样本规格单独展示；字段正文、素材ID、seed改变不等于组合配置改变。
- 纳入容量可观察最小闭环：采集当前请求规模与真实结果；未覆盖/未知显示警告。默认允许合同内请求执行，不能把“小样本通过”展示成整个范围已经认证。
- 第17节的规格向量用于证据与告警，不是首期强制逐样本准入。仅管理员显式启用require_evidence时，未知/未覆盖才阻断；范围认证后续实施，不推断任意较小组合必然安全。
- 保持提交不确定时不自动重发、取消需远端事实确认、幂等键冲突报错、费用授权及超时语义。队列超时、请求超时、证据 TTL 和容量覆盖不得混成一个状态。
- 手工预览和 APIClient 合成测试不得写为真实 Smoke。集成测试通过与模型输出质量通过分别报告。

<!-- source-section e0eb50b16ab5 -->
<a id="12-验收矩阵与完成定义"></a>

## 12. 验收矩阵与完成定义

| 验收项 | 必测场景 | 通过标准 |
| --- | --- | --- |
| 实例生命周期 | 草稿、发布、后继、并发修改、停用、退役 | 已发布不可变；错版本及冲突明确拒绝 |
| 引用一致性 | 错模型、错部署、缺 Projector、多策略歧义 | 保存／发布／提交相应门禁阻断，不回退首项 |
| 槽位绑定 | 零／最小／最大／超限、混合素材、配对音频、同素材多用途 | 合同一致；标签、实体与实际端口匹配 |
| 参数 | 边界、越界、固定值、步长／余数、随机 seed | 服务端和页面一致；第一次实际值冻结，重试稳定 |
| 提示词 | profile／manual／none、人工字段、辅助生成、未知占位符 | 无重复装配、无隐式调用；审核结果及引用不丢失 |
| v21 回归 | 六段顺序、用途摘要、视频／音频保留、镜头时间码、通道限制 | 保存配置驱动结果；与确认规则逐项对照，不要求与旧文案逐字相同 |
| 跨入口一致性 | 相同版本、相同输入分别走素材库与生产准备 | 排除业务归属信息后，目标、绑定、参数和提示词摘要一致 |
| 精确验收 | 另一能力/策略通过、过期、配置变化、辅助链 | 身份不匹配阻断；过期按显式策略warn/block，证据事实不改 |
| 容量 | 小样本对大请求、未知规格、策略调整 | 默认告警可运行；require_evidence显式阻断；不把可运行标为已认证 |
| 持久任务 | 幂等、字段修订、取消、重试、阻断恢复、uncertain、重启 | 不重复外呼/计费，不丢任务，不重跑已完成辅助调用 |
| 权限与输入安全 | 越权 Asset、跨项目产物、路径注入、模板表达式 | 按现有权限和白名单拒绝，错误不泄露密钥／原响应 |
| 切换和删除 | 不兼容Run/配置/审计闭包、有效共享资源、重复清理、影响变化、文件失败 | 清单内物理删除，闭包外不受影响；数据库失败回滚本批，部分失败如实呈现；不生成兼容映射 |
| 无配方人工制作 | recipe FK为空、逐镜不同实例、人工输入/采用、候选、装配、交付 | 同一Run/Stage/Attempt/Artifact链，不能隐藏造Recipe或跳过事实校验 |
| 页面与文档 | 选择／切换／错误／刷新／只读版本／关联跳转 | UI、API、Schema、文档一致，未覆盖状态清晰 |

验证分层：

1. 单元和合同测试：复用 `test_reference_context`、`test_prompt_profile_compiler`、`test_workflow_projectors` 等，补新增模型／共用服务测试。
2. 隔离 API 验收：复用 `test_asset_generation`、`test_production_runs_api`、`test_production_run_orchestration`、`test_deployment_text_smoke_queue`、`test_acceptance_smoke_overview`、`test_registry_deletion`；外部响应 Mock，明确不是真实 Smoke。
3. Schema／接口／前端：`scripts/check.ps1`为统一入口；按影响执行Django check、迁移一致性、新建库/当前库清理升级测试、相关Node tests、vue-tsc和Vite build。可逆Schema变更验证往返；明确不可逆的清理验证删除结果及不可逆声明，不要求恢复旧审计。真实PostgreSQL约束/并发不得仅以SQLite通过替代。
4. 正式页面与目标环境：验证保存、配置预览、任务提交、失败展示、取消和结果追溯。真实模型／GPU 调用按实施时有效授权与预算执行，记录具体组合和规格；没有真实验证则保留该项未验证，不扩大结论。

最终完成必须同时满足：所有本轮登记的AI内容调用已接入；业务只选实例版本；三种提示词方式与动态素材链一致；无配方人工Run可正式交付；本轮明确范围内的旧执行／读取／展示和兼容字段已移除并通过残留核对；当前合同所需业务规则来自配置而非新增模型专用分支；相关主文档同步；U1-A～F有相应真实证据，无伪造就绪、Smoke或历史映射。旧批清理是带日期的事实，不是本轮再次删除数据的完成条件；确需处理不兼容数据须先核对精确影响、备份及有效授权，不能删除或改写实际任务、费用和审核来制造验收通过。

<!-- source-section b0a6f3c05041 -->
<a id="17-可用性优先的验收与配置"></a>

## 17. 可用性优先的验收与配置

<a id="171-不同层面的结论"></a>

### 17.1 不同层面的结论

| 层次 | 首期行为 | 是否允许配置放宽 |
| --- | --- | --- |
| 定义与协议正确性 | 发布依赖、target身份、必填/类型/数量/对齐、受审节点/路径和输出解析必须合法 | 不可通过容量或Smoke开关绕过 |
| 权限、版权、预算、幂等与远端不确定 | 保留原正式检查、资源并发与费用授权 | 不提供关闭全部检查的开关 |
| 组合通过记录 | 默认要求当前精确组合至少真实通过一次；最近同身份明确失败阻断 | require_passed可收紧到require_fresh；不新增“伪通过” |
| 证据TTL | 默认过期告警、可继续使用同身份真实通过记录 | 可配置require_fresh |
| 本次规格容量 | 未覆盖/未知默认告警，可在硬合同及预算内执行 | 可配置require_evidence；仅正式主烟测保留告警取样；完整范围认证后续 |

实例发布只冻结配置，不要求自己的Smoke。组合Smoke可对published实例执行，主实例的组合证据门禁豁免；unknown/uncovered容量在主烟测中仅告警，允许建立真实样本，不预先标covered。其本次新调用的辅助叶子按production用途独立检查，不继承两类豁免。叶子没有辅助依赖，先验收叶子再验收主链。Workflow自身仍走Live Validate→基础Smoke→发布，未通过的图不冒充可用依赖。

真实成功/失败证据只由对应任务完成产生。正常业务成功可作为规格观测，但首期不自动将业务任务改成组合Smoke通过；组合结果仍走正式Smoke证据链。

<a id="172-配置项与决策顺序"></a>

### 17.2 配置项与决策顺序

下列新增键统一放入ConfigDefinition/ConfigRevision及“默认值与限制”页面，本次仅设计，未修改运行配置：

| 配置键 | 值/初始默认 | 生效与说明 |
| --- | --- | --- |
| execution.admission.smoke_policy | require_passed / require_fresh；默认require_passed | 系统级管理员修改；缺少精确通过记录或最近同身份失败仍阻断；仅TTL过期可在require_passed下警告 |
| execution.admission.capacity_policy | warn / require_evidence；默认warn | 所有新协议消费者同口径；unknown/uncovered在warn下不阻断，require_evidence阻断生产及新辅助调用；正式主烟测告警取样 |
| execution.preparation.recheck_interval_seconds | integer，默认10，范围1–300 | 原worker的准备扫描间隔，不启动另一守护进程 |
| execution.preparation.claim_timeout_seconds | integer，默认60，范围10–600 | 纯数据库准备工作租约；不作为远端提交超时 |

配置缺失、非法或读取失败不能静默当warn。初始值通过迁移/正式配置修订明确登记；用户可以在上线后统一调整。本轮授权不是现在就修改真实服务配置。

所有新协议消费者及其底层readiness调用都使用此唯一准入决策。旧函数中独立强制Smoke TTL或逐样本覆盖的运行阻断必须收敛，不能页面显示warn、worker又按另一规则拒绝；也不能通过吞异常达成放行。Workflow首次发布仍保留基础真实验收，已发布资源的运行证据TTL按统一策略处理；主机不可用、凭据失效及文件不完整仍是硬阻断。

决策顺序：硬错误 → 实例精确身份/真实通过 → 当前策略对应TTL处理 → 本次规格观测 → 当前预算/并发 → 保存admission_decision并提交。普通客户端无skip_checks、ignore_capacity等越权字段。人工Run可以要求更严格，但不能覆盖管理员更严格规则；unattended采用同一系统配置并在创建预算授权时展示可能存在未认证规格，不额外隐藏另一套强制门禁。

选证据先按实例definition/target/operation/mode及scope匹配；不混用不同策略。按真实完成时间选择最近适用结果：failed不能被较早passed覆盖；未完成任务本身不是failed。TTL只影响expires_at判定，不改历史结果。

<a id="173-容量观测而非逐token强制验收"></a>

### 17.3 容量观测而非逐token强制验收

统一capacity_signature_v1记录已能可靠取得的输出尺寸/帧数/时长/steps/batch、参考数量和实际有效尺寸、必要token数量及主机/运行时。缺少性能观测维度标unknown，不造数据；Asset坏文件、缺失必需媒体信息仍是输入硬错误。

首期covered只表示相同规格有真实样本，uncovered/unknown表示没证据，默认warn允许继续。文本正文、seed、Asset UUID不直接作为规格身份；token长度变化不会在默认策略下阻断主链。托管API显示provider_managed和登记上限，不推测服务商GPU能力。

将capacity_policy设require_evidence后，未覆盖/未知阻断生产及本次新辅助调用，并显示差异。只有服务端创建的正式主Smoke允许取样，保留原容量状态及告警，记录own_capacity_gate_exempt；不放宽硬合同、真实素材、权限、预算或幂等。页面可从已完成字段准备导出精确fixture启动规格Smoke；复用已确认字段选择及来源，不重复付费生成字段。Smoke通过并产生同规格有效观测后，按正式恢复动作继续原主请求；缺少维度仍是unknown。新的范围/envelope匹配算法不纳入本期，不能未经实测声称较小组合必然安全。

<a id="174-基础资源smoke去binding"></a>

### 17.4 基础资源Smoke去Binding

两类原SmokeAttempt增加scope resource/instance及nullable execution_instance_version；准备引用由Preparation对应owner FK的唯一反向关系读取，不另写第二FK。resource时无实例FK，instance时有。仅可原生满足当前合同的证据继续使用；不把旧行批量改标resource以保住历史，需补造字段或旧解析才能读取的证据清理。组合结果不能更新Workflow基础验收状态。

文本基础Smoke新增明确的profile/manual请求联合：profile提交精确prompt_profile_version_id和受控fixture来源；manual只在能力合同允许时提交完整受控通道。不再调用select_smoke_profile、旧Binding选择或frozen Binding验证函数。基础图Smoke继续使用固定fixture及图合同，无需先选择执行实例。

资源Smoke与组合Smoke都经同一纯请求/字段服务，但由服务端动作确定purpose；客户端不能伪造resource scope跳过业务门禁。resource事实不能授权instance组合。发布、options、执行、证据查询及所有基础Smoke都完成去Binding接线；旧选择、冻结Binding校验和历史只读解析一并删除。

预备中的Smoke纳入原活动唯一约束，防止重复创建。主调用deadline在真正进入原提交队列时初始化，人工等待不消耗它；未提交可为空，已提交沿原超时/取消/消歧规则。准备扫描/恢复见第19节。

<!-- source-section bc719e97b43b -->
<a id="22-复核问题映射与编码验收"></a>

## 22. 复核问题映射与编码验收

| 问题/新增目标 | 1.3处理 | 必须通过的回归 |
| --- | --- | --- |
| Stage单请求与多镜头冲突 | 每次Preparation，Stage只聚合 | 两镜头不同实例、同镜头多候选、fallback与重试互不覆盖 |
| 素材字段误复用阶段自动审核 | 纯校验器+本次审核合同+独立受控字段批准策略 | 无项目手动确认；unattended自动字段与阶段批准分别有真实证据 |
| 人工字段创建死循环 | 缺字段可创建awaiting_input，创建body支持field_values | 不配置辅助AI也能填字段、确认、执行 |
| 逐token容量拦截 | 默认warn，严格要求显式配置 | 变更文本长度/素材数量不因无样本自动失败；require_evidence真阻断 |
| 基础Smoke仍依赖旧Binding | resource请求精确profile/manual，删除Binding及全部读取函数 | 无Binding表仍可基础验收；不伪造instance证据 |
| 多confirmed修订歧义 | ExecutionFieldSelection显式采用 | 不自动latest，串镜头拒绝，冻结后改选409 |
| Smoke/准备解除阻断无法恢复 | 共用continue+原worker持久扫描 | 预算补足/证据刷新/重启可恢复，不重复已成功辅助任务 |
| 无配方人工制作 | manual RunPlan+同一Stage/Attempt/Artifact链 | 无Recipe行完成生成、人工采用、装配和ReleaseBundle |
| 审核/容量放行与证据混淆 | warnings/blockers/policy与实际证据分层 | 告警放行不写假passed，配置读取失败不静默放行 |
| 旧版本迁移/重试复活 | 清理不兼容配置/任务/审计，删除旧执行及只读解析 | 已删对象404；没有旧协议分支、别名或历史格式兜底 |
| 为保护审计反复增加工作 | 同一清理授权覆盖审计依赖，沿原删除服务一次收敛 | 闭包内物理删除，闭包外共享资源不变，不创建历史存档/转换系统 |
| 新业务规则又被写死 | 原合同/配置中心为唯一来源，复用通用原语 | 改配置可改变声明式规则；更换资源名称/ID不会触发私有分支；缺合同明确失败 |

发布/Smoke循环、输入双来源、部署漂移、锁定/默认冲突、辅助递归等当前仍适用的约束继续回归。只服务于已移除旧接口/Schema/历史兼容行为的测试删除或改为当前合同用例，不为旧测试通过重新增加兼容代码。

新增测试模块建议：test_execution_instances、test_execution_inputs、test_execution_preparation、test_execution_field_review、test_execution_admission_policy、test_execution_instance_smoke、test_manual_production_runs、test_execution_instance_migration。扩展现有Recipe、asset、production、registry、assembly与delivery测试；前端覆盖无配方创建、字段选择与恢复交互。具体文件按现有test目录命名规范创建。

验收必须包含一条无Recipe且有真实模型/媒体结果的人工生产链及一条schema3 Recipe链。合成API测试可先证明接线和错误合同，但不能写成模型质量或容量认证通过。开发可先交付warn模式的可用主链，再配置严格容量，完整envelope不是首期上线前提。
