# 14. CL 开发数据及被替代实现清理

> 原设计与任务编号保持；此模块的勾选仅在本文件维护，不复制到索引。

[总索引](../../README.md) · [当前续接](../../current.md) · [按日期查证据](../../records/README.md)

<!-- source-section 97cc61c49f90 -->
<a id="14-cl-开发数据及被替代实现清理"></a>

## 14. CL 开发数据及被替代实现清理

<a id="141-范围与安排"></a>

### 14.1 范围与安排

代码清理随各模块做，不能全部堆到最后。此处CL专门处理已确认不适配的开发业务数据和模块切换，不是新增清理平台。用户已说明历史费用是模拟数据，**不做费用核实、结算、审计留存或历史兼容前置工程**。

已明确对象：旧Run `8dc9b9e9-44a3-48da-b79f-834c3b4a7ff1`、旧配方`ffcfe442-a9c0-4157-96f4-b99bcd8cfc49`及其来源链。2026-10-07用户明确“保留来源链，取消本期物理删除”，按CR-CL-RETAIN-SOURCES-01保留上述根对象和从属来源，不删除或解关联。项目`9d2b590f-d3c7-4538-aad2-c1ff37528436`及有效剧本/Bible/素材/共享配置均保留；取消/退役不写成已删除。物理删除不再是本期交付条件，也不开发历史兼容层。

<a id="142-细任务与逐项验收"></a>

### 14.2 细任务与逐项验收

- [x] **CL-01 明确目标和关联清单**（P1；依赖：无）。实现/产出：在实际清理阶段只读核对精确目标、独占/共享关联、在途任务及文件归属。验收：清单能解释保留和删除边界，不扫描成全库清空，不再核对模拟费用。**第343批按清单职责完成**：复用328正式两目标preview与[12项引用路径](../../../../.artifacts/batch328/cl-kept-reference-paths.json)、330保留方案；本次[确切根对象回读](../../../../.artifacts/batch343/cl-scope-state.json)确认Run8dc9b9e9已取消、配方ffcfe442已退役，所属21任务/38调用均终态，12份有效共享对象仍在。目标仅原Run/配方及列明独占关联，项目/镜头、10份共享Asset及媒体文件、M8配方81bd3a6f和ScriptRevision fc742929来源保留，不解关联或扩大删除。第343批清单已能说明边界，当时CL-02保留方案待决定；第348批用户已决定保留来源链、取消本期物理删除，349完成CL-04核对，不再待决定。各批删除0，未把保留称为物理删除，原preview不是未来删除许可。
  - **第302批CL只读盘点及两处代码收敛（非清理完成）**：移除原清理预检以未知实际费用阻断已取消开发Run的旧条件，保留确切对象、共享引用、在途/结果未知、备份摘要及删除边界；退役资源选择测试中的shot.detailed_description/request.idea与旧Smoke输出合同夹具，改用当前输入槽位/确切输出版本，不增加兼容。36项隔离测试通过，未删现用记录或文件；迁移单列覆盖和关联盘点缺口见18.3，CL-01～04仍不勾选。
  - **第291批旧提示词代码退役（非数据盘点/清理完成）**：按290已确认的正式writer可达性，移除旧target_prompt_contract专属投影、生成分支及测试，旧合同明确拒绝；当前翻译及显式独立字段协议共用校验保留，仍有效的策略约束测试改用现行作者/合同入口。实际验证、失败与修正证据见18.3。本批不访问现用数据、不迁库/加载服务、不清理记录/文件，CL-01～04不因此勾选。
  - **第289批只读代码残留包（非数据清理完成）**：Lagrange定位[人工分镜面板](../../../../frontend/src/components/ManualStoryboardAdoption.vue)仍读取已退役五固定创作字段，主线确认API仅提供独立creative_field_revision后删除该无来源展示，复用原独立字段说明与正式镜头入口；新增反向断言、原来源/冲突/采用测试通过。另在[prompt_normalization_contract](../../../../backend/studio/services/prompt_normalization_contract.py)发现冻结旧shot.core的条件分支，当前writer使用schema_version/items，尚未确认当前可达性；只读发现不等于已删除、迁转或兼容。CL整体保持未勾选，未删现用业务/文件，详情见18.3。
