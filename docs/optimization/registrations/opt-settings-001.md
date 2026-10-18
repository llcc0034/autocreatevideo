# OPT-SETTINGS-001 Provider 与模型录入收敛为服务端选项目录

> 历史方案／登记依据，不是当前施工清单；取消或延期事项不会因本次整理恢复。

[总索引](../README.md) · [当前续接](../current.md) · [按日期查证据](../records/README.md)

<!-- source-section 0fd0e09d5084 -->
<a id="opt-settings-001-provider-与模型录入收敛为服务端选项目录"></a>

## OPT-SETTINGS-001 Provider 与模型录入收敛为服务端选项目录

<a id="已落实"></a>

### 已落实

- Provider 的 `driver_type`、`modalities`（支持模态）和 `operations`（声明操作）由服务端注册驱动目录形成单选/多选，不再提供自由文本输入。
- 模型主模态、业务能力、提示词策略族和 Artifact 角色由同一服务端 options 合同提供；目录失败时页面失败关闭。
- 模型版本号与规格结构版本由服务端分配；首次为 v1，下一版在事务中按全部历史递增，模型版本族与版本号创建后锁定。
- 选中这些说明性运营标记不会给驱动增加能力；即使伪造请求，输入新值不会给驱动增加能力，服务端也不读取它们做执行授权、能力兼容性校验或候选筛选。
- 当前实际能力由注册驱动的 `capabilities()`、`ModelProfileVersion.specification.capabilities` 版本合同、Deployment / Workflow 精确合同和新鲜真实 Smoke 共同确定；Probe 只确认健康和能力快照。
- 模型卡片内具体运行实例的真实 Smoke 候选操作来自模型版本 `specification.capabilities`，不是来自 Provider 的 `operations` 字符串。

<a id="验收条件"></a>

### 验收条件

- 用户不能再向 Provider 任意写入未知模态或操作。
- 切换 `driver_type` 后，可选模态与传输操作随注册驱动目录同步更新。
- 伪造 operation 不能显示为驱动能力，也不能进入烟测候选或生产路由。
- 模型业务 capability、驱动传输能力和真实执行证据三层语义在页面与 API 中明确区分。
- 旧 Provider 的既有值在受控选项中显式标为历史值，可保留或移除；新增值不再走自由文本旁路。
- 相关回归测试、OpenAPI 契约检查和系统设置页面浏览器验收全部通过。

<a id="本次不代表"></a>

### 本次不代表

- 本条记录不把 Provider 运营标记提升为可执行能力。
- 本条记录不自动改写既有 Provider 历史声明。
- 本条记录不替代 Deployment / Workflow 的真实 Probe、Smoke、Validate 或 Publish 门禁。
