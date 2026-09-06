import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = (relativePath: string) => readFileSync(new URL(relativePath, import.meta.url), 'utf8')

const workflowView = source('../src/views/WorkflowView.vue')
const storyboardView = source('../src/views/StoryboardView.vue')
const seriesStudioView = source('../src/views/SeriesStudioView.vue')
const productionRunView = source('../src/views/ProductionRunView.vue')
const artifactCompare = source('../src/components/GeneratedArtifactCompare.vue')
const artifactComparison = source('../src/services/artifactComparison.ts')
const workflowEditor = source('../src/views/WorkflowVersionEditorView.vue')
const workflowExecutionInputEditor = source('../src/components/WorkflowExecutionInputEditor.vue')
const workflowExecutionInputs = source('../src/services/workflowExecutionInputs.ts')
const settingsView = source('../src/views/SettingsView.vue')
const platformApi = source('../src/services/platformApi.ts')
const repository = source('../src/services/repository.ts')
const studioStore = source('../src/stores/studio.ts')
const productionManual = source('../src/content/productionManual.ts')
const platformCss = source('../src/platform.css')
const taskGroups = source('../src/services/taskGroups.ts')
const referenceVideoAssets = source('../src/services/referenceVideoAssets.ts')

const retiredRuntimeFragments = [
  'generatePrompt',
  'generateWholeEpisode',
  'regenerateShot',
  'createWorkflow',
  'build-production',
  '/prompts/generate',
  '/workflows/generate',
]

test('daily production creation has one ProductionRun entry and no retired low-level runtime calls', () => {
  for (const value of [workflowView, storyboardView, seriesStudioView, repository, studioStore]) {
    for (const fragment of retiredRuntimeFragments) assert.equal(value.includes(fragment), false, fragment)
  }
  assert.ok(workflowView.includes('/production`'))
  assert.ok(storyboardView.includes('/production`'))
  assert.ok(seriesStudioView.includes('/production`'))
  assert.ok(productionRunView.includes('platformApi.createProductionRun'))
  assert.ok(productionRunView.includes('创建生产运行（ProductionRun）'))
})

test('new-series onboarding supports manual, reviewed import, and durable AI ProductionRun paths', () => {
  for (const label of ['手工设定', '导入剧本', 'AI 描述创建']) assert.ok(seriesStudioView.includes(label), label)
  assert.ok(seriesStudioView.includes('platformApi.uploadScript'))
  assert.ok(seriesStudioView.includes('platformApi.createPastedScript'))
  assert.ok(seriesStudioView.includes('platformApi.getScriptRevision'))
  assert.ok(seriesStudioView.includes('platformApi.confirmScriptRevision'))
  assert.ok(seriesStudioView.includes('platformApi.createProductionRun'))
  assert.ok(seriesStudioView.includes('服务端提取正文（只读）'))
  assert.ok(seriesStudioView.includes('项目已创建，但后续步骤尚未完成'))
  assert.ok(seriesStudioView.includes("scriptMode: 'ai'" ) || seriesStudioView.includes("createRunFromOnboarding(created, 'ai')"))
  assert.ok(seriesStudioView.includes("createRunFromOnboarding(createdProjectForOnboarding.value, 'confirmed_revision'"))
  for (const fragment of [
    'platformApi.getRecipeMediaRouteOptions',
    'creationMediaRouteIndex',
    'creationWorkflowSelectionError',
    '先按“视频输出 + 项目默认模式”核验整集统一路由（route）',
    "mode: 'uniform'",
    'workflowSelection: selectedCreationRecipeSummary.value.hasMediaGeneration',
    '查看路由（route）依赖或模式缺口',
    'creationMediaRouteSupportsInitialShots',
    '输出 ${routeOption.outputKinds.join',
    'creationMediaPromptProfileBindingId',
    'creationBiblePromptProfileBindingId',
    'promptProfileSelections:',
    'bibleImagePromptProfileBindingId:',
    '显式选择提示词策略（PromptProfile）',
  ]) assert.ok(seriesStudioView.includes(fragment), fragment)
  assert.ok(seriesStudioView.includes('schemaVersion: 2'))
  assert.ok(seriesStudioView.includes('/settings?section=prompts'))
  assert.equal(seriesStudioView.includes('/settings?section=prompt-profiles'), false)
  assert.ok(seriesStudioView.includes('手工模式只建立空项目'))
  assert.ok(seriesStudioView.includes('有剧本请使用“导入剧本”'))
  assert.equal(seriesStudioView.includes('v-model="newProject.masterScript"'), false)
  assert.equal(seriesStudioView.includes('/series-concept-drafts/generate/'), false)
})

