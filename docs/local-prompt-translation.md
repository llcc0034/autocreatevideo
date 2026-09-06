# Local Prompt Translation and Authoring Adapters

本文描述内容创作阶段的翻译/提示辅助，不把它们当作 ProductionRun 媒体执行器或生产就绪证明。

## 职责边界

authoring adapter 可以帮助操作员生成或翻译导演字段、提示文本和内容草案。输出始终是待审核内容，不能：

- 直接创建媒体 Task；
- 绕过脚本或 Bible 审核；
- 选择 ProductionRun route；
- 证明某个媒体模型/deployment 已启用；
- 代替 semantic workflow validation 或 real smoke。

所有新生产仍从 ProductionRun 创建。

## prompt.normalize 部署

提示词翻译不再由页面选择 `local_qwen` 或 `deepseek_api` 固定枚举。页面读取项目级 `prompt.normalize` capability，展示所有由已发布 `ModelProfileVersion.specification.capabilities` 精确声明该能力的部署，并以 `ModelDeployment` UUID 提交。

当前 standalone `prompt.normalize` 只允许 loopback OpenAI-compatible endpoint。外部或非 loopback 部署会显式返回 `external_idempotency_unimplemented`，不能烟测或提交；翻译 API 和页面没有外发授权或预算入口。云端脚本生成和媒体 ProductionRun 的外发/预算机制不受此边界影响。

部署只有在 provider、credential binding、配置签名和未过期真实 smoke 都有效时才是 `ready`。smoke 与正式调用共用 request id、受保护 token 和 English 非拉丁文字校验；返回原样中文不会通过 smoke。

管理员必须显式审核并发布 text profile，绑定受控 endpoint，通过 SecretBinding 选择受管凭据（若驱动要求）并执行真实 smoke。catalog 中出现 provider 名称不代表目标环境已经安装、授权或验证。

本地模型的安装与进程生命周期由部署运维负责；平台只连接已登记的受管 endpoint。普通 setup、restart 和 demo seed 不会下载模型或发布模型配置。

## 数据规则

- 原文是权威内容事实；翻译结果保留来源 revision。
- AI 输出进入 draft/review，不自动确认。
- 脚本确认后，ProductionRun 的 AI 路径以确定性逻辑 materialize Bible。
- 对失败、超时、未配置、配额不足和返回格式错误显示真实状态。
- 不使用旧缓存、模板文本或空结果伪装成功。
- ProductionRun 创建后使用内容与配置快照，不因后续翻译变化静默漂移。

## 配置与安全

provider credential 只通过 SecretBinding 使用；明文只在超级管理员的 write-only 请求中出现，数据库保存 AES-256-GCM 密文。页面、普通 API、日志、fixture 和文档不返回明文、密文、nonce、key id、Authorization header 或内部 endpoint。

对本地 runtime 记录模型来源、hash、许可与版本。对外部 provider 记录 request/attempt、耗时、错误码和审计关联，但在日志中遮蔽敏感内容。

## 操作流程

1. 在项目内容页面选择待翻译/生成的字段。
2. 从服务端动态列表选择 `ready` 的本地 deployment；没有项目默认值时必须人工选择。
3. 以该 deployment UUID 提交 authoring 请求。
4. 审阅输出与原文差异。
5. 明确接受或修改，不自动写成确认态。
6. 完成脚本/Bible 审核后，从 ProductionRun 页面开始生产。

如 ProductionRun 选择 AI script 模式，其正式阶段是“脚本生成 → 人工审核 → 确定性 Bible → 人工审核”；不要把普通字段翻译按钮写成该流程的替代入口。

## 验证

自动化测试应覆盖未配置、超时、格式错误、revision 漂移、credential 不回显和审核 gate。真实 provider 仍需在目标环境验证授权、配额和响应；mock 测试不是外部 provider E2E 证据。
