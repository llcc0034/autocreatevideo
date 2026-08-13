import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  CREATIVE_DRAFT_ENGLISH_LIMITS,
  adaptCreativeDraft,
  adaptCreativeDraftApplyResult,
  creativeDraftDataPayload,
  creativeDraftEditorWarnings,
  creativeFailureRecovery,
  creativeDraftIsEditable,
  creativeDraftIsEmptyFailure,
  creativeDraftRequestError,
  creativeDraftReviewIssueAction,
  creativeDraftReviewIssueTitle,
  creativeSafeFailureDiagnostic,
} from '../src/services/creativeDraft.ts'

const rawDraft = {
  id: 'draft-1', project: 'project-1', episode: 'episode-1', status: 'draft', revision: 3,
  generation_no: 2, context_hash: 'abc', context_current: true, creativity: 'balanced',
  variation_note: '运镜克制', can_edit: true, can_regenerate: true, can_confirm: true, can_apply: false,
  validation: { status: 'passed', errors: [], warnings: ['核对方位'] },
  draft_data: {
    schema_version: 1,
    episode: { title_zh: '交锋', title_en: 'Confrontation', synopsis_zh: '雨夜交锋。', synopsis_en: 'A confrontation in the rain.' },
    shots: [{
      client_key: 's1', order: 1, title_zh: '雨夜入栈', title_en: 'Entering the Inn',
      narrative_zh: '沈砚站在门外，推门入栈，停在门内。', narrative_en: 'Shen Yan stands outside, opens the door, and stops inside.',
      camera_zh: '中景缓慢推进，以逼近揭示警觉。', camera_en: 'one slow motivated push-in reveals his alertness',
      action_zh: '沈砚推门入栈。', action_en: 'Shen Yan opens the door and enters.',
      continuity_zh: '门外站立进入门内停步。', continuity_en: 'standing outside to a stop just inside',
      soundscape_zh: '雨声连续。', soundscape_en: 'continuous rainfall', music_zh: '无', music_en: 'no music',
      duration_ms: 5000, media_type: 'video', h3_mode: 'Ref2VA', include_audio: true,
      scene_id: 'scene-1', character_ids: ['character-1'], prop_ids: [],
      dialogue: [{ speaker_id: 'character-1', text_zh: '谁在那里？', delivery_zh: '压低声音', delivery_en: 'in a low voice', voiceover: false }],
    }],
  },
}

test('creative draft adapter preserves bilingual structured IR and operator gates', () => {
  const draft = adaptCreativeDraft(rawDraft)
  assert.equal(draft.draftData.shots[0].clientKey, 's1')
  assert.equal(draft.draftData.shots[0].h3Mode, 'Ref2VA')
  assert.equal(draft.draftData.shots[0].dialogue[0].textZh, '谁在那里？')
  assert.equal(draft.validation.status, 'passed')
  assert.equal(draft.canEdit, true)
  assert.equal(draft.canApply, false)
})

test('creative draft payload round-trips without translating locked dialogue text', () => {
  const draft = adaptCreativeDraft(rawDraft)
  const payload = creativeDraftDataPayload(draft.draftData)
  assert.equal(payload.shots[0].dialogue[0].text_zh, '谁在那里？')
  assert.equal(payload.shots[0].dialogue[0].delivery_en, 'in a low voice')
  assert.equal(payload.shots[0].character_ids[0], 'character-1')
  assert.equal(payload.shots[0].duration_ms, 5000)
  assert.equal((payload.shots[0].dialogue[0] as Record<string, unknown>).text_en, undefined)
})