test('unattended AI onboarding follows the selected workflow reference-video cardinality', () => {
  for (const fragment of [
    'creationIsUnattendedRecipe',
    'productionRecipeInteractionMode',
    "repository.getAssets({ type: 'video' })",
    'v-model="item.assetId"',
    'v-model="creationUnattendedConfirmed"',
    "backendKind === 'reference_video'",
    'selectedCreationRouteMode',
    'mode.requiresEntityReferenceBible',
    "stageKeys.has('bible_image_review')",
    'selectedCreationUnattendedReferences',
    'creationReferenceTopology',
    'creationReferenceVideoPurposes',
    'creationUnattendedReferenceInputs',
    'defaultPurposeKey',
    'v-model="item.useEmbeddedAudio"',
    'unattendedConfirmed: creationIsUnattendedRecipe.value',
    'referenceInputs: creationIsUnattendedRecipe.value',
    'if (creationUnattendedError.value) { creationError.value = creationUnattendedError.value; return }',
    'creationReferenceVideoInput',
    'creationReferenceVideoRequired',
    '动态参考视频数量、用途和音轨规则来自当前 WorkflowTemplateVersion 的冻结合同',
    '按冻结合同逐条选择；不自动代选素材',
    '创建时不要求也不会提交视频素材',
  ]) assert.ok(`${seriesStudioView}\n${referenceVideoAssets}`.includes(fragment), fragment)
  assert.ok(seriesStudioView.includes('referenceVideoUsableForOwner'))
  assert.ok(seriesStudioView.includes('REFERENCE_VIDEO_CONTENT_SAFETY_NOTICE'))
  const createProjectBody = seriesStudioView.slice(
    seriesStudioView.indexOf('const createProject = async () =>'),
    seriesStudioView.indexOf('const confirmImportedScriptAndContinue = async () =>'),
  )
  assert.ok(createProjectBody.indexOf('if (creationUnattendedError.value)') < createProjectBody.indexOf('const created = await createBaseProject()'))
  assert.equal(seriesStudioView.includes('creationUnattendedReferenceVideos.value[0]'), false)
})

test('storyboard keeps manual editing and prompt audit without direct generation controls', () => {
  assert.ok(storyboardView.includes('persistCurrentShotKeepingSelection'))
  assert.ok(storyboardView.includes('studio.saveShot'))
  assert.ok(storyboardView.includes('提示词审计'))
  assert.equal(storyboardView.includes('ResolutionProfilePicker'), false)
  assert.equal(storyboardView.includes('ProductionWorkflowPicker'), false)
  assert.equal(storyboardView.includes('episode.multiShot'), false)
  assert.equal(repository.includes('multi_shot:'), false)
})

