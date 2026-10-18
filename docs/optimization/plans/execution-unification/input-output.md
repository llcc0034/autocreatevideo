# 4. 输入素材、槽位与参数合同

> 历史方案／登记依据，不是当前施工清单；取消或延期事项不会因本次整理恢复。

[总索引](../../README.md) · [当前续接](../../current.md) · [按日期查证据](../../records/README.md)

<!-- source-section 7cfd5c7c6cbe -->
<a id="4-输入素材槽位与参数合同"></a>

## 4. 输入素材、槽位与参数合同

<a id="41-三层职责"></a>

### 4.1 三层职责

| 层 | 配置或事实 | 唯一负责人 |
| --- | --- | --- |
| 输入定义 | 槽位 key、类型、数量范围、是否必填、可用用途、时长／尺寸约束 | Workflow／API 能力合同；执行实例只能映射、命名及收紧 |
| 本次绑定 | 哪份素材、哪个实体／镜头、用途、顺序、父子／配对关系 | 上游映射或人工选择经共用解析服务规范化 |
| 执行表达 | 节点端口／API 字段及提示词中的 Picture／Video／Audio 标签 | 适配器和提示词编译器共同消费同一份规范化绑定结果 |

提示词策略定义“如何描述已确定的输入”，不独占数量合同、实际素材 ID 或节点端口。分类元数据也不能自动证明素材中确实出现某个人物；模型辅助建议需人工确认，首期不做视觉识别猜测。

<a id="42-输入定义与调用方来源映射"></a>

### 4.2 输入定义与调用方来源映射

- 实例输入项定义稳定 key、类型、用途和底层目标槽位／策略输入 key。它不保存来源阶段、Shot ID、Asset ID 或某个项目的来源路径。
- 来源映射仅属于 Recipe 阶段或人工请求，指向实例输入 key；允许的来源目录与求值规则见第 16 节，不接受任意路径表达式。
- 生产侧来源可选择“当前镜头已采用的人物图”“指定上游阶段的选中产物”等；必须冻结选中记录，不使用未审核候选或运行时不断变化的 latest。
- 素材库来源可选择已有 Asset、正式上传后返回的 Asset、人工正文／说明；不为复用生产编译器而创建假的项目实体。
- 多个候选无法唯一解析时返回待选择；必需输入缺失时阻断；可选输入缺失时按合同明确省略，不替换为示例图片或随机素材。
- 具体请求保留素材稳定引用／checksum、语义角色、用途、镜头范围、顺序和配对引用。沿已有 Asset／产物事实记录，不发明独立媒体版本事实。
- 本地上传先完成受管 Asset 创建、权限与格式校验，再参与绑定；不把临时路径作为调用参数。

<a id="43-动态槽位分配"></a>

### 4.3 动态槽位分配

1. 按来源规则选出确定的素材集合，校验权限、类型、数量、重复策略和作用范围。
2. 由明确顺序分配逻辑槽位；一个素材多用途是否允许由合同约束，不能全局按 Asset ID 静默去重。
3. 建立输入标签、实体标签和配套音频关系。标签须唯一且受控；物理端口零起算与展示标签一起算由适配器转换，调用方不自行加减编号。
4. 生成一份有摘要的绑定清单，同时提供给提示词编译和工作流／API 请求准备。
5. 任务冻结后禁止重新排序。替换素材、改变用途或顺序构成新请求，重试使用原快照并重新核查必要的权限与安全状态。

建议页面示例：`人物参考图 → 人物甲 → 已选择素材 → 用于镜头 1 → <Picture 1>`。该示例仅说明交互，不是模型可识别身份的保证。

<a id="44-参数与输出合同"></a>

### 4.4 参数与输出合同

- 有效范围是模型、Workflow／API、执行实例限制及平台安全边界的交集；任何冲突应指明来源，不采用最后写入覆盖规则。
- 先处理限制和锁定，再处理默认值：范围只能收紧；底层、实例或阶段锁定值／锁定来源不可被本次输入覆盖。未锁定参数才按“本次显式值 → 阶段默认值 → 实例默认值 → 底层明示默认值”选择；每层带来源，必需值缺失报错。规则见第 16 节。
- 帧数、种子、CFG、LoRA 强度等只在底层明确开放时显示；范围、步长、余数对齐、单位与枚举复用现有校验。LoRA 文件与 LoRA 强度是不同职责，不因开放强度而允许换文件。
- 配方阶段不必把每个运行值都写死，但必须固定取值来源；来自上游的值在任务提交前求值并冻结。随机种子只允许明示生成规则，实际 seed 必须在首次提交前固定，重试不能换 seed。
- 输出必须明确媒体类型、业务用途、数量、文本／结构化 schema 和解析器。不能因为两个能力都返回文字就视为可以互换。
- “提示词候选”与“剧本生成”输出语义分别核对；若现有 operation 无法表达候选合同，在现有受控能力目录新增明确操作及策略，不借旧名称伪装兼容。

