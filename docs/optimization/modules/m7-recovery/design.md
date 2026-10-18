# 11. M7 媒体生成与任务恢复

> 原设计与任务编号保持；此模块的勾选仅在本文件维护，不复制到索引。

[总索引](../../README.md) · [当前续接](../../current.md) · [按日期查证据](../../records/README.md)

<!-- source-section e1700b71b4ae -->
<a id="m7"></a>

<a id="11-m7-媒体生成与任务恢复"></a>

## 11. M7 媒体生成与任务恢复

<a id="111-背景需求与产出"></a>

### 11.1 背景、需求与产出

用户需要知道为什么没有视频、在哪一步失败、如何只处理失败项。辅助提示词成功仅说明输入字段生成完成。人工无配方主调用也必须有正式恢复入口；独立素材任务的失败实际返回不能被遗漏。

输入：已准备字段/素材、最终提示词/参数、所选执行实例、人工或配方执行意图。输出：原任务体系的主调用、实际返回、可用产物或失败信息、可解释的重试/继续结果。成功主调用不因阶段重试再次发出。

<a id="112-实现方式"></a>

### 11.2 实现方式

复用`production_runs.py`、`production_manual_commands.py`、`production_manual_preparations.py`、原准备/队列/attempt及`StageExecutionPreparations`/`ExecutionPreparationWorkbench`。阶段动作枚举实际失败/未提交/状态不确定单元：已成功跳过；准备问题修好后继续原单元；明确失败可创建新Attempt；远端状态未知先沿原查询恢复，不盲目重发。

任务页显示“输入准备→辅助调用→主调用→输出解析/落库→选择”的实际阶段、原因和相关操作。取消是终态；普通查看/刷新不发新模型请求。质量提醒不当作传输/结构错误，原始实际响应有则显示，缺失就诚实注明。

<a id="113-细任务与逐项验收"></a>

### 11.3 细任务与逐项验收

- [x] **M7-01 媒体输入与实际请求贯通**（P0；依赖：M1-08, M5-04, M6-04）。实现/产出：配方/人工主调用引用同一字段/素材/编译结果和确切实例。验收：页面所示采用字段、素材版本/顺序与提交请求一致；改配置不会暗改已提交请求。完成：2026-10-04第279批；278正式writer/冻结/驱动隔离证据与279原组件组合及浏览器核验共同完成本细项，不代表M7模块或真实模型验收。
  - **第279批页面对应验收完成**：[9项原组件测试](../../../../frontend/tests/mediaRequestCorrespondence.test.ts)覆盖配方/人工×Comfy/REST、采用A而非待审B、确切实例/Profile/冻结参数、同名旧版本与倒序槽位、预览到冻结读取、跨Run/准备/镜头迟到响应、读取失败及身份/SHA不符拒绝显示和只读恢复。Poincare真实隔离树9/9，主线同SHA集成后与OPTIONS专项合计10/10通过23.805s；原组件[隔离浏览器页](../../../../frontend/tests/fixtures/media-request-correspondence.html)四种入口、全文/参考顺序、身份错误与重读恢复、390px布局及控制台通过。与278传输体/冻结文件的后继配置不改原请求证据共同闭合细项；浏览器仅合成回执，不算再次真实发送或完整远端轮询回收。主项66/91、G6/6，M7后续及模块交付仍按依赖继续。
  - **第278批隔离双入口请求组合（部分完成）**：新增[请求贯通专项](../../../../backend/studio/tests/test_m7_media_request_contract.py)，人工正式POST与配方原准备writer复用真实finalization/冻结文件/只读页面API/提交驱动，仅在网络边界截获传输。Poincare隔离树最终新进程8/8通过285.561s，四正向为人工/配方×Comfy/REST，四拒绝为manifest损坏、参考文件变化、错属/重复上传回执和REST缺远端ID后不重发。Comfy核对同名旧版本与倒序槽位；REST当前只支持标量映射，不凭空添加参考传输。采用A不发送待审B，后继配置不改原请求/SHA。主线已审查并以相同SHA集成新测试，代表性人工Comfy/配方REST两项复验2/2通过61.522s；完整证据见18.3。浏览器渲染、真实模型和完整轮询回收未覆盖，历史准入仅为隔离夹具；不勾选整项。