test('episode copy is a read-only formal source, with optional empty English synopsis', () => {
  const withoutSynopsis = adaptCreativeDraft({
    ...rawDraft,
    draft_data: {
      ...rawDraft.draft_data,
      episode: { ...rawDraft.draft_data.episode, synopsis_en: '' },
    },
  })
  assert.equal(withoutSynopsis.draftData.episode.synopsisEn, '')
  assert.equal(creativeDraftDataPayload(withoutSynopsis.draftData).episode.synopsis_en, '')
  assert.deepEqual(creativeDraftEditorWarnings(withoutSynopsis.draftData), [])

  withoutSynopsis.draftData.shots[0].narrativeEn = ''
  assert.ok(creativeDraftEditorWarnings(withoutSynopsis.draftData).some((message) => message.includes('缺少中英叙事结构')))

  const workbench = readFileSync(new URL('../src/components/CreativeDraftWorkbench.vue', import.meta.url), 'utf8')
  assert.ok(workbench.includes('本集标题与梗概 · 正式来源'))
  assert.ok(workbench.includes('English synopsis · 可选只读'))
  assert.ok(workbench.includes('未生成英文摘要，不影响分镜或视频；本栏仅供只读查看。'))
  assert.ok(workbench.includes(':value="activeEpisode?.title??workingData.episode.titleZh"'))
  assert.ok(!workbench.includes('v-model="workingData.episode.'))
  assert.ok((workbench.match(/readonly aria-readonly="true"/g) || []).length >= 4)
})

test('editor warnings enforce the 5-15 second and structured-field quality contract', () => {
  const draft = adaptCreativeDraft(rawDraft)
  assert.deepEqual(creativeDraftEditorWarnings(draft.draftData), [])
  draft.draftData.shots[0].durationMs = 4000
  draft.draftData.shots[0].cameraEn = ''
  const warnings = creativeDraftEditorWarnings(draft.draftData)
  assert.ok(warnings.some((message) => message.includes('5–15 秒')))
  assert.ok(warnings.some((message) => message.includes('单一运镜')))
})

test('creative English editors keep tighter Qwen generation budgets and explicit post-repair caps', () => {
  assert.deepEqual(CREATIVE_DRAFT_ENGLISH_LIMITS, {
    narrative: { generation: 1000, editor: 1000 },
    action: { generation: 1000, editor: 1000 },
  })

  const draft = adaptCreativeDraft(rawDraft)
  draft.draftData.shots[0].narrativeEn = 'n'.repeat(1000)
  draft.draftData.shots[0].actionEn = 'a'.repeat(1000)
  assert.ok(!creativeDraftEditorWarnings(draft.draftData).some((message) => message.includes('超过')))

  draft.draftData.shots[0].narrativeEn += 'n'
  draft.draftData.shots[0].actionEn += 'a'
  const warnings = creativeDraftEditorWarnings(draft.draftData)
  assert.ok(warnings.some((message) => message.includes('英文叙事结构超过 1000 字符')))
  assert.ok(warnings.some((message) => message.includes('英文主动作超过 1000 字符')))

  const source = readFileSync(new URL('../src/components/CreativeDraftWorkbench.vue', import.meta.url), 'utf8')
  assert.match(source, /v-model="shot\.narrativeEn"[^>]*:maxlength="CREATIVE_DRAFT_ENGLISH_LIMITS\.narrative\.editor"/)
  assert.match(source, /v-model="shot\.actionEn"[^>]*:maxlength="CREATIVE_DRAFT_ENGLISH_LIMITS\.action\.editor"/)
  assert.ok(source.includes('{{ shot.narrativeEn.length }}/{{ CREATIVE_DRAFT_ENGLISH_LIMITS.narrative.editor }}'))
  assert.ok(source.includes('{{ shot.actionEn.length }}/{{ CREATIVE_DRAFT_ENGLISH_LIMITS.action.editor }}'))
  assert.ok(source.includes('生成≤{{ CREATIVE_DRAFT_ENGLISH_LIMITS.narrative.generation }}'))
  assert.ok(source.includes('生成≤{{ CREATIVE_DRAFT_ENGLISH_LIMITS.action.generation }}'))
  assert.ok(source.includes('!dirty||!editable" title="保存始终允许'))
  assert.ok(!source.includes('hasEnglishFieldOverflow'))
  assert.equal(source.match(/:maxlength=/g)?.length, 2)
  assert.doesNotMatch(source, /v-model="shot\.(?:narrativeZh|actionZh|cameraEn|continuityEn)"[^>]*maxlength/)
})

