# 5. M1 字段与结构配置

> 原设计与任务编号保持；此模块的勾选仅在本文件维护，不复制到索引。

[总索引](../../README.md) · [当前续接](../../current.md) · [按日期查证据](../../records/README.md)

<!-- source-section b94b82e662ed -->
<a id="m1"></a>

<a id="5-m1-字段与结构配置"></a>

## 5. M1 字段与结构配置

<a id="51-背景需求与产出"></a>

### 5.1 背景、需求与产出

当前提示词字典、输出解析、实例表单存在不同职责和硬编码。目标是把“字段是什么、长什么结构、从哪里取、如何用于模型”分清，新增受支持类型的字段不用改解析器/表单。系统设置新增“字段维护”一级大类，下设字段定义、结构合同、来源目录/映射预览、引用影响。Profile只选择字段和编辑策略，不再管理另一套字典。

输入：用户字段定义、结构版本、样例JSON、已有来源目录。输出：可复用的字段/结构版本、同源Schema/表单、解析结果及路径错误、下游可选字段目录。**不在M1一次接完所有业务消费者**，逐模块接入后才能宣称全链完成。

<a id="52-实现方式"></a>

### 5.2 实现方式

- 后端：在现有模型/来源/输出/实例体系增加第3节的数据对象；一个类型校验/结构编译/路径映射服务同时供配置预览、实际准备和结果解析调用。
- 前端：`SettingsView`导航接新分区；从`PromptProfileSettings`抽取字段管理，保留共用字段选择器/引用跳转。字段类型和来源从后端目录生成选项，不手填内部ID/JSON作为唯一操作入口。
- 参数：执行目标原生参数约束继续由所选实例/目标提供，业务字段中心引用并展示，不复制成第二套可冲突的默认参数。
- 引用变更：提供引用位置和新版本影响，不建设审批流；有效ID保留，失效开发配置可删。保存与预览严格分开，示例永不进入任务实际值。

<a id="53-细任务与逐项验收"></a>

### 5.3 细任务与逐项验收

- [x] **M1-01 字段所有权清单**（P0；依赖：无）。实现/产出：列出现有字典字段、全局必填/默认、Profile规则及消费者，标明保留ID、迁移位置、清理项。验收：每一类值只有一处权威写入，无法归类的差异明确提出，不新增第二字典。完成：2026-09-28；清单及源码核对证据见5.5节。本项是归属盘点，不表示后续迁移或消费者切换已经完成。
- [x] **M1-02 字段身份与版本**（P0；依赖：M1-01）。实现/产出：字段模型重命名/版本、类型/单位/枚举，移走全局使用规则；Django迁移及序列化器。验收：同ID新版本不改变旧准备含义，显示名修改不造成引用丢失，隔离空库及可保留定义的迁移通过。完成：2026-09-28；最终收敛与验证见本项末两段。以下续段保留当时状态，不代表当前仍未完成。
  - 进行中（2026-09-28）：已写`FieldDefinition`模型重命名、0199迁移、`FieldDefinitionVersion`及生命周期服务/序列化器；保留原ID与Profile关系，已有定义仅生成版本草稿。续段已移除字典全局必填/默认，规则拥有固定值/缺失默认/必填，API和现有策略编辑页同步切换；身份表重复定义列清理和Profile确切定义版本接入仍未完成，不能部署为完整M1或勾选本项。
  - 本段代码：[模型](../../../../backend/studio/models.py)、[0199迁移](../../../../backend/studio/migrations/0199_field_definition_versions.py)、[版本服务](../../../../backend/studio/services/field_definitions.py)、[版本序列化器](../../../../backend/studio/field_definition_serializers.py)、[版本测试](../../../../backend/studio/tests/test_field_definition_versions.py)、[迁移测试](../../../../backend/studio/tests/test_field_definition_version_migration.py)。现有运行时模型符号及测试夹具同步改名，不新增旧名导入别名；历史迁移中的原模型名和待M1-09收敛的现有HTTP组件名未伪装为已经移除。
  - 隔离验证：进程内设置`DATABASE_URL=sqlite:///:memory:`后，`manage.py test studio.tests.test_field_definition_versions studio.tests.test_field_definition_version_migration studio.tests.test_prompt_profile_preview_examples studio.tests.test_execution_profile_inputs studio.tests.test_registry_deletion --noinput`39项通过（25.114s，不含建库时间），含空库完整迁移及0198→0199保留ID/外键/枚举、草稿种子验证。首轮先被索引名超30字符的系统检查拒绝，修复后才完成此轮，不计首轮为测试通过。
  - 关联回归：`manage.py test studio.tests.test_prompt_profiles studio.tests.test_openapi_schema --noinput`96项通过（110.572s）；该轮暴露settings中旧模型枚举路径警告，已修正。修正后枚举及服务端表单合同2项通过，`manage.py spectacular --validate --fail-on-warn --file NUL`通过、无警告；另新增非对象请求输入1项通过（无需数据库）。这些是不同轮次的定向结果，不相加冒称一次全量回归。
  - 其余验证：`manage.py check`通过，`makemigrations --check --dry-run --noinput`无漂移；本轮活动文档93个文件链接无失效，91任务ID唯一、仅M1-01勾选；定向diff检查通过，仅已有LF/CRLF转换提示。未做前端/浏览器交互、全后端回归、PostgreSQL锁语义或真实新链验收；未迁现用库、重启服务/ComfyUI、执行模型、改业务配置/审核或删除数据。下一段继续M1-02剩余规则迁出和版本消费者，完成前不接M1-03。
  - 续段实现（2026-09-28，使用规则归属）：[0200迁移](../../../../backend/studio/migrations/0200_prompt_rule_value_ownership.py)删除字典`default_value/is_required`，规则增加`fixed_value/has_default/default_value`并保留本规则`required`；`fixed.value`替代旧全局默认来源，后端合同/装配/实例输入/字段准备、API/预览/克隆、前端表单/来源说明/实际调用追溯同步使用。合同schema7只冻结规则自己的值，缺键才应用显式默认；null/空串/空列表不触发替换，false/0保真。字典旧属性写入明确报错，不保留别名。OpenAPI、架构/API、页面指南及生成版指南已同步。旧v21测试快照改为明确标注的schema7合成参考夹具，不作为历史真实执行证据。
  - 续段迁移边界：0200不自动转换旧Profile或重算其摘要/审核状态，schema1–6不作为新规则执行；原ID及引用保留。删除全局列意味着相应旧值不再作为当前配置保留，真正迁现用库/旧记录清理须按R06另核范围；本轮未执行。新规则缺失行为的定向用例不等于M1-04完整嵌套结构/nullable验收。
  - 续段验证（2026-09-28）：新增[使用规则测试](../../../../backend/studio/tests/test_prompt_rule_value_ownership.py)及[迁移测试](../../../../backend/studio/tests/test_prompt_rule_value_migration.py)。新使用语义/字段合同/来源目录33项通过；策略配置、来源说明、写法说明、平台适配62项前端测试通过；新表单/指南/设置加载48项通过；任务追溯25项修订断言后通过。`vue-tsc -b`、Vite build、OpenAPI无警告校验、Django check与迁移漂移检查通过；构建仍有SettingsView体积预警。后端92项首轮2错（审计摘要旧属性及来源断言旧词，已修正）；扩大关联265项（481.267s）有7个旧视觉夹具错误，其余258项通过，含0198→0199及0199→0200隔离迁移、新旧合同边界和API生命周期。修正共用夹具后，使用规则7项、API固定false写入/规则默认1项及全部7个失败用例共15项最终通过（22.352s）；首次定向重跑另有新测试方法归类错误，已修正并计入最终通过，不将失败轮次记为全通过。最终变更后未重新执行全量265项，更没有全平台/真实链验收。
  - 续段命令证据：后端全部使用进程内`DATABASE_URL=sqlite:///:memory:`；265项命令为`manage.py test studio.tests.test_prompt_profiles studio.tests.test_prompt_rule_value_migration studio.tests.test_field_definition_version_migration studio.tests.test_execution_native_text studio.tests.test_execution_normalization_text studio.tests.test_execution_resource_selection studio.tests.test_execution_text_contracts studio.tests.test_production_recipe_preparations studio.tests.test_production_recipe_media_preparations studio.tests.test_production_recipe_bible_regeneration studio.tests.test_published_reference_profile studio.tests.test_prompt_normalization studio.tests.test_openapi_schema --noinput`。最终15项为`test_prompt_rule_value_ownership`全模块，加`ExecutionNativeTextTests`的7个`configure_visual_instance`失败用例；33项为`test_prompt_rule_value_ownership.PromptRuleValueOwnershipTests`、`test_execution_field_contracts`、`test_prompt_source_catalog`。前端运行`node --test --test-concurrency=1`对应`promptRuleValueOwnership/systemSettingsGuide/settingsLoading`、`taskCenter`及策略适配用例，另`vue-tsc -b`、`vite build`；指南由唯一JSON生成。详细设计91任务ID唯一、仍仅M1-01勾选，23个文件链接有效；定向diff检查无空白错误。未迁现用库、改服务或业务配置、真实调用/审核/删除业务数据；无新增需求或待用户决定事项。
  - 版本维护API续段（2026-09-28）：补齐精确版本消费者所需的维护入口[版本API](../../../../backend/studio/field_definition_views.py)，复用已有版本服务/序列化器；一个`field-definition-versions`资源支持分页摘要/详情、草稿创建/修改、同身份确切来源克隆、启用/停用与九类字段类型目录。不增加第二套定义模型、旧路由别名、审批或审计门禁；不自动改写身份定义内容、Profile或准备。列表只读选择所需列，UUID筛选不加载整条身份正文。API文档、架构与根OpenAPI同步。新API只是M1-02基础，字段页面、Profile消费者及身份重复列清理仍待接完，本项继续不勾选。
  - 本段验证：[API测试](../../../../backend/studio/tests/test_field_definition_api.py)+`test_field_definition_versions`共20项最终通过（3.911s，不含建库）。覆盖确切版本含义保留、草稿/启用/停用、类型目录、跨身份克隆/非法字段/不可变版本/权限错误、分页及无业务写入。首轮18项通过、2项失败：测试误读统一错误信封，以及查询计数包含公共配置读取并暴露默认FK筛选多加载身份正文；改用UUID过滤、按字段表精确核对2条查询（count+摘要）、修正信封断言后完整复验20项。SQLite查询计划命中索引；未验证PostgreSQL。另原字段枚举OpenAPI回归1项通过，新增版本类型/状态枚举命名已登记，`spectacular --validate --fail-on-warn --file ../openapi.yaml`无警告，Django check及迁移漂移检查通过。本段无新Schema迁移、无前端改动，未做全后端/浏览器/现用服务/真实模型验证；未迁现用库、改服务、改业务配置、审核或删除数据。当前无新增需求/用户决策阻塞，继续版本消费者切换。
  - 精确版本消费者续段（2026-09-28）：新增[0201迁移文件](../../../../backend/studio/migrations/0201_prompt_rule_field_versions.py)，规则引用`field_definition_version`，新Profile合同schema8；规则写入/候选预览明确选择可用版本，装配、克隆和合同验证读取确切定义；冻结ID/key/版本号/含义/标签/类型/单位/枚举，不查询当前字典重解释，不自动取最新。旧规则FK保留null，旧schema7及其摘要不重写；不实现兼容。去掉字典修改时重算策略合同的旧路径，语言说明、展示标签改用版本事实。新增[页面版本选择器](../../../../frontend/src/components/FieldDefinitionVersionSelect.vue)，版本候选为摘要、所选详情按需读取，固定/默认值与样例预览不再使用身份表当前类型；只读配置显示版本ID。已有测试夹具显式创建测试版本，不把测试辅助函数接入业务代码。身份重复列与旧字典编辑路径尚未收敛，M1-02继续不勾选；对象/媒体/嵌套列表运行结构留给M1-03，不把九类类型目录冒称完整类型系统。
  - 本段验证过程：首轮43项中14错误/1失败为schema7夹具/断言；扩展177项中5失败/5错误涉及API缺版本、未保存版本FK和复制字段未同步元数据，均按新契约修正夹具。第二轮184项183通过、1失败：新增零查询测试包含原公共配置读取；明确提供配置边界后[精确版本消费者7项](../../../../backend/studio/tests/test_prompt_field_version_consumers.py)通过（0.811s）。相邻50项48通过，含[0201不回填旧合同的隔离迁移](../../../../backend/studio/tests/test_prompt_field_version_migration.py)；2项声明式API夹具缺版本修正后，`DeclarativeSourceProjectionApiTests + test_prompt_field_version_consumers + test_execution_field_contracts`共34项最终通过（15.537s）。去除旧schema条件后纯快照2项再次通过（0.002s）。这些组合有重叠，不相加为独立总数，也不是全仓回归。
  - 本段命令与边界：后端均用进程内`DATABASE_URL=sqlite:///:memory:`。184项为`test_prompt_field_version_consumers/test_prompt_rule_value_ownership/test_field_definition_api/test_field_definition_versions/test_execution_profile_inputs/test_prompt_profiles/test_prompt_normalization/test_execution_field_contracts/test_published_reference_profile`；50项为`test_prompt_field_version_migration/test_content_sources/test_declarative_source_projection/test_prompt_profile_compiler/test_execution_reference_fields/test_execution_reference_preparation`。前端`fieldDefinitionVersionSelect/promptRuleValueOwnership/promptProfileConfiguration/platformFoundation`共58项通过；新增指南组合57项首轮55通过、2项旧schema文案断言修正后全部通过（8.194s），覆盖过期响应/跨身份/不自动选最新版。`vue-tsc -b`、Vite构建（11.59s，既有大chunk提示）、Django check、迁移无漂移及OpenAPI严格生成校验通过。真实浏览器桌面/窄屏、现用服务、全后端和其他工厂消费者尚未完整复验；本段不冒称部署或真实生产验收，未迁现用库、改服务/配置、调用模型/Smoke、代审或删除数据。无新增需求/用户决定阻塞；下一段清理身份重复列与旧编辑链，完成M1-02后再接M1-03。
  - 本段收尾：指南源与生成Markdown一致；最后一次类型检查、Vite构建（10.10s，保留原分块提示）、纯快照2项和迁移漂移检查通过。三份活动计划文档104个文件链接均有效；91项ID唯一、仅M1-01已勾选；定向diff无空白错误，仅既有LF/CRLF提示。未留运行中的测试/构建命令，下一次按18.1接续，不重跑无关完整回归。
  - 最终收敛（2026-09-28）：[0202迁移](../../../../backend/studio/migrations/0202_field_identity_only.py)删除身份表六项重复定义内容，只保留ID/key、启停、排序；身份API拒绝旧内容字段，启停不重写具体版本。原字典表单已去掉原地修改含义/类型的入口，新增共用[定义版本管理组件](../../../../frontend/src/components/FieldDefinitionVersions.vue)及客户端，支持明确版本查看、草稿/差异PATCH、指定来源克隆、启用/停用，类型/状态取同一服务端目录。独立导航、HTTP命名及Profile字典入口迁出仍属于M1-09，不提前冒称完成。准备字段、批量审核、辅助返回展示均取冻结标签；删除预览列出从属版本，不遗漏级联；移除按业务key硬编码的预览示例，样例仅按具体版本类型生成。API、架构、指南/操作手册与OpenAPI同步。测试工厂显式生成测试版本，不设运行时旧属性别名；历史迁移测试仅在隔离Schema暂置空列，不宣称能恢复0202删除的数据。
  - 最终验证：首轮98项有5个错误（身份默认目录仍读旧列及旧标签断言），修正后组合53项中51项通过，另2项暴露审计摘要未登记版本ID、历史测试反向重建旧必填列失败；修正后[0202迁移与0199/0201迁移](../../../../backend/studio/tests/test_field_identity_migration.py)、规则删除/字段版本关联删除共5项通过（8.442s）。相邻`test_prompt_profile_compiler/test_execution_profile_inputs/test_content_sources/test_execution_native_text/test_execution_normalization_text/test_execution_text_contracts/test_execution_resource_selection/test_execution_auxiliary_definitions`110项通过（230.226s）；最终版本/身份API、单字段冻结名称与批量审核11项通过（8.764s，前次命令类名写错未计为通过）；规则使用值/预览样例纯逻辑10项通过。前端定义版本/选择器/使用规则/平台适配/指南68项通过（10.089s，首轮旧字典适配断言已修正），`vue-tsc -b`、Vite build（10.12s，仍有SettingsView体积警告）、Django check、无Schema漂移、严格OpenAPI生成/校验、指南生成一致性、定向diff检查通过。后端均为进程内`DATABASE_URL=sqlite:///:memory:`，包含空库完整迁移与确切ID/版本/内容保留；这些轮次有重叠，不相加冒称全量回归。未完成真实浏览器/窄屏、全仓回归、PostgreSQL、现用服务或真实生产链验收；未迁现用库、操作服务/ComfyUI、发布业务配置、真实调用/审核或删除业务数据。M1-02按其基础验收完成，下一项M1-03，M1模块交付仍待M1-10。