- [x] **M7-02 状态投影与取消文案**（P0；依赖：M1-08）。实现/产出：统一解释准备/辅助/主调用/产物状态，修复取消后继续等待等矛盾提示。验收：辅助成功主任务失败、准备阻断未调用、已取消等组合均真实，查看不产生任务。
  - 2026-09-29生产准备段：列表/分组/详情共用[只读状态说明](../../../../backend/studio/services/execution_preparation_presentation.py)，分开解释准备、原调用和本次主调用的产物/选择记录；最终合同为顶层`outcome`。不将派发、辅助成功或停止准备冒充媒体成功/远端终态。产物计数只统计当前页最新主调用，批量窄列查询，不读取合同/文件、不补结果；计数不是文件可用性证明。原始状态及错误仍独立显示。
  - 修复[原配方媒体阶段](../../../../backend/studio/services/production_recipe_media_stages.py)把cancelled准备计为等待输入的问题：发现已停止准备先返回`execution_preparations_cancelled`，不推进其余准备或伪造取消成功；没有新重试/门禁体系。公共[列表](../../../../frontend/src/components/StageExecutionPreparations.vue)和[准备工作台](../../../../frontend/src/components/ExecutionPreparationWorkbench.vue)只消费服务端说明，取消不显示继续按钮。API/OpenAPI与前端严格适配同步，无旧字段兼容。
  - 验证：`test_preparation_outcome`两项（含13组状态）及ProductionAuxiliaryTests的新增组合/原分页只读、RecipeMediaPreparationTests的取消/原等待共**7项通过（47.852s）**；`executionPreparation.test.ts` **34项通过（21.921s）**，含刷新不写入和缺失/非法状态说明拒绝；vue-tsc和Vite构建通过（13.23s，既有大chunk提示仍在）。后端首轮新增索引夹具因缺冻结请求标记触发约束，补全隔离夹具后重跑通过，未放宽模型约束。
  - 首段浏览器使用[显式隔离夹具](../../../../frontend/tests/fixtures/preparation-outcomes.html)，检查四类主准备、辅助成功、失败/取消详情与刷新；默认桌面及390px无横向溢出，错误日志为空。证据：[取消详情](../../../../frontend/tests/fixtures/preparation-outcome-cancelled.png)、[窄屏](../../../../frontend/tests/fixtures/preparation-outcome-narrow.png)。Django check、makemigrations --check --dry-run及本轮路径diff --check通过；未改Schema。未连接业务API、模型或现用服务；首段部分完成不等于本项或M7模块验收。
  - 2026-09-29收敛完成：生产列表/详情、独立素材准备和[任务中心详情](../../../../backend/studio/task_center_details.py)统一使用顶层outcome和[同一前端适配器](../../../../frontend/src/services/preparationOutcome.ts)，删除presentation内重复字段，无旧字段回退。任务中心指定历史调用只计该次产物，不混入最新失败或其他重试；独立素材区分任务登记、文本结果、Asset关联与远端不确定，不把入库写成下游选择。Smoke/无准备返回null并保留原证据；记录校验不再根据成功状态推断产物存在。停止准备无继续按钮，查看/刷新不派发。
  - 新增/关联隔离验证：后端**8项通过（39.896s）**，含[素材两入口一致性与只读矩阵](../../../../backend/studio/tests/test_execution_preparation_api.py)、[精确历史调用](../../../../backend/studio/tests/test_task_center.py)、原生产主/辅助分离及列表查询上限；聚合一次SQL、窄列、不读manifest，SQLite EXPLAIN使用索引。状态单测含15组组合，最终去重分支后**3项复验通过**。前端executionPreparation/taskCenter **59项通过（22.938s）**；首轮两处夹具/旧文案断言未同步而失败，修正后整组通过，未降低断言。类型检查、Vite构建（11.77s，既有大chunk提醒）、Django check/Schema漂移检查及OpenAPI严格生成通过。
  - 新[任务状态隔离页面](../../../../frontend/tests/fixtures/task-outcomes.html)实测失败→历史成功→返回最新失败、成功无产物、独立素材停止/准备工作台；默认桌面与390px通过（scrollWidth382），取消无继续按钮，浏览器错误日志为空。证据：[成功无产物](../../../../frontend/tests/fixtures/task-outcome-missing.png)、[独立素材窄屏](../../../../frontend/tests/fixtures/task-outcome-narrow.png)。只用显式夹具和隔离SQLite；未迁现用库、加载业务服务、调用模型、实际审核或删除。M7-02开发验收完成，非M7整体/用户/真实远端验收；后续接M7-03失败项恢复核对。