- [x] **CL-02 旧记录处置收口**（P1；依赖：CL-01；原“实际旧记录清理”按CR-CL-RETAIN-SOURCES-01修订）。实现/产出：保留已取消Run、已退役配方及有效来源链，取消本期物理删除，不解关联或扩围。验收：精确根对象和共享来源仍可读，不将退役/保留称为删除。**第348批完成**：用户明确“保留来源链，取消本期物理删除”，[18:16只读回读](../../../../.artifacts/batch348/cl-retention-state.json)确认原Run取消/配方退役、21任务/38调用终态、12份共享对象存在；复用328完整引用路径和330保留方案。实际删除记录0、文件0、解除关联0；无需恢复，原数据/文件仍在。该回读复用旧脚本，scope标签说明物理清理未执行；本项依据新的保留边界收口，非删除成功。
- [x] **CL-03 模块切换数据与Schema清单**（P1；依赖：M1-09）。实现/产出：各模块迁移/清理范围、可保留ID及不可兼容开发配置列表，按依赖顺序附迁移文件。验收：空库和对应隔离升级场景可用；未知/新增删除对象先确认范围，无长期双读/别名层。**第343批按原范围完成**：14.3已覆盖依赖顺序、保留ID/旧列、无隐式回填、配置退役及0234边界；304的0219/0220专项11项、306的0223/0225/0228专项13项、313的0231修订约束及320的0234非空旧Job往返、空库建库组合均有证据。264/320现用迁移、逐行保留与完整性检查及340当前0234回读已分别记录，不新增迁移、不重复测试、不把320的65项组合全称为迁移专项。CL-01/02共享引用与物理删除决定独立保留；清单交付不等于实际旧记录清理或现用逆向恢复验证。
- [x] **CL-04 新链试用前核对**（P1；依赖：CL-02, CL-03, M9-09）。实现/产出：新合同引用完整、旧取消运行不会续跑、旧配方不再作为默认来源的切换清单。验收：只修当前已批准目标，不恢复D01/PG/费用项目；真实服务/迁库是否执行分别记录。**2026-10-07第349批完成**：[只读核对](../../../../.artifacts/batch349/preflight-read-second.log)确认7份已发布配方及其当前引用合法，H3人工实例v2的确切工作流/部署/模型合同有效；旧Run仍cancelled、旧配方仍retired，生效配置未引用旧配方。原创建入口拒绝非published配方，原自动唤醒仅queued/running、终态Run不续跑；[操作前状态](../../../../.artifacts/batch349/live-before.json)无实际生成/Smoke/装配/探测在途，仅两份原M6已采用但未执行的preparing样例。未删除/迁库/重启，不修无关旧H3策略版本。当前目标Smoke历史已过期，另按INT-04新授权执行，不把合同有效当作可调用或试用成功。

CL-01/02可在明确操作阶段独立安排，不阻挡M1设计/实现；本次审核稿不执行它们。如果清理必然影响仍有效共享数据，仅停止相关删除并询问具体范围，不停掉无关的安全开发任务。

<a id="143-cl-03模块切换清单2026-10-06第343批按原范围核对完成"></a>

### 14.3 CL-03模块切换清单（2026-10-06第343批按原范围核对完成）

**现用状态（第320批实际迁移；第325批只读复核）**：Schema已到0234，323加载后续代码。289切换M7及0233时109张studio表逐行不变、仅worker租约随重启更新的证据仍属该批；320在备份后沿正式入口应用0234，未补写历史消费来源。0213–0222由192实际切换，0223–0229由224切换，0230–0232由264在259核定范围内完成；备份、配置退役和逐行保留证据见18.3对应批次。M2/M3/M7已开发交接，不等待用户手动反馈。325复核Popper只读增量结论、0234源码和320日志，不重跑迁移测试、不重迁现用库。下表不等于CL-01/02旧业务清理完成。

