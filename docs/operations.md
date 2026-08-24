# Operations

本文是当前代码的运行与运维入口。服务管理使用仓库脚本，Schema 使用 Django migrations，机器 API 契约使用根 `openapi.yaml`；不要维护第二套命令或副本。

## 1. 前置条件

目标机器需要能够运行项目内安装/解析的 Python、Node.js、FFmpeg 和 PostgreSQL。ComfyUI 或 async REST provider 是可选执行器，但未完成 provider、deployment、workflow 和 smoke 配置时不能承担生产任务。

运行期业务配置保存在数据库的版本化 ConfigRevision 中。provider 密钥等敏感值由超级管理员写入 SecretBinding，材料以 AES-256-GCM 密文和不可变 SecretVersion 保存；API、页面、日志与审计正文只展示 configured/usable 状态。数据库解密密钥环必须放在部署 secret manager 或受保护环境文件中，不能写入数据库，也不能与数据库备份打包在一起。

## 2. 首次安装

在仓库根目录运行：

```powershell
.\scripts\setup.ps1
```

setup 安装/修复项目运行时、安装后端和前端依赖、生成独立数据库加密密钥环、执行数据库迁移、创建首个超级管理员并导入受版本管理的资产库。首次创建时终端只显示一次随机临时密码；该账号首次登录必须先改密。workflow catalog 不由 seed 数据或启动流程覆盖。

需要本地演示项目时必须显式选择：

```powershell
.\scripts\setup.ps1 -WithDemoProject
```

或：

```powershell
Push-Location backend
.venv\Scripts\python.exe manage.py seed_data --with-demo-project
Pop-Location
```

不要在生产启动流程中运行 demo seed。可选本地翻译模型也必须通过 setup 参数或专用安装脚本显式安装；这与生产媒体模型部署是不同职责。

## 3. Migration 流程

每次部署先在目标版本和目标数据库上审阅：

```powershell
Push-Location backend
.venv\Scripts\python.exe manage.py migrate --plan
.venv\Scripts\python.exe manage.py migrate --check
Pop-Location
```

`migrate --check` 表明有待应用 migration 时，应先备份并按变更窗口执行：

```powershell
Push-Location backend
.venv\Scripts\python.exe manage.py migrate
.venv\Scripts\python.exe manage.py migrate --check
Pop-Location
```

不得用 ORM 读侧兼容、手工建表或静默 fallback 代替 migration。本文不记录某一开发数据库的临时应用状态。

### 旧 `.env` 业务配置迁移

升级已有安装时，先完成数据库与媒体备份，并确保独立数据库加密密钥环已安全保管。随后在 backend 目录先预览，不写库且不回显秘密：

```powershell
.venv\Scripts\python.exe manage.py import_legacy_runtime_config
```

预览只用于发现键名与计划范围；它不会发网络请求，也不能证明 endpoint、provider、现有 deployment 或密钥可用，且 `safe_to_remove` 固定为 `false`。确认 `config_keys`、`retired_keys` 与 `review_required` 后，再执行原子导入：

```powershell
.venv\Scripts\python.exe manage.py import_legacy_runtime_config --apply
```

只有确认值与当前系统策略一致时，才执行 `--apply --publish-config` 激活 system revision；单独提供 `--publish-config` 仍是 dry-run。`--skip-deployments` 不代表跳过秘密导入，如需纯配置迁移必须同时使用 `--skip-secrets`。

没有同 profile 的活动 Comfy deployment 时，导入器会把原有 endpoint 建成 active/default 以保持既有执行路径，但这不等于 ready，仍需 probe、workflow validate 和 real smoke；与现有活动合同冲突时只建 disabled review candidate。DeepSeek/model 事实始终保持 draft/disabled，必须在页面中审核、发布、绑定和真实 smoke。完成解析、SecretBinding 可解密、deployment 合同与新 revision 验证后，才从实际 `.env` 删除已迁移或明确退役的业务键。数据库连接、Django secret、数据库加密密钥环、主机路径、网络 allowlist 与进程硬上限继续由部署层管理。

