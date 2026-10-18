import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

import { adaptTaskProgressTelemetry, workflowProgressPresentation } from '../src/services/taskProgress.ts'

test('content saves preserve timing and episode settings without resurrecting retired creative columns', async (context) => {
  const server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent',
    appType: 'custom', server: { middlewareMode: true, hmr: false } })
  context.after(async () => { await server.close() })
  const { adaptShot, shotPersistencePayload, adaptCreatedEpisode } = await server.ssrLoadModule('/src/services/backendAdapters.ts')
  const { api } = await server.ssrLoadModule('/src/services/api.ts')
  const { repository } = await server.ssrLoadModule('/src/services/repository.ts')
  const raw = { id: '29c81b7c-cdeb-44f0-8471-5db8f406a5b1', number: 1, title: 'test',
    narrative: '[0s–2s] Boat drifts.\n[2s–5s] Boat stops.', action: 'Boat approaches a stone.', camera: '',
    media_type: 'video', duration_ms: 5000, episode_start_ms: 45000, episode_end_ms: 50000,
    include_audio: true, conditioning_mode: 'Ref2VA', dialogue: [], character_relations: [], props: [],
    metadata: {}, reference_bindings: {}, continuity_notes: 'Keep the same light.' }
  const project = { id: 'project', bible: { characters: [], scenes: [], props: [] } }
  const shot = adaptShot(raw)
  for (const key of ['narrative', 'action', 'framing', 'camera', 'negativePrompt', 'continuityNotes']) {
    assert.equal(key in shot, false, key)
  }
  const payload = shotPersistencePayload(project, shot)
  for (const key of ['episode_start_ms', 'episode_end_ms', 'duration_ms', 'dialogue']) {
    assert.deepEqual(payload[key], raw[key], key)
  }
  assert.deepEqual(payload.metadata, {})
  assert.equal('reference_bindings' in payload, false)
  assert.equal('reference_assets' in payload, false)
  assert.equal('referenceBindings' in shot, false)
  assert.equal('referenceAssetIds' in shot, false)
  const empty = adaptShot({ ...raw, narrative: '', action: 'Only an action summary.' })
  assert.equal('narrative' in shotPersistencePayload(project, empty), false)
  const changed = { ...shot, narrative: 'Edited narrative', action: 'Edited summary' }
  for (const key of ['narrative', 'action', 'camera', 'continuity_notes', 'negative_prompt', 'lighting', 'framing']) {
    assert.equal(key in shotPersistencePayload(project, changed), false, key)
  }
  const settings = { include_audio: true, script_source: { id: 'confirmed-script', sha256: 'source' },
    breakdown_method: 'test-parser', default_shot_duration_ms: 5000 }
  const episode = adaptCreatedEpisode({ id: 'episode', number: 1, title: 'test', status: 'draft', prompt_settings: settings })
  episode.shots = [shot]
  episode.audioEnabled = false
  const originalPatch = api.patch
  const writes = []
  api.patch = async (url, body) => {
    writes.push({ url, body })
    return { data: { id: url.includes('/episodes/') ? 'episode' : raw.id, ...body } }
  }
  try {
    const saved = await repository.saveEpisode(project, episode)
    assert.deepEqual(writes[0].body.prompt_settings, { ...settings, include_audio: false })
    assert.deepEqual(saved.promptSettings, { ...settings, include_audio: false })
    assert.equal(saved.shots[0].duration, 5)
    assert.equal('narrative' in saved.shots[0], false)
    assert.equal('action' in saved.shots[0], false)
  } finally { api.patch = originalPatch }
})

