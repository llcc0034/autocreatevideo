# Production Readiness

本清单用于目标环境验收，各条是待核对的通过条件，不是本仓库已全部完成的声明。catalog、单元测试或 mock 通过不等于真实生产就绪。当前未完事项、验收缺口与延期决定统一在[最新待办复核清单](optimization/backlog-review-2026-09-28.md)维护，日期化实施证据见[执行计划](optimization/execution-instance-unification-plan.md)。

当前项目按开发阶段、可用性优先和业务模块独立验收推进；本清单中的生产级容量、完整灾备、目标数据库和外部分发要求不是M1–M9的默认前置门禁。只验证本次明确纳入的目标范围，不因本清单存在而恢复延期工作或增加审批。

2026-09-21文档审核：后续开发与服务切换已有记录，但9月20日部分资源已重建且未复制旧Smoke。验收必须注明环境、源码/配置版本、对象、日期、结果和证据；不能复用旧版本成功代替当前通过。本轮未执行以下真实验收，模型调用、费用、配置发布和最终封存仍需明确授权。

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
- `value_schema.resource_ref` 推荐精确执行实例版本，服务端目录标明不可选原因，保存变更时核验已发布合同；空值表示未推荐，不自动选默认实例。任务只使用明确选中的版本，保存推荐不要求先运行模型，也不能替代执行准入。

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
- 各业务用途有明确的执行实例版本；策略组装方式固定一个精确已发布Profile，人工完整通道与无提示词方式按合同分别核对，不通过默认resolver或Binding挑选策略。
- 每个 Profile 的 model family/model version、operation、mode、source selector、channel、顺序、缺省处理和长度合同已在 deterministic preview 核对。
- 多策略通过不同执行实例配置区分用途；实例出向引用明确且版本不可变，底层部署、工作流和策略可以共享。Preparation冻结精确组合与本次输入，缺失、不兼容、未发布引用及摘要漂移均有明确失败，不按priority隐式替换。
- seed 产生的 `needs_review` draft 不被当作生产能力，生产路径不会回退到通用提示词模板。

## 4. Workflow

- bootstrap `needs_review` 条目已逐个审阅。
- semantic input/output bindings 已解析。
- graph validation 通过。
- real smoke 包含实际 executor submission 与结果证据。
- workflow 已显式 publish。
- 节点角色、端口与拓扑由精确Projector/Workflow合同配置，不依赖主编排中的模型专用节点ID。
- saved-workflow草稿通过fresh discover，在所属成功来源携带当前scope version/hash重新preview/import；版本由服务端分配，不固定要求v3。部分失败来源不作为空目录，也不复用旧review或旧validation/smoke evidence。
- 新发现文件只有在唯一受支持主输出、唯一模态兼容 published 模型版本、同来源 active Deployment、实时 object_info、非空模型依赖、每项依赖的最新未过期 AVAILABLE size/hash 证明及无 blocker 同时成立时才标为自动 matched；精确历史 provenance 仅表示身份归属，不替代当前生产 readiness。
- 导入与 validate 只冻结并校验 parameter Schema、固定素材槽或注册动态参考拓扑、模型和唯一主输出；具体参数与 Asset 只在工作流版本验收页的 per-attempt fixture，或素材生成/ProductionRun 的运行输入中选择并分别冻结。
- 每个素材 Load 节点只形成一个 optional `0..1` 或 required `1..1` 槽；没有把多个 Load 节点、同槽数组或动态拓扑混为一谈。无素材槽 workflow 已验证可导入和 validate。
- 每个真实 smoke 的 attempt、fixture hash、Asset binding facts、compiled graph、真实提交、输出字节、时长及 OOM/uncertain 人工核对均有目标环境证据；attempt API 不公开原始 fixture 参数值。
- ComfyUI 导出返回 `download_import`，不声称 direct-open；validated/published/retired 版本使用精确 `validated_deployment`，draft 使用明确选择且兼容的 active deployment。
- deployment/provider/model/evidence/hash 漂移会在访问 ComfyUI 前阻断，导出不创建 smoke 或生产任务。

## 5. 多模态素材库