- [x] **M1-03 输入输出统一结构**（P0；依赖：M1-02）。实现/产出：对象/列表/媒体节点、使用规则、来源/输出合同引用统一结构。验收：嵌套列表、数字/布尔/枚举/媒体均生成一致Schema；循环结构/错类型明确定位，不重复存两套业务Schema。
  - 输出接入段（2026-09-28）：新增[0205迁移](../../../../backend/studio/migrations/0205_business_output_structure.py)，BusinessOutputVersion以确切外键引用模型响应结构；schema2合同冻结同一节点/字段定义并计入原SHA。新建/改选/发布检查已发布结构、既有协议的响应路径/类型与示例；不自动猜结构或另存Schema。后继复制确切结构，旧结构退役不改变冻结元数据；空引用旧行的详情/引用明确失败，迁移不重写旧parser/SHA/大小。已保存预览与正式返回通过[同源输出服务](../../../../backend/studio/services/business_outputs.py)引用共享值校验，路径错误可见，不转换/截断返回值。输出页面复用共享结构选择器、dirty/保存校验，选用前按需核验精确详情，失败及迟到响应不误选。
  - 输出段验证（2026-09-28）：首轮20项因新测试夹具误将根/列表元素标必填，19项报错，修正夹具后组合81项79通过、2项为新增测试UUID对象/字符串比较及误写JSON Schema名称maxLength而非平台max_length；修正后扩展84项83通过，唯一失败为把既有公共配置12次查询算进冻结元数据查询。核对SQL无字段/结构/输出版本查询后修正边界断言，最终输出API/冻结结构/0205迁移/OpenAPI及三个实例→准备→返回解析隔离场景**26项通过（53.199s）**；前轮结构/协议/预览/审核纯逻辑通过，轮次重叠不相加，不冒称整仓回归。前端**8项通过（3.933s）**，涵盖结构必选、请求携带确切ID、dirty、详情失败/退役/缺结构与迟到结果不选用；vue-tsc、Vite build（10.87s，仍有SettingsView体积警告）、严格OpenAPI生成/校验、Django check、无Schema漂移通过。后端全部为进程内DATABASE_URL=sqlite:///:memory:，0204→0205验证不回填/重算历史。未做浏览器/窄屏、全仓、PostgreSQL、真实模型/素材或现用服务验收，未迁现用库、重启、发布业务配置、实际审核采用或删除数据。
  - 基础段（2026-09-28，部分完成，不勾选）：新增[0203迁移](../../../../backend/studio/migrations/0203_data_structures.py)、`DataStructureVersion/DataStructureNode`；节点子表只维护位置/使用约束并引用确切字段版本，不复制定义字典、不持久化第二份可编辑Schema。[统一编译服务](../../../../backend/studio/services/data_structures.py)支持显式对象/嵌套列表、九类字段、类型适配约束及Asset引用Schema；未知属性保留，循环/断链/错类型/矛盾约束定位到节点。草稿可不完整，可用版本不可原地覆盖，明确来源克隆；快照可离线同源编译。正式维护/只读Schema预览API已写入[接口文档](../../../api.md)，摘要分页不读节点大正文；字段删除预览列出共享结构引用，不连带删除结构。
  - 来源接入段（2026-09-28）：新增[0204迁移](../../../../backend/studio/migrations/0204_content_source_structure.py)，ContentSourceVersion以确切外键引用统一结构；schema2来源合同冻结节点/字段快照，不另存Schema。新建/改选/发布检查结构及已实现读者的类型，旧行只保留空引用，不补配/重算摘要；后继保留确切结构，旧快照不随新版本或退役变化。策略端口、准备解析及策略来源投影用相同冻结结构检查真实文字值，纯校验不查库、不转换或截断值。来源管理页新增共享[结构选择器](../../../../frontend/src/components/DataStructureVersionSelect.vue)，搜索/分页/确切版本选择，展开才读候选；来源列表改为窄摘要，查看/选用按需读取并核验详情，失败不使用列表冒充详情。
  - 当前边界：来源与输出均已接入同一结构，M1-03按上述基础验收完成，不等于M1模块交付。当前来源读者仍为既有文字协议，输出仍为visual_rules.v2有限业务语义，不宣称任意JSON解析已实现。结构维护/同源值控件及字段中心、缺失默认、通用解析映射、真实媒体装配和其他消费者分别由M1-06/M1-09、M1-04、M1-05、M1-07/M1-08继续。下一项M1-04；无新增需求或需用户决定事项，未触及现用数据库/服务/配置/调用/业务数据。
  - 验证（2026-09-28）：首轮结构/API 18项通过；结构/0203迁移/字段版本及API/OpenAPI组合91项中90项通过、1项仍断言身份表提供preview_example，改为确切版本后加引用删除测试的94项通过（47.224s）。最后复核收敛重复发布编译，并补属性名/版本ID拒绝隐式转换用例；18项中2项测试夹具缺少名称先触发名称错误，补齐名称且保留节点错误断言后，94项中仅新增EXPLAIN断言失败，实际为列表全扫描/临时排序。按真实排序增加`idx_structure_version_order`后，最终`test_data_structures/test_data_structure_migration`19项通过（2.586s），EXPLAIN使用该索引且不再临时排序。所有测试均在进程内`DATABASE_URL=sqlite:///:memory:`，包含空库迁移与0202→0203不改旧定义/来源合同；Schema编译1次联查、分页2次查询且不带节点/说明正文。OpenAPI严格生成/校验、Django check、无Schema漂移及定向diff检查通过；不同轮次不相加，最终未重跑整仓。未改前端，未做结构页面/浏览器、真实素材类型权限/可读性、现用服务、PostgreSQL或真实生产链验收；未迁现用库、重启/调用服务、发布配置、代审或删业务数据。
  - 来源段验证（2026-09-28）：原来源/结构31项通过；扩展35项有1个错误响应封套断言失败，修正后45项中43项通过，另外2项为测试口径（把公共配置查询算成来源列表查询、OpenAPI未使用分离的Request名称）。只读SQL核对确认来源列表为count+窄摘要2次，不查询结构节点；修正断言后最终来源/共享结构/0204迁移/既有项目来源及2项OpenAPI定向测试**45项通过（22.546s）**。0203→0204保持旧reader/SHA/大小不变、不生成结构。前端首次5项通过，改为展开才加载目录后暴露Vue内联语句编译错误，改为命名事件处理函数后最终5项通过（4.805s），覆盖分页、确切选择、过期响应、详情失败不选用与关闭状态不请求目录。`vue-tsc -b`、Vite build（11.20s，仍有SettingsView体积警告）、严格OpenAPI生成/校验、Django check、无Schema漂移通过。不同轮次不相加；后端均为进程内`DATABASE_URL=sqlite:///:memory:`。未跑整仓、浏览器/窄屏、真实素材、PostgreSQL、现用服务或真实生产链；未迁现用库、重启、真实调用、发布业务配置、审核采用或删除数据。
- [x] **M1-04 缺失和空值语义**（P0；依赖：M1-03）。实现/产出：缺失报错/省略/显式默认，null和空值独立约束。验收：缺键、null、空串、空列表、0、false六组样例结果与第3.2节一致；默认来源可查看。
  - 2026-09-28实现：[共享结构服务](../../../../backend/studio/services/data_structures.py)增加使用处has_default/default_value与缺键解析器，只有键不存在才填默认；无默认则按required报路径错误或省略。null/空串/集合/0/false按原值独立校验，未知属性和原始对象不变；不凭子默认创建可选父对象、不补造列表元素。嵌套默认按同一规则解析并校验，来源逐项含路径/节点/确切结构及字段版本/配置值；任意JSON属性名的路径支持括号消歧。严格原文校验器仍不补值，后续消费者显式接入并保留来源，不将默认冒充模型返回。
  - 产出：[0206迁移文件](../../../../backend/studio/migrations/0206_structure_node_defaults.py)仅新增关闭状态/空配置，不重写旧请求及摘要；结构冻结包schema2拒绝旧包和缺少默认规则的快照，不查当前配置兜底。结构详情/克隆/Schema投影同步字段。[value-preview API](../../../../backend/studio/data_structure_views.py)支持确切草稿/可用/停用版本的只读样例；返回值、默认来源及错误，不创建任务/素材、改配置或外呼。UI表单及字段中心仍归M1-06/M1-09，通用JSON文本/映射归M1-05；M1-04不冒称这些后续项已交付。
  - 验证：[值语义及API测试](../../../../backend/studio/tests/test_structure_values.py)、[迁移测试](../../../../backend/studio/tests/test_structure_defaults_migration.py)与关联回归共135项全部通过（98.989s，不含建库）。覆盖六类值、空对象、嵌套对象/列表、显式空默认、非法默认、精确来源、冻结后零查询/不追最新、权限与无写入预览；来源/业务输出/影视规则预览和冻结审核合同同轮通过。首轮32项30通过、2错误（预览非法请求抛字符串导致500、测试未取回发布后对象），均修复；后加可空父节点错误定位及非有限JSON测试，一并纳入最终135项，不将失败轮计为通过。
  - 命令：仅进程内`DATABASE_URL=sqlite:///:memory:`，`manage.py test studio.tests.test_data_structures studio.tests.test_structure_values studio.tests.test_structure_defaults_migration studio.tests.test_content_sources studio.tests.test_business_outputs studio.tests.test_visual_rules_output_preview studio.tests.test_visual_rule_review_contract studio.tests.test_openapi_schema --noinput`。另`manage.py spectacular --file ../openapi.yaml --validate --fail-on-warn`、`manage.py check`、`manage.py makemigrations --check --dry-run`通过。API/架构/根OpenAPI同步；未改前端，未重复构建。未验现用迁移/服务加载、浏览器、PostgreSQL、全仓或真实模型/媒体；未改现用数据/服务/配置，不审核/采用或删除数据。无新增需求/用户决策阻塞，下一项M1-05；整个M1尚未达到M1-10模块验收。
- [x] **M1-05 JSON解析与配置映射**（P0；依赖：M1-03）。实现/产出：属性路径、列表逐项、嵌套构造、固定值、稳定key引用的共用服务和无副作用预览。验收：布局A/B只改配置得到相同业务输入；非法JSON和不存在路径定位到原文/字段，不创建任务。
  - 2026-09-28实现：[结构映射服务](../../../../backend/studio/services/structure_mapping.py)读取确切源/目标结构与字段版本，提供显式属性/下标路径、嵌套列表逐项、对象/列表构造、固定JSON值、按声明稳定文本key精确引用；重复、空或缺失key报具体路径，不按显示名、翻译或第一项猜测。对象/数组/逐项/固定值复用既有`execution_structure_mapping`装配器，严格文本解析收敛到`json_contracts`；不存在第二套字段字典、业务值存储或执行队列。
  - 产出：结构版本新增`path-options`目录和`mapping-preview`只读API，草稿可试配但不等于允许执行。报告保留原文解析值、源/目标解析值、默认路径及确切版本、取值来源、未知字段提醒与字段错误；非法JSON返回行/列/字符位置，不修复围栏、重复键或非有限数值。原始未知属性保留但不自动定义，缺键默认仍走M1-04共用解析器；预览不保存配置、不创建任务/素材或外呼。接口及架构、根OpenAPI已同步；实际消费者接入仍属M1-08，页面/媒体/字段中心仍属M1-06/M1-07/M1-09。
  - 验证：[解析/映射专项测试](../../../../backend/studio/tests/test_structure_mapping_preview.py)含18项纯服务和2项API测试，覆盖布局A/B等价、非剧本字段、嵌套列表、稳定引用、精确默认来源、缺路径/非法JSON、无写入、权限及异常值保留。自查修正固定对象与构造对象的默认语义不一致、非法冻结包错误封套；有界只读复核发现的重复列表展开、提前构造长路径、警告及完整来源记录未计入预算均已修复并复验。仅复用现有响应大小/JSON字段边界限制预览和默认展开工作量，不新增生产门禁或配置项，不代替最终提示词长度检查。
  - 最终命令：进程内`DATABASE_URL=sqlite:///:memory:`，`manage.py test studio.tests.test_structure_mapping_preview studio.tests.test_execution_structure_mapping studio.tests.test_recipe_structure_preparation studio.tests.test_visual_rules_output_preview studio.tests.test_structure_values studio.tests.test_data_structures studio.tests.test_openapi_schema --noinput`共**126项通过（68.781s，不含建库）**。之前45/101/124项各轮均通过，最终纯服务18项通过，各轮重叠不相加。`manage.py spectacular --file ../openapi.yaml --validate --fail-on-warn`、`manage.py check`、`manage.py makemigrations --check --dry-run`通过。未新增迁移/前端改动，不重复前端构建；未验浏览器、全仓、PostgreSQL、真实素材/模型或现用部署，未迁库、操作服务/ComfyUI、发布业务配置、审核采用或删除业务数据。无新增需求/用户决策阻塞；下一项M1-06，整个M1仍待M1-10验收。
- [x] **M1-06 同源表单与值展示**（P0；依赖：M1-03, M1-04）。实现/产出：嵌套对象/可增减列表、枚举、文本、数字、媒体的共用编辑与只读组件。验收：新增非剧本字段不改Vue组件即可填写/显示；字段错误对应控件；预览样例不能成为默认任务值。
  - 2026-09-28实现：扩展已有[StructuredJsonEditor](../../../../frontend/src/components/StructuredJsonEditor.vue)及同源Schema工具，不另建第二套表单。读取确切字段版本的名称/含义/单位与结构的属性、列表元素、枚举、数值范围、nullable分支；登记字段从目录选择，任意命名的自定义字段无需业务分支。缺键/null/空串/空集合/0/false分别呈现，未知属性保留并标注未映射；只读按实际值展示，不以目标类型把错误原值变成空值。服务端路径错误对应控件/缺失字段，格式尚不能读取的数字草稿不会提交旧值。
  - 产出：[DataStructureValuePreview](../../../../frontend/src/components/DataStructureValuePreview.vue)挂在原来源/输出结构选择器的“试填结构样例（不保存、不执行）”，展开才请求确切版本的schema-preview/value-preview；原始样例、解析副本、默认来源分开呈现，未输入不填默认/示例、无自动采用/配置保存/业务任务写入，切换版本清空样例并忽略过期响应。来源页将选择器移出配置禁用fieldset，已发布配置仍不可编辑但可以试填独立样例。媒体引用复用Asset读取、AssetMediaPreview及原素材选择器；样例仅选已有素材，关闭上传分支，实际来源策略/上传装配归M1-07，三处运行消费者归M1-08，字段维护中心归M1-09。
  - 验证：[结构表单专项测试](../../../../frontend/tests/structureValueForm.test.ts)8项覆盖任意字段/嵌套列表、nullable枚举整数、缺失默认隔离、原值与未知字段、精确错误路径、过期/失败请求、只读/禁止上传及非法媒体原文；与structuredInputForm、dataStructureVersionSelect、executionRequestForm、workflowAssetGroups、modelChatRequestOptions、modelTextExecution、recipeStructureMapping共**57项通过（30.130s）**。最初11项有1项失败（SSR生命周期不保持immediate watcher），改客户端测试装载后曾因SSR编译上下文缺失失败，补测试上下文后复验通过，未删断言或改业务语义掩盖；前两轮56项均通过，不与最终轮相加。`vue-tsc -b`及Vite build（10.29s）通过，仍有既有SettingsView大包提示。
  - 浏览器：[隔离UI夹具](../../../../frontend/tests/fixtures/structure-value.html)仅挂载实际组件和显式替代的测试接口，未调用业务API。默认宽屏、360px容器及390×844视口检查添加自定义字段/正文、列表增项、readonly呈现、素材读取失败、缺失提示及数字无效→纠正→预览；390px视口scrollWidth为382，无横向溢出，控制台无warn/error，临时视口已恢复。浏览器发现nullable枚举缺少null切换入口已补控件/断言；自查补充数字未完成时不提交旧值，代码复核发现的非法媒体原值被隐藏已修正并补两组输入。页面验证是隔离组件证据，不是现用管理配置/数据库已升级或真实素材可用。
  - 边界：本轮仅前端及文档；后端/API合同/Schema无变更，未重复后端全回归或生成OpenAPI。未迁现用库、重启/改服务或ComfyUI、发布配置、真实模型/Smoke/生产重试、审核采用或删除数据；未验证现用API联调、真实媒体读取/上传或M1整个模块。无新增需求/用户决策阻塞，下一项M1-07；M1仍待M1-10独立验收。
