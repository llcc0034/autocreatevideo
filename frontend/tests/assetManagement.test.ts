import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { buildAssetGenerationJobRequestPayload } from '../src/services/assetGenerationPayload.ts'

test('batch deletion uses one atomic endpoint and requires visible exact selection confirmation', () => {
  const view = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')
  const component = readFileSync(new URL('../src/components/AssetBatchDelete.vue', import.meta.url), 'utf8')
  const repo = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  assert.ok(view.includes('AssetBatchDelete'))
  assert.ok(view.includes('canDeleteAsset'))
  assert.ok(component.includes("confirmation.value !== '删除所选素材'"))
  assert.ok(component.includes('targets.value.map(asset => asset.id)'))
  assert.ok(component.includes('role="alert"'))
  assert.ok(repo.includes("api.post('/assets/batch-delete/'"))
  assert.equal(component.includes('repository.deleteAsset('), false)
})

test('asset library keeps upload/edit/delete and delegates generation to one instance form', () => {
  const view = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')
  const form = readFileSync(new URL('../src/components/AssetExecutionCreateForm.vue', import.meta.url), 'utf8')
  const store = readFileSync(new URL('../src/stores/studio.ts', import.meta.url), 'utf8')
  for (const text of ['AssetExecutionCreateForm', 'downloadAsset(selected)', '编辑素材信息', '删除素材', 'v-model="deleteConfirmName"', ':disabled="deletePending||!deleteConfirmed"', 'role="alert"']) assert.ok(view.includes(text), text)
  assert.ok(store.includes('await repository.deleteAsset(assetId)'))
  assert.ok(form.includes('生成提示词候选')); assert.ok(form.includes('创建生成任务'))
  for (const old of ['generationRouteKey', 'workflowInputDraft', 'parameterDefinitions', 'promptTargetId']) assert.equal(view.includes(old), false, old)
})

test('frozen instance inputs share paged library selection and governed local uploads', () => {
  const form = readFileSync(new URL('../src/components/AssetExecutionCreateForm.vue', import.meta.url), 'utf8')
  const editor = readFileSync(new URL('../src/components/ExecutionRequestInputEditor.vue', import.meta.url), 'utf8')
  const picker = readFileSync(new URL('../src/components/ExecutionInputAssetPicker.vue', import.meta.url), 'utf8')
  const repo = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  assert.ok(form.includes('ExecutionRequestInputEditor'))
  for (const text of ['ExecutionInputAssetPicker', 'paired_with_input_key', 'allowed_purposes', 'allowed_scope_kinds']) assert.ok(editor.includes(text), text)
  assert.ok(picker.includes('repository.getAssetPage(next, query)'))
  assert.ok(picker.includes('repository.createAsset({ file: file.value'))
  assert.ok(picker.includes("imageCategory: 'reference'"))
  assert.ok(picker.includes("emit('selected', asset, 'asset_library', reference)"))
  assert.ok(picker.includes("emit('selected', asset, 'asset_upload')"))
  assert.ok(picker.includes('uploadUnknown.value = true'))
  assert.ok(repo.includes("form.append('file', payload.file, payload.file.name)"))
})

test('instance generation keeps the exact pending request and candidate adoption stays explicit', () => {
  const form = readFileSync(new URL('../src/components/AssetExecutionCreateForm.vue', import.meta.url), 'utf8')
  const repo = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  const workflow = readFileSync(new URL('../src/components/TaskMediaInspector.vue', import.meta.url), 'utf8')
  assert.ok(form.includes('if (!pending.value)'))
  assert.ok(form.includes('repository.createAssetGenerationJob(pending.value)'))
  assert.ok(form.includes('idempotencyKey: crypto.randomUUID()'))
  assert.ok(form.includes('sequence !== optionSequence'))
  assert.ok(form.includes('sequence !== contractSequence'))
  assert.ok(form.includes('result.sha256 !== option.definitionSha256'))
  assert.ok(form.includes("contract.value?.promptMode !== 'manual'"))
  assert.ok(form.includes('采用到选定通道'))
  assert.ok(form.includes('prepare') === false) // No direct dispatch/continue on the creation form.
  assert.ok(repo.includes("'Idempotency-Key': payload.idempotencyKey"))
  assert.equal(repo.includes('raw.routes'), false)
  assert.ok(workflow.includes('ExecutionPreparationWorkbench'))
  assert.ok(workflow.includes("preparing:'准备输入 / 待准入'"))
  assert.equal(workflow.includes('selectedAssetJob.prompt '), false)
})

