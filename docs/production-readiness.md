# Production Readiness

本清单用于目标环境验收。catalog、单元测试或 mock 通过不等于真实生产就绪。

## 1. 数据与启动

- `manage.py migrate --plan` 已审阅。
- `manage.py migrate --check` 返回成功。
- 目标环境已执行迁移并留存记录。
- 使用 `scripts/restart.ps1` 启动全部服务。
- restart-required 配置已在新 boot 显式 activate。
- `scripts/check.ps1` 通过。
- 首个超级管理员已通过受控 bootstrap 创建，临时密码已首次登录修改，旧 token 已失效；普通用户和 platform administrator 看不到权限管理入口。
- `secret_store` readiness 通过，活动 SecretBinding 均可用目标环境密钥环解密；密钥环与数据库备份分开保管。
- Settings 对布尔、枚举、数值、文本和受管凭据显示类型化编辑器、说明、影响、范围、默认值与必填状态；已拆分字段不要求普通用户编辑 JSON。
- `value_schema.resource_ref` 字段使用服务端下拉选项。已验证 `prompt.normalize` deployment 选项会标记不可选原因，发布时重新核验资源存在、capability、readiness 和当前有效 operation smoke；历史空值显式 `unconfigured` 并阻断依赖生产。

## 2. Provider、模型与主机

- provider/deployment 只关联受管 SecretBinding；授权已在目标环境验证，普通 API 不返回秘密材料或内部密钥标识。
- model artifact 的来源、hash、许可和用途已审核。
- model deployment probe 成功。
- runtime host probe 是当前快照。
- recommendation 已人工审阅；如接受，仅形成草案，后续发布/激活分别执行。
- recommendation decide 已由服务端复验 host snapshot、catalog/config hash 和候选当前兼容性；陈旧结果已重新 probe/recommend。
- 没有把 catalog 条目、自动探测建议或进程并发当成已部署的多 GPU scheduler。

平台不会自动下载模型。H3、SDXL 等名称若出现，只能作为 catalog/adapter/许可审阅事实。

## 3. PromptProfile

- 字段目录中的 key、类型、说明、影响、默认值、枚举、必填性与停用状态已审阅。
- `script.generate`、`script.structure`、`prompt.normalize` 均有可被默认 resolver 唯一解析的 active binding；`image.generate`、每个 `video.generate` mode 与 Bible image 均至少有一个兼容 binding choice，并能在 ProductionRun 页面显式选择。
- 每个 Profile 的 model family/model version、operation、mode、source selector、channel、顺序、缺省处理和长度合同已在 deterministic preview 核对。
- 文本默认 resolver 的同模型多策略使用明确 specificity/priority 且不存在最高优先级歧义；媒体/Bible 多策略的每次显式 binding ID、Profile 版本与 contract hash 会完整冻结，缺失、额外、不兼容、未发布引用与 checksum 漂移已验证 fail-closed。
- seed 产生的 `needs_review` draft 不被当作生产能力，生产路径不会回退到通用提示词模板。

## 4. Workflow

- bootstrap `needs_review` 条目已逐个审阅。
- semantic input/output bindings 已解析。
- graph validation 通过。
- real smoke 包含实际 executor submission 与结果证据。
- workflow 已显式 publish。
- 没有依赖固定 Comfy node ID。
- saved-workflow v1/v2 草稿已通过 fresh 全局 discover，在所属 `succeeded` source 上携带当前 scope version/hash 重新 preview/import 为原模板的新 v3 版本；`partial` 中失败来源未被当作空目录，且没有复用旧 review 或旧 validation/smoke evidence。
- 新发现文件只有在唯一受支持主输出、唯一模态兼容 published 模型版本、同来源 active Deployment、实时 object_info、非空模型依赖、每项依赖的最新未过期 AVAILABLE size/hash 证明及无 blocker 同时成立时才标为自动 matched；精确历史 provenance 仅表示身份归属，不替代当前生产 readiness。
- 导入与 validate 只冻结并校验 parameter Schema、固定素材槽或注册动态参考拓扑、模型和唯一主输出；具体参数与 Asset 只在工作流版本验收页的 per-attempt fixture，或素材生成/ProductionRun 的运行输入中选择并分别冻结。
- 每个素材 Load 节点只形成一个 optional `0..1` 或 required `1..1` 槽；没有把多个 Load 节点、同槽数组或动态拓扑混为一谈。无素材槽 workflow 已验证可导入和 validate。
- 每个真实 smoke 的 attempt、fixture hash、Asset binding facts、compiled graph、真实提交、输出字节、时长及 OOM/uncertain 人工核对均有目标环境证据；attempt API 不公开原始 fixture 参数值。
- ComfyUI 导出返回 `download_import`，不声称 direct-open；validated/published/retired 版本使用精确 `validated_deployment`，draft 使用明确选择且兼容的 active deployment。
- deployment/provider/model/evidence/hash 漂移会在访问 ComfyUI 前阻断，导出不创建 smoke 或生产任务。

## 5. 多模态素材库

