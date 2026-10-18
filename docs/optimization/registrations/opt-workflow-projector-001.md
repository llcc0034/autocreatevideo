# OPT-WORKFLOW-PROJECTOR-001 工作流合同方案版本化与 H3 模式配置化

> 历史方案／登记依据，不是当前施工清单；取消或延期事项不会因本次整理恢复。

[总索引](../README.md) · [当前续接](../current.md) · [按日期查证据](../records/README.md)

<!-- source-section d9da6688c59b -->
<a id="opt-workflow-projector-001-工作流合同方案版本化与-h3-模式配置化"></a>

## OPT-WORKFLOW-PROJECTOR-001 工作流合同方案版本化与 H3 模式配置化

> 2026-09-21审核：开发已有后续集成/切换及部分实际操作记录（统一计划25.179–182）；本项改列待验收。下列9月初记录用于追溯原验收范围；未覆盖的生命周期、异常来源及发布后重投影仍需补证，不因部分成功提前关闭。

<a id="背景与根因-1"></a>

### 背景与根因

旧实现把 H3 图族指纹、conditioner 模式、输出节点、模型 selector 和动态素材规则分散在导入代码中。规则变化必须改代码，T2VA/FL2VA/Ref2VA 容易互相误判，且历史工作流无法证明当时使用了哪一版投影规则。

<a id="当前已实现自动验证完成2026-09-04-1"></a>

### 当前已实现（自动验证完成，2026-09-04）

1. 新增稳定 `WorkflowProjector` 与版本化 `WorkflowProjectorVersion`；definition 冻结 driver、operation/output、节点角色/连接/数量、参数策略、模型文件角色、素材槽/动态引用和注册 adapter，并记录 schema/size/SHA 与发布审计。
2. 代码只保留受审 engine、adapter 和安全原语；具体 `generic.comfy.fixed`、H3 dual-clock T2VA、FL2VA、Ref2VA 由版本数据描述。
3. 新投影只使用 active family 的 current published 版本；合同草稿与工作流输入合同冻结 projector version ID/number、definition SHA 和 engine identity。
4. 发布 projector 后继版本不改写历史 candidate、draft、WorkflowTemplateVersion 或运行；采用新规则可基于同一不可变 candidate 生成新的 draft/version，只有来源字节变化时才生成新的 candidate revision。

<a id="剩余交付与验收-1"></a>

### 剩余交付与验收

- 生命周期权限与跨 family 并发、engine 跨字段 Schema、选择歧义、历史冻结、种子、OpenAPI 和管理 UI 已纳入实现。管理 UI 已改为服务端 options 驱动的分区参数表单，Engine 锁定唯一 adapter/projection 合同，完整 JSON 降为高级复核入口；7 项 Projector 服务回归及结构化编辑器数据级前端回归已通过。剩余经授权的新建草稿、发布、退役和启停生命周期写操作验收后再关闭本项。
- 自动验收已覆盖：稳定 family key 与 published/retired 内容不可覆盖；每 family 仅一个 current published；未知 engine/adapter 失败关闭；projector 缺失/歧义只生成 candidate-only draft；同 engine 的后继版本或替代 family 可按冻结 identity 校验；promotion 精确验证版本 ID、SHA 与 adapter。