test('shot persistence keeps exact independent identities and raw dialogue, including when audio is disabled', async (context) => {
  const server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent',
    appType: 'custom', server: { middlewareMode: true, hmr: false } })
  context.after(async () => { await server.close() })
  const { adaptShot, shotPersistencePayload } = await server.ssrLoadModule('/src/services/backendAdapters.ts')
  const first='11111111-1111-4111-8111-111111111111', second='22222222-2222-4222-8222-222222222222'
  const raw={id:'33333333-3333-4333-8333-333333333333',number:1,media_type:'video',include_audio:false,duration_ms:6000,episode_start_ms:null,episode_end_ms:null,
    narrative:'',dialogue:[{speaker_id:'S2',text:'  原文\n保留  ',delivery:' softly ',voiceover:true,start_ms:123,end_ms:987}],
    character_relations:[{role:'visible',position:0,character_id:first},{role:'speaker',position:0,character_id:second}]}
  const project={bible:{characters:[{id:first,name:'同名',stableSpeakerId:'S1'},{id:second,name:'同名',stableSpeakerId:'S2'}],scenes:[],props:[]}}
  const shot=adaptShot(raw)
  const result=shotPersistencePayload(project,shot)
  assert.deepEqual(result.dialogue,raw.dialogue)
  assert.deepEqual(result.character_relations,raw.character_relations)
  assert.equal('characters' in result,false)
  shot.characterRelations=shot.characterRelations.filter(row=>row.role==='speaker')
  assert.equal(shotPersistencePayload(project,shot).character_relations.length,1)
  shot.mediaType='image'
  assert.deepEqual(shotPersistencePayload(project,shot).dialogue,raw.dialogue)
  shot.characterRelations[0].character_id=first
  assert.throws(()=>shotPersistencePayload(project,shot),/身份不一致/)
  assert.throws(()=>adaptShot({...raw,character_relations:undefined,characters:[first,second]}),/不从旧人物池推断/)
})

test('task adapter preserves actual Run identity and strict prepared inputs', async (context) => {
  const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
  const server = await createServer({ root: frontendRoot, logLevel: 'silent', appType: 'custom',
    server: { middlewareMode: true, hmr: false } })
  context.after(async () => { await server.close() })
  const { adaptTasks } = await server.ssrLoadModule('/src/services/backendAdapters.ts')
  const execution = { production_run_id: 'run-1', stage_run_id: 'stage-1', stage_key: 'media_generation', stage_ordinal: 1,
    unit_key: 'candidate-2', attempt_id: 'attempt', attempt_number: 1, preparation_id: 'prep',
    execution_instance_version_id: 'instance', operation: 'video.generate' }
  const frozen = () => ({ kind: 'video', execution, input_contract_status: 'available', input_contract_code: '', input_parameters: {
    preparation_id: 'prep', attempt_id: 'attempt', execution_instance_version_id: 'instance',
    workflow_version_id: 'workflow', request_sha256: 'request', operation: 'video.generate', output_kind: 'video',
    prompt_mode: 'manual', prompt_profile_version_id: null, parameters: [], media_uses: [], prompt_channels: []
  } })
  const [task, unavailable] = adaptTasks([{
    id: 'task-1', project: 'project-1', episode_id: null, episode_number: null, episode_title: '', project_title: 'Actual project',
    status: 'queued', kind: 'video', production_run_id: 'OLD-IGNORED', production_variant_number: 2, ...frozen()
  }, {
    id: 'task-invalid', project: 'project-1', execution: null, status: 'failed', kind: 'video',
    input_contract_status: 'unavailable', input_contract_code: 'frozen_contract_invalid', input_parameters: null
  }])
  assert.equal(task.execution.productionRunId, 'run-1')
  assert.equal(task.execution.unitKey, 'candidate-2')
  assert.equal(task.productionVariantNumber, undefined)
  assert.equal(task.productionRunId, undefined)
  assert.equal(task.episodeNumber, undefined)
  const cancelled = adaptTasks([{ ...frozen(), status: 'cancelled' }])[0]
  assert.equal(cancelled.currentStep, '任务已取消；请返回所属制作轮次查看可用操作')
  assert.doesNotMatch(cancelled.currentStep, /创建.*重试/)
  assert.equal(cancelled.status, 'cancelled')
  assert.equal(cancelled.nodes[0].status, 'cancelled')
  const cancelling = adaptTasks([{ ...frozen(), status: 'cancel_requested', progress_source:'comfyui', comfy_progress:75 }])[0]
  assert.equal(cancelling.status, 'cancel_requested')
  assert.equal(cancelling.nodes[0].status, 'cancel_requested')
  assert.equal(workflowProgressPresentation(cancelled).compactLabel, '已取消')
  assert.equal(workflowProgressPresentation(cancelled).percent, undefined)
  assert.equal(workflowProgressPresentation(cancelling).compactLabel, '取消中')
  assert.equal(workflowProgressPresentation(cancelling).percent, undefined)
  assert.equal(unavailable.execution, null)
  assert.equal(unavailable.inputContractStatus, 'unavailable')
  assert.equal(unavailable.inputParameters, undefined)
  assert.deepEqual(unavailable.referenceAssets, [])
  assert.deepEqual(unavailable.contextMediaUses, [])
  assert.throws(() => adaptTasks([{ id: 'missing', execution: null, status: 'queued', kind: 'video' }]), /状态缺失/)
  assert.throws(() => adaptTasks([{ id: 'missing-code', kind: 'video', execution: null, input_contract_status: 'unavailable', input_parameters: null }]), /必须明确为空并提供原因/)
  const audioSource = { ...frozen(), kind: 'audio' }
  audioSource.input_parameters.output_kind = 'audio'
  const audio = adaptTasks([audioSource])[0]
  assert.equal(audio.kind, 'audio')
  assert.equal(audio.nodes[0].type, 'audio')
  assert.match(audio.nodes[0].label, /音频/)
  assert.throws(() => adaptTasks([{ ...frozen(), kind: undefined }]), /产物类型/)
  assert.throws(() => adaptTasks([{ ...frozen(), kind: 'audio' }]), /本次输入准备不一致/)
  assert.throws(() => adaptTasks([{ ...frozen(), input_contract_status: 'unavailable', input_contract_code: 'frozen_contract_invalid' }]), /必须明确为空并提供原因/)
  const malformed = frozen()
  malformed.input_parameters.media_uses = [{ asset_id: 'asset-1', category: 'video' }] as never
  assert.throws(() => adaptTasks([malformed]), /素材使用类型缺失/)
})