- [x] **M1-07 来源和媒体装配配置**（P0；依赖：M1-05, M1-06）。实现/产出：上游/固定值/素材来源目录，媒体允许来源、数量/用途/顺序与本次改选规则；复用上传→Asset。验收：本地上传和库内选取都得到确切Asset引用，失效来源不静默换默认图；未接模块标明不可用。
  - 实现：[input_source_policies](../../../../backend/studio/services/input_source_policies.py)提供单一来源目录及严格schema1媒体策略，声明允许/默认来源、允许本次整组改选、媒体类型、数量、用途和明确顺序。固定值/结构上游仍复用M1-05映射，不复制解析器；库内Asset、上传后Asset与已选择上游产物复用原执行媒体读者，按原授权/文件事实核对；同一来源仅在本次解析内复用核验结果，不合并其多个用途/使用标识。原上传写入/读取抽取同一来源常量；客户端不能自称上传来源。解析前应用服务端现有媒体数量/字节边界，目标端口/能力约束仍由原适配器承担。
  - 产出：现有结构API增加`input-source-options`与`media-source-preview`，仅返回目录、实际解析项或路径错误，不保存配置、上传、建任务或调用模型。预览只认当前用户素材，没有真实Run上游上下文时明确报告不可用，不允许请求伪造上下文；Bible主图/变体版本读者依赖M9/M5，目录显式不可用。前端新增[来源策略控件](../../../../frontend/src/components/InputSourcePolicyEditor.vue)和[有序素材选择控件](../../../../frontend/src/components/MediaSourceSelectionEditor.vue)，复用原Asset选择器，支持纯库内/纯上传模式、增删排序及失效原值呈现；停用已选来源可移除但不可重加，空数量不误判有效。上传期间保留组件挂载，父级busy不吞回执，类型/来源/使用项/策略改变后不回填旧回执并说明素材可能已经入库。
  - 后端验证：[来源策略专项测试](../../../../backend/studio/tests/test_input_source_policies.py)与execution_media_sources、execution_selected_outputs、execution_input_resolution、asset_management_api共56项通过（14.766s），覆盖正式multipart上传后引用、同Asset库内引用、确切上游决定/产物、所有者/类型/失效文件、重复用途、改选与空值、超限零媒体读取、只读预览零业务写入。早期11/44项轮次暴露测试响应包/UUID断言问题与请求序列化器字符串错误体导致500的问题，均修正后复验；不将不同轮次相加。Django check、makemigrations --check --dry-run均通过，无Schema漂移；根OpenAPI生成/validate --fail-on-warn通过。
  - 前端验证：[来源策略专项测试](../../../../frontend/tests/inputSourcePolicies.test.ts)及结构表单、素材管理、执行输入、媒体组、配方结构映射与生产引用映射共81项通过（30.099s）。复核发现上传自身卸载、不可用来源无法移除、夹具误把配置当override，均修正；真实父模板编译挂载测试覆盖延迟成功/失败及合法用途策略变化，最后8项定向复验通过（3.126s，不与81项相加）。vue-tsc通过，Vite build通过（仍有既有SettingsView大包提示）；测试装载/HTML属性断言曾失败，修正测试装载而非放松业务规则。
  - 浏览器：[隔离UI夹具](../../../../frontend/tests/fixtures/input-source-policy.html)挂载实际控件但替代素材接口，不调用业务API。检查默认宽屏、360px容器与390×844视口下选择/增项/上限/用途排序、读取失败保留引用、禁止本次改选、数量缺失→报错→纠正和配置选择校验；390px视口文档宽382，无横向溢出，控制台无warn/error，临时页关闭及视口恢复。真实上传路径只在临时目录与隔离数据库测试中执行，未对现用业务上传。
  - 边界：M1-07交付共用服务/组件/API，不宣称配方/实例/Profile三处保存、冻结和业务准备已接通；接续M1-08，字段中心归M1-09，整个M1仍待M1-10。无新增配置表、迁移、执行队列或需求；未迁现用库、操作服务/ComfyUI、发布实际配置、真实模型/Smoke/生产重试、审核采用或删除业务数据。无须用户决策的新阻塞。
- [x] **M1-08 配方/实例/策略引用接入**（P0；依赖：M1-05, M1-07）。实现/产出：三处编辑器引用同一字段/结构，逻辑输入到目标端口映射及输出目录。验收：自定义字段经过配置→准备→示例下游可见；新字段不要求新增专用枚举/分支。完成：2026-09-29，通用文本增量与组合证据见5.6；M1-09/10和其他业务模块不在此勾选范围内。
  - 2026-09-28首段完成（整体仍未勾选）：[ExecutionInputSpec](../../../../backend/studio/models.py)增加确切结构外键及[0207迁移文件](../../../../backend/studio/migrations/0207_execution_input_structure.py)，原创建/更新/克隆/配置读取API与编辑器支持可选`data_structure_version_id`，null明确移除，未选不追加业务结构约束；纯Profile自动派生输入拒绝重复定义，显式混合接线保留原Profile约束。不存在/草稿/退役引用明确失败，失败保存回滚，不查最新版补齐。
  - [共用输入结构服务](../../../../backend/studio/services/execution_input_structures.py)将ID冻结为`interface.input_structures`；表单从同一快照派生标签/Schema，原目标限制不覆盖。准备解析在来源汇合后、目标投影前调用M1-04共用缺键解析器，再检查目标/Profile合同。未知属性原文、null/空值/0/false保留；原来源摘要、默认路径/确切版本与最终值摘要分开记录，不伪造模型原文或原审核。纯快照读取不查询字段/结构注册表，仍按既有系统边界解析；无默认长文本不重复计入展开预算。
  - 请求控件及构建器只有明确引用结构时才允许把“有显式默认的缺键”留给后端；前端不应用默认、不改原始请求，其他类型/必填/值约束仍提示。原同源控件非此消费者保持严格校验。新[后端测试](../../../../backend/studio/tests/test_execution_input_structures.py)覆盖配置→冻结→表单→真实输入解析器→端口示例、API回读/克隆/移除/回滚、精确身份/状态、默认与空值/未知属性、40KiB无展开边界；不把示例字段端口当作真实模型生成结果。
  - 已验证：75项后端关联隔离测试（51.858s）；48项前端主回归（25.153s）及18项另组共用控件回归（8.736s，文件不重叠）；类型检查、Django检查、迁移漂移检查、OpenAPI验证通过，Vite构建通过（既有SettingsView大包提示保留）。早期试跑暴露的不存在FK误报404、重复预算及请求表单提前拦截默认缺键已修复，不累加早期失败/重复轮次。独立[浏览器夹具](../../../../frontend/tests/fixtures/execution-input-structure.html)验证选择/清除/只读/读取失败恢复、默认合同启停时原值始终为`{}`、390×844窄屏无横向溢出及无控制台错误；临时页关闭、视口恢复，不调用真实业务API。
  - 2026-09-28第二段完成（整体仍未勾选）：配方结构模板/带结构的固定值预检读取同一`interface.input_structures`，复用`_apply_defaults`在临时副本展开已知默认，再核对逻辑结构及全部目标合同；不存在默认的必填键、默认与目标冲突、显式非法值继续返回路径错误。不将预检副本保存为配方或来源事实；`each`只验证元素模板，实际列表/动态来源仍由原准备解析器校验并记录默认来源，不伪造单元或值。
  - 页面：原配方装配编辑器支持“省略此项，使用结构默认值”与目录恢复；只有明确引用的结构允许省略必填默认键，普通目标default不授权。恢复仅创建待配置映射，不复制默认值；nullable对象同样使用共用目录。父Route核对全部目标Schema并去重错误，anyOf分支不跳过外层约束。新增[隔离页面](../../../../frontend/tests/fixtures/recipe-defaults.html)，无业务API/模型调用。
  - 第二段验证：66项后端隔离关联测试（57.327s）通过，覆盖配方→准备→示例端口、逐条真实隔离审核内容、默认来源及原模板/数据不变；补anyOf外层回归后25项定向复验（7.363s）通过。29项前端关联测试（11.270s）、vue-tsc、Django check、迁移漂移检查通过，Vite构建通过（10.33s，既有SettingsView大包提示保留）。初轮生产夹具缺少运行上下文、前端新增夹具括号及主体类别不完整均已修正复验，不累加失败/重复轮次。浏览器验证引用启停/恢复空映射/只读，390×844文档宽382且scrollWidth382、无控制台warn/error；省略确认弹窗触发工具超时，未完成浏览器确认操作，只以组件测试证明省略逻辑。阻塞页与新验证页均已关闭，视口已恢复。
  - 2026-09-28第三段完成（整体仍未勾选）：[共享路径读者](../../../../backend/studio/services/structure_paths.py)供M1-05预览及原配方执行装配复用。已有context_field/selected_output结构叶子可使用目录声明的深层`path`（属性名/非负整数下标数组），未提供或`[]`为整值；item_field必须明确path并替换旧field，`[]`取当前条目整体。each.source可为上述三者，嵌套each按最近条目解析；中文/点号属性不被拆解，不支持表达式、通配符或根据实际值/目标猜Schema。未声明JSON来源只能整值，显式if_missing仅处理最终对象属性缺失，不能掩盖null、错类型、中间缺失或列表越界。
  - 第三段冻结/来源：[RunPlan](../../../../backend/studio/services/production_run_plans.py)首次创建冻结`structure_source_schemas`目录SHA；首次绑定在原业务读者前核对来源Schema，绑定摘要随准备保存，实际解析再次核对。历史计划读取不查当前目录；来源仍通过原授权/范围/审核/修订读者，证据分离原值SHA、提取路径、来源结构SHA、提取值SHA。整份计划使用原JSON上限；服务器目录摘要纯计算，不为每个字段重复读取限值。旧field语法及缺该冻结目录的旧计划明确不兼容，不双读、不自动补写，也没有清理现用业务数据。
  - 第三段页面：[路径选择器](../../../../frontend/src/components/SchemaPathSelect.vue)接入原配方装配编辑器，不另建入口；支持整值、逐级目录属性和明确列表下标。失效来源/路径保留原值并提示，输出目录变动重新校验；显式重选清理旧field和不适用if_missing，列表来源变更保留原确认机制并清空受影响item映射，readonly不提交。目标静态类型预检不替代实际值的完整目标校验，通用输出目录尚未接入。
  - 第三段验证：最终83项后端隔离关联测试通过（94.187s），覆盖路径、预览、真实隔离Shot审核来源→生产绑定→共用解析器、默认、实例结构、RunPlan及阶段修正；另补1项持久计划冻结→同路径/类型但Schema变化→首次绑定前拒绝，0.010s通过，原计划未变化。32项前端关联测试通过（13.115s），覆盖实际组件路径选择、目录移除、readonly与父Route输出目录变动；vue-tsc、Django check、makemigrations --check --dry-run、根OpenAPI生成及validate --fail-on-warn通过；Vite构建11.58s通过，仅保留原SettingsView大包提示。初轮测试装载Ref错误、断言转义、空计划141次配置查询及TS属性推导错误均修复后复验，不累加重复轮次；两位只读复核提出的冻结时机及三处前端问题已闭合。
  - 第三段浏览器：[隔离夹具](../../../../frontend/tests/fixtures/structure-paths.html)挂载实际递归编辑器，不调用业务API。验证两层each、整值/中文/点号字段、显式下标0、负数提示、失效路径保留与逐级修正、readonly全部禁用；390×844视口scrollWidth382、无横向溢出，控制台无warn/error。临时页已关闭、视口恢复；列表更换确认弹窗只做组件测试，不将此前工具受限的弹窗行为称为浏览器验收。
  - 2026-09-28第四段完成（通用输出配置/目录/只读解析，整体仍未勾选）：[BusinessOutputVersion服务](../../../../backend/studio/services/business_outputs.py)增加`structured_json.v1`，沿原输出定义/发布/后继/退役链引用确切DataStructureVersion；通用解析合同仅key/format，字段含义、属性名、层级、类型和缺键默认来自原字段/结构快照，不复制字典。允许声明的对象、列表、标量及可空根，不借用影视规则字段。通用合同拒绝影视专用映射/质量配置；已保存输出身份不更换协议，未创建新表或迁移。
  - [同源解析器](../../../../backend/studio/services/structured_output_parsing.py)复用M1-04缺键解析、M1-05严格JSON解码/目录/未知字段提醒；原始解析值、默认解析副本、默认来源、路径错误分别返回。未知字段保留，null/空串/0/false按声明处理；重复键、非有限数、错误类型、过深嵌套不伪装成功。`POST business-output-versions/{id}/preview-json/`按输出ID/修订/SHA核对，复用原响应字节边界，报告不写任务、审核、采用、产物或生命周期事件。
  - 页面复用原[业务输出管理器](../../../../frontend/src/components/BusinessOutputManager.vue)/结构选择器，新增[通用JSON预览](../../../../frontend/src/components/StructuredOutputPreview.vue)，由确切Schema渲染实际值/字段目录；修改样例或配置即清除过期报告，晚到响应不能覆盖，失败保留原文。现有影视执行器、冻结定义验证、影视预览/最终解析/审核明确拒绝通用协议误用，选择器标明不支持；通用版本目前仅可配置/解析，不代表能生成Bible或已作为配方上游来源。
  - 第四段验证：最终42项后端隔离关联测试通过（50.939s）：`test_structured_outputs/test_business_outputs/test_execution_input_structures`，覆盖输出生命周期/冻结后零查询解析、布局A/B→共享下游示例映射、原值/默认/未知字段、JSON根空值矩阵、严格请求类型、修订/摘要与权限、错误协议和深层异常。20项前端关联测试通过（9.001s）：`structuredOutputs/businessOutputs/visualRulesOutputPreview/outputFieldSources`，覆盖目录、提交、dirty、未支持协议、异步失效及错误报告；vue-tsc、Django check、迁移漂移检查、根OpenAPI生成/严格校验通过，Vite构建11.57s通过（既有SettingsView大包提醒）。首轮映射夹具缺envelope、SSR测试未启动watch及类型推导问题已修正，两位只读复核的四项异常处理问题已闭合，不累加失败/重复轮次。
  - 第四段浏览器：[隔离夹具](../../../../frontend/tests/fixtures/structured-output.html)挂载实际预览控件，明确模拟API响应、不读取业务数据。原始值/默认副本/目录均可见；错误字段仍显示42，非法JSON/读取失败保留样例；未保存配置禁用预览。390×844视口scrollWidth382，无横向溢出、控制台无warn/error；临时页已关闭、视口已恢复。配置保存与后台接口通过隔离组件/API测试，未操作现用配置页面提交。
  - 2026-09-28第五段（原主调用原文留存，整体仍未勾选）：[原文本worker](../../../../backend/studio/services/execution_production_text_worker.py)将成功主调用的实际返回沿既有ExecutionTextResponseReview/私有文件保存，不用ScriptRevision或影视转换结果冒充原文，不新建表/队列。生成成功不填写人工确认/采用；原人工修正仍另存corrected正文。共享读者核对原Attempt关联、冻结请求、文件大小/SHA，成功主调用只读，返回与候选分别保留。晚到/重复领取不增候选或响应记录，未保存历史不回填。
  - 第五段失败/页面：原文写后回读成功才生成候选/字段；保存失败记execution_response_storage_failed，保留已收到调用的结算事实，不称模型格式无效、未提交或已保留，不自动重领/ordered_fallback；显式人工重试沿原入口。主调用字符串提醒与辅助对象提醒分别按真实协议展示；共享JSON解码错误分类按稳定code接线，不再匹配已删除的旧错误文字。工作台改为“模型返回与处理记录”，成功只读、格式失败可修正、已修正的历史诊断默认收起，不暗示已采用。[隔离页面](../../../../frontend/tests/fixtures/retained-primary-response.html)验证三态、读取失败、原文转义和390×844无横溢出/控制台warn/error，已关闭临时页并恢复视口。
  - 第五段验证：首轮102项后端关联回归发现3失败/1错误；修复后12项定向复验通过（82.967s），覆盖原文精确留存/来源校验、主调用字符串提醒、写入与回读失败、禁止自动换路、共享JSON诊断、辅助/Smoke原有失败保存和人工修正；未重跑整组102项，不宣称整组通过。最终33项前端关联测试（15.573s）、类型检查、Django check、无迁移漂移通过，Vite构建12.36s通过（既有SettingsView大包提示）。隔离页面验证范围见上一条；无现用数据库/服务/业务配置/真实模型调用或数据清理。
  - 第六段已接入Profile结构化来源：`input_structure_version`引用唯一确切已发布结构；规则选择`request.structured_input`和`source_path`，任意已声明属性/固定下标经共用`structure_paths`取值（含点名称不按点切割，[]明确选整值）。同一根值供多个不同类型字段取值，不再为此增加业务selector；保存/克隆/候选预览/合同冻结/派生输入/实际准备/装配均复用原链。结构默认先处理，规则缺省只处理最终属性缺失；false/0/null保留，父级缺失、错误类型及列表越界明确报错。确切根结构归Profile所有，执行实例不能另写第二份定义；现阶段仅passthrough/server_locked，不把M6辅助生成/翻译路径提前实现，也不与文字source_version读者并配。
  - 第六段实现证据：[结构化Profile共用接线](../../../../backend/studio/services/prompt_profile_structures.py)、[隔离回归](../../../../backend/studio/tests/test_profile_structured_inputs.py)、[迁移0208](../../../../backend/studio/migrations/0208_profile_structured_input.py)、[前端接线测试](../../../../frontend/tests/promptStructuredInput.test.ts)、[隔离页面夹具](../../../../frontend/tests/fixtures/profile-structure.ts)。页面支持确切版本回显、共用目录选路径、完整根值编辑、读取失败重试、清空无效草稿；普通字段预览例不被拼成根值。
  - 第六段验证：首轮67项后端关联回归发现清空已使用结构返回500，已改为400并事务回滚；修复后最终`test_profile_structured_inputs`、`test_execution_profile_inputs`、`test_prompt_profiles`共68项通过（97.901s）。只读复核指出nullable文本列表类型读取及清空草稿/失败重试问题，均修复并补测；最终5个前端测试文件14项通过（13.987s），类型检查、Vite构建（10.58s，仅既有SettingsView大包提示）、Django check、无迁移漂移、严格OpenAPI生成校验通过。迁移0208仅测试库执行，未迁现用库。
  - 第六段页面证据：实际PromptProfileSettings使用显式隔离内存目录，验证确切版本与`$["动态.输入"]["action"]`回显、路径导航、完整根值输入及清空；390×844下文档scrollWidth=382、弹窗scrollWidth=364，无横向溢出，临时页关闭并恢复视口。夹具在预览请求边界显式报错，不伪造后端装配成功；后端装配由上述隔离API/服务测试验证，未验证现用页面、真实模型或完整鼠标/触屏覆盖。
  - 第七段（2026-09-28，实例媒体来源）：[实例策略适配器](../../../../backend/studio/services/execution_media_source_policy.py)复用M1-07共享策略校验/选择，原InputSpec仅增加可空`media_source_policy`保存方式/默认/改选/顺序；类型、数量、用途仍取原字段，无第二份可漂移约束。[0209迁移](../../../../backend/studio/migrations/0209_execution_media_source_policy.py)仅新增列，未迁现用库。API保存、读取、克隆、定义构建/完整性检查和原输入解析接通；策略未配置表示无额外来源限制，原模型/媒体要求不变，不是旧字段兼容。
  - 第七段请求/页面：配置策略时原请求输入明确提供`selection`，允许改选才提供完整`override`；整组替换、不读取废弃基础选择、不混来源、不自动换图。原Asset/已选择产物读者仍校验确切文件/运行上下文，上传核对原入库事实；逐次使用新增`source_selection`并进入原SHA，文本来源清单保持非媒体职责。[配置包装组件](../../../../frontend/src/components/ExecutionMediaSourcePolicyEditor.vue)和原请求编辑器复用共用来源控件/素材选择器，目录读取失败可重试；只读能力目录允许已登录业务用户读取，结构管理/媒体预览仍限staff。配对视频原音继续用未配置额外来源策略的原关联输入，Bible主图/变体目录仍待M9/M5。
  - 第七段验证：[后端测试](../../../../backend/studio/tests/test_execution_media_source_policy.py)与原来源/媒体/输入/实例合计105项通过（18.397s）；首轮26项的2个错误为测试路径和共享快照夹具错误，修正后进入上述组合。普通用户目录权限修正后20项复验通过（3.913s，和105项重叠，不累加）。[前端测试](../../../../frontend/tests/executionMediaSourcePolicy.test.ts)及原选择器/请求表单最终34项通过（17.853s，首轮3个主体目录夹具错误已修正），指南/设置47项通过；类型、Vite构建（10.64s，仅既有SettingsView大包提示）、严格OpenAPI、Django check、无迁移漂移通过。覆盖保存/克隆/移除/回滚、原文件暂存、上游范围、整组改选和用途/顺序，未将夹具合同视为真实发布/Smoke证据。
  - 第七段浏览器：[隔离夹具](../../../../frontend/tests/fixtures/execution-media-policy.html)使用正式组件和模拟目录，禁止真实API。验证库选确切ID进入本地请求、上传模式仅显示上传入口、上游上下文缺失禁用和禁止改选；桌面/390×844下无横向溢出（scrollWidth/clientWidth均382）、控制台无warn/error；临时页关闭、视口恢复。未进行真实上传/保存/准备提交或模型调用。API/架构/指南与根OpenAPI已同步；本段不是整个M1-08验收。
  - 下一段/未验：CR-M1-OUTPUT-01的G01/G02完成，G03阶段位置/重复使用仍待此前用户选择，仅暂停拓扑修改。接续配方/Profile的媒体策略保存/冻结/实际准备消费者；字段中心归M1-09、独立验收归M1-10。通用阶段选择、下游读取及结果字段页面未完成，未验证现用全链及真实模型产出。
  - 第八段（2026-09-28，配方固定素材/既有审核参考的来源策略接线）：[配方媒体选择适配器](../../../../backend/studio/services/production_recipe_media_selection.py)复用共享选择合同；原input_sources新增media_selection，只保存来源和有序item_key/purpose/asset_id，不复制实例数量/用途。原保存/读取/后继、RunPlan冻结及媒体来源展开接入；准备解析仍由原execution_input_resolution读取正式Asset。非默认来源须实例允许改选，上传检查原上传事实；固定图是本次生成输入，不是跳过生成直接当作成片。既有reviewed_references按实例明确的default_source封装，保留真实审核、镜头/主体/配对身份；实际来源不符报错，不自动切换读者。没有新增Schema、队列或真实调用。
  - 第八段页面：[RecipeMediaSelectionEditor](../../../../frontend/src/components/RecipeMediaSelectionEditor.vue)挂在原配方路线输入来源，复用MediaSourceSelectionEditor/ExecutionInputAssetPicker；来源目录、数量/用途、改选权限同源。固定选择只接受request范围的非配对媒体；逐镜关联继续走原审核参考，通用Bible主图/变体版本待M9/M5，Profile媒体消费者继续接线。加载/错误/上传中/非法原引用/只读可见，失败不写默认素材。
  - 第八段验证：[8项新增后端用例](../../../../backend/studio/tests/test_recipe_media_selection.py)覆盖纯合同、实际临时PNG/所有者/失效/上传事实、RunPlan冻结、原审核参考身份及原API保存/读取/后继；API目标是服务合同夹具，RunPlan与解析分层验证，不是完整实例发布→准备落库→视频调用的组合验收。首轮6项有1个夹具允许主机设置错误，已修复；随后52项关联测试中51项通过、1项后继测试遗漏published前置，修正后连同新参考封装在27项定向组合中通过（4.305s，重叠不累加）。30项前端关联测试通过（14.838s），47项指南/设置测试通过（4.776s）；vue-tsc、Vite build（11.59s，既有SettingsView分块提示）、Django check、无迁移漂移与严格OpenAPI验证通过。
  - 第八段浏览器：[隔离配方路线夹具](../../../../frontend/tests/fixtures/recipe-media-selection.html)拦截真实API，验证库选ID写入本地草稿、非默认上传模式只显示上传入口、缺失素材提示、未接入上游/Bible模式禁用、只读及390×844无横向溢出（scrollWidth/clientWidth=382），控制台无warn/error；临时页关闭、视口恢复。实际上传/业务配置保存/准备提交/模型未操作。该段不勾选整体M1-08；下一段为Profile媒体消费者及其余组合接线，G03既有阶段选择问题仍待用户确认。
  - 第九段（2026-09-28，声明式Profile媒体消费者）：复用原[参考素材投影](../../../../backend/studio/services/execution_reference_materials.py)、prepare_execution_profile及编译链，不在Profile另建来源策略/素材选择器；策略仍负责渲染，实例/配方拥有来源配置。逐次参考使用新增透传解析器的source_selection摘要，随原上下文及辅助字段请求冻结，不复制数量/用途，不按名字重新选图；来源摘要不是最终视频正文片段，原素材/Artifact身份、实际槽位和范围不变。没有新Schema/HTTP动作或额外门禁，动态文案简化仍归M5/M6。
