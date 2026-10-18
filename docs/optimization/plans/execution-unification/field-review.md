# 18. 字段输入、采用、审核与费用

> 历史方案／登记依据，不是当前施工清单；取消或延期事项不会因本次整理恢复。

[总索引](../../README.md) · [当前续接](../../current.md) · [按日期查证据](../../records/README.md)

<!-- source-section e19492bff442 -->
<a id="18-字段输入采用审核与费用"></a>

## 18. 字段输入、采用、审核与费用

<a id="181-完整人工路径"></a>

### 18.1 完整人工路径

创建任务/人工步骤时可以提交field_values=[{unit_key,field_key,text}]，由服务器按主策略验证并创建pending修订。也允许缺少人工字段时创建Preparation(awaiting_input)及父preparing任务，再通过正式字段接口填写，不因缺少待填写字段形成“先有鸡还是先有蛋”的死循环。

输入素材、实例和固定参数必须足以建立intent；缺少这些结构事实仍400。待人工填写的字段正文是后续修订，不要求在intent中伪造。若未提供待生成字段，显示可调用的辅助用途和估算，只有原请求明确allow_external_calls且预算授权涵盖时才排队子调用。

模板服务器锁定的事实字段不能被人工覆盖。manual完整提示词与profile人工字段是不同输入联合，不能同时提交，不能对完整提示词再套一次策略。

从真实已冻结来源确定性派生的服务器字段可记origin_kind=server、approval_kind=server_derived并由服务端选择，不要求用户重复确认Asset ID等技术事实；这不是对模型生成文案的自动批准。人工/模型字段按第18.3节处理，不能把模型结果标server_derived绕审核。

<a id="182-确认与实际采用分离"></a>

### 18.2 确认与实际采用分离

字段pending可转confirmed/rejected；正文修改创建新revision，不修改旧正文。确认只表示该修订通过批准，ExecutionFieldSelection明确本次采用哪一修订：
- 初次确认可显式select_for_execution=true建立选择；不传则只确认。
- 已有选择时更换必须带expected_selection_revision及明确replace，不因产生较新confirmed自动替换。
- 每次执行的每个必需unit/field恰有一个选择，source_sha256、field_contract_sha256、实际实例/主策略上下文一致，不能串用镜头或候选。
- finalize事务锁准备及选择，一次冻结最终选择ID和SHA；冻结后新建/改选/确认影响本请求的动作409。需要修改就建立新业务准备。
- 明确重试/规格Smoke允许选择原准备的既有confirmed结果，但必须携带来源Preparation、字段ID并重新核查owner权限、来源/合同一致性；只引用原结果，不伪造新的生成调用或重复收费。

确认不直接调用模型。人工审核场景由“确认并继续”动作锁内完成确认选择并finalize；只保存确认可以继续编辑选择。自动审核场景在所有字段齐全时由同一推进服务finalize。

<a id="183-审核职责与来源"></a>

### 18.3 审核职责与来源

提取validate_execution_field纯校验器，负责类型、空值、保护标签、长度、来源和策略必需项；它不负责决定谁批准。

本期新增受控字段批准策略execution_fields.accept_valid_v1，冻结其版本/实现SHA，不冒充现有阶段decision_policy。该策略只可采用已通过纯校验且来源正确的结果，不声称AI文案语义质量已被证明。

| 业务上下文 | 批准方式 |
| --- | --- |
| 素材库/无配方人工Run/单独发起的Smoke | manual；用户可单项或批量“确认并继续”；无隐式自动批准 |
| Recipe assisted/manual | 复用其人工批准要求；字段确认不替代阶段批准 |
| Recipe unattended | schema3显式配置字段批准策略execution_fields.accept_valid_v1，并保留原阶段decision_policy及其门禁 |
| 已确认字段用于重试/规格Smoke | 复核已选修订与来源，复用原批准；不要求用户再次确认相同正文 |

审核合同来自这次请求所属的RunPlan或素材库/Smoke正式动作，不由实例决定；新字段自动策略的权限、适用范围和冻结身份必须明确。原_verified_unattended_decision_policy只用于真实unattended review stage，不传假Run/Stage绕过。

<a id="184-辅助任务与预算"></a>

### 18.4 辅助任务与预算

生产侧使用真实GenerationAttempt(stage_run必填)，素材库/Smoke用子AssetGenerationJob。生产辅助B先建立本身Preparation，由可信服务端构造并冻结主准备A的ID/intent SHA、精确批次和辅助叶子引用；按原Stage+稳定unit_key+request_key定位，不扫描JSON查关系。B完成准备后才派发原Attempt，实际任务同时引用preparation=B、parent_preparation=A。未派发前不伪造任务或费用；已派发后的父关系以正式FK为准并与冻结意图交叉校验。公共字段解析/结果落库服务只接受标准上下文，不复制整个生产worker，也不增加第三套远端队列。

字段结果逐批次落入其父准备；字段批次身份包含有序unit/field及source/contract SHA。子成功不自动证明主模型可执行；主请求若因预算/证据阻断，保留已确认字段，不重跑辅助。

主调用和每次辅助外呼分别预留/结算；父total只聚合，不能既把子actual_cost写入父又汇总。明确预算不足时保存blocked，不偷偷缩减内容或降模型。用户解除预算阻断只授权尚未调用部分，不重复占用已结算费用；uncertain继续占用可能已消费的预算，沿正式消歧结算。