test('short-drama tasks remain read-only while asset workflows keep their scoped actions', () => {
  assert.ok(workflowView.includes('PRODUCTION WORKFLOWS'))
  assert.ok(workflowView.includes('只读诊断'))
  for (const handler of ['@click="retry"', '@click="cancelTask"', '@click="deleteTask"', '@click="resumeSelectedBatch"', '@click="cancelSelectedBatch"', '@click="createDeferredGeneration"', '@click="syncWorkflowToComfy"']) {
    assert.equal(workflowView.includes(handler), false, handler)
  }
  for (const implementation of ['const toggleRun=', 'const simulate=', '重新确认取消', '再取消当前任务']) {
    assert.equal(workflowView.includes(implementation), false, implementation)
  }
  assert.ok(workflowView.includes('const cancelAssetJob=async(job:AssetGenerationJob)'))
  assert.ok(workflowView.includes('const retryAssetJob=async(job:AssetGenerationJob)'))
  assert.ok(workflowView.includes('resolveUncertainAssetGenerationJob(jobId'))
  assert.ok(workflowView.includes('v-if="selectedAssetJob"'))
  assert.ok(workflowView.includes('v-else-if="selected"'))
})

test('task page distinguishes asset, short-drama, and independent media sources', () => {
  for (const fragment of [
    "type TaskScope = 'all' | 'short_drama' | 'asset' | 'media'",
    'studio.assetGenerationJobs',
    'refreshAssetGenerationJobs()',
    '素材任务 · {{ assetMediaLabel(job) }}',
    "selected.productionRunId?'短剧任务':'独立媒体任务'",
    'route.query.scope',
    'route.query.task',
    "const selectedAssetJob=computed",
  ]) assert.ok(workflowView.includes(fragment), fragment)
  assert.ok(workflowView.includes("job.status==='uncertain'"))
  assert.ok(workflowView.includes("filter==='attention'&&job.status==='uncertain'"))
  assert.ok(workflowView.includes("filter==='cancelled'&&job.status==='cancelled'"))
})

test('task diagnostics groups exact whole-episode identities while retaining audit attempts', () => {
  for (const fragment of [
    "`production-run:${workflow.productionRunId}:episode:${workflow.episodeId}`",
    "`legacy-batch:${workflow.batchId}`",
    'tasks: orderedTasks',
    'currentTasks',
    'taskListFilterCounts',
  ]) assert.ok(taskGroups.includes(fragment), fragment)
  assert.ok(taskGroups.includes("if (!workflow.shotId) return `task:${workflow.id}`"))
  assert.ok(workflowView.includes('data-task-group-key'))
  assert.ok(workflowView.includes('selectedTaskGroup.tasks'))
  assert.ok(workflowView.includes('/production/${entry.productionRunId}'))
  assert.ok(workflowView.includes('整集任务状态与入口'))
  for (const retiredFragment of ['isBatchListStart', 'filteredWorkflows', 'syncDefaultCollapsedBatches']) {
    assert.equal(workflowView.includes(retiredFragment), false, retiredFragment)
  }
})

test('project master script import stays explicit and confirmed revision remains usable with existing Episodes', () => {
  assert.ok(productionRunView.includes('const projectMasterScript = computed'))
  assert.ok(productionRunView.includes("platformApi.createPastedScript(projectId.value, projectMasterScript.value, 'plain')"))
  assert.ok(productionRunView.includes('导入当前项目母稿'))
  assert.ok(productionRunView.includes('不是新的 AI 生成结果'))
  assert.ok(productionRunView.includes("? selectedRevision.value?.status === 'confirmed'"))
  assert.ok(productionRunView.includes('仍可从已确认版本（revision）创建新运行'))
  assert.ok(productionRunView.includes('现有正式对象不会被页面静默覆盖'))
  assert.equal(productionRunView.includes("projectEpisodes.value.length === 0 && selectedRevision.value?.status === 'confirmed'"), false)
})