- [x] **M7-03 阶段失败项恢复核对**（P0；依赖：M7-02）。实现/产出：复用原阶段重试逻辑，显示本次会重试/跳过/需处理的单元。验收：一成功一失败只创建失败项新Attempt；成功产物/采用不变，未知状态不直接重新提交。
  - 2026-09-29接续核对：原retry_context/current_primary_attempts/retry_stage_executions已有失败集合与原准备重试链。[原重试定向测试](../../../../backend/studio/tests/test_production_execution_retry.py)的失败重试/幂等及范围变化、成功、未知状态排除共**2项通过（20.596s）**，只运行隔离SQLite与模拟协议，未触发真实重试。本轮尚未改M7-03功能代码；会重试/跳过/需处理的逐单元影响预览仍待实现，本项不勾选。原实现中的费用条件仅为当前代码事实，不作为新增门禁或已完成清理的证明。
  - 2026-09-29部分实现：新增[只读分页影响服务](../../../../backend/studio/services/production_retry_preview.py)及原Run确认弹窗中的[预览组件](../../../../frontend/src/components/ProductionRetryPreview.vue)，复用原重试资格/范围及POST，区分可重试、跳过、需处理；读取无写入/模型副作用，范围变化或读取错误不沿用旧结果。未新增费用条件，不代表原费用逻辑已清理。
  - 验证：原重试与预览后端14项通过（166.868s），覆盖失败项新Attempt、成功产物/选择不变、未知/取消/未提交/权限、分页/只读SQL/索引及范围漂移；[前端专项](../../../../frontend/tests/productionRetryPreview.test.ts)3项通过（3.5285s），含真实组件SSR/挂载、旧响应抑制、分页/错误/卸载与零POST。vue-tsc、Vite（20.24s，既有大chunk提示）、Django check及无Schema漂移通过；严格OpenAPI相对路径写入首次OSError22，绝对路径重跑通过。
  - 2026-09-29收敛完成：[隔离页面](../../../../frontend/tests/fixtures/stage-retry-preview.html)使用真实BaseModal与预览组件，浏览器实测成功/失败混合、第二页未提交准备、远端未知及读取错误；读取失败/未知不能确认，默认桌面与390×844窄屏无横向溢出（documentWidth382），错误/警告日志为空。证据：[桌面](../../../../frontend/tests/fixtures/stage-retry-preview-desktop.png)、[窄屏](../../../../frontend/tests/fixtures/stage-retry-preview-narrow.png)。浏览器只连显式夹具，不连接业务API；确认按钮只检查状态，不触发重试。
  - 同轮[真实生产页setup测试](../../../../frontend/tests/productionRunLoading.test.ts)补足未读取拒绝、失败集合复制/漂移、阶段变化/未知拒绝、重复点击仅一次、原回执与项目刷新；与原API序列化、预览组件测试共**47项通过（6.684s）**。首轮两处旧setup用例未补子组件ready状态、一处夹具structuredClone不能复制Vue代理导致失败，修正测试后全组通过，未减弱业务断言。前轮14项后端/构建等证据不重复算本轮验证。本项开发验收完成，非M7整体/用户/真实远端验收；实际业务页面提交与真实重试未测。临时Vite已停止、浏览器页已关闭并恢复窗口；未迁现用库、改业务服务/配置、触发模型/Smoke/重试、审核采用或删除业务数据。下一项M7-04沿原人工命令接入主调用恢复，不新增配方。
