# 13. M9 素材库管理

> 原设计与任务编号保持；此模块的勾选仅在本文件维护，不复制到索引。

[总索引](../../README.md) · [当前续接](../../current.md) · [按日期查证据](../../records/README.md)

<!-- source-section 21227c42800d -->
<a id="m9"></a>

<a id="13-m9-素材库管理"></a>

## 13. M9 素材库管理

<a id="131-背景需求与产出"></a>

### 13.1 背景、需求与产出

素材库既是独立工作入口，也是自动生产结果的同一落点。用户能直接上传/选已有/生成主图及变体，查看各版本与来源；也能不提前建任何素材，让新剧本自动生成后在库中看到它们。所有Asset不要求挂Bible，图片/视频/音频应可供后续字段选择。

输入：上传文件、已有Asset、生成意图、可选Bible关联。输出：可管理/预览/引用的素材、主/变体系列与版本、实际来源及引用影响。M9不另写生成执行器，主图/变体按钮调用M4。

<a id="132-实现方式与页面"></a>

### 13.2 实现方式与页面

- 扩展`AssetLibraryView.vue`、`AssetExecutionCreateForm.vue`、`AssetMediaPreview.vue`和原Asset API；保留当前列表/筛选/上传，增加主图及变体关系详情、版本浏览、可选Bible关联和生成入口。
- 采用第3.4节系列/版本关系。独立系列仅有名称/角色/父系列及素材关系，不存第二份Bible身份/外观锚点；独立生成描述通过M1表单写入本次准备。关联Bible时读取M3确切设定，不自动根据文件名反推实体。
- 主图上传、变体上传和复用可先交付；M4就绪后接生成按钮。变体生成必须选择/继承一个确切主图版本；上传/复用变体只做关联，不伪造该图片曾由主图生成。
- 上传失败不创建可用空素材；模型失败保留真实任务。新版本不覆盖旧文件；更换默认选择只作用于后续读取，已冻结请求仍指向原Asset。
- 删除沿现有正式入口展示具体目标/引用，明确是否只是移除系列关联还是删除Asset/独占文件。有效共享文件不自动删除；开发历史清理按第14节处理，不因旧审计/模拟费用永久禁止删除。

<a id="133-细任务与逐项验收"></a>

### 13.3 细任务与逐项验收

- [x] **M9-01 主/变体/版本关系Schema**（P0；依赖：M1-03）。实现/产出：明确素材系列、父主系列、版本→Asset、可选Bible设定、真实生成依据等关系及迁移。验收：独立无Bible合法；变体与候选版本区分；同文件/Asset不复制成另一个库。完成：2026-09-29；仅Schema/模型约束和隔离迁移验收，关系API/页面及实际生成不在本项完成范围。
  - [模型](../../../../backend/studio/models.py)新增AssetReferenceSeries（所有者、名称、主/变体角色、父主系列、可选BibleVariant）及AssetReferenceVersion（系列序号、Asset、来源、可选确切设定修订、生成依据版本）。原Asset文件事实不复制；不建第二份锚点、执行队列或名称推断。
  - 生成来源分别关联既有GeneratedArtifact或AssetGenerationJob，二者恰选一个；独立生成不伪造配方产物。上传/复用无生成来源及generated_from；模型生成变体的依据必须属于本系列父主图系列，主图新候选不替换旧依据。基础同属关系由模型clean验证，服务调用、文件真实性与实际请求对应将在M9-02/03及M4接入，不能以存在外键冒称模型确已参考主图。
  - [0211迁移](../../../../backend/studio/migrations/0211_asset_reference_relations.py)仅新增关系表、必要外键、系列版本唯一约束和所有者/角色/名称索引；无旧数据转换或回填。`manage.py test studio.tests.test_asset_reference_models --noinput` **8项通过（11.028s）**，含独立无Bible主图＋两个变体、同变体多个版本、不复制Asset、错误归属/多级关系/生成来源拒绝、确切旧依据、解除关系不删Asset、两种原生成来源及0210→0211保留原Asset。Django check、无迁移漂移通过，全部在进程内内存SQLite及临时媒体，未迁现用库。