## 4. 启动与状态

日常启动/重启：

```powershell
.\scripts\restart.ps1
```

静态与契约检查：

```powershell
.\scripts\check.ps1
```

`restart.ps1` 是本地正式启动/重启入口。它会设置新的 boot identity、执行迁移（除非明确使用维护用跳过参数）、启动 API/前端与 generation、text-generation、async-media、assembly、platform-maintenance worker，并按配置处理 ComfyUI。boot identity 生成不等于 restart-required 配置已激活。仓库当前没有独立的本地停止脚本；停止进程应使用部署环境的服务管理器，不得引用不存在的命令。

不要为 demo seed、workflow 覆盖或旧生产入口添加 restart 分支。

## 5. 五层配置与 boot activation

有效配置按以下顺序解析：

1. model version default；
2. workflow version default；
3. active system revision；
4. active project revision；
5. active run override。

后层覆盖前层。ProductionRun 创建时冻结最终值、来源、revision 与 hash。环境变量只保留数据库连接、Django 启动秘密、数据库加密密钥环、主机路径、进程硬上限和网络安全边界等数据库启动前职责，不作为业务配置的隐式回退层。

运维流程：

1. 创建并校验 config revision。
2. publish revision。
3. 对无需重启的定义按 activation policy 生效。
4. restart-required revision 保持 pending。
5. 启动新 boot。
6. 在新 boot 上显式调用 activation。
7. 用 resolved config 和审计记录核对有效值。
8. 新建 ProductionRun，确认 snapshot 固化预期 revision。

不要把 pending 显示为 active，也不要在 restart 脚本中静默激活。回滚同样通过新 revision/activation 完成，保留历史审计。

### 类型化配置与资源依赖

Settings 的默认值页面按 `ConfigDefinition.value_type` 和 `value_schema` 渲染布尔、枚举、数值、文本、资源下拉和受限 JSON 编辑器，并展示描述、影响、默认值、单位、范围、必填、人工复核和重启影响。普通用户不需要用 JSON 手工填写已有类型化字段；高级 JSON 只用于尚未拆分的封闭结构合同。

配置值需要引用平台资源时，在 `value_schema.resource_ref` 中声明 resource、capability、`allow_empty` 和 `require_ready`。页面通过 `GET /api/v1/config-definitions/{id}/resource-options/` 读取服务端审核的选项，并显示不可选原因；不提供自由输入 UUID 的旁路。当前已登记的资源引用是 `prompt.normalize` 的 model deployment。非空值发布时，服务端重新验证 deployment 存在、模型能力、可路由状态和当前有效 operation smoke；不依赖浏览器旧选项。历史 revision 中的空值显式解析为 `unconfigured`，会阻断依赖该路由的新生产，不会回退到环境变量、默认 deployment 或旧缓存。

## 6. Provider、deployment 与 workflow 上线

### Provider

- 建立 provider profile。
- 由超级管理员创建或轮换 SecretBinding，并把受管凭据绑定到 Provider 或 Deployment；平台管理员不能替换秘密绑定。
- 在目标环境验证授权、配额和 endpoint。
- 只展示 configured 与 owner lifecycle；configured 不等于可调用，停用的 Provider、Deployment 或 SecretBinding 都必须显示 blocked。

### Model profile、artifact 与 deployment

