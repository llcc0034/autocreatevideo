# 可配置边界与误阻断审计（2026-08-30，0116–0127 已收敛）

> 文档类别：历史证据（2026-09-21统一标注）。正文中的版本、任务、通过/失败及“进行中/下一步”仅表示当时状态，不是当前操作指令或新的执行授权。当前未完成工作以[统一台账](README.md)为准，实际操作按[当前手册](../production-operator-manual.md)。不按旧记录重放任务，不把旧验收外推到重建配置。

## 当前结论

0116–0127 已把项目创建、Provider/Deployment 执行、运行恢复、内容合同、canonical 完整上下文建议、运营分页、节点元数据浏览与 GPU 探测超时的普通业务策略收敛为版本化或已发布配置合同。受限异步 REST 的终态与产物主机列表容量已进入独立安全边界；媒体 Workflow 的尺寸倍数也必须由不可变版本显式证明。通用主流程不再以模型名、页面或 worker 内的固定数值作为第二事实源。

建议值只形成可审计警告，不改变成功状态、不隐式拒绝请求，也不自动无限重试。只有冻结能力合同、结构协议和安全绝对上限可以阻断；阻断必须指出缺失或不匹配的合同字段。

## 分类与执行口径

| 类型 | 当前事实源 | 处理方式 |
| --- | --- | --- |
| 创作建议、质量启发式、队列建议 | 已冻结的 PromptProfile/ModelProfile advisory policy、`production.recommended_*` | 仅写入 warning/advisory，人工决定是否调整；不作为创建或入队门禁 |
| 模型、工作流与媒体能力 | 已发布 ModelProfileVersion、WorkflowTemplateVersion、Deployment 和新鲜 Smoke 所冻结的合同 | 不匹配时显式失败；范围、槽位、分辨率、FPS、时长和输入输出不可由调用方猜测 |
| 普通运行策略 | 当前已发布 ConfigRevision 或创建时冻结到任务/运行快照的配置 | 使用配置值；修改通过受控 revision 发布，不在页面或 worker 复制默认值 |
| 平台安全绝对上限 | `security_boundary` 类型的 system 配置 | 始终 fail closed；只允许超级管理员创建、修改、发布或激活相关变更 |

## 0116–0127 已完成事实

| 版本 | 已完成收敛 | 当前主路径 |
| --- | --- | --- |
| 0116 | 运行主机探测、维护与相关运行策略迁入配置；安全限制作为独立边界定义 | worker 从已发布配置读取间隔、租约和回收策略，普通配置受安全 ceiling 约束 |
| 0117 | Provider/Deployment 执行合同显式化 | async REST、ComfyUI 与兼容文本端点必须声明运行源、路径、超时、轮询、能力 TTL、响应字段和幂等约定；任务冻结已验证合同，缺失或损坏即失败 |
| 0118–0119 | 运行状态与内容合同继续版本化 | 就绪/分发状态及内容结构限制不再由读取侧临时补值；非法或未知合同数据明确失败 |
| 0120 | workflow smoke、worker stale recovery、deferred retry、assembly retry 等运行策略收敛 | 普通运行值来自 system/project 配置并受 security ceiling 约束；入队时需要冻结的重试/预算事实写入对应快照 |
| 0121 | 已退役 legacy draft graph 删除 | 四张 draft 表及其孤立入口已移除；`Shot.detailed_description`、ProductionRun、审计事实和当前正式创作链不作为清理对象 |
| 0122 | Shot 与 PromptArtifact 的时长模型默认值删除 | 调用方必须明确提供时长；不再从模型字段默认值伪造业务时长 |
| 0123 | `production.project_creation_policy` 升级为 v2 | 默认语言、默认画幅及可选画幅、FPS、默认镜头媒介类型、剧集/镜头/容量与连续性策略均由闭合的 v2 合同定义，并受对应安全 ceiling 限制 |
| 0124 | PromptArtifact 业务布尔默认值删除 | `is_multi_shot` 与 `include_audio` 必须由创建路径明确写入，不再让模型默认值承担业务选择 |
| 0125 | 资产恢复、运营分页及节点元数据浏览配置化 | 提交宽限、uncertain reconciliation batch、默认页大小、节点元数据页大小和输入预览数量从当前 system 配置读取；查询、批量和客户端列表仍受 security ceiling 限制 |
| 0126 | canonical 完整上下文建议、GPU 探测超时及异步 Provider 合同容量收敛 | `production.canonical_context_policy` 只产生 warning/audit，不截断事实；`runtime.gpu_probe_timeout_seconds` 受运行时 timeout ceiling 保护；三项 `security.provider.async_rest_*` 作为 superadmin-only 安全边界 fail closed |
| 0127 | 媒体 Workflow 冻结尺寸倍数准入 | 显式宽高必须同时冻结一致的 `required_dimension_multiple`、width/height `multiple_of` 及可整除的 graph/default；无法证明网格的 current/published 旧版本保留历史证据但退出生产准入，validated 版本回到 draft 重新审核 |

## 当前关键合同

### 项目创建 v2

`production.project_creation_policy` 是闭合对象合同：包含语言、画幅与选项、FPS、默认镜头媒介类型、剧集与镜头数量、建议时长、最大生成任务数及连续性种子策略。项目创建只使用该发布策略；安全边界限制最大 FPS、剧集数、镜头数和生成任务数。它不将任何建议值转化为隐式的创作质量门禁。

### PromptArtifact 事实

