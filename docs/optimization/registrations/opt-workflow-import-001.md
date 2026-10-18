# OPT-WORKFLOW-IMPORT-001 ComfyUI 本地工作流分阶段纳管与准入

> 历史方案／登记依据，不是当前施工清单；取消或延期事项不会因本次整理恢复。

[总索引](../README.md) · [当前续接](../current.md) · [按日期查证据](../records/README.md)

<!-- source-section fd82ed34f268 -->
<a id="opt-workflow-import-001-comfyui-本地工作流分阶段纳管与准入"></a>

## OPT-WORKFLOW-IMPORT-001 ComfyUI 本地工作流分阶段纳管与准入

> 2026-09-21审核：开发已有后续集成/切换及部分实际操作记录（统一计划25.179–182）；本项改列待验收。下列9月初记录用于追溯原验收范围；未覆盖的生命周期、异常来源及发布后重投影仍需补证，不因部分成功提前关闭。

<a id="背景与根因"></a>

### 背景与根因

旧流程把目录发现、来源登记、结构投影、模型证明和生产准入压在同一步，导致一个目录合法且可解析的工作流仅因目标 Deployment、模型文件证明或 projector 暂缺而完全无法纳管，也让“已发现”“已纳管”“可执行”在页面上难以区分。

<a id="当前已实现自动验证完成2026-09-04"></a>

### 当前已实现（自动验证完成，2026-09-04）

1. `discover` 保持只读；staff 显式以 `import_kind=candidate` 保存不可变 `ComfyWorkflowSource`。candidate 只检查受控目录、JSON、大小和敏感字段，不要求目标执行 Deployment、published 模型或 projector。
2. preview 必须基于精确 candidate；未选主输出只返回候选，选定主输出后持久化或复用不可变 `ComfyWorkflowContractDraft`，以 blockers、deferred Live Validate requirements 和 actionability 保留待处理项。
3. 只有 `ready_for_executable_import` 草稿可提升。promotion 校验并消费冻结 candidate/draft，不重读本地文件或实时 `object_info`，创建/复用 needs_review WorkflowTemplateVersion 与正式 provenance。
4. candidate/draft 无执行权；Live Validate 才要求 active 目标 Deployment、published 模型、artifact 和节点 Schema，真实 Smoke 才提交具体参数和 Asset，发布后才能进入 Recipe/ProductionRun。

<a id="剩余交付与验收"></a>

### 剩余交付与验收

- 数据迁移、后端 76 项定向回归、OpenAPI 契约、前端 candidate/draft 操作、类型检查、生产构建和正式桌面浏览器只读验收已经通过；剩余在用户选择的目标环境中显式执行 candidate/draft 实际操作、Live Validate 与真实 Smoke。未做这些有状态操作前保持“实施中”。
- 自动验收已覆盖：无目标模型/Deployment/projector 的 candidate 成功纳管；object_info 故障仍可发现并纳管；preview 无 candidate 失败；选主输出持久 draft；危险值形成 candidate-only blocker；candidate-only 不可提升；ready draft promotion 不重读来源；同执行合同跨路径复用版本；来源变化形成新 revision。
