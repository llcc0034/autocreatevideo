# MVP Requirements

## 产品目标

MVP 为单人或早期团队提供一条可追踪、可审核、失败可恢复的逐集媒体生产链路。验收对象是 ProductionRun、受管产物、逐集母版和 ReleaseBundle，不是某个固定模型、固定节点 ID 或单台 GPU。

## 角色

- 内容操作员：维护脚本、Bible、镜头并完成内容审核。
- 生产操作员：创建 ProductionRun，处理路由、成本、候选和异常 gate。
- 平台操作员：维护 provider、deployment、workflow、configuration 和 runtime host。
- 审核/交付角色：验证逐集母版与 ReleaseBundle。

同一人可承担多个角色，但状态和审计边界仍需保留。

## 必须实现的生产链路

1. 项目拥有已确认脚本，或由 ProductionRun 进入 AI 脚本路径。
2. AI 脚本必须先审核；Bible 由确认脚本确定性生成后单独审核。
3. storyboard、route、cost、recipe、workflow 和 config snapshot 在生产前锁定。
4. 所有新生产从 ProductionRun 创建并按 gate 推进。
5. 媒体输出归档为 managed artifact 并记录 attempt/provenance。
6. 每个 episode 生成独立 master。
7. ReleaseBundle 聚合并验证逐集母版。

Task、DeferredGenerationRequest、Assembly、ReleaseBundle 只用于完全只读诊断。创建、重试、取消与有序 fallback 只允许通过所属 ProductionRun，不存在任务级例外。

可发布 ProductionRecipe 必须包含唯一正式 content 入口，以及完整有序的 content → storyboard → prompt → media → selection → assembly → delivery 闭包。

## 工作流与执行器

MVP 必须支持：

- 以 semantic inputs/outputs 绑定 Comfy workflow；
- workflow graph validation；
- real smoke evidence 后显式 publish；
- async REST T2I；
- async REST T2V；
- 外部结果归档与媒体校验。

bootstrap workflow catalog 必须保持 inactive draft、\`needs_review\`，直到操作员完成绑定、验证和真实 smoke。

I2V/reference image 不得假装支持。没有 provider-specific upload contract 时必须以 \`provider_upload_contract_missing\` 阻断。当前不要求跨节点或多 GPU scheduler。

## 配置与运行主机

MVP 的有效配置由五层组成：model version default、workflow version default、active system revision、active project revision、active run override。ProductionRun 冻结最终 snapshot；restart-required revision 需要 pending 状态、新 boot identity 和显式 activation。

RuntimeHost 必须能 probe 实际能力并生成 recommendation draft。decide 必须由服务端复验 host snapshot、catalog/config hash 与候选当前兼容性并拒绝陈旧推荐。推荐不能自动下载模型、伪造 deployment、发布 workflow 或激活配置。

## 数据与产物

- PostgreSQL 是结构化事实源，Django migration 是唯一 Schema 历史。
- generation、text、async-media 与 assembly 队列状态均持久化在数据库；worker 通过 claim/lease 领取，不能另建第二套消息队列事实。
- 原始输入、生成输出和 master 使用受管存储。
- 远端 URL 必须归档后才能成为 production artifact。
- \`comparison_parameters\` 必须在归档时从冻结运行事实白名单生成；参数差异不得使用 manifest 身份、对象键或 hash。
- episode master 必须记录 source items 与 provenance。
- ReleaseBundle 不把整季拼成单条长视频。

## 安全与审计

- credential 由 SecretBinding 关联不可变 SecretVersion；原值以 AES-256-GCM 加密入库，普通读取只返回 configured/usable state。
- API、前端、日志、文档和审计正文不得回显秘密。
- 超级管理员独占用户角色、状态、临时密码、权限审计和 SecretBinding 写入；临时密码首次登录必须修改并使旧 token 失效。
- workflow JSON、远端地址、文件类型和大小必须校验。
- 状态变更必须可审计，并对重复提交、lease 过期和外部不确定响应提供显式处理。
- 失败、部分成功、等待审核和阻断不得降级为成功。

## 操作入口

- 页面：项目内容、ProductionRun、平台设置、工作流版本、生产手册。
- API：根 \`openapi.yaml\` 声明的 \`/api/v1/\` 路由。
- 服务管理：首次安装使用 \`scripts/setup.ps1\`，日常启动/重启使用 \`scripts/restart.ps1\`；当前没有独立的本地停止脚本。
- Schema：Django migrations。
- 机器 API 契约：根 \`openapi.yaml\`，动态 \`/api/schema/\` 同源。
- demo：仅显式 \`seed_data --with-demo-project\`。

## MVP 非目标

- 自动下载或安装任意模型；
- 自动发布 workflow 或自动激活推荐；
- provider 通用 reference upload 猜测；
- 跨节点、多 GPU、抢占式 scheduler；
- 整季单条长视频；
- 用 catalog 条目声称模型已部署或许可已确认；
- 用 mocked test 代替真实 provider/GPU E2E。

## 验收标准

代码级验收应覆盖 ProductionRun 状态机、AI script/Bible 审核、semantic compiler、async T2I/T2V、上传契约阻断、managed artifact、逐集 master、ReleaseBundle、五层配置、boot activation、host probe/recommend 和旧写入口拒绝。

环境级验收还必须在目标机器补齐：

- migrations/check 通过；
- provider 授权真实可用；
- deployment probe 与 real smoke 通过；
- T2I/T2V submit/poll/archive 完成；
- artifact 校验与逐集装配完成；
- ReleaseBundle verify 通过；
- 备份恢复演练成功。

没有这些证据时，只能声明“实现了契约与流程”，不能声明真实外部 provider/GPU 生产就绪。