<!-- source-section a890a20621fa -->
<a id="16-输入参数辅助操作与输出"></a>

## 16. 输入、参数、辅助操作与输出

<a id="161-唯一来源映射"></a>

### 16.1 唯一来源映射

实例只声明逻辑输入及目标。Recipe阶段保存来源规则；无配方人工步骤/素材任务直接提交本次来源；三者都只通过同一规范化器。

| kind | 必填与可选字段 | 规则 |
| --- | --- | --- |
| literal | value | text/json按受控schema校验，不执行表达式 |
| asset | item_key、asset_id、purpose；subject_ref/scope_ref/paired_to可选 | 正式Asset、checksum、媒体探测及访问权；不可用路径代替ID |
| selected_output | stage_key、output_key、scope、selection_policy=reviewed_selected | Recipe仅用于受控目录的文本/JSON输出；人工媒体仍从有效已采用输出目录提交精确Decision/Artifact ID，不使用配方规则替代实际身份 |
| reviewed_references | stage_key=storyboard_review、scope={kind:shot}、groups | 配方媒体来源；groups按顺序声明source_key及purposes/subject_kinds筛选，null明确表示不筛选，不替换原用途或主体 |
| context_field | selector_key、scope、content_revision_id（求值后必有） | 受控目录；不接受任意JSONPath；人工端可显式选择真实上下文，不借此跨项目访问 |
| structured | template | 按精确输入schema组合对象、列表和逐单元上下文；不执行表达式。叶子只能使用明确literal、目录context_field/selected_output、本次unit_key、已规范化reference_subjects或当前列表的已登记item_field |

媒体以items有序数组提交；item_key在请求内唯一，paired_to引用它；若提供order必须等于数组位置。subject_ref为character/scene/prop/external+稳定UUID，external取本次subject_facts，不能伪造Bible。scope包含request/当前真实镜头/剧集/项目；无项目素材请求只允许request或获授权的明确真实引用。

context_field的修订身份由目录revision_kind解释：script_revision使用原ScriptRevision ID，storyboard_approval/bible_approval使用原ApprovalDecision ID，不增加ContentRevision副本。Recipe镜头来源使用本次Shot范围；实体来源使用request范围及原生产准备明确的实体目标，不扩张媒体语义scope。目录只提供已实现的标量和创作JSON；普通JSON不得冒充参考原语的有序单元或生成上下文。

数量校验以来源展开后的实际数量进行。selected_output多选但目标单值时阻断；一个选择产生多个产物时item_key由DecisionID+ArtifactID+用途生成，不能只用DecisionID导致碰撞。相同Asset多用途由合同决定，禁止全局静默去重。

2026-09-12用户确认结构映射扩展。template为封闭节点：object.fields（字段名到节点）、array.items（有序节点）、unit_map.value（仅以根准备授权的当前单元key为键）、each.source/item（按目录数组顺序映射当前条目）、literal.value、context_field、selected_output、unit_key、reference_subjects、item_field.field。目录context_field由原阶段绑定精确修订，selected_output沿原已采用文本目录绑定产物/选择决定；规范化器复核权限、审核、漂移并保存逐叶证据，不先变成literal。项目当前风格的取值时点另按目录说明，不冒充审核。reference_subjects仅从同次已验证的主图片输入取主体，不能从整个项目或其他镜头猜测。each只能读取目录声明的数组和字段，不开放任意路径。缺项不补空值；明确的空literal表示管理员有意留空。结构输出仍须通过精确目标schema及既有参考原语、时长/主体/对白校验。新规则不解除完整生产门禁。

2026-09-12用户已确认逐镜来源映射方案。来源目录由后端发布：人物/场景/道具参考图、首帧、尾帧、连续性参考图、参考视频、视频配套音频、独立音频。配置处只选择有序来源组和用途/主体类别筛选；真实素材、实体与音轨关系来自原Shot参考审核。来源组按配置顺序合并，组内保持审核顺序，配套音频按具体视频引用身份连接到实例声明的父输入，不按下标猜测。原图片引用未声明用途时保留null；要求命名用途的实例必须拒绝，不能取allowed_purposes第一项。视频原音以明确video_audio来源及paired_to保留父视频身份，预览只核对受管视频字节与真实音轨，不执行提取或伪造AudioAsset。目标端口能原生读取时复用父视频上传；需要独立音频文件时沿原受管配套音频适配器生成派生文件与原上传回执，详见25.145。