- 按 Provider → draft model profile/声明 artifact → publish → 启用并 probe host 至 active → deployment → 再 probe host 形成 availability → artifact verify → deployment probe/smoke 的顺序登记；不能先发布再补文件，也不能在没有主机事实时把 declared 文件伪装为 available。
- 审核来源、hash、许可和用途。
- 先把目标 host 探测为 active，再建立 active deployment；建立后再次探测，形成该 deployment 与文件的最新 availability 快照。
- 日常 Settings/API 探测只负责创建持久 `RuntimeHostProbeAttempt`（HTTP `202`）；platform-maintenance worker 领取任务、持续心跳并写入终态。页面刷新后从 attempt collection 恢复，不得因 12 秒客户端超时把仍在后台执行的 SHA 误报为失败。
- running 探测的取消先持久化为 `cancel_requested`，在 8 MiB 文件分块检查点安全停止；worker 崩溃后由陈旧 claim 恢复。若快照已经与 attempt 原子关联，恢复流程收敛为 succeeded，不重复扫描大文件。
- `manage.py probe_runtime_hosts --due` 是人工维护/故障诊断的同步命令；正常运营只使用 Settings/API 异步入口，不与 active attempt 并发执行。
- 对本地模型文件使用最新、未过期的 RuntimeHost snapshot 执行 artifact verify；远端文本模型没有本地 artifact 时明确标为 N/A，不伪造文件。
- 执行 probe 和 smoke-test。文本部署分别选择 `script.generate`、`script.structure` 与 `prompt.normalize`；每次调用都先返回 HTTP `202` 的持久 `ModelDeploymentSmokeAttempt`，Settings 从 attempt collection 恢复并轮询终态，不受前端短超时影响。worker 在落库后只跨越一次提交边界：`script.generate` 提交最小一集一镜请求，另外两项使用各自严格合同；公开证据只保留 hash/费用/错误分类。外部部署未配置确定 pricing 时不会排队提交。`queued` 可取消，进入 `submitting` 后若状态不确定必须由管理员核对 Provider 控制台/账单并追加消歧证据，系统不会自动重放付费请求。
- 未成功前保持不可路由。

draft profile/artifact 可以编辑或删除；published profile 不能覆盖，只能先停用引用它的 deployment、清理 current workflow 约束后 retire，变更通过下一版本完成。Deployment 必须先 disabled 才能请求删除；已有 probe/smoke、host availability 或生产引用时保留历史并返回冲突。

平台不会自动下载模型。catalog 中存在 H3、SDXL 或其他系列只表示可配置/待许可审阅，不是已安装证明。

### PromptProfile

PromptProfile 是提示词可配置语义装配的唯一主路径，应在 workflow 和 ProductionRecipe 之前完成治理：

1. 在字段目录中审阅 key、类型、分类、描述、影响、默认值、枚举和必填性。
2. 为 model family/model version、operation 和 mode 建立 draft Profile，按白名单 source selector 选择字段，并配置 channel、顺序、标签、分隔符、缺省处理和长度。
3. 使用确定性 preview 对照装配结果与 trace；preview 不调用模型，不代替 deployment/workflow smoke。
4. 审核官方参考、合同 hash 与系统指令来源后发布。只允许管理员默认值进入 `system_instruction`，项目或请求文本不得提升为系统指令。
5. 创建 active binding；可以为同一模型配多个策略，但实际 model version/mode 与 priority 必须解析为唯一最佳结果。

`script.generate`、`script.structure`、`prompt.normalize`、`image.generate` 和每个 `video.generate` mode 都要单独核对。seed 生成的 `needs_review` draft 不是生产能力；缺少 binding、同优先级多解、引用未发布 Profile 或合同漂移会在配方发布、运行创建或阶段提交时显式失败，不会回退到通用模板。变更已发布策略时新建版本并重新绑定；历史运行继续读冻结的 Profile/binding/checksum。

### Workflow

bootstrap workflow 初始为 inactive draft、`needs_review`。上线必须：

1. 导入/编辑图；
2. 绑定 semantic inputs/outputs；
3. validate；
4. 在真实 executor 上 smoke-test；
5. 审阅 evidence；
6. 显式 publish。

刷新页面后仍应从详情 API 看到 validated deployment、validation/smoke evidence 与合同 hash。已发布工作流不可原地更新；需要调整时导入新版本，完成 validate/smoke 后发布替代版，再 retire 旧版。不要依赖固定 Comfy node ID，也不要由 seed 或 restart 覆盖 catalog。