- [x] **M9-02 关系服务与API**（P0；依赖：M9-01）。实现/产出：系列/版本新增读取、明确关联/移除、来源投影共用服务。验收：主/变体循环关联拒绝，引用不存在定位错误；绑定Bible只保存引用不复制设定正文，分页可定位同组版本。完成：2026-09-29；仅关系后端/隔离验收，页面和实际上传闭环归M9-03至M9-05，生成writer归M4，不是M9整体用户验收。
  - [共用关系服务](../../../../backend/studio/services/asset_references.py)、[API](../../../../backend/studio/asset_reference_views.py)和[字段合同](../../../../backend/studio/asset_reference_serializers.py)提供系列创建/分页/改名/关系修改/删除及确切版本新增/分页/读取/解除。版本仅关联原Asset；同系列、Asset、来源、设定修订重复提交复用原ID。角色/所有者服务端维护；多级/循环、错误所有权、缺失引用及设定不匹配明确报错。改关联不篡改已存在版本的主图依据/设定修订。
  - 上传/复用公开入口仅允许upload/existing，upload核对原Asset上传事实，不允许客户端声明generated及生成依据。生成版本读取原GeneratedArtifact或AssetGenerationJob状态，不复制执行响应，不创建影子任务或伪造人审。此处只保存关系，不据此宣称文件已验真可播放，媒体来源核对沿M9-03接原共用reader。解除系列/版本仅删除关系，不删除Asset/文件/Bible/原任务；存在子系列或生成依据引用时明确409，非审计/费用保留门禁。
  - [API隔离测试](../../../../backend/studio/tests/test_asset_reference_api.py)16项，连同M9-01模型/迁移8项：`manage.py test studio.tests.test_asset_reference_api studio.tests.test_asset_reference_models --noinput` **24项通过（26.415s）**。覆盖两个变体/多候选/跨页确切ID、重复提交、错误引用与权限、可选Bible不复制锚点、两种原生成来源、状态真实投影、关联删除边界；窄投影无N+1，SQLite EXPLAIN命中关系/所有者索引。普通查询无写入，不读取锚点/大payload或媒体字节。
  - OpenAPI新关系和原Bible合同2项通过（1.501s）；严格`manage.py spectacular --validate --fail-on-warn --file ../openapi.yaml`、Django check、`makemigrations --check --dry-run`通过，原AppearanceRoleEnum命名复用，根契约/API/架构同步。首轮测试启动遇到错误导入，修正后13项API先通过，再补3项范围/状态测试并跑完整24项；首轮schema的匿名查询与同值枚举命名告警已修正并严格重验。未迁现用库，未验证PostgreSQL/并发压力/现用页面/模型，未删业务数据/文件。