test('structured review issues are adapted, safely titled and targeted without backend prose', () => {
  const reviewed = adaptCreativeDraft({
    ...rawDraft,
    validation: {
      status: 'needs_review', errors: [], warnings: [],
      summary: { issue_count: 3, risk_count: 2, requires_edit_count: 1 },
      review_issues: [
        { path: 'result.shots[0].action_en', code: 'direction_mismatch', severity: 'risk', suggestion_zh: '请对照中文动作核对左右侧。' },
        { path: 'result.shots[0].camera_en', code: 'camera_motivation_missing', severity: 'warning', suggestion_zh: '补充运镜为何发生。' },
        { path: 'result.shots[0].character_ids', code: 'unknown_character_id', severity: 'requires_edit', suggestion_zh: '替换或删除未知人物。' },
      ],
    },
  })
  assert.equal(reviewed.validation.status, 'needs_review')
  assert.equal(reviewed.reviewSummary.issueCount, 3)
  assert.equal(reviewed.reviewSummary.requiresEditCount, 1)
  assert.deepEqual(
    reviewed.reviewIssues.map(({ shotIndex, field, severity }) => ({ shotIndex, field, severity })),
    [
      { shotIndex: 0, field: 'actionEn', severity: 'risk' },
      { shotIndex: 0, field: 'cameraEn', severity: 'warning' },
      { shotIndex: 0, field: 'characterIds', severity: 'requires_edit' },
    ],
  )
  assert.match(creativeDraftReviewIssueTitle(reviewed.reviewIssues[0]), /方位、左右侧/)
  assert.match(creativeDraftReviewIssueTitle(reviewed.reviewIssues[2]), /应用前需要替换或删除/)

  const unknown = adaptCreativeDraft({
    ...rawDraft,
    review_issues: [{
      path: 'result.shots[0].narrative_en', code: 'future_internal_parser_code', severity: 'blocking',
      detail: 'Invalid control character at byte 991', message: 'raw model output',
      suggestion_zh: 'JSONDecodeError at byte 991',
    }],
  })
  assert.equal(unknown.reviewIssues[0].severity, 'risk')
  assert.equal(unknown.reviewIssues[0].suggestionZh, undefined)
  assert.equal(creativeDraftReviewIssueTitle(unknown.reviewIssues[0]), '此处可能与剧本或中英文对应关系不完全一致')
  assert.doesNotMatch(JSON.stringify(unknown.reviewIssues), /Invalid control character|raw model output/)
})