- 图片、视频、音频和剧本的本地上传均已在目标环境用真实允许格式验证；扩展名、MIME、媒体解码、大小、时长和租户配额错误会显式失败。
- 素材生成只选择精确执行实例版本，目录按真实输出合同区分文本、图片、视频、音频，并显示配置/证据及不可选原因。底层资源Smoke与组合Smoke分别核验，其他能力通过不能替代本组合；TTL和容量按有效配置告警或阻断，不把未测规格标成通过。
- 表单按精确实例输入合同展示素材逻辑键、类型、数量、用途、配对与运行参数；请求使用`inputs`、`parameters`、`manual_channels`及字段值等当前合同字段，不提交旧`workflow_inputs`或裸Deployment/Workflow。服务端与生产共用准备、授权、编译、暂存及预处理，素材任务不要求ProductionRecipe。
- 提示词候选在独立任务中返回并保持可编辑，未采用前不会自动提交素材生成或产生第二次外部费用。
- 外部素材任务的授权、预算、可确定 pricing、幂等键、取消、明确失败重试和 `uncertain` 禁止自动重放已演练；staff 的四类 `resolve-uncertain` 结论及其幂等重放、request id 摘要一致性、不可变证据、跨 revision 费用冲突重新阻断，以及取消后晚到成功只记费用不入库均有测试证据。
- text-generation 与 async-media worker 可在页面刷新/进程恢复后继续素材任务；成功只在受限下载、类型/大小/hash/媒体校验和受管 Asset 物化完成后显示。
- `preview_url` 与 `download_url` 过期后可通过重新读取 Asset 详情刷新；下载使用附件响应且跨租户签名无效。
- 生成人物/场景/道具图片以及参考视频、参考音频可在现有短剧工作台选择。剧本素材导入项目只创建关联来源的 preview revision，并经过原有人工确认门。

## 6. ProductionRun

- 新的短剧生产只从 ProductionRun 创建；独立素材生成不能创建或推进短剧运行。
- 新建页的制作方式（配方/人工）与剧本来源（稍后填写/导入）组合均已验证；人工创建仅保存项目和人工运行，AI生成在原工作台明确发起，不创建隐藏配方或自动调用模型。
- 导入脚本覆盖`ScriptSource`预览、明确确认与不可变修订。规范确认稿可以沿`script_input/confirmed_revision`确定性拆稿；显式配置`script_structure/ai`时仍走所选实例和真实候选审核，不以内容看似规范跳过调用，也不制造no-submission候选。
- AI script → review → deterministic Bible → review 的人工 gate 可见。
- route、cost、workflow、recipe 和 config snapshot 可追踪。
- recipe明确冻结阶段路线、执行实例、逐镜来源及参数策略；manual在具体动作中选择实例和输入，不另选`uniform/per_shot workflow_selection`。自动生成的Bible、镜头、说话人与素材按已审核来源及明确映射解析，不全局广播参考素材。
- 已有分镜时自动拆稿在创建前拒绝覆盖；剧本45–50秒转换为该镜头5秒时长，逐镜生成读取本镜头时间与冻结参数，不把全剧起止时间传成工作流截取区间。
- 无配方制作的recipe为空，支持文本与媒体输入、明确候选采用、来源变化后的复核/主动重做、逐集装配和封存；不暗中创建配方，不改写原准备或自动重跑。
- 预算提醒、调用次数与平台文本输出额度只由系统配置管理；超额保留任务等待人工确认，确认绑定精确请求/修订/暴露，不自动派发。实际超支保留成功产物，冻结请求不被调额改写；输出截断按支持的文本审核路径保留原响应，不静默续写或重跑。外发授权、币种/计价、未知费用、权限、安全和真实能力仍独立核验；审计链不含凭据或原始正文。
- published recipe 已通过完整 content → storyboard → prompt → media → selection → assembly → delivery 闭包校验。
- 不确定外部响应可通过 run action 显式解决。
- Tasks/Assemblies/Releases作为结果与证据入口；创建、重试、取消与有序fallback只在所属ProductionRun，旧Deferred requests入口已移除。

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

只有当前受验范围的独立素材/短剧真实provider或GPU链路、受管产物、逐集母版、ReleaseBundle和恢复演练均有目标环境证据，才可标记该范围生产就绪。历史部分实测、契约/模拟测试或当前配置已发布都不能替代；尚未覆盖的模型、音频或协议明确列为未验收，不概括为全平台通过。
