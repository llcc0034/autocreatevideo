# OPT-SMOKE-UI-001 Deployment Smoke 按 operation 补齐集中覆盖矩阵

> 历史方案／登记依据，不是当前施工清单；取消或延期事项不会因本次整理恢复。

[总索引](../README.md) · [当前续接](../current.md) · [按日期查证据](../records/README.md)

<!-- source-section 0ee1dbad3f95 -->
<a id="opt-smoke-ui-001-deployment-smoke-按-operation-补齐集中覆盖矩阵"></a>

## OPT-SMOKE-UI-001 Deployment Smoke 按 operation 补齐集中覆盖矩阵

<a id="已落地与缺口2026-09-21审核"></a>

### 已落地与缺口（2026-09-21审核）

- 状态：实施中。`frontend/src/components/DeploymentCapabilitySmoke.vue`已逐能力显示证据时间、到期、过期状态、策略分项与最近任务状态；不能继续登记“只有最近一条聚合状态”。
- `frontend/src/services/deploymentCapabilitySmoke.ts`与同名测试处理证据信号；资源Smoke与执行实例组合Smoke不是同一事实，不以基础能力0/N推断组合不可用。
- 剩余开发/交互：逐能力精确Attempt编号与详情定位、行内复测动作的完整闭环；复用原正式入口，不创建第二证据表或把查询变成执行。
- 剩余验收：多operation、未测/过期/失败/不确定、最新失败覆盖旧成功、单行重跑不影响其他行，以及行内对象与服务端身份一致。新增真实Smoke须另行明确授权。