test('relation and dialogue review issues identify the exact safe editor target', () => {
  const reviewed = adaptCreativeDraft({
    ...rawDraft,
    validation: {
      status: 'needs_review', errors: [], warnings: [],
      review_issues: [
        { path: 'shots[1].scene_id', code: 'unknown_scene_id', severity: 'requires_edit' },
        { path: 'shots[0].character_ids', code: 'unknown_character_id', severity: 'requires_edit' },
        { path: 'shots[1].prop_ids', code: 'unknown_prop_id', severity: 'requires_edit' },
        { path: 'shots[0].dialogue[0].speaker_id', code: 'unknown_speaker_id', severity: 'requires_edit' },
        { path: 'shots[].dialogue', code: 'dialogue_manifest_mismatch', severity: 'requires_edit' },
      ],
    },
  })
  assert.deepEqual(reviewed.reviewIssues.map(({ shotIndex, field }) => ({ shotIndex, field })), [
    { shotIndex: 1, field: 'sceneId' },
    { shotIndex: 0, field: 'characterIds' },
    { shotIndex: 1, field: 'propIds' },
    { shotIndex: 0, field: 'dialogueSpeakerId' },
    { shotIndex: undefined, field: 'dialogueSpeakerId' },
  ])
  assert.match(creativeDraftReviewIssueTitle(reviewed.reviewIssues[0]), /关联场景不存在/)
  assert.match(creativeDraftReviewIssueTitle(reviewed.reviewIssues[1]), /关联人物不存在/)
  assert.match(creativeDraftReviewIssueTitle(reviewed.reviewIssues[2]), /关联道具不存在/)
  assert.match(creativeDraftReviewIssueTitle(reviewed.reviewIssues[3]), /对白说话人不存在/)
  assert.match(creativeDraftReviewIssueTitle(reviewed.reviewIssues[4]), /原剧本清单不一致/)
  assert.match(creativeDraftReviewIssueAction(reviewed.reviewIssues[4]), /逐镜核对说话人与对白原文/)

  const source = readFileSync(new URL('../src/components/CreativeDraftWorkbench.vue', import.meta.url), 'utf8')
  assert.ok(source.includes("'dialogueSpeakerId', 'dialogueTextZh'"))
  assert.ok(source.includes("'全部镜头'"))
  assert.ok(source.includes("issue.shotIndex===undefined?'查看相关镜头':'跳到字段'"))
  assert.ok(source.includes('issue.suggestionZh || creativeDraftReviewIssueAction(issue)'))
})

test('script action coverage is an advisory Chinese action-chain review target', () => {
  const reviewed = adaptCreativeDraft({
    ...rawDraft,
    validation: {
      status: 'needs_review', errors: [], warnings: [],
      review_issues: [{
        path: 'shots[0]', code: 'script_action_coverage_low', severity: 'requires_edit',
        suggestion_zh: '请对照剧本核对本镜动作。',
      }],
    },
  })
  const issue = reviewed.reviewIssues[0]
  assert.deepEqual(
    { shotIndex: issue.shotIndex, field: issue.field, language: issue.language, severity: issue.severity },
    { shotIndex: 0, field: 'actionZh', language: 'zh', severity: 'warning' },
  )
  assert.equal(creativeDraftReviewIssueTitle(issue), '本镜可能未完整覆盖剧本动作链')
  assert.match(creativeDraftReviewIssueAction(issue), /不会阻止保存、确认或应用/)
  assert.equal(reviewed.reviewSummary.requiresEditCount, 0)

  const source = readFileSync(new URL('../src/components/CreativeDraftWorkbench.vue', import.meta.url), 'utf8')
  assert.ok(source.includes('本镜连续动作链 / 主动作摘要'))
  assert.ok(source.includes('动作：{{ firstActionSentence(shot.actionZh) }}'))
  assert.ok(source.includes('当前没有可核实的逐动作总数，因此不显示推测性的 X/Y'))
  assert.ok(source.includes('多个快速动作可组成一条连续动作链'))
  assert.ok(source.includes('2 镜最多可承载 30 秒'))
  assert.ok(source.includes('动作密集时可提高每镜时长，或压缩单个动作节奏'))
})

test('creative workbench keeps review issues advisory and requires explicit risk acknowledgement', () => {
  const source = readFileSync(new URL('../src/components/CreativeDraftWorkbench.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/creativeDraftRepository.ts', import.meta.url), 'utf8')
  assert.ok(source.includes('已生成 · 待人工修正'))
  assert.ok(source.includes('jumpToReviewIssue(issue)'))
  assert.ok(source.includes('issuesForField(index,\'actionEn\')'))
  assert.ok(source.includes('保存后问题清单会重新计算'))
  assert.ok(source.includes('我已阅读以上提示，确认由我人工判断'))
  assert.ok(source.includes('@click="requestConfirm"'))
  assert.ok(source.includes("@click=\"requestApply('chinese_only'"))
  assert.ok(source.includes('await confirmReview(true)'))
  assert.ok(source.includes('reviewAcknowledged,'))
  assert.ok(source.includes('&& !requiresEditCount.value'))
  assert.ok(!source.includes("if (saved.validation.status !== 'passed')"))
  assert.ok(!source.includes('不能改为静音；请重新生成'))
  assert.ok(repository.includes('review_acknowledged: reviewAcknowledged'))
  assert.ok(repository.includes('review_acknowledged: options.reviewAcknowledged === true'))
})

