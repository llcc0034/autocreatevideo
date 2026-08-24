# ComfyUI Workflow Audit

本文用于审阅 Comfy workflow 是否可以进入 ProductionRun 路由。它不指定固定模型、固定节点号或固定 GPU。

## 审阅对象

每个 \`WorkflowTemplateVersion\` 应同时包含：

- 原始 workflow graph；
- workflow family；
- semantic input bindings；
- semantic output bindings；
- executor compatibility；
- validation evidence；
- real smoke evidence；
- 状态与发布审计。

生产编译器通过 semantics 定位 prompt、negative prompt、尺寸、帧数、帧率、seed 和输出节点，而不是依赖某个模板的固定 node ID。

## 初始 catalog

bootstrap catalog 条目刻意创建为 inactive draft、\`needs_review\`、smoke not run。它们是待审阅的 catalog/adapter 候选，不是已启用生产路径。demo seed 和服务启动不会覆盖这些版本。

catalog 可能保留 H3、SDXL 或其他模型/adapter 名称，用于兼容性与许可审阅；这些名称不证明模型已下载、deployment 已建立、节点包已安装、workflow 已验证或商业许可已确认。

## 上线检查

1. 确认 workflow graph 来源、hash 和许可记录。
2. 在目标 ComfyUI 安装所需节点包和模型；平台不会自动下载。
3. 为所有必需 semantics 建立唯一、可解析的绑定。
4. 运行 validate，修复缺失节点、类型错误、歧义绑定和不兼容输出。
5. 将 workflow 关联到已 probe 的 model deployment/runtime host。
6. 运行真实 smoke。证据必须包含实际 executor submission 和受管结果。
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

## 执行与产物

Comfy executor 返回的历史记录或文件引用必须归档为 managed artifact，完成地址、类型、大小和媒体校验后才能进入装配。ProductionRun 记录 workflow/version/deployment/config 快照，避免发布后修改影响既有 run。

I2V/reference image 与 async REST upload contract 是另一执行器边界；不能因为 Comfy workflow 能接收本地图片，就宣称所有 provider 都支持 reference upload。

## 失败处置

- \`needs_review\`：完成语义绑定和人工审阅。
- validation failed：修复 graph/bindings，不回退固定节点号。
- smoke failed：保留失败 evidence，修复目标 deployment 或 workflow。
- deployment unavailable：重新 probe host/deployment。
- artifact invalid：检查远端输出、归档和媒体校验。
- route incompatible：选择兼容 route 或更新 catalog 草案。
- license unknown：阻断发布，补充来源与许可判断。

## 尚未证明的边界

仓库测试覆盖 semantic compiler 与失败状态，但没有真实外部 ComfyUI/GPU 完整 E2E 证据。目标环境上线必须补齐真实模型、节点包、deployment probe、workflow smoke、产物归档和逐集装配证据。