- 第九段组合证据：[新增5项用例](../../../../backend/studio/tests/test_profile_media_source_policy.py)初次通过（42.968s），覆盖服务端实例发布→来源解析→准备保存→Profile编译→原工作流槽位，使用真实临时PNG且禁止网络；固定配方选择经过原适配器，不等于完整ProductionRun推进。上传改选保留有序身份，已保存准备不随当前名称或请求变化；缺素材/来源不符不生成准备。上游所选Artifact另验原读取/参考投影，保留真实产物身份，不伪造Asset，不查询当前实例/素材重选。Workflow的可用状态为隔离夹具，不是实际Smoke。最终5个相关模块合计40项通过（107.857s）：test_profile_media_source_policy、test_execution_reference_materials、test_execution_reference_fields、test_execution_reference_preparation、test_recipe_media_selection；该组已包含新增5项，不累计重叠测试；该段不勾选整体M1-08。
  - 第九段验证边界：此前夹具遗漏model_family、passthrough生成来源、JSON数组和服务端固定参数边界，以及可变测试合同被修改，已逐项修正；未降低业务断言或放宽正式校验。补[辅助字段范围测试](../../../../backend/studio/tests/test_execution_reference_fields.py)，确认来源摘要只进入对应单元。47项指南/设置测试通过（5.304s），Django check及无迁移漂移通过；当前没有HTTP/Schema/控件布局变更，不重复前端构建或浏览器流程。未迁现用库、改服务/配置、真实上传/模型、审核采用或清理业务数据。
- [x] **M1-09 字段中心与旧字典收敛**（P1；依赖：M1-08）。实现/产出：新系统设置分区、定义/版本/样例/引用影响页面；移除Profile字典CRUD及旧API入口，同步OpenAPI/类型/指南。验收：只有一个字段管理入口，Profile可跳转；残留搜索无运行中的旧别名/双读，有效定义未误删。完成：2026-09-29；以下四段完成代码及隔离验证，M1-10用户验收另列。
  - 第一段（2026-09-29，部分完成）：新增系统设置`fields`分区及[字段维护组件](../../../../frontend/src/components/FieldMaintenanceSettings.vue)，统一身份分页/搜索/启停、原定义版本管理、类型样例、内容来源维护及只读引用影响。Profile删除字典CRUD/独立版本弹窗，只选择确切版本并跳转`/settings?section=fields&field_definition={id}`；内容来源选择模式不再写配置，管理回到中心。后端身份路由、序列化器、客户端类型及注册对象资源类型统一为`field-definitions`/`FieldDefinition`/`field_definition`，删除旧名及重复Profile目录选项，无兼容路由或第二模型，不改ID/版本/已冻结合同；无Schema变更。引用影响复用原注册对象关联查询，失败/不完整不伪装无引用，删除为另一个明确动作。
  - 已验：[身份API测试](../../../../backend/studio/tests/test_field_definition_api.py)覆盖新路由、旧路由404、同ID/版本保持、搜索/启停；与规则值所有权/关联删除共29项通过（17.380s）。Profile CRUD及OpenAPI组56项中54项通过，两处断言仍漏列已批准G01的可选`output_version_id`，对照正式序列化器修正字段集合/必填/可空UUID后，定向2项通过（1.868s），未冒称56项整组已重跑。前端[字段中心测试](../../../../frontend/tests/fieldMaintenance.test.ts)、版本、规则值、平台基础、同源指南最终70项通过（13.477s），覆盖最小写入、空改动不提交、错误保留草稿、过期结果丢弃及来源选择零写入；删除旧CRUD断言和更新既有样例说明断言，不保留失效接口。vue-tsc通过，Vite通过（12.56s，既有SettingsView大包提醒）；Django check、无迁移漂移、严格OpenAPI校验通过。根OpenAPI直接覆盖生成两次遇Windows写入错误，改为临时文件生成后按差异补丁同步，SHA256完全一致，不留第二契约文件。
  - 隔离页面：使用[真实组件夹具](../../../../frontend/tests/fixtures/field-maintenance.ts)，拦截全部真实API；核对1280px桌面和390px窄屏、创建后版本窗口、已发布只读和失败保留草稿。浏览器点击未得到可靠事件结果，实际动作以键盘激活完成，不宣称鼠标全流程已验；窄屏无外层横溢（document宽382px、窗口390px）。[桌面截图](../../../../frontend/public/help/system-settings/09-fields.jpg)及[窄屏错误截图](../../../../frontend/tests/fixtures/field-maintenance-narrow.jpg)归档，临时页面关闭并恢复视口。computer-use技能用于隔离页面验证，不连接现用业务写入。
  - 第二段（2026-09-29，结构维护）：字段中心新增[输入／输出结构管理](../../../../frontend/src/components/DataStructureManager.vue)与[节点编辑器](../../../../frontend/src/components/DataStructureNodeEditor.vue)，复用原结构API和同源试填；可按页搜索、从明确版本克隆、修改节点层级/外部属性名/排序/确切字段版本/使用规则、保存、启用或停用。只提交修改，发布先保存当前编辑；已启用/停用正文只读。无效数字不转成0，未完成输入不能被旧校验结果冒充；移除节点不隐式删子树。字段选择扩展原组件的服务端分页目录，确切已选版本即使不在本页也单独读取，不默认选最新。约束目录新增只读`authoring-options`，与原编译器共用`CONSTRAINT_SCHEMAS`，状态和节点初始值来自原模型/服务；没有新增业务字段枚举、Schema迁移或执行入口。
  - 第二段验证：[结构后端21项](../../../../backend/studio/tests/test_data_structures.py)通过（1.178s），新增作者目录同源/零写入/权限用例。首轮20通过1错误，原因是目录误用不存在的模型Status类；改为模型字段choices后整组21项重跑通过，未降低断言。[OpenAPI定向结构用例](../../../../backend/studio/tests/test_openapi_schema.py)1项通过（0.758s），严格生成及校验通过，根契约与临时生成结果SHA256一致。[管理器/节点/全局字段选择测试](../../../../frontend/tests/dataStructureManager.test.ts)连同字段中心/定义版本/同源表单共10项通过（8.242s），既有字段选择/结构选择/试填共11项通过（6.823s）；包括空白/非法数字不提交、空改动零写入、保存失败保留、保存后启用、迟到结果忽略、null/false保持，不累计此前重复轮次。
  - 第二段隔离页面：[夹具](../../../../frontend/tests/fixtures/structure-maintenance.ts)运行真实组件并拦截全部真实API，验证1280px桌面、390px窄屏、字段节点编辑、保存失败保留、内存启用后只读；动作使用键盘激活，未冒称完整鼠标验证。窄屏document为390/390px，两个字段分组scrollWidth等于clientWidth，控制台无error/warn；表格在自身容器横向滚动。[桌面失败保留截图](../../../../frontend/tests/fixtures/structure-maintenance-desktop.jpg)与[窄屏截图](../../../../frontend/tests/fixtures/structure-maintenance-narrow.jpg)归档。computer-use技能用于隔离交互核验，不代表真实后端持久化、配置发布或业务验收。临时浏览器页已关闭，视口恢复。
  - 未完/下一段：字段中心映射预览及完整引用影响/导航联测仍待接续，M1-09保持未勾选；之后M1-10模块交付及用户确认。正式API/架构/操作说明和同源设置指南同步结构入口，不代表现用后端或页面已加载。无用户决策阻塞；未迁现用库、改服务/配置、真实模型/Smoke/审核采用或清理业务数据。
  - 第三段（2026-09-29，字段中心映射预览）：新增[只读试配页面](../../../../frontend/src/components/DataStructureMappingPreview.vue)，选择确切来源/目标结构并粘贴原始JSON；草稿可试配，但不等于允许执行。路径/稳定key引用与对象/列表/逐项/固定值复用[原组合编辑器](../../../../frontend/src/components/RecipeStructureMappingEditor.vue)和[原解析服务](../../../../backend/studio/services/structure_mapping.py)，没有第二份映射存储或执行逻辑；新增只读mapping-options，方式/作用域来自校验器同一目录。前端仅遍历Schema提供属性与列表路径，不执行映射。显示原始值、来源解析值、目标结果、默认值及取值路径；修改原文/映射/结构或出现未完成数字会废弃旧预览，迟到响应不能覆盖新状态。切换结构保留已有映射并提示核对，不自动重选字段；运行消费者仍只选可用版本。
  - 第三段测试：[映射API/服务](../../../../backend/studio/tests/test_structure_mapping_preview.py)及共享装配首轮31项通过（3.460s）；补非法scope类型用例后，与结构OpenAPI用例合计32项通过（3.332s，不含建库），最终OpenAPI目录断言单项再验通过（0.782s）。所有后端测试均进程内SQLite内存库，目录/预览查询断言零配置写入。前端[预览专项](../../../../frontend/tests/structureMappingPreview.test.ts)、原配方编辑器/结构选择/字段中心24项通过（13.682s），同源表单/配方作者22项通过（8.929s），指南/设置47项通过（4.743s）；各组不与之前轮次累加。首轮24项中4项失败：3处旧测试监听把新增draftValidity事件误写为modelValue，另1处目录参数断言未同步；修正事件夹具和明确published筛选断言后整组复验，不降低业务断言。没有代码或页面字段名依赖特定剧本。
  - 第三段隔离页面：[夹具](../../../../frontend/tests/fixtures/structure-mapping.ts)加载真实组件、禁用全部真实API，以明确标注的固定回执验证展示（解析算法由后端测试验证，不伪称浏览器集成调用）。1280px桌面和390×844窄屏验证草稿选择、路径明确选择、预览值/false显示、改原文立即隐藏旧结果、错误保留原文及行列信息；键盘激活，未冒称完整鼠标流程或全部嵌套映射浏览器验收。窄屏document为382/382px，无横向溢出，控制台无warn/error；[桌面](../../../../frontend/tests/fixtures/structure-mapping-desktop.jpg)与[窄屏](../../../../frontend/tests/fixtures/structure-mapping-narrow.jpg)已归档。computer-use技能用于隔离页面核验，临时页已关闭并恢复视口。
  - 第三段边界：vue-tsc及Vite构建通过（12.52s，既有SettingsView大包提醒仍在），Django check、无迁移漂移、严格OpenAPI生成/校验通过；根契约与临时生成件SHA256完全相同后仅删除临时契约（可重新生成），没有业务数据/文件删除。指南1.3.9、API/架构/操作说明同步，校正3.7节过时的拟议接口口径。未做现用DB迁移、服务加载/重启、配置发布、真实模型/Smoke/审核采用或全仓/模块用户验收。下一段为完整引用影响与导航联测，M1-09保持未勾选；无新增需求决策阻塞。
  - 第四段完成（2026-09-29）：[原关联查询](../../../../backend/studio/services/registry_relations.py)纳入field/field_version/structure/source，计数与分页明细同一外键查询；重复节点/端口只计唯一目标，同名不同版本不合并。沿来源/输出、策略、实例继续查原配方引用，不复制关系表、不把当前配置图当全部冻结请求/产物扫描。字段引用影响改用原[关联摘要](../../../../frontend/src/components/FieldMaintenanceSettings.vue)，删除影响仍留原单独删除流程；[定义](../../../../frontend/src/components/FieldDefinitionVersions.vue)、[结构](../../../../frontend/src/components/DataStructureManager.vue)、[来源](../../../../frontend/src/components/ContentSourceManager.vue)管理器支持确切版本深链接与返回关联，含当前列表外/停用版本，错误不改选最新。
  - 第四段验证：[关联测试](../../../../backend/studio/tests/test_field_registry_relations.py)及原注册关联组合15项通过（65.630s；不含建库），覆盖逐层计数/明细、分页搜索、403/404、同名/重复引用、零写入、单SQL及EXPLAIN索引命中。首轮因夹具缺category、旧计数断言/SQL投影漏新增分组为1失败2错误，修正后全组通过；不隐去失败轮。前端管理/导航15项通过（9.965s）；首轮14通过1失败是新增草稿确认缺window模拟，补齐确认夹具后通过。SSR控制器测试未挂载真实RouterView的守卫提示保留；真实路由隔离页无此提示。vue-tsc/Vite通过（11.81s，既有SettingsView大包提醒）；严格OpenAPI生成/校验通过，指南1.3.10及设置47项通过（4.918s）。
  - 第四段页面：[隔离导航夹具](../../../../frontend/tests/fixtures/field-references.ts)在1280px桌面与390px窄屏走通字段→定义版本→停用版本配置→引用结构→分页外结构配置→返回关联；原组件、固定展示回执，所有真实API/写入禁用。窄屏文档宽度382/382，控制台无warn/error；[桌面截图](../../../../frontend/tests/fixtures/field-references-desktop.jpg)、[窄屏截图](../../../../frontend/tests/fixtures/field-references-narrow.jpg)。键盘Enter导航不等于所有鼠标/全部消费者页面或现用后端联测。来源深链接由控制器测试验证，未宣称本轮浏览器验证全部来源/配方页面。未验证运行库性能P95、现用迁移/服务加载、真实模型或用户验收；无新需求阻塞。源码旧字段CRUD/别名残留搜索为空，历史迁移和404断言保留。
