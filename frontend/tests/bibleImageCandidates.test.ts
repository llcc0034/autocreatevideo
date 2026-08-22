import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

const projectId = '11111111-1111-4111-8111-111111111111'
const entityId = '22222222-2222-4222-8222-222222222222'
const promptId = '33333333-3333-4333-8333-333333333333'
const generatedWorkflowId = '44444444-4444-4444-8444-444444444444'
const taskId = '55555555-5555-4555-8555-555555555555'
const retryTaskId = '88888888-8888-4888-8888-888888888888'
const assetId = '66666666-6666-4666-8666-666666666666'
const anchorId = '99999999-9999-4999-8999-999999999999'
const sourceFingerprint = 'a'.repeat(64)
const requestFingerprint = 'b'.repeat(64)
const idempotencyKey = 'bible-image-77777777-7777-4777-8777-777777777777'

const rawPromptConfig = () => ({
  schema_version: 1,
  style_prompt: 'cinematic neo-wuxia',
  style_negative_prompt: 'low quality',
  composition_rules: {
    character: 'neutral full-body or three-quarter standing pose',
    scene: 'wide establishing view',
    prop: 'three-quarter product view',
  },
  negative_rules: {
    character: 'multiple people',
    scene: 'people',
    prop: 'hands',
  },
  quality_suffix: 'high detail, production reference',
})

const rawPromptInputs = () => ({
  schema_version: 1,
  positive: [
    {
      key: 'entity.visual_anchor',
      label: '视觉锚点',
      value: 'Elderly man with white beard',
      role: 'positive',
      source: 'character.visual_identity',
      editable: true,
    },
    {
      key: 'system.composition.character',
      label: '系统构图规则',
      value: 'neutral full-body or three-quarter standing pose',
      role: 'positive',
      source: 'project.production_settings.bible_image_prompt_config.composition_rules',
      editable: true,
    },
    {
      key: 'project.style_prompt',
      label: '项目风格',
      value: 'cinematic neo-wuxia',
      role: 'positive',
      source: 'project.production_settings.bible_image_prompt_config.style_prompt',
      editable: true,
    },
  ],
  negative: [
    {
      key: 'negative.system.character',
      label: '系统负面构图规则',
      value: 'multiple people',
      role: 'negative',
      source: 'project.production_settings.bible_image_prompt_config.negative_rules',
      editable: true,
    },
  ],
  final_prompt_text: 'one single character, Elderly man with white beard, cinematic neo-wuxia',
  negative_prompt: 'multiple people, low quality',
})

const rawLatest = (status = 'queued', sourceCurrent = true, overrides: Record<string, unknown> = {}) => ({
  prompt_artifact_id: promptId,
  generated_workflow_id: generatedWorkflowId,
  generation_task_id: taskId,
  task_status: status,
  created_at: '2026-08-15T10:00:00Z',
  idempotency_key: idempotencyKey,
  request_fingerprint: requestFingerprint,
  source_fingerprint: sourceFingerprint,
  source_current: sourceCurrent,
  quality: {
    policy_id: 'bible_image_candidate_advisory_v1',
    status: 'needs_attention',
    advisory_only: true,
    would_block_enqueue: false,
    issues: [{ code: 'bible_image_source_fields_sparse', severity: 'warning', path: 'bible', message: '实体视觉字段较少。' }],
    warnings: ['实体视觉字段较少。'],
  },
  prompt_inputs: rawPromptInputs(),
  output_media: status === 'succeeded' ? [{
    index: 0,
    kind: 'image',
    filename: 'candidate.png',
    preview_url: '/media/candidate.png',
    download_url: '/media/candidate.png',
    ai_generated: true,
  }] : [],
  promoted_assets: [],
  review_required: true,
  persisted_as_asset: false,
  ...overrides,
})

const rawProjection = (status = 'queued', sourceCurrent = true, history?: Record<string, unknown>[]) => {
  const latest = rawLatest(status, sourceCurrent)
  const candidateHistory = history || [latest]
  return {
    entity_type: 'character',
    entity_id: entityId,
    entity_name: '沈砚',
    source_fingerprint: sourceFingerprint,
    current_prompt_inputs: rawPromptInputs(),
    anchor_asset: null,
    reference_assets: [],
    latest_candidate: candidateHistory[0] || null,
    candidate_history: candidateHistory,
  }
}