test('apply adapter exposes exact applied shots and confirmed translation count', () => {
  const result = adaptCreativeDraftApplyResult({
    draft: { ...rawDraft, status: 'applied', can_edit: false, can_apply: false },
    episode: { id: 'episode-1' }, applied_shot_ids: ['shot-9'], translations_written: 8,
  })
  assert.equal(result.episodeId, 'episode-1')
  assert.deepEqual(result.appliedShotIds, ['shot-9'])
  assert.equal(result.translationsWritten, 8)
})

test('failed candidate remains editable for human repair but stale context never does', () => {
  const failed = adaptCreativeDraft({ ...rawDraft, status: 'failed', can_edit: true })
  assert.equal(creativeDraftIsEditable(failed), true)
  assert.equal(creativeDraftIsEmptyFailure(failed), false)
  assert.equal(creativeDraftIsEditable({ ...failed, contextCurrent: false }), false)
  assert.equal(creativeDraftIsEditable({ ...failed, status: 'confirmed' }), false)
})

test('a persisted zero-shot failure is a retry record, not an editable empty draft', () => {
  const failed = adaptCreativeDraft({
    ...rawDraft, status: 'failed', can_edit: true, can_regenerate: true,
    error_message: 'model returned invalid JSON',
    validation: { status: 'failed', errors: ['model returned invalid JSON'], warnings: [] },
    draft_data: {},
  })
  assert.equal(failed.draftData.shots.length, 0)
  assert.equal(creativeDraftIsEmptyFailure(failed), true)
  assert.equal(failed.canRegenerate, true)
})

test('creative failure recovery maps current V3 runtime codes without raw details', () => {
  const recoveries: Array<[string, RegExp]> = [
    ['creative_timeout', /超过运行时限/],
    ['creative_parent_timeout', /超过运行时限/],
    ['creative_invalid_runtime_json', /响应格式异常/],
    ['creative_runtime_exit', /进程异常退出/],
    ['creative_validation_failed', /未通过结构校验/],
  ]
  for (const [code, expected] of recoveries) {
    const message = creativeFailureRecovery(code)
    assert.ok(message)
    assert.match(message, expected)
    assert.match(message, /正式分镜未修改/)
    assert.doesNotMatch(message, /Invalid control character|JSONDecodeError|line 1/)
  }
  const stale = creativeFailureRecovery('creative_validation_failed', false)
  assert.match(stale || '', /按当前内容生成新草稿/)
  assert.doesNotMatch(stale || '', /一键重试整稿/)
  assert.equal(creativeFailureRecovery('creative_future_unknown'), undefined)
})

test('creative failure diagnostics expose only safe code and field path identifiers', () => {
  assert.deepEqual(creativeSafeFailureDiagnostic({
    error_code: 'creative_storyboard_validation_failed',
    validation_error: { code: 'field_too_long', path: 'shots[0].narrative_en', detail: 'secret model output' },
  }), { code: 'field_too_long', path: 'shots[0].narrative_en' })
  assert.deepEqual(creativeSafeFailureDiagnostic({
    error_code: 'BAD CODE: raw parser detail', validation_path: 'Traceback: /tmp/key',
  }), { code: '', path: '' })
  assert.match(creativeFailureRecovery('creative_storyboard_validation_failed') || '', /字段长度校验/)
  assert.match(creativeFailureRecovery('deepseek_api_authentication_failed') || '', /鉴权失败/)
})

