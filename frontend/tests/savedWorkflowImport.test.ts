import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
const server = () => createServer({
  root: frontendRoot,
  logLevel: 'silent',
  appType: 'custom',
  server: { middlewareMode: true },
})

test('source image resolution is read-only evidence, not an editable width default', async () => {
  const vite = await server()
  try {
    const { workflowResolutionSummary } = await vite.ssrLoadModule('/src/services/workflowResolution.ts')
    assert.equal(workflowResolutionSummary(undefined), '')
    assert.equal(workflowResolutionSummary({ resolution_strategy: { mode: 'explicit_width_height' } }), '')
    assert.match(workflowResolutionSummary({ resolution_strategy: { mode: 'source_image_derived' } }), /尚未形成完整执行合同/)
    const summary = workflowResolutionSummary({ resolution_strategy: {
      mode: 'source_image_derived', adapter: 'comfy.flux_kontext_image_scale/v1', asset_semantic_keys: ['asset.image.source'],
    } })
    assert.match(summary, /asset.image.source/)
    assert.match(summary, /无需运行时手填宽高/)
    assert.match(summary, /生成后核对产物/)
    const page = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
    assert.match(page, /workflowResolutionSummary\(savedWorkflowDrafts/)
    assert.match(page, /review_workflow_contract: '查看匹配诊断/)
  } finally { await vite.close() }
})

class MemoryStorage {
  values = new Map<string, string>()
  getItem(key: string) { return this.values.get(key) || null }
  setItem(key: string, value: string) { this.values.set(key, value) }
  removeItem(key: string) { this.values.delete(key) }
}

class FailingStorage extends MemoryStorage {
  failure: 'set' | 'remove'
  constructor(failure: 'set' | 'remove') { super(); this.failure = failure }
  override setItem(key: string, value: string) {
    if (this.failure === 'set') throw new Error('quota sentinel')
    super.setItem(key, value)
  }
  override removeItem(key: string) {
    if (this.failure === 'remove') throw new Error('remove sentinel')
    super.removeItem(key)
  }
}

test('model-context saved workflow view separates exact, eligible, review, contract-blocked, and diagnostic rows', async () => {
  const vite = await server()
  try {
    const contextView = await vite.ssrLoadModule('/src/services/savedWorkflowModelContext.ts')
    const context = {
      modelProfileVersionId: 'profile-current',
      providerId: 'provider-current',
      workflowVersionIds: new Set(['workflow-current']),
    }
    const candidate = {
      modelProfileVersionId: 'profile-current', profileKey: 'current', version: 1,
      status: 'published', modality: 'video', targetDeploymentId: 'deployment-current',
      eligible: true, eligibleOutputNodeIds: ['123'], rejectionCodes: [],
      liveValidateReady: true, liveValidateRejectionCodes: [],
    }
    const base = {
      sourceProviderId: 'provider-current', matchedModelProfileVersionId: '', workflowVersionId: '',
      errorCode: '', suggestedImportKind: 'template_version', matchStatus: 'unmatched',
      analysisStatus: 'complete', actionability: 'ready_for_executable_import',
      outputCandidates: [{ canvasNodeId: '123', nodeType: 'SaveVideo', suggestedOutputKind: 'video' }],
      modelProfileCandidates: [candidate],
    }
    const classify = (overrides = {}) => contextView.classifySavedWorkflowModelContext(
      { ...base, ...overrides }, context,
    )

    assert.equal(classify({ matchedModelProfileVersionId: 'profile-current', modelProfileCandidates: [] }), 'exact')
    assert.equal(classify({
      sourceProviderId: 'PROVIDER-CURRENT',
      matchedModelProfileVersionId: 'PROFILE-CURRENT',
      modelProfileCandidates: [],
    }), 'exact', 'server identifiers are normalized before exact evidence comparison')
    assert.equal(classify({ workflowVersionId: 'workflow-current', matchStatus: 'blocked', modelProfileCandidates: [] }), 'exact')
    assert.equal(classify(), 'eligible_candidate')
    assert.equal(classify({
      modelProfileCandidates: [{
        ...candidate, status: 'draft', targetDeploymentId: '', liveValidateReady: false,
        liveValidateRejectionCodes: ['model_profile_not_published', 'compatible_deployment_missing'],
      }],
    }), 'review_candidate')
    const contractBlockedItem = {
      analysisStatus: 'blocked',
      actionability: 'candidate_only',
      modelProfileCandidates: [{
        ...candidate,
        eligible: false,
        eligibleOutputNodeIds: [],
        rejectionCodes: ['unsupported_dynamic_workflow_family', 'required_dimension_multiple_unresolved'],
        liveValidateReady: false,
      }],
    }
    assert.equal(
      classify(contractBlockedItem),
      'contract_blocked_candidate',
      'the exact published model remains visible when only the registered structural projector blocks it',
    )
    assert.equal(
      contextView.savedWorkflowBelongsToModelContext({ ...base, ...contractBlockedItem }, context),
      true,
      'a contract-blocked candidate belongs in the current-model scan view while candidate admission remains separate',
    )
    for (const rejectionCodes of [
      ['required_dimension_multiple_unresolved'],
      ['unsupported_dynamic_workflow_family', 'unexpected_contract_drift'],
      ['workflow_profile_projection_failed'],
    ]) {
      assert.equal(classify({
        analysisStatus: 'blocked', actionability: 'candidate_only',
        modelProfileCandidates: [{
          ...candidate, eligible: false, eligibleOutputNodeIds: [], rejectionCodes,
          liveValidateReady: false,
        }],
      }), 'contract_blocked_candidate', 'the browser follows server-owned actionability instead of a hard-coded Projector blocker list')
    }
    assert.equal(classify({
      modelProfileCandidates: [{
        ...candidate, eligible: false, eligibleOutputNodeIds: [],
        rejectionCodes: ['model_artifact_attestation_required'],
      }],
    }), 'diagnostic', 'a published but ineligible same-provider profile is not positive model evidence')
    assert.equal(classify({
      modelProfileCandidates: [{ ...candidate, status: 'draft', liveValidateReady: false }],
    }), 'review_candidate', 'contract compatibility and Live Validate readiness are separate facts')
    assert.equal(classify({
      matchedModelProfileVersionId: 'profile-other',
      workflowVersionId: 'workflow-current',
      matchStatus: 'matched',
    }), 'diagnostic', 'an exact association to another model must win over a current workflow link and candidate')
    assert.equal(classify({ sourceProviderId: 'provider-other' }), 'diagnostic')
    assert.equal(classify({ modelProfileCandidates: [] }), 'diagnostic')
    assert.equal(classify({
      modelProfileCandidates: [{ ...candidate, eligible: false, eligibleOutputNodeIds: [], rejectionCodes: ['model_profile_modality_mismatch'] }],
    }), 'diagnostic')
    assert.equal(classify({ outputCandidates: [{ canvasNodeId: '123', nodeType: 'SaveImage', suggestedOutputKind: 'image' }] }), 'diagnostic')
    assert.equal(classify({
      suggestedImportKind: 'execution_snapshot',
      matchStatus: 'matched',
      matchedModelProfileVersionId: 'profile-current',
    }), 'diagnostic', 'a linked execution snapshot remains read-only diagnostic evidence')
    assert.equal(classify({
      suggestedImportKind: 'execution_snapshot',
      matchStatus: 'unmatched',
    }), 'diagnostic', 'an unlinked execution snapshot remains read-only diagnostic evidence')
    assert.equal(classify({
      suggestedImportKind: 'execution_snapshot',
      matchStatus: 'unmatched',
      workflowVersionId: 'workflow-current',
    }), 'diagnostic', 'an execution snapshot cannot become exact through an inconsistent workflow link')
    assert.equal(classify({
      errorCode: 'source_read_failed',
      matchedModelProfileVersionId: 'profile-current',
      workflowVersionId: 'workflow-current',
    }), 'diagnostic', 'a failed scan row cannot become exact through stale association evidence')
    assert.equal(classify({
      modelProfileCandidates: [{
        ...candidate, status: 'draft', modality: 'multimodal', targetDeploymentId: '',
        liveValidateReady: false, liveValidateRejectionCodes: ['model_profile_not_published'],
      }],
    }), 'review_candidate')
    assert.equal(contextView.savedWorkflowBelongsToModelContext({ ...base }, context), true)
  } finally { await vite.close() }
})

test('saved workflow batches use only a server-returned effective limit and aggregate every result', async () => {
  const vite = await server()
  try {
    const batches = await vite.ssrLoadModule('/src/services/savedWorkflowBatch.ts')
    const items = Array.from({ length: 45 }, (_, index) => ({
      sourcePath: `saved/${index}.json`,
      expectedSha256: 'a'.repeat(64),
      expectedSourceScopeSchemaVersion: 1,
      expectedSourceScopeSha256: 'b'.repeat(64),
      importKind: 'ignored',
    }))
    assert.throws(
      () => batches.chunkSavedWorkflowBatch(items, null),
      /服务端未返回有效的工作流批量上限/,
    )
    const chunks = batches.chunkSavedWorkflowBatch(items, 17)
    assert.deepEqual(chunks.map((chunk: unknown[]) => chunk.length), [17, 17, 11])
    const grouped = batches.groupSavedWorkflowBatches([
      ...items.slice(0, 19).map((item: unknown) => ({ sourceDeploymentId: 'deployment-1', item })),
      ...items.slice(19, 25).map((item: unknown) => ({ sourceDeploymentId: 'deployment-2', item })),
    ], 17)
    assert.deepEqual(grouped.map((group: { deploymentId: string, items: unknown[] }) => [group.deploymentId, group.items.length]), [
      ['deployment-1', 17], ['deployment-1', 2], ['deployment-2', 6],
    ])
    const result = (deploymentId: string, offset: number, outcomes: Array<'created' | 'reused' | 'failed'>) => ({
      schemaVersion: 1,
      deploymentId,
      status: 'complete',
      createdCount: 0,
      reusedCount: 0,
      failedCount: 0,
      items: outcomes.map((outcome, index) => ({
        sourceDeploymentId: deploymentId,
        sourcePath: `saved/${offset + index}.json`, sourceSha256: 'a'.repeat(64),
        sourceId: `source-${offset + index}`, importKind: 'ignored', result: outcome,
        code: '', detail: '', workflowVersionId: '',
      })),
    })
    const first = batches.mergeSavedWorkflowBatchResults(null, result('deployment-1', 0, ['created', 'reused']))
    const merged = batches.mergeSavedWorkflowBatchResults(first, result('deployment-2', 2, ['failed', 'created']))
    assert.equal(merged.status, 'partial')
    assert.deepEqual(merged.deploymentIds, ['deployment-1', 'deployment-2'])
    assert.throws(
      () => batches.mergeSavedWorkflowBatchResults(null, {
        ...result('deployment-1', 0, ['created']),
        items: [{
          ...result('deployment-1', 0, ['created']).items[0],
          sourceDeploymentId: 'deployment-2',
        }],
      }),
      /权威来源 deployment 不一致/,
    )
    batches.assertSavedWorkflowBatchResultMatchesRequest(
      'deployment-1',
      items.slice(0, 2),
      result('deployment-1', 0, ['created', 'reused']),
    )
    for (const invalid of [
      { ...result('deployment-1', 0, ['created', 'reused']), items: result('deployment-1', 0, ['created']).items },
      { ...result('deployment-1', 0, ['created', 'reused']), items: [result('deployment-1', 0, ['created']).items[0], result('deployment-1', 0, ['created']).items[0]] },
      { ...result('deployment-1', 0, ['created', 'reused']), items: [{ ...result('deployment-1', 0, ['created']).items[0], sourceSha256: 'c'.repeat(64) }, result('deployment-1', 1, ['reused']).items[0]] },
      { ...result('deployment-1', 0, ['created', 'reused']), items: [{ ...result('deployment-1', 0, ['created']).items[0], importKind: 'candidate' }, result('deployment-1', 1, ['reused']).items[0]] },
    ]) {
      assert.throws(
        () => batches.assertSavedWorkflowBatchResultMatchesRequest(
          'deployment-1', items.slice(0, 2), invalid,
        ),
        /请求项目集合不一致/,
      )
    }
    assert.deepEqual(
      [merged.createdCount, merged.reusedCount, merged.failedCount, merged.items.length],
      [2, 1, 1, 4],
    )
  } finally { await vite.close() }
})

test('saved workflow review draft persists only safe fields and exact source revision', async () => {
  const vite = await server()
  try {
    const sessions = await vite.ssrLoadModule('/src/services/savedWorkflowDraftSession.ts')
    const storage = new MemoryStorage()
    storage.values.set('autoprompt.saved-workflow-import.v1.user-1', JSON.stringify({ smokeAssetIds: { old: 'asset-1' } }))
    storage.values.set('autoprompt.saved-workflow-import.v2.user-1', JSON.stringify({ schemaVersion: 2 }))
    const row = {
      sourceScopeSchemaVersion: 1,
      sourceScopeSha256: 'a'.repeat(64),
      sourceSha256: 'b'.repeat(64),
      selected: true,
      importKind: 'template_version',
      primaryOutputNodeId: '123',
      modelProfileVersionId: 'profile-1',
      useExistingTemplate: false,
      templateId: '',
      newTemplateName: 'Qwen 图像工作流',
      newTemplateSlug: 'qwen-image',
      outputKind: 'image',
      objectInfoSchemaSha256: 'c'.repeat(64),
      convertedGraphSha256: 'd'.repeat(64),
      parameterSchemaSha256: 'e'.repeat(64),
      structuralContractSha256: 'f'.repeat(64),
      preview: { raw_prompt: 'must-not-persist' },
      attestation: { request: 'must-not-persist' },
    }
    assert.equal(sessions.writeSavedWorkflowDraftSession(
      storage,
      'user-1',
      'deployment-1',
      {
        'image/qwen.json': row,
        'audio/h3.json': {
          ...row,
          sourceSha256: '1'.repeat(64),
          newTemplateName: 'H3 音频工作流',
          newTemplateSlug: 'h3-audio',
          outputKind: 'audio',
        },
      },
      new Date('2026-08-24T00:00:00Z'),
    ), true)
    const restored = sessions.readSavedWorkflowDraftSession(storage, 'user-1', 'deployment-1')
    assert.equal(restored.items['image/qwen.json'].sourceSha256, 'b'.repeat(64))
    assert.equal(restored.items['image/qwen.json'].sourceScopeSchemaVersion, 1)
    assert.equal(restored.items['image/qwen.json'].sourceScopeSha256, 'a'.repeat(64))
    assert.equal(sessions.savedWorkflowDraftMatchesSource(
      restored.items['image/qwen.json'],
      { sourceScopeSchemaVersion: 1, sourceScopeSha256: 'a'.repeat(64), sourceSha256: 'b'.repeat(64) },
    ), true)
    assert.equal(sessions.savedWorkflowDraftMatchesSource(
      restored.items['image/qwen.json'],
      { sourceScopeSchemaVersion: 1, sourceScopeSha256: '9'.repeat(64), sourceSha256: 'b'.repeat(64) },
    ), false)
    assert.equal(restored.items['image/qwen.json'].newTemplateSlug, 'qwen-image')
    assert.equal('preview' in restored.items['image/qwen.json'], false)
    assert.equal('attestation' in restored.items['image/qwen.json'], false)
    assert.equal(restored.items['image/qwen.json'].objectInfoSchemaSha256, 'c'.repeat(64))
    assert.equal(restored.items['image/qwen.json'].convertedGraphSha256, 'd'.repeat(64))
    assert.equal(restored.items['image/qwen.json'].parameterSchemaSha256, 'e'.repeat(64))
    assert.equal(restored.items['image/qwen.json'].structuralContractSha256, 'f'.repeat(64))
    assert.equal(restored.items['audio/h3.json'].outputKind, 'audio')
    assert.equal(JSON.stringify(restored).includes('smokeAssetIds'), false)
    assert.equal(JSON.stringify(restored).includes('smokeParameterValues'), false)
    assert.equal(JSON.stringify([...storage.values.values()]).includes('must-not-persist'), false)
    assert.equal(storage.values.has('autoprompt.saved-workflow-import.v1.user-1'), false)
    assert.equal(storage.values.has('autoprompt.saved-workflow-import.v2.user-1'), false)
    assert.equal(sessions.readSavedWorkflowDraftSession(storage, 'other-user', 'deployment-1'), null)
  } finally { await vite.close() }
})

test('hydration guard protects the stored session until a delayed scan is merged', async () => {
  const vite = await server()
  try {
    const sessions = await vite.ssrLoadModule('/src/services/savedWorkflowDraftSession.ts')
    const storage = new MemoryStorage()
    const valid = {
      sourceScopeSchemaVersion: 1, sourceScopeSha256: 'd'.repeat(64),
      sourceSha256: 'e'.repeat(64), selected: true, importKind: 'template_version',
      primaryOutputNodeId: '123', modelProfileVersionId: 'profile-1', useExistingTemplate: false,
      templateId: '', newTemplateName: 'Persisted', newTemplateSlug: 'persisted', outputKind: 'image',
      objectInfoSchemaSha256: 'f'.repeat(64), convertedGraphSha256: '1'.repeat(64),
      parameterSchemaSha256: '2'.repeat(64), structuralContractSha256: '3'.repeat(64),
    }
    sessions.writeSavedWorkflowDraftSession(storage, 'user-1', 'deployment-1', { 'safe/one.json': valid })
    const guard = sessions.createSavedWorkflowHydrationGuard()
    let release!: () => void
    const delayed = new Promise<void>((resolve) => { release = resolve })
    const hydration = guard.run(async () => { await delayed })
    assert.equal(guard.active(), true)
    if (!guard.active()) sessions.writeSavedWorkflowDraftSession(storage, 'user-1', 'deployment-1', {})
    assert.ok(sessions.readSavedWorkflowDraftSession(storage, 'user-1', 'deployment-1'))
    release(); await hydration
    assert.equal(guard.active(), false)
  } finally { await vite.close() }
})

test('attestation recovery requires the exact persisted review identity', async () => {
  const vite = await server()
  try {
    const sessions = await vite.ssrLoadModule('/src/services/savedWorkflowDraftSession.ts')
    const snapshot = {
      source_deployment_id: 'deployment-1', source_path: 'safe/one.json',
      source_scope_schema_version: 1, source_scope_sha256: 'f'.repeat(64),
      source_sha256: 'a'.repeat(64), target_model_profile_version_id: 'profile-1',
      primary_output_node_ids: ['123'], object_info_schema_sha256: 'b'.repeat(64),
      converted_graph_sha256: 'c'.repeat(64),
    }
    const review = {
      deploymentId: 'deployment-1', sourceScopeSchemaVersion: 1,
      sourceScopeSha256: 'f'.repeat(64), sourcePath: 'safe/one.json', sourceSha256: 'a'.repeat(64),
      modelProfileVersionId: 'profile-1', primaryOutputNodeId: '123',
      objectInfoSchemaSha256: 'b'.repeat(64), convertedGraphSha256: 'c'.repeat(64),
    }
    assert.equal(sessions.attestationMatchesSavedWorkflowReview(snapshot, review), true)
    assert.equal(sessions.attestationMatchesSavedWorkflowReview(
      { ...snapshot, converted_graph_sha256: 'd'.repeat(64) }, review,
    ), false)
    assert.equal(sessions.attestationMatchesSavedWorkflowReview(
      { ...snapshot, target_model_profile_version_id: 'old-profile' }, review,
    ), false)
  } finally { await vite.close() }
})

test('restored review contracts require explicit confirmation when hashes are missing or changed', async () => {
  const vite = await server()
  try {
    const sessions = await vite.ssrLoadModule('/src/services/savedWorkflowDraftSession.ts')
    const currentObjectInfo = 'a'.repeat(64)
    const currentGraph = 'b'.repeat(64)
    const current = {
      objectInfoSchemaSha256: currentObjectInfo,
      convertedGraphSha256: currentGraph,
      parameterSchemaSha256: 'c'.repeat(64),
      structuralContractSha256: 'd'.repeat(64),
    }
    assert.equal(sessions.reconcileSavedWorkflowReviewHashes(current, current), 'exact')
    assert.equal(sessions.reconcileSavedWorkflowReviewHashes({
      ...current, structuralContractSha256: '',
    }, current), 'missing')
    assert.equal(sessions.reconcileSavedWorkflowReviewHashes({
      ...current, parameterSchemaSha256: 'e'.repeat(64),
    }, current), 'changed')
  } finally { await vite.close() }
})

test('server integer contract boundary survives an exact Node JSON round trip', () => {
  const unsafeSource = '{"max":18446744073709551615}'
  assert.equal(
    JSON.stringify(JSON.parse(unsafeSource)),
    '{"max":18446744073709552000}',
  )
  const contract = {
    parameters: {
      'sampling.seed': {
        type: 'integer',
        min: -Number.MAX_SAFE_INTEGER,
        max: Number.MAX_SAFE_INTEGER,
      },
    },
  }
  assert.deepEqual(JSON.parse(JSON.stringify(contract)), contract)
})

test('saved workflow import freezes only the server-derived structural contract', async () => {
  const source = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const apiSource = await readFile(new URL('../src/services/platformApi.ts', import.meta.url), 'utf8')
  const typesSource = await readFile(new URL('../src/services/platformTypes.ts', import.meta.url), 'utf8')
  const editorSource = await readFile(new URL('../src/views/WorkflowVersionEditorView.vue', import.meta.url), 'utf8')
  assert.match(source, /!draft\.reviewRequiresConfirmation/)
  assert.match(source, /reviewRequiresConfirmation = true/)
  assert.match(source, /configurableParameters/)
  assert.match(source, /expectedParameterSchemaSha256/)
  assert.match(source, /expectedStructuralContractSha256/)
  assert.match(source, /item\.contractUpgradeRequired/)
  assert.match(source, /draft\.templateId === item\.templateId/)
  assert.match(
    source,
    /const previewSavedWorkflow[\s\S]*draft\.reviewRequiresConfirmation = true[\s\S]*platformApi\.previewComfySavedWorkflow/,
  )
  assert.match(source, /draft\.preview\.primaryOutputNodeIds\[0\] === draft\.primaryOutputNodeId/)
  assert.match(typesSource, /suggestedOutputKind: '' \| 'image' \| 'video' \| 'audio' \| 'text'/)
  assert.match(source, /待 Projector 解析/)
  assert.match(source, /登记所需模型/)
  assert.doesNotMatch(source, /新建同服务提供方（Provider）模型草稿/)
  assert.match(source, /const openSavedWorkflowModelDraft[\s\S]*openEditor\('model'\)[\s\S]*provider/)
  assert.match(
    apiSource,
    /expected_parameter_schema_sha256:\s*item\.expectedParameterSchemaSha256/,
  )
  assert.match(apiSource, /expected_structural_contract_sha256:\s*item\.expectedStructuralContractSha256/)
  for (const retired of ['input_contract: item.inputContract', 'semantic_bindings: item.semanticBindings', 'smoke_inputs', 'smoke_asset_bindings']) {
    assert.equal(apiSource.includes(retired) || source.includes(retired), false, retired)
  }
  assert.match(apiSource, /\/assets\/workflow-smoke-candidates\//)
  assert.match(editorSource, /ResourceSmokeCreateForm/)
  assert.ok(editorSource.replace(/\s/g, '').includes("router.push({name:'workflows',query:{category:'smoke',kind:'preparation',id}})"))
  assert.match(editorSource, /smokeAttempt\.fixtureSha256/)
  assert.doesNotMatch(source, /\/settings\/workflows\/new/)
  assert.doesNotMatch(apiSource, /importWorkflowVersion/)
  for (const retired of ['VueFlow', '导入 JSON', '画布结构与添加节点']) assert.equal(editorSource.includes(retired), false, retired)
})

test('saved workflow import separates promotability from selection and exposes blocker recovery', async () => {
  const source = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const promotableStart = source.indexOf('const savedWorkflowPromotable =')
  const selectionStart = source.indexOf('const savedWorkflowReady =', promotableStart)
  const batchStart = source.indexOf('const savedWorkflowBatchPayload =', selectionStart)
  assert.ok(promotableStart >= 0)
  assert.ok(selectionStart > promotableStart)
  assert.ok(batchStart > selectionStart)

  const promotable = source.slice(promotableStart, selectionStart)
  const selection = source.slice(selectionStart, batchStart)
  for (const gate of [
    "draft.preview?.actionability === 'ready_for_executable_import'",
    'hardSavedWorkflowBlockers(draft).length === 0',
    'savedWorkflowIsContextReadOnly(item)',
  ]) assert.ok(promotable.includes(gate), `promotability misses ${gate}`)
  assert.match(selection, /\?\.selected && savedWorkflowPromotable\(item\)/)

  for (const marker of [
    '定位第一个阻断项',
    'focusFirstSavedWorkflowBlocker',
    'saved-workflow-promote-help',
    '查看合同技术证据',
    'class="saved-workflow-technical"',
    '你需要：{{ savedWorkflowRecoveryLabel',
  ]) assert.ok(source.includes(marker), `saved workflow recovery UX misses ${marker}`)
  assert.doesNotMatch(source, /<details[^>]+class="saved-workflow-technical"[^>]+open/)
})

test('Projector management exposes create, successor, and reviewed lifecycle actions', async () => {
  const source = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const createBlock = source.slice(
    source.indexOf('const submitWorkflowProjectorCreate ='),
    source.indexOf('const openWorkflowProjectorEditor ='),
  )
  const createFamilyIndex = createBlock.indexOf('await platformApi.createWorkflowProjector(')
  const createV1Index = createBlock.indexOf('await platformApi.createWorkflowProjectorVersion(')
  assert.ok(createFamilyIndex >= 0)
  assert.ok(createV1Index > createFamilyIndex, 'a new stable family is created before its v1 draft')
  assert.match(source, /新建方案族与 v1 草稿/)
  assert.match(source, /createNextWorkflowProjectorVersion/)
  assert.match(source, /openAction\('publish-workflow-projector'/)
  assert.match(source, /openAction\('discard-workflow-projector'/)
  assert.match(source, /openAction\('retire-workflow-projector'/)
  assert.match(source, /openAction\('workflow-projector-status'/)
  assert.match(source, /放弃 v\{\{ version\.version \}\} 草稿/)
  assert.match(source, /platformApi\.discardWorkflowProjectorVersion/)
  assert.match(source, /确认放弃草稿/)
  assert.doesNotMatch(source, /@click="publishWorkflowProjectorVersion/)
  assert.doesNotMatch(source, /@click="retireWorkflowProjectorVersion/)
})

test('matched and imported candidates can request a dedicated Projector and immutable successor', async () => {
  const source = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const recommendationStart = source.indexOf('const savedWorkflowCanRecommendProjector =')
  const recommendationEnd = source.indexOf('const savedWorkflowCanReprojectAsSuccessor =', recommendationStart)
  const recommendationGate = source.slice(recommendationStart, recommendationEnd)
  assert.ok(recommendationStart >= 0)
  assert.match(recommendationGate, /auth\.user\?\.isSuperuser/)
  assert.match(recommendationGate, /item\.candidateManaged/)
  assert.match(recommendationGate, /item\.candidateSourceId/)
  assert.match(recommendationGate, /draft\?\.primaryOutputNodeId/)
  assert.doesNotMatch(recommendationGate, /blockers|matchStatus/)

  const successorStart = source.indexOf('const reprojectSavedWorkflowAsSuccessor =')
  const successorEnd = source.indexOf('const refreshSavedWorkflowContract =', successorStart)
  const successorFlow = source.slice(successorStart, successorEnd)
  assert.ok(successorStart >= 0)
  assert.match(successorFlow, /draft\.useExistingTemplate = true/)
  assert.match(successorFlow, /draft\.templateId = item\.templateId/)
  assert.match(successorFlow, /draft\.projectorSuccessorRequested = true/)
  assert.match(successorFlow, /draft\.successorOf \|\|= item\.workflowVersionId/)
  assert.match(successorFlow, /await previewSavedWorkflow\(item\)/)

  assert.match(source, /基于此候选生成专用 Projector 建议/)
  assert.match(source, /用当前已发布 Projector 生成后继合同/)
  assert.match(source, /当前工作流已冻结自动匹配的 Projector/)
  assert.match(source, /item\.contractUpgradeRequired\s*\|\|\s*draft\.projectorSuccessorRequested/)
  assert.doesNotMatch(source, /savedWorkflowNeedsProjectorRecommendation/)
})

test('only an explicit successor unlocks a matched import model target', async () => {
  const vite = await server()
  try {
    const { savedWorkflowModelSelectionLocked } = await vite.ssrLoadModule('/src/services/savedWorkflowModelContext.ts')
    assert.equal(savedWorkflowModelSelectionLocked({ matchStatus: 'matched' }, { projectorSuccessorRequested: false }), true)
    assert.equal(savedWorkflowModelSelectionLocked({ matchStatus: 'matched' }, { projectorSuccessorRequested: true }), false)
    assert.equal(savedWorkflowModelSelectionLocked({ matchStatus: 'unmatched' }, { projectorSuccessorRequested: false }), false)
    const source = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
    assert.match(source, /savedWorkflowModelSelectionLocked\(item,savedWorkflowDrafts\[savedWorkflowKey\(item\)\]\)/)
    assert.match(source, /@change="refreshSavedWorkflowContract\(item\)"/)
  } finally { await vite.close() }
})

test('successor promotion pins its predecessor instead of using the refreshed scan version', async () => {
  const source = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const payload = source.slice(source.indexOf('const savedWorkflowBatchPayload ='), source.indexOf('const batchImportSavedWorkflows ='))
  assert.match(payload, /successorOf: draft\.projectorSuccessorRequested \? draft\.successorOf : null/)
  assert.doesNotMatch(payload, /item\.workflowVersionId/)
  assert.match(source, /本批结果可能未知/)
  const api = await readFile(new URL('../src/services/platformApi.ts', import.meta.url), 'utf8')
  assert.match(api, /successor_of: item\.successorOf \|\| null/)
  const editor = await readFile(new URL('../src/views/WorkflowVersionEditorView.vue', import.meta.url), 'utf8')
  assert.match(editor, /仅重新验收时可在导入中心用当前已发布 Projector 生成后继合同/)
  assert.doesNotMatch(editor, /如需重新验收，请从 ComfyUI 保存新 revision/)
})

test('storage write and removal failures are explicit to the caller', async () => {
  const vite = await server()
  try {
    const sessions = await vite.ssrLoadModule('/src/services/savedWorkflowDraftSession.ts')
    const valid = {
      sourceScopeSchemaVersion: 1, sourceScopeSha256: 'b'.repeat(64),
      sourceSha256: 'a'.repeat(64), selected: false, importKind: 'ignored',
      primaryOutputNodeId: '', modelProfileVersionId: '', useExistingTemplate: false,
      templateId: '', newTemplateName: 'Safe', newTemplateSlug: 'safe', outputKind: 'text',
      objectInfoSchemaSha256: '', convertedGraphSha256: '', parameterSchemaSha256: '', structuralContractSha256: '',
    }
    assert.equal(sessions.writeSavedWorkflowDraftSession(
      new FailingStorage('set'), 'user-1', 'deployment-1', { 'safe/one.json': valid },
    ), false)
    assert.equal(sessions.writeSavedWorkflowDraftSession(
      new FailingStorage('remove'), 'user-1', 'deployment-1', {},
    ), false)
    assert.ok(sessions.SAVED_WORKFLOW_STORAGE_ERROR.includes('浏览器'))
  } finally { await vite.close() }
})

test('corrupt saved workflow draft rows fail closed without destroying other deployments', async () => {
  const vite = await server()
  try {
    const sessions = await vite.ssrLoadModule('/src/services/savedWorkflowDraftSession.ts')
    const storage = new MemoryStorage()
    const valid = {
      sourceScopeSchemaVersion: 1, sourceScopeSha256: 'b'.repeat(64),
      sourceSha256: 'c'.repeat(64), selected: false, importKind: 'ignored',
      primaryOutputNodeId: '', modelProfileVersionId: '', useExistingTemplate: false,
      templateId: '', newTemplateName: 'Safe', newTemplateSlug: 'safe-workflow', outputKind: 'text',
      objectInfoSchemaSha256: '', convertedGraphSha256: '', parameterSchemaSha256: '', structuralContractSha256: '',
    }
    sessions.writeSavedWorkflowDraftSession(storage, 'user-1', 'deployment-1', { 'safe/one.json': valid })
    sessions.writeSavedWorkflowDraftSession(storage, 'user-1', 'deployment-2', {
      '../escape.json': valid,
      'safe/two.json': { ...valid, sourceSha256: 'not-a-hash' },
    })
    assert.equal(sessions.readSavedWorkflowDraftSession(storage, 'user-1', 'deployment-2'), null)
    assert.ok(sessions.readSavedWorkflowDraftSession(storage, 'user-1', 'deployment-1'))
  } finally { await vite.close() }
})
