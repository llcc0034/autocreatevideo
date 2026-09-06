# ComfyUI Workflow Audit

本文用于沿同一证据链审阅 Comfy saved workflow 从“可纳管来源”到“可进入 ProductionRun 路由”的状态。它不指定固定模型、固定节点号或固定 GPU，也不把 candidate/draft 当成可执行版本。

## 审阅对象

审计对象分为五层，不能合并成一个“已导入”状态：

1. `ComfyWorkflowSource(import_kind=candidate)`：来源作用域、规范化路径、原始文档、大小、source/document/import SHA 和操作者；这里只证明目录与文档安全，可以不存在目标 Deployment、published 模型或 projector。
2. `ComfyWorkflowContractDraft`：精确 candidate、主输出、可选模型版本、projector version、`object_info`/converted graph/structural/projection SHA、admission tier、actionability、blockers 和 deferred Live Validate requirements。
3. `WorkflowTemplateVersion`：由 ready draft 提升出的不可变结构合同与正式 provenance。
4. validation/smoke evidence：精确目标 Deployment、实时 `object_info`、模型文件、fixture、compiled graph、真实提交、媒体探测和产物证据。
5. lifecycle/route evidence：publish/retire 审计以及 Recipe、ProductionRun 冻结引用。

每个可执行 `WorkflowTemplateVersion` 应同时包含：

- 原始 workflow graph；
- workflow family；
- semantic input bindings；
- semantic output bindings；
- executor compatibility；
- validation evidence；
- real smoke evidence；
- 状态与发布审计。

candidate 和 contract draft 永远不能进入 Live Validate、Smoke、Recipe 或 ProductionRun。提升时只验证并消费不可变 draft，不重新读取本地文件或实时 `object_info`；只有本地来源字节变化时才重新扫描并形成新 candidate revision。`object_info`、Projector 或目标 Deployment 变化不会改写 candidate：应从同一 candidate 生成新 draft/version，并重新完成 Live Validate 与 Smoke。

生产编译器通过 semantics 定位 prompt、negative prompt、尺寸、帧数、帧率、seed 和输出节点，而不是依赖某个模板的固定 node ID。

## 初始 catalog

bootstrap catalog 条目刻意创建为 inactive draft、\`needs_review\`、smoke not run。它们是待审阅的 catalog/adapter 候选，不是已启用生产路径。demo seed 和服务启动不会覆盖这些版本。

catalog 可能保留 H3、SDXL 或其他模型/adapter 名称，用于兼容性与许可审阅；这些名称不证明模型已下载、deployment 已建立、节点包已安装、workflow 已验证或商业许可已确认。

## 上线检查

1. 从配置的受控目录 discover，并显式纳管 immutable candidate；这里只检查来源目录、JSON、大小和敏感字段。
2. 基于该 candidate preview；先选唯一主输出，再生成不可变执行合同草稿。未匹配 projector 或模型证明时保留 `candidate_only` 和明确 blocker，不删除来源。
3. 只把 `ready_for_executable_import` 草稿提升为 needs_review WorkflowTemplateVersion；核对草稿 ID、projection SHA 和冻结 projector identity。
4. 在目标 ComfyUI 安装所需节点包和模型，建立 active 目标 Deployment，补齐模型文件证明；平台不会自动下载。
5. 运行 Live Validate，修复缺失节点、类型错误、歧义绑定、projector/adapter 漂移和不兼容输出。
6. 运行真实 Smoke。证据必须包含具体 fixture、实际 executor submission、媒体探测和受管结果。
7. 审阅输出媒体、managed artifact、attempt 与 provenance。
8. 显式 publish。
9. 用新的 ProductionRun 验证路由快照。

仅能构造请求、mock 返回成功或打开 ComfyUI 页面都不算 real smoke。

## semantic binding 原则

- binding 指向节点与字段，但业务代码只消费 semantic 名称。
- workflow family 定义必须输入、可选输入和输出。
- 图更新后必须重新 validate；旧 evidence 不自动继承。
- 多个节点满足同一 semantic 且无明确 binding 时应失败，不猜测。
- 输出节点必须可追踪到实际媒体结果。
- 不在 prompt 文本中隐藏无法验证的节点参数。

## Projector 版本审计

`WorkflowProjectorVersion` 是独立版本化领域对象，而不是普通 ConfigRevision：

- 稳定 family key 创建后不可变；版本只按 draft → published/current → retired 推进。
- definition 冻结 driver、operation、output kind、节点角色/连接/数量、参数策略、模型文件角色、素材槽/动态引用和注册 adapter；服务端记录 schema、大小、SHA、发布人和发布时间。
- engine/compiler/adapter 必须来自受审代码注册目录，definition 不能注入代码或任意执行 key。
- 合同草稿与工作流输入合同都冻结精确 projector version ID/number、definition SHA 与 engine identity。
- 发布后继 projector 版本不重写旧 candidate、草稿、WorkflowTemplateVersion、Smoke 或 ProductionRun；采用新规则可基于同一不可变 candidate 创建新草稿和新版本，只有来源字节变化时才创建新来源 revision。

## 执行与产物

Comfy executor 返回的历史记录或文件引用必须归档为 managed artifact，完成地址、类型、大小和媒体校验后才能进入装配。ProductionRun 记录 workflow/version/deployment/config 快照，避免发布后修改影响既有 run。

I2V/reference image 与 async REST upload contract 是另一执行器边界；不能因为 Comfy workflow 能接收本地图片，就宣称所有 provider 都支持 reference upload。

## 失败处置

- candidate admission failed：修复目录、JSON、大小或敏感字段；不要通过补 Deployment/projector 掩盖来源安全错误。
- `candidate_only`：按 blockers/recovery actions 补主输出、模型或 projector；保留候选证据，不把它显示为导入失败或可执行。
- `needs_review`：在版本验收页核对服务端冻结的语义绑定、模型证明和 projector identity；页面不编辑合同。
- validation failed：按原因回 ComfyUI、模型目录或 projector 后继草稿修正权威来源。只改 projector 时从既有 candidate 重新 draft → promote；工作流来源变化时才从新 revision 重新 candidate → draft → promote。不在平台内手改 graph/bindings，也不回退固定节点号。
- smoke failed：保留失败 evidence，修复目标 deployment 或 workflow。
- deployment unavailable：重新 probe host/deployment。
- artifact invalid：检查远端输出、归档和媒体校验。
- route incompatible：选择兼容 route 或更新 catalog 草案。
- license unknown：阻断发布，补充来源与许可判断。

## 尚未证明的边界

仓库测试覆盖 semantic compiler 与失败状态，但没有真实外部 ComfyUI/GPU 完整 E2E 证据。目标环境上线必须补齐真实模型、节点包、deployment probe、workflow smoke、产物归档和逐集装配证据。
