import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  acceptAuthoritativeCanonicalGlossary,
  adaptShotTranslationPromotionResult,
  adaptCanonicalGlossaryEnsureResult,
  adaptTranslationEntry,
  applyPromotedFieldsToShot,
  applyShotTranslationPatch,
  bibleCanonicalTargetCollisionMessage,
  bibleCanonicalTargetCollisions,
  confirmedTranslationForPrompt,
  createTranslationLoadGate,
  dialogueDeliveryTranslationFields,
  formatTranslationGlossary,
  missingBibleCanonicalNames,
  parseTranslationGlossary,
  reconcileTranslationDrafts,
  translationReviewRequestError,
  translationReviewStatus,
} from '../src/services/translationReview.ts'
import { promptArtifactNeedsRegeneration, translationAppliesToShot } from '../src/services/promptArtifactFreshness.ts'
import { adaptPromptNormalizationCapability } from '../src/services/promptNormalization.ts'
import type { H3PromptPayload, PromptTranslationEntry, PromptTranslationField, Shot } from '../src/types/index.ts'

test('prompt normalization adapter preserves an arbitrary ready local deployment id', () => {
  const capability = adaptPromptNormalizationCapability({
    schema_version: 2,
    capability_key: 'prompt.normalize',
    review_required: true,
    default_model_deployment_id: '11111111-1111-4111-8111-111111111111',
    configuration_public_sha256: 'a'.repeat(64),
    deployments: [{
      model_deployment_id: '11111111-1111-4111-8111-111111111111',
      deployment_key: 'third-party-normalizer',
      label: '第三方标准化模型',
      provider_key: 'third-local-compatible',
      provider_label: 'Third Local Compatible',
      provider_driver: 'openai_compatible',
      model_profile_version_id: '22222222-2222-4222-8222-222222222222',
      model_identifier: 'vendor/prompt-normalizer-v3',
      capability: 'prompt.normalize',
      state: 'ready',
      ready: true,
      reason_code: 'ready',
      transport: 'local_endpoint',
      data_egress: false,
      data_egress_notice: '',
      estimated_cost: '0.03',
      cost_currency: 'USD',
      cost_basis: 'fixed_request',
      smoke_snapshot_id: '33333333-3333-4333-8333-333333333333',
      smoke_expires_at: '2026-08-24T00:00:00Z',
    }],
  })
  assert.equal(capability.defaultModelDeploymentId, '11111111-1111-4111-8111-111111111111')
  assert.equal(capability.deployments[0].modelDeploymentId, '11111111-1111-4111-8111-111111111111')
  assert.equal(capability.deployments[0].ready, true)
  assert.equal(capability.deployments[0].transport, 'local_endpoint')
  assert.equal(capability.deployments[0].dataEgress, false)
})

test('prompt normalization adapter exposes external deployments only as unavailable', () => {
  const capability = adaptPromptNormalizationCapability({
    schema_version: 2,
    capability_key: 'prompt.normalize',
    review_required: true,
    default_model_deployment_id: null,
    configuration_public_sha256: 'c'.repeat(64),
    deployments: [{
      model_deployment_id: '66666666-6666-4666-8666-666666666666',
      deployment_key: 'external-normalizer',
      label: 'External normalizer',
      provider_key: 'external-compatible',
      provider_label: 'External Compatible',
      provider_driver: 'openai_compatible',
      model_profile_version_id: '77777777-7777-4777-8777-777777777777',
      model_identifier: 'vendor/external',
      capability: 'prompt.normalize',
      state: 'unavailable',
      ready: false,
      reason_code: 'external_idempotency_unimplemented',
      transport: 'remote_api',
      data_egress: true,
      data_egress_notice: '该部署不可提交',
      estimated_cost: null,
      cost_currency: '',
      cost_basis: 'unknown',
    }],
  })
  assert.equal(capability.deployments[0].ready, false)
  assert.equal(capability.deployments[0].reasonCode, 'external_idempotency_unimplemented')
})