- [x] **M7-04 无配方主调用恢复入口**（P0；依赖：M7-03）。实现/产出：原人工命令/API/页面增加明确失败主调用重试，沿同一准备和回执。验收：不暗建配方，不重跑辅助成功字段，不覆盖旧Attempt；连续点击不会重复提交。完成：2026-09-29，代码/隔离开发验收如下；未触发真实主调用重试。
  - 2026-09-29实现及隔离验证：新增[原人工重试适配器](../../../../backend/studio/services/production_manual_retry.py)，沿原Run锁、命令幂等回执、准备和共同dispatch；阶段目录v5声明retry，只向原冻结目录已声明的Run开放，不重写历史计划。[原工作台组件](../../../../frontend/src/components/ManualPrimaryRetry.vue)提交确切准备修订、失败主调用及原scope，不从unit_key猜scope；重复点击互斥，网络回执未知保留同键核对，明确拒绝刷新、错配回执不冒称成功。原人工生成及生产准备入口共用该组件，失败主调用不再走普通继续按钮。
  - 验证：[后端专项](../../../../backend/studio/tests/test_production_manual_retry.py)与原命令测试共**17项通过（109.364s）**，覆盖成功候选/旧请求保留、同键与不同键去重、失败后继确切目标、当前资源阻断、scope/hash/revision漂移、终态/已采用及事务回滚；均隔离SQLite及假协议，不是实际重试。首轮断言误将历史dispatched当当前evaluation状态，按原事实职责修正断言后通过，未改写历史派发状态。前端[控制器专项](../../../../frontend/tests/manualPrimaryRetry.test.ts)与原人工API测试共**18项通过（6.878s）**；类型检查及Vite build通过（21.32s，既有大chunk提示），Django check、无Schema漂移、严格OpenAPI生成/校验通过。
  - [0212迁移](../../../../backend/studio/migrations/0212_manual_primary_retry.py)仅增加动作选项，用户另行明确批准后已随M1试用应用现用库。API、架构、根OpenAPI及原生产手册同源说明同步。没有提交真实重试/模型请求、审核采用或自动推进阶段。
  - 收尾：修复同一Attempt从running转failed时未自动加载重试目录，补状态变化回归。最新`node --test tests/fieldMaintenance.test.ts tests/manualPrimaryRetry.test.ts tests/productionManual.test.ts` **23项通过（5.434s）**，含M1分页及手册关联，不与早期18项相加；SSR测试仍有24678端口占用及原Router挂钩警告，未影响断言，不把它视为浏览器错误。新增[隔离页面](../../../../frontend/tests/fixtures/manual-primary-retry.html)挂载真实组件并拦截业务网络；桌面核对确认后单次提交、回执非成功、网络未知保留同键（模拟提交2次/不同请求1个）、目录读取失败恢复、旧计划无动作不可提交。390×844再次操作回执未知，文档宽/滚动宽均382，控制台无warn/error；[桌面证据](../../../../frontend/tests/fixtures/manual-primary-retry-desktop.png)、[窄屏证据](../../../../frontend/tests/fixtures/manual-primary-retry-narrow.png)。临时标签已关闭、视口恢复；未增加业务服务或执行队列。
