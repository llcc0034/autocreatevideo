# OPT-DIST-001 z-image-turbo 目标环境真实分发复验

> 历史方案／登记依据，不是当前施工清单；取消或延期事项不会因本次整理恢复。

[总索引](../README.md) · [当前续接](../current.md) · [按日期查证据](../records/README.md)

<!-- source-section f3608e001939 -->
<a id="opt-dist-001-z-image-turbo-目标环境真实分发复验"></a>

## OPT-DIST-001 z-image-turbo 目标环境真实分发复验

<a id="已落地合同"></a>

### 已落地合同

- 分发服务已经从冻结 WorkflowTemplateVersion 解析精确 ModelProfileVersion 的模型身份，披露与 manifest 使用模型中立合同，不维护 z-image 或其他模型名注册表。
- 分发记录冻结源/发布 SHA-256、model key、版本、`media_profile_sha256` 与目标结果；媒体处理 profile 由 release config 和 `release_id` 约束，页面继续把本地生成成功和分发 `ready/failed/pending` 分开显示。
- 因此不再建设“每模型 managed distribution profile”，也不为 z-image 增加专用分支。

<a id="历史证据2026-08-26-3"></a>

### 历史证据（2026-08-26）

- 当时三个 Bible 图片任务的本地 Asset、审核、逐镜绑定和 ReleaseBundle 成功，但附加 distribution 因无法从当时冻结事实解析受管模型身份而失败。
- 该失败解释历史样本，不代表当前模型中立分发合同仍有同一缺陷。

<a id="当前观察与触发条件"></a>

### 当前观察与触发条件

- 2026-09-02 本地只读快照没有形成可声明为当前 z-image 目标环境 E2E 的关联 Workflow 与 GenerationTask 证据，因此不能把单元测试写成真实分发通过。
- 只有出现明确对外分发目标、精确发布 Workflow 和真实图片任务后才执行复验；核对 label、model key/version、源/发布 SHA-256、`media_profile_sha256`、`release_id`、manifest 和目标回执。
- 真实复验通过后关闭本观察项；失败继续与本地生成成功分开展示，并按冻结事实定位通用合同问题，不补模型专用 profile。