const rawCandidates = (status = 'queued', sourceCurrent = true) => ({
  project_id: projectId,
  prompt_config: rawPromptConfig(),
  candidates: [rawProjection(status, sourceCurrent)],
  counts: { characters: 1, scenes: 0, props: 0, total: 1, with_candidate: 1 },
})

const createOptions = (extendedGenerationConfirmed = false) => ({
  entityType: 'character' as const,
  entityId,
  idempotencyKey,
  extraDirection: '冷雨逆光，保持身份稳定',
  seed: null,
  priority: 0,
  maxAttempts: 3,
  promptOverrides: {},
  extendedGenerationConfirmed,
})

test('Bible image candidate contract stays read-only until an explicit create or promotion', async (context) => {
  const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
  const server = await createServer({
    root: frontendRoot,
    logLevel: 'silent',
    appType: 'custom',
    server: { middlewareMode: true, hmr: false },
  })
  context.after(async () => { await server.close() })

  const service = await server.ssrLoadModule('/src/services/bibleImageCandidates.ts')
  const adapters = await server.ssrLoadModule('/src/services/backendAdapters.ts')
  const { repository } = await server.ssrLoadModule('/src/services/repository.ts')
  const { api } = await server.ssrLoadModule('/src/services/api.ts')
  const originalGet = api.get
  const originalPost = api.post
  context.after(() => { api.get = originalGet; api.post = originalPost })

  await context.test('GET is zero-write and strictly projects frozen target, advisory, task, stale and outputs', async () => {
    let writes = 0
    api.get = async (url: string) => {
      assert.equal(url, `/projects/${projectId}/bible-image-candidates/`)
      return { data: rawCandidates('succeeded', false) }
    }
    api.post = async () => { writes += 1; throw new Error('unexpected write') }

    const result = await repository.getBibleImageCandidates(projectId)
    assert.equal(writes, 0)
    assert.equal(result.candidates[0].entityType, 'character')
    assert.equal(result.promptConfig.stylePrompt, 'cinematic neo-wuxia')
    assert.equal(result.candidates[0].currentPromptInputs.positive[1].key, 'system.composition.character')
    assert.equal(result.candidates[0].latestCandidate?.generationTaskId, taskId)
    assert.equal(result.candidates[0].candidateHistory.length, 1)
    assert.equal(result.candidates[0].latestCandidate?.sourceCurrent, false)
    assert.deepEqual(result.candidates[0].latestCandidate?.quality.warnings, ['实体视觉字段较少。'])
    assert.equal(result.candidates[0].latestCandidate?.outputMedia[0].previewUrl, '/media/candidate.png')
    assert.throws(() => service.adaptBibleImageCandidates({ ...rawCandidates(), project_id: undefined, projectId }), /契约无效/)
  })

  await context.test('GET preserves all candidate history for side-by-side review', async () => {
    const olderPromptId = '77777777-7777-4777-8777-777777777777'
    const older = rawLatest('succeeded', true, {
      prompt_artifact_id: olderPromptId,
      generation_task_id: retryTaskId,
      idempotency_key: 'bible-image-history-old',
      output_media: [{
        index: 0,
        kind: 'image',
        filename: 'older.png',
        preview_url: '/media/older.png',
        download_url: '/media/older.png',
        ai_generated: true,
      }],
    })
    const latest = rawLatest('succeeded', true)
    const projection = service.adaptBibleImageCandidateProjection(
      rawProjection('succeeded', true, [latest, older]),
    )
    assert.equal(projection.latestCandidate?.promptArtifactId, promptId)
    assert.equal(projection.candidateHistory.length, 2)
    assert.equal(projection.candidateHistory[1].promptArtifactId, olderPromptId)
    assert.equal(projection.candidateHistory[1].outputMedia[0].previewUrl, '/media/older.png')
    assert.throws(
      () => service.adaptBibleImageCandidateProjection({
        ...rawProjection('succeeded', true, []),
        latest_candidate: latest,
      }),
      /契约无效/,
    )
  })

  await context.test('POST uses only the closed OpenAPI fields and advisory warnings never disable generation', async () => {
    let postedUrl = ''
    let postedPayload: Record<string, unknown> = {}
    api.post = async (url: string, payload: Record<string, unknown>) => {
      postedUrl = url
      postedPayload = payload
      return {
        data: {
          project_id: projectId,
          prompt_artifact_id: promptId,
          generated_workflow_id: generatedWorkflowId,
          generation_task_id: taskId,
          idempotent_replay: false,
          review_required: true,
          persisted_as_asset: false,
          candidate: rawProjection('queued', true),
        },
      }
    }

    const quality = service.adaptBibleImageCandidateQuality(rawLatest().quality)
    assert.equal(quality.advisoryOnly, true)
    assert.equal(quality.wouldBlockEnqueue, false)
    await repository.createBibleImageCandidate(projectId, createOptions())

    assert.equal(postedUrl, `/projects/${projectId}/bible-image-candidates/`)
    assert.deepEqual(postedPayload, {
      entity_type: 'character',
      entity_id: entityId,
      idempotency_key: idempotencyKey,
      extra_direction: '冷雨逆光，保持身份稳定',
      prompt_overrides: {},
      priority: 0,
      max_attempts: 3,
      extended_generation_confirmed: false,
    })
    assert.deepEqual(Object.keys(postedPayload).sort(), [
      'entity_id', 'entity_type', 'extended_generation_confirmed', 'extra_direction',
      'idempotency_key', 'max_attempts', 'priority', 'prompt_overrides',
    ])
    for (const forbidden of ['model', 'provider', 'template', 'template_id', 'workflow', 'workflow_id']) {
      assert.equal(forbidden in postedPayload, false)
    }
  })

  await context.test('queue confirmation and refresh recovery reuse the same persisted key', async () => {
    const state = service.createBibleImageCandidateRetryState(
      projectId,
      'character',
      entityId,
      '冷雨逆光，保持身份稳定',
      { 'project.style_prompt': 'manual style' },
      '77777777-7777-4777-8777-777777777777',
    )
    const restored = service.parseBibleImageCandidateRetryState(JSON.stringify({
      ...state,
      queueConfirmationRequired: true,
    }))
    assert.equal(restored?.idempotencyKey, state.idempotencyKey)
    assert.equal(restored?.extraDirection, state.extraDirection)
    assert.deepEqual(restored?.promptOverrides, { 'project.style_prompt': 'manual style' })

    const payloadBefore = service.bibleImageCandidateCreatePayload({ ...restored, extendedGenerationConfirmed: false })
    const payloadConfirmed = service.bibleImageCandidateCreatePayload({ ...restored, extendedGenerationConfirmed: true })
    assert.equal(payloadBefore.idempotency_key, payloadConfirmed.idempotency_key)
    assert.equal(payloadBefore.extra_direction, payloadConfirmed.extra_direction)
    assert.deepEqual(payloadBefore.prompt_overrides, { 'project.style_prompt': 'manual style' })
    assert.equal(payloadBefore.extended_generation_confirmed, false)
    assert.equal(payloadConfirmed.extended_generation_confirmed, true)

    let reads = 0
    api.get = async () => ({ data: rawCandidates(++reads === 1 ? 'queued' : 'succeeded', true) })
    assert.equal((await repository.getBibleImageCandidates(projectId)).candidates[0].latestCandidate?.taskStatus, 'queued')
    assert.equal((await repository.getBibleImageCandidates(projectId)).candidates[0].latestCandidate?.taskStatus, 'succeeded')
  })

  await context.test('task list adapter reads only the public top-level purpose and immutable target', () => {
    const rawTask = {
      id: taskId,
      project: projectId,
      status: 'succeeded',
      kind: 'image',
      created_at: '2026-08-15T10:00:00Z',
      generation_purpose: 'bible_image_candidate',
      bible_target: {
        schema_version: 1,
        reference_category: 'character',
        entity_id: entityId,
        entity_name: '沈砚',
        source_fingerprint: sourceFingerprint,
      },
      input_parameters: {
        generation_purpose: 'must-not-be-used',
      },
      capability_snapshot: { generation_purpose: 'must-not-be-used' },
      output_media: [],
    }
    const [task] = adapters.adaptTasks([rawTask])
    assert.equal(task.generationPurpose, 'bible_image_candidate')
    assert.deepEqual(task.bibleImageTarget, {
      schemaVersion: 1,
      referenceCategory: 'character',
      entityId,
      entityName: '沈砚',
      sourceFingerprint,
    })

    const [missingTarget] = adapters.adaptTasks([{ ...rawTask, bible_target: null }])
    assert.equal(missingTarget.generationPurpose, 'bible_image_candidate')
    assert.equal(missingTarget.bibleImageTarget, undefined)
    const [ordinary] = adapters.adaptTasks([{ ...rawTask, generation_purpose: null, bible_target: null }])
    assert.equal(ordinary.generationPurpose, undefined)
    assert.equal(ordinary.bibleImageTarget, undefined)
  })

  await context.test('task list and retry responses preserve the public purpose and target', async () => {
    const rawTask = {
      id: retryTaskId,
      project: projectId,
      status: 'queued',
      kind: 'image',
      created_at: '2026-08-15T10:05:00Z',
      generation_purpose: 'bible_image_candidate',
      bible_target: {
        schema_version: 1,
        reference_category: 'character',
        entity_id: entityId,
        entity_name: '沈砚',
        source_fingerprint: sourceFingerprint,
      },
      input_parameters: {},
      output_media: [],
    }
    api.get = async (url: string) => {
      assert.equal(url, '/tasks/')
      return { data: { count: 1, next: null, previous: null, results: [rawTask] } }
    }
    const [listed] = await repository.getWorkflows()
    assert.equal(listed.generationPurpose, 'bible_image_candidate')
    assert.equal(listed.bibleImageTarget?.entityId, entityId)

    api.post = async (url: string, payload: unknown) => {
      assert.equal(url, `/tasks/${taskId}/retry/`)
      assert.deepEqual(payload, { extended_generation_confirmed: true })
      return { data: rawTask }
    }
    const retried = await repository.retryGenerationTask(taskId, true)
    assert.equal(retried.id, retryTaskId)
    assert.equal(retried.generationPurpose, 'bible_image_candidate')
    assert.equal(retried.bibleImageTarget?.sourceFingerprint, sourceFingerprint)
  })

  await context.test('manual promotion sends only output index/name and incrementally appends without changing anchor', async () => {
    let postedPayload: unknown
    api.post = async (url: string, payload: unknown) => {
      assert.equal(url, `/tasks/${taskId}/promote-output-to-asset/`)
      postedPayload = payload
      return { data: {
        id: assetId,
        kind: 'character',
        name: '沈砚参考图',
        description: '',
        checksum: 'c'.repeat(64),
        preview_url: '/media/promoted.png',
        owner: 1,
        metadata: {},
      } }
    }
    const asset = await repository.promoteTaskOutputToAsset(taskId, 0, '沈砚参考图')
    assert.deepEqual(postedPayload, { output_index: 0, name: '沈砚参考图' })
    await repository.promoteTaskOutputToAsset(taskId, 1, '旧候选', undefined, { acceptStaleBibleImageCandidate: true })
    assert.deepEqual(postedPayload, {
      output_index: 1,
      name: '旧候选',
      accept_stale_bible_image_candidate: true,
    })

    const projection = service.adaptBibleImageCandidateProjection(rawProjection('succeeded', true))
    const anchor = { id: 'anchor-id' }
    projection.anchorAsset = anchor
    const bibleItem = { id: entityId, name: '沈砚', description: '', visualPrompt: '', anchorAssetId: null, referenceAssetIds: [], locked: true }
    const first = service.mergeBibleImagePromotion(projection, bibleItem, asset, 0, projection.candidateHistory[0])
    const second = service.mergeBibleImagePromotion(first, bibleItem, asset, 0, first.candidateHistory[0])
    const secondAsset = { ...asset, id: '55555555-5555-4555-8555-555555555555', checksum: 'd'.repeat(64) }
    const third = service.mergeBibleImagePromotion(second, bibleItem, secondAsset, 0, second.candidateHistory[0])
    assert.equal(first.anchorAsset, anchor)
    assert.equal(bibleItem.anchorAssetId, null)
    assert.deepEqual(bibleItem.referenceAssetIds, [assetId, secondAsset.id])
    assert.deepEqual(second.referenceAssets.map((item: { id: string }) => item.id), [assetId])
    assert.deepEqual(third.referenceAssets.map((item: { id: string }) => item.id), [assetId, secondAsset.id])
    assert.equal(second.latestCandidate?.persistedAsAsset, true)
    assert.equal(second.candidateHistory[0].persistedAsAsset, true)
    assert.equal(second.latestCandidate?.outputMedia[0].promotedAssetId, assetId)
  })

  await context.test('Bible adapter and sync keep the canonical anchor separate from promoted references', async () => {
    const item = adapters.adaptBibleItem({
      id: entityId,
      slug: 'shen-yan',
      stable_speaker_id: 'S1',
      name: '沈砚',
      description: '',
      visual_identity: 'stable identity',
      anchor_asset: anchorId,
      reference_assets: [assetId],
      is_locked: true,
      metadata: {},
    })
    assert.equal(item.anchorAssetId, anchorId)
    assert.deepEqual(item.referenceAssetIds, [assetId])

    let syncPayload: Record<string, any> | undefined
    api.post = async (url: string, payload: Record<string, any>) => {
      assert.equal(url, `/projects/${projectId}/sync-bibles/`)
      syncPayload = payload
      return { data: {
        updated_at: '2026-08-19T10:00:01Z',
        characters: [{
          id: entityId,
          slug: 'shen-yan',
          stable_speaker_id: 'S1',
          name: '沈砚',
          description: '',
          visual_identity: 'stable identity',
          anchor_asset: anchorId,
          reference_assets: [assetId],
          is_locked: true,
          metadata: {},
        }],
        scenes: [],
        props: [],
      } }
    }
    await adapters.syncBible({
      id: projectId,
      updatedAt: '2026-08-19T10:00:00Z',
      bible: { characters: [item], scenes: [], props: [] },
    } as any)
    assert.equal(syncPayload?.expected_updated_at, '2026-08-19T10:00:00Z')
    assert.equal(syncPayload?.characters[0].anchor_asset, anchorId)
    assert.deepEqual(syncPayload?.characters[0].reference_assets, [assetId])
  })
})

