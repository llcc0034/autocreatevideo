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
const settingsView = source('../src/views/SettingsView.vue')
const platformApi = source('../src/services/platformApi.ts')
const repository = source('../src/services/repository.ts')
const studioStore = source('../src/stores/studio.ts')
const productionManual = source('../src/content/productionManual.ts')
const platformCss = source('../src/platform.css')

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
  assert.ok(productionRunView.includes('创建 ProductionRun'))
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
    '先按“视频输出 + 项目默认模式”核验整集统一 route',
    "mode: 'uniform'",
    'workflowSelection: selectedCreationRecipeSummary.value.hasMediaGeneration',
    '查看 route 依赖或模式缺口',
    'creationMediaRouteSupportsInitialShots',
    '输出 ${routeOption.outputKinds.join',
  ]) assert.ok(seriesStudioView.includes(fragment), fragment)
  assert.ok(seriesStudioView.includes('手工模式只建立空项目'))
  assert.ok(seriesStudioView.includes('有剧本请使用“导入剧本”'))
  assert.equal(seriesStudioView.includes('v-model="newProject.masterScript"'), false)
  assert.equal(seriesStudioView.includes('/series-concept-drafts/generate/'), false)
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

test('task page is a read-only diagnostic surface without direct task mutation controls', () => {
  assert.ok(workflowView.includes('PRODUCTION TASK DIAGNOSTICS'))
  assert.ok(workflowView.includes('只读诊断'))
  for (const handler of ['@click="retry"', '@click="cancelTask"', '@click="deleteTask"', '@click="resumeSelectedBatch"', '@click="cancelSelectedBatch"', '@click="createDeferredGeneration"', '@click="syncWorkflowToComfy"']) {
    assert.equal(workflowView.includes(handler), false, handler)
  }
  for (const implementation of ['const toggleRun=', 'const simulate=', '重新确认取消', '再取消当前任务']) {
    assert.equal(workflowView.includes(implementation), false, implementation)
  }
})

test('project master script import stays explicit and confirmed revision remains usable with existing Episodes', () => {
  assert.ok(productionRunView.includes('const projectMasterScript = computed'))
  assert.ok(productionRunView.includes("platformApi.createPastedScript(projectId.value, projectMasterScript.value, 'plain')"))
  assert.ok(productionRunView.includes('导入当前项目母稿'))
  assert.ok(productionRunView.includes('不是新的 AI 生成结果'))
  assert.ok(productionRunView.includes("? selectedRevision.value?.status === 'confirmed'"))
  assert.ok(productionRunView.includes('仍可从已确认 revision 创建新运行'))
  assert.ok(productionRunView.includes('现有正式对象不会被页面静默覆盖'))
  assert.equal(productionRunView.includes("projectEpisodes.value.length === 0 && selectedRevision.value?.status === 'confirmed'"), false)
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
    '查看 route 依赖缺口',
    '发布/current、smoke 与部署绑定',
    'workflowSelection:',
  ]) assert.ok(productionRunView.includes(fragment), fragment)
  assert.ok(productionRunView.includes('v-if="recipeHasMediaGeneration"'))
  assert.ok(platformCss.includes('.workflow-selection-panel'))
  assert.ok(platformCss.includes('.workflow-shot-row'))
  assert.ok(platformCss.includes('.workflow-route-gaps'))
  assert.ok(platformApi.includes('/media-route-options/'))
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
  assert.ok(platformCss.includes('.page-content{min-height:0;flex:1;overflow:auto'))
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
  assert.ok(seriesStudioView.includes('尚无由 ProductionRun 归档的历史候选'))
})

test('workflow editor describes real smoke execution and does not promise UI graph preservation', () => {
  for (const fragment of ['worker submit / poll / collect', '可能消耗算力和费用', '创建烟测任务', 'cancelWorkflowSmokeAttempt', 'listWorkflowSmokeAttempts']) assert.ok(workflowEditor.includes(fragment), fragment)
  assert.ok(workflowEditor.includes('当前后端没有 UI 图双快照'))
  assert.equal(workflowEditor.includes('复杂控件和子图完整保留'), false)
  assert.equal(workflowEditor.includes('不执行生成'), false)
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
  assert.ok(seriesStudioView.includes('ProductionRun 归档的分集母版'))
  assert.ok(seriesStudioView.includes("tab==='release'"))
})

test('operator manual describes ProductionRun instead of retired submission paths', () => {
  assert.ok(productionManual.includes('ProductionRun'))
  for (const fragment of retiredRuntimeFragments) assert.equal(productionManual.includes(fragment), false, fragment)
  for (const label of ['生成全部提示词', '保存并重新生成', '重新生成本镜头']) {
    assert.equal(productionManual.includes(label), false, label)
  }
})
