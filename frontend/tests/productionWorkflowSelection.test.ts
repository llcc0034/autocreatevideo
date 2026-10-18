import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = (relativePath: string) => readFileSync(new URL(relativePath, import.meta.url), 'utf8')

const taskCenterView = source('../src/views/WorkflowView.vue')
const taskCenterDetail = source('../src/components/TaskCenterDetail.vue')
const workflowView = source('../src/components/TaskMediaInspector.vue')
const storyboardView = source('../src/views/StoryboardView.vue')
const seriesStudioView = source('../src/views/SeriesStudioView.vue')
const productionRunView = source('../src/views/ProductionRunView.vue')
const artifactCompare = source('../src/components/GeneratedArtifactCompare.vue')
const artifactComparison = source('../src/services/artifactComparison.ts')
const workflowEditor = source('../src/views/WorkflowVersionEditorView.vue')
const executionInputEditor = source('../src/components/ExecutionRequestInputEditor.vue')
const resourceSmokeForm = source('../src/components/ExecutionSmokeCreateForm.vue')
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
  assert.ok(storyboardView.includes(':to="productionTarget"'))
  assert.ok(storyboardView.includes('/production?view=list'))
  assert.ok(storyboardView.includes('/production/${route.query.run}'))
  assert.ok(storyboardView.includes('router.push(projectInputsTarget)'))
  assert.ok(storyboardView.includes("edit: 'episodes', section: 'episodes'"))
  assert.ok(seriesStudioView.includes('projectProductionTarget'))
  assert.ok(productionRunView.includes('platformApi.createProductionRun'))
  assert.ok(productionRunView.includes('创建生产运行（ProductionRun）'))
})

test('new-series onboarding supports manual, reviewed import, and durable AI ProductionRun paths', () => {
  for (const label of ['无配方人工制作', '导入剧本', '按配方生产']) assert.ok(seriesStudioView.includes(label), label)
  assert.ok(seriesStudioView.includes('platformApi.uploadScript'))
  assert.ok(seriesStudioView.includes('platformApi.createPastedScript'))
  assert.ok(seriesStudioView.includes('platformApi.getScriptRevision'))
  assert.ok(seriesStudioView.includes('platformApi.confirmScriptRevision'))
  assert.ok(seriesStudioView.includes('platformApi.createProductionRun'))
  assert.ok(seriesStudioView.includes('服务端提取正文（只读）'))
  assert.ok(seriesStudioView.includes('项目已创建，但后续步骤尚未完成'))
  assert.ok(seriesStudioView.includes("scriptMode: 'ai'" ) || seriesStudioView.includes("createRunFromOnboarding(created, 'ai')"))
  assert.ok(seriesStudioView.includes("createRunFromOnboarding(created, creationMode.value === 'ai' ? 'ai' : 'confirmed_revision', revision?.id)"))
  assert.ok(seriesStudioView.includes('RecipeExecutionSummary'))
  assert.ok(seriesStudioView.includes('creationRecipeContractError'))
  for (const oldField of ['getRecipeMediaRouteOptions', 'workflowSelection:', 'bibleImagePromptProfileBindingId:', 'referenceInputs:']) {
    assert.equal(seriesStudioView.includes(oldField), false, oldField)
  }
  assert.ok(seriesStudioView.includes('稍后填写 / 逐步生成'))
  assert.ok(seriesStudioView.includes('先建立人工制作记录，不自动调用模型。'))
  assert.equal(seriesStudioView.includes('v-model="newProject.masterScript"'), false)
  assert.equal(seriesStudioView.includes('/series-concept-drafts/generate/'), false)
})

test('unattended AI onboarding requires explicit approval-policy confirmation before creating anything', () => {
  assert.ok(seriesStudioView.includes('v-model="creationUnattendedConfirmed"'))
  assert.ok(seriesStudioView.includes('unattendedConfirmed: creationIsUnattendedRecipe.value'))
  const createProjectBody = seriesStudioView.slice(seriesStudioView.indexOf('const createProject = async () =>'), seriesStudioView.indexOf('const confirmImportedScriptAndContinue = async () =>'))
  assert.ok(createProjectBody.indexOf('if (creationUnattendedError.value)') >= 0)
  assert.ok(createProjectBody.indexOf('if (creationUnattendedError.value)') < createProjectBody.indexOf('const created = await createBaseProject()'))
  assert.equal(seriesStudioView.includes('creationUnattendedReferenceInputs'), false)
})

