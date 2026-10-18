# API

M4-05动态素材阶段：原运行详情的候选目录、参考图审核及绑定漂移检查，按图片阶段已冻结的素材需求确定实体范围，不要求项目中没有本轮外观需求的实体提供图片。纯主图与主图＋变体共用原选择/登记流程；未配置自动选择时，即使候选全部生成成功，也停在`waiting_material_selection`，明确选定后才登记确切素材版本。进行中可预览部分候选，但生成完成及审核写回必须覆盖全部冻结的`unit_key + requirement_key`，包括尚未创建准备的变体；已有/上传履约仍为零调用，不补造人审。无新增HTTP端点、请求字段或Schema；实现/隔离验证及现用加载边界见唯一详细设计第18节。

正式追加Bible候选后，受影响单元须通过原`material-selection-context`/`select-artifact`改选或明确保留当前图，不能直接沿用追加前的决定自动结束阶段；仅晚到的原候选不触发改选。比较组件为已选图保留“查看素材选择影响”入口，是否可确认仍由服务端返回；同键原正文重放不新增决定，新键表示新的选择意图。明确保留原图时记录新的一次人工选择，复用原Asset/版本；此前自动决定保持自动来源，已生成变体保留原依据，仍不等于Bible审核采用。

输出版本可选字段：`parsers.field_source_options` 中 `optional=true` 表示可在版本中启用／移除的受控字段；目前支持英文文字 `camera_guidance`。`field_sources` 必须仍含三项基础字段，加入 `"camera_guidance":"camera_guidance"` 后，实际响应及人工 `candidate_overrides` 均须包含非空镜头指导。只存原审核决定／阶段快照，项目写回及项目变化检查仍限原三项，完整审核摘要覆盖全部所选字段。下游 `reviewed_visual_rules.camera_guidance` 只能引用实际含此字段的已审核轮次；旧三项结果不补值，不读当前目录或项目设置替代。移除需明确提交完整 `field_sources`，非空 `response_keys` 也必须同步覆盖所选字段。任意新字段／类型／协议尚未开放。

本文维护当前API说明，唯一机器字段契约仍为根OpenAPI。2026-09-21本轮仅收敛已过期的开发/切换状态说明，未修改接口或机器契约，也未逐接口重跑验收；当前状态与遗留工作见[统一工作包](optimization/records/execution-unification/2026-09-25.md#current-work-packages)。隔离回归、服务已切换与真实业务通过必须分别判断。

## 唯一契约

### 全安装历史清理

- 接口：`GET /api/v1/maintenance/history/`只读预览；`POST`同路径执行。登录且超级管理员；页面“系统设置 → 凭据与审计 → 业务历史清理”。实现`maintenance_views.py`与`services/history_cleanup.py`，不另设清理队列。
- 请求：GET查询参数及POST JSON接受`include_maintenance`，布尔值默认false；无分页。POST必填`plan_sha256`（预览返回的64位小写十六进制摘要）、`pause_token`及`confirmation="CLEAR_ALL_HISTORY_KEEP_ASSETS"`。页面要求输入“清空所选历史并保留素材”。范围是当前安装全部用户的业务历史；只有显式true才包括系统维护历史，不接受隐式项目过滤或“只删部分也成功”。
- 响应：预览返回所选`include_maintenance`、`delete_counts`、`preserve_counts`（模型名到数量）、`blockers[{model,id,reason,label,detail_url,link_label,status,started_at,heartbeat_at,progress_marker}]`、`can_execute`、`plan_sha256`、`confirmation`及`files_deleted=false`。进度/时间字段取原记录，无对应字段时null，不估算完成率。`detail_url`为已支持正式入口的站内路径，否则null，不猜造地址；任务链接定位确切准备/调用/装配，主机探测通过`/settings?section=runtime&probe_attempt=<id>`读取原探测。成功执行返回`deleted_counts`、`retained_assets`、`files_deleted=false`。空历史可预览；非空阻断清单不代表已删除。
- 失败：400请求/确认错误，401未认证，403权限不足；409预览变化、活跃执行、未解决远端结果、保留配置引用或新表职责尚未登记。锁内重新计算和事务内外键核验失败时整批回滚。响应未知先读取当前事实，不能重发旧意图冒称第一次失败。
- 副作用：默认删除项目/剧本/Bible/镜头/运行/准备/生成调用/审核/装配/发行/Smoke/操作审计及非系统配置修订，保留用户、凭据、目录配置和素材。解除素材版本的旧生成/设定FK，保留`source=generated`并设`origin_history_cleared=true`；保留库内主图/变体关系。Asset原历史标识移入只读`metadata.cleared_history_origin`，不可作为有效运行引用或伪造上传来源；客户端不得改写该标记。原文件不删除，不触发模型。业务历史包含旧平台审计，不额外保存第二套请求正文或虚构审计；操作人经鉴权，结果由原请求返回。
- 可选系统维护范围：`RuntimeHostProbeAttempt`主机探测任务/进度/结果，`HostCapabilitySnapshot`硬件/工具能力快照，`RuntimeModelAvailability`模型文件存在/大小/摘要等观测，以及系统级`RecommendationSnapshot/Candidate/Decision`推荐、候选及采纳/覆盖/拒绝记录。项目或非系统配置修订关联的推荐与决定仍随业务历史删除。默认保留上述系统维护事实，探测运行与进度更新不阻挡、不改变业务清理摘要；仍检查保留记录是否实际引用待删除业务对象。
- 运行证明：业务清理重置工作流`validation_state`/`smoke_test_state`及相应证据；仅包含系统维护历史时额外清除模型文件的主机核验引用/时间/人员并还原为declared，不删除模型文件或部署配置。可执行图、输入合同和不可变导入配置快照不改写。导入快照中的历史库存注记不是当前输入，不能当作活跃引用拦截清理，也不能当作新执行的证明。装配终态保留的旧attempt token不是活跃领取；所选范围内未完成领取、执行或分发仍阻止清理。
- 并发/恢复：SQLite先取得数据库写锁，PostgreSQL使用原引用锁及表锁；备份由正式运维入口完成，页面不提供撤销。隔离SQLite验证清理、回滚、权限和素材复用；PostgreSQL实机及跨机器安装结果未在此冒称通过，当前验证证据见唯一详细设计18节。
- 预览摘要覆盖记录成员、业务内容和保留配置。普通更新时间/心跳，以及执行准备的下次检查时间、临时领取标识和期限，不作为范围变化；领取是否仍有效由提交时锁内的当前状态另行检查。新增记录、内容/状态/修订或配置引用变化仍使原预览失效；不会因忽略调度时间而跳过在途执行保护。
- 清理提交必填`pause_token`（UUID）。超级管理员先`POST /api/v1/maintenance/history/background-pause/`，正文`{token: UUID, include_maintenance: false}`，200返回同一token、范围及`expires_at`。原`WorkerLease`保存5分钟暂停事实并绑定范围；新准备自动领取在同一锁下检查，只有include_maintenance=true才暂停新增定时主机探测。已领取工作/既有探测继续结束；业务范围无需等待探测。原GET仍纯只读，不自动暂停；页面阻挡时每10秒GET，不自动提交清理。
- 同一路径`GET background-pause/`只读返回`active`、`expires_at`（不活跃为null）、`server_time`、`include_maintenance`，不返回持有者/token、不创建或续期。冲突窗口每10秒只读核对占用并显示剩余有效期；解除后仍须显式重新预览，不自动删除。倒计时不是任务完成时间。弹窗区分准备/核对/等待/提交/结果，显示操作已等待时间、最近核对及下次核对；正在刷新时保留上次阻挡项并标注时间。各请求30秒超时，未知删除结果不盲重试；未知恢复仍可关闭且明确告知恢复未确认、依靠到期失效。
- `DELETE`同一`background-pause/`路径，正文`{token: UUID}`，204释放自己的暂停，重复释放幂等。相同活动token重放POST不延长；其他窗口占用、提交token过期/不属于当前账号返回409 `history_cleanup_pause_conflict`，无删除。关闭页面尝试释放，异常退出依靠到期恢复；清理成功同事务释放。401/403/400沿原鉴权/UUID校验；不取消任务、不新增队列或Schema，暂停不会绕过原在途与范围复核。
- 任务详情`actions`明确包含`cancel_asset`、`cancel_smoke`，均布尔值；前端只在当前可操作详情顶部显示，历史只读和指定旧调用无取消入口。素材取消复用原`asset-generation-jobs/{id}/cancel/`接口，服务端重新检查所有权和当前状态，不用GET触发取消。

### 素材批量删除

- 接口：`POST /api/v1/assets/batch-delete/`，与单条DELETE共用`AssetViewSet._delete_assets`；素材库勾选后“批量删除”。按已有登录态及素材可见/所有权授权；全局素材仅超级管理员可删。
- 请求：JSON `asset_ids`为1–200个不重复UUID，`confirmation="DELETE_SELECTED_ASSETS"`必填。示例结构为`{"asset_ids":["00000000-0000-4000-8000-000000000001"],"confirmation":"DELETE_SELECTED_ASSETS"}`，该ID仅说明格式。
- 响应：200返回`deleted_ids`与`deleted_count`。400空/重复/超量/确认错误或存在引用，401/403鉴权/权限失败，404包含不存在或不可见素材，409正式Bible素材保护。没有部分成功响应，所有成员先核对后事务删除；响应未知先刷新ID列表。
- 副作用：删除所选Asset记录及提交后不再共用的受管上传文件，保留其他素材/文件；本地库原路径/外部文件不删除。不删除项目或触发执行，不写伪造来源/调用。素材版本仍被引用时必须先通过原生命周期入口处理关系。
- 验证：隔离API覆盖真实共享文件、引用回滚、权限、重复ID及明确确认；页面仅预览/取消核验不等于已删除现用素材。详见本轮检查点。

仓库根 [openapi.yaml](../openapi.yaml) 是唯一机器可读契约。运行时 `GET /api/schema/` 从同一契约动态返回 schema；文档、客户端和校验工具不得引用或维护其他副本。

路由前缀为 `/api/v1/`。本文只解释调用边界；字段、状态码、枚举和必填项以根 OpenAPI 为准。

生产能力`GET /api/v1/capabilities/production/`不再返回或要求`script_breakdown_auto_split_trigger_chars/target_chars`：结构化声明已经给出集/镜边界，不使用字符阈值拆分。原默认时长与连续性仍保留，权限、项目覆盖和错误包络不变。0227只退役这两项及道具简称配置；迁移文件与隔离验证不代表现用配置已切换。

### Bible 变体设定与修订

操作者标识 `created_by_id`（摘要、设定及固定主图修订）和 `decided_by_id`（审核）遵循根 OpenAPI 的整数合同；前端按正整数读取，不将其当 UUID 或接受字符串别名。实体、设定、修订和审核记录本身的 ID 仍为 UUID。

M3-08局部清理（2026-10-01，第154批）：设定服务仅接受`payload_schema_version=2`完整摘要；详情/来源/另存修订/审核、作为新变体主图依赖以及原人工/策略流程核对遇到旧schema1时明确409或阻断，提示版本停用，不自动转换正文摘要或补造归属。有效schema2的历史修订不受影响，列表/历史摘要可保留旧记录的ID及版本用于识别，但不代表正文或执行可用。[0218迁移文件](../backend/studio/migrations/0218_bible_setting_schema_default.py)仅把新对象默认版本改为2，旧记录和审核不改写，实际清理另核对目标；0213–0218均未执行现用迁移。本轮没有新增端点、权限或请求形状，根OpenAPI同步移除元数据说明里已下线的候选apply写入入口。

生成规则与保存校验共用M1确切字段结构和原剧本语义合同：实体/素材引用名不限语言，创作正文是否必填及语言要求来自配置，不要求固定前置块或隐式英文翻译。引用名冲突按同一NFKC＋casefold规则比较，原拼写保留，不按名称匹配实体、不覆盖已保存设定；已有多语言/布局、人工/策略、来源变化和冲突回归继续复用。M5已移除旧规范稿分镜读者，人工/配方统一消费确切保存结构，不设置M3兼容入口。

M3-07素材需求输出（2026-10-01，第153批）：原Bible审核事务将`material_requirements`保存到同一阶段输出，原`GET /api/v1/production-runs/{id}/`的`stages[].output_snapshot`可读取。无新HTTP端点、顶层字段或数据库Schema；下表说明原JSON快照的新增内容，鉴权/项目归属沿原运行详情及审核动作，不新增CLI或旁路入口。

| 原Bible决定路径 | `bible_review.output_snapshot`中的需求位置 |
| --- | --- |
| 无配方人工审核 | `materialization.material_requirements` |
| 配方人工审核 | `review_payload.material_requirements` |
| 配方策略决定 | `policy_evidence.material_requirements` |

该对象包含`schema_version=1`、`kind=bible_material_requirements`、`project_id`、`source`与`targets`。source保存原已采用声明引用；原纯人工项目Bible配方审核为`{kind: project_bible}`。每个target含唯一requirement_key、实体scope、appearance_role、确切setting修订/摘要、reference_name、primary_dependency及declared_reference_asset（未声明为null）。主图依赖是设定引用而非图片；参考Asset/SHA不是已采用生成结果。按本次声明的外观身份选择本次核对修订，不带入其他来源的实体/变体，也不还原人工编辑前的正文。无外观输出空需求、无变体不造候选，旧快照不补造此字段。内部读者校验原决定/来源/保存输出及明确RunPlan范围；缺失或变化显式失败，不静默重算返回。写入失败沿原审核事务回滚，没有模型调用或新任务。需求输出不要求未来镜头先存在，实际剧集范围消费仍核对镜头关联。M4动态生成和M3模块页面交付尚未完成，不能把这份输出当作图片已就绪或现用环境已验收。

配方需求输入（M4-05部分）：原`bible_image_generation.input_snapshot.material_requirements`保存原已审核需求及`run_plan_sha256`、`bible_review`引用，沿同一运行详情可读，无新端点或数据库Schema。自动阶段仅取本轮声明与RunPlan范围交集，不枚举项目全部实体造主图；无外观不造任务，空集合显式失败，数量边界按外观需求数乘候选数计算。`units`逐外观投影原需求，不再把同一实体的主图/变体折叠为一个实体单元，字段如下。缺前置审核、内容变化或旧快照不匹配显式失败，不回填。原调度器按外观角色推进：先核对全部变体路线的主图/差异来源，主图未选择登记时不准备或派发依赖变体；登记完成后接原队列。任一需求未完成不报告阶段成功。声明参考Asset不当作已履约结果，混合生成及明确选材履约的开发证据见下文；上游自动履约、后续审核消费及M4模块整体验收未完成。

| `input_snapshot.units[]`字段（Bible图片阶段） | 含义与读取边界 |
| --- | --- |
| `unit_key` | `appearance:<appearance_id>`；确切外观身份，不是名称、实体ID或候选序号 |
| `requirement_key` | 原`setting:<setting_revision_id>`需求；改修订不改外观身份，但不能替换本轮冻结需求 |
| `scope`、`appearance_role` | 原实体`{kind,id}`归属及`primary/variant`；scope负责内容/权限，不代替执行单元身份 |
| `primary_dependency` | 主图为null；变体为确切主图的`{unit_key,requirement_key}`。保留原依赖修订，即使本轮主图为另一修订或无对应目标，也不自动改绑或添加目标，更不证明已有可用图片 |

这些单元仅是原审核回执在原阶段快照内的引用投影，不是第二需求表/队列。普通镜头阶段的`units`仍为镜头scope，不受此变更影响。旧实体单元快照不兼容读取或自动转换；不适配现用开发记录的具体清理另核范围。原详情鉴权与项目权限不变。

M3-06设定页面（2026-10-01，第151批，开发及隔离页面验收完成）：原详情分为共同身份、主图外观和变体差异；变体前两区只读其固定主图修订，编辑只保存差异。保存新修订前显示变更影响：不改其他变体、原审核和冻结请求，后续消费者仍需核对基线。列表/详情/历史“尚未人工核对”只表示没有BibleVariantReview，不代表ProductionRun流程未通过；人工确认/拒绝明确标记，配方策略决定在原运行审批审计查看，不在本页推算。来源对照、修订和审核仍用下表原接口，没有新HTTP字段或权限变化。实体参考图历史的variant_index显示为“生成候选序号”，不解释成服装变体数量。正式入口仍是项目工作室→一致性 Bible→管理主图与变体；现用切换/联调未完成，不能以隔离样例替代现用验收。

M3-02实体登记接线（2026-10-01，第147批，未切换现用）：`/characters/`、`/scenes/`、`/props/`保留原实体基础信息、结构化创作字段和参考素材关系，不再提供`visual_identity`读写或搜索。列表/详情及POST/PUT/PATCH响应不含此字段；写入请求提交该旧字段（即使空字符串/null）返回400，错误详情指向主图/变体设定。非JSON对象仍走原400类型校验，归属和锁定规则不变。实体可以先无图片、无共同身份/外观登记，不会自动生成设定或调用模型；共同身份和外观必须通过下表的唯一设定修订接口维护，普通实体编辑不会改其正文或修订。

页面：项目工作室→一致性Bible→新增角色/场景/道具，只填写名称，可选基础描述和参考图；保存后在卡片“管理主图与变体”登记身份/外观。素材选择只预填名称/说明，不把素材提示词复制成设定。原声明物化也不再复制身份到实体旧列；模型与迁移文件[0217](../backend/studio/migrations/0217_remove_bible_visual_identity.py)已移除三类旧列，同时删除临时defer/exclude。0217仅丢弃旧列，不转换历史文字为设定；逆迁移只重建空列，原值须从备份恢复。其他实体字段、设定、审核、来源及素材关联保持。0213–0217未迁现用库，实际切换仍按R06核对范围，不把开发验收当成现用服务已加载。

固定母稿候选查询已下线：`GET /projects/{id}/script-bible-candidates/`及其旧apply均无路由，不保留重定向或兼容包装；所有账号访问返回404，不解析母稿、查询或写入Bible。业务候选/来源/设定继续从已采用的结构化修订进入原手动/配方完整Bible审核，不按同名或旧实体文字猜配。旧`metadata.script_bible_v1`不再有写入入口；已有保留记录仍不得由CRUD注入、修改或删除，当前错误提示不再指向已不存在的apply。

三类Django管理表单也排除旧锚点，直接提交旧键会给出表单错误，搜索只使用名称/slug/说明；项目现用URLconf未挂载Django admin，本轮仅在测试URLconf验证其表单，不新增运行入口。既有`seed_data --with-demo-project`仅为本次新建的示例实体通过正式主图writer登记显式样例身份，重复运行保留人工修订，已有实体无设定时不从旧列补造。普通初始化不创建示例/设定；没有本轮现用命令或写入。隔离路由、机器合同、三类表单、来源保护和初始化验证见[详细设计7.3](optimization/modules/m3-bible/design.md#m3)。

M3-01项目级身份接线（2026-10-01，未切换现用）：原物化器通过`BibleEntityIdentity`按项目＋稳定声明key复用原Bible对象，不新增HTTP接口或改变请求/响应形状。同名不同key各自创建，跨项目同key不复用；新源修订保留已有实体名称和设定正文，缺失造型key才新建。每份来源保存实际引用的确切设定ID/SHA，重放不改取后续修订。原人物/场景/道具PATCH仍检查身份关系的跨项目移动，正常修改沿原编辑规则。迁移文件0215/0216未执行于现用库，不回填/转换历史来源；缺身份或确切引用显式冲突。M3-01隔离验收不等于整个M3或现用生产链已交付。

状态：第151批开发中。接口和页面代码已接入，代码及隔离测试不能作为现用服务已可用的证明，切换情况以唯一检查点为准。入口为项目工作室→一致性 Bible→人物/场景/道具卡片→“管理主图与变体”；可区分主图/变体角色、原剧本声明来源及确切主图设定修订。声明入库已接入原手动/配方决策事务并完成自动与人工隔离验证；明确关系分镜writer及主图派生生产仍未完成。内部只读分镜来源预检不新增HTTP接口，不代表现用生产链已经切换。

| Method / Path | 请求与响应 | 数据口径与副作用 |
| --- | --- | --- |
| `GET /bible-variants/` | 必填query project_id/entity_kind/entity_id；kind为character/scene/prop；可选page/page_size，最多50；200标准分页摘要 | 按项目所有者授权，列表不带锚点正文。未登记返回空数组；失败不伪装空态 |
| `POST /bible-variants/` | 归属字段+完整设定+显式角色/确切主图依赖；201摘要与setting | 创建稳定主图或变体ID和修订1，未审核；没有模型调用或图片采用 |
| `GET /bible-variants/{id}/` | 可选正整数revision，默认当前；200摘要+所选setting/review及primary_setting | 历史详情保留所选正文，外层revision/current_decision仍表示当前修订；依赖按保存的主图修订读取，不用当前主图替代 |
| `GET /bible-variants/{id}/revisions/` | page/page_size同样最多50；200分页修订摘要，revision降序 | 正文按单份详情读取，不在历史列表复制 |
| `GET /bible-variants/{id}/source/` | 必填正整数query revision；200所选setting ID/SHA、初始source/document_sha256及fields对照 | 按需只读初始结构化详情；不从当前剧本补值，不改变设定、审核、来源或图片 |
| `POST /bible-variants/{id}/revise/` | 完整设定+expected_revision；200最新详情 | 版本冲突409；无变化400；追加不可变修订，原审核不继承 |
| `POST /bible-variants/{id}/review/` | expected_revision、expected_sha256、decision=approve/reject、comment（可空，最多1000字符）；200详情 | 明确记录人审；同用户/修订/决定/说明重放幂等，冲突409，不覆盖既有决定 |

来源对照（M3-03，2026-10-01）：在设定详情点击“查看初始声明与本修订对照”，以正在查看的确切revision调用source接口；默认清单/详情不额外读取剧本结构化正文。fields逐项提供key、label、declaration_value、setting_value、origin、source_path和changed_from_declaration。主图包含名称、引用名、共同身份和外观；变体不重复共同身份，依旧查看其固定主图修订。origin区分已保存输出字段source_value、配置常量configured_literal、配置默认configured_default、初始未提供missing、人工登记且无初始声明manual。明确空字符串不等于未提供null；有声明时changed_from_declaration只比较初始值与所选修订，无声明时为null，不伪造对照。保存输出不是原稿逐句事实，更不保证内容为非模型推断；配置补值和后续人工修改分别标明。初始binding和document摘要始终来自该造型首次创建的精确ScriptRevisionStructure，不随新源或当前配置变化；原每源使用回执仍保留本次实际采用的设定引用。读取校验原设定、来源声明及结构化详情；来源缺失/摘要漂移409，不降级旧缓存或新剧本。revision缺失/非法或未知query返回400，不可见对象404、未登录401；该接口没有写入、模型调用、设定审核或图片采用。前端切换修订会清除旧对照，失败保留明确错误并允许重试，迟到结果不覆盖新修订。

完整设定字段为 `name/reference_name/identity_anchor/appearance_anchor/basis_asset_id/basis_asset_sha256`，均显式提供。名称最多160字符、规范化后最多191，不允许控制符及伪造引用标记；reference_name在项目内NFKC/casefold唯一、语言不限。两份锚点各最多16KiB UTF-8，主图身份与各造型外观可先留空、补充后再清空；遗漏字段、null及错误类型仍拒绝，不补默认值。create另须显式提交`appearance_role/primary_setting_revision_id/primary_setting_sha256`：primary使用null及空摘要，每实体至多一份主图；variant必须引用同项目、同实体、角色为primary的确切修订ID及所见64位小写SHA256，允许明确选择历史修订，不要求已有图片或已审核。变体identity_anchor必须为空，共同身份只从该主图修订读取，不能复制到差异设定。revise不接收角色、来源绑定或主图依赖，保留确切旧引用，不跟随主图新修订自动变化。摘要不符或重复主图409，错误依赖/变体重复身份400，未知字段拒绝。basis_asset_id可为null，此时SHA为空；否则须64位小写SHA256，保存及批准时沿正式媒体读取器验证真实文件、MIME、权限和可用性。已有普通basis Asset只是设定来源，**不是已采用主图证明，也不授予变体生成资格**。页面沿“新增主图设定”→主图详情或历史修订→“基于此修订新增变体”；已有图来源只读保留。保存不表示图片就绪，主图图片采用/派生链仍待M4接通；M3-02已完成共同身份/外观归属收敛，现用切换仍未执行。

详情、声明使用回执和原审核入口共用确切设定正文查询，读取所选修订及其固定主图依赖，不通过旧实体身份文本或原剧本正文补齐。详情中依赖正文损坏仍返回409，不替换为当前主图；审核读取同时核对来源、依赖及审核引用，不只比较正文SHA。提交审核时所需修订缺失必须失败并回滚，不能跳过后将剩余项视为完整集合。设定写者及原身份消费者已在M3-02统一替换，实体旧列按0217移除；本段不代表现用环境已完成迁移。

新建主图设定示例（UUID需替换为实际可见项目和实体）：`{"project_id":"11111111-1111-4111-8111-111111111111","entity_kind":"character","entity_id":"22222222-2222-4222-8222-222222222222","appearance_role":"primary","primary_setting_revision_id":null,"primary_setting_sha256":"","name":"日常主图","reference_name":"角色甲·主图","identity_anchor":"明确登记共同身份","appearance_anchor":"明确登记本造型状态","basis_asset_id":null,"basis_asset_sha256":""}`。新增变体改用variant角色、所选真实主图修订ID/SHA，identity_anchor为空，名称及外观填写本变体差异。响应setting含修订ID/序号、原文、content_sha256、保存人/时间及可空review；review含决定、说明、审核人/时间，不返回凭据或供应商连接信息。

只读响应字段：列表/详情增加`appearance_role=primary|variant`与`source`；source为null表示没有声明来源，否则包含binding_id、script_revision_id、entity_key、variant_key（主图为null）及declaration_sha256。source始终表示造型初次创建来源，后续剧本复用不将它替换成本次来源；每份剧本的确切使用另由内部回执保存。后续剧本声明可复用同一项目实体上已由人工创建的唯一主图，其source仍为null、来源声明摘要仍为空，不改名或覆盖正文；新变体引用此次选定的主图修订，旧源重放不跟随更新。没有稳定声明key的人工变体不按名称匹配，引用名冲突显式失败并回滚整批。修订摘要含payload_schema_version、source_declaration_sha256及primary_setting_revision_id；人工另存修订不冒充原始声明正文。详情的primary_setting为所选修订的确切主图设定（含variant_id、修订ID/序号/SHA、身份/外观及原review），没有依赖时为null，不表示图片已采用。空字符串是实际保存内容，不依赖原始修订推导创作必填门禁；读取仅按需展示，不自动审核、复制主图锚点或改绑。只有create接收上述三项明确角色/依赖输入，完整source、primary_setting及来源摘要不可写；revise/review不接收角色或主图依赖。

鉴权沿平台登录态；普通用户仅本项目所有者，管理员可管理授权范围。跨项目实体、不存在或无权限404，未登录401，输入/文件/容量错误400，摘要/唯一名称/并发版本/既有审核冲突409，删除及覆盖方法405。写入方为 `services/bible_variants.py`，持久化为BibleVariant、BibleVariantRevision、BibleVariantReview；修订和审核追加不可变，不覆盖原剧本、Bible、图片绑定或ProductionRun。只有设定登记与审核，不自动继续生产、分配素材或触发费用。

M3-04引用名诊断：创建、声明物化和另存修订共用项目范围的NFKC/casefold唯一校验；409继续使用原error信封，在说明中列出占用名称的实体类型/显示名/ID、主图或变体名称/ID及现有引用名，不查询或泄露其他项目同名对象。显示名本身允许重复，改名仍保留设定ID及实体关联，不全局替换剧本、对白、其他实体或旧修订；没有新接口、自动合并或名称翻译调用。M3-04开发及隔离验收已完成，现用切换与M3整体验收未完成。

M3-04生产主体投影（第149批，未切换现用）：策略选用`request.subject`时，主体按类型/实体ID读取自己的名称标识及主图正文，不按同名词取第一个对象。明确配置的术语原样保留；项目内同名标签或Unicode等价标签附`[类型:实体UUID]`消歧，无配置时仍使用本对象已有speaker/slug标识，不生成英文名。可选翻译的隐式词表不为同名实体猜唯一身份，明确术语不因此删除；上下文变化使旧翻译失效而不改旧记录。允许原语言的字段直接编译，显式英文语言策略仍报对应字段错误；没有自动翻译、全局替换对白、合并实体、后台写入或真实模型调用。初始名称沿M2配置映射入库，人工改名沿原实体PATCH/设定revise，新来源不覆盖人工名称。本项开发及隔离验收完成，不含M6槽位绑定或真实模型语言支持验收。

既有`PATCH /characters/{id}/`、`/scenes/{id}/`、`/props/{id}/`仍通过原项目锁和所有权校验；已有设定或ScriptBibleEntityBinding的对象跨项目移动返回400，不重绑历史来源。Bible展示名可相同，身份仍为稳定ID/声明key，引用名继续受项目唯一性约束；同名不会自动合并。0198只添加来源关系，不给历史设定推断主图/声明身份，不重算旧审核与正文摘要。新修订采用包含归属/来源/主图依赖的v2摘要，历史v1按原摘要读取。

验证：隔离API测试覆盖权限、修订、重放、摘要、文件与列表；前端类型/构建、请求约束测试及隔离浏览器夹具已核对。正式服务迁移后页面/API集成与主图派生链仍未验收；实施边界见[唯一计划](optimization/plans/execution-unification/reference-variants.md#reference-variant-plan)。

### 项目一致性快照与同步锁

沿原项目接口核对所有者/管理员权限；不新增业务入口，不触发模型、图片采用或设定审核。

| Method / Path | 请求与响应 | 数据口径与副作用 |
| --- | --- | --- |
| `GET /projects/{id}/continuity/` | 无请求体；200 `ConsistencySnapshot`，含项目/风格/三类实体/已存活动锁 | 实体的`primary_setting`来自当次明确当前主图的确切修订，包含appearance_id、setting_revision_id、revision、content_sha256及两段锚点正文；无主图为null，已保存空文字仍为空字符串。没有旧visual_identity正文或从锁中补取正文。只读，不同步锁、不修复历史快照。 |
| `POST /projects/{id}/sync-locks/` | 无必填请求体；200 `ConsistencyLock[]`，不是项目创建或项目对象 | 在原项目锁和一个事务内同步派生锁；实体identity锁的primary_setting_ref只存上述四项内容引用或null，不复制锚点正文。重复相同值不升版，确切主图改变后仅受影响锁升版；中途失败包含失效锁处理在内全部回滚。 |

主图当前指针/正文损坏或归属不符返回409 `bible_variant_conflict`，沿统一error信封，不退回旧实体文字。不可见项目404，未登录沿原鉴权401/403。`locks`按原样返回已存值与version，可能落后于当前设定；GET不会悄悄同步或替换旧锁，只有原显式同步操作更新派生值。旧锁不作为主图正文来源，也不增加旧数据转换或兼容层。

素材关联及稳定Picture/Subject槽位保持原规则。角色`metadata.visual_reference_policy`的显式include/exclude优先；auto按原高置信度词汇检查实体说明及主图确切修订的identity_anchor，不再读取旧visual_identity，也不把造型差异当作永久身份。无主图或明确空身份不会补旧文字；损坏设定明确报错。快照中的决策仍为schema_version/eligible/mode/reason/source，auto未命中时reason为visual_reference_default，source保持semantic_fallback。这些是全局设定资格和一致性读者的规则，不再作为Shot内容保存时自动选材的依据。Shot素材选择通过生产运行的逐次引用入口处理，见下文内容编辑合同。

快照不是创作审核/采用凭证，不自动把主图正文注入视频提示词。项目提示词和参考上下文的人物来源读取`ShotCharacterRelation`：visible只决定画面主体，speaker按原对白行position确定发言者，原speaker_id仅校验身份；不使用全局视觉资格改变voiceover。缺失/错位/跨项目关系报错，不按同名、旧人物池或当前全局资格补齐。原text/delivery与已声明毫秒区间保留，关闭本次音频不会写空原对白；已冻结上下文仍只读原事实。该人物读取合同不改变冻结请求；旧素材绑定及固定创作列分别由0230、0232退役，现用迁移与模块验收状态见唯一清单18.3。

### 视觉风格管理

- 页面入口：风格素材库→管理视觉风格；新建连续剧→管理视觉风格复用同一弹框，不离开创建表单。
- `GET/POST /api/v1/styles/`，`GET/PATCH/PUT/DELETE /api/v1/styles/{id}/` 读写 `Style`。列表支持 `search/category/is_active/is_system/scope` 与标准分页；`scope=personal` 表示当前用户自己的风格，`global` 表示共享风格。`GET /api/v1/styles/catalog/` 返回200 `{scopes:[{value,display_name}],can_manage_categories}`；共享范围和管理权限由服务端提供。
- “管理视觉风格→管理题材分类”通过 `GET/POST /style-categories/`、`GET/PATCH/PUT/DELETE /style-categories/{id}/` 管理唯一的 `StyleCategory` 目录。登录可读、管理员可写；支持 `search/is_active/ordering` 和标准分页，默认按 `sort_order/name/id` 排序。字段为 `name`（必填、唯一、120字内）、`description`（可空、500字内，仅辅助选择）、`sort_order`（非负整数，越小越靠前）、`is_active`；`key/id` 由服务端生成且只读，返回直接引用的 `style_count/can_manage`。示例：`{"name":"校园青春","description":"校园日常与成长题材","sort_order":20,"is_active":true}`。未知分类400、重复名称400、非法字段400、无权限403、不存在404；删除成功204、有风格引用400 `style_category_in_use`，数据库PROTECT保护引用。
- `Style.category` 是目录稳定key外键，不再是代码枚举；可空，未提供表示未分类，不默认“其他”。`category_label/category_active` 提供显示名称与状态。分类只归类并供策略引用的项目风格上下文参考，不参与模型/工作流/配方路由；项目风格上下文同时携带key与名称，目录说明不进入该上下文。分类改名保留关联，停用禁止新选但允许保留原有分类；目录改动不改写冻结请求。0175迁移把已有代码原样关联到目录初始11项，遇到未知原始代码显式停止，不猜分类或删除风格。
- 创建示例：`{"name":"柔和写实","category":"drama","scope":"personal","description":"柔和窗光与低饱和色彩","visual_prompt":"soft window light, muted colors","negative_prompt":"","is_active":true}`。`description` 是选择说明，`visual_prompt/negative_prompt` 为策略按需引用的模型内容；空字符串表示未配置，不自动拼接默认提示词。每项文本写入不超过64 KiB。`slug/id/owner/is_system` 由服务端管理；更名不更换标识，共享范围创建后不可更改。
- 登录用户可读取自己的和共享风格，只能修改自己的；管理员可管理所有风格、创建共享风格。响应200/201包含 `scope/can_manage/category_label/asset_count/project_count/generation_job_count`。这些计数为全部直接引用；`GET /styles/{id}/references/?kind=projects|assets|jobs` 分页返回 `{id,name,kind}`，仅显示调用者有权查看的记录，不泄露其他用户的项目或素材名称。
- 停用通过 `PATCH {"is_active":false}`；已有项目、素材、任务的引用保持不变，不能新建关联到停用风格。删除成功204；有直接引用返回400 `style_in_use`，底层外键同时使用PROTECT，竞态/其他受保护引用沿409 `protected_resource`。未登录401、无写权限403、不可见或不存在404、字段不合法400；前端展示实际错误，不假装成功。
- 写入沿用平台生命周期审计，记录操作人、风格ID、前后配置摘要哈希和启停状态，不把完整提示词写入审计。保存不创建模型请求；影响后续读取风格的准备步骤，不修改已经冻结的请求。系统初始化只补缺失预置，不覆盖人工配置和启停状态。0174迁移仅替换精确匹配旧模板的11项预置简介，不修改模型提示词。
- 验证覆盖：风格CRUD、权限与共享范围、计数与关联隔离、停用、删除保护、初始化幂等和定向简介迁移；页面验证记录见优化台账。

### 镜头时间与剧本模板

分镜人物编辑（M5-08代码合同，模块现用切换仍单列）：

- 入口为分镜工作台的“本镜头出镜人物”和音频页的“对白与发言者”；沿原 `GET/POST /api/v1/shots/`、`GET/PATCH /api/v1/shots/{id}/`，原登录、项目所有者/管理员权限及生命周期审计不变。GET无写入，列表沿原分页；创建201、更新/读取200、无权对象404、未登录401/403、字段错误400并保留原数据。
- 请求/响应使用 `character_relations` 数组，每项恰为 `{role,position,character_id}`：role为visible或speaker，position为从0开始的整数，character_id为本项目确切人物UUID。visible的位置为出镜顺序且人物不可重复；speaker的位置逐一对应dialogue行，每行恰有一个，允许同一人物多次发言，也允许只有发言没有出镜。`dialogue[i].speaker_id`必须与该行指定实体的stable_speaker_id一致，不用名字或旧池选择人物。
- 最小例：`{"dialogue":[{"speaker_id":"S2","text":"  原文。  ","voiceover":true,"start_ms":123,"end_ms":987}],"character_relations":[{"role":"speaker","position":0,"character_id":"22222222-2222-4222-8222-222222222222"}]}`（UUID仅示意；必须实际存在）。出镜可为空；不把画外音或素材引用转成出镜关系。
- POST省略关系只允许无对白；PATCH省略关系保留当前关系，显式数组替换该镜全部人物关系。变更对白行数/发言身份时须同步提交完整关系，否则400。相同关系保存不更换行ID；正文和关系在锁内重验并原子保存，失败全部回滚。旧characters写入明确400，响应不再含该字段；旧表未在此段删除或回填。
- 对白逐行保存原JSON文本、可选delivery/voiceover及整数毫秒时间；不再通过“角色名：文本”格式化/解析往返，空格、换行及显式false保留。可选时间成对填写且在镜头时长内，空对不补造；关闭音频或改规划媒体类型不清空原对白。UI按确切人物选项及发言标识区分同名，移除行同时移动后续speaker位置。字段失败沿保存错误显示，不调用模型。
- 本段只修改Shot、ShotCharacterRelation及原既有编辑字段；不写入来源结构、素材用途选择、审核决定、执行准备或历史请求。隔离API、组件/浏览器和未验边界见[详细设计第18节](optimization/modules/common/design.md#tracking)，不冒充现用切换或用户验收。
- Shot列表/详情及创建/更新回执不再暴露`reference_bindings`、`reference_assets`；POST/PATCH包含任一旧字段（包括空对象/空数组）均返回400，并在原`error.details`中给出该字段的停用与逐次引用提示，不能部分保存其他字段。普通内容保存不校验或重写旧素材池、旧绑定或`ShotReferenceUse`选择；关系、对白及时间规则保持，权限边界不变。模型已删除旧characters/reference_assets/reference_bindings，0230迁移只移除对应旧列/关系表，不读取旧值来补新引用或转换历史请求；现用库尚未应用0230，须随M5模块切换先备份。
- 分镜工作台“一致性引用”页仅编辑明确出镜/场景/道具关系及创作连续意图；选材沿“进入生产工作台选材”→原“选择素材 / 上传（单镜 / 多镜）”。逐次来源、版本和人工/自动策略沿原运行合同，模型槽位/必要用途按每次调用映射。删除旧模式裁剪、自动选图、直接分配首尾帧及旧Picture顺序预览；保存失败明确保留编辑状态并提示未入库。

- 入口：分镜工作台→镜头时间；原 `POST /api/v1/shots/` 和 `PATCH /api/v1/shots/{id}/`，沿原登录及项目所有者/管理员权限、项目写入锁与审计，无新生成入口。成功201/200返回实际Shot，非法值400、不可访问对象沿原404；失败不更新镜头、不创建模型调用。
- `episode_start_ms` / `episode_end_ms` 为可空非负整数毫秒，表示镜头在本集中的明确起止位置。空对表示只设置 `duration_ms`，按镜头顺序排列，不猜原稿时间。提供区间但不提供时长时由后端计算差值；同时提供时长必须相等。改单端点可重新计算时长；保持区间而只改为不一致时长会返回字段错误，需修改区间或显式清空两端。数据库同样约束成对、有序且差值等于时长。存储为Shot窄字段，Schema由0163 migration维护。
- 例如本集45–50秒，对应镜头时长5000ms；逐镜生成、动作及对白使用本镜头的0–5秒。它不是源视频截取时间，也不直接转换为ComfyUI端口。帧数仍由执行实例/工作流冻结合同明确配置，不能从这些新增字段猜测。顺序装配检查已声明的时间是否与前序镜头匹配，缺口/重叠返回阻断说明，不自动补黑场、移位或切片。
- 导入文件或粘贴文本的原接口仍保存原稿及确认修订，不静默改文。新AI生成与结构化候选按确切输出版本解析并保存保真原文/结构化详情；旧规范稿检查、固定母稿渲染及拆镜writer已删除。Bible来源/审核/物化及人工/配方分镜实际写入统一消费原保存语义映射，不重新解析原稿。未保存映射的文本不能直接进入这些结构化消费者，缺失时明确提示；人工直接编辑并采用已有分镜不受原稿模板限制。素材绑定、独立字段编辑/策略及模块交付仍按M5/M6后续项验收。
- 明确镜头头部支持 `镜头1【45秒–50秒】`、`镜头1【起始=00:00:45;结束=00:00:50;时长=5秒】`；规范渲染为 `【时长=5秒】【起始=45秒;结束=50秒】` 并保留原连续性标记。时间支持秒、分:秒、时:分:秒，精确到毫秒；无效或冲突时长不会回退默认。结构化合同以原源行核对已声明时长/区间，禁止模型丢失、改写或合并这些明确镜头。
- 分镜采用、参考审核及无配方选片审核均纳入原时间字段；选片审核使用schema4并核对当前内容及上游审核依据，变更后需重新核对。此处描述当前代码合同；切换记录见统一计划25.180–202，具体范围的真实验收须另留证据。

### 原任务分发副本恢复

入口：项目制作运行（无配方及配方共用）→“查看分发副本 / 恢复处理”。身份与原ProductionRun一致：项目所有者或管理员可读，提交还核对Run所有者/管理员及Task属于本轮。无权访问目标404，未登录401；本轮已结束或已有封存发布包时不安排新处理。202仅表示本地后处理已排队，不代表副本或成片通过验收。

- `GET /api/v1/production-runs/{id}/distribution-recovery/`：仅page/page_size/format单值参数，沿原统一分页范围，返回count/next/previous/results。只列本轮已成功图片、视频原任务；每项task_id/kind/state/revision/requested_at/started_at/finished_at/error来自原Task有类型字段。没有处理为not_requested/修订0/空时间；queued/running/ready/failed为此原任务本地副本阶段，不修改生成状态。窄列查询，不读提示词、图或媒体文件，不外呼、不写库；成功200，无任务返回空分页，查询失败不展示旧成功。
- `POST`同一路径：JSON仅包含task_id和expected_revision（实际非负整数）。服务端锁Run与原Task，比较页面读取修订并增加一次；原generation worker处理，不创建GenerationAttempt/Preparation或调用模型。相同旧修订重放200返回已安排轮次，首次202；修订越过一轮或状态冲突409，坏输入/外来Task/不允许的运行状态400。明确拒绝后重读；网络或5xx响应不确定时保留同请求核对，不换修订重试。源文件在worker中从原签名和准备核验；不接受客户端提交新素材、参数、身份或执行覆盖。
- 仅采用当前已配置的分发方案/披露/字体创建副本，母版字节与原费用事实不变。既有完整副本幂等复用，损坏的已发布副本不能被静默覆写。处理锁与Task领取均有心跳；过期后明确失败，需人工重试。完成后沿原Run继续：配方仍按原审核流程推进，人工制作仍需确认成片，恢复请求本身不封存、不对外发布。

本项新增0158 Schema，未应用真实库；当前为隔离API、真实本地PNG副本、组件/构建测试，最终扩大回归见改造计划25.113。未执行实际浏览器、长视频、模型调用或运行服务切换。

### 配方配置与生产链核对

配方创建沿原 `POST /api/v1/production-runs/` 字段：AI 剧本明确提交 `idea`、`episode_count`、`shots_per_episode`、`episode_duration_seconds`；`style_hint` 可留空。导入剧本只提交已确认修订，或在声明 `script_structure/ai` 时同时提供三项计划。未提供不读取项目默认值。已有分集制作必须明确选择非空 `episode_ids`。本轮值通过配方来源映射的 `run.*` 目录项进入精确实例，不能用客户端传入其他运行身份替代；结构化的可选 `planning` 输入由上述三项组成封闭对象，不增加另一个创建 API。结果必须满足明确数量与总时长，原稿明确时间不可改写。发布及新建运行均核对精确配置和内容、审核、分镜、媒体、选片、装配、交付的完整链路；草稿可不完整，但不能绕过发布检查。创建只冻结本轮计划、配置和实际阶段，不启动模型。完全相同的幂等请求返回原运行；同键不同输入返回409，配方后续退役不重写已受理的运行。正式发布、创建至封存已通过隔离接口串联；实际环境切换和真实模型验收仍按实施台账分工执行。

`GET /api/v1/production-readiness/`：仅超级管理员，入口为系统设置→生产配方→“配方配置与生产链核对”。无请求体；只读已发布Recipe合同、精确执行实例及其辅助依赖、原组合Smoke和本地资源/Worker事实，不创建请求、不外呼、不写审计或更换配置。成功200返回schema2、request_checked=false、总体状态与逐配方capabilities；没有发布版本为unconfigured，权限不足403。

每个能力项标记scope=configuration_only，包含state/required/reason_codes/warning_codes、去重后的instance_count与viable_instance_count。实例数不可完整核验返回null而非0；完整配方合同无效返回blocked和recipe_contract_invalid，不把缺失旧路线解释为无需执行。输出按text/script/image/video/audio及assembly分组。显式备用路线按组核对：组内至少一个可行才算该组具备配置条件，未就绪备选显示提示，不当作额外必调实例。配置辅助实例不表示每次必调；其问题显示提示，实际需要生成字段时仍独立准入。证据TTL与容量策略和实际Preparation使用同一决策器；目录尚无本次输入，容量为unknown，不能用较小历史样本伪造covered，也不能代表本次预算或实际调用已授权。查询异常显示明确原因，不回退旧快照；未知类型或旧schema在页面显示读取失败。

逐配方`validation_errors`保留合同校验的原字段路径`field_path`、消息`message`和可空原错误码`code`，`phase`区分definition、requirements及pipeline。已知步骤和路线提供从0开始的`stage_index`/`route_index`及`stage_key`/`stage_kind`/`routing_key`；完整定义或无法定位的错误这些字段为null，不猜测归属。无合同错误时为[]；内部读取异常仅返回readiness_evaluation_failed，不披露异常原文。设置页面的配方版本详情按需展开这些错误，不用旧成功详情覆盖读取失败。该管理员诊断共用正式作者校验核心、全部当前定义/来源合同及本配方动态文本输出目录，读取不取得registry写事务/锁；配方保存、发布、Run/Preparation写入仍在原参考保护区内。匿名health/ready只投影状态与原因代码，不返回配方身份、字段明细或错误正文。第336批隔离API/SQL及页面组件验证证据在唯一执行台账记录，现用加载和实际页面验证另列，不将组件渲染等同正式实页验收。

`GET /api/v1/health/ready/` 无鉴权、无参数，HTTP200/503仅表示基础服务检查是否通过，仍保持存活与业务能力分离。其capabilities使用上述schema2检查范围，数据库或迁移不可用时明确blocked；不会同步探测模型服务。配方详情显示配置告警与原因，刷新失败不保留旧成功。该合同已有隔离API/组件验证及后续服务切换记录；当前业务验收以统一工作包为准。

P4任务查看：`GET /api/v1/tasks/{id}/comfyui-workflow/`只读本次主Preparation，不再导出旧GeneratedWorkflow。`workflow_format=api`（默认）不外呼；`ui`用该任务精确目标的节点目录派生画布，失败明确503、不切换默认服务。`download=true`返回同源附件。响应区分prepared/staged、缺少回执数量，给出实际Attempt/Preparation/执行实例版本/Workflow版本ID；request_sha256是完整准备请求摘要，content_hash是本次导出JSON摘要，不混用。缺失回执可以查看准备图，但import_ready=false；损坏回执或请求明确400，没有旧图回退或读侧修复。GET不上传、重编译、提交或写库，仍按原所有者/管理员授权。UI导出是下载后手动导入，不是跨页面自动载入。实现进展最初见计划25.75，后续切换见25.180–202；具体导出行为须按当前对象核验。

P4源码新增任务`submission_state=terminal_succeeded`，仅表示原Comfy请求明确完成；`status=succeeded`还要求实际主Preparation校验、媒体探测、签名与受管GeneratedArtifact归档通过。远端完成但产物不合格时任务仍为failed，不能展示为可采用候选，也不是“提交结果未知”。主Attempt记录`execution_output_invalid`；未知提交记录`uncertain_remote_response`，保留原token和预算，不自动重发。取消指令尚未得到远端确认时保持cancel_requested。该枚举由0153迁移声明，后续已记录整体切换；隔离验证见计划25.72，不把远端成功等同平台产物成功。

当前统一执行实例合同：阶段目录version 4不接受独立`prompt_compile`，并明确登记可重复的`structured_text`职责，提示词字段与组装归属真实调用的Preparation。原异步媒体队列已接入同一冻结请求，生成成功只保存未采用候选；远端未提供实付时保持未知和原预留，真实费用不因下载失败或取消而丢失。共同Comfy读取器已核对完整编译请求，实例定义包含冻结节点监测规则，生产准备包含本轮运行时限。Comfy生产、逐镜输入、无配方制作和schema3发布已有后续接线及切换记录；完整真实验收仍以第0.1节为准，不沿用25.65–25.75的旧开发状态。

### 项目创建时的服务端默认值

`GET /api/v1/projects/?owned_only=true` 为执行输入的项目来源选择提供当前账号拥有的项目；参数只接受 `true` / `false`，默认 `false` 沿原所有者/管理员管理范围，非法值返回字段级 400。它只缩小只读查询，不授予执行权限：管理员可管理其他账号项目，不代表能将其作为自己请求的来源。沿原登录态、分页和项目汇总响应，空列表表示当前范围无项目；不创建任务或调用模型。执行来源仍由服务端核对精确项目及确认修订。查询默认按更新时间、ID降序稳定分页；页面显示范围和空状态，不自动选项目或最新修订。

执行实例组合烟测面板在当前页含进行中任务时定时读取原 evidence 接口，结束、读取失败或关闭面板后停止；后台页面和未保存编辑期间暂停。所选准备随服务端修订变化重新读取，不覆盖未保存正文、不自动确认字段或重跑任务。读取失败保留已知结果及错误提示，恢复需显式刷新。

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

- **入口**：设置一级导航“能力关联与验收” → “能力与关联总览”，沿用 `/settings/acceptance-smoke?view=relations`，不新增配置编辑器。执行实例版本、模型版本、模型部署、提示词策略、Projector 版本和配方列表使用同一摘要组件。
- **查询**：`GET /api/v1/registry-relations/`，仅 staff；未登录/未授权返回 401/403，不支持写入。必填 `kind=field|field_version|structure|source|execution|model|deployment|workflow|prompt|projector|recipe|output`，旧 `binding` 类型返回400。可选 `ids` 为逗号分隔的 1–50 个 UUID；`search` 最多 160 字；`page` 从 1 起；`page_size` 最大 50，默认沿用平台配置并受安全上限约束。总览每页 25，嵌入摘要每批最多 50、最多两个请求并行。
- **逐层查看**：同时传 `source_kind`、`source_id`、`relation`，`kind` 必须是该分组的目标类型。例如先查 `?kind=model&ids={model_version_id}`，再采用返回分组 URL 的 `source_kind=model&source_id={model_version_id}&relation=executions&kind=execution` 查执行实例版本。分组计数与明细共享同一查询条件；不是按名称猜测关联。
- **字段与结构引用**：`field → versions/structures/prompts`，`field_version → field/structures/prompts`；`structure → fields/sources/outputs/prompts/executions`，`source → structure/prompts`。策略提供`structure/fields/sources`，业务输出提供`structure`，执行实例提供`structures`，继续沿原实例/配方关联下钻。只用确切外键，节点或端口重复使用同一版本仍只计一个对象；名称相同但版本不同不会合并，不扫描原始响应或复制引用事实。导航参数`field_definition/field_version/structure_version/source_version`在字段中心打开原管理器；当前配置关系与运行期冻结历史分开，不把本页称为全部历史影响扫描。
- **响应**：`count / next / previous / results` 分页；`generated_at` 是本次读取时间，`context_label` 表示来源与关系。每行返回精确 `id / kind / label / status`、`capabilities` 声明摘要、`groups[{key,label,kind,count,url}]`、`notices` 和 `config_url / overview_url / acceptance_url`。不返回节点图、完整规格、Projector Definition、配方 Definition、凭据或运行参数。缺失的可选关系计数为 0；读取失败不能用 0 替代。精确 `ids` 对象不存在返回空结果；来源不存在返回 404；参数不完整、未知关系或超过批量/分页限制返回 400。
- **语义**：执行实例的 `deployment / workflow / prompt` 读取精确外键；`auxiliaries / consumers` 是辅助调用及反向引用，同一目标承担两个用途仍只计一个对象。模型、部署、工作流、策略的 `executions` 只按真实外键定位，不推测模型族策略。模型部署的 `validated / candidates` 分别表示校验工作流/其他同模型工作流，不是组合烟测结论。Projector 关系按冻结版本 ID 和摘要定位，不替换成 current；完整身份与执行合同有效性仍在工作流详情及验收中核对。人工/无提示词执行方式没有策略是正常状态；文本及 async REST 不统一要求 Workflow / Projector。
- **配方**：只读取 schema3 的 `stages[].routing.routes[].execution_instance_version_id` 及素材生成阶段的 `stages[].variant_routing.routes[].execution_instance_version_id`，并支持反向查看。输入 literal、普通说明、其他相同名称字段及旧资源路线不算引用。计数是唯一目标对象数，重复阶段/角色引用不重复计数；引用路径保留角色。最终选中路线仍查本轮 ProductionRun；底层部署/策略通过执行实例逐层查看，不另猜隐式资源关系。
- **业务输出**：`kind=output` 查询 `BusinessOutputVersion` 的名称、版本、状态、解析协议和引用数量；搜索名称或 `parser_key`。分组 `output → executions` 与 `execution → output` 只读取 `ModelExecutionInstanceVersion.output_version` 精确外键，不按同名／同协议／输出族的最新版推测。包含草稿、退役和停用配置，不计调用／审核次数；原本无独立输出引用的历史实例显示0，不补挂新版本。可继续沿执行实例查看配方／辅助调用方，但不是全部运行期数据消费者的影响清单。`config_url` 的 `section=models&output_version={id}` 打开原业务输出管理器中的精确版本，支持返回关联总览；错误／不存在的身份不打开其他版本代替。管理器显示同源引用摘要，跳转、关闭、切换未保存草稿需确认，提交期间禁止离开；页面刷新对未保存内容发出浏览器离开提醒。列表、查看、刷新及跳转均不调用模型、不新增审核或关联记录，输出后继不自动替换旧引用。
- **下一步与副作用**：未配置执行实例、缺失 Projector 记录均有说明；原对象配置入口携带 `relation_context` 返回上下文。`section=executions&execution_version={id}` 打开精确版本，不受列表当前页影响；加 `execution_view=smoke` 查看该非草稿版本的组合烟测。无效地址、对象缺失、错误归属与过期响应不选择其他版本代替，未保存编辑先明确阻止跳转。本 API 只读持久化事实，不调用 Provider/ComfyUI、不执行校验、发布、生产，不新增关联事实表。配置页沿用原写入权限。
- **性能与验证边界**：分页摘要在一条带相关计数子查询的 SQL 中读取；关系筛选额外读取一个来源对象，无逐行网络请求。FK 条件复用现有索引，配方的显式引用检索扫描小型配方注册表（不扫描 ProductionRun 历史），应随注册表规模观察成本。SQLite 的精确计数/分页、权限、空关系、历史引用和前端过期响应隔离由回归测试覆盖；PostgreSQL JSONPath 实现须在对应部署库实测，不以本地 SQLite 测试代替生产数据库验收。

### 工作流关联配置查询

工作流关联区区分“校验部署”“执行实例”和“Projector”。校验部署是资源核验目标，执行实例才是业务调用组合；具体策略从精确执行实例版本查看，不按模型族推测。

- **入口与权限**：现有 `GET /api/v1/workflow-template-versions/`（支持 `template`、`model_profile_version` 等原有筛选、`page` / `page_size` 分页）及 `GET /api/v1/workflow-template-versions/{id}/` 返回 `configuration_links`。沿用 staff 登录权限；未认证/未授权为 401/403，详情不存在为 404。列表页面按每页 50 条读取。
- **响应口径**：`model` 为工作流声明的精确模型版本及其全部部署数，`validated_deployment` 仅表示持久化的校验目标，未校验时为 null，不选择默认部署。`execution_instances` 只统计 `workflow_version` 精确引用本工作流的执行实例版本。`total` 包含草稿/退役等全部版本，`active_total` 仅统计已发布且实例启用的版本；数据库先筛选再排名，`preview` 最多三项，包含实例名称、精确版本号、操作、模式及提示词来源。这里“启用”不是组合烟测通过，也不代表某次任务实际采用；本次调用以冻结Preparation为准。
- **Projector 来源**：`projector` 只根据工作流 `input_contract.contract_projector` 的版本 ID、key、版本号、SHA、引擎和操作身份关联，绝不按 current 版本补齐。`state=not_recorded` 表示没有记录，`invalid` 表示引用缺失/不一致，`linked` 表示冻结身份与对应发布/退役版本元数据一致；这不是重新进行合同校验或验收。已退役及非当前版本可追溯查看。
- **失败与边界**：模型不存在或未声明族不影响已知的执行实例外键查询，不补猜族级关系。接口/关联响应异常由页面明确报错并允许刷新，不伪装为“没有关联”。只读本地数据库，按页批量获取有界窄列摘要，不访问 ComfyUI、不创建执行实例、不校验、Smoke 或发布。
- **页面跳转**：只有一个启用版本时定位 `section=executions&execution_version={version_id}`；多个版本时进入该工作流的精确执行实例关联列表，不替用户选第一项。“更多关联”展示启用版本预览、其他版本数量和全部关联入口。Projector 使用 `section=workflows&projector_version={version_id}` 只读打开冻结配置，可进入方案族管理后继草稿。`workflow_context` 保留来源工作流，全部跳转无写操作。
- **验证**：隔离回归覆盖分页/详情一致性、权限、冻结版本追溯、异常Projector、无校验目标、精确外键及先筛选后预览、恒定批量查询；前端覆盖链接与严格响应解析。后续环境切换已有记录；具体当前验收范围另核对，机器字段以根 OpenAPI 为准。

## 认证与敏感信息

受保护端点使用 JWT。provider credential 通过 SecretBinding 关联；秘密创建与轮换只接受超级管理员的 write-only `secret_value`，读取仅返回 configured 与生命周期元数据，不能据此宣称远端调用 usable。客户端不得记录或显示原始密钥、密文、nonce 或 key id；本文也不提供密钥示例。

首次临时密码登录时，仅允许 `GET /api/v1/auth/me/` 与 `POST /api/v1/auth/password/change/`。改密成功后旧 access/refresh token 立即失效，客户端必须重新登录。

浏览器对既有 `POST /api/v1/auth/token/refresh/` 使用同源 Web Locks 串行续期。并发页面在锁内重新读取当前凭据，已有新 access 时不重复交换已轮换的 refresh；每个业务请求最多重试一次，账号改变时不重放。只有明确的续期拒绝且凭据仍对应原请求才清除会话，网络/5xx 错误不等于登录失效。路由不再因迟到的401清除其他页面的新登录；账号切换或退出会清空旧页面业务状态。未改变后端 JWT 失效、权限或密码规则；不支持 Web Locks 的浏览器会明确提示，不能退化为并发交换。

超级管理员专用治理端点包括 `/api/v1/access/users/`、用户的 `role`、`status`、`temporary-password` actions、`/api/v1/access/audit-events/`、`/api/v1/secret-bindings/`、`POST /api/v1/secret-bindings/{id}/rotate/`、`POST /api/v1/secret-bindings/{id}/status/` 与 `/api/v1/secret-audit-events/`。SecretBinding 只允许创建、轮换和启停，没有修改秘密正文或删除入口；SecretVersion 与密钥审计记录只读且不可变。普通成员和 platform administrator 对秘密治理端点均无权限；平台管理员仍可管理不含秘密材料的平台 catalog/config，并可只读查询 `/api/v1/platform-lifecycle-audit-events/`。

平台 catalog/config 生命周期写操作追加 `PlatformLifecycleAuditEvent`，记录操作者、动作、对象标识、稳定原因代码、变更前后 hash 与扁平安全摘要；不保存 secret、endpoint、path 或原始 JSON。客户端可用 `X-Audit-Reason-Code` 提交稳定原因代码；未提交时服务端写入对应动作的稳定系统原因代码。审计 collection 支持 actor、action、object_type、object_id 与 reason_code 过滤，不提供 update/delete。

对状态变更请求，应使用 OpenAPI 声明的幂等/条件字段。冲突、等待人工决策、外部响应不确定和上传契约缺失均显式呈现；系统预算或次数提醒进入待人工确认，不作为终态失败，不会伪装成功。

## 素材库：本地入库、独立生成与下载

### 主图、变体系列与确切素材版本（M9基础管理）

这是素材库与配方生产共用的关系API，不生成媒体、不修改Bible正文。素材库已有“添加主图 / 变体素材”的上传/复用入口和“主图 / 变体关系”的浏览、名称/Bible关联编辑及关系解除；配方和独立生成沿同一个登记writer写入这里的确切版本，不通过公开POST补造生成来源。系列角色选项由本集合的OPTIONS元数据`actions.POST.role.choices`提供，不在页面另建角色字典。开发代理须转发OPTIONS并保留Django响应及鉴权失败，不以CORS空204冒充元数据成功；代理配置见[运行说明](operations.md#4-启动与状态)。代码、隔离验证与现用环境切换分别记录于详细设计第18节，不能用关系存在证明本轮真实模型验收。

生成来源由原事实区分：配方版本的`generated_artifact`含原产物ID、**产物状态**、`production_run_id/generation_attempt_id`，`generation_job`为空；独立生成指向原`generation_job`，不补造生产调用。产物状态不是调用状态；“查看原生产调用”按原Attempt ID打开任务中心，再读取实际状态/返回。生成依据`generated_from_id`固定原请求使用的主图版本，系列/版本GET只读、不登记或换成最新版本。图片阶段尚无产物时，从运行页或准备列表的“查看本阶段原调用”按确切Run/Stage查询任务中心；没有确切Attempt时不冒称已定位某次调用，不提供文本修订作为媒体修复。

| 方法/路径 | 输入与结果 |
| --- | --- |
| `GET /api/v1/asset-reference-series/` | `page/page_size`标准分页；可组合`role=primary|variant`、`primary_series_id`、`bible_variant_id`、`search`名称（空串不筛选）、`media_type=image|video|audio`、`bible_linked=true|false`和确切`asset_id`。返回`count/next/previous/results`，每行含ID/名称/角色/所有者/父主系列ID/可空BibleVariant ID、版本数/变体数和时间，不嵌套加载所有版本 |
| `POST /api/v1/asset-reference-series/` | 必填`name`（任意语言，最长160字符）、`role`；变体必填`primary_series_id`，主图必须无父系列；`bible_variant_id`可省略/空。所有者由登录用户确定，201返回系列摘要 |
| `GET/PATCH /api/v1/asset-reference-series/{id}/` | GET单个系列摘要；PATCH仅接受`name/primary_series_id/bible_variant_id`，未传不改、null解除可选关系，角色和所有者不可改。父系列须同属所有者且为主图；不能改成多级/循环关系，不能改写已有版本的设定/主图生成依据 |
| `DELETE /api/v1/asset-reference-series/{id}/` | 204解除本系列及其版本关联；有子变体或仍被引用的生成依据时409。**不删除原Asset、文件、Bible、任务或产物** |
| `GET /api/v1/asset-reference-versions/?series_id={id}` | 必填确切系列ID，支持`page/page_size`，按版本序号倒序分页，跨页仍可按版本ID读取；不存在/无权系列404 |
| `POST /api/v1/asset-reference-versions/` | 必填`series_id/asset_id/source`，公开来源只接受`upload|existing`，可选`setting_revision_id`须属于本系列明确关联的BibleVariant。首次201，同系列/Asset/来源/设定修订重复关联200复用原ID，序号由服务端分配 |
| `GET/DELETE /api/v1/asset-reference-versions/{id}/` | GET返回该确切版本及原来源引用；DELETE仅移除该关联，204；被其他生成版本用作主图依据则409，原Asset和文件不变 |
| `GET /api/v1/asset-reference-series/{id}/bible-options/` | 同系列所有者、同主图/变体角色的原BibleVariant选项；`page/page_size`分页，`search`按项目/实体/设定/引用名搜索，`id`可读取当前确切选择。返回`count/next/previous/results`；不要求审核、不创建新设定 |
| `GET /api/v1/asset-reference-series/{id}/removal-impact/` | 读取删除此系列及其版本关系的影响，不执行删除；200返回下述影响对象 |
| `GET /api/v1/asset-reference-versions/{id}/removal-impact/` | 读取仅解除此确切版本关系的影响，不执行删除；200返回下述影响对象 |

所有请求需登录；普通用户仅访问自己的系列与版本，staff可管理已有系列，但关联的Asset仍必须属于该系列所有者或为全局素材，不因管理员身份混入他人的私有素材。停用Asset及技术Smoke素材不能加入。`upload`还需匹配原Asset正式上传事实，客户端不能伪造该来源；`existing`表达本次复用，不覆盖Asset原取得方式。关联写入通过`resolve_media_fulfillment_target`复用`resolve_execution_asset_media`核对原Asset的受控文件、非空字节、checksum及实际媒体类型/观测事实，登记MIME须与实际文件相符；缺失、变化或不可读取在`asset_id`明确报错，不保存空版本。系列/版本列表仍不扫描文件或访问远端。写入时文件检查通过**不等于后续永远可播放、模型生成成功或已采用**，实际读取/执行仍重新核对。

M4-01已有素材目标段沿用上述POST，不增加HTTP参数/响应字段。共用服务明确区分`upload|existing`零调用履约与`generate`生成准备：保留系列主图/变体角色、确切Asset和可空设定修订，执行实例、候选策略、生成输入和主图生成依据均为空；只有原关系writer保存AssetReferenceVersion，不创建Job、Preparation、ProductionRun或审核记录。可选设定通过原确切修订读取器核对正文摘要、版本、实体和固定主图设定依赖；仍不要求审核。省略修订保持空值，显式旧修订不换成当前修订，锚点不复制到素材/关联；设定依赖不是模型实际参考图。无主图素材版本也可直接关联上传/已有变体，不能把它记作参考主图生成。配方已有/上传素材满足本轮需求已接原履约路径；主图/变体生成请求及原混合阶段已有独立接线，不由此POST代办。

M4-03内部关联适配器 `attach_recipe_material_asset` 复用原关系writer：只接受冻结单元、确切需求/执行计划和显式系列/Asset，复核原M3审核、系列归属/角色/父主图外观及真实图片；已有生成准备仅在该单元全部配置候选均有明确失败且无产物的当前主调用时允许接管，其他情况不得替换。内部 `select_recipe_material_asset` 在同一原事务中明确新增 `ProductionMaterialFulfillment`，引用该确切版本、计划/需求、原目标摘要和实际选择人，不复制素材、生成候选或审核记录。相同请求复用原选择，不同素材/来源/系列冲突；已有变体准备不能再补选主图来源。原共用准备writer反向拒绝已选择素材的单元。只读执行验证核对原选择及文件，无选择时返回缺省、不会搜库补选；明确历史读取可不重套今日M3审核，但仍核对版本/文件。正式需求核对与履约HTTP入口见下节；原调度已核验并跳过本轮明确满足的单元，来源配置/人工页面及履约结果审核已分别接线，隔离混合审核已验证，整个M4仍待后续验收，不代表整个Run已完成；素材库POST仍不会自动满足某个Run需求。0220新增选择关系；2026-10-02已随0213–0222迁移现用库，迁移不是正式选材入口完成。

第185批内部主图依赖读者现可消费本轮已明确选择的确切主图版本（包括保留原generated来源的复用版本），核对其确切计划/需求、版本、来源及文件；库内关联而无Run选择仍不能供给变体。冻结回执只保存真实fulfillment_id/version_id，不补造decision/artifact/preparation；与生成来源的原四ID回执互斥。原变体输入和后续generated_from使用这份确切版本，当前提交复核来源，历史读取不重选。生成输入的原OpenAPI JSON详情形状不变；CR-M4-SOURCE-FLOW-01已按用户澄清落实为配方配置上游/已有/上传来源及自动/人工策略，不再待选择时机。原调度跳过已明确满足单元，变体生成仍等待确切主图；来源作者、选材页面、库内唯一确切版本自动履约及全部失败后的原请求明确重试已接通；人工确切版本选择及全部明确失败后换成图来源已接通；完整验收边界仍见详细设计第18节。

第192批调度及正式结果快照共用 `read_recipe_material_fulfillments`，仅投影本轮已明确保存的选择：fulfillment_id、unit_key、requirement_key、reference_version_id、target_sha256、source。非空列表保存在原Bible图片阶段canonical.material_fulfillments；全复用不伪造entities中的生成候选，混合阶段同时保留原真实生成候选。保存及当前复核使用同一验证来源，不按库内最新版本补选；履约结果审核通过上述原writer消费这份回执，不以此替代完整混合审核验收。

上传沿`POST /api/v1/assets/`正式multipart入口，取得Asset ID后才提交系列关联；不新增上传接口或复制文件。先明确选择/创建主图系列，或为变体指定一个父主图系列；无需剧本、Bible或ProductionRun。上传失败不尝试关联；上传已成功但关联失败时保留原Asset，页面保留当前选择并只重试关联。创建系列响应未知时先查询核对，不能自动再创建；关联重试沿原系列/Asset/来源复用原版本。上传或复用变体的`generated_from`为空，不声明其由主图模型生成。

系列关联不额外套用模型执行/Smoke的单素材大小上限，原上传限制不变；这里只按已授权文件核对真实字节与媒体可读性。具体执行实例需要的大小、时长、数量等约束仍在执行输入核验时检查，不把“可加入素材系列”冒称“所有模型均可用”。

列表筛选先于分页：媒体类型以原Asset记录的MIME判断是否存在符合条件的版本，多个匹配版本只返回一个系列，不缩减该系列的完整版本计数；同时指定`asset_id`时必须同一版本满足条件。`bible_linked`省略表示不筛选，不等于false；缺少文件不会在列表中被静默隐藏，列表不扫描文件。详情里的父主图/同组变体和版本分页不继承外层筛选，避免隐藏相关版本。预览按版本里的原Asset ID调用既有`GET /api/v1/assets/{id}/`读取受控媒体地址，失败显示确切版本并允许重读，不替换成其他图片。来源主图跳转按`generated_from_id`读取确切版本，即使不在当前第一页也不替换为最新版；任务链接仅使用原`generation_job`或`generated_artifact.production_run_id`。页面切换仅改变浏览状态，不写采用事实或生产绑定。

通用输入选材复用这些GET接口与原Asset详情接口；“选择此版本用于本项”重新核对版本ID、系列、序号及原Asset ID，再回填已有媒体来源合同的`asset_id`。结构媒体值仍是`{asset_id}`，有序来源项保留原`item_key/purpose`，没有新增可变“最新版本”别名或选材写接口。类型和用途由使用处配置，系列只是查找路径，不把本次用途写成素材全局属性；此输入来源仍是`asset_library`。本地上传继续使用原multipart接口，返回Asset后再按`asset_upload`来源回填。关系失效/无权或素材停用、类型不符会明确失败，不回退其他版本。精确Bible绑定及输入位置的后续业务装配不由本选材界面代办。

创建变体系列示例（ID为请求方实际选择的主系列，不按名称推断）：

```json
{"name":"沈念·校服","role":"variant","primary_series_id":"11111111-1111-4111-8111-111111111111","bible_variant_id":null}
```

关联已有图片、视频或音频示例：

```json
{"series_id":"22222222-2222-4222-8222-222222222222","asset_id":"33333333-3333-4333-8333-333333333333","source":"existing","setting_revision_id":null}
```

版本响应包含`id/series_id/version/source/setting_revision_id/generated_from_id/created_at/updated_at`及窄投影`asset={id,name,kind,mime_type,is_active}`。`generated_artifact`为null或`{id,status,production_run_id,generation_attempt_id}`，`generation_job`为null或`{id,status}`；二者沿原事实读取，不带模型原始响应、锚点、文件本地路径或完整请求。上传/复用版本的两种生成来源和`generated_from_id`均为null。原生成版本可读，但公开POST不能传`generated`、生成来源、生成依据、owner或version来补造事实；实际生成写入由共用登记服务依据原执行请求完成，公开关联接口不能代写生成事实。

错误沿统一`error.status_code/code/details`：400定位`primary_series_id/bible_variant_id/asset_id/setting_revision_id/source`等字段或结构约束（包括不存在的输入引用）；404表示访问目标不存在/无权，非法UUID路径同样404；409 `asset_reference_in_use`表示关系仍被引用，可先分页查看子系列/版本处理依赖，不以费用/审计留存为门禁；409 `bible_variant_conflict`表示所选确切设定正文/归属损坏或摘要版本已停用，保留原素材与已有关系，须查看原设定问题，不自动替换/修复或回填。原文件删除仍属于Asset正式入口，与此处移除关系是不同动作。

Bible选项仅投影`id/name/reference_name/project_id/project_title/character_name/scene_name/prop_name`，不加载锚点、设定正文或原始请求；不属于该实体种类的名称为null。staff编辑他人的系列时仍按系列所有者查选项，不能绑定staff自己的项目。省略`search/id`不筛选，空搜索合法；无结果正常返回空页，错误参数400，目标系列无权或不存在404。

影响对象为`{target_type:series|version,id,name,version_count,asset_count,variant_count,dependent_version_count,production_use_count,can_remove}`。`version_count`为将解除的版本关系数，`asset_count`为涉及但保留的不同Asset数；`variant_count`是仍依赖父主系列的子变体数，`dependent_version_count`为目标之外仍引用其版本作生成依据的版本数，`production_use_count`为明确引用目标版本的生产素材选择记录数（不是生成次数或审核数）。任一依赖非零时`can_remove=false`，页面分别显示三类计数并禁用解除；不是模型/费用/审核门禁。预览只读、无媒体扫描/外呼/写入；DELETE仍在原所有者事务内以外键保护复核，返回409并保留依赖，不自动删除Run或选择记录。预览不是删除令牌或永久许可，不新增签名/审批流程。

页面先读取当前系列，仅PATCH真实修改的名称及可选Bible ID；null明确解除，不按名称猜测设定。改名不改变Asset/版本ID或旧请求；现有版本引用的确切设定修订/生成依据不被改写，不满足既有关联约束返回字段错误。关系删除弹窗展示具体目标、计数、保留对象及不可恢复影响，可取消；失败不显示成功、不自动重试。原Asset删除入口显示新关系引用的数量/处理位置，返回原结构化错误；删除同一上传object key的一个Asset时保留其他Asset仍引用的文件，最后一个引用删除后才清理文件。不涉及旧运行/配方清理或费用核实。

隔离验收见`studio.tests.test_asset_reference_api/test_asset_reference_browse`及前端`assetReferenceManagement.test.ts`：无剧本关系创建、版本分页、作用域、错误引用、去重、来源只读、命名/Bible关联、影响只读/依赖变化与共享文件保留。M4-01第二段新增`test_media_fulfillment_targets`11项，原关系API/上传/浏览/模型约束及双入口生成目标共6模块62项通过，覆盖零调用、旧修订、省略设定、MIME错误、不可变设定损坏和失败回滚；该历史批次未重跑前端。M4-03第184批新增`test_recipe_material_fulfillment_choices`核对确切运行选择、互斥、回滚与生产引用删除保护；前端11项及桌面/390px真实组件隔离夹具核对生产引用计数和禁用解除，类型/Vite及严格OpenAPI比对通过。网络禁止、只用隔离数据库/临时文件或内存目录，未验现用页面；不以这些测试代替整个M9/M4或真实生成验收。

### 配方已有/上传素材的明确履约（M4-03部分）

配方图片候选按原素材单元处理：每个已配置调用均到达可核对终态后，明确失败且无产物的候选不阻止同单元成功图片进入原选择器。配置自动选择时沿原候选编号规则选定；人工模式保留 `waiting_material_selection`，仍通过原 `material-selection-context` / `select-artifact` 操作，不代选、不重发失败调用。登记使用同一原选择和素材版本，变体只消费确切主图；阶段结果只统计实际成功产物。候选全部明确失败的单元进入 `waiting_material_recovery`：Stage 保持 running，Run 为 awaiting_input，退出自动扫描；原页面提供“重试当前阶段”的只读影响预览和明确确认，确认前不新建调用或选空结果，未满足主图的变体不派发。结果未知、取消、晚到忽略、归档不完整或有效图片校验失败仍显式拒绝，不能当作可重发失败。全部明确失败后也可通过下述原履约入口改用已有/上传成图；不代表整个M4验收。

第202批原配方图片审核页面通过同一阶段输出快照的 `canonical.entities[].fulfilled_materials` 展示已履约图片；每行包含 `asset_id/asset_name/checksum/mime_type/appearance_role/reference`，reference原样指向全局 `material_fulfillments` 或 `material_generations` 中的确切选择。该数组是原事实的显示投影，不是新增输入、选材或审核事实。页面保留原主图/变体和原版本，通用上传图片按确切关系校验，不强行改类别；绑定仍提交原 `bible_image_bindings` 的Asset ID与摘要。运行页和人工入口复用同一校验/提交器，数量上限沿原生产能力目录加载；缺失投影、素材失配、漏选履约、替换固定主图或读取失败不能提交，同快照轮询保留草稿。主图/变体可按12项分段查看全部来源，版本标识折叠展示。该子段已通过隔离代码及页面验证，未完成上游自动履约/失败接管或M4模块验收；现用环境边界见详细设计第18节。

混合审核通过 `material_generations` 引用原单元、需求、选择决定、生成产物、登记版本及真实主图依据。生成变体只进入参考清单；原已选主图保持锚点，不重新选择多候选、不复制文件或重记人审。原人工请求提交已选生成候选时也消费同一已登记Asset；版本缺失、选择变化和文件漂移拒绝审核。跨轮保留图片沿实体内部审核引用读取确切原版本；该 `image_binding_review` 由原writer与回执原子写入，不属于公开请求/响应字段，客户端显式提交会被拒绝。通过原实体PATCH部分解绑保留剩余图片依据，全部解绑清空内部引用，不删除素材/版本/原回执；有审核引用的实体不可跨项目移动。缺失或损坏依据不退回素材库猜配，不回填历史。内部Schema迁移0223是否已切换见详细设计第18节；人工审核页面、上游自动履约与失败接管尚未全部验收。

第199批审核消费验证：新增7项隔离审核测试与原生成采用15项、冻结策略上下文3项组合，共25项通过（357.668s）。覆盖原阶段推进、审核绑定与审批事务；新增审核测试中的策略授权使用既有测试替身，不代表完整配方准入、调度整链或现用审核采用。

已履约图片继续通过原 `bible_image_review` 审核，不新增采用接口：原图片阶段快照中的 `material_fulfillments` 必须与本轮原选择及确切素材版本一致。配置的自动审核可将已履约主图作为锚点、变体作为参考素材；不要求复用/上传再产生 GeneratedArtifact。审核结果和后续漂移核对保留原履约/版本/来源引用，同一Asset满足多个确切需求时只绑定一次文件、保留全部需求回执。通用上传图凭确切设定关系校验，不改写原Asset分类/上传来源。人工审核沿原请求形状，不得丢弃已履约结果或更换已履约主图；依赖改选仍归M4-06。审核写入失败原子回滚绑定，原选择不删除；自动审批的actor为recipe_policy、decided_by为空，自动生成候选选择也不填写人工选择人。仅此审核消费已接线，上游自动选材、失败接管、混合生成的完整审核与M5消费者仍未全部验收，不能视为整个M4完成。

第197批验证：来源/复用主图/HTTP/原选择共50项隔离后端测试通过1031.130s，定向OpenAPI1项另通过；前端27项、类型检查/Vite及桌面/390px实际组件隔离页通过。覆盖来源约束、共用准备防绕过、混合生成及零调用等待/履约；未保存现用配方、未完成运行选材表单或执行真实模型，不视为整模块验收。

第196批提供正式HTTP入口及platformApi类型适配；第197批接入配方主图/变体来源配置与原调度待来源分支。第198批接入原运行详情的成图履约表单；上游自动履约、失败转人工及后续审核消费尚未完整交付，不以子段可用代替M4模块手动试用验收。这里选择的是“本轮需求的成图”，不是模型参考图片输入，也不是生成候选审核。

页面入口：项目工作台 → 本轮制作详情 → 展开图片生成阶段 → “本轮素材需求”。原阶段冻结需求按50项分页，显式点击“查看本项选材”才读取上述context。目录按确切外观/角色查询原系列接口，选定系列后复用原素材选择/上传控件；无系列时通过原素材库创建并关联本外观，返回刷新，不按名称猜测或暗建关系。“确认此图满足本轮需求”只提交上述POST；原扫描可按既有运行授权继续，按钮不代替审核。提交未知时禁止换图，仅同载荷重试或读取核对；上传成功但关联被明确拒绝时保留原Asset草稿，重读同目标并重新选系列后可再次确认，不重复上传。状态变更、读取失败和迟到响应不能把旧上下文当作可提交数据。

第198批表单验证：30项定向前端测试（含15项新增）、类型检查、Vite及桌面/390px隔离实际组件通过；覆盖只读、失败恢复、上传与选图分离、已保存响应中断后读取恢复且POST不增加。仅为隔离组件/接口适配证据，本批未重载现用后端、未对真实运行提交履约，也不冒称上游自动选材、后续审核或整个M4完成。

仅`bible_image_generation/existing_pipeline`可选配`material_sources: {primary: generate|existing|upload, variant: generate|existing|upload}`；提供时必须同时明确两角色，不接受未知来源、附加键或跨职责声明。省略整个对象保持本生成职责的`generate`默认，不代表已有/上传失败可降级生成，不重写旧配置。目录`material_source_options`随原配方editor-options返回；页面分别配置两角色，切换非生成来源前通过共用确认弹窗明确移除对应`routing`或`variant_routing`及输入，取消不改原值/路线，另一角色保持。切回生成不恢复已移除的输入。已有/上传角色不允许附带闲置路线；全零调用配方不要求执行实例。

第205批增加同一阶段的可选 `material_selection: {primary?: manual|unique_version, variant?: manual|unique_version}`。提供对象时必须非空且仅含已有/上传角色；省略角色策略默认为manual，生成角色仍用原候选evaluator，不接受成图选材策略。目录 `material_selection_options` 随原editor-options返回；页面分别配置两角色。改变来源清除该角色旧选材策略，保留另一角色；切回生成不恢复旧路线，不把自动选材记为阶段人审。

来源与策略随原不可变RunPlan冻结，不新增选择事实源。已有/上传角色的 `unique_version` 在阶段原输入冻结时只查同所有者、确切设定/外观/角色及父主图关系的确切版本（existing允许原source为existing/upload/generated，upload仅允许原上传版本），最多读取两个ID以判定唯一或歧义；不按最新版本挑图。原 `input_snapshot.material_source_versions` 按unit_key保存 `{status: unique|missing|ambiguous, version_id: UUID|null}`；后续轮询不重搜素材库，晚到版本不改变此冻结来源。原自动writer在registry→Run/Stage锁内重新核对原需求、版本、实际文件及生成互斥，复用原履约记录写入点，selection_mode=automatic、selected_by=null；不创建关联版本、Asset、候选、人审或模型调用。已有人工/自动选择优先且不改身份。

生成版本复用：配置existing/unique_version时，原登记generated版本也参与唯一性判定；与已有或上传版本并存时明确歧义，不优先生成版或最新版本。履约直接引用原reference_version_id；回执source及后续素材清单保留generated，自动selected_by_id仍为null。共用read_media_fulfillment_version沿原产物登记读者核对归档文件、原请求、确切设定与generated_from，目标摘要包含原版本/产物/主图身份；损坏不重新关联或改写来源。本轮主图选择不回写已生成变体的原主图依据。当前已接通ProductionRun产物登记版本；独立Job结果版本登记尚未接通，不能凭Job/Asset推断来源。第208批人工POST已支持互斥的原reference_version_id选择，服务端保留真实generated来源，不接受客户端generated声明；第209批已接通全部明确失败后通过同一入口改用已有/上传图片或确切版本，不取消、覆盖或重发原调用。此次没有新Schema迁移，内部隔离验证不等于现用切换或真实模型验收。

无匹配、多版本或冻结文件不可用的未履约单元保持 `running/waiting_material_source`，明确显示失败分类，并由原本轮选材表单人工接管；不改走生成或选择其他版本。确切M3依据变化、快照损坏及已提交回执损坏仍显式拒绝，不伪装成来源空缺。独立有效单元可先履约；缺主图的生成变体继续等待，其他就绪单元沿原路线。共用准备writer拒绝绕过非生成来源；手动完成剩余需求后原扫描沿同一回执继续，全复用/上传零调用。生成全部失败的原请求重试接管见上文；生成版本自动/人工保真复用及全部明确失败后切换成图来源已接通，不以这些子段代替整个M4验收。

| 方法与路径 | 输入与结果 |
| --- | --- |
| `GET /api/v1/production-runs/{id}/material-fulfillment-context/` | 必填查询参数`stage_run_id`、`unit_key`；200返回确切需求、可选择状态及已有选择，不扫描库内素材补选。 |
| `POST /api/v1/production-runs/{id}/fulfill-material/` | 公共必填`stage_run_id/unit_key/expected_run_plan_sha256/requirement_key`；成图选择二选一：仅`reference_version_id`复用原版本，或完整`series_id/asset_id/source`建立原Asset关联。混填/缺项400；新增选择201，相同请求核验重放200；不同请求400且保留原选择。 |

GET返回`stage_run_id/unit_key/run_plan_sha256/requirement_key/appearance_id/setting_revision_id/appearance_role/reference_name/primary_appearance_id/source_mode/can_select/reason/selection`。`source_mode`为冻结角色的`generate|existing|upload`；建立Asset关联时，明确已有/上传模式要求POST的`source`相符；引用确切版本时，existing可复用existing/upload/generated原版本，upload只允许原upload。generate允许尚无准备，或全部配置候选均为以下两种明确可接管状态：主/辅助请求均未冻结、无任何调用且未被工作进程占用的blocked准备；原主调用已明确失败且无产物。其他未提交待准备/待审核状态、在途、未知、成功及独立取消均不能覆盖。不因人工提交改写版本来源或自动选取最新版本。`primary_appearance_id`只在变体有值；`selection`为null或下述回执。已有选择、原Run/Stage状态不允许、不满足上述条件，或主图的依赖变体已有准备，均明确`can_select=false`及原因；原M3设定或已选媒体损坏直接报错，不伪装为可选择空状态。该GET不是提交资格预留，POST仍在原事务重新核对。

确认成图时，未提交受阻准备及其确切辅助准备复用原取消writer停止，与新素材关联/履约回执同事务提交或回滚；原冻结输入、阻断evaluation及已失败请求保留，不补造调用、费用或审核。仅关联素材而未选择本轮结果不会停止准备。已保存回执允许核对它实际停止的准备，不将取消本身当成新的选材授权；原调度读取履约后不再派发被替代的准备。页面明确说明停止范围，人工选择不冒充图片审核或自动生成成功。此恢复子段的验证及现用加载状态见详细设计第18节。

POST的两种选择互斥。复用已有关联时仅提交`reference_version_id`，服务端从原版本读取系列、Asset、来源及设定，核对所有者、外观/角色、父主图系列、确切设定修订与实际媒体；generated还经同一生成归档读者核对原产物/请求及generated_from，不调用关联/登记writer、不新增版本或复制文件。生成变体保留原请求实际使用的主图依据，不改成本轮另选的主图。建立新关联则完整提交`series_id/asset_id/source`，`source`仍只接受`existing|upload`；上传先沿原`POST /api/v1/assets/`multipart入口取得真实Asset，失败不代办，上传成功而履约失败保留Asset。系列沿原关系入口建立/选择；`upload`必须匹配原上传事实。不接受混填、客户端操作者/选择模式、设定正文、generated来源声明、模型调用参数或本地文件路径。计划摘要与需求键使用刚读取的确切值，不以今日设定替换。

提交回执为`id/reference_version_id/series_id/asset_id/source/target_sha256/selection_mode/selected_by_id/created_at`。`selection_mode`必填为`manual|automatic`：人工必须有原整数用户ID，自动必须为null；模型和数据库约束共同验证，前端拒绝缺失或矛盾组合，不推断身份。当前POST仍只支持人工明确选择，服务端记录`manual`及实际请求人，客户端不得提交选择模式或操作人；相同请求读取已有回执，不把原自动记录改成人工。原调度可按冻结unique_version策略自动复用已有成图（包括已生成入库的确切版本）或原上传来源；该POST本身不触发自动选择，也不表示人审采用。Asset分支的关联版本与本轮选择原子提交/回滚；原版本分支仅新增本轮选择，失败不修改原版本/Asset/文件。相同重放保留原模式与操作者，不新建版本；冲突不覆盖。不创建Preparation/Attempt、生成候选、审核、队列或阶段推进；若原Run恰处于当前素材阶段的waiting_material_recovery暂停，成功保存会在同一事务将Run恢复为running，交原持久化扫描按既有授权继续。其他暂停/审核/终态不能恢复，后续未满足单元仍保留各自错误；POST不直接触发模型。提交响应直接投影已提交记录，不在事务后重新读取文件而把已保存选择误报为未保存。0224为此身份合同的增量迁移，保留原人工记录与实际操作人；是否已迁现用环境见详细设计第18节。

第208批原运行选材表单在明确系列后复用`GET /api/v1/asset-reference-versions/?series_id=…&page=…`分页目录、确切版本详情和原Asset预览；页面列出原版本序号/来源以及设定或来源策略不匹配的原因，不自动选第一条/最新项。点击原版本只形成草稿，明确确认才向原fulfill-material提交版本ID；仍保留原素材选择/上传建立新关联方式，两者不会混填。版本目录/预览失败清除该版本草稿，切换需求丢弃迟到响应；未知提交结果锁住原请求，只读核对恢复或同载荷重放，不重复关联/上传。已保存回执不等于阶段审核。20项后端隔离用例与30项前端定向测试、桌面/390px浏览器夹具通过；截图和完整边界见详细设计第18节，非现用业务验收。

权限沿原Run可见范围及活跃所有者，并交叉核对路径Run与所提交Stage；跨Run（包括staff使用另一个Run的Stage）404。无访问权限/不存在的目标404，停用所有者403；未知参数、无效引用、陈旧计划/需求/M3状态、生成准备冲突、素材/来源/文件不符等400，沿统一错误封套明确原因。GET/POST响应均`Cache-Control: private, no-store`，读取无业务写入，不锁定将来的选择。

隔离证据：`test_recipe_material_fulfillment_api`13项与原选择20项、OpenAPI定向1项共34项通过（286.268s）；最新错误响应Schema另复验1项通过。测试覆盖主图+两个兄弟变体、正式上传、严格输入/权限、读取后条件变化、幂等/冲突、损坏媒体和原子回滚，禁止外部网络/生成调用。前端履约及原选材适配13项、类型检查/Vite通过；该批当时未完成选材页面、未重载现用服务或执行真实业务选择；第198批页面接线与验证见上文。

### 原Asset及独立生成入口

- `GET/POST /api/v1/assets/`
- `GET/PATCH/DELETE /api/v1/assets/{id}/`
- `GET /api/v1/asset-generation-jobs/options/?job_type={prompt|asset}&media_type={image|video|audio|script}`
- `GET /api/v1/asset-generation-jobs/appearance-options/?role={primary|variant}&search={关键词}&page={页码}`（当前用户按角色筛选的外观设定目录）
- `GET/POST /api/v1/asset-generation-jobs/`
- `GET /api/v1/asset-generation-jobs/{id}/`
- `POST /api/v1/asset-generation-jobs/{id}/cancel/`
- `POST /api/v1/asset-generation-jobs/{id}/retry/`
- `POST /api/v1/asset-generation-jobs/{id}/resolve-uncertain/`（仅 staff）

素材库以受管 `Asset` 统一承载图片、视频、音频与剧本。图片可标记为人物、场景、道具或通用参考；视频和音频按短剧引用合同入库为 `reference_video`、`reference_audio`；剧本统一规范为 UTF-8 Markdown `script`。本地上传只接受 OpenAPI 与服务端校验器声明的格式；参考视频和音频入库时只探测并记录可验证的源媒体事实。素材生成任务选定 Workflow 和 `CONDITIONING_MODE` 时，以及素材随后进入 ProductionRun 时，服务端都会按该不可变版本的 `conditioning_contract.modes[].dynamic_reference_topology.preprocessing` 冻结合同校验时长、帧率、音轨和预处理要求。剧本支持 TXT、Markdown、Fountain 与 DOCX，PDF 明确拒绝。

`name`、`description`、`prompt_hint` 与 `style` 表达素材本身的业务信息；`source_info` 只表达“如何进入素材库”，由服务端写入并在 Asset API 中只读。`PATCH /api/v1/assets/{id}/`允许无引用保护的图片在通用参考（`reference`）、人物、场景、道具四类之间双向更正，保留原文件；改为人物、场景或道具仍须填写描述和视觉锚点。视频、音频、剧本等其他素材类型不可通过类别编辑互转。正式 Asset POST 必须在上传文件、受管路径和外部引用地址中恰好选择一种，分别记录“用户本地上传”“服务端受管路径登记”或“外部引用地址登记”；客户端提交 `source_info` 会被显式拒绝。独立模型/Workflow 生成、延迟连续生成、素材清单导入和 ProductionRun Bible 审核也分别记录对应来源，不得把任务 UUID 或运行说明写入名称、基础描述或视觉锚点。`source_info` 是面向用户的来源摘要，不是机器血缘权威；`AssetGenerationJob.output_asset`、GeneratedArtifact/ProductionRun 关系和 Asset `metadata` 中的冻结 ID/hash 才用于复验与审计。历史素材（包括已退役且权威任务关系已不存在的旧候选）没有可复验来源时返回空值，页面明确显示“来源未记录”，不会仅凭客户端可写 metadata 猜测或伪装来源。

`metadata.fixture_scope=workflow_smoke` 标识只供工作流真实烟测使用的技术素材。普通 `GET /api/v1/assets/`、项目新建、分镜和 ProductionRun 生产选择器不会返回它；即使绕过页面提交，分镜绑定与全自动 Run 冻结也会显式拒绝。只有 staff 的 `GET /api/v1/assets/workflow-smoke-candidates/` 会继续返回并实时复核这类素材。此规则不修改既有 Shot、Run 或任务审计事实。

以下是当前执行实例代码合同，不代表真实服务已升级。素材页面、辅助字段准备、统一准入及原worker已接入；剧本生成/结构化、ProductionRunPlan、人工与配方生产主链已做隔离验证。最终开发回归及实际维护切换状态见统一计划第0.1节与最新记录，真实模型验收由用户执行。

P4素材类别变更/删除保护（25.100）：读取冻结实例声明的逻辑媒体和Preparation来源摘要，覆盖未结束准备、生成与实例烟测；取消父Run不等于子Task或远端已终结。命中引用或来源无法核对返回400，保留素材和文件；不扫描旧任务/提示词JSON寻找任意UUID。`resolved_inputs`读取与准备写入共同使用可配置的`security.snapshot.stage_max_bytes`，不叠加固定64KiB／256KiB上限；合法的大准备不阻止无关素材改类或删除，实际引用、超限或摘要损坏仍阻断。沿用准备创建的引用写锁与正式Asset事务。未引用、无其他保护的本地上传仍可删除，文件在事务提交后删除；全局素材、正式Bible/发行证据和跨租户保护不变。这是源码合同及隔离测试覆盖说明；后续已记录服务切换，不能替代真实专项验收。

`options`改为标准分页`count/next/previous/results`，查询必填`job_type=asset|prompt`与`media_type=image|video|audio|script`，可传page/page_size。只列启用实例的已发布精确版本，按冻结输出合同过滤并沿原Workflow模板权限过滤；字段为execution_instance_version_id、key/name、version、operation/mode、prompt_mode、definition_sha256。它不读取Smoke样本作为默认值，不返回独立Deployment/Workflow选择或can_submit。列表不带完整定义；选中后按精确ID读取input-contract。此只读详情对登录用户开放其可选已发布版本，草稿/退役/停用/无权模板404；管理写入仍只允许管理员。

`POST /asset-generation-jobs/`必须提供`Idempotency-Key`（1–191字符）；body必填execution_instance_version_id、expected_definition_sha256、job_type、media_type、inputs、parameters、manual_channels、subject_facts、source_language、target_language和field_values。后三类集合没有内容时明确传空对象/数组，语言不适用时明确空字符串。inputs按冻结逻辑输入键提供来源；parameters只能在实例合同内取值；manual_channels是完整人工通道，profile模式只能以field_values提供待审核字段（unit_key/field_key/text），不能混用。不接受独立model_deployment_id、workflow_template_version_id、execution_kind、workflow_inputs、Binding、parent_preparation、scope或批准信息。

素材业务字段仍为style_id（可空）、name、description、image_category；素材生成必须有名称，图片需明确类别，分类图片需基础描述。外部授权为allow_external_calls（布尔，默认false）；业务预算由当前系统资源策略管理，超额保留准备等待人工确认，不接受单次budget_limit。辅助field/text任务不能从此公开入口创建。真实输出类型由执行实例合同决定，不从策略用途或媒体名称猜执行能力。

M4-01共用目标准备基础：图片/视频/音频Job与配方媒体阶段通过同一目标准备服务，将`media_generation_target`保存在原Preparation解析详情及摘要中。目标记录`source_mode=generate`、实际媒体类型、确切实例/定义SHA、原候选策略、原解析输入SHA和可空设定引用；不是模型成功、图片已采用或主图已作为输入的证据。独立入口保持原单次请求（候选数1、evaluator为空），不暗建ProductionRun；配方保留原冻结候选数/evaluator，追加候选及重试仍使用各自原请求编号。输入或目标核对失败沿原事务回滚，重放读取原准备，不重算来源/种子。

原准备服务接受严格的`appearance_target`声明：主图/变体角色、可空确切设定修订、变体必填的确切主图素材版本及其原输入`input_key/item_key`、下述原差异来源`difference_input`。它进入原幂等意图和解析目标；所选设定复用原正文校验器，变体主图复用受控Asset文件读取器，并与原解析图片输入的Asset及实际字节摘要匹配，不按父系列、名称或最新版猜测；提供变体设定时，主图素材版本必须记录它固定的同一主图设定修订。配方主图单元只接受与原写回基线一致的主图声明；变体使用其自身外观单元，不能覆盖主图。独立请求声明缺失不推断角色，设定省略保持null；配方主图的已声明阶段职责按下段接入，重放均不读取当前选择。公开API接受下述主图及变体声明并复用同一准备校验；目标输入能力按下述M4-02同源目录预检；独立与配方登记及已有素材履约的接通范围见下文，仍不等于M4整体验收完成，不将创建请求通过冒充真实生成完成。上传和直接关联仍使用前述原Asset/M9入口。

变体声明的实际输入核对：选中的`input_key/item_key`除Asset与字节一致外，还必须通过原`read_execution_workflow_uses`核对冻结定义、逐使用来源摘要及完整`workflow_slots`分配，并确实到达图片端口。仅上下文使用失败；同一Asset在另一个输入位置有端口也不替代本次声明，不能按第一张图或首端口猜测。重放/详情只验证原冻结事实，不重新选择实例/设定或探测文件；大小限制沿原配置边界读取，不复制端口分配器。该校验不是模型已收到图片的运行证据，也未完成完整用途/Projector预检及结果登记链。

变体差异合同（M4-01）：声明必填`difference_input`，普通来源为`{kind:manual_channel|logical_input,key}`；结构化参考字段为`{kind:reference_field,key,unit_key,field_key}`；结构化直通字段为`{kind:structured_field,key,field_key}`。仅引用原请求/解析输入，不接受另存text/body、自由selector/path、缺省来源或结构字符串化。人工通道须由当前manual实例声明且在原请求实际提供，经原通道规范化；logical_input须是当前profile实例的text输入，经原投影映射到assembly_source/generation_source，不能仅作为上下文。reference_field须映射到确切字段reference_units，按原reference_context及field_requests核对单元、策略、正文贡献规则与source_text一致；当生成来源为request.structured_input时，按冻结规则的确切source_path读取原单元structured_input中的实际文字，不改读渲染字段、换单元或用结构默认值补齐。structured_field须是json输入中唯一映射到策略assembly_source的字段，通过策略已发布source_path及原read_profile_path读取实际文字；调用方不能重选路径，原结构解析的默认补值、缺失、非文字及空值不得冒充差异。所有策略来源均与冻结profile_sources投影一致；冻结`appearance.difference`只保存声明来源键及`value_sha256`（原非空文字的JSON合同SHA），不复制正文或选择辅助结果。创建、重放及详情读者验证同一原意图，不查询当前业务来源补齐。图片声明式参考准备使用原已核对的素材角色/端口，明确mode为空且conditioning_mode为null，不伪造视频模式；视频的确切模式和时钟校验保持。direct策略的媒体限制不放宽。公开变体创建复用该合同；动态配方目标作者、履约、结果登记及阶段内选择均复用下述原服务；完整模块交付另列。当前独立上下文目录不提供参考单元结构，参考单元只从本次literal输入选择，不新增无实际来源的目录或猜造单元。主图四键合同不变，不新增HTTP端点、数据库Schema、队列或生产门禁。 当本次变体明确以`reference_field`作为差异时，共用字段选择快照要求该确切单元/字段有已确认并采用的贡献，即使可复用规则自身为可选；原缺字段/待采用评估及编译入口共用此事实，不能省略差异再提交。其他单元和未被用作差异的可选字段不变，不改写已发布规则或旧冻结正文。

原输入位置和差异来源的标识逐字匹配：`input_key/item_key/key/field_key/unit_key`在API反序列化时不裁剪首尾空白；原合同的非空、长度、存在性及唯一性校验仍有效。例如原参考输入中的`"second"`与`" second "`不是同一单元，不能把后者改成前者后创建或重放。同键更换确切来源返回409；不存在的确切来源返回400且不产生新任务。

配方主图接线：首次`prepare_recipe_media_unit`在原所有者事务内先冻结上述阶段输入（已有快照只读核对），按原需求选择实体唯一主图外观，使用`appearance:<id>`作为原Preparation/选路事件的单元键。候选与路线仍由原`request_key`区分，正式追加动作按同一外观读取已用候选号和种子，不再查询旧实体键；scope继续保存真实实体归属。服务端`appearance_target_source=bible_primary`仍进入原意图，共用目标服务投影同一准备的`bible_target.primary_setting`为主图`appearance`。首次解析须将当前有效的M3需求与冻结阶段需求、确切设定一起核对；缺审核/需求或来源变化失败回滚，首次冻结、候选和选路事件一同回滚。原`resolved_inputs.recipe_material_requirement`保存`schema_version=1`、`run_plan_sha256`、原`bible_review`及选中`target`，随解析输入SHA进入原意图，原详情API只读返回，不存第二份锚点。原结构化映射的`unit_key/unit_map`和Profile统一使用根准备传入的实际单元键，不根据scope重建实体键。原阶段及主/辅助调用准入仍核对当前需求有效；历史详情和幂等重放不重读今日M3、不重冻阶段，重放成功不代表准许再次派发。独立可空设定与普通镜头路径不变；本段无新增请求字段、配置项或页面，准备本身不生成素材版本/采用决定；混合生成调度与已有素材明确履约见下文，完整M4验收未完成。

配方变体准备：内部原prepare_recipe_media_unit可接收明确appearance_target，必须匹配原M3审核回执中的确切变体设定；候选键仍为appearance:<id>＋原路线/候选号，按该单元角色读取variant_routing和selected_variant_route_index，不继承主图配置或共用兄弟变体身份。确切主图素材版本须对应变体固定的主图设定修订，图片Asset/字节/原输入位置须通过共用实际端口校验；实际差异来源的文字SHA须等于该已审核变体的appearance_anchor，不能只挂设定而传其他正文。变体保留production_unit/原运行时限，不捕获主图bible_target写回基线。首次解析冻结同一recipe_material_requirement，旧准备重放不重读当前M3或主图文件，改变主图版本/声明不能复用旧请求键。本轮来源绑定已接混合阶段和结果登记（见下文）。直接手填appearance_target缺少recipe_material_binding时仍禁止派发；主/辅助调用前须核对当前同轮主图选择与冻结回执一致，变更后明确拒绝旧准备，不重写请求。已有/上传素材履约已通过M4-03隔离验收。沿原配方配置页面/API维护输入来源，没有新HTTP端点或公开配方目标提交动作，不能将内部准备通过当成整模块可试用。

配方阶段内素材选择：原主图/变体生成阶段按单元候选完成情况和该角色已配置evaluator调用共用选择服务，写原ArtifactSelectionDecision；GET运行详情的原artifact_selection_decisions可见automatic且selected_by为空，不能据此认定人工审核或Bible已采用。按本次候选号选择通过实际文件/来源校验的成功候选，已有有效选择不因迟到结果自动替换；未配置evaluator不自动选。公开人工选择复用原select-artifact，先读下述影响上下文再提交，记录真人manual；确切变体依赖只接受同轮/实体/固定主图设定的原选择，不默认用库图。当前消费核对M3、路线/唯一主调用和原selected状态，失败不换图、不补造决定。历史读取给出确切决定ID，不应用今日选择/审核改写历史，也不授权新调用。普通无依赖选择不创建Asset/系列版本；存在变体依赖的明确主图改选在同一事务内登记新主图并处理依赖。两者均不写Bible或派发调用，原运行扫描按已有授权接续执行。公开影响确认及已生成/未调用混合处理见下段；辅助准备已有但主调用尚未完成时，按下段真实调用/占用状态处理，不能把自动选择记为人审。

M4-06重新准备事务（公开确认沿原入口，验收状态见详细设计）：`change_recipe_primary_and_reprepare`复用原候选选择、Asset登记和准备分配器，在原文件保护器/registry/Run事务中明确改选主图并替代未执行变体。0225仅为原ExecutionPreparation增加不可变supersedes一对一关系；旧合同/种子/请求保留，新准备通过确切替代链读取，不按时间取最新。按原路线及显式替代链投影当前准备：reprepare要求主调用尚未创建且无worker占用；辅助准备可尚未派发，辅助调用可为明确终态，或严格核验为未领取、未开始提交、无远端标识/实际费用的排队任务；retain须有唯一当前成功调用和真实有效产物，保留旧主图依据/请求/结果，不再调用；其余blocked并展示原原因，不能取消或重发在途/未知调用。GET与POST复用同一影响读者，摘要覆盖当前ID、准备修订、状态和动作，以及全部辅助准备/调用（含旧重试）的确切状态和提交/占用事实，确认时在原Run锁内重查；清单、动作、修订或选择基线变化拒绝。任一登记/准备失败整单回滚，本动作不派发模型。辅助未派发准备停止，确定未提交排队调用经原取消/预留释放函数停止；明确终态的辅助准备、请求、原始响应、字段与费用原样保留，新准备不移植旧字段结果。辅助在途、结果未知或归属不一致仍拒绝；历史替代链也逐一复核所有旧辅助，不能只看最新一次成功。 辅助字段准备沿同一个父来源校验器确定真实主准备，再检查父外观单元的生成来源/履约互斥，不把field批次当外观需求。失败后成图接管只排除经完整替代链核验、从未发起主调用且辅助已安全结束的旧准备，其他取消、未知或缺失调用仍不能跳过。

阶段内选择API（2026-10-02）：`GET /api/v1/production-runs/{id}/material-selection-context/?artifact_id=<UUID>`按单一候选、原Run/Stage锁只读核对，返回artifact_id、stage_run_id、unit_key、appearance_role、已审核reference_name、candidate_number、expected_decision_id（未选择为null）、selected_artifact_id、can_select/reason、dependencies_sha256及dependent_preparations（id/unit_key/reference_name/status/revision/action/reason，action为reprepare/retain/blocked）。Cache-Control为private, no-store，不创建/推进任务。`POST /api/v1/production-runs/{id}/select-artifact/`对于配方素材候选必填`material_selection:{expected_decision_id:<UUID|null>}`，原确认页面同时原样回传GET的`expected_dependencies_sha256`；其他产物仍只传artifact_id，不能附带不适用基线。原选择writer在相同所有者锁内复核，当前同一候选重放不追加决定；不同候选且基线过期409/material_selection_changed，有依赖而未确认影响摘要返回409/material_dependency_change_required；影响摘要过期返回409/material_dependencies_changed（即使ID相同但准备修订或动作变化也拒绝）；当前blocked依赖400，不能以确认绕过，未知字段/缺基线/停止阶段400，无权运行或外来候选404。角色、设定、单元从原准备与冻结需求读取，客户端不能填写正文、操作者或自动选择来源。界面入口为配方运行详情→媒体产物与选片→查看素材选择影响→确认选择此候选（有依赖则“确认改选及依赖处理”）；清单分别展示停止旧准备并创建新准备、保留原依据和不能处理项。错误/迟到响应不恢复旧可提交状态，历史版本不被修改。已被真实更高修订选择替代的旧登记回调不登记旧图、不误阻断运行，由原扫描接续当前选择；未知决定或损坏来源仍明确失败。

配方素材登记：`register_recipe_material_selection`要求确切选择决定ID、当前有效M3/路线/调用及运行中原阶段，沿原Run/实体/候选锁复用共用图片入库写者与原`asset_references`关系服务。首次登记创建原Asset及`source=generated`的主图或变体AssetReferenceVersion，指向同一GeneratedArtifact和确切设定；不创建上传事实、AssetGenerationJob、虚假人审或主图的generated_from。重复同一选择复用同一Asset/版本，人工明确改选形成同系列新版本并保留旧版本；来源陈旧、关联损坏或同实体外观存在多个素材系列均明确拒绝，不猜测或修补。版本可由原`GET /api/v1/asset-reference-versions/?series_id=…`读取。原混合主图/变体阶段已接入：配置的自动选择落库后，通过提交回调在独立登记事务中物化，提交前保持running/waiting_material_registration；原Asset和版本就绪才继续原执行器。回调丢失由既有运行中配方扫描恢复，不新增队列或状态表；重复唤醒不重复复制/调用。文件保护器覆盖登记事务提交，失败回滚新记录/文件并把原阶段及Run标为blocked/material_registration_failed；取消或后续决定已改变运行状态时不写入、不恢复运行。确切选择改变时不登记旧图；合法更高修订替代的回调不误阻断，原扫描接续当前选择，回调自身不擅自换决定。未知决定或损坏来源仍失败。变体依赖读者可返回同轮原选择对应的确切已登记版本，未选择/未登记则等待，坏关联明确报错；不会挑最新库图或补造关系。变体登记generated_from为原请求实际使用的确切主图版本，系列指向该主图系列；旧结果不按今日选择改绑。登记不等于Bible审核采用，无新增HTTP端点或Schema。

独立图片素材登记：原Job的REST/Comfy完成路径共用事务及新输出文件保护；明确声明主图/变体时，原output_asset与共用AssetReferenceVersion一起登记，source=generated、generation_job指向真实Job、generated_artifact为空。不补造Run/Artifact/上传或人审。可选设定核对原确切修订；变体generated_from固定实际使用的原主图版本，不查最新图。无声明保持普通Asset，无设定的独立任务各建系列，不按null/名字合并；有设定出现多个系列报错。原来源读者核对字节摘要、MIME、清单及冻结关系；重放返回原Asset/版本，不复制文件或修复坏来源。登记失败回滚本次数据库输出和新文件，沿原任务失败状态保存；取消或已完成任务不重新提交。原版本列表及零调用履约消费同一结果。没有新增HTTP合同、Schema或队列；实际媒体回执保留及精确任务链接已接通，读取与未保存边界见下述任务详情；M4整体验收仍未完成。

图片外观创建：素材库→图片→生成素材→选择确切实例→“生成外观角色”，明确选择主图或变体；默认不声明，不选择设定。原POST可选appearance_target，省略或null为无声明。主图严格四键：role=primary、setting_revision_id=确切UUID或null、primary_version_id=null、primary_input=null。变体为role=variant，主图版本UUID与原input_key/item_key必填，另必填上述difference_input；字段角色、额外正文/路径或不完整嵌套来源均400。仅job_type=asset/media_type=image可用，所有者、确切设定、实际主图文件/端口及差异仍由原准备验证器核对；失败事务回滚。通过原AssetReferenceBrowser选确切主图系列版本，再从原输入选择同一Asset所在项，不自动复制图片、提示词或创建系列。参考字段从本次literal原ordered_units选择确切unit_key；上下文来源尚无可选单元时显式阻止，不提供自由填写ID或猜测来源。未知提交深拷贝嵌套目标并保持同键同输入重试，更换声明复用键409。创建成功不代表已生成、登记或采用结果。

`appearance-options`需登录，必填role=primary|variant；可选search（最多160字符）、page/page_size正整数，不接受其他或重复参数（400）；标准分页count/next/previous/results，分页不存在404。只列当前用户拥有项目、指定角色的设定当前修订，staff不扩大权限；按项目、实体、名称或引用名搜索。返回setting_revision_id、appearance_id、project_id/project_title、entity_name、name/reference_name、revision，不读取锚点正文、文件或推断审核。目录更新不改已选确切旧修订；失败禁止从陈旧目录新选，允许重试和移除引用；切换角色清空旧设定/主图/差异，切换实例或用途经原确认后清空输入。任何目标更改撤销表单复核，未知提交锁定请求。没有新队列/表/ProductionRun；共用目标合同已完成；已冻结依赖后的主图改选/重新准备及配方已有素材目标交互仍未完成；混合生成登记见本节。

`GET /execution-preparations/{id}/`在原所有者权限与锁内读取详情；存在`resolved_inputs.media_generation_target`时复用同一目标验证器，核对已存定义/输入摘要及声明，不重新解析当前设定、选择主图或探测文件。合同异常沿原错误结构返回400；无权根对象404且不读取目标。缺少目标不补写、不推断角色，文本/辅助/Smoke详情仍沿原链读取。响应仍为原详情结构，没有新增目标详情端点或数据库副本。

素材任务和配方阶段复用的“准备、字段与执行”工作台增加“本次媒体生成目标”只读摘要：媒体类型、显式主图/变体角色、原候选数/选择策略、可空确切设定；变体展示原逻辑输入位置及人工通道/策略文字或结构化参考字段差异来源（单元与字段明确显示，原来源不冒充已选辅助结果），确切主图/实例/设定ID和差异文字摘要折叠显示，不复制正文。未声明角色和未关联设定分别说明，没有目标记录时不生成该卡片；格式异常显示错误，刷新失败沿原工作台标为上次内容。这里只读冻结准备，不代表模型成功、参考图已传入物理端口或人工采用；主图/变体创建表单按上述公开合同提供，执行效果与结果登记另行验收。

创建在同一原任务事务内写Job与唯一Preparation，解析受权来源、一次性种子、参数和精确策略；根Job默认preparing，缺人工字段可awaiting_input，已填写但未确认可awaiting_review。不会伪造正文、隐式生成字段或批准字段。首次202并返回Job的Location；同键同意图200返回原Job、Preparation及种子，同键异意图409。源素材/参数/初始字段错误400且二者一起回滚；权限403、目标不存在404。Job详情新增execution_instance_version/name/version及preparation摘要；实际输入、通道和最终请求从Preparation详情读取，不再展示原Job中重复的提示词/参数副本。

本地素材上传仍使用正式`POST /assets/`，保存后以Asset ID填写逻辑输入；不得提交本地路径、节点、checksum或上传名。每次使用的item_key、用途、主体、范围和配对关联按实例合同校验；物理槽位来自冻结Projector/Workflow。失败不回滚已经独立上传成功的素材，页面应分别显示上传和生成错误。

Job请求头只保存根意图SHA、精确版本/定义SHA、输出类型和运行预算；解析输入、字段选择、编译图、通道与最终请求属于Preparation唯一事实。只有完成字段采用及统一准入后才能freeze/dispatch；人工等待不消耗远端deadline。图片/视频/音频仍复用原编译器、受管来源读取、FFmpeg预处理、上传回执和输出验真；不创建第二套队列、虚拟项目或分镜。原worker只读此唯一冻结请求，不使用旧快照Schema恢复新任务。生成Asset的prompt_hint读取本次冻结的实际正向通道，不读取旧Job提示词或当前策略；没有该通道时保持空值。

`retry`不接收请求体，可提供最长191字符的`Idempotency-Key`；省略时同一原任务只分配一个稳定的默认重试意图。明确failed/cancelled且无claim或在途/uncertain子任务时才允许创建，首次202+Location、原键重放200、同键异来源或原Job已有任何重试后继时新键409。后继成功不允许从旧任务重复生成；后继失败或取消时须重新读取并明确重试该后继，不能绕过在途或未知孙代从祖先分叉。同键始终回到原后继，不寻找最新任务；省略键也不替代此前显式键。返回retry_of指向原Job，retries只读列出原关系中的全部直接后继ID与实际状态，不保存第二套可重试标志。列表使用窄字段预取；页面对已有后继显示确切查看入口，只有失败/取消叶节点提供准备重试，409复用统一错误摘要保留实际原因。沿用原始输入/已解析参数/seed，保留pending字段并精确引用原已采用且已确认的修订，不重建正文、不自动确认。新Job从preparing开始，通过当前准入后才可再次调用。改输入、改实例或再生成候选应使用新的collection POST，而非给retry附加参数。

`resolve-uncertain` 只接受 `uncertain` 任务并追加不可变管理员证据，结论为 `confirmed_not_submitted`、`confirmed_remote_failed`、`confirmed_remote_cancelled` 或 `confirmed_remote_succeeded`。该 action 也支持 `Idempotency-Key`；响应丢失后同键同请求返回既有 resolution，任务详情返回 `latest_resolution` 供刷新后追溯。媒体远端结论必须关联与既有事实一致的 `provider_request_id` 或服务端已冻结的摘要；确认未提交时任务不得已有 request id 摘要或 submitted 时间。改造后的ComfyUI请求使用Preparation UUID作为稳定submission token，恢复只查询该冻结token；远端missing不能证明未提交，仍保留uncertain等待staff。超长provider id不会被截断用于轮询，只保存SHA-256并进入人工核验。可提交实际费用与币种，省略时只沿用最近一轮经完整性核对的人工费用；后续collect金额或币种如与人工证据冲突会重新进入对账状态，绝不静默覆盖。

执行链费用与取消的当前合同：文本及ComfyUI完成、远端成功后产物校验失败、取消后的晚到结果、管理员确认远端成功，都不能将estimated_cost填入actual_cost。没有账单或人工费用凭证时actual_cost保持null，原预算聚合继续保留estimated_cost占用；token usage不是金额。受限REST collect的明确实际金额必须带自身有效币种（零金额也一样），不从估算币种推断、不截断或自动换算；已登记的有效人工金额不会被无账单结果清空。确认未提交时的零费用是明确的管理员结论，才可释放未消费占用。媒体取消尝试在原Job中先落库再发送，worker在边界崩溃后只轮询原request，不重复cancel；有取消尝试不代表远端已经取消，仍按原超时和消歧处理。

文本模型部署的请求超时 `timeout_seconds` 必须为 JSON 有限正数，可使用小数秒；布尔值、数字字符串、缺失值或超出当前配置边界均明确拒绝。精确部署覆盖优先于服务连接值，二者均未声明时不提供代码默认值。DeepSeek、Local Qwen 和通用 OpenAI-compatible 文本接口共用此校验；请求超时不是 Smoke 证据有效期，也不是工作流节点静默等待时限。

任务详情公开 `submission_started_at`、`cancel_requested_at`、`cancel_submitted_at`、`ignore_late_result` 和 provider request id 摘要，不公开原始远端 id。取消意图一经记录不会因人工消歧丢失：远端晚到成功只恢复原 request 的费用 collect，产物不下载入库；文本响应或仅有 request id 摘要的结果无法安全恢复时明确关闭后，才允许用户创建新的显式意图。

异步 API 产物只有在受限下载和媒体校验后才物化；ComfyUI 产物只有从受控 `COMFYUI_OUTPUT_DIR` 中冻结输出前缀下取得唯一、类型匹配的持久 Save 主输出并完成大小、checksum 与媒体探测后才物化。Asset 详情中的 `preview_url` 与 `download_url` 是短期签名地址；客户端下载前应重新读取详情，`download_url` 以附件响应返回受管文件。只有远端 `reference_url` 而没有受管文件的条目不提供下载链接。人物/场景/道具图片以及参考视频、参考音频可由现有短剧工作台选择；剧本素材必须通过 `POST /api/v1/script-sources/` 的 `asset_id` 输入复制为目标项目的 preview revision，并继续走人工确认门，不能覆盖或绕过已确认剧本。

独立素材生成只写素材库和自身审计状态，不创建 Episode、Shot、ProductionRun、装配或交付事实；因此它不是短剧生产旁路。

## ProductionRun：短剧生产唯一入口

`GET /api/v1/production-runs/` 读取分页运行摘要，不预取各阶段、审核或产物集合；`GET /api/v1/production-runs/{id}/` 批量读取阶段审核、选片决定、路由事件及产物。响应字段、权限、冻结计划校验和脱敏后摘要口径不变。相同安全边界解析只在单次读取响应内复用；请求结束即释放，后续请求重新解析当前配置，不缓存整个运行结果或跨请求沿用旧安全限制。 详情读取中冻结合同缺字段、内容或摘要不符沿标准400错误信封返回原错误代码与字段原因（例如run_plan_source_conflict），不抛无说明500，不重建或转换旧运行计划；无权对象仍404，不返回部分成功的执行计划。

### 配方创建的项目来源确认

沿原 `POST /api/v1/production-runs/` 登录用户与项目权限边界。`plan_source:recipe` 可提交 `project_source_confirmations` 列表，省略等同未确认任何项目来源；人工制作不接收此字段。每项仅为 `{"execution_instance_version_id":"<UUID>","input_key":"project.logline","reference":"<原预览凭据>"}`，不接收正文、来源路径或审批标记。凭据来自同一精确版本 `input-suggestion/` 预览；页面用户点击确认后提交。配方规则仅为 `{kind:"project_snapshot"}`，不保存项目身份或凭据。

首次创建须覆盖全部可能路线已经配置的项目快照输入，按精确实例＋输入唯一；同一输入多次使用只提交一项，未配置的可选输入无需凭据。后端核对冻结合同SHA、预览签名、原用户及当前项目／来源访问权；缺失、多余、重复、篡改、跨项目或合同不一致返回400并不创建Run/Plan/Stage。正常创建201；原幂等请求重放200；同键不同确认凭据返回409，不能借重放改变本轮输入。原请求回执保存确认列表并绑定RunPlan；后续文字／媒体准备通过共享读者保存原正文及来源，不读取后来项目编辑替换。创建请求确认不是外部模型调用授权、字段审核或候选采用。对应页面为项目工作台创建本轮与新建连续剧续建步骤；新项目先落库再预览，不为预览创建模型任务。隔离回归覆盖原创建／准备、跨范围、篡改、丢失与重放；当前环境真实配方闭环验收状态以优化台账为准。

### 无配方制作

创建必须显式提供 `plan_source=recipe|manual`。manual 创建轮次时不选配方或模型资源；在具体生成动作中只选择精确执行实例。recipe 必须引用精确 schema3 版本，不接受独立 workflow_selection、提示词Binding或顶层 reference_inputs 字段。无配方创建、剧本采用、文本/媒体准备、候选审核、原装配交付与完整schema3执行沿同一Run/Stage链，已做隔离串联；实际上线与用户验收状态另行记录，不由接口存在推断完成。

| 接口与用途 | 请求 | 响应与副作用 |
| --- | --- | --- |
| `GET /api/v1/production-runs/manual-options/` 创建目录 | Query 必须且只能提供 `project_id:UUID` | `project_id`、有 schema/字节数/SHA 的 `stage_catalog`、`scope_kinds/delivery_kinds/initial_content_kinds`、实际已有内容计数；只读、不选择实例 |
| `POST /api/v1/production-runs/` 无配方轮次 | `project_id`、`plan_source:manual`、`idempotency_key`、`manual_plan`，明确外发授权/配置覆盖（资源阈值统一由系统管理） | 首次201，同请求重放200；Run、RunPlan、StageRun 同事务创建，`awaiting_input`，不创建隐藏 Recipe、不调用模型 |
| `GET /api/v1/production-runs/{id}/manual-action-options/` 动作与采用状态 | Path 为当前人工 Run UUID | `run_id/run_plan_sha256`、已接入 `actions[{stage_key,action,operation,output_kinds,scope_kinds}]`；文本审核动作另含服务端 `source_stage_keys`。`script_adoption`、`script_review` 分别返回该步骤的 `{revision,decision_id,content}`，`current_script` 是本轮当前明确采用内容，`current_script_decision_id` 指向其原采用决定（未采用时两者均null）；`visual_rules_review`另含`status=not_reviewed/current/changed`及`reasons`，表示原审核依据与当前事实是否一致；读取共用Run锁，不按更新时间猜测，不修改审核记录 |
| `GET /api/v1/production-runs/{id}/manual-change-impact/` 改选影响（只读） | 必填 `stage_key` 来自动作选项的 `content_change_stage_keys`；可选 `offset` 默认0、`limit` 默认20且1–50 | 当前源步骤/决定、查询时间、本轮已开始后续步骤、分页准备及引用/调用计数；不是更换确认或重做命令；无权404，参数/源记录不一致400，`private,no-store` |
| `GET /api/v1/production-runs/{id}/manual-script-structure/` 已采用剧本结构检查（只读） | 本轮人工Run，不接受查询参数；制作台“检查已采用剧本结构（不调用模型）”按需读取，关闭轮次也可读 | 返回`run_id/current_script/current_script_decision_id/status/reason_code/reason/episode_count/shot_count`。定位当前原采用决定，复用`read_manual_script_structure`核对确切确认修订、正文及已冻结`structure_source`，从原详情的语义投影统计声明，不重解析原文或读取模型请求文件。`no_script`不猜母稿；`structured`返回非负集／镜头数，0只表示无对应声明，不代表已创建分镜；`unstructured`仅限采用来源明确为null且原文/确认事实完好，原因码`script_structure_required`、数量null，不要求一定调用模型。已有详情缺失/损坏、来源或采用关系漂移、正文不符均400，不伪装无结构；无权404、未认证401/403。`private,no-store`，无模型调用、候选、分镜、采用或状态写入；不等于Bible审核、范围／时长或业务质量通过。普通轮询不读大详情，采用依据变化后旧结果不再适用，不持久化第二套状态。 |
| `GET /api/v1/production-runs/{id}/targets/` 制作对象目录 | 必填 `kind=project|episode|shot|character|scene|prop`，可选 `page/page_size` | `run_id/run_plan_sha256/count/next/previous/results`；每项 `scope{kind,id}/unit_key/label`。只列本轮项目及冻结剧集范围，角色/场景/道具在选集范围须关联其镜头；稳定分页、不携带内容正文。复用owner/staff访问控制与全局分页配置。未知/重复参数400、无权对象404、空结果为0，`private,no-store` |
| `GET /api/v1/production-runs/{id}/manual-media-review/` 完整选片审核视图 | 本轮人工Run；不接受分页/筛选参数 | `run_id/run_plan_sha256/selection/selection_sha256/approval`；selection v4 包含 items、delivery_timeline（含可空本集起止时间）及content_basis；approval.changes解释变化类别。整个合同有大小上限。只读元数据、不探测全部文件，`private,no-store`；无权404，不一致400 |
| `GET /api/v1/production-runs/{id}/manual-assembly/` 装配核对与任务 | 本轮人工Run；不接受筛选/分页 | `run_id/run_plan_sha256/contract_sha256/frozen/blockers/approval/episodes/output/authors/author_contracts/author_schemas`；每集完整逐镜来源、原job状态/尝试数/错误；输出配置不含服务端字体路径。只读元数据、不执行媒体探测或入队，`private,no-store`；无权404、缺失或不一致400 |
| `POST /api/v1/production-runs/{id}/manual-assembly/` 预览装配作者 | `expected_run_plan_sha256`、`authors`（空对象或assembly_output/assembly_timing/assembly_media）；每个作者含确切`output_version_id/expected_contract_sha256/source_text/mapping` | 返回同上完整核对合同及摘要；复用已发布通用JSON输出/M1结构映射，几何只覆盖宽高FPS，时序及片段身份须与已审核完整集镜逐项相等。仅计算预览、不保存、不探测、不创建Job或命令；`private,no-store`。无权404、计划过期或修改冻结作者409、缺字段/非法原文/版本退役/范围时序或片段不符400；失败保留页面草稿，重新核对后才能提交 |
| `GET /api/v1/production-runs/{id}/manual-delivery/` 成片确认清单 | 本轮人工Run；无Query | `run_id/run_plan_sha256/contract/contract_sha256/blockers/bundle`；schema1清单含装配合同SHA、选片审核修订、每集准确job/Artifact及摘要。只读数据库，既有bundle摘要不是实时文件核验；`private,no-store`，无权404、异常400 |
| `GET /api/v1/production-runs/{id}/bible-review/` 完整Bible审核 | 本轮拥有者；手动/配方共用，不接受查询参数 | `plan_source/run_id/run_plan_sha256/project_bible/project_bible_sha256/candidates/settings`；manual另含当前剧本、来源和审核Decision状态，recipe另含source_snapshot_sha256（无声明的manual阶段为null）。settings含原声明、当前设定全文和固定主图正文/审核证据；只读，`private,no-store`；无权404、不一致400/409 |
| `GET /api/v1/production-runs/{id}/manual-bible-image-review/` 多图与唯一主图审核 | 本轮人工Run；不接受分页/筛选参数 | 真实准备候选目录及摘要、实体内容/现有主图和参考图及摘要、服务端本次选择数量上限、独立审核修订/状态；读取不采用、不创建模型任务；`private,no-store`；无权404、合同不一致400 |
| `GET /api/v1/production-runs/{id}/manual-storyboard/` 分镜内容与来源采用视图 | 本轮人工Run；完整冻结选集，不接受筛选 | `content/content_sha256/adoption/materialization`及Run/计划标识；原Episode/Shot创作内容及从已采用剧本拆稿的条件、参数、摘要；读取不物化、不推断执行能力；`private,no-store`；无权404、缺失或不一致400 |
| `GET /api/v1/production-runs/{id}/manual-storyboard-review/` 逐镜参考审核 | 本轮人工Run；完整冻结范围，不接受筛选 | `ready/reason/contract/contract_sha256/references/approval`；读取同份已采用镜头、原素材及上游审核，引用按镜头派生，不分配实例槽位；未就绪明确原因，无权404、不一致400，`private,no-store` |
| `POST /api/v1/production-runs/{id}/manual-actions/` 明确人工动作 | 闭合合同见下文 | `script_input/adopt`、`script_review/approve`、`media_generation/adopt`、`artifact_selection/approve` 首次201；已登记文本或媒体步骤的 `generate/retry` 首次202；同请求重放200。回执 `id/action/created_at/result_kind/target_id` 只引用正式决策或准备，不复制任务状态或正文 |

`manual_plan` 必须含五项：`stage_catalog_key`、`expected_stage_catalog_sha256`、`scope`、`delivery`、`initial_content`。scope 恰为 `{kind:project}` 或 `{kind:episodes,episode_ids:[UUID,...]}`；剧集数组非空、不重复且属于项目，空数组不表示全部。delivery 恰为 `{kind:episode_masters}`（原逐集交付协议）。initial_content 显式 null 表示空白开始，或 `{kind:script_revision,id:UUID,expected_sha256:SHA256}` 引用本项目真实已确认修订；创建只冻结引用，不自动采用/批准。RunPlan 上限64KiB；原 Run.config_snapshot 继续沿已有 `security.snapshot.stage_max_bytes`，不提高限制或裁剪原配置事实。

人工失败主调用恢复使用同一 `manual-actions` 的 `action=retry`，仅原冻结阶段目录声明该动作的生成步骤开放；现有RunPlan不补写新动作。原步骤、unit、scope和计划摘要必须匹配准备中记录的请求，不能从unit_key推测对象范围。`payload`恰含`preparation_id`、`expected_preparation_revision`、`expected_attempt_id`，不接收新输入或`expected_selection_revision`。核验明确失败、已冻结请求及唯一当前主调用末端后，沿原准备与原共同dispatch创建retry_of关联；不重跑成功辅助字段、不暗建配方、不重编译原提示词或覆盖旧Attempt/产物。取消/成功的轮次不开放，失败轮次可沿此明确动作恢复；采用及装配限制继续核对。

重试回执`result_kind=preparation`、`target_id`和首次202的Location均指向原准备；同键重放200，已有后继调用时不同键也不重复分配。202仅表示命令已记录，不保证已排队、已调用或成功；当前资源校验阻断时读取原准备的evaluation原因，准备的dispatched仍是此前派发事实。网络回执未知时页面保留同一请求与幂等键供核对，不能自动另建请求；确切拒绝与回执核对失败分别显示。入口为无配方制作台的原准备工作台“重试此失败主调用”，用户确认本次请求后提交，不推进下一阶段。主调用即使`status=failed`，但`error_code=uncertain_remote_response`或`actual_cost_reconciliation_required`时仍不可新建普通重试；页面显示“主调用状态待核对”，不展示新重试确认或按钮。同一调用的错误码变化会撤销旧确认；已经提交但回执未核实的命令仍只沿原请求和幂等键核对，不借此重发未知模型调用。

父页轮询或重新读取替换同一运行/准备对象时，按实际 ID 判断身份，不清除在途命令及待核对的原键；仅切换到不同运行/准备或卸载时使原异步回执失效。该保护限当前页面会话，不承诺整页重新加载后自动找回未核实命令。

`manual-action-options` 的 `scope_kinds` 是当前动作允许的输出对象类型：`bible_image_generation/generate` 使用 `character/scene/prop`，`media_generation` 使用 `project/episode/shot`。这些对象身份不自动成为输入素材、提示词主体或隐含上下文；输入仍按实例合同显式选择。实体图片准备在同一 `resolved_inputs.bible_target` 保存schema_version=2：冻结名称、描述、确切主图设定引用及原主图/参考素材ID，供后续审核比较，不复制锚点正文、不额外建立写回事实。`primary_setting`为`{appearance_id,setting_revision_id,revision,content_sha256}`；未建立主图设定时为null，已有主图但修订损坏或归属错误明确失败，不借旧值或最新修订补齐。该目标不是模型输入，实际所需文字仍按实例输入合同显式提供。创建202/重放200均不生成、采用或绑定参考图。

手动媒体生成的镜头来源：在“生成准备”选择镜头及实例后，可按需“读取本镜已审核用途”，从原 `manual-storyboard-review` 的当前审核目录显式选择每个输入槽的用途。目录不代替素材需求履约或生成候选选择，不确认、采用或调用模型。素材库/上传仍保留在允许 `request` 范围的输入槽；镜头用途只用于允许 `shot` 范围、媒体类型和主体类别匹配的输入槽。

当素材来源策略锁定 `asset_upload` 时，允许 `shot` 范围的槽仍显示“本镜已审核用途”；请求保留原上传来源选择封装，服务端照常核对正式 Asset 上传事实，不改用 `asset_library`、不把生成/已有来源标记为上传。shot-only 槽不开放绕过镜头审核的独立上传入口。

“使用配对视频的原音”通过 `kind=video_audio` 与显式 `paired_to` 引用父视频使用，`scope_ref` 继承该父项：已审核镜头视频为确切 shot，其余请求级视频为 request。音频槽必须允许继承范围，父项必须在声明的视频输入中且唯一；独立音频仍核对自身与父项范围一致。输入槽排序或视频重排不按数组位置改配对，不给原音编造独立 Asset、审核用途或主体；实际音轨及目标端口能力仍由原服务端合同校验。

选择镜头用途时，`generate.payload.expected_storyboard_review` 必须恰含原 `decision_id` 与 `snapshot_sha256`；未选择此来源时不能附带该字段。媒体项仍为 `kind=asset`、确切 `asset_id`、本镜 `scope_ref` 及 `item_key=reviewed:<SHA256>`。摘要取 UTF-8 紧凑 JSON，键按 `input_key/reference_key/shot_id` 排序；`reference_key` 沿原逐镜审核目录。同一文件的不同用途标识不会合并。客户端不传入版本/设定快照，服务端在原 Run 事务内校验当前审核、镜头范围及既有共用解析器的原用途来源，冻结确切版本与设定。创作描述仅展示原值，不能猜测或代填接口用途；无额外用途合同仍传 null。

审核未就绪、过期、伪造、跨镜头或范围不匹配均 400，权限继续由原 Run 可见范围控制；失败不创建准备或请求。目录读取失败撤销旧目录，页面保留填写并阻止旧来源提交，不降级到直接 Asset。切换轮次/计划/步骤/镜头后旧异步回执无效。原幂等键重放只返回原冻结准备，不追随后来修改的用途，也不重发调用。开发证据为 `test_manual_generation_shot_sources.py`、`manualGenerationShotSources.test.ts` 与真实组件隔离页；未据此宣称真实模型或现用版本已验证。

当前剧本采用依据：原ProductionRun.script_adoption_decision指向初始输入采用或生成候选审核的原ApprovalDecision；两种正式写入都在原Run事务内同时保存当前剧本与决定。动作选项返回current_script_decision_id，页面显示对应来源。读取必须匹配本轮合法步骤的当前决定、快照与已确认剧本；缺失、跨轮次/步骤、旧决定或内容不一致均报错，不按步骤优先级/时间猜测，也不回填历史。决定ID由后端写入，客户端不能直接覆盖；旧命令重放不切回旧剧本。该依据修正不放开已有下游结果的改选。

动作选项另返回只读 `current_script_identity`（未采用为null）：原稿文件名 `source_name`、来源类型/格式说明、精确 `decision_stage_key`，以及生成审核采用时的 `candidate{artifact_id,variant_index,stage_id,stage_key,attempt_id,preparation_id,edited_from_revision_id}`。

原文采用沿已确认修订的原Attempt核验；人工编辑采用沿原审核快照的edited_from_revision_id核验原模型修订与候选来源，该字段为null表示原文采用。两者均核对本轮步骤，不按正文指纹或名称匹配。已确认原稿改选时candidate为null，不虚构生成候选。原稿无名称显示“未命名原稿”，不以项目标题替代。此摘要不进入current_script或expected_current_script，不改变冻结请求及幂等前置条件，也不新增采用事实。

人工剧本区区分本轮当前内容与此前输入改选记录；候选、原稿、修订、采用决定ID及正文SHA分别标注。已有剧本时“更换本轮采用的剧本”默认收起，审核候选并采用后不要求重复选择同一修订。原候选详情允许available与selected；selected只读展示及查看原调用，不开放再次审核采用，也不意味着本轮仍在使用该候选。改选继续沿原正式动作及下游影响检查。

人工Run成功、失败或取消后，仍从原`manual-action-options`读取已注册步骤，从原`generated-artifacts`列表／详情查看available或selected文本候选；步骤目录不授予写入权限。制作台保留剧本／影视规则候选只读入口，核对原Run／Stage／Attempt／摘要和owner/staff范围，不另建历史副本。弹窗打开后收到结束状态也立即关闭编辑、确认及重试；原未确认请求需核对历史，不能由查看触发重提。已结束轮次的新审核命令400；原幂等回执仍可按既有合同核对，不更改实际成功／失败事实。影视规则只读字段标为模型候选原文，不借当前项目值或未保存编辑伪装当时最终采用值。拒绝、无效和缺失候选的读取约束不变。

动作选项不再复用详情页的全量Artifact预取；身份摘要按原决定精确窄查，不装载候选正文。审核弹窗保留明确打开时的Run/Stage/采用依据，后台轮询不会卸载其内容、丢失选择或把新依据自动塞进未提交请求；明确重新读取仍走原接口，提交仍检查原修订及当前内容，不能借刷新自动采用。

改选影响入口：本轮人工制作台 → “查看改选影响” → 选择内容步骤。后端 `production_manual_change_impact` 从原 `StageRun/ApprovalDecision/ExecutionPreparationContract/GenerationAttempt/ProductionManualCommand/GeneratedArtifact/ProductionAssemblyLink` 读取；不创建影响状态表、不请求模型、不读取文件正文、不写入或重排任务。登录态及owner/staff范围与原Run详情一致，不新增权限旁路。

例如 `GET /api/v1/production-runs/{id}/manual-change-impact/?stage_key=script_input&offset=0&limit=20`：响应 `source` 含 `stage_run_id/stage_key/decision_id/revision/script_revision_id`（尚未采用为修订0、决定和剧本null），`stages` 是本轮顺序之后已有任务、准备、产物或决定的步骤，**不是全部被证实失效的步骤**。`count/offset/limit/next_offset/results` 仅分页执行准备，稳定按阶段顺序、准备创建时间及ID排序，`next_offset=null` 表示末页；`stages[].preparation_count` 合计等于count。无后续工作返回空数组和0；有人工决定但无准备时stages非空、count仍为0。

`replacement.code=review_only` 表示后续已有工作全部属于可复核原决定的Bible、分镜、参考图及文本人工采用/审核，审核步骤自身无生成准备、调用或产物（可引用此前步骤的真实候选或人工库图）。复用各步骤原读取器验证决定、快照及原来源；文本审核额外核对原候选，不依据“succeeded”标签推断合法。可沿原内容动作明确更换，再依次核对；原下游审核保持原值，影视规则及媒体依据改变时显示changed，当前剧本只读Run明确采用决定。文本候选仍保留原生成来源，失配时必须创建新准备而非放宽审核。读取影响不是权限票据，提交时重新检查；新出现的下游活动、无法核对的原审核、已装配或已结束均阻断。分镜单独复核也使用这一状态，不保留旧的专用状态别名。

`replacement.code=review_and_stopped` 表示上述原人工采用/审核与已明确停止的生成准备同时存在：先核对原审核依据，再以同一停止/回执/调用终态规则检查其余步骤。只有其余准备均明确停止、原调用已确定结束且无主调用成功或产物时才允许更换；必须逐项重新采用/审核，再由用户主动创建新准备。原决定、冻结输入、任务和费用不改写，原生成命令重放仍返回旧准备，不自动续跑。仅停止准备不能代替远端结束；损坏审核或回执、新任务以及未接通复核的其他结果均阻断。读写共用原Run锁内判断；前端直接展示服务端原因，不根据阶段标签猜测放行。

`replacement.code=text_review_required` 表示后续包含可核对的原生文本生成结果：原候选、调用和动作回执完整，所有主/辅助调用确认结束，准备已成功交付或明确停止。可与上述人工审核、媒体结果和其他已停止准备共存，每组均单独核对，不遗漏未停止的额外准备。更换上游不重写原文本来源：剧本候选仍要求原本轮采用修订和母稿基线匹配；影视规则核对本次冻结输入、原请求及项目规则写回基线，不隐式依赖剧本或Bible；已复制的梗概和风格不会随项目编辑变化。失配候选返回原来源冲突，用户回到原生成步骤明确创建新准备、提交及审核新候选；刷新和旧命令重放不重新生成。媒体结果复用沿原参考图/选片的明确审核，不将文本成功当作新内容的通过证据。

`results` 每项含准备ID、步骤、`unit_key`、原状态/修订、`references[{input_path,kind}]` 和 `attempts[{role,status,count}]`。引用只读注册来源位置，支持结构原语的叶子、分镜/Bible关联审核、已采用文本或媒体，**不扫描字面值中的同名JSON字段**。`reference_state=recorded_reference` 表示已记录直接引用，`manual_review_required` 表示未找到直接引用而非不受影响（仍可能间接或人工引用），`unverifiable` 表示该准备来源缺失、损坏或未登记；逐项显示，不过滤问题行或退回当前数据。`adoption_baseline` 单独表示结果写回比较基线，不冒充提示词输入。主/辅助调用按原状态计数，不取最近成功代替其他记录；不返回提示词正文、凭据或完整请求。

`assembly_locked` 表示本轮已存在原装配关联；`run_read_only` 表示原Run终态。结束或封存后更换内容需新建人工制作轮次。`replacement={allowed,code,reason}`来自与写入口共用的判断；允许状态为no_dependents/stopped_unused/review_only/review_and_stopped/media_review_required/text_review_required，各自满足本节定义的条件。阻断状态为run_read_only/assembly_locked/dependent_results/active_preparations/inconsistent_records/unresolved_calls。停止准备不能代替原远端终态；未知结果、取消中、损坏回执或尚未接通复核协议的结果继续阻断。写入在原Run锁内重新核对，并校验原选择修订；旧页面结论不能越过新创建的准备或结果。更换只追加原采用决定，不修改旧准备/请求/费用，不自动重跑；原生成命令重放仍返回旧准备，新生成需明确的新幂等键。查询不是变更授权。页面按需查询，保留带原查询时间的上次成功结果并持续显示失败，不将旧响应覆盖另一轮或另一内容步骤。验证覆盖正式API及隔离组件；后续切换不等于所有页面负向场景已完成实操验收。

人工采用请求结构（标识与摘要须从正式 API 读取，以下仅说明字段）：

人工参考图入口：本轮制作台 → “选择参考图与唯一主图”。`bible_image_review/approve` 使用本项目scope/unit_key，修订取 `manual-bible-image-review.approval.revision`；payload恰为 `{expected_candidates_sha256,expected_targets_sha256,expected_content_basis_sha256,bindings,comment}`。每个binding沿原多图合同：`entity_type/entity_id/selected_artifact_ids/selected_library_assets[{asset_id,expected_sha256}]/anchor_artifact_id/anchor_library_asset_id`，两个主图字段恰有一个非空且属于已选集合。所有本轮实体都须提交；可以仅选库图，不要求生成Stage已完成。目录、上游依据、内容/关系或修订变化409；原文件校验、唯一主图、多参考图写回、正式决策与回执在同一事务，失败清理本次新复制文件。同请求重放不复制图片或重复选片。

参考图详情返回 `content_basis{script_sha256,bible_sha256,visual_rules_sha256,upstream_reviews_sha256}` 及整体摘要 `content_basis_sha256`。原审核快照schema2冻结这份依据及RunPlan摘要；完整创作Bible不包含本次写回的主图/参考图关系，图片关系另以写回后的result_targets核对，避免审核立即自失效。与选片复用同一个原剧本采用/审核修订读取器；不重新解析旧生成输入。`approval.changes` 明确列出内容、审核修订、候选或关系变化；current只表示人工审核依据一致，不是模型能力验收。缺失或旧版依据明确报错，不补齐伪装为通过。更换上游后需确认旧图片仍适用，或返回原生成入口新建候选；具体改选范围沿manual-change-impact，未知调用、装配锁等不放行。

人工全剧影视规则审核沿同一文本候选页面及 `manual-actions`：`action=approve`、`stage_key=visual_rules_review`、本项目scope/unit_key；`expected_selection_revision` 取动作选项新增 `visual_rules_review{revision,decision_id}`。payload恰为 `{text_candidate:{artifact_id,expected_sha256,expected_manifest_sha256},candidate_overrides,comment}`。`candidate_overrides` 必须完整覆盖原输出版本所选的审核字段：三项基础规则，以及该版本启用时的 `camera_guidance`；不得漏项或多报字段。来源步骤由 `source_stage_keys` 返回；只读取明确来源候选。

确认沿原影视规则候选写入服务，核对候选与原准备的输入来源、实际提示词摘要以及项目原规则基线，再写入原Project字段、ArtifactSelectionDecision/ApprovalDecision/ManualCommand。不要求已采用剧本或Bible审核，也不隐式合并项目原负面词。项目规则变化409，缺字段400，回执失败同事务回滚。审核只完成当前步骤，不自动物化内容或启动下游。人工审核快照schema3保存input_baseline与business_input_sha256；复制后编辑的梗概/风格是冻结内容，不跟随项目现值变化。动作选项的visual_rules_review.status为not_reviewed/current/changed；现有规则变化显示具体原因，旧输入协议明确显示需要新版审核，不补造新依据。current不代表模型Smoke或输入仍符合新的创作意图。修订仍是原决定序号，不因查询或变更而改写；损坏记录报错。

人工剧本审核入口：本轮人工制作台 → “查看并审核生成的剧本” → 明确选择服务端登记的生成/结构化步骤 → 分页候选及完整正文 → 阅读确认。沿原 `manual-actions` 提交 `action=approve`、`stage_key=script_review`、本项目scope/unit_key，`expected_selection_revision` 取 `script_review.revision`，不是初始剧本步骤的修订。payload必含 `{text_candidate:{artifact_id,expected_sha256,expected_manifest_sha256},expected_current_script,comment}`，可选`script_content`（按原候选合同编辑后的完整JSON文本，不接受null或非文本）；`expected_current_script` 完整引用当前选项响应（未采用为null），不能用最近候选替代。采用只确认所选剧本修订并更新项目母稿，项目标题与梗概保持原值；候选自定义字段仍保留，审核页面不要求固定title/logline字段。

Bible审核入口：本轮制作台的手动Bible审核或配方“设定集完整内容审核”，共用 `bible-review` 读取，原 `manual-bible-review` 路由已移除。原剧本实体声明、主图/变体设定及当前项目完整创作字段（含声音、负面约束等）分别折叠展示，逐项确认。设定依赖的历史主图正文按确切修订读取，不自动切到最新版本、不改写其旧审核。原声明已经入库时只核对来源，采用的是页面明确展示的当前人工修订，不恢复初始修订。

M3-02完整审核合同收敛：`candidates[].identity_anchor`只表示原剧本身份声明，必返回字符串或null；null为未声明，空字符串为明确空文本。删除原候选`visual_identity`别名，不从已有实体或主图补齐来源。只有身份声明而无primary时仍不猜建主图。`project_bible`和候选`existing`不再返回旧实体`visual_identity`；共同身份与外观通过`appearance_settings`引用原设定修订，审核详情的`settings`读取确切正文及固定历史主图。声音、说话人标识、其他原创作字段与metadata仍完整参与审核，图片绑定沿原独立审核合同。手动和配方生成审核快照共用这一投影；旧实体重复文字变化不使新审核失效，主图修订或其余创作内容变化仍使基线/原审核陈旧。含旧实体字段的历史审核即使重算自身摘要也不会被剥离字段后冒充当前合同；下游需重新核对，原快照不改写。前端拒绝旧形状，不静默兼容。旧实体编辑、物化写者及其余提示词消费者尚未全部收敛，本段不代表旧Schema已删除或现用已切换。

新声明使用保存的`script_semantics.v1`语义，不接受旧`text/origin/source_lines`包装。原内部Bible来源快照为schema3，包含确切`structure_source`和声明摘要；声明设定的`source_facts`返回`script_revision_id/already_materialized/structure_source/semantic_path/description/identity_anchor/appearance_anchor/primary_reference_name`。`semantic_path`定位原保存语义的`value.entities[index]`；三项描述文本为字符串或null，null表示未声明，空字符串表示声明为空文本。界面分别显示这两种状态，可展开确切来源指纹，不伪造原文行号。无外观声明只物化实体，不自动补主图；变体须明确主图和引用名。沿原权限、审核请求及事务错误处理，无新HTTP端点、不兼容旧来源快照、不改写历史业务记录。重复同修订物化保持原绑定与初始设定，不覆盖当前人工修订；不代表图片生成或跨修订实体合并已实现。

手动动作仍用 `manual-actions` 和本项目scope/unit_key。`bible_materialize/adopt` 的payload恰为 `{expected_current_script,comment}`，修订取 `source.revision`；核验原已确认ScriptRevision、生成请求和产物，冻结V2声明来源凭证，保存schema3的原ApprovalDecision，不创建Bible或调用模型。`bible_review/approve` 的payload恰为 `{expected_current_script,expected_source_snapshot_sha256,expected_project_bible_sha256,reviewed_candidate_ids,reviewed_setting_keys,reviewed_project_bible,comment}`，修订取 `approval.revision`。配方沿原 `approve`，批准bible_review必须额外提交 `bible_review` 对象，其五项为 `{expected_source_snapshot_sha256,expected_project_bible_sha256,reviewed_candidate_ids,reviewed_setting_keys,reviewed_project_bible}`；其他阶段或拒绝不接受该对象。两个ID集合必须与完整读取集合精确相等（允许明确空集合），缺项、重复、多项、来源/项目漂移均拒绝；整体项目确认必须为true。

共享服务在原阶段事务中保存声明→实体绑定、主图/变体设定及ApprovalDecision，手动路径另保存真实设定review和原手动回执；失败整批回滚。实体身份是项目＋稳定key，本次来源记录确切输出修订，不按同名猜测合并；引用名占用明确冲突。已批准设定不改写审核，已拒绝当前设定须先另存修订；图片生成/采用仍是独立动作。手动首次201、同请求重放200；配方沿原审批200。配方自动路径沿现有继续/后台调度：须本轮unattended确认及冻结approve_frozen_bible_v1/SHA均一致；原来源、设定及显式依据素材校验后记录actor=recipe_policy、decided_by=null与policy_evidence，不创建BibleVariantReview或人工reviewed集合。assisted继续停在awaiting_approval并要求完整核对，未授权/来源漂移/拒绝/写入失败明确阻断，不换用默认设定。运行详情原approval_decision_facts及页面“审批审计事实”区分人工与配方策略，无新增HTTP字段或操作。下游核验策略事实及当前内容，单项无人审时保留null，不借owner代审；旧决定不回填。现有下游结果仍受原改选影响门槛约束。旧V1名称物化写实现、旧规范稿拆镜writer及编解码已删除，人工/配方分镜入口共用声明writer；M4原混合调度已消费确切主图图片依赖，开发证据见下文，不代表整链、部署或真实模型产出验收。

原分镜编辑通过原Shot/Episode接口保存有限业务事实：标题、媒体类型、原对白、毫秒时序、明确人物/场景/道具关系及连续要求。Shot的GET/POST/PATCH不再暴露或保存 `narrative/detailed_description/camera/action/sound_effects/music/continuity_notes`；提交任一旧键（包括空串）返回400，不部分保存。metadata中的旧framing/negative_prompt同样拒绝提交，读取不再返回；其他元数据及服务端版本保护维持原合同。新增镜头无需填写narrative；没有结构化来源时，独立字段接口明确报缺失，不用标题或旧列推造创作输入。原创作值/版本沿ShotScriptSource和field-revisions，下游组装只在策略中配置；页面“分镜与独立字段”复用原字段查看与修订组件。模型及0232迁移已退役七个固定创作列；Shot翻译来源仅保留按原位置绑定的对白delivery，不读旧创作列或metadata负面词。现用迁移与模块切换的实际范围见唯一清单当前检查点。
分镜原结构化字段只读详情（M5-08）：项目→剧集→分镜→“分镜与独立字段”→“查看原结构化字段”，调用 `GET /api/v1/shots/{id}/source-fields/`，无请求正文，不生成、不审核、不保存镜头。登录及项目所有者/管理员权限沿Shot，未登录401/403、他人或不存在镜头404，POST等写方法405。

- 成功200返回 `{source,fields}`。`source`为 `shot_id/structure_id/stage_id/episode_number/shot_number/document_sha256/materialization_sha256`，structure_id是原ScriptRevisionStructure，坐标为绑定时坐标而非当前显示编号；`fields`每项为 `structure_version_id/node_key/field_definition/source_path/value_path/present/source_present/default_paths`，仅present=true才带value。field_definition保留确切字段身份、定义版本和冻结元数据；原始字符串空格、null、空串、false、0、空集合分别保留。默认值保留路径依据，不记成模型或人工填写。
- 缺来源绑定返回409 `storyboard_source_missing`；绑定摘要或原坐标冲突为409 `storyboard_source_conflict`；其他无效来源为409，沿原错误封套给出原因，不返回空成功或改读当前版本。成功及来源错误均private/no-store、nosniff。
- 页面按需读取，不在Shot列表/普通详情扩展源正文；缺失/空值/默认值分别显示，集合按需展开。切换项目/镜头清除原详情并丢弃迟到响应，重新读取失败清除旧值；未保存镜头不发请求。该页只证明原来源，不表示某次模型已收到这些字段；原字段只读接口不承担编辑；另存修订见下文，策略消费者继续M5-08/M6，尚未完成模块或现用切换。

分镜独立字段人工修订（M5-08，第243批）：同页“独立字段 · 人工修订”使用 `GET/POST /api/v1/shots/{id}/field-revisions/`。当前只修订已绑定原结构中的节点/路径，不为未绑定镜头猜字段，不重写剧本/人物关系/对白/毫秒时间。字段字典仍唯一，原值与定义/结构版本仍由原详情负责；人工值单独另存，不回填旧固定列，也不预先拼接提示词。

- GET无正文；可选`revision_id`读取本镜确切历史，省略时读取当前人工修订（没有修订则是原值）。200为`{source,source_sha256,revision,fields}`；revision含id/revision/previous_id/来源与内容SHA/created_by/created_at，初始为null。fields含original（原只读字段合同）、schema（原冻结节点Schema）、overridden/present及存在时的value。null、空串、false、0、缺失分别保留，不应用新默认值；对象与子字段是独立输入槽位，修订对象不重写其子槽位。只读详情可逐个回到上一修订，不批量展开历史正文。
- POST只接受JSON，正文严格为`{request_id,source_sha256,expected_revision_id,node_key,value_path,operation,value?}`；set必须有原始类型value，reset禁止value并另建修订恢复该槽位原值。首次201返回`{detail,replayed:false}`；同操作人同请求同内容200返回其原修订和replayed=true，即使后来已有新修订，也不冒充当前版本。当前修订ID或原来源摘要冲突、同请求异内容/异操作人409；节点/路径/类型/结构约束/未知字段/字节超限400，事务不留半条修订。未知回执只重试同请求，确定拒绝保留草稿、重新读取后再改。
- 沿Shot所有者/管理员权限，他人404、未登录401/403；确切历史不属于本镜或原绑定/保存体损坏409，不回退当前版本。成功和业务错误private/no-store、nosniff。修改在锁内核对依据，默认Shot查询不带修订JSON；共同分镜审核内容纳入最新修订的窄元数据，使旧依据陈旧，而不自动审核/采用。已发执行请求不被改写，策略取值接线仍未完成。0231仅创建空修订表且隔离验证，未应用现用库；手动试用入口要待M5-09模块切换就绪后才作为现用交付。

人工分镜采用：制作台 → “逐镜核对并采用内容”，沿`GET /api/v1/production-runs/{id}/manual-storyboard/`预览和`POST /api/v1/production-runs/{id}/manual-actions/`原命令。登录、项目/Run权限不变，他人404、未登录401/403；GET禁未知query且private/no-store。`storyboard/adopt` payload为`{expected_content_sha256,source,comment}`，修订取`adoption.revision`，项目scope/unit_key及原幂等键/RunPlan摘要必填。`source={kind:existing_storyboard}`另含全部`reviewed_shot_ids`；允许无剧本的真实人工分镜，拒绝空集、遗漏或重复。`source={kind:confirmed_script,expected_materialization_sha256}`须当前ready及确切摘要，仅项目范围且没有既有剧集时可创建；不能提交覆盖参数。沿共同materialize_declared_storyboard写声明集/镜头、类型化关系和原来源绑定，再在同一命令事务写Stage/真实人工ApprovalDecision/ManualCommand；绑定、采用快照或命令回执失败全部回滚，不改母稿、素材或生成任务。首次201、同请求重放200且零目标写入；陈旧请求/修订POST409，不满足依赖或损坏合同400。GET损坏已存采用或当前来源400，不以空详情/旧缓存代替。原采用快照schema5：source只有kind，或confirmed_script的kind/contract/sha256；不含旧解析method/warnings，不转换旧快照。创作内容仍为schema3，明确保存script_decision_id，未采用剧本为null；重新采用同一正文仍改变依据。

结构化创建合同：`materialization.contract`为与配方共用的schema3 `{source_basis,run_config_sha256,parameters}`（另有schema_version）。source_basis引用确切M2结构、原script_stages及script_decision_id、本轮真实Bible决定和已解析声明关系摘要；不扫描当前实体名字，不复制独立创作正文。parameters仅保留冻结时长范围/缺省时长、规划媒体、音频、多镜头与连续性布尔值；没有预期集数、自动切段或道具别名。按声明编号创建，已有时长/时间区间优先于缺省，不截断或补造。缺结构化来源、未完成/失效Bible采用或来源损坏时ready=false并返回reason和空contract/sha256，读取不写入、不重新解析原稿；已有分镜方式不因此被伪装成结构化来源。已完成的原来源回执不随后来配置/设定改写。页面显示确切结构/Bible依据和冻结参数，原请求重试保持幂等键，不从空旧描述列推断独立源字段缺失。

配方确定性分镜（M5-03已接入部分）：沿制作台原“继续”和`POST /api/v1/production-runs/{id}/continue/`，请求无需新增字段；仍要求登录和本Run所有权，他人404、未登录401/403。原Stage输入中的`materialization={contract,sha256}`保存本轮确切M2结构、M3人审/策略依据和冻结配置（contract.schema_version=3）。执行时重新核验，复用共同writer原子创建声明Episode/Shot、来源和类型化关系，不调用模型、不复制独立创作字段值、不改项目母稿或选择素材。缺上游/来源/Bible依据、陈旧输入或已有不同分镜时返回Run的明确blocked状态；不会伪装Stage成功。成功Stage重复继续不重做，完整相同直接执行重放零写入；写入失败不留半套目标，原输出回执保存异常回滚当次调度事务，不能当作已完成。

原分镜Stage回执和共用内容读取的镜头另含`script_source`（structure_id/stage_id、原episode_number/shot_number及document_sha256/materialization_sha256；无绑定为null）、`character_relations`（role/position/character_id，明确visible或speaker）及`reference_uses`（use_key/position/media_kind/purpose/setting_revision_id）。参考快照还包含用途的source_mode/reference_version_id/asset_id/asset_sha256；创作视图排除这些后续选择字段。三类事实批量窄查询，不另存正文，也不从旧混合人物池推导新角色；原漂移校验包含来源/角色/用途。人工和配方原入口均已切换共同writer；已移除旧breakdown_script及规范文本编解码、直接旧测试/辅助依赖；M5-08/M6及现用模块交付仍未完成。API/组件/浏览器隔离证据和未验范围见[详细设计第18节](optimization/modules/common/design.md#tracking)。

单镜选材（M5-05人工页面/命令/API增量，尚未完成模块现用切换）：登录用户通过 `GET /api/v1/production-runs/{id}/manual-shot-references/?shot_id=UUID` 读取本轮冻结剧集范围内的镜头；只接受唯一shot_id。200返回run_id/stage_key/run_plan_sha256/shot_id、storyboard_adoption_revision/storyboard_adoption_sha256、uses_sha256、state及editability（allowed/code/reason），并设private,no-store。state.schema_version=1，含project_id、shot_id及按position排序的uses；每项保留id/shot_id/use_key/position/media_kind/purpose/setting_revision_id/source_mode/reference_version_id/asset_id/asset_sha256。uses_sha256覆盖全部用途声明和选择，不覆盖时间戳；读取不探测媒体、不选默认、不写业务数据，不能作为输入可用证明。

写入复用 `POST /api/v1/production-runs/{id}/manual-actions/` 原信封：action=select、stage_key=storyboard_review、scope={kind:shot,id:UUID}、unit_key=shot:UUID，expected_selection_revision为上述分镜采用修订，并携带准确RunPlan SHA和独立idempotency_key。payload恰为 `{expected_storyboard_adoption_sha256,expected_uses_sha256,choices}`；choices为非空数组，每项恰含use_id/source_mode/reference_version_id/asset_id/asset_sha256。source_mode明确为existing/upload/generated，版本与直接Asset恰选一项、另一项为null；upload核对真实上传来源，generated须引用原正式登记的生成版本。提交只修改本镜已存在用途的选择字段，未提交用途、其他镜头、Bible主图和历史请求不变；不增用途、猜实体、伪装审核或自动运行。成功201返回原命令id/action/created_at及result_kind=shot_reference_choices、target_id=原Shot UUID；响应不确定时重放原键和完整原请求，200仅确认原动作，不重写后续选择。新请求必须重新读取基线；同键异内容、旧计划/采用修订/用途摘要409。

选材权来自本轮冻结人工动作目录，不接受客户端allow_override等权限声明；旧计划缺动作则明确不可用，不改写旧计划。未采用或创作内容变化、错用途/范围/版本、损坏文件、已结束Run或装配冻结400；无权Run404，未登录401/403。该镜头已有下游准备或调用时只读；仅已明确取消、未调用、未冻结且无claim/定时推进的准备允许重新选择，其他镜头独立处理。写入与原命令回执同事务，回执失败全部回滚；不存在新任务队列或第二份选择事实。人工和配方适配器复用同一选择事实；模块现用试用与真实调用验收另列，验证记录见详细设计第18节。

人工页面入口：项目工作台 → 无配方人工制作 → 分镜 → “选择本镜素材 / 上传”。原镜头目录分页选择本轮镜头，读取上述用途基线后按名称、媒体类型和目的选择一个用途；共享素材选择器保留选定系列的确切版本，库内选择记existing，上传经原Asset入口后记upload，不把原生成取得来源改写为本轮生成。保存只提交当前用途，其他待选用途保持待选。当前已选素材按确切版本/Asset与指纹读取名称和预览，缺失、类型/指纹不符或预览失败显式展示，不找最新版本或主图兜底。

页面将初次明确拒绝、摘要冲突和结果未知分开：初次明确拒绝保留草稿允许修正，409要求重读；超时或无法确认的回执保留原键及完整原请求，只允许原请求核对。已确认成功而重读失败显示成功与陈旧状态，不另发一次保存。关闭重开保留当前草稿及待核对请求；工作台在编辑期间保护原动作上下文并暂停外层轮询覆盖，离开提醒未保存/未知状态。这一控件同时供人工与配方工作台使用；配方的默认来源和改选权限由冻结规则决定，不能由页面覆盖。

第223批接入配方编辑器的“分镜审核 → 逐镜素材来源规则”。原配方目录返回 `shot_reference_source_options`，每项含 `value`、`label`、可空的 `upstream_stage_key` 和 `selection_options`；草稿仍通过原配方版本创建/读取/更新 API 保存，不新增选材 HTTP 入口。可选阶段字段为：

```json
{
  "shot_reference_selection": {
    "source": "upstream",
    "source_stage_key": "bible_image_generation",
    "selection": "unique_setting",
    "allow_override": false
  }
}
```

此完整字段只允许在 `storyboard_review/review`。`source=upstream` 必须明确选择本配方之前的 `bible_image_generation/existing_pipeline`，选择方式可为 `manual` 或 `unique_setting`；`existing`、`upload` 仅人工选择且 `source_stage_key=null`。允许改选必须为显式布尔值，不能以字符串或缺省代替；未声明整个规则时不补权限或默认来源。来源切换会清空关联的上游和选择方式，不能自动选第一项。`allow_override=false` 表示不可替换已有选择，不禁止未选择用途的首次选材；默认匹配本身不覆盖已有选择。这与阶段审核策略、模型输入用途是不同职责。

规则随原 RunPlan 冻结，并在原 Stage 输入快照中记录 `shot_reference_selection={rule,upstream}`；上游身份为原 `stage_run_id/output_snapshot_sha256`，非上游则为 null，不复制素材或第二份选择事实。内部 `plan_configured_shot_reference_choices` 核验原冻结计划/配置/输入和上游摘要，再调用第222批只读匹配计划；不接受调用方注入来源、策略或改选权限。后续配方修改不改变本轮规则，上游结果变化必须拒绝，缺失/歧义不猜测主图或最新版本。

冻结 `unique_setting` 时，原 `storyboard_review` 事务先核对全部镜头及本轮上游结果，全部明确后写原用途选择，保留已有明确选择，不改变Bible主图。选材与审核策略分离：`required` 也自动匹配，但仍停在 `awaiting_approval`，不写ApprovalDecision或创建下游媒体准备；缺设定、无结果或歧义保留 `waiting_shot_reference_selection` 及具体镜头/use_key，不产生半轮选择。已等待人审的Run可由原制作台“继续 / 检查阶段”（`POST /api/v1/production-runs/{id}/continue/`，无正文、原权限、返回200原详情）重新应用冻结规则，仅补空选择并保留人审停点；来源不符、文件损坏或执行锁定返回明确400，事务回滚。GET、刷新及后台扫描不补选；重复Continue保留已有选择。策略审核分支仍以 `awaiting_input` 等待缺项，Stage为running，普通扫描不自行恢复；选择及媒体合同通过后由原ApprovalDecision记录actor=recipe_policy、decided_by=null，不冒充人审。无新HTTP端点、Schema或队列。

第226批接入配方单镜人工选材 API：`GET /api/v1/production-runs/{id}/recipe-shot-references/?shot_id=UUID` 只接受唯一shot_id，核对原RunPlan范围及本轮已冻结分镜中的镜头，返回private,no-store。200包含run_id/stage_key/run_plan_sha256/shot_id、source_snapshot_sha256、uses_sha256、共同state基线、editability、selection_rule和upstream_candidates；**没有人工分镜采用修订**。selection_rule为原Stage的`{rule,upstream}`；候选仅来自冻结上游原目录，每项保留source原结果引用、source_mode、reference_version_id、version、setting_revision_id、asset_id/asset_sha256、name/mime_type；已有/上传来源的该目录为空。此GET只读元数据，不检查文件或表示素材可用；来源缺失、上游/创作漂移明确400，不重新冻结或找其他来源。

写入复用原 `manual-actions` 命令/回执表，配方仅开放 `storyboard_review/select`，不是另一执行队列。信封仍包含明确shot scope/unit_key、RunPlan SHA和独立幂等键，但expected_selection_revision必须为null；payload恰为 `{expected_source_snapshot_sha256,expected_uses_sha256,choices}`。choices与人工共同writer相同。上游来源只能提交冻结目录内的确切版本、目录给出的本轮使用模式和SHA，重验原M4结果及文件，不允许用直接Asset、库内别的版本或上传替代；material_generations使用generated、material_fulfillments使用existing，不重写素材版本的原取得来源。非上游的existing/upload分别要求明确相同来源模式，upload必须是原正式上传。`allow_override=false`允许未选择用途首次选择及原值不变的确认，禁止替换已有选择；客户端不能注入权限、用途声明或其他镜头。允许改选也不能越过本镜准备/调用、已完成审核及装配边界。

成功201返回原命令id/action/created_at及shot_reference_choices/原Shot UUID，不写ApprovalDecision、不改Run/Stage状态、不推进模型。相同键与完整原请求重放200，只核对原回执目标，不重新写后来选择；同键异内容、旧来源/用途/计划摘要及创作来源冲突409。无权Run404；冻结来源或原结果缺失/损坏、权限/执行状态或文件错误400；选择与回执同事务，任一失败全部回滚。其他配方动作仍从原生产控制入口执行，本增量不开放人工采用或审核动作。

配方制作台的“本轮素材选择（单镜 / 多镜）”复用人工同一选材控件，按冻结来源仅开放上游目录、已有或正式上传；先核对确切版本/Asset/SHA，不猜用途。禁止覆盖不妨碍未选择项首次选材。提交不审批或恢复，结果未知保留原命令；打开、草稿或未知状态暂停父层轮询和其他运行操作。创作来源排除四项选择字段，完成审核冻结全部选择并检查漂移。旧实体主图审核writer、旧请求字段/页面草稿及临时错误边界已删除，不做兼容。

多镜批量选材仍使用原读取与写入API，不增加批量端点：制作台 → “选择素材（单镜 / 多镜）”（人工入口“选择素材 / 上传（单镜 / 多镜）”）→ 明确选择镜头、用途与素材 → “加入多镜批量选择” → 勾选镜头/用途 → “核对批量影响与冲突” → 确认保存。预览显示各独立用途的原选择/新名称、版本、来源和预览图；逐镜GET核对原uses/创作来源基线及冻结来源权限，发现变化不自动覆盖。单镜内勾选choices一次提交原storyboard_review/select，复用一个原事务、幂等键和回执；未勾选镜头/用途不提交，各镜头之间不是整批事务。实际文件、权限及目标合同仍由原保存服务核验，目录/预览不保证提交必定成功。

批量逐镜显示已确认、冲突、明确拒绝、结果未知，不将部分完成显示为整批成功。明确冲突/拒绝须移除该镜头草稿后重读再选；未知保存字节一致的原请求，只能显式核对该请求，不能重新生成幂等键或换素材。关闭面板保留本页草稿/未知请求，离开或刷新前提醒可能丢失；批量期间沿原父层上下文保护暂停轮询与运行操作。成功回执仅证明原命令结果，随后再次编辑必须重读，不自动审核、恢复调度或调用模型。

配方人工参考审核：`GET /api/v1/production-runs/{id}/storyboard-reference-selection/`不接受query，仅当前assisted配方的awaiting_approval/storyboard_review可读。沿原登录/Run所有者范围，他人404；200返回`source_snapshot_sha256/selection_sha256/shot_count/selection/display`且private,no-store。selection为schema1、project_id及全部有序shots/uses；逐用途保留身份/位置、媒体类型、可选创作原文、来源模式、确切设定/版本或直接Asset身份及文件SHA。只核验已有选择和原目标合同，不创建选择、审核、准备或调用；缺项/文件/来源/目标错误400，状态冲突409。

原`POST /api/v1/production-runs/{id}/approve/`的storyboard_review/approve须沿stage_key/decision/comment提交64位小写十六进制`expected_source_snapshot_sha256`及`expected_selection_sha256`，不再接受shot_reference_bindings。两个摘要在原事务内重验；改选即使创作来源未变也409/code=storyboard_reference_selection_changed，须重新读取，不自动重试。只写原正式快照/ApprovalDecision（真实human/decided_by）并沿原调度，回执失败整体回滚，Bible默认主图和兄弟镜头不变；其他阶段/reject不接受选择摘要。页面“本轮分镜确切素材审核”只读展示独立用途及确切版本，同身份轮询不替换所见摘要；选择打开清除批准依据，返回重读，原审核读取错误明确禁止批准。未知响应继续沿原回执核对。

两种制作台的分镜审核页复用“查看确切素材”：仅用户展开某用途后，按原选择依次GET确切`asset-reference-versions/{id}`（版本引用时）、Asset和`asset-reference-series/{id}`；直接素材不补造版本。配方以原selection中的版本号/系列/取得方式/Asset/SHA复核，人工以所见原canonical.reference_uses的use_key、来源、版本ID及文件指纹核对执行投影；不补查当前Shot、最新版本或默认主图。展示当前素材/系列名称、媒体预览、主图/变体与确切版本，选择来源与素材取得方式区分，原ID/SHA收在技术详情。身份不符、无权限、失效/指纹变化、缺预览地址与媒体加载失败明确显示，可重新读取同一身份；收起、选择变更或离开丢弃迟到结果。预览不重算审核摘要、不产生采用/选择/模型调用，名称与图片可见也不等于文件已通过正式执行校验；原批准事务仍重新验证。

上述配方GET及人工`GET /api/v1/production-runs/{id}/manual-storyboard-review/`共同返回只读`display`（人工未就绪时为null）。`name_basis=current_bible_by_id`明确名称时点；`shots`按原`episode_number/episode_id/number/shot_id`稳定排序，含`episode_title/title`、原`scene`、`characters`和`props`。人物条目为`role=visible|speaker`、原`position`及`entity`，speaker位置对应原对白行（页面显示位置+1），可重复发声，不推断出镜。实体为`{id,name,status}`，同项目确切ID当前有非空名称时status=available，否则name=null/status=unavailable；不泄露外项目名称、不按同名对象或旧混合人物集合补齐。缺失类型化原关系或非法排序身份400；确切关系存在但名称不可用时保留身份并明确提示。display不进入原canonical、selection或审核摘要，不持久化、不改变批准请求，也不表示已执行请求的绑定。页面及API只读、身份/失败/排序与查询上限已经隔离验证；未执行改选影响及审核联动已通过定向隔离核验。

两种分镜审核页与原分镜页复用“工作流历史请求”入口，显式操作后才按project及shot unit_key读取原`/api/v1/tasks/`、所选任务详情和`/api/v1/tasks/{id}/input-prompt/`。只读核对任务/准备/Attempt/请求SHA/实例版本一致，逐原用途显示原名称、输入槽位、引用标签、配对、参数与暂存状态，原始媒体与仅提示词上下文分开；技术ID/指纹折叠。媒体地址仅来自原任务reader的所有者与原文件指纹核验，不按当前选择替换；缺地址或媒体失效保留原绑定，原请求未记录系列版本则明确未记录。该原素材预览仍仅覆盖ComfyUI GenerationTask；同页新增“本镜执行准备历史（所有驱动）”，按`GET /api/v1/task-center/?category=production&project_id=...&unit_key=...`及原游标分页读原生产准备，选择后复用原`task-center/preparation/{id}/`详情、指定调用与正文分块接口的只读模式，其中unit_key取`shot:{shot_id}`。unit_key只匹配有实际StageRun的ExecutionPreparation，与原权限及项目/轮次/阶段条件取交集，不从旧任务JSON推断或把孤立任务算入。未冻结、无记录、无媒体绑定与不可核对分别呈现；当前非ComfyUI编译器未接通媒体输入，不能将当前镜头素材补进旧请求。只读模式不轮询、不取消/修正/编辑/触发媒体操作，不自动选择最新准备；读取正文期间防切换，坏分页/身份/权限错误明确失败。人工逐镜审核未知响应保留原body/key，暂锁刷新、关闭、路由及整页轮询，仅重试同一原请求；首次明确拒绝才解锁重读，核对未知请求时再遇拒绝仍保留原请求。

配方选材等待的恢复沿原 `POST /api/v1/production-runs/{id}/continue/`，无需另建重试命令。`waiting_shot_reference_selection` 下保存选择、普通扫描及未标为人工继续的内部推进均不恢复；明确点击继续后重验原冻结来源和全轮当前用途，未选齐仍保持 awaiting_input，不创建审核或模型调用。自动策略恢复仍记 recipe_policy/null，不因人工选材变成人审。共同只读审核核心核验原上游结果、确切版本/文件及目标媒体合同，并给出来源与完整选择摘要；原人工配方approve已使用上文双摘要防陈旧合同；等待恢复、选择和审核是不同动作，均不替代媒体请求编译或真实调用验收。

默认选材遇到已有合法素材选择但与本轮来源规则不符时（例如原轮生成、本轮已有素材复用），仅在冻结规则允许替换且冲突镜头尚可编辑时，保留原选择并返回 `waiting_shot_reference_selection`；required 审核保持 `awaiting_approval`，策略审核保持 `awaiting_input`，均不形成批准。每轮新应用的默认选择在保存点内回滚后才展示接管，其他镜头不会部分保存。不允许替换或已有准备/调用的镜头仍明确拒绝。人工选材和审核仍拒绝来源不符的请求（`shot_reference_source_conflict`）；全轮当前选择的真实文件先于来源分类校验，文件、权限、冻结上游或模型合同错误不降级为该等待。按本轮允许改选权限明确保存确切版本和使用来源，再点击原 Continue 核对；素材版本原取得来源不改写。旧代码已保存为 blocked 的确切阶段须先读取原 retry-preview 并确认原 retry；无调用阶段的重试不创建模型任务，也不重新生成成功素材。

逐镜参考审核：制作台 → “逐镜审核参考素材”。先采用分镜内容，再读取 `manual-storyboard-review`；ready=false明确给出未采用、来源变化或素材/上游审核问题，不返回伪造合同。ready=true包含同份完整canonical、原素材身份、所需上游决定和逐镜references视图（不另存引用事实）。`storyboard_review/approve`沿原manual-actions，payload恰为 `{expected_contract_sha256,reviewed_shot_ids,comment}`，修订取approval.revision；全部镜头须逐项核对，包括items为空的镜头。请求摘要/修订变化409，不完整或门禁阻断400，成功201，同请求重放200。确认只写原StageRun/ApprovalDecision/回执，不改素材绑定、分配模型槽位或提交模型请求。

上述人工审核及`reviewed_references`准备读者使用唯一`ShotReferenceUse`和共用确切来源解析器：直接Asset/版本、原用途、顺序、设定修订及指纹保持，待选用途明确未就绪，不从旧`reference_bindings`或Bible默认图补齐。独立图片无需先进入Bible图片池；关联设定/实体仍核对原Bible内容审核，不补造图片审核。审核合同assets只包含确切用途实际选定的素材，页面不再展示未使用的旧附加素材；旧池变化不改变新合同，实际用途/选择/文件变化仍须重新核对。`source_options`与配方`reference_source_options`共用目录，页面不维护第二套来源名称。目录名称只描述素材类别，不声明“已审核”；审核状态以本轮合同的`approval.status`及修订为准，未审核和陈旧时不得由类别标签暗示已通过，执行读者仍检查原审核事实。

逐次用途经本次调用映射、共用执行输入解析器及`execution_reference_materials`形成实际输入端口/标签；没有把旧Shot JSON或全局Picture编号再转换为执行输入的第二路径。创作描述不决定首帧或模型用途；同一素材的独立使用保留不同标识，是否允许复用仍由目标合同检查。Shot内容API的旧绑定字段现已退役，详见上文；物理字段、种子及其余创作消费者仍按M5-08/M6继续。渲染单元夹具、真实本地素材组合测试和未验的现用/真实模型范围分别记录于唯一清单。

共同分镜内容快照只含`character_relations`的明确visible/speaker及原position，不再返回`character_ids/visual_reference_character_ids/reference_asset_ids/reference_bindings`。speaker逐行覆盖原dialogue且保留原文字与时间；缺失、重复或越界关系明确失败，不按名字或旧人物池修补。`shot.linked_bible`提供唯一实体正文及明确关系，不能把其中characters全集理解为出镜角色；`shot.dialogue_with_speakers`按speaker位置绑定确切已审核实体，原speaker_id须与该实体一致，不重新匹配或改写对白。原采用/审核快照不转换、不回填，当前依据变化按原漂移规则处理。Shot CRUD人物关系已同步上文明确关系合同；旧绑定编辑/API及模型旧字段已退役，0230仅在隔离库验证，现用库旧列/关系表尚未删除。独立创作字段及策略消费者仍按M5-08/M6继续。

新用途图片/视频/音频分别投影为`images/videos/audios`；用途身份不依赖Asset或数组位置，主体只来自确切设定。不从用途文字推断首尾帧、连续性或音画配对。分镜`purpose`为可选创作描述并保留原文；结构化原值保留缺失与空串，关系行允许空串。每次调用由原输入映射选择素材及目标槽位，仅非空`allowed_purposes`合同要求组内明确模型`purpose`；无额外用途时省略组键，由适配器输出null，不清空原审核描述。原审核漂移、文件/指纹/来源变化仍拒绝，重放不重新选材。用途修正已获用户批准；配方自动规则、人工改选和原审核共用确切选择，模型内容策略消费者及M5模块交付仍按各自细任务验收。

原执行输入解析器对镜头范围的`reviewed:`项，按调用输入key、shot_id和原用途reference_key重建身份，并通过共用`resolve_shot_reference_sources`核对已保存的确切设定、素材版本/Asset及文件事实；主体和素材必须与该用途一致。不能因为新选图片不在Bible旧主图/参考池就拒绝，也不能靠伪造前缀、另一镜头/用途或已变更选择通过。此解析只读，不改Bible主图、不补参考池、不把选材视为审批；非分镜映射的独立主体输入仍按其原关联合同校验。准备及请求编译不等于模型准入、提交或真实Smoke。

人工审核核对同Run、冻结计划来源步骤、实际Preparation/成功主Attempt、候选正文与清单SHA，以及准备时冻结的剧本采用基线。复用原ScriptRevision确认、项目母稿写入和ArtifactSelectionDecision，保留项目原标题和梗概，再同事务写入原ApprovalDecision/ManualCommand；任何失败全部回滚。不创建另一个文本执行器、配方或伪造已完成生成阶段。原母稿/当前剧本或审核修订变化409；迟到结果、未支持来源、字段缺失400。已有下游按同一manual-change-impact分类核对：受支持的文本、媒体和已停止准备允许明确更换后逐项复核或主动新建准备；在途、未知结果、装配锁和终态仍阻断。旧基线候选不能覆盖后来已采用的稿件，也不能重新绑定为新内容的生成结果。

`script_adoption` 是初始输入步骤的历史采用，`script_review` 是生成候选审核步骤的历史采用；两者revision各自递增，不能相加或比较为全局版本。`current_script` 才代表Run实际使用内容，复用原完整已确认剧本引用（id/source_id/revision/sha256/size_bytes/confirmed_by_id/confirmed_at），不是正文。读取只校验元数据；提交显式审核才核验完整内容。页面不把初始记录当作当前剧本。请求结果不确定时保留同一请求重试；重新读取并确认后才能构造新意图。采用后不自动生成下一步、批准影视规则、物化Bible或开始装配。

媒体采用先读取 `GET /generated-artifacts/?production_run=UUID&stage_run=UUID&page=N` 的原分页候选，再显式打开 `GET /generated-artifacts/{id}/`。详情新增 `manual_media_candidate`：包含 `run_id/stage_key/unit_key/scope/run_plan_sha256/preparation_id/request_sha256/selection_revision/selected_artifact_id/selection_decision_id`；只读核对真实准备、成功调用、归档及采用状态，不创建决策。列表不返回该详情或逐文件探测。非人工媒体生成候选该字段为null；无权404，清单/字节/归属或当前选择不一致400，不回填旧快照。

媒体沿相同人工动作信封，`action=adopt`、`stage_key=media_generation`。payload只含 `source`，禁止旧的平铺字段：本轮候选为 `{kind:artifact,artifact_id,expected_sha256,expected_manifest_sha256}`；素材库来源为 `{kind:asset,asset_id,expected_sha256,media_kind:image|video|audio}`。候选的scope/unit_key/RunPlan摘要/采用修订使用详情返回值；素材采用的对象由targets明确选择，unit_key为该对象的完整标识，修订取manual-media-review中同一单元的当前采用，尚无采用为0。不能按素材名称推断镜头，也不接收模型、工作流或策略覆盖。

两种来源均返回 `result_kind=artifact_selection` 指向原ArtifactSelectionDecision。选片与回执同事务，同键重放不重新采用；新键确认同一个已选产物也不增加采用修订。修订冲突409，无效来源/错误范围/装配已封存400。素材库来源复用正式Asset权限/启停/来源哈希校验及媒体探测，归档到租户受管目录；GeneratedArtifact的source=adopted并引用原Asset，无Preparation/Attempt/Task。文件归档与数据库不是跨系统原子事务：数据库失败不生成成功回执，重试校验并复用相同受管字节，不覆盖损坏文件。原素材文件随后变化不会替换已归档副本；新采用仍须核对当前授权来源。不声称系统已自动审查版权。

候选详情的 `manual_media_candidate` 明确 `source_kind=execution|asset`；前者有preparation_id/request_sha256而source_asset_id为空，后者有source_asset_id、执行准备与请求摘要为空。采用不会自动批准阶段、执行模型、装配或交付。图片/视频/音频预览与下载共用原签名媒体接口，按实际字节核对类型、大小与SHA。

选片审核的 `selection.schema_version=4` 同时冻结 `delivery_timeline` 与 `content_basis`。前者含本轮各集 `episode_id/number/title` 和顺序排列的 `shots[].shot_id/number/title/duration_ms/media_kind/episode_start_ms/episode_end_ms`；后者含 `script_sha256/storyboard_sha256/bible_sha256/visual_rules_sha256/upstream_reviews_sha256`，均为服务端对原事实生成的摘要，不由用户填写。其范围分别为本轮采用剧本与决定、完整分镜及参考、人物场景道具、影视规则及媒体步骤之前的原审核修订。变更时 `approval.status=changed`，新增 `approval.changes:string[]` 解释已变化类别；旧提交409，共享已采用媒体读取及装配阻断。人工重读并明确确认后才能复用旧选片，不修改Preparation/Attempt或原候选，不再次解析旧selected_output，也不代表模型重新执行过。缺失时长、空剧集仍如实返回，由装配门禁阻断。此合同不双读旧审核协议，真实切换前按P5清理。

改选影响的 `replacement.code=media_review_required,allowed=true` 表示：后续仅为已接通重新审核的人工Bible/分镜步骤与人工参考图生成/审核、媒体生成/选片，媒体原调用全部有确定终态且准备已成功交付或明确停止；允许回到原上游动作更换内容，之后必须逐项重采用/审核，并明确确认旧媒体复用或主动创建新候选。不代表自动取消、自动重跑或已完成交付。原在途/未知结果、可继续执行但未停止的准备、其他尚未接通复核协议的生产结果、装配封存和终态保持阻断。入口、权限、分页、读写事务及错误状态沿原manual-change-impact/manual-actions；隔离验证与后续切换见计划书第25节；当前剩余验收只在第0.1节维护。

页面入口：人工制作轮次 → “查看本轮媒体候选” → 采用本轮候选，或“从素材库直接采用” → 读取制作对象和当前采用状态 → 选择素材/上传本地文件入库 → 明确采用；之后另行进入“审核本轮已采用媒体”。失败保留同一请求供重试，重新读取才放弃旧输入；不是自动选取最后完成项。交付与下游影响复核已有后续实现记录，最终效果/封存和当前负向场景仍待验收。

装配来源失败使用原错误信封，按已知事实定位“第N集／分镜M（镜头UUID）／产物UUID”：缺少选片时给出缺失数量及首个确切镜头，尚无产物则不补猜产物ID；素材文件缺失、摘要或来源漂移、探测失败及没有视频流均定位原项。入队前发现异常不创建Job；已排队后发现来源损坏不执行编码、不跳过片段伪造成功，保留原选片及失败任务。冻结输入顺序不自动排序纠正，配方与人工入口复用同一装配器，接口形状和审核规则不变。

装配入口为同一工作台的“查看选片与装配任务”。先从 `manual-assembly` 读取完整选片、镜头时长、输出尺寸/FPS、编码配置与逐集任务；首次提交后返回冻结配置，不按当前设置替换。沿人工动作信封提交 `action=assemble`、`stage_key=assembly`、`scope={kind:episode,id:剧集UUID}`、`unit_key=episode:UUID`；`expected_selection_revision` 为视图里的选片审核修订，payload含 `{expected_contract_sha256,expected_assembly_job_id}`，可另含 `authors`（本次预览所用的确切输出版本、原文及M1映射），首次job为空，仅失败重试携带准确旧job ID。首次202返回原 `assembly_link` 引用，重放200；同键异内容/旧合同/任务漂移409，未审核、缺片、错误范围、在途重跑或失败来源400。装配与回执同事务，失败清除本次新concat文件；不直接修改已有媒体。后台真正完成才归档母版，人工Run继续等待交付。装配冻结后改选需新建制作轮次，不原地换片。页面先展示指定剧集与输出规格，再由“确认提交本集装配”执行；取消、重新读取或切换Run使未提交确认失效。响应未知保留同一冻结请求；重新核对只读任务、不丢弃原请求，不取消可能已创建的任务。回读失败仍保留恢复按钮并标明旧详情，禁止创建新意图；首次请求被明确拒绝可解除该意图；已经未知的原请求只有确定回执才解除未决保护，恢复时后续鉴权/权限/输入拒绝不能否定此前执行。隔离验证范围见计划25.98、25.102，2026-09-16真实两镜头生成、选片及装配验证见25.182；最终业务效果仍待用户验收。

AssemblyJob只读响应新增`consumer_stage`（UUID或null）与`consumer_input_sha256`（原输入摘要；未记录为空）；0234不回填未知历史。任务中心assembly行的`production_run_id`沿真实消费Stage返回，按Run/Stage筛选包含已被新任务替换的旧失败Job，不从当前ProductionAssemblyLink或素材生成Stage推断。详情`input.submitted`新增`consumer_stage_id`、`consumer_input_sha256`、`source_signature`及`consumer_input_status=current|changed|unrecorded`：changed只说明原摘要不同于Stage当前摘要，不替换历史；权限仍沿原任务所有者/管理员。人工逐集页面提供此任务及本轮装配历史链接，失败来源仍保留原选片，重试不调用媒体模型。

装配配置错误沿原接口显式返回400：当前或已冻结的尺寸/FPS必须为安全上限内的明确正整数，重试/披露字段类型须正确，编码正文及其SHA256须一致；缺项或多余字段不自动兼容。`manual-assembly`读到无效冻结配置不会改取当前设置，提交也不会据此新建任务或文件。第300批人工页新增可选`assembly_output`/`assembly_timing`作者，复用配方同一字段映射组件；`POST manual-assembly`只预览，首次assemble原样提交authors并在原事务中重验摘要、已审核完整时序及当前已发布版本。原文、确切输出/结构合同、投影同原输入快照冻结；后续各集及失败重试不换版本、不重新读取当前配置，已退役版本仍可按冻结合同回读，缺失/损坏不能自动修复。预览失败保留草稿；修改作者后取消旧确认，未知响应保留原请求。权限沿Run拥有者/管理员，原选片/审核、队列和交付动作不变。输出合同与投影按需展示，不暴露字体路径。隔离API/队列和组件验证见详细设计18.3第300批；现用后端未加载、完整多集浏览器及真实编码未在本批验证。

配方装配首次准备在原StageRun配置快照内保存`assembly_configuration`：编码、重试上限和披露取本轮已冻结配置，尺寸/FPS及服务端字体取首次准备时值；输入快照的`assembly_sources`同时保存确切剧集/镜头顺序、毫秒时长、成对可空剧集时间、产物及文件/manifest摘要、原采用决定ID/修订。首次入队和明确失败重试核对这两份原合同，不读后来的项目尺寸或业务配置替换原值。来源、采用修订或时序变化时，原继续流程明确记录阶段blocked，原明确重试接口返回400；原快照、失败Job和关联保留，不静默换源重试，不新增媒体生成。缺失、损坏或与RunPlan/上游不一致时拒绝，不为旧阶段补造合同。两个正式入口均向原装配器传完整`frozen_sources`；人工入口仅从本次已审核清单投影，配方入口返回原阶段逐镜快照，不再退化成只有产物ID的字典。集镜身份/顺序、整数毫秒时长、类型、区间、摘要及采用修订校验后，冻结值直接用于FFmpeg时长、逐镜来源记录与来源签名；当前Shot仅用于归属/漂移核对，不能覆盖原值。空清单、缺字段、重复身份、错序或错误类型均先于探测和Job创建拒绝。原权限、队列、任务身份、选片和重试请求/响应形状不变；可配置选片/顺序/时长/交付作者映射仍未据此完成。

人工成片确认：工作台“查看成片与封存”→读取 `manual-delivery`→通过原GeneratedArtifact签名预览/下载核对逐集母版→“确认成片并封存”。沿同一 `POST manual-actions` 信封提交 `action=deliver`、`stage_key=delivery`、`scope={kind:project,id:本项目UUID}`、`unit_key=project:UUID`、准确RunPlan SHA及 `expected_selection_revision=contract.selection_revision`；payload恰为 `{expected_contract_sha256:清单SHA,comment:确认说明字符串}`。后端 `production_manual_delivery` 复用原ReleaseBundle服务，校验本轮完整选片/审核/装配范围、在途调用、源文件/分发副本/母版签名；不编码、不修补文件、不创建新任务。首次201返回 `result_kind=release_bundle/target_id`，同键同内容200复用并实时复验；旧SHA/修订或同键异内容409，缺集、未完成、损坏或在途400。当前用户必须拥有Run或为管理员；发布包、人工审核、Run终态与命令回执同事务，失败均回滚。已封存不等于对外发布，也不会结束未实际操作的可选阶段；剩余阶段保留原状态。基础清单读取不能替代用户播放验收或提交时完整核验。隔离实现见计划25.102，后续切换见25.180–202；最终播放与封存仍须用户确认。

成片页面仅在Run ID或冻结计划SHA改变时重置预览和未提交说明；同一运行的常规状态轮询不清空正在查看的成片。明确“重新读取成片清单”仍会刷新依据，提交封存仍执行原文件/合同完整核验，不能用保留的页面状态绕过审核。2026-09-16已核对真实母版预览及跨轮询稳定性，最终封存尚待用户效果验收，详见计划25.182。

选片审核使用上文定义的schema4完整selection（含content_basis和delivery_timeline），不接受仅含items的旧集合。审核沿同一人工动作：`action=approve`、`stage_key=artifact_selection`、`scope={kind:project,id:本轮项目}`、`unit_key=project:UUID`，`expected_selection_revision` 是该审核修订；payload恰为 `{selection,expected_selection_sha256,comment}`。提交时核对完整集合、当前内容依据及实际归档文件，拒绝过时或删减的成员，追加原ApprovalDecision；同一完整审核依据再次确认不增加决策。之后改选只使审核不再匹配，不修改旧决策或自动开始装配。

共用selected_output解析器核对精确Decision/Artifact、当前完整选片审核、真实来源和归档字节。人工生成准备与Comfy素材暂存共用冻结RunPlan的来源阶段校验；人工media_generation可在未结束的同一生成步骤中显式使用已审核选片，不把生产者伪装为成功。页面“读取本轮已审核素材”复用manual-media-review清单，再按槽位选择对应类型；请求仍提交精确采用记录，不创建Asset代理。素材使用ID由输入键、Decision、Artifact及用途确定，配对引用同步保持；改选后旧审核/来源明确阻断，不自动选择新结果。此链处于源码与隔离测试验收，不代表真实模型、完整下游影响或自动重做已完成。

生成动作首次202和原请求重放200均带 `Location` 响应头，指向同一执行准备详情；非生成动作不返回该头。创建、查看准备和重新读取回执都不表示已调用模型。

`manual-action-options` 的文本生成动作返回步骤要求的精确 `operation` 和空 `output_kinds`；媒体动作返回 `operation:null` 及允许的 `output_kinds`，按实例冻结输出核对，不根据操作名猜类型；非生成动作两者为null/空数组。`scope_kinds` 表示可选制作对象类型。生成沿相同外层合同，`action=generate`、`expected_selection_revision=null`，`scope/unit_key` 使用目标目录返回的明确对象；文本只能项目级，媒体可选择项目、剧集或镜头。媒体准备冻结 `production_unit` 的RunPlan摘要、目标范围与真实祖先归属，后续任务不得从名称或旧图推断剧集。`payload` 必须且只能包含 `execution_instance_version_id`、`expected_definition_sha256`、`inputs`、`parameters`、`manual_channels`、`subject_facts`、`source_language`、`target_language`。输入结构复用实例输入合同；不会接受旧路线、批准快照、任意字段值或从最新内容补齐。返回 `result_kind=preparation` 的 `target_id` 可经现有 `/execution-preparations/{id}/` 查看；新候选需要新幂等键，不能覆盖已有准备。同键变更报409；取消后重放仅读取同一回执，不恢复运行。

```text
action: adopt
stage_key: script_input
unit_key: project:<当前项目UUID>
scope: {kind: project, id: <当前项目UUID>}
expected_run_plan_sha256: <本轮RunPlan SHA256>
expected_selection_revision: <已读取的采用修订，初次为0>
idempotency_key: <本次动作唯一标识>
payload:
  content: {kind: script_revision, id: <精确修订UUID>, expected_sha256: <正文SHA256>}
  comment: <可为空的人工说明>
```

认证沿平台登录，限当前 Run/项目授权用户或管理员。采用复用 ApprovalDecision，记录真人、时间、摘要，更新本轮所用剧本与该阶段快照；不批准其他阶段、不派发后续模型任务。源未确认/摘要损坏/未知字段/未接入动作400，无权对象404，旧选择修订或同键不同请求409；回执写入失败回滚采用与决策。后续已全部明确停止且未形成产物或审核结果时允许重新采用；已有结果、仍执行或远端未确认时继续阻断，不静默作废下游事实；目标按计划23.3在装配前支持改选及复核，装配已提交后才要求新轮次。原配方 approve/select-route/select-artifact/retry/候选重生成不能绕过人工回执；取消、费用和远端不确定处理继续复用原控制协议。

页面入口：项目生产工作台 → 无配方人工制作 → 开始人工制作；轮次详情可采用选中的剧本，或“选择步骤与执行实例”→选择制作对象→“创建本次执行准备”。创建准备不会派发字段辅助任务或主模型调用，原准备工作台继续处理确认和显式提交；外发授权、预算、Smoke及当前准入由原Run执行链核对。表单失败保留同一请求，换步骤/对象/实例提示清空未提交内容；剧本上下文选择限制到本轮项目。详情返回冻结 `execution_plan`，不从当前配方重建。后续清理与服务切换已有记录；当前版本的页面与真实模型验收须分别核对，不能沿用旧阶段状态。其余制作与schema3接线见[统一计划](optimization/execution-instance-unification-plan.md)。

### 配方配置（schema3 作者入口，2026-09-09 源码状态）

逐镜来源映射：`production-recipes/options/` 返回 `reference_source_options`，每项为 `value/label/kind/scope_kind/stage_key/paired`。配方媒体输入使用 `reviewed_references`，包含 `stage_key=storyboard_review`、`scope={kind:shot}`、有序 `groups=[{source_key,purposes,subject_kinds,purpose?}]`。`purposes`为可选的原创作描述精确筛选：null表示不筛选，否则为非空唯一原文数组（单项最多160字符），不是模型枚举；`subject_kinds`为null或实例允许的非空唯一类别数组。单数`purpose`仅在目标输入有用途合同时必需，必须属于该次调用合同；无合同则不配置此键，不增加用户必填项。模型用途改变不改原创作描述，数量仍严格核对，不默认截断。原自由媒体 `selected_output.output_key` 不再接受；文本/JSON继续使用目录中的selected_output。来源映射在“系统设置→配方→阶段路线→输入来源”配置；人工新用途通过原单镜/多镜选择面板选择素材、原逐镜审核确认。首尾帧/连续性/音画配对不从新用途文字推断，原配方审核已接已保存确切选择及双摘要，已有/上传选定图片经原Preparation到工作流请求编译已获第230批隔离验证，未提交远端，不代表M5模块试用、M6独立字段策略或真实效果验收。

统一媒体输入的配套原音使用`{kind:"video_audio",item_key,purpose,paired_to}`，可附带与父视频相同的scope_ref/order；不得附带asset_id/artifact_id、路径或主体。它只用于映射到Workflow的配对音频输入，paired_to必须指向同请求、合同指定父输入中的具体视频使用，且实测含音轨。原视频仍来自受管Asset或明确已采用产物。来源冻结父使用SHA与视频字节/观测事实，音频逻辑使用的asset_id/artifact_id均为null，不建立伪音频素材。

分类接线原生支持视频音轨时，共用父视频上传与已注册组件音频输出；若还有独立音频文件端口，则沿现有paired_video_audios预处理合同生成任务内受管副本。处理配置、实际字节、配对父视频及上传回执沿原worker/任务持久化；没有相应合同、静音视频、混选外部音频、跨范围/错父视频均拒绝，不按文件名猜测、不新建队列。已有独立音频仍使用asset/selected_output和原paired_to。不选择原音时不提取；读取/预览只核对来源。用户入口为统一输入表单的“使用配对视频的原音”，或配方分镜参考审核的“使用此视频原音”。

原配方参考审核读取已保存的ShotReferenceUse，复用同一来源解析和精确实例合同；approve只保存正式参考快照与审核决定，不修改选择、Bible主图或创建ExecutionPreparation/AI任务。视频/音频按该次调用的槽位和来源映射验证，目标确有用途合同时才配置模型用途；可选创作描述不被推断为枚举或首帧。调用计数仍沿原Run/Preparation分配，重复核对不重复计数。完整选定媒体的准备请求与槽位消费仍待M5/M6，不以读取成功代替。

#### 逐镜映射预览

- 接口：`POST /api/v1/production-runs/{id}/reference-mapping-preview/`；原Run owner/staff登录访问，非所属运行404。页面：配方运行详情→“查看逐镜输入映射”→选择阶段、镜头和候选编号。读原RunPlan、精确实例合同、Stage/ApprovalDecision、Shot与受管Asset；不保存第二份选择事实。分镜待审时页面只开放冻结来源设置的只读查看，禁用正式映射预览，不将未提交参考草稿冒充已批准输入。
- 请求：全部必填 `{stage_run_id:UUID,shot_id:UUID,candidate_number:integer,expected_run_plan_sha256:SHA256}`；候选编号从1开始且不超过冻结variant_count。不接受客户端传入的素材清单、审核状态、模型覆盖或额外字段。固定路线读取唯一配置；人工选择读取已明确选择的路线，未选时400；顺序回退按该单元/候选的Preparation及StageRouteEvent读取实际路线，不在预览中触发回退。尚未创建准备时使用该候选的初始路线。真实镜头来自共用 `GET /api/v1/production-runs/{id}/targets/?kind=shot&page=1`，人工与配方共用分页目标目录。
- 响应：200包含 `schema_version=1,status=inputs_validated,run_plan_sha256,definition_sha256,stage_run_id,shot_id,candidate_number,route_index,execution_instance_version_id,review,inputs,excluded_reference_keys,notice`。route_index为从0开始的冻结路线位置；页面显示从1开始的路线序号。review记录原审核阶段/决定ID、修订和快照摘要；无需参考来源时明确null。inputs逐槽位返回key/kind/minimum/maximum及有序items（名称、精确素材或产物身份、用途、主体、顺序、paired_to）；excluded_reference_keys为未映射引用。空可选槽位items为空；必需空集不能成功。
- 失败：400明确返回未知字段、范围/版本不一致、未配置路线、尚未审核、审核漂移、缺失素材、越限或错误配对原因；不保留旧预览为当前成功。正常轮询返回新对象不重置面板、选择或结果；真实审核/选路变化清除结果并提示，但保留展开和选择。页面换镜头/阶段/计划或读取失败时清除结果，迟到响应（含卸载后）不能覆盖新选择；可重新读取镜头目录并复核。无权运行404，不回退其他运行。
- 副作用：核对使用原输入规范化器（包括本地受管素材完整性），但不创建Preparation/GenerationAttempt，不生成提示词、不分配种子、不外呼、不批准费用、不做音轨提取；响应`private,no-store`。输入预览通过不代表Smoke、预算或完整生产就绪。
- 验证：隔离API、原审核漂移/权限及真实临时PNG规范化已覆盖；前端真实组件动作与迟到响应测试通过。正式服务已有后续切换记录，具体页面/生产验收需单独证据，不把隔离fixture记为真实烟测。

入口为系统设置 → 生产配方 → 新建/编辑草稿；详情使用同一表单的只读视图。作者合同与后续迁移/服务切换已有记录；当前配方须核对精确版本、发布状态和真实组合证据，不因作者入口存在而视为生产就绪。

| 方法与路径 | 用途与响应 | 权限/写入 |
| --- | --- | --- |
| `GET /api/v1/production-recipes/options/` | 返回 `definition_schema_version=3`、阶段/可执行模式/需路线模式、制作方式、阶段审核、精确字段批准策略、来源/范围/参数策略选项、空路线预设、装配有限目标结构`assembly_output_schema`/`assembly_timing_schema`/`assembly_media_schema`及交付`delivery_metadata_schema`及独立 `effective_limits.max_variants/max_routing_routes` | 登录读取，不创建资源或执行任务；不依赖完整生产能力或 ComfyUI conditioning 目录 |
| `POST /api/v1/production-recipes/` | 提交新族 `recipe_key`、`name`、`definition`；201 返回服务端版本号、正文、Schema版本/大小/SHA | staff；同事务保存草稿与原生命周期审计，旧族仍须走后继入口 |
| `GET/PATCH /api/v1/production-recipes/{id}/` | 200 返回精确记录；PATCH 仅草稿，已发布/退役版本不可修改 | 读取沿现有可见范围，写入 staff |
| `POST /api/v1/production-recipes/{id}/publish/` | 校验完整schema3阶段、精确路线、来源及审核闭包后发布；合同不完整返回400且不改变状态，发布不等于Smoke通过 | staff；无模型调用 |

`definition` 只接受 `{schema_version:3, interaction_mode:assisted|unattended, stages:[...]}`。原生业务单次职责仍以`key`标识，保持目录的相对顺序且不可重复；通用文本步骤声明独立唯一`key`、`kind=structured_text`及`name`，可在依赖允许的位置重复插入，同名不合并、改名不改身份。每步声明`mode/approval`，可带独立`decision_policy/field_review/routing`；非模型阶段不得绑定模型路线。通用文本使用`mode=ai/approval=auto`，候选选择由`routing.evaluator`配置，不增加第二次阶段审批；原生阶段审核规则保持不变。无人值守不能等待人工选路/批准或通用候选选择；阶段审核策略与生成字段批准策略不可互换。

装配步骤`key=assembly,mode=existing_pipeline`可另带`assembly_output`，共同字段为`{output_version_id,expected_contract_sha256,mapping}`，来源严格二选一：`source_text`保存按确切结构编写的原始JSON文本；或`source={kind:"selected_output",stage_key,output_key,scope:{kind:"project"},selection_policy:"reviewed_selected"}`引用本配方更早通用文本步骤的确切JSON输出。不可同时提供两种来源。输出版本必须是确切小写UUID及64位小写SHA256对应的已发布`structured_json.v1`；`mapping={schema_version:1,node:...}`复用M1节点/路径作者，将自定义布局投影到服务端`assembly_output_schema`的完整`width/height/fps`正整数。保存/PATCH/发布共用原文解析或上游结构/完整目标映射校验，PATCH在原注册引用写锁内再次核对版本；动态来源还核对原目录的步骤、输出版本、结构版本和摘要，不假定未来结果已存在。未知键、前向引用、陈旧摘要、错误协议/状态、缺项、错误类型或超限返回400字段错误，不保存半套配置、不补项目值、不做生成或采用。未声明该字段时仍沿项目几何首次准备冻结；编码、重试、披露和字体配置没有第二份作者。

同一装配步骤可选声明`assembly_timing`，字段与`assembly_output`相同，也支持原文或确切已选择上游二选一，引用独立的确切输出版本及其字段布局，复用M1解析/映射。目标为`{episodes:[{number,shots:[{number,duration_ms,episode_start_ms,episode_end_ms}]}]}`，完整必填、非空、集镜号为递增唯一正整数；时长是明确正整数毫秒，起止须同时为null（仅时长）或满足原顺序装配的连续区间且差值等于时长。原文保存校验实际结构/类型/时序；动态来源保存时校验结构与映射，阶段准备读取实际已选择结果后再验证值和时序。入队前逐项核对完整本轮集镜范围、顺序、时长和起止；不自动排序/补镜，不修改分镜、素材采用或媒体裁切。未声明时直接使用原本轮分镜时序，不新增必填门禁。素材、采用决定身份及摘要仍由服务端原来源快照读取，不能在时序值中指定替代它们。

页面为上述配方草稿→装配步骤→“装配输出参数”或可选“装配时序核对”→“装配输入来源”，两者复用同一编辑器。人工JSON选择已发布通用输出版本并填写原文和映射；动态来源从“确切上游步骤与输出”目录选择，保留步骤身份而不只按输出版本合并。更换版本保留原文/映射，不猜路径；切换来源先确认，仅清除互斥来源及版本、保留映射供核对。上游移除、合同漂移或读取失败显式保留草稿并阻止保存，恢复目录/重新读取后继续；已发布详情为只读，移除映射或改变职责/模式先确认。

阶段首次准备把确切输出及结构完整合同、原值、显式默认、来源和映射结果保存在原`config_snapshot.assembly_output`/`assembly_timing`/`assembly_media`。动态来源通过原已选择文本reader核对本轮已完成Stage、选择决定修订及产物，直接映射已选择结构值，不重新包装成手填JSON、不重新应用默认；额外保存`selected_source`中的原步骤/决定/产物及合同身份，自动选择不记成人审。映射几何进入原`assembly_configuration`；时序及片段身份与原`input_snapshot.assembly_sources`全量核对后才允许同一原装配队列入队。已冻结阶段使用原输出合同，不查询新版本补值；上游选择仍需有效且与冻结身份/值一致，未完成、无选择、修改决定或原配置/映射重算摘要后不一致均拒绝，不取最新候选替换。人工装配入口仍只接受原文，不隐式绑定配方上游。

可选`assembly_media`共用上述版本/来源/M1映射，目标为`{episodes:[{number,shots:[{number,artifact_id}]}]}`。完整非空集镜列表、递增唯一正整数号和规范UUID须与本轮原采用来源一致；产物可来自原生成候选，或原人工素材采用形成的`adopted_media`，不是Asset ID、名称或路径。映射只是核对明确采用，不创建采用决定、不自动换片或补缺项；摘要、审核修订和Asset来源由原reader提供。schema3支持原文或确切结构化上游，人工装配仍只支持原文；人工预览返回三类`author_schemas`，首次assemble把完整authors、author_contracts与原选片冻结，原命令摘要包含原文，重复键改原文即冲突。省略此可选作者不改变原装配行为，不增加必填门禁。当前开发/加载与完整组合验证边界见详细设计第18节。

两入口媒体选择共用真实候选读取器及原选择 writer：人工采用与配方 `POST /api/v1/production-runs/{id}/select-artifact/` 的新媒体选择都核验归档字节、文件摘要、来源清单、成功主调用及原准备。缺失/损坏文件或来源不符返回400，不写选择决定或成功回执，不触发模型。原键/原正文重放先返回原正式回执，它只证明历史决定，不代表文件刚刚重验；新意图必须重新验证。文本候选及素材依赖选择继续各自已有专用合同，不强行转换为媒体或人工审核。人工与配方最终使用同一 `create_assembly` / AssemblyJob 队列及封存服务；人工素材保留原 Asset/`adopted_media` 身份，没有隐式配方或调用来源。封存前人工母版预览/下载与封存后共用确切母版校验；下载实时重读原产物，读取失败清除旧预览，不以旧签名链接替代。

动态来源的完整映射检查使用同一M1映射器：整对象/列表路径递归核对已声明子字段的类型与必需目标键，目标不允许的额外属性须改为逐字段映射；显式数组节点同时检查目标数量约束，空episodes/shots在保存时即返回400。整数及可空整数字段可映射到可空起止时间，不把JSON Schema的类型联合误判为不同类型，也不把原0/null转换或补成默认。保存检查不保证尚未产生的实际值、可选路径或时序一定有效；实际读取仍执行原字段、数值、完整范围和连续时序校验。

2026-10-05第305批完成动态结构化上游几何/时序作者、正式API及原Stage配置冻结的隔离验证，配套正式共用组件的合成页面验证已通过；第306批复现并修复上述三类静态映射校验缺口，新增正式POST/PATCH拒绝且旧草稿不变、动态整数0/可空值的保存及冻结复读测试通过。不是现用配方页面保存/发布或真实装配验收，305–306后端未现用加载。完整多集组合、候选/已有视频确切来源作者及交付字段结构引用仍待接续，不表示M8-01或M8模块交付。

配方`options.stage_options`提供同源`label/repeatable/candidate_evaluator_options`：通用文本候选选项为`null`人工选择与`first_succeeded`按编号自动选择成功项，单次职责的候选选项目录为空。原配方编辑器支持添加通用职责、维护显示名、保持原业务相对顺序的上下移动和删除步骤；改变已配置职责会先确认并清空该步骤旧路线，不重写下游引用。显示名可重复，系统生成的步骤key在改名、排序及后继复制中保持不变。发布配方仍核对原生产闭环；通用步骤不能代替必要业务writer。无配方人工制作仅创建目录明确提供人工动作的阶段，不附加一个空通用文本步骤。

路线严格为 `{execution_instance_version_id, expected_definition_sha256, input_sources, parameter_policies}`，不再接受 Deployment、Workflow、Binding、临时路径、客户端冻结快照或第三份提示词模板。配置中已提供的版本必须真实、已发布、实例启用且完整定义未漂移；参数使用实例全部约束的交集。未知来源、前向/自身引用、失效版本、越界值返回400，不保存部分修复结果。生产阶段的业务输出协议仍须匹配，不能用视频实例代替 `script.generate`。

作者保存与冻结运行读取共用路线结构校验：版本ID须为目录返回的小写连字符UUID，不接收大写、无连字符、花括号或URN别名，也不在查询时转换后原样保存；定义SHA为64位小写十六进制，input_sources为对象，parameter_policies为列表。routing的mode、正整数variant_count、evaluator与routes同时核对；完整合同必须有路线，fixed只能有一条，草稿明确允许空路线。当前资源可用性、输入/参数合同与配置上限仍在原作者/准入边界检查，读取冻结结构不查询今日配置、不改写历史计划。非法保存返回原400字段错误且不创建配方/准备/运行；冻结内容损坏显式失败，不修补别名。本段不新增HTTP字段；混合变体派发还须满足本节素材绑定与依赖检查。

候选选路读取先核对当前阶段、当前单元内的准备请求键与原冻结意图投影出的请求键一致，且能精确归属冻结路线及正整数候选编号；异常键、编号别名、越界路线或换成另一合法候选/路线的键均显式失败，不能被预期键过滤器漏掉并当作尚未准备而再次分配。仅所选候选加载完整冻结详情并沿原摘要/身份校验，其他单元不扫描，其他候选只投影请求键、不向应用加载完整JSON或作为本次输入来源；其自身读取仍独立报错。检查不修复旧键、不新建选路事件或调用，也不改正常不可变写入保护。沿原准备/派发错误信封返回，不新增端点或请求字段。

同一步的多路线若使用`BusinessOutputVersion`，所有路线必须同时使用相同operation、输出版本ID及合同SHA；相同字段名或相同结构不能替代输出身份，不能混入无输出合同的路线。草稿已填路线和发布共用此校验，错误定位`routing.routes[index].execution_instance_version_id`。原项目文本准备还复核冻结备选定义的身份，不因备选当前退役或运行资源状态而重新选路；实际选中路线仍走原准入。没有业务输出合同的媒体路线不改变原规则。

素材生成阶段（`key=bible_image_generation,mode=existing_pipeline`）可独立声明`variant_routing`，原`routing`代表主图；其他阶段不允许该字段。两组均使用同一`{mode,variant_count,evaluator,routes}`结构与确切实例版本/来源/参数校验，不新增资源组合或执行队列；候选数与选择策略独立，缺省变体配置表示未配置，不继承主图配置。草稿可留空路线，完整合同不可留空。RunPlan原样冻结两组，资源引用、删除影响、执行就绪检查及项目快照确认均扫描两组；同一确切项目输入仍共用原签名回执。M3需求按每个目标角色的候选数合计检查数量上限；没有变体需求时不强制配置变体，有变体需求而缺配置则在准备前报错。角色配置、分别选路、动态变体准备/派发及实际主图输入共用原执行链；按配置选择并登记素材，不能将配置保存或生成选择解释为Bible审核采用。M4整体交付与真实模型效果另列验收。

草稿可缺路线或必需输入；已填内容不能非法。例如以下可保存但不可发布，系统不会补选实例：

```json
{"schema_version":3,"interaction_mode":"assisted","stages":[{"key":"media_generation","mode":"existing_pipeline","approval":"required","routing":{"mode":"fixed","variant_count":1,"evaluator":null,"routes":[]}}]}
```

作者上限来自同一服务端安全配置；配方编辑器不再读取 `ProductionCapability` 作为替代来源。缺失、布尔、非整数及非正上限显示目录错误，不使用前端默认上限；这不改变完整生产能力接口自身的完整性要求。

`input_sources` 以逻辑槽位 key 保存固定文本/JSON、受控上下文或上游已采用输出的规则；规则不是已解析素材事实。实际内容修订、数量、用途与配对仍在生产准备时核对。`parameter_policies` 作者表单开放 `default/locked_value/locked_source`。同一 options 接口的 `parameter_source_options` 返回 `{selector_key,label,scope_kind,type,unit,source_kind,parameter_key,stage_key,description}`；parameter_key/stage_key 限制适用参数/阶段，null 表示不限制。`context_scalar` 登记项目输出宽高（px）、帧率（fps）、默认镜头时长（ms），按原单位读取。`workflow_frames` 仅适用于媒体生成阶段的 video.frames：明确选择已审核镜头时长并按冻结工作流规则换算。自动种子沿实例首次准备规则，不能再配置步骤值/来源，不在作者页面生成。该接口不批准内容、不创建 Preparation/Run、不访问 Provider/ComfyUI。401/403沿原认证/权限信封；资源不存在404，后继摘要/版本冲突409；页面保留失败原因与草稿。

固定来源示例：`{"key":"image.width","policy":"locked_source","source":{"kind":"context_scalar","selector_key":"project.width","scope":{"kind":"project"}}}`。参数 key 必须已由精确实例声明；不能附带 value、对象ID、任意路径或公式。草稿可保存尚未选定的空 selector，完整合同校验必须选定；已填写但未登记的来源即使在草稿也拒绝。首次准备在原事务内一次读取实际本轮项目的登记字段、归属和更新时间，冻结到原 `resolved_source_contracts`，含 schema_version=1、精确 project ID、observed_updated_at 与原始 value，并复用同一参数交集校验；缺值或违反固定值/范围均报错。项目级、逐镜及Bible配方准备共用此入口。重放同一 Preparation 不重读来源；明确创建新候选才读取新值，不锁定之后的项目编辑。

镜头帧数示例：`{"key":"video.frames","policy":"locked_source","source":{"kind":"workflow_frames","selector_key":"shot.duration_ms","scope":{"kind":"shot"}}}`。保存/发布检查精确工作流的 frame_policy 与帧率语义；首次准备复用本轮分镜审核读取器，拒绝未审核、已变更、跨项目或不在本轮范围的镜头。按本次步骤参数解析得到的实际帧率（含固定值、固定来源、默认值或工作流静态事实）与冻结 frame_policy 调用原 aligned_frames，采用最近合法帧数，距离相同时取较低值；不读取时间轴起止位置、不读取项目默认镜头时长代替、不裁剪超出工作流范围的时长。结果仍须满足实例全部参数层交集。冻结来源含审核 ID/修订/摘要、镜头 ID、duration_ms、definition_sha256、workflow_version_id、实际 fps 及来源、frame_policy 与 value；重试不重算。未选择此策略时不隐式派生；人工实例/素材库完整参数输入不受此配方选项影响。

验证：隔离正式保存/详情/发布/后继接口、Schema/摘要一致性、引用保护、非法旧字段及前端只读渲染已覆盖；完整配方创建、RunPlan上游解析、审核、媒体与交付已做隔离串联。实际服务切换和用户线上验收另行记录，草稿校验或隔离通过不等于真实模型已验收。

### 配方控制（schema3）

以下接口使用统一RunPlan及原生产职责服务；配方发布和创建校验完整阶段、精确实例路线、输入映射与审核闭包。无效合同明确阻断，不提供旧schema1/2或独立部署/工作流组合旁路。无配方使用上一节明确的manual来源，不能伪造或隐藏创建配方。

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

制作台“继续 / 检查阶段”使用原`POST /api/v1/production-runs/{id}/continue/`（无新增正文，登录及Run所有权校验，200返回原Run详情）。文本/媒体生成阶段因`stage_validation_failed`受阻时，明确点击继续可沿原准备主路径重新检查修正后的输入或合同；复用已有准备、采用和调用，不重发成功任务，不改RunPlan或原冻结请求。问题仍存在则保留当前真实blocked原因。后台扫描、普通内部推进不会自行恢复；审批、取消、失败调用重试和其他阻断仍用原各自入口，不借Continue跳过。重检可能按原授权推进尚未提交的正常工作，不等于只读刷新。

`select-artifact` 的正文保持 `{artifact_id, material_selection?}`，素材选择保留原确切决定及依赖摘要；必需请求头 `Idempotency-Key` 为1–191字符。同键同Run/调用人/完整正文返回原 `ProductionManualCommand` 回执 `{id, action: "select", created_at, result_kind: "artifact_selection", target_id}`，同键不同内容409；权限先校验，无权对象404，缺键/无效候选400，响应 `Cache-Control: private, no-store`。A→B后重放A仅返回A的历史决定，不回退当前B；新键是新意图，同产物是否产生新决定由原对应writer处理。新命令在运行结束、取消中或装配已冻结时拒绝，已成功原键仍可核对。选择、回执及新增文件受同一事务/文件保护覆盖；自动选择不走人工命令包装，无配方仍使用原manual-actions，不扩展dispatcher范围。

媒体末端的无人值守`artifact_selection`仅按本轮冻结单产物策略执行：每组必须恰有一个通过实际来源/文件校验的成功产物，再沿原选择writer记录`selection_mode=automatic`、`selected_by=null`。运行详情的原`artifact_selection_decisions`返回该事实，下游引用保留同一决定ID/修订；阶段批准另记`actor=recipe_policy`，不借Run所有者充当人工选片人。所有组校验和阶段批准同事务，失败不遗留部分选片，明确重试不再生成媒体。公开`select-artifact`仍只记录实际请求人的人工选择，客户端不能指定选择模式或操作者；人工选择新增稳定请求头与命令回执，不新增接口、Schema或历史回填。

#### 创建回执与采用后的读取恢复

项目生产工作台的配方及无配方创建入口，在首次提交前校验并保留独立的完整请求副本与幂等键；包含嵌套配置、选集及精确目录／修订摘要。进行中不能重复提交。连接中断、408、409、429、服务端错误或身份不一致回执均不能证明未创建，原表单保持锁定；同页“按原请求恢复创建”显式重交相同POST，仍走原创建幂等合同：存在则返回原记录，不存在则创建。因此恢复不是只读查询，配方创建仍可能开始生产；人工创建不调用模型。明确4xx拒绝（排除408／409／429）后可修改输入并使用新键，不将未知结果按拒绝处理。

已获得同项目／同制作方式的有效Run回执时，若详情导航失败，页面保留Run ID，后续只打开已创建的原运行，不再POST。创建与跳转结果绑定原项目、页面读取代次及登录会话；切换运行／项目或卸载后不写回、不跳到旧记录。恢复草稿仅在当前页内保留，不是持久任务或新事实源；离开页面前可通过“查看原项目制作记录”核对真实记录，不应另开创建来试探上次结果。

采用回调与选片回调只重新读取原Run和原项目，不自动再采用、重试或生成。两个读取均成功且身份一致后才恢复规则展示／提示刷新完成；任一失败明确保留“采用已提交，但刷新失败”，通过工作台“重新读取”恢复。跨Run／项目、旧会话及卸载后的迟到结果不能恢复当前审核可用性或覆盖提示。子工作台按原Run（文本审核同时按阶段）区分挂载身份，选片回执还须核对`productionRun`；这些界面保护不取代服务端审核、权限和版本校验。

#### 配方阶段选路

`POST /api/v1/production-runs/{id}/select-route/` 复用原Run所有者/管理员权限和事务；请求为 `{stage_key:string,route_index:integer,appearance_role?:primary|variant}`，索引从0开始，角色缺省为原阶段路线`primary`。`variant`仅适用于上述素材生成阶段并要求明确的`variant_routing`，不继承主图已选路线。分别写原Stage的`selected_route_index`及新增`selected_variant_route_index`；返回的只读字段允许null，表示尚未选择。只读取本轮不可变RunPlan中的schema3路线，不读取旧Stage.routing或当前Recipe覆盖。仅人工选择模式且阶段尚待执行/待确认时可操作；存在任何执行准备或运行已结束时400，不能改写已分配的请求。返回200原Run详情，原StageRouteEvent记录操作人和所选索引，变体事件的unit_key为`stage:<stage_key>:variant`；重复确认同一角色的未开始路线不新增事件，选择未来阶段不代替当前审核。迁移0219只新增可空的变体路线选择列，不回填历史选择或改写RunPlan，尚未迁移现用库。

页面“配方执行路线”同时列出尚未开始的人工选择阶段，必须主动选择，不默认勾选第一项。可以先选择后续媒体路线，再审核上游参考素材；选择后续路线只记录该阶段，不批准、重启或推进正在等待审核的上游。选择当前待选路阶段后，仍由原ProductionRun推进器继续，并重新执行原输入、审核、资源、组合Smoke、费用和次数检查。

固定路线只创建其精确准备。顺序回退只在原主调用明确失败、费用已核对且无产物/不确定提交时，按冻结顺序创建同一单元/候选的新Preparation并记录原失败调用；不把其他成功候选当替代，不修改原种子/请求。输入不完整、待审核、准入阻断、取消或未核对费用不自动换路；回退后的准备重新执行自身准入。原失败路线一旦已有正式回退，不得再派发或重试它；显式重试在尚未回退时仍使用原准备。文本候选审核及媒体阶段汇总按连续选路事件链读取各候选的当前准备，不按最新成功结果推断。

上述接线正在隔离回归；完整Recipe发布/启动仍保留开发闭包门禁，不以局部选路通过宣称全链可用。未修改实际环境，最终在线业务验收由用户执行。

#### 配方阶段的明确失败重试

生产工作台的继续、阶段重试／取消、审批、选路、费用确认、追加Bible候选及管理员消歧共用原Run动作保护：提交绑定项目、运行、页面读取代次和登录会话，返回ID／项目不一致时不接纳。切换上下文或卸载后的迟到成功／失败不得更改新页面，也不刷新新项目；提交前已发起的GET不能覆盖操作回执。项目刷新失败单独说明，不能将已经返回的服务端动作说成未提交。写入回执未能确认时保留原Run事实与人工输入，暂停该运行的公共写动作；用户需点击“重新读取原运行（不重试）”核对原详情和项目，成功读取后重新选择动作，不自动重放原POST、不推断成功或伪造费用。关闭确认框不解除此保护，失败读取保留待核对状态；前端保护不替代服务端权限、状态、精确目标及幂等校验。

`GET /api/v1/production-runs/{id}/` 的 `retry_context` 提供本次确认范围：`expected_stage_id` 与 `expected_attempt_ids`（当前失败主调用 UUID 数组；尚无主调用的阶段为空数组）。原页面“重试当前阶段”复制这一范围，确认后原样提交 `POST /api/v1/production-runs/{id}/retry/`；不再接受空对象请求或由服务端猜测失败范围。鉴权及项目权限沿原 Run 接口，人工制作仍使用其正式人工动作，不借配方重试绕过人工回执。

`GET /api/v1/production-runs/{id}/retry-preview/?expected_stage_id={uuid}&page=1&page_size=20` 是原确认框的只读影响预览，不是新重试命令。必须登录并具有原Run项目访问权；无配方运行、缺失/非法阶段或阶段已变化返回400，越权目标404，越界页404。分页沿平台页大小配置与上限；按创建时间、ID及类型稳定排序，包含当前重试链末端主调用和尚未有主调用的准备，不把辅助成功计作主调用。

响应含 `retry_context`（与详情同源）、`mode=primary_calls|stage_flow|assembly_jobs`、`can_submit/reason/message`、`call_count/preparation_count`，以及标准 `count/next/previous/results`。前端直接解析完整分页封套，不能通过通用解包器只取 `results` 而丢失确认范围、计数和翻页元数据；缺失封套仍明确报错，不靠当前页面补造。模型行给出 `kind=call|preparation`、`id/unit_key/preparation_id/attempt_id/status/error_code` 和 `disposition=retry|skip|attention`、具体说明。成功跳过；明确失败复用原重试检查；取消、未知或需处理的失败给出原因；未提交准备与失败重发分开说明。`can_submit`只是当前原检查结果，不保证资源准入或提交成功；原POST仍在事务中检查状态、完整失败集合及幂等。取消及确认范围内不能重发的失败会阻止整批提交，不静默跳过这些目标创建部分重试。

配方装配的详情、预览与原POST共用确切范围：`expected_attempt_ids=[]`，另带`expected_assembly_job_ids`（完整失败Job UUID集合）及`expected_input_sha256`（Stage冻结输入SHA）。未冻结的只读详情可给空SHA，但不能提交重试；非装配阶段拒绝这些装配字段。装配预览按集号、Link ID稳定分页，`mode=assembly_jobs`和`assembly_count`表示当前逐集任务数；行`kind=assembly/id`指原Job，`preparation_id/attempt_id`均为null，另给`episode_number/consumer_input_sha256/error_message`。成功行skip，在途或未确认终态行attention、不进入重试集合。消费来源未记录、Job/集/项目/所有者不匹配、试算任务、冻结来源漂移或当前Link未覆盖完整冻结剧集均阻止提交，不补造历史。

装配POST在原Run/Stage与Project锁之后锁定任务，复验原失败集合、消费输入及原选片来源；按原装配器整批创建后继，不重跑成功及在途集、不顺带补未确认的缺失集、不生成镜头媒体。任一创建、后续阶段或事务提交失败时回滚新Job/Link/source_items，并删除仅本次新写的concat清单，旧任务与文件保留。原请求已经替换Job后再次提交会因目标变化返回400，即使其后继再次失败也不能重试新目标；必须先GET原Run和任务中心确认结果，再重新预览明确的新失败集合。沿用原鉴权/404与校验/400，不新增队列或假审核。

对于配方Bible图片阶段，`retry_context`只含同单元当前候选全部failed且尚未明确履约的主调用；已有成功或仍在执行的候选时，该单元可核对的失败同伴显示skip并保留原记录，取消、结果未知或归档不完整的同伴仍显示attention并阻止提交。这个范围与同伴安全检查由共用实现供详情、预览及POST使用，资源提醒/授权状态变化不会扩大范围。`waiting_material_recovery`允许Run awaiting_input/Stage running进入原明确重试；其他普通等待输入状态不因此开放重发。失败集合、费用或提交结果变更会在POST复查并拒绝；不会因为预览显示可提交而放松原安全条件。

模型预览只查询窄列与产物存在性，不读模型请求/响应正文；装配预览读取原冻结合同核验消费来源，不加写锁、不访问模型或媒体探测、不创建任务、不审核或选择产物。页面打开、刷新、翻页仅GET，旧响应或确认范围变化不能放开提交；具体任务通过原任务中心查看。没有模型主调用的其他阶段仍进入原阶段流程，不由此预览推断所有下游动作成功。M7-09／验收场景A01已用完整ProductionRunView、正式API及原worker在隔离SQLite中验证成功/失败混合、丢回执只回读、两页旧范围拒绝和重新确认后继；后端两页预览的只读SQL及旧目标重放也已覆盖。桌面详情关闭重开、390px整页重载通过；浏览器本轮预览仅一页，不冒称其动态多页组合已测。协议响应为隔离合成，不是现用业务提交、真实远端恢复或整个生产运行交付验收；装配新增验收与正式加载状态见唯一详细设计第18节。

主调用重试保留同一 Preparation、执行实例、路线、冻结提示词、素材顺序及参数；只增加 `GenerationAttempt`，以唯一 `retry_of` 引用原失败调用。配方恢复由原worker按Run冻结的 `production.task_max_attempts`（总次数含首次）及 `execution.preparation.recheck_interval_seconds` 最小等待间隔执行，主请求与辅助批次分别计数，不按阶段ordinal推断次数。`retry_requested_by` 表示派发授权身份，自动重试使用Run所有者，不代表该用户当时点击或人工审核。重复扫描／同一失败目标命令重放均复用原后继；手动重试后继失败时须重新预览并确认新目标，不能重复旧命令扩大范围。

运行/阶段原因 `execution_retry_wait`、`execution_retry_running` 显示次数和等待状态；耗尽使用 `execution_retry_exhausted`，不能安全重发使用 `execution_retry_requires_action`，Run为awaiting_input、Stage为blocked。未知远端及费用待核对保留原原因码和消歧入口；准入阻断也保留真实原因。辅助耗尽时主准备awaiting_review，阶段使用 `execution_preparation_review_required` 等待原准备处理；辅助显式重试可恢复原Run，不重做成功批次。装配最终失败使用原 `assembly_failed/assembly_partial_failure` 原因码，Run转awaiting_input，原确切范围重试接口继续可用。

已回退的旧路线、成功任务与已履约素材不进入新重试集合，阶段结果读取明确路线及重试链末端。本地明确失败、无产物且预留已释放不要求虚构账单。生产文本主调用及字段辅助的`execution_request_invalid`若明确结束、从未开始提交、无提交登记/远端ID/状态/领取/媒体任务/响应记录/结果/字段值/产物且预留为0，允许按原次数与准入重试，不把未知实际费用改成0；外部已提交调用仍要求已核对实际费用。原任务`error_message`显示提交前失败环节及脱敏校验原因（超过2000字符显式说明仅显示前段），不生成虚假模型响应记录，也不重写历史缺失原因。已返回待修正正文、未知结果、取消与归档失败不自动重发。状态读取/预览本身不写入、不派发；恢复逻辑仅由原推进与派发写入口执行。

每次新增 Attempt 重新检查原 Run 的累计用量、当前系统资源提醒策略、实例与执行准入。超额准备进入 awaiting_review，经具体请求的资源确认后另行继续；不是终态失败，也不会自动重发。原 `authorize-cost` 保留外发授权与未知费用核对，并可处理历史预算阻断记录；不再修改 Run 预算。历史失败目标仍须明确重试。未知费用、可能已提交、已有产物或未确认的媒体失败不能普通重发，应先核对/恢复原任务。异步媒体的提交幂等键由持久 Attempt ID 确定：同次尝试重放稳定，明确重试使用新键；只有投递标识变化，Preparation 的业务请求及 SHA 不变。

成功返回 `200` 原 Run 详情；当前准入阻断也返回真实 blocked 状态及原因，不代表已经重发。缺少字段、重复/越界目标、状态变化或无法安全重试返回 `400`；无权或不存在对象沿原接口 `404`。原远端消歧冲突仍使用 `409`。重试只落原持久队列，不在 HTTP 请求内直接调用模型。该项回归使用隔离协议响应；后续已记录服务切换，当前真实失败恢复专项仍需验收。

新的短剧生产必须从 collection POST 创建，并按服务端返回的当前 gate/action 推进。不得从 task、assembly、release、素材生成任务或已删除的旧生成路由发起旁路生产。

`GET /api/v1/capabilities/production/` 返回当前队列计数及 `production.recommended_max_pending`、`production.recommended_max_active_per_user` 两个配置建议值。其 `queue_guidance_enforcement` 固定为 `advisory`；超出建议值时只在 `queue_advisories[]` 返回结构化 warning，新执行实例调用的准入记录按本次Preparation留痕，不写Task的旧capability_snapshot，不返回 409，也不要求确认。响应的 `effective_limits` 同时投影当前 active system Revision 中已经实际生效的 route、集数、每集镜头、总镜头、生成任务、参考绑定、Bible 候选/绑定/重生成实体、对白、执行监控、提示词审核快照和审计说明安全上限；客户端应以该投影生成提示，不得内置同名上限。`creation_defaults` 则唯一投影 `production.project_creation_policy` 的项目尺寸、FPS 与新运行集数/镜数/时长/生成任务默认值；缺失或非法时能力接口返回 409，客户端不得回退本地默认值。外发授权、运行主机实际并发和执行完整性仍是独立约束；业务预算/次数由系统资源策略提供，超额进入人工确认，Run历史上限不再限制执行。

项目不保存默认视频输入模式。`POST/PATCH /projects/` 的 `production_settings` 若含 `default_conditioning_mode`，返回400 `invalid`，沿统一错误信封的 `error.details.production_settings` 说明应选择执行实例；不静默丢弃新写入的旧字段。`GET /capabilities/production/` 不返回视频目录及默认模式；独立 `/capabilities/video-conditioning/` 返回 `conditioning_modes=[]` 时是合法的空目录，不阻止创建项目。`POST/PATCH /shots/` 的视频规划允许 `conditioning_mode` 留空，不从项目或目录首项补值；拆稿也不写入模式。仅明确选择的执行实例合同决定实际生成模式和素材校验，历史任务冻结事实不变。页面入口为新建连续剧、分镜规划和生产工作台。

### 项目创建规格（轻量只读投影）

- 入口：新建连续剧；`GET /api/v1/capabilities/project-creation/`，需要登录，无请求参数、无写入、无模型调用。
- 返回：`creation_defaults`（与完整制作能力接口使用同一个创建策略投影，含语言、画幅选项及对应宽高、计划默认值）和 `configuration_public_sha256`（此公开投影的内容指纹）。数据唯一来源仍为当前生效的 ConfigRevision；不复制规则或增加缓存事实源。
- 错误：未登录401；必需配置缺失或非法409，返回 `code/detail/missing_keys`，不返回兜底尺寸。
- 性能边界：不读取任务历史、不扫描执行实例、不做工作流核验。完整制作能力接口继续服务生产工作台；实际提交与 Preparation 仍执行原有完整合同校验。
- 页面立即展示表单；风格和规格并行加载、分别重试，配方目录仅在选择配方制作时读取。风格需明确选择；画幅决定保存的项目目标尺寸，不提供不生效的“分辨率档位”。实际模型输出仍以执行实例合同为准，不能把项目目标尺寸当作模型能力证明。

`GET /api/v1/capabilities/video-conditioning/` 独立只读返回 `conditioning_modes` 和 `conditioning_contract_sha256`。条件输入模式从当前用户可见、启用且已发布的视频执行实例合同读取：先按服务端能力目录中的视频操作、工作流声明的视频产物或无工作流部署的视频模型范围选择候选，再完整核验精确冻结定义与当前资源，只合并实例实际选择的视频模式。任一候选核验失败或同名模式合同冲突仍返回409 `production_conditioning_contract_invalid`，不静默跳过。与视频无关的文本／图片／音频实例不属于本目录的依赖，不因其缺失输出合同或发生漂移而阻断视频规划；这些实例自身的发布、配方引用和提交校验不变。无视频候选返回合法空目录，不回退历史 Smoke 或裸部署扩张模式。目录可见不等于该组合已通过烟测；本次输入、外发授权、预算和证据准入仍在 Preparation 检查。查询沿既有可见性和生命周期规则，无配置、任务、审核或费用写入，无外呼；请求内复用安全配置解析，不跨请求缓存模式。通用 `/capabilities/production/` 只提供生产配置、限制和队列信息，不再依赖该目录；原响应不保留模式字段别名。新 Run、分镜参考审核、Preparation、finalize 和 dispatch 仍各自校验确切 Route/实例，目录解耦不放宽执行准入。系列工作台仅在 Bible 页读取视频目录；失败保持错误与未知镜头计数，可重试且不阻止编辑 Bible，不回退空目录或旧计数。

该目录接口无请求体或项目参数，要求已登录用户（沿现有认证）；200响应使用唯一OpenAPI组件`VideoConditioningCapability`，无候选时`conditioning_modes`为空但摘要仍有效。鉴权失败沿统一认证错误，目录核验失败返回上述409；读取不改变任何配置、运行或审核。代码入口为`studio/capability_views.py::VideoConditioningCapabilityView`，正式UI为项目工作台的一致性Bible；目录错误与通用生产配置失败分开显示。第302批鉴权、空目录、错误、只读和精确Route拒绝的隔离验证与现用加载证据见[唯一详细设计18.3](optimization/records/cross-module/undated.md#183-文档交付检查)。

创建模式包括：

- existing project：不提交剧本输入，只允许已有 Episode 的项目进入下游生产；
- imported script：先通过 `ScriptSource → ScriptRevision → confirm` 固化用户导入原文，再用含 `script_input/confirmed_revision` 的 published recipe 创建运行；recipe 可在其后声明 `script_structure/ai → script_review/review(required)`，把非规范原文结构化为可解析的分集分镜候选；
- AI script：故事输入 → 剧本候选 → 正式审核 → Bible/分镜 → 媒体与交付。两种制作方式统一使用schema3的`interaction_mode`，不保留schema1/2入口。完整且通过合同校验的已发布配方可创建运行；真实调用仍受本轮准备、外发授权、费用及准入检查约束。

完整配方中的文本候选审核必须有同一冻结计划中在先的对应生成/结构化阶段；已确认导入原稿不能被当作尚不存在的AI候选重复审核。计划采用自动拆稿且项目已有剧集/分镜时，创建前即拒绝（`existing_storyboard_requires_manual_resolution`），不创建空跑任务；请使用明确的人工分镜计划或新项目。真正物化前仍沿同一规则复核，防止创建后并发新增内容导致覆盖。幂等重放沿原已冻结计划读取，不因此重新检查后续已生成的剧集并改写原运行。

新建连续剧页面先选择“无配方人工制作 / 配方生产”，再选择剧本来源，复用上述正式 API，不新增旁路。两种方式均先创建项目，再创建真实 ProductionRun。配方导入先保存并确认原文，提交 `script_revision_id`；配方生成剧本提交 `idea` 等参数。配方版本须与来源匹配，创建后进入返回的精确运行，由原服务端链路自动推进；遇到审核、输入、授权、预算、选路、选片或异常门禁时暂停。无配方使用 `manual-options` 返回的目录和 SHA 创建项目范围的人工运行；可空白起步，或把已确认导入修订作为 `manual_plan.initial_content`，但初始内容不代替本轮采用与审核，创建不会自动调用模型。

项目与运行不是一个跨请求原子事务：后续失败保留已创建项目并明确提示部分完成。同页运行提交结果未知时保留原幂等键和完整请求，重试不重新创建项目或导入；项目创建响应未知时停止盲目重建，提示先到项目列表核对。页面刷新不承诺恢复尚未完成的客户端表单。生产页面 `?view=list` 明确列出制作记录，`?view=new&source=manual|recipe` 明确建立独立新轮次，两者不自动恢复旧运行；精确运行页面返回项目时携带 `?run=UUID`。

项目“本轮制作”读取所选运行详情及其精确 `script_revision`、`recipe_version`，只读显示已采用剧本和配方版本，不从空白 `master_script` 回退或另存母稿。多轮无法唯一确定时要求选择。项目 Bible/剧集仍表示当前编辑内容，历史输入以运行和任务冻结事实为准。分镜执行详情复用 `/tasks/?project=UUID&unit_key=shot:UUID`、任务详情与 `input-prompt`，按任务展示人工/策略模式、完整通道、素材端口和参数；不把人工通道写回为字段译文。Bible 的已绑定参考素材与本项目生成候选历史分别显示，候选为空不代表没有可用素材。页面复用既有 API，没有新增业务字段。

配方创建事务提交后唤醒原 `advance_production_run`；文本候选提交后同样唤醒。平台维护 worker 定期检查 `plan_source=recipe` 且 `status=queued/running` 的运行，恢复重启或丢失回调后的推进，不要求浏览器持续打开或重复调用 continue。后台入口在原注册表锁、Run 行锁内再次核对模式/状态；待输入、待审核、失败、终态与人工运行不被自动恢复。阻断仅有一项基于已落库执行事实的恢复：当前Stage与Run同为`uncertain_remote_response`，原ComfyUI主调用已按同一Attempt/token恢复跟踪或已成功归档，且本阶段无其他当前未决/取消调用时，原恢复事务同步解除这一失效阻断，再交原调度器检查完整流程。维护worker用相同规则补查遗漏，不在GET或页面中伪装修复，不重发请求、增加尝试、改写冻结合同或批准记录；其他阻断保持原状态。冻结合同在阶段开始前损坏时记录 `production_auto_advance_failed`，保留原任务，不自动重试模型。0171 仅新增 `(plan_source,status,id)` 查询索引，不新增队列或转换业务数据。POST 回执仍是创建事实；后续状态由原详情/阶段接口读取，不把 HTTP 201 视为生产完成。

schema3无人值守阶段必须显式选择匹配职责的`decision_policy`；生成字段的`field_review`独立引用规则key/version，并保留配置冻结时的implementation SHA供诊断；字段批准兼容只匹配key/version，不以代码摘要变化阻断。自动审核不伪造人工签名，不替代实际候选与媒体验收。生产职责沿RunPlan、Preparation及原队列执行；发布时验证完整路线与自动审核策略，不从旧Deployment路线推导执行实例。

无人值守审核写入 `ApprovalDecision.actor=recipe_policy`、注册 policy 及其 hash，`decided_by` 必须为空；人工 approve action 不能写入该运行。脚本、文本 Bible、提示词标准化、Bible 图片、逐镜参考绑定和单候选产物只有在各自冻结事实满足策略时才自动推进。`bible_image_review` 使用 `select_single_bible_candidate_per_entity_v1`，要求每个实体恰好存在一个来自本 Run、本生成阶段的当前模型候选；策略会把该候选物化为受管 Asset 并设为实体锚点，不会在无人值守状态下猜选素材库图片或接受人工追加生成。`storyboard_review` 使用 `bind_reviewed_shot_references_by_instance_v3`：按冻结来源/选择规则应用明确的本轮结果，缺失或歧义转waiting_shot_reference_selection；保留已有明确选择，随后以共同只读审核核对有序ShotReferenceUse及确切设定/版本/文件。创作描述可空，模型用途仅由本次调用合同/映射确定，不推断实体主图、首帧或广播素材。是否需要素材及其数量、类型、用途和端口由本轮精确实例与配方映射校验；合同允许无参考素材时可审核空清单，缺少必需素材则明确失败。来源漂移、缺失/损坏选择、实例停用或不符合输入合同不会写入成功审核；完整配方启动仍以整链门禁为准。字段生成与翻译的自动批准使用执行准备冻结的字段审核策略，不另存目标提示词翻译审批。所有采用结果须核对完整批次、来源及真实调用；预算与次数超额保留工作并等待资源确认；策略异常、费用未知、外发未授权、远端不确定或媒体校验失败仍显式保持阻断或不确定状态，不会回退人工身份、旧素材或模拟成功。

`script.structure` 在统一实例链使用冻结的完整文本或精确已确认修订，不允许截断后外发。生成请求由本轮 RunPlan 的精确实例版本、显式策略及共享 Preparation 编译，再交原 GenerationAttempt/text worker；不再维护基于规范文本的 no-op 候选、隐式 active Binding 或低层 route 分支。返回仍须覆盖完整来源并通过原严格 IR 校验，写入原 preview revision；审核采用前不改当前正式剧本。

结构化计划在模型调用前检查原稿中已有的声明协议。不支持作为再结构化来源的 `SCRIPT_BIBLE_V*` 返回校验错误 `script_bible_schema_unsupported`，不得把它当“无Bible的普通原文”转写或丢弃变体字段。原生生成v2/结构化v3已接入输出`SCRIPT_BIBLE_V2`，原模型版本编辑器可编辑后继声明规则，但Bible物化/分镜及主图依赖仍在开发，尚不能发布为完整可生产链。已有V2结果应经原来源读取和审核使用，不应通过删除声明、回退旧版本或盲目重新结构化绕过。此校验不修改导入原文、已冻结请求或审核事实。

运行会固化 project、content、configuration、recipe、workflow、route 和 cost 相关快照。后续项目配置变化不回写既有 run。

`POST /api/v1/production-runs/{id}/approve/` 批准 `script_review` 或 `visual_rules_review` 时必须提交 `text_candidate: {artifact_id, expected_sha256}` 及生成阶段的 `expected_source_snapshot_sha256`。服务端核对冻结候选清单、成功主调用、原产物内容及摘要，不默认选第一条。剧本采用只更新所选剧本修订与项目母稿，不回写项目标题或梗概，不提供元数据采用开关；未知请求字段返回400。候选自定义字段原样保留，审核页面可按通用字段查看或编辑，不要求固定title/logline。仅剧本批准可额外提交script_content另存人工修订后采用，规则见“脚本与内容事实”；省略则采用原候选。剧本比较基线schema_version=2只冻结项目母稿摘要与本轮已采用剧本修订，归属每次Preparation，重放不刷新；标题、梗概的人工编辑不影响剧本采用，母稿或本轮已采用修订变化则阻断。旧基线不转换、不静默兼容，须使用新准备。影视规则必须提供覆盖原输出版本全部所选字段的已复核 `candidate_overrides`（基础三项，以及启用时的镜头指导）。拒绝或其他阶段不接受 text_candidate；该动作同事务复用原 ScriptRevision 确认、项目字段写入、ArtifactSelectionDecision 与 ApprovalDecision，失败整体回滚。页面入口为项目工作台 → 当前审核阶段 → 分页选择候选 → 查看正文/核对字段 → 批准采用。无候选显示空态，读取/摘要漂移显示错误，不补默认值。

schema3配方在自身路线冻结执行实例版本、摘要、输入来源及参数策略。策略、Workflow、Projector由该实例版本固定；不再要求调用方提交另一份`workflow_selection`或重新挑选Binding。生产页和新项目引导共用只读执行配置摘要，显示逐阶段精确实例ID/SHA、输入映射键及参数策略数量，不代表准入或Smoke通过。请求仅提交配方版本及正式剧本/范围/授权参数；旧独立资源选择和顶层参考素材数组均作为未知字段拒绝。创建时校验完整冻结计划，具体执行失败及恢复动作由当前阶段返回。

通用结构化文本的G03选择服务已接原`POST /api/v1/production-runs/{id}/select-artifact/`，请求仍为`{artifact_id}`，必须携带1–191字符的`Idempotency-Key`，成功返回原ProductionManualCommand五字段回执（id/action/created_at/result_kind/target_id），target_id为选择决定而非产物；当前选择须另行GET。限当前项目授权用户；错误运行/步骤/非当前路线/无效产物400，无权对象404。显式选择复用ArtifactSelectionDecision，`selection_mode=manual`并记录真人；同键原正文重放不增修订；新键按原选择writer处理新意图，不发模型、不创建业务实体、不自动推进下一阶段。原生剧本和影视规则仍走上面的审核入口，不能借通用选片跳过。

通用步骤的自动选择由冻结`routing.evaluator=first_succeeded`驱动，按候选编号取当前有效成功项，不按完成时间、调用序号或质量排名；部分候选失败不阻止选择，其失败事实仍保留。`evaluator=null`等待明确人工选择，阶段处理器返回`execution_candidate_selection_required`，原Run调度器据此暂停为awaiting_input。选择后沿原阶段输出快照完成本步；自动再检查不替换既有选择、迟到结果不改选、下游已开始不能改写其输入。原阶段详情中的选择记录新增只读`selection_mode=manual|automatic`；自动时`selected_by=null`，原人工记录不改为自动。0210迁移只增加模式列、允许自动操作者为空并约束二者一致。正式可重复目录、原顺序调度与配方配置组件已接入；通用结果字段页已接原阶段详情与Artifact读取/选择；**G04动态下游来源仍待接通**。当前验证使用隔离数据库、存储配方/提交准入夹具与假Provider，不代表完整配方已发布、现用服务已切换或真实生产通过。

配方作者页面读取精确实例的`GET /api/v1/model-execution-instance-versions/{id}/input-contract/`。旧`media-route-options/`路由、资源组合DTO及前端选择器已删除，不再返回空目录、兼容映射或独立策略选项。实际素材由每条执行路线声明的输入来源在对应步骤解析，不从当前active/priority绑定或旧Run根素材数组补齐。

阶段顺序、执行模式与无人值守策略以 production-stage-catalog 的当前版本为准；新配方拒绝独立规范化生成/审核阶段，不为旧配方增加兼容映射。对应媒体提示词由本次执行准备统一负责，媒体生成、选片、装配与交付沿同一本轮计划执行，不另建旧路线旁路。

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

模型候选物化为 Asset 时，`name`、`description`分别取当前已核对实体的名称和基础描述，名称须非空，描述允许明确为空；`style`取自项目，`source_info`单独记录ProductionRun与候选编号。`prompt_hint`保持空值，不复制Bible共同身份或主图正文，不用来源说明补造素材内容。生成依据沿Asset→GeneratedArtifact→原Preparation的确切目标引用核对，主图修订变化后旧候选不可采用，原请求、设定修订及已生成图片保留。此写者不转换或补填旧素材元数据；不适配开发数据的实际清理另按已核定范围执行。 首次入库仍复制到既有租户Asset存储并二次核验字节，失败随外层事务和文件保护器回滚新记录/新文件，保留原归档。配方登记与原Bible采用共用该写者；已登记候选再次使用或采用时核对原Asset，不另复制一份。

`POST /api/v1/production-runs/{id}/regenerate-bible-images/` 只允许项目所有者或 staff 在配方人工/辅助运行处于 `bible_image_review/awaiting_approval`、且尚无审核决定时调用。它使用本RunPlan已冻结的Bible执行实例版本及来源/参数规则，为指定实体各创建一个新候选的ExecutionPreparation；已有候选、已物化Asset与正式Bible绑定都不会覆盖。不在此动作重新组合模型、策略或Workflow。请求为：

```json
{
  "request_id": "本次操作的 UUID",
  "expected_source_snapshot_sha256": "页面当前候选快照的 64 位小写 SHA-256",
  "entities": [
    { "entity_type": "character", "entity_id": "实体 UUID" }
  ]
}
```

`entities`为至少一个不重复的character/scene/prop实体；请求实体数和每实体累计候选数分别使用配置`security.production.max_bible_regeneration_entities_per_request`、`security.production.max_bible_candidates_per_entity`。成功返回更新后的ProductionRun详情，表示新准备已记录，不表示已调用模型或生成成功。同一request_id与同一请求可安全重放；同一ID改变请求内容返回409 `bible_regeneration_idempotency_conflict`。页面快照、候选、实体来源或状态变化返回明确409；在途/待消歧调用阻止重复追加。新准备与首次生成使用同一Run任务/费用/外发准入，费用未知保持阻断，需通过正式价格配置处理，不继承旧动作的未知费用估算。全自动配方禁止注入人工追加请求；无配方制作使用自身正式动作，不调用此配方接口。

追加候选在原 Preparation 首次创建事务内，对协议已登记的种子字段（ComfyUI 的 `sampling.seed`、其他受支持协议的 `seed`，以及显式声明种子策略的参数）施加 `generate_once` 请求层。普通默认种子可被替代，范围、枚举、对齐、`fixed`、`const` 和步骤锁定值不放宽；合同缺少有限取值域、锁定或合法值已用尽时返回校验失败，不创建调用。服务端从合法集合均匀抽取，排除同一生成阶段、同一实体已有准备中已冻结的种子。原准备摘要、排除值、实际种子及来源随 `resolved_inputs.new_candidate_seed_basis` 和原 `parameters.generated_values/contracts` 冻结。客户端不能传入种子或排除列表；重复动作、刷新、恢复和失败重试只读原准备，不再次抽取。首次候选仍遵守原声明策略；不支持种子的实例不虚构种子，新种子也不保证画面差异。历史准备与已发布定义不改写。

追加候选时，仅将尚无审核决定的`bible_image_review`输入/输出快照恢复到未冻结状态（SHA为空），保留配置合同与原候选。新生成阶段完成后，以同一RunPlan及全部前置阶段输出重新冻结审核输入；未完成前置或其他已冻结合同变化仍明确拒绝，不以空对象摘要作为有效冻结输入，也不通过重置清除已产生的审核决定。

`storyboard_review`是配方媒体生成前的确切选择审核门。人工先读取已保存选择，只提交所见的上游/选择双摘要；自动策略以同一读取器核对独立用途、原版本/Asset/文件和该次实例输入映射，不猜实体主图。合同允许无参考素材时保留空清单，不增加逐镜人选门槛。整批验证及原正式快照/ApprovalDecision同事务，失败不部分批准；人工选择仅由原单镜命令完成，批量控件逐镜复用同一命令。下游准备/工作流仍按相同身份、字节和有序映射核验，第230批已验证已有/上传选定图的原准备与请求编译，真实提交和效果未验。创作描述不等于模型级内容隔离，本期不提供参考视频截取。

页面按集/镜头折叠查看，仅初始化完全空白的绑定草稿时，从该冻结审核快照为镜头明确关联的人物/场景/道具选取唯一已批准主图；已有绑定（含明确空列表）原样水合，缺少或歧义不猜选。初始化不在GET中写库，也不产生批准；用户改选/取消在同一快照的轮询与素材刷新中保留，只有原approve事务成功后成为正式引用。图片选项同时核对实体范围、批准身份和摘要/MIME，失效选择显式保留并阻止审核，不静默替换。

Bible 图片阶段同样引用输出为图片的精确执行实例，提示词策略及工作流来自该实例版本，不能在Run创建时另传Bible Binding。实体、素材及参数由阶段路线声明并在本次Preparation冻结；Bible实体接线与批量审核仍在P4迁移，不将旧PromptArtifact/Binding快照读成新执行配置。

参考图审核候选快照用`target_content_sha256`表示实体创作内容，候选列出`preparation_id`、`execution_instance_version_id`、`request_sha256`、`manifest_sha256`及自身`target_snapshot_sha256`；策略版本可以明确为null（人工提示词），不通过旧Binding或variant_group名称推断目标。`eligibility=eligible`表示已核验可供审核，不是复制候选的可变生命周期状态。多选逐项指定候选/素材库图片和唯一主图，数量边界复用服务端同一配置。参考图入库来源schema3只记录原生成Run、候选、Preparation、实例、请求摘要、调用及归档字节事实，不含adoption_run_id/review_stage_id；原bible_reference_image生成用途标记不表示已人审。实际审核/采用仍由原决定和步骤输出记录，人工与配方消费者核对同一生成来源。不转换或兼容旧schema2素材；现用数据切换另按R06核定，登记不等于采用。

`GET /api/v1/projects/{id}/bible-image-candidates/` 是只读历史投影，沿登录态和项目owner/staff访问控制，返回`private,no-store`。每个实体含当前主图/参考图、候选历史和最近候选；空历史为[]/null。候选返回artifact/run/stage/attempt/task、`preparation_id`、`execution_instance_version_id/name`、`request_sha256`、精确模型/工作流/策略版本和`target_snapshot_sha256`，不再返回旧PromptArtifact、Binding或来源快照别名。目标来自真实准备的production_unit/bible_target，不从单元名称或旧图推断。`source_current`比较当时名称/描述及确切主图设定引用与当前内容；主图设定新增或升版会改变该状态，同一修订的后续审核、旧重复身份文字和主图图片关系本身不会。原冻结目标仍验证其确切旧修订，不以当前修订替换；错误引用或损坏当前设定返回明确错误。审核写回另需核对完整关系及上游审核基线。

文件损坏、迟到结果或候选不可用时`integrity_status=invalid`、错误码明确、预览/下载URL为空；客户端不得使用Run artifact的旧URL绕过。目标/准备不可归属、目标已删除或素材关系越界返回409，不伪造空历史；无权项目404。该GET只读取真实数据库与归档文件，不创建、采用、入库、修复或调用模型，来源/完整性正常也不代表已审核。项目页只读历史展示执行实例和准备身份；多图/唯一主图审核及写回仍在P4接线，源码/API隔离验证不代表在线服务已切换。

`generation-attempts` 是按项目租户（`production_run.project.owner`）隔离的只读安全投影；`ProductionRun.owner` 仅表示发起操作的 actor/audit 身份。管理员可在 ProductionRun 被 `uncertain_remote_response` 或 `actual_cost_reconciliation_required` 阻断时，把投影中的 `attempt_id` 提交给 `resolve-uncertain`。响应只包含 request/result/provider request id 的 hash、管理员可见的短 suffix 和状态/费用元数据，不返回原始 provider request id、请求 payload、响应正文或错误正文。人工消歧只追加不可变 resolution；`confirmed_remote_submitted` 仅恢复异步媒体轮询，其他结论也不得展示为生成或 smoke 通过。

## 脚本与内容事实

候选字段编辑复用原剧本审核入口与通用字段编辑器。配方approve或人工manual-actions的script_review/approve可选提交`script_content`完整JSON，按原冻结合同校验并在采用事务另存人工修订；原模型返回不变，省略则采用原文。编辑稿的origin_generation_attempt为空，原审核快照通过edited_from_revision_id连接原候选修订，不新增业务表或队列。原项目标题和梗概不回写。

草稿明确采用前不保存；内容与原文完全相同不创建假编辑修订。其他阶段/拒绝动作不接受script_content，非文本/null或结构/关系错误400，来源/母稿变化409；错误包含字段路径且数据库写入整体回滚。人工同命令重放200不重复另存，网络结果未知保留同请求核对。权限、成功状态及原调用入口不变；现用迁移/真实模型与Bible下游尚未因此验收。

原剧本候选采用证据新增服务端生成的`structure_source`：冻结已确认修订引用、结构化详情SHA、输出版本ID/版本/SHA和映射配置SHA；客户端不提交或选择另一份来源。原候选、人工编辑稿、人工/配方及配置自动采用共用同一writer，失败回滚原事务；没有确切输出合同不再退回旧声明读取。人工导入采用沿原manual-actions请求/回执执行，原script_input只读输出快照改为schema_version=4，包含schema_version、content和structure_source；配方确认输入的原Artifact清单改为schema_version=2，保留原修订身份并包含同一可选structure_source。无详情的纯文本允许采用，来源明确为null，按需请求结构化输入时返回script_structure_required；已有详情损坏不能当成纯文本采用。无新HTTP动作，完整结构化导出是内部按需读取，不扩充普通列表/选项。配方读取/接续必须有原输入产物，不能补造；输入执行和首次明确批准仍在原事务创建。配方完成审核/接续校验同一来源，缺失或漂移明确失败，不按当前输出配置重新解析，不把新修订当作旧依据。导入/人工修订不要求模型调用；新增预览不影响旧引用，旧修订被后续确认替代时依原状态明确失败。不兼容旧采用快照或旧清单，不改写现存业务事实；无新数据表或迁移。原已采用结构检查已复用此出口，区分已保存、尚无结构与实际数据错误；只统计原语义声明，不再按旧文本格式判断。Bible来源/审核/物化和内部只读分镜预检已直接消费同一保存语义。配方读者核对声明的前序成功script_review或script_input及其原输出回执，读取不补造缺失产物；人工读者仍以本轮确切采用决定为依据。相同修订重复Bible接续不复制对象或覆盖人工修改；跨修订身份与逐源确切设定引用已按M3-01接通原物化/审核/预检；分镜Episode/Shot、确切结构来源及类型化关系已接通共同事务writer；素材绑定、字段编辑/策略等后续工作仍不代表完整下游验收。

- `/api/v1/script-sources/`
- `/api/v1/script-revisions/`
- `POST /api/v1/script-revisions/{id}/confirm/`
- `/api/v1/projects/` 及其 OpenAPI 中声明的内容子资源

AI 生产阶段对脚本来源的写入受 ProductionRun lineage 和状态约束。客户端不能绕开审核把生成文本直接写成确认脚本。

剧本原文创建/另存沿上述现有 API，不新增平行入口：

| 方法与路径 | 输入及结果 | 失败与副作用 |
| --- | --- | --- |
| `POST /api/v1/script-sources/` | `project_id`必填；`content`文本、`file`上传、`asset_id`素材三选一。文本可选`source_format=plain/markdown/fountain`，默认plain；成功201返回`{source,revision}`。 | 需登录且拥有项目或为管理员，越权/缺失项目404；类型/来源组合错误400；文本/文件内容校验失败422，错误含原因。文本失败不产生来源/修订；上传提取失败保留原文件及FAILED来源供诊断，不产生正常修订。配置缺失409。 |
| `POST /api/v1/script-revisions/` | `source_id` UUID与`content`文本必填；可选`structure_basis={revision_id,document_sha256}`，分别为同来源原修订UUID、已读结构化详情的64位小写SHA256，无默认值、不接受null或替换合同。成功201返回新预览修订，原版本不改写。 | 按ScriptSource所有者/管理员授权，越权/缺失来源404；字段类型、正文、来源状态、结构依据或解析错误400，配置缺失409。带structure_basis时新原文与新结构化详情同事务提交，任一失败全部回滚；AI生成来源不能通过此入口改写。 |
| `GET /api/v1/script-revisions/{id}/` | 返回同一原修订的正文、UTF-8字节数与SHA256；列表只含元信息，不附大正文或结构化详情。 | 同一ScriptSource归属控制，越权/不存在404；读取不确认、采用或重新解析。 |

文本`content`必须是字符串，不把数字/布尔/对象转成文本；保留首尾空白及CRLF/CR/LF，字节数和SHA256以实际UTF-8文本计算，空白正文、NUL、非法Unicode和超出项目上传字节限制显式拒绝。`.strip()`只判断空内容，不修改保存值。上传修订保存提取出的原文：UTF-8文本按utf-8-sig解码，DOCX按原提取器处理，原文件独立保留；不能把提取正文的摘要等同于上传二进制摘要。素材库既有`utf8_markdown_v1`入库归一化合同不变，导入项目复制其已保存文本，不再次转换换行。

创建/另存均不隐含确认或采用；确认仍为独立动作。JSON内容先沿原文入口保存为预览修订，再通过下方显式动作按确切输出版本解析；这不改变已有文件格式目录，粘贴JSON仍是原文文本，不额外新增文件上传格式。自由文本不隐式请求模型。

### 剧本修订的JSON结构化

结构化剧本的完整来源值、语义投影与路径证据保存在按需读取的`ScriptRevisionStructure`详情；写入、读取及全剧分镜字段投影使用已有`security.snapshot.text_max_bytes`文本快照预算，不使用执行配置的64 KiB预算。输出/映射配置与小型来源引用仍受原配置边界限制。超限、摘要不符或来源错误继续显式失败，不截断、不重解析补造，也不自动采用候选；不改变Schema或全局配置上限。

入口为项目生产页→新建→已确认剧本类型配方→导入或选择剧本→原文预览下方“JSON结构化结果”。[原修订视图动作](../backend/studio/script_revision_structure_views.py)复用同一`ScriptRevisionStructure`读写服务；没有新解析器、队列或自动确认。实现及隔离验证已完成，0213尚未迁移现用库，未宣称现用平台或M2全链已验收。

| 方法与路径 | 请求／响应 | 权限、失败与副作用 |
| --- | --- | --- |
| `GET /api/v1/script-revisions/structure-options/` | 可选`search`按名称或稳定输出key搜索，`page/page_size`使用原标准分页（页面每页20）；200返回`count/next/previous/results`，每项仅`id/output_key/version/name/description`。 | 登录即可只读查询已发布`script_semantics.v1`输出版本；不暴露草稿或完整配置、不默认选最新、不写入。401未登录、分页越界404。 |
| `GET /api/v1/script-revisions/{id}/structure/` | 200返回`{structure:null}`或`{structure:{document,document_sha256,json_schema,can_revise}}`；null仅为此修订尚无详情。document含原修订引用、确切输出快照、来源值及自定义字段、语义投影、默认值/提示/取值路径。json_schema由该冻结结构编译；can_revise表示此来源可从原公共修订入口另存，不表示已确认或采用。原始正文仍从原修订详情读取，不复制一份。 | 原ScriptSource所有者或管理员；越权/不存在404，摘要/关系身份漂移400、配置缺失409，不将损坏详情显示为null。已退役输出的已有结果仍按原快照读取，不查询当前版本重解析；零写入、零模型请求。 |
| `POST /api/v1/script-revisions/{id}/structure/` | 唯一必填`output_version_id` UUID，从上述目录明确选择；不接受替换正文或调用方输出快照。200返回同上已保存详情。 | 沿原来源权限；必须选已发布剧本输出版本。仅首次为preview修订解析保存，同修订/同输出重复提交返回原详情，不再次解析；不同输出需另存修订。类型/JSON/结构/引用关系/状态错误400并保留原稿、不留半成品；配置缺失409。保存不确认、不采用、不物化Episode/Bible/素材、不请求Provider。 |

页面解析失败保留原文与所选版本并显示具体原因；切换修订废弃迟到响应。若原文不是JSON，用户使用后续明确的AI结构化流程，系统不会在此动作中静默扩写。已确认修订不追加首次结构化详情，可将原文另存为预览后操作。输出版本管理仍归原配置入口，业务页仅选择；新结构化结果的审核采用、模型候选编辑及下游消费仍见[M2详细清单](optimization/module-development-detailed-design-2026-09-28.md)。验证见[API隔离测试](../backend/studio/tests/test_script_revision_structure_api.py)、[前端交互测试](../frontend/tests/scriptRevisionStructure.test.ts)及[原文测试](../backend/studio/tests/test_script_source_text.py)。

已有结构化详情的导入来源提供“按字段编辑并另存”：复用M1字段表单及本修订json_schema，编辑对象/列表/标量及原有自定义字段；false/0/null不当缺值。点击“另存预览修订并解析”沿原修订API提交，例如`{"source_id":"11111111-1111-4111-8111-111111111111","content":"{\"自定义标题\":\"另存样例\"}","structure_basis":{"revision_id":"22222222-2222-4222-8222-222222222222","document_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}`。ID及摘要仅为示例，实际取原详情；正文必须符合该详情合同。服务端锁原来源，核验依据归属/摘要，沿原创建器另存新preview，再复用同一writer解析，事务成功后返回201原修订详情响应；合同可已退役但须仍是原确切版本，不改用当前配置。新修订记录实际创建者，origin_generation_attempt不伪造；原修订及其确认事实不变，不新增审核/采用、模型调用或独立审计事实。新JSON文本可有重新排版，原文本逐字保留。400路径错误留在草稿中，未知提交结果提示核对已有版本、不自动重复提交；切换来源丢弃迟到回执。页面仅切换到成功返回的新preview，不隐含确认、生产或项目标题/梗概回写。AI来源和实际模型候选不通过公共修订入口编辑，改在原生产审核页面编辑草稿并明确另存采用，不借此旁路编辑。此段仅经隔离API及组件/浏览器夹具验证，尚不表示现用服务加载或M2整体验收。

结构化解析失败仍使用原400字段错误信封；关系错误同时给出内部业务路径及映射确定的实际源路径（例如`value.episodes[0]...`和`source.chapters[0]...`），literal配置不伪造原文字段。HTTP正文不复制失败的完整解析报告，原稿仍归原修订。此行为由上述API测试覆盖。

原执行准备的返回记录/补全区共用字段诊断展示：通用文本沿既有`diagnostics.structure_errors`，结构化剧本的`script_semantic_result_invalid`另保留`relation_errors`（path/code/message及可用的source_path/origin_kind）、`validation_scope`。来源是解析器，不采用模型自报错误，也不复制返回值；同一关系的源/业务路径合并为一项可见诊断，坏格式显式提示。补全后原诊断只作历史展示。新剧本协议已接原worker的冻结请求核对、解析、候选保存、原响应补全分派和原候选详情/共用采用writer；实例/Profile请求规则、模型传输政策、候选字段编辑及确切采用来源交接均走原正式入口。Bible读取保存语义，分镜实际写入仍待M5；隔离验证不代表现用部署或真实调用通过。

### 结构化剧本的原候选详情与采用

`GET /api/v1/generated-artifacts/{id}/` 沿原登录态和项目归属返回 `text_candidate`。当原调用冻结接口声明 `script_semantics.v1` 业务输出时，服务端核验原请求、候选指针及原修订详情；不根据返回形状、当前版本或名称猜测协议。`candidate` 是完整原始JSON值（可为根数组，保留自定义字段及0/false/null），`text` 是保真原文；`revision/output_contract/projection/json_schema` 分别提供原修订、确切输出快照、语义投影和同源结构。顶层warnings为可读提醒，projection含原路径/默认/关系诊断，但不再复制一份source_value。此类候选的quality_advisories/review_contract为null；无须为满足旧协议生成master_script/title/logline。损坏、缺失原请求或来源不一致明确失败，不回退旧标记、旧result.candidate.master_script分支或重跑AI；即使旧形状候选的外层摘要完整，缺少确切修订指针也不能读取/采用。列表仍只含摘要，任务中心输出详情复用同一读者。

采用仍调用原配方审核或人工运行 `POST /api/v1/production-runs/{id}/manual-actions/`，不新增旁路确认接口。人工审核请求仍为action=approve、stage_key=script_review，携带原候选ID/正文及清单摘要、已读当前剧本和原Run/采用修订；成功201，完全相同动作重放200返回原回执。同一writer重验冻结来源/原修订与母稿基线，在原事务确认ScriptRevision、选择Artifact、更新Run及项目母稿；保留项目原标题和梗概，不将结构字段映射回项目。候选损坏400、母稿变化409；任一写入失败回滚确认/采用及动作回执。读取或补全不等于采用，人工采用不自动执行下游。

此段接通既有详情和采用入口，验证使用隔离实例、原请求文件、原worker及API事务，Provider明确替代；正式实例作者准入已由后续同源接线完成，候选编辑、Bible下游及现用页面/真实模型仍未验收。0213仍未迁现用库。验证记录见[详细设计](optimization/module-development-detailed-design-2026-09-28.md)，隔离用例见[候选审核测试](../backend/studio/tests/test_script_semantic_review.py)。

M2后续输出作者接线复用原`model-execution-instance-versions`创建/更新、`input-contract`、`publish`和`clone-successor`接口，无新增请求字段：确切选择仍为`output_version_id`。原生接口现投影同一已发布输出，传输政策仅取模型timeout_seconds/chat_request_options，不携带旧字段合同/参考声明规则或读取旧资源建议。生成和结构化两种能力已在隔离库通过实际API保存/发布再编译，输出新版本不会自动替换原选择；错误协议、草稿/退役输出不得变成成功配置，缺少输出不得发布完整定义。正式目录现已准入script.generate/script.structure与script_semantics.v1配对，成功路径测试不再替换该目录。模型政策作者已切为仅timeout_seconds/chat_request_options，schema_version=3目录与表单一致，旧字段合同和参考声明编辑器移除；旧键明确拒绝，不自动兼容或转换。原生计划/编译/解析及冻结核对要求确切输出，不回退旧协议。此为代码和隔离验收边界，0213未迁现用库、服务未重载，不能声称现用链可执行或M2整体交付。基础资源只读合同复用同一入口，不创建Smoke/准备或外呼。证据见[实例接线测试](../backend/studio/tests/test_script_semantic_native_compilation.py)。

## 只读诊断投影

结构化剧本请求的任务中心追踪沿原接口返回，不新增端点或独立事实：system按当次输出结构、语义映射、策略通道的连续范围及摘要核对，策略段不再猜测旧REVIEWED_PROMPT_PROFILE标签；user按冻结inputs逐字段比较完整嵌套值，带点/中文字段名按实际键处理，不复制原稿正文。额外键、值/类型不符、缺失分段及输出身份不符分别保留partial/mismatch/unavailable，不重建请求或用当前配置补造。该接线已随正式实例作者与模型传输目录统一切换，验证仍为隔离编译/追踪，现用运行边界见上述M2说明；matched不代表模型收到、生成成功或已采用。

P4新增任务检查记录：`GET /api/v1/tasks/{id}/observations/`按原任务所有者/管理员授权，接受`page`/`page_size`，复用服务端统一分页配置，按创建时间与ID倒序返回`count/next/previous/results`。未知筛选400、其他所有者404、写方法405；只读且不调用执行端，响应为private/no-store。每条包含检查类型（节点预检/资源释放）、通过/失败、起止时间、worker领取次数及开始时间、请求SHA，节点/连接数与节点目录SHA，或释放策略SHA、实际操作、HTTP状态及原因码。无记录返回空页，不等于通过；HTTP缺失保持null。检查通过不代表生成、烟测或候选采用成功，不提供旧快照/原始载荷兜底。页面“任务中心→任务详情→素材操作与媒体诊断→执行检查记录”展开时按需读取，失败明确显示，不保存跨任务检查缓存。0154为该关系的初始迁移；后续切换已有记录，当前环境与未完成验收以[统一工作包](optimization/records/execution-unification/2026-09-25.md#current-work-packages)为准，读取不重放执行检查。

- `GET /api/v1/tasks/`、`GET /api/v1/tasks/{id}/`
- `GET /api/v1/tasks/{id}/input-prompt/`
- `GET /api/v1/tasks/{id}/observations/`
- `GET /api/v1/generation-attempts/`、`GET /api/v1/generation-attempts/{id}/`
- `GET /api/v1/generated-artifacts/`、`GET /api/v1/generated-artifacts/{id}/`
- `GET /api/v1/assemblies/`、`GET /api/v1/assemblies/{id}/`
- `GET /api/v1/release-bundles/`、`GET /api/v1/release-bundles/{id}/`
- `GET /api/v1/release-bundles/{id}/verify/`

M8播放与导出沿原verify接口按需取得完整签名文档及实时文件核验，不新增导出writer或生产调用。原Run共用交付面板按本包`selection.episodes`与`assemblies`确切对应读取GeneratedArtifact详情，并核对Run、母版身份、SHA、非空字节、媒体类型与签名播放/下载链接。JSON导出保留完整服务器文档（含原来源/签名/verification），不截取页面行；失败清除旧可交付详情及清单链接。配方最终成功同样使用实时文件校验而非仅签名校验；签名媒体Range在构建响应时绑定同一文件句柄，416/未消费关闭均释放，提前EOF明确中断。以上沿原只读权限与HTTP合同，无新Schema或请求字段；正式入口和操作边界见[受管产物与逐集交付](operations.md#11-受管产物与逐集交付)。

旧 `/prompts/`、`/workflows/`（历史生成图）和 `/deferred-generation-requests/` API 已移除，不保留只读或别名入口。项目详情只读取剧集、Bible 和分镜事实，不按创建时间选择历史提示词，也不据其存在标记分镜就绪。实际请求统一由本次 Preparation / GenerationAttempt 定位，并通过 Task 的 `input-prompt` 和 `comfyui-workflow` 读取；模型执行资源的工作流版本目录保持独立。旧存储模型及其剩余内部消费者属于R5后续清理，接口下线不表示实际数据库已经迁移。

P4装配详情改用`source_media_count`（只读非负整数），统计原AssemblySourceItem冻结成员，不是模型任务数或当前分镜数；已移除`source_tasks`。列表与详情仍按原所有者/管理员授权且只读，缺失/越权404、写方法405。项目制作台显示“已采用媒体”数量，缺字段明示而不补猜。签名清单记录生成产物的真实实例版本、Preparation/请求摘要；采用库素材的条目记录Asset，模型调用字段为null。`ai_generated`为可空布尔：已完成装配存在正式模型调用来源时为true；仅库素材或尚未完成时为null，不推定为人工原创，也不代表签名/字节已实时通过验证。前端保留未知。0156–0157为上述结构的初始迁移，后续切换已有记录；代码回归不等于当前版本装配或封存已通过真实验收。

ReleaseBundle列表/详情必须返回非空只读`production_run`，页面直接用该记录跳转。`selection`仅含`shots[{shot_id,artifact_id}]`、`episodes[{episode_id,artifact_id}]`及`series_artifact_id:null`；不再返回或解析旧task/output_index、assembly_id选择。已验证v3清单的`assemblies`另记录实际`assembly_id`供母版追踪，不作为选择身份。前端对缺失、重复或清单成员错配显式报错；`verification_status=invalid`不能补成已验证。

发布包只有`autoprompt-production-release-bundle-v3`合同；`production_run`必填，一轮至多一个包。封存读取本轮RunPlan交付范围、已审核选片、逐集母版与原装配来源，不接受旧任务/PromptArtifact身份或独立包自动选片。签名覆盖`run_plan_sha256`；生成素材记录`preparation_id/request_sha256`，库素材记录`source_asset_id`，不存在的模型来源为null。Comfy必须具备有效公开副本；无效来源、内容变化、冻结计划缺失/摘要不符，verify返回400而非通过或服务器异常。verify为当前字节与来源的只读核验，无模型调用、创建、重试或发布副作用；缺失/越权404。人工制作的交付动作和配方交付阶段均调用原ReleaseBundle封存；正式入口隔离串联已验证，实际服务切换及用户线上验收状态见统一计划第0.1节。

配方`delivery/existing_pipeline`可选声明`delivery_metadata`，字段与装配作者共用：确切`output_version_id/expected_contract_sha256/mapping`，以及严格二选一的`source_text`或本轮更早步骤`selected_output`来源。原options增加只读`delivery_metadata_schema`，有限目标为`{title,notes}`；title为1–200字符文本，notes允许空文本，两者不接受NUL。保留原始空白、换行、Unicode及自定义源字段，不改写项目标题/梗概；未配置时沿原交付默认值，不增加必填用途或新队列。正式配方作者API及页面复用M1结构/映射和确切版本校验；缺项、错误类型、陈旧摘要、前向/双来源均显式400，失败不保存半套配置。

首次交付准备将完整输出/结构合同、投影原值及来源冻结于原`config_snapshot.delivery_metadata`；动态来源记录原选择决定、修订、产物和自动/人工身份。封存将投影title/notes原值及完整delivery_metadata纳入同一v3签名。列表/详情只用批量读取的原RunPlan及已签名字段作纯校验，不重新查询阶段、锁定上游、读取当前注册版本或修补数据。`verify`另行检查当前冻结阶段及上游选择是否仍一致；后续上游漂移可以使live校验失败，但不改变未受损历史包的签名有效性。签名内内容或数据库显示title/notes被修改仍无效；配置上限异常统一为业务错误，不穿透为500。此作者子段的隔离链及页面证据见详细设计18.3第311批，不代表完整多集交付或现用模型验收。

这些资源用于观察 lineage、attempt、冻结提示词、产物、source item、装配和交付验证；Task 没有 create/retry/cancel 写端点；旧 Deferred request 表和入口已删除。创建、重试、取消与有序 fallback 必须使用所属 ProductionRun 的正式 action。ReleaseBundle verify 只执行交付诊断，不是生产创建入口。`generation-attempts` 按 `stage_run` 过滤，`generated-artifacts` 可按 `production_run`、`stage_run` 或 `generation_task` 过滤；Task collection支持production_run_id、stage_run_id、preparation_id、execution_instance_version_id及unit_key精确筛选。旧generated_workflow、batch_id、shot_id筛选已删除，未知字段明确400，不可静默忽略后返回全部任务。

任务页面保留原`cancelled`与`cancel_requested`：已取消不是执行失败，取消中也不是远端成功或确认终止。徽标、节点、日志和共用进度均读取这一状态；取消阶段不显示此前ComfyUI采样百分比，取消提示仅返回所属Run查看合法动作，不承诺可重新创建调用。原结果未知错误不因停止Preparation而改写为确定取消或成功；重复命令只回读原回执，是否可针对确切失败后继新建调用由原正式恢复动作判断。配方、人工主调用与独立素材保留各自业务入口，共用既有执行能力，不新增Task重试旁路。任务中心的`output_count`是输出分页条目数，包含待处理原始正文；是否存在生成候选应看`outcome.artifact_count`和确切产物身份，留存正文不等于生成成功或采用。

Task的execution对象来自唯一实际主GenerationAttempt及其Stage/Run/Preparation，包含production_run_id、stage_run_id、stage_key/ordinal、unit_key、attempt_id/number、preparation_id、execution_instance_version_id和operation；关系缺失或不一致时为null，不能从旧快照、Episode或标题推断。episode展示只使用Task真实FK，不再从PromptArtifact补齐。详情 `input_parameters` 只投影该调用的Preparation：`parameters` 为key/value/value_source/rule数组，包含可调参数及工作流固定语义；固定项来源为workflow.static_semantics，rule.const展示冻结值，不产生可编辑参数。读取复用原参数完整性校验，拒绝值、来源或合同漂移；`media_uses` 为逐次使用的有序数组，包含item_key/input_key、Asset或已采用Artifact身份、用途/配对、参考标签、实际ports、source_sha256/checksum及staging_status。context-only的ports为空、暂存状态为not_applicable；图输入未上传为not_staged，有效回执为staged。同一素材的不同使用不合并。参考预览仅在显式详情中按当前权限/启用状态/字节摘要提供，不用当前名称替换冻结事实。

`GET /api/v1/tasks/{id}/input-prompt/` 返回本次冻结通道：key、semantic_key、text、character_count、value_source，以及实际Attempt/Preparation/实例版本/请求摘要、prompt_mode、精确策略版本和声明的参考标签。人工原文、显式空字符串、通道次序如实保留，不重跑策略，不固定为两个通道，不与旧PromptArtifact拼接。没有提示词模式明确none；已冻结不等于远端已接受或执行成功。

列表/详情都声明input_contract_status与input_contract_code。合法准备返回available/空原因码；列表遇到缺失或损坏的实际关系、请求或回执时返回unavailable/frozen_contract_invalid及input_parameters=null，保留可观察任务状态，不补造输入。详情及正文读取遇到上述错误明确失败。旧legacy_schema_unsupported分支已删除，不增加旧执行兼容。公开DTO移除旧batch/shot/variant/retry_of、generated_workflow及重复参数、原始暂存/输出载荷；timeline仅保留timestamp，不再提供occurred_at别名。原dispatch事务已创建Preparation驱动Task，并移除数据库旧trace/快照/冗余执行参数；分镜编辑页已删除按shot查询任务、取最新成功作预览及读侧改写Shot状态，保留规划编辑并链接生产工作台；生成、预览与采用从正式Run处理，不在此建立第二份选片事实。当前实际切换状态以统一计划第0.1节和最新第25节证据为准，不以旧阶段记录判断。

任务诊断页只按execution.production_run_id + stage_run_id分组；同组按真实且不解析语义的unit_key区分单元，使用最大attempt_number识别当前调用，不按心跳/完成时间选择新旧。全部实际调用仍展示；较旧的在途调用不被较新成功行遮蔽。计数仅覆盖已创建任务，不能冒充整个阶段的待办或验收；未创建项由生产工作台展示。关联异常任务独立显示，不按Episode、标题、旧batch或本地缓存补组，不再创建虚拟任务。

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

Smoke详情包含只读`scope`（resource/instance）和`execution_instance_version`：基础资源为空，组合验收必须是精确版本。两类Attempt支持preparing，此阶段远端deadline为空，不等于排队/执行中。组合结果的`result_schema_version/result_size_bytes/result_sha256/evidence_expires_at`随原Attempt返回；基础部署仍由final_snapshot提供其资源证据。此Schema、前端适配及组合提交链路已接入；0146及25.35–25.36记录初始开发状态，后续切换与待验收项见统一计划0.1，不代表任一当前组合已通过Smoke。
- `POST /api/v1/workflow-template-versions/{id}/publish/`
- `POST /api/v1/workflow-template-versions/{id}/retire/`

ProductionRecipe publish 会校验唯一正式 content 入口和完整有序的 content → storyboard → prompt → media → selection → assembly → delivery 闭包；缺阶段、顺序错误、媒体 route 未冻结，或 route 引用的 Provider + Deployment 合并执行合同不完整都会拒绝发布。新建 ProductionRun 也会执行同一准入校验；合同无效时在任何费用承诺或远端提交前返回 `400 deployment_execution_contract_invalid`，错误响应不回显底层端点、配置值或密钥。

注册表使用受状态约束的生命周期，而不是对审计事实开放任意 CRUD：Provider 可新增、编辑和停用，非系统且无引用的停用项才可删除；模型版本与配方只有 draft 可修改或删除，published 只能 retire；工作流版本不支持原地 PATCH，必须导入新版本。新模型 `POST /model-profile-versions/` 的写入合同不包含 `version`，服务端固定创建 `v1`；`specification.schema_version` 也由服务端写入当前支持值，客户端应省略，提交不同值会被拒绝。已存在的 `profile_key` 必须改用精确源版本的 `draft-next` action。该 action 只接受当前最高 published/retired 版本，先校验源合同的大小与 SHA-256，再在事务中锁定模型族锚点，并按全部历史（包括 retired）分配下一号；当前最高版本已是 draft 时要求继续编辑，旧源或并发请求返回 409，普通 PATCH 不能改变 `profile_key` 或 `version`。`GET /model-profile-versions/options/` 返回当前规格结构版本、注册驱动及其模态/传输操作、模型模态、Artifact 角色、受控业务能力与已登记提示词策略族，供正式页面构造单选/多选；新模型可在选择器中显式选择由合法 `profile_key` 派生的同名新策略族，其他自定义文本会被拒绝；目录缺失不能回退为任意文本。ModelArtifact 仅在所属模型版本仍为 draft、状态仍为 `declared`、从未核验且不存在 RuntimeModelAvailability、工作流或配方引用时允许删除；绑定 published/retired 模型版本的记录是不可变证据。Deployment 必须先停用：完全没有 probe/smoke、主机可用性、工作流、配方、推荐或运行证据的误建空记录才可删除；需要保留证据的停用记录在替换 current 工作流与 draft/published 配方引用后使用 `retire`，进入不可逆只读终态。项目一旦存在正式 ProductionRun，`DELETE /api/v1/projects/{id}/` 同样返回 `409 protected_resource`，要求归档而不是级联删除；ComfyUI/async 候选、已物化 Asset、任务、签名 manifest 与审核历史必须继续可审计。

Settings 注册资源的日常删除统一使用服务端影响复核，不先发无正文 DELETE 再猜依赖。`POST /api/v1/registry-deletion-preview/` 提交 `resource_type + object_id`，返回目标状态、依赖对象、关系说明、配方 JSONPath、`selectable_delete / automatic_delete / blocking_reference / preserved_history` 分类、精确计数、确认短语及 `impact_sha256`。草稿配方依赖须明确勾选；日常入口不级联删除运行或已发布版本。不兼容旧数据的专项物理清理仍按已授权改造计划核对精确闭包，不受“历史必须保留”的旧口径限制。

唯一删除执行入口是 `POST /api/v1/registry-deletion-execute/`，注册资源详情不开放绕过复核的 DELETE。正文包含 `resource_type`、`object_id`、`expected_impact_sha256`、全部已确认的 `selected_dependency_keys` 和完整 `confirmation_phrase`；原因码通过 `X-Audit-Reason-Code` 传递。原注册关系 guard、目标/父对象及依赖锁和第二次完整影响核对保持不变。对象或引用变化返回 `409 deletion_impact_changed`，漏选依赖返回 `409 dependency_confirmation_required`，均不执行部分删除。成功返回 HTTP 200 及实际删除清单；生命周期摘要在删除子配置前读取，不能把已清空的父配置作为删除前事实。此入口仅删除平台注册记录，不移动或删除磁盘模型/媒体文件。

执行实例目录与版本复用同一入口，类型为 `model_execution_instance` 和 `model_execution_instance_version`。页面在草稿配置中提供“删除草稿（复核）”，停用实例提供“删除空实例（复核）”。只允许未使用草稿及停用且无版本的空实例；输入、目标映射、参数、辅助绑定和派生合同属于草稿自身，列明后删除，不连带删除部署、工作流、策略或辅助目标。部署/工作流/策略和实例目录的预览反向列出精确执行版本；辅助消费者、任务、准备、烟测及冻结 RunPlan 阻断日常草稿删除。配方和 RunPlan 只按 schema3 阶段路线的 `execution_instance_version_id` 识别引用，不将输入 literal 中的同名 UUID 算作关联。删除成功才关闭原配置和失效深链、重读目录；取消复核不改配置。

本地 Comfy 模型的正确准备顺序为 Provider → draft ModelProfileVersion/声明 ModelArtifact → 启用并 probe RuntimeHost 至 active，由工作流 discover 返回的同 Provider active loopback 内部 source Deployment 完成草稿 artifact attestation 与完整文件闭包 → 发布 ModelProfileVersion → 建立目标 ModelDeployment → RuntimeHost Probe 形成该 deployment 的 availability → artifact verify。完成准备后，ComfyUI 工作流的正式验收链路是 **RuntimeHost Probe → Live Validate → Workflow Smoke**；Deployment Probe/Smoke 不能替代某个不可变工作流版本的实时结构校验和真实 Workflow Smoke。`async_rest` 不执行 ComfyUI 工作流，使用 **Deployment Probe → Deployment Smoke** 验证其部署 operation；文本驱动仅对已声明并受支持的 operation 执行 **Deployment Smoke**，不提供 Deployment Probe。管理员不在扫描前手选 source Deployment。远端模型没有本地文件时明确为 N/A，不伪造 artifact。ModelDeployment 写入使用唯一权威合同校验，同时核对 Provider/Deployment JSON 完整性、敏感字段和驱动必填项。Provider 启用或更改 driver/config 时会在同一事务锁定并重验所有 active Deployment；任一合同失效则整个变更回滚，而停用 Provider 仍允许以便运维修复。已发布模型的文件关联不可原地修改。`model-artifacts/{id}/verify/` 只接受未过期且合同 hash 有效的 HostCapabilitySnapshot，并把 snapshot、操作者和时间写为可追溯核验证据。

ModelArtifact 的 `filename` 是受控 models 根目录下的相对文件名，`inventory_folder` 是受控库存目录；API 不保存可随机器变化的绝对路径，也不会移动或删除物理模型文件。修改 draft artifact 的定位字段、SHA-256 或大小时，服务端原子恢复为 `declared` 并清空当前 `verified_*` 指针，既有 RuntimeModelAvailability 仍保留为历史观测；新就绪判断必须等待新声明对应的新鲜主机快照。published/retired artifact 的相对定位或字节发生变化时必须创建更高 ModelProfileVersion。仅 ComfyUI 安装根变化而相对定位与字节不变时，不改 artifact；按运维文档更新受控根/端点、重启并重新执行 host probe、artifact verify，以及受 target 漂移影响的 deployment/workflow 验证。

ComfyUI 模型文件身份与节点提交选项分开核对：文件要求使用规范化相对名和 SHA，导入图中的模型控件则从目标 `/object_info` 冻结唯一、精确的枚举字符串（包括 Windows 分隔符）。Live Validate 不再把仅规范化后相同的字符串当作可提交选项；不匹配须重新导入新版本，不在运行时替换已发布图，也不影响普通参数和素材上传回执。

Deployment基础烟测按明确资源选择读取受控能力与统一输入合同，不自动缩小参数、填种子或生成一集一镜样例；用户在原Preparation表单明确输入与策略/人工通道。原生文本分别用对应业务响应协议校验，另一operation的通过不能替代本次能力。visual_rules.generate的硬容量及可选quality_advisories仍来自模型冻结合同；未配置建议不启用隐含业务清单。原ModelDeploymentSmokeAttempt保存任务身份，Preparation保存输入/请求，原worker只跨越一次提交边界。基础结果快照schema2引用原任务与结果，不保存另一份产物；实例组合证据是独立scope，不由基础通过授予。当前resource worker仍有开发期门禁，隔离回归通过不等于真实服务已启用。

工作流版本以semantic bindings对接Comfy图，不依赖固定节点号。bootstrap版本为needs_review草案；导入、Live Validate、真实Smoke、发布与退役是不同动作。详情返回验证目标、合同hash与验证证据；API不自动下载模型或把catalog条目标成已部署。业务素材生成仅选择精确执行实例版本，Workflow从实例冻结引用读取；基础工作流通过不能绕过组合准入、冻结、幂等、取消、uncertain及原产物入库状态机。

已发布/退役工作流再次调用`publish/`返回400；共享发布服务在检查并清理草稿的目标漂移前拒绝该操作，不降级历史版本或清空原Validate/Smoke证据。普通导入继续按精确来源和结构去重；显式重建通过原批量提升接口的 `successor_of` 指定原工作流版本并追加新草稿，不继承旧版本的验收。原版本的目标漂移或证据过期由只读准入明确报告，不以重新发布修复，也不将旧通过状态当作新组合证据。

基础Smoke不接收旧prompt_profile_binding_id或旧smoke_inputs。明确资源选择和精确策略引用按统一合同冻结；当前动作读取所选资源返回的输入/参数定义。已发布策略或完整人工通道由prompt_mode确定，不在后台按Binding优先级选一条。实例Smoke直接读取实例的冻结组合；请求冲突不得复用其他组合的活动任务。当前创建、恢复、继续、取消入口与结构以本文件的统一资源/实例Smoke接口卡片及根OpenAPI为准。

### 统一执行实例注册

管理员通过草稿注册、完整定义查询与显式发布/退役接口管理实例。素材、生产及无配方入口引用精确发布版本；注册接口只保存作者配置并核对冻结，不执行模型、不生成Smoke，也不以保存/发布成功宣称实例可运行。实际数据迁移、服务切换和用户线上验收状态以改造计划R7/U1台账为准。

| 接口 | 请求与结果 |
| --- | --- |
| `GET, POST /api/v1/model-execution-instances/` | 分页查询；创建提交 `key/name/description`，返回 201。key 创建后不可修改 |
| `GET, PATCH /api/v1/model-execution-instances/{id}/` | 查看；更新明确提交 `expected_revision` 及修改的名称、说明或 `is_active`，返回 200 |
| `GET, POST /api/v1/model-execution-instance-versions/` | 分页查询；创建明确提供实例/父修订、Deployment、执行 operation/mode、prompt_mode、精确策略和 Workflow（不适用时明确 null）；可一并提交 inputs/parameters/auxiliaries |
| `GET, PATCH /api/v1/model-execution-instance-versions/{id}/` | 详情返回精确作者 configuration；草稿更新要求版本 `expected_revision`，子配置采用完整数组原子替换 |
| `POST /api/v1/model-execution-instance-versions/{id}/clone-successor/` | 明确源修订及 `expected_definition_sha256`（草稿 null，已冻结来源精确 SHA）；创建新草稿和子项，不复制发布信息或验收证据 |
| `GET /api/v1/model-execution-instance-versions/{id}/input-contract/` | 返回 `version_id/revision/frozen/schema_version/size_bytes/sha256/target_fingerprint/definition/context_fields/input_forms/language_form`；草稿现场核对完整定义、不落库；已发布/退役返回校验完整性的冻结正文，不以当前配置改写；填写说明不进入冻结定义SHA |
| `GET /api/v1/model-execution-instance-versions/{id}/parameter-mappings/` | 按刚读取的 `expected_definition_sha256` 查询标量参数声明的工作流节点／API字段；核验依据不一致时明确不可用，不修改冻结定义、不执行模型 |
| `POST /api/v1/model-execution-instance-versions/{id}/publish/` | `expected_revision` + 刚查看的 `expected_definition_sha256`；完整复核后在一个事务内冻结合同、发布版本、更新父修订并写审计，返回 200 版本详情；不要求或触发本实例 Smoke |
| `POST /api/v1/model-execution-instance-versions/{id}/retire/` | 上述修订和 SHA + 非空 `reason`（最多191字符）；返回 200 版本详情，保留冻结配置，不调用失效的实时目标 |

实例版本的 `operation` 是执行能力；`prompt_operation/prompt_profile_mode` 从精确策略只读返回，不接受客户端改写，不要求两类 operation 字符串相等。列表只读窄字段并分页，不返回完整配置 JSON、Provider 执行配置或模型合同正文。

输入 `semantic_contract` 的主体类别固定为平台事实类型 character/scene/prop/external，范围类别为 request/shot/episode/project，均由后端目录与 OpenAPI 枚举提供；选择类别不代表获得某个具体项目、主体或素材的访问权。`allowed_purposes` 的具体成员仍需对应目标/策略用途目录核对，不能把草稿结构合法当作完整接线验收通过。

策略本次取值的自动输入：创建／修改草稿时，服务端从精确策略的已登记 `request.*` 值来源派生输入，逻辑标识等于来源标识，类型／必填／数量取自同一合同，范围固定为本次请求；不包括参考素材和额外上下文。客户端可省略这些独立端口或原样回传，不能改名、改类型、放宽范围或合并来源，否则400且整次修改回滚。`inputs:[]`不会删除策略必需输入。实际值仍在创建执行准备时填写或明确选择，不读取项目、不代选上游结果、不调用模型。工作流/API槽位与该来源明确复用时保留作者映射，并继续核验物理合同，不能据此绕过范围或类型校验。后继草稿重新派生独立请求端口；已发布合同、冻结输入、任务和审核记录不改写。

`GET /api/v1/model-execution-instance-versions/{id}/authoring-options/`仅供草稿，返回必需字段 `derived_input_keys`（字符串数组，人工／无策略输入时为空）标明只读派生端口；页面不再在“添加输入”中重复提供这些来源。项目 `project.logline` 和关联风格 `style.visual_prompt` 同样自动派生，但范围固定为真实项目，冻结 `source_reader={protocol:"project_snapshot/v1",selector,scope_kind:"project",value_kind:"text",snapshot_policy:"confirmed_preview"}` 与 `source_kinds:["project_snapshot"]`。它们只接受预览确认的项目快照，不与可编辑的 request 输入混同。类型不符、项目范围缺失或物理目标禁止此来源时明确拒绝。派生不读取正文、不会调用模型。自定义来源注册、输出注册与通用下游引用仍未完成。

已审核输出同样从精确策略派生：`reviewed_visual_rules.continuity_summary/color_script/negative_prompt` 分别对应本轮人工采用后的连续性说明、色彩脚本和负面约束。每项是项目范围单值文字，只接受同标识的 `context_field`；冻结 `source_reader.protocol=reviewed_visual_rules/v1`、`snapshot_policy=exact_current_approval`、`output_schema_version=3`、审核阶段与输出字段。不是候选原文或项目未审核现值，不允许literal冒充。输入／物理目标共同限制来源，保存时检查精确审核决定、原成功任务及候选、现值摘要和所有权；来源变化拒绝新准备，已冻结准备保留原值。配方来源须声明更早的`visual_rules_review`，由原绑定器固定当次审核ID，不借用初始剧本修订。

这里的3是人工审核业务记录的版本，不是模型返回协议版本（影视规则候选仍为2）。人工和配方采用共用原审核写入函数产生版本3；配方外层继续使用正式阶段封装。读取时同时校验外层摘要、项目与审核内文的一致性；不升级或覆盖历史审核记录。组合Smoke可明确选择只读生产来源上下文后消费此类字段；基础资源Smoke不提供此上下文，不能用假审核或字面值绕过。

#### 参数声明目标的只读查询

- 入口：管理执行实例 → 查看配置 → 复核完整合同 → 参数目标映射 → 读取参数目标映射。实现为 `execution_parameter_mappings.execution_parameter_mappings`；不是新的参数配置或编译入口。
- `GET /api/v1/model-execution-instance-versions/{id}/parameter-mappings/`，只接受一个必填Query `expected_definition_sha256`（64位小写十六进制）。无Body、分页或默认SHA。管理员可查草稿／已发布／退役版本；普通登录用户只可查其有权选择的启用已发布版本，沿原工作流模板权限，不放宽作者权限。
- 200返回 `version_id/revision/definition_sha256/status/code/message/basis_kind/basis_id/basis_sha256/rows`。每行是 `{key,destinations:[{node_id,field}]}`：工作流为原声明节点输入，保留一对多；部署API为JSON顶层字段且node_id为null。只含 `interface.parameters`，不包含素材、提示词、模型文件、服务器内部参数或静态事实。`verified`仅说明映射依据核验通过；行的空目标明确表示未记录，不能推断；有依据且rows为空表示合同没有可调标量参数。
- Workflow依据精确目标的 `execution_contract_sha256`，校验图、接线及合同完整性；动态节点展开、尺寸／帧计算仍由原编译器执行，此处不是最终请求。async REST只在部署合并传输配置摘要与实例记录一致时读取原 `request_field_map`；不返回URL、Headers、凭据、图中值或完整传输配置。文本适配器没有独立映射记录时为 `not_recorded`；资源缺失、漂移或校验不通过为 `unavailable`，均返回空rows和具体安全说明，不拿当前配置补写历史。
- 请求格式错400、无登录401/403、无权版本或不存在404、所查看定义SHA变化409；前端保留明确错误并可重新读取。200的 `unavailable/not_recorded` 是证据状态，不是成功映射或任务失败。响应 `Cache-Control: private, no-store`。按需读取本地冻结定义与精确资源，无写入、外呼、解密、任务创建或审核副作用。历史实际请求仍在任务中心核对。
- 验证覆盖一对多、配置漂移、接线损坏、SHA冲突、权限、敏感信息排除及只读查询；实际页面和其余未完成范围记录在统一优化检查点，不以此查询代替真实Smoke。

#### 组合Smoke的已审核来源上下文

- 接口：管理员 `GET /api/v1/model-execution-instance-versions/{id}/smoke-source-contexts/`，只读正式Run／Stage身份与计划摘要；实现`execution_smoke_sources.smoke_source_context_rows`。Query仅`page`（默认1）、`page_size`（默认原分页配置且至多50），拒绝重复或未知参数。响应200为`count/next/previous/results`，空结果为0与空数组，`private, no-store`。
- 每项含`run_id/project_id/project_title/run_created_at/stage_key/run_plan_sha256`。仅返回当前用户拥有的Run与项目、已成功影视规则审核之后的步骤身份；不代表其审核现值有效，必须继续调用上方原预览API。目录不读取审核正文、候选文件、完整计划JSON或逐行查询。
- 页面入口：执行实例 → 新建真实烟测 → 已审核输出输入 → 读取来源轮次与步骤 → 明确选择 → 在下方预览并确认各字段。未读／加载／空结果／失败独立呈现，不默认选最近轮次；换来源或重新读取会清除旧选择与确认。
- 创建仍为原 `POST .../{id}/smoke-test/`，可选`reviewed_source_context={run_id,stage_key,run_plan_sha256}`只用于新建组合烟测实际引用的已审核影视规则字段，输入继续提交精确审核决定ID。按Run锁核对计划、前置步骤、owner、项目及审核内容；来源上下文进入原准备意图，字段来源含同一上下文及审核／任务身份。请求幂等重放返回原冻结任务，不改取新值。
- 边界：只授权已审核影视规则读者，不开放其他Run输入、生产参数、媒体选择或生产执行身份；不会新增ProductionRun、推进阶段、创建审核、采用输出或修改原费用。基础资源烟测、未使用该来源、与复用准备同时提供上下文均400。复用准备继续使用原精确冻结输入，不重新查询现值；不是重新批准。
- 错误：未登录／非管理员按原鉴权401／403；实例不存在404；无权来源、无前置审核、审核过期／损坏或缺上下文400，计划摘要与幂等冲突409。失败事务不遗留新Smoke或准备，页面保留错误及重读入口。实际调用仍受本次显式外呼授权和原系统提醒机制约束。
- 验证：隔离测试覆盖真实原候选→人工审核→Smoke准备冻结、原生产状态不变、来源变化、分页／跨用户／封闭请求、幂等重放；不以隔离准备通过宣称真实模型或媒体验收成功。

#### 执行实例的通用输入结构与配方默认预检（M1-08阶段交付）

- 入口：原系统设置→模型执行实例→草稿配置→文本/JSON输入→“通用输入结构（可选）”；复用同一个结构版本选择器，不另建字段字典。原`POST/PATCH /api/v1/model-execution-instance-versions/`的`inputs[]`增加可选`data_structure_version_id`（确切已发布UUID；null明确移除）。未设置不附加业务结构，模型目标原合同仍生效；GET返回配置只在有引用时携带此键，克隆保持确切版本。
- 所属：`ExecutionInputSpec.data_structure_version`由0207迁移文件增加PROTECT外键；输入配置只存ID。纯Profile自动派生输入不在实例重复维护；显式物理/混合接线仍同时满足Profile与目标原合同。媒体输入继续走素材来源合同，不用文字/JSON结构代替真实媒体解析。
- 冻结：定义的`interface.input_structures`按逻辑输入key保存同一结构的完整快照；表单`input_forms.schemas`从快照派生，保留原目标限制。版本身份、键集合或快照不匹配返回400，不能查当前最新版补齐。文本要求不允许null的字符串根节点，JSON支持结构声明的类型及空值语义；结构名称与字段标签均由确切版本决定。
- 解析：原`resolve_execution_input_sources`在字面值/上下文/已选择文本/结构装配汇合后、目标端口投影之前调用共享缺键解析器。缺键默认生成副本，不覆盖null、空串、0、false或未知属性；解析失败返回具体字段路径，成功值仍须满足原生目标/Profile约束。`input_provenance[].structure_resolution`记录`structure_version_id/defaults/source_value_sha256`，原来源及叶子证据保留，外层`value_sha256`对应解析后值。原请求不改写，不将配置默认伪装为模型提供内容；冻结读取无需查询当前字段/结构注册表。
- 权限/副作用：沿用实例管理/读取权限、修订冲突与正式准备入口。选择/试填只改本地草稿或做只读预览，保存只改草稿配置；不调用模型、审核或采用内容。非法/未发布/不存在的结构引用返回400并回滚草稿变更，整个实例不存在仍按原404处理。
- 配方预检：原配方来源校验对完整结构模板及带结构的固定值，使用确切冻结结构在临时副本中展开已知缺键默认，再核对逻辑结构与全部目标约束；不因默认能够补齐的缺键提前拒绝。省略必填字段只认可所引用结构的显式默认，普通目标Schema的default不是补值授权。可空对象/列表、显式空值及默认与目标限制冲突仍检查；动态来源和动态单元键在准备得到实际值后终检。预检不读取未来值、不创建默认来源或审核记录，原配方模板不改写；真实准备继续记录默认来源及摘要。原配方编辑器可省略默认字段、从结构目录恢复为待配置映射，并保持只读约束。
- 验证边界：隔离API→配置→冻结→同源表单→准备输入解析→下游端口示例已覆盖，未迁现用数据库或发布现用实例。配方通用深层路径映射、Profile任意来源字段路径、通用输出目录及媒体策略三处消费者接线仍是M1-08未完项；不是M1模块整体交付。

#### 已审核影视规则预览

- 采用依据：支持原人工审核决定与冻结配方授权的 `recipe_policy` 自动采用决定。自动路径复用原策略授权校验，核对决定／策略摘要、原生成阶段快照、候选和采用凭据；不补造人审、不重算或改写旧结果。下游仍要求同一 Run 的允许前置步骤、精确采用决定与当前未漂移的规则，不能仅凭 `succeeded` 状态引用。
- 接口：登录后 `GET /api/v1/production-runs/{id}/reviewed-visual-sources/?stage_key=<目标步骤>`；`stage_key`必填、不接受其他或重复参数。实现 `production_reviewed_visual_rules.reviewed_visual_source_options`，页面入口是人工创建准备 → 已审核影视规则输入 → 读取本轮已审核规则 → 确认引用此审核版本。
- 权限：Run与项目均须属于请求用户；管理员管理权限不替代业务所有权。只允许冻结计划中目标步骤的前置审核来源，不能跨运行、引用未来步骤或独立Smoke伪造审核。
- 成功200：`run_id/project_id/stage_key/run_plan_sha256/review_contract/sources`；sources为原输出版本所选字段（基础三项，可加镜头指导），每项含`selector_key/label/content/content_sha256/review/output`。`review`为原审核步骤、决定、修订及快照SHA；`output`给出协议版本、原生成步骤／准备／任务／候选／结果摘要及字段，并以`business_output`记录原生产输出版本身份。`review_contract`是从原候选及实际请求核验后派生的只读合同：schema_version=1，output含id/output_key/version/name/sha256，fields含key/label/value_type/allow_blank/source_selector/response_path/response_format/transformation。字段顺序、名称、格式与响应位置来自原冻结版本，不使用当前目录或下游实例的输出版本。空的已审核负面约束仍是明确空文字，不当成来源缺失。
- 提交输入只携带 `{kind:"context_field",selector_key,scope:{kind:"project",id},content_revision_id:<审核决定ID>}`；不提交预览正文。服务端重新核对后读取审核快照，冻结单字段及原来源；预览后再改审核版本会拒绝，不静默切换新版本。
- 失败：400表示无前置审核、旧协议、已变更、记录或查询不一致；无权查看的Run为404，未登录按原鉴权返回。页面保留错误及重读入口，清除未确认或失效的选择，不显示为可用。响应`private, no-store`。
- 副作用：只读原Run、Stage、ApprovalDecision、GeneratedArtifact与GenerationAttempt；不创建任务、不采用候选、不调用模型、不读取完整剧本、不改写审核或费用。验证与未完成范围见优化台账第六批检查点；不等于任意来源／输出注册已完成。

`GET /api/v1/generated-artifacts/{id}/` 的影视规则`text_candidate.review_contract`使用上述同一只读投影。候选详情、正式采用和下游读取均核对原Preparation中的输入、输出版本、质量建议及候选字段／摘要；仅重算候选或审核快照的自有SHA不能替代与原请求／产物的一致性核验，不一致返回400且无写入。审核页面依该冻结字段表逐项确认，不能按当前目录补齐字段；历史旧协议或没有独立输出版本的结果仍可只读查看，review_contract=null并明确adoption_block_reason。这不开放新的业务字段或类型，也不自动转换旧结果。来源保存仍只提交精确审核ID，服务端在原input_provenance.output.business_output冻结生产输出版本身份，不写第二份可变合同。

完整定义的 `interface.profile_inputs` 支持当前完整合同的直接取值策略（direct_v1，无投影配置；server_locked/passthrough）。目标 key 来自精确规则的 source_selector，不从实例输入名猜测。每项返回类型、值域、必填/单值数量、枚举交集、消费字段与各字段显式默认值；默认值仍属于各自字段，不合并成一个隐藏来源默认。不同逻辑输入不能争写同一提示词来源，缺必需映射、错类型或冲突枚举返回400。声明式参考投影及生成/翻译字段沿同一输入合同、准备与明确辅助实例接线；不受支持的组合返回具体缺口，不放宽发布验证。

模型能力选项目录新增 `prompt.fields.generate`、`prompt.fields.translate`、`prompt.draft`。声明这些能力的模型必须明确配置 `specification.text_execution = {schema_version: 1, operations: {...}}`；operations 的 key 必须与声明的新能力逐项一致。每项仅含 `parameters` 与 `chat_request_options`：前者复用执行参数的类型/必填/范围/枚举/对齐/default/const 规则，只接受可选 seed；不允许模型配置 max_tokens。所有文本协议的输出长度统一来自系统 execution.resources.review_policy，在准备中冻结来源与数值。后者复用现有受控 Chat 选项校验；不能覆盖 model、messages、凭据、传输地址或输出预算。模型草稿保存和发布均核对，缺失/未知键或不兼容驱动返回400；这些声明不为其他文本业务生成兼容别名。

新文本实例的定义查询/发布复用上述正式接口：`prompt.draft`复用既有策略的 system_instruction/positive_prompt 通道，分别传给聊天协议的 system/user；起草要求 positive_prompt 非空，不新增提示词通道别名。输出适配为`{text:string}`。字段生成/翻译通过必须接线的 `operation_input:field_request` 使用内部冻结字段合同，注册适配器明确声明 prompt_mode=none，不额外接收手填提示词。field_request 标记 server_owned，由 Preparation 或正式Smoke夹具从精确已发布主策略字段产生，不是普通客户端自由上传字段规则的入口。响应严格为`{items:[{request_id,text}]}`，拒绝缺项、重复/额外ID、重复JSON键和附加说明；不部分采用、不自动批准。定义、组包、解析与基础资源/组合Smoke均接共同Preparation及原worker，不使用旧Binding；真实字段AI验收仍需对应实际调用证据。

原模型选项接口提供`text_execution`表单元数据：`schema_version`、`operations[{value,label}]`、`parameters[{value,label,description,required,require_maximum,template}]`，以及`integer_constraints.value_fields/seed_policies`两组`{value,label}`目录。目录缺失或无法识别时表单阻止编辑，不自行补选项。只提供协议骨架，不声明某模型的预算上限/默认值。模型新建/编辑的结构化表单写入同一specification，发布/保存仍由后端模型策略校验器校验；取消能力勾选但保留对应协议配置返回校验失败，不自动清理。查看已发布模型合同显示当时保存的范围与选项，不读取当前模板补历史值。

文本参数固定表单支持必填、最小/最大值、默认/固定值、允许值列表、多个步长/余数以及模型种子策略限制，完整JSON只核对该合同，不允许第二次编辑。未设置enum表示不限制枚举，显式空列表无效；新增空行不猜测数值，删除最后一行明确取消此列表约束。零、未设置与无效值区分保留。默认值可以被后续显式值替代，固定值、范围、枚举和对齐是硬限制；不允许其他层放宽。模型省略seed_policy时由实例选择，模型若声明fixed或generate_once，各层必须一致；后者需要有限范围或枚举，且不能有默认/固定值。切换策略不自动删数据，冲突就近显示并阻止保存。服务端共享标量编译器负责最终交集（含多组对齐无解）校验；本表单不生成种子、不派发调用、不改变历史版本。

原生`script.generate`/`script.structure`/`visual_rules.generate`分别读取既有`series_concept`/`script_structure`/`visual_rules`模型政策；使用各自登记的业务响应schema及最终校验。实例冻结对应adapter与policy；本次参数必须明确seed，max_tokens只来自系统资源策略，实例、配方与客户端均不能覆盖。完整准备请求额外冻结`operation_plan`，原Smoke/素材worker读取该计划，不重新从Binding或当前项目组装。剧本素材物化为校验后的Markdown；影视规则只产生待审核候选，不自动应用到项目。

这三类新原生请求的 `payload.messages` 是一次调用内的消息列表：system 包含业务输出协议和已配置的系统约束，user 的 `prompt_profile` 只含 `positive_prompt/negative_prompt`。`operation_plan.plan.prompt_profile_assembly.structured_fields` 保留完整字段审计，但不再复制到发给模型的 user 正文。拒绝未知／非文本通道、非空 `structured_input` 或仅有字段快照却没有渲染正文的组装；影视规则还要求正向输入非空，不隐式读取剧本补齐。此调整不修改业务 API Schema、现存冻结正文或费用记录；结构化业务的必需原剧本和规范化协议的保护词／逐项映射不属于可删除的审计重复。

全剧影视规则的当前协议为`text.visual_rules/v2`，响应schema2只含`schema_version/color_design/negative_constraints/continuity_summary`。不要求模型回显来源ID/hash/字节数。业务输入在“系统设置 → 提示词策略”声明字段，再在“执行实例 → 编辑草稿 → 输入与素材接线”选择`profile_input`目标；推荐`request.theme`（一句话梗概）和`request.style`（视觉风格），不是服务端强制输入名或默认正文。创建准备时可沿原项目引用预览复制梗概/风格并编辑，或填写本次内容。复制来源及是否编辑进入input_provenance；input_baseline保存其摘要、逻辑值摘要及项目规则基线，最终请求再保存实际通道组装摘要。发送给模型的user正文仅含明确组装内容；system由输出协议规则和实际配置指令构成，不自动读取剧本/Bible/旧规则。已发布旧协议需新建版本重新配置，不修改既有请求或借用旧Smoke作为新组合验收。

独立`prompt.normalize`使用`text.prompt_normalize/v1`，输出kind=text/schema1，容量来自模型`prompt_normalization`。operation_input目标`items`为单个JSON列表；每条明确提供request_id、text、source_lang、target_lang、field_key、label、context、protected_tokens。标识与语言不能留空；未用说明填空字符串、未用保护词填空列表。长度和批量上限由原翻译协议及模型合同校验，不截断、不补默认语言、不接受额外生产字段指令。响应须完整且无重复JSON键，逐条核对身份及保护词并还原，结果为`{schema_version:1,items:[{request_id,translation}]}`。精确策略或人工通道只作为本次翻译上下文；结果是待使用文本，不自动写回项目。它可由原实例Smoke及原素材队列的提示词候选请求使用，不是已删除的独立生产规范化阶段，不能生成剧本/影视规则Artifact。项目字段与术语生成共用异步Job/Preparation；术语另有明确确认写回。服务切换已有后续记录，真实专项验收另核对。

文本/JSON逻辑输入允许受控`context_field`：`{kind:"context_field",selector_key,scope:{kind:"project",id},content_revision_id}`。`script.content`返回精确已确认剧本全文；`script.visual_context`同时冻结该修订与同项目当前风格、影视规则和Bible。此处content_revision_id即目录声明的真实ScriptRevision ID，没有额外ContentRevision表或“最新”别名。正文、hash、确认人/时间由后端读取，客户端不能提交替换快照。target的source_kinds/context_selectors限制允许来源；影视规则v2没有强制的source槽位；只接收执行实例明确映射的提示词输入，可填写literal或选择合同允许的来源，不自动使用script.visual_context。新准备检查确认与所有权，已冻结准备不追随后续修订。显式选择script.visual_context时，Bible各实体的primary_setting包含当前确切主图的ID/修订号/SHA及共同身份、基础外观；未建立为null，空正文保持空，坏当前指针/损坏正文返回400，不读取visual_identity或旧修订替代。三类实体共用批量设定读者，后续主图升版只影响新读取，既有冻结输入不改写；不自动增加模型输入或新的审核要求。前端使用现有项目、剧本来源及`GET /api/v1/script-revisions/?source={id}&status=confirmed`分页目录提供选择，不自动确认或启动任务。

配方逐单元的创作字段沿下述确切`shot_field`结构映射进入`request.structured_input`，由策略独立组装，不拼接旧列。执行输入`context_field`目录不再提供`shot.content`及`shot.narrative/detailed_description/action/camera/sound_effects/music/continuity_notes`；新作者请求或已绑定输入使用这些键均返回400，结构来源Schema也不再冻结它们。没有整段Shot JSON捷径、旧键别名或自动转换；已保存的无效草稿在原编辑器保留原值并显示错误，须明确选择确切结构/字段后才能保存。时长、原对白、连续要求及明确关联事实仍通过目录中的`shot.*`结构化来源读取，使用真实`scope={kind:"shot",id}`，`revision_kind=storyboard_approval`，绑定的`content_revision_id`为原分镜审核的精确ApprovalDecision ID。`production.entity.content`（JSON）及`production.entity.description`（文本）使用`scope={kind:"request"}`，`revision_kind=bible_approval`，ID为原Bible审核决定；目标只从原生产准备明确选择的人物/场景/道具读取，不新增实体语义范围或从输入名称猜测。这些已审核来源均要求本轮更早成功阶段的当前一致审核及原内容摘要；未审核、跨项目/运行、目标不在RunPlan、内容变化或旧审核身份返回400，不创建准备/任务。每次规范化共享一次原审核内容核对，来源、单元与值SHA进入原输入证据；重放不替换原值。 主图文字分别由`production.entity.primary.identity_anchor`和`production.entity.primary.appearance_anchor`提供（text、request范围、bible_approval），只读取原审核指向的确切主图修订；同次解析两项共用一次正文查询，`input_provenance.setting`保存该修订完整引用，不将另一段文字自动拼入本字段。缺主图、跨实体、缺修订或引用不符明确失败；保存的空文本原样交给目标字段合同判断，不以旧实体文本补齐。`production.entity.content`删除旧visual_identity和素材关系，在primary_setting内分别返回原审核主图的identity_anchor/appearance_anchor；完整设定引用仍由appearance_settings和input_provenance.setting提供，不拼接成另一份身份文字。明确未建立任何设定时primary_setting及来源setting为null；已有设定却缺唯一主图、引用损坏或跨实体则失败，不以null掩盖。整体JSON与两个单字段在同次解析共享同一确切正文读取。旧production.entity.visual_identity已移出目录，不转换旧配置；原整体Bible审核、实体编辑及其余旧身份消费者已共用设定事实；旧Schema字段按0217删除，实际应用状态以唯一清单当前检查点为准。

配方单值JSON输入支持`{kind:"structured",template}`。模板使用封闭节点；页面`structure_node_options`目录提供：`object.fields`、`array.items`、`unit_map.value`、`each.source/item`以及`literal.value`、`context_field`、`selected_output`、`unit_key`、`reference_subjects`、`item_field.path`叶子。object按精确实例`value_schema`字段接线，array明确顺序；unit_map以原准备的当前真实制作单元标识分组，不能手写该标识。JSON目录中的`shot.duration_ms`、首/尾帧连续要求及`shot.dialogue`仍来自原持久合同。叶子由原配方生产者绑定精确修订或采用身份，仍经过同一权限/范围/漂移核对；来源/目标类型、必需字段、常量、列表去重及动态值不合同时返回400。reference_subjects在同次真实媒体规范化后提取主图片主体；不增加项目主体、不把未登记图片当人物。此规则只用于原Recipe运行，人工来源仍填同一逻辑输入schema，不接受自造生产目标。

独立字段接入处于M5-08分段开发：原配方创建/修改API的structured模板支持`{kind:"shot_field",field:{structure_version_id,node_key,field_definition_version_id},scope:{kind:"shot"},path:[]}`。field三项必填，版本为确切UUID、节点为原node_key；path可省略，整值/深层路径/声明列表的each继续复用M1，不接受客户端Schema、名称猜测、最新版本或新的直接输入协议。需要更早的分镜审核、实例开放shot范围且属于逐镜媒体职责。原API权限、冻结版本与400错误不变，保存只写配方配置、不采用业务值或触发执行；错身份/类型/范围不覆盖原草稿。RunPlan按实际引用保存`structure_source_schemas.shot_field`，键为field对象规范JSON、值为确切M1节点Schema SHA；未引用无此组，不给缺冻结依据的运行补目录。绑定后填入`content_revision_id/source_schema_sha256`，规范化器核对本轮原审核、真实镜头及原绑定Schema，保留原JSON类型、默认/人工修订及审核证据，缺失/歧义/陈旧/越权/累计超限显式失败。原配方编辑器在逐镜媒体路线中提供“字段装配→独立分镜字段”：复用共用结构版本选择器及服务器编译字段目录，选择版本、节点和子路径，不手填UUID或默认使用最新版本。须有前置分镜审核和shot输入范围；已存失效映射原样保留并明确报错，加载/目录失败/字段或目标类型不符阻止保存，可重读目录。换版本清除此项依赖字段/路径，取消时保持保存值与下拉显示一致；只读不能修改。原API、共用策略输入及隔离原页面已分段验证，不含现用切换、业务发布、真实模型或M5整体验收。无新增HTTP路由，根OpenAPI的开放JSON定义形状不变。

结构叶子的`path`是属性名/非负整数下标的数组，例如`["a.b",0,"情绪"]`；含点号和中文的属性按原名读取，不是表达式或JSONPath。`context_field/selected_output/shot_field`省略path或使用`[]`表示整值；`item_field`必须明确path，`[]`表示当前条目整体，item_field旧的`field`路径别名不再支持（不同于shot_field确切身份对象）。路径只能来自服务器声明的`value_schema`，未声明结构的JSON只可整值传递，不从样例、目标Schema或实际产物推断字段。`each.source`可为上述来源或item_field，读取声明的数组；嵌套each的item_field总是相对最近一层条目。共享路径读者同时服务结构预览与正式准备；来源仍先由原读者授权/核验，再取深层值。静态预检不代替真实来源值和最终目标合同检查。

RunPlan初次创建增加`structure_source_schemas`：业务上下文按context selector、原剧本投影按output key、通用文本按`[stage_key,output_key]`的规范JSON键冻结来源结构SHA目录；不是复制字段定义、业务值或选择模型。首次准备绑定前核对同一摘要，随后原绑定叶子携带`source_schema_sha256`，实际解析再次核对。历史计划读取仅验冻结目录形状与原JSON摘要，不查当前目录补齐；目录变化明确报错，不能重解释旧路径。输入证据分别保留原审核/修订身份、`source_value_sha256`、`value_path`、`source_path`、`source_schema_sha256`及提取后`value_sha256`。旧缺此目录的计划不自动补写；本轮只更改开发合同和隔离夹具，没有迁移/清理现用运行。Profile自定义来源路径、实例/配方媒体策略及声明式Profile参考消费已分段接通；通用文本步骤、选择及后端下游字段读取已接入，动态来源目录和选择表单已接入，不视为全部业务输入输出已经接通。

页面入口：配方草稿 → 精确实例路线 → 输入来源 → 按合同逐字段映射。`GET /api/v1/production-recipes/options/`返回`structure_node_options`，实例合同`context_fields`按需附带`value_schema`供受控选择。`POST /api/v1/production-runs/{id}/reference-mapping-preview/`提交`stage_run_id/shot_id/candidate_number/expected_run_plan_sha256`，保留原所有者或管理员权限及`private, no-store`；`value_inputs:[{key,kind,value,provenance}]`展示当前已审核内容解析出的文本/JSON，`inputs`仍为媒体槽位。页面按输入折叠显示解析值与来源证据，并显示该候选路线；失败不保留为成功结果。已有准备不会随当前预览更新，notice明确提示其冻结身份；原执行输入以该准备详情为准。预览不创建准备/任务、不分配种子、不调用模型。当前剩余验收见改造计划第0.1节；实施、隔离验证及后续切换证据见第25节。

`item_field`可显式声明`if_missing`，例如`{kind:"item_field",path:["voiceover"],if_missing:false}`。只在路径最后一级对象属性缺失时使用；中间路径缺失、列表越界、已有`false/true/null`或错误容器类型不被替换，仍接受目标Schema校验。整值或末级列表下标不接受该配置。配置值同时校验来源目录和目标类型；未配置则缺字段报错。实际补值证据记录`resolution=explicit_if_missing/source_field_present=false/value`，原来源及审核身份保持原样。配方编辑器复用目录路径选择器，支持整值、逐级属性和明确下标；重选来源/列表路径会明确确认并清理受影响的条目映射，不自动迁移旧字段。

### 未提交媒体准备的明确策略后继

入口：生产任务准备详情 → “修正未提交准备的提示词策略”。登录后的原运行所有者或管理员可操作；代码入口为 `execution_configuration_replacement`，沿用原准备锁、`supersedes` 替代链与原生产队列，不增加数据表。

| Method / Path（前缀 `/api/v1/execution-preparations/{id}/`） | 请求 | 成功响应与作用 |
| --- | --- | --- |
| `GET configuration-successors/` | 无正文 | `preparation_id/revision/allowed/reason/replacement_id/versions`；只列同一实例及同一策略的已发布明确后继，未选择时不默认采用。versions含id、version、name、profile_name、profile_version、definition_sha256。不可切换时allowed=false并说明原因；已替代则返回原后继ID，空候选不表示可执行。只读、no-store。 |
| `POST configuration-replacement-preview/` | `version_id` UUID、`expected_preparation_revision` 正整数，均必填 | `preparation_id/version_id/revision/preview_sha256/changes`；changes为path/before/after原策略差异。核对状态和精确合同，并试组装原事实，但不分配准备、种子或调用。只读、no-store。 |
| `POST configuration-replacement/` | 上述两项及必填64位 `preview_sha256` | `preparation_id/replacement_id/version_id`；创建明确关联的新准备。只替代策略派生字段，不代表外部调用或媒体成功。相同确认回执重放返回同一后继，不重复分配。 |

请求示意：`{"version_id":"<明确后继UUID>","expected_preparation_revision":3,"preview_sha256":"<预览返回的64位SHA>"}`。不接受客户端自填替代事实。应用时重查原准备修订、依赖状态及新旧定义；策略以外的模型、工作流、输入接线、参数、输出及辅助实例必须相同。复制原已保存输入/素材、source/target language、参数值（含种子）和审核规则，只用新策略重新准备 `profile_sources/field_requests/media_generation_target`；参数来源层中明确归属旧实例版本的标签改为所选后继版本，并按新冻结合同复核，规则、数值、种子及外部来源凭据不变，不重新抽样。不读项目最新业务值，不改原冻结RunPlan、旧输入或已成功任务，不移植旧字段采用。

仅适用执行中、未装配配方的Bible主图/变体及镜头媒体主准备。已有任意主调用、主请求冻结、worker占用、辅助在途或结果未决均拒绝；明确未提交的辅助队列可随旧准备停止，已结束辅助结果保留在旧准备。应用动作本身不派发，随后原worker对新版本重新检查准入并推进，可能产生新辅助调用。未执行阶段的输入修订仍要求该阶段尚无准备/调用，两个入口不互相放宽边界。

非法参数/不兼容版本/条件不满足400；不可见404；原修订、差异或已有后继冲突409；未登录沿原鉴权401/403。停止旧准备和创建后继同一事务，失败全部回滚。页面先展示差异并要求勾选确认；失败显式提示，重读后再操作；响应丢失可原样重试。验证状态与正式应用证据见[短剧实操记录](e2e-auto-short-drama-2026-10-10.md)。

### 未执行媒体与项目文本阶段的输入受控修订

- 接口：`POST /api/v1/production-runs/{id}/stage-input-amendment/`，登录态，原运行所有者或管理员。媒体入口：生产工作台 → 查看逐镜输入映射 → 媒体阶段；项目文本入口：生产工作台 → 当前阻断阶段 → “输入配置阻断？核对执行前受控修订”。服务入口`production_stage_input_amendments.amend_stage_inputs`。
- 请求：必填`stage_run_id:UUID`、`expected_run_plan_sha256:64位SHA`、`action:preview|apply`、`policy`；apply另需预览返回的`expected_amendment_sha256`。无分页；拒绝未知字段、模型、参数或审批身份。规则二需`source_changes:[{route_index:从0开始的整数,input_key:原输入名,source:来源对象}]`，不接受重复、未变更或未声明的输入。请求及修订受原配置字节上限与64KiB较小者约束。
- 规则一`dialogue_missing_voiceover_is_false_v1`：把结构中名为dialogue且直接来自`shot.dialogue_with_speakers`的原叶子改为原顺序逐条映射八个正式对白字段，仅voiceover显式`if_missing:false`。不接受source_changes；明确画外音、时间、正文保留，无匹配或自定义转换不覆盖。已记录此规则的修订继续按其原规则及摘要读取。
- 规则二`explicit_media_input_sources_v1`：用户明确替换原路线中已有文本/JSON输入的来源配置，复用原来源DSL与精确实例校验；不允许更换媒体参考、模型、路线结构、参数或阶段。例：将镜头结构字段放入`{kind:"unit_map",value:<原字段来源>}`，或明确用each从已审核说话人来源组装对白；不自动猜测映射或补值。页面高级编辑区先从冻结路线选择字段，再编辑来源并预览前后差异；所有变更需在同一份修订中确认。
- 规则三`explicit_project_text_input_sources_v1`：复用规则二同一来源修订实现，适用于阶段目录登记的项目文本AI职责（含可重复通用文本步骤，不按自定义步骤名猜测）。预览和确认都校验本轮上游可用性、项目范围及唯一正式选用记录；例如将AI起稿后的视觉输入从初始剧本改接`script_generate / generated_script.script.content / reviewed_selected`。不会读取项目最新剧本作为兜底。`StageRun.input_amendment_policy`为当前阻断文本阶段返回此规则，否则null；这仅表示入口适用，是否仍无准备/调用由确认时原锁内检查决定。页面展示字段级校验原因、前后映射和独立确认，修订后仍需原Run重试，不自动生成。
- 响应200：`status=proposed|applied`、`sha256`、`evidence`（原计划SHA、原/生效阶段SHA及各路线/输入/路径before/after）、`id/created_by/created_at`（未应用时null）、`notice`；`private,no-store`。预览只读，确认只新增不可变`ProductionStageInputAmendment`，不改变RunPlan、状态、审批、产物或费用，不启动生成；相同确认幂等返回原审计。
- 并发/错误：注册表→Run→Stage同源锁，只能首次修订当前未执行媒体或项目文本阶段；有任何准备或调用即拒绝新增修订。错误阶段、原计划或修订SHA不一致、未知字段或坏来源返回400；无权限403，其他用户的运行不可见404。每阶段最多一份不可变修订，已有不同修订不能覆盖；相同修订重放只返回原记录，不支持重规划。
- 读取口径：正式阶段读取器校验修订摘要及原计划后应用，预览与准备共用；映射预览另返回`input_amendment`和`effective_input_sources`，区分原设置与生效设置。新准备intent冻结修订ID/SHA并在重放时核对，已有准备不追随变更。
- 阶段快照：配置/输入快照保留原RunPlan声明摘要与原制作对象集合，重试时仍逐项核验；有效输入通过修订审计和准备intent另行绑定，不把合法修订误判为原计划漂移，也不改写原快照。
- 验证：隔离测试及原运行实际应用/只读核验结果见优化台账第132批。映射通过不代表真实模型生成、选片或成片验收通过。

结构模板也允许`selected_output`叶子：作者格式与原配方已采用文本来源一致，运行时绑定原decision_id/artifact_id/output_key/scope，沿原采用及剧本审核读取器取值；嵌套绑定仍保留其来源阶段授权。可在unit_map内明确选择已采用剧本的项目风格，不退回初始剧本。它不允许任意媒体候选/JSON路径或自动选片；each的列表来源仍只允许声明了条目schema的context_field。

`shot.linked_bible`和`shot.dialogue_with_speakers`的目录另声明`revision_dependencies=[{key:"bible",revision_kind:"bible_approval",stage_key:"bible_review"}]`。作者仍只选择字段与范围；原配方生产者绑定分镜`content_revision_id`及`related_revision_ids={bible:<精确Bible审核ID>}`。规范化器同时核对两个原审核及当前创作内容，证据保留`review`与`related_reviews.bible`，缺少/伪造/旧审核均拒绝。前者只返回镜头明确关联的人物/场景/道具，与production.entity.content复用同一创作投影：不含visual_identity或素材关系，primary_setting含原审核指定主图的identity_anchor/appearance_anchor，完整引用保留在appearance_settings；明确无设定为null，空文字原样保留，坏引用/正文拒绝而不猜取当前修订。一次批量读取所需正文，同次解析按Run、原Bible审核和实体身份复用，重复映射不会逐项重复查询，逐镜结构映射仍拒绝读取其他镜头，返回对象互不污染。后者保留原对白顺序和已填字段，按关联人物stable_speaker_id补入事实字段subject（姓名）与voice_profile（声音设定），不额外加载主图正文；这不绕过前置审核的完整性检查，不按全项目搜索人物、推测时间、追加画外音提示或生成素材绑定。仅在显式选择此来源时提供设定，不自动注入视频提示词。

`script.project_style`沿同一已确认剧本身份读取该项目当前关联的风格，返回name/category/description/visual_prompt/negative_prompt；未关联时返回null，不添加默认风格。它与script.visual_context复用唯一风格事实投影，但只读风格、不查询Bible或主图，不因无关设定损坏阻断；不代表风格已审核或版本化；准备时冻结值及摘要，既有准备重放不重新取值。原已采用剧本输出目录同样提供`generated_script.script.project_style`与`structured_script.script.project_style`，沿原候选采用及剧本审核核对身份，不要求用户改选初始剧本。

配方编辑器在“受控内容来源（取值时点见说明）”按精确实例类型、允许范围、前置审核及协议筛选目录；运行时自动补本次对象ID和目录要求的修订身份，不人工填写UUID。独立素材/Smoke表单只提供其实际支持的剧本修订选择，不伪造生产审核。镜头JSON不含素材引用，实体JSON排除单独管理的素材绑定；它们是创作事实，须经明确structured映射才能组成`reference_context.units`及逐单元上下文，不以普通JSON冒充v21参考原语合同。实际v21字段映射与真实配置验收继续列入P4；已接入这些来源不等于完整生产门禁已开放。

管理员`GET /api/v1/model-execution-instance-versions/{id}/authoring-options/`用于草稿尚未接好必需输入时读取表单目录；返回version_id/revision、resources、input_targets、profile_inputs、input_purposes、static_semantics、parameters、prompt_channels、prompt_not_required、auxiliary_purposes、identity_options。与完整builder复用同一个资源投影器，不冻结合同、不随机/发任务、不返回可提交或验收结论。参数是底层模型/Workflow范围，尚未与此草稿限制取交集；作者配置最终仍须经过input-contract和publish完整核对。无权403、不存在404、不支持的资源/策略400，已发布或退役版本409并要求查看冻结input-contract，不能用当前目录覆盖历史。

通用动态素材目标`wiring.kind=semantic_group`的`reference_bindings`以确切`semantic_key`为键，值为工作流参数已声明的`{category,input_slot}`；固定槽仍使用单个`reference_binding`，分类组仍沿原登记协议。提示词引用及明细只按本次`target_port_ref.semantic_key`读取冻结角色，不按组名/数组位置推测，也不查询当前工作流补齐旧定义。缺失声明时原始媒体接线不产生引用标签，引用原语策略返回`execution_interface_unavailable`；重复序号、越界目录或坏角色明确失败。投影修正后的合同须按原不可变后继发布流程生效，不改写旧合同、准备、调用或验收；原请求权限、端口数量和来源核验保持。

该投影修正会改变含通用动态组的完整定义摘要，包括空角色映射和手动提示词实例；旧冻结合同仍可原样读取，但新的准备/Smoke请求按当前合同核对时要求创建后继版本，不能把历史可读或原始媒体可接线理解为旧实例仍具备新请求准入。后继须按正式流程单独发布并取得当前组合证据，不复制历史验收。

所有输入（包括text/json）的`semantic_contract.allowed_scope_kinds`均由作者明确配置，页面使用同一`identity_options.scope_kinds`目录，不自动授予项目或镜头来源。对于来源种类仅限`context_field`/`selected_output`的目标，完整合同复核和发布要求至少有一种已登记来源同时匹配输入类型与允许范围，否则400并指出`inputs.<key>`。这不改变普通字面量输入权限；实际请求仍核对项目、来源和精确已确认修订，不能用手写JSON伪造快照。配置入口与操作说明见[素材生成方案配置](asset-generation-model-configuration.md)。

管理员`GET /api/v1/model-execution-instance-versions/selection-options/`用于创建草稿前的选择。可不传查询参数，此时返回Schema的提示词方式、状态、输入/目标类型、参数类型/策略/种子规则及主体/范围选项，`target=null`。可传唯一`deployment_id`，并在ComfyUI场景传唯一`workflow_version_id`；返回该精确模型声明的operations/modes、模型ID/模型族、driver、workflow_required及所选Workflow冻结的Projector引用。能力标签复用模型能力目录，不通过提示词用途、名称或输出类型推导。参数重复/未知、无效UUID/不可见对象、未发布模型、跨模型/校验部署工作流或直接API携带Workflow返回400；无管理员权限403。此GET不写表、不外呼、不选择策略、不构建发布合同、不返回can_submit或烟测结论；不存在“选项目录成功即组合通过”的语义。

配置页面入口为`系统设置 → 执行实例`：执行实例及版本分别分页；新建实例后先保存版本组合，再从authoring-options补输入、数量、主体/范围/用途、配对、参数覆盖和辅助版本。更换组合需明确确认清除表单中相关配置，再保存组合读取新目录；不能继续套用旧目标目录。保存草稿只提交实际变化与原revision。查看已发布/退役版本不请求当前资源authoring-options，主面板显示已保存配置，完整合同读取冻结input-contract；发布/退役提交已复核的精确revision/SHA并二次确认，不自动Smoke。页面、接口与后续迁移/服务切换已有记录；当前精确资源和剩余浏览器负向路径仍需专项验收，见统一计划0.1。

`input_purposes`按当前目标实际支持的媒体类型返回模型精确模式的用途目录，成员含value/label/description/minimum_items/maximum_items/source；目前投影模型既有dynamic_reference_topology中的视频/音频用途。不能按模型名内置用途、把视频用途用于音频或为图片/文本凭空补用途；没有受审声明时为空。实例allowed_purposes只能收紧为此目录成员，实际素材展开后的每用途数量、配对和互斥还须由准备解析器检查。声明式参考策略的完整来源与用途接线仍在实施，不把模型用途目录当作原语配置已完成的证明。

`static_semantics`保留所选Workflow明确冻结的非节点控件事实，例如是否输出音频；无此类声明时为空对象，不等于声明音频开启或关闭。它只读，不能作为普通参数覆盖，也不能与同名节点参数并存。静态尺寸/帧率等仍检查模型硬范围；最终请求的联合约束需在实际参数解析后再次检查。

直接策略的生成/翻译来源按generation_source_selector声明文本输入；已选择上下文按`context.<selector>`声明JSON单值目标，该名字是目录键，不是任意JSONPath。fields[]中的usage区分assembly_source/generation_source/generation_context，并保留精确selector；最终输出只能从后续明确选中的字段值取得，目录读取不等于字段已经生成或被采用。完整定义的interface.auxiliaries冻结每用途的version_id/definition_sha256/target_fingerprint/operation，验证每个已发布叶子的真实完整合同及当前执行身份；不含组合Smoke成功证明。嵌套辅助依赖、缺失合同或目标漂移均阻断构建，已发布父版本的只读详情仍读取其冻结引用。

参数行必须显式提交 `seed_policy`：null表示普通参数，fixed表示使用明确种子，generate_once表示每次新请求首次准备时生成一次。generate_once只允许整数、明确有限范围/枚举，不能覆盖底层固定值或同时指定实例default/locked值；参数名含seed不会自动开启随机。素材Job和生产RunPlan适配器均通过Preparation创建器首次初始化，再按持久化值重放；注册/查询接口不调用初始化函数，不因刷新页面或重放请求重抽种子。

#### 执行准备与人工字段（当前代码合同）

准备详情“输入与运行参数 → 辅助字段配置来源”只读保存的`field_requests`，展示确切Profile版本ID/摘要、字段定义版本/原名称及单元标识；展开后通过原`GET /api/v1/registry-relations/`批量读取prompt/field_version配置链接。目录不替换原请求事实；缺失、矛盾、权限及失败显式呈现，重试仍只读。切换准备/折叠清除旧链接并取消读取，迟到响应不覆盖新记录，明确空批次与未记录来源分开。链接沿原深链新标签打开，不生成、确认或采用字段，无新增公开HTTP合同。

未冻结的人工通道或无提示词模式依据冻结执行定义识别，返回 `mode=unavailable` 及明确说明，可在“输入与运行参数”核对已保存输入；不把正常的空 `profile_sources` 当成策略来源，也不为查看提示词触发冻结或模型调用。真正的策略模式缺少或损坏来源仍报错，不能降级成人工模式。冻结分支继续只读保存请求中的实际通道。

完整提示词展示入口是准备详情的“完整提示词与系统组装”。`GET /api/v1/execution-preparations/{id}/prompt-display/?expected_preparation_revision={revision}`仅接受单个正整数修订号；owner/管理员可读，不可见404，未登录401/403，非法或重复参数400，准备陈旧409。响应包含`preparation_id/preparation_revision/mode/prompt_mode/content_kind/request_sha256/frozen_at/channels[{key,text,character_count}]/messages[{role,content,character_count}]/field_basis/profile_basis/sources{references,dialogue}/issues/length_diagnostics`。`mode=frozen`读取正式保存请求：profile/manual对应`content_kind=channels`；none聊天请求对应`messages`，按原顺序展示保存的协议消息，不伪装成影视对白或正向通道；none非聊天请求为`not_applicable`。其他表示的数组为空；字符数为Unicode码点。不会重编译当前配置/候选；冻结不代表调用成功或已发送。`mode=preview`复用正式策略编译器，以原冻结来源、已采用字段为先，其余槽位仅取最新pending/confirmed候选并在field_basis标明candidate；这不是批准、采用或提交授权。缺失/最新被拒绝的必需字段返回unavailable与issues，不拿旧成功版本兜底；损坏的文件、来源、必要消息或合同显式报错，不静默重建。查看无数据库写入、无模型调用，不改变配置或审核规则；原生产组装仍只接受明确采用的字段。正文完整展示并HTML转义，内部ID和指纹折叠；准备变更后清除旧结果。原请求只读、权限/修订、表示类型和损坏输入由`test_execution_prompt_messages_display`及前端对应测试验证；真实操作证据见短剧实操记录。

`sources.references`逐使用展开原冻结参考事实：视频的配对`audio_use`紧随父视频展示，独立音频仍单列；同一素材的两个用途保留各自模型标记与策略引用名，不按Asset去重、不漏掉配对音轨，也不读取今日素材名称替换原事实。此规则同时用于预览和冻结展示，不生成新的审核或来源记录。

策略正常预览、已冻结请求及通道超限诊断均返回`profile_basis{version_id,version,contract_sha256}`和`length_diagnostics`。逐通道包含`channel/character_count/max_chars/over_by/limit_sources[{source,max_chars}]/fragments[{field_key,label,field_definition_version_id,source_selector,max_chars,text_chars,separator_chars}]`；通道`max_chars`是本次确切目标与策略合成后的生效上限（各层最小值），null为未声明上限，不是无限容量保证。`limit_sources`保留冻结目标/策略层的来源及各自上限；片段`max_chars`是该字段规则上限，`text_chars`包含标签及系统投影，`separator_chars`单列，两者合计等于通道总长。字符数沿后端Unicode码点计量，不是UTF-8字节或token。字段版本ID、采用修订和确切策略指纹可追溯，不用当前版本替代。成功编译时明细与正文一并保存；冻结展示只读取该证据和确切定义，不重新组装，缺失或损坏明细显式失败。unavailable及非策略模式的冻结请求为`profile_basis=null/length_diagnostics=[]`。

未冻结请求超出策略或更严格的目标通道上限时可返回HTTP 200、`mode=diagnostic`：保留正式组装器拒绝的完整原文和累计项，不是有效请求或发送成功；不截断、不提高限制、不重新生成，其他提交校验尚未完成。`issues`列明失败，诊断无`request_sha256/frozen_at`；字段自身硬校验、来源完整性等其他错误仍显式失败。真实finalize/dispatch仍拒绝同一超限请求，查看不会采用字段或生成调用。

准备详情的“查看准备字节明细”按需调用`GET /api/v1/execution-preparations/{id}/storage-metrics/?expected_preparation_revision={revision}`。复用原owner/管理员可见性，仅读一条包含准备修订及合同元数据的窄查询，不加载JSON正文、请求文件或字段历史，不查询Attempt后重复计量。响应为`preparation_id/preparation_revision/observed_at/items`，固定依次提供intent、resolved_inputs、review_contract、evaluation、prepared_request五项；每项包含key、label、state、basis、schema_version、size_bytes、sha256、current_limit_bytes、over_current_limit_bytes。`recorded`表示保存时的元数据；只有尚未评估/冻结的可选项可为`not_generated`，其版本/大小/摘要/超出量均为null，不用0代替。JSON大小是规范化UTF-8字节（canonical_json_utf8），不是SQLite序列化文字、数据库物理占用或token；请求文件大小来自原manifest（canonical_request_file_utf8），不代表这次已验证实际文件完整性。当前上限沿原配置及Schema硬边界，不是保存当时的冻结上限；已有大小超过当前上限仍保留原值并显示差额，不截断、重算或提高限制。各项可含重复内容，页面不求和、不计字段历史或重复调用引用；完整正文继续走原prompt-display。成功响应`Cache-Control: private, no-store`；未知/重复/非法修订参数400、准备修订变化409、不可见404、未登录401/403、无效身份403；损坏或必要元数据缺失400，无修复写入。页面切换准备/修订时清空旧结果，忽略迟到响应，失败保留明确重试入口；不会采用、冻结或派发。

最终`prepared_request`仍是授权详情中的完整对象，未冻结时为null；内部从原准备manifest定位私有受管JSON文件，核对所属准备、版本、字节大小和SHA，不向客户端返回文件路径或直接下载地址。索引列表不加载该文件。完整请求及其原生文本请求计划共用`security.snapshot.stage_max_bytes`文档边界，准备阶段与最终组装读取同一上限，不套用数据库配置对象的64KiB上限。聚合准备事实`resolved_inputs`暂时使用256KiB专属存储上限（0196），其余数据库JSON字段仍限64KiB；运行期都受对应系统安全边界进一步收紧，配置调大不能突破Schema硬上限。临时扩大不裁剪来源、合同或审核证据，不改变响应结构；D01独立存储重构已取消，容量核对沿M6，不由旧`OPT-PREP-STORAGE-001`恢复立项。请求计划／传输正文／完整文档超过文档边界时，冻结前保持`reason_code=execution_request_invalid`、blocked，`evaluation.diagnostic`提供`code=execution_request_too_large`及`part/actual_bytes/max_bytes/configuration_key/message`；页面原因包含安全的对象名称、规范化UTF-8字节数和配置键，不包含原文或文件路径，不自动重试、不截断。实际存储／完整性失败保存`reason_code=execution_request_storage_invalid`、`evaluation.phase=request_storage`，保持blocked且不自动重复推进；修复后由原准备“继续”复核，已确认字段不必重新生成。冻结后文件缺失或摘要损坏返回明确校验错误，不回退到当前配置或重新调用模型。此存储调整不改变权限、审核、费用或提交语义。

| API | 请求与结果 |
| --- | --- |
| `GET /api/v1/execution-preparations/groups/?stage_run_id={stage_id}&kind={kind}&page={page}` | 生产页制作对象分组索引，stage_run_id与kind（primary/auxiliary）必填；标准page/page_size受当前API配置约束。返回count/next/previous/results，count为制作对象数；每组unit_key、label、version_count（执行准备记录数）、latest（原准备摘要含presentation）。辅助任务沿同阶段GenerationAttempt.parent_preparation外键的业务unit_key合并，不按标题或解析子任务字符串猜归属；无可核验父关联的准备单独显示。服务端先分组后分页，组按首次创建时间/业务键排序，latest按创建时间/id倒序，不选最后成功覆盖新失败。只读、不读合同正文、原文或发起外呼；owner/管理员可读，不可见404，缺失/重复/非法参数400，空结果count=0。当前SQLite隔离及正式读取验证见优化台账，目标PostgreSQL验收延期 |
| `GET /api/v1/execution-preparations/?stage_run_id={stage_id}&page={page}&kind={kind}&unit_key={unit}` | 原准备明细索引，强制stage_run_id；kind可选primary/auxiliary，省略返回全部。unit_key可选，提供时必须指定kind，沿与分组索引相同的业务对象归属筛选，返回该组历史并按created_at/id倒序；不提供时按created_at/id正序。标准page_size受当前API配置约束，count为执行记录数。摘要presentation展示对象名称、关联调用实际状态及可核验的主准备关联；不读取合同正文、不创建任务。非owner且非管理员404，未知/重复/非法查询400，无记录空页。生产页展开“执行版本 / 历史”时按需读取，可查看原失败及重试详情，查看不重试、不确认采用 |
| `GET /api/v1/execution-preparations/{id}/` | owner授权后读取经摘要核对的intent、resolved_inputs、review_contract、prepared_request和索引；review_summary给出冻结审核方式的mode/label/description。返回field_selections原记录，不能用最新confirmed推测采用项。auxiliary_batches逐项展示冻结字段批次及其最新真实子任务，无批次为[]；未冻结prepared_request为null。response_diagnostic只读原生产文本Attempt/部署Smoke结果的已知文本响应拒绝诊断，返回state/task_id/observed_at/summary/details；recorded表示原诊断核验通过，不是任务成功，unverifiable表示原证据损坏或身份不一致。null只表示没有此类已记录诊断，不表示通过或未失败。字段辅助调用读自身准备，不将其失败冒充接收字段的主调用结果；response_diagnostic不暴露原始响应/远端请求ID；生产及独立素材文本的text_response_review按原调用/请求/文件摘要另行返回已留存正文及只读/可修正状态，查看不确认、不采用、不重试或更新事实 |
| `GET /api/v1/execution-preparations/{id}/smoke-fixture/?expected_preparation_revision={revision}` | 管理员复核相同输入的新组合烟测；严格正整数修订、无其他/重复参数。锁内核对来源，返回execution_instance_version_id、expected_definition_sha256、reuse_preparation_id、expected_reuse_revision、完整fixture及明确field_selections。只读，不继承外呼授权/预算，不创建任务；陈旧修订409，非法输入400，无权403/404 |
| `POST /api/v1/execution-preparations/{id}/continue/` | 仅提交expected_preparation_revision；共用推进服务，汇聚真实字段子任务、检查明确采用项、当前准入后冻结并派发到原队列；返回当前准备详情200，blocked/awaiting_input/review也是真实结果而非提交成功。素材及组合Smoke根已接通文本、受限REST与Comfy原worker；生产Stage根已接项目级原生文本主调用与字段生成/翻译辅助调用。成功派发只创建原GenerationAttempt，不伪造素材任务，不代表阶段采用/完成；未支持的操作明确阻断 |
| `POST /api/v1/execution-preparations/{id}/retry-field-batch/` | expected_preparation_revision正整数、field_batch_key为64位SHA、attempt_number为明确的下一序号且至少2。原准备工作台重试已失败/取消的无领取凭据批次，生产另支持待处理的返回。批次返回`requires_uncertain_acknowledgement=true`时，必须额外传当前调用UUID `acknowledged_uncertain_task_id`，由“确认风险并新增尝试”确认。适用于独立素材/实例烟测未决批次，及生产中已终止、无返回/产物/领取的`execution_provider_failed`辅助调用的未知外部费用；生产`uncertain_remote_response`仍需先恢复原提交。不接受自选模型/策略、任意父准备或覆盖正文。返回当前准备详情200；旧修订409，不满足条件/缺少明确确认400，不可见404。重复或陈旧确认不新增第二次调用 |

独立素材/实例烟测的未决重试仍使用原登录态、owner/管理员权限和父任务锁；只允许主准备未冻结/未取消、原根任务preparing、当前批次无有效领取凭据时新增下一尝试。新调用保存`uncertain_retry_authorized`追加事实（操作人、原调用ID、原批次、原意图摘要、下一序号），它不是原任务终态消歧，不进入`OPERATOR_OUTCOMES`。不改原调用状态、实际费用、响应或冻结请求；原未知费用继续参与原预算投影，新调用另行计价/准入。迟到的旧尝试不向该批次再写字段。后台扫描不会携带此授权或自动重发未决；新字段必须来自新辅助模型的真实返回，仍执行原审核/采用规则。此能力不提供人工补字段的验收替代路径。测试覆盖缺少/错误确认、旧修订、无权限、在途/成功拒绝、单次分配、迟到保护、费用保留及自动字段来源。

生产辅助未知费用重试沿同一父准备、批次摘要与下一尝试序号创建关联准备，复用原GenerationAttempt队列和原审核/采用合同；原Run为running，或因`execution_preparation_review_required`暂停时可操作。锁内再次核对修订、原调用、无领取/返回/产物以及明确风险确认，记录`retry_auxiliary_unknown_cost`生命周期事件关联原调用与新准备。原失败状态、实际费用null、费用预留和冻结请求不改写；新尝试另行计价准入。自动协调器不能代替确认。返回详情中的`can_retry`与风险确认标记由同一判断产生，不按失败状态猜测。

聊天传输错误保留白名单诊断：服务错误码、连接/请求/响应阶段、异常类型及HTTP状态，包括DNS解析、请求配置、凭据读取及网络规则拒绝等提交前错误；不保存异常原文、地址、Header或凭据。明确ConnectTimeout归类未送达；读取超时、连接中断、5xx等无法证明终态时仍保留未决，不自动重发。旧记录没有保存底层原因时不追补推测。
| `GET /api/v1/execution-field-values/?preparation_id={id}&page={n}` | 必须指定有权准备；原服务端分页count/next/previous/results，列表含来源/正文SHA、修订、状态及批准信息，不含正文text |
| `GET /api/v1/execution-field-values/{id}/` | 单条摘要加text；仅有权owner/管理员可读，核对正文摘要 |
| `POST /api/v1/execution-preparations/{id}/cancel/` | 仅提交正整数expected_preparation_revision；停止明确生产主准备及其真实辅助准备，复用原Attempt/GenerationTask取消与账目。200返回准备详情，表示准备已停止而非远端已结束；重放返回原状态。旧修订409，不可见404，非生产/辅助直接取消/已成功主调用/终态运行/已提交装配400。 |
| `POST /api/v1/execution-field-values/` | preparation_id、unit_key、field_key、text、revision、expected_preparation_revision；创建pending返回201，同一修订同正文重放200 |
| `POST /api/v1/execution-field-values/{id}/confirm/` | expected_text_sha256、expected_source_sha256、expected_preparation_revision；可明确select_for_execution（默认false）。替换已有选择还须replace=true及expected_selection_revision；返回field/preparation/selection，未采用时selection=null |
| `POST /api/v1/execution-field-values/{id}/reject/` | 上述三个预期摘要/修订字段，不接受选择参数；返回rejected，不伪造批准信息 |

来源任务和批准身份由后端记录，不接受origin_kind、origin任务、status、approval等客户端赋值；数值修订/布尔标志不隐式转换。素材Job使用实际owner；阶段使用实际Run.owner；基础Smoke仅管理员。不可见404，未登录/禁用身份沿原401/403，结构错误400，摘要/旧修订/已有不同正文/冻结冲突409（execution_preparation_conflict）。同一已确认且已采用动作幂等返回原记录；只有确认、尚未采用时，旧准备修订不能授权新的选择。确认与采用事务原子，替换冲突不会留下新增确认。没有PATCH/DELETE、公共Preparation创建或直接远端提交旁路。GET和字段确认不提交模型；continue及原worker准备扫描使用同一推进服务，人工来源不自动批准字段，不把确认或排队当作Smoke/主生成成功；客户端不能传skip_checks、伪造证据或在continue中追加费用授权。

生产准备停止入口：制作轮次 → 当前步骤的执行准备 → 打开主准备 → “停止此准备与辅助任务”。详情的`cancellation`为服务端实时提供的`allowed/reason/cancelled_at/cancelled_by`；非生产任务为null。只接受原owner或管理员，采用原根锁和修订校验；取消后同一准备不可恢复或重新派发，需要明确新准备。未保存字段或组合验收表单时页面先要求处理编辑，不自动丢弃。停止一个准备不会取消同阶段其他候选或整轮；调用结果、费用和远端不确定性仍保存在原Attempt/任务中。该动作不自动改选上游、采用候选或重跑模型，原Run取消也复用同一任务处理。0161仅增加真实停止时间/操作人，未回填旧取消身份。接口与隔离验证见改造计划25.127，后续切换见25.180–202；不据此推断全部实际取消场景已验收。

辅助批次摘要包含`field_batch_key/purpose/field_keys/attempt_number/task_id/preparation_id/status/reason/can_retry`；尚未分配时序号0、两个ID为null、状态not_started。任务已分配且仍为preparing时，reason展示其自身准备的阻断原因及代码；status仍是原任务状态，不将准备阻断改称模型执行失败。任务已有错误时优先展示任务错误，不用旧准备原因替换；完成后也不沿用准备阻断。重试由服务端核对原根任务权限、状态、冻结版本和最新序号；重放旧修订不会再分配任务。每次实际辅助外呼仍受原Run/Job预算授权与准入限制，新增调用可能产生费用；接口本身仅准备和派发，不同步调用模型。

准备工作台的辅助重试使用页内明确确认：固定主准备ID、修订、批次、原任务及下一尝试序号，只读取服务端`can_retry`，不由失败标签推断权限。“返回”或读取详情不提交；确认前状态变化要求重新读取。字段正文、人工输出补全及Smoke输入未保存时，继续和辅助重试均不可提交，不丢弃草稿。继续／重试／字段保存与审核的迟到响应只作用于原准备读取代次；切换准备或卸载后不能污染新上下文的正文、通知或提交状态。写入回执不明时保留原准备并要求GET核对，前端不自动重放；已提交成功调用不能靠continue重做。页面保护不替代后端权限、修订和队列幂等约束。

GenerationAttempt只读响应包含`execution_instance_version/preparation/parent_preparation/role/auxiliary_purpose/field_batch_key`：preparation是本次外呼自己的准备，parent_preparation是接收字段的主准备，primary时后者为空。策略身份仅引用本次冻结的精确版本和合同摘要。字段结果以原ExecutionFieldValue.origin_attempt或origin_asset_job追溯，重试不覆盖旧结果、不改变已采用项。入口为execution_preparation_views和execution_auxiliary_actions。已完成隔离回归并切换真实服务；实际组合烟测与三个业务入口的验收进展见改造计划25.182，不以代码测试代替业务通过。

字段列表/详情同时返回服务端`approval_label`；实际批准仍以`approval_kind/reviewed_at/approval_policy_sha256`为准。准备工作台显示审核方式、实际批准来源和只读冻结合同。真实RunPlan冻结`execution_fields.accept_valid_v1`：schema3的`field_review={mode:"policy",policy:{key,version,implementation_sha256}}`只能属于无人值守配方；本轮还须明确unattended_confirmed。兼容判断仅匹配注册的`key/version`；`implementation_sha256`保留配置选择/冻结时的代码摘要供诊断，仍须符合SHA格式，但不与当前代码比较、不因部署更新改写。审核合同另外冻结run_plan_sha256/stage_key，实例/素材/单独Smoke不能授予此权力。原评估服务核对全部必需字段、唯一候选、来源/通用字段合同及真实成功辅助任务，再事务批准并建立原选择记录；不会重写提示词、自动选最新或完成阶段。内部代码变化允许继续新采用，不支持的规则标识/业务版本或无效授权仍报409 field_review_stale；多候选报409 field_selection_conflict。`approval_policy_sha256`记录原授权合同摘要，不证明执行了其冻结时的代码；已明确采用的原批准及时间可复用、不重写。该合同复用于schema3配方作者、发布校验和生产准备；无人值守权限、字段采用与阶段审核仍分别核对。语言、正文格式及创作质量不参与字段自动采用准入。

原Job派发与worker提交前读取同一当前准入：资源、精确组合Smoke、当前价格和根预算均重新检查。阻断后保留已冻结请求；ready表示请求已冻结，dispatched表示已交原队列管理，当前是否可提交读取evaluation.admission及原Job状态/原因。上传与主调用分两次检查，上传等待不启动主调用提交时钟；原有队列互斥、远端取消及uncertain禁止重发规则不变。准备扫描使用数据库next_check_at/claim_until恢复，不依赖页面刷新或通知；不增建守护进程。

准备摘要增加reason_code、reason、evaluation_revision及evaluated_at；详情增加nullable evaluation，描述当时字段/准入检查、来源修订与时间。尚未检查时为null；evaluation_revision小于revision时是旧判断，不能当成当前可执行结论。字段变更清除当前原因并回到preparing，GET不自动重新评估。0143为该结构的初始迁移；后续切换已有记录，本文不维护实时迁移状态。

生产主准备的辅助等待仍通过原协调器回读确切子准备/调用。字段和采用未变、当前辅助观察也未变时，不先写入临时字段等待再改回辅助等待，不递增作者修订或使素材改选影响无故过期；真实子状态变化、字段写入/采用及显式重试仍更新原评估。子状态观察与等待写入在原根锁内核对评估所依据的修订；并发字段采用明确冲突，不把旧等待覆盖成当前判断，也不以重新读取修订掩盖旧的缺字段决定。改选保留完整依赖摘要与锁内409校验，不放宽在途、未知结果或已冻结请求的保护。

前端`executionPreparation`及`executionPreparationApi`映射唯一HTTP合同，保留空选择/未冻结与缺失响应的区别、原服务端分页及原文空白。素材创建与首次Smoke共用冻结合同输入表单；素材列表按页选择精确实例版本，待确认字段进入原任务的准备工作台。原素材根创建及continue已接入唯一合同，生产RunPlan也已通过正式生产准备链接入；测试通过不等价于任一当前模型组合或页面负向路径已验收。

生产列表与生产准备详情返回只读`presentation={kind,label,operation,parent,task}`，其他准备详情的presentation为null。kind来自精确实例的operation；label从当前有权项目的镜头编号或设定实体名称读取，仅作展示，不改冻结输入。task为本准备最新GenerationAttempt的`{id,status,error_code,attempt_number}`，没有调用为null；`dispatched`只是准备已派发，不得冒充调用成功。parent为`{id,label,status,unit_key}`或null：列表只取同阶段实际调用的parent_preparation外键，未派发详情由正式冻结来源读取器核验后提供；不根据客户端UUID字符串猜关联。关联不一致显式400，不跨运行展示。主准备和辅助任务分类、制作对象分组在分页前完成；摘要显示最近创建的执行准备，不以旧成功掩盖新失败。历史按需读取原记录，不覆盖失败或将执行版本当成正文采用版本。辅助页提供只读跳转到同阶段主准备，审核与采用仍走原字段API，查看不会派发调用或改变冻结manual/policy规则。元数据批量窄列读取，无准备合同正文或逐项N+1；当前SQLite回归与实页验证见优化台账，目标PostgreSQL验收延期。

顶层`outcome={message,artifact_count,selected_count}`由同一只读准备投影服务生成，供生产准备列表/分组/详情、独立素材准备详情和任务中心详情使用，不在presentation内重复返回。message区分输入受阻未调用、任务登记但仍preparing、派发但调用记录缺失、调用失败、待处理输出、取消/取消中、远端结果不确定和成功无产物；辅助成功不视为媒体成功。生产计数只取选定主调用、同Run/Stage的GeneratedArtifact总数及status=selected数：准备页为最新调用，任务中心attempt_id为确切历史调用，不能混入其他重试；辅助调用计数为0。独立素材读取原AssetGenerationJob，artifact_count为output_asset关联数量0/1，selected_count为0（该入口不定义生产选择），保存文本结果单独说明、不计为媒体Asset。Smoke和无准备任务的outcome为null，仍展示其原校验/输出事实。计数不探测文件或检查媒体质量，不能作为文件可用或整阶段完成证明；停止准备不覆盖原调用终态或产物记录。GET不写入或派发任务，缺失输出不补造；原调用与输出摘要校验仍独立呈现。原配方媒体推进发现cancelled准备时返回execution_preparations_cancelled，不再归入waiting_execution_preparation，也不继续提交同阶段其余准备；不自动重试已停止单元。

字段叶子Smoke通过`GET /api/v1/model-execution-instance-versions/{id}/field-smoke-source/?profile_version_id={id}`读取已发布策略中与叶子操作匹配的真实字段规则；返回实例/策略ID与SHA、系统输入键和规则。`POST`同一路径仅核对并组装测试输入，接收profile_version_id、expected_definition_sha256、expected_profile_contract_sha256和items；每项为field_key、source_text、source_language、target_language、context_facts、protected_tokens。调用方不能提交或覆盖field_contract。返回的literal source供正式Smoke fixture使用，不创建Preparation/任务、不请求模型、不产生验收结果。两动作仅管理员可用；参数/不匹配规则400，摘要变化409，无权403。正式Smoke创建及执行会再次核对冻结规则；测试上下文是显式填写的测试数据，不冒充实际项目事实。

字段叶子冻结规则的协议错误沿共用校验入口保留原错误：新Smoke请求返回400且不分配准备或任务；就绪目录将主调用不可核验的证据标记为`execution_evidence_unverifiable`并阻断，条件辅助叶子标记为`execution_auxiliary_attention`且不计为可用证据，不因该已知合同错误使整个目录返回`readiness_evaluation_failed`。不修补旧规则、不跳过坏记录回退旧成功证据，未知运行错误仍按原失败边界处理。

详情同时返回`field_slots=[{unit_key,field_key,field_label,next_revision,latest_value_id,field_contract,source_language,target_language,effective_language}]`。规则与来源语言来自已核对的冻结字段请求；effective_language按字段language_policy解析（inherit取传入目标，preserve_source取来源，显式语言/双语/any保留策略），不把项目语言误作字段生效语言。field_label是现行字段注册目录的展示名称，空字符串表示未提供名称，不改变冻结合同。next_revision与latest_value_id按单元/字段读取，尚无正文时latest_value_id=null；最新修订仅供查看，不代表批准或采用。前端优先读取已有采用正文，否则读取最新修订，单条正文仍走有权详情接口，不将正文加入摘要列表。没有字段时为空数组，不省略。人工正文合同错误返回400 execution_field_contract_invalid。共用准备工作台先展示正文，修改另存修订；规则与完整历史按需查看，保留确认/明确采用与替换/拒绝/继续，冻结后只读。继续前单独告知原授权可能触发后续辅助与主调用，GET及确认不自动执行。

0144登记execution.admission.smoke_policy（require_passed/require_fresh）、execution.admission.capacity_policy（warn/require_evidence）、execution.preparation.recheck_interval_seconds（1–300秒）和execution.preparation.claim_timeout_seconds（10–600秒）。全部为系统级、非秘密、必需且无需重启的现有配置中心项；不新增平行配置API。初始值分别require_passed、warn、10、60；运行读取须有生效系统修订，不能回退初始值。迁移及原任务消费者统一接线已有后续切换记录；运行时仍须核对当前激活修订，不能因历史迁移默认值推断可执行。

require_evidence阻断unknown/uncovered的生产请求及新辅助调用。只有服务端创建的正式主组合Smoke可对该规格取样，原容量状态和告警保留，决策记录own_capacity_gate_exempt；不标记covered、不预造通过记录。辅助Preparation为production，不继承主Smoke豁免；硬合同、权限、预算、幂等及远端不确定检查均不豁免。普通调用方没有skip_checks或ignore_capacity字段。

作者请求不接受未声明字段、直接写发布状态/版本号/派生正文或旧 Binding；修订与布尔类型不作隐式转换。子项非法会回滚版本分配、父修订和审计。修订/冻结冲突 `execution_definition_conflict` 409、执行目标漂移 `execution_target_drift` 409，结构与配置错误 400，缺失对象 404；沿既有错误信封。相同发布动作重放不重复冻结或审计，但仍核对当前目标；相同退役动作重放不重复写入，换原因或 SHA 返回409。所有动作沿管理员权限，匿名/非管理员按原认证合同返回401/403；没有 PUT/直接 DELETE、preview、组合 Smoke 或执行旁路。

完整定义由同一builder核对精确资源、目标摘要、参数交集、素材接线、提示词通道与输出规则；媒体Workflow、受限REST、字段生成/翻译/起草协议、策略与辅助叶子的完整定义已接入。API模型声明的每个请求字段必须有服务连接字段映射，不能接受配置后悄悄丢弃。注册管理、Preparation与素材原队列已接代码，业务页面和生产消费者仍在切换开发；`frozen=true`只表示返回当时配置，不代表当前目标或烟测有效。

`input-contract`及下述`smoke-contract`统一返回只读`input_forms={输入键:{label,description,schemas:[],suggestions:[]}}`。名称、说明、类型及填写参考来自精确映射目标及已注册的原生协议，不按输入键或模型名称推测语义；同一输入对应多个目标时保留全部约束。该元数据不写入或修改已冻结definition/SHA，不是新的业务许可。原生剧本生成的集数、每集镜数和单集时长显示正整数框，实际总量边界继续由服务端配置核验；复杂值才显示对象/列表编辑。提交仍沿原fixture及服务端校验，没有新增写入接口。`suggestions`项包含`key/label/requires_project`，范例另有`example`正文；范例来自目标schema及注册协议的教学说明，不作为运行默认值。只有允许literal的文字输入提供参考入口；没有精确实例ID的基础资源表单不调用实例引用接口。

#### 外观输入只读目录

M4-02输入能力核对：首次独立Job或配方变体准备，在解析来源/读取素材前复用此目录验证所选`appearance_target.primary_input.input_key`确有主图片端口；未映射返回400，`error.details.appearance_target`含确切输入key及缺端口原因，不创建Job/Preparation或触发调用。仅输出图片、操作名称为图生图、另一输入拥有图片端口均不能代替此检查。页面仍使用原输入目录和原API错误展示，不添加认证页；更正配置沿系统设置→模型执行实例/工作流的原编辑器。已映射后的用途、数量、权限、实际文件和原端口消费仍由原输入解析器核对，不因目录通过而略过；Projector及输出/端口配置沿原精确执行定义构建/预检，不新增全协议或真实Smoke门禁。纯主图/未声明外观仍允许所选实例原有的文生图能力；冻结历史读取不按今日配置重新预检。

原`GET /api/v1/model-execution-instance-versions/{id}/input-contract/`新增`appearance_inputs={primary_inputs,difference_sources}`；沿原认证和实例可见范围，无新增请求参数/写入端点。目录从同一响应的精确`definition`投影，不读取业务正文、文件或当前策略，不改变冻结定义/SHA，也不是实例已通过Smoke或公开变体可提交的标志。草稿投影与发布/退役冻结读取仍沿原语义。

- `primary_inputs=[{input_key,target_key}]`只列配置为`image`且有`is_primary=true`实际Workflow图片槽的原输入；上下文图片、次映射及仅图片输出不能冒充主图端口。确切Asset版本及本次item_key不在目录中，正式准备仍检查原文件与实际槽位。
- `difference_sources`中的人工通道/文字输入为`{kind:manual_channel|logical_input,key}`；参考贡献/结构化直通文字字段为`{kind:reference_field|structured_field,key,field_key}`。来源沿原字段usage和已发布路径结构派生，去重，不列辅助上下文或非文字直通字段；reference_field不提供unit_key，须另从本次原输入选择确切单元。目录无正文、默认值、调用方路径或自动选择。
- 空目录表示该定义无对应输入声明；缺失或畸形目录显式报错，不改用本地猜测或上次实例。素材库图片创建页按需展开“查看本实例的外观输入目录（只读）”，切换实例/用途清空旧目录；失败沿原合同重试。该目录不会写入生成请求、引发自动提交或替代原差异非空/非默认值校验。原素材创建页据此选择原输入中的主图位置与差异来源，目录本身不改写输入。配方变体作者复用同一目录；当前独立上下文来源没有确切单元选项时明确阻止，不推测单元身份。

#### 独立主体身份与语义类别

`input-contract`及`smoke-contract`同时返回只读`subject_categories=[{value,label}]`，当前为`character/scene/prop`（人物／场景／道具）。这是平台可表达的语义目录，不等同于某个精确策略已支持全部类别；实际素材类别与用途仍须通过冻结策略的`reference_rules`校验，不支持时在辅助调用前明确阻断。

素材生成和独立Smoke共用`subject_facts:[{id,category,name,description}]`；每项必须明确选择`category`，`id`为本次主体UUID，`description`可为空，不接受`kind`等额外字段。素材项以`subject_ref:{kind:"external",id:主体UUID}`引用；`external`只表示本次独立主体身份，不是渲染类别。普通分类图片使用显式类别选择策略规则，固定首帧／尾帧仍保留其槽位用途；主体关联仍以`external + id`匹配，不伪造项目Bible身份或采用决定。字段与所有权／范围／素材权限沿原校验执行。

真实项目主体仍以`subject_ref:{kind:"character"|"scene"|"prop",id}`引用；仅在实例允许该类型、当前所有者/项目可见且该素材是主体已登记anchor或reference时有效；分镜reviewed项则核对原ShotReferenceUse的确切选择，不要求另写Bible默认池。解析后的`logical_values.<key>[].subject`只保存`kind/id/project_id/name/description`，外部主体保存`kind/id/category/name/description`，无主体明确为null。这是所选图片的身份关联，不是隐式选择主图文字：不复制旧visual_identity，也不补入当前主图锚点或修订；需要主图文本时仍须显式配置原context_field。主体说明和素材description/prompt_hint在原解析事实中保留原值，名称/说明在新解析时重新读取，已冻结输入不跟随其后修改。参考策略投影按本次冻结输出合同区分图片/视频：图片仍可取这些外观来源；视频不自动把主体描述、素材description/prompt_hint及配套音轨的对应元数据送入辅助字段请求或参考渲染。视频仍保留主体名、确切来源摘要、使用标识、端口/顺序及范围；用户在策略中明确选定的动态字段、上下文和原对白按原值传递，不靠关键词或正则删改正文。已有准备与历史请求不改写，本规则应用于新准备。

M6-02新准备的参考对应记录同时保存`original_name`（主体名优先，否则原Asset名；未记录名称为空）、`entity_label`（本次使用的主体/素材可读名）和`name_disambiguated`，以及原`item_key/entity_id/asset_id/artifact_id/source_sha256/input_label/target_port_ref/unit_keys/purpose`。同一调用先按确切主体类型与ID分组：同一主体的主图、变体及多视角共用主体可读名，不因多张图片而消歧；没有主体关联的素材使用各自成组。再沿共用Bible名称冲突键（NFKC＋casefold）识别组间同名/等价名称，仅对冲突可读名追加组内排序最先的确切使用标识，不依赖输入位置；保留原名字节，也防止生成名称撞上另一个原名。配套和独立音频在同一名称空间内参与，不把同名素材合并成实体。每张图仍有独立使用键、来源、版本、端口和Picture标签；重排不改变主体或确切使用身份。辅助字段和最终参考渲染消费同一冻结记录；名称不一致与真实用途不一致分别报错，正文不做名称替换，形似`<Picture n>`的名称不能登记额外模型槽位。没有增加用户必填用途或新的HTTP写入字段；正式只读对应展示见下述reference_binding，旧冻结记录不重新命名，不将原文件名替代策略引用名。

原任务`input_parameters.media_uses[].reference_binding`与任务中心`input.media_trace.items[].reference_binding`共用`execution_reference_display.frozen_reference_display`。字段为`position`（本次逻辑输入内0起位置）、`original_name`（冻结主体/素材原名）、`reference_name/name_disambiguated/input_slot`（原策略参考上下文保存的逐使用引用名、消歧标记、1起槽序号）及`shot_reference`（上述确切分镜选材证据或null）。没有策略名称对应时后三项为null，不在读侧重新计算或把原素材名冒作策略名；版本来源未记录与直接Asset未选系列版本分别展示。读者复用原合同摘要和素材使用校验，逐项核对使用身份、来源SHA、Asset/Artifact、用途、版本、主端口与标签，含配对音轨，缺失/矛盾明确失败而非重算。该新增只读字段不改变请求、Schema或审核依据；旧数据不回填。正式分镜“工作流历史请求”、媒体任务详情和任务中心接线面板复用`FrozenReferenceCorrespondence`，逐项显示原名/消歧名、系列版本、设定修订、创作原值与实际输入顺序，模型用途及实际标签仍单独显示。技术身份折叠；上下文素材不虚构槽位，预览失败不替换素材。开发验证使用正式组件及隔离响应；现用后端重载、真实模型与完整M6交付仍按模块检查点另验。

M6-02逐使用版本来源沿原解析详情冻结：镜头范围的`reviewed:`项在服务端重验后增加`shot_reference`，完整保留共用M5读者的`use_id/use_key/position/media_kind/purpose/setting/source_mode/source/asset`。其中`source.kind=reference_version`才有确切`reference_version_id/series_id/version/acquisition_source`；直接Asset保持`{kind:asset,asset_id}`，不通过同Asset关联或最新版本猜系列。`setting`为空或包含原`appearance_id/setting_revision_id/revision/content_sha256/entity_kind/entity_id/appearance_role/reference_name`，不携带身份/外观正文；`asset`保存确切ID、sha256和mime_type。原创作`purpose`、选材位置与本次模型用途/输入顺序分别保留，不能相互覆盖。未关联Bible主体的用途同样核对确切选择，不能靠省略subject_ref绕过。该证据进入原source_sha256、准备输入及参考/辅助请求，后续当前选择、版本或设定改变不修改已冻结记录。客户端不能提交shot_reference；普通独立Asset/已采用产物不补造这份分镜来源。冻结Workflow与参考消费者共用形状/身份/指纹交叉核对，缺失或损坏明确拒绝、不查当前库补值；这不替代原准备的审核/陈旧校验、当前素材授权或实际文件核验。原详情API仍返回同一resolved_inputs及field_requests，不新增端点或可编辑来源表。

参考提示词投影与工作流素材装配共用冻结主体校验：核对允许类型、精确字段、UUID、名称/说明及外部类别，连同原逐使用SHA/端口检查执行。旧身份字段、缺subject（与明确null不同）、缺类别或其他畸形事实即使重新计算摘要也明确失败，不静默忽略或转换；须沿原入口重新准备。冻结投影不查当前主体来补值，实际素材上传仍沿原授权与字节校验，不因冻结主体可读而免除素材有效性检查。本段无新增HTTP请求字段、业务入口或现用数据转换。

类别随规范化来源进入冻结输入及摘要，修改类别会改变来源SHA。此目录不修改已发布实例definition/SHA。历史缺类别输入不回填、不猜测，原准备、字段、费用与审核记录保持不变；需要不同输入时通过正式表单创建新准备，不能以原幂等键覆盖旧记录。缺目录、缺类别或非法类别显式拒绝，不默认成道具。

#### 提示词语言设置

上述两个合同读取接口同时返回只读 `language_form`：`choices=[{value,label}]` 与策略编辑共用语言标签目录；`allow_custom` 声明其他语言代码入口；`source/target={required,fields:[]}` 表示来源/目标语言依赖及实际作用字段，fields为空表示无需填写，非空且required=false表示使用这些可选字段时才需要声明；`fixed_fields=[{label,language}]` 解释策略已固定的语言。该说明从摘要核对后的精确策略及输入映射生成，不按模型名或输入键推断、不写库、不更改实例SHA；策略与实例摘要不一致返回400并停止展示，不能以空目录伪装无需语言。页面显示中文选项但提交原fixture中的source_language/target_language，不做自动检测或翻译；缺少必需声明时提示用户选择，最终仍由服务端字段规则校验。

语言上下文不是所有原始输入的统一语言声明，也不是剧本/对白/视频输出语言覆盖。preserve_source生成字段依赖source_language，inherit字段依赖target_language；固定语言按策略执行。直通/服务器锁定字段若装配值与该字段明确冻结来源的类型和值完全相同，以原文未变证明preserve_source，不用项目语言推断上游语言；默认值、投影改写、模型生成/翻译和人工替换不适用此证明。比如创意需模型处理且映射preserve_source、风格只映射原生业务输入时，创意英文应选en，风格可保留自己的原文。多个需处理的preserve_source字段若实际使用不同语言，现有单一上下文无法逐字段表达，页面明确提示，不伪造混合语言标签或偷偷翻译。英文字段组合和原文不变已由隔离准备/编译测试覆盖，不代表模型真实生成验收。

对白发声语言独立保存于每条 `dialogue.language`，不使用上述 `source_language/target_language` 或项目语言覆盖。`script_semantics.v1`、分镜对白、`shot.dialogue_with_speakers`、参考上下文和冻结请求展示共用此事实；字段可用语言名称或代码，不自动检测或翻译。旧内容缺键或空值仍表示未声明，读取不回填。新结构可配置为必填，有台词时由创作／结构化策略逐句声明，没台词时可明确留空。导演及演绎语言仍由对应字段策略决定。

参考渲染模板的 `{language}` 仅从当前对白取值。模板实际消费该占位符且有台词时，缺失、空值或纯空白会报出单元及 `dialogue[n].language`，不退回项目语言、不生成空标签；未使用占位符的模板不额外要求此字段。对白语言标签禁止控制字符及模板分隔标记；`text` 逐字保留。更新输出结构／语义映射后，还必须让执行版本与配方来源显式传递该字段，不会自动升级旧版本或重写历史请求。旧定向修订 `dialogue_missing_voiceover_is_false_v1` 的八字段转换保持原合同，不能用于传递新语言字段；新配置应采用完整对白来源或明确包含语言的映射。

#### 手工输入的参考预览

- `GET /api/v1/model-execution-instance-versions/{id}/input-suggestion/`，登录鉴权；普通用户仅能读取可选择实例，项目必须属于当前用户。代码入口为`ModelExecutionInstanceVersionViewSet.input_suggestion`，页面入口为人工生成准备中的“使用项目梗概”“使用项目视觉风格”“查看填写范例”。
- Query：必填`input_key`、`suggestion_key`（均来自该精确实例`input_forms`目录），可选UUID `project_id`；引用项目内容时必填。示例：`?input_key=idea&suggestion_key=project.logline&project_id=<当前项目UUID>`。无分页，不隐含选择项目或最新剧本修订。
- 响应：`input_key/definition_sha256/label/content/reference/origin`。`content`是本次预览正文，`origin`记录来源及原对象更新时间；`reference`为服务端校验的预览来源凭据，不是模型调用授权。梗概读取项目`logline`，视觉风格只读取关联Style的`visual_prompt`，不混入名称、简介或负向约束，不触发翻译、生成或任何持久化写入。
- 内容为空、项目缺失/非本人、目录项无效返回400；未登录401/403，无权访问实例或实例不存在404。页面显示错误并允许重新点击读取，不用默认范例或旧预览替代失败。
- 用户显式填入并编辑后，原fixture的文字输入提交`{kind:"literal",value:"最终确认的正文",reference:"预览响应中的凭据"}`；不引用时仍提交`{kind:"literal",value:...}`。服务端沿原来源解析链核对所有者、项目、输入键及精确合同SHA，把预览正文、来源、摘要、读取时间和是否修改写入已有Preparation解析事实；实际输入仍为`value`，不重读项目替换。凭据不能跨用户、项目、输入或合同使用，不授予上下文来源权限，不允许JSON/媒体输入携带。
- 原请求幂等重试及Preparation冻结逻辑不变。预览后项目发生修改，不改变已预览并明确采用的文字；如需使用新的项目正文，显式重新预览。只读预览不创建审计对象；来源随正式准备保存。测试覆盖空值、越权、凭据篡改、字段错配、编辑后的冻结值及准备持久化；本接口不代表真实模型验收。

#### Bible 对象描述的可编辑预填

前述可编辑副本另支持 Bible 对象描述预填（同一 `input-suggestion` API，不新增生成入口）：

- 精确实例的文字输入映射到策略 `request.subject` 且允许 `literal` 时，`input_forms` 提供 `production.entity.description`、`production.entity.primary.identity_anchor`（主图共同身份）与 `production.entity.primary.appearance_anchor`（主图基础外观）三个建议，均带 `requires_production_scope=true`；只有描述项带 `prefill=true`。主图两项分别预览，不拼接、不自动替换本次输入。按目标语义派生，不依赖实例自定义输入键的拼写。仅人工 Bible 参考图准备具备明确对象时自动预填空白描述，不覆盖已填写内容；其他入口不显示缺上下文的建议。旧视觉设定建议已移除，旧签名凭据也不能绕过来源复核作为普通引用使用。
- Query 除 `input_key/suggestion_key/project_id` 外必须同时提供 `production_run_id/entity_kind/entity_id`，类型为 UUID／`character|scene|prop`／UUID；部分上下文返回400。例：`?input_key=request.subject&suggestion_key=production.entity.description&project_id=<项目UUID>&production_run_id=<本轮UUID>&entity_kind=scene&entity_id=<场景UUID>`。
- 复用本轮已采用剧本的原 Bible 来源、审核和实体读取链：描述读取已审核实体description，主图两项读取该审核引用的BibleVariantRevision身份或外观，不解析最新剧本、不读取全剧正文、不补写或翻译。`origin`含制作轮、对象范围、计划SHA、采用剧本修订／采用决定及Bible审核决定／修订／摘要；主图项另含`setting`（造型/修订ID、修订号、SHA、真实来源及原审核引用），显示原文和主图设定修订供人工对照编辑。不要求已有图片；缺主图或所选文字为空明确提示，用户仍可按literal合同直接填写，不增设创作必填门禁。响应不缓存（`private, no-store`），读取不创建准备、不调用模型、不批准内容。
- 最终仍提交普通 `{kind:"literal",value:"本次确认正文",reference:"原预览凭据"}`。首次保存复核所有权、项目、本轮、对象、可引用的前置审核以及原审核版本与字段内容；越权、跨轮／跨对象、缺审核、来源已变化或凭据被篡改均拒绝并保留表单，不自动改用新版本。来源原文／原审核身份与人工修改标记记入已有 `input_provenance`，实际正文写入 `logical_values`；不反写剧本或 Bible。保存后仍遵循原 Preparation 冻结／幂等逻辑，旧任务不补填来源。
- 这不是新来源类型注册或自动批准：尚未取得本轮 Bible 审核时必须明确提示。合法的本次手工正文仍由实例的 `literal` 合同允许；不要把手工正文当作已审核 Bible 原始事实。手工整段提示词模式不擅自添加另一套主体映射。

#### 项目已保存事实的受控引用

- 同一 `input-suggestion` API 和准备表单也消费精确 `profile_inputs.source_reader`。策略选择 `project.logline` 或 `style.visual_prompt` 后，实例无需再配置同名输入；创建准备时点击“预览并引用项目梗概／项目视觉风格”，再点击“确认引用此项目内容”。项目工作台限定当前项目；独立Smoke／素材入口无项目时须先从当前账号拥有的项目中明确选择，不猜最近项目，不回退示例。
- Query 使用该输入表单目录的 `input_key/suggestion_key/project_id`。预览只读取授权项目 `logline` 或关联Style的 `visual_prompt`；风格仅允许本人或公共记录。不查询完整剧本、人物库，不生成、翻译或写任务。响应的 `origin` 增加项目身份／名称、来源页面、对象更新时间；凭据绑定精确读取协议、用户、输入、合同SHA、来源项目、内容摘要和预览时间。
- 确认后提交 `{kind:"project_snapshot",reference:"预览凭据"}`，**不提交可替换的value**。服务端在原输入解析器复核当前所有权（管理员权限不代替业务所有权）、本次项目范围和协议／输入／SHA一致性，采用凭据内确认的原文，写入已有Preparation `logical_values/input_provenance`。项目发生修改时仍保存明确确认的旧预览；需更新时显式重新预览确认。来源权限丢失、项目不匹配、正文注入、伪造凭据或合同变化返回400，保留表单并要求修正；之后继续/执行仅读取原准备快照。
- 这是两个已实现读取器的版本化消费路径，不是页面自定义来源注册。`project.*`/`style.*`其他字段、辅助生成的额外context及Recipe自动绑定尚未一并扩展。需要编辑副本时选择声明 `request.theme/request.style` 的策略，不得把修改后的正文标成项目原始事实。发布定义和真实历史请求不重写，无新数据表或迁移。

#### 基础资源烟测输入、创建与恢复

工作流版本详情的素材摘要区分固定槽位绑定记录与执行暂存数量：`asset_bindings` 为空只表示没有固定绑定记录，不代表没有动态参考输入；`result.evidence.staged_input_count` 仅展示非负整数回执，缺失或无效时明确未知，不补成0，也不当作素材身份、用途或物理槽位。“查看本次完整输入、动态参考与调用记录”沿本次 `preparation_id` 打开任务中心原共用准备详情，逐次使用的身份、用途、顺序、实际端口及暂存事实只从原冻结输入读取，不再在摘要复制解析器；缺少准备关联则说明无法核对。这里只读，不创建或重发Smoke，历史成功仍须按当前验收合同核对有效期。

- 接口卡片：原`/api/v1/model-deployments/{id}/`和`/api/v1/workflow-template-versions/{id}/`对象下提供`GET smoke-options/`、`POST smoke-contract/`、`POST smoke-field-source/`。登录管理员权限，沿用原CSRF规则；代码入口`execution_resource_smoke_views.py`。这些接口供原资源烟测表单读取输入规格，原`POST smoke-test/`负责创建原SmokeAttempt与Preparation，不是另一条生产入口。
- `smoke-options`不接受Query，返回现有执行选择目录结构：`target`明确部署、模型、能力、模式及Workflow身份；图资源只从该工作流已记录的在线校验部署读取，不从Comfy部署猜测工作流。基础图不提供策略组装模式。返回选项并不等于已通过在线校验、可提交或烟测通过。
- `smoke-contract`请求必须明确`operation`、`mode`、`prompt_mode`（`profile/manual/none`）和`prompt_profile_version_id`。profile必须提供已发布精确策略UUID；manual/none必须为null，不能附带Binding、实例、scope或自编目标。示例结构为`{"operation":"script.generate","mode":"","prompt_mode":"manual","prompt_profile_version_id":null}`，实际能力和通道以所选模型合同为准，并非所有模型都接受该示例。
- 成功200返回`scope=resource_smoke`、`version_id=null`、`schema_version/size_bytes/sha256`、`target_fingerprint`、`definition`、`context_fields`。definition使用同一纯执行合同，包含实际输入、类型、数量、配对、参数规则、通道和输出；不返回虚构的实例revision或frozen标记，不填入业务测试值。
- `smoke-field-source`请求`selection`为上述选择对象，另提供字段规则来源`profile_version_id`。不传items时读取真实已发布策略中符合叶子操作的字段规则；组装时必须同时提供`expected_definition_sha256`、`expected_profile_contract_sha256`、`items`，各项字段同实例字段来源接口。返回相同规则/来源结构，但`execution_instance_version_id=null`；规则来源不等于给叶子绑定主策略，不能提交field_contract覆盖规则。
- 原`POST smoke-test/`需Header `Idempotency-Key`，Body为`selection`及`expected_definition_sha256/fixture/field_values/allow_external_calls`（形状同组合烟测，不接受reuse）。首次202、同键同请求200，返回`{attempt_kind,attempt_id,status,preparation}`及Preparation详情Location；准备的purpose为resource_smoke、实例FK为null。准备阶段不外呼，主deadline只在正式进入原队列时启动；缺少策略字段留在原准备中人工补充、核对和采用，不自动调用辅助模型。图资源创建必须先完成原Live Validate。
- 原SmokeAttempt列表/详情返回`preparation_id`，没有原准备时明确null；列表支持scope=resource或instance过滤。页面从“准备与输入”进入共用Preparation详情/continue/字段审核工作台。原cancel动作返回202；未提交同时取消根任务和准备，进入提交边界后由原远端取消或消歧，不把取消受理当作远端终态。基础与组合验收不能相互替代。
- 数据口径与副作用：选项/合同/字段来源读取真实模型/部署/工作流/策略合同及上下文目录，使用原注册表事务保护，不创建实例、Preparation、SmokeAttempt、假项目或字段记录。仅原smoke-test明确创建原两类记录；不读取旧PromptProfileBinding、不接触Provider/GPU、不产生通过证据，读取失败不回退为内置样例。
- 原worker完成后，基础Deployment快照与Workflow证据保存schema2精确引用：原SmokeAttempt、Preparation、结果/请求/定义摘要；不复制产物正文、不借用实例组合证据。Workflow另冻结当次Live Validate身份；晚到结果不得覆盖较新的校验代。基础发布核对同一原请求与产物结果，过期返回expired，不延长原任务TTL。
- ComfyUI主产物收取先核对冻结输出节点、任务目录和真实文件，再核验媒体尺寸/帧数/音轨；原任务的`artifact_facts`记录已验证的`kind/node_id`、真实MIME/字节数/SHA及媒体观测。不能仅凭远端succeeded写通过，也不能以回执自称的类型或哈希替代真实文件探测。
- 失败处理：字段不完整、不匹配合同或非法联合400；所选对象删除404；未授权403；定义/目标摘要变化409。页面应显示错误并重新读取，不继续使用旧选择。原资源准备现在通过同一内部准入与原worker推进，不再因尚待切换而一律阻断。权限、资源/Live Validate、费用授权、取消及不确定提交仍按当前事实检查；基础证据和组合证据不互相替代。资源媒体原worker的14项隔离回归通过，其他生产消费者及清理切换仍在实施；不视为真实模型或浏览器验收。

#### 执行实例组合烟测

规格夹具复用：除正常必填body外，同时提交`reuse_preparation_id`、`expected_reuse_revision`和`field_selections=[{source_preparation_id,field_value_id,unit_key,field_key}]`（明确空数组表示不复用字段）；此时`field_values`必须为空。fixture与来源准备输入/参数必须一致，实例定义必须相同。来源请求存在`appearance_target`时，`smoke-fixture/`会原样导出，提交须完整保留其主图/变体声明、确切主图版本、图片输入位置与差异来源；不可遗漏、更改，也不可在非复用验收中新增。保留原单元键、解析事实及一次性种子；逐项核对已确认且已采用的字段修订，只引用原批准，不选择较新版本或重跑已复用字段。来源修订冲突409，字段未采用或输入不一致400。沿用有效staff的既有全局准备管理权限；停用账号不可操作。原请求的幂等重放不受来源后续编辑影响；更改来源/选择/授权则冲突。

- 接口：`POST /api/v1/model-execution-instance-versions/{id}/smoke-test/`，登录管理员权限及原CSRF规则；Header必须为本次用户意图提供`Idempotency-Key`。入口为执行实例验收；HTTP服务与“烟测记录→准备与输入→用相同输入做一次新烟测”复核表单已接入。首次输入和复用表单均使用共同合同/准备服务；当前精确组合和复用负向路径的真实验收另行记录。
- Body必填：`expected_definition_sha256`；`fixture={inputs,parameters,manual_channels,subject_facts,source_language,target_language}`；`field_values=[{unit_key,field_key,text}]`（可空）；`allow_external_calls`布尔值。资源预算使用当前系统策略，不接受单次budget_limit。独立验收使用`smoke`单元；输入按本版本input-contract填写。不能传scope、跳过检查、已通过证据或批准标志。
  - 例如人工文本夹具可使用`inputs:{}`、`parameters:{}`、`manual_channels:{positive_prompt:"描述一只蓝色纸盒。"}`、`subject_facts:[]`及明确语言值；参数/通道仅在所选版本合同允许时有效，不是所有模型的默认夹具或默认范围。
- 首次202，重放200，返回`{attempt_kind,attempt_id,status,preparation}`；`Location`指向正式Preparation详情。原Deployment/Workflow SmokeAttempt与准备同事务创建，缺待填字段仍可preparing/awaiting_input。相同幂等键但不同fixture/字段/授权/定义409；活跃或未消歧任务冲突409；结构错误400、权限403、不可见对象404。读取详情不外呼。
- 原Attempt记录本次授权、预算和自己的预估/实际费用；子字段调用仍用原AssetGenerationJob，各次费用分别记录，根仅聚合。未知实际费用保留null及估算占用，不伪造零费用或已结算。准备等待不启动Smoke主deadline；正式进入原队列才初始化。组合结果只更新本次Attempt，不改变基础Workflow/Deployment的通过状态。
- 当前组合Smoke的创建→continue/原扫描→原claim→冻结请求传输→组合证据已接入文本、受限REST与Comfy原worker，并有后续切换记录；当前具体组合的真实验收仍须单独核对。Comfy复用原动态素材暂存，上传与提交分别检查准入；释放轮询租约不释放远端GPU占用。明确完成后校验实际输出，取消受理需继续查询终态；不确定结果不重发，不能冒充基础Smoke通过。规格夹具复用、字段审核表单、分页证据及部分容量观测已接正式入口；完整容量测量与重建后当前组合的真实验收仍待补齐；旧Binding已退役，不将其初始迁移工作重复列为待开发。

Comfy内联text.generate结果保留冻结能力身份，读取指定节点的唯一文本并核对摘要/字节，复用原文本结果落库。纯内联输出不需要output.prefix；合同若明确提供该语义仍必须匹配原准备专属前缀。媒体文件始终要求前缀及文件证据，不因文本支持而放宽文件路径规则。

#### 组合烟测证据查询（已接入，精确版本验收独立核对）

- `GET /api/v1/model-execution-instance-versions/{id}/evidence/`：管理员登录态；读取此精确版本的原ModelDeploymentSmokeAttempt或WorkflowSmokeAttempt及唯一Preparation。入口为“模型执行实例→版本→烟测记录”；不探测主机、提交模型、更新TTL或重建历史配置。
- 可选查询：`status`为原任务状态；`page`为正整数，`page_size`为1–200且受既有安全分页上限约束。未知、重复、非法或执行器不支持的状态返回400；非管理员403、对象不存在404、冻结定义冲突409。草稿没有冻结合同，不伪造验收摘要。
- 响应：`summary`包含精确`identity`（version_id/definition_sha256/target_fingerprint/operation/mode/scope=instance_smoke）、read_at、evidence_state（missing/failed/fresh/expired）、latest_completed_attempt_id、finished_at、expires_at、冻结部署/Workflow/策略版本引用及当前准入策略来源。摘要校验最近适用完成记录的正文、请求和索引；筛选“成功”或翻页不会隐藏后续失败。摘要不是can_submit，也不是容量范围认证。
- 历史响应复用`count/next/previous/results`分页；每行含原attempt_id/attempt_kind、任务状态/时间/错误码、preparation_id/status/revision/reason_code、request_sha256及估算/实际成本/币种。列表仅投影窄字段，历史任务status不替代已核对的摘要；不加载正文、凭据、请求体或供应商请求ID。actual_cost=null为未知，不以估算冒充实付。
- “准备与输入”按需读取既有`GET /execution-preparations/{id}/`，显示原准备、参数、明确采用来源及评估；冻结请求尚无时明确提示，不运行新任务。证据正文与索引不一致时显式报错，不取更早通过记录兜底。
- 组合烟测历史的“请求取消此烟测”打开平台内确认弹窗，展示原调用类型、ID及费用／历史保留边界；明确确认后复用原`POST /model-deployment-smoke-attempts/{id}/cancel/`或`POST /workflow-smoke-attempts/{id}/cancel/`，不新增任务或取消 API。确认期间暂停详情刷新，重复提交受阻；切换任务或账号后旧确认失效，迟到回执不污染新任务。取消失败留在确认页说明未确认结果；取消已受理但详情刷新失败时只重读，不重发取消。未提交任务可停止准备；已提交的同步文本不可安全撤回，工作流交由原队列请求取消。页面重新读取服务端终态，不把提交取消请求当作远端结束。
- Preparation详情增加只读`capacity_sample`，无样本时为null；有样本时包含id、observed_at、elapsed_ms、signature_sha256（缺维度为null）、dimensions_complete、known_dimensions及missing_dimensions。来自本准备原任务的成功终态，读取不重新测量、不更新证据。耗时含远端排队、轮询和收集，不代表GPU耗时；部分样本不证明容量已覆盖，普通业务样本也不代表Smoke通过。当前缺少受审token测量或预处理后媒体测量的请求仍明确unknown。
- 已进行隔离HTTP/查询与前端适配测试；后续迁移与整体切换已有记录；此处不宣称所有新页面已完成真实专项验收。

### 验收与烟测集中查询

`GET /api/v1/acceptance-smoke/` 是 staff 平台管理员使用的唯一集中查询接口。它只读取已持久化的模型、工作流、Probe、Live Validate 和 Smoke 事实，不调用 Provider/ComfyUI，不创建 attempt，不修复或改写历史状态，也不把读取时发现的漂移回写成新的验收结论。对应正式页面为 `/settings/acceptance-smoke`；页面负责集中展示、筛选和解释证据，只跳转到既有唯一动作表单：ComfyUI 的 Live Validate、Workflow Smoke 与发布仍在 `/settings/workflows/{workflow_version_id}`，`async_rest` 的 Deployment Probe/Smoke 与文本 Deployment Smoke 仍在“设置 → 模型”的实例操作区。集中页不得复制 fixture 编辑器或直接建立第二套提交路径。

查询参数如下：

| 参数 | 约束与含义 |
| --- | --- |
| `subject` | 必填，枚举为 `workflow` 或 `deployment`；决定结果以工作流版本还是模型部署为主对象 |
| `search` | 可选；按服务端声明的安全名称和标识字段筛选，不改变关联判定 |
| `deployment_id` | 可选 UUID；筛选与该实例有关的精确验收、执行或兼容结果 |
| `workflow_id` | 可选 UUID，且仅允许与 `subject=workflow` 同时使用；用于工作流详情精确读取当前 freshness，其他组合返回 400，不得静默忽略 |
| `page` | 可选，正整数页码；格式无效返回 400，请求不存在的页沿用 DRF 标准 404 分页错误 |
| `page_size` | 可选，服务端每页最多返回 200 条；大于 200 时钳制为 200，格式无效返回 400，不得绕过服务端分页 |

成功响应使用标准 `count / next / previous / results` 分页合同和稳定排序。客户端必须跟随 `next` 读取所需页面，不能把首屏结果当成全集；无匹配项返回空 `results`，权限不足、查询无效或事实无法安全投影时显示明确错误，不能伪装为空数据。接口仅允许 staff 访问：匿名和非 staff 分别按认证与权限合同失败；非法 `subject`、UUID、`workflow_id`/`subject` 组合或其他非分页查询参数返回 400，无效页码沿用 DRF 的 404。

每条结果必须区分以下三层证据，不能按名称相似、默认实例、优先级或同一 Provider 猜关联：

1. **Profile 兼容**：工作流版本的 `model_profile_version` 与 Deployment 的 profile 精确一致，只证明两者可作为候选；不代表已经验收或执行。
2. **精确验收**：工作流版本的 `validated_deployment` 精确指向该 Deployment，证明历史 Live Validate 对这个实例执行过。
3. **精确基础验收任务**：`scope=resource`、实例版本为空的持久 `WorkflowSmokeAttempt` 同时精确指向工作流版本和 Deployment；创建任务不代表已提交，终态与证据完整性仍须单独判断。`scope=instance`的组合烟测不参与基础工作流的最新任务与通过计数。

历史结论与当前 freshness 是两个维度。历史 `validation passed` 或 `smoke passed` 保留其当时的不可变审计语义；接口在读取时用当前 Provider、Deployment、ModelProfileVersion、Artifact、工作流结构与 target hash 复验并单独返回 `fresh`、`drifted`、`not_run` 或明确不可判定状态。因配置、artifact、结构或 target 变化而得到 `drifted` 时，页面必须同时显示“历史曾通过”和“当前已漂移”，并引导重新 Live Validate/Workflow Smoke；不得把历史 passed 改写为 failed，也不得把旧 passed 当成当前可发布证据。Deployment Smoke 与 Workflow Smoke 始终分别展示，二者不能互相替代。

模型页的模型部署只展示资源核验；ComfyUI 部署按精确 `deployment_id` 分页读取工作流校验与烟测，其他候选与历史按需展开。这些结果不能代替业务执行实例的组合烟测。页面提供“查看引用此部署的执行实例”，按精确外键下钻；策略、工作流、参数及辅助依赖从执行实例版本查看，不再显示模型族推测的策略。查询不提交烟测或改写配置，实际切换已有后续记录，查询结果仍不能代替组合验收。

可直接调用的模型部署按 `deployment_smoke.operations[]` 逐能力展示，不再使用实例级最后一条 Smoke 代表所有能力。模型页与验收中心共用展示组件；`有效通过 x/y 项` 仅统计当前适用能力，最近任务排队、执行、失败和证据有效期是独立维度。读取失败或数据缺失必须显示不可判断，不能退回历史 `passed`。

| 字段 | 类型与口径 |
| --- | --- |
| `evidence_state` | 字符串：`fresh` 当前有效、`expired` 到期、`drifted` 目标配置改变、`failed` 失败且无有效通过、`missing` 无记录、`unverified` 有记录但无有效证据、`unavailable` 合同不可核验、`uncertain` 提交待确认、`not_applicable` 当前不适用。服务端从持久事实投影，不回写历史 |
| `latest_snapshot` | 对象或 null；按该能力的最新烟测快照，包含 `captured_at`、`expires_at`、历史 `status` 和当前 `target_matches`，过期历史不因另一能力新烟测而消失 |
| `fresh_passed_snapshot` | 对象或 null；该能力最近完成的基础快照经原准备/结果核验、目标一致且未过期时才返回。最新失败或损坏不会回退到更早通过；新任务仍在准备/执行时，已完成结果与进行中任务分列 |
| `latest_attempt` | 对象或 null；该能力最近任务，包含状态与开始/结束时间；任务成功不自动等于证据当前有效 |
| `prompt_profiles` | 数组；此能力已有基础验收记录的精确策略版本，每项含 `prompt_profile`、`evidence_state`、`latest_snapshot`、`fresh_passed_snapshot`。不是适用Binding目录或完整模式覆盖表；人工通道/无策略记录不伪装策略项 |

Attempt 和 Snapshot 的安全摘要返回实际选择的 `prompt_profile`（`id/name/version/contract_sha256/operation/mode/execution_mode`，不包含Binding或提示词正文），明确人工或无策略模式返回null。损坏或不符合当前准备协议的记录不能作为有效证据，也不会通过查找Binding修补。基础能力摘要取最近完成结果；不同已提交策略分别显示，不表示全部策略通过，更不能替代执行实例版本的组合验收。准备中任务占用原活动名额，页面不能重复新建；损坏的旧证据不阻止用户明确重新烟测。

证据有效期仍由 Provider/实例运行配置的 `capability_ttl_seconds` 在生成快照时冻结；前端仅在服务端 `expires_at` 到达时将有效状态降为过期，不自行配置 TTL 或推断通过。按能力历史通过分页范围内的快照窗口查询批量读取，不逐实例查询，不调用外部执行器。

Deployment当前执行目标按当前页批量读取部署、模型合同、凭据引用和模型文件摘要，与单目标执行校验共用指纹生成器；不复用旧ORM关联对象或历史目标。目标丢失、配置损坏逐项显示不可核验，不影响同页合法目标的判断，不把整页伪装为通过。逐能力的完成结果和正在准备的任务分列，某项新任务不覆盖其他能力的过期或缺失状态。

使用 `deployment_id` 查看 Workflow 时，`validation.fresh` 和 `smoke.fresh` 只允许归属于该筛选实例：若历史 `validated_deployment_id` 或 `smoke.evidence_deployment_id` 指向另一个实例，响应保留历史 hash/状态，但返回 `not_current_for_selected_deployment` 和稳定原因码，不得借用另一个实例的绿色状态。存在未消歧的 Workflow Smoke attempt 时，新的 Workflow Smoke 动作必须禁用，并返回指向既有 `workflow-smoke-attempts/{id}/resolve-uncertain/` 合同的处理动作。Deployment 反向汇总中的“仅 Profile 兼容”只计算既没有精确验收、也没有该实例精确 Smoke 历史的 Workflow 去重集合。

集中响应只在当前生效的 `workflow.max_graph_bytes`、Workflow/Profile 完整性和 execution target 本地预检均通过后把 Live Validate 动作标记为 `ready`；这些预检不调用 Provider。结构或 target 已确定损坏时返回 `workflow_validation_precheck_failed` 并禁用该动作，不能先显示“可执行”再由正式动作确定性失败。

Deployment 行分别返回模型合同状态和当前 target 状态。模型规格损坏使用 `model_profile_contract_invalid`，实例 target 无法核验使用 `deployment_target_invalid`；相关 Probe/Smoke 动作 fail closed。已有健康或 Smoke 快照仍作为历史摘要返回，但 `target_matches=false`、`fresh=false`，不得把合同损坏静默显示为“未配置”或“没有历史”。

媒体 ModelProfileVersion 的 `specification.execution_monitoring` 声明默认及节点类型静默窗口，工作流配置提供其冻结的节点覆盖。实例发布冻结可用规则，每次Preparation冻结本次实际请求与监测合同；GenerationTask通过唯一主GenerationAttempt核对该准备、实际图和暂存回执。worker不读取旧GeneratedWorkflow或PromptArtifact，也不根据当前项目默认值重建历史时限；提交、恢复和节点推进核对原领取身份，心跳不续期硬时限。优先级与deadline规则见 [长节点执行监控合同](execution-monitoring-contract.md)。

### ComfyUI 已保存工作流纳管

该主路径分为五层，权限与副作用不能混用：

1. **发现并纳管来源**：`discover` 只读扫描；staff 显式以 `import_kind=candidate` 纳管合法文件后，创建不可变 `ComfyWorkflowSource`。candidate 准入只检查受控目录、JSON 可解析、大小边界和敏感字段，不要求目标模型已发布、目标执行 Deployment 可用或 projector 已匹配。用于读取本地目录的内部 source Deployment 只是受控传输代理，不是目标执行授权。
2. **生成执行合同草稿**：preview 只接受已经纳管的精确 candidate。没有主输出选择时返回候选；明确选择主输出后，服务端读取实时 `object_info`，持久化或复用不可变 `ComfyWorkflowContractDraft`。响应以 `admission_tier`、`actionability`、`blockers`、`live_validate_requirements` 和 `recovery_actions` 区分“仅候选”与“可提升”；无法解释的部分不会删除来源候选。
3. **Live Validate**：只有 `actionability=ready_for_executable_import` 的草稿可提升为 `WorkflowTemplateVersion`。版本进入 Live Validate 时才要求匹配的 active 目标 Deployment、published ModelProfileVersion、新鲜模型文件证明、实时节点 Schema 以及冻结 projector/adapter 身份一致。
4. **真实 Smoke**：原工作流资源入口读取明确的执行选择与共用输入合同，经 Preparation 冻结本次参数、素材及人工通道，再由原 WorkflowSmokeAttempt 队列验证真实提交和产物。基础验收不代表执行实例组合或容量范围已通过。
5. **发布与生产路由**：合同、Live Validate 和真实 Smoke 证据均通过且仍 fresh 后才能 publish；Recipe 与 ProductionRun 只接受已发布版本。candidate/contract draft 不具备执行权，不能进入 Validate、Smoke 或生产路由。

Settings 携带 `model_profile_version` 上下文时，底层 `discover` 仍扫描全部受控来源；URL 上下文只过滤和解释列表，不改变来源扫描或生成关联事实。精确关联必须来自 candidate/draft/version 的 UUID 与冻结 provenance，不能按文件名、同模型族、默认 Deployment 或相似名称推断。切换或清除上下文后，旧请求结果不得回填当前列表。

精确匹配的已导入来源仍返回原冻结模型/主输出身份；`model_profile_candidates` 另用同一准入计算列出当前其他模型候选，分别保留草稿未发布、文件证明或执行目标不足等原因，不继承旧工作流验收。普通匹配导入锁定原模型；只有页面显式请求后继时允许选择修正后的模型并重新生成合同草稿，旧版本及既有实例不自动改绑。

| Method 与 Path | 请求与用途 | 成功响应与副作用 |
| --- | --- | --- |
| `GET /api/v1/comfy-workflow-sources/discover/` | 按系统配置 `workflow.comfyui_saved_workflow_directory` 扫描全部 active ComfyUI userdata 来源作用域，并在服务端发现/匹配 | 200；返回每个来源的成功或失败、source SHA、分类、匹配状态、候选、精确恢复动作、既有 provenance、`contract_upgrade_required`，以及包含有效目录和扫描/批处理预算的 `effective_limits`；部分来源失败不会伪装为空目录，不写数据、不提交 GPU |
| `POST /api/v1/model-deployments/{id}/saved-workflows/batch-import/`，`import_kind=candidate` | 对 discover 结果显式纳管来源；只提交 source path/SHA、来源作用域版本/hash 和 candidate 类型 | 200；逐项创建或复用不可变 candidate provenance。不会创建 WorkflowTemplateVersion，不要求目标模型、projector 或目标执行 Deployment，不提交 GPU |
| `POST /api/v1/model-deployments/{id}/saved-workflows/preview/` | 基于精确 candidate 提交 source、SHA、来源作用域版本/hash、可选唯一 canvas primary 与可选目标 profile | 200；未选主输出只返回选择信息；选定主输出后创建/复用不可变合同草稿，并返回 draft ID/revision、投影 SHA、projector 身份、参数/素材/模型依赖、blockers、deferred Live Validate 要求与 actionability。该调用不是纯只读 |
| `GET /api/v1/model-deployments/{id}/saved-workflows/preview-receipt/?request_key=...` | 原操作人（staff）按精确部署及请求键核对原回执 | 200返回原完整预览响应及`request_receipt`；400键或完整性错误、403权限不足、404本账号／部署下未查到。`Cache-Control: private, no-store`，不读取主机、不生成合同、不提升或执行；404不是未保存证明 |
| `GET /api/v1/comfy-workflow-contract-drafts/`、`GET /api/v1/comfy-workflow-contract-drafts/{id}/` | staff 审计精确 candidate 的合同草稿 | 200；只读返回 `projection_summary`（节点/依赖数量、结构摘要、blockers、Live Validate 前置项和恢复动作）、projection schema/size/SHA、主输出、模型、projector 版本、admission tier 和 actionability；不返回可执行图正文，也不提供 PATCH/DELETE |
| `POST /api/v1/model-deployments/{id}/saved-workflows/attest-model-artifacts/` | 使用 discover 返回的来源作用域与当前转换审阅事实，为同 Provider draft profile 建立模型证明 | 202 + `Location`；创建或复用 RuntimeHostProbeAttempt，不提交 GPU prompt |
| `POST /api/v1/model-deployments/{id}/saved-workflows/batch-import/`，`import_kind=template_version` | 提交精确 contract draft、模型版本、主输出、四类草稿审阅 hash 及新/既有模板目标；可选 `successor_of` 为前驱版本 UUID，仅允许既有原模板 | 200；逐项创建/复用 needs_review WorkflowTemplateVersion 和 provenance。普通请求结构去重；显式后继在原模板锁内检查同路径/来源作用域的最高版本（含草稿和退役），创建不继承验收的新版本；同一完整请求优先回读原后继，过期前驱或来源不符的新请求返回单项 `workflow_successor_conflict` 失败。不存在前驱或非 template_version/新模板组合返回400。不重新读取本地源文件或 `object_info`，不接收具体参数/Asset |
| `GET/POST /api/v1/workflow-projectors/`、`GET/PATCH/PUT /api/v1/workflow-projectors/{id}/` | superuser 管理稳定 projector family | 200/201；key 创建后不可修改；停用 family 只影响新投影，不改写历史草稿或版本 |
| `GET/POST /api/v1/workflow-projector-versions/`、`GET/PATCH/PUT /api/v1/workflow-projector-versions/{id}/` | superuser 管理 projector 草稿版本 | 200/201；仅 draft 可修改，服务端校验 definition 并计算 schema/size/SHA；published/retired 不可覆盖 |
| `GET /api/v1/workflow-projector-versions/options/` | superuser 读取 Projector 结构化编辑合同 | 200；返回 definition/editor schema 版本、表单分区与字段帮助、枚举、数量上限，以及每个 Engine 的说明、唯一 adapter contract、projection kind、锁定字段、专项字段和 `creation_template`。后者包含 driver/operation/output_kind 与不含业务规则的 definition 空白骨架，独立于已有版本；不是通过校验的合同。无副作用；权限不足返回 403。客户端遇到未知 Schema/Engine、缺失骨架或身份不一致时失败关闭，不能登记或自由拼接任意 engine/adapter key |
| `POST /api/v1/workflow-projector-versions/recommendation/` | superuser 基于任一已纳管 candidate、来源作用域/SHA、唯一 canvas 主输出和人工确认的 output kind 请求 Projector 建议；候选即使已由 fallback 匹配或已提升为工作流版本也可使用 | 200；重新校验不可变 candidate，读取实时 `object_info`，返回 `persistence=unsaved_draft` 的 `generic.fixed/v1` 或经结构证据确认的 `generic.references/v1` definition、节点/输入安全目录、证据 SHA 与人工复核警告；不创建 family/version，不发布、不提交 GPU，也不返回 source path、提示词、素材名或模型文件字面值 |
| `POST /api/v1/workflow-projector-versions/{id}/draft-next/`、`publish/`、`retire/` | superuser 创建后继草稿并管理不可变生命周期 | 200/201；发布后成为该 family 的 current published 版本；后继版本不会改写历史 draft/WorkflowTemplateVersion |
| `POST /api/v1/workflow-projector-versions/{id}/discard/` | superuser 放弃误建或不再采用的 Projector 草稿 | 204；只允许删除该 family 的最高、未发布、非 current 且未被合同草稿引用的版本，并记录 `discard` 生命周期审计；published/current/retired 和历史引用保持不可变。非最高或已有引用返回 409 |
| `GET /api/v1/assets/workflow-smoke-candidates/` | 工作流版本验收页读取每次 fixture 可选的受管 Asset，包括普通生产列表刻意隐藏的 `fixture_scope=workflow_smoke` 技术素材 | 200 分页；返回资格和非敏感原因，不改变版本 |
| `POST /api/v1/workflow-template-versions/{id}/validate/` | body 为 `deployment_id`，校验实时结构、模型和主输出 | 200 时更新 validation evidence，并使旧 smoke evidence 失效；模型主机证明不可用时返回 400 稳定原因码与 `details.recovery={state,title,summary,steps,actions}`，页面据当前探测状态引导等待、主机探测、模型文件核对或重新校验；不要求具体 Asset |
| `POST /api/v1/workflow-template-versions/{id}/smoke-test/` | 仅接受当前Live Validate通过的validated版本；Header为Idempotency-Key，body为selection、expected_definition_sha256及共同fixture/字段/授权/预算，见基础资源烟测小节 | 首次202、同键同请求200，返回原Attempt和Preparation；输入错误400、摘要或活动任务冲突409，不创建重复任务 |
| `POST /api/v1/workflow-smoke-attempts/{id}/resolve-uncertain/` | 仅 staff；body 为三种终态 `outcome`、核验说明和可选只写 `provider_request_id` | 201 首次追加不可变结论；200 完全相同重放；冲突为 409 |
| `POST /api/v1/workflow-template-versions/{id}/publish/` | 复验 validation/smoke/fixture/compiled graph/target 证据 | 200；发布不可变当前版本 |

discover 只读各工作流库，不返回 canvas 正文、不写候选/版本、不提交 prompt；实际项数、字节与同步时间边界由当前安全配置控制，页面只使用响应中的 `effective_limits`。candidate 纳管与 `execution_snapshot`/`ignored` 使用同一 batch 外壳但语义互斥；candidate 保存精确原始文档和来源 hash，永远没有 `workflow_version`。preview 必须带 discover 的 `expected_sha256`、`expected_source_scope_schema_version` 与 `expected_source_scope_sha256`，并且服务端必须找到完全一致的 candidate，否则返回 `workflow_candidate_required`。来源作用域漂移返回 `source_scope_changed`；candidate 自身不被覆盖，重新扫描后纳管新的 revision。

合同preview的`request_key`为1–191位`[A-Za-z0-9:._-]`字符串；前端提交前生成并保留稳定键，缺省时服务端分配键，但客户端若未收到键便不能按原请求恢复。未选择主输出不写回执。选定主输出时合同与安全响应回执原子提交；同操作人／键、同完整请求身份重放返回原响应，身份变化返回409 `workflow_preview_receipt_conflict`。`request_receipt`包含`request_key`、`candidate_source_id`及`request`（deployment_id、source_path、expected_sha256、expected_source_scope_schema_version、expected_source_scope_sha256、primary_output_node_ids字符串数组、model_profile_version或null）；响应保留原`suggested_input_contract`、四类SHA及draft ID/revision，不返回原始图。并发失败后若已存在匹配回执则返回原结果，不重新执行写入。GET严格限定原用户，不能借相似内容或其他账号的草稿认领成功。旧请求不补造回执；空／错误／身份不符时保留未知，前端禁止重复生成，只在当前候选及选择仍一致时恢复表单，之后由用户显式提升。

最小回读示例：登录后的原作者发送`GET /api/v1/model-deployments/{原部署UUID}/saved-workflows/preview-receipt/?request_key=example:1`，无Body、无分页。200的`request_receipt.request_key`为`example:1`，`request_receipt.request.model_profile_version`明确为原UUID或null；其余字段沿原preview响应结构，不用当前目录补齐。服务端入口为`ModelDeploymentViewSet.saved_workflow_preview_receipt`与`comfy_workflow_preview_receipts.py`，只读`ComfyWorkflowPreviewReceipt.response`并校验schema/size/SHA；POST按原用户、请求摘要和合同FK记录事实，不重复产生执行或模型调用审计。JSON容量沿现有工作流图字节安全上限；查询命中操作人／键唯一索引，不扫描全部合同。迁移0236及读写路径已做隔离数据库／页面验收；现用环境迁移、服务加载及正式只读验证范围见[当前检查点](optimization/current.md)。无现场回执样本时不宣称200恢复正例已在现用环境验证。

明确选择一个服务端候选主输出后，preview 重读 `/object_info`，完成 UI canvas → API graph、子图展开、可达剪枝和结构审计，并持久化草稿。`configurable_parameters` 只用于审阅 parameter semantic、type/range/options、selection group 和精确 API node/input target；具体值不进入草稿。普通固定素材槽 required 为 `1..1`、optional 为 `0..1`；动态 topology 只来自冻结的 published projector。没有唯一 projector、模型证明或目标 Deployment 时，草稿可保持 `candidate_only` 并记录 blocker/Live Validate 要求；只有 `ready_for_executable_import`、无结构 blocker、精确 projector/model/primary 与四类 SHA 全部一致时才允许提升。

preview 发现目标 draft model profile 缺少模型声明时，可调用 `attest-model-artifacts`。该 action 只接受当前 source、两个转换审阅 hash、唯一 canvas primary 和同 Provider draft profile；模型 filename、size 和 SHA 不接受客户端提交。来源必须是本机 loopback ComfyUI 且 RuntimeHost 的 Comfy scope 都是本地。服务端重算完整模型闭包，整批成功后才原子写入 artifact size/SHA/verified snapshot；失败只保留诊断 snapshot。调用方按 `Location` 轮询，不能把部分证明当作可用模型。

正式提升的 batch 不重新扫描目录、重读本地 JSON 或再次请求 `object_info`。服务端锁定并校验 contract draft 的 projection schema/size/SHA、candidate 身份、主输出、模型版本、projector version/definition SHA/engine identity 和四类审阅 hash，再建立版本；草稿不匹配返回 `workflow_contract_draft_mismatch`，仍有阻断项返回 `workflow_contract_blocked`，且零写入。具体参数值和 Asset ID 不属于提升请求。响应可以是 `complete/partial/failed`，HTTP 200 仍须检查 `failed_count` 和逐项 `code`。

`WorkflowProjectorVersion` 是独立版本化领域对象，不是普通 ConfigRevision JSON。definition 描述 driver/operation/output、节点角色与关键连接、可选节点、参数策略、模型 selector 角色、素材槽/动态引用及 adapter；代码只注册受审 engine、adapter 与安全原语。新投影只选择 active family 的 current published 版本，并把 version ID/number、definition SHA 和 engine identity 冻结进草稿与工作流输入合同。发布 projector 后继版本不会重解释历史候选、草稿或 WorkflowTemplateVersion；要采用新规则，可基于同一不可变 candidate 生成新草稿并提升新版本，只有来源字节变化时才纳管新的 candidate revision。未发布的最高版本如果尚无合同草稿引用，可通过受控 `discard` 动作放弃；这不是删除历史版本。

通用动态素材的 options 增加 `definition_editor.choices.asset_group_wirings`（`ordered_ports/reduce`）；新引擎的 `projection_fields` 使用 `asset_group_list` 控件，固定 compiler/output/dynamic 为 `generic.comfy/v2`、`comfy.primary/v1`、`generic.asset_groups/v1`。`projection.config.groups` 包含 key、source_role、branch_roles、output_role/output_index、target_roles、input_names、wiring、reducer_role/reducer_inputs/reducer_output_index、minimum_items/maximum_items、conditioning_mode/model_input；字段必须完整，未知字段/算法、越界数量或不兼容接线均拒绝。数量为模型来源时本地 min/max 必须为 null。

数量映射选项 `definition_editor.choices.model_quantity_inputs` 返回 `{value,label,media_kind}`，与模型素材合同作者目录 `input_slots` 共用同一注册输入目录；元数据不证明具体模型能力。通用动态组选择模型来源后，展开“选择模型数量声明 / 查看来源”，读取精确模型版本，再从其实际声明的模式与同媒体输入中选择，点击“应用所选数量声明到本组”。只应用 `conditioning_mode/model_input`，不保存对照模型ID、不复制数量；生成工作流时仍校验实际绑定版本。目录缺失／重复、模型无模式、输入未注册、类型不匹配、无效范围或0–0均不提供可用选择；读取失败与延迟响应不会使用旧结果。切换模型不自动修改现有字段，失效字段仍显示原值并提示不匹配。冻结工作流上下文及只读版本不提供应用动作。动态组、槽声明及数量映射仅由执行引擎表单修改，高级JSON可核对但不能覆盖这部分；其他字段仍沿同一Definition编辑与服务端完整校验。读取和未保存的应用不新增配置、任务、审计或模型调用，不构成Smoke通过。

通用固定与动态引擎通过 `projection.config.capability` 显式选择执行能力，正式入口为 Projector 草稿的“执行引擎与接线”。选项来自 `definition_editor.choices.execution_capabilities`，复用模型能力目录中支持 ComfyUI 的条目，不依据模型名称或主输出推断文生图/图生图等能力。可保留未完成的方案供配置，但生成可提升合同必须声明能力，并与主输出模态及所选模型能力同时匹配；缺失返回 `workflow_projector_capability_required`，不匹配返回 `workflow_projector_capability_mismatch` blocker，不能提升或提交。明确能力冻结到新工作流 `input_contract.capability`，读取时校验与该 Projector 版本一致；不补写旧合同。分类参考引擎继续使用其原有 capability/conditioning_mode 校验。

参数范围的正式管理入口是 **系统设置 → 工作流 → Projector 草稿 → 参数与素材 → 参数策略 → 运行参数范围与默认值**。`parameter_policies[]` 保持原有五个必需字段，可增加 `constraints` 对象，允许 `min`、`max`、`default`、`multiple_of`、`alignment_modulus`、`alignment_remainder`。仅 `runtime/object_info` 策略可声明非空范围，固定值和来源锁定不因此解锁。省略或空对象继承节点／引擎合同；填写后只允许收窄，不能放宽实时节点边界或既有对齐规则。小数步长以十进制从 0 起算，例如 LoRA `min=0,max=1,multiple_of=0.05,default=0.3`；整数帧数可声明 `alignment_modulus=17,alignment_remainder=5`。生成合同会验证类型、交集和默认值，默认值不合法时返回 `workflow_projector_parameter_range_invalid` blocker，不截断或替换成成功值。最终范围写入 `input_contract.parameters`，素材生成、Smoke 和生产编译复用同一数值校验；分类参考引擎的 `frame_policy` 在生成时从同一帧数定义派生。已发布版本不原地改写，采用新范围须发布 Projector 后重新生成、验收工作流。

分类参考使用 `generic.classified/v1`（compiler `generic.comfy/v3`）。Projector 草稿 API 的 `definition.projection.config` 配置 capability、conditioning_mode、target_role、prompt_template_keys、static_semantics 与 dynamic_groups；四类组为 images/videos/video_audios/audios。每组选择 mode_input，声明按序 input_names 或 prefix（二选一），数量由对应 asset_slots.contract_source 确定。model_profile 来源不填写本地 min/max；workflow 来源显式配置 minimum_items/maximum_items，不能超出模型合同和实时节点容量。页面可读取模型版本的能力与模式辅助填写，生成合同时再冻结精确版本。端口必须唯一且不能覆盖静态输入，配套音轨按所属视频序号接入；Live Validate 探测最小、最大及稀疏音轨情况。不满足约束会产生 blocker，不能提升或提交 GPU。

三个 Generic 引擎共用参数治理：fixed 生成 default+const，运行解析对所有类型拒绝不同值；必选单值参数通过 fixture Schema 的 options 表示，前端只读展示。source_locked 表示来源默认优先，不是禁止显式覆盖。object_info.step 不推导从 0 起算的整数格点；应使用显式 multiple_of 或模数/余数配置。已发布旧合同不会自动补写或改变。

数量来源选项 `contract_source=workflow` 的页面名称为 **Projector 中配置**：通用动态读取 `projection.config.groups[].minimum_items/maximum_items`，分类参考读取 `projection.config.dynamic_groups`；随后校验真实接线端口和节点能力，不自动推测画布可选素材数。`contract_source=model_profile` 名称为 **模型版本合同**，按 `conditioning_mode/model_input`（分类参考为 `mode_input`）读取所选模型版本的 `specification.conditioning_contract.modes[].inputs`。通用动态组在保存及编译前校验注册输入的媒体类型，不得借用音频数量合同配置图片槽。通用动态的 Projector 数量声明不是模型能力认证；分类参考则必须具有精确模型合同，无论选择哪种数量来源，均不能超出对应输入范围。

通用动态组选择模型数量来源时，服务端生成只读的 `input_contract.asset_groups[].model_quantity`：`conditioning_mode/model_input/total_images`（非图片为null）。组的 `min_items/max_items` 仍精确来自模型对应输入，不另建可编辑范围。合同生成、导入及发布／执行前完整性检查核对来源与模型版本；缺少来源证据的旧模型数量合同须重新生成新工作流版本并验收，不回填历史。Projector数量来源不得携带此模型证明。

同一目标节点接收多个组时，模型数量来源须属于同一模式、各输入不重复；重复引用同一输入不会得到多份容量，`total_images`也不能与其图片分项混用。图片组独立范围之和须整体落在模型`total_images`上下界内；当前通用接线不表达跨组共享额度，因此“部分组合合法”不能放行整个更宽范围，不自动截断。模型与Projector来源混用但汇入同一目标时无法核对归属，明确阻断；纯Projector来源仍仅是接线数量声明。检查按声明的目标节点进行：同一组分发到多个节点，每个目标只计一次；独立接收节点不盲目相加，也不猜测不同节点的下游是否最终共享模型。此为已声明接收处的数量验证，不是完整模型容量认证。错误指出目标、组、模式／输入及冲突范围，通过既有合同生成错误返回，不写入错误工作流版本；最终实例输入／Smoke表单／编译器继续消费同一冻结组范围，不增加第二份用户数量配置。

分类参考的图片范围还必须落在 `total_images` 的最少和最多数量内；不允许冻结时丢失模型的总数下限或将其替换为更宽松值。有冲突须显式修正 Projector 或新建模型版本，生成阶段返回 `workflow_projector_contract_mismatch` blocker，不能提升或提交。收窄后的用途、配对、互斥及预处理仍按同一合同校验；若把媒体上限降为零却与原模型用途等规则矛盾，将指出模式及字段路径，不自动删除这些规则。模型原文及历史冻结请求不修改。Projector 页面显示对照版本、模型输入范围及图片总量，并即时提示数量越界；此只读比较不绑定版本、不代表节点校验或真实 Smoke，历史工作流仍展示原冻结范围。

模型数量合同的正式管理入口是 **系统设置 → 模型 → 草稿版本（或新建下一版本）→ 模型素材数量合同**。`GET /model-profile-versions/options/` 返回只读 `conditioning_contract` 编辑目录：`schema_version/adapter_key/input_kinds/input_slots/maximum_items/maximum_modes/identifier_pattern/mode_template/dynamic_reference_topology`；权限沿用模型注册表 options，缺失目录时表单失败关闭。表单自动补齐系统字段和零数量槽位，也可通过已有 `GET /model-profile-versions/{id}/` 读取并明确复制完整素材合同；复制不修改其他规格，不自动推测模型能力。模式形态、首尾帧／参考图片／视频／音频的最少和最多数量通过结构化字段编辑，`0..0` 不支持、`N..N` 固定、`min<max` 动态；图片总数仍独立校验，不能超过各图片槽上限之和。展开“视频／音频用途、预处理与配对规则”维护同一份 `dynamic_reference_topology`，完整 JSON 只读复核素材合同。保存沿用模型版本 POST/PATCH 的 `specification`，发布仍走既有完整合同校验；非法基数、未声明拓扑或发布态修改按原接口返回 400，错误不得伪装为已保存。

用途表单提供名称、说明、数量、默认用途、预处理毫秒时长与帧率、配对来源开关及互斥规则。组数量来自上方槽位；默认／配对／互斥仅选择已声明用途，改名或移除不自动级联，失效引用保留并阻止表单保存。只读目录 `dynamic_reference_topology` 提供 `schema_version/maximum_rules/maximum_duration_ms/maximum_fps/label_max_characters/text_max_characters/maximum_targets_per_source/media_groups/preprocessing_slots/pairing_direction`。预处理候选从实际执行注册表 `REFERENCE_PREPROCESSING_ADAPTERS` 投影；未知处理器显式阻断，不把新标识当新执行能力。初次配置只建立协议结构，不猜用途、容量或预处理器，不外呼。Projector仍按精确版本／模式／槽位引用，已发布工作流不会被本表单改写；实际执行须共同满足模型、接线与节点约束。API对错误用途列表类型（如对象／数组）返回400，失败不留成功审计；隔离创建→PATCH→发布→后继验证不代表真实媒体Smoke。

模型“尺寸、帧率与时长”沿同一 `GET /api/v1/model-profile-versions/options/` 返回只读 `media_constraints` 目录：结构版本、`resolution_fields`（key/label/unit/minimum/maximum）、上下界配对 `resolution_pairs`、帧率的合法范围与最多项数、时长合法毫秒范围。字段集合与当前安全边界从 `normalize_model_media_constraints` 的同源元数据投影；获取不写入、不调用模型。权限沿原管理员模型注册入口，目录缺失／损坏不能以手写Key兜底。原模型 POST/PATCH 的 `specification.media_constraints` 仍是唯一保存位置；发布版本不可变，普通表单只改草稿。空字段表示未声明，不等于无限；时长须上下界成对，帧率不得重复，未知键／非法范围返回400且不局部保存。完整JSON仍显示全部规格，但页面拒绝通过它改写已表单化的媒体约束；尚未表单化的其他扩展维持现有管理入口。实际执行继续共同校验模型、工作流与请求约束，不覆盖Projector接线，不改变系统资源提醒。

“设置固定宽高”不新增API或合同字段。宽高成对目录完整时，页面让用户明确输入两个正整数，一次应用为`minimum_width=maximum_width`、`minimum_height=maximum_height`，保留倍数与短长边上限等其他字段；不把任一现有范围端点当作默认固定值。应用只更新本地草稿，保存/发布沿原模型入口；它不声明实际执行已通过共同约束。取消或校验失败不写入，暂存时模型保存禁用，外部值变化取消暂存。验证覆盖原字段序列化及固定宽高、倍数、短长边的实际执行约束，无单独固定模式解释器。

分辨率声明须存在至少一组同时满足范围、倍数及短／长边上限的正整数宽高。范围端点本身不必是倍数：宽度353–400、倍数32合法（存在384）；固定352、倍数64不合法。前端保留用户原值并提示冲突、禁用保存；固定宽高快捷应用失败不更新原四项。服务端在创建、PATCH、draft-next与发布时沿同一媒体合同校验，不依赖页面；错误返回400，不留下新版本、部分规格或成功发布审计。校验只判断声明可满足性，不取整、不填默认生成尺寸、不改写已发布合同或历史请求；不表示真实模型Smoke或与工作流联合验收通过，也不改变系统预算提醒／人工确认机制。

同一目录的只读 `chat_request_options` 提供 `schema_version`、与传输校验共用的 `temperature_minimum`、常见 `response_format_types` 及装配／边界说明。模型页创建／编辑／下一版本的标准化、影视规则和通用文本执行协议共用固定请求选项表单：采样温度输入数值，响应格式选择；不发送、显式 null 与 0 分别保存。初次选“数值”明确从目录最小值开始；不会在加载时补入默认参数。页面展示完整规格路径，选项随模型版本冻结。模型、消息、输出预算、seed、传输配置仍由各自权威入口维护。

此目录是作者工具而非供应商能力认证：不猜测 temperature 最大值或承诺某模型支持所列格式。`response_format.type=json_schema` 的结构只从执行实例所选确切业务输出派生；模型作者不得保存 `response_format.schema` 或 `response_format.json_schema.schema`，即使内容与输出相同也拒绝第二定义。`json_schema` 对象可省略，提供时只允许name（1–64位ASCII字母/数字/下划线/短横线）、strict（布尔）、description（字符串）。未声明name时生成 `output_<输出UUID去短横线>`；不自动补strict。原生剧本、影视规则和text.structured共用派生器；无业务输出的协议不接受该格式。格式选项错误或缺输出在原400结构化错误中明确返回，模型保存失败不写入记录，实例发布/准备不得猜选输出或回退格式。

派生发生于准备编译，结构来自已冻结的BusinessOutputVersion/DataStructureVersion，与提示和解析同源；最终Chat请求仍使用供应商的response_format结构，不新增可编辑Schema。业务字段名称不会被误当传输参数，但接口选项的保留键、凭据及大小检查保持。未知格式与其他接口扩展继续受控维护；切换格式不静默删除不适用的json_schema配置，用户需明确移除。历史模型、已发布定义与旧请求不回写，也不增加兼容配置副本。

执行实例混合输入同时连接物理目标与结构化策略时，原作者writer自动填入该策略的 `data_structure_version_id`。省略/null表示使用确切策略结构；不同的显式ID拒绝，不能覆盖策略。未连此类策略的独立输入仍可选择或清除自己的结构。只读 `input_forms.*.schemas` 去除完整JSON相同的重复项，不删不同目标约束；存储的来源关系、结构版本身份与输入校验不变。

同一模型目录的只读 `async_media` 提供受限REST作者表单：**系统设置 → 模型 → 选择异步REST服务连接 → 新建／编辑草稿／后继版本 → 异步媒体请求合同与Smoke样例**。返回 `schema_version=1`、`maximum_string_length`、`operations[{key,output_kind}]`、`fields[{key,label,types,source_description}]` 和 `description`，来自执行协议与当前安全边界；服务连接／部署的映射选项共用字段目录。仍保存原 `specification.async_media`，不是新存储或接口。

只支持文生图、文生视频、文生音频的封闭标量Schema；产物类型固定，operations与所选媒体能力精确一致。model／正负提示词为字符串，宽高／seed为整数，fps／duration_seconds为整数或数值，audio是布尔开关而非素材。固定控件维护必填性、数值范围、字符长度和类型化枚举；prompt必填且必须明确字符上限。创建、PATCH、draft-next、发布与实际请求共用标量校验；非法类型、非有限数、倒置范围、空／重复／越界枚举在写入前返回400，不留部分版本或成功审计。

Smoke样例同字段行填写，仅为测试材料，不能成为生产默认值或模型选择器；0、false、空文本与省略区分。页面解释“策略通道／任务参数→标准Key→服务连接基线request_field_map”；远程映射在服务连接／部署维护，部署覆盖、媒体约束交集与缺映射仍由执行实例检查。创建合同仅显式建立协议结构，容量和样例留空；未知配置保留显示，重建／移除须确认。完整JSON禁止重复编辑async_media；此表单不注册任意协议或响应解析器，保存／隔离验证不等于真实Smoke，不产生供应商调用。

同一模型目录的只读 `script_contracts` 提供 **系统设置 → 模型 → 新建模型／编辑草稿／新建下一版本 → 剧本模型传输设置**。目录 `schema_version=3`，仅返回 `policies[{key,operation,label,number_fields}]` 和说明；不再返回固定剧本字段字典、声明用途目录或 `field_contract_schema_version`。两个政策仍分别保存在 `specification.series_concept`（`script.generate`）和 `specification.script_structure`（`script.structure`），不增加存储或业务调用入口。

| 政策字段 | 表单、校验与读取方 |
| --- | --- |
| `timeout_seconds` | 必填有限正数秒，允许小数；最大值来自 `security.provider.async_rest_timeout_max_seconds`，不改变任务总体时限 |
| `chat_request_options` | 必填对象，可显式为空；复用共享表单/校验器，保留0、null、省略及接口扩展 |

除此之外的旧 `field_contract/reference_policy`、镜头时长/原稿容量字段及未知项全部拒绝，不自动转换或忽略。字段类型、含义、嵌套、必填、默认和使用规则在共享数据结构与确切业务输出版本维护；创作要求在提示词策略维护。页面保留错误值供修正，旧项只在草稿显式确认后移除；完整规格JSON不能成为同一设置的第二编辑入口。“初始化传输设置”只显式建立空请求选项，不猜等待时长或创作字段。目录缺失/非法数值、只读已发布版本均不能写入。

创建、PATCH、draft-next、发布和执行共用传输校验，失败返回400且不留下部分版本或成功生命周期记录。现有版本不被迁移/改写，旧规格需要按新职责明确建立后继配置，不能继续执行旧请求协议。目录读取沿原管理员权限，不读取项目内容或调用模型。

原生剧本生成/结构化现在都必须明确选择已发布 `script_semantics.v1` 业务输出版本。原实例及资源合同从同一能力/输出目录读取，冻结输出ID、版本、SHA、共享结构和语义映射；缺少输出、错误协议或草稿输出拒绝保存/准备，不补默认输出。实例发布后的输出快照不随输出后继版本变化。底层原生适配器身份仍为 `text.script_generate/v2` / `text.script_structure/v3`，其中内部适配器数字不是业务JSON根字段的schema_version要求。

正式请求沿原准备/编译/队列，system包含确切结构的RESPONSE_SCHEMA及同源CONSUMER_SEMANTICS，user为明确的operation、已解析inputs和策略通道；不再发送旧固定字段合同、REFERENCE_SCHEMA或要求模型回显来源摘要。原稿字节/摘要/修订仍在冻结计划保存核验。响应按同一输出解析结构、映射和关系，保留实际JSON原文与自定义字段，不重写为固定Markdown正文；无效响应保留原失败证据，不回退旧解析器。任务中心逐个比较本次冻结输入和实际user字段，不用旧固定路径补造核对结果。

**当前开发边界**：上述模型作者、正式目录、实例/准备/编译、隔离worker及素材原文链已切换；不等于现用服务重载、配置写入、真实模型通过或M2模块交付。候选编辑、下游业务消费及剩余旧实现清理继续按M2计划推进，0213仍未迁现用库；未改写历史请求/产物。主图→变体真实生成仍归后续模块。

同一目录的 `execution_monitoring` 返回 `max_idle_timeout_seconds`、`max_node_type_overrides`；它们直接使用执行监控校验器的当前安全边界。模型编辑不再读取生产能力聚合接口：无关实例准入错误不得阻断模型草稿编辑。目录本身读取失败／字段缺失仍明确禁止保存，不能退回客户端默认上限；生产审核等其他入口继续使用其自身合同。

`GET /api/v1/business-output-versions/parsers/` 返回`parsers=[{key,label,description,configuration}]`协议目录，供 **管理执行实例 → 管理业务输出版本** 使用。`structured_json.v1`从确切结构定义任意JSON字段/层级，配置、请求冻结、通用文本解析及原worker候选保存/读取已接通；配方目录/顺序阶段/选择与配置已接，原结果字段页已接，动态下游来源及选择表单已接入；`visual_rules.v2`执行已有三项基础输出及可选镜头指导的业务转换。`parser`与同源`editor`等其余字段专供影视规则配置，不用它们解释通用结构，不开放自定义解析脚本。`editor.number_fields`给出8项质量建议的key/label/unit/minimum/maximum，`number_pairs`给出数量上下界配对；范围来自实际校验器。`color_choices`、`outputs`、`response_example`与影视规则执行使用同一协议。原模型选项目录的只读展示复用同一元数据，并非另一写入入口。

`BusinessOutputVersion` 是独立输出合同，不保存生成结果。管理员通过 `/api/v1/business-output-versions/` 分页查询或 POST 草稿（name、description、parser_key、必选data_structure_version、quality_advisories，以及按协议选择的allowed_values、field_sources、response_keys或semantic_mapping）；详情 GET、草稿 PATCH，以及 POST `publish/`、`successor/`、`retire/`。更新及动作须携带整数 `expected_revision`，冲突或非法配置400，非管理员403；禁止 DELETE。发布后内容不可变，后继只复制配置、不复制执行或审核证据。发布摘要及生命周期另存审计。列表不包含质量正文及解析合同，支持 status/output_key/parser_key/search 筛选。

**通用JSON协议（M1-08配置/预览段）**：创建时选`parser_key=structured_json.v1`和已发布`data_structure_version`，可提供名称/说明；`quality_advisories`只能为`{}`或省略，影视专用`allowed_values/field_sources/response_keys`必须省略（包括空对象也不接受）。协议合同仅`{key,format:"json"}`，字段含义/层级/类型/缺键默认全部来自共用结构，没有第二份字典或内联Schema。已保存身份不能更换协议，新建另一输出定义；后继沿用身份和协议，可明确改选结构。允许结构声明的对象、列表及标量/可空根，不强制影视对象格式；冻结后不查当前字段定义替换。

**剧本语义配置（M2-01）**：同一输出管理入口增加`script_semantics.v1`，`parsers`中的`configuration=script_semantics`。只在原输出版本存储`parser_contract={key,format:"json",mapping}`，来源仍是原`data_structure_version`；不建第二字典或配置表。`parsers.script_semantics={key,json_schema,mapping}`提供有限业务输入目录与共用映射方式，目录描述实体key/主图/变体/分集/镜头等含义，不规定模型返回属性名称或嵌套；自定义创作字段仍在来源结构维护。

| 项目 | 剧本语义配置与预览 |
| --- | --- |
| 配置请求 | POST/PATCH原输出版本；`semantic_mapping={schema_version:1,node:...}`仅此协议可用，新建必需、更新省略保留，不接受null或任意脚本；quality_advisories为空，影视专用字段须省略 |
| 映射规则 | 共用path/lookup/object/array/literal/each及root/item作用域；在确切来源结构与业务目录之间校验。保存修改同步校验映射/结构；错误整笔回滚。结构内容、映射和版本身份共同进入原合同摘要 |
| 预览入口/权限 | `POST /api/v1/business-output-versions/{id}/preview-script-semantics/`；管理员，其他用户403；不存在404；页面为管理执行实例→管理业务输出版本→剧本结构与语义映射 |
| 预览请求 | 恰为`expected_revision`整数、`expected_contract_sha256`64位摘要、`response_text`原始字符串；无缺省，无数值转字符串。示例JSON字段须符合用户已保存结构；不会补内置人物或剧本 |
| 200报告 | `preview_only=true,validation_scope=structure_and_mapping`；输出ID/修订/SHA、source_structure_version_id、configuration_sha256、source_fields、parsed/valid、raw_value/source_value/value、defaults/provenance/warnings/errors |
| 数据含义 | raw_value保留原解析值；source_value为含显式默认与自定义创作字段的完整来源；value是业务语义投影，非生成产物。provenance按实际列表位置记录来源/目标路径及结构版本/映射摘要；未知键保留并提醒 |
| 错误 | 非法JSON、重复键、类型/路径错误返回200且valid=false；保留可获得的原值与具体错误。修订/SHA漂移、错误协议、非法请求/配置返回400，不换最新版、不猜译名或回退旧标记。Cache-Control为private, no-store |
| 生命周期/副作用 | 保存/发布/后继/退役复用原事务及配置生命周期记录；只读预览无数据库写入、审核或模型调用，不产生新候选。发布版本不可变、后继改配置不替换旧合同 |
| 验证/边界 | 后端两种布局保存→重开→预览及冻结/回滚隔离测试，前端编辑/迟到响应与隔离桌面/390px验证；剧本实例/Profile请求、导入、完整关系校验及候选保存已接原正式链，开发验收证据见详细设计M2。此预览仍仅检查结构/映射，不执行模型或采用；text.structured只接受structured_json.v1。0213现用迁移、真实模型和完整生产链不属于上述隔离验证 |

代码入口为`business_output_views.py`、`business_outputs.py`和`script_semantics.py`，解析/默认/路径/映射复用M1服务。页面使用原可视化映射编辑器，修改样例、映射、结构或版本会清除过期报告，保存失败保留填写内容；已发布只读版本仍可预览。当前仅配置与结构投影，不宣称实体引用关系已校验、素材已就绪或结果已审核。

**页面业务逻辑说明**：管理执行实例 → 管理业务输出版本 → 查看／配置剧本输出，已保存定义与草稿映射共用“这份输出如何变成剧本与素材需求”。页面说明策略创作要求、响应结构、语义映射、平台解析／引用检查与采用后的下游用途，区分实体名称（Canonical）、稳定key、主图／变体引用和实际素材。字段中文含义来自现有`parsers.script_semantics.json_schema`；配置来源从该输出的确切`parser_contract.mapping`或当前草稿展示，不按同名字段猜关联。整体路径与逐项映射展示声明路径；固定值、查找、逐项不同来源明确说明，未映射项不补值。该面板不是新解析器，不运行校验或生成，不表示历史任务成功；当前平台目录与已保存映射分别标明，历史请求以任务保存正文为准。目录失败显示原因与只读重读入口，原配置保留。本次无API字段或后台规则变更。

| 项目 | 通用输出JSON样例预览 |
| --- | --- |
| 入口/权限 | `POST /api/v1/business-output-versions/{id}/preview-json/`；已登录管理员，非管理员403；不存在404 |
| 请求 | `expected_revision`整数、`expected_contract_sha256`64位摘要、`response_text`原始字符串，三项必填，不接受额外字段及数值转字符串 |
| 200结果 | `preview_only=true`；输出ID/修订/SHA、结构ID、同源`json_schema/fields`目录，`parsed/valid`，`raw_value`原始解析值、`value`缺键默认解析副本、`defaults/warnings/errors` |
| 字段目录 | 由确切节点/字段版本推导；`path`为属性token列表，列表元素以`{list_item:true}`表示，点号/中文名称不拆解；不是实际产物或当前配方可引用来源 |
| 错误/空值 | 非法JSON/重复键/非有限数/过深嵌套/样例结构错误返回200且`valid=false`与明确原因；`parsed`区分合法null和未解析。未知字段保留并提醒，空串/0/false/null不代换；原始文本保留在请求及页面，不存业务结果 |
| 配置错误 | 修订/SHA不一致、合同无效、非通用协议或非法请求400，不回退最新版；响应`Cache-Control: private, no-store` |
| 边界/实现 | `business_output_views.py`、`structured_output_parsing.py`复用共用严格JSON解码/结构缺键解析/字段目录与未知字段提醒；读取现有`security.provider.max_response_bytes`限制。无任务、生成、审核或写入副作用 |

页面按已保存合同提供只读结果/目录和可编辑样例，失败保留原文，修改配置/样例即清空过期报告，丢弃晚到响应。现有影视规则执行器及审核/最终解析只接受`visual_rules.v2`，选择器明确禁止将通用版本当作该业务协议。**text.structured已接通配置、冻结请求、共享解析及原worker候选/原文留存；配方阶段、选择事实、上游来源及各业务写入器仍未接通，不以隔离worker通过代表完整生产可用。**以下字段映射、质量建议与审核语义均针对`visual_rules.v2`。

同一 `parsers/` 的 `allowed_value_fields=[{key,label,choices}]` 提供9个已实现色彩字段的类型化选项，布尔选项为真正的true/false，不接受数字或字符串别名。表单“模型输出允许取值”写入 `allowed_values`，如 `{"base_colors":["navy"],"lighting":["low_key"],"preserve_natural_skin_exposure":[false]}`。键仅表示 `color_design` 下的已声明字段，不执行任意路径；每个数组须非空、唯一且为目录子集。省略更新字段表示保留，空对象清除全部限定，省略单个色彩字段表示接受该字段的全部平台合法值；空数组、null、未知键／值返回400，不局部保存。配置存入唯一 `parser_contract.allowed_values` 并计入版本SHA；没有单独的值配置表或模型副本。保存时调整格式示例使其符合选项，绝不以该示例补齐或修正真实响应。

选项随执行版本冻结，驱动最终请求的 `OUTPUT_ENUMS` 和正式解析、预检使用同一允许集合。`base_colors` 只限制数组内的取值，数量／正文长度仍由原质量建议提供，不新增额度硬限制。不符合允许集合的样例返回 `valid=false`、`visual_rules_output_value_not_allowed` 和具体字段；真实执行保留原响应及失败诊断，不能用示例修正、伪造Smoke通过或自动采用。人工明确修订仍走原审核事实链，不反写原始响应或输出合同。三项基础输出及下游映射不变，影视协议内不接受任意新业务字段或路径脚本；通用JSON的字段定义及只读解析另见上文，不改变影视审核语义。

详情另返回只读 `parser_contract`（协议身份、响应版本、所选字段映射及格式示例）；业务输出schema2合同另冻结所选 `DataStructureVersion` 的完整节点与确切字段定义，整体最大64KiB并共同计入SHA。新建/改选必须选择已发布结构，发布拒绝已退役结构。所有配置的 `response_keys` 必须存在于结构，类型须符合所选解析格式，格式示例也通过同源结构校验。保存版本的预览和实际返回使用冻结结构检查，错误包含响应路径且不截断正文；未保存的目录示例预检不代表结构验收。摘要只带结构ID，不展开节点；页面查看/选用时再读核验详情。后继复制确切结构与解析合同，冻结结构同源编译，不查询当前字段/结构/输出版本替换；既有业务解析器仍检查公共配置中的实际响应边界。0205仅加空外键，不猜旧结构、不重写历史SHA；缺结构旧行的详情/引用明确失败。当前`visual_rules.v2`和`structured_json.v1`各自使用声明的业务执行能力；通用文本worker、配方阶段/选择/上游来源及字段中心已接入。`script_semantics.v1`已支持配置/预览、JSON修订详情和原worker响应侧接线；正式剧本实例准入、请求编译及公开采用/下游仍按M2推进，不将这些分段完成等同于业务全链完成。

质量建议例如 `{"schema_version":1,"negative_constraints_max_items":0,"continuity_summary_max_chars":3000}`；空对象或缺省单项表示不启用，0不是无限。英文建议短语按实际校验器规范化。超建议保留完整候选及 warning/advisory audit；结构、类型、枚举和安全包络仍硬校验。新模型版本接口拒绝 `specification.visual_rules.quality_advisories`；已发布模型旧数据只读，不改写历史。

执行版本可空外键 `output_version_id`用于能力声明的确切输出合同：`visual_rules.generate`要求`visual_rules.v2`，`text.structured`要求`structured_json.v1`；不声明输出协议的能力不接受此引用。完整预检／发布必须有确切已发布输出；草稿可暂缺并明确报未配置。草稿 PATCH 传 null 可清除，未选择时配置 JSON 不补字段。完整定义 `interface.business_output` 冻结精确版本、解析协议、配置及SHA，请求计划及解析结果消费并留存同一快照。版本退役禁止新的配置引用，但不阻止已经冻结的响应按原合同解析，不以最新输出版本覆盖旧请求。既有未配置输出引用的组合仅保留原合同查看，新执行需新建后继并选择输出；不自动迁移或复用旧Smoke。影视规则审核后仍经原 `reviewed_visual_rules.*` 来源链引用，不新增结果库。

#### 通用结构化文本的配置与冻结合同

ProductionRun详情的阶段展开区已接入通用文本结果：按本轮冻结步骤身份/ordinal读取显示名，原Artifact分页列表仅列摘要，用户明确查看时才读取候选详情。共用结构组件按`json_schema`展示对象、列表、标量和明确空值，默认来源/未映射字段提醒及技术合同单独展示。原选择记录区分人工与自动，显式选择仍调用原`select-artifact`；不会审核/写入剧本或触发生成。已结束的运行/步骤只读，读取失败保留上次清单并标记陈旧、禁用提交；原始返回与失败调用通过同一步/同一准备的原任务中心入口查看。动态下游字段目录已接入；结果页接通及隔离验证不代表真实模型全链验收。

`text.structured`在原模型能力目录登记，使用同一`specification.text_execution.operations`输入容量、超时与请求选项；输出额度仍取共用资源策略。执行实例编辑器和资源烟测表单均复用业务输出选择器。原选择目录的`target.operations[].output_parser_keys: string[]`由服务端返回，通用文本为`["structured_json.v1"]`、未声明输出协议为`[]`；前端不另推断协议或自动选择最新版本。

原执行实例保存／后继沿`output_version_id: UUID|null`；资源烟测selection支持同名可选字段，但选中此能力时预检与提交必须给出确切已发布版本。仍使用现有登录／资源操作权限和错误响应：缺输出、协议不符、模型未声明、失效引用返回配置错误；同一烟测幂等标识重放时更换或移除输出版本为冲突，不创建第二次调用。只读预检不产生任务或模型调用，实际提交烟测仍是独立操作。

定义冻结`output_adapter={kind: text, schema_version: 1, adapter: text.structured/v1}`及`interface.business_output`；请求`operation_plan`携带相同输出快照，读取时核对完整合同，不接受替换为另一份自洽快照。系统消息按原布局的`protocol/output_structure/prompt_channels`组装JSON格式要求、确切JSON Schema与明确配置的系统通道，用户消息使用已装配positive通道；不注入剧本或影视创作规则。解析唯一复用`parse_structured_output`，结果包含`value`、`defaults`、确切输出/结构ID及合同摘要；对象、列表、标量和null依所选结构校验，未知字段原值保留并提醒，非法JSON／重复键／错误类型失败，不截断或伪装修复。

已验证两种结构、后继不改冻结请求、缺输出／错协议、原资源预检及隔离幂等、任务中心协议说明。原worker的结果保存与读取合同见下文；配方阶段、候选选择及下游已完成开发接入，真实模型链尚未验收，现用页面与隔离消费者证据分别记录。分项验证见[详细设计G01–G06](optimization/modules/m1-fields/design.md#generic-structured-text)。

通用文本调用沿原`GenerationAttempt → ExecutionTextResponseReview / GeneratedArtifact`保存，不新建结果表、队列或业务入口。原生产文本操作/产物角色由`execution_production_text_contracts.py`统一登记；`text.structured`使用`kind=other`、`role=structured_text_candidate`，不写ScriptRevision、Bible或采用事实。

- 成功结果的原始字符串（包括空白）单独保留；候选包含`schema_version=1`、`output_version_id`、`output_contract_sha256`、`structure_version_id`、`value`及`defaults`。`value`允许确切结构声明的对象、列表、标量或null；`defaults`只记录共享解析器实际使用的缺键默认及来源，不改原文。
- 原产物详情`text_candidate.candidate`返回上述结果封套，实际值在`candidate.value`；额外的`output_contract`是原请求完整冻结合同，`json_schema`由同一数据结构编译器从原请求冻结合同生成，`structure_warnings`为路径/code/message结构提醒，`warnings`为其可读文字。未知属性仍在实际值中保留。读取核对原请求、确切输出/结构、结果摘要及值类型，不查询新版本补解释，不重算默认或采用。任务中心使用同一候选读者。
- 非法JSON、重复键或类型错误进入原响应修正流程，不产生有效候选；`text_response_review.original_text`保留全文，`diagnostics.validation_code=structured_text_result_invalid`及`structure_errors`给出具体错误。原准备的修正动作以相同冻结合同解析，另存corrected正文及真实修正者；成功修正不等于选择产物，更不等于业务内容已采用。
- 原领取/事务处理重复及晚到结果；原文存储失败不产生成功候选，数据库写候选失败回滚本次落库，不自动再次向模型提交。无法确认外部提交结果仍按原不确定状态处理，不伪造返回或成功。

G02的隔离worker验证只证明已冻结准备到原文/候选/读取API。后续G03已登记正式可重复职责，并验证原步骤处理器、顺序调度及自动/人工选择；G05已接配方配置组件和原运行结果字段页。步骤按独立身份冻结，不查询当前目录重排历史。测试仍使用隔离配方、提交准入与假Provider，不代表完整生产配方通过发布或现用页面发起验收；动态下游目录及其字段选择表单也已接通，完整证据及未验范围见同一详细设计，不重复计算历史验证。

`parsers/` 另返回 `field_source_options=[{field,label,choices:[{response_field,value_type,transformation,example}]}]`。创建／PATCH可提交完整 `field_sources={"continuity_summary":"continuity_summary","color_script":"color_script","negative_prompt":"negative_constraints"}`，保存到唯一 `parser_contract.fields` 并参与SHA；省略整个更新字段表示保留。`color_script` 可选择同名英文字符串或 `color_design` 枚举对象（由平台渲染）。最终均进入原审核字段，采用后才可引用同一 `reviewed_visual_rules.color_script`。选定的根字段和类型严格一致，另一格式或两字段并存均不接受，不按响应猜格式。文本模式不发送 `OUTPUT_ENUMS`、不生成 `color_design` 或基础色数量；仍有非空 `allowed_values` 或 `base_colors_min_items/max_items`（包括0）则返回400并保留原版本，须显式清除冲突配置或改选结构化来源。空／部分映射、null、未知来源、任意路径返回400，不局部保存。切换只更新变更映射的格式示例，不修复真实响应；已发布版本不可修改。

三项来源均有受控格式选项，可独立组合（共8种），不是任意字段名／JSONPath／脚本：

| 审核字段／采用后的来源 | 可选响应字段与类型 | 转换与空值语义 |
| --- | --- | --- |
| `continuity_summary`／`reviewed_visual_rules.continuity_summary` | `continuity_summary: string` 或 `continuity_points: string[]` | 说明必须非空；要点至少一项，每项非空安全Unicode文本（允许换行，禁止HTML及不可用控制字符），不校验语种，去首尾空白，保留顺序与所有项，用一个空格连接。合并后核对说明长度建议 |
| `color_script`／`reviewed_visual_rules.color_script` | `color_design: object` 或 `color_script: string` | 对象按所选枚举渲染；文本安全校验后保留，不反推枚举；结构化限制冲突须显式处理 |
| `negative_prompt`／`reviewed_visual_rules.negative_prompt` | `negative_constraints: string[]` 或 `negative_prompt: string` | 数组可空，文本空字符串表示无约束；文本仅按英文逗号拆分。每条去首尾空格／句点，禁止空项、大小写无关的重复项、分号／换行／HTML。与建议短语稳定去重后用英文逗号和空格连接；数量、单条长度建议按模型短语核对，不计建议补充项 |

请求输出说明与负面语法由所选冻结映射派生；质量建议的技术键不变，审计字段标明实际响应来源。非法要点返回 `continuity_points_shape_invalid/text_invalid`，非法负面文本返回 `negative_prompt_text_invalid` 或原短语校验错误；预检HTTP200中`valid=false`且无候选，正式任务仍保存原响应供人工处理，不改记成功。所有格式进入原候选／审核／引用链，不新增结果表，也不自动批准。2026-09-23新增格式的隔离链路验证与实页边界见优化检查点；不代表真实模型和下游生产验收。

管理员可在同一表单展开“输出样例校验（不调用模型）”。目录中的 `response_example` 是与实际请求共用的格式示例，不是默认输入、模型结果或审核事实。

`parsers/` 的 `response_key_policy={pattern,max_length,reserved,description}` 提供响应字段位置规则。创建／PATCH可用 `response_keys={"continuity_summary":"rules.0.summary","color_script":"rules.1.palette","negative_prompt":"rules.2.avoid"}` 为本版本所选业务结果配置JSON对象／固定数组位置，保存到 `parser_contract.response_keys` 并计入原版本SHA；`field_sources`仍只选择已实现的格式与转换，不是第二份位置配置。点分隔，最多4层，首段及对象字段匹配`[a-z][a-z0-9_]{0,63}`，数组下标为无前导零的非负整数，整串最多259字符。每个数组须从0连续完整声明所有位置；不可留空、混用对象字段或按内容猜测顺序，不能用巨大下标分配稀疏数组。任意层禁止`schema_version`、`constructor`、`prototype`，不接受通配符、筛选表达式、脚本、外部读取或字段别名。所选字段的位置不得重复或互为父子（如`visual`与`visual.palette`）。部分映射、null、非字符串、空名和冲突返回400，不产生版本或审计变更。省略整个更新字段保留原配置，空对象明确使用所选格式的顶层字段名。位置变化不修改已审核业务字段或下游`reviewed_visual_rules.*`标识；支持可选 camera_guidance 英文文字；不开放任意新业务字段或新的叶子类型。

同一冻结映射驱动请求`CONTRACT`、字段说明、色彩枚举前缀、完整样例、解析预检和真实worker；根对象和每个声明的中间容器均必须精确匹配。对象缺／多字段、数组长度不符、对象与数组类型互换、父级null、叶子错误类型均失败，不跳过／补齐／重排条目，不读旧名、字面量点号键或两套格式。固定位置并不验证同类型正文的业务意义，仍须人工检查内容与槽位是否对应。根`schema_version`仍固定为2，叶子继续原文本／数组／色彩对象校验。格式／位置同时变化时一次构造新样例，分支互换不覆盖其他值；格式示例不用于补齐真实响应。发布后不可原地调整，后继不替换旧实例／请求；候选、人工采用和下游读取仍走原事实链。质量建议键仍表示业务语义，数量提醒记录实际响应位置、正文长度提醒记录转换后的业务字段。结构映射不代表真实供应商调用或创作效果验收。

| 输出样例预检 | 当前合同 |
| --- | --- |
| 接口与权限 | `POST /api/v1/business-output-versions/preview-visual-rules-output/`；登录管理员，非管理员403、未登录401；旧模型路径已移除 |
| 请求 | 必填 `response_text: string`（完整JSON正文，空字符串也是可检验的无效样例）。新建未保存草稿可传 `quality_advisories: object`、`allowed_values: object`、完整 `field_sources: object`及`response_keys: object`；缺省无附加限制，采用初始结构化色彩映射及其字段名；已保存版本必须成对传 `output_version_id: UUID`、`expected_revision: integer>=1`，不得混入任何草稿配置。拒绝null、任务／项目／批准ID和未知字段，无分页 |
| 成功响应 | HTTP200：`preview_only=true`、`response_schema_version=2`、`valid`、`candidate`、`warnings`、`errors=[{code,message}]`；另返回 `output_version_id`、`output_revision`、`output_contract_sha256`，未保存草稿三项均为null。有效时candidate为本版本所选字段的解析文字；无效时candidate为null且errors非空 |
| 错误口径 | 请求字段／质量建议非法、版本不存在、修订冲突、合同缺失／损坏返回400。样例缺字段、重复JSON键、非有限数、代码围栏、非法枚举／文本及超安全字节边界返回200但valid=false；不修补、截断、补值或生成 |
| 数据与副作用 | 读取当前安全配置和指定输出版本；已保存版本的合同与质量建议取自同一记录。使用与真实执行相同的严格JSON读者及影视规则解析器；不写模型、候选、审核、任务、Smoke证据、费用或正文日志，不读取项目内容、不外呼 |
| 使用与验证 | 页面主动粘贴／填入版本格式示例并校验；已保存版本先保存修改再预检，已发布／退役版本可只读预检。改变样例、建议或版本会清除旧结果，迟到／错版本／错摘要响应不覆盖新内容，失败保留样例。仅格式验证，不验证调用成功或创作质量；仍须经原人工审核才可下游引用 |

最小请求：`{"response_text":"{}"}`，响应为 `{"preview_only":true,"response_schema_version":2,"valid":false,"candidate":null,"warnings":[],"errors":[{"code":"visual_rules_response_invalid","message":"visual_rules.generate 响应根对象字段不完整或包含未知字段。"}],"output_version_id":null,"output_revision":null,"output_contract_sha256":null}`。有效样例取选中版本的 `parser_contract.response_example`；新建草稿取同源目录，避免文档重复维护枚举。质量建议超限仍返回完整candidate与warnings，不伪装Smoke失败或通过。


生产配方 route 冻结的是明确的执行目标、版本合同及选择规则，不是把区间直接传给 ComfyUI。每个生成任务在提交前解析出具体值并校验范围、冻结执行快照；种子可以按任务／变体确定，帧数可以按已冻结规则由镜头时长计算。因此“每次执行有确定参数”不等于“整条路由下所有镜头必须手填同一组常数”。

工作流详情的 `workflow_input_schema` 投影冻结参数与 `asset_groups=[{key,media_kind,min_items,max_items,semantic_keys,wiring}]`，无分组时为空。semantic_keys为固定槽的有序子集；固定预处理参数不伪装可编辑参数。实际请求从共用执行合同读取端口键、数量、类型、配对及参数规则，提交明确Asset使用，不发送节点图或分组定义。Smoke使用与实例相同的输入解析器，不能借用推荐、草稿或Live Validate充当真实产物验收。

正式页面可对任一已纳管且已确定唯一主输出的 candidate 调用 recommendation action，包括存在 `workflow_projector_*` 阻断、已由 Generic fallback 自动匹配或已经提升为工作流版本的候选。它把该 candidate 的可达节点精确数量、主输出映射、唯一节点角色、可证明连接、受控模型 selector、参数目标和素材槽填入**本地未保存表单**。单份候选不能证明 optional node，也不能替管理员决定图族长期 key/name、业务产物语义、关键 discriminator 或发布优先级；响应固定 `requires_human_confirmation=true`，任何保存仍只创建 draft，发布继续执行完整冲突校验。已发布专用方案不会改写既有工作流；管理员必须从同一 candidate 显式生成后继合同草稿，并提升到原模板的新版本。编辑器中的“执行引擎与接线”展示 compiler/output/dynamic Adapter 与 projection kind：它们说明平台用哪段已审核代码编译合同、取回产物和处理动态输入，不是 ComfyUI 工作流业务字段。扫描建议不推断执行能力，须在该页显式选择后再生成合同；空配置只表示尚待补齐，不能作为可执行证明。

上述管理 API 仅 staff 可读取候选与合同草稿；projector family/version 的写入与生命周期动作仅 superuser。稳定错误包括 `workflow_candidate_required`、`workflow_contract_draft_mismatch`、`workflow_contract_blocked`、`workflow_projector_catalog_unavailable`、`workflow_projector_ambiguous`、`source_changed` 与 `source_scope_changed`；客户端应展示服务端 `detail/blockers/recovery_actions`，不得把失败降级为未匹配或空结果。详细页面顺序、错误恢复和人工验收见 [ComfyUI 已保存工作流导入、验证与发布手册](comfyui-saved-workflow-validation.md)。

WorkflowTemplateVersion详情返回只读v2 `workflow_input_schema={available,reason_code,parameters,asset_slots,asset_groups,dynamic_references,dynamic_reference_modes}`，用于参考冻结工作流本身的范围与默认值，不合成烟测样例。合同无效时available=false、reason_code=workflow_input_contract_invalid，数组为空、dynamic_references=null、dynamic_reference_modes={}；页面明确不可核验，不能当作零输入。真正的烟测表单只使用smoke-options/smoke-contract返回的统一资源定义；本次素材、参数、提示词及审核选择保存在原Preparation，可从“准备与输入”查看。Attempt详情只投影摘要，不复制准备正文；输入错误400、定义/活动任务/幂等冲突409。固定槽、动态顺序、分类用途、音轨配对和预处理都来自精确版本合同，不按模型名补默认值。

已保存工作流的 `comfyui-workflow` action 只从明确绑定的 ComfyUI deployment 读取 `object_info`，并复用正式 API graph → UI canvas 转换器生成可导入 JSON；它不会 submit、poll 或创建 smoke/生产任务。validated、published 与 retired 版本只能使用持久化 `validated_deployment`，请求中的 `deployment_id` 如与证据不一致会被拒绝；没有 `validated_deployment` 的 draft 必须传入显式 `deployment_id`，且该部署须为 active、ComfyUI 驱动并与工作流已有 Provider/模型版本绑定兼容，禁止回退全局或默认 ComfyUI。停用部署、非 published 模型、绑定不一致或 validation target hash 漂移都会在网络访问前失败。元数据响应包含实际 `deployment_id`、`deployment_name`、`deployment_source`，并固定声明 `direct_open_supported=false`、`direct_open_url=null`、`open_mode=download_import`；`?download=true&deployment_id={uuid}` 返回附件。浏览器客户端必须明确提示“下载后导入”，不得伪造跨页面直接打开能力。读取 `object_info` 或转换失败时返回显式 4xx/503，且不回传远端原始诊断正文。

工作流 smoke 创建返回 `202 WorkflowSmokeAttempt`，客户端轮询 `queued/submitting/running/cancel_requested` 到 `succeeded/failed/cancelled/uncertain`。fixture hash 相同的 active 请求复用原 attempt；不同 fixture 返回冲突。`submission_uncertain`/`remote_state_uncertain` 表示提交边界可能已被远端接受，未消歧时任何 fixture 都禁止重放。staff 只能通过 attempt 的 `resolve-uncertain` 追加 `confirmed_not_submitted`、`confirmed_remote_failed` 或 `confirmed_remote_succeeded`；原始 request id 只写不回显，并且远端终态必须与该 attempt 的稳定 submission token 一致。人工结论不修改旧 attempt、不生成 passed evidence；即使确认远端成功，也仍需新 attempt 重新收集并校验后才可发布。ComfyUI 明确失败且 history 含标准 `execution_error` 时，`error_message` 会保存服务端脱敏后的失败节点、异常类别、具体原因与处理建议；节点类型以本次提交的冻结 graph 为准。原始 `status.messages`、traceback、当前输入输出和 request id 不进入 API；结构未知时明确提示到 ComfyUI History 继续定位。OOM 仍使用稳定分类 `provider_smoke_failed`，但可从具体错误说明识别显存原因。发布只接受重新读取后与当前版本、成功 attempt、fixture、compiled graph 和 validated target 完全一致的证据。

所有 Deployment 烟测也统一返回 `202 ModelDeploymentSmokeAttempt`：文本任务按 `queued → submitting → succeeded/failed/uncertain` 推进，async media 使用 `queued/submitting/submitted/cancel_requested`。它们的 `resolve-smoke-uncertain` 只适用于 Deployment attempt，不能用于 WorkflowSmokeAttempt。

上述接口必须通过候选准入、草稿持久化、projector 生命周期、promotion 不重读来源及 Live Validate/Smoke 门禁的仓库契约测试；本文档变更未执行真实长 GPU smoke。目标环境仍须逐版本记录 attempt、fixture hash、真实提交、输出字节校验、耗时及 OOM/uncertain 核对，不能以自动测试或 validate 替代。

### PromptProfile 配置面

#### 内容来源版本（受支持读者，2026-09-22）

正式页面：系统设置 → 字段维护 → **内容来源版本**；策略规则编辑中点“选择已发布来源版本”只读选用。管理员可命名受控来源并选择统一结构的确切已发布版本，不能输入任意数据库路径、URL或脚本。当前支持 `project.logline`、`style.visual_prompt`、四个 `reviewed_visual_rules.*` 文字读者，以及目录中19个已有 `request.*` 文字输入；范围及确认策略来自原读者定义，类型/使用约束引用同一个结构，不另配第二套权限或业务Schema。结构选择器支持搜索、分页、读取当前精确版本，不自动选最新版；可展开“试填结构样例（不保存、不执行）”查看同源控件与解析结果。结构作者统一进入字段维护的“输入／输出结构”。

`request_text/v1`只消费本次明确提交的文字（`kind=literal`），范围为`request`、确认方式为`confirmed_request`，不要求虚构项目、不查项目正文或其他任务。支持的后缀为`theme/lighting/environment/action/camera/style/language/text/bgm_style/dialogue_delivery/negative_constraints/subject/composition/color_palette/motion/duration/aspect_ratio/sound_effects/voiceover`；即使名称涉及时长／画幅，这也是文字描述，不是模型数值参数，不会改变帧数、尺寸或调用语言配置。来源作者仍只接受text/long_text字段，数字、枚举、媒体和任意新Key不因此获得读者。主题／风格可显式预览项目内容后以带引用凭据的literal复制、修改并确认；这与不可手工替换的`project_snapshot/v1`不同。

| 接口 | 请求与响应 | 权限、错误与副作用 |
| --- | --- | --- |
| `GET /api/v1/content-source-versions/` | 按 `status/source_key/reader_selector` 筛选、`search` 搜索名称；`page/page_size` 分页，默认至多50、上限200；返回 count/results 及 `id/source_key/version/name/reader_selector/status/data_structure_version/revision` | Staff；窄列摘要不读取或核验完整合同、结构节点及说明；查看/选择须再读详情，不把列表摘要当作可执行配置 |
| `GET /api/v1/content-source-versions/{id}/`、`GET .../readers/` | 精确版本详情／受支持来源选项；目录说明填写位置、消费者、权限范围和确认方式 | Staff；无对象404，无权限401/403 |
| `POST /api/v1/content-source-versions/` | `name`（1–120字符）、`description`（最多1000字符，可省略）、`reader_selector`、必填 `data_structure_version` UUID；返回201草稿 | 家族UUID、版本1、revision及摘要由服务端分配；结构须已发布且根字段为text/long_text。缺结构、未登记来源、错类型或多余字段400，不调用模型 |
| `PATCH /api/v1/content-source-versions/{id}/` | `expected_revision` 必填，可改上述四个作者字段；返回更新后的草稿，省略字段保留 | 仅草稿；过期修订、已发布／退役、无效读取位置/结构400，事务内保存并追加生命周期审计 |
| `POST .../{id}/publish/`、`successor/`、`retire/` | 仅接受 `expected_revision`；publish/retire返回200，successor返回201新草稿 | 发布不夹带编辑；下一版只能从最新不可变版本建立；退役禁止新规则选用，不影响既有精确引用；无DELETE入口，不改写任务、审核或费用 |

来源合同为schema2，包含 `data_structure` 的身份/版本/名称及确切字段节点快照，由同源编译器派生Schema，不持久化第二份业务Schema；规范化合同沿共用64KiB边界，持久化大小及SHA-256，不包含业务正文。`ContentSourceVersion.data_structure_version`以PROTECT引用确切版本；新建/改选/发布要求结构已发布，后继复制原结构ID，退役不修改既有快照。0204只加可空引用以保留未处理旧行，不猜造结构、不重算旧摘要；缺结构或旧schema1合同不可作为新链配置，不能靠保存隐式升级。已发布来源不可原地编辑。`PromptProfileFieldRule.source_version` 可选择精确已发布来源；服务端派生 `source_selector/generation_source_selector`，同时提交不一致值则拒绝。文字字段可选择passthrough、model_generated或translate_only；后两者将同一精确来源冻结为主要原文，采用结果后填入本字段，来源正文和定义不被覆盖。

`reader_contract`随版本存储；API只读`reader`经完整摘要核验后返回原合同，`readers/`才是当前可选作者目录。草稿改名称、发布、退役或创建后继均不重新选择读者；仅新建或明确改变`reader_selector`时从当前目录取合同。读取规则改变必须使用新的已实现协议，不能改写v1语义。合同缺失、被改写或无法执行时返回400，禁止用当前目录重建或通过保存重新计算摘要掩盖损坏。实例`source_reader`直接使用策略中所选精确来源的合同；同一端口的多个来源定义必须一致。管理页分别展示待保存的目录选择和本版本已保存规则，当前页面位置只作导航帮助，不替换历史规则。

策略schema8的该规则冻结 `source_definition`（身份、版本、业务名称、说明、读取协议、统一结构快照及摘要），执行实例按读者的真实范围自动派生端口并冻结 `interface.profile_inputs.*.source_definitions`。同一来源多字段使用仍只有一项输入确认。准备表单展示该精确业务名称：请求文字使用原literal解析与冻结；项目快照或已审核输出通过原解析器核对owner、范围、修订并确认。缺值、错误类型、非合同来源方式或伪造引用均拒绝；不自动读项目补值。未选择来源版本的基础来源规则不产生此引用，不改写其现有合同。下一版不会自动替换策略、实例或请求；退役依赖可随策略复制到待修改草稿，但重新发布前必须明确换成已发布来源。

来源结构在准备解析和策略来源投影中均按冻结快照校验实际值，错误包含输入路径及约束（如maxLength），不截断、隐式转换或查询当前结构代替；嵌套值复用相同编译与校验服务，未声明属性保留。验证边界：本次隔离数据库/API与前端组件测试覆盖结构选择、精确冻结、运行时约束和分页；类型检查/构建不代表当前运行页面已加载新版本。未迁现用库、未重启服务、未做本次浏览器或真实调用验收。任意JSON解析/映射、新读者和输出结构接入仍未完成，不能把来源配置当成全平台已去除硬编码。

### 字段定义版本 API（M1基础，字段及结构维护入口已接入；中心映射预览待接续）

用途：维护同一个字段身份的确切定义版本。代码入口为`field_definition_views.py`、`field_definition_serializers.py`及`services/field_definitions.py`；写入`FieldDefinitionVersion`，不保存任务值、输入输出方向或全局必填/默认。不自动更新现有Profile、实例或准备，不产生调用或业务产物。入口为系统设置 → 字段维护的“定义版本”，规则编辑器只选用可用版本，并可跳转对应字段。身份路由统一为`/field-definitions/`，旧字典CRUD路由已移除；字段ID及已有版本不变。M1-09中心映射预览及引用导航已接入；模块用户验收仍属M1-10，不能把此段当作整个M1交付。

| Method / Path | 请求 | 返回与副作用 |
| --- | --- | --- |
| `GET /api/v1/field-definition-versions/` | 按`field_definition`（UUID）、`status`、`value_type`、`category`筛选，`search`匹配key/标签；`page/page_size`使用平台分页 | `count/next/previous/results`；摘要含版本ID、身份ID/key、版本号、状态、标签、分类、类型、单位及时间，不带完整说明/枚举；按身份、版本倒序稳定排列；无写入 |
| `POST /api/v1/field-definition-versions/` | 必填`field_definition`；可选`source_version`（同身份的确切版本ID）及定义内容 | 201完整版本。服务器分配递增版本号，始终创建draft；克隆复制明确版本，再应用提供的内容，不取最新版本 |
| `GET /api/v1/field-definition-versions/{id}/` | 确切版本ID | 200完整定义，包含说明、影响、枚举值及只读`preview_example`；示例仅按所选版本类型提供，不读取业务值；停用版本仍可查看；无写入 |
| `PUT/PATCH /api/v1/field-definition-versions/{id}/` | 草稿定义内容；身份不能改变，不接受source_version改变已有版本 | 200保存后的完整版本；已启用/停用版本不可改写，需创建后继草稿 |
| `POST /api/v1/field-definition-versions/{id}/publish/` | 无请求内容（可提交空对象） | 200启用后的定义；检查必要定义内容、类型/枚举及字段启用状态，无额外审批；不会发布Profile或配置实例 |
| `POST /api/v1/field-definition-versions/{id}/retire/` | 无请求内容（可提交空对象） | 200停用后的定义；不改变内容或引用；草稿尚未启用，不能停用 |
| `GET /api/v1/field-definition-versions/options/` | 无 | `value_types/statuses`选项目录，含可读标签；来自同一版本模型；无写入 |

定义内容：`label`（≤160字符）、`category`（≤80字符、小写稳定标识）、`description/impact`（说明文本）、`value_type`（text、long_text、integer、decimal、boolean、enum、object、list、media）、`unit`（≤80字符、无隐式单位转换）、`value_options`（枚举的非空、不重复文本数组）。草稿允许内容不全，启用时必须有label/category/value_type；enum必须有选项，其他类型不得携带枚举选项。对象子字段、列表元素及媒体用途不在此处重复定义，后续由统一结构使用规则承担。

示例（身份ID需用真实已登记值，此处仅为占位结构）：`{"field_definition":"00000000-0000-0000-0000-000000000001","label":"动作次数","category":"custom","value_type":"integer","unit":"次"}`。响应`id`是版本身份，不是字段身份；`field_definition`及只读`field_key`表示共同字段身份。`source_version`仅写入请求使用，不是任务实际来源证明。版本号、状态与时间由服务端管理；POST提供这些只读属性不会使其成为可写配置。

鉴权沿平台管理API的staff权限；未登录401/403、非staff403。参数、跨身份克隆、不可编辑版本及非法状态操作返回400（统一`error.status_code/code/details`信封）；不存在ID返回404；不提供DELETE直删（405）。状态操作拒绝夹带定义修改；失败事务不留下部分定义。该接口不增加发布审批或新的审计流水，不涉及外部服务、密钥、任务值或费用。实际API及隔离验证范围记于[详细设计M1-02](optimization/modules/m1-fields/design.md#m1)，未做现用服务加载/浏览器或真实模型验收。

### 统一结构版本 API（来源与业务输出共用）

正式作者入口：系统设置 → 字段维护 → **输入／输出结构**。按页搜索版本，新建或从明确版本建立草稿；逐节点选择父级、外部属性名、顺序和确切字段定义版本，配置使用约束及缺键默认。约束控件与编译器共用目录，默认值不是字段样例；空排序输入不会自动变为0。只对修改内容提交PATCH，启用前先保存当前编辑；已启用/停用内容只读，新版本不自动替换引用。校验与试填只针对已保存且未再编辑的版本，不把旧结果用于未保存草稿。页面/API失败保留输入，不触发任务、采用或真实模型；当前仅代码与隔离验证，未加载现用环境。

用途：以一个结构定义业务输入或输出的形状，不保存任务值，也不另存一份可编辑JSON Schema。代码入口为`data_structure_views.py`、`data_structure_serializers.py`及`services/data_structures.py`；写入`DataStructureVersion`和`DataStructureNode`，节点引用确切`FieldDefinitionVersion`。定义用于什么步骤由消费者决定，不在此处固定输入/输出方向。**ContentSourceVersion与BusinessOutputVersion及其schema2冻结合同均已接入；来源文字值和输出响应复用同一结构校验，两个配置入口复用确切结构选择器。M1-05的JSON解析/映射共用服务及只读预览已实现，M1-06同源值控件与样例预览已实现；M1-07共用媒体来源策略、读取预览及选择控件已实现，M1-08已接实例媒体策略，配方固定素材及既有参考清单已接实例来源策略，声明式Profile已消费同一媒体解析结果，M1-08通用文本生产者及配方下游已完成隔离组合验证，M1-09字段与结构维护、中心映射预览和引用导航已接入；M1-10模块用户验收待完成，不能将合同接入当作M1模块交付。**

| Method / Path | 请求 | 响应及副作用 |
| --- | --- | --- |
| `GET /api/v1/data-structure-versions/` | 可选`structure_key` UUID、`status`、`search`名称、`page/page_size` | 200分页摘要`count/next/previous/results`；仅ID、结构身份、版本、名称、状态、时间。默认50、最多200，按结构身份/版本倒序/ID稳定排序，不查询节点或说明正文；无写入 |
| `GET /api/v1/data-structure-versions/authoring-options/` | 无 | 200`node_defaults/statuses/constraint_schemas/value_schemas`；节点初始值、模型状态和编译器允许的类型/约束目录，供编辑器同源渲染。不是业务默认值目录，不读任务/业务正文，无写入；沿原staff权限 |
| `POST /api/v1/data-structure-versions/` | 可选`name/description/nodes`，或`source_version`确切来源ID | 201草稿详情；无来源时新结构身份/v1，有来源时复制明确版本的节点/定义引用并分配同组下一序号，再应用请求内容，不自动取最新正文 |
| `GET /api/v1/data-structure-versions/{id}/` | 确切版本ID | 200完整版本及节点，字段含义仍由确切定义版本读取；无写入 |
| `PUT/PATCH /api/v1/data-structure-versions/{id}/` | `name/description/nodes`；省略属性不修改，提供nodes时原子替换整组草稿节点 | 200保存后详情；可用/停用版本不可改写，已有版本不能改来源。草稿可不完整，非法父节点ID或错误基本值类型不能保存；失败不留下部分替换 |
| `POST /api/v1/data-structure-versions/{id}/publish/` | 空对象或无正文 | 200可用版本；检查名称、唯一根节点、父子关系/循环、确切可用字段版本、使用规则和编译结果，不增加人工审批，不发布配方/实例 |
| `POST /api/v1/data-structure-versions/{id}/retire/` | 空对象或无正文 | 200停用版本，内容和已有引用不改；停用后不能重新启用，需创建新版本 |
| `GET /api/v1/data-structure-versions/{id}/schema-preview/` | 确切版本ID，无业务样例 | 200`{valid,json_schema,errors}`。成功返回由同一节点树生成的JSON Schema 2020-12；配置不完整返回`valid:false,json_schema:null,errors:{路径:[说明]}`，不伪装为空的有效Schema；无写入/调用。此接口不是业务JSON解析/映射预览 |
| `POST /api/v1/data-structure-versions/{id}/value-preview/` | 必须显式提供`{"value":任意JSON值}`，包括null/false；不接受其他属性 | 200`{preview_only:true,structure_version_id,valid,value,defaults,errors}`。按确切结构解析缺键规则，返回拷贝后的值、逐路径默认来源及校验错误；草稿/可用/停用均可只读预览。值不合规则仍返回实际值和`valid:false`；配置无效时保留输入、defaults为空并返回配置路径错误。无配置/任务/素材写入或外部调用；并非业务映射预览或已执行结果 |

节点请求/响应（数组内每项）：

| 属性 | 类型与含义 |
| --- | --- |
| `node_key` | 结构内唯一稳定key，1–100字符，小写字母起始，可含数字/点/横线/下划线；与外部属性名、字段身份均不同 |
| `field_definition_version` | 确切字段版本UUID，草稿可null；不按字段名推断、不自动选最新 |
| `parent_key` | 同结构父节点key；根为null。对象有任意声明子节点，列表必须有一个元素定义；标量/媒体不可有子节点 |
| `property_name` | 对象属性名，≤160字符，可与字段key不同；根/列表元素为空字符串 |
| `position` | 非负整数，缺省0；同序号按node_key稳定排列 |
| `required` | 布尔，缺省false，仅用于对象子属性；根/列表元素必须false |
| `has_default / default_value` | 缺键规则：未启用默认且required为true时报缺失，否则省略；has_default=true时仅对缺失键填入default_value。启用时必须显式提供default_value，允许null时可明确填写null；未启用只允许null占位。默认值及其嵌套缺键解析结果须符合节点结构，根/列表元素不能配置缺键默认。字段字典不保存此使用规则 |
| `allow_null` | 布尔，缺省false；独立于必填及空值，编译为可空类型分支 |
| `allow_blank / allow_empty` | 布尔，缺省true；分别控制文本空串、对象/列表空集合，不把0/false当空值 |
| `constraints` | 使用处约束对象，缺省`{}`；不复制字段标签/含义。文本可用min_length/max_length；整数/小数可用minimum/maximum；列表可用min_items/max_items；枚举可用字段版本选项的非空enum子集；媒体必须明确media_types（image/video/audio不重复数组），可选非空usage。错误类型、未知规则和矛盾上下限明确定位 |

媒体值Schema为`{"asset_id":"确切Asset UUID"}`，不接受本地路径、URL或Base64替代引用；数量通过列表节点表达。`x-media-types/x-media-usage`记录使用约束，**当前编译不查询真实素材存在/权限/媒体类型，实际素材解析与装配由M1-07接入后验证**。原生执行参数仍由实例/目标合同维护，不归此结构复制。

隔离示例：`{"name":"自定义输入","nodes":[{"node_key":"root","field_definition_version":"00000000-0000-0000-0000-000000000001"},{"node_key":"motion","parent_key":"root","property_name":"action","field_definition_version":"00000000-0000-0000-0000-000000000002","required":true}]}`。两个占位UUID需分别换成已登记的对象、文本定义版本。编译投影带`x-node-key/x-field-definition`，让字段含义与路径保持关联；对象未声明属性允许保留，不自动晋升为字典字段或静默删除。默认值以JSON Schema的default注释投影，但实际赋值只有共享缺键解析器负责；不能依赖JSON Schema校验库自动补值。JSON文本解析、配置映射及未映射提醒走下方M1-05共用预览。

缺键预览不会用默认值替换已有null、空串、空集合、0或false；类型错误不强制转换，空值仍按allow_null/allow_blank/allow_empty检查。缺失可选父对象不凭子字段默认值创建；已经存在的对象或显式默认对象可继续处理子键，现有列表只逐项解析、不增造元素。对象的最终形状在子键解析后校验；例如显式默认`{}`可由子键默认补齐必填值。原始请求值始终不变，未声明属性也保留。`defaults`每条包含`path/node_key/structure_version_id/field_definition_version_id/source/value`，source固定为configured_default，value是该次插入的原配置默认值；嵌套追加项各有独立来源，最终结果看外层value。路径用`value.rows[0].name`，带点/括号等外部属性名用JSON括号路径，避免歧义。无默认来源不会假装成模型/人工输入；错误结果必须先看valid，不凭value非空判断成功。

0206迁移文件只为结构节点添加has_default=false及default_value=null，不启用历史默认、不重算历史合同。新的冻结结构包为schema2，完整保存缺键规则；旧结构包不兼容、不查当前版本补齐。来源/业务输出外层合同仍为schema2。原`validate_structure_value`保持严格、不补值的原文校验，缺键处理由`resolve_structure_value`显式调用并返回来源；JSON映射预览已接入；实例/Profile运行消费者由M1-08接入，不把辅助返回校验成功伪装为原模型提供了默认字段。M1-06共用表单已复用此接口：在来源/输出的确切结构选择处展开“试填结构样例（不保存、不执行）”，按Schema填写嵌套对象/列表、文本/数字/枚举/布尔和媒体引用；原始样例、解析结果与默认来源分开显示。未提供/null/空值/0/false及未知字段明确区分，字段错误与控件路径对应；数值尚不能读取时不提交之前的值。媒体详情按需读取同一Asset并复用正式预览，原ID/错误原文保留；样例只选已有素材，不上传、保存配置或建任务。样例及Schema中的示例/默认不写入业务准备；M1-07媒体来源公共基础已实现，实例媒体策略已接入；配方固定素材已接入；声明式Profile已消费同一媒体解析结果，通用文本配方下游及字段维护中心已在M1-08/M1-09接入并完成隔离验证；M1-10模块用户验收仍待完成。

鉴权沿平台staff管理权限；未登录401/403、非staff403、缺失ID404、非法写入/发布400（统一error信封）；不提供DELETE（405），状态操作拒绝夹带修改。版本号、身份、状态不可在普通请求中伪造。只写配置表，不生成任务/素材，不记录虚构审核，不触发外部调用、配置发布或历史请求重写。0203只增加表，不回填现有来源/输出合同。隔离验证及未完成范围见[详细设计M1-03](optimization/modules/m1-fields/design.md#m1)，现用迁移/服务加载、真实浏览器页面和真实业务产出未验。

### 结构JSON解析与配置映射预览

M1-05提供确定性JSON解析与配置路径映射，不解析任意文本语法、执行表达式或自动生成业务动作。用户配置任意受支持字段/层级；已有原生输出协议和通用解析复用`json_contracts.parse_json_value_text`，对象协议只补充根对象要求。`structure_mapping.py`提供路径/稳定key适配，对象、数组、逐项与固定值复用`execution_structure_mapping.py`装配器，没有第二个执行队列或结果库。

| Method / Path | 请求 | 响应与失败 |
| --- | --- | --- |
| `GET /api/v1/data-structure-versions/{id}/path-options/` | 确切结构ID | 200`{structure_version_id,valid,options,errors}`。options逐节点给出path、node_key、完整确切field_definition、nullable及structure_version_id；配置不完整则valid=false、options=[]及具体错误，不提供猜测目录 |
| `POST /api/v1/data-structure-versions/{id}/mapping-preview/` | 路径id为目标结构；正文必须是source_structure_version（来源结构UUID）、source_text（原始JSON文本）、mapping（下述schema1配置） | 200预览报告，包含preview_only=true、valid、parsed、两端确切结构ID、raw_value、source_value、value、defaults、provenance、warnings、errors。字段/格式/类型问题是valid=false，不创建任务。缺请求字段、非法ID/结构配置或夹带写操作400；未知目标404；权限同staff结构管理 |

两端可选择确切草稿/可用/停用结构进行只读配置预览，草稿本身仍不能冻结用于执行；不会默认选最新版本。路径目录的path为token数组：字符串代表原样对象属性名，非负整数代表确定列表下标；目录用`{"list_item":true}`描述元素层级，该标记只供选项/表单展示，不能作为实际取值token。遍历未知数量列表使用each及item相对路径；不支持星号表达式、脚本或隐式列表展开。字段名含点/括号时仍是一个完整字符串，不切分或猜字段含义。

mapping外层严格为`{"schema_version":1,"node":映射节点}`，节点只支持以下形状：

| kind | 其余属性 | 含义 |
| --- | --- | --- |
| object | fields：目标属性名到子节点的对象 | 构造声明的目标对象，不凭名称自动登记字段 |
| array | items：有序子节点数组 | 构造明确顺序列表 |
| each | source：path或lookup节点；item：子节点 | 逐条读取声明列表，item作用域是当前条目，支持嵌套each；不合并/过滤/自动去重 |
| literal | value：显式JSON值，包括null/false/0 | 固定值；与构造值共用目标缺键默认及最终结构校验，不做类型转换 |
| path | scope：root或item；path：属性/下标token数组 | root指整个解析来源；item仅在each内有效，空数组指该作用域整个值。静态路径须在合同目录，实际缺路径/越界显式报错，不静默省略或找替代值 |
| lookup | collection：path节点；key_path：条目稳定key路径；key：literal或path节点；value_path：选中条目取值路径 | 在明确对象列表中按非空文本key精确查找；不翻译、不按显示名匹配。重复key、缺声明、空key均报错，不选首项；value_path=[]可取整条声明，也可取其列表供each使用 |

最小映射样例：请求中`source_text`为`{"mood_note":"紧张"}`，来源结构登记mood_note、目标结构登记tone后，mapping为`{"schema_version":1,"node":{"kind":"object","fields":{"tone":{"kind":"path","scope":"root","path":["mood_note"]}}}}`。该例纯配置，不创建实际剧本/素材。

报告语义：parsed仅表示JSON已解析，不能代替valid。raw_value保存解码后的原始值，source_value为按来源合同补默认后的副本，value为目标映射/缺键处理后的结果；映射失败时value=null，目标校验失败则保留已有结果与错误。JSON语法错误提供行/列/字符偏移，不修复代码围栏、重复属性或非有限数；原始source_text不修改、不重复落库。原文大小沿现有`security.provider.max_response_bytes`，不新增硬编码字数或创作质量门禁。同步预览还复用这一配置值作为累计展开预算：统计中间取值、默认复制和构造/路径记录的JSON字节工作量，复制或逐项追加前检查；超出返回mapping.preview_budget（单值预览为value.preview_budget），不继续笛卡尔式放大。此计数不等于最终提示词长度，也不是生产调用新增门禁；可缩小样例或改为相对item来源。结构配置的复合默认展开沿既有64KiB配置边界，不恢复D01或新增资源配置项。

defaults来自M1-04唯一解析器：source前缀表示来源默认，value前缀表示目标默认，每条含确切结构/字段版本。provenance记录目标path、path/lookup/literal等来源kind、实际source_path、查找key及两端结构ID；固定值没有伪造的上游路径。warnings给出结构外属性的路径及unmapped_field，raw_value/source_value保留这些字段，但不能在未登记时当正式目录路径选择。errors为路径到说明列表：source_text语法、source字段、mapping配置或value目标错误；不自动调用模型修复，不把默认当模型返回或人工采用。

接口仅读取所选结构及定义版本，编译/解析/映射不访问项目、Bible、素材、审核或任务，不写配置、不触发网络。映射保存归原配方/实例/Profile版本编辑器，不在字段中心存副本；M1-09的“字段映射预览”复用同一个组合编辑器与上述预览服务，选择确切来源/目标结构，显示原始解析值、来源/目标结果、默认与取值来源、警告和路径错误。样例/映射变化立即使旧报告失效，未完成局部数字不提交旧值，结构读取失败保留映射并明确报错；预览含草稿不代表可用于执行。完整引用导航及M1模块验收仍待后续，未作现用真实映射运行验收。隔离验证及未验证范围见[详细设计M1-05/M1-09](optimization/modules/m1-fields/design.md#m1)。

`GET /api/v1/data-structure-versions/mapping-options/`（staff，只读）返回`schema_version`、`nodes`与`scopes`，后两者是`{value,label}`目录。映射方式及root/item作用域与映射校验器使用相同注册表；对象/列表/固定值继续共用原装配器目录，不新增业务字段枚举。无POST/保存/执行动作。

### 共用输入来源目录与媒体装配预览

代码入口：`services/input_source_policies.py`、`data_structure_views.py`及对应序列化器；共用前端为`InputSourcePolicyEditor`、`MediaSourceSelectionEditor`和原`ExecutionInputAssetPicker`。这是M1-07公共基础；M1-08已将实例媒体策略接入原编辑器、保存/克隆/定义冻结和实际准备读取（见下文），配方固定素材及既有参考清单已接来源策略（见下文），声明式Profile媒体消费者复用同一解析结果和来源摘要（见下文），字段中心入口归M1-09。未迁现用库、发布配置或执行真实任务；没有新增执行队列或来源映射副本。

| Method / Path | 请求与权限 | 响应、错误及副作用 |
| --- | --- | --- |
| `GET /api/v1/data-structure-versions/input-source-options/` | 无正文；已登录用户可读取固定能力目录，未登录401/403；不开放结构配置管理 | 200：`schema_version/sources/orders/configuration_owner/description`。每项含value、label、kinds、reader、available、requires、description；目录可用不代表每个业务入口均提供所需上下文。无数据库写入、模型或网络调用 |
| `POST /api/v1/data-structure-versions/media-source-preview/` | 必须`policy`及`selection`，可选`override`；无路径ID，不接受run/stage或执行参数；仍限staff，非staff403 | 200：`preview_only=true/valid/resolved/errors`。配置/引用/类型/失效错误为`valid=false,resolved=null`及路径说明；非法请求封套400，权限失败403。仅读取当前用户可用的确切Asset及正式文件，复用原类型/摘要/媒体探测；不保存配置、上传、审核、采用、创建任务或调用模型 |

媒体策略版本1的全部字段均须明确：

| 字段 | 含义、取值与更新规则 |
| --- | --- |
| `schema_version` | 整数1，不隐式转换 |
| `media_kind` | `image/video/audio`；目标实际能力仍由既有执行适配器校验 |
| `allowed_sources` | 非空无重复来源key列表；`asset_library`、`asset_upload`、`selected_output`为已实现媒体读者。`bible_reference`显示不可用且不能配置为可用来源，等待M9/M5确切版本接入 |
| `default_source` | 必须在允许集合内；只指定方式，不选择默认图片。配置的选择必须属于此来源，不在失败后自动回退 |
| `allow_override` | 明确布尔值；允许时本次override整组替换配置选择，包括来源、素材、用途及顺序。禁止时不得提交override，即使内容相同；未提供与显式null不同 |
| `min_items/max_items` | 非负整数，下限不大于上限；0是有效数量，不截断或自动补齐；所属目标的原有数量约束仍需一起验证 |
| `allowed_purposes` | 使用处的用途key目录；非空时每项显式选择其中一项，空列表时每项purpose须为null；不推断为外观或动作 |
| `order` | `provided`：保持调用方明确列表顺序；不按名称排序、按Asset去重或猜测首尾帧 |

`selection/override`结构为`{source,items}`。每项提供唯一非空`item_key`、`purpose`，库选/上传提供`asset_id`，上游提供`decision_id/artifact_id`；不混入其他来源属性或临时路径。输出`resolved`含本次策略、source、overridden及有序items；每项有position、kind、确切Asset/Artifact ID、原读者origin及media_facts，不输出本地文件路径。同一素材可有不同用途/使用标识，单次核验复用读取结果而不合并使用项。失败不返回一份伪装成功的部分结果。

上传仍经`POST /api/v1/assets/`；`asset_upload`只接受具有原上传写入方事实的Asset，不将库内/模型生成素材冒充上传，也不声称本次发生新上传。独立预览没有ProductionRun授权上下文，所以`selected_output`返回“此入口未提供已授权运行和上游阶段”；实际服务必须显式提供原运行及上游阶段，不能从HTTP预览参数取得授权。固定值/上游结构字段目录分别指向已有M1-05结构映射器，不在媒体服务内复制文本解析。

预览沿用现有`security.workflow_smoke.asset_max_items/asset_max_file_bytes`服务端读取边界和执行配置JSON大小边界，超出时先报错，不追加配置门禁或静默截断；这些边界不是客户端policy可以扩大。组件仅编辑调用者草稿：原资源拥有配置版本；上传/库选两种模式分开，上传返回后若组件卸载或来源/类型改变，不回填新选择。M1-06纯结构样例继续禁止上传。隔离API/真实临时文件、前端组件及浏览器验证范围见[详细设计M1-07](optimization/modules/m1-fields/design.md#m1)，未做现用配置发布或真实媒体生成验收。

### 执行实例媒体来源策略（M1-08实例消费者）

正式入口：执行实例分区 → 草稿“输入与素材接线” → “配置媒体来源方式”。沿原版本创建/PATCH、配置读取、克隆、合同冻结和请求表单；没有新增执行动作或配置表。`ExecutionInputSpec.media_source_policy`可为空/不提供，表示不增加来源方式限制，不取消原输入/模型目标校验。

配置非空时严格为`{schema_version:1,allowed_sources:[...],default_source:...,allow_override:true|false,order:"provided"}`。类型、min_items/max_items、allowed_purposes只存原输入定义，执行时投影成上节共用策略，不另存可漂移副本。仅image/video/audio可配置，未知/停用来源、不合法默认和重复数量字段返回400且原子回滚；没有自动选择图片。0209仅新增可空列，不修改已有选择或快照，现用库未迁移。

配置了来源策略的请求输入为`inputs.<key>={selection:{source,items},override?:{source,items}}`；items沿原媒体使用协议包含`kind/item_key/purpose`及对应Asset或Decision/Artifact身份，原subject_ref/scope_ref/paired_to/order语义不变。未配置策略的输入继续使用通用`{items}`协议；它是无额外策略的明确模式，不是读旧字段兜底。selection使用默认方式；override须获本版本允许且整组替换，不合并、不解析被替换的selection素材。可选输入未提供仍省略；空列表仍检查数量。配对视频原音为原有专用关联方式，不属于本段独立素材来源目录，需保留无来源策略的配对输入。

来源选择先于I/O校验，随后复用原媒体读者、主体/范围/重复使用、端口及真实文件检查；上传来源额外核对原Asset上传写入事实。selected_output只接受调用服务传入的授权运行与上游阶段，客户端不能扩权。冻结媒体记录增加`source_selection={source,overridden}`并纳入原source_sha256；文本input_provenance仍只保存非媒体来源，不增加第二条媒体事实。配置/读取失败保留错误，不选择首项、最新图片或备用来源。

本段隔离验证覆盖实例API保存/克隆/移除/回滚、冻结选择的原媒体解析和暂存、选择产物授权、前端来源限制与数量同源；配方固定素材和既有参考清单的策略接线见下节。未发布现用配置、运行真实模型或验证完整配方链；声明式Profile参考消费者见下节，其他M1-08工作仍未完成。

### 配方本轮主图与变体差异来源（M4-04部分）

入口：系统设置 → 生产配方 → Bible参考图阶段 → 变体路线 → 输入来源。沿原管理员配方POST/PATCH/克隆及发布校验，没有新端点或准备按钮；混合变体调度按原主图登记依赖推进，保存成功不等于依赖、字段或调用准入已满足。代码为`production_recipe_material_inputs.py`及原配方作者/准备，页面复用原路线与递归结构编辑器。

来源合同：图片输入可配置`{kind:"material_primary",item_key:"main",purpose:null}`，用途存在时须从精确实例允许项中选择；图片数量、请求范围、来源改选权限仍由实例控制，必须映射实际主图片端口。不得填写Asset/版本ID。文字输入配置`{kind:"material_difference"}`；JSON输入仍使用原`structured`模板，只在明确对象字段/数组位置配置同一差异节点，服务器按该位置核对唯一已发布Profile直通文字字段或`reference_units`单元贡献字段。后者必须位于`ordered_units`中明确的单元对象内，`structured_input`后的路径须等于该字段已发布`source_path`，该单元的`unit_key`须为原`{kind:"unit_key"}`节点；作者阶段不填写实体或单元身份，原素材准备器绑定本轮确切单元后，复用原`reference_field`差异来源及字段请求链。不支持动态unit_map/each重复差异，不另填正文、路径或默认值。完整路线恰好一处主图和一处差异；不完整草稿可暂缺另一处，已有非法来源不静默删除。仅变体路线允许这些来源，主图/其他职责保存拒绝。参数/其他输入仍走原合同。

内部`prepare_recipe_media_unit(material_unit_key=...)`按同轮冻结需求确定设定与角色，只从同一主图依赖读者（生成选择/登记，或本轮明确的已有/上传选择）取得该变体确切主图版本，再把显式标记投影为原媒体选择及字面文字/结构节点，交给唯一输入解析与外观验证器。其他固定内容不改写；同一原详情附紧凑`recipe_material_binding`，生成来源记录决定/产物/准备/版本ID，复用来源仅记录fulfillment_id/version_id；差异仍记录确切设定ID/正文SHA，不复制正文或新增事实表。原目标、M3需求、实际图片字节/端口及差异消费再次核对。准备重放仅返回原冻结身份，不重选主图/重读当前差异；新候选仍核对当前来源。未选择/未登记或失效来源明确拒绝，不找最新库图替代。错误沿原字段400/幂等409及所有者权限返回，失败不留半套准备，无模型调用、素材写入或采用副作用。

验证边界：隔离实例/临时PNG测试覆盖确切主图及两个不同差异的原准备、幂等重放、缺来源和配置反例；绑定测试明确替换上游依赖读者，第179批的绑定测试不计混合整链；新增原混合阶段/队列/图片归档/版本测试证据另见详细设计第180批。正式编辑器隔离浏览器覆盖可选用途、错误/范围/只读/目录恢复及390px布局；未发布现用配置、迁库或真实模型验收。

混合阶段推进：原调度器先核对全部变体路线的绑定配置，再按本轮主图登记依赖准备单元，沿同一Preparation/Attempt/GenerationTask工作流队列及归档，不新增队列或HTTP提交动作。主图未选择或未登记不创建依赖变体准备；主图失败不发变体。逐单元候选均结束且至少一个成功、其他候选均为明确失败时，按该角色evaluator从有效成功候选选择并登记；取消、结果未知或归档不完整仍阻断，其他单元尚未完成时阶段仍为running；主图及两个变体的生成、选定、素材登记均不等于Bible审核采用。等待状态沿原reason_code/reason展示：waiting_material_registration为确切版本未就绪，waiting_material_dependency为主图依赖未就绪，waiting_material_selection为没有配置自动选择且尚无人明确选择；此时可在原媒体对比入口读取影响并人工选择，不增加虚假的自动人审。直接手填版本但无本轮绑定的准备继续禁止调用；已有冻结准备的主图选择发生变化时拒绝再次派发，不自动修改旧请求。原worker/历史版本读者保留实际使用主图，变体不会出现在实体主图审核/历史目录；可通过原运行产物和素材版本查看。全部明确失败后已有/上传成图接管已沿原履约接通，旧请求及费用保留；未冻结且主/辅助均无调用的受阻准备也可由明确人工选择原子停止并履约，原输入与受阻事实保留。冻结请求、持有执行领取、独立取消和结果未知不能按此路径替换；未知结果不重发。主图改选已接原公开确认：未调用项明确重新准备，已成功项保留原实际依据，确认不派发；辅助准备/调用安全恢复已按原状态接通；独立入口登记见前述共用writer，独立与生产执行沿原Attempt/Job保存实际失败回执并链接确切任务，未收到正文时不构造正文；M4整体交付及真实模型效果仍另列验收。

### 配方固定素材与来源策略（M1-08配方消费者）

入口：系统设置 → 生产配方 → 草稿步骤 → 执行实例 → 输入来源 → “固定素材（素材库／上传入库）”。使用原`POST /api/v1/production-recipes/`、`PATCH /api/v1/production-recipes/{id}/`、详情及`clone-successor`，沿原管理员权限；没有新执行API。代码为`production_recipe_media_selection.py`与原配方校验/准备服务；UI复用`MediaSourceSelectionEditor`及正式Asset选择/上传接口。

媒体输入来源保存为`input_sources.<key>={kind:"media_selection",selection:{source:"asset_library"|"asset_upload",items:[{item_key,purpose,asset_id}]}}`。来源策略由引用的精确实例定义，配方不再保存数量、用途或另一份允许来源。所选方式不是实例默认方式时必须由`allow_override=true`允许；准备时明确投影为原selection/override协议，整组选择、不混合。每项保留确切Asset和使用标识，顺序由数组确定，未声明用途为null；空列表仅在数量允许时成立，没有默认素材。

此固定选择用于每个制作单元的本次输入（request范围、无虚构主体/配对关系），仍会生成新产物，不表示跳过模型直接复用成片。逐镜主体或配对输入继续使用相应参考映射；通用Bible主图/变体版本选择仍待M9/M5。配方保存时不能嵌入另一轮的Decision/Artifact；动态上游走运行内的既有参考映射，不推断“最新产物”。

原`reviewed_references`在实例配置来源策略时，以策略明确的default_source封装已审核的有序项，保留原subject/scope/paired关系与审核身份；实际项来源不符合该方式时返回错误，不自动改选方式或过滤素材。没有来源策略时不附加该限制。视频配套原音仍使用原无来源策略的配对输入合同。

配方版本及RunPlan保留选择原文；来源展开只构造输入，不读取当前配方替换冻结选择。真正准备解析复用原素材读者核对所有者、存在性/活动状态、上传事实、文件摘要/实际媒体和目标槽位。错误为400及具体字段路径（无权限401/403、对象不存在404沿原接口）；不会新建素材、审核、候选或模型调用来补齐。上传是用户主动使用原Asset API的独立写入，随后选择其ID。

隔离验证分开覆盖原配方保存/读取/后继、RunPlan冻结、原逐镜来源展开及真实临时PNG读取、默认/改选、非法引用/顺序和来源错误；保存API测试的执行目标使用服务合同夹具，不能当作完整发布到视频执行的组合验收。页面验证使用拦截真实API的桌面/窄屏夹具；没有迁现用库、发布配置、真实上传或模型调用。声明式Profile参考消费者见下节，完整M1-08验收仍待接续。

### Profile参考素材消费者（M1-08）

Profile不另存媒体来源策略。配置仍在执行实例的“输入与素材接线”，配方选择固定素材或提供审核参考；声明式Profile通过原`projection.output`与参考原语使用本次已解析的素材。Profile维护渲染规则，不能按名称重选图、覆盖实例来源方式或把普通文字字段当成媒体端口。

`execution_reference_materials`将同一素材使用的Asset/Artifact身份、单元范围、用途、实际槽位和来源摘要送入原参考上下文；已配置来源策略时，逐项同时保留解析器给出的`source_selection={source,overridden}`。它只表示实际选择的来源方式及是否显式改选，不复制允许来源/数量/用途配置，不表示本次新上传，也不表示人工审核。未配置来源策略的使用不补造该摘要。配对音轨的选择保存在对应`audio_use`，不能混作父视频来源。

准备保存、字段请求及提示词编译沿同一上下文，工作流文件槽位仍由同一已解析输入分配；没有额外素材读取、队列或自动回退。配置不完整、无效素材或来源不符沿原入口显式报错。来源摘要不作为最终视频提示词的渲染片段；辅助任务的原冻结参考事实保留该摘要，不改变现有字段请求合同。视频动态文案及素材引用名的进一步简化归M5/M6。正式页面/API入口不新增，准备详情的原JSON事实承载该摘要；完整提示词展示的既有HTTP字段形状不变。

### 提示词策略与当前字段入口

素材库、Smoke 和生产准备共用精确执行实例的输入合同。图片策略只选用参考主体定义／保留声明，且这些模板不引用 `{shots}`、没有单元原文映射或逐单元生成依赖时，不声明 `reference_context.units`；表单只展示实际参考素材及其他已声明来源。准备和编译只消费选中的原语输出，无须提供虚构时长、对白、声景或音乐。视频、时间线原语、引用单元的模板及逐单元生成继续要求明确单元并校验时长／帧数；未消费的审核贡献仍拒绝丢弃。旧实例的冻结合同与历史任务不改写，输入变化通过发布后继实例版本生效。

声明式参考渲染配置沿原策略创建/PATCH/发布入口校验模板、分隔符和用途规则正文，复用公共字符校验器拒绝NUL等控制字符及Unicode格式字符，保留正常换行/回车/制表符。无效写入返回400，原配置与合同SHA保持不变；不清洗原值、不自动修复已冻结历史。

`source_selectors`、规则创建/修改及OpenAPI枚举不再包含已退役的七个Shot固定创作列来源。新发布、输入派生和编译共用当前来源校验；旧规则不转换到请求字段、不自动补结构，返回400并要求明确选择确切结构路径。字段叶子Smoke的来源预览、新建及复用准备新建同样校验实际选中规则的最终来源和生成来源；只读原冻结事实和同幂等键取回原任务不重新执行、不改写旧正文。0233仅调整字段choices，不删除或改写保存的规则、请求、人工正文及审核。只读历史记录不构成再次执行授权。自定义结构内命名为action/camera等的真实字段和原语内部贡献接收位置仍有效，不等于旧Shot来源。

参考单元通过 `structured_input` 提供策略确切结构的原值；渲染位置通过 `rendering.unit_text_bindings` 显式选择文本路径，空对象表示不使用原文位置。位置目录由原Profile options的 `reference_rendering_text_slots` 提供，不是业务字段或Shot列。公共单元不接受旧 `source_facts` 或内部 `render_values`。解析只使用结构登记的缺键默认，原值及确切版本独立冻结，仍缺失或非文本报错。编译重放原输入，不回查当前业务；已选生成贡献独立进入原接收项，不改原文或伪造人审。旧不完整合同须显式配置后继草稿，不转换历史。

字段身份及确切定义版本在字段维护中心管理；提示词页选择字段版本并维护策略规则，不再维护字典CRUD，不提供新旧路由别名。已发布策略通过执行实例的 `prompt_profile_version` 明确引用；不再提供独立运行时绑定管理、族级选择或优先级选取接口。关联摘要直接列出引用该策略的执行实例版本，同族但没有外键引用不构成关联。

实际提示词预览和请求冻结仍走原执行准备/字段选择API：原准备器核对确切输入与选择摘要，事实编译器只消费该次来源、最终值或逐单元贡献及已有选择证据，不读取当前项目/Shot替换原输入，也不生成另一份审核回执。未选择必需生成字段、损坏选择或错误单元继续由原入口明确拒绝；自动选择与人工确认按原策略记录，不因编译变成人审。内部旧项目媒体编译包装及其审核行/指纹协议已退役，不新增HTTP入口、Schema或旧请求兼容。

#### 结构化输入与字段路径（M1-08已接入段）

通用辅助结果由策略规则自身作为接收目标，不要求为每个字段登记后台槽位。`generation_output_semantics=final` 在 direct_v1 和 declarative_v1 均按本次准备的 unit_key 生成一次，采用后走同一字段类型、顺序、标签、分隔符与通道装配。可用于剧本创作/结构化、视觉规则、图片及视频等策略输入；执行实例须显式配置所需辅助实例。字段辅助本身仍是叶子，system_instruction 不接受模型结果，人工完整提示词、素材接线及运行参数不由此改写。

声明式规则可用 `generation_source_selector=projection.output` 将该字段的确定性装配原文作为辅助输入；页面称“本字段的确定性装配原文”。原文由冻结单元和实际素材接线计算，不能由请求注入；生成结果直接填本字段，不再次拼接原文。也可选择明确的主要文本来源和独立结构化附加字段。多个final字段并行处理原始事实，不读取另一字段未采用的结果；需要逐镜方向插入模板时保留contribution，它仍按明确单元和原语接收位置处理。缺失必需结果、错误单元、重复接收或未声明标签明确报错，不用原文静默替代。

复用下列Profile/规则CRUD与preview，不新增执行队列或接口别名。页面入口：系统设置 → 提示词策略 → 编辑策略 → “结构化输入”，选择确切已发布结构；编辑规则 → 最终提示词取值选择“本次结构化输入字段” → “装配字段路径”。沿原staff登录权限（无权限401/403、目标不存在404）；代码入口为`platform_serializers`、`prompt_profile_structures`、`prompt_profiles`及`execution_profile_inputs`。

| 所属请求/响应 | 字段 | 含义与空值语义 |
| --- | --- | --- |
| Profile创建/更新/详情、完整preview candidate | `input_structure_version` | 可选UUID/null，引用确切已发布DataStructureVersion；null表示不使用结构化来源，不自动选最新版。已有路径规则时不允许清除根结构或换成不兼容结构，返回400字段错误并回滚。 |
| 规则创建/更新/详情、candidate.field_rules | `source_selector` / `source_path` | 新通用来源`request.structured_input`使用明确路径数组，如`["动态.输入","动作"]`或`["列表",0,"动作"]`；`[]`表示整个根值，null表示未选。装配与生成均未选择结构化来源时source_path必须为null。属性名中的点号不拆分，不执行表达式或隐式遍历。 |
| preview.sources、本次逻辑输入 | `request.structured_input` | 一份完整JSON根值，多个规则读取同一根的不同路径；不把单字段示例当根输入、不接收外部注入的projection.output代替真实结构。实际请求沿原逻辑输入来源/准备API提交。 |

最小使用示例：Profile选择一个声明`{"动态.输入":{"动作":"text"}}`语义的确切结构（此例只是字段布局说明，不是结构创建体），文字规则配置`{"source_selector":"request.structured_input","source_path":["动态.输入","动作"],"generation_mode":"passthrough","generation_source_selector":"request.structured_input"}`，预览sources提供`{"request.structured_input":{"动态.输入":{"动作":"转身，镜头推进"}}}`。其余必填规则字段仍按下表提交。

该来源支持原值装配（passthrough/server_locked）；direct_v1 的文本路径也可配置 model_generated 或 translate_only，输出语义须为 final，generation_source_selector 仍为 request.structured_input，沿用同一 source_path，不另选固定分镜列或猜字段名。辅助请求只带该路径的文本、显式 generation_context_selectors 及本字段来源依据，不自动夹带整份根值或其他规则路径；复用原字段请求、候选与采用流程，不因准备成功就调用模型或自动人审。未采用结果不能用原文或规则默认值代替：生成必需/最终必填字段显式阻止编译，非必需字段按原空值规则处理。declarative_v1的逐单元贡献使用source_selector=projection.output、generation_output_semantics=contribution、generation_source_selector=request.structured_input及同一source_path；每个ordered_units条目明确提供structured_input，按Profile的确切input_structure_version解析。生成来源与接收项分开：projection_primitive决定渲染目标，不按自定义字段名猜目标，也不把原值复制到固定Shot列。准备冻结完整原输入，辅助请求只收到本单元所选文本和明确上下文；路径缺失/null/非文本或漏单元返回400且不新增准备。原采用/选择接口与权限不变，未采用贡献不回退原文。

不能同时绑定文字ContentSourceVersion。路径类型须匹配所选字段版本，number可读取integer，不隐式将对象转文字；文本列表支持可空列表声明，但实际null仍受规则必填/省略处理。原值装配时，结构缺键默认先由共用解析器应用，之后仅路径末级对象属性缺失可使用规则默认；空值、false、0不被默认替换，缺父级/列表越界/类型错误直接指出路径。辅助生成只接受明确文本（字段类型text/long_text/enum且路径为string），可使用结构已声明的缺键默认及原默认来源证据，但缺失/null/非文本不能改用规则默认；仅翻译还要求非空正文及明确目标语言。无效组合沿原创建/更新API返回400且不修改原合同；后继版本不改已发布合同。开发验证覆盖原API修改/回滚、冻结合同、保存准备→人工候选确认/采用→原编译器及只读恢复；没有真实模型调用、现用部署或生产采用验收。

合同schema8有结构时冻结`input_structure`快照，新来源规则冻结`source_path`；新版本和后继不改旧摘要。执行实例自动派生一个JSON根输入并绑定该结构，禁止在实例另配第二份根结构；准备记录根输入、默认来源及逐规则路径，编译仍调用同一个装配器。预览返回原`channels/structured_fields/trace`，trace含路径和结构默认解析来源，不存配置、不写任务、不调用模型。无效结构/路径/类型返回400；candidate基础SHA漂移409；读取失败保留配置并提供重试，不换其他结构或示例。

验证：本段68项后端关联测试及14项前端测试通过，隔离页面验证规则/策略编辑、根值/路径和窄屏；0208只在隔离测试建库中应用，现用迁移、配置发布、真实生成及M1整体独立验收未执行。详见唯一详细设计M1-08第六段。

`/settings?section=prompts&prompt_profile_version={id}` 定位精确策略；不存在或读取失败时明确报错，不选择同模型族候选。发布不自动更新执行实例或现有准备。退役仍经原 staff API、注册关系锁和审计，存在启用且已发布的执行实例引用时返回400；草稿、退役版本或停用实例不会阻止策略退役，但原引用不会被改写，新发布/调用仍需通过原精确合同准入。此处描述当前代码合同；初始隔离验证及后续切换见统一计划第25节，当前版本的实际通过证据仍须独立核对。

- `/api/v1/field-definitions/`
- `GET /api/v1/field-definitions/options/`
- `/api/v1/prompt-profile-versions/`
- `/api/v1/prompt-profile-field-rules/`
- `GET /api/v1/prompt-profile-versions/options/`
- `POST /api/v1/prompt-profile-versions/{id}/clone-successor/`
- `POST /api/v1/prompt-profile-versions/{id}/preview/`
- `POST /api/v1/prompt-profile-versions/{id}/publish/`
- `POST /api/v1/prompt-profile-versions/{id}/retire/`

这三个管理目录与 ProductionRecipe 共用[填写表单最小输入标准](form-input-standard.md)。Recipe/Profile的首版号和后继版本号、Recipe结构版本、字段展示排序都由服务端分配。字段身份`options`只返回`system_defaults`；定义类型由版本`options`提供。其他目录按各自合同返回受控目录、系统默认及`custom_entry_fields`，不可把缺失目录伪装为空选项。两个`clone-successor` action均要求确切来源SHA-256，来源过期、已有草稿或正文与摘要不一致返回409且零写入。

Recipe 的 `clone-successor` 只授予复制草稿的权限：在原注册引用写锁和版本族锁内，验证来源正文摘要、阶段/路线结构、主图及变体路线的冻结执行定义（含辅助引用）、独立分镜字段与装配/交付输出的确切引用。不存在、摘要错误或作者配置被改写的引用返回400且零写入。不重新构建当前执行接口，不要求旧引用仍启用或烟测有效；因此可先复制旧配方，再明确更换新版实例。新草稿逐字保留来源定义及摘要，不继承发布/验收状态；普通创建、PATCH/PUT保存、发布与执行仍走原有当前合同和准入校验，未修正的旧引用不能借复制直接发布或运行。不创建ProductionRun、准备或模型调用。

字段身份接口（staff登录；写入仅`FieldDefinition`）：`GET /field-definitions/`按key搜索、按`is_active`筛选，平台分页，按`display_order/key`排序；`POST`只填写`key`，初始启用和追加排序由服务端确定；`GET /{id}/`返回`id/key/is_active/display_order/created_at/updated_at`；`PATCH/PUT /{id}/`只能改变启停，key不能更换。名称、含义、类型、单位和枚举只能在上方确切版本API维护，身份请求携带这些旧字段返回400字段错误。无权限401/403，不存在404；失败不改版本或策略。删除仍走现有注册对象删除预览/执行入口，预览包含从属定义版本及规则关系，不隐式删除无关素材。页面入口为`/settings?section=fields`，可带`field_definition={id}`打开指定字段版本；按页查询、管理身份/定义版本、查看类型样例与引用影响。引用影响复用`registry-relations`的`field`摘要，沿定义版本、结构、来源/输出、策略、执行实例与配方逐层查询；不存在或不完整回执显式报错，不回退为0。可加`field_version={id}`定位确切定义版本，或用`structure_version={id}`、`source_version={id}`打开原管理器，含分页外/停用版本；错误身份不替换为最新。删除仍单独使用原删除预览及确认，不把查看引用当作删除授权。Profile不再管理字典或下发字段类型/分类/身份默认，类型目录唯一来自定义版本options。内容来源选择只读，管理回到字段维护。具体隔离验证与未完成范围见详细设计M1-09。

PromptProfile 是唯一的类型化提示词合同，不接受可执行模板。每条字段规则同时回答两个业务问题：“字段内容如何获得”和“最终提示词如何排版”。最终排版由 `source_selector`、可选 `projection_primitive`、`channel`、`position`、`label_style`、`separator`、`required`、`omit_if_empty` 与 `max_chars` 决定；字段内容规则使用下列通用字段：

草稿策略可在正式作者页明确切换 `direct_v1` / `declarative_v1`，并保存新投影配置；已有字段规则保留原 ID、确切字段版本、来源及原值，不自动删除或转换。草稿保存不是发布或可执行结论：声明式至少需要一条登记原语的 `projection.output` 规则，直接策略不得消费投影原语或贡献；不兼容组合在发布及执行时沿原完整合同校验拒绝。已发布/退役策略仍不可原地修改，只能创建确切后继草稿。切换不创建准备、调用或审核记录。

`GET /api/v1/prompt-profile-versions/options/` 的 `effective_limits` 返回字段文本、validators 和 context selectors 的当前安全上限。字段规则 API 自身不再复制发布时的旧静态 maximum；写入与运行时统一由相同 active security boundary 校验，因此管理员调高后不会继续被旧 OpenAPI 常量拦截，调低后立即生效。

| API 字段 | 类型与范围 | 含义 |
| --- | --- | --- |
| `fixed_value` | 类型与所选字段一致的 JSON 值 | 来源选择 `fixed.value` 时，使用当前规则的固定值；不引用字典默认值，不生成外部输入端口。 |
| `has_default` / `default_value` | boolean / 类型与所选字段一致的 JSON 值 | 非固定来源可显式启用本规则的缺失默认值；仅来源键不存在时应用。显式 null、空串、空列表、0、false 都不触发默认替换。未启用时 default_value 必须为 null。 |
| `required` | boolean | 只约束本规则最终装配，不与字段字典进行全局 OR。当前字典 API 已移除 default_value、is_required，旧写入返回字段错误。 |
| `generation_mode` | `server_locked`、`model_generated`、`translate_only`、`passthrough` | 谁负责取得或生成字段。新建规则必须显式提交，不能靠缺省猜测。使用策略的业务 operation 共用字段辅助链路，不限图片/视频；options 的 `generation_leaf_operations` 列出不能递归声明辅助生成的叶子操作。系统约束仍只能使用维护者明确配置的固定文本。 |
| `generation_source_selector` | 模型生成／仅翻译：选 `generation_source_selectors[].value`，或 `request.structured_input` 加确切结构与 `source_path`；锁定／透传：等于 `source_selector` | 生成目标字段所需的原值输入，不是文件或数据库路径。普通文字选项复用已实现执行读者；逐单元贡献仅从本单元结构路径读取，不把渲染接收位置当作生成来源。 |
| `generation_context_selectors` | 去重字符串数组；上限来自 `effective_limits.max_context_selectors`；允许空数组 | 选择受控上下文，或同一绑定输入结构中的字段路径。空数组只使用主要内容来源；相邻镜头和全剧范围必须显式选择。 |
| `source_selector` | 受控来源枚举 | `direct_v1` 读取通用事实；`declarative_v1` 的原语输出规则固定使用 `projection.output`。历史 H3 专属 selector 只用于读取冻结旧合同。 |
| `projection_primitive` | 空字符串或 `source_projection_primitives[].value` | 仅 `source_selector=projection.output` 时必填；声明代码中已注册的确定性原语。其他 source selector 必须为空。原语结果写入本规则所选 `field_definition.key`，因此业务字段映射不写死在代码中。 |
| `generation_required` | boolean | 字段内容是否必须生成成功；与最终拼装的 `required` 分开记录。 |
| `language_policy` | `inherit`、`preserve_source`、`any`、BCP 47 标签或 `bilingual:<tag>+<tag>` | 创作正文的输出语言约束；冻结 Bible/素材参考原名、输入槽位及原剧本对白/画外音原句始终原样透传，不翻译，不受 en 等语言设置影响。`translate_only` 不能使用 `preserve_source` 或 `any`。 |
| `length_unit` | `none`、`characters`、`english_words`、`items` | 长度计量方式。`none` 要求上下限都为空；其他单位至少设置一个边界。 |
| `generation_min` / `generation_max` | null 或 1–30000 整数 | 生成长度边界；同时存在时 min 不得大于 max。 |
| `generation_validator_keys` | 最多 16 项的受控枚举数组 | `non_empty`、`no_control_characters`、`valid_json`、`json_object`、`json_array`用于通用结构检查；其余（标题、单行、占位符、去重、原文引用次数）作为生成指导随策略发送，不检查正文是否遵循。`generation_min/max`始终只作长度提醒；未知、重复或矛盾的配置仍拒绝。 |

Profile 级 `channel_limits` 是最终提示词通道合同，键只能使用受控 channel，值包含必填 `max_chars`（1–30000）与可选 `warn_at_chars`（1–`max_chars`）。未配置的通道表示该 Profile 不限制最终长度，不存在代码内隐藏默认值。装配超过 `max_chars` 以 `prompt_profile_channel_too_long` 阻断；达到预警阈值仍返回结果，并在 preview 的 `warnings` 与 `channel_audit` 中显式标记。

`GET /api/v1/prompt-profile-versions/options/` 返回已有 operation、模型族、模式、装配来源、通道、标签和分隔符建议，并返回 `generation_modes`、`generation_source_selectors`、`language_policies`、`length_units`、`generation_validators` 与 `generation_context_selectors`。

其中 `source_selectors[]` 的每项包含 `value`、`label`、`group_label`、`description`、`record_location`（事实维护／输入页面位置）及 `execution_usage`（如何通过显式接线进入请求），均为非空字符串。这是当前平台只读使用说明，不包含业务正文，不是新解析器，不写入策略或执行实例的冻结合同。沿用该 options 接口的认证和权限；GET 无业务副作用，不新增分页或写入参数。前端说明缺失时显式报错，不按同名字段推断、补造来源或回退旧目录。

通用字段字典定义内容含义与类型；策略规则通过 `field_definition` 外键和 `source_selector` 显式连接两者，不存在一对一或同名自动绑定。字典的“查看来源关系”读取实际策略规则和精确版本，并显示生命周期；“查看内容来源目录”、规则详情及执行实例“查看填写位置与字段对应”共用上述说明。草稿读取 authoring options 的 `profile_inputs`；已发布／退役实例读取精确 `input-contract` 的 `definition.interface.profile_inputs[来源标识].fields` 和 `input_forms`，核对版本 ID、修订及 frozen 状态，区分装配、生成来源、生成上下文与参考单元用途，不拿当前资源重新投影。`request.theme` 的项目梗概引用须先预览确认复制，可人工编辑；直接选择 `project.logline` 或 `style.visual_prompt` 的新实例草稿则派生受控项目输入，按本节“项目已保存事实的受控引用”选择项目、预览确认并冻结，不接受手工冒充项目事实。其他 `project.*` 标识不会因此获得读取能力。另一个任务的输出须由正式输入合同支持并明确选择记录与修订，不能将任务 ID 当作来源标识。生成／翻译规则另显示辅助结果路径，其主要事实与上下文仍由 `generation_source_selector` / `generation_context_selectors` 声明，不能与装配来源混淆。

`generation_source_selectors` 复用内容来源目录的已实现执行读者，不再维护 38 项固定 Bible／Shot 列清单。当前包含本次请求文字、受控项目梗概／视觉风格及本轮已审核影视规则输入；范围、预览确认和修订要求沿原读取协议，不能凭名称自动查询业务列。各项保留 `value`、`label`、`scope_type`、`group`／`group_label`、`description`、`availability`、`scope`、状态及 `generation_modes`，说明复用同一来源帮助目录。`request.structured_input` 由结构路径控件单独选择并校验确切输入结构；声明式贡献必须选本单元 `structured_input` 中的文本路径，其接收位置由原语决定。未登记的旧列名明确拒绝，不作为 active option 或兼容别名。译文维护的可编辑字段范围独立于生成来源目录，不因此获得执行读取权限。

每个 `generation_context_selectors` 预设候选包含 `value`、中英双语 `label` 和 `description`；这是预设上下文的唯一目录。页面不得维护第二套枚举或在目录缺失时静默回退。预设值为 `project.core`、`project.style`、`target_shot.core`、`target_shot.dialogue_metadata`、`target_shot.dialogue_authored_text`、`target_shot.linked_bible`、`adjacent_shots.core`、`all_shots.core`。`target_shot.dialogue_metadata` 只提供当前镜头说话人、语气、起止时间和画外音标记，不含对白正文；只有显式选择 `target_shot.dialogue_authored_text` 才向字段生成模型提供已经确认的当前镜头对白原文。`adjacent_shots.core` 与 `all_shots.core` 都必须逐规则显式开启，且其核心事实仍不包含其他镜头的对白正文。

辅助生成／翻译还可从策略绑定的确切输入结构多选附加字段。页面入口：编辑字段规则 → 内容生成 → 来自本次输入结构的附加字段（多选）。API复用字符串数组，使用 `request.structured_input:` 加紧凑JSON路径，例如 `request.structured_input:["identity"]`；它是既有属性／下标路径，不是表达式。主要文本来源可独立选择已实现读者或本字段确定性装配原文，附加路径须存在于策略绑定的结构；未知预设、非法／重复路径或不兼容生成方式返回400字段错误，保存失败不改原规则。主来源仍须文本；附加值保留其结构类型，包括显式null、false和0，缺失所选路径则报错，不回退其他字段。final从本次请求的结构根取值，逐单元contribution只读取本单元structured_input，两者不隐含混用。实例只派生一个确切结构根输入，不另派生可覆盖每条路径的端口；准备将所选值冻结在原field_requests.context_facts中，未选择字段不送入辅助请求。数量上限沿用可配置的 `security.contract_complexity.prompt_profile.max_context_selectors`。无需新增结构迁移、执行队列或候选协议；新规则必须发布为后继，不改历史准备。

同一 options 响应的 `source_projection_keys` 是当前可写目录，只包含 `direct_v1` 与 `declarative_v1`。`source_projection_primitives` 是代码原语目录，每项返回 `value`、双语 `label`、`description`、`engine_key`、可消费的 `contribution_source_selectors` 和只读的 `rendering_template_keys`（可能使用的文案模板键）；页面据此显示可选项、字段模板对应关系并校验 contribution，不维护模型专属副本。模板对应目录不是执行轨迹，也不作为版本配置或模板默认值写入合同；实际模板选择仍由本次素材、音频与贡献条件决定。当前 `reference_context_ir_v1` 引擎注册六个原语：`reference_context.subject_definitions`、`reference_context.summary`、`reference_context.retention_analysis`、`reference_context.detailed_description`、`reference_context.overall_soundscape`、`reference_context.non_diegetic_music`。新增模型可以按自己的字段定义、数量、顺序、标签和通道复用其中任意原语；只有平台还不能执行的新确定性算法才需要增加代码原语。

策略详情的“内容生成与模板配置”读取所选版本已保存的 `source_projection_config`，展示模板标识、分隔符、列表前缀，并按需展开模板正文和其他原语配置。字段规则的“查看相关模板”通过上述目录定位同一只读配置弹框；未登记原语、未保存模板、留空和格式异常分别显示，不从新版本或教学示例补值。历史版本未保存显式模板时只展示其原始规则，不还原当时的内置文案。查看不会生成草稿、写入配置、调用模型或改变发布/绑定状态。

`operations` 是便于录入的建议目录，不是创建或执行授权：Profile草稿可登记符合 `^[a-z][a-z0-9_.-]{1,99}$` 的新用途key；发布须有受控operation目录、同模型族已发布模型版本的精确能力，或注册Provider driver能力作为依据，否则以 `prompt_profile_operation_evidence_missing` 拒绝。Provider可编辑的`modalities/operations`只是运营说明，不参与执行授权或加载代码。策略由精确执行实例引用，执行能力与提示词用途分别校验，不恢复族级Binding。Profile发布不能替代实际资源、组合证据或本次请求准入。

语言选项是常用建议，API 仍接受符合上述安全语法的其他 BCP 47 标签。创建字段规则时 `generation_mode`、`generation_source_selector`、`generation_context_selectors`、`generation_required`、`language_policy`、`length_unit` 和 `generation_validator_keys` 七个合同成员必须显式提交；`projection.output` 规则还必须提交非空 `projection_primitive`。对于 model-generated/translate-only，服务端还会核对 generation source 是否属于同一执行读者目录，或是否为通过确切结构／路径校验的 request.structured_input；对于 server-locked/passthrough，则核对其与 `source_selector` 相等。context 与 validator 数组都可以为空，PATCH 可只提交需要修改的字段，但服务端会把旧值与新值合并后验证完整组合。

Profile 的新建、编辑、发布及运行读取统一使用合同 schema v8：逐字段 `generation_context_selectors`、`projection_primitive`、字段内容规则、来源投影、最终提示词排版、Profile 级 `channel_limits` 与版本化 `source_projection_config` 全部进入规范 JSON、大小和 SHA-256，修改任何一项都会改变合同摘要。v8新增确切`field_definition`定义快照，规则API/候选预览必须指定同一身份的`field_definition_version`；原v7已新增每条规则的 `fixed_value/has_default/default_value`，删除全局 `field_default_value/field_required`；字典不保存业务值或全局必填。旧 schema v1–v7 拒绝读取和执行，不保留历史兼容编码器，不自动升级或重算已发布摘要。0201迁移新增规则版本FK和schema8默认值；旧规则版本引用保留null，不自动选择最新版。0200/0201迁移只调整Schema和新策略默认版本，不将旧冻结合同冒充为新合同；落到现用库及不兼容记录清理须按已批准详细设计R06另核具体操作范围，本轮仅隔离验证。

`declarative_v1` 至少需要一条 `source_selector=projection.output` 字段规则，当前执行要求 `source_projection_config.schema_version=5`。`primitive_options` 的 engine key 必须与字段规则实际使用的原语引擎精确一致；`reference_context_ir_v1.rendering` 明确保存 `templates`、`separator`、`list_prefix`、`prompt_template_key`、`reference_rules`、`task_separator` 、`unreferenced_task` 和 `unit_text_bindings`。模板字段和允许的占位符从 PromptProfile options 的 `reference_rendering_fields` 读取，编辑器提供文本框和占位符按钮；只支持简单的 `{字段名}` 插值，禁止表达式、属性访问、索引、格式指令、函数和脚本。参考声明、逐镜描述、对白、风格开场、空素材说明及音频关闭说明都来自所选版本配置，不读取 H3 内置文案。素材标签来自实际绑定，模式及数量来自冻结模型合同，视频帧数来自冻结 Workflow frame policy；缺少合同会拒绝编译，不按模型名补默认值。已审核逐镜贡献使用 `reviewed_shot` 模板，贡献或对白缺少对应占位符时拒绝，不静默丢弃。可选 `quality_advisories` 只产生建议，不放宽结构和身份校验。`direct_v1` 不接受 `projection.output`、原语或 contribution。

`reference_rules` 是有界列表，每项精确包含文本 `category`、`purpose`、`task`、`relationship`、`usage`；类别与用途组合不可重复，空用途只匹配未声明用途，并非通配。任务名称、保留方式与使用说明由此配置决定；未知组合拒绝执行，不根据模型名推测。普通素材及配套音频都进入声明、保留关系、用途摘要与相应镜头；配套音频使用自己的用途/说明和父视频镜头范围，不继承视频主体。`task_types` 按实际用途规则稳定去重并由 `task_separator` 连接；无素材时采用显式 `unreferenced_task`。`reference_usage` 模板将实际标签和使用说明同时提供给摘要与逐镜模板，审核后的模型段落不会取代这些确定性关系。首镜头使用 `shot_opening`，后续镜头使用 `shot_transition`；`timecode` 是此前镜头有效时长累计的 `MM:SS.mmm`，具体标记文案仍随版本冻结。配置表单编辑后需保存草稿、复核与发布，不原地修改已发布内容。当前闭合 schema 由来源配置校验器负责结构限制，文本叶子继续接受规范化安全校验；不为该配置放宽任意上下文的嵌套限制。

对白原值可显式提供 `speaker_subject_key: [主体类型, 确切主体标识]`（两个非空字符串）；省略表示没有声明参考主体关联，不能用姓名推断。生产来源 `shot.dialogue_with_speakers` 从已审核的逐行 speaker 关系提供该值，配方结构映射需明确传入，素材/Smoke使用同一个单元合同。对白与画外音模板可配置 `{speaker_reference_label}`：复用 `reference_layout` 的唯一主体标签映射，并核对当前单元的实际参考范围；缺失布局、确切身份或本单元参考时拒绝编译。同请求的稳定说话人和主体身份冲突也拒绝。独立旁白可以省略关联并选择不需要主体标签的声线模板，不伪造人物或素材。`{speaker_index}` 按本次有序单元及单元内 `start_ms` 的首次发言顺序从1编号（同刻依列表顺序、同一稳定说话人复用），不改写项目 `speaker_id`。`{start_timecode}` / `{end_timecode}` 为前序单元时长加对白相对毫秒的 `MM:SS.mmm`；`start_ms/end_ms` 仍是单元内时间。标记文字、语言标签、声音及模型格式全部由策略配置，后端不写模型专用对白句式。新增占位符及来源说明由原 options 目录返回，无新API或数据库迁移；已冻结请求不补算或重写。

提示词的确定性组装使用精确Profile合同和本次ExecutionPreparation内明确确认、采用的字段；生产字段生成/翻译按实例配置调用`prompt.fields.generate/translate`叶子实例，不再调用独立生产`prompt.normalize`阶段或回填旧目标字段覆盖记录。人工最终通道路径不强制AI改写。Bible/素材参考名称、原始描述及确切槽位关系由冻结绑定原样插入，不因输出语言而翻译、拒绝或改名；原剧本对白及画外音的原句同样按冻结`dialogue.text`来源保留，不要求为英文动作描述翻译中文对白。辅助结果、人工字段与最终编译共用通用结构检查：必填、类型、声明的JSON结构、安全字符及真实输入接线。语言、标题、单行、正文引用标签写法/次数和重复内容属于策略生成指导，不作内容硬门禁；生成长度范围始终只作提醒。字段/接口的显式硬字符容量与建议长度不同。没有模型专属正则，也不维护剥离对白后的语言视图；正文不自动改写，不回写旧准备、候选或已冻结请求。生产媒体与无配方沿各自正式根适配器进入统一准备；不兼容历史按R7精确清理，不作为新请求的数据来源。

`reference_rendering_fields` 同时返回只读教学元数据：`description`（套用场景）、`assembly`（原语与字段去向）、`requirement`（必填/条件必需/留空含义）、`placeholder_help`（与允许占位符同键的中文名称、来源说明及虚构 `example` 值），以及 `example.template/result`。示例结果由正式单次标量替换函数生成，仅用于说明语法；不会写入 Profile、合同 SHA、生产默认值或任务，不代表当前用户模板的预览，也不代表验收通过。编辑器显示中文插入按钮，支持光标处插入；说明与示例按需展开。缺少帮助目录时明确提示更新服务并重新读取，不回退到前端内置目录。实际值为空时不自动隐藏周围的文字；双花括号用于输出字面花括号，替换值不递归展开。字段排版与通道仍通过已有最终提示词预览检查；该预览使用示例字段值，真实参考模板替换需结合实际项目和镜头验收。

可选的 `rendering.reference_layout` 明确包含 `subject_label_template`、`source_separator`、`summary_reference_template`、`shot_reference_template` 四项。主体模板仅接受 `<名称 {index}>`，按本次确切主体身份和引用顺序分配；`label` 为逻辑标签，`source_labels` 为合并的实际端口标签，`subject` 仍是原始名称。声明须保留真实来源，不改动工作流素材槽位。同名不同身份不合并；同一身份的矛盾用途拒绝编译。摘要/逐镜模板分别覆盖共享的 `reference_usage` 文案，允许显式留空；不从模型名生成默认配置。未配置此可选布局的版本保持自身既有模板语义。映射进入逐单元辅助事实和真实素材接线核对，辅助任务只获得本单元来源；不从自由正文的标签推断或强制检查素材。`no_shot_headers`等为模型生成指导，不拒绝或静默删除新策略的镜头标题。

提示词规范化合同校验、冻结批次规划以及单次生产创建/推进/审批在操作内复用现有安全边界解析快照，避免对每个嵌套文本重复读取整份配置。该快照不跨操作或外部执行任务保留；下一次校验重新解析当前配置，解析失败不会回退到先前结果，文本/深度/字段数量等校验仍逐项执行。

声明式来源配置要求当前 schema 5 的用途规则及渲染模板；不兼容旧配置按R7精确清理，不调用旧编译器执行。合法的 `direct_v1` 静态配置不受此声明式限制。后继草稿须在策略页完成文案与字段规则复核，再由新执行实例版本显式引用并单独验收；不修改已发布内容、转移烟测证据或以旧策略作为执行回退。

图片候选缩放的安全边界由统一 ConfigDefinition 管理：`security.workflow.image_resolution_min_dimension`、`max_dimension`、`max_pixels`、`max_candidates`（后面三项同一前缀）。在安全边界页面配置，options 返回实际生效限制，提交与执行使用同一校验。初始化值分别为 64、8192、16777216、32；不是运行期缺配置时的默认值。16 像素对齐、原生 Kontext 候选尺寸和 Lanczos/居中裁切属于已注册适配器的协议语义，不能通过提高安全边界改变。具体目标尺寸仍在 Projector 配置并冻结到工作流版本。

`classified_reference_context_ir_v1` 及 `request.h3_ref2va.*` selector 仅供已冻结的旧 Profile、ProductionRun 和审计事实读取，不再执行旧编译，但不出现在当前可写 projector 目录中，也不能用于新建、编辑后发布或克隆后继版本。新 H3 使用 `declarative_v1 + projection.output + projection_primitive`；其他需要同类确定性转换的模型也可按自己的字段规则复用这些原语。这样引擎原语在代码中受审，业务字段组合在页面和版本化数据库合同中维护。

`preview` 使用与生产相同的确定性装配器，返回 `channels` 中的实际最终提示词文本；页面不得用 JSON 或浏览器端第二套拼装逻辑代替。编辑草稿时可提交 `expected_base_contract_sha256` 与完整 `candidate`：候选包含 Profile 的模型族、operation、mode、来源投影设置、`input_structure_version`、通道限制及完整 `field_rules`（含`source_path`）快照。服务端在内存中校验并计算候选合同 SHA-256，不保存候选、示例输入或合同字段；基础合同已变化时返回 HTTP 409。未提交候选时按已保存合同预览，且同样不会刷新或写入数据库。

`FieldDefinitionVersion.preview_example`是所选定义版本详情的只读预览示例，完全按类型生成，不按业务key硬编码；对象/媒体尚未配置结构时返回null，不猜素材ID或嵌套值。它不读取业务默认、不进入Profile合同或SHA、不用于ProductionRun。页面仅将其作为普通来源的本次预览sources初值，结构化根值不从单字段示例初始化；用户修改或清空后保留用户输入，可显式恢复普通来源示例。字段身份API不再返回示例，版本示例也不可写入为定义内容。

对 `declarative_v1`，`sources.projection.output.<field_definition.key>` 表示原语已经得到的示例字段内容。响应固定声明 `preview_scope=sample_final_assembly`，并用 `executed_stages` 明确 `field_generation=false`、是否运行了静态来源投影以及 `final_assembly=true`。因此该接口能真实反映未保存的字段顺序、标签、分隔符、通道、必填/空值与长度规则，但不会调用文本模型，也不会假装已经用真实项目/镜头执行参考上下文原语；原语参数仍需在真实上下文中验收。语义校验失败返回稳定错误，不由 preview 自行重试。`system_instruction` 只能使用 `fixed.value`，项目或请求内容不得提升为系统指令。

只有 `review_state=ready` 且每条“字段内容 + 最终排版”规则组合完整的 draft 可发布；published/retired 版本及其字段合同不可原地修改。具体模型要求的字段名称、顺序、语言、长度和校验组合属于已发布 Profile 版本数据，不进入通用 API 的模型特例分支。策略模式的执行实例版本直接引用已发布的精确 Profile，复核其操作、模型族及模式与实际资源合同的兼容性；不再登记运行时 Binding，也不按 priority 自动选策略。发布、退役和 CRUD 使用 staff 配置权限与生命周期审计；执行定义冻结精确资源身份，逐次 Preparation 冻结本次输入、字段选择和实际请求。

剧本生成、结构化、独立翻译及图片/视频等业务请求均选择精确执行实例版本及定义 SHA，不接受另外拼装 deployment/workflow/binding ID。实例明确采用策略、人工通道或无提示词模式；原生文本合同按实际操作装配，字段辅助调用只能使用实例中明确配置的叶子实例。缺配置、资源不相容或定义漂移均明确返回错误，不回退其他策略。策略示例预览不构成资源或组合烟测证据；未完成生产链与验收范围见[最新待办复核清单](optimization/backlog-review-2026-09-28.md)，实际开放仍须核对当前精确版本与准入证据。

`image.generate` 策略不会因为旧 Bible `prompt.normalize` 路径无法处理项目标题、简介、剧本或风格说明而禁止发布。直接来源按策略字段投影为有类型的 `profile_inputs`，由精确实例显式接线；发布实例时检查必需输入是否完整，本次准备时核对授权来源及值，缺失不猜测。字段生成、翻译、语言与人工确认仍按该策略及辅助实例合同执行，不因允许来源而绕过审核。

#### 创建 PromptProfile 后继草稿

| 项目 | 合同 |
| --- | --- |
| Method / Path | `POST /api/v1/prompt-profile-versions/{id}/clone-successor/` |
| 权限与入口 | 登录且具有 staff 配置权限；API 接受 `direct_v1`/`declarative_v1` 的不可变来源，系统设置当前在已发布/已退役的 `declarative_v1` Profile 显示“创建后继草稿” |
| Body | `expected_source_contract_sha256`：必填 64 位小写 SHA-256；`name`：必填 1–160 字符 |
| 成功响应 | HTTP `201`，返回同一 `profile_key` 下按全部历史版本递增的新 draft；`review_state=needs_review`、合同 schema v8，并精确复制来源 Profile 的配置与全部字段规则 |
| 数据副作用 | 新增 `PromptProfileVersion`、全部字段规则和 `clone_successor` 生命周期审计，并计算新合同摘要；不修改来源版本、Binding、ProductionRun 或外部服务，不自动发布或激活 |
| 失败处理 | 来源 hash 过期、来源不是 published/retired、来源使用只读 legacy projector、名称无效或并发变化时返回显式 4xx，且不创建部分草稿 |

调用方必须先读取来源版本并把当时显示的 `contract_sha256` 原样提交；响应丢失后先按同一 `profile_key` 重新读取版本列表，不能无条件重复创建。该 action 不接受客户端指定版本号，也不会把既有执行实例自动改指向新草稿。修改并预览后仍需人工把 `review_state` 设为 ready 并发布；要在后续执行中使用新策略，须通过精确执行实例版本显式引用，并满足其校验与准入要求，不创建已退役的旧Binding。已发布实例和历史ProductionRun继续保留原策略版本及冻结事实，不随新草稿改写。

## 项目独立翻译与人工审核

以下为当前统一实例接口；0144是9月12日历史快照，不能作为当前迁移依据，后续实际切换记录见25.180–202。入口为项目/Bible现有翻译审核面板：字段旁创建异步翻译；“查看翻译任务”恢复原准备与候选。它不是生产目标字段的独立规范化阶段。

独立翻译请求由原实例映射生成 `schema_version/items`；原生协议构造器只接受登记的翻译字段、策略装配数据、受限修复反馈及传输容量探针，未知根字段明确拒绝，不投影另一份字段生成上下文。保护词使用服务器占位符往返，翻译的计数修复仅绑定原条目索引及原保护词次数，不接受字段生成的长度修复授权。生产字段生成/翻译仍由 `prompt.fields.generate/translate` 的确切 `field_contract`、`source_text/context_facts/reference_facts` 验证；两者复用纯字段校验能力，不共享旧目标合同执行入口。已冻结请求不重建，历史结果不补造。

| 接口（统一前缀 /api/v1） | 职责 |
| --- | --- |
| GET projects/{project}/translations/options/ | 按原分页返回已发布、启用、可访问的原生prompt.normalize精确实例版本/SHA及本英文审核面的明确语言、批量配置；目录不代表本次请求已准入，不选默认部署 |
| POST projects/{project}/translations/requests/ | Idempotency-Key必需；明确实例版本/SHA、字段原文/作用域、所见人工候选ID/修订、共同request/field_values及外发授权/预算。条目输入由服务端按该实例实际映射装配；客户端不能注入另一份items。首次201、原请求重放200、来源/同键冲突409、坏输入400 |
| GET projects/{project}/translations/requests/ | 分页原Job状态、Preparation/精确实例ID、时间和来源数量；不读原文、结果或保护快照 |
| GET projects/{project}/translations/requests/{request}/sources/ | 按位置分页字段身份、所见修订与采用回执；不含正文 |
| GET projects/{project}/translations/requests/{request}/sources/{source}/ | 按需核对原Job、冻结请求/输入摘要、原结果及保护词，返回原文/译文与当前可采用性；原任务未成功或证据损坏400，不伪造空候选 |
| POST 同一来源详情路径，正文{} | 明确采用为原PromptFieldTranslation待审核候选；原文/对白身份/相关上下文/人工修订变化409。重复采用返回原回执，不覆盖之后的人工修改，不调用模型、不自动确认或晋升 |
| GET projects/{project}/translations/；POST projects/{project}/translations/manual/ | 保留原人工审核读取及手工空白候选入口；列表episode/shot为真实UUID，status为正式枚举，非法参数400 |
| POST translations/{id}/confirm/、translations/{id}/revoke/ | 只确认或撤销候选审核；原文、source_hash、Shot字段及对白不变。名称禁止经普通confirm隐式确认，撤销不删除术语 |
| POST translations/{id}/confirm-canonical/ | 明确确认人物/场景/道具名称并原子写入项目术语表，校验所见候选修订、术语值、项目版本及来源上下文；不调用模型 |

`translations/promote-shot/` 已退役，候选响应不再返回 `promoted_fields`。确认沿所见候选修订校验；已确认请求以旧修订再次提交返回409，不以译文与源文碰巧相同推断成功。对白delivery候选必须核对原明确位置、发言者和正文摘要，缺少身份或位置变化均冲突；确认不改写原对白、delivery或毫秒。Shot固定创作字段和metadata负面词不再是翻译来源；手工新建返回400，辅助生成入口在分配Job或调用模型前拒绝。旧候选仍可读取原文、译文与历史，但context_current为false，重新确认返回409/source_unavailable，不通过当前配置重造上下文。历史 `runtime_metadata` 和审核记录如实保留，但不是当前回写指令。分镜页移除旧翻译聚合和译文回填；独立创作字段另存原字段修订，仅通过明确策略及来源映射进入新准备，不自动使用翻译覆盖层；旧自动覆盖辅助实现已删除，确认候选与执行字段选择仍是不同事实，编译仅消费本次明确输入。

权限同时遵守项目与原独立Job所有者范围，管理员可操作；未登录401，越权项目/请求404，服务层越权拒绝。读取private/no-store；分页只接受page/page_size/format单值参数，复用平台上限，不自动读取隐藏历史。状态、费用和远端提交仍由原AssetGenerationJob负责，Preparation仍是输入事实源。条目/原文/保护内容受现有配置与64KiB合同字节上限约束，不截断后提交。

模型返回的不完整批次作为原Job失败，不能把部分格式错误响应写成通过的译文。原文发生变化后可查看旧结果，但需要重新选择当前来源创建请求；原任务的明确重试复用冻结事实和种子，不刷新到最新原文。失联提交重试同一幂等请求；不确定远端提交在原任务中人工消歧，不能直接重跑。

已返回但不符合响应合同的文本任务标记failed，错误码asset_text_response_invalid，远端状态response_rejected；响应丢失或本地结果落库失败仍为待核对，不会因新错误分类自动重试。审核仅接受显式当前上下文版本和准确摘要，不从旧译文的词语推断上下文有效。页面可逐项选择原文，空批次或超过配置明确阻断，不自动拆成多个模型请求；文本候选应回本来源审核，不进入素材库的媒体生成页。

名称字段的输入上下文包含该实体保存的名称、说明和`primary_setting`，随原请求冻结。已建立主图时，从其当前指针读取确切设定修订，包含appearance_id、setting_revision_id、revision、content_sha256、identity_anchor及appearance_anchor；不读取旧实体visual_identity，不混入变体差异、不自动拼接文字。尚未建立主图明确为null，已有主图但当前修订缺失、损坏或归属/名称不一致则报来源冲突，不把错误当null，也不拿旧修订补齐。名称/说明或主图确切修订变化后，原结果仍可查看但不能直接采用/确认；无关实体变化及同一修订后续审核不改变此名称内容上下文。

主图锚点翻译使用六项明确来源：`character|scene|prop.primary_setting.identity_anchor`与同作用域的`primary_setting.appearance_anchor`（前缀按具体实体类型替换）。旧三项`*.visual_identity`移出当前目录，原手工候选/异步翻译请求拒绝旧键，不迁移或改写历史译文。服务端先核对实体归属，再通过共用主图读者核验当前确切修订；无主图是来源冲突，不使用旧文字，已保存空正文保持空值且不能创建空翻译任务。上下文冻结完整主图内容身份和两段原文，同字但新修订也使旧候选不可采用/确认；审核决定或图片关系变化不改变文字来源。

手动入口：项目工作室 → 对应Bible实体 → 主图/变体设定 → 当前主图详情 →「当前主图设定 · 英文候选审核」。复用原空白候选、精确实例生成及人工确认流程；历史修订、变体详情不提供这个当前主图入口。工作室原字段面板不再提供旧视觉身份字段；分镜页已移除旧翻译聚合，独立创作字段通过原字段修订组件另存；翻译变更仍通知工作室标记依赖输入变化。译文不回写主图正文，不创建图片、审核或采用事实。锚点及译文是否进入执行取决于本次策略和明确来源映射，不自动覆盖subject/environment或参考描述，不读取旧实体visual_identity；没有主图时不伪造身份正文，是否必需由所选输入合同决定。

请求和来源详情的采用操作只写待审核候选。人物/场景/道具生产主链直接使用原名称，不需要英文名补全；工作台已移除名称译表编辑、名称候选生成及写表确认入口，不再调用 `confirm-canonical`。该后端接口和历史记录未在本次前端收敛中删除，不代表生产链使用其译名。正文翻译候选保留独立入口，确认不改原文，也不表示被本轮采用。制作版本中的统一名称只读本轮保存的实体/参考名称，不把项目当前英文术语表认作历史快照。保存Bible只保存实体和绑定，不自动外呼或确认；本段不改变后端接口合同。

项目删除前须确认关联翻译根任务及其辅助调用已终结；完成后的项目删除可清理来源/审核关联，原独立Job和Preparation沿原生命周期管理，不因删除项目而重写已提交请求。0159反向迁移在存在新来源或text根候选时明确停止，要求先按批准闭包清理，不自动转换或抹除数据。

旧基础部署选择能力目录、同步字段翻译/重译/ensure-canonical-glossary入口及其同步封装已移除，没有别名；基础资源合同与烟测校验不是业务翻译旁路，仍按其职责保留。当前证据为隔离API/原worker（受控返回）、组件、全量前端、类型/构建及迁移测试；实际浏览器、外部模型与运行库尚未验收。详见改造计划25.114。

### 名称候选的正式术语确认

- 接口：POST `/api/v1/translations/{id}/confirm-canonical/`，登录项目所有者或管理员；不可见候选404，非法形状/非名称/无效英文400，项目/来源/候选/术语变化或跨实体碰撞409。
- 请求必须且仅包含 `expected_revision`（正整数）、`expected_project_updated_at`（读取项目时的带时区时间）、`expected_canonical`（当前术语文字，无配置明确null）和 `translation`（经人工核对的最终英文）。不接受布尔修订、伪造采用目标或额外配置；英文名称长度/词数沿原可配置名称合同，保留原单行英文及身份冲突校验，不新增音译/直译规则。
- 读取原候选增加 `confirmation_target=field_review|canonical_glossary` 和 `canonical_value`。名称确认只写 `SeriesProject.production_settings.translation_glossary[原名称]` 与原 `PromptFieldTranslation` 审核事实；不改中文名称、视觉描述、工作流或策略，也不重新生成候选。普通空白手工草稿显示为可编辑，不代表空文本已有效；确认仍校验真实填写内容。
- 成功200返回 `entry`、`project_id`、`source_text`、`canonical_english`、`previous_canonical`、`updated_at`。回执包含原候选修订、前后术语、当前名称合同与配置解析SHA；上下文重基于已确认术语，原请求输入仍不可变。项目锁、实体锁和候选锁保证一个事务内提交；失败不部分更新。
- 同一请求的网络重放返回已记录回执，不再次写入；回执的项目时间是原次确认的时间，不冒充随后其他编辑的新版本。已变化的术语或候选不能被旧请求重放覆盖。页面只合并回执对应术语，保留其他未保存编辑；若该术语在等待期间又被本地编辑，保留人工文本并提示核对。
- 验证范围：本轮新增隔离API/共享worker、组件和契约回归；最终结果见改造计划25.117。后续运行切换已有记录，当前浏览器及真实模型验收范围见第0.1节。

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

项目工作台的图片/视频候选比较从原详情GET重新读取签名预览，不因列表对象已有manifest而复用可能过期的URL。刷新只读，不写选择或调用模型；单候选请求序号及运行身份隔离旧回包。候选非available/selected、详情失败或媒体GET/浏览器解码失败时明确提示，失败播放器及下载入口移除、选择禁用；“重新读取预览”仅重读该候选，顶部刷新重读当前比较项，不自动采用。文件加载错误不能据浏览器信息猜成具体HTTP原因；已存在的“当前选择”事实不会因预览故障被清除。两处候选页共用稳定请求恢复：未知结果保留原键及完整正文，暂停父页轮询/整页重载与新意图；独立核对入口不依赖当前媒体URL或候选状态。回执确认后只GET当前Run、项目和文本分页，不把历史回执当当前选片；读取失败明确保留已确认状态并禁止重复提交。M8-02现用加载及完整验收以唯一清单为准。

原生文本候选读取（当前统一实例合同）：

审批动作需登录且具备本轮操作权限；成功返回200及原ProductionRun详情，未找到/越权沿用404，来源或项目并发冲突返回409及code/details，缺项/候选内容错误返回400。审批不创建外呼；批准后的原推进器可能根据冻结计划准备下一阶段，仍按本轮外呼授权、准入和预算处理。

配方选项增加只读text_output_options目录，含output_key、stage_key、artifact_role、kind、scope_kind、context_selector和label。当前登记生成/结构化剧本的script.content（文本）与script.visual_context（JSON）投影。作者选择selected_output、上游stage_key、目录output_key、scope.kind=project、selection_policy=reviewed_selected；本轮准备时解析为精确decision_id、artifact_id、输出键及项目ID。只允许唯一的正式采用产物并核对对应审核；多个采用结果、未审核、跨Run、过时决定及类型不符均拒绝。影视规则实例只允许合同声明的上下文投影。实际值及选择/审核/修订证据首次冻结，不通过“最新剧本”替代。目录不是新执行入口，不读Provider或创建数据。

通用结构化文本的配方保存/冻结/准备使用相同的内部动态输出目录：从已配置步骤的确切执行定义读取BusinessOutputVersion和结构，整值输出键为`structured_text.{output_version_id}`，与`stage_key`共同定位；同名步骤或复用同一输出版本不合并。目录还含`reader=structured_text`、`output_version_id/output_contract_sha256/structure_version_id/value_schema`；所有候选/fallback路线的输出身份须一致，缺路线的草稿不生成猜测输出，非法配置返回400。作者仍通过原Recipe definition的`selected_output`规则选择更早步骤，`scope.kind=project`，`selection_policy=reviewed_selected`沿原选择协议；通用文本只核对实际自动/人工选择，不新增剧本审核要求。准备绑定叶子额外保留`stage_key`，原读者验证同一Run的当前Decision/Artifact、完成步骤的选择快照和原请求输出合同，读取真实`candidate.value`；证据包含确切输出/结构身份、原选择模式及实际操作者。返回值为false、0、null或空集合时不替换；未知字段不在读侧删改。对象/列表/深层路径及Profile根输入均走原映射器，读取不创建选择、任务或回填历史。新派生的Profile结构化根输入允许已有scope目录的显式接线，其他原来源端口范围不变；实际读取仍核对归属和合法上游。

上述链路已接入配方原字段装配表单：按当前步骤之前的确切输出选择来源，再用共用路径控件选择对象属性、明确下标或列表逐条映射。同名步骤以步骤序号区分，选择值以步骤身份＋输出身份保存，不以显示名称匹配。接口与隔离页面已验证；完整配方发布/组合验收仍见详细设计G06，不代表现用库迁移或真实模型产出通过。

#### 配方草稿的动态文本来源目录

- **接口**：`POST /api/v1/production-recipes/source-options/`；系统设置→配方→步骤路线→输入来源。继承配方管理的登录管理员权限；不另建配置或结果表。
- **请求**：JSON对象，必需`definition`为当前schema3草稿（与保存接口同形：`schema_version/interaction_mode/stages`）；无URL参数、分页或默认值。步骤声明必须合法，路线引用明确的实例版本及摘要；输入映射可尚未配置。编辑器仅在步骤/顺序/输出路线声明变化时刷新，不因填写字段值重复读目录。
- **响应200**：`{"text_output_options":[...]}`；各项包含`stage_key/output_key/kind/scope_kind/label`及可用的`value_schema`；通用输出另含`output_version_id/output_contract_sha256/structure_version_id`。标签前缀为当前步骤序号，唯一身份为`stage_key + output_key`，同输出版本可被两个步骤分别使用。只返回草稿声明的步骤；空列表表示没有可用输出，不推测字段或生成结果。没有路线的未完成通用步骤不产生目录项。
- **示例**：提交`{"definition":{"schema_version":3,"interaction_mode":"assisted","stages":[{"key":"script_generate","mode":"ai","approval":"auto"}]}}`返回该已声明剧本步骤的原生投影；通用步骤的`routing.routes`使用原配方确切实例合同，目录从它的已冻结输出结构派生，不接受调用方自行提供Schema。
- **错误**：400说明非法步骤声明、重复身份、错误实例摘要/能力或同一步候选路线输出版本不一致；403表示非管理员或未登录（鉴权入口也可能返回401）。全次失败不返回猜测目录。页面明确显示失败和“重新读取来源目录”；旧目录不继续用于选择，草稿及失效路径保留，必须显式修正。过期响应被丢弃。
- **读取与副作用**：复用`recipe_stage_declarations`和`execution_text_output_options`，只读确切执行合同与输出结构，无外部请求、不查询候选值、不保存配方、不建准备/任务/选择，不写审计或更新历史。它不是就绪、发布或运行准入；保存与实际准备仍核对映射/前置依赖。原GET`options/`继续提供编辑枚举及原生投影，两接口复用唯一输出目录实现。
- **实现/验证**：`ProductionRecipeVersionViewSet.source_options`、`recipe_text_source_options`；隔离API核对权限、错误、两个同名同输出步骤及零写入；前端真实组件与隔离浏览器核对路径、迟到响应、失败/重试及只读。未发布现用配方、未迁库、未真实调用模型。

| 项目 | 合同 |
| --- | --- |
| 入口与权限 | 原产物列表/详情；需登录，管理员可查看，普通用户仅本项目所有者范围。沿用项目工作台的产物入口，不新增业务CLI。 |
| 请求 | 列表支持production_run、stage_run、generation_task、kind、status、variant_group筛选；page/page_size沿用平台分页配置及安全上限。详情id是原GeneratedArtifact UUID。 |
| 列表响应 | 原分页count/next/previous/results及窄列产物信息；不含manifest_metadata或text_candidate正文。无结果返回空results。 |
| 详情响应 | 保留manifest_metadata；text_candidate包含operation、preparation_id、result_sha256、candidate、text、warnings、quality_advisories。长度建议仅展示冻结候选实际声明，无声明为null，不注入前端默认上限。剧本text为原修订正文；影视规则text为null，字段在candidate；通用结构化文本text为null，candidate.value是实际JSON值，另返回output_contract确切合同、同源编译的json_schema和structure_warnings路径提醒，candidate.defaults保留默认来源。非本链文本候选的text_candidate为null。 |
| 数据与副作用 | 读取原GeneratedArtifact、GenerationAttempt、Preparation及按需ScriptRevision；不访问Provider，不生成新任务、不批准、不选择产物或覆盖项目内容。列表不读取文本候选正文及关联Run/Attempt的大快照。 |
| 错误 | 未登录401/403，越权或对象不存在404；候选身份、结果SHA或剧本正文漂移400并返回text_candidate字段错误。读取不修复源数据，不返回默认候选。 |
| 验证 | 使用隔离数据库及虚拟Provider响应、分页候选组件交互测试验证；记录见统一改造计划25.58–25.59。真实服务、浏览器及付费模型未验收。 |

生成阶段完成只代表冻结数量的候选齐备。原生项目文本审核接原approve动作：人工明确选一个候选，或按冻结的无人值守政策仅在候选恰好一个时采用。通用选产物动作拒绝绕过文本审核；明确失败重试沿原准备/调用状态判断，成功辅助批次不重跑，uncertain必须先消歧。后续阶段沿本轮冻结计划和已采用事实推进。

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

主机推荐是资源及默认参数建议，不是可执行路由。候选分别记录能力、Deployment及可选工作流资源，不返回旧`route`组合。`decide`在核对当前主机快照、目录及配置摘要后记录决策：接受/覆盖只创建待复核的ConfigRevision，不创建或修改执行实例、策略、工作流或配方。仅`override`允许`override_values.configuration`对象，旧route、未知覆盖字段、错误类型以及accept/reject携带覆盖值均返回400且不写入。决策响应保留`config_revision`，不再有`recipe_version`。真正执行仍须从正式配置入口创建精确实例，并在schema3配方或人工计划中明确选择。

`config-definitions/options` 是新增/编辑定义的目录事实源，返回配置类别、值类型、作用域、默认来源、域、单位、影响模块、系统默认和 `custom_entry_fields`。当前设置分区固定新定义的配置类别；配置键与类别创建后不可修改。定义响应额外返回只读 `has_values` 与 `mutable_fields`；已有 ConfigValue 时结构字段不可改变，客户端只发送相对打开快照的真实差异 PATCH。服务端按规范化后的真实值判断变化，相同结构字段不会制造伪冲突；未知目录项仍被拒绝。

resolved config 按 model default → workflow default → active system revision → active project revision → active run override 合并，并返回每项来源。设置页面和客户端只能提交 dirty keys；恢复继承使用 `unset_keys`，不得把合并后的 model/workflow 默认值整体复制成 system override。ProductionRun 创建时冻结不可变 snapshot；restart-required revision 必须在新 boot 显式 activate。

`ConfigDefinition.configuration_class=security_boundary` 表示仅由当前 active system Revision 提供的安全绝对上限。此类定义必须同时满足 `domain=security`、`allowed_scopes=system`、无默认来源、required、非 secret 且 active；缺失、分类错误、来源错误、hash 漂移或请求越界均失败关闭。platform administrator 可读取定义和值，但只有 super administrator 可以新增或修改安全定义、在 Revision 中显式 set/unset 安全值，以及发布或激活 `changes_security_boundaries=true` 的 Revision。Project、Run、Model、Workflow 和历史冻结快照均不能放宽当前安全边界。

安全值由业务提交、解析、下载或媒体处理边界实时解析。工作流 Smoke Fixture 的字节数、参数项和物理素材数，延迟生成抽帧时限，工作流输出前缀长度，以及提示词事实树的嵌套深度、字段数和键长度均使用 `security.*` 定义；媒体 FPS/时长、阶段快照、Bible 候选数、单实体 Bible 参考图数、单次 Bible 重生成实体数、Provider usage、PromptProfile 字段/validators/context selectors、Comfy timeout 与异步轮询也不得再叠加代码常量。队列建议、自动分段、任务健康阈值、AI 披露文本和显存建议档位属于可调业务配置，不在 `value_schema` 复制任意业务 maximum/maxItems/maxLength；其真正资源风险应由独立 `security.*` 边界负责。API 返回的 `changes_security_boundaries` 是服务端只读分类事实；缺失或未知时客户端必须失败关闭。安全边界错误使用 `security_boundary_definition_missing`、`security_boundary_definition_invalid`、`security_boundary_source_invalid`、`security_boundary_value_invalid` 或 `security_boundary_exceeded` 等稳定 code，不允许客户端改用本地默认值继续提交。

`ConfigDefinition.value_schema.resource_ref` 是服务端统一校验的精确执行实例推荐合同，唯一资源类型为 `execution_instance_version`；`capability` 指模型/工作流执行能力，`allow_empty` 表示可不推荐。`GET resource-options` 为管理员提供标准 `count/next/previous/results` 分页，每项包含 `value`、安全 label、resource/capability、state、`reason_code` 和 `is_selectable`，不返回服务地址、凭据或诊断正文。`configured` 仅表示实例启用、精确版本及资源已发布且冻结定义可读取，不表示 Smoke、容量或当前 worker 准入通过；草稿、停用、退役或冻结定义损坏返回不可选原因。新增或真正变更非空配置值时再次核对精确能力、生命周期及当前完整定义；原样继承仍做存储合同和整版摘要校验，不因瞬时运行证据变化阻断无关配置。`production.prompt_normalization_execution_instance_version_id` 只作为明确选择的推荐，初始空值不触发自动选择或模型调用。任务必须明确引用精确版本并通过自己的 Preparation/准入，不从该推荐隐式替换；旧 Deployment 资源类型、require_ready 开关及全局英文门禁已移除。

ConfigRevision publish 会在锁内检查 `based_on_revision`，旧草稿返回 `stale_config_base`；同 scope 已有待激活版本时拒绝第二个 pending。普通 platform administrator 可以原样携带已有 SecretVersion，但增加、替换或移除秘密绑定必须由 super administrator 在 publish 与 activate 时再次授权。

RuntimeHost 由基础设施登记，collection 保持只读。`status` 只负责启用/停用；启用后先进入 unavailable，必须重新 probe 才能 active。仍有 active deployment 时不能停用，已禁用主机不能 probe，active deployment 也不能绑定非 active 主机。

`POST runtime-hosts/{id}/probe/` 只创建或复用该主机唯一 active 的 `RuntimeHostProbeAttempt`，返回 `202` 和 attempt detail 的 `Location`，不会在 HTTP 请求内执行模型文件 SHA。状态为 `queued → running → succeeded|failed`，取消时为 `cancel_requested → cancelled`；queued 可立即取消，running 在受控文件分块检查点停止。Settings 必须从 attempt collection 恢复任务、轮询详情并等待服务端终态，不能用客户端超时推断失败。platform-maintenance worker 会领取 queued/recovered attempt、持久化心跳、回收陈旧 claim，并通过 snapshot 的一对一 attempt 关系处理“快照已提交、终态未回写”的进程崩溃。attempt `succeeded` 只表示不可变快照已完整提交，不代表快照的 `probe_status` 或 readiness 为通过。同步 `probe_runtime_hosts` 只保留为受控运维命令，不是日常页面/API 旁路。

主机探测调度读取同一系统配置的 `runtime.host_probe_mode`（`automatic|manual`）。自动模式按 `runtime.capability_ttl_seconds` 为新快照写入 `expires_at`，默认2592000秒（30天）；手动模式不自动入队，正式 probe API 仍可创建／复用任务，并按 `runtime.host_probe_manual_validity_days` 写入长期有效期，默认36500天（约100年）。这是明确的将来到期时间，不是“永不过期”标志或准入绕过；其他模块仍读取原 `expires_at`。切换模式不取消已有任务或改写旧快照；新探测使用开始时读取的配置，`details.runtime_configuration`记录模式、有效秒数和配置摘要。切回自动时调度取已有到期时间与“采集时间＋当前自动周期”的较早值，避免长期手动快照阻止自动刷新。模式和期限缺失／非法仍明确报错，探测失败／部分成功、主机停用及文件不匹配规则不变。配置仍使用既有配置修订创建／发布接口和权限，不新增保存入口，不调整 Provider/执行实例 Smoke 有效期。

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

## 系统资源策略与人工确认

入口为系统设置 → 默认值与限制 → 系统配置 → `execution.resources.review_policy`。使用原 ConfigRevision 草稿、发布与启用流程，只允许 system 作用域，动态生效。结构如下（数字仅为示例，不代表供应商能力或自动授权）：

```json
{"budget_by_currency":{"USD":"20.0000","CNY":"100"},"call_count":100,"output_length":8192}
```

- 金额是各记账根（生产轮次、素材任务或烟测及其辅助调用）的累计提醒值，不是余额、单次价格或汇率。金额非负，最多8位整数和4位小数；0会触发非零费用确认，币种未配置也要求确认，不自动换算。
- call_count 为正整数，统计主调用、辅助调用及明确新建的重试，不是镜头数或并发数。output_length 为正整数，单位 token，作为新建文本准备的输出额度，并用于既有文本请求的超额提醒。新准备冻结策略来源，旧准备不随配置更新改写。模型/服务商真实能力和媒体格式约束不属于可审批突破的业务额度。
- Run、素材任务、项目翻译及 Smoke 创建不再接受单独 budget_limit；Run 也不接受 budget_currency/max_generation_tasks。配方预算/调用上限、Run 预算/调用上限及项目创建次数字段已移除，旧写入字段明确拒绝。外发授权独立提供，不继承配方授权。业务阈值不再直接令任务失败。
- 无配方工作台 `POST /production-runs/{id}/authorize-cost/` 保留外发授权管理，不接受 budget_limit，不调用模型。调用进行中不得修改授权；已保存准备不会因授权变更自动重试，用户需重新读取并明确继续。
- `GET /production-runs/budget-options/` 登录可读，无请求参数；返回 currencies 数组与 resource_policy（schema_version/values/source）。目录合并服务端常用币种、系统策略及启用部署计价；非法声明400，未登录401。无秘密、账目或外呼；下拉不自动选择首项，读取失败不回退旧预算。
- 准备详情新增 nullable `resource_review`：fingerprint、policy_current、issues、projected_calls、projected_total、currency、confirmed、confirmed_at。未冻结超额准备为 awaiting_review；已经冻结排队的请求保留原派发事实，原队列暂停且不自动提交。
- 本地明确零费用请求保留原估算币种，但累计预算按原 Run 账目币种评估；非零币种冲突仍拒绝。用量确认保留观察时的准确次数；次数在系统上限以内变化不使费用确认失效，越界或越界后增加、请求/费用/策略变化仍需重新确认。
- 准备详情 `media_archive_recovery`（nullable）提供原 task_id、attempt_id、task_updated_at、receipt_sha256、can_recover、reason。正式任务中心“准备与调用”提供“仅归档原回执（不重新生成）”。`POST /execution-preparations/{id}/recover-media-archive/` 接收 expected_preparation_revision 及上述四项精确身份，只允许所有者或管理员恢复远端 terminal_succeeded、本地 failed 且仍有效的 ComfyUI 生产主调用。校验冻结请求、原回执字节/摘要和终态后，将同一 Task/Attempt 排回原队列，不创建模型调用；200表示已排队，不表示已归档。旧状态、错误代码、错误正文SHA256、结束时间及回执指纹追加到 Run 生命周期记录，不记录错误正文，原回执不改写。重复/陈旧/取消冲突409，不可见404，非法输入400；成功任务不可重新执行。原回执缺失或损坏不回查远端、不重发；原文件仍须通过媒体、来源签名和幂等归档检查。Run/Stage 的业务暂缓不拒绝有效在途成功回执，取消及失效调用仍拒绝。
- 准备详情 `media_uncertain_retry`（nullable）提供 task_id、attempt_id、task_updated_at、request_sha256、can_retry、reason。`POST /execution-preparations/{id}/retry-uncertain-media/` 接收上述四项身份、expected_preparation_revision 及明确为 true 的 acknowledged；仅所有者或管理员可在原 ComfyUI 生产主调用已回收为 failed / uncertain_remote_response、无领取/回执/产物/取消/成功后继时确认。此 failed 是本地处理状态，不代表远端明确失败。原记录、提交令牌、未知实际费用和预算预留不改写；新调用复用同一准备的冻结输入与精确执行版本，经原准入/预算/队列新增 retry_of 后继，新增预算独立预留。旧调用停止自动恢复和采用迟到结果，但保留未决事实；新请求可能重复执行或计费。读取和自动重试不能触发此操作。页面必须明确勾选风险确认；200表示已排队，不表示模型成功，同原调用/操作者/请求摘要重放只返回既有后继，不再次创建。观察/修订/取消/结果冲突409，不可见404，非法输入400。该路径不适用于已有成功回执的归档恢复，也不适用于辅助文本或异步REST协议。后继正常推进不再被已显式替代的旧未决调用拦住；其他未决调用仍阻止新提交。
- 生产文本与异步媒体收到明确实际账单后，若累计真实费用高于系统提醒值，调用与候选保持成功，Stage/Run 进入 awaiting_approval（actual_budget_exceeded），从原费用确认入口核对；不要求为保住结果而提高额度。不明账单仍保留预留，不能用预估冒充实际费用。后续新调用重新核对当前系统策略。
- `ProductionRun.actual_cost_total` 是已记录且确认的实际金额小计，不是全部调用已结算的证明；可能在存在 `GenerationAttempt.actual_cost=null` 时仍为0。运行详情及取消／重试确认标为“已记录实际”，明确0不表示免费，未知部分继续从原调用核对，不由前端补零、按token计价或重算历史。
- `POST /execution-preparations/{id}/confirm-resources/` 需要 expected_preparation_revision 正整数、fingerprint（64位SHA）。仅有权owner/管理员，Smoke仍限管理员。原子保存不可变确认并返回200详情；同指纹重放幂等；评估/策略/修订冲突409，不可见404，非法输入400。确认不提交、不采用、不增加次数，不是全局豁免。另行 `continue/` 重新核验真实价格、用量和策略，变化后需再次确认。
- 生产文本已返回但未通过冻结响应格式校验（包括JSON不完整、输出字段/枚举不合约），或生产文本被 length 截断、服务商400/422以明确容量代码拒绝时，GenerationAttempt 为 awaiting_review，不自动重试；格式错误记录安全的validation_code和summary。生产主调用和字段辅助的成功响应均保留实际正文（成功主调用diagnostics.contract_error_code=response_candidate_recorded、editable=false，人工确认时间/操作人为空），格式或字段硬约束不合时保留原文并等待人工处理，不丢弃返回、不部分写入字段。缺失或非字符串正文明确失败，不伪造可修复文本，也不误报提交不确定。详情 `text_response_review` 提供 id/attempt_id/asset_job_id/original_text/original_sha256/corrected_text/corrected_sha256/confirmed_by/diagnostics/editable/confirmed_at/readonly_reason、fields数组（unit_key/field_key/label/text）与notices数组（code/message）。fields仅在完整唯一JSON与原批次request_id精确匹配时投影；否则展示原文，不按位置或相似内容猜测归属。主调用notices保留原生候选的字符串提醒，辅助notices仍投影字段对象提醒，不混用类型。原始正文从非公开受管文件按SHA与大小校验读取，同时核对同一Attempt/Preparation的冻结请求；不保存推理内容或任意供应商错误正文。未知错误、内容过滤、鉴权和提交不确定不冒充容量问题。存储写入或回读失败标记execution_response_storage_failed，不伪装已留存、格式错误或成功候选；保留该次真实调用结算，不自动重领或切换配方路线。成功/已人工处理/仍待修正由原事实区分，人工处理后的原诊断按历史记录收起。生产正文恢复归属GenerationAttempt，独立素材归属AssetGenerationJob（见下方独立素材补正段），不将独立Smoke失败伪造成验收通过。
- `POST /execution-preparations/{id}/complete-text-response/` 需要 expected_preparation_revision、review_id、original_sha256、text。按冻结原文本协议验证；错误400且保留原响应，冲突409，不可见404。生产路径成功200，只创建主调用待审阅候选，分别保存原响应/人工修订文件大小、摘要及确认人。在原根事务内先留存补全文本和真实确认记录，再创建引用该记录的候选并完成调用结算；候选或结算失败时确认记录及本次业务行一起回滚，不留下“已确认但无候选”的成功状态。同内容重放幂等，不覆盖已确认正文、不追加模型调用或自动采用；已取消不可补全。响应读取复用原生产请求核对，同内容的另一文件不能替代原准备所引用的请求文件。辅助响应只读保留，用户在主准备原字段表单人工填写并确认，避免将人工内容冒充模型结果；辅助等待人工时停止自动轮询，主准备字段齐备后可明确继续。文件读取失败显式报错，不补空内容。
- 配方文本输出或执行准备等待人工审核时，Run 为 awaiting_input，不参与自动推进扫描；人工补全只保存候选，用户需另行继续原运行。人工制作确认 actual_budget_exceeded 后回到 awaiting_input，保留原候选，费用确认不调用模型、不自动采用。
- 历史未留存的失败正文无法恢复，原终态、请求、计费和审计不改写。需模型重新处理时，调整系统额度、缩小/拆分输入或选择适合实例后明确创建新候选。不会自动分段、自动换模型、付费重试，也不会以确认突破供应商硬限制。
- 字段详情额外提供warnings数组（code/message），复用冻结字段校验器。执行字段的建议长度偏离、自带字段标题是提醒，不以创作质量阻断；保留实际文本，由用户核对、修改并明确确认采用。必填、类型、存储硬上限、控制字符、引用身份和冻结接口约束仍需满足，不能把无效引用或不明归属自动变成输入。提示词辅助详情直接展示实际返回；主准备按需读取对应批次的原辅助详情，核对阶段、父准备和调用身份，不创建候选、不自动审核。历史失败且未留存正文/原因时明确标识不可恢复，不将“字段未保存”说成“模型没有输出”。生产辅助awaiting_review可由原批次显式重试入口新建下一尝试；人工另存字段不改写原调用，未知费用不释放。
- 范围与配置：字段语义校验器及准备展示是共用能力，不按Recipe/Run ID分支。新增原始响应审核存储在生产GenerationAttempt辅助路径启用；独立AssetGenerationJob的失败原文保留并非此次完成范围，Smoke继续沿原证据存储和验收协议。字段生成指导与结构检查由“系统设置 → 提示词策略 → 字段规则”配置；生成长度上下限始终为提醒，语言/标题/正文引用形式不阻断执行。字段是否人工确认来自“配方阶段 → 生成字段确认”，不是执行实例的独立开关；素材库、独立Smoke及无配方制作为人工确认。已发布定义需创建后继版本，既有准备不被当前配置重写。原文留存和不伪造历史是平台行为，没有本次新增的可关闭开关。

### 单镜与多镜字段确认

- 单镜字段正文与批量复核共用“内容提醒（不阻断采用）”展示，仅消费原回执中的warnings，不按页面文字另算质量等级或改写正文。建议长度、文风提醒不单独禁用确认/采用；手动入口仍需明确确认，已配置的自动策略沿原policy事实，不补造human审核。字段硬校验失败保留未保存草稿及原采用版本；接口、文件或映射硬错误仍按原服务返回原因，不能用提醒标签把失败变成可执行或成功。不改变既有HTTP/Schema或新增质量开关。
- 共用Profile文本类型校验保留来源/已采用值的首尾空格与换行，不隐式strip；新装配按实际字符检查字段和通道硬上限，不能靠删空白规避接口容量。显式策略投影、标签和分隔符仍由原策略配置，既有冻结请求不重算或回写。
- `POST /execution-preparations/bulk-field-preview/`：登录后的原owner或管理员提交 `{"preparation_ids":["准备UUID"]}`，显式1–50个、不得重复；仅支持同一未结束生产阶段内、同项目不同镜头的媒体主准备，不接受辅助/Smoke。只读，不派发、不确认、不采用。成功200返回 `items`，每项包含 `preparation_id/expected_preparation_revision/preview_sha256/label/ready/reason/fields`；字段包含 `unit_key/field_key/label/state/reason/value_id/revision/text/warnings`，state为candidate/selected/blocked。正文按原冻结字段合同与来源核验；已有采用保持不变，唯一可选修订才可批量采用，多个修订需逐字段明确选择。缺正文、硬校验失败、冻结/取消等明确提示，本镜头不参与一键采用；质量提醒不阻止复核。响应为private,no-store。
- `POST /execution-preparations/bulk-field-confirm/`：提交 `{"items":[{"preparation_id":"准备UUID","expected_preparation_revision":1,"preview_sha256":"64位SHA"}]}`，明确选择1–50个就绪预览回执，不接受正文、replace、自动执行或跳过校验标志。全批锁定原Run及准备，先重新核验所有预览，再复用原字段确认/采用服务写入当前操作者的human审核和选择记录。已有采用不替换；成功200返回 `confirmed_field_count` 与逐准备 `{preparation_id,revision}`。单镜多字段与多镜提交都为同一事务；任一失败全部回滚，不返回假部分成功。准备/正文/候选/采用状态变化或结束为409，须重新读取复核；重复旧回执同样409，不重复审核。非法范围/字段400，不可见404，未登录沿原鉴权401/403。
- 这两个入口是共用生产准备能力，不绑定某个配方ID；不改变配方字段审核配置，不批准整个阶段、不直接冻结请求或发起模型调用。确认后沿原调度机制：已授权的自动运行在字段齐备后可能继续执行并产生费用；处于人工等待的运行需从原入口继续。两者均须通过原资源检查，不新增外发授权。不得将预览成功或已采用字段当作媒体生成成功。

新增确认/响应审核记录只追加追溯事实，仍复用原 Preparation/Run/Job/Smoke 与原队列。原始和人工修订正文进入正式非公开文件存储，不公开路径。实际启用及验证状态见[统一计划](optimization/execution-instance-unification-plan.md)。

## 统一任务中心（只读投影）

文本 Smoke 原 worker 在业务解析前，将供应商返回的原始文本保存到正式非公开响应存储，并在原任务 evidence 中记录 `response_body` manifest（原任务 ID、长度、SHA256、存储身份）。任务中心复核原任务结果及正文摘要后，在原 `output.text` 返回脱敏原文；存储路径不外露。校验失败保留 `failure_phase=response_validation` 与具体 `diagnostics.validation_code`，例如 `response_json_invalid`；请求检查、响应存储和证据收尾失败分别为 `smoke_request_invalid`、`smoke_response_storage_failed`、`smoke_evidence_invalid`。成功和失败响应均按同一白名单留存供应商实际返回的 `usage.prompt_tokens/completion_tokens/total_tokens`，限定非负整数及当前安全边界；缺失不补0，不由token推算或覆盖实际费用。缺正文、文件丢失或摘要不符不能以空文本冒充成功；旧任务未留存的正文及用量不补造。这些记录不是可编辑候选，人工修改不能变成原 Smoke 通过，也不触发自动重试。

`script.generate`／`script.structure` 成功烟测的 `evidence.result` 是 `schema_version=1, representation=script_validation_summary` 的校验摘要，不是完整解析预览或业务候选：保留确切输出/结构版本、配置摘要、`parsed/valid/validation_scope`、完整解析报告的 `report_sha256/report_size_bytes`、`source_field_count/warning_count`。`warnings` 按相同 `code/message` 归并并记录全部出现次数 `count`，外层提醒同步显示计数；不逐字段重复存储原文、映射值和来源列表。完整模型正文仍只由原 `response_body` 拥有，冻结请求/输出合同保留原解释依据；报告摘要不被解释为已保存完整报告。不提高结果JSON上限，不截断模型正文，不回写旧失败尝试，也不改变正常生产的剧本候选与采用链路。

人工制作的 `manual-action-options` 同时返回 `execution_activity[]`：每项含 `stage_id/stage_key/stage_status/lifecycle_label`、按状态聚合的 `preparations[{status,count,label}]`、按角色和状态聚合的 `calls[{role,role_label,status,label,count}]`。数据读取原 StageRun、ExecutionPreparation、GenerationAttempt；不读完整正文，不增加任务表或重写阶段状态。允许追加候选的 running 阶段显示“步骤开放”，不能作为“模型运行中”的依据。多候选主／辅助调用分别计数；失败、待审、成功均保留，不用最后一项覆盖其他任务。

- `GET /api/v1/task-center/`：在原 Preparation／Attempt／AssetGenerationJob／Smoke／Assembly 事实上查询，不创建任务表或执行队列。查询 `category=production|asset|smoke`、`status`、`project_id`、`production_run_id`、`stage_run_id`、`execution_instance_version_id`、`operation`、`created_after/created_before`、`source_id`、`anomaly`；标识均为可选UUID，各条件取交集，省略不限定。`page_size` 默认25、上限100。按创建时间及稳定身份排序，用绑定当前用户和筛选范围的签名 `cursor` 翻页。无效UUID、未知或重复参数400，不接受前端全量拼接。
- 从人工制作的“查看本步骤任务”／“在任务中心查看本轮任务”带入实际轮次／步骤ID，查询、刷新、类别切换与翻页保留范围；“清除筛选”明确解除。不按同名步骤、同项目或最新轮次推断。根准备通过 `stage_run`，装配通过 `ProductionAssemblyLink`，关联异常任务仅通过真实Attempt关系筛选；同一装配多集关联不重复计数，无关联不猜归属。借用生产来源的独立Smoke不属于该轮生产。不存在／不可见的范围返回空列表与零统计，不扩大权限或隐式恢复任务。
- 部署“能力烟测”的最近任务链接复用`category=smoke&source_id=<latestAttempt.id>`查询原准备，不增加详情API或把验收快照／部署／策略ID当作任务ID；缺少真实任务标识不生成链接，未命中／无权限不扩大查询。该入口只读，原Smoke权限、状态、费用及快照判定不变。
- 列表直接返回 `{count, next, previous, results, counts, read_at}`，不是详情 envelope；`counts` 为 all/production/asset/smoke，应用除类别以外的相同筛选，`count` 为当前类别数量。行只含标识、类型、原状态、项目／版本、原调用、时间、费用及关联异常，不携带完整输入或输出。根准备只列一次，辅助调用不另算根任务；未关联准备的 GenerationTask 以关联异常保留，不能被当作可独立执行入口。
- `GET /api/v1/task-center/options/`：返回服务端类别、状态、操作目录及当前可见历史涉及的项目／精确实例版本选项。`domain=all|projects|execution_versions`、`search`、`page`、`page_size`（25，上限100），项目与版本各有独立 `pagination`；禁止任意 key 输入或猜测下拉来源。
- `GET /api/v1/task-center/{kind}/{id}/`：`kind=preparation|generation_task|assembly`，按原拥有者／管理员权限读取详情，不可见404。生产准备可用 `attempt_id` 选择属于该准备的历史调用，不能跨准备读取。`call_page/call_page_size`、`output_page/output_page_size` 各默认1/25，上限100；调用分页含主／辅助角色、原调用ID、准备ID、状态、时间与重试来源。
- 详情含 `input.state/submitted/resolved/parameters/prompts/prompt_trace/field_batch_trace/media_trace/api_mapping/native_input_trace/system_message_trace/notes`、`output.state/result/text/media/notes`、`validation`、`calls`、`links`、`purpose`、顶层`outcome`、原状态与冻结摘要；outcome复用准备API的状态说明合同，生产历史页仅取精确选中attempt，独立素材取原根任务，Smoke/无准备为null。调用成功不能替代输出落库或校验通过。参数逐项保留实际值及来源，提示词逐角色／通道读取冻结传输内容，不重新编译当前策略。未冻结没有最终提示词，校验异常明确为 invalid，不用空结果冒充成功。人工补全来源经原确认记录校验；历史拒绝不删除已生成正文，也不恢复采用权限。实际费用未知为 null，不用0替代。
- Smoke 尚无结果摘要且保留模型初始空对象（`result={}`、schema 1、2字节）时，非成功任务的 `output.state=unavailable`、`output_count=0`，不因默认2字节误报校验失败，也不推断没有外呼；失败或不确定状态仍显示原任务诊断。已成功但缺摘要、非空结果缺摘要或大小／Schema异常仍为 `task_center_output_invalid`，不修改原记录或补造结果。
- `input.system_message_trace`核对原生影视规则／剧本生成／剧本结构化／独立规范化及字段生成／翻译的新请求system分段；其他能力或未冻结为null。原builder拼接消息时将`system_message_layout`写入原请求（原生业务在`operation_plan.plan`，字段协议在transport顶层），随原请求摘要冻结，只保存角色、消息索引、总字符数／SHA256及分段key／start／end／SHA256，不复制正文、不发送给供应商、不增加查询或外呼。详情state为matched／mismatch／unavailable，rows含label／source／start／end／state（matched或mismatch），最多11个登记段。位置是原文Unicode字符的0起始半开区间，页面显示从1计数的范围，包含标签与换行；不是字节或Token，脱敏文本可能改变可见长度。逐段及整条摘要、无缺口覆盖、协议／角色身份均须对应，正文变化报mismatch，缺失或损坏布局报unavailable；不会用当前代码重建旧请求。平台协议、字段规则、输出示例／允许值／质量建议、策略通道与可选规划分开解释；标签说明不授予修改平台协议的权限，不证明模型收到、遵循、Smoke通过或已审核。正文及原任务状态始终保留。
- `input.native_input_trace`在已冻结原生影视规则／剧本生成／剧本结构化／独立规范化提供，其他能力为null。复用已授权及验摘要的原计划和messages，零新增查询、写入和外呼。按登记路径严格比对JSON类型和值：`state=matched|mismatch|partial|unavailable`；rows含label、source（原operation_plan路径或已登记协议投影，注明转换）、destination（原user JSON位置）、state（matched/mismatch/unavailable），不重复正文。unmapped_count为未解释的user顶层与prompt_profile子字段数，不逐个回显未知名称或静默忽略；0不表示system全文已校验。身份不一致、缺计划、角色异常、重复JSON键及非有限数均明确不可核对，保留原消息和任务状态。已知源缺失不补空串，目标缺失与null／false／0分开；历史structured_fields仅在确实随消息发送时核对，不恢复旧构建路径。最多10个登记位置，结果有界内联，不加入大正文分段目录。不重建请求、不检查system平台正文、不证明送达或Smoke通过。
- 独立规范化按冻结v1条目复用纯保护词转换，完整比较items数组的身份／顺序／类型／语言／正文／上下文／保护词和策略数据；返回骨架仅是保序request_id及空translation模板，不是模型输出。task核对已登记`text.prompt_normalize/v1`的固定翻译类型；策略system通道对应user中的`system_constraints_as_data`，不提升为system指令。结构字段转换只读遍历限制为64层，超过或出现保留占位符／结构键冲突时不可核对，不改变执行状态。重复或空request_id、重复／错误保护词、非独立的额外generation context均不可核对；不使用当前安全阈值、策略或请求builder重建历史。整批一行不等于只检查首项，嵌套额外字段参与精确比较，条目再多也不展开成无界响应。
- `input.api_mapping`为受限REST的声明字段解释；其他协议／未冻结／输入不可读为null。`state=verified|unavailable`仅说明精确部署合并配置与原`target.configuration_sha256`是否一致；含deployment_id、configuration_sha256、code、message及rows(key、field、state)。field为供应商JSON顶层字段，无映射为null；行状态included（按规则进入正文）、omitted（未提供／null）、unmapped（标准字段存在但无映射）。只读取原部署一次，合并服务连接与部署覆盖并验摘要，不解密、外呼或读取最新实例；部署退役仍可核对，精确资源缺失／配置损坏／摘要漂移返回unavailable且rows为空，原请求与实际状态保留。与适配器共用纯字段投影，0／false／空字符串不当缺值，model/prompt必填等请求校验仍由原主路径执行。仅返回已登记9个标准字段的名称／状态，不复制值或发送正文；结果有界内联，不在content_field分段目录。请求头、端点、凭据、生产派发时设置的幂等标识不回显。映射归属模型部署传输配置（覆盖服务连接配置）；不新建可编辑配置或历史事实。页面默认折叠，明确不是已发送正文、送达、执行成功或Smoke证明；没有供应商真实样本时仅有隔离回归证据。
- `input.field_batch_trace`仅解释`prompt.fields.generate|prompt.fields.translate`的`text.prompt_fields/v1`：先核对原resolved_inputs.inputs.operation_inputs.field_request的规范JSON摘要与transport.field_request_sha256，再与唯一user消息的完整JSON精确比较，拒绝重复JSON键，不混同布尔和数字。`state=matched|mismatch|unavailable`、说明、父策略ID/SHA、批次SHA、实际JSON位置及逐项`items`；其他操作、未冻结或延迟加载为null。条目保留request_id/unit_key/field_key，区分generation_source_selector→source_text、额外context_selectors、原生成说明／语言／参考事实数量，以及父策略声明的审核后source_selector／channel／输出语义。新内部批次payload v2通过context_refs及reference_facts中的_ref引用shared_facts，同单元相同资料只发送一次；解释器按原表展开计数。历史v1只读原context_facts及内联reference_facts，不转换或授权重发。源正文不在解释里复制，字符数非token；完整内容查看原消息。该核对不读取当前策略／配置、重新生成或进行当前准入；对外仍走原安全脱敏。摘要／结构缺失明确不可核对，实际消息冲突保留原批次并提示不一致，不改任务结果。system正文未由此核对，不能以批次匹配代替Smoke、审核、采用或证明上游对象当前有效。大解释复用`content_field=input.field_batch_trace`的64KiB内联与分段读取，不新增写入入口或历史快照。
- `input.media_trace`为同一详情的Comfy只读媒体解释；其他驱动、未冻结或延迟加载为null。`state=recorded`仅表示原合同可读取，含工作流版本／传输请求摘要（不是准备的request_sha256）／精确根任务（未创建时null）、逐次用途`items`和共用原图`nodes/edges`。用途保留input_key/item_key、素材或上游产物ID、origin、purpose、paired_to、来源事实及字节SHA、提示词参考标记、声明端口、原图媒体占位符的精确node/input_path；不按Asset ID合并重复用途。`mode=workflow_input|embedded_audio|context_only`；内嵌原音明确共用父视频入口和回执，仍显示自身声明端口；上下文用途不附加上传或图端口。冻结预处理合同／配置修订与原回执的处理元数据分开展示，`receipt_state=recorded|missing|not_applicable`，来源／提交字节SHA不代表模型已收到或成功。图连接只是冻结拓扑，不冒充节点执行顺序、内部算法或运行回执。沿原read_execution_workflow_receipts核验当前所选Attempt对应根任务的owner/root/target/source/metadata摘要；损坏使用现有task_center_input_invalid和input.error，不降为空成功，也不改实际任务状态。无当前素材查询、图重编译、文件读取、预处理、上传或外呼；路径／凭据经原public_value脱敏；大内容复用content_field=input.media_trace的64KiB分段读取。历史缺失回执保持missing，不补造。
- 已冻结聊天请求的`input.notes`包含协议规则与配置归属说明；按原定义的operation/output与登记适配器精确对应，消息缺失、重复、非文本或顺序不符不套用规则，未知版本明确不可说明。支持起草、辅助字段生成／翻译、影视规则、剧本生成／结构化、独立规范化；不查询当前策略、不重编译历史、不外呼。说明不是原文逐段校验或送达证明，具体原文仍为`input.prompts`，逐通道比对仍归`input.prompt_trace`。媒体请求没有messages时不制造system/user解释。复用原notes数组，无新API或持久化事实；已冻结详情的额外说明默认折叠，非冻结／异常说明仍直接展示。
- `input.media_trace.scalar_wiring`在原任务权限范围内读取冻结target.workflow.id对应的精确工作流版本（含退役版本），核验原图／输入合同／绑定／模型需求及执行合同摘要后，将标量语义与原请求声明node/input的类型和值比较。3次批量版本／绑定／需求读取，加1次精确冻结Projector读取；安全上限复用请求内解析缓存，不读latest、当前实例或素材。`state=matched|mismatch|partial|unavailable`；`contract_sha256`为冻结工作流合同SHA；`rows`逐项含semantic_key、value_type、node_id、input_name、class_type、state、message，行状态为matched/mismatch/omitted/unavailable。一对多绑定逐项保留，空字符串／0／false不当缺值，布尔不等于数字；可选字段只有语义与端口均省略才omitted。覆盖string/enum/integer/number/boolean，排除媒体／模型文件和内部output.prefix，不复制原值。版本缺失／摘要漂移明确unavailable但保留原请求和媒体事实；动态移除／替换节点不可核对，不按相同正文寻找新节点。结果不是整图转换、节点执行、送达或Smoke证明；不修改历史、重新编译或外呼，沿原public_value和64KiB分段读取。页面入口为“工作流接线与媒体回执”，接线来源是精确工作流版本随导入冻结的语义绑定，不是任务页可编辑配置；结构变化须重新导入新版本。
- 参数API保留逐层原记录。页面仅将完全同键、同JSON值的展示归组，保留全部来源；同键不同值逐项展示并提示记录冲突，不选择覆盖值、不改任务状态。不同工作流节点按精确graph路径保持独立，不按节点类名合并；不同语义Key不猜测别名或槽位映射。延迟加载的参数完整校验后才归组，不改写历史请求或伪造来源优先级。
- 提示词大小为页面从已有`input.prompts[].text`计算的展示指标，不新增API事实字段：逐段及合计按Unicode码点／UTF-8字节统计安全过滤后正文，不含角色、JSON包装和传输开销，不等于原始请求大小、token或费用。整段非空展示文本精确相同才列出角色／通道，包含空白且不归一化，不做局部／语义匹配，不因相同正文删减总量或原记录；安全过滤可能让不同原文呈现相同。`input.prompts`待完整加载时不统计；明确空段与无记录区分。所有提示仅解释可查看副本，不写回请求或任务状态。
- 详情`input.prompt_trace`只读原冻结`profile_compilation.assembly`：`state=recorded|unavailable`、说明、精确策略ID/SHA、`fields`与`channels`；未冻结或大字段待加载为null。字段显示原来源标识、通道、默认值／审核值标志，来源位置和对象仅按冻结实例的显式profile_input接线关联原input_provenance，不按同名键猜测；默认值或已审核辅助字段不冒充第一层项目输入。通道显示原始Unicode字符数（不是转换后长度、token数或费用）、目标及比对状态`matched|mismatch|unavailable|omitted`。支持影视规则`text.visual_rules/v2`、剧本生成`text.script_generate/v2`、结构化`text.script_structure/v3`的user JSON通道字段及system配置段；结构化另核对冻结计划是否声明末尾PLANNING约束。`text.prompt_draft/v1`核对整条消息。独立`text.prompt_normalize/v1`按冻结条目、复用同一纯保护词转换后核对user JSON；系统通道目标为`prompt_profile.system_constraints_as_data`，不是system角色，首尾空白不删除。缺计划、合同冲突或未支持协议明确不可核对；不按相似正文猜测。不会重新调用编译器、读取当前策略、复制正文或改写历史；不一致仅报告证据冲突，不修改原任务结果。system的协议正文不是策略字段，其他业务输入和规范化的structured_fields仍查看完整消息；不将局部通道核对宣称为整条请求完整追溯。字段采用来源转换时显示原语义原语，但尚不展开完整动态素材转换。
- 原生生产、独立素材及辅助文本详情的 `output.result.provider_response` 只读原Attempt或AssetGenerationJob的 `response_received` 用量回执：`state=recorded|unavailable|unverifiable`、原`task_id`、`observed_at`（未记录/损坏为null）、`usage`。usage仅含服务商明确返回的非负整数 `prompt_tokens/completion_tokens/total_tokens`，缺项未知；明确返回0与未返回不同，不用输入长度或模型价格回填实际费用。生产回执在原worker领取锁和结算事务中追加一次；素材/辅助文本在原任务领取锁内、响应业务解析之前追加一次，保留成功、解析失败、非法响应编号及取消后的晚到响应用量。适配器明确拒绝响应且提供安全用量时同样保留；不确定传输异常不当作完整响应。审计保存失败不发布成功候选、不自动重发，失效领取不能覆盖任务。不改变候选采用、Smoke或费用事实，系统回执不进入人工消歧选择项。重复/损坏/跨请求或不支持的合同版本均明确无法核验，不覆盖原候选；历史未记录不补造。页面“输出与校验”同步显示中文用量或未知原因；Smoke原用量仍来自原验收证据。
- 媒体策略通道沿同一`input.prompt_trace.channels`解释：只按冻结定义的`semantic_key`核对ComfyUI `generic.comfy` v1–v3的`payload.compiled_workflow.resolved_semantics[key]`或受限REST的`payload[key]`，不是另一个参数／字段目录。工作流编译身份、合同摘要及输出身份须与本次冻结目标相符；声明缺失、类型冲突、重复目标或未知协议为`unavailable`，实际位置缺值／非字符串／正文不同为`mismatch`，明确空字符串仍可核对。此层不反推节点端口、不读取当前版本、不验证供应商重映射或送达；未知适配器仅保留通道不可核对，不抹去完整字段来源记录。纯通道比对零查询；对外展示仍经现有脱敏器核对当前安全边界。
- 单字段超过64 KiB时返回含字段SHA256的 `deferred_fields`，明确标注待加载；通过同一详情的 `content_field`（input.submitted/resolved/parameters/prompts/prompt_trace/field_batch_trace 或 output.result/text/media）、`content_offset`（Unicode字符）、`content_limit`（4–65536字节，默认16384）读取 JSON 文本分片。返回offset/next_offset、总字符数、字节数及SHA256，客户端固定调用身份、完整拼接并校验后展示。刷新只在请求、调用、产物页及字段摘要一致时保留已加载正文，不混入新重试，不把截断片段伪装完整事实。指定历史调用链接携带 `attempt_id`，历史视图只读；处理准备需返回最新调用。
- 任务中心接口均为GET，登录且启用的用户可访问自身生产／素材；Smoke只限管理员。所有响应 `Cache-Control: private, no-store`。提示词和结果仅在有权详情返回脱敏副本，隐去秘密、内部地址及本地路径，不进入列表或普通审计日志；正文加载不调用模型、不修改原事实。
- `actions.cancel_smoke` 仅投影原取消入口允许的状态；前端仍使用原 Smoke 取消API。素材取消／重试／消歧与生产运行动作沿用既有API及权限，不在任务中心增加统一旁路写接口。程序执行／格式校验通过不等于内容已人工验收或采用。历史未保存的失败正文与诊断不凭空补齐。

任务中心产物详情除当前`generation_attempt`外，还核对其Run、Stage与GenerationTask归属；任一关系冲突则`output.state=error`、`validation.error_code=task_center_output_invalid`，不返回该页结果/媒体，不修复原记录或从其他调用补齐。文件可读性仍由原签名媒体入口核对状态、拥有者、受管路径、实际非空文件与大小/SHA/MIME；详情有登记不代表文件可读。准备中的诊断跳转沿原任务中心，生产链接固定`attempt_id`；独立素材/Smoke须与该准备的任务ID一致，否则不提供替代跳转。

### 逐次素材使用的处理摘要

`input.media_trace.items[].processing_summary`仅解释当前有权读取的原暂存回执和冻结合同。`state=not_applicable/declared/recorded/partial/unverifiable`分别表示上下文不处理、仅声明、已读取原记录、记录缺字段、处理元数据无法完整核对；不是任务成功状态。`label/description`与作者表单共用适配器目录，`configuration_source`说明模型模式合同和冻结媒体处理配置的分工。

`declared`只列冻结合同的时长范围、目标FPS及源FPS要求；`observed`只列原元数据，适配器有效值与处理后探测值分别标注。各行含`key/label/unit/source/state/value`，行状态`recorded/missing/unavailable/invalid`区分有值、缺字段、明确null和类型／值无效。非recorded行的value为null；明确0和false保留，毫秒／fps／帧单位不混用。元数据须匹配原staging版本、适配器、用途及冻结配置摘要，否则不解读数值，原证据仍保留；缺值不根据声明回填，不读取当前配置、不重新FFprobe或执行转换。

`byte_identity=same/changed/unavailable/not_applicable`只比较原来源与提交字节SHA，不代表模型送达。`shared_with`为内嵌音频共用的视频使用标识，不冒充独立音轨处理；直接原字节上传没有探测值时保持空观察列表。原`processing/processing_metadata/receipt/media_profile`仍可按需核对，沿原脱敏与64KiB分段读取，没有新持久化数据、查询或写接口。

### 逐次素材使用的声明端口路径

`input.media_trace.items[].port_routes`逐次保留同一素材的不同用途，不按Asset ID合并。每项含`target_key`、`destination`（`node_id/input_name`或null）、`state`（`connected/disconnected/unavailable`）、`entry_node_id`、`edge_indexes`和`message`。索引从0引用同一`media_trace.edges`，是一条最短路径的证据，不是全部路径、节点算法或运行顺序；直接写入冻结声明的加载端口时可连通且无中间边。未连通不等于任务失败，目标无法核对时不推测当前映射。

节点端口来自原执行定义；语义端口和动态素材组的顺序／归并目标来自精确冻结工作流，与`scalar_wiring`共享原四次有界查询及摘要核对。原图搜索保留输出索引和最终输入端口，防止通过同一节点的其他输入或循环误判。无重新编译、当前素材查询、文件读取、上传或真实调用；不把图连通当作已送达、Smoke通过或人工采用。

### 工作流完整冻结图的输入差异

`input.media_trace.nodes/edges`覆盖完整冻结图，包括无参考素材的文生图、上游提示词／模型节点和未连通节点；`items`为空不代表图为空，节点存在不代表已执行。逐次媒体`port_routes.edge_indexes`引用此同一完整边列表，只说明从该素材入口到声明端口的路径。节点除`node_id/class_type`外还返回`origin`、`inputs`和`removed_inputs`。`origin`为`original/added/replaced/unavailable`，分别表示精确原图同ID同类型、请求新增、同ID类型替换、原图基线不可核对。只按精确ID比较，不按名称猜测继承关系。

`inputs[]`包含`input_name`、`change`（`unchanged/changed/added/unavailable`）、`kind`（`literal/connection/media/redacted`）和`value`。保留0、false、空字符串和null的区别；connection值为原节点ID与输出索引。媒体占位符以及敏感／文件路径值在服务端隐藏，返回null和明确kind，不可当作真实空值。`removed_inputs`只列原同类型节点中已移除的端口名称，不回填旧值；类型替换或缺少基线时不得标为未变化。

比较复用已核验的原图与冻结编译图；只做一次批量脱敏，配置查询不随端口数增长，不新增逐节点数据库查询或持久化。仍沿用任务详情原64KiB上限和受控完整读取。页面值是编译配置证据，不是节点内部算法、执行顺序、实际发送正文、送达或成功证明。

### 当前调用的本地提交证据

任务详情的必填 `submission` 只投影同次权限校验、同次选择的原调用对象，不额外查询其他重试或最新记录。支持 `generation_attempt/asset_job/deployment_smoke/workflow_smoke`；无原调用或不适用时 `state=unavailable`，不能解释为从未发送。

- `state=recorded/unavailable/inconsistent` 是字段可读性／一致性状态，不是任务或送达状态。
- `started_at` 是原 `submission_started_at` 的本地边界时间；`registered_at` 是原 `submitted_at`，可能来自worker、恢复或人工核对，不能当作网络发送时间。null只表示原字段未记录，不由成功状态补值。
- `identifier_state=recorded/digest_only/not_recorded/unavailable/inconsistent` 只呈现请求标识存储和原摘要核对状态。标识可能预先分配，不是供应商接收证明；不返回标识正文或摘要。
- 原时间倒序、类型异常或标识摘要不一致时明确 `inconsistent`，保留可读字段及说明，不改任务状态、不自动重试、不补历史。`message/notes` 展示证据边界。

入口为任务中心详情“准备与调用 → 提交证据”。上述本地标记不包含逐字节HTTP正文或远端送达证明，投影本身不新增存储或查询；原生生产文本、素材文本与文本Smoke附加的发送前序列化审计及有界读取见下方“HTTP序列化证据”。冻结请求摘要不能替代HTTP正文摘要，任何详情读取均不触发调用。费用、Smoke结果和人工采用仍分别读取原事实。

诊断沿原任务、提交标记、响应文件和产物分别读取，不另存一套“阶段状态”。下表是证据解释，不是新增状态枚举，也不能仅凭任务 `failed` 推断失败发生在哪一步：

| 所处环节 | 可核对的原事实 | 不可推断的结果 |
| --- | --- | --- |
| 准备／发送前拒绝 | 原冻结输入及校验错误；例如 REST 请求摘要错误 `async_attempt_invalid` 在提交前终止 | 冻结完成不等于已发送；缺提交标记也不能证明历史从未外呼 |
| 提交／回执未知 | `submission.started_at`、`registered_at`、原标识存在性；`uncertain_remote_response` 明确结果未知 | 不能补造回执、认作远端拒绝或自动重发 |
| 远端失败 | 原 REST poll 的 `remote_generation_failed`，或 ComfyUI 终态 history 与 `execution_workflow_failed` | 只有 poll 错误不等于已保存 collect 正文；远端成功也不等于本地成功 |
| 解析／输出校验 | 原响应文件及其校验诊断；例如 `provider_response_invalid` 或 `execution_output_invalid` | 正文可读不等于已有候选；不能用下一次成功替代本次失败 |
| 保存／登记 | 原存储错误 `execution_response_storage_failed`、实际文件和确切产物登记 | 未保存不能展示为已有正文；有回执不等于有素材文件，登记仍须通过原签名文件检查 |

HTTP序列化审计只按其支持的原协议呈现；ComfyUI history／REST collect JSON 仍是驱动对象而非逐字节传输证据。未支持或未保存的证据明确不可用，不从新配置重算，也不作为协议已验收通过的依据。准备页诊断链接固定确切原调用，查看和返回均为读取，不触发上述任何写入阶段。

### 独立素材文本原始返回与人工补正

独立ComfyUI及受限异步REST图片/视频/音频的收集回执复用下述原响应存储及两处详情入口。收集前核对原提交ID/摘要；适配器回执必须关联确切原request_id，不用Preparation ID替换REST远端ID，也不补填缺失或错误身份。`diagnostics.body_kind=collect_receipt_json`表示实际驱动collect对象的JSON，不是远端HTTP原始字节。REST的`source_response`保留实际收到的解码JSON，外层request_id仅表示该次GET的关联身份，不宣称远端正文回显了ID；缺产物、错误条目或超界状态等驱动校验错误也先保留已收到对象，再报告失败。网络失败或没有收到正文不构造回执。

原响应文件先保存，再校验/物化；空输出、错节点、重复主输出、REST清单错误或本地归档/探测/登记失败仍可读回执，不能据此判定已有Asset。已知远端完成但本地失败与提交结果未知分开，均不自动重新提交；费用冲突沿原人工核对状态，已核对费用不被新返回覆盖。任务`output.text`及准备`text_response_review.original_text`均按对象键递归脱敏；`original_sha256`仍为私有原文件摘要，非脱敏副本摘要。回执始终`editable=false`、补正409；取消/忽略/失去领取不挂接晚到回执，原取消费用处理不变。保存失败明确终止、不伪装有正文；已保存回执后的本地归档中断沿原陈旧领取恢复读取同一文件，不重新collect或submit；文件损坏显式失败，不重收集覆盖。独立链路与下述生产工作流回执共用存储，不代替M4整体验收。

生产工作流媒体沿原`GenerationTask/GenerationAttempt`保存实际终态history，`response_source=comfyui_history`，原JSON封装为`{schema_version, request_id, history}`。外层ID只表示查询关联，不补造远端回显；请求身份为Attempt.pk，输出文件命名空间仍为Preparation.pk。显式远端失败及成功后的空/错误输出均可查阅，未完成history不登记为终态。回执在媒体探测/归档事务之前单独持久化，归档失败不撤回收到的正文；原陈旧running领取可用同一回执恢复本地归档，不重查history、上传、释放资源或POST。已落本地终态失败不因有回执自动重试。取消与丢失claim均不新增回执，原取消/失败结算继续；存储写入、文件丢失或摘要损坏显式记录存储错误，不伪装模型输出无效或回退远端。

运行因其他单元等待/阻塞不丢弃原提交回执；原归档规则拒绝时显式落本地失败，不凭正文发布候选。

生产REST Attempt沿同一响应表和私有文件保留实际collect对象，`response_source=async_rest_collect`；身份为原`provider_request_id`，不能替换成Attempt或Preparation ID。只有原提交已知成功且领取有效才挂接；驱动已经收到但拒绝的正文仍保留，原校验错误不会变成候选。原在途归档中断恢复读取同一回执，不重复collect/submit；原费用结算幂等，缺费用仍未知，取消晚到回执不挂接但明确实付仍走原结算。存储损坏明确`execution_response_storage_failed`，不重收集覆盖。这里保留的是collect响应，不把只有poll错误或网络异常当作已有正文。

原`GET /api/v1/task-center/preparation/{id}/?attempt_id={id}`按确切生产Attempt展示脱敏回执及实际已登记媒体；只有回执时`output.state=available`表示正文可读，不改变任务失败状态或虚增产物数量。媒体回执不进入准备级文本补正编辑器，历史重试的多份回执分别归属各原Attempt；补正接口拒绝媒体正文。原独立聊天或ComfyUI文本详情仍以`output.text`展示保留原文、`output.result.response_review`展示原请求身份与解析诊断，`candidate_text`只在有合法候选时出现。读取无写入或模型调用，列表不加载正文，详情沿原分段/响应上限，不开放文件URL。

成功、解析失败及明确输出额度耗尽时收到的部分正文共用私有响应存储，并核验原Job/Preparation/请求及文件大小、SHA256。收到空文本与未收到文本分开：前者可读取零字节原文并保留失败状态，后者`output.state=unavailable`，不从错误消息、输入、推理或工具调用构造正文。提交不确定、只有请求容量拒绝或内容过滤错误不作为已收到正文；取消/失去领取的晚到结果不生成活动正文或候选。原文文件损坏/缺失明确`output.state=error`及`validation.error_code=task_center_output_invalid`，不回退显示候选冒充原文。

ComfyUI文本沿原worker的submit/poll/collect路径留存。收集前核对原提交标识/摘要及提交事实，收集回执必须指向原Preparation的请求标识，正文只能来自冻结的唯一文本主输出节点。收集器不裁掉首尾空白；`text`/`strings`数组仍按原协议换行连接，这不是逐字节HTTP回执。收到空白正文、大小/摘要不符或正文解析失败时保存实际节点文本及安全诊断，任务仍失败、不生成候选；前两类以`comfyui_text_output_invalid`诊断标记。缺正文、节点错误/歧义、请求标识不符不猜测归属。原64 KiB收集上限及正式文本存储边界仍有效，超界明确失败、不截断伪装已留存。私有响应诊断包含`response_source=comfyui_collect`、`provider_request_id`和`output_node_id`，读取时重验；保留远端成功和本地失败的区别，不新增费用或审核事实。

原素材生成目录与创建共用业务类型到输出类型的映射：提示词候选及`media_type=script`匹配执行器`text`输出，图片/视频/音频仍必须同类型。剧本素材正文通过校验后沿原文件物化器创建`Asset(kind=script)`，其受管文件与保留原文是不同用途的事实；保留原文不裁剪，素材文件按原规则规范CRLF换行。不要求新增名为`script`的传输输出，也不将任意文本写成图片/视频/音频。`response_contract_valid`只表示正文合同通过；是否已有素材/候选仍读取原任务结果，不能从此诊断提前判定成功。

人工补正复用登录态下的原`GET /api/v1/execution-preparations/{id}/`和`POST /api/v1/execution-preparations/{id}/complete-text-response/`，不另建队列或伪造GenerationAttempt。任务中心入口是“输入 / 输出与记录”→“准备与调用”→“模型返回与处理记录”→“校验并保存人工修订（不调用模型）”；素材任务也复用同一执行准备工作台。重试另行创建新任务，会再次调用模型，补正不会调用或自动采用。

- 请求仍为`expected_preparation_revision`（正整数）、`review_id`（UUID）、`original_sha256`（64位摘要）、`text`（按原冻结协议的完整正文）；不用当前新版本重新解释旧请求。返回200为原准备详情；正文不合约400、原修订/归属/状态冲突409、不可见404，原拥有者或管理员可操作，登录/角色边界不变。含NUL的正文返回原`null_characters_not_allowed`错误码，明确提示移除控制字符，不误称为空字符串；失败不保存修订、不新增调用，页面保留编辑内容供修正。
- `text_response_review`增加`asset_job_id`（素材路径非空）、`attempt_id`（生产路径非空），二者恰好一个；另有`corrected_text/corrected_sha256/confirmed_by`，未确认全部null，确认后返回经私有文件摘要核验的真实人工正文及确认人ID；`confirmed_at`是实际确认时间。列表不加载这些正文。原生产补正同样返回这些修订字段，但不改变其候选/采用职责。
- 只有已收到并留存正文的明确校验失败或输出额度耗尽任务可补正。取消/忽略、未知、仍被领取、已产出结果、已有重试或人工消歧的任务只读；字段子任务仍返回父准备人工填字段，不把人工修订记为模型字段结果。成功模型返回也只读。正文缺失不构造补正入口，文件损坏明确报错。
- 原根锁内保存一次性人工修订/确认，再复用原文本物化器写入提示词结果或剧本Asset，并增加准备修订。原始正文、冻结请求、原远端状态和未知费用不改；同正文重放幂等，不同正文冲突。失败回滚业务行与确认，并清理仅本次新建文件，原文不删除。没有新模型调用、自动采用或下游推进。
- 剧本Asset标记`metadata.source=human_correction`及`text_response_correction`（review_id、原文/修订摘要、确认人和时间），文件摘要为`corrected_file_sha256`，不标为模型生成摘要。任务详情`output.result.human_correction`单独展示修订和确认，`output.text`始终是模型原文；文本结果仍由原Job保存。人工补正不产生模型成功容量样本或额外用量、费用。

开发验证使用隔离数据库/临时文件和假Provider；完整任务中心页面通过真实隔离API验证，原模型正文与人工修订分别读取，任务与调用数量按库内事实核对。不是现用任务补正或模型效果验收；现用迁移、代码加载和模块交付状态统一见详细设计第18节，不从源码可用推断已经切换。

### Chat请求字段组包说明

任务中心详情“输入与实际提示词 → 请求协议与配置归属（只读说明）”沿用 `input.notes`，不增加写接口或参数配置入口。DeepSeek、OpenAI-compatible与本地Qwen的准备编译和实际调用共用 `services/chat_body.py` 的组包函数；新准备在原冻结transport内保存 `chat_body_layout`（schema_version=1、adapter、fields、transport_only），随原请求摘要核验，只记录字段去向，不复制正文、地址或凭据。

| 冻结来源 | Chat JSON位置 | 配置归属／含义 |
| --- | --- | --- |
| `interface.model_identifier` | `model` | 所选模型版本的模型标识 |
| `payload.messages` | `messages` | 原角色、消息顺序与正文；不再拼接提示词 |
| `payload.chat_request_options[key]` | 顶层同名 `key` | 模型版本的请求选项；移除包装层，0、false、null与省略分别保留 |
| `payload.max_tokens/seed` | 顶层同名字段 | 本次冻结参数；没有时不补值 |
| `payload.timeout_seconds` | 不进入JSON | HTTP客户端超时；传输配置不属于提示词策略 |

字段去向核对不读取当前模型策略或重建历史请求；旧任务无记录时明确缺失，记录异常时不能作为已核对正文。正文仍从原 `messages` 读取。它不是逐字节HTTP正文、实际发送或远端送达证明：JSON序列化仍在HTTP库完成，地址与凭据由原模型部署／服务连接及密钥管理消费。原权限、脱敏、响应大小与失败语义保持不变，不触发真实调用；验证范围见统一计划第85批。

#### HTTP序列化证据（生产、素材与Smoke原生文本）

原任务中心详情 `GET /api/v1/task-center/preparation/{id}/` 的“准备与调用 → 提交证据”复用 `submission.notes`，显示原生产文本、素材文本或部署文本Smoke的 `http_body_prepared` 审计；没有新增写接口、参数选项或日志入口。仍按原任务权限读取，不请求远端。

- 写入方：原生产文本worker、素材文本worker（均含同worker处理的辅助字段）及原部署文本Smoke worker（实例／资源范围），在Requests生成 `PreparedRequest.body` 后、固定DNS适配器交给网络层之前，分别向原 `GenerationAttemptResolution`、`AssetGenerationJobResolution`、`ModelDeploymentSmokeAttemptResolution` 追加一次记录。主请求与准备身份须一致，领取有效、未取消且无重复记录；素材辅助任务同时检查原预算／取消根任务未停止。写入失败阻止本次网络发送，保留原领取恢复规则，不自动重试或释放未知费用预留。
- Smoke系统观察的 `decided_by` 为空，不是人工消歧。新建Smoke门禁、验收概览和模型部署详情仅将三个 `confirmed_*` 人工结果视为消歧；仅有正文摘要的 `uncertain` 仍须核验，不能重新外呼。原人工消歧接口仍只接收／返回人工结果，在原事件之后递增修订，不改任务状态、费用或通过事实，不能伪造系统事件。
- 素材系统观察的 `decided_by` 同样为空；`latest_resolution`、人工消歧输入／输出及人工费用核对只读四种 `confirmed_*` 结论，不把正文摘要当成人工决定或账单。事件幂等键固定为 `system:http_body_prepared`，与同Job下的人工键共用唯一约束；人工请求不能复用该键。系统行的 `request_sha256` 是本次事件内容摘要，事件内的同名字段才是原Preparation冻结请求摘要，不与Job的窄请求头摘要混淆。该内部行不通过人工消歧接口返回。
- 内容：外层固定 `schema_version=1`、`preparation_id`、`request_sha256`；`body` 仅含 `schema_version=1`、`encoding=requests.prepared_json/v1`、真实序列化字节数 `size_bytes` 和 `sha256`。摘要来自HTTP库的原字节，不从当前代码重新组包。原审计的 `created_at` 是记录时间，**不是发送或送达时间**。不保存正文、地址、请求头、凭据或第二份提示词。
- 读取方：详情只对选中原Attempt或素材Job做一条有界索引查询，最多读2条以发现重复；校验原审计摘要、版本、结构及请求身份。素材详情仅对已核验冻结消息请求展示Chat证据，不拿当前部署类型重建历史。无记录、重复、损坏或错配均明确说明，不重建历史、不覆盖原任务／候选／用量结果。原列表不加载此证据，未记录不推断从未外呼。
- 边界：这是**发送前序列化证据**，不是网络发送、远端受理、扣费、输出合法或人工采用证明。仅覆盖上述原生Chat调用，不将ComfyUI或异步媒体请求伪装成已覆盖。原安全DNS／凭据／重定向限制不变；验证范围见统一计划第86–87、90批，隔离网络测试不是新真实模型调用验收。

## 契约校验

```powershell
.\scripts\check.ps1
```

`check.ps1` 会把动态 schema 生成到系统临时文件，完成 Spectacular 校验后，将 UTF-8 BOM 和换行符规范化，再与根 `openapi.yaml` 做逐字节比较。任何漂移都会明确失败，且检查过程不会覆盖根契约；审阅 API 改动后必须显式重新生成并提交根契约。检查同时拒绝重新出现的 `backend/openapi.yaml`，避免形成第二份机器契约。

如需检查动态 schema，可在测试环境启动后比较 `GET /api/schema/` 与根契约的路由集合。不得把临时生成文件提交为第二份契约。