- [x] **M9-03 上传及复用主图/变体**（P0；依赖：M9-02, M1-07）。实现/产出：上传→正式Asset→加入对应系列，已有Asset选择；无需剧本/Run。验收：主图及两个变体都可上传或复用，0模型调用，不伪称参考生成；上传失败无可用空版本。完成：2026-09-29；代码及隔离API/页面验证通过，非M9整体或现用环境验收。
  - [素材库入口](../../../../frontend/src/views/AssetLibraryView.vue)增加“添加主图 / 变体素材”，懒挂载[入库面板](../../../../frontend/src/components/AssetReferenceIngest.vue)。角色选项读正式OPTIONS；名称/角色/父主系列明确填写或从服务端搜索分页选择，不要求Bible/剧本/Run。上传与选择复用原ExecutionInputAssetPicker、repository及正式Asset API，不另建上传队列。取得原Asset后才关联，源为upload/existing；不伪造生成任务或主图参考依据。
  - [入库控制器](../../../../frontend/src/services/assetReferenceIngest.ts)保留关联失败的原素材与目标，重试只提交同一关系；请求中禁止重复提交/切换，卸载忽略迟到回执，创建系列结果未知时查询核对，不自动重建。[类型化关系API](../../../../frontend/src/services/assetReferenceApi.ts)复用原系列/版本接口。后端关系写入复用[共用媒体reader](../../../../backend/studio/services/execution_media_sources.py)核对文件/摘要及实际媒体；列表不扫描文件。系列关联不套用执行/Smoke大小限制，执行消费者原上限仍保留。
  - [上传与关系测试](../../../../backend/studio/tests/test_asset_reference_ingest.py)7项＋原关系API16项＋共用媒体读取6项：`manage.py test studio.tests.test_asset_reference_ingest studio.tests.test_asset_reference_api studio.tests.test_execution_media_sources --noinput` **29项通过（20.120s）**。临时文件和内存SQLite验证主图/两个变体正式multipart上传及复用、重复关联同ID、0模型/Run、无效图片/空字节/丢失/变化拒绝、不删除上传事实、校验一致但非图片仍拒绝、OPTIONS角色合同及库内关联不加执行大小门禁。前两轮20项/5项和收尾修正前28项与最终组合重叠，不累加。
  - [前端控制器/合同测试](../../../../frontend/tests/assetReferenceIngest.test.ts)9项及相邻来源策略4项 **13项通过（5.482s）**，类型检查和Vite build通过（11.98s，仅原SettingsView分块提示）；Django check、无迁移漂移通过。本段无HTTP字段/Schema变化，不重复生成不变的根OpenAPI。API/架构/运维入口与失败语义同步。
  - [隔离页面夹具](../../../../frontend/tests/fixtures/asset-reference-ingest.html)使用真实组件、内存目录并拦截真实API：桌面查询/翻页、确切变体选择、已有素材、关联失败保留/重试、父主图选择后创建变体、本地测试PNG选择及上传回填通过；390px页面无横向溢出，浏览器无error/warn。普通鼠标点击在本轮工具中未触发表单，键盘Enter验证交互，文件选择器可用；不宣称鼠标操作全部通过。页面上传返回为明确夹具，真实multipart/文件校验由上述隔离后端测试覆盖，非现用库上传。期间修正夹具的跨系列累计版本号为系列内编号；后端本就按系列分配。临时预览服务/标签已关闭、视口恢复；现用服务和模型未操作。视频/音频的真实预览、关系详情及生成回库分别留后续项，不以图片夹具代验。