本节汇总已有代码及隔离升级证据，不是新增兼容层或批量转换脚本。M1试用0209–0212的授权、备份、重载和样例见5.4；后续实际切换按上方状态及18.3，不再沿用早期“0213/0214未迁”口径。配置及共享引用仍在各自实际操作前刷新，不根据历史截图推断；未实现模块不预填迁移或假设完成。

| 范围及依赖顺序 | 现有迁移与实际行为 | 保留/不自动迁转的边界 | 隔离证据位置 |
| --- | --- | --- | --- |
| M1字段身份与使用值；0198之后 | [0199](../../../../backend/studio/migrations/0199_field_definition_versions.py)保留字段ID并形成显式定义草稿；[0200](../../../../backend/studio/migrations/0200_prompt_rule_value_ownership.py)移除全局必填/默认、改为使用处值；[0201](../../../../backend/studio/migrations/0201_prompt_rule_field_versions.py)增加规则确切定义版本；[0202](../../../../backend/studio/migrations/0202_field_identity_only.py)删除身份上的重复含义列 | 字段身份/有效版本及关联ID保留；草稿不等于发布，旧规则不自动指派定义版本，旧全局值不当作新使用值。旧Profile快照不改hash冒充新合同；definition.default等旧使用方式不作兼容读取 | M1-02记录；test_field_definition_version_migration、test_prompt_rule_value_migration、test_prompt_field_version_migration、test_field_identity_migration |
| M1结构/来源/输出；0202之后 | [0203](../../../../backend/studio/migrations/0203_data_structures.py)新增结构/节点；[0204](../../../../backend/studio/migrations/0204_content_source_structure.py)与[0205](../../../../backend/studio/migrations/0205_business_output_structure.py)增加来源/输出结构关联；[0206](../../../../backend/studio/migrations/0206_structure_node_defaults.py)增加显式默认标志和值 | 结构不会由旧JSON猜测生成，旧来源/输出关联仍空，节点默认不开启；旧reader/parser/hash不重写为schema2。新配置通过字段中心选择确切结构和路径；不适配测试配置在核定后清理，不保留双读 | M1-03/04记录；test_data_structure_migration、test_content_source_structure_migration、test_business_output_structure_migration、test_structure_defaults_migration |
| M1实例/Profile/通用文本选择；0206之后 | [0207](../../../../backend/studio/migrations/0207_execution_input_structure.py)增加实例输入结构；[0208](../../../../backend/studio/migrations/0208_profile_structured_input.py)增加Profile输入结构/来源路径；[0209](../../../../backend/studio/migrations/0209_execution_media_source_policy.py)增加媒体来源策略；[0210](../../../../backend/studio/migrations/0210_artifact_selection_mode.py)区分人工/自动选择及对应操作者 | 新关联为空，不替现有实例/配方/Profile选择配置或发布版本。旧输入路径/媒体来源/合同需逐对象核对，新配方引用确切结构；旧Run/Attempt不自动换输入或重跑。自动选择不得填成人工操作者 | M1-08及G01–G06记录；[实例输入](../../../../backend/studio/tests/test_execution_input_structures.py)、[媒体来源](../../../../backend/studio/tests/test_execution_media_source_policy.py)、[通用文本选择](../../../../backend/studio/tests/test_structured_text_selection.py)、[原配方集成](../../../../backend/studio/tests/test_structured_text_recipe_integration.py) |
| M9基础；0210之后 | [0211](../../../../backend/studio/migrations/0211_asset_reference_relations.py)只新增素材系列/版本关系 | 原Asset ID/checksum/文件/metadata保留；不按名称猜主图/变体/Bible，不复制文件，不自动回填已有生成关系。上传/复用不能标成参考主图生成 | M9-01的0210→0211测试及M9-02至07 API/页面；test_asset_reference_models与本轮test_asset_reference_ingest |
| M2结构化详情及旧估算退役；0212之后 | [0213](../../../../backend/studio/migrations/0213_script_revision_structure.py)增加确切修订的结构化详情；[0214](../../../../backend/studio/migrations/0214_retire_script_resource_advisory.py)停用旧资源估算配置，并为含该键的活动系统/项目配置创建移除此键的后继 | 不解析/回填旧正文，不改旧请求、值或素材；保留其余配置值/凭据关联。新输出、映射、实例和配方须沿正式入口明确维护。192已实际迁移，不以新建内存库升级代替该切换证据 | M2-03及M2-08；[详情测试](../../../../backend/studio/tests/test_script_revision_structures.py)、[配置退役测试](../../../../backend/studio/tests/test_retire_script_resource_advisory.py) |
| M3项目身份；依赖0214 | [0215](../../../../backend/studio/migrations/0215_bible_entity_identity.py)新增空BibleEntityIdentity；项目＋entity_key唯一、key非空，三类实体恰选一个且各自一对一 | 原实体ID/正文不改；不按名称推断身份、不回填历史绑定 | [专项升级测试](../../../../backend/studio/tests/test_bible_entity_identity_migration.py)：不猜身份、不改实体；空新表Schema往返 |
| M3逐源设定引用；依赖0215 | [0216](../../../../backend/studio/migrations/0216_script_bible_setting_uses.py)三类来源绑定改为PROTECT外键并按修订＋实体唯一；移除uk_bible_appearance_source，改为实体＋source_variant_key条件唯一（source_binding非空）；新增空ScriptBibleAppearanceBinding，来源绑定＋variant_key唯一 | 原实体/来源绑定/造型/设定ID及值保留；不创建项目身份或确切设定引用，不转换正文、不自动消解约束冲突 | [专项升级测试](../../../../backend/studio/tests/test_script_bible_setting_uses_migration.py)：旧源行保留、不补事实；同实体新源与重复设定约束 |
| M3重复描述退役；依赖0216 | [0217](../../../../backend/studio/migrations/0217_remove_bible_visual_identity.py)删除CharacterBible/SceneBible/PropBible的visual_identity列及列值 | 保留实体ID、其他字段、素材关系、已有设定/审核与历史快照；不转换为新设定。逆向Schema只恢复空列，旧文字恢复依赖切换前备份 | [专项升级测试](../../../../backend/studio/tests/test_bible_visual_identity_migration.py)：三列删除、不补造事实、逆向空列及实体唯一索引保留 |
| M3设定合同默认值；依赖0217 | [0218](../../../../backend/studio/migrations/0218_bible_setting_schema_default.py)仅将BibleVariantRevision.payload_schema_version的新建默认值改为2 | 原修订ID、v1正文/版本/hash及审核保留，不重标为v2；运行读取拒绝退役v1合同，保留历史不等于兼容使用 | [专项升级测试](../../../../backend/studio/tests/test_bible_setting_schema_migration.py)：默认值调整与往返不重标、不改旧事实 |
| M4变体路线选择；依赖0218 | [0219](../../../../backend/studio/migrations/0219_stagerun_selected_variant_route_index.py)为StageRun新增可空PositiveSmallInteger字段selected_variant_route_index；不是新增数据库索引 | 原Stage ID及已有字段不改，新列为NULL；不复制主图路线选择、不替旧配方/冻结计划补变体配置 | 第304批[专项升级测试](../../../../backend/studio/tests/test_appearance_cutover_migrations.py)3项通过：原主路线NULL/0/2均不复制，旧行逐项保留，已记录迁移重入不改后续选择；空业务库Schema往返。不外推非空逆向数据恢复 |
| M4素材履约；依赖0219及AUTH_USER_MODEL | [0220](../../../../backend/studio/migrations/0220_production_material_fulfillment.py)新增空ProductionMaterialFulfillment，保存单元/需求key、计划/目标SHA、确切素材版本、操作者、Stage引用；Stage＋unit_key唯一，当时selected_by必填 | 原Run/Stage/Asset/素材版本ID保留；不把库内关联自动变成Run履约，不补造选择、生成或审核事实，不替旧计划补来源配置 | 第304批[专项升级测试](../../../../backend/studio/tests/test_appearance_cutover_migrations.py)8项通过：升级空表及旧行保留、版本v1不追v2、必填/外键/Stage＋单元唯一、PROTECT及Stage局部CASCADE、重入保留已写事实、空业务库Schema往返。不验证现用清理或非空逆向恢复 |
| M5镜头语义关系；依赖0220 | [0221](../../../../backend/studio/migrations/0221_shot_semantic_relations.py)新增空ShotCharacterRelation（visible/speaker）及ShotReferenceUse，增加角色/位置、用途key/位置唯一性及确切来源约束；该迁移要求use_key和purpose非空，purpose后由0229放宽 | 原Shot ID/正文/对白、旧人物及参考池不改；不从混合池推断可见角色或素材用途，不自动补关系或适配旧配置 | [专项升级测试](../../../../backend/studio/tests/test_shot_semantic_migration.py)：旧Shot与characters关系保留、不猜visibility；空新表Schema往返 |
| M9文本响应归属；依赖0221 | [0222](../../../../backend/studio/migrations/0222_asset_text_response_owner.py)将ExecutionTextResponseReview.attempt改为可空一对一，新增可空asset_job一对一，约束两类owner恰有一个 | 原响应ID、attempt归属、manifest及文件字节保留；历史AssetGenerationJob不补响应，不伪造原文或审核 | [专项升级测试](../../../../backend/studio/tests/test_asset_text_response_migration.py)：旧归属/manifest/字节不改、不回填、单owner约束；逆向仅覆盖尚无Asset响应的状态 |