test('task page presents invalid prepared inputs without misleading legacy labels or detail hydration', () => {
  const source = readFileSync(
    new URL('../src/components/TaskMediaInspector.vue', import.meta.url),
    'utf8',
  )

  assert.match(source, /本次执行输入不可展示/)
  assert.match(source, /原因码 \/ Code/)
  assert.match(source, /selectedInputContractUnavailable/)
  assert.match(source, /v-if="!selectedInputContractUnavailable"[^>]+@click="copyInputParameters"/)
})

test('style adapters use only backend facts and preserve unknown server identifiers', async (context) => {
  const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
  const server = await createServer({
    root: frontendRoot,
    logLevel: 'silent',
    appType: 'custom',
    server: { middlewareMode: true, hmr: false },
  })
  context.after(async () => { await server.close() })
  const adapters = await server.ssrLoadModule('/src/services/backendAdapters.ts')
  const backendId = '11111111-1111-4111-8111-111111111111'
  const [style] = adapters.adaptStyles([{
    id: backendId,
    slug: 'wuxia',
    name: '服务端武侠',
    category: '服务端分类',
    description: '服务端说明',
    visual_prompt: 'server-owned visual prompt',
    asset_count: 7,
  }])

  assert.equal(style.name, '服务端武侠')
  assert.equal(style.description, '服务端说明')
  assert.equal(style.promptPrefix, 'server-owned visual prompt')
  assert.deepEqual(style.tags, ['服务端分类'])
  assert.equal(style.assetCount, 7)
  assert.equal(adapters.backendStyleId('wuxia'), backendId)
  assert.equal(adapters.styleSlug(backendId), 'wuxia')
  assert.equal(adapters.styleSlug('future-server-style'), 'future-server-style')
  assert.equal(adapters.styleSlug(), '')

  const [asset] = adapters.adaptAssets([{
    id: 'asset-1',
    kind: 'character',
    name: '林澈',
    description: '人物基础描述',
    prompt_hint: '人物视觉锚点',
    source_info: 'ProductionRun run-1 审核绑定的 Bible 参考图',
    mime_type: 'image/png',
  }])
  assert.equal(asset.sourceInfo, 'ProductionRun run-1 审核绑定的 Bible 参考图')

  const project = adapters.adaptProjectSummary({
    id: 'project-1', title: '项目', style: backendId, language: 'Chinese', aspect_ratio: '9:16', width: 768, height: 1344, production_settings: {},
  })
  assert.equal(project.styleId, 'wuxia')
  assert.equal('defaultMode' in project, false)
  assert.equal(project.language, 'Chinese')
  assert.equal(project.aspectRatio, '9:16')

  const payload = adapters.projectPayload({ title: '新项目', language: 'Chinese', aspectRatio: '9:16', width: 768, height: 1344 })
  assert.equal('default_conditioning_mode' in payload.production_settings, false)
  assert.equal('bible_image_prompt_config' in payload.production_settings, false)
  assert.equal(payload.language, 'Chinese')
  assert.equal(payload.aspect_ratio, '9:16')
  assert.equal(payload.width, 768)
  assert.equal(payload.height, 1344)
  assert.throws(() => adapters.projectPayload({ title: '缺少创建事实' }), /语言、画幅和宽高事实/)
  assert.throws(() => adapters.adaptProjectSummary({ id: 'project-2', title: '缺少事实', production_settings: {} }), /language、aspect_ratio 或尺寸事实/)

  const [withoutCount] = adapters.adaptStyles([{
    id: '22222222-2222-4222-8222-222222222222',
    slug: 'server-only',
    name: '仅服务端风格',
  }])
  assert.equal(withoutCount.assetCount, null)
})