test('all output kinds send the same instance fixture without legacy routing fields', () => {
  const common = {
    jobType: 'asset' as const, executionInstanceVersionId: 'version-1', expectedDefinitionSha256: 'a'.repeat(64),
    imageCategory: 'character' as const, name: '生成素材', description: '业务描述',
    fixture: { inputs: { brief: { kind: 'literal', value: '原始文字' } }, parameters: { cfg: 0 }, manual_channels: { positive_prompt: '人工通道' }, subject_facts: [], source_language: '', target_language: '' },
    allowExternalCalls: false, idempotencyKey: 'exact-request',
  }
  for (const mediaType of ['image', 'video', 'audio', 'script'] as const) {
    const payload = buildAssetGenerationJobRequestPayload({ ...common, mediaType }, null)
    assert.equal(payload.execution_instance_version_id, common.executionInstanceVersionId)
    assert.equal(payload.expected_definition_sha256, common.expectedDefinitionSha256)
    assert.equal(payload.media_type, mediaType)
    assert.equal(Object.hasOwn(payload, 'image_category'), mediaType === 'image')
    assert.deepEqual(payload.parameters, { cfg: 0 }); assert.deepEqual(payload.field_values, [])
    assert.deepEqual(payload.inputs, common.fixture.inputs)
    assert.equal(payload.allow_external_calls, false)
    for (const key of ['budget_limit', 'model_deployment_id', 'workflow_template_version_id', 'workflow_inputs', 'prompt', 'negative_prompt', 'execution_kind']) assert.equal(Object.hasOwn(payload,key), false,key)
  }
  assert.equal(Object.hasOwn(buildAssetGenerationJobRequestPayload({ ...common, mediaType: 'image', jobType: 'prompt' }, null), 'image_category'), false)
})

test('compact multimodal cards avoid eager script and media downloads', () => {
  const preview = readFileSync(new URL('../src/components/AssetMediaPreview.vue', import.meta.url), 'utf8')

  assert.ok(preview.includes("props.asset.type !== 'script' || props.compact || !source.value"))
  assert.ok(preview.includes(":preload=\"compact?'none':'metadata'\""))
  assert.ok(preview.includes("asset.description || '剧本素材'"))
})

test('asset previews release local blobs and refresh expired signed sources', () => {
  const view = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')
  const preview = readFileSync(new URL('../src/components/AssetMediaPreview.vue', import.meta.url), 'utf8')
  const image = readFileSync(new URL('../src/components/AssetImage.vue', import.meta.url), 'utf8')
  const store = readFileSync(new URL('../src/stores/studio.ts', import.meta.url), 'utf8')

  assert.ok(view.includes('const resetAssetModal=()=>'))
  assert.ok(view.includes('revokeUploadPreview();assetModalOpen.value=false'))
  assert.ok(view.includes('resetAssetModal();await refresh()'))
  assert.ok(view.includes('<div class="asset-detail-image"><AssetMediaPreview'))
  assert.equal(view.includes('<button type="button" class="asset-detail-image"'), false)
  assert.equal(view.includes('.asset-detail-image video{pointer-events:none}'), false)
  assert.ok(preview.includes('sourceError: [payload: { assetId: string; source: string }]'))
  assert.ok(image.includes("emit('sourceError', failedSource)"))
  assert.ok(view.includes('const recoverAssetPreview=async'))
  assert.ok(view.includes('await studio.refreshAsset(assetId,mergeIntoList)'))
  assert.ok(view.includes('@source-error="recoverAssetPreview"'))
  assert.ok(store.includes('const refreshAsset = async (id:string, mergeIntoList=true)'))
})