test('prompt normalization adapter keeps an absent default empty and unavailable models disabled', () => {
  const capability = adaptPromptNormalizationCapability({
    schema_version: 2,
    capability_key: 'prompt.normalize',
    review_required: true,
    default_model_deployment_id: null,
    configuration_public_sha256: 'b'.repeat(64),
    deployments: [{
      model_deployment_id: '44444444-4444-4444-8444-444444444444',
      deployment_key: 'not-smoked',
      label: '尚未烟测模型',
      provider_key: 'local-compatible',
      provider_label: 'Local Compatible',
      provider_driver: 'openai_compatible',
      model_profile_version_id: '55555555-5555-4555-8555-555555555555',
      model_identifier: 'local/model',
      capability: 'prompt.normalize',
      state: 'smoke_test_required',
      ready: false,
      reason_code: 'smoke_test_required',
      transport: 'local_endpoint',
      data_egress: false,
      data_egress_notice: '',
      estimated_cost: '0',
      cost_currency: 'USD',
      cost_basis: 'fixed_request',
    }],
  })
  assert.equal(capability.defaultModelDeploymentId, undefined)
  assert.equal(capability.deployments[0].ready, false)
  assert.equal(capability.deployments[0].reasonCode, 'smoke_test_required')
})

test('translation confirmation conflicts explain canonical recovery without exposing internal ids', () => {
  const fallbackConflict = Object.assign(new Error('entry_id: internal; field_key: character.visual_identity'), {
    response: { data: { error: { code: 'translation_promotion_conflict', details: {
      code: 'canonical_target_missing',
      entry_id: 'internal-entry-id', field_key: 'character.visual_identity',
      missing_mappings: [{ source_term: '断刃', requires_glossary: 'True' }],
    } } } },
  })
  const fallbackMessage = translationReviewRequestError(fallbackConflict, '视觉锚点确认失败。')
  assert.match(fallbackMessage, /断刃/)
  assert.match(fallbackMessage, /全剧视觉规则/)
  assert.doesNotMatch(fallbackMessage, /entry_id|field_key|internal-entry-id/)

  const configuredConflict = Object.assign(new Error('Request failed with status code 409'), {
    response: { data: { error: { details: {
      code: 'canonical_target_missing',
      missing_mappings: [{ source_term: '玄纹剑', canonical_target: 'Xuanwen Sword' }],
    } } } },
  })
  assert.match(
    translationReviewRequestError(configuredConflict, '确认失败。'),
    /玄纹剑 = Xuanwen Sword/,
  )
})

test('translation confirmation conflict codes map to actionable review messages', () => {
  const conflict = (code: string) => Object.assign(new Error('raw internal response'), {
    response: { data: { error: { details: { code, entry_id: 'internal' } } } },
  })
  assert.match(translationReviewRequestError(conflict('context_changed'), 'fallback'), /重新翻译/)
  assert.match(translationReviewRequestError(conflict('source_changed'), 'fallback'), /中文源文已变化/)
  assert.match(translationReviewRequestError(conflict('revision_conflict'), 'fallback'), /刷新/)
  assert.equal(
    translationReviewRequestError(new Error('entry_id: internal；field_key: shot.narrative'), '安全提示'),
    '安全提示',
  )
})

const field: PromptTranslationField = {
  fieldKey: 'shot.action', label: '动作与画面描述', sourceText: '侠客推门进入客栈。',
  scopeType: 'shot', scopeId: 'shot-1', impact: 'prompt',
}

test('translation review exposes all operator-facing states', () => {
  assert.equal(translationReviewStatus(field, undefined), 'untranslated')
  const draft = adaptTranslationEntry({
    id: 't1', field_key: 'shot.action', scope_type: 'shot', scope_id: 'shot-1',
    source_text: field.sourceText, translated_text: 'The swordsman enters the inn.', status: 'draft_translation', revision: 1,
  })
  assert.equal(translationReviewStatus(field, draft, true), 'translating')
  assert.equal(translationReviewStatus(field, draft), 'pending_review')
  assert.equal(translationReviewStatus(field, { ...draft, status: 'confirmed' }), 'confirmed')
  assert.equal(translationReviewStatus(field, { ...draft, status: 'failed' }), 'failed')
  assert.equal(translationReviewStatus({ ...field, sourceText: '源文已经改变。' }, draft), 'source_changed')
  assert.equal(translationReviewStatus(field, { ...draft, status: 'confirmed', contextCurrent: false }), 'source_changed')
})