- [x] **M9-04 素材列表与关系详情**（P0；依赖：M9-03）。实现/产出：名称/媒体类型/关联筛选、主图/变体/版本切换、预览和任务/来源跳转。验收：能一眼区分主图、两个变体及同变体多个版本；孤立图片/视频/音频仍能正常浏览。完成：2026-09-29；代码、隔离API/页面验证通过，非现用环境或M9整体验收。
  - [关系浏览面板](../../../../frontend/src/components/AssetReferenceBrowser.vue)在原素材库懒挂载；系列名称/媒体/角色/Bible关联筛选走原系列API，父主图和同组变体、版本独立分页。原“全部素材”入口不强制加入系列；入库变化会刷新关系面板。角色复用OPTIONS，无业务名称硬编码。
  - [只读控制器](../../../../frontend/src/services/assetReferenceBrowser.ts)分别处理目录/关系/版本/预览/来源请求序号；切换与卸载不接收迟到响应。按确切Asset ID复用原reader与AssetMediaPreview；读取失败明确呈现，不换图。原素材详情和原任务中心链接复用正式路由；生成依据主图按确切版本ID打开，即使旧版不在第一页也不替换为最新。查询失败不显示“成功零条”，浏览不写绑定/采用事实。
  - [查询接口](../../../../backend/studio/asset_reference_views.py)增加组合媒体/Bible关联/确切Asset筛选，Exists避免重复系列，不缩减完整版本数；省略布尔筛选与false不同，空名称合法。[隔离查询测试](../../../../backend/studio/tests/test_asset_reference_browse.py)4项＋原关系API16项＋定向OpenAPI1项：`manage.py test studio.tests.test_asset_reference_browse studio.tests.test_asset_reference_api studio.tests.test_openapi_schema.OpenApiEnumNamingTests.test_asset_reference_contract_keeps_associations_separate_from_generated_origins --noinput` **21项通过（19.976s）**。内存SQLite验证组合筛选、跨页、确切Asset与媒体同版本匹配、访问范围、无读侧写入/大载荷，关系SQL最多2条；不扫描媒体字节。
  - 首轮20项中4失败：发现QueryDict把省略BooleanField当作未勾选表单false，改为普通查询映射后修复漏系列；查询数断言原计入通用分页配置读取，改为限定关系查询且继续核对整请求无写入。之后5项定向验证与最终21项通过，不降低过滤/权限/无写入断言，不累计重叠测试。[前端测试](../../../../frontend/tests/assetReferenceBrowser.test.ts)10项＋原入库9项 **19项通过（5.973s）**，类型检查、Vite build **11.44s**通过（仅原SettingsView分块提示）；严格OpenAPI生成、Django check、无迁移漂移通过，根API/架构/操作文档同步。
  - [真实组件隔离夹具](../../../../frontend/tests/fixtures/asset-reference-browser.html)拦截真实API并使用内存关系、合成图片/WebM/静音WAV。验证同屏主图/两个变体、跨页旧版、准确旧主图依据、媒体/Bible筛选、查询失败及恢复、原素材详情/任务路由跳转；独立图片加载640px，独立视频与变体视频解码640×360/readyState4，独立音频1秒/readyState4，均无媒体错误。一次嵌套video选择器读取超时，改用只读DOM元素核对解码状态，未修改页面业务。桌面及390px无横向溢出、控制台无error/warn；[桌面截图](../../../../frontend/tests/fixtures/asset-reference-browser-desktop.png)与[窄屏截图](../../../../frontend/tests/fixtures/asset-reference-browser-narrow.png)。使用computer-use仅进行隔离UI检查，无真实模型、业务写入/采用；临时5194及标签已关闭、视口恢复。不代表真实任务页面/生成回库、实际库权限组合或部署验收。