test('creative request errors distinguish timeout, busy, unavailable and safely redact runtime parser details', () => {
  const timeout = creativeDraftRequestError(
    { code: 'ECONNABORTED', message: 'timeout of 1020000ms exceeded' }, 'fallback',
  )
  assert.match(timeout, /不等于服务端已取消/)
  assert.match(timeout, /一键重试整稿/)
  assert.match(timeout, /17 分钟/)
  assert.match(timeout, /16 分钟硬上限/)
  assert.doesNotMatch(timeout, /1020000ms/)

  const busy = creativeDraftRequestError({
    response: { status: 409, data: { error: { code: 'creative_resource_busy', details: { detail: 'busy' } } } },
  }, 'fallback')
  assert.match(busy, /正在占用资源/)
  assert.match(busy, /正式分镜未修改/)

  const unavailable = creativeDraftRequestError({
    response: { status: 503, data: { error: { code: 'creative_runtime_unavailable', details: { detail: ['model missing'] } } } },
  }, 'fallback')
  assert.match(unavailable, /运行环境不可用或模型未就绪/)

  const malformedOutput = creativeDraftRequestError({
    response: { status: 502, data: { code: 'creative_output_invalid_json', detail: 'Invalid control character at line 1 column 94' } },
  }, 'fallback')
  assert.match(malformedOutput, /格式不完整/)
  assert.match(malformedOutput, /一键重试整稿/)
  assert.match(malformedOutput, /1–2 镜/)
  assert.match(malformedOutput, /缩短本次创作要求/)
  assert.match(malformedOutput, /正式分镜未修改/)
  assert.doesNotMatch(malformedOutput, /Invalid control character/)

  const failed = creativeDraftRequestError({
    response: { status: 502, data: { code: 'creative_runtime_failed', detail: 'strict JSON validation failed at byte 81' } },
  }, 'fallback')
  assert.match(failed, /本地 Qwen 运行失败/)
  assert.match(failed, /一键重试整稿/)
  assert.match(failed, /正式分镜未修改/)
  assert.doesNotMatch(failed, /strict JSON validation failed/)

  const runtimeJson = creativeDraftRequestError({
    response: { status: 502, data: { code: 'creative_invalid_runtime_json', detail: 'JSONDecodeError: line 1 column 14' } },
  }, 'fallback')
  assert.match(runtimeJson, /响应格式异常/)
  assert.doesNotMatch(runtimeJson, /JSONDecodeError|line 1/)

  const reviewAck = creativeDraftRequestError({
    response: { status: 400, data: { code: 'creative_review_acknowledgement_required', detail: 'raw issue payload' } },
  }, 'fallback')
  assert.match(reviewAck, /已阅读并承担风险/)
  assert.doesNotMatch(reviewAck, /raw issue payload/)

  const requiresEdit = creativeDraftRequestError({
    response: { status: 400, data: { code: 'creative_review_requires_edit', detail: 'unknown FK 88' } },
  }, 'fallback')
  assert.match(requiresEdit, /未知素材、说话人或无效数据/)
  assert.match(requiresEdit, /正式分镜未修改/)
  assert.doesNotMatch(requiresEdit, /unknown FK/)

  const unknown = creativeDraftRequestError({
    response: { status: 500, data: { detail: 'Invalid control character at line 1 column 94' } },
  }, '安全兜底提示')
  assert.equal(unknown, '安全兜底提示')

  const exhausted = creativeDraftRequestError({
    response: { status: 502, data: { error: { code: 'creative_output_budget_exhausted' } } },
  }, 'fallback')
  assert.match(exhausted, /动态输出容量/)
  assert.match(exhausted, /1–2 镜/)
  assert.match(exhausted, /权威对白边界/)
  assert.match(exhausted, /不要按原设置重复提交/)

  const repairNotStarted = creativeDraftRequestError({
    response: { status: 502, data: { code: 'creative_repair_not_started' } },
  }, 'fallback')
  assert.match(repairNotStarted, /修复阶段未能启动/)
  assert.match(repairNotStarted, /缩短本次创作要求/)
  assert.match(repairNotStarted, /正式分镜未修改/)
})