PromptArtifact 不再依靠字段默认值推断 `is_multi_shot`、`include_audio` 或业务时长。创建者必须写入这些事实；缺失应由输入合同或服务显式拒绝，而不是在读取、序列化或执行时补成“看似成功”的默认值。

### Provider、Deployment 与执行快照

Provider/Deployment 的传输路径、请求/响应字段、终态、幂等头、超时、轮询和能力 TTL 必须是显式合同。执行时使用已选择并冻结的版本/Deployment 合同；运行期不回退浏览器默认值、模型名称特判、固定 timeout 或固定 poll interval。远端提交不确定、合同哈希不一致、缺少必填字段或不符合安全边界时必须失败并保留审计事实。

### 恢复、分页和节点浏览

陈旧任务恢复、提交宽限与不确定任务核对批量属于普通运行策略；运营列表默认页大小、ComfyUI 节点元数据分页和输入预览大小也是普通配置。它们可以由已发布 system revision 调整，但输入仍须通过单一 server-side security ceiling，不能由前端扩大或以本地常量取代。

### Canonical 完整上下文、GPU 探测与异步 Provider 边界

`production.canonical_context_policy` 是 system/project 普通配置，只为专名 glossary、Bible 描述和视觉身份上下文生成非阻断建议审计；超过建议容量仍保留完整作者事实，最终请求只由既有安全字节包络阻断。`production.canonical_name_policy` 中实体与推理项容量同样是建议/分批口径，不是业务拒绝条件。

`runtime.gpu_probe_timeout_seconds` 是 system 普通配置，运行主机探测读取当前 active revision 并受 `security.runtime.max_timeout_seconds` 封顶。受限异步 REST Provider 的终态每类项数、单个状态字符数和产物主机白名单项数分别读取三项 `security.provider.async_rest_*` 安全边界；命中上限时明确失败，不能静默截断后伪装为可执行合同。

### Workflow 冻结尺寸倍数

`generic.comfy` 与 `minimax_h3` 的图片/视频显式宽高合同必须在版本中保存同一个正整数 `required_dimension_multiple`，并与 `image.width`、`image.height` 的 `multiple_of` 完全一致；冻结 graph literal 与使用的 default 也必须满足该倍数。导入预览可以显示 blocker，但 blocker 未消除就不能导入、Live Validate、Smoke 或 Publish。`aspect_megapixels` 继续 fail closed。后续增加的 `source_image_derived` 只接受已注册的 FluxKontextImageScale 尺寸链、受管图片真实字节及运行产物核验，边界见 [输入图片决定输出尺寸](../comfyui-saved-workflow-validation.md#31-输入图片决定输出尺寸)；不能填 `1` 或模型名默认值绕过。

0127 不重写 published 工作流及其历史 Smoke：缺少上述证明的 published/current 版本改为 retired/non-current，validated 版本清除失效准入证据并回到 draft。恢复生产必须导入新不可变版本，再依次执行 Live Validate、真实 Smoke 和 Publish。

## 必须保留的硬不变量

- 凭据不返回浏览器；SSRF、origin、路径、跨项目引用和受管 SecretBinding 校验失败必须阻断。
- Published/retired 版本不可原地改写；版本、来源、请求、素材和执行快照哈希必须可复核。
- 不确定远端提交不得自动重放；幂等、费用、有限重试和任务原子性不是 advisory。
- JSON 类型/深度/字节数、数据库关系、唯一性、媒体可探测性、未知 IR 字段和输出合同属于结构或安全不变量，不能降级为质量警告。
- H3 等已注册适配器的经审核图拓扑、静态语义、节点连线和协议所需结构是版本合同的一部分；例如固定音频拓扑必须通过冻结语义表达，缺失即失败，不得回退到 PromptArtifact 字段。
- 媒体 Workflow 的尺寸网格必须来自冻结版本合同；不得在物化、Smoke 或 worker 中从当前 host、模型名称或隐藏默认值补出 `required_dimension_multiple`。

安全绝对上限使用 `security_boundary` 分类、仅允许 system scope，并在创建/修改定义、发布 revision 与激活 revision 时强制超级管理员权限。普通管理员可管理普通配置的权限不等同于调整安全边界的权限。

## 何时仍需代码

配置和版本合同表达的是数据、范围、默认选择和可验证能力；它们不承载可执行脚本。出现以下变化时仍需新增或修改代码，并配合新版本合同与验证：

- 新 Provider 的鉴权、提交、轮询、取消、采集或错误协议；
- 现有语义绑定无法表达的新 ComfyUI 图转换、输入拓扑或输出探测；
- 新的安全验证、数据迁移、审计事实写入或确定性媒体处理；
- 需要把外部不稳定响应转换为本地冻结事实的适配器。

代码只能实现受范围约束的协议/适配器与验证器，不能借此重新引入模型名分支、运行时静默默认值或第二套业务阈值。新的可调数值应先进入发布的配置或版本合同；新的绝对资源限制应进入 `security_boundary`，而不是前端常量。

## 验收口径

1. 建议值超出时仍可进入正式流程，并留下 warning/advisory，而非被静默阻断。
2. 更换模型、Workflow、Deployment 或项目创建策略时，通过发布新合同完成；通用业务流程不需要添加同义数字。
3. 缺失、篡改或不匹配的冻结能力/执行合同明确失败；不得回退旧字段、页面默认值或本地常量。
4. 只有超级管理员可以改变安全绝对上限；所有其他路径只能读取并执行该限制。
5. 新协议或新适配器以受测代码实现，并与其版本合同、Smoke 和审计事实同步交付。