test('library script import is scoped to active script assets and revision confirmation matches its preview', () => {
  assert.ok(productionRunView.includes("asset.isActive === true && asset.backendKind === 'script'"))
  assert.ok(productionRunView.includes('const resetProjectScopedState = (nextProjectId: string) =>'))
  assert.ok(productionRunView.includes('watch(() => [projectId.value, runId.value]'))
  assert.ok(productionRunView.includes('const sequence = ++productionLoadSequence'))
  assert.ok(productionRunView.includes('const sequence = ++revisionLoadSequence'))
  assert.ok(productionRunView.includes('previewRevision.value?.id !== revisionId'))
  assert.ok(productionRunView.includes('selectedRevisionId.value !== revisionId'))
  assert.ok(productionRunView.includes(':disabled="Boolean(actionBusy)" @change="selectExistingRevision"'))
  assert.ok(productionRunView.includes('previewRevision.id!==selectedRevisionId'))
})

test('manual Bible review sends the operator to the current project Bible and blocks incomplete approval', () => {
  for (const fragment of [
    "currentStage.value?.stageKey === 'bible_review'",
    "currentStage.value?.mode === 'manual'",
    'manualBibleCounts',
    'missingManualBibleLabels',
    '不会从剧本猜测、提取或自动生成实体',
    '至少各创建 1 个人物、1 个场景和 1 个道具',
    '平台不会用启发式规则从剧本自动补齐',
    "name:'project-detail'",
    "query:{tab:'bible'}",
    '打开当前项目 Bible',
  ]) assert.ok(productionRunView.includes(fragment), fragment)
  assert.ok(productionRunView.includes('approvalDecision.value === \'approve\' && reviewApprovalError.value'))
  assert.ok(productionRunView.includes("missingManualBibleLabels.value.join('、')"))
  assert.ok(seriesStudioView.includes('studioTabFromRoute'))
  assert.ok(seriesStudioView.includes('route.query.tab'))
})

test('ProductionRun exposes every exceptional gate through its dedicated action', () => {
  assert.ok(productionRunView.includes("'cost_currency_missing'"))
  assert.ok(productionRunView.includes('routeOptions'))
  for (const field of ['driverType', 'providerId', 'modelProfileVersionId', 'modelDeploymentId', 'workflowVersionId', 'workflowOutputKind', 'isExternal']) {
    assert.ok(productionRunView.includes(field), field)
  }
  assert.equal(productionRunView.includes('type="number" min="0" max="15" aria-label="route index"'), false)
})

test('ProductionRun creation supports frozen uniform and complete per-shot workflow selection', () => {
  for (const fragment of [
    "workflowSelectionMode = ref<'uniform' | 'per_shot'>('uniform')",
    'workflowSelectionError',
    'recipeHasMediaGeneration',
    'shotWorkflowRouteIndexes',
    'episodeWorkflowRouteIndexes',
    'applyEpisodeWorkflow',
    'getRecipeMediaRouteOptions',
    'routeSupportsShot',
    'recipeMediaRoutesError',
    '整集统一工作流',
    '逐镜头',
    '应用到本集',
    '查看路由（route）依赖缺口',
    '发布状态、当前版本（current）、烟测（smoke）与部署绑定',
    'workflowSelection:',
    'uniformPromptProfileBindingIds',
    'shotPromptProfileBindingIds',
    'bibleImagePromptProfileBindingId',
    'promptProfileSelections:',
    'promptProfileBindingId:',
    '显式选择提示词策略（PromptProfile）',
    '优先级（priority）仅作目录信息',
  ]) assert.ok(productionRunView.includes(fragment), fragment)
  assert.ok(productionRunView.includes('schemaVersion: 2'))
  assert.ok(productionRunView.includes('v-if="recipeHasMediaGeneration"'))
  assert.ok(platformCss.includes('.workflow-selection-panel'))
  assert.ok(platformCss.includes('.workflow-shot-row'))
  assert.ok(platformCss.includes('.workflow-route-gaps'))
  assert.ok(platformApi.includes('/media-route-options/'))
})