第303批补齐0215–0222八项逐迁移索引：Epicurus只读返回后主线复核源码与测试名称，当批未重跑历史迁移测试或重迁现用库。第304批Ohm只读方案返回后由主线新增0219/0220专属单步升级测试，上述11项在隔离SQLite中通过；迁移文件未改、现用库未重迁，192实际切换证据不重跑。空表往返不外推非空回滚，其他迁移测试仍只引用原批次证据。

0223–0234增量边界（278只读盘点；0233的284新增验证及0234的320证据另列，其余通过项引用18.3历史证据，不冒称重新运行）：

| 迁移及依赖 | 实际写入与保留边界 | 已有验证与未验范围 |
| --- | --- | --- |
| [0223](../../../../backend/studio/migrations/0223_bible_image_binding_review.py)，依赖0222 | 三类Bible增加可空审核Stage引用；不从旧Asset猜测或补写审核，实体/素材ID保留 | 第306批[单步升级测试](../../../../backend/studio/tests/test_execution_link_cutover_migrations.py)5项：实体/锚图/M2M原值逐列保留，新审核NULL；悬空引用拒绝、三类RESTRICT及解除后局部删除、迁移重入保留后续引用、空业务库Schema往返。未验现用删除或非空逆向恢复 |
| [0224](../../../../backend/studio/migrations/0224_material_fulfillment_selection_mode.py)，依赖0223 | 原人工履约增加manual模式；人工必须有actor，自动必须无actor；旧来源、版本、操作人不改 | [专项升级](../../../../backend/studio/tests/test_material_fulfillment_mode_migration.py)1项，204已通过 |
| [0225](../../../../backend/studio/migrations/0225_execution_preparation_supersedes.py)，依赖0224 | 新增可空一对一supersedes/PROTECT；不生成替代准备、不取消或改写原请求 | 第306批[单步升级测试](../../../../backend/studio/tests/test_execution_link_cutover_migrations.py)5项：7种准备状态、修订/租约/请求SHA及合同原值保留且supersedes为空；显式替代一对一/PROTECT、悬空父引用拒绝、重入不改替代、空业务库Schema往返。不外推现用恢复或非空逆向 |
| [0226](../../../../backend/studio/migrations/0226_shot_script_source.py)，依赖0225 | 新增空ShotScriptSource，保存原Shot、结构、Stage、坐标和摘要；不推断旧来源 | [专项升级](../../../../backend/studio/tests/test_shot_script_source_migration.py)2项，214通过组包含；旧Shot逐行保留 |
| [0227](../../../../backend/studio/migrations/0227_retire_script_breakdown_policies.py)，依赖0226 | 退役自动切段阈值/目标及道具别名三键；受影响活动配置新增后继，原Revision改superseded/inactive，原值、草稿和其余值/凭据保留。reverse为noop | [迁移函数测试](../../../../backend/studio/tests/test_retire_script_breakdown_policies.py)4项，216通过组包含；不能称所有旧行完全不变或专门MigrationExecutor升级 |
| [0228](../../../../backend/studio/migrations/0228_manual_shot_reference_selection_action.py)，依赖0227 | 原人工Command增加select choice；不新增命令或选择事实 | 第306批[单步升级测试](../../../../backend/studio/tests/test_execution_link_cutover_migrations.py)3项：原六类回执与所有studio旧表不变，select显式字段校验、原幂等唯一约束、重入及空业务库choices往返；choices不是数据库枚举约束，不冒称执行过真实选材 |
| [0229](../../../../backend/studio/migrations/0229_optional_shot_use_description.py)，依赖0228 | 用途文本允许空，仅use_key必须非空；原文字/ID/关系不转换 | [专项升级](../../../../backend/studio/tests/test_shot_use_description_migration.py)1项，224最终组通过 |
| [0230](../../../../backend/studio/migrations/0230_retire_shot_mixed_reference_fields.py)，依赖0229 | 移除旧characters/reference_assets中间关系及reference_bindings列；不删Shot/Asset/Bible/props及新关系，不补造替代关系 | [专项升级](../../../../backend/studio/tests/test_shot_legacy_fields_migration.py)、259副本、264实际切换。26项目77镜、47份旧绑定、99/157旧关系已在授权内移除；不重跑，恢复旧值依赖备份 |
| [0231](../../../../backend/studio/migrations/0231_shot_field_revision.py)，依赖0230及用户模型 | 新增空ShotFieldRevision及修订/请求唯一约束；不搬运旧正文或创建修订 | 243修订/API/内容20项；259/264组合核对新表为空；313新增[单步升级键约束](../../../../backend/studio/tests/test_shot_field_revision_migration.py)分别验证同镜头重复修订与重复request_id各自拒绝、新键及不同镜头同键允许、旧Shot逐行不变/外键有效。未改迁移或现用库，未验有数据逆向恢复 |
| [0232](../../../../backend/studio/migrations/0232_retire_shot_fixed_creative_columns.py)，依赖0231 | 删除narrative/detailed_description/camera/action/sound_effects/music/continuity_notes七列；保留ID、对白/毫秒、新关系/修订及历史翻译，不转换原值 | [专项升级](../../../../backend/studio/tests/test_shot_creative_columns_migration.py)、258/259/264证据；已切现用，不重跑，逆向Schema不能恢复原文字 |
| [0233](../../../../backend/studio/migrations/0233_alter_promptprofilefieldrule_source_selector.py)，依赖0232 | 仅收敛source_selector choices，无RunPython；不删除历史规则/快照，退役选择器包含shot.negative_prompt，与0232物理七列不是同一集合 | 276 sqlmigrate为no-op；284新增[单步升级测试](../../../../backend/studio/tests/test_retired_prompt_source_migration.py)验证0232→0233、重复执行、逆向恢复以及所有studio业务表逐行不变（含旧选择器原值/发布合同），并复验[旧来源拒绝](../../../../backend/studio/tests/test_retired_prompt_sources.py)3项。289备份后正式迁现用、check及无迁移漂移通过；[切换证据](../../../../.artifacts/batch289/cutover.json)区分业务记录保持与运行租约更新 |