test('storyboard keeps manual editing while request prompt inspection belongs to the actual task', () => {
  assert.ok(storyboardView.includes('persistEpisodeKeepingSelection'))
  assert.ok(storyboardView.includes('studio.saveEpisode'))
  assert.ok(workflowView.includes('本次请求提示词'))
  assert.ok(workflowView.includes('inputPrompt.preparationId'))
  assert.ok(workflowView.includes('inputPrompt.requestSha256'))
  assert.ok(repository.includes('/input-prompt/'))
  assert.equal(storyboardView.includes('ResolutionProfilePicker'), false)
  assert.equal(storyboardView.includes('ProductionWorkflowPicker'), false)
  assert.equal(storyboardView.includes('episode.multiShot'), false)
  assert.equal(repository.includes('multi_shot:'), false)
})

test('short-drama tasks remain read-only while asset workflows keep their scoped actions', () => {
  assert.ok(taskCenterView.includes('EXECUTION HISTORY'))
  assert.ok(workflowView.includes('只读诊断'))
  for (const handler of ['@click="retry"', '@click="cancelTask"', '@click="deleteTask"', '@click="resumeSelectedBatch"', '@click="cancelSelectedBatch"', '@click="createDeferredGeneration"', '@click="syncWorkflowToComfy"']) {
    assert.equal(workflowView.includes(handler), false, handler)
  }
  for (const implementation of ['const toggleRun=', 'const simulate=', '重新确认取消', '再取消当前任务']) {
    assert.equal(workflowView.includes(implementation), false, implementation)
  }
  assert.equal(workflowView.includes('cancelAssetJob'),false,'task cancellation belongs to the parent task toolbar')
  assert.ok(workflowView.includes('const retryAssetJob=async(job:AssetGenerationJob)'))
  assert.ok(workflowView.includes('resolveUncertainAssetGenerationJob(jobId'))
  assert.ok(workflowView.includes('v-if="selectedAssetJob"'))
  assert.ok(workflowView.includes('v-else-if="selected"'))
})

test('task center filters authoritative server pages without loading all jobs', () => {
  for(const fragment of ['taskCenterApi.list','execution_instance_version_id','source_id','仅看关联异常','pageCursor']) assert.ok(taskCenterView.includes(fragment),fragment)
  for(const retired of ['studio.initialize()', 'getAssetGenerationJobs()', 'buildTaskListEntries', 'route.query.scope']) assert.equal(taskCenterView.includes(retired),false,retired)
  assert.ok(workflowView.includes('repository.getAssetGenerationJob(props.assetJobId)'))
  assert.ok(workflowView.includes('repository.getWorkflow(taskId)'))
  assert.ok(taskCenterDetail.includes('data.calls'))
  assert.ok(taskCenterDetail.includes('辅助调用'))
})

test('task diagnostics reads original preparations and every server-paged call', () => {
  assert.ok(taskCenterDetail.includes('c.preparation_id'))
  assert.ok(taskCenterDetail.includes('data.call_count'))
  assert.ok(taskCenterDetail.includes('data.production_run_id'))
  assert.ok(taskCenterDetail.includes('准备与调用'))
  for (const retiredFragment of ['isBatchListStart', 'filteredWorkflows', 'syncDefaultCollapsedBatches']) {
    assert.equal(workflowView.includes(retiredFragment), false, retiredFragment)
  }
})