test('only an exact-source confirmed translation can enter a formal prompt', () => {
  const confirmed = adaptTranslationEntry({
    id: 't1', field_key: 'shot.action', scope_type: 'shot', scope_id: 'shot-1',
    source_text: field.sourceText, translated_text: 'The swordsman enters the inn.', status: 'confirmed', revision: 2,
  })
  assert.equal(confirmedTranslationForPrompt(field, confirmed), confirmed.translatedText)
  assert.equal(confirmedTranslationForPrompt(field, { ...confirmed, status: 'draft_translation' }), '')
  assert.equal(confirmedTranslationForPrompt({ ...field, sourceText: 'changed' }, confirmed), '')
})

test('dialogue review translates only delivery and never dialogue text', () => {
  const fields = dialogueDeliveryTranslationFields(
    '沈砚（voiceover; 压低声音，克制地）：谁在等我？\n沈砚：这句没有演绎说明。',
    [{ name: '沈砚', stableSpeakerId: 'S1' }],
    'shot-1',
  )
  assert.equal(fields.length, 1)
  assert.equal(fields[0].fieldKey, 'shot.dialogue.0.delivery')
  assert.equal(fields[0].sourceText, '压低声音，克制地')
  assert.doesNotMatch(fields[0].sourceText, /谁在等我/)
  assert.doesNotMatch(fields[0].sourceText, /voiceover/i)
})

test('canonical glossary accepts explicit English mappings and rejects ambiguous entries', () => {
  const parsed = parseTranslationGlossary('玄纹剑 = Xuanwen Sword\n听雨客栈 -> Listening Rain Inn')
  assert.equal(parsed.error, '')
  assert.deepEqual(parsed.value, { 玄纹剑: 'Xuanwen Sword', 听雨客栈: 'Listening Rain Inn' })
  assert.equal(formatTranslationGlossary(parsed.value), '玄纹剑 = Xuanwen Sword\n听雨客栈 = Listening Rain Inn')
  assert.match(parseTranslationGlossary('玄纹剑 = 玄纹剑').error, /英文 canonical/)
  assert.match(parseTranslationGlossary('玄纹剑 Xuanwen Sword').error, /格式错误/)
  assert.match(parseTranslationGlossary('玄纹剑 = Xuanwen Sword\n玄纹剑 = Pattern Sword').error, /重复/)
})

test('Bible canonical backfill finds only missing CJK names', () => {
  assert.deepEqual(
    missingBibleCanonicalNames(
      ['沈砚', '听雨客栈', 'Xuanwen Sword', '沈砚', ' 玄纹剑 '],
      { 沈砚: 'Shen Yan' },
    ),
    ['听雨客栈', '玄纹剑'],
  )
})

test('Bible canonical targets reject NFKC whitespace and casefold collisions without guessing names', () => {
  const collisions = bibleCanonicalTargetCollisions(
    ['凌霜', '顾千山', '玄纹剑'],
    {
      凌霜: 'Ｍing   Shao',
      顾千山: 'ming shao',
      玄纹剑: 'MING SHAO',
      已删除旧名: 'ming shao',
    },
  )
  assert.deepEqual(collisions, [{
    canonicalTarget: 'Ming Shao',
    sourceNames: ['凌霜', '顾千山', '玄纹剑'],
  }])
  const message = bibleCanonicalTargetCollisionMessage(collisions)
  assert.match(message, /canonical_target_collision/)
  assert.match(message, /凌霜.*顾千山.*玄纹剑/)
  assert.match(message, /系统不会猜测或自动改名/)
  assert.deepEqual(
    bibleCanonicalTargetCollisions(['凌霜', '顾千山'], { 凌霜: 'Ling Shuang', 顾千山: 'Gu Qianshan' }),
    [],
  )
})

test('canonical ensure accepts the row-locked server glossary over a stale browser snapshot', () => {
  const staleBrowserSnapshot = { 玄纹剑: 'Old Browser Value' }
  const rowLockedServerResult = { 玄纹剑: 'Newer Manual Value', 听雨客栈: 'Listening Rain Inn' }
  const accepted = acceptAuthoritativeCanonicalGlossary(rowLockedServerResult)
  assert.deepEqual(accepted, rowLockedServerResult)
  assert.notDeepEqual(accepted, staleBrowserSnapshot)
  accepted.玄纹剑 = 'mutated copy'
  assert.equal(rowLockedServerResult.玄纹剑, 'Newer Manual Value')
})