- [x] **M7-05 独立素材实际返回覆盖**（P0；依赖：M7-02）。实现/产出：AssetGenerationJob路径留存/展示已收到原始返回及解析错误，复用正文恢复组件。验收：有返回但校验失败仍可读；未返回不造内容；重试与编辑分别说明是否会调用。完成：2026-10-02，第188–190批代码及隔离验收；现用联调和M7模块交付另列。
  - **2026-10-02第190批：独立素材人工补正闭环**：[原响应服务](../../../../backend/studio/services/execution_text_response_review.py)沿原`complete-text-response`动作核对确切Job、原文SHA及准备revision，按原冻结输出合同校验。原文/诊断不覆盖；确认后保留真实操作者、时间、修订SHA/正文和原Job结果，不造GenerationAttempt。仅确切失败正文可补正，未返回不造内容，字段子任务回到父准备；已有重试/人工结束、取消/忽略/执行中/不确定或已有结果均只读。同稿重放幂等、异稿或过期409、错误正文400、越权404；失败回滚确认/结果，并只清理该事务新建文件。
  - [原素材writer](../../../../backend/studio/services/asset_generation.py)复用ScriptAsset落库，明确`human_correction`来源与原响应/人工修订指纹，不把修订文件标成模型生成；候选、原文和人工修订在原任务详情分开展示。[容量记录器](../../../../backend/studio/services/execution_capacity_observations.py)拒绝人工补正样本，结果说明不再称模型主调用成功。实际远端状态/费用不改造，不重调模型、不自动采用或推进生产步骤；原始失败证据保留。
  - [共用工作台](../../../../frontend/src/components/ExecutionPreparationWorkbench.vue)按真实归属复用补正编辑器，明确编辑不调用、重试会调用；确认前禁用提交，失败保留草稿，异步旧回执丢弃。返回确认原文/修订内容和身份必须一致，已确认修订只读并展示实际操作者；没有新增旁路API或队列。API序列化器/前端类型新增可空asset_job_id、corrected_text、corrected_sha256、confirmed_by，Attempt与Job恰好一方，生产路径同步同一合同；正式API/架构及唯一OpenAPI同步，无新Schema。
  - 后端[新增10项](../../../../backend/studio/tests/test_asset_text_response_correction.py)与聊天留存、Comfy留存、生产语义worker及结果提示最终 **51 tests / 314.141s / OK**。首轮10项曾1失败/3错误（任务详情缺导入及测试夹具/文件数量范围），第二轮39项曾2失败（说明追加位置及另一Smoke夹具的容量样本断言范围）；修复后完整51项通过，未放宽业务合同。覆盖原HTTP权限/零写入读取、重复/过期/状态/重试、原文损坏、事务与新文件回滚、部分输出补正、ScriptAsset真实来源及生产路径回归。原Smoke/模型均显式隔离夹具，不是真实调用。
  - 前端[新增2项](../../../../frontend/tests/assetTextCorrection.test.ts)与原准备工作台共 **36 tests / 24.405s / OK**；vue-tsc、Vite build（15.72s，仅既有分块提示）、Django check、无迁移漂移及严格OpenAPI生成通过。首次OpenAPI绝对路径覆盖OSError22，改同一文件相对路径重试成功，未保留第二契约。[隔离浏览器页面](../../../../frontend/tests/fixtures/asset-text-correction.html)只允许内存模拟回执：真实组件验证成功另存、错误留稿、已有重试只读及读取失败；原文脚本字面量不执行，调用计数0。桌面及390×844验证，窄屏scrollWidth/clientWidth=382，控制台无warn/error，已恢复视口并关闭临时页；[桌面证据](../../../../frontend/tests/fixtures/asset-text-correction-desktop.png)、[窄屏证据](../../../../frontend/tests/fixtures/asset-text-correction-narrow.png)。不外推现用页面或整个M7验收，0213–0222未迁现用库。
  - **2026-10-02第189批：ComfyUI文本收集与留存（部分完成）**：[原worker](../../../../backend/studio/services/asset_generation.py)在确切提交/原领取及远端成功后收集，复用[主输出选择器](../../../../backend/studio/services/execution_workflow_outputs.py)、原响应表与私有文件。核对收集回执的原Preparation标识、冻结唯一文本节点；正文/回执校验失败保存可明确归属的实际文本及安全诊断，不生成候选、费用或审核。取消/忽略/失去领取不接纳晚到正文，缺文本/节点歧义/身份不符不猜测；旧领取重放在collect前退出。读取再次核对请求、节点及字节摘要，损坏不回退候选。
  - [原ComfyUI收集器](../../../../backend/studio/services/comfyui.py)移除正文首尾裁剪及提取层空白拒绝，收到的空字符串可作为零字节原文留存，候选仍由原输出/业务校验决定。text/strings数组按既有换行协议连接，不宣称保存逐字节HTTP回执；64 KiB收集上限和正式文本存储边界未变，超界不截断伪装已留存。存储失败明确本地失败、保留远端成功状态。独立聊天及Comfy成功解析诊断改为response_contract_valid，不提前声称候选已写入。
  - [新增17项隔离测试](../../../../backend/studio/tests/test_comfy_asset_text_response_retention.py)覆盖实际提取至原worker/详情的空白、空文、NUL、回执摘要/大小、缺失/错误/重复节点、请求身份、存储失败、取消/丢领取/忽略、原文损坏、权限、读取零写入及旧领取重放；正式剧本Asset文件与原文分别验证。测试发现原目录/创建将业务script与传输text直接同名匹配，已用同一映射修复两个入口；图片/视频/音频仍精确匹配，并新增拒绝文本冒充媒体的反向用例。只修既有剧本素材路径，不增加另一执行器或新业务字段。
  - 首组`test_comfy_asset_text_response_retention`（当时14项）、`test_execution_comfy_text_worker`、`test_asset_text_response_retention`、`test_execution_workflow_outputs`、`test_comfyui_client`：**58 tests / 207.420s / OK**。补充剧本与媒体回归首轮**37 tests / 342.115s / 2 errors + 3 failures**：两项暴露上述script/text匹配缺陷，三项因测试进程PATH缺仓库已有FFmpeg。未删除或减弱断言；修复映射并仅给测试进程加入`.runtime/toolchains/ffmpeg-9.0.1-win64-gpl-shared-9.0/bin`后，`test_comfy_asset_text_response_retention`（17项）、`test_execution_comfy_text_worker`、`test_asset_text_response_retention`、`test_execution_comfy_asset_worker`、`test_text_workflow_smoke`最终**38 tests / 355.569s / OK**。本轮最终38项与未受后续修改影响的原输出7项/收集器23项共**68个不同用例**，不把重叠58/37项相加。视频样例由隔离FFmpeg编码并真实探测，远端为显式夹具，不是真实模型或Smoke验收。
  - 当前仍不勾选：ComfyUI原文留存段已接通，不再列作下一步；独立素材共用正文补正动作/组件及人工修订来源尚未实现，完整模块页面与现用联调未验。下一段沿原complete-text-response/准备组件接确切Asset归属，保留原文、另存真实人工修订，不造Attempt/自动采用。Django check及makemigrations --check --dry-run --noinput通过，无新Schema/HTTP字段，不重复生成未变OpenAPI或运行无关前端构建。0213–0222仍未迁现用库，无服务/配置操作、真实模型/Smoke/重试、实际审核采用或业务删除；M4/M5语义待决定仅暂停各自分支。
  - **2026-10-02第188批：独立聊天文本原文留存与详情（部分完成）**：[原worker](../../../../backend/studio/services/asset_generation.py)保留实际成功/解析失败正文，以及明确输出额度耗尽收到的部分正文；与[原共用响应服务](../../../../backend/studio/services/execution_text_response_review.py)和私有存储复用，原文不写成成功候选、不复制到审计、不新建队列。存储失败显式失败且不二次调用；缺文本、容量拒绝、提交不确定、取消/失去领取不补造正文，token回执与实际费用仍分离。
  - [0222迁移文件](../../../../backend/studio/migrations/0222_asset_text_response_owner.py)将原响应表归属扩展为原Attempt或AssetGenerationJob恰好一个，保持唯一/不可改绑；不回填历史正文，不生成虚假Attempt。[原任务详情](../../../../backend/studio/task_center_details.py)按原权限只读显示原文、诊断及独立合法候选；损坏原文报错不回退候选，收到空文本与未收到分开。重试仍是新调用，页面明确本段尚无人工补正入口。
  - [新增留存12项](../../../../backend/studio/tests/test_asset_text_response_retention.py)、[原worker](../../../../backend/studio/tests/test_execution_asset_text_worker.py)、原生产正文成功/修正2项与上述M5共**45 tests / 105.654s / OK**。覆盖CRLF/空白逐字节、NUL校验失败、空/缺正文、部分返回/未知/取消、存储失败、损坏、权限404、单归属/不可改绑、重放单次调用、零业务写入详情及SQLite唯一索引。原worker测试改用独立临时MEDIA_ROOT，不触碰业务文件。此组合已包含M5的15项，不把早期41项或失败复验重复相加。
  - [0222隔离迁移测试](../../../../backend/studio/tests/test_asset_text_response_migration.py) **3 tests / 8.778s / OK**：历史0221响应的原Attempt、所有manifest/诊断/确认列和实际文件字节保持；旧素材任务不回填正文；无新素材响应时Schema往返保留旧行；数据库拒绝双归属/无归属和同Job重复响应。使用原MigrationExecutor隔离测试框架及内存文件存储，不连接现用数据。有新素材响应后反向迁移不能恢复非空Attempt，不承诺直接回退。
  - 本项继续未勾选：共用正文补正动作/组件、ComfyUI文本收集及完整模块页面验证尚未接入；没有把旧生产Attempt恢复API冒充独立素材编辑能力。下一段继续这些原入口，不新增旁路队列或重试。0222与0213–0221均未迁现用库；无现用服务/配置、真实模型/Smoke/重试、实际审核采用或业务删除。