test('storyboard edits planning facts without promoting latest task success into adoption', () => {
  const storyboard = readFileSync(new URL('../src/views/StoryboardView.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  for (const obsolete of ['getShotWorkflows', 'latestSuccessfulTask', 'loadShotOutputPreview', 'shotWorkflowTasks', "shot.status = 'done'", "shot.status = 'rendering'"]) {
    assert.equal(storyboard.includes(obsolete), false, obsolete)
  }
  assert.equal(repository.includes('getShotWorkflows'), false)
  assert.ok(storyboard.includes('规划内容 · 非生成画面'))
  assert.ok(storyboard.includes('查看生产与采用结果'))
  assert.ok(storyboard.includes(':to="productionTarget"'))
  assert.ok(storyboard.includes("typeof route.query.run==='string'"))
})

test('confirmed script selection stays explicit and remains usable with existing Episodes', () => {
  assert.equal(productionRunView.includes('projectMasterScript'), false)
  assert.equal(productionRunView.includes('importProjectMasterScript'), false)
  assert.ok(productionRunView.includes('platformApi.createPastedScript'))
  assert.ok(productionRunView.includes('platformApi.confirmScriptRevision'))
  assert.ok(productionRunView.includes("? selectedRevision.value?.status === 'confirmed'"))
  assert.ok(productionRunView.includes('仍可从已确认版本（revision）创建新运行'))
  assert.ok(productionRunView.includes('现有正式对象不会被页面静默覆盖'))
  assert.equal(productionRunView.includes("projectEpisodes.value.length === 0 && selectedRevision.value?.status === 'confirmed'"), false)
})

test('library script import is scoped to active script assets and revision confirmation matches its preview', () => {
  assert.ok(productionRunView.includes("asset.isActive === true && asset.backendKind === 'script'"))
  assert.ok(productionRunView.includes('const resetProjectScopedState = (nextProjectId: string) =>'))
  assert.ok(productionRunView.includes('watch(() => [projectId.value, runId.value, route.query.view, route.query.source]'))
  assert.ok(productionRunView.includes('const sequence = ++productionLoadSequence'))
  assert.ok(productionRunView.includes('const sequence = ++scriptActionSequence, loadSequence = productionLoadSequence'))
  assert.ok(productionRunView.includes('previewRevision.value?.id === selectedRevisionId.value'))
  assert.ok(productionRunView.includes('matchesInput() && selectedRevisionId.value === revisionId'))
  assert.ok(productionRunView.includes('confirmed.contentSha256 !== original.contentSha256'))
  assert.ok(productionRunView.includes(':disabled="Boolean(actionBusy)" @change="selectExistingRevision"'))
  assert.ok(productionRunView.includes('previewRevision.id!==selectedRevisionId'))
})

test('recipe Bible review requires the shared full-content receipt and never infers missing facts', () => {
  for (const fragment of [
    "currentStage.value?.stageKey === 'bible_review'",
    '!bibleConfirmation.value',
    '<RecipeBibleReview',
    '@confirmation="bibleConfirmation = $event"',
  ]) assert.ok(productionRunView.includes(fragment), fragment)
  assert.ok(productionRunView.includes('approvalDecision.value === \'approve\' && reviewApprovalError.value'))
  const review = source('../src/components/RecipeBibleReview.vue')
  assert.ok(review.includes('if (!detail.value || loading.value || error.value) return null'))
  assert.ok(review.includes('buildBibleConfirmation'))
  assert.ok(review.includes("emit('confirmation', null)"))
  assert.ok(review.includes('读取不会调用模型或写入设定'))
  assert.ok(source('../src/components/BibleContentReview.vue').includes('未填写内容不会自动补全'))
  assert.ok(!productionRunView.includes('missingManualBibleLabels'))
  assert.ok(seriesStudioView.includes('studioTabFromRoute'))
  assert.ok(seriesStudioView.includes('route.query.edit'))
  assert.ok(source('../src/components/ManualBibleImageReview.vue').includes("query:{run:run.id,edit:'bible',section:'bible'}"))
})

test('ProductionRun chooses only frozen execution routes through the original stage action', () => {
  assert.ok(productionRunView.includes("'cost_currency_missing'"))
  assert.match(productionRunView, /<RecipeExecutionRouteChoices\b[^\r\n]*:run="currentRun"/)
  assert.ok(productionRunView.includes(':stage-keys="visibleStages.map(stage=>stage.stageKey)"'))
  assert.ok(productionRunView.includes('@select="selectRoute"'))
  const choices = source('../src/components/RecipeExecutionRouteChoices.vue')
  assert.ok(choices.includes('props.run.executionPlan?.value.recipe'))
  assert.ok(choices.includes("group.routingMode==='manual_choice'"))
  assert.ok(choices.includes('route.versionId'))
  assert.ok(choices.includes("emit('select',{stageKey:stage.key,routeIndex:index,appearanceRole:stage.appearanceRole})"))
  assert.equal(productionRunView.includes('routeOptions'), false)
  assert.equal(productionRunView.includes('type="number" min="0" max="15" aria-label="route index"'), false)
})

test('recipe creation reads exact execution routes without independently selecting workflows or bindings', () => {
  for (const view of [productionRunView, seriesStudioView]) {
    assert.ok(view.includes('RecipeExecutionSummary'))
    assert.ok(view.includes('recipeExecutionOverview'))
    for (const oldField of ['getRecipeMediaRouteOptions', 'workflowSelection:', 'promptProfileSelections:', 'bibleImagePromptProfileBindingId:', 'referenceInputs:']) assert.equal(view.includes(oldField), false, oldField)
  }
  assert.equal(platformApi.includes('/media-route-options/'), false)
  const summary = source('../src/components/RecipeExecutionSummary.vue')
  assert.ok(summary.includes('route.versionId'))
  assert.ok(summary.includes('route.definitionSha256'))
  assert.ok(summary.includes('不代表执行准入或烟测已通过'))
})

test('unattended creation confirms frozen policy without a second root media-input contract', () => {
  for (const fragment of ['productionRecipeInteractionMode', 'v-if="!isScriptManagement && isUnattendedRecipe"', 'v-model="unattendedConfirmed"', 'unattendedConfirmed:', '!unattendedCreationError.value', 'stage.approvalDecisionFacts', "fact.actor==='recipe_policy'"]) assert.ok(productionRunView.includes(fragment), fragment)
  assert.equal(productionRunView.includes('unattendedReferenceInputs'), false)
  assert.ok(productionRunView.includes('<RecipeReferenceMappingPreview :run="currentRun"'))
  assert.equal(productionRunView.includes('isProductionReferenceAsset(asset)'), false)
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

test('selected script preview keeps its full checksum readable in narrow layouts', () => {
  assert.ok(productionRunView.includes('aria-label="本次选用剧本预览"'))
  assert.ok(productionRunView.includes('.script-preview>header{display:flex;align-items:flex-start;flex-wrap:wrap;gap:8px}'))
  assert.ok(productionRunView.includes('.script-preview>header>div{display:grid;flex:1 1 220px;min-width:0;gap:4px}'))
  assert.match(productionRunView, /\.script-preview>header small\{[^}]*overflow-wrap:anywhere/)
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
  const smokeFixtureSurface = `${workflowEditor}\n${executionInputEditor}\n${resourceSmokeForm}\n${taskCenterDetail}`
  for (const fragment of ['创建真实烟测任务', 'cancelWorkflowSmokeAttempt', 'listWorkflowSmokeAttempts', 'ResourceSmokeCreateForm', 'ExecutionPreparationWorkbench', 'buildExecutionRequestFixture', 'smokeAttempt.fixtureSha256', 'pairedTo']) assert.ok(smokeFixtureSurface.includes(fragment), fragment)
  for (const retired of ['VueFlow', '导入 JSON', '添加动态节点', '保存新版本']) assert.equal(workflowEditor.includes(retired), false, retired)
  assert.ok(workflowEditor.includes("query:{category:'smoke',kind:'preparation',id}"))
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
  assert.ok(!seriesStudioView.includes('productionRunForAssembly'))
  assert.ok(!seriesStudioView.includes('loadReleaseBundles'))
  const content = source('../src/components/ProductionVersionContents.vue')
  assert.ok(content.includes('section===\'delivery\''))
  assert.ok(content.includes(':bundle-id="run.releaseBundleId" :run-id="run.id" :project-id="run.project"'))
  assert.ok(content.includes('ProductionReleaseDelivery'))
})

test('run cost summary and action confirmation distinguish recorded subtotal from settled billing', () => {
  assert.ok(productionRunView.includes('费用小计（预估 / 承诺 / 已记录实际）'))
  assert.ok(productionRunView.includes('已记录实际仅汇总已确认金额；0不表示免费，未结算费用请到调用详情核对。'))
  assert.equal(productionRunView.match(/\{\{ recordedCostSummaryNote \}\}/g)?.length, 2)
  assert.ok(productionRunView.includes('已记录实际 {{ pendingRunConfirmation.actualCost }}'))
  assert.ok(productionRunView.includes('{{ currentRun.actualCostTotal }} {{ currentRun.costCurrency }}'))
  assert.equal(productionRunView.includes('费用（预估 / 承诺 / 实际）'), false)
  assert.equal(productionRunView.includes('已发生 {{ pendingRunConfirmation.actualCost }}'), false)
})

test('operator manual describes ProductionRun instead of retired submission paths', () => {
  assert.ok(productionManual.includes('ProductionRun'))
  for (const fragment of retiredRuntimeFragments) assert.equal(productionManual.includes(fragment), false, fragment)
  for (const label of ['生成全部提示词', '保存并重新生成', '重新生成本镜头']) {
    assert.equal(productionManual.includes(label), false, label)
  }
  for (const label of ['schema-v2', 'PromptProfile Binding', 'input_snapshot.reference_inputs', '三条不可变配方']) {
    assert.equal(productionManual.includes(label), false, label)
  }
  assert.ok(productionManual.includes('无配方制作不创建隐藏配方'))
  assert.ok(productionManual.includes('用户负责后续真实模型及线上业务验收'))
})