- [x] **M9-05 命名、关联和删除操作**（P0；依赖：M9-04）。实现/产出：任意语言引用名、可选Bible关联、准确删除/解除关系影响提示。验收：改名不改变Asset身份/旧请求，删除不误删其他版本和共享文件；不能以模拟费用缺失阻断范围明确清理。完成：2026-09-29；代码、隔离API/页面验证通过，非现用环境或M9整体验收。
  - [管理面板](../../../../frontend/src/components/AssetReferenceManagement.vue)由原关系浏览懒挂载；从当前确切系列读入任意语言名称和可选Bible，选择项由原BibleVariant窄分页读取，按系列所有者/角色筛选，支持项目/实体/引用名搜索及当前选项跨页保留。[管理控制器](../../../../frontend/src/services/assetReferenceManagement.ts)仅PATCH真实修改，显式null解除，写入时禁重复提交/关闭，失败保留编辑内容、不自动重试；成功再刷新目录，不修改Asset/版本或旧请求。
  - [原共用服务](../../../../backend/studio/services/asset_references.py)新增只读确切关系影响：目标版本数、保留的不同Asset数、子变体数及外部生成依据依赖。页面二次确认系列删除或单版本解除，原DELETE继续事务内检查依赖，预览后新增依赖也明确409；不级联删主图依赖或媒体，不引入费用/审核/签名门禁。原Asset入口补引用数量与位置说明、结构化错误展示；共享上传object key仍有其他Asset时不删文件，最后引用删除后才清理。没有迁移或新的执行/事实来源。
  - 隔离后端`test_asset_reference_api`20项＋`test_asset_reference_browse`4项＋原封面删除10项＋定向OpenAPI1项，最终 **35项通过（41.597s）**。覆盖命名/身份、明确绑定/解除、所有者与角色选项、分页/空搜索、窄查询与SQLite索引、影响无写入、预览后新增依赖拒绝、删除不波及其他版本/产物及共享文件保留。初轮34项中1项夹具违背每实体唯一主图约束，改为不同实体各一个主图后通过，未放宽产品约束。前后组合重叠不累计。
  - [前端管理测试](../../../../frontend/tests/assetReferenceManagement.test.ts)10项＋浏览10项＋入库9项 **29项通过（8.242s）**，覆盖dirty PATCH、任意语言/null、跨页/迟到响应、取消、重复点击、请求失败和删除影响不匹配。[隔离浏览夹具](../../../../frontend/tests/fixtures/asset-reference-browser.html)扩展内存管理，真实API一律拒绝；桌面/390px验证改名、Bible翻页选择/解除、失败重读、空选项、依赖禁删、解除单版本/系列且原素材保留，原Asset删除错误和独立夹具删除后计数。一次label选择器超时后用实际combobox角色恢复，无产品改动；窄屏切换后首次截图尚为旧尺寸，随后实际操作及DOM核对正常。控制台无error/warn、无横向溢出；[管理截图](../../../../frontend/tests/fixtures/asset-reference-management-desktop.png)、[窄屏确认](../../../../frontend/tests/fixtures/asset-reference-management-narrow.png)。computer-use仅操作可重载恢复的内存夹具，未触碰现用数据。
  - 类型检查、Vite build **12.89s**通过（原SettingsView大分块警告保留）；严格OpenAPI生成、Django check及无迁移漂移通过，API/架构/运维文档同步。临时5194及标签已关闭、视口恢复。未做现用环境、PostgreSQL并发、存储故障或真实模型验收；M9-06通用选择器及M9后续生成仍未完成。