test('legacy running progress never guesses that the submission marker is quantifiable', () => {
  const adapted = adaptTaskProgressTelemetry({
    progress: 10,
    submission_state: 'submitted',
    remote_state: 'running',
  }, 'running')

  assert.equal(adapted.progressSource, 'none')
  assert.equal(adapted.comfyProgress, undefined)
  assert.equal(adapted.remoteState, 'running')
  assert.equal(adapted.progressIndeterminate, true)
  const presentation = workflowProgressPresentation({
    status: 'running',
    submissionState: 'submitted',
    remoteState: adapted.remoteState,
    ...adapted,
  })
  assert.equal(presentation.indeterminate, true)
  assert.equal(presentation.percent, undefined)
  assert.match(presentation.summary, /尚未收到量化进度/)
  assert.doesNotMatch(presentation.valueLabel, /10%/)
})

test('submission progress is presented as initialization without a fake percentage', () => {
  const adapted = adaptTaskProgressTelemetry({
    progress_source: 'submission',
    progress_telemetry_state: 'connected',
    submission_state: 'submitted',
    remote_state: 'submitted',
  }, 'running')
  const presentation = workflowProgressPresentation({
    status: 'running',
    submissionState: 'submitted',
    ...adapted,
  })

  assert.equal(adapted.progressIndeterminate, true)
  assert.equal(presentation.mode, 'indeterminate')
  assert.equal(presentation.percent, undefined)
  assert.equal(presentation.compactLabel, '模型初始化')
  assert.equal(presentation.summary, '已提交，正在模型初始化')
})

test('ComfyUI progress uses only raw current-node telemetry and labels its scope', () => {
  const adapted = adaptTaskProgressTelemetry({
    progress: 62,
    progress_source: 'comfyui',
    comfy_progress: 37.6,
    progress_node_id: 'sampler-10',
    progress_updated_at: '2026-08-13T01:30:00+08:00',
    progress_telemetry_state: 'receiving',
    remote_state: 'running',
  }, 'running')
  const presentation = workflowProgressPresentation({ status: 'running', ...adapted })

  assert.equal(adapted.comfyProgress, 38)
  assert.equal(adapted.progressNodeId, 'sampler-10')
  assert.equal(adapted.progressUpdatedAt, '2026-08-13T01:30:00+08:00')
  assert.equal(adapted.progressTelemetryState, 'receiving')
  assert.equal(adapted.progressIndeterminate, false)
  assert.equal(presentation.mode, 'comfyui')
  assert.equal(presentation.percent, 38)
  assert.equal(presentation.valueLabel, '38%')
  assert.match(presentation.heading, /当前节点采样进度/)
  assert.match(presentation.note || '', /不等于整项任务完成度/)
  assert.doesNotMatch(presentation.valueLabel, /62/)
})

test('telemetry failure remains honest and keeps the generation running', () => {
  const adapted = adaptTaskProgressTelemetry({
    progress_source: 'submission',
    progress_telemetry_state: 'unavailable',
    progress_telemetry_error: 'WebSocket handshake failed',
  }, 'running')
  const presentation = workflowProgressPresentation({ status: 'running', ...adapted })

  assert.equal(adapted.progressTelemetryError, 'WebSocket handshake failed')
  assert.equal(presentation.indeterminate, true)
  assert.match(presentation.note || '', /任务仍会继续执行/)
  assert.match(presentation.note || '', /WebSocket handshake failed/)
})

test('terminal progress is always a determinate 100 percent', () => {
  const adapted = adaptTaskProgressTelemetry({
    progress_source: 'terminal',
    comfy_progress: null,
    progress_telemetry_state: 'connected',
  }, 'completed')
  const presentation = workflowProgressPresentation({ status: 'completed', ...adapted })

  assert.equal(adapted.progressSource, 'terminal')
  assert.equal(adapted.progressIndeterminate, false)
  assert.equal(presentation.mode, 'terminal')
  assert.equal(presentation.percent, 100)
  assert.equal(presentation.valueLabel, '100%')
})