- [x] **M7-06 质量与硬错误分离**（P0；依赖：M7-01, M7-05）。实现/产出：统一质量提醒、可人工处理正文、接口/文件/映射硬错误的明确原因。验收：建议长度/文风不硬阻断；无文件/超真实接口上限不假成功；自动/人工策略行为匹配配置。完成：2026-10-04第280批开发及隔离组合验收；现用后端加载与M7模块交付另列。
  - 单镜/批量字段共用[质量提醒组件](../../../../frontend/src/components/ExecutionQualityNotices.vue)，只展示原warnings，不改正文、不单凭建议禁用采用。手动确认和policy批准保留原来源；硬错误保持具体原因、未保存草稿及原采用版本。共用[Profile类型校验](../../../../backend/studio/services/prompt_profiles.py)移除文本隐式strip，保留首尾空白；字段/通道/实际实例硬上限仍按完整文本拒绝，显式策略格式不变，旧冻结请求不重算。
  - 新增[10项后端组合](../../../../backend/studio/tests/test_m7_quality_boundaries.py)及[文本边界回归](../../../../backend/studio/tests/test_prompt_profiles.py)，与原最终组装、字段审核共68/68通过397.689s；覆盖原解析/保存→人工或配置policy→采用→实际传输编译，硬容量、缺实际临时媒体文件、身份/映射错误及零额外产物。前端26/26、类型/构建、桌面/390px[原组件隔离页](../../../../frontend/tests/fixtures/prompt-field-review-contract.html)通过。浏览器是明确合成回执、后端正向终点为传输编译，均不冒称真实发送/远端回收、用户验收或整个M7完成；详细红绿过程及边界见18.3。