- [x] **M9-06 通用素材选择器接入**（P0；依赖：M9-05）。实现/产出：M1媒体控件可按类型/用途/系列选准确版本，并支持本地上传入库后选中。验收：不依赖Bible的图片/视频/音频也能用于对应输入；修改本次选择不全局换主图。完成：2026-09-29；代码及隔离页面验证通过，不代表现用环境或M9整体验收。
  - [共用选材控件](../../../../frontend/src/components/ExecutionInputAssetPicker.vue)增加“按系列与版本选择”，懒加载并复用[关系浏览](../../../../frontend/src/components/AssetReferenceBrowser.vue)的查询、父主图/变体、分页和媒体预览。选材模式不展示改名/关联/删除/来源跳转；显式点击“选择此版本用于本项”才回填。类型固定为当前输入，展示使用处的用途；不把用途误做素材全局属性，不新建另一套关系或执行事实。
  - [确切版本解析](../../../../frontend/src/services/referenceInputSelection.ts)只读重查版本ID/系列/序号/原Asset，再读取原Asset并核对类型、启用和摘要。失效/无权/身份不符不找最新或同名替代。回填现有Asset ID合同，原用途/顺序/使用项标识不变；未来M5精确Bible绑定和M6位置装配仍未完成。独立Asset无需Bible或系列，原本地上传入口继续保留，结构媒体字段也可上传入库后回填；策略仍限制允许来源。请求中防重复，卸载/类型/用途或字段内容变更后不覆盖新值。
  - [解析测试](../../../../frontend/tests/referenceInputSelection.test.ts)7项与[来源/组件测试](../../../../frontend/tests/inputSourcePolicies.test.ts)10项（新增确切选择迟到响应、结构字段覆盖保护，扩充用途变化）以及原浏览/管理/入库/请求表单组合：最终 **68项通过（29.413s）**。覆盖图片/视频/音频、旧版、未知/失效来源、重复选择、卸载及字段改变、原顺序/用途保留、受控上传；前轮26/66与定向17项重叠，不累加。原后端消费者`test_execution_media_source_policy/test_input_source_policies` **21项通过（3.742s）**，使用内存SQLite；不涉及真实模型。
  - [真实组件隔离夹具](../../../../frontend/tests/fixtures/asset-reference-picker.html)拦截全部真实API，合成图片及内存素材验证：两个输入分别选制服旧版/便装；无Bible独立图片/视频/音频进入M1字段；本地测试PNG经内存上传返回Asset后回填；查询失败/恢复、版本解除不回填、取消/分页和390px选择通过，原系列/版本不变、关系写入及模型调用为0。初轮发现共用浏览依赖全局RouterLink警告，改为显式导入后复验无error/warn。桌面选材弹窗加宽、窄屏单列无横向溢出，操作可达；[桌面截图](../../../../frontend/tests/fixtures/asset-reference-picker-desktop.png)、[窄屏截图](../../../../frontend/tests/fixtures/asset-reference-picker-narrow.png)。computer-use只操作可刷新恢复的夹具；本轮视频/音频验证选材，不重新冒称媒体解码或真实上传接口验收。
  - 类型检查与Vite build **13.99s**通过（原SettingsView分块提示保留）；API/架构/运维说明同步。HTTP合同/Schema未变，未重复生成OpenAPI或迁移；未验现用服务联调、全仓/PG或真实生成，不迁现用库、改业务服务/配置、审核采用或删除业务数据。临时5194及标签已关闭、视口恢复。下一项M9-07基础段独立验证；M1-10继续待用户方向确认和现用验收。
- [x] **M9-07 基础段独立验证**（P1；依赖：M9-06）。实现/产出：M9-A01无剧本上传、M9-A02版本/预览、M9-A03选择/删除边界样例及页面/API验证。验收：基础管理可操作；此项完成只表示基础段通过，不能宣称M9生成已完成。完成：2026-09-29；基础段代码及隔离页面/API验收通过，现用环境试用仍待R06范围确认。
  - 在[原上传隔离测试](../../../../backend/studio/tests/test_asset_reference_ingest.py)新增2项跨入口测试，沿正式multipart上传、系列/版本API、M1字段媒体预览API和原Asset删除串联验证。主图＋两个变体、同变体旧版/新版/第三版、跨页确切ID、任意语言改名、用途及顺序保留；改名/新版本不替换已选旧Asset，预览SQL无写入。无Bible/项目/Run/生成任务，外部请求拦截且调用数为0。
  - 解除单版本后，系列入口读该版本404，但原Asset/文件及已选asset_id仍有效；再通过原Asset入口删除独占测试素材后，字段预览明确失效，不回退其他版本，主图及另一输入文件保留。原Asset在关系解除前拒绝删除；共享文件及生成依赖边界复用M9-05证据，不在本项重复制造另一套删除实现。
  - 本段命令：进程内`DATABASE_URL=sqlite:///:memory:`、临时MEDIA_ROOT，`manage.py test studio.tests.test_asset_reference_ingest --noinput` **9项通过（5.899s）**，包括原7项和新增2项，不与此前7/29项重复累加。页面及前端证据复用同轮M9-06的68项测试、类型/构建、桌面/390px真实组件隔离操作；M9-A01/A02的上传/预览与M9-A03关系管理历史证据见M9-03/04/05。三组正式入口复现步骤见13.4，不以夹具代称现用服务或真实模型通过。