运行工作台选择真实镜头后可预览本轮冻结配方的映射；只校验映射/素材完整性，不产生准备、种子、费用批准或任务。实际准备复用同一展开函数，再走唯一输入规范化器和原ExecutionPreparation。来源审核身份随原准备详情一起固定；重放不重新取数。审核缺失/内容漂移、数量超限、无效用途、缺主体或孤立配对均明确失败；未映射清单可查看，不静默丢弃到“成功生成”结论。配方草稿允许明确的未配置来源状态，完整验收和准备不接受该占位状态。

规范化记录含input_key、item_id、position、Asset/Artifact及checksum、media_facts、实体/范围/用途、配对、logical_label、target_port_ref。提示词与执行器消费相同顺序；标签及物理端口只由受控适配器派生。图端口的零起算与展示标签的一起算不得由用户自行换算。

未来输出在阶段就绪、创建该单元准备时解析，不在创建Run时猜测。准备建立后不追随“最新选片”；重新选片后显式创建新准备。缺失正式素材、必需checksum或必需解码信息是硬错误；缺少用于性能估算的tokenizer/容量测量只是第17节的观测未知。

<a id="162-参数"></a>

### 16.2 参数

1. 硬类型/范围/枚举/步长取模型、Workflow/API、实例及阶段明示限制的交集；多组整数余数约束必须同时满足，不只取最大step。
2. 底层固定、实例locked和步骤locked_source/locked_value相冲突即报错，不按覆盖顺序掩盖。
3. 无锁定时按本次值、步骤default、实例default、底层明示default取值；必需缺失报错，可选省略。不注入隐藏模型名默认值。
4. 有锁定时同值可接受、不同值拒绝；boolean不能当integer，禁止NaN/Infinity。
5. seed_policy=fixed/generate_once是明确的受控参数元数据。首次持久准备产生实际seed并纳入intent，幂等重放/辅助等待/重试不变；“再生成一个候选”是新准备，可生成新seed。

普通参数的seed_policy显式为null；fixed沿明确参数值/默认/锁定取值。generate_once必须有有限整数范围或枚举，按所有格点约束共同解抽取；不能与底层固定值、实例默认值、实例locked或本次手动值同时使用。查询/合同编译不抽取随机值，首次准备初始化结果在同一事务保存，重放读取并复验原值。

步骤parameter_policies为有界{key, policy:default/locked_value/locked_source, value?, source?}集合；不同policy的字段互斥。风险偏好不能修改模型必需的对齐公式、端口类型、固定模型文件或输出解析规则。

<a id="163-提示词与辅助操作"></a>

### 16.3 提示词与辅助操作

- profile：精确策略组装，含服务器事实、人工字段、生成/翻译字段；人工覆盖仅限策略明确开放的字段。
- manual：提交受控通道中的完整提示词，不额外调用AI、不自动改写；校验长度、实际引用标签及输出合同。
- none：只有能力合同明确不需要提示词才可用；拒绝多余提示词参数。

新增受控操作prompt.fields.generate、prompt.fields.translate、prompt.draft。模型没有这些能力声明时创建后继能力合同，不能借script.generate伪装兼容。不同用途叶子可共享部署，但必须分别绑定对应操作。

字段操作输入items含request_id、field_contract、source_text、context_facts、reference_facts；输出严格{items:[{request_id,text}]}，ID集合必须一致。field_contract由父策略冻结规则派生，不接受普通调用方任意注入。叶子Smoke可选择已发布主策略的精确字段规则作为fixture来源，不需要先有父实例Smoke，不形成循环。

prompt.draft返回{text:string}。script.generate、script.structure、visual_rules.generate保留原业务输出schema；目标提示词字段统一使用prompt.fields.generate/translate批次合同，不再发独立生产prompt.normalize请求。独立术语/身份翻译所复用的normalize校验与协议不在此处改成任意字符串，也不回填旧目标字段覆盖记录。API/ComfyUI均按受审adapter判断能否执行对应operation。

共用上下文为值对象：logical_inputs、subject_facts、ordered_units、reviewed_fields及数量/时间合同。ordered_units既可是真实镜头，也可为素材请求单元；不要求伪Django Shot/SeriesProject。
