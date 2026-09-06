import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { buildAssetGenerationJobRequestPayload } from '../src/services/assetGenerationPayload.ts'

test('asset management exposes one multimodal upload, generation, download and guarded deletion surface', () => {
  const view = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  const store = readFileSync(new URL('../src/stores/studio.ts', import.meta.url), 'utf8')

  assert.ok(repository.includes('async createAsset(payload:'))
  assert.ok(repository.includes("payload.mediaType === 'video' ? 'reference_video'"))
  assert.ok(repository.includes("payload.mediaType === 'script' ? 'script'"))
  assert.ok(repository.includes("...(payload.kind ? { kind: payload.kind } : {})"))
  assert.ok(repository.includes("'/asset-generation-jobs/options/'"))
  assert.ok(repository.includes("'/asset-generation-jobs/'"))
  assert.ok(repository.includes('await api.delete(`/assets/${assetId}/`)'))
  assert.ok(store.includes('await repository.deleteAsset(assetId)'))
  assert.ok(store.includes('assets.value=assets.value.filter((asset)=>asset.id!==assetId)'))
  assert.ok(view.includes("{ value: 'video', label: '视频'"))
  assert.ok(view.includes("{ value: 'audio', label: '音频'"))
  assert.ok(view.includes("{ value: 'script', label: '剧本'"))
  assert.ok(view.includes('文本模型生成提示词'))
  assert.ok(view.includes('创建生成任务'))
  assert.ok(view.includes('downloadAsset(selected)'))
  assert.ok(view.includes('编辑素材信息'))
  assert.ok(view.includes('删除素材'))
  assert.ok(view.includes('v-model="deleteConfirmName"'))
  assert.ok(view.includes(':disabled="deletePending||!deleteConfirmed"'))
  assert.ok(view.includes('role="alert"'))
})