- [x] **M9-08 主图/变体模型生成入口**（P0；依赖：M9-07, M4-07）。实现/产出：库内选择实例、填写M1表单、生成主图/参考主图生成变体，调用M4共用服务。验收：页面能看到确切主图和输入差异；不偷偷创建剧本/配方，按钮请求确实走同一M4逻辑。完成：2026-10-04第275批，开发/隔离组合验收通过。
  - [组合测试](../../../../frontend/tests/assetGenerationRelationJourney.test.ts)运行原创建表单、外观编辑器、M1输入组件、API适配器和关系浏览器：可选设定留空→主图P→原输入实际P及明确差异→变体V的generated_from P→原Job深链及返回。错误图片不被自动替换、无POST；明确改选后须重新核对。子树及主线各3/3（不累加），原后端登记2项在内存SQLite＋临时媒体目录完成实际PNG登记、确切旧P引用与重放无副本、无GenerationAttempt/ProductionRun断言，见18.3。
  - 桌面/390px原组件MOCK页通过；发现并修复[外观输入选择器](../../../../frontend/src/components/AssetAppearanceTargetEditor.vue)长ID撑宽，选择器限宽且完整当前输入/素材标识单独换行，不裁掉来源。最终模拟读取48、创建2、未声明网络0，控制台无error/warn；类型/构建通过。仅HTTP回执模拟，未触发真实模型/Smoke/ComfyUI；不替代现用加载、M9-09配方产物回库或M9-10模块交付。
- [x] **M9-09 自动产物回库与旧逻辑清理**（P1；依赖：M9-08, M4-09）。实现/产出：配方生成的主/变体自动显示同一关系详情，补API/指南/测试，清理重复来源/生成控件。验收：配方产物与库内生成无文件副本/影子关系，来源如实区分；失败实际返回复用任务页。完成：2026-10-04第277批，开发及隔离组合验收通过，非M9整体/现用/真实模型验收。
  - 配方与独立生成仍共用原`register_generated_material`；[原主图/两变体组合](../../../../backend/studio/tests/test_recipe_mixed_material_stage.py)扩充重复登记、原Asset/版本/文件SHA不变、确切旧主图依据与正式关系API零DML检查。首次归档到Asset按原架构允许一次物化复制，后继重放/浏览无新副本或影子关系，不声称从未发生物理复制。4个不同用例分段最终通过，首轮夹具断言错误及复验见18.3。
  - [原阶段组件](../../../../frontend/src/components/StageExecutionPreparations.vue)由生产页传入确切Run/Stage，图片失败或零产物仍可“查看本阶段原调用”，复用任务中心，缺Run不猜链接、不使用文本修订代替图片恢复；[关系页](../../../../frontend/src/components/AssetReferenceBrowser.vue)分开显示原调用链接与产物状态，保留独立Job来源，不补第二份执行事实。API/运维/页面指南同步清除过期待交付说明及错误产物枚举夹具。
  - 主线前端27个不同用例通过，最终17项重叠复验通过；类型/Vite、真实组件桌面及390px隔离浏览器通过。原调用A/B、跨Run、空列表/缺身份、原失败说明及内存导航已核对，未加载真实任务详情或提交业务写入。Newton包已集成验证并关闭，M9-10仍依赖M4-10；完整证据及未验边界见18.3第277批。
