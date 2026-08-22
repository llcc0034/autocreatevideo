import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  adaptShotDetailDraft,
  adaptShotDetailDraftApplyResult,
  adaptShotDetailDraftBatchApplyResult,
  adaptShotDetailDraftBatchDetail,
  shotDetailDraftDataPayload,
  shotDetailDraftEditorWarnings,
  shotDetailDraftIsEditable,
  shotDetailDraftRequestError,
  shotDetailDraftReviewIssueTitle,
} from '../src/services/shotDetailDraft.ts'
import { adaptShotDetailedDescription, shotDetailedDescriptionPayload } from '../src/services/shotDetailSync.ts'
import { legacyShotVersionKey, normalizeShotVersionScopeKey, wholeEpisodeRetryStorageKey } from '../src/services/shotVersionScope.ts'
import type { Shot } from '../src/types/index.ts'

const rawDraft = {
  id: 'detail-1', project: 'project-1', shot: 'shot-1', status: 'draft', revision: 3,
  generation_no: 2, context_hash: 'detail-context', context_current: true,
  variation_note: '加强雨水反光', can_edit: true, can_regenerate: true, can_confirm: true, can_apply: false,
  validation: { status: 'passed', errors: [], warnings: ['需核对右手持剑'] },
  draft_data: {
    schema_version: 1,
    detail_zh: '雨水沿着剑鞘滑落，人物推门入栈后停在门内，镜头缓慢推进。',
    detail_en: 'Rain runs down the sword sheath as the subject enters the inn and stops just inside; the camera makes one slow push-in.',
  },
}

test('single-shot detail adapter keeps bilingual candidate isolated from compact fields', () => {
  const draft = adaptShotDetailDraft(rawDraft)
  assert.equal(draft.shotId, 'shot-1')
  assert.equal(draft.draftData.detailZh.startsWith('雨水'), true)
  assert.match(draft.draftData.detailEn, /slow push-in/)
  assert.equal(draft.validation.status, 'passed')
  assert.equal(shotDetailDraftIsEditable(draft), true)
  assert.equal(shotDetailDraftIsEditable({ ...draft, contextCurrent: false }), false)
})

test('parseable high-detail candidates retain structured review issues for human confirmation', () => {
  const candidate = adaptShotDetailDraft({
    ...rawDraft,
    validation: {
      status: 'needs_review',
      errors: ['raw parser diagnostic must not be rendered'],
      warnings: [],
      review_issues: [{
        code: 'motion_direction_mismatch', severity: 'risk', path: 'detail_en',
        suggestion_zh: '核对镜头向右移动是否与中文候选一致。',
      }],
      summary: { issue_count: 1, risk_count: 1, requires_edit_count: 0, status: 'needs_review' },
    },
    can_confirm: true,
  })
  assert.equal(candidate.validation.status, 'needs_review')
  assert.equal(candidate.reviewSummary.status, 'needs_review')
  assert.equal(candidate.reviewIssues[0].field, 'detailEn')
  assert.match(shotDetailDraftReviewIssueTitle(candidate.reviewIssues[0]), /方位|动作/)
  assert.equal(candidate.reviewIssues[0].suggestionZh, '核对镜头向右移动是否与中文候选一致。')
})

test('high-detail protocol salvage and missing-language issues use accurate operator copy', () => {
  assert.match(shotDetailDraftReviewIssueTitle({
    code: 'model_output_protocol_validation_failed', severity: 'requires_edit',
    path: 'result', field: 'detailZh',
  }), /模型已返回可编辑正文/)
  assert.match(shotDetailDraftReviewIssueTitle({
    code: 'detail_en_missing', severity: 'requires_edit',
    path: 'detail_en', field: 'detailEn',
  }), /候选内容不完整/)
})

test('single-shot detail payload and local checks use capability policy limits', () => {
  const draft = adaptShotDetailDraft(rawDraft)
  const payload = shotDetailDraftDataPayload(draft.draftData)
  assert.equal(payload.schema_version, 1)
  assert.equal(payload.detail_zh, draft.draftData.detailZh)
  assert.equal(shotDetailDraftEditorWarnings(draft.draftData).length, 0)
  const policy = {
    recommendedZhChars: 500, recommendedEnChars: 750,
    transportZhMaxChars: 3000, transportEnMaxChars: 6000,
  }
  assert.ok(shotDetailDraftEditorWarnings({
    schemaVersion: 1, detailZh: '中'.repeat(501), detailEn: 'a'.repeat(751),
  }, policy).some((message) => message.includes('500')))
  assert.ok(shotDetailDraftEditorWarnings({
    schemaVersion: 1, detailZh: '中'.repeat(501), detailEn: 'a'.repeat(751),
  }, policy).some((message) => message.includes('750')))
  assert.ok(shotDetailDraftEditorWarnings({
    schemaVersion: 1, detailZh: '中'.repeat(3001), detailEn: 'a'.repeat(6001),
  }, policy).some((message) => message.includes('应用前配置阈值 3000')))
  assert.equal(shotDetailDraftEditorWarnings({
    schemaVersion: 1, detailZh: '中'.repeat(501), detailEn: 'a'.repeat(751),
  }).length, 0)
})

