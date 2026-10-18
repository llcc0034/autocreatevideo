# OPT-CAPACITY-001 主机容量证据包 / Certified execution envelope

> 历史方案／登记依据，不是当前施工清单；取消或延期事项不会因本次整理恢复。

[总索引](../README.md) · [当前续接](../current.md) · [按日期查证据](../records/README.md)

<!-- source-section 650d097051e0 -->
<a id="opt-capacity-001-主机容量证据包--certified-execution-envelope"></a>

## OPT-CAPACITY-001 主机容量证据包 / Certified execution envelope

2026-09-06当前决定：用户要求先打通可用主链。本项拆为首期规格观测/可配置告警（并入统一计划）与后续完整envelope认证。以下保留当轮证据文字，不要求保存不兼容数据库记录；原“全部容量认证必须先完成”的P0排期已替代。目标准入只以[统一计划1.3](../execution-instance-unification-plan.md)第17节及25.201–202为准；首期观测/告警已有实现与历史配置记录，完整认证仍延期。

<a id="已落地基础"></a>

### 已落地基础

- `RuntimeHost → HostCapabilitySnapshot → RuntimeModelAvailability` 已持久化主机硬件、快照 TTL、详情 hash 与精确模型文件 size/hash；主机 Probe 生成可审计快照。
- ProductionRun 已冻结 `execution_capacity`：绑定主机快照、配置与物理并发上限、兼容并发和超时；worker 在领取和提交前复核容量 hash、当前主机、快照完整性与并发。
- Production Readiness 已检查精确 Workflow、真实 Smoke、主机快照和模型文件证明；`uncertain`、稳定 submission token、不自动重新 POST 与版本化 `execution_monitoring` 已有独立权威合同。
- 上述基础能证明“主机、文件、并发和执行合同完整”，但不能证明“当前请求的宽高、帧数、素材组合与 steps 落在实测范围内”。

<a id="尚未完成的容量认证能力"></a>

### 尚未完成的容量认证能力

- 2026-09-21核对：`execution_capacity_signatures.py`与`execution_capacity_observations.py`已实现规格签名和每Preparation的成功样本；`execution_admission.py`按`warn/require_evidence`处理精确覆盖。缺失维度保持unknown，不能再写成完全没有规格观测。
- 当前没有 `CertifiedExecutionEnvelope` 模型、API、标准 Schema 或覆盖匹配器。
- 完整envelope、跨规格范围覆盖、峰值显存/RAM及完整多维测量尚未闭环；精确签名样本不能外推为所有尺寸、时长或参考组合已认证。
- 较小 fixture 因而仍可能让更大的生产请求进入高成本队列，在采样前 OOM、长时间无有效进度或使原生进程退出。
- 不能用模型名、固定帧数或单一 Workflow 特例修补；否则新模型、新节点或低显存实现仍需修改核心路由。

<a id="历史证据2026-08-26"></a>

### 历史证据（2026-08-26）

- 当时 H3 Workflow v3 的成功 Smoke 样本为 352×608、22 帧、4 steps、1 条参考视频；请求扩大为 124 帧、3 张参考图、1 条动态视频、长提示词与原生音频后，四个样本为 0/4 通过。
- 该案例只证明“小 Smoke 不能外推大请求”的通用缺口，不代表当前 Workflow 版本、当前主机状态或已经认证的硬件下限。

<a id="后续完整认证方案不阻塞首期可用主链"></a>

### 后续完整认证方案（不阻塞首期可用主链）

1. 由成功的真实 Smoke 派生不可变、模型无关的容量认证事实；人工只能发起、审核或退役，不能手填“通过”。
2. 每份 envelope 绑定 RuntimeHost、新鲜 HostCapabilitySnapshot、精确 Deployment/Workflow target hash、模型文件 hash、驱动与运行时版本、启动参数及 Smoke attempt。
3. 从 Workflow/Deployment 的结构化合同投影通用请求维度：输出媒体、宽高/像素、帧率、帧数/时长、steps、conditioning mode、音频模式、输入槽媒体类型与基数、参考视频有效帧及提示词长度桶。
4. Smoke 补录执行耗时、最长有效静默、峰值显存/RAM、远端终态和 FFprobe/文件探测结果；Provider 不支持的指标明确为 `unavailable`，不能伪造。
5. ProductionRun 选路和 worker 提交前调用同一覆盖匹配器；超出范围时列出具体未覆盖维度，不静默降参、删素材或换模型。
6. 在现有“验收与烟测中心”展示容量认证，不再建立独立 Smoke 入口；Workflow 发布与容量认证继续是两个独立事实。
7. envelope 可记录实测最长静默作为认证证据，但不取代现有 `execution_monitoring`、硬截止时间、`uncertain` 和不重放合同。

<a id="首期操作口径与后续收紧"></a>

### 首期操作口径与后续收紧

当前已落地的首期操作口径：

1. 只把真实 Smoke 的精确分辨率、帧数、素材槽组合、运行配置和主机视为已验证样本，不向外推断更大请求。
2. 超出/未知样本默认显示容量警告，在硬合同、原资源并发和费用授权内执行；管理员启用require_evidence才按覆盖阻断。不为每种token长度强制补测，不把放行伪装为已认证，也不手填未经测试的最低显存。
3. 任一 OOM、idle 误杀或 native abort 都保留原 Attempt，并沿现有 uncertain/失败合同核对远端终态；恢复后创建新 Attempt，不自动重放旧提交。

<a id="后续完整认证的严格模式验收"></a>

### 后续完整认证的严格模式验收

- 新模型或新工作流能只通过结构化能力合同、系统设置和 real smoke 建立 envelope，无需修改核心路由源码。
- require_evidence下，22帧/少素材样本不能授权124帧/多素材；warn下允许硬合同内请求但明确显示未覆盖。两者不改变真实样本内容。
- RuntimeHost、运行时版本、模型hash、工作流合同、启动参数或能力快照漂移后旧envelope不再显示有效；执行身份漂移仍阻断，单纯容量证据失效是否阻断由统一策略决定。
- 并发请求、参考视频帧数、原生音频和提示词长度桶均进入覆盖判定，并有边界、漂移和伪造值回归测试。
- 容量覆盖失败沿现有可审计的不确定/失败状态处理，不重复提交；恢复后必须创建新的 Attempt 和新的容量证据。
- 系统设置、Readiness、任务详情和运维指南以中英双语显示已覆盖范围、缺失维度、证据时间与失效原因。

<a id="本次不代表-1"></a>

### 本次不代表

- 本条记录不表示 certified execution envelope 已实现，也不改变现有 Workflow、Deployment 或任务状态。
- 任一历史小样本都不能替代完整请求验收；更大显存主机与 DynamicVRAM headroom 的最低安全值仍待真实测试。
- 本方案不允许用自动降参、删除参考素材或改用其他生成模式伪装请求成功。