第306批复用Hume第305批已返回的单步测试建议，由主线补齐0223/0225/0228共13项隔离SQLite测试，迁移文件未改。首轮13项均在测试辅助函数的历史SQLite列元数据解析处失败；改为读取实际查询列名后13/13通过125.627s，原迁移与保护断言未放宽。仅空业务状态做反向Schema/choices往返，旧行升级及显式保护在各自非空夹具中验证；不代表重迁现用库、真实历史回滚或完整CL-03完成。

0234增量（325复核320原证据，未新跑升级/回滚）：

| 迁移及依赖 | 实际写入与保留边界 | 已有验证与未验范围 |
| --- | --- | --- |
| [0234](../../../../backend/studio/migrations/0234_assembly_consumer_input.py)，依赖0233 | AssemblyJob新增可空consumer_stage（StageRun外键、PROTECT）及非NULL/可空字符串consumer_input_sha256（64字符），assembly_consumer_input_pair要求两者同时有值或同时为空。无RunPython、不猜历史归属；旧Job新列为NULL/空串，原列/来源项保留。新增外键索引studio_assemblyjob_consumer_stage_id_e2e821e1，无SHA/复合索引，不操作媒体文件 | 320 [专项迁移测试](../../../../backend/studio/tests/test_assembly_consumer_migration.py)1项覆盖非空旧失败Job的0233→0234→0233→0234，旧列逐项相同且不补消费事实；[消费来源测试](../../../../backend/studio/tests/test_assembly_consumer_input.py)3项覆盖失败/重试保留、历史不猜、当时SHA不随新输入重标及部分配对约束。包含在320的65/65（327.029s）组合，不是65项迁移专项。EXPLAIN使用该FK索引，排序仍有临时B树。实际切换见320备份/重启及post-load.json：Schema0234、quick_check通过、FK异常0、当时AssemblyJob0。未验证现用回退或带新消费事实的回滚恢复；逆向删除新列/约束/索引，重升不能恢复已删除消费事实。未专项覆盖NULL Stage＋非空SHA、引用Stage的PROTECT删除及悬空Stage外键，不将这些缺少用例写成失败 |