- [x] **M1-10 模块交付与用户验收**（P1；依赖：M1-09）。实现/产出：关联后端/前端/隔离迁移验证及配置样例。验收：M1-A01布局A/B、M1-A02非剧本自定义字段、M1-A03空值矩阵、M1-A04媒体来源均通过；用户可在页面复现，未真实执行部分单列。完成：2026-09-29；方向及5.4现用试用操作已确认，0212也明确获准。A01–A03现用页面、A04现用作者保存/重开与隔离消费者/后端联合通过，证据与真实执行未验边界见5.4。按用户最新要求以开发验收和交接勾选，用户手动试用异步、不阻塞后续任务；不冒称模型产出或整条短剧链通过。

<a id="54-优先级依赖与验收边界"></a>

### 5.4 优先级、依赖与验收边界

首个开发模块；01–08先形成完整配置闭环，09–10完成收敛和交付。不需要先生成真实剧本/媒体，不承担未知文本语法、全新协议或所有业务动作的自动生成。

<a id="m1-10页面复现与交付核对2026-09-29开发验收完成手动试用异步"></a>

#### M1-10页面复现与交付核对（2026-09-29开发验收完成，手动试用异步）

唯一勾选仍为上方M1-10，不另建任务清单。用户已于2026-09-29明确确认M1方向及下述迁移、平台重载、样例写入，并单独批准0212一并迁移；以下四组按约定完成开发验收，不重复审批技术方向。现用页面与隔离消费者/文件链证据分别列出，不把夹具称为现用生产；真实模型执行不属此次验收。用户手动试用结果不作为后续开发前置。

| 验收项 | 页面复现及预期 | 已有代码/隔离证据与尚待核对 |
| --- | --- | --- |
| M1-A01 两种布局 | 字段维护创建所需对象/列表/文字/布尔定义；输入／输出结构分别配置下方A/B和目标形状。在字段映射预览中选确切来源/目标，用对象＋逐项映射将两种外部属性名映射到同一目标；动作、情绪、false均应相同，原始路径应分别保留。 | 2026-09-29已用正式页面保存启用A/B/目标v1，并调用实际映射预览验证两种来源输出同一目标；B的5条来源路径与确切版本已逐项展开核对。原worker与多镜证据见[布局测试](../../../../backend/studio/tests/test_structure_mapping_preview.py)及5.6；用户可异步复现，不以夹具回执冒充本次真实解析。 |
| M1-A02 非剧本字段 | 登记检查读数inspection.reading（小数）与inspection.accepted（布尔），在独立对象结构中设外部属性reading/accepted，试填0/false。控件、Schema、目录和只读结果应出现新字段，不改组件/枚举，也不自动创建剧本、Bible或素材。再把accepted加入目标结构并显式映射。 | 2026-09-29正式页面独立字段/结构、0/false表单与解析通过；本轮从value_matrix到inspection显式映射reading/accepted，实际结果为0/false、两条确切来源路径正确且无默认注入。均为试用配置，不是新增内置字段；[实际映射证据](../../../../frontend/tests/fixtures/m1-trial-extension-mapping-live.png)。 |
| M1-A03 空值矩阵 | 在结构节点分别配置必填、可选、明确缺键默认与nullable，逐次试填缺键、null、空串、空列表、0、false。只有缺键用配置默认；其余保留原值并按节点规则通过或显示具体错误，原文/默认来源分开。 | [结构值矩阵](../../../../backend/studio/tests/test_structure_values.py)原隔离证据保留。本轮现用value_matrix验证缺键显式默认0/false/missing、nullable保留null、空串/空列表保留、无默认可选键仍缺失、默认来源精确可查；前轮非法null/缺键具体路径错误通过。修复原始表单忽略明确默认而误报必填；默认仅在解析结果应用，未回填原文。 |
| M1-A04 媒体来源 | 原实例/配方/策略表单选择配置允许的已有素材、上传入库、本次选择或确切上游已选结果，检查种类、用途、顺序及实际来源；缺失结果不得自动猜选。纯结构样例只允许选已有素材，不把预览扩成上传/执行。 | 现用纯样例已有图片选择/读取/解析及重开/390px通过；独立实例草稿三种来源、默认、改选保存重开通过。正式消费组件隔离验证确切库内引用、上传专用入口、禁止改选、上游缺失、配方固定选择/顺序/数量；[共用媒体来源](../../../../backend/studio/tests/test_input_source_policies.py)、[实例策略](../../../../backend/studio/tests/test_execution_media_source_policy.py)、[配方选择](../../../../backend/studio/tests/test_recipe_media_selection.py)29项后端隔离验证正式上传API、确切字节与上游选择、冻结/替换/错误不回退通过。现用上传/业务采用/模型字节效果及原生确认分支未完整实测，不据此宣称真实执行验收。 |

布局A、B与预期目标（仅配置样例，不写入项目）：

```json
{
  "A": {"episodes": [{"shots": [{"action": "转身", "mood_note": "紧张", "off_screen": false}]}]},
  "B": {"chapters": [{"frames": [{"motion": "转身", "tone": "紧张", "off_screen": false}]}]},
  "target": {"episodes": [{"shots": [{"action": "转身", "mood": "紧张", "off_screen": false}]}]}
}
```

实际映射样例分别粘贴A或B的值，不把上面的A/B/target外层说明对象当单一来源；根列表来源选episodes或chapters，当前条目列表选shots或frames，再将当前镜头的action/motion、mood_note/tone、off_screen映射为目标的action、mood、off_screen。每层明确选择root/item作用域，不依赖隐藏字段或默认最新版本。

2026-09-29组合核对：新增[正式配置API验收](../../../../backend/studio/tests/test_field_module_acceptance.py)三项，通过原字段身份/定义版本/结构创建及启用接口保存配置，再走真实Schema/路径目录/值与映射预览；不是固定响应网页。A01两种嵌套布局共用字段身份，覆盖多个镜头及空列表、原始路径；A02配置新增inspection.reading/accepted后新结构可消费0/false，原结构不变，未知内容保留原文；A03覆盖缺键/nullable/空串/空集合/0/false及默认来源，预览零SQL写入。A04复验原媒体来源策略与确切文件字节读取，生成来源、已有与上传入库选择不混用；Bible素材版本reader仍属于M9/M5后续接入，当前目录明确不可用，不提前开放。

- 后端命令：进程内`DATABASE_URL=sqlite:///:memory:`，`manage.py test studio.tests.test_field_module_acceptance studio.tests.test_structure_values studio.tests.test_execution_media_sources studio.tests.test_execution_media_source_policy studio.tests.test_structured_text_combination --noinput`。首轮31项中30通过、1项因测试进程找不到FFmpeg在夹具准备时出错（25.048s）；仅向重验进程设置仓库既有FFMPEG_BINARY/FFPROBE_BINARY路径后，整组**31项通过（52.737s，不含建库）**。组合用例沿原配置→冻结→worker假Provider→选择→下游；临时媒体由FFmpeg合成，不是真实模型产出。
- 前端：`node --test --test-concurrency=1 tests/structureValueForm.test.ts tests/structureMappingPreview.test.ts tests/executionMediaSourcePolicy.test.ts` **16项通过（10.969s）**，覆盖Schema控件、空值、陈旧响应、路径编辑及媒体选择。仅SSR/控制器验证；桌面/窄屏证据沿用5.3的实际隔离页面，不声称本轮新增浏览器端到端验证。前端源码及HTTP合同本轮未改，不重复构建或生成OpenAPI。
- 前轮交付缺口为现用页面复现及样例交接；已由下述2026-09-29页面及媒体组合增量补齐。用户手动试用单列且不阻塞整体进度；真实模型和媒体生成不纳入本次页面试用。M2/M3业务writer仍有独立任务，不能把字段基础通过称为整条短剧链完成。

<a id="现用页面试用操作范围2026-09-29已授权并执行切换"></a>

##### 现用页面试用操作范围（2026-09-29，已授权并执行切换）

本节落实用户已确认的M1方向，不新增产品需求或另一份验收清单。页面入口为“系统设置→字段维护”（`/settings?section=fields`）；仍逐项记录上表M1-A01～A04，开发验收完成并交接后勾选M1-10；用户手动试用不作为勾选或后续开发门禁。

**切换前只读快照**：现用数据库是`backend/db.sqlite3`（SQLite），当时`showmigrations`显示已应用至0208；最初`migrate --plan`列0209、0210、0211，之后用户另批准新迁移0212。现用API/前端端口8000/5173与项目进程对应，ComfyUI端口8188存在。生成Task/Attempt、独立素材生成、两类Smoke均仅有终态记录，AssemblyJob为空；两条未结束ProductionRun均为manual（一个running、一个awaiting_input）。0210涉及的原选择记录没有selected_by为空的记录。实际执行前已重查，执行结果见下文；没有取消、继续或重试这些Run。

| 顺序 | 拟执行内容及具体影响 | 验收/停止边界 |
| --- | --- | --- |
| 1 | 使用原`scripts/backup.ps1`建立一次现用SQLite备份，范围仅数据库，不复制媒体、ComfyUI产物或秘密配置。 | 保留回退手段，不新增审计/费用门禁；不删除现有文件。 |
| 2 | 原Django迁移入口应用0209～0212：0209增加实例媒体来源策略；0210增加自动/人工选择模式及操作者约束；0211建立素材主图/变体系列与版本关系；单独获准的0212登记人工失败主调用重试动作选项。 | 0211属于当前代码依赖的M9关系Schema，不代表M9生成验收；0212不转换业务数据。不重写历史请求、生成记录或创建素材关系数据。0210沿迁移默认把既有人工选择标为manual，执行前再次核对操作者约束；不另做历史数据转换或清理。 |
| 3 | 沿原`scripts/restart.ps1 -SkipComfyUI`加载新平台代码；该入口会停止/重启本项目API、前端、生成/文本/异步媒体/装配worker和maintenance，并执行既有装配恢复检查，不是只重启页面。 | ComfyUI不启动、不停止、不重启；不执行任何cleanup/cutover参数。执行前若新增非终态生成/Smoke/装配或自动推进的配方运行，先停止本次切换并报告，不猜测允许执行。标准后台探测/维护仍属该启动入口原行为，不把它宣称为零后台活动。 |
| 4 | 通过原页面/API创建`m1_trial_20260929`前缀的独立字段定义版本、A/B及统一目标结构、非剧本/空值样例与必要内容来源/输出合同；按表单要求启用这些样例版本，实际映射只做预览。 | 不覆盖现有配方、实例、策略或系统默认；需要引用时使用独立草稿，不发布执行配置。不创建或继续ProductionRun，不调用模型、Smoke、生成或实际采用。 |
| 5 | 实际页面验证A/B映射同结果、非剧本0/false字段、六类空值及错误路径、媒体来源选项/确切身份；保存后重开验证，检查桌面/窄屏。 | 媒体优先选登录用户已有有效素材，只预览，不改绑定或采用；若无合适素材，具体上传文件先明确。上游真实产物与模型字节效果留待后续模块，不伪造成功。约定开发验收及交接完成后勾选M1-10并告知手动试用；用户未反馈不阻塞后续工作。 |