test('apply response exposes only the authoritative formal shot patch', () => {
  const result = adaptShotDetailDraftApplyResult({
    draft: { ...rawDraft, status: 'applied', can_edit: false, can_apply: false },
    applied_shot: {
      id: 'shot-1', detailed_description: 'A reviewed English high-detail direction.',
      metadata: { shot_detail_draft_id: 'detail-1', applied_language_mode: 'bilingual_confirmed' },
    },
    translation_written: true,
  })
  assert.equal(result.appliedShot.id, 'shot-1')
  assert.equal(result.appliedShot.detailedDescription, 'A reviewed English high-detail direction.')
  assert.equal(result.translationWritten, true)
  assert.equal(result.appliedShot.metadata?.shot_detail_draft_id, 'detail-1')
})

test('ordinary shot sync preserves an applied high-detail field unless a local value explicitly owns it', () => {
  const baseShot = {
    id: '11111111-1111-4111-8111-111111111111', order: 1, title: '雨夜入栈',
    mediaType: 'video', duration: 5, mode: 'T2VA', framing: '中景', camera: 'slow push-in',
    action: 'She enters the inn and stops.', prompt: '', negativePrompt: '',
    characterIds: [], propIds: [], referenceAssetIds: [], audioEnabled: true,
    audio: { dialogue: '', soundscape: '', music: '' }, status: 'draft',
  } satisfies Shot
  const legacyShot: Shot = { ...baseShot }
  const undefinedField: Shot = { ...baseShot, detailedDescription: undefined }
  const explicitClear: Shot = { ...baseShot, detailedDescription: '' }
  const appliedEnglish: Shot = { ...baseShot, detailedDescription: 'A reviewed English high-detail direction.' }

  // This is the ordinary sync envelope boundary: only a present string is sent.
  assert.deepEqual({ shots: [shotDetailedDescriptionPayload(legacyShot)] }, { shots: [{}] })
  assert.deepEqual({ shots: [shotDetailedDescriptionPayload(undefinedField)] }, { shots: [{}] })
  assert.deepEqual({ shots: [shotDetailedDescriptionPayload(explicitClear)] }, { shots: [{ detailed_description: '' }] })
  assert.deepEqual({ shots: [shotDetailedDescriptionPayload(appliedEnglish)] }, {
    shots: [{ detailed_description: 'A reviewed English high-detail direction.' }],
  })
  assert.deepEqual(adaptShotDetailedDescription({}), {})
  assert.deepEqual(adaptShotDetailedDescription({ detailed_description: '' }), { detailedDescription: '' })

  const adapters = readFileSync(new URL('../src/services/backendAdapters.ts', import.meta.url), 'utf8')
  assert.ok(adapters.includes('...shotDetailedDescriptionPayload(shot)'))
  assert.ok(adapters.includes('...adaptShotDetailedDescription(raw)'))
  assert.ok(!adapters.includes('detailed_description: shot.detailedDescription ||'))
})

test('single-shot detail errors keep resource contention and formal-shot safety explicit', () => {
  const busy = shotDetailDraftRequestError({
    response: { data: { error: { code: 'shot_detail_resource_busy', details: { detail: 'busy' } } } },
  }, 'fallback')
  assert.match(busy, /资源暂时占用/)
  assert.match(busy, /单镜可稍后重试/)
  assert.match(busy, /整集扩写会入队/)
  assert.match(busy, /正式分镜未修改/)
  const stale = shotDetailDraftRequestError({ response: { data: { code: 'shot_detail_draft_conflict' } } }, 'fallback')
  assert.match(stale, /上下文已变化/)
  const raw = shotDetailDraftRequestError({
    response: { data: { code: 'unknown', detail: 'model candidate and parser offset must stay private' } },
  }, 'safe fallback')
  assert.equal(raw, 'safe fallback')
})