上述迁移不操作媒体文件；后续CL-01/02仍须按原明确目标核对共享引用，特别是审核Stage、Preparation替代链、Shot来源/修订及装配消费Stage的RESTRICT/PROTECT关系，不能凭“旧Run”推断全部关联可删。空表测试不外推非空逆向恢复；0234的非空旧失败Job往返也不代表新消费事实可逆。259副本、264现用操作及之后试用产生的回执分别记录，不混称迁移自动产物。其余缺少专门单步用例不撤销已有组合与实际切换证据，也不重新阻塞已交付模块。

实际切换仍按既有R06明确范围：先核对现用迁移起点及拟使用的字段/结构/来源/输出/实例/Profile/配方，再区分保留对象、需重新配置的新版本和不适配的开发测试对象。新配置沿正式页面/API维护，不把旧请求改写为新合同；清理范围只沿CL-01/02已核定目标及独占关联，未知共享对象先确认。无费用核实前置、无新增审计门禁、无第二套Schema或服务脚本。

2026-09-29补充[0206→0211组合迁移测试](../../../../backend/studio/tests/test_module_cutover_migration.py)：**3项通过（15.644s）**。隔离旧配置、已取消Run/退役配方、原Asset/文件及人工选择记录升级后原ID/旧列/hash/字节不变；新结构、来源路径、媒体策略和素材关系不根据名称推断；新自动选择不伪造人工操作者，明确主/变体关系复用Asset不复制文件。外呼被测试桩禁止，未迁现用库。首轮测试辅助函数参数与模型name重名导致3项准备错误，修正后完整重跑通过；未修改迁移文件，不开发旧数据兼容。

第343批收口：以上各模块当前Schema、依赖顺序、保留与退役范围、空库和对应隔离升级证据已满足CL-03；未发现需要新增迁移的明确缺口，不再用“尚未交付模块待核对”泛化阻挡清单完成。0234已在320迁入现用，337/340仅加载代码；M1/M2/M3及264已执行范围不再列为待授权。后续若实际发现新Schema差异，按对应任务补增量，不预建迁移或恢复PostgreSQL。现用逆向恢复、新消费事实回退及部分单约束未专项测试边界仍如上披露；CL-01的12项有效共享引用已盘点，CL-02物理删除决定及CL-04试用前核对仍独立未完成。