需要在 ComfyUI 中查看已保存工作流时，使用 Settings 的“下载 ComfyUI 导入文件”。系统固定返回 `open_mode=download_import`，不声称支持 direct-open。validated、published 和 retired 版本只能使用记录证据的精确 `validated_deployment`；没有该证据的 draft 必须显式选择兼容 active ComfyUI deployment。部署停用、provider/model 绑定不一致、validation target hash 漂移或请求伪造其他 deployment 均在请求 ComfyUI 之前阻断。导出只读 `object_info` 并生成 UI canvas JSON，不 submit、poll 或创建 smoke/生产任务。

### ProductionRecipe

draft 配方可查看完整 stage/route JSON、编辑和删除；发布前在确认窗口核对 script mode、deployment/workflow UUID、阶段顺序与交付闭包。published 配方不可覆盖，只能创建下一版本或 retire；历史 ProductionRun 继续引用原不可变版本。

### ConfigDefinition 与 ConfigRevision

设置页只提交人工修改的 dirty keys。恢复 model/workflow/default 的继承关系使用 `unset_keys`；禁止把 resolved 全量值写成 system override。ConfigDefinition 已被 revision 使用后只允许安全元数据与启停变更，结构合同不可重写。

发布 revision 前核对 old source → new source、restart impact 与秘密绑定变化。旧基线草稿、第二个 pending 和无权变更秘密的请求会被服务端显式拒绝；失败时保留当前 active revision，不得在前端伪装成功。

## 7. RuntimeHost probe 与 recommend

注册 host 后执行 probe，并审阅：

- CPU、内存、GPU/加速器与可用显存；
- 已安装运行时和执行器；
- deployment 与 workflow 兼容性；
- 快照时间和错误。

recommend 依据当前事实生成 draft。接受 recommendation 只创建 reviewable recipe/config draft。它不会下载模型、自动部署、publish workflow 或 activate 配置。

提交 recommendation decision 时，服务端会重新验证 host snapshot 的有效期、catalog/config hash 和候选当前兼容性。收到 `recommendation_stale` 时重新 probe/recommend，禁止沿用旧草案或仅由前端放行。

目前没有跨节点/多 GPU scheduler。可以启动多个 worker，但 worker 的并发是进程局部参数；运维必须按目标机器容量设置并观察，不得将它描述为全局 GPU 调度。

## 8. ProductionRun 操作

新建连续剧页面提供三种内容入口：

- 空项目：只建项目，不伪造脚本或生产成功。
- 导入脚本：粘贴或上传后先检查 `ScriptSource` 服务端预览，再显式确认 revision。规范脚本的 `script.structure` 产生确定性 no-submission 审核候选；非规范原文由已审核 route 转为覆盖全文的 preview revision。两者都在 required `script_review` 人工确认后才取代运行中的脚本事实。
- AI 创建：填写需求、集数、每集镜数和时长，同时选择已发布配方、外发授权、币种和预算。项目创建后用正式 ProductionRun 生成脚本，不经由瞬时草稿旁路。

只有需要执行的内容会创建 ProductionRun，而所有新生产一律从 ProductionRun 创建。运行前检查：

- 项目和内容事实正确；
- confirmed script 可用，或选择 AI script；
- AI script 和确定性 Bible 均完成人工审核；
- workflow 已 validate、real smoke、publish；
- deployment/host 可用；
- resolved config 与 run snapshot 可追踪；
- route 与成本币种/授权明确；
- 受管存储可写。

有媒体阶段的运行必须显式选择 workflow route。`uniform` 把同一 route 应用到全部选中镜头；`per_shot` 必须为每个镜头选择 route，页面可以先将一条 route 批量应用到整集再逐镜修改。两种模式都由服务端检查完整覆盖、Shot 归属、workflow/deployment/model/mode/PromptProfile 兼容性并冻结 hash。AI/导入入口尚无物化 Shot 时只接受 `uniform`；无媒体阶段的纯脚本配方不提交 workflow selection。

所选 published recipe 必须已经通过完整 content → storyboard → prompt → media → selection → assembly → delivery 闭包校验；不要以缺阶段的“部分配方”启动正式 run。

运行中只在 ProductionRun 执行 approve、route selection、cost authorization、continue、artifact selection、retry、resolve-uncertain、cancel 或有序 fallback。Tasks、Deferred requests、Assemblies、ReleaseBundles 完全只读，没有任务级写例外。