- [x] **M7-07 现有诊断证据补齐**（P1；依赖：M7-06）。实现/产出：所用协议传输前后实际请求/返回、任务/资源证据跳转和文件身份检查。验收：准备/提交/远端/解析/保存可区分；资源非空/失败/缺证据点击不串任务，未支持协议不伪报通过。完成：2026-10-04第281–282批开发及隔离验收；现用加载、真实协议目标与M7模块交付另列。
  - **第282批剩余证据收口**：[新增原worker→详情组合7项](../../../../backend/studio/tests/test_m7_diagnostic_stages.py)与原提交6项、聊天HTTP序列化12项分别通过，准备拒绝/提交未知/远端失败/解析/保存可区分；沿原事实读取，不造第二状态源或历史回执。第278批媒体发送边界及第281批确切产物/签名文件证据继续有效，不将驱动对象称为HTTP原始字节。[准备诊断导航组合](../../../../frontend/tests/preparationDiagnosticJourney.test.ts)主线最终11项和未受后续夹具调整影响的历史身份19项通过，原SFC/适配器/RouterLink在桌面与390px完成原A、最新B、错属拒绝、独立任务、草稿、读取失败恢复与缺证据组合；浏览器仅合成内存GET，不是真实模型或现用后端验收。正式API同步五环节证据解释；详细并行、SHA和验证边界见18.3。依赖满足后接M7-08，不等待用户手动验收。
  - **第281批调用/产物身份组合（部分完成）**：[原产物读者](../../../../backend/studio/task_center_details.py)在原Attempt外核对Run/Stage/GenerationTask，错属返回明确输出错误，不投影结果或媒体、不改原事实；[准备工作台](../../../../frontend/src/components/ExecutionPreparationWorkbench.vue)诊断深链固定原生产attempt_id，独立任务须与准备ID关系一致。历史详情拒绝错调用回执、隔离迟到响应/正文分页且只读，文件沿原签名入口验证非空、身份、SHA与可读性。新增后端5项与相邻组合41/41、集成前端45/45、类型/构建及桌面/390px原组件隔离浏览器通过。协议准备/提交/远端/解析/保存分阶段证据和准备页跳转浏览器组合仍待验，整项不勾选；具体红绿、合成回执边界及并行证据见18.3。
- [x] **M7-08 恢复测试与旧入口清理**（P1；依赖：M7-04, M7-07）。实现/产出：成功/失败/取消/状态未知/迟到响应/重复动作测试，删除旁路重试入口与陈旧提示。验收：相同事实只一处状态来源；没有新增费用核实或全局认证门禁。完成：2026-10-04第283批开发及隔离验收；现用加载和完整模块试用沿M7-09执行。
  - [新增5项恢复组合](../../../../backend/studio/tests/test_m7_recovery_journeys.py)沿正式人工动作→原worker→确切调用详情，旧失败不混入成功后继、结果未知不重发、取消不被旧命令重新开启、迟到成功不登记产物、待复核原文不算成功候选；配方重试、独立素材链、REST/Comfy回收定向复验共19个不同后端用例分轮最终通过。未新增恢复端点、队列、Schema或费用门禁。
  - [任务适配器](../../../../frontend/src/services/backendAdapters.ts)保留cancelled/cancel_requested；[共用进度](../../../../frontend/src/services/taskProgress.ts)及原媒体详情的徽标、节点和日志不再误报失败/运行中或显示旧采样百分比。删除store未用独立重试包装，保留原repository/正式页面唯一调用；取消提示返回所属Run，外部授权提示不再要求已退役的本轮预算/切本地路由。前端38项、类型/正式构建和桌面/390px[原组件合成页](../../../../frontend/tests/fixtures/recovery-status.html)通过；首轮夹具错误、测试计数及并行边界见18.3，不冒充真实模型恢复或M7整体交付。