test('episode detail batch adapter preserves frozen before snapshots and atomic candidate revisions', () => {
  const result = adaptShotDetailDraftBatchDetail({
    batch: {
      id: 'batch-1', project: 'project-1', episode: 'episode-1', request_id: 'request-1',
      shot_version_key: 'creative-draft:version-1', shot_version_label: 'V1',
      version_scope_status: 'resolved',
      provider: 'deepseek_api', generation_status: 'completed', apply_status: 'not_started',
      revision: 4, total_count: 2, pending_count: 0, succeeded_count: 1,
      failed_count: 0, stale_count: 0, skipped_count: 1, applied_count: 0,
      policy_snapshot: { recommended_max_shots: 16 },
    },
    items: [{
      id: 'item-1', batch: 'batch-1', shot: 'shot-1', ordinal: 1, status: 'succeeded',
      attempts: 1, max_attempts: 2,
      before_snapshot: { target_shot: { title: '冻结标题', camera: 'slow push' } },
      draft: rawDraft,
    }, {
      id: 'item-2', batch: 'batch-1', shot: 'shot-image', ordinal: 2, status: 'skipped',
      attempts: 0, max_attempts: 2, before_snapshot: { shot: { title: '图片镜头' } },
    }],
    pagination: { count: 2, next: null, previous: null },
    applied_shots: [],
    translation_written: false,
  })
  assert.equal(result.batch.provider, 'deepseek_api')
  assert.equal(result.batch.shotVersionKey, 'creative-draft:version-1')
  assert.equal(result.batch.shotVersionLabel, 'V1')
  assert.equal(result.batch.versionScopeStatus, 'resolved')
  assert.equal(result.batch.revision, 4)
  assert.equal(result.items[0].beforeSnapshot.targetShot.title, '冻结标题')
  assert.equal(result.items[0].draft?.revision, 3)
  assert.equal(result.items[1].status, 'skipped')
  assert.deepEqual(result.appliedShots, [])
  assert.equal(result.translationWritten, false)
})

test('storyboard version scope uses one deterministic initial-version key', () => {
  assert.equal(legacyShotVersionKey('episode-1'), 'legacy:episode-1:initial')
  assert.equal(normalizeShotVersionScopeKey('', 'episode-1'), 'legacy:episode-1:initial')
  assert.equal(
    normalizeShotVersionScopeKey(' creative-draft:version-2 ', 'episode-1'),
    'creative-draft:version-2',
  )
  assert.notEqual(
    wholeEpisodeRetryStorageKey('episode-1', 'creative-draft:version-1'),
    wholeEpisodeRetryStorageKey('episode-1', 'creative-draft:version-2'),
  )
})

test('unscoped historical batches remain audit-only instead of falling into current version', () => {
  const result = adaptShotDetailDraftBatchDetail({
    batch: {
      id: 'legacy-unknown', project: 'project-1', episode: 'episode-1',
      request_id: 'request-unknown', provider: 'local_qwen',
      generation_status: 'completed', apply_status: 'complete', revision: 1,
      total_count: 6, applied_count: 6,
    },
    items: [], pagination: { count: 0, next: null, previous: null },
    applied_shots: [], translation_written: false,
  })
  assert.equal(result.batch.shotVersionKey, '')
  assert.equal(result.batch.versionScopeStatus, 'legacy_unknown')
  assert.notEqual(result.batch.shotVersionKey, legacyShotVersionKey('episode-1'))
})

test('episode detail batch detail exposes committed shots for lost-apply-response recovery', () => {
  const result = adaptShotDetailDraftBatchDetail({
    batch: {
      id: 'batch-applied', project: 'project-1', episode: 'episode-1', request_id: 'request-2',
      provider: 'deepseek_api', generation_status: 'completed', apply_status: 'complete',
      revision: 6, total_count: 1, applied_count: 1,
    },
    items: [],
    pagination: { count: 0, next: null, previous: null },
    applied_shots: [{
      id: 'shot-1', detailed_description: 'Authoritative recovered detail.',
      metadata: { shot_detail_provenance: { applied_language_mode: 'bilingual_confirmed' } },
    }],
    translation_written: true,
  })
  assert.equal(result.appliedShots[0].detailedDescription, 'Authoritative recovered detail.')
  assert.equal(result.translationWritten, true)
})

test('episode detail apply uses the committed formal shots returned by the server', () => {
  const result = adaptShotDetailDraftBatchApplyResult({
    batch: {
      id: 'batch-1', project: 'project-1', episode: 'episode-1', request_id: 'request-1',
      provider: 'deepseek_api', generation_status: 'completed', apply_status: 'complete',
      revision: 5, total_count: 1, applied_count: 1,
    },
    items: [],
    applied_shots: [{
      id: 'shot-1', detailed_description: '数据库最终写入的英文描述',
      metadata: { shot_detail_provenance: { shot_detail_draft_id: 'detail-1' } },
    }],
    translation_written: true,
  })
  assert.equal(result.appliedShots[0].id, 'shot-1')
  assert.equal(result.appliedShots[0].detailedDescription, '数据库最终写入的英文描述')
  assert.equal(result.translationWritten, true)
})