选择“自动生产”不会跳过人工门。各 worker 在文本、媒体或装配终态事实提交后幂等触发 auto-advance，状态机自动继续普通阶段；遇到 required review、路由选择、成本授权或候选选择时保持 waiting/blocked，等操作员通过正式 run action 决策。后置推进异常不会修改已持久的 worker 事实。

每次外部调用都必须在 run snapshot 中有显式授权、币种和预算上限。未授权、pricing 不可确定、预算缺失或超额会在提交前阻断。排查时通过 ProductionRun、StageRun、GenerationAttempt、route/cost event、PromptProfile 冻结引用和 artifact provenance 对账；不将密钥、完整提示词、远端响应或签名 URL 复制到日志/审计正文。

## 9. async REST 与上传边界

T2I/T2V 的目标环境验收必须覆盖 submit、poll、结果获取、managed archive 与 provenance。仅请求校验或 mock 不能作为 real smoke。

I2V/reference image 需要 provider-specific upload contract。当前标准执行器没有该契约；遇到 `provider_upload_contract_missing` 应保持 blocked，登记 provider 上传/引用格式后再开发和验证。禁止丢弃参考图、发送纯文本任务或回退旧执行器并显示成功。

## 10. 受管产物与逐集交付

远端输出进入装配前必须：

- 通过地址、大小和媒体类型限制；
- 完成下载/读取；
- 写入 managed storage；
- 建立 generated artifact、attempt 和 provenance；
- 完成可播放性/结构校验。

归档时还会从冻结 task/attempt/stage、route、workflow 和 configuration 事实白名单固化 `comparison_parameters`。操作员比较候选参数时只读该字段；manifest 的任务 ID、对象键、输出索引和 hash 仅用于身份/provenance。

装配每次生成一个 episode master，并记录 source items。ReleaseBundle 汇总逐集 master 并执行 verify。系统不生成整季单条长视频；缺一集、来源不完整或 artifact 不可验证都必须阻断交付。

## 11. 健康检查与日志

基础健康检查：

- `GET /api/v1/health/`
- PostgreSQL 可达，数据库任务 claim/lease 可读写；
- 所有 worker 有近期 heartbeat/lease 活动；
- managed storage 可读写；
- provider/deployment/workflow readiness 为当前状态。

运行日志位于项目运行目录配置的位置。排障时保留 ProductionRun ID、stage、attempt、external request ID、artifact ID、worker 和时间戳；先遮蔽 credential、签名 URL 和敏感地址。

常见状态：

- waiting review：完成人工审核；
- blocked：按错误码修复依赖；
- uncertain：核对 provider 后用 run 的 resolve-uncertain；
- failed/retryable：在 ProductionRun 恢复；
- Deferred 历史：只读核对所属 ProductionRun，并在该 run 上执行恢复或取消；
- pending restart activation：新 boot 后显式 activate；
- workflow needs_review：绑定、validate、real smoke、publish。

## 12. 备份与恢复

备份必须在一致时间点覆盖：

- PostgreSQL；
- managed media/artifact storage；
- 部署所需的非敏感配置和版本清单；
- workflow/model provenance 与许可记录。

恢复时先恢复数据库和媒体，再执行 `migrate --plan`、`migrate --check`，核对 artifact 引用、逐集 master 与 ReleaseBundle。恢复演练应在隔离环境定期进行；只备份数据库而不备份受管媒体不是可恢复状态。详细步骤见 [backup-restore.md](backup-restore.md)。

## 13. 发布前验证

```powershell
.\scripts\check.ps1

Push-Location frontend
npm run test:manual
npm run typecheck
npm run build
Pop-Location
```

随后在目标环境完成真实 provider/GPU E2E：授权、deployment probe、workflow real smoke、T2I/T2V submit/poll/archive、episode master、ReleaseBundle verify 和恢复演练。仓库测试通过只能证明当前代码契约，不足以证明目标基础设施生产就绪。