**范围不含**：旧运行/配方/审计清理、历史兼容、真实模型/Smoke/生产重试、素材生成、现用项目审核采用、ComfyUI操作和PostgreSQL。试用产生的样例保留并列明对象，后续清理另核对确切范围。上述迁移/服务/样例写入已获明确授权；计划内验收及缺陷修复不重复询问。

**2026-09-29实际执行证据**：切换前复核现用SQLite到0208，生成Task/Attempt、独立素材生成、两类Smoke及装配均无非终态，两条manual运行保持原状；无空操作者的既有选择。沿原`backup.ps1 -SkipMedia -SkipComfyUIOutput -BackupName m1-trial-20260929-before-0209-0212`完成数据库备份，目录`backups/m1-trial-20260929-before-0209-0212`，150753280字节，SHA256 `f04c965e65697de3a363aa42e020b83e67d4e52bd45b002da0a92505714f17a5`。沿原`restart.ps1 -SkipComfyUI`成功应用0209–0212并重载平台，API/前端/就绪检查通过，装配恢复0项；`migrate --check`通过。ComfyUI监听PID12036前后未变，未操作该服务。切换后上述执行对象无新增非终态、两条manual运行状态未变。0212仅登记重试动作选项，不转换业务数据。未触发真实模型/Smoke/重试/采用或删除。

**现用页面发现并修复**：字段分页API适配器误用通用`unwrap`丢失DRF分页`count`，导致“字段目录响应不完整”；修复为保留分页信封并补HTTP适配回归，[字段维护测试](../../../../frontend/tests/fieldMaintenance.test.ts)4项通过（2.927s，原SSR RouterLink告警不等于浏览器错误）。重载实际字段页面后显示42条既有字段，随后经正式页面新增独立样例。后续记录具体配置及A01–A04结果，不提前声明整体验收完成。

**前轮页面样例与结果（2026-09-29）**：正式页面已创建并启用六个`m1_trial_20260929.*`字段v1（reading/accepted/object/list/text/media）及四个同前缀结构v1（inspection、layout_a、layout_b、target），未修改原配方/实例/Profile或系统默认。inspection结构ID为`58a0c4dc-3252-4c2d-be07-ec84570f0fa0`，A/B/目标结构ID依次为`45f97d71-892d-4346-93a7-77378022062a`、`faa8ec8f-d22f-4fec-becb-582fa2f31678`、`d9c1a48a-b2ce-4966-a93d-fffc54c02b64`；原42条字段增至48条。A/B均经实际API映射为上方target，B展开的5条来源路径保留chapters/frames/motion/tone/off_screen及确切结构版本。inspection的0/false表单与实际解析通过，[页面证据](../../../../frontend/tests/fixtures/m1-trial-inspection-live.png)。A03增量：允许的空串和空列表保留原值；禁止null时显示`source.chapters[0].frames[0].motion`类型错误，缺必填字段显示相同路径的缺失原因，不静默补值；恢复有效样例后预览重新通过。以下接续本轮新增证据，不把前轮切换/测试重复计为本轮操作。

**本轮现用矩阵与扩展映射（2026-09-29）**：通过正式页面新增并启用`m1_trial_20260929_value_matrix` v1（`924d8875-2b83-4cb0-9f59-9d375cad4ff7`），复用上述六个定义，结构总数增至五个。reading/accepted必填并配置缺键默认0/false；note可选、nullable且缺键默认`missing`；items是可选文字列表；optional可选且无默认；media可选且限制image。第一次启用时未声明媒体种类，服务端准确指出node_8，补齐明确类型后通过，不绕过类型检查。实际`{}`仅得到三个明确默认及来源，无默认键不补值；note:null保留null，note:""和items:[]保留原值；随后显式将reading/accepted映射到inspection，实际结果为`{"reading":0,"accepted":false}`，两条来源路径及精确版本可查、默认注入为零。

- 页面缺陷与修复：[结构试填](../../../../frontend/src/components/DataStructureValuePreview.vue)向原编辑器传入同源默认Schema，消除“已声明默认仍误报必填”，不回填原文；[共享编辑器](../../../../frontend/src/components/StructuredJsonEditor.vue)仅当同一对象内字段标签重复时附外部属性名，避免复用文字定义后note/optional不可辨；[纯结构媒体预览](../../../../frontend/src/components/StructureMediaValue.vue)明确禁用上传，原业务选材上传能力保留，不改变输入来源合同。
- 媒体与响应式：选择已有图片“沈念”，实际读取预览和结构解析通过，不上传、不创建素材、不改绑定/采用。关闭重开已发布结构后试填值仍为缺失，没有把样例存成默认。390×844视口的文档宽度390px、对话框374px，未外溢，预览按钮可用；[桌面证据](../../../../frontend/tests/fixtures/m1-trial-value-matrix-live.png)、[390px证据](../../../../frontend/tests/fixtures/m1-trial-value-matrix-narrow.png)、[非剧本映射证据](../../../../frontend/tests/fixtures/m1-trial-extension-mapping-live.png)已归档。使用computer-use技能核对真实页面，不将图片外观质量当作本次验收。
- 验证：`node --test --test-concurrency=1 tests/structureValueForm.test.ts tests/structuredInputForm.test.ts tests/structureMappingPreview.test.ts tests/executionMediaSourcePolicy.test.ts tests/inputSourcePolicies.test.ts`最终**30项通过（16.020s）**；覆盖同源默认、同名标签、媒体禁止上传、迟到结果及来源策略。vue-tsc通过，Vite通过（13.03s，既有大包提示）。前一条命令误写不存在的策略测试文件名，只执行到16项的轮次不算策略验证；以上以正确文件名重验。不改后端/Schema/HTTP合同，本轮未重复后端整组或OpenAPI生成。
- 工具及边界：旧试配标签的浏览器原生移除确认导致自动化焦点命令超时，未对其数据做猜测性修改；同站新标签完成上述验收，收尾时旧标签已成功关闭。正式字段页保留供试用。此段只读查看既有实例端口/Profile结构选项，未保存原配置；以下另列独立实例草稿及隔离消费者增量。未重启服务、迁库、触发模型/Smoke/重试、业务审核采用或删除。

**M1-A04组合收尾与交付（2026-09-29）**：沿现用正式页面创建独立实例`m1_trial_20260929_media_sources`（M1试用 · 媒体来源（仅草稿）），v1版本ID`68ca86d6-fb51-4a99-93f8-b9f3ef10f8ac`。引用已有H3参考生视频目标只为获取真实输入合同；images端口0–9、request范围，允许asset_library/asset_upload/selected_output，默认asset_library、允许整组改选、provided顺序。保存重开值一致，未自动选择任何素材，Bible版本读者明确未接入。草稿未发布、不可作为本次生产已就绪证明；未改任何既有实例或运行。[桌面](../../../../frontend/tests/fixtures/m1-trial-media-policy-live.png)及[390px](../../../../frontend/tests/fixtures/m1-trial-media-policy-narrow.png)证据；窄屏文档390/390px、对话框374px无外溢。

- 正式消费者隔离验收：使用既有[实例页面夹具](../../../../frontend/tests/fixtures/execution-media-policy.html)及[配方路线夹具](../../../../frontend/tests/fixtures/recipe-media-selection.html)，全部真实API被拦截，仅内存样例。实例初始缺素材有明确数量错误，库内选择保留确切Asset ID，纯上传模式仅出现上传按钮，不上传；禁止改选时来源选择禁用，配置上游但无运行上下文时明确不可用、不回退。配方固定素材明确区别于上游，选定一个后本地形状有效；第二个缺失时显示缺失并禁用超上限添加，移动后原Asset与item_key不变，390px文档382/390px无横溢，[顺序与错误证据](../../../../frontend/tests/fixtures/m1-trial-recipe-media-narrow.png)。这不是现用配方保存或生产请求成功。
- 后端组合：进程内`DATABASE_URL=sqlite:///:memory:`，`manage.py test studio.tests.test_input_source_policies studio.tests.test_execution_media_source_policy studio.tests.test_recipe_media_selection --noinput` **29项通过（5.819s，不含建库）**；正式multipart上传仅写隔离临时媒体，覆盖读取确切文件字节、实例保存/克隆、配方原冻结选择、上游选择上下文、数量/用途/顺序和错误不回退。没有Provider/模型/现用数据库操作。前端本轮最终30项保持通过；清理过时测试标题后来源策略组10项复验通过（4.538s），不累计重复测试数。
- 未完整实测：带已有素材切换来源时的浏览器原生confirm在自动化中超时，无法据此确认该浏览器分支；相同整组替换合同由前后端测试覆盖，未为工具限制改动业务行为。关闭所有受影响临时标签后其他输入操作恢复，现仅保留字段维护页。现用库上传、真实上游生产/采用与模型效果未执行；不发布试用执行配置来代替授权。它们不扩大为已批准M1基础验收的生产门禁。
- 交接：系统设置→字段维护→搜索`m1_trial_20260929`可查看六个字段；输入／输出结构中同前缀五个结构可试填/映射。模型→管理执行实例可查看上述未发布来源策略草稿。样例不会自动进入现有项目/配方，来源说明/实际值/默认来源分别可查。M1-10按A01–A04约定开发范围勾选，主项20/91；用户可异步手动试用，不等待反馈，下一项M2-01。无新增需求或需用户决定事项。

<a id="55-m1-01字段归属盘点2026-09-28"></a>

### 5.5 M1-01字段归属盘点（2026-09-28）

范围是仓库当前Schema、写入器和消费者；没有读取或修改现用数据库，以下种子示例不冒称现用字典全量。核对入口：`models.py`的字段定义及Profile规则；`platform_serializers.py`字段CRUD；`platform_views.py`字典选项及发布引用限制；`prompt_profiles.py`类型校验、合同编译和装配；`execution_profile_inputs.py`逻辑输入目录。

| 当前信息/写入方 | 唯一目标归属 | 保留、迁移与清理 |
| --- | --- | --- |
| 字典`id/key` | 字段身份 | 原表模型重命名保留ID/key，引用不按显示名重建；不自动合并同名不同语义字段 |
| `label/category/description/impact/value_type/value_options` | 字段定义版本 | 类型、含义、枚举及新增单位进入确切版本；管理显示名与外部属性名不能重解释已冻结值；最终移除身份表上的重复定义内容 |
| `is_active/display_order` | 字段身份的管理元数据 | 启停与列表排序仍由字段中心管理；序号和版本号服务端生成，示例值不是任务值 |
| 字典`is_required`、规则`required` | 各结构节点/使用规则 | 全局必填删除；每个使用位置独立设置，不能继续在读侧对二者取OR |
| `default_value`及`definition.default` | 明确使用处的固定值/缺失默认 | 固定系统指令属于Profile规则的显式固定来源；其他默认进入具体使用处且仅缺键时应用。不按空串/null/空列表偷偷回退；无法归属的旧开发配置按CL范围清理，不自动推断 |
| Profile的来源、语言、生成指令、校验建议、顺序/标签/分隔符/通道/长度 | `PromptProfileFieldRule`及Profile版本 | 保留策略职责，引用字段版本；不让策略重新定义字段类型或全局默认 |
| 模型原生参数/取值约束 | 所选执行目标及实例合同 | 字段中心只引用/展示，不能复制`fps/seed`等参数产生第二份可冲突默认值 |
| 项目/剧本/镜头/素材实际内容 | 原业务修订、素材及执行准备 | 仍由原业务写入器负责；字段配置不存内容、不直接创建业务实体 |
| `preview_example`及预览样例服务 | 只读示例 | 不落为默认值，不参与真实准备解析；新增字段无示例也不自动取邻近字段的内容 |

消费者收敛清单：

- `prompt_profiles.py`：M1-02续段已切为规则自己的必填、固定值和缺键默认，schema8已冻结确切定义版本；身份重复列仍待清理，M1-04继续完整嵌套结构的缺失/null/空值合同。
- `execution_profile_inputs.py`：M1-02续段已按规则自己的必填和默认派生输入，不再对全局必填取OR；M1-08继续同一字段/结构版本的统一输入接入。
- `content_sources.py`及`prompt_source_catalog.py`：已将`definition.default`写入来源替换为本规则`fixed.value`并同步维护位置说明；不新增第二套来源目录。
- `execution_preparation_fields.py`、`execution_field_bulk_review.py`、`execution_text_response_review.py`：当前按key取标签；保留业务事实与修订，用确切版本区分历史含义，显示名不充当关联身份。
- `PromptProfileSettings.vue`、`platformApi.ts`、`platform_serializers.py`、`platform_views.py`、`urls.py`：M1-09收敛页面与API，只保留字段中心一个CRUD入口；不保留旧路由别名。
- `registry_deletion.py`、`platform_lifecycle_audit.py`、测试夹具与迁移：切换模型引用并验证保护关系；历史迁移里的旧模型名仅表达当时Schema，不是运行时兼容层。

类型和同名歧义的明确处理：旧种子包含text/long_text/string_list/boolean/number/enum。`voiceover`种子表示旁白正文（long_text），不等于对白的画外音布尔值；新布尔字段用独立ID，外部同名属性靠显式映射区分。`reference_assets`的string_list是文字列表，不自动转换为Asset引用。旧number不根据`fps/seed/duration_seconds`名称猜整数或单位；新版本由配置明确类型/单位。现有系统指令、叙事/动作、视觉/镜头、连续性、对白/声音、格式/时长、参考说明、负向/质量及运行参数等分类可保留有效ID，不把这些分类固化为允许的业务字段白名单。当前没有需要用户新增决策的归属冲突；若实际清理或使用处出现无法确定的值，按第1.2节提出而非猜测。

<a id="generic-structured-text"></a>

<a id="56-cr-m1-output-01通用结构化文本2026-09-28已批准"></a>

### 5.6 CR-M1-OUTPUT-01：通用结构化文本（2026-09-28已批准）

<a id="561-背景目标与明确不做"></a>

#### 5.6.1 背景、目标与明确不做

批准依据：用户明确“确认新增‘通用结构化文本’能力”。现有`structured_json.v1`已经能维护确切输出结构并只读解析样例，但主调用、候选读取及配方来源仍有剧本/影视规则专用分支；不能把配置预览成功当成任意结构的实际生产能力。

目标是：配置输入及提示词→原执行准备/模型调用→按冻结输出结构解析→保留实际原文与候选→按配置自动或人工选择→配方下游按目录路径读取。新增业务字段名、层级、列表布局不修改解析器/结果组件；示例可用剧情之外的任务说明、标签集合等，不能用例子名字写死处理。

- 新能力显示名“通用结构化文本”，技术operation采用`text.structured`；它不是`prompt.draft`、`text.generate`或`script.structure`的别名。注册输出适配器为`text.structured/v1`，解析仍唯一调用`structured_json.v1`及`parse_structured_output`。
- 复用已实现chat传输及`text_execution.operations`参数/请求选项，不建立另一套模型、Provider、队列或预算。输入使用既有实例逻辑输入/Profile结构化来源；不为故事、角色等业务字段增加接口白名单，不隐式读取某个项目或最新素材。
- 输出版本只能选择确切已发布的通用JSON BusinessOutputVersion；结构来自其DataStructureVersion快照。生成要求来自明确手写通道或Profile，协议层只传递所需JSON结构，不暗加创作规范。对象、列表、标量及可空根按所选结构判断，不强制套一个剧本字段壳。
- 不自动创建ScriptRevision、Bible、镜头、素材或任意数据库实体；这仍由M2/M3等唯一业务writer消费结构化值后处理。不是任意脚本执行、通用文本语法DSL、新可视化DAG或全协议Provider扩展。
- 不加入新的人审门禁、评分系统或费用核实前提；自动选择沿R04。既有产物/选择事实若缺少必要的模式或合同引用，只补明确字段/关系，不另建一套候选记录。

<a id="562-唯一数据链与失败行为"></a>

#### 5.6.2 唯一数据链与失败行为

