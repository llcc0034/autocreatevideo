import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  adaptTranslationEntry,
  createTranslationLoadGate,
  reconcileTranslationDrafts,
  translationReviewRequestError,
  translationReviewStatus,
} from '../src/services/translationReview.ts'
import type { PromptTranslationEntry, PromptTranslationField } from '../src/types/index.ts'


test('translation confirmation conflicts explain canonical recovery without exposing internal ids', () => {
  const fallbackConflict = Object.assign(new Error('entry_id: internal; field_key: character.visual_identity'), {
    response: { data: { error: { code: 'translation_review_conflict', details: {
      code: 'canonical_target_missing',
      entry_id: 'internal-entry-id', field_key: 'character.visual_identity',
      missing_mappings: [{ source_term: '断刃', requires_glossary: 'True' }],
    } } } },
  })
  const fallbackMessage = translationReviewRequestError(fallbackConflict, '视觉锚点确认失败。')
  assert.match(fallbackMessage, /断刃/)
  assert.match(fallbackMessage, /当前工作台已停止名称翻译/)
  assert.match(fallbackMessage, /原名称生产不以此为前提/)
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
  fieldKey: 'project.continuity_summary', label: '项目连续性说明', sourceText: '侠客推门进入客栈。',
  scopeType: 'project', scopeId: 'project-1', impact: 'prompt',
}

test('translation review exposes all operator-facing states', () => {
  assert.equal(translationReviewStatus(field, undefined), 'untranslated')
  const draft = adaptTranslationEntry({
    id: 't1', field_key: 'project.continuity_summary', scope_type: 'project', scope_id: 'project-1',
    source_text: field.sourceText, translated_text: 'The swordsman enters the inn.', status: 'draft_translation', revision: 1,
  })
  assert.equal(translationReviewStatus(field, draft, true), 'translating')
  assert.equal(translationReviewStatus(field, draft), 'pending_review')
  assert.equal(translationReviewStatus(field, { ...draft, status: 'confirmed' }), 'confirmed')
  assert.equal(translationReviewStatus(field, { ...draft, status: 'failed' }), 'failed')
  assert.equal(translationReviewStatus({ ...field, sourceText: '源文已经改变。' }, draft), 'source_changed')
  assert.equal(translationReviewStatus(field, { ...draft, status: 'confirmed', contextCurrent: false }), 'source_changed')
})

test('retired storyboard aggregation cannot become an implicit prompt or dialogue overlay', () => {
  const source = readFileSync(new URL('../src/services/translationReview.ts', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /confirmedTranslationForPrompt|dialogueDeliveryTranslationFields/)
})

test('workbench uses original names and offers only prose translation on demand', () => {
  const source=readFileSync(new URL('../src/views/SeriesStudioView.vue',import.meta.url),'utf8')
  const panel=readFileSync(new URL('../src/components/TranslationReviewPanel.vue',import.meta.url),'utf8')
  const repository=readFileSync(new URL('../src/services/repository.ts',import.meta.url),'utf8')
  assert.match(source, /<ProjectOriginalNames :project="project" @manage="openVariantSettings"/)
  assert.match(source, /<template v-if="proseTranslationLoaded">/)
  assert.match(source, /正文翻译候选（按需使用）/)
  assert.doesNotMatch(source, /translationGlossaryText|canonicalNameFields|canonicalGlossaryBlockedReason|可选英文译名|canonical-confirmed/)
  assert.doesNotMatch(panel+repository, /confirmCanonicalTranslation|确认并写入术语表|confirm-canonical/)
})

test('standalone translation requires explicit instance, budget review and common preparation instead of synchronous deployment calls', () => {
  const panel = readFileSync(new URL('../src/components/TranslationReviewPanel.vue', import.meta.url),'utf8')
  const workbench = readFileSync(new URL('../src/components/ProjectTranslationRequests.vue', import.meta.url),'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url),'utf8')
  assert.match(panel,/ProjectTranslationRequests/)
  for (const expected of ['ExecutionPreparationWorkbench','allow_external_calls','translationFixture','待审核候选']) assert.ok(workbench.includes(expected),expected)
  assert.doesNotMatch(workbench,/budget_limit|budget_currency/,'resource thresholds belong to the system policy, not translation requests')
  assert.doesNotMatch(repository,/translateProjectFields|retranslateProjectField|getTranslationCapability/)
})

test('translation review never sends a temporary client shot id as a UUID query filter', () => {
  const panel = readFileSync(new URL('../src/components/TranslationReviewPanel.vue', import.meta.url), 'utf8')

  assert.ok(panel.includes("const persistedShotId = isBackendId(props.shotId) ? props.shotId : ''"))
  assert.ok(panel.includes('shotId: persistedShotId'))
})

test('manual English review and every negative prompt source are first-class recovery inputs', () => {
  const panel = readFileSync(new URL('../src/components/TranslationReviewPanel.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  const storyboard = readFileSync(new URL('../src/views/StoryboardView.vue', import.meta.url), 'utf8')
  const studio = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')

  assert.ok(panel.includes('手工填写英文'))
  assert.ok(panel.includes('createManualProjectTranslation'))
  assert.ok(repository.includes('/translations/manual/'))
  assert.doesNotMatch(storyboard, /selectedTranslationFields|TranslationReviewPanel/)
  assert.ok(studio.includes('style.negative_prompt'))
  assert.ok(studio.includes('project.production_settings.negative_prompt'))
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

test('historical promotion metadata cannot become an active write receipt', () => {
  const raw = {
    id: 'entry-1', field_key: 'shot.narrative', source_text: ' 原文 ',
    translated_text: 'Reviewed text.', status: 'confirmed', revision: 3,
    runtime_metadata: { promotion: { promoted: true } },
    promoted_fields: [{ field_key: 'shot.narrative', value: 'Must not overwrite.' }],
  }
  const entry = adaptTranslationEntry(raw)
  assert.equal(entry.sourceText, ' 原文 ')
  assert.equal(entry.translatedText, 'Reviewed text.')
  assert.equal('promotedFields' in entry, false)
  assert.deepEqual(entry.runtimeMetadata, raw.runtime_metadata)
})

test('storyboard uses independent field revisions without translation writeback or glossary save gate', () => {
  const source = readFileSync(new URL('../src/views/StoryboardView.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /TranslationReviewPanel|selectedTranslationFields|translationPromotionBusy|applyPromotedFieldsToShot|canonicalTargetCollisionError/)
  assert.match(source, /<ShotSourceFields /)
  assert.match(source, /<ShotFieldRevisions /)
  assert.match(source, /<ShotDialogueEditor /)
  assert.match(source, /执行准备中查看并确认本次内容/)
  assert.doesNotMatch(source, /generatedPrompt|promptArtifactNeedsRegeneration|旧 Prompt Artifact/)
})

test('review confirms only candidates and ignores late responses from another scope', () => {
  const panel = readFileSync(new URL('../src/components/TranslationReviewPanel.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  assert.doesNotMatch(repository, /promote-shot|promoteShotTranslations/)
  assert.doesNotMatch(panel, /promotedFields|promotableEntries|emit\('promoted'|promotionBusy|fieldWasPromoted/)
  assert.ok(panel.includes('repository.confirmProjectTranslation(entry.id, translatedText, entry.revision)'))
  assert.ok(panel.includes('requestContext === `${props.projectId}:${props.episodeId}:${props.shotId}`'))
  assert.ok(panel.includes('确认译文只更新候选审核状态，不改写原文、分镜字段或对白'))
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
  assert.ok(source.includes('执行是否使用该译文由本次策略及来源映射决定'))
})
