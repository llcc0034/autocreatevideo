# 长节点执行监控合同

`execution_monitoring` 用于声明媒体模型和工作流在“没有可量化进度”时允许静默多久。它是纯数据合同，不包含模型名称判断、脚本、正则选择器或可执行表达式；运行时只读取 ProductionRun/GenerationTask 已冻结并校验 SHA-256 的有效合同。

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

所有时限均为 `30–86400` 的整数秒。`node_overrides` 的键必须存在于本版本 graph；不存在的节点会在导入/校验阶段被拒绝。`node_type_overrides` 使用 graph 中精确的 `class_type`，允许 `KSampler (Efficient)` 这类扩展节点名；只禁止空值、控制字符或超过 191 个字符的键。节点 ID 容易随图变化，能用稳定节点类型表达时优先使用模型版本的 `node_type_overrides`。平台不会把无法表达的节点专属需求降级为任意文本输入。

## 合并与执行规则

有效窗口的优先级为：

1. 工作流节点 ID；
2. 工作流节点类型；
3. 模型节点类型；
4. 工作流默认值；
5. 模型默认值；
6. 任务冻结的历史全局 `task_idle_timeout_seconds`。

任务进入一个新节点时，worker 只设置或延长一次该节点的 idle deadline。相同节点后续的 `running` 心跳、轮询成功或采样百分比只作为遥测展示，不会形成无限滑动窗口。任务 hard deadline 来自已冻结的执行容量合同，以 `production.task_timeout_seconds` 和主机容量上限解析出的有效值为准；任何 idle 窗口都被 hard deadline 截断，hard deadline 本身永不续期。没有更具体规则时使用的历史全局值是 `runtime.task_idle_timeout_seconds`。

如果 ComfyUI WebSocket 没有提供节点切换事件，worker 无法应用某个节点 ID/类型的专用窗口，只会保留提交阶段设置的默认窗口。因此，已知目标环境可能缺少节点遥测时，应把 `default_idle_timeout_seconds` 设置为能覆盖整次远端执行的保守值，并同时确认冻结的硬时限更长。

## 冻结与审计

创建新 ProductionRun 时，系统把模型规则和工作流规则合并为规范化有效合同，并在运行的媒体路由中冻结合同及 SHA-256。创建媒体任务时，同一合同还会写入 GenerationAttempt 请求快照、GeneratedWorkflow 执行元数据和 GenerationTask capability snapshot。新 GeneratedWorkflow 已冻结合同而 GenerationTask 缺失、哈希错误或与路由漂移，都会在提交 ComfyUI 前阻断。

GeneratedWorkflow 执行元数据和 GenerationTask capability snapshot 两层都必须存在合同/hash，且规范化合同与摘要必须完全一致。任一层缺失、损坏或漂移都会在远端提交前 fail closed，不会回退到当前全局值，也不会伪造回填新合同。开发阶段的旧任务/旧产物不兼容此新主路；统一清理后，必须通过新 WorkflowVersion、Recipe 和 ProductionRun 重建。已发布的不可变工作流不会自动获得新规则。

## node 10 长采样节点迁移示例

假设当前已发布工作流的长节点为 `10 / SamplerCustomAdvanced`，原全局 idle 为 1800 秒，而一次真实采样可能需要 45–70 分钟：

1. 创建下一模型能力版本，在类型化执行监控字段设置 `SamplerCustomAdvanced: 5400` 并发布；不要覆盖已发布版本。
2. 回到 ComfyUI 保存新的工作流修订，从 Settings 扫描并导入引用该模型版本的新 WorkflowTemplateVersion。若图中还有同类型短节点且确实需要 `node 10` 专属窗口，先由开发侧扩展受审 workflow-family projector；版本验收页不接受手填 `node_overrides`。
3. 确认 `production.task_timeout_seconds` 及主机容量解析出的有效硬时限大于 5400 秒；否则 idle 窗口仍会被硬时限截断。
4. 完成合同校验、真实 smoke、输出探测并发布新版本。
5. 创建新的 ProductionRecipeVersion，选择该工作流版本并发布；之后只对新建 ProductionRun 生效。
6. 在新任务详情核对冻结合同 hash、节点进入时间、idle deadline 与 hard deadline。重复采样进度不应改变 idle deadline。

这里的节点和值只是对当前图的迁移示例，不是针对某个模型名称的代码分支。其他模型或工作流通过同一合同接入，无需修改主逻辑。

## 常见阻断

- `schema_version` 不是 `1`：改为当前支持的合同版本。
- 节点 ID 不存在：修复注册 projector 或 ComfyUI 源修订后重新导入；不能在页面复制旧版本 ID。
- 时限小于 30 秒或大于 86400 秒：调整到允许范围。
- 合同或 SHA-256 漂移：不能手工改历史快照，应创建新版本和新 Run。
- 配置 5400 秒仍在更早时间终止：检查 hard deadline；它是不可续期上限。
- 节点专用规则未生效：核对 WebSocket 是否报告节点切换；没有节点遥测时使用足够的默认窗口。