| 环节 | 实现/事实归属 | 必须保持的语义 |
| --- | --- | --- |
| 配置/请求 | 原ModelExecutionInstanceVersion、BusinessOutputVersion、ExecutionPreparation及冻结请求 | 保存确切输出ID/版本/摘要/结构；预览和准备读同一合同，不能读取发布后的新版本重新解释返回 |
| 实际调用 | 原GenerationAttempt和文本worker，原chat传输 | 请求与返回关联唯一Attempt；成功、失败、未知提交、重复领取按原状态处理，不新建队列或自动盲目重发 |
| 原文/候选 | 原ExecutionTextResponseReview原文文件及GeneratedArtifact | 原始返回不被默认值/格式化结果覆盖；通用产物角色明确为structured_text_candidate，不冒充影视规则或剧本；存储失败不能生成可用候选 |
| 解析 | parse_structured_output及共用结构解析/路径服务 | 保留原值、解析值、默认来源、未映射字段提醒和具体错误路径；false/0/null/空集合按配置，不吞未知字段；类型/结构错误不能标成可采用 |
| 选择 | 原ArtifactSelectionDecision及原运行处理事实 | 自动/人工模式与来源明确；自动按本次候选序号取首个满足基础条件的成功项，不伪造人审；已被下游引用的选择不因晚到结果静默替换 |
| 下游 | 原RunPlan来源目录、selected_output解析及字段路径映射 | 引用确切上游步骤/产物/选择/输出合同；不查“最新成功”。目录的类型、默认及路径从该步确切输出结构生成，不重新写字段表 |

多个候选的基础失败不应一律阻止采用其他已成功候选；具体候选失败仍展示。结构无效时保留可读原文与路径错误，复用现有修正/再次处理入口；查看、下载和解析已有内容不触发模型。创作质量只提醒，不额外加入“审核通过才可生成”的固化步骤。

配方增加通用结构化文本职责，使用既有阶段、路由和依赖表达；不把本次能力扩成任意DAG。**2026-09-28用户明确“确认方案1”：通用结构化文本可以在上游依赖满足的位置插入，并可在同一配方重复使用；首版仍为顺序流程，不增加分支、循环或新流程图编辑器。** 不再等待阶段位置/重复使用的选择。

G03按以下边界实施，均属于本次已批准方案，不另增主任务：

- 区分步骤身份和执行职责。同一配方的多个通用文本步骤分别拥有稳定身份，不能以相同operation或显示名互相覆盖；改显示名不改变身份。原有剧本/Bible等业务职责仍由各自writer处理，不因通用步骤插入而被替代。
- 每个通用步骤分别配置确切执行实例/输出结构、输入来源、候选及自动/人工选择方式；复用原路由、准备、调用、产物与选择事实，不新建执行队列或结果表。
- 配方中的顺序就是执行顺序；输入来源只能指向已声明且在前的步骤。缺失依赖、自引用、向后引用给出明确字段位置，不静默调整用户顺序；原业务步骤的必要依赖仍检查，不放开成任意业务步骤重排。
- RunPlan冻结具体步骤身份、职责和顺序；StageRun、准备幂等键、候选分组、选择及错误信息均定位到该步骤。两个通用文本步骤可使用同一个模型或相同字段名，但结果不能串用。
- 下游配置明确选择“哪个步骤的哪个输出字段”；字段目录从该步确切输出合同生成，按G04接续，不取同类型最后一步、最新成功候选或同名字段猜来源。
- G03验收增加同一配方两个通用文本步骤、不同合法插入位置、明确上游依赖、重复显示名不混淆、前向/自身引用拒绝和冻结后配置变化不改旧请求。选择仍沿R04，不新增固定人工审核步骤。完整保存/冻结/执行/页面接线完成前，不把目录登记或局部测试称为整项完成。

当前代码核对：`recipe_stage_declarations`区分稳定步骤key和职责kind；正式目录登记可重复structured_text职责及显示名，原业务单次职责仍保持相对顺序。RunPlan冻结、准备、候选和选择均定位唯一StageRun；自动/人工选择沿原服务，原配置/结果页面与动态上游目录均已接通。G06已用完整有效配方经过原API发布、首次创建和原顺序调度，连接两种布局的生成、选择与下游Profile准备；发布、根创建、准入没有patch，只有Provider传输及媒体下载是显式隔离夹具。完成步骤的通用值从正式output_snapshot.review_payload.selection读取，不保留旧扁平快照读法。该证据不代表现用迁移、真实模型或媒体交付通过。

输出目录必须从已配置步骤/路线的确切实例输出合同生成。同一步全部候选/fallback路线必须使用相同`output_version_id + output_contract_sha256`，不能只比较字段名或JSON类型后取第一条；保存/发布/RunPlan冻结和实际准备均核对，异构输出路线暂不混入同一来源。目录身份包含上游步骤和输出身份，不能仅按outputKey跨步骤匹配Schema。修改配置只影响新准备，原冻结事实不追写。

<a id="563-开发子项依赖与验收"></a>

#### 5.6.3 开发子项、依赖与验收

以下六项均归M1-08；G01先行，G02→G03→G04形成实际闭环，G05随各接线同步完成，G06作集中隔离验收。P0是闭环必要项，P1同属批准范围且完成后才交模块验收。

- [x] **M1-08-G01 能力与确切输出合同接入**（P0；依赖：M1-03/M1-04及M1-08已完成的通用输出配置、Profile路径段）。实现：能力目录/文本策略登记text.structured；原实例接口接受且要求structured_json.v1输出版本；保存、后继、只读预检、冻结定义/请求及输出适配器同时携带确切引用，普通业务协议互不混用。产出：模型/实例原配置入口、API/类型/根OpenAPI、针对性合同测试。验收：两种不同字段布局都可配置同一通用能力；缺输出、错协议、版本变化、模型未声明均给明确配置错误；实例/输出修改不改变已冻结合同；不调用真实模型。
  - 实现证据：[共用输出协议](../../../../backend/studio/services/execution_output_contracts.py)、[结构化请求及解析](../../../../backend/studio/services/execution_structured_text.py)、[隔离测试](../../../../backend/studio/tests/test_execution_structured_text.py)。原能力目录、text_execution策略、Profile操作目录、实例输出选择及资源烟测复用同一确切输出合同；目录返回typed output_parser_keys，输出额度沿原资源策略。请求冻结完整输出快照且核对定义；原任务中心识别新协议，资源烟测幂等比较输出版本，不借用影视规则或提示词字段任务。
  - 验证证据：修复系统max_tokens适配遗漏及测试查询范围后，74项后端回归通过（129.526s）；补冻结请求读取的32项通过（118.501s）；最终再修复输出版本幂等/任务追溯后的53项通过（87.783s），各轮有重叠、不累加为测试总数。35项前端定向测试（15.877s）、17项指南测试、类型检查、严格OpenAPI生成校验、Django check及无迁移漂移通过；Vite构建11.86s通过，仅既有大包提示。所有后端验证为隔离SQLite，不触发真实Provider。
  - 页面证据：真实配置组件配显式内存目录验证A对象/B列表两种输出选择、布局B v2确切回显；390×844文档scrollWidth=382，无横向溢出。夹具拦截所有业务API，不伪装后端保存或模型成功；临时页已随唤醒结束关闭，视口覆盖已清除。尚未验现用页面加载、真实模型、完整生产链。
  - G01交接时仅提供G02前置系统JSON Schema、严格共享解析及原任务中心追溯；后续worker验收独立记录如下，不以配置测试替代生产结果接线。
- [x] **M1-08-G02 原worker生产与解析结果**（P0；依赖：G01）。实现：原请求编译器附明确JSON结构要求，原文本worker调用共用解析器并保存通用Artifact和实际返回，候选读者识别确切角色/合同；原失败保留/人工修复解析也按同一合同。产出：请求、响应、候选及错误读取接线与隔离worker测试。验收：真实worker函数配隔离假Provider完成调用→原文→候选；对象/列表/标量/null按结构支持，缺键默认可溯源，错误类型/重复键/非法JSON仍可查看原文；重复/晚到/存储失败不产生伪成功或重复候选。完成：2026-09-28，证据及边界如下。
  - 实现：[唯一生产文本角色目录](../../../../backend/studio/services/execution_production_text_contracts.py)供原队列、准入、worker及候选读者复用，删除native模块中的重复生产操作表；通用产物使用kind=other/role=structured_text_candidate，不落ScriptRevision或另一结果表。[原worker](../../../../backend/studio/services/execution_production_text_worker.py)保留原领取、返回文件、事务和结算流程；[结果合同](../../../../backend/studio/services/execution_structured_text.py)保留实际JSON值、输出/结构身份、缺键默认来源及结构提醒。[候选读者](../../../../backend/studio/services/execution_production_candidates.py)核对原冻结请求/确切合同/值，不用当前版本重算结果；原产物API及任务中心复用同一读者。
  - 错误/修正：[原响应处理](../../../../backend/studio/services/execution_text_response_review.py)保留原始空白和全文；非法JSON/重复键/错类型记录structured_text_result_invalid与具体路径。人工修正使用同一冻结结构，corrected另存并记录真实修正者，不覆盖原文、不自动选择或生成Bible。重复/晚到不增候选；原文存储失败不记成功，候选数据库失败回滚，不确定提交不重复调用模型。
  - 验证：[隔离worker测试](../../../../backend/studio/tests/test_structured_text_worker.py)与原派发/辅助文本/合同共57项通过（285.941s）；新增数据库失败及任务中心API断言后，最终worker 8项通过（25.734s）。覆盖对象/空列表/文本/0/false/null、缺键默认/未知字段、原文/错误/修正、身份漂移、幂等/晚到/存储失败；读取及修正均不生成选择事实。两轮有重叠，不合计为65项完整回归。首轮测试错误来自夹具实例key重复、nullable参数名及导入TestCase重复发现，已修正；不把失败轮次计为通过。
  - 配套验证：最终47项指南/设置加载测试通过（4.835s），指南同源生成检查通过；vue-tsc及Vite构建通过（11.77s，保留既有SettingsView大包提示），严格OpenAPI无警告、Django check及makemigrations --check --dry-run无漂移。本段未改页面业务渲染，未另作G05浏览器验收；G01隔离配置页面证据见上一项。
  - 边界：夹具真实调用原请求编译/存储、领取、worker、Artifact写入/读取及原API；假Provider且提交准入显式隔离，stage_key仅存储夹具、未注册配方新阶段。因此证明G02，不证明G03配方发起/选择、G04下游读取或G05结果字段页面完成。无新增Schema迁移、现用库/服务/配置操作、真实模型/Smoke、业务审核采用或数据清理。