test('canonical ensure adapter follows the project endpoint contract', () => {
  const result = adaptCanonicalGlossaryEnsureResult({ data: {
    project_id: 'project-1',
    updated_at: '2026-08-20T14:45:00Z',
    translation_glossary: { 沈砚: 'Shen Yan' },
    summary: { requested: 1, generated: 1, reused: 0, conflict: 0, failed: 0 },
    entries: [{
      scope_type: 'character', scope_id: 'character-1', name: '沈砚', canonical_english: 'Shen Yan',
      status: 'generated', origin: 'model', retryable: false, error_code: '', message: '',
    }],
  } })
  assert.equal(result.projectId, 'project-1')
  assert.equal(result.updatedAt, '2026-08-20T14:45:00Z')
  assert.deepEqual(result.translationGlossary, { 沈砚: 'Shen Yan' })
  assert.equal(result.summary.generated, 1)
  assert.deepEqual(result.entries[0], {
    scopeType: 'character', scopeId: 'character-1', name: '沈砚', canonicalEnglish: 'Shen Yan',
    status: 'generated', origin: 'model', retryable: false, errorCode: undefined, message: undefined,
  })
})

test('canonical ensure adapter keeps visual translation candidate separate for manual review', () => {
  const result = adaptCanonicalGlossaryEnsureResult({
    project_id: 'project-1', translation_glossary: { 沈砚: 'Shen Yan' },
    summary: { requested: 1, generated: 1, reused: 0, conflict: 0, failed: 0 },
    entries: [{
      scope_type: 'character', scope_id: 'character-1', name: '沈砚',
      canonical_english: 'Shen Yan', status: 'generated', origin: 'model', retryable: false,
    }],
    visual_summary: { requested: 1, generated: 1, reused: 0, not_required: 0, conflict: 0, failed: 0 },
    visual_translations: [{
      scope_type: 'character', scope_id: 'character-1', field_key: 'character.visual_identity',
      source_text: '清瘦青年剑客，右眉尾浅疤。',
      translated_text: 'A slim young swordsman with a shallow scar at the tail of his right eyebrow.',
      translation_id: 'translation-visual-1', translation_status: 'draft_translation', revision: 1,
      status: 'generated', origin: 'model', context_current: true, retryable: false,
    }],
    runtime: {
      invocation_attempted: true,
      submission_uncertain: false,
      model_deployment_id: '11111111-1111-4111-8111-111111111111',
      provider: 'third-local-compatible',
      data_egress: false,
    },
  })

  assert.equal(result.visualSummary.generated, 1)
  assert.equal(result.visualTranslations[0].status, 'generated')
  assert.equal(result.visualTranslations[0].translation?.fieldKey, 'character.visual_identity')
  assert.equal(result.visualTranslations[0].translation?.status, 'draft_translation')
  assert.equal(result.visualTranslations[0].translation?.sourceText, '清瘦青年剑客，右眉尾浅疤。')
  assert.equal(result.visualTranslations[0].translation?.translatedText.startsWith('A slim'), true)
  assert.equal(result.runtime?.invocationAttempted, true)
  assert.equal(result.runtime?.submissionUncertain, false)
  assert.equal(result.runtime?.modelDeploymentId, '11111111-1111-4111-8111-111111111111')
  assert.equal(result.runtime?.dataEgress, false)
})