test('Storyboard uses one current-shot drawer, retains stale-response protection, and invalidates the prompt after apply', () => {
  const panel = readFileSync(new URL('../src/components/ShotDetailDraftPanel.vue', import.meta.url), 'utf8')
  const storyboard = readFileSync(new URL('../src/views/StoryboardView.vue', import.meta.url), 'utf8')
  const adapters = readFileSync(new URL('../src/services/backendAdapters.ts', import.meta.url), 'utf8')
  const translations = readFileSync(new URL('../src/services/translationReview.ts', import.meta.url), 'utf8')
  const workbench = readFileSync(new URL('../src/components/CreativeDraftWorkbench.vue', import.meta.url), 'utf8')
  const episodeModal = readFileSync(new URL('../src/components/EpisodeDetailDraftModal.vue', import.meta.url), 'utf8')
  assert.ok(panel.includes('let loadSequence = 0'))
  assert.ok(panel.includes('let actionSequence = 0'))
  assert.ok(panel.includes('const stillCurrent = () => requestSequence === loadSequence'))
  assert.ok(panel.includes('if (!isShotCurrent(token.shotKey)) { emit(\'applied\', result); return }'))
  assert.ok(panel.includes('已应用到当前正式镜头'))
  assert.ok(panel.includes('采用中文，转上方翻译审核'))
  assert.ok(panel.includes('只扩写当前镜头的“镜头运动、动作与画面描述”'))
  assert.ok(panel.includes('reviewAcknowledged'))
  assert.ok(panel.includes('detail-review-issues'))
  assert.ok(!panel.includes("saved.validation.status !== 'passed'"))
  assert.ok(storyboard.includes('<ShotDetailDraftPanel v-if="selectedShot.mediaType===\'video\'"'))
  assert.ok(storyboard.includes('@applied="applyShotDetailDraft"'))
  assert.ok(storyboard.includes("fieldKey:'shot.detailed_description'"))
  assert.ok(storyboard.includes("invalidatePrompt(target, 'inputs_changed')"))
  assert.ok(adapters.includes('...shotDetailedDescriptionPayload(shot)'))
  assert.ok(adapters.includes('shot_version_key: shotVersionKey'))
  assert.ok(adapters.includes('normalizeShotVersionScopeKey(episode.activeShotVersionKey, episode.id)'))
  assert.ok(translations.includes("field.fieldKey === 'shot.detailed_description'"))
  assert.ok(!workbench.includes('ShotDetailDraftPanel'))
  assert.ok(storyboard.includes('class="episode-tool-group"'))
  assert.ok(storyboard.indexOf('整集扩写') < storyboard.indexOf('整集应用'))
  assert.ok(storyboard.indexOf('整集应用') < storyboard.indexOf('生成全部提示词'))
  assert.ok(episodeModal.includes('batchDetailAll'))
  assert.ok(storyboard.includes(':shot-version-key="selectedShotVersionScopeKey"'))
  assert.ok(episodeModal.includes('props.projectId, props.episode.id, props.shotVersionKey'))
  assert.ok(episodeModal.includes('next.batch.versionScopeStatus !== \'resolved\''))
  assert.ok(episodeModal.includes('next.batch.shotVersionKey !== props.shotVersionKey'))
  assert.ok(episodeModal.includes('props.projectId, props.episode.id, props.shotVersionKey'))
  assert.ok(episodeModal.includes('detail.value = null'))
  assert.ok(episodeModal.includes("next.batch.applyStatus === 'complete'"))
  assert.ok(episodeModal.includes('appliedShots: next.appliedShots'))
  assert.ok(episodeModal.includes('expectedBatchRevision'))
  assert.ok(episodeModal.includes('itemId: item.id, draftId: item.draft!.id, expectedRevision: item.draft!.revision'))
  assert.ok(episodeModal.includes('当前后端没有逐镜重试接口'))
  assert.ok(panel.includes('void loadCapability(); void load()'))
  assert.ok(panel.includes('provider.temporarilyBusy'))
  assert.ok(panel.includes(':disabled="!provider.ready"'))
  assert.ok(panel.includes('资源占用，请稍后重试'))
  assert.ok(!panel.includes('资源占用，可排队'))
})

test('detail repository forwards explicit review acknowledgement for confirm and apply', () => {
  const repository = readFileSync(new URL('../src/services/shotDetailDraftRepository.ts', import.meta.url), 'utf8')
  assert.ok(repository.includes('async confirm(id: string, expectedRevision: number, reviewAcknowledged = false)'))
  assert.ok(repository.includes('review_acknowledged: reviewAcknowledged'))
  assert.ok(repository.includes('review_acknowledged: options.reviewAcknowledged === true'))
  assert.ok(repository.includes('params: { episode: episodeId, shot_version_key: shotVersionKey'))
  assert.ok(repository.includes('shot_version_key: options.shotVersionKey'))
  assert.ok(repository.includes('current.reviewSummary.requiresEditCount'))
  assert.ok(repository.includes('VITE_SHOT_DETAIL_DRAFT_TIMEOUT_MS'))
  assert.ok(!repository.includes('720_000'))
})