test('unattended ProductionRun creation requires policy confirmation and follows the workflow video-input contract', () => {
  for (const fragment of [
    'productionRecipeInteractionMode',
    "productionRecipeInteractionMode(selectedRecipe.value?.definition || {}) === 'unattended'",
    "=== 'unattended'",
    'v-if="isUnattendedRecipe"',
    'v-model="item.assetId"',
    'v-model="unattendedConfirmed"',
    "backendKind === 'reference_video'",
    'unattendedConditioningMode',
    'mode.requiresEntityReferenceBible',
    'selectedUnattendedReferences',
    'unattendedReferenceTopology',
    'unattendedReferenceVideoPurposes',
    'unattendedReferenceInputs',
    'defaultPurposeKey',
    'v-model="item.useEmbeddedAudio"',
    'unattendedConfirmed:',
    'referenceInputs:',
    '!unattendedCreationError.value',
    'unattendedReferenceVideoInput',
    'unattendedReferenceVideoRequired',
    '动态参考视频是否必选，由当前选中 WorkflowTemplateVersion 的输入基数合同决定',
    '创建时不要求也不会提交视频素材',
    '全程自动审核和继续',
    '当前全自动配方缺少该条件模式要求的 Bible 参考图生成/审核阶段',
    '真正全自动的新剧本运行要求角色、场景和道具 Bible 均为空',
    'stage.approvalDecisionFacts',
    "fact.actor==='recipe_policy'",
  ]) assert.ok(productionRunView.includes(fragment), fragment)
  assert.equal(productionRunView.includes('definitionSchemaVersion === 2'), false)
  assert.ok(productionRunView.includes("repository.getAssets({ type: 'video' })"))
  assert.ok(productionRunView.includes('合同 adapter 的真实文件复探'))
  assert.ok(productionRunView.includes('isProductionReferenceAsset(asset)'))
  assert.ok(productionRunView.includes('REFERENCE_VIDEO_CONTENT_SAFETY_NOTICE'))
  assert.ok(productionRunView.includes('条件模式要求实体参考 Bible'))
  assert.equal(productionRunView.includes("purpose: 'motion_camera_reference'"), false)
})

test('artifact comparison can choose any server candidate while keeping a 2-4 viewport', () => {
  assert.ok(artifactCompare.includes('artifact-candidate-picker'))
  assert.ok(artifactCompare.includes('toggleCandidate'))
  assert.ok(artifactCompare.includes('reconcileArtifactSelection'))
  assert.ok(artifactComparison.includes('selectedIds.length >= 4'))
  assert.ok(artifactComparison.includes('selectedIds.length <= Math.min(2'))
  assert.equal(artifactCompare.includes('props.artifacts.slice(0, 4)'), false)
  assert.ok(artifactComparison.includes("item.status === 'selected'"))
  assert.ok(artifactCompare.includes('platformApi.selectArtifact'))
  assert.ok(artifactCompare.includes("@play=\"synchronize(artifact.id,'play')\""))
  assert.ok(artifactCompare.includes('comparisonParameterRows'))
  assert.equal(artifactComparison.includes('.slice(0, 30)'), false)
})

test('stage disclosure, confirmations, and narrow layouts preserve the single-scroll control plane', () => {
  assert.ok(productionRunView.includes(':aria-expanded="expandedStageId===stage.id"'))
  assert.ok(productionRunView.includes('toggleStageDisclosure(stage.id)'))
  assert.ok(productionRunView.includes('<BaseModal :open="Boolean(pendingRunConfirmation)"'))
  assert.ok(productionRunView.includes('run.status !== pending.runStatus'))
  assert.ok(platformCss.includes('.page-content{width:100%;min-height:0;flex:1;overflow:auto'))
  assert.ok(platformCss.includes('.app-frame{height:100vh;min-height:0;overflow:hidden}'))
  for (const width of ['1280', '980', '720', '390']) assert.ok(platformCss.includes(`@media(max-width:${width}px)`), width)
  assert.ok(platformCss.includes('.run-stage-toggle:focus-visible'))
  assert.ok(platformCss.includes('.artifact-compare-grid>article{min-width:88vw'))
})

