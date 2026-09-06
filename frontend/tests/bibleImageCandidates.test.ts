import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

test('Bible image candidates expose read-only history without create or promotion controls', () => {
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  const studio = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  const service = readFileSync(new URL('../src/services/bibleImageCandidates.ts', import.meta.url), 'utf8')
  assert.match(repository, /api\.get\(\`\/projects\/\$\{projectId\}\/bible-image-candidates\/\`\)/)
  assert.doesNotMatch(repository, /api\.post\([^\n]*bible-image-candidates|createBibleImageCandidate|promoteTaskOutputToAsset/)
  assert.doesNotMatch(studio, /repository\.(createBibleImageCandidate|promoteTaskOutputToAsset)/)
  assert.doesNotMatch(studio, /@click="(?:generateAllUnconfirmedBibleImageCandidates|generateBibleImageCandidate|promoteBibleImageOutput)/)
  assert.doesNotMatch(service, /bibleImageCandidateCreatePayload|CandidateRetryState|mergeBibleImagePromotion/)
  assert.match(studio, /只读历史/)
})

test('ProductionRun candidate preview and selection trust only verified history facts', () => {
  const view = readFileSync(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8')
  const match = view.match(
    /const bibleCandidatePreview = \(artifactId: string, expectedSha256: string\) => \{([\s\S]*?)\n\}\nconst bibleEntityKey/,
  )
  assert.ok(match, 'Bible candidate preview helper must remain a single auditable gate')
  const body = match[1]
  assert.match(body, /bibleImageHistoryByArtifactId\.value\[artifactId\]/)
  assert.match(body, /history\.sha256 === expectedSha256/)
  assert.match(body, /history\.sourceCurrent/)
  assert.match(body, /history\.integrityStatus === 'valid'/)
  assert.match(body, /\? history\.previewUrl/)
  assert.doesNotMatch(body, /currentRun|\.artifacts|previewUrl\s*\|\|/)
  assert.match(
    view,
    /:disabled="!bibleCandidatePreview\(candidate\.artifactId,candidate\.sha256\)"/,
  )
})

test('ProductionRun distinguishes preview verification from a verified missing preview', () => {
  const view = readFileSync(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8')
  const image = view.indexOf('<img v-if="bibleCandidatePreview(candidate.artifactId,candidate.sha256)"')
  const loading = view.indexOf('<div v-else-if="bibleImageHistoryLoading" class="bible-image-review-placeholder" role="status"><LoaderCircle class="spin" :size="28"/><span>正在校验预览…</span></div>', image)
  const unavailable = view.indexOf('<div v-else class="bible-image-review-placeholder"><Image :size="28"/><span>预览不可用，禁止盲选</span></div>', loading)
  assert.ok(image >= 0, 'verified preview remains the first rendering branch')
  assert.ok(loading > image, 'history loading must render a preview verification state')
  assert.ok(unavailable > loading, 'blind-selection warning must only render after loading completes')
})

test('materialized candidate remains readable when it is no longer bound', async (context) => {
  const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
  const server = await createServer({
    root: frontendRoot,
    logLevel: 'silent',
    appType: 'custom',
    server: { middlewareMode: true, hmr: false },
  })
  context.after(async () => { await server.close() })
  const { adaptBibleImageCandidates } = await server.ssrLoadModule('/src/services/bibleImageCandidates.ts')
  const projectId = '11111111-1111-4111-8111-111111111111'
  const entityId = '22222222-2222-4222-8222-222222222222'
  const artifactId = '33333333-3333-4333-8333-333333333333'
  const assetId = '44444444-4444-4444-8444-444444444444'
  const candidate = {
    artifact_id: artifactId,
    production_run_id: '55555555-5555-4555-8555-555555555555',
    stage_run_id: '66666666-6666-4666-8666-666666666666',
    generation_attempt_id: null,
    generation_task_id: null,
    prompt_artifact_id: null,
    workflow_version_id: null,
    model_profile_version_id: null,
    model_identifier: '',
    prompt_profile_version_id: null,
    prompt_profile_binding_key: '',
    prompt_profile_contract_sha256: '',
    source_snapshot_sha256: 'a'.repeat(64),
    source_current: true,
    integrity_status: 'valid',
    integrity_error_code: '',
    variant_group: 'bible:character:candidate:1',
    variant_index: 1,
    status: 'selected',
    binding_role: 'unbound',
    materialized_asset: {
      id: assetId,
      kind: 'character',
      name: '候选 C',
      checksum: 'b'.repeat(64),
      preview_url: '/assets/c',
    },
    preview_url: '/candidate/c',
    download_url: '/candidate/c/download',
    sha256: 'b'.repeat(64),
    size_bytes: 100,
    mime_type: 'image/png',
    created_at: '2026-08-24T00:00:00Z',
  }
  const result = adaptBibleImageCandidates({
    project_id: projectId,
    candidates: [{
      entity_type: 'character',
      entity_id: entityId,
      entity_name: '弟弟',
      anchor_asset: null,
      reference_assets: [],
      latest_candidate: candidate,
      candidate_history: [candidate],
    }],
    counts: { characters: 1, scenes: 0, props: 0, total: 1, with_candidate: 1 },
  })

  assert.equal(result.candidates[0].latestCandidate?.bindingRole, 'unbound')
  assert.equal(result.candidates[0].latestCandidate?.materializedAsset?.id, assetId)
})

test('Bible approval freezes library Asset checksums and regeneration has a distinct idempotent API', async (context) => {
  const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
  const server = await createServer({
    root: frontendRoot,
    logLevel: 'silent',
    appType: 'custom',
    server: { middlewareMode: true, hmr: false },
  })
  context.after(async () => { await server.close() })
  const { api } = await server.ssrLoadModule('/src/services/api.ts')
  const { bibleImageBindingsPayload, platformApi } = await server.ssrLoadModule('/src/services/platformApi.ts')
  const entityId = '11111111-1111-4111-8111-111111111111'
  const artifactId = '22222222-2222-4222-8222-222222222222'
  const assetId = '33333333-3333-4333-8333-333333333333'
  const runId = '44444444-4444-4444-8444-444444444444'
  const requestId = '55555555-5555-4555-8555-555555555555'
  const expectedSha256 = 'a'.repeat(64)
  const sourceSnapshotSha256 = 'b'.repeat(64)

  assert.deepEqual(bibleImageBindingsPayload([{
    entityType: 'character',
    entityId,
    selectedArtifactIds: [artifactId],
    selectedLibraryAssets: [{ assetId, expectedSha256 }],
    anchorLibraryAssetId: assetId,
  }]), [{
    entity_type: 'character',
    entity_id: entityId,
    selected_artifact_ids: [artifactId],
    selected_library_assets: [{ asset_id: assetId, expected_sha256: expectedSha256 }],
    anchor_artifact_id: null,
    anchor_library_asset_id: assetId,
  }])

  const originalPost = api.post
  context.after(() => { api.post = originalPost })
  let capturedUrl = ''
  let capturedPayload: unknown = null
  api.post = async (url: string, payload: unknown) => {
    capturedUrl = url
    capturedPayload = payload
    return { data: { id: runId, project: '', created_at: '', updated_at: '' } }
  }
  await platformApi.regenerateBibleImages(runId, {
    requestId,
    expectedSourceSnapshotSha256: sourceSnapshotSha256,
    entities: [{ entityType: 'character', entityId }],
  })
  assert.equal(capturedUrl, `/production-runs/${runId}/regenerate-bible-images/`)
  assert.deepEqual(capturedPayload, {
    request_id: requestId,
    expected_source_snapshot_sha256: sourceSnapshotSha256,
    entities: [{ entity_type: 'character', entity_id: entityId }],
  })
})

test('regenerated Bible review snapshot keeps old and new candidates visible', async (context) => {
  const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
  const server = await createServer({
    root: frontendRoot,
    logLevel: 'silent',
    appType: 'custom',
    server: { middlewareMode: true, hmr: false },
  })
  context.after(async () => { await server.close() })
  const { parseBibleImageReviewPayload } = await server.ssrLoadModule('/src/services/productionRunReview.ts')
  const entityId = '11111111-1111-4111-8111-111111111111'
  const generationStageId = '22222222-2222-4222-8222-222222222222'
  const candidate = (index: number, historical: boolean) => ({
    artifact_id: `${index + 2}3333333-3333-4333-8333-333333333333`,
    variant_group: `bible:character:${entityId}:candidate:${index}`,
    variant_index: index,
    status: 'available',
    sha256: String(index).repeat(64),
    attempt_id: `${index + 4}5555555-5555-4555-8555-555555555555`,
    prompt_artifact_id: `${index + 6}7777777-7777-4777-8777-777777777777`,
    prompt_profile_version_id: `${index + 7}8888888-8888-4888-8888-888888888888`,
    prompt_profile_binding_key: 'bible-character-image',
    source_snapshot_sha256: 'c'.repeat(64),
    origin_production_run_id: `${index + 1}2222222-2222-4222-8222-222222222222`,
    historical,
  })
  const parsed = parseBibleImageReviewPayload([{
    id: generationStageId,
    ordinal: 8,
    stageKey: 'bible_image_generation',
    status: 'succeeded',
    outputSnapshotSha256: 'd'.repeat(64),
    outputSnapshot: {
      stage_key: 'bible_image_generation',
      formal_object_type: 'bible_image_candidate_manifest',
      canonical: {
        generation_stage_id: generationStageId,
        entities: [{
          entity_type: 'character',
          entity_id: entityId,
          entity_name: '林雾',
          source_snapshot_sha256: 'c'.repeat(64),
          candidates: [candidate(1, true), candidate(2, false)],
        }],
      },
    },
  }])
  assert.equal(parsed.error, '')
  assert.equal(parsed.value?.entities[0].candidates.length, 2)
  assert.deepEqual(parsed.value?.entities[0].candidates.map((item: { historical: boolean }) => item.historical), [true, false])
})

test('Bible review UI excludes project covers and does not reuse generic retry for regeneration', () => {
  const view = readFileSync(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8')
  assert.match(view, /asset\.metadata\?\.usage !== 'project_cover'/)
  assert.match(view, /expectedSha256: bibleLibraryAsset\(assetId\)\?\.checksum \|\| ''/)
  assert.match(view, /anchorLibraryAssetId: selection\.anchorAssetId \|\| undefined/)
  assert.match(view, /requestId: crypto\.randomUUID\(\)/)
  assert.match(view, /platformApi\.regenerateBibleImages/)
  const confirmation = view.match(/const confirmBibleRegeneration = async \(\) => \{([\s\S]*?)\n\}\nconst requestRunConfirmation/)
  assert.ok(confirmation)
  assert.doesNotMatch(confirmation[1], /retryProductionRun|requestRunConfirmation\('retry'\)/)
})

test('SeriesStudio exposes a configured blocking cross-shot continuity readiness gate', () => {
  const view = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  for (const fragment of [
    'bibleContinuityEntities',
    'bibleMissingAnchorEntities',
    'bibleEntitiesWithoutAnyImage',
    'conditioningModeByKey(conditioningModes.value, shot.mode)?.requiresEntityReferenceBible',
    '跨镜头一致性就绪状态',
    '实体参考图未就绪 · 阻断',
    '其他动态输入都不能替代实体参考图',
    '相同随机种子（Seed）',
    '使用本次 Bible 选择的模型生成或重新生成候选，也可从素材库选择',
    '前往 ProductionRun 补齐参考图',
  ]) assert.match(view, new RegExp(fragment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')), fragment)
  assert.match(view, /bibleContinuityEntities\.value\.length > 0 && bibleMissingAnchorEntities\.value\.length === 0/)
  assert.match(view, /:to="`\/projects\/\$\{project\.id\}\/production`"/)
  assert.doesNotMatch(view, /@click="[^"]*(?:generate|regenerate).*Bible/i)
})
