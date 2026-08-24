# Production Operator Manual

操作员的权威逐步手册位于前端“生产手册”页面；其结构化源文件是 [frontend/src/content/productionManual.ts](../frontend/src/content/productionManual.ts)。本文只说明入口、范围和维护规则，避免形成第二套会漂移的步骤正文。

## 使用入口

1. 使用 `scripts/restart.ps1` 启动服务。
2. 打开前端并进入“生产手册”。
3. 按手册先完成 migrations、provider/deployment、PromptProfile 字段目录/策略/binding、workflow validation/real smoke、配置 activation 和 host readiness。
4. 在“新建连续剧”选择空项目、导入脚本或 AI 创建。导入模式必须先核对 `ScriptSource` 预览并确认 revision；AI 模式必须显式选择外发授权、币种和预算。
5. 有媒体阶段的运行选择整集统一 workflow 或逐镜 workflow；AI/导入时 Shot 尚未建立，先选统一 route。需要在 ComfyUI 核对工作流时，下载 `download_import` JSON 并手动导入，不期待浏览器直接打开。
6. 在 ProductionRun 内处理 AI/结构化脚本与 Bible 审核、route、cost、候选、重试、不确定响应和交付 gate。worker 会自动继续非人工阶段，required review 必然暂停。
7. Tasks、Deferred requests、Assemblies、ReleaseBundles 页面完全只读；所有创建、重试、取消和有序 fallback 都回到所属 ProductionRun。
8. 最终交付检查每集 episode master 和 ReleaseBundle，不寻找整季单条长视频。

## 当前能力边界

页面手册必须清楚区分：

- 已实现：空项目、导入脚本与 AI 创建三种新建入口；
- 已实现：`ScriptSource` 安全 preview → confirmed revision，以及确定性 `script.structure` no-op/文本模型结构化 preview → required review；
- 已实现：AI script → review → deterministic Bible → review；
- 已实现：PromptProfile 字段目录、多策略 Profile/binding 与缺少/歧义 fail-closed；
- 已实现：Settings 类型化参数与经服务端核验的 `resource_ref` 依赖下拉；
- 已实现：`script.generate`、`script.structure`、`prompt.normalize` 与媒体 deployment smoke 统一返回持久 `202` task，Settings 恢复/轮询终态，单提交不确定状态只允许追加管理员消歧证据；
- 已实现：semantic Comfy workflow；
- 已实现：ComfyUI 安全 `download_import` 导出，精确绑定 validated deployment，不伪造 direct-open；
- 已实现：整集统一或逐镜 workflow selection；
- 已实现：worker 终态事实提交后幂等 auto-advance，required review/成本/选择 gate 暂停；
- 已实现：外部调用显式授权、币种/预算门禁与运行审计链；
- 已实现：async REST T2I/T2V；
- 已实现：managed artifact、逐集 master、ReleaseBundle；
- 已实现：归档时从冻结事实白名单生成 GeneratedArtifact `comparison_parameters`，候选参数差异不使用 manifest 身份/hash；
- 已实现：ProductionRecipe 发布要求完整 content → storyboard → prompt → media → selection → assembly → delivery 闭包；
- 已实现：五层配置、new-boot activation、host probe/recommend draft；
- 已实现：recommendation decide 由服务端复验陈旧快照、目录、配置和候选兼容性；
- 阻断：I2V/reference upload 尚无统一 provider contract；
- 未实现：跨节点/多 GPU scheduler；
- 不会执行：自动下载模型、自动发布 workflow/推荐、自动激活配置；
- 初始状态：bootstrap workflow catalog 是 `needs_review` 草案；
- 尚无仓库证据：真实外部 provider/GPU 完整 E2E。

catalog 或 adapter 中出现 H3/SDXL 等词不代表它是固定生产后端、已经启用或已经通过许可审查。

## 维护

更新生产流程时，应在同一改动中同步：

- `frontend/src/content/productionManual.ts`；
- `frontend/tests/productionManual.test.ts`；
- 根 `openapi.yaml`（仅当 API 契约实际改变）；
- 本页和受影响的正式文档。

验证：

```powershell
Push-Location frontend
npm run test:manual
npm run typecheck
npm run build
Pop-Location
```

不得在 Markdown 中复制整份页面手册，也不得把旧路由或历史操作按钮保留为“备用入口”。