- [x] **M1-08-G03 配方阶段与自动/人工选择**（P0；依赖：G02及现有运行计划/选择事实）。实现：通用文本职责进入原阶段目录/路由、生成推进、候选选择；生产运行和原候选操作入口复用相同服务。自动沿R04，人工明确选择；不增加固定人审阶段，也不在读取时采用。产出：阶段/选择接线、必要Schema迁移文件及阶段/幂等测试。验收：自动模式能在部分候选失败时按序选有效成功项，人工模式停留待选择；模式与操作者事实真实；晚到不静默改选；旧剧本/影视业务不被新通用分支接管。若现有阶段结构不足，先在本附录记录同范围最小调整；需新增DAG等业务范围时再征求用户确认。
  - 第一段实现（2026-09-28）：[原配方合同](../../../../backend/studio/services/production_recipe_contracts.py)区分步骤身份/职责/显示名。同名同职责的两步不覆盖，单次业务职责仍按原相对顺序；可重复职责须由目录显式登记。本段只用隔离目录验证插入和重复，正式目录/接口尚未开放，不引入分支、循环或另一配方引擎。
  - 同步接入同一步多路线的确切输出一致性：保存/发布复用已验证实例定义，比对operation及BusinessOutputVersion的ID/摘要，不以同一结构或同名字段替代身份。[原项目文本准备](../../../../backend/studio/services/production_recipe_preparations.py)复用同一比较器核对冻结候选路线；不因备用实例当前退役而新增就绪限制，不选择路线或改写历史请求。实际选中路线继续原准入；没有业务输出合同的媒体路线保持原行为。
  - 验证：[新增9项合同测试](../../../../backend/studio/tests/test_recipe_step_contracts.py)覆盖重复/保留身份、合法位置、同名不同配置、未登记职责拒绝、精确输出与同结构不同身份、冻结摘要及备用实例退役；无准备/调用新增。先行18项通过（14.464s），最终与原配方合同/准备/选路/阶段目录/RunPlan共59项通过（138.355s），两轮重叠不累加。均使用隔离SQLite，Django check通过、makemigrations --check --dry-run --noinput无漂移；正式API/架构说明同步。
  - 第一段交接边界：当时尚未登记可重复职责、接通目录快照/原分派、候选自动/人工选择和配方页面，因此G03不勾选；G04动态上游输出目录随后接续。该段无Schema/HTTP形状/页面变更，未重跑前端构建或浏览器；未迁现用库、操作服务/业务配置、真实模型/Smoke、审核采用或清理数据。
  - 第二段实现（2026-09-28）：[原RunPlan读者](../../../../backend/studio/services/production_run_plans.py)核对明确的repeatable职责属性，不将字符串/数字当布尔、不按当前目录重建；[原项目文本阶段](../../../../backend/studio/services/production_project_text_stages.py)及准备入口从冻结声明读取职责，路由校验不再把自定义步骤key当operation。候选清单仍按本次StageRun和指定准备定位，两个同名同职责步骤的请求、幂等和候选不会合并；已有来源资格继续按实际声明顺序判断，不改成目录固定ordinal。
  - 第二段验证：[新增5项隔离串联测试](../../../../backend/studio/tests/test_recipe_repeatable_preparations.py)覆盖同名重复步骤独立准备、原请求幂等重放、当前配方/目录变化不改冻结内容、真实声明顺序、跨步骤准备/候选拒绝、错误职责与repeatable类型校验。两步各编译真实请求，隔离队列夹具接原worker假Provider产生不同整数结果，再核对只读候选清单、零采用写入。最终与原配方合同/准备、项目文本阶段、RunPlan及通用worker共64项通过（177.236s）；Django check及迁移漂移检查通过。初轮5项中worker用例失败，原因是队列夹具漏填Profile关联；单项诊断复测仍失败，补齐真实实例绑定后组合复验通过，没有弱化worker校验。
  - 第二段边界：使用显式隔离目录与职责映射，Run/Stage及发布配方是存储夹具；提交准入及Provider也是隔离边界，不证明正式配方发布、完整顺序调度、自动/人工选择或G04下游取值已完成。G03保持未勾选；下一段接选择事实/阶段推进及正式目录，G05同步页面。没有新增表列、公开HTTP形状或页面改动，未重复前端构建/浏览器；未迁现用库、操作服务/业务配置、真实模型/Smoke、审核采用或清理数据，无新增用户决策阻塞。
  - 第三段实现（2026-09-28）：[通用文本选择服务](../../../../backend/studio/services/production_structured_text_selection.py)核对同一步骤/当前候选路线与原worker产物，按冻结candidate_number而非完成时间或attempt_number选择。原阶段处理器允许部分失败；自动模式直接复用正式选择，人工模式显式等待。人工选择沿原select-artifact HTTP和共用Artifact状态/决定writer，不创建ScriptRevision/Bible或另一结果表；原业务文本审核保持原入口。自动再检查不替换既有人工/自动选择，重复确认同一项不增修订；已完成本步的输出不改写，未完成本步可明确改选。不增加另一审核步骤或质量排名，也不在读取时采用。
  - 第三段Schema/API：[0210迁移文件](../../../../backend/studio/migrations/0210_artifact_selection_mode.py)给原ArtifactSelectionDecision增加selection_mode=manual/automatic，selected_by允许自动时为空并以数据库约束保证来源真实；原人工写者继续manual。原阶段详情、根OpenAPI及selected_output来源摘要同步模式/真实空值，不把运行所有者当自动选择的人审。实际现用库未执行迁移。
  - 第三段验证：[新增选择测试](../../../../backend/studio/tests/test_structured_text_selection.py)覆盖部分失败、候选编号/完成次序不同、两步同名独立选择、明确人工选择及原HTTP、重复选择、晚到更早候选、只读零写入、跨步/无权/取消/内容损坏拒绝及模式数据库约束；SQLite选择查询EXPLAIN命中索引。先与原准备、已选文本/媒体来源和原项目文本阶段共36项通过（173.241s）；最终12项选择测试及1项OpenAPI合计13项通过（60.171s），有重叠不累加。最终还覆盖已拒绝候选不阻挡其他成功项、明确选定项不要求其他候选完整、尚有在途时继续等候、全终态且无可用项显示实际问题而非误报人工模式。此前补测12项及中间边界1项亦通过，不计为额外总数。初轮8项有6个夹具错误：同一步的测试调用序号固定1，违反原唯一约束；修正夹具按调用序号分配，不改生产约束。Django check、无迁移漂移、严格OpenAPI生成已通过。
  - 第三段边界：仍使用明确隔离的重复目录/RunPlan存储/提交准入夹具和假Provider；验证原阶段处理器与选择API，不等于正式目录、完整顺序调度、页面或G04来源闭环通过。未改前端，未重跑前端构建/浏览器；G03保持未勾选，下一段是正式目录、顺序调度和G05配置/结果页面。无新增需求决策阻塞，未操作现用库/服务/业务配置、真实模型/Smoke、业务审核采用或数据清理。
  - 第四段实现（2026-09-28）：[正式阶段目录](../../../../backend/studio/services/production_stage_catalog.py)版本4明确登记structured_text/ai与repeatable，删除前两段测试中的临时目录/职责patch；原单次职责顺序不变。配方options同源提供label、repeatable及候选选择方式，不按项目/人名/实例猜测。通用文本以候选选择完成本步，不再另配阶段审核；无人值守要求自动候选选择。原就绪检查与非模型快照修正为读取职责而非自定义步骤key，无配方人工制作只生成目录明确有人工动作的职责，不附加空通用步骤。
  - 第四段接线：[原配方编辑器](../../../../frontend/src/components/ProductionRecipeEditor.vue)复用[作者合同](../../../../frontend/src/services/productionRecipeAuthoring.ts)，明确选择重复职责时生成独立步骤key，改名/上下移动保留身份；同名可区分，原业务单次职责相对顺序不可倒置。改职责或移除已有步骤会提示本地配置影响，不自动改写下游引用；候选选择与辅助输入字段确认分开。冻结概览也展示显示名及身份。[原API/调度集成测试](../../../../backend/studio/tests/test_structured_text_recipe_integration.py)覆盖不同插入位置的草稿保存/修改、已发布存储夹具后继复制、原子初始化与幂等重放、按序创建两步准备、人工等待后继续、自动选择及成功不重跑；原完整配方准入仍拒绝缺交付闭包的局部夹具。
  - 第四段页面证据：[组件测试](../../../../frontend/tests/repeatableRecipeSteps.test.ts)与[隔离页面](../../../../frontend/tests/fixtures/repeatable-recipe-steps.html)使用真实组件及明确内存目录，拦截所有业务API。桌面验证改名保留key、键盘上移改变顺序、人工/自动选择、添加重复步骤与只读禁改；390×844下文档宽与scrollWidth均382，无横向溢出。鼠标上移工具未观察到变更，改用键盘Enter验证成功，不声称鼠标测试已完成；最终控制台无warn/error，临时页关闭、视口恢复。页面未保存现用配方、创建运行或调用模型。
  - 第四段验证：正式配方/阶段目录/原手动创建及动作/重复准备/候选选择共63项通过（186.069s）。前轮35项含2个测试错误（草稿不能克隆后继、GET应读manual-action-options）；61项中随机配方key以数字开头被原规则正确拒绝；随后63项中局部配方被公开创建的完整交付闭包正确拒绝。修正测试边界及数据后，单项原初始化复验通过，再最终63项全通过，没有放宽实际准入。前端作者合同/真实组件共20项通过（13.475s），指南17项通过（0.235s）、同源指南生成检查通过，vue-tsc及Vite构建通过（18.17s，仅既有SettingsView大包提示）。Django check、makemigrations --check --dry-run --noinput无漂移，严格OpenAPI校验/生成通过；初次相对路径写OpenAPI发生本机OSError，改为仓库绝对路径重新生成成功。各重叠轮次不累加为总测试数。
  - 第四段相邻验证：原execution_catalog_readiness与production_recipe_stage_snapshots共15项通过（56.915s），核对职责识别改动不损坏原实例就绪/非模型冻结；相邻前端注册配置测试1项通过（0.915s），其目录夹具同步新元数据。不将它描述为全平台前后端回归。
  - 第四段边界：原初始化测试调用实际create_recipe_run_root并验证公开创建重放；它是局部冻结测试，不替代完整配方发布/首次创建准入。顺序调度测试复用原advance_production_run，提交准入与假Provider仍隔离，存储Run夹具的根校验显式patch；不是媒体交付结果。G03仍不勾选；继续G05原候选/结果字段页面与G04精确下游来源，G06再做组合验收。无新增迁移，0210仍未执行于现用库；未操作现用服务、业务配置、真实模型/Smoke、审核采用或数据清理，无新增用户决策阻塞。
  - 第五段（G05结果展示接线）：[原运行页面](../../../../frontend/src/views/ProductionRunView.vue)的阶段展开区复用[通用文本结果组件](../../../../frontend/src/components/ProductionStructuredTextResults.vue)，只调用原Artifact分页/详情/选择接口；读取冻结RunPlan的步骤身份、名称和ordinal，重名不合并。原候选读者按相同冻结结构编译只读json_schema，共用StructuredJsonEditor呈现对象/列表/标量/null，明确展示0/false、默认来源及未映射提醒。列表/详情读失败、无候选、异步过期、重复提交和已结束只读都有明确状态；失败或未解析结果沿原任务中心链接查看原文，不另建候选后台、不自动审核/写入剧本或触发生成。原选择事实通过Stage适配器展示人工/自动及真实空操作者，不按配方模式推断事实。
  - 第五段验证：[后端原worker/选择](../../../../backend/studio/tests/test_structured_text_worker.py)20项通过（78.049s）；[前端结果组件](../../../../frontend/tests/productionStructuredText.test.ts)、重复配方、原文本审核、Run加载及结构表单最终75项通过（14.580s，补人工选择等待说明测试；与前轮74项重叠、不累加），指南17项通过（0.191s）。首轮前端7/8通过，补全缺recipe_version的测试夹具后修复；首轮类型检查发现Object.hasOwn超出当前target，改用已有兼容写法后通过。最终vue-tsc、Vite构建（13.21s，仍有既有SettingsView大包提示）、Django check、makemigrations --check --dry-run --noinput及严格OpenAPI生成均通过。部分大文件写入及前两次OpenAPI输出发生本机I/O错误，核对文件无破坏后重试成功；不把失败轮次计为通过。
  - 第五段页面证据：隔离[真实组件夹具](../../../../frontend/tests/fixtures/structured-text-results.html)以请求拦截器禁止真实API，完成[桌面](../../../../frontend/tests/fixtures/structured-text-results-desktop.png)/[390px窄屏](../../../../frontend/tests/fixtures/structured-text-results-narrow.png)显示、键盘查看/选择、人工/自动标识、读取失败、无候选检查。初始夹具括号及nullable格式错误已修正为正式编译器的anyOf格式；页面验证另发现父刷新清除成功提示，改用稳定上下文签名并补回归后复验通过。窄屏clientWidth/scrollWidth均382，无横溢；刷新后warn/error日志为空，视口已恢复、临时标签已关闭。鼠标工具点击未观察到变化，实际操作验证采用键盘，不冒称鼠标已通过。
  - 第五段边界：G05仍有G04来源目录/下游字段选择接线未完成，G03/G05和M1-08不整体勾选。原文链接目标和后端原文读取已有隔离证据，本轮未在现用运行或完整任务中心做真实业务操作。没有新迁移，0210仍未执行于现用库；未改运行服务、业务配置、模型/Smoke、审核采用或清理数据，无新增决策阻塞。下一段接G04确切上游输出目录/值读取及G05对应来源表单，再做G06组合验收。
- [x] **M1-08-G04 确切上游目录与下游读取**（P0；依赖：G03、M1-05及M1-08已完成的深层路径段）。实现：从配置步骤的输出版本生成目录并冻结，原selected_output按决定/产物读取通用值，配方对象/列表/路径映射和Profile根输入继续复用既有解析器。产出：动态来源目录、准备解析及关联测试。验收：布局A/B各自生产、选择后，按不同路径得到相同下游字段；自定义非剧本字段无需代码分支；false/0/null不消失；未选择/错误运行/合同不符/多路线歧义明确报错；下游读取不新建选择或回填历史。
  - 2026-09-29后端接线段：[同源输出目录](../../../../backend/studio/services/execution_text_output_catalog.py)逐步核对全部确切路线，复用原路线身份验证和输出一致性检查。同名步骤/同一输出结构以stage_key＋output_key区分，原RunPlan结构摘要保存规范复合键；只派生结构、不复制业务结果或推测最新实例，重复使用同一实例只读一次合同。配方保存、项目/媒体准备及原结构映射器传递同一目录；原selected_output读者核对实际选择、完成步骤快照、原请求和输出身份后读取candidate.value，通用文本不加剧本审核。Profile结构化根端口允许既有scope目录的明确接线，真实来源仍走原权限/范围核对，其他端口合同不变。
  - 隔离验证：[通用下游测试](../../../../backend/studio/tests/test_structured_text_downstream.py)覆盖同名步骤、无写目录读取、根值0/false/null、对象嵌套列表与列表根两种布局映射成相同字段、未选择/跨Run/错误步骤或输出、未满足的上游依赖拒绝、同形不同输出版本的多路线拒绝、非法草稿显式报错、当前配方变化不替换旧计划；原草稿API保存规则→原worker假Provider→原选择→下游Preparation→Profile实际编译→重放不重读已通过。初始相关12项通过（44.899s）；第一轮25项中5项因测试覆盖冻结RunPlan而被拒绝，已改为新建隔离Run，不放松事实不可改写约束。随后16项通过（53.278s）；最终关联66项通过（72.500s），追加原API断言的单测复验通过（6.227s，轮次重叠不累加）。Django check与makemigrations --check --dry-run --noinput通过，无新增迁移。
  - 边界与接续：原公开GET配方选项目录仍是固定业务投影，下一段由G04/G05接动态目录API及来源字段表单，之后G06组合验收；所以G04暂不整项勾选。此段未改前端或公开HTTP形状，未机械重跑前端构建/浏览器、OpenAPI生成；不代表完整配方发布、真实模型或业务交付通过。正式API/架构与五处检查点已同步；主清单7/91、子项2/6不变，无新增需求决策阻塞。未迁现用库、操作服务、现用配置、模型/Smoke、审核采用或清理业务数据。
- [x] **M1-08-G05 配置与结果页面完整接线**（P1；依赖：G01–G04，随实现同步）。实现：模型能力、实例输出、配方来源目录、主准备/候选和任务中心复用现有页面；共用结构组件展示字段、原文、默认/未映射提醒和错误，UUID/技术合同折叠。产出：原入口可操作、类型与API帮助一致，无另一候选后台。验收：页面能配置两种布局、查看实际返回和解析值、区分自动/人工选择，并为下游选字段；读失败/空值/无候选/部分失败可见；隔离桌面/窄屏验证不操作现用业务配置。
  - 2026-09-29来源表单段：原配方增加[只读动态目录](../../../../backend/studio/services/execution_text_output_catalog.py)，POST source-options复用原确切输出读者、不写业务事实，也不要求未配置完的输入先通过保存校验；无路由不猜输出。原[配方组件](../../../../frontend/src/components/ProductionRecipeEditor.vue)、路线及结构映射组件按stage_key＋output_key选择并显示序号，先前步骤和适用scope之外的来源不可选；加载/失败/重试与失效已存值可见，过期回执不覆盖新草稿，不静默清空已有映射。
  - 来源表单验证：后端来源/作者27项通过（61.421s），[前端动态来源](../../../../frontend/tests/recipeDynamicSources.test.ts)与结构映射/重复步骤/作者合同40项通过（22.325s），指南17项通过。vue-tsc与Vite构建通过（12.95s，仅既有SettingsView分块提示）；Django check、无迁移漂移及严格OpenAPI生成通过。早轮39项中旧测试未声明前序/兼容scope及新夹具缺少目录项，修正测试后通过，未放宽实际来源约束。
  - 隔离浏览器：[夹具](../../../../frontend/tests/fixtures/recipe-dynamic-sources.html)拦截真实API，选第2个同名步骤后路径切为result/amount；原值保存于本地草稿。目录失败显式显示、重试恢复、只读禁用，390×844无横向溢出（382/382），控制台无warn/error。证据：[桌面](../../../../frontend/tests/fixtures/recipe-dynamic-sources-desktop.png)、[窄屏](../../../../frontend/tests/fixtures/recipe-dynamic-sources-narrow.png)；临时页关闭、视口恢复。没有现用配置保存/模型调用，不据此勾选整个G05或M1模块；接续G06组合验收。
- [x] **M1-08-G06 组合隔离验收与文档收敛**（P1；依赖：G01–G05）。实现：原配置API→冻结准备→原worker假Provider→结果/选择→配方下游输入的组合测试；同步正式API/架构/指南和唯一检查点，残留搜索防止通用结果仍被判为影视规则/剧本。验收：A/B布局、自定义字段、人工/自动、部分失败、无效返回、过期引用、版本变化均通过；类型/匹配构建及Schema漂移检查通过。保留假Provider标识，不把隔离测试当现用迁移、部署或真实效果；现用操作仍按R06另核对。
  - 完成：2026-09-29。[组合测试](../../../../backend/studio/tests/test_structured_text_combination.py)用两种配置布局（对象内列表、列表根）、任意字段名及稳定步骤key，经正式输出/Profile/实例发布、配方发布/首次创建、原队列worker、自动/人工选择、确切目录映射进入第三步Profile。第一步三候选包含Provider失败、非法JSON和成功项；失败原文保留且不妨碍选有效结果。第二步等待原select-artifact明确选择，第三步接收相同的0/false/null并编译实际请求。后继草稿改名/换序不改旧RunPlan，请求重放不增调用；第三步后推进原script_generate，不宣称后续业务生产完成。
  - 根因修复：[已选文本读者](../../../../backend/studio/services/execution_selected_text.py)原先读取处理器临时扁平selection，而原调度器完成阶段会封装到review_payload.selection，导致真实完成链下游取值失败。现只读并核对正式封装及准确选择/产物，不双读或回填历史；[关联夹具](../../../../backend/studio/tests/test_structured_text_downstream.py)也改为经过原advance_production_run完成步骤，避免手工标succeeded掩盖此层错误。复用原交付夹具仅增加可传前置步骤参数，不改正式发布/准入规则。
  - 后端验证：71项关联回归（225.818s）中70项通过、组合用例失败于夹具漏配label_style=none；补全显式配置后，最终组合/下游13项全部通过（76.411s）。其他70项不受该测试夹具调整影响；不把前轮报为71项全通过，也不累计重叠轮次。早期组合夹具参数及FFmpeg进程环境遗漏已修正；一次测试会话结果丢失未计为通过。Django check及makemigrations --check --dry-run --noinput通过，无新增迁移。
  - 页面/合同验证：前端动态来源/映射/重复步骤/作者40项、结果/输出16项、指南17项通过；vue-tsc、同源指南更新后Vite最终11.38s（既有大包提醒）及严格OpenAPI通过。同源指南、正式API/架构、唯一清单及五处检查点同步。G01/G05已有隔离真实组件桌面/390px证据继续适用；本段仅修下游服务和测试，未变页面布局，不重复浏览器验收。
  - 验收边界：原发布/根创建/调度/准入/请求编译/选择/读取均实际运行于隔离SQLite和临时媒体，Provider与下载明确模拟，配置验收用的Smoke也是假Provider隔离流程，不是现用或真实Smoke。未操作现用库、服务、ComfyUI、业务配置、审核采用或删除数据；M1-09/10及所有其他业务模块仍按原依赖接续。G03–G06及M1-08据此勾选，先前未勾选段落保留为日期化过程证据。

<a id="564-当前状态与后续接续"></a>

#### 5.6.4 当前状态与后续接续

M1-08及G01–G06已完成（2026-09-29）：配置合同、原worker/原文保留、原配方可重复顺序步骤、自动/人工选择、确切动态来源/下游读取和原页面均接通，G06隔离组合验收通过。主清单8/91、子项6/6；未增加分支/循环、新流程图或执行队列。下一项M1-09字段中心与旧字典收敛，随后M1-10模块交付与用户验收，不能把M1-08勾选称为M1或全链完成。M9-01仍仅Schema只读盘点，暂无新增需求决策阻塞。

已接点：实例和资源配置使用同源输出目录；通用候选按确切结构核对对象/列表/标量/null；配方按冻结身份/顺序执行，原选择服务保留自动/人工来源；原运行阶段区显示字段、原文、默认与错误。动态目录以步骤＋输出身份提供Schema，原表单提供整值/深层路径选择；原下游准备读取正式完成快照和已选产物后编译Profile。G06在隔离库验证三步通用链并推进到原剧本阶段，未执行后续媒体交付。字段管理仍待从Profile搬入独立维护中心；实际服务/数据库切换与真实效果验收另按R06核对。