test('asset workflow references can upload into the managed library and bind the returned asset', () => {
  const view = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')
  const editor = readFileSync(new URL('../src/components/WorkflowExecutionInputEditor.vue', import.meta.url), 'utf8')
  const smokeView = readFileSync(new URL('../src/views/WorkflowVersionEditorView.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')

  assert.ok(editor.includes('allowAssetUpload?: boolean'))
  assert.ok(editor.includes('requestAssetUpload: [request: WorkflowInputAssetUploadRequest]'))
  for (const target of ["kind:'asset_slot'", "kind:'picture'", "kind:'video'", "kind:'paired_audio'", "kind:'audio'"]) {
    assert.ok(editor.includes(target), target)
  }
  assert.ok(editor.includes('上传后进入素材库并自动选中'))
  assert.ok(editor.includes(':aria-busy="Boolean(assetUploadPendingKey)"'))
  assert.ok(view.includes('allow-asset-upload'))
  assert.ok(view.includes('@request-asset-upload="requestWorkflowInputAssetUpload"'))
  assert.ok(view.includes('saved=await studio.createAsset({'))
  assert.ok(view.includes("imageCategory:request.mediaKind==='image'?'reference':undefined"))
  assert.ok(view.includes('workflowInputAssets.value=[saved,...workflowInputAssets.value.filter'))
  assert.ok(view.includes('workflowAssetLoadSequence+=1;workflowInputAssetsLoading.value=false'))
  assert.ok(view.includes('@reset="workflowInputUploadStatus=\'\'"'))
  assert.ok(view.includes("watch(()=>assetForm.value.generationRouteKey,()=>{workflowInputUploadStatus.value=''"))
  assert.ok(view.includes('workflowInputUploadCandidateCompatible(schema,workflowInputDraft.value,request,saved)'))
  assert.ok(view.includes('bindUploadedWorkflowInputAsset(workflowInputDraft.value,request.target,saved.id)'))
  assert.ok(view.includes('已上传并入库，但不符合当前工作流'))
  assert.ok(view.includes('role="status"'))
  assert.equal(smokeView.includes('allow-asset-upload'), false)
  assert.ok(repository.includes("form.append('file', payload.file, payload.file.name)"))
  assert.ok(repository.includes("payload.mediaType === 'video' ? 'reference_video'"))
  assert.ok(repository.includes(": payload.imageCategory || 'reference'"))
})

test('asset generation retries keep one idempotency key and stale option responses are ignored', () => {
  const view = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')
  const workflowView = readFileSync(new URL('../src/views/WorkflowView.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  const payloadBuilder = readFileSync(new URL('../src/services/assetGenerationPayload.ts', import.meta.url), 'utf8')

  assert.ok(view.includes('assetIdempotencyKey:crypto.randomUUID()'))
  assert.ok(view.includes('promptIdempotencyKey:crypto.randomUUID()'))
  assert.ok(view.includes('idempotencyKey:form.assetIdempotencyKey'))
  assert.ok(view.includes('idempotencyKey:form.promptIdempotencyKey'))
  assert.ok(payloadBuilder.includes('idempotencyKey: string'))
  assert.ok(repository.includes("'Idempotency-Key': payload.idempotencyKey"))
  assert.equal(repository.includes("payload.idempotencyKey || crypto.randomUUID()"), false)
  assert.ok(view.includes('const sequence=++optionsLoadSequence;const mediaType=assetForm.value.mediaType'))
  assert.ok(view.includes('sequence!==optionsLoadSequence'))
  assert.ok(view.includes("requiredAssetOperation=computed(()=>({image:'image.text_to_image',video:'video.text_to_video',audio:'audio.text_to_audio',script:'script.generate'}"))
  assert.ok(view.includes("const routeOptionKey=(item:AssetGenerationRouteOption)=>`${item.targetKind}:${item.targetId}`"))
  assert.ok(view.includes("item.targetKind==='comfy_workflow'"))
  assert.ok(view.includes('`[ComfyUI] ${item.name} · ${item.modelDeploymentName}`'))
  assert.ok(view.includes('`[异步 API] ${item.name}'))
  assert.ok(view.includes('v-model="assetForm.generationRouteKey"'))
  assert.ok(view.includes('executionKind:option.targetKind,generationTargetId:option.targetId'))
  assert.ok(repository.includes("raw.routes"))
  assert.ok(repository.includes("targetKind !== 'deployment' && targetKind !== 'comfy_workflow'"))
  assert.ok(repository.includes('smokeEvidenceId: String(raw.smoke_evidence_id'))
  assert.ok(payloadBuilder.includes('workflow_template_version_id: payload.generationTargetId'))
  assert.ok(view.includes('提示词文本模型：需要已发布且启用的文本运行部署（Deployment），声明剧本生成能力（script.generate）'))
  assert.ok(workflowView.includes('v-if="assetJobsError"'))
  assert.equal(view.includes('studio.assets.unshift(job.outputAsset)'), false)
})

test('video, audio and script generation payloads never submit an image category', () => {
  const common = {
    jobType: 'asset' as const,
    generationTargetId: 'target-1',
    imageCategory: 'character' as const,
    name: '生成素材',
    prompt: '生成一个素材',
    idempotencyKey: 'asset-generation-payload-test',
  }

  for (const mediaType of ['video', 'audio'] as const) {
    const payload = buildAssetGenerationJobRequestPayload({
      ...common,
      mediaType,
      executionKind: 'comfy_workflow',
    }, null)
    assert.equal(Object.hasOwn(payload, 'image_category'), false, mediaType)
    assert.equal(payload.media_type, mediaType)
    assert.equal(payload.workflow_template_version_id, common.generationTargetId)
  }

  const scriptPayload = buildAssetGenerationJobRequestPayload({
    ...common,
    mediaType: 'script',
    executionKind: 'deployment',
  }, null)
  assert.equal(Object.hasOwn(scriptPayload, 'image_category'), false, 'script')
  assert.equal(scriptPayload.media_type, 'script')
  assert.equal(scriptPayload.model_deployment_id, common.generationTargetId)

  const imagePayload = buildAssetGenerationJobRequestPayload({
    ...common,
    mediaType: 'image',
    executionKind: 'comfy_workflow',
  }, null)
  assert.equal(imagePayload.image_category, 'character')
})

test('asset generation renders declared parameters while preserving an advanced JSON path', () => {
  const view = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')
  const workflowView = readFileSync(new URL('../src/views/WorkflowView.vue', import.meta.url), 'utf8')

  assert.ok(view.includes('const parameterDefinitions=computed<ParameterDefinition[]>'))
  assert.ok(view.includes("['prompt','negative_prompt','model'].includes(key)"))
  assert.ok(view.includes("definition.options)?definition.options:Array.isArray(definition.enum)"))
  assert.ok(view.includes("definition.min??definition.minimum"))
  assert.ok(view.includes("definition.multiple_of??definition.multipleOf"))
  assert.ok(view.includes('updateParameter(definition,$event)'))
  assert.ok(view.includes('const validateParameters=(parameters:Record<string,unknown>)'))
  assert.ok(view.includes('const parameterError=validateParameters(parameters)'))
  assert.ok(view.includes('resetGenerationParameters'))
  assert.ok(view.includes('高级参数 JSON'))
  assert.ok(view.includes('工作流 {{ selectedGenerationOption.workflowName }}'))
  assert.ok(workflowView.includes('assetJobExecutionLabel(job)'))
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
  assert.ok(view.includes(':dismissible="!assetSaving"'))
  assert.ok(view.includes(':title="assetModalTitle"'))
})

test('uncertain asset generation has one staff evidence resolution path', () => {
  const view = readFileSync(new URL('../src/views/WorkflowView.vue', import.meta.url), 'utf8')
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
  assert.ok(repository.includes('providerRequestIdSha256: String(raw.provider_request_id_sha256'))
  assert.ok(repository.includes('ignoreLateResult: raw.ignore_late_result === true'))
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
  const workflows = readFileSync(new URL('../src/views/WorkflowView.vue', import.meta.url), 'utf8')

  assert.equal(assets.includes('asset-job-panel'), false)
  assert.equal(assets.includes('素材生成任务人工消歧'), false)
  assert.equal(assets.match(/name:'workflows',query:\{scope:'asset',task:job\.id\}/g)?.length, 2)
  assert.ok(workflows.includes("name:'assets',query:{type:job.mediaType,promptJob:job.id}"))
  assert.ok(workflows.includes("name:'assets',query:{type:job.mediaType,asset:job.outputAsset.id}"))
  assert.ok(assets.includes('const consumeRouteContext=async()=>'))
  assert.ok(assets.includes('route.query.promptJob'))
  assert.ok(assets.includes('route.query.asset'))
  assert.ok(workflows.includes('const taskScope = ref<TaskScope>'))
  assert.ok(workflows.includes('素材任务 · {{ assetMediaLabel(job) }}'))
  assert.ok(workflows.includes("selected.productionRunId?'短剧任务':'独立媒体任务'"))
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
  assert.ok(view.includes('overflow-x:auto;overflow-y:hidden'))
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
  assert.ok(view.includes('<label>来源信息</label>'))
  assert.ok(view.includes('来源未记录（历史素材）'))
  assert.ok(view.includes('视觉锚点缺失，素材信息不完整'))
  assert.ok(view.includes('来源由系统记录，不能在素材信息编辑中改写。'))
  assert.ok(view.includes("const editingAsset=ref<Asset|null>(null)"))
  assert.ok(view.includes('editingAsset.value=asset'))
  assert.ok(view.includes('const original=editingAsset.value'))
  assert.ok(view.includes('overflow-wrap:anywhere'))
  assert.equal(view.includes('v-model="assetForm.sourceInfo"'), false)
  assert.equal(repository.includes('source_info: payload'), false)
})