test('asset creation follows the active media tab and modal close is enabled by default', () => {
  const view = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')
  const modal = readFileSync(new URL('../src/components/BaseModal.vue', import.meta.url), 'utf8')

  assert.ok(modal.includes('withDefaults('))
  assert.ok(modal.includes('{ dismissible: true }'))
  assert.ok(modal.includes('const close = () => { if (props.dismissible)'))
  assert.ok(modal.includes('type="button" class="icon-btn" :disabled="!dismissible"'))
  assert.ok(view.includes('const assetMediaTypeFixed=ref(false)'))
  assert.ok(view.includes("const openCreateAssetFromCurrentTab=()=>openCreateAsset('local',selectedMedia.value||undefined)"))
  assert.equal(view.match(/@click="openCreateAssetFromCurrentTab"/g)?.length, 2)
  assert.ok(view.includes('v-if="!editingAssetId&&!assetMediaTypeFixed" class="asset-kind-picker"'))
  assert.ok(view.includes(':dismissible="!assetSaving&&!generationBusy&&!recoveryLoading"'))
  assert.ok(view.includes(':title="assetModalTitle"'))
})

test('uncertain asset generation has one staff evidence resolution path', () => {
  const view = readFileSync(new URL('../src/components/TaskMediaInspector.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  const store = readFileSync(new URL('../src/stores/studio.ts', import.meta.url), 'utf8')
  const types = readFileSync(new URL('../src/types/index.ts', import.meta.url), 'utf8')

  for (const outcome of ['confirmed_not_submitted', 'confirmed_remote_failed', 'confirmed_remote_cancelled', 'confirmed_remote_succeeded']) {
    assert.ok(types.includes(outcome))
    assert.ok(view.includes(outcome))
  }
  assert.ok(repository.includes('async resolveUncertainAssetGenerationJob'))
  assert.ok(repository.includes('`/asset-generation-jobs/${id}/resolve-uncertain/`'))
  assert.ok(repository.includes('provider_request_id: payload.providerRequestId'))
  assert.ok(repository.includes('actual_cost: payload.actualCost'))
  assert.ok(repository.includes('cost_currency: payload.costCurrency'))
  const adapter = readFileSync(new URL('../src/services/assetGenerationAdapters.ts', import.meta.url), 'utf8')
  assert.ok(adapter.includes("providerRequestIdSha256: string(r,'provider_request_id_sha256')"))
  assert.ok(adapter.includes("ignoreLateResult: boolean(r,'ignore_late_result')"))
  assert.ok(repository.includes("'Idempotency-Key': payload.idempotencyKey"))
  assert.ok(store.includes('await repository.resolveUncertainAssetGenerationJob(id,payload)'))
  assert.ok(view.includes('idempotencyKey:crypto.randomUUID()'))
  assert.ok(view.includes('resolutionIntentJobId!==job.id'))
  assert.ok(view.includes('idempotencyKey:form.idempotencyKey'))
  assert.ok(view.includes("selectedAssetJob.status==='uncertain'&&auth.user?.isStaff"))
  assert.ok(view.includes("selectedAssetJob.status==='uncertain'"))
  assert.ok(view.includes('请联系平台管理员核对远端请求、终态与账单；不要直接重试'))
  assert.ok(view.includes('v-model="resolutionForm.note"'))
  assert.ok(view.includes('v-model="resolutionForm.providerRequestId"'))
  assert.ok(view.includes('v-model="resolutionForm.actualCost"'))
  assert.ok(view.includes('v-model="resolutionForm.costCurrency"'))
  assert.ok(view.includes('resolutionTarget.ignoreLateResult'))
  assert.ok(view.includes('resolutionTarget.providerRequestIdSha256'))
})

test('asset workflows have one task surface and preserve the round trip to the library', () => {
  const assets = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')
  const workflows = readFileSync(new URL('../src/components/TaskMediaInspector.vue', import.meta.url), 'utf8')

  assert.equal(assets.includes('asset-job-panel'), false)
  assert.equal(assets.includes('素材生成任务人工消歧'), false)
  assert.equal(assets.match(/name:'workflows',query:\{category:'asset',source_id:job\.id\}/g)?.length, 1)
  assert.ok(workflows.includes('const target=assetPromptContinuationTarget(job);if(target)void router.push(target)'))
  assert.ok(workflows.includes('v-if="assetPromptContinuationTarget(selectedAssetJob)"'))
  assert.ok(workflows.includes("name:'assets',query:{type:job.mediaType,asset:job.outputAsset.id}"))
  assert.ok(assets.includes('const consumeRouteContext=async()=>'))
  assert.ok(assets.includes('route.query.promptJob'))
  assert.ok(assets.includes('route.query.asset'))
  assert.ok(readFileSync(new URL('../src/views/WorkflowView.vue',import.meta.url),'utf8').includes('taskCenterApi.list'))
  assert.ok(workflows.includes('素材任务 · {{ assetMediaLabel(selectedAssetJob) }}'))
  assert.ok(workflows.includes("selected.execution?.productionRunId?'生产任务':'执行关联不可用'"))
})

test('asset categories and actions stay in a compact sticky control deck', () => {
  const view = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')

  assert.ok(view.includes('class="asset-command-deck panel"'))
  assert.ok(view.includes('class="style-picker asset-style-picker"'))
  assert.ok(view.includes('class="asset-toolbar"'))
  assert.ok(view.includes('class="asset-subtabs"'))
  assert.ok(view.includes('class="result-bar"'))
  assert.ok(view.includes('.asset-command-deck{position:sticky;top:0;z-index:12'))
  assert.ok(view.includes('.asset-style-picker{min-width:0;flex:1;display:flex'))
  assert.ok(view.includes('overflow-x:auto'))
  assert.ok(view.includes(':aria-pressed="selectedMedia===tab.value"'))
  assert.ok(view.includes('aria-label="刷新素材"'))
  assert.ok(view.includes('<ArrowUpDown :size="13"/>'))
})

test('asset details separate complete business metadata from read-only source information', () => {
  const view = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')
  const types = readFileSync(new URL('../src/types/index.ts', import.meta.url), 'utf8')
  const adapters = readFileSync(new URL('../src/services/backendAdapters.ts', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')

  assert.ok(types.includes('sourceInfo?: string'))
  assert.ok(adapters.includes("sourceInfo:String(raw.source_info||'')||undefined"))
  const facts = readFileSync(new URL('../src/components/AssetLibraryFacts.vue', import.meta.url), 'utf8')
  assert.ok(view.includes('<AssetLibraryFacts :asset="selected"/>'))
  assert.ok(facts.includes('<h3>来源说明</h3>'))
  assert.ok(view.includes('来源未记录（历史素材）'))
  assert.ok(view.includes('视觉锚点缺失，素材信息不完整'))
  assert.ok(facts.includes('来源由系统记录，编辑素材信息不会重写来源或重新生成文件。'))
  assert.ok(view.includes("const editingAsset=ref<Asset|null>(null)"))
  assert.ok(view.includes('editingAsset.value=asset'))
  assert.ok(view.includes('const original=editingAsset.value'))
  assert.ok(view.includes('overflow-wrap:anywhere'))
  assert.equal(view.includes('v-model="assetForm.sourceInfo"'), false)
  assert.equal(repository.includes('source_info: payload'), false)
})