test('studio and task UI keep generation, confirmation, refresh and promotion explicit', () => {
  const studioView = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  const workflowView = readFileSync(new URL('../src/views/WorkflowView.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')

  const submitStart = studioView.indexOf('const submitBibleImageCandidate = async')
  const submitEnd = studioView.indexOf('\nconst generateBibleImageCandidate', submitStart)
  const submit = studioView.slice(submitStart, submitEnd)
  assert.ok(submit.includes('repository.createBibleImageCandidate'))
  assert.ok(!submit.includes('promoteTaskOutputToAsset'))
  assert.ok(studioView.includes('@click="promoteBibleImageOutput(bibleImageCandidateFor(item),candidate,media,index)"'))
  assert.ok(studioView.includes('const resolveSavedBibleImageItem = async'))
  assert.ok(studioView.includes('fresh = await studio.loadProject(currentProject.id, true)'))
  assert.ok(studioView.includes('当前实体有未保存编辑或服务端版本已变化；请先保存并刷新 Bible 后再生成参考图。'))
  assert.ok(studioView.includes('const generateAllUnconfirmedBibleImageCandidates = async'))
  assert.ok(studioView.includes('一键生成未确认参考图'))
  assert.ok(studioView.includes('candidateHistory'))
  assert.ok(studioView.includes('候选参考图完整预览'))
  assert.ok(studioView.includes('手工调整提示词 / 补充创作要求'))
  assert.ok(studioView.includes('实体参考图提示词默认配置'))
  assert.ok(studioView.includes('本次参考图提示词来源'))
  assert.ok(studioView.includes('bibleImagePromptOverridesFor'))
  assert.ok(studioView.includes('promptOverrides: state.promptOverrides'))
  assert.ok(studioView.includes('createBibleImageCandidateRetryState('))
  assert.ok(studioView.includes('bibleImageRandomId()'))
  assert.ok(studioView.includes("bibleImageCandidateErrorCode(error) === 'production_queue_confirmation_required'"))
  assert.ok(studioView.includes('queueConfirmationRequired: true'))
  assert.ok(studioView.includes('persistBibleImageRetry(pending)'))
  assert.ok(studioView.includes('sourceCurrent'))
  assert.ok(studioView.includes('quality.warnings'))
  assert.ok(studioView.includes('打开任务'))
  assert.ok(studioView.includes('刷新状态'))
  assert.ok(studioView.includes('bibleImageOutputActionLabel'))
  assert.ok(studioView.includes('已绑定参考图'))
  assert.ok(studioView.includes('重新绑定参考图'))
  assert.ok(studioView.includes('仍加入参考图'))
  assert.ok(studioView.includes('confirmStaleBibleImageCandidatePromotion'))
  assert.ok(studioView.includes('acceptStaleBibleImageCandidate: !latestCandidate.sourceCurrent'))
  assert.ok(!studioView.includes('旧候选不能确认'))
  assert.ok(studioView.includes('不会删除素材或候选历史'))

  const bindingSave = studioView.slice(
    studioView.indexOf('const saveBibleAssets = async'),
    studioView.indexOf('\nconst episodeDuration', studioView.indexOf('const saveBibleAssets = async')),
  )
  assert.ok(bindingSave.includes('repository.getProject(projectId)'))
  assert.ok(bindingSave.includes('for (let attempt = 0; attempt < 3; attempt += 1)'))
  assert.ok(bindingSave.includes('isProjectExpectedUpdatedAtConflict(error)'))
  assert.ok(!bindingSave.includes('studio.loadProject(projectId, true)'))
  assert.ok(bindingSave.includes('target.anchorAssetId = anchorAssetId'))
  assert.ok(bindingSave.includes('target.referenceAssetIds = referenceAssetIds'))
  assert.ok(bindingSave.includes('saved = await studio.saveProject(fresh)'))
  assert.ok(bindingSave.includes('候选历史保持不变'))
  assert.ok(!bindingSave.includes('await save(true)') || bindingSave.includes('!bibleImagePersistedId(item.id)'))
  assert.ok(studioView.includes('@click="saveBibleAssets(item,assetTypeForBible())"'))

  const promotionCall = studioView.slice(
    studioView.indexOf('const promoteBibleImageOutput = async'),
    studioView.indexOf('\nconst handleVisualRulesApplied'),
  )
  assert.ok(promotionCall.includes('repository.promoteTaskOutputToAsset('))
  assert.ok(promotionCall.includes('latestCandidate.generationTaskId,\n      outputIndex,\n      `${candidate.entityName}参考图`,\n      undefined,\n      { acceptStaleBibleImageCandidate: !latestCandidate.sourceCurrent },\n    )'))
  assert.ok(promotionCall.includes('const refreshed = await studio.loadProject(candidateProjectId, true)'))
  assert.ok(promotionCall.includes('project.value.updatedAt = refreshed.updatedAt || project.value.updatedAt'))
  assert.ok(promotionCall.includes('localBibleItem.referenceAssetIds = [...refreshedBibleItem.referenceAssetIds]'))
  assert.ok(!promotionCall.includes('referenceCategory'))
  assert.ok(!promotionCall.includes('entityId:'))

  assert.ok(workflowView.includes("workflow?.generationPurpose === 'bible_image_candidate'"))
  assert.ok(workflowView.includes('outputPromotionSelection.bibleImageTarget'))
  assert.ok(workflowView.includes('selectedBibleImageTargetMissing'))
  assert.ok(workflowView.includes('任务数据完整性错误：该实体参考图候选没有服务端冻结目标'))
  assert.ok(workflowView.includes("selectedBibleImageTargetMissing?'冻结目标缺失'"))
  assert.ok(workflowView.includes('分类和实体来自任务持久化快照，当前页面不能改选'))
  assert.ok(workflowView.includes('确认请求只发送 output_index 与可选名称，不发送分类或实体'))
  assert.ok(workflowView.includes('bibleImageSourceCurrent'))
  assert.ok(workflowView.includes('acceptStaleBibleImageCandidate'))
  assert.ok(workflowView.includes('仍可作为历史参考图加入'))
  assert.ok(workflowView.includes('await repository.getBibleImageCandidates(workflow.projectId)'))
  assert.ok(workflowView.includes('studio.loadProject(selection.projectId,true)'))
  assert.ok(!workflowView.includes('Promise.all([studio.refreshAssets(),studio.refreshProjects()])'))
  assert.ok(repository.includes('/bible-image-candidates/'))
  assert.ok(!repository.includes('bible-image-candidate/'))
})