test('creative workbench hides the editor for zero-shot failures and keeps one-click retry explicit', () => {
  const source = readFileSync(new URL('../src/components/CreativeDraftWorkbench.vue', import.meta.url), 'utf8')
  assert.ok(source.includes('v-if="emptyFailedDraft"'))
  assert.ok(source.includes('0 镜”只是保留用于追踪的失败记录，不是可保存的空草稿'))
  assert.ok(source.includes('一键重试整稿'))
  assert.ok(source.includes('creativeFailureRecovery(failedDraftErrorCode.value, activeDraft.value?.contextCurrent !== false)'))
  assert.ok(source.includes('creativeSafeFailureDiagnostic(activeDraft.value?.runtimeMetadata)'))
  assert.ok(source.includes('failedDraftDiagnostic.code'))
  assert.ok(source.includes('failedDraftDiagnostic.path'))
  assert.ok(source.includes('本地 Qwen 未返回可审核的结构化镜头。请一键重试整稿；若持续失败，改为 1–2 镜或缩短本次创作要求。正式分镜未修改。'))
  assert.ok(!source.includes('|| activeDraft.value?.errorMessage'))
  assert.ok(!source.includes('|| activeDraft.value?.validation.errors[0]'))
  assert.ok(source.includes('<template v-else>'))
  assert.ok(source.includes('persistedDraftIdFromError(cause) || current.id'))
  const checkScript = readFileSync(new URL('../../scripts/check.ps1', import.meta.url), 'utf8')
  assert.ok(checkScript.includes('npm run test:creative'))
  assert.ok(checkScript.includes("Assert-NativeSuccess 'Creative draft failure recovery tests'"))
})

test('creative generation and regeneration wait 17 minutes and expose the CPU 1-4 shot limit', () => {
  const repositorySource = readFileSync(new URL('../src/services/creativeDraftRepository.ts', import.meta.url), 'utf8')
  const componentSource = readFileSync(new URL('../src/components/CreativeDraftWorkbench.vue', import.meta.url), 'utf8')
  assert.ok(repositorySource.includes('const CREATIVE_GENERATION_TIMEOUT_MS = 1_020_000'))
  assert.equal(repositorySource.match(/timeout: CREATIVE_GENERATION_TIMEOUT_MS/g)?.length, 2)
  assert.ok(repositorySource.includes('maxShots: Number(raw.max_shots || 4)'))
  assert.ok(repositorySource.includes("device: 'cpu', minShots: 1, maxShots: 4"))
  assert.ok(repositorySource.includes('baselineMaxNewTokens: Number(raw.baseline_max_new_tokens || 0)'))
  assert.ok(repositorySource.includes('configuredMaxNewTokens: Number(raw.configured_max_new_tokens || raw.max_new_tokens || 0)'))
  assert.ok(repositorySource.includes('baselineMaxNewTokens: 2304'))
  assert.ok(repositorySource.includes('configuredMaxNewTokens: 3072'))
  assert.ok(!repositorySource.includes('660_000'))
  assert.ok(componentSource.includes('capability.value?.maxShots || 4'))
  assert.ok(componentSource.includes('镜总时长约 {{ targetDurationRange.min }}–{{ targetDurationRange.max }} 秒（每镜 5–15 秒）'))
  assert.ok(componentSource.includes('页面等待至 17 分钟'))
  assert.ok(componentSource.includes('1–2 镜更适合反复迭代；4 镜实测约 12 分钟'))
  assert.ok(componentSource.includes('输出容量由镜数 + 剧本中的权威对白共同决定'))
  assert.ok(componentSource.includes('预计超过上限会在加载模型前要求拆段'))
})