- [x] **M9-10 整体模块交付与用户验收**（P1；依赖：M9-09, M4-10）。实现/产出：M9-A04独立主图/变体生成、M9-A05自动剧本回库、M9-A06上传/已有/生成混合。验收：两个入口都可用；库内不是自动短剧前置手工步骤，生成未实测部分单列，不以基础段替代整体验收。**2026-10-07第345批完成开发交付**：A04复用325独立三Job真实生成、A05复用331空库三调用自动回库和332零调用复用；345补齐同一正式系列三来源与旧生成依据保留，M4-10依赖已交付。用户手动反馈异步，不等待下载或未授权H3视频。
  - **M9-A06正式混合来源**：备份/在途/确切系列回读后，内置浏览器在325主系列`e8de91ab-e1be-4168-8135-5d1903baeedd`新增上传版v2 `e2bf44ec-0b3c-44db-b274-2b9583cb005c`（Asset3084cff2，名称明确为M9-A06 345本地合成上传夹具），再复用279原Asset c0b57a9b新增existing版v3 `1ef81006-1b3f-4fa7-8f6e-07067c48b6fa`。原generated版v1 bcf6b632及两个生成变体整行/原文件/请求均保留；两个变体“查看生成依据主图版本”仍打开v1，不追新v2/v3。三版本预览/正式整页刷新通过，非生成关系没有生成Job/Artifact/generated_from。新增恰为1份正式上传Asset、2条关系，无新系列/准备/Run/模型调用，未重跑325生成。
  - [当前实证](../../../../.artifacts/batch345/m9-final.json)、[21项来源/零调用/不变断言](../../../../.artifacts/batch345/m9-invariants.json)、[上传来源实页](../../../../.artifacts/batch345/m9-upload-version.png)与18.3收口。混合调度上传/已有零调用及生成单元执行的代码证据复用M4/M9原正式API组合，不用历史口述替代真实来源：Galileo核对指出277夹具实际source=existing，故本批专门补明确upload关系；332是本轮existing使用方式，原版本source=generated不改标。此为关系与流程客观交付，不代表合成夹具是模型产物或艺术质量验收。

<a id="134-优先级依赖与验收边界"></a>

### 13.4 优先级、依赖与验收边界

01–07在M1基础后独立实施，不等M2/M3；可选Bible关系复用既有设定模型，M3后续按稳定ID接入。08–10依赖M4，实现依赖单向明确。不是新增素材搜索推荐、自动内容识别、数字资产版权管理或全局文件去重平台。

<a id="m9基础段复现与交付核对2026-09-29"></a>

#### M9基础段复现与交付核对（2026-09-29）

以下复用既有验收编号，不新增任务或要求。现用页面写入、上传或删除须先核对R06；当前通过范围是隔离API和可刷新恢复的真实组件夹具，不是现用环境验收。样例名称可用任意语言，不是内置业务字段。

| 验收 | 正式入口及样例步骤 | 应看到的结果 | 本轮及既有证据 |
| --- | --- | --- | --- |
| M9-A01 无剧本上传 | 素材库→添加主图/变体素材；上传主图，创建同组两个变体，分别上传或复用已有Asset；不创建剧本/Run | 素材入库后才关联；upload/existing如实区分，失败保留原上传便于重试，不伪造生成依据 | M9-03隔离页面/真实multipart；M9-07新增组合测试，项目/Run/生成任务均0 |
| M9-A02 版本/预览 | 素材库关系视图→主图→两个变体；同变体添加多个版本，翻页打开旧版；全部素材浏览独立图片/视频/音频 | 各版本ID/原Asset可区分，旧版不被新候选替换；无关联Asset仍可查看，失效明确报错 | M9-04桌面/390px及合成媒体解码；M9-07正式API跨页选旧版，再新增第三版并改名，原选择不变 |
| M9-A03 选择/删除边界 | 可编辑媒体输入→选择素材→按系列与版本选择；两个输入分别指定旧版/另一变体，也可选择无Bible素材或本地上传；关系视图解除测试版本，再按明确目标在原Asset入口删除 | 只更新本次输入，保留类型/用途/顺序；选材弹窗不管理关系。解除关系不删素材，删除Asset不换成其他版本，共享文件不误删 | 同轮M9-06桌面/390px选材、上传/迟到响应/错误恢复；M9-07组合API验证关系解除前后及原Asset删除，M9-05共享文件与依赖测试 |

基础段交付不包含M9-A04至A06的模型生成/自动回库，不包含M5的Bible精确绑定或M6最终提示词装配。库内生成和全自动剧本两条路径仍按M4/M9-08至10接续，不把预先手工建素材变成自动短剧的前提。