test('SeriesStudio candidate history has no dead generation form or mutation state', () => {
  for (const fragment of [
    'submitBibleImageCandidate', 'generateBibleImageCandidate', 'generateAllUnconfirmedBibleImageCandidates',
    'bibleImageDirections', 'bibleImagePromptOverrides', 'bibleImageQueueConfirmOpen', 'bibleImageActionErrors',
    '手工调整提示词 / 补充创作要求',
  ]) assert.equal(seriesStudioView.includes(fragment), false, fragment)
  assert.ok(seriesStudioView.includes('尚无由生产运行（ProductionRun）归档的历史候选'))
})

test('workflow review keeps per-attempt dynamic inputs without a platform graph editor', () => {
  const smokeFixtureSurface = `${workflowEditor}\n${workflowExecutionInputEditor}\n${workflowExecutionInputs}`
  for (const fragment of ['创建烟测任务', 'cancelWorkflowSmokeAttempt', 'listWorkflowSmokeAttempts', '本次 Smoke 输入', 'listWorkflowSmokeAssetCandidates', 'smokeFixtureRequest()', 'smokeAttempt.fixtureSha256', '动态参考 ·', '@click="addVideo"', 'schemaVersion: 2', 'references: {']) assert.ok(smokeFixtureSurface.includes(fragment), fragment)
  for (const retired of ['VueFlow', '导入 JSON', '添加动态节点', '保存新版本']) assert.equal(workflowEditor.includes(retired), false, retired)
  assert.match(workflowEditor, /任务创建后会冻结完整节点参数与本次素材绑定/)
})

test('settings can update existing providers, deployments, and draft recipes without echoing credentials', () => {
  assert.ok(settingsView.includes("platformApi.updateProvider(editorId.value"))
  assert.ok(settingsView.includes("platformApi.updateDeployment(editorId.value"))
  assert.ok(settingsView.includes("platformApi.updateRecipe(editorId.value"))
  assert.ok(settingsView.includes('delete parsed.credential_id'))
  assert.ok(settingsView.includes("editorCredentialId.value === '__clear__'"))
  assert.ok(settingsView.includes('保持现有绑定'))
  assert.ok(!settingsView.includes('credential_ref'))
  assert.ok(platformApi.includes("api.patch(`/production-recipes/${id}/`"))
  assert.ok(settingsView.includes('v-model="selectedHostId" type="radio"'))
  assert.ok(settingsView.includes('watch(selectedHostId'))
  assert.ok(settingsView.includes('clearRuntimeEvidence()'))
  assert.ok(settingsView.includes('loadSelectedHostSnapshot()'))
  assert.ok(settingsView.includes(':disabled="!canDecide'))
  assert.ok(settingsView.includes('runtimeEvidenceState(selectedHostId.value'))
})

test('assembly and release are read-only projections of ProductionRun', () => {
  for (const fragment of ['assembleEpisode', 'createReleaseBundle', 'proposeReleaseSelection', 'updateReleaseBundle', 'sealReleaseBundle', 'releaseReleaseBundle']) {
    assert.equal(studioStore.includes(fragment), false, fragment)
    assert.equal(repository.includes(fragment), false, fragment)
  }
  assert.equal(seriesStudioView.includes('assembleEpisode(episode)'), false)
  assert.ok(seriesStudioView.includes('productionRunForAssembly'))
  assert.ok(seriesStudioView.includes('生产运行（ProductionRun）归档的分集母版'))
  assert.ok(seriesStudioView.includes("tab==='release'"))
})

test('operator manual describes ProductionRun instead of retired submission paths', () => {
  assert.ok(productionManual.includes('ProductionRun'))
  for (const fragment of retiredRuntimeFragments) assert.equal(productionManual.includes(fragment), false, fragment)
  for (const label of ['生成全部提示词', '保存并重新生成', '重新生成本镜头']) {
    assert.equal(productionManual.includes(label), false, label)
  }
})