- 图片、视频、音频和剧本的本地上传均已在目标环境用真实允许格式验证；扩展名、MIME、媒体解码、大小、时长和租户配额错误会显式失败。
- 异步 API 素材生成选项只显示 active/published 且具有新鲜真实 operation smoke 的 Deployment；图片、视频、音频分别验证 `image.text_to_image`、`video.text_to_video`、`audio.text_to_audio`，文本提示词/剧本 Deployment 也有匹配能力证据。ComfyUI 方案则以 current/published Workflow 的版本声明 operation 为准，例如参考生视频可为 `video.reference_to_video`，并具有精确 Live validation 与真实 Workflow smoke evidence。
- ComfyUI 素材表单已按 `workflow_input_schema` 验证全部标量类型、多个固定图片/视频/音频槽、单/多 `CONDITIONING_MODE` 以及动态多图、多视频、音频用途和配对；创建体只通过 `workflow_inputs` 传 Asset ID 与业务意图。服务端复用短剧的授权、semantic compiler、staging 与 conditioning/preprocessing，并在越权、文件漂移或 required 输入缺失时于提交前阻断；素材任务本身不要求 ProductionRecipe。
- 提示词候选在独立任务中返回并保持可编辑，未采用前不会自动提交素材生成或产生第二次外部费用。
- 外部素材任务的授权、预算、可确定 pricing、幂等键、取消、明确失败重试和 `uncertain` 禁止自动重放已演练；staff 的四类 `resolve-uncertain` 结论及其幂等重放、request id 摘要一致性、不可变证据、跨 revision 费用冲突重新阻断，以及取消后晚到成功只记费用不入库均有测试证据。
- text-generation 与 async-media worker 可在页面刷新/进程恢复后继续素材任务；成功只在受限下载、类型/大小/hash/媒体校验和受管 Asset 物化完成后显示。
- `preview_url` 与 `download_url` 过期后可通过重新读取 Asset 详情刷新；下载使用附件响应且跨租户签名无效。
- 生成人物/场景/道具图片以及参考视频、参考音频可在现有短剧工作台选择。剧本素材导入项目只创建关联来源的 preview revision，并经过原有人工确认门。

## 6. ProductionRun

- 新的短剧生产只从 ProductionRun 创建；独立素材生成不能创建或推进短剧运行。
- 新建页的空项目、导入脚本和 AI 创建三种入口均已验证；空项目不伪造 ProductionRun，导入/AI 后续只走正式 run。
- 导入脚本覆盖 `ScriptSource` 服务端 preview、明确 confirm 和不可变 revision。规范文本的 `script.structure` no-submission 候选与非规范文本的 AI 结构化 preview 都会等待 required `script_review`，原 confirmed revision 不被提前覆盖。
- AI script → review → deterministic Bible → review 的人工 gate 可见。
- route、cost、workflow、recipe 和 config snapshot 可追踪。
- 媒体运行的 `uniform` 与 `per_shot` workflow selection 都已验证完整覆盖、Shot 归属、route 兼容性和冻结 hash；AI/导入时尚无 Shot 只接受 `uniform`。
- 外部调用必须有明确授权、币种、可确定费率和预算上限；缺失或超额在提交前阻断，run/stage/attempt/cost/profile/provenance 审计链可对账且不含凭据/请求响应正文。
- published recipe 已通过完整 content → storyboard → prompt → media → selection → assembly → delivery 闭包校验。
- 不确定外部响应可通过 run action 显式解决。
- Tasks/Deferred requests/Assemblies/Releases 完全只读；创建、重试、取消与有序 fallback 只在所属 ProductionRun。

## 7. 媒体与交付

- async REST T2I/T2V/T2A 在真实 provider 上完成 submit、poll 和 archive。
- I2V/reference image 在无 provider upload contract 时明确 blocked。
- 远端结果已归档为 managed artifact，类型、大小与 provenance 可验证。
- 候选参数差异只使用归档时从冻结事实白名单生成的 `comparison_parameters`，不使用 manifest 身份/hash。
- 每集 episode master 可播放且 source items 完整。
- ReleaseBundle verify 通过并包含所有逐集母版。
- 不以整季单条长视频作为交付验收项。

## 8. 运行与恢复

- generation、text-generation、async-media、assembly、platform-maintenance worker 均健康。
- text/media/assembly worker 终态事实提交后会幂等 auto-advance；普通自动阶段继续，required review、cost、route 和 selection gate 准确暂停。
- auto-advance 失败时已持久 worker 终态不被改写，操作员可从 run/stage/attempt 事实恢复。
- worker lease 与 ProductionRun retry/cancel/uncertain recovery 已演练。
- 数据库和受管媒体完成一致性备份。
- 恢复演练在隔离环境完成。
- 监控能区分 failed、blocked、waiting review、uncertain 和 partial 状态。

## 9. 结论

只有独立素材生成和短剧生产的真实 provider/GPU E2E、managed artifact、episode master、ReleaseBundle 和恢复演练都有目标环境证据时，才可标记生产就绪。仓库当前的契约/模拟测试不能替代这些证据；本次开发未调用真实工作流，必须由人工测试补齐目标环境证据。
