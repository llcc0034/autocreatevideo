# 长节点执行监控合同

`execution_monitoring` 用于声明媒体模型和工作流在“没有可量化进度”时允许静默多久。它是纯数据合同，不包含模型名称判断、脚本、正则选择器或可执行表达式。

精确实例→ProductionRun/Preparation→主GenerationAttempt→GenerationTask的领取、远端请求、归档及交付已接通，并有分项回归及后续服务切换记录。切换不代表所有GPU规格或长任务故障恢复已验收；当前剩余验收以[统一工作包](optimization/records/execution-unification/2026-09-25.md#current-work-packages)为准，下一次维护仍须核对实际服务与迁移状态。

## 在哪里配置

### 模型公共默认值

进入“系统设置 → 模型版本”，创建或编辑草稿媒体模型版本，在“执行监控合同 / Execution monitoring”填写：

```json
{
  "schema_version": 1,
  "default_idle_timeout_seconds": 1800,
  "node_type_overrides": {
    "SamplerCustomAdvanced": 5400
  }
}
```

模型版本只允许声明默认值和节点类型规则。它不知道具体工作流的节点 ID，因此不能包含 `node_overrides`。发布后不可原地修改；调整规则应创建新模型版本。

### 工作流精确规则

工作流版本验收页只读展示导入版本已经冻结的执行监控合同，不提供 JSON、节点 ID 或节点类型的人工编辑入口。普通调整应在下一模型能力版本的类型化字段中配置默认值或节点类型规则；只有注册 workflow-family compiler 确实需要区分同类型节点时，才由受审的服务端 projector 随 ComfyUI 保存修订确定性生成 `node_overrides`：

```json
{
  "schema_version": 1,
  "default_idle_timeout_seconds": 1800,
  "node_type_overrides": {
    "SamplerCustomAdvanced": 5400
  },
  "node_overrides": {
    "10": 5400
  }
}
```

时限为正整数秒，上限来自已生效的 `security.execution.max_idle_timeout_seconds`，不是本页写死的固定值；节点类型和节点规则的数量上限同样读取既有安全配置。`node_overrides` 的键必须存在于本版本 graph；不存在的节点会在导入/校验阶段被拒绝。`node_type_overrides` 使用 graph 中精确的 `class_type`，允许 `KSampler (Efficient)` 这类扩展节点名；只禁止空值、控制字符或超过 191 个字符的键。节点 ID 容易随图变化，能用稳定节点类型表达时优先使用模型版本的 `node_type_overrides`。平台不会把无法表达的节点专属需求降级为任意文本输入。

## 合并与执行规则

有效窗口的优先级为：

1. 工作流节点 ID；
2. 工作流节点类型；
3. 模型节点类型；
4. 工作流默认值；
5. 模型默认值；
6. 本次生产准备从运行配置中冻结的 `runtime.task_idle_timeout_seconds`。

节点执行的验收要求保持不变：进入新节点才设置该节点的idle deadline；重复心跳或同节点采样进度不形成滑动窗口，idle始终受不可续期hard deadline截断。生产Preparation冻结 `runtime.task_idle_timeout_seconds` 与 `production.task_timeout_seconds`，GenerationTask只从主Preparation初始化。遥测、终态和恢复观察核对领取次数、开始时间和原截止时间；过时worker不能续期或覆盖新领取。WebSocket、HTTP轮询、取消和健康失败已用当前原始任务验证，隔离测试不等于真实GPU超时验收。

如果 ComfyUI WebSocket 没有提供节点切换事件，worker 无法应用某个节点 ID/类型的专用窗口，只会保留提交阶段设置的默认窗口。因此，已知目标环境可能缺少节点遥测时，应把 `default_idle_timeout_seconds` 设置为能覆盖整次远端执行的保守值，并同时确认冻结的硬时限更长。

## 冻结与审计

创建/发布Comfy执行实例定义时，从精确模型版本和工作流版本合并 `interface.execution_monitoring`，由完整定义SHA覆盖；不为每个调用复制另一份可编辑规则。缺少此字段或不是规范化合同的定义明确拒绝，不读取旧任务补齐。模型/工作流规则变化应创建后继版本，不能原地改已发布合同。

创建生产Preparation时，从该Run已经冻结的配置读取idle/hard时限及配置SHA、修订标识，写入同一准备的 `resolved_inputs.execution_deadlines`。恢复/读取只能使用原值；不能重读当前项目默认、从旧route取值或用导入图示例补齐。节点规则留空是合法的“未覆盖”，与准备时限缺失的错误状态不同。原生文本不混入Comfy节点规则，异步媒体仍使用自己的原队列。

共同请求读取器核对精确实例、编译器/工作流合同、图、已解析语义、素材绑定、模型文件要求及主输出合同；素材库、组合Smoke和生产媒体共用此校验。源图尺寸派生核对保存的真实图片观测及尺寸候选，不另选素材或重新编译。原任务归档引用同一次Preparation和原始生成尝试，旧PromptArtifact/GeneratedWorkflow不再是运行或读取入口。

## node 10 长采样节点调整示例（整体切换后验收）

假设当前已发布工作流的长节点为 `10 / SamplerCustomAdvanced`，原全局 idle 为 1800 秒，而一次真实采样可能需要 45–70 分钟：

1. 创建下一模型能力版本，在类型化执行监控字段设置 `SamplerCustomAdvanced: 5400` 并发布；不要覆盖已发布版本。
2. 回到 ComfyUI 保存新的工作流修订，从 Settings 扫描并导入引用该模型版本的新 WorkflowTemplateVersion。若图中还有同类型短节点且确实需要 `node 10` 专属窗口，先由开发侧扩展受审 workflow-family projector；版本验收页不接受手填 `node_overrides`。
3. 确认 `production.task_timeout_seconds` 及主机容量解析出的有效硬时限大于 5400 秒；否则 idle 窗口仍会被硬时限截断。
4. 完成合同校验、真实 smoke、输出探测并发布新版本。
5. 在精确执行实例版本中选择新的模型运行资源、工作流和提示词配置，核对冻结定义并做组合验收；配方或人工制作选择该执行实例版本，不在业务调用端再选一套工作流。
6. P4/P5完整通过后，新建运行并在原任务详情核对节点进入时间、idle deadline与hard deadline。重复采样进度不应改变idle deadline；本页不授权或自动执行真实调用。

这里的节点和值只是对当前图的迁移示例，不是针对某个模型名称的代码分支。其他模型或工作流通过同一合同接入，无需修改主逻辑。

## 常见阻断

- `schema_version` 不是 `1`：改为当前支持的合同版本。
- 节点 ID 不存在：修复注册 projector 或 ComfyUI 源修订后重新导入；不能在页面复制旧版本 ID。
- 时限不是正整数或超过当前生效安全上限：在模型配置和安全边界中核对单位与允许范围。
- 合同或 SHA-256 漂移：不能手工改历史快照，应创建新版本和新 Run。
- 配置 5400 秒仍在更早时间终止：检查 hard deadline；它是不可续期上限。
- 节点专用规则未生效：核对 WebSocket 是否报告节点切换；没有节点遥测时使用足够的默认窗口。