test('standalone translation UI has no external authorization or budget submission controls', () => {
  const panel = readFileSync(new URL('../src/components/TranslationReviewPanel.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  assert.doesNotMatch(panel, /externalAuthorized|budgetLimit|外部 API 数据发送提示|单次预算上限/)
  assert.doesNotMatch(repository, /allow_external_calls|budget_limit/)
})

test('manual English review and every negative prompt source are first-class recovery inputs', () => {
  const panel = readFileSync(new URL('../src/components/TranslationReviewPanel.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  const storyboard = readFileSync(new URL('../src/views/StoryboardView.vue', import.meta.url), 'utf8')
  const studio = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')

  assert.ok(panel.includes('手工填写英文'))
  assert.ok(panel.includes('createManualProjectTranslation'))
  assert.ok(repository.includes('/translations/manual/'))
  for (const fieldKey of [
    'style.negative_prompt',
    'project.production_settings.negative_prompt',
    'character.negative_constraints',
    'scene.negative_constraints',
    'prop.negative_constraints',
    'shot.negative_prompt',
  ]) assert.ok(storyboard.includes(fieldKey), fieldKey)
  assert.ok(studio.includes('style.negative_prompt'))
  assert.ok(studio.includes('project.production_settings.negative_prompt'))
})

test('visual ensure adapter preserves bypass and retry states without inventing a candidate', () => {
  const result = adaptCanonicalGlossaryEnsureResult({
    project_id: 'project-1', translation_glossary: {},
    summary: { requested: 2, generated: 0, reused: 2, conflict: 0, failed: 0 },
    entries: [],
    visual_summary: { requested: 2, generated: 0, reused: 0, not_required: 1, conflict: 0, failed: 1 },
    visual_translations: [{
      scope_type: 'scene', scope_id: 'scene-1', field_key: 'scene.visual_identity',
      source_text: 'Rain-soaked timber inn.', translated_text: '', translation_id: null,
      translation_status: '', revision: null, status: 'not_required', origin: 'source_english',
      context_current: false, retryable: false, message: '视觉锚点已是英文，无需翻译',
    }, {
      scope_type: 'prop', scope_id: 'prop-1', field_key: 'prop.visual_identity',
      source_text: '乌黑旧剑鞘，银色回纹。', translated_text: '', translation_id: 'translation-failed',
      translation_status: 'failed', revision: 1, status: 'failed', origin: 'model',
      context_current: false, retryable: true, error_code: 'translation_resource_busy',
      message: '视频生成正在执行，请稍后重试。',
    }],
  })

  assert.equal(result.visualSummary.notRequired, 1)
  assert.equal(result.visualTranslations[0].status, 'not_required')
  assert.equal(result.visualTranslations[0].translation, undefined)
  assert.equal(result.visualTranslations[1].status, 'failed')
  assert.equal(result.visualTranslations[1].retryable, true)
  assert.equal(result.visualTranslations[1].translation?.status, 'failed')
  assert.equal(result.visualTranslations[1].translation?.error, '视频生成正在执行，请稍后重试。')
})

test('failed translation preserves safe runtime error metadata for operator diagnosis', () => {
  const failed = adaptTranslationEntry({
    id: 't-failed', field_key: 'project.continuity_summary', scope_type: 'project',
    scope_id: 'project-1', source_text: '玄纹剑保持暗银冷光。', translated_text: '',
    status: 'failed', revision: 1,
    error_message: '英文候选经一次确定性修复后仍含中文字符，已拒绝采用。',
    runtime_metadata: {
      error_code: 'residual_cjk_after_repair', residual_cjk: ['玄纹剑'], repair_attempted: true,
    },
  })
  assert.equal(failed.scopeType, 'project')
  assert.equal(failed.runtimeMetadata?.error_code, 'residual_cjk_after_repair')
  assert.deepEqual(failed.runtimeMetadata?.residual_cjk, ['玄纹剑'])
})

test('a successful retry replaces the empty draft left by the same failed entry id', () => {
  const failed = adaptTranslationEntry({
    id: 'translation-1', field_key: 'project.continuity_summary', scope_type: 'project',
    scope_id: 'project-1', source_text: '全剧以冷青灰雨夜为主色。', translated_text: '',
    status: 'failed', revision: 1,
  })
  const retry = adaptTranslationEntry({
    id: 'translation-1', field_key: 'project.continuity_summary', scope_type: 'project',
    scope_id: 'project-1', source_text: '全剧以冷青灰雨夜为主色。',
    translated_text: 'Use a cool blue-gray rainy-night palette throughout the series.',
    status: 'draft_translation', revision: 2,
  })

  assert.deepEqual(
    reconcileTranslationDrafts({ 'translation-1': '' }, [failed], [retry]),
    { 'translation-1': retry.translatedText },
  )
})

test('a passive refresh preserves an unsaved operator edit at the same revision', () => {
  const candidate = adaptTranslationEntry({
    id: 'translation-1', field_key: 'project.continuity_summary', scope_type: 'project',
    scope_id: 'project-1', source_text: '全剧以冷青灰雨夜为主色。',
    translated_text: 'Use a cool blue-gray palette.', status: 'draft_translation', revision: 2,
  })
  const edited = 'Use a restrained cool blue-gray rainy-night palette.'

  assert.deepEqual(
    reconcileTranslationDrafts({ 'translation-1': edited }, [candidate], [{ ...candidate }]),
    { 'translation-1': edited },
  )
  assert.deepEqual(
    reconcileTranslationDrafts({ 'translation-1': edited }, [candidate], [{ ...candidate }], ['translation-1']),
    { 'translation-1': candidate.translatedText },
  )
})

test('a mutation invalidates every older translation load response', () => {
  const gate = createTranslationLoadGate()
  const firstLoad = gate.begin()
  const secondLoad = gate.begin()
  assert.equal(gate.isCurrent(firstLoad), false)
  assert.equal(gate.isCurrent(secondLoad), true)

  // A later translate/confirm mutation wins even if either GET resolves last.
  gate.invalidate()
  assert.equal(gate.isCurrent(firstLoad), false)
  assert.equal(gate.isCurrent(secondLoad), false)

  const refreshAfterMutation = gate.begin()
  assert.equal(gate.isCurrent(refreshAfterMutation), true)
})

test('old compiler artifacts and later relevant translation mutations require regeneration', () => {
  const shot = { id: 'shot-1', characterIds: ['character-1'], sceneId: 'scene-1', propIds: ['prop-1'] }
  const artifact = {
    compiler_current: true, artifact_created_at: '2026-08-10T09:00:00Z',
  } as H3PromptPayload
  const entry = (scopeType: PromptTranslationEntry['scopeType'], scopeId: string, updatedAt: string): PromptTranslationEntry => ({
    id: `${scopeType}-${scopeId}`, fieldKey: `${scopeType}.visual_identity`, scopeType, scopeId,
    sourceText: 'source', translatedText: 'translation', status: 'confirmed', revision: 1, updatedAt,
  })

  assert.equal(promptArtifactNeedsRegeneration({ ...artifact, compiler_current: false }, 'project-1', shot), true)
  assert.equal(promptArtifactNeedsRegeneration(artifact, 'project-1', shot, [entry('shot', 'shot-1', '2026-08-10T09:01:00Z')]), true)
  assert.equal(promptArtifactNeedsRegeneration(artifact, 'project-1', shot, [entry('character', 'character-1', '2026-08-10T09:01:00Z')]), true)
  assert.equal(promptArtifactNeedsRegeneration(artifact, 'project-1', shot, [entry('shot', 'shot-2', '2026-08-10T09:01:00Z')]), false)
  assert.equal(promptArtifactNeedsRegeneration(artifact, 'project-1', shot, [entry('shot', 'shot-1', '2026-08-10T08:59:00Z')]), false)
  assert.equal(translationAppliesToShot(entry('project', 'project-1', '2026-08-10T09:01:00Z'), 'project-1', shot), true)
})

test('promotion adapter preserves authoritative formal fields and guarded metadata patch', () => {
  const result = adaptShotTranslationPromotionResult({
    shot_id: 'shot-1',
    promoted_fields: [
      { field_key: 'shot.narrative', value: 'The swordsman steps inside and stops.', dialogue_index: null },
      { field_key: 'shot.dialogue.0.delivery', value: 'in a low restrained voice', dialogue_index: 0 },
    ],
    entries: [{
      id: 'translation-1', field_key: 'shot.narrative', scope_type: 'shot', scope_id: 'shot-1',
      source_text: '剑客入门停步。', translated_text: 'The swordsman steps inside and stops.',
      status: 'confirmed', revision: 3,
    }],
    shot_patch: {
      metadata: { prompt_field_version: 2, negative_prompt: '', legacy_negative_prompt_recovery: { recovered: true } },
      updated_at: '2026-08-10T12:00:00Z',
    },
  })
  assert.equal(result.shotId, 'shot-1')
  assert.equal(result.promotedFields[1].dialogueIndex, 0)
  assert.equal(result.entries[0].revision, 3)
  assert.equal(result.shotPatch.metadata?.negative_prompt, '')
  assert.equal(result.shotPatch.updatedAt, '2026-08-10T12:00:00Z')
})

test('authoritative promotion patches only director fields and preserves Chinese title and dialogue text', () => {
  const metadata = { reference_bindings: { first_frame_asset_id: 'asset-1' }, negative_prompt: 'safe negative' }
  const shot: Shot = {
    id: 'shot-1', order: 1, title: '雨夜入栈', mediaType: 'video', duration: 5, mode: 'T2VA', framing: '中景',
    camera: '缓慢推进', action: '剑客推门。', continuityDirection: '衣着不变。', prompt: '', negativePrompt: 'safe negative',
    characterIds: ['character-1'], sceneId: 'scene-1', propIds: [], referenceAssetIds: [], metadata,
    audioEnabled: true,
    audio: { dialogue: '沈砚（压低声音）：别动。', soundscape: '雨声。', music: '古琴。' }, status: 'draft',
  }
  const applied = applyPromotedFieldsToShot(shot, [
    { fieldKey: 'shot.narrative', value: 'The swordsman pushes the door open and stops inside.', dialogueIndex: null },
    { fieldKey: 'shot.action', value: 'legacy field must be ignored', dialogueIndex: null },
    { fieldKey: 'shot.camera', value: 'Slow dolly in at eye level.', dialogueIndex: null },
    { fieldKey: 'shot.continuity_notes', value: 'Preserve the same robe and screen direction.', dialogueIndex: null },
    { fieldKey: 'shot.sound_effects', value: 'Steady rain and one short metallic click.', dialogueIndex: null },
    { fieldKey: 'shot.music', value: 'Sparse guqin harmonics.', dialogueIndex: null },
    { fieldKey: 'shot.dialogue.0.delivery', value: 'in a low restrained voice', dialogueIndex: 0 },
  ], [{ name: '沈砚', stableSpeakerId: 'S1' }])

  assert.deepEqual(applied, [
    'shot.narrative', 'shot.camera', 'shot.continuity_notes', 'shot.sound_effects', 'shot.music', 'shot.dialogue.0.delivery',
  ])
  assert.equal(shot.title, '雨夜入栈')
  assert.equal(shot.action.startsWith('The swordsman'), true)
  assert.equal(shot.camera, 'Slow dolly in at eye level.')
  assert.match(shot.audio.dialogue, /别动。/)
  assert.match(shot.audio.dialogue, /in a low restrained voice/)
  assert.equal(shot.negativePrompt, 'safe negative')
  assert.equal(shot.metadata, metadata)

  const patched = applyShotTranslationPatch(shot, {
    metadata: { ...metadata, prompt_field_version: 2, negative_prompt: '' }, updatedAt: '2026-08-10T12:00:00Z',
  })
  assert.equal(patched, true)
  assert.equal(shot.negativePrompt, '')
  assert.equal(shot.legacyNegativePromptRepairEligible, false)
})

test('storyboard hides stale artifacts and routes prompt recompilation through ProductionRun', () => {
  const source = readFileSync(new URL('../src/views/StoryboardView.vue', import.meta.url), 'utf8')
  assert.ok(source.includes("fieldKey:'shot.narrative', label:'动作与画面描述', sourceText:shot.action"))
  assert.ok(!source.includes("fieldKey:'shot.action', label:'动作与画面描述', sourceText:shot.action"))
  assert.ok(source.includes('promptArtifactNeedsRegeneration'))
  assert.ok(source.includes("invalidatePrompt(selectedShot,'translations_changed')"))
  assert.ok(source.includes('旧 Prompt Artifact 已失效'))
  assert.ok(source.includes('进入 ProductionRun 重新编译并复检'))
  assert.ok(source.includes('@promoted="applyShotTranslationPromotion"'))
  assert.ok(source.includes('@promotion-busy="translationPromotionBusy=$event"'))
  assert.ok(source.includes('applyPromotedFieldsToShot(target, result.promotedFields'))
  assert.ok(source.includes('applyShotTranslationPatch(target, result.shotPatch)'))
  assert.ok(source.includes('legacyNegativeRepairShots'))
  assert.ok(source.includes("episode.value.shots.find((shot) => shot.id === result.shotId)"))
  assert.doesNotMatch(source, /result\.shotId\s*!==\s*selectedShotId\.value/)
  assert.doesNotMatch(source, /applyShotTranslationPromotion[\s\S]{0,1200}refreshProjects/)
})

test('canonical target collisions visibly block save and translation sync before ProductionRun', () => {
  const studio = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  const storyboard = readFileSync(new URL('../src/views/StoryboardView.vue', import.meta.url), 'utf8')
  const panel = readFileSync(new URL('../src/components/TranslationReviewPanel.vue', import.meta.url), 'utf8')
  assert.ok(studio.includes('bibleCanonicalTargetCollisions(allBibleNames.value'))
  assert.ok(studio.includes('canonicalTargetCollisionError'))
  assert.ok(studio.includes(':blocked-reason="canonicalGlossaryBlockedReason"'))
  assert.ok(studio.includes(':disabled="saving||Boolean(canonicalGlossaryBlockedReason)"'))
  assert.ok(studio.includes(':title="canonicalGlossaryBlockedReason||\'\'"'))
  assert.ok(studio.includes('if (glossaryChanged) translationReviewRefreshKey.value += 1'))
  const canonicalBackfill = studio.slice(
    studio.indexOf('const backfillMissingCanonicalGlossary = async'),
    studio.indexOf('\nconst addBibleItem', studio.indexOf('const backfillMissingCanonicalGlossary = async')),
  )
  assert.ok(canonicalBackfill.includes('studio.ensureCanonicalGlossary(project.value, { includeAllMissing: true })'))
  assert.ok(canonicalBackfill.includes('检测到术语表有未保存的人工修改'))
  assert.ok(!canonicalBackfill.includes('await save('))
  assert.ok(studio.includes(':blocked-reason="canonicalGlossaryBlockedReason"'))
  assert.ok(storyboard.includes('canonicalTargetCollisionError'))
  assert.ok(storyboard.includes('canonical 术语碰撞 · 保存、翻译同步和生成已暂停'))
  assert.ok(storyboard.includes('if (canonicalTargetCollisionError.value) throw new Error'))
  assert.ok(panel.includes('canonical 术语碰撞 · 翻译确认与正式字段同步已暂停'))
  assert.ok(panel.includes('reviewMutationBlocked'))
})

test('review panel uses the router endpoint, expected revision, one promotion event per batch, and repair-only action', () => {
  const panel = readFileSync(new URL('../src/components/TranslationReviewPanel.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  assert.ok(repository.includes("'/translations/promote-shot/'"))
  assert.ok(repository.includes('expected_revision: entry.expectedRevision'))
  assert.ok(repository.includes('repair_legacy_negative_prompt: options.repairLegacyNegativePrompt === true'))
  assert.ok(panel.includes('repository.confirmProjectTranslation(entry.id, translatedText, entry.revision)'))
  assert.ok(panel.includes('upsertEntries(result.entries, { emitPromotion: false, emitChanged: false })'))
  assert.ok(panel.includes('requestShotId === props.shotId'))
  assert.ok(panel.includes('shotId: row.scopeId || requestShotId'))
  assert.ok(panel.includes('shotId&&(promotableEntries.length||repairLegacyNegativePrompt)'))
  assert.ok(panel.includes("emit('promoted', result)"))
  assert.ok(panel.includes('撤销译文确认（不回滚正式英文）'))
})

test('review panel keeps actionable rows expanded and collapses only settled confirmations', () => {
  const source = readFileSync(new URL('../src/components/TranslationReviewPanel.vue', import.meta.url), 'utf8')
  assert.ok(source.includes("stateFor(field) === 'confirmed' && !draftChanged(field)"))
  assert.ok(source.includes("collapseConfirmed(field)?'details':'article'"))
  assert.ok(source.includes('v-for="field in orderedFields"'))
  assert.ok(source.includes('待处理始终展开 · 已确认默认折叠'))
  const upsertStart = source.indexOf('const upsertEntries')
  const invalidation = source.indexOf('loadGate.invalidate()', upsertStart)
  const spinnerClear = source.indexOf('loading.value = false', upsertStart)
  const mutationApply = source.indexOf('const previousEntries = entries.value', upsertStart)
  assert.ok(upsertStart >= 0 && invalidation > upsertStart)
  assert.ok(spinnerClear > invalidation && mutationApply > spinnerClear)
})

test('source-changed rows expose only retranslation of a read-only stale candidate', () => {
  const source = readFileSync(new URL('../src/components/TranslationReviewPanel.vue', import.meta.url), 'utf8')
  assert.ok(source.includes("if (stateFor(field) === 'source_changed') return '重新翻译生成候选'"))
  assert.ok(source.includes(":readonly=\"stateFor(field)==='source_changed'\""))
  assert.ok(source.includes('旧英文译文 · 已失效（只读）'))
  assert.ok(source.includes('旧译文仅供对照，不能确认也不会进入正式 prompt'))
  assert.ok(source.includes('旧译文已失效，不能确认；请先重新翻译生成候选'))
  assert.ok(source.includes("stateFor(field)!=='source_changed'"))
  assert.ok(source.includes('确认镜头导演字段后会原子同步到下方正式表单'))
  assert.ok(source.includes('镜头标题和对白正文仍保留中文'))
})