- [x] **M7-09 模块交付与用户验收**（P1；依赖：M7-08）。实现/产出：M7-A01阶段失败项、M7-A02人工主调用、M7-A03独立失败正文、M7-A04未知/取消状态。验收：页面动作与实际新增调用数一致；隔离回执不能当真实远端恢复，真实目标可用性单列。完成：2026-10-05第289批；四组隔离完整组合、授权范围环境切换与现用只读页面交接通过，用户手动试用异步，真实模型/Smoke/生产重试未验。

  **验收场景索引**：下列 `M7-A01～A04` 是本项的四组组合试用简称，不是新增开发任务，也不是 `M7-01～04`。它们不另计入91项；完成情况见本项日期化证据及第18节。后续汇报使用“M7-09／验收场景A01：阶段失败项恢复”等完整名称。

  | 场景简称 | 内容与原页面入口 | 主要验收边界 |
  | --- | --- | --- |
  | M7-A01 | 阶段失败项恢复：制作运行页“重试当前阶段”→影响预览→确认重试 | 只重试确切失败集合，成功候选不重跑；回执丢失只回读，范围变化重新确认，历史调用不被后继覆盖 |
  | M7-A02 | 人工主调用恢复：无配方制作运行→已有准备→“重试此失败主调用” | 原请求不重编译；同键回读不重复派发；当前资源阻断／确认／再次明确重试的调用数一致 |
  | M7-A03 | 独立失败正文恢复：任务中心→准备与调用→模型返回与处理记录 | 原文和人工补正分开保存；非法输入不写入，补正不重新调用模型、不冒充审核采用 |
  | M7-A04 | 未知／取消状态恢复：上述原调用详情及停止入口 | 结果未知不普通重发；提交前取消不调用，取消后迟到结果不登记成功产物，重开仍读同一事实 |

  - **2026-10-05第289批收尾完成**：A02资源阻断/确认均保留一个原失败、零新增协议调用，新键当前修订的明确重试才产生一个后继；原worker合成成功仅一个候选，原输入不重编译、无审核采用。后端10/10、前端51/51、类型/构建及桌面/390px完整旅程通过；修复旧资源提示未清除。286–288的A03/A04/A01证据不重跑或重复累加。备份后正式重载平台并迁0233，现用原独立Run的取消准备、原种子、无冻结提示及零调用详情回读通过，109/110张studio表逐行不变、仅worker租约更新。没有创建现用合成失败或真实重试；手动入口、备份/恢复边界及四场景实际证据见18.3。以下“未完成/未切换”段落为当时记录。
  - 2026-10-05第288批部分完成：验收场景A01两组完整父页/API/worker通过，并修复正式前端预览丢失分页封套的缺陷；丢回执只回读A→B，两页旧A范围拒绝且不新增POST，重新明确确认B后仅B→C。成功候选及冻结输入/参数/种子不改写，历史A/B失败详情与最新成功分开，零审核采用。后端5/5、前端64/64、类型/构建、桌面详情重开和390px整页重载通过；A01此组合已验，A02当前资源阻断分支待接，M7-09仍不勾选、不冒称现用切换或真实远端恢复。已补上述四场景定义表，不新增任务；证据及Goodall只读包见18.3。
  - 2026-10-05第287批部分完成：A04完整ProductionRunView→正式重试API→原worker的未知、提交前取消、取消后迟到成功三组通过，各仅一个后继，种子后合成协议调用分别1/0/1，零产物/审核采用；未知结果通过原页面停止后仍保留uncertain_remote_response，不能普通重试。桌面关闭重开、390px及未知整页重载通过；后端原恢复5项+桥接4项共9/9，临时库/页面已回收。A01接线方案已只读返回，接原ProductionRetryTests.failed_stage与stage-retry场景；A02资源阻断组合仍待，不勾选整项或冒称现用加载。证据见18.3。
  - 2026-10-05第286批部分完成：A03完整任务中心列表→准备与调用→独立失败正文人工补正→关闭重开通过；非法原文400且不写入，有效修订200，原NUL/CRLF/SHA保留、确认人真实记录、种子后调用/审核采用0。原NUL校验器错误提示已修正，隔离清理及嵌套守卫断言已加固；后端13项、前端34+26、类型/OpenAPI一致及桌面/390px通过。A01、A02阻断分支及A04组合仍待，整项未完成/未现用切换；下一包复用manual隔离入口做A04未知/取消/迟到组合，证据见18.3。
  - 2026-10-05第285批部分完成：A02完整ProductionRunView→真实隔离API→原worker验证命令丢回执、同键202/200核对、仅1个后继成功且保留原失败/原请求/零采用；发现并修复同ID父页轮询抹掉pending的缺陷，前端组合100/100、类型/构建及桌面/390px通过。前轮API/运维补丁已应用并同步本轮行为。A02阻断分支及A01/A03/A04其余完整组合仍待补齐，A03只读包已返回未实现；现用仍0232，未切换。0233升级保留测试沿284已验不重复。证据见18.3，不以这一成功场景代替M7整项验收。

<a id="114-优先级依赖与验收边界"></a>

### 11.4 优先级、依赖与验收边界

02–05可在M1后用既有夹具提前，完整媒体请求依赖M5/M6。不重新建设任务中心、监控平台或费用系统；不以用户问如何重试作为代其触发真实任务的授权。
