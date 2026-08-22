import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  adaptSeriesConceptDraft,
  SERIES_CONCEPT_MAX_IDEA_CHARS,
  seriesConceptAcceptedFields,
  seriesConceptCandidateErrors,
  seriesConceptDraftInputErrors,
  seriesConceptDraftPayload,
  seriesConceptDraftRequestError,
  seriesConceptDraftRequestFailure,
  seriesConceptPlanningEstimate,
  seriesConceptSourceSnapshot,
} from '../src/services/seriesConceptDraft.ts'
import { adaptPromptProviderCapability } from '../src/services/providerCapabilities.ts'

const masterScript = '\n第1集：雨夜来客\n镜头1【时长=8秒】【首帧连续=否】【尾帧连续=否】：客栈门外\n[0-8秒] 动作：沈砚推门而入\n'
const rawResult = {
  source_fingerprint: 'a'.repeat(64),
  candidate: {
    title: '听雨不归人',
    logline: '失忆剑客循着雨声中的死亡记忆追查自己的身份。',
    master_script: masterScript,
    bible: {
      characters: [{
        name: '沈砚',
        description: '失忆剑客，循雨声追查身份。',
        visual_identity_en: 'young wuxia swordsman, rain-soaked dark robe, restrained expression',
      }],
      scenes: [{
        name: '雨夜客栈',
        description: '山路尽头的旧客栈，雨声不断。',
        visual_identity_en: 'old roadside inn at rainy night, warm lanterns, misty mountain path',
      }],
      props: [{
        name: '裂纹铜铃',
        description: '能引出死亡记忆的旧铜铃。',
        visual_identity_en: 'cracked antique bronze bell, worn red cord, subtle patina',
      }],
    },
  },
  format: {
    schema_version: 2,
    template_id: 'deterministic-script-breakdown-v3',
    compatible: true,
    episode_count: 1,
    shot_count: 1,
    episode_duration_seconds: 60,
    total_duration_ms: 8000,
    shot_durations_ms: [[8000]],
  },
  resource_estimate: {
    total_shots: 1,
    estimated_input_chars: 480,
    estimated_output_tokens: 1024,
    configured_output_token_hint: 4096,
    output_hint_utilization_percent: 25,
  },
  warnings: ['创建角色 Bible 后再自动拆解对白。'],
  runtime: {
    provider: 'deepseek_api',
    model: 'deepseek-chat',
    transport: 'remote_api',
    elapsed_ms: 1234,
    review_required: true,
    persisted: false,
  },
}

test('series concept adapter preserves the complete script and compatibility metadata', () => {
  const result = adaptSeriesConceptDraft(rawResult)
  assert.equal(result.candidate.masterScript, masterScript)
  assert.equal(result.candidate.bible.characters[0].name, '沈砚')
  assert.equal(result.candidate.bible.scenes[0].visualIdentityEn.includes('rainy night'), true)
  assert.equal(result.candidate.bible.props.length, 1)
  assert.equal(result.candidate.masterScript.startsWith('\n'), true)
  assert.equal(result.candidate.masterScript.endsWith('\n'), true)
  assert.equal(result.format.schemaVersion, 2)
  assert.equal(result.format.templateId, 'deterministic-script-breakdown-v3')
  assert.equal(result.format.compatible, true)
  assert.equal(result.format.shotCount, 1)
  assert.equal(result.format.episodeDurationSeconds, 60)
  assert.deepEqual(result.format.shotDurationsMs, [[8000]])
  assert.equal(result.resourceEstimate.estimatedOutputTokens, 1024)
  assert.equal(result.runtime.provider, 'deepseek_api')
  assert.equal(result.runtime.persisted, false)
})

test('resource hint utilization may exceed 100 percent without rejecting the candidate', () => {
  const result = adaptSeriesConceptDraft({
    ...rawResult,
    resource_estimate: {
      ...rawResult.resource_estimate,
      estimated_output_tokens: 5_000,
      configured_output_token_hint: 4_096,
      output_hint_utilization_percent: 123,
    },
  })
  assert.equal(result.resourceEstimate.outputHintUtilizationPercent, 123)
})

test('series concept adapter rejects uncontracted camelCase response aliases', () => {
  assert.throws(() => adaptSeriesConceptDraft({
    sourceFingerprint: 'b'.repeat(64),
    candidate: { title: '折灯', logline: '守灯人用最后一盏灯改写城中人的命运。', masterScript },
    format: {
      schemaVersion: 2, templateId: 'deterministic-script-breakdown-v3', compatible: true,
      episodeCount: 1, shotCount: 1, episodeDurationSeconds: 60,
      totalDurationMs: 8000, shotDurationsMs: [[8000]],
    },
    resourceEstimate: {
      totalShots: 1, estimatedInputChars: 480, estimatedOutputTokens: 1024,
      configuredOutputTokenHint: 4096, outputHintUtilizationPercent: 25,
    },
    runtime: {
      provider: 'local_qwen', model: 'Qwen', transport: 'local_process', elapsedMs: null,
      reviewRequired: true, persisted: false,
    },
  }), /可审核且兼容/)
})

test('series concept adapter rejects incomplete, incompatible, or implicitly persisted results', () => {
  assert.throws(() => adaptSeriesConceptDraft({ ...rawResult, candidate: { ...rawResult.candidate, master_script: '' } }), /可审核且兼容/)
  assert.throws(() => adaptSeriesConceptDraft({ ...rawResult, candidate: { ...rawResult.candidate, bible: undefined } }), /可审核且兼容/)
  assert.throws(() => adaptSeriesConceptDraft({
    ...rawResult,
    candidate: {
      ...rawResult.candidate,
      bible: {
        ...rawResult.candidate.bible,
        characters: [{ ...rawResult.candidate.bible.characters[0], name: '' }],
      },
    },
  }), /可审核且兼容/)
  assert.throws(() => adaptSeriesConceptDraft({ ...rawResult, format: { ...rawResult.format, compatible: false } }), /可审核且兼容/)
  assert.throws(() => adaptSeriesConceptDraft({
    ...rawResult,
    format: { ...rawResult.format, schema_version: 1, template_id: 'deterministic-script-breakdown-v2' },
  }), /可审核且兼容/)
  assert.throws(() => adaptSeriesConceptDraft({ ...rawResult, runtime: { ...rawResult.runtime, persisted: true } }), /可审核且兼容/)
  const { persisted: _persisted, ...runtimeWithoutPersisted } = rawResult.runtime
  assert.throws(() => adaptSeriesConceptDraft({ ...rawResult, runtime: runtimeWithoutPersisted }), /可审核且兼容/)
  assert.throws(() => adaptSeriesConceptDraft({ ...rawResult, runtime: { ...rawResult.runtime, review_required: false } }), /可审核且兼容/)
  assert.throws(() => adaptSeriesConceptDraft({ ...rawResult, source_fingerprint: 'not-a-fingerprint' }), /可审核且兼容/)
  assert.throws(() => adaptSeriesConceptDraft({ ...rawResult, format: { ...rawResult.format, total_duration_ms: 7000 } }), /可审核且兼容/)
  assert.throws(() => adaptSeriesConceptDraft({ ...rawResult, resource_estimate: { ...rawResult.resource_estimate, total_shots: 2 } }), /可审核且兼容/)
  assert.throws(() => adaptSeriesConceptDraft({
    ...rawResult,
    format: {
      ...rawResult.format,
      episode_count: 2,
      shot_count: 4,
      episode_duration_seconds: 10,
      total_duration_ms: 20_000,
      shot_durations_ms: [[6_000, 6_000], [4_000, 4_000]],
    },
    resource_estimate: {
      ...rawResult.resource_estimate,
      total_shots: 4,
    },
  }), /可审核且兼容/)
  assert.throws(() => adaptSeriesConceptDraft({
    ...rawResult,
    resource_estimate: { ...rawResult.resource_estimate, output_hint_utilization_percent: 26 },
  }), /可审核且兼容/)
})

test('phase errors are actionable and never claim a product count maximum', () => {
  const failure = (code: string, provider: 'local_qwen' | 'deepseek_api' = 'local_qwen') => (
    seriesConceptDraftRequestError({ response: { data: { error: { code } } } }, provider)
  )
  assert.match(failure('series_concept_model_load_failed'), /模型加载失败/)
  assert.match(failure('series_concept_inference_failed'), /推理失败/)
  assert.match(failure('series_concept_output_invalid_json'), /结构化候选/)
  assert.match(failure('series_concept_validation_failed'), /兼容模板校验/)
  assert.match(failure('series_concept_resource_busy'), /刷新状态或稍后直接重试/)
  assert.match(failure('series_concept_parent_timeout'), /等待时限/)
  assert.match(failure('series_concept_runtime_protocol_failed'), /无效协议响应/)
  assert.match(failure('series_concept_runtime_exit'), /进程异常退出/)
  assert.match(failure('series_concept_provider_network_failed', 'deepseek_api'), /连接 DeepSeek API 失败/)
  assert.match(failure('series_concept_provider_rate_limited', 'deepseek_api'), /限流/)
  assert.match(failure('series_concept_provider_request_rejected', 'deepseek_api'), /完整请求/)
  assert.match(failure('series_concept_provider_invalid_response', 'deepseek_api'), /无效响应/)
  assert.match(failure('series_concept_draft_output_invalid_json', 'deepseek_api'), /DeepSeek 返回内容/)
  assert.match(failure('series_concept_draft_empty_output', 'deepseek_api'), /没有返回候选正文/)
  const runtime = failure('series_concept_runtime_failed')
  assert.ok(!runtime.includes('减少集数或每集镜头数'))
  assert.ok(!runtime.includes('分批'))
  assert.match(runtime, /手工表单和上一版候选均未修改/)
})

test('truncated DeepSeek output exposes only the allow-listed expandable diagnostic', () => {
  const secret = 'PRIVATE-AUTHORED-STORY-SENTINEL'
  const redactedShape = '{"title":"<redacted_text>","episodes":[{"shots":[{"action":"<unterminated_text>"'
  const failure = seriesConceptDraftRequestFailure({
    response: {
      data: {
        error: {
          code: 'series_concept_provider_output_truncated',
          details: {
            detail: secret,
            raw_body: secret,
            diagnostic: {
              diagnostic_id: 'a'.repeat(32),
              provider: 'deepseek_api',
              error_code: 'series_concept_provider_output_truncated',
              failure_phase: 'provider_output',
              finish_reason: 'length',
              usage: {
                prompt_tokens: 781,
                completion_tokens: 8192,
                total_tokens: 8973,
                raw_provider_content: secret,
              },
              details_available: true,
              details: {
                attempts: 1,
                elapsed_ms: 115000,
                incomplete: true,
                requested_max_tokens: 384000,
                configured_max_tokens: 384000,
                configured_timeout_seconds: 900,
                output: {
                  character_count: 32768,
                  utf8_byte_count: 65536,
                  sha256: 'b'.repeat(64),
                  redacted_json_shape: redactedShape,
                  raw_content: secret,
                },
                api_key: secret,
              },
              unexpected_transport_field: secret,
            },
          },
        },
      },
    },
  }, 'deepseek_api')

  assert.match(failure.message, /达到当前连续剧模型的输出能力/)
  assert.match(failure.message, /不是集数或镜头数的产品上限/)
  assert.match(failure.message, /单次请求/)
  assert.match(failure.message, /上一版候选均未修改/)
  assert.equal(failure.diagnostic?.diagnosticId, 'a'.repeat(32))
  assert.equal(failure.diagnostic?.finishReason, 'length')
  assert.equal(failure.diagnostic?.usage?.completionTokens, 8192)
  assert.equal(failure.diagnostic?.details.configuredMaxTokens, 384000)
  assert.equal(failure.diagnostic?.details.configuredTimeoutSeconds, 900)
  assert.equal(failure.diagnostic?.details.output?.sha256, 'b'.repeat(64))
  assert.equal(failure.diagnostic?.details.output?.redactedJsonShape, redactedShape)
  assert.ok(!JSON.stringify(failure).includes(secret))
})

test('invalid JSON diagnostics keep parser coordinates and reject malformed or mismatched detail', () => {
  const baseDiagnostic = {
    diagnostic_id: 'c'.repeat(32),
    provider: 'deepseek_api',
    error_code: 'series_concept_draft_output_invalid_json',
    failure_phase: 'provider_decode',
    finish_reason: 'stop',
    details_available: true,
    details: {
      output: {
        character_count: 901,
        utf8_byte_count: 1602,
        sha256: 'd'.repeat(64),
        redacted_json_shape: '{"title":"PRIVATE-AUTHORED-STORY"}',
        json_decode_line: 8,
        json_decode_column: 17,
        json_decode_position: 433,
      },
    },
  }
  const wrapped = (diagnostic: Record<string, unknown>) => ({
    response: { data: { error: {
      code: 'series_concept_draft_output_invalid_json',
      details: { detail: 'private server detail', diagnostic },
    } } },
  })
  const failure = seriesConceptDraftRequestFailure(wrapped(baseDiagnostic), 'deepseek_api')

  assert.match(failure.message, /无法解析为结构化候选/)
  assert.equal(failure.diagnostic?.failurePhase, 'provider_decode')
  assert.equal(failure.diagnostic?.details.output?.jsonDecodeLine, 8)
  assert.equal(failure.diagnostic?.details.output?.jsonDecodeColumn, 17)
  assert.equal(failure.diagnostic?.details.output?.jsonDecodePosition, 433)
  assert.equal(failure.diagnostic?.details.output?.redactedJsonShape, undefined)
  assert.ok(!JSON.stringify(failure).includes('PRIVATE-AUTHORED-STORY'))
  assert.ok(!JSON.stringify(failure).includes('private server detail'))

  for (const diagnostic of [
    { ...baseDiagnostic, diagnostic_id: 'invalid' },
    { ...baseDiagnostic, provider: 'local_qwen' },
    { ...baseDiagnostic, error_code: 'series_concept_generation_failed' },
  ]) {
    assert.equal(
      seriesConceptDraftRequestFailure(wrapped(diagnostic), 'deepseek_api').diagnostic,
      null,
    )
  }
})

test('generation payload uses the exact selected channel and never truncates the idea', () => {
  const idea = `  ${'雨'.repeat(SERIES_CONCEPT_MAX_IDEA_CHARS - 4)}  `
  const payload = seriesConceptDraftPayload({
    idea,
    provider: 'deepseek_api',
    episodeCount: 3,
    shotsPerEpisode: 4,
    episodeDurationSeconds: 60,
    styleHint: '新武侠；克制冷色',
    attempt: 2,
  })
  assert.equal(payload.idea, idea.trim())
  assert.equal(payload.idea.length, SERIES_CONCEPT_MAX_IDEA_CHARS - 4)
  assert.equal(payload.provider, 'deepseek_api')
  assert.equal(payload.episode_count, 3)
  assert.equal(payload.shots_per_episode, 4)
  assert.equal(payload.episode_duration_seconds, 60)
  assert.equal(payload.style_hint, '新武侠；克制冷色')
  assert.equal(payload.attempt, 2)
})

test('counts have no product maximum while physical duration remains explicit', () => {
  const base = {
    idea: '一个雨夜复仇故事', provider: 'local_qwen' as const,
    episodeCount: 100, shotsPerEpisode: 20, episodeDurationSeconds: 120,
    styleHint: '', attempt: 1,
  }
  const limits = { minimumShotDurationMs: 5000 }
  assert.deepEqual(seriesConceptDraftInputErrors(base, limits), [])
  assert.ok(seriesConceptDraftInputErrors({ ...base, idea: '雨'.repeat(SERIES_CONCEPT_MAX_IDEA_CHARS + 1) }).some((item) => item.includes('6,000')))
  assert.deepEqual(seriesConceptDraftInputErrors({ ...base, episodeCount: 500, shotsPerEpisode: 50, episodeDurationSeconds: 250 }, limits), [])
  assert.ok(seriesConceptDraftInputErrors({ ...base, shotsPerEpisode: 13, episodeDurationSeconds: 60 }, limits).some((item) => item.includes('至少需要 65 秒')))
  assert.ok(seriesConceptDraftInputErrors({ ...base, episodeDurationSeconds: 0 }, limits).some((item) => item.includes('正整数秒')))
  assert.equal(SERIES_CONCEPT_MAX_IDEA_CHARS, 6000)
})

test('planning estimate is coarse, non-blocking, and scales without a count cap', () => {
  const local = seriesConceptPlanningEstimate({
    provider: 'local_qwen', episodeCount: 3, shotsPerEpisode: 4, episodeDurationSeconds: 60,
  }, { configuredMaxNewTokens: 8192 })
  const large = seriesConceptPlanningEstimate({
    provider: 'deepseek_api', episodeCount: 100, shotsPerEpisode: 20, episodeDurationSeconds: 90,
  }, { configuredMaxNewTokens: 100_000 })
  assert.equal(local.totalShots, 12)
  assert.equal(local.plannedTotalDurationSeconds, 180)
  assert.equal(local.deepseekCostLevel, '不适用')
  assert.equal(large.totalShots, 2000)
  assert.equal(large.estimatedWaitLevel, '很长')
  assert.equal(large.localResourcePressureLevel, '低')
  assert.equal(large.deepseekCostLevel, '高')
  assert.ok(large.estimatedOutputTokensUpper > local.estimatedOutputTokensUpper)
})

test('source changes mark candidates stale independently of acceptance, including provider and style', () => {
  const base = {
    idea: '雨夜来客', provider: 'local_qwen' as const,
    episodeCount: 3, shotsPerEpisode: 3, episodeDurationSeconds: 60,
    styleHint: '武侠', attempt: 1,
  }
  const snapshot = seriesConceptSourceSnapshot(base)
  assert.equal(seriesConceptSourceSnapshot({ ...base, attempt: 9 }), snapshot)
  assert.notEqual(seriesConceptSourceSnapshot({ ...base, idea: '雪夜来客' }), snapshot)
  assert.notEqual(seriesConceptSourceSnapshot({ ...base, provider: 'deepseek_api' }), snapshot)
  assert.notEqual(seriesConceptSourceSnapshot({ ...base, episodeDurationSeconds: 90 }), snapshot)
  assert.notEqual(seriesConceptSourceSnapshot({ ...base, styleHint: '悬疑' }), snapshot)
})

test('explicit acceptance returns one detached three-field patch', () => {
  const candidate = adaptSeriesConceptDraft(rawResult).candidate
  const accepted = seriesConceptAcceptedFields(candidate)
  assert.deepEqual(accepted, {
    title: rawResult.candidate.title,
    logline: rawResult.candidate.logline,
    masterScript,
  })
  assert.equal('bible' in accepted, false)
  assert.notEqual(accepted, candidate)
  accepted.title = '人工修改'
  assert.equal(candidate.title, '听雨不归人')
  assert.deepEqual(seriesConceptCandidateErrors(candidate), [])
  assert.ok(seriesConceptCandidateErrors({ ...candidate, logline: '雨'.repeat(501) }).some((item) => item.includes('500')))
})

test('repository reuses provider capability, has no request timeout, and mock generation fails explicitly', () => {
  const source = readFileSync(new URL('../src/services/seriesConceptDraftRepository.ts', import.meta.url), 'utf8')
  assert.ok(source.includes("api.get('/capabilities/creative-drafts/')"))
  assert.ok(source.includes('adaptPromptProviders(raw)'))
  assert.ok(source.includes('raw.series_concept_draft_supported === true'))
  assert.ok(source.includes('raw.series_concept_draft_review_required === true'))
  assert.ok(source.includes('raw.series_concept_draft_persisted !== false'))
  assert.ok(source.includes('raw.series_concept_draft_preferred_provider'))
  assert.ok(source.includes('raw.series_concept_draft_limits'))
  assert.ok(source.includes('limits.default_episode_duration_seconds'))
  assert.ok(source.includes('limits.minimum_shot_duration_ms'))
  assert.ok(source.includes('limits.maximum_shot_duration_ms'))
  assert.ok(source.includes("'/series-concept-drafts/generate/'"))
  assert.ok(source.includes('{ timeout: 0 }'))
  assert.ok(source.includes('Mock 模式不生成或伪造连续剧候选'))
  assert.ok(!source.includes('mockCandidate'))
})

test('series model suitability is capability-driven guidance rather than a generation gate', () => {
  const experimental = adaptPromptProviderCapability({
    id: 'local_qwen', label: '本地模型', enabled: true, configured: true, ready: true,
    model: 'operator-selected-model', transport: 'local',
    series_concept_draft_suitability: 'experimental',
    series_concept_draft_suitability_reason_code: 'strict_structure_not_recommended',
    series_concept_draft_suitability_notice: '该部署仅建议用于短内容试验。',
  }, 'local_qwen')
  const recommended = adaptPromptProviderCapability({
    id: 'deepseek_api', label: '远端模型', enabled: true, configured: true, ready: true,
    model: 'operator-selected-remote', transport: 'remote_api',
    series_concept_draft_suitability: 'recommended',
    series_concept_draft_suitability_reason_code: 'strict_structure_recommended',
    series_concept_draft_suitability_notice: '适合完整结构候选。',
  }, 'deepseek_api')
  assert.equal(experimental.seriesConceptDraftSuitability, 'experimental')
  assert.equal(experimental.seriesConceptDraftSuitabilityReasonCode, 'strict_structure_not_recommended')
  assert.equal(experimental.seriesConceptDraftSuitabilityNotice, '该部署仅建议用于短内容试验。')
  assert.equal(experimental.ready, true)
  assert.equal(recommended.seriesConceptDraftSuitability, 'recommended')
  assert.equal(recommended.ready, true)
  const uncontractedAlias = adaptPromptProviderCapability({
    id: 'local_qwen', enabled: true, configured: true, ready: true,
    model: 'operator-selected-model', transport: 'local',
    seriesConceptDraftSuitability: 'recommended',
  }, 'local_qwen')
  assert.equal(uncontractedAlias.seriesConceptDraftSuitability, undefined)
})

test('new-project workbench keeps a detached editable candidate behind explicit acceptance', () => {
  const component = readFileSync(new URL('../src/components/SeriesConceptDraftWorkbench.vue', import.meta.url), 'utf8')
  const studio = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  assert.ok(component.includes('v-model="candidate.title"'))
  assert.ok(component.includes('v-model="candidate.logline"'))
  assert.ok(component.includes('v-model="candidate.masterScript"'))
  assert.ok(component.includes('连续性 Bible 候选'))
  assert.ok(component.includes('candidate.value.bible.characters'))
  assert.ok(component.includes('visualIdentityEn'))
  assert.ok(component.includes('创建项目后仍需在 Bible 审核页人工确认'))
  assert.ok(component.includes('确认并填入故事基础'))
  assert.ok(component.includes("emit('accept', accepted)"))
  assert.ok(component.includes('generatedSourceSnapshot.value !== currentSourceSnapshot.value'))
  assert.ok(component.includes('你仍可人工确认采用'))
  assert.ok(component.includes('let requestSequence = 0'))
  assert.ok(component.includes('sequence !== requestSequence'))
  assert.ok(component.includes('上一版候选仍在'))
  assert.ok(component.includes('外部 API 数据发送提示'))
  assert.ok(component.includes('v-model.number="episodeDurationSeconds"'))
  assert.ok(component.includes('前端粗估 · 仅供规划'))
  assert.ok(component.includes('DeepSeek 可能费用级别'))
  assert.ok(component.includes('服务端本次资源估算'))
  assert.ok(component.includes('result.resourceEstimate.outputHintUtilizationPercent'))
  assert.ok(component.includes('可超过 100%，仅提示资源压力，不会禁用生成'))
  const readiness = component.match(/const generationReady = computed\([\s\S]*?\n\)\)/)?.[0] || ''
  assert.ok(readiness)
  assert.ok(!readiness.includes('planningEstimate'))
  assert.ok(!component.includes('peakBatchInputChars'))
  assert.ok(!component.includes('peakBatchOutputTokens'))
  assert.ok(!component.includes('estimatedModelCalls'))
  assert.ok(!component.includes('安全分批'))
  assert.ok(component.includes('正整数；不设产品数量上限'))
  assert.ok(!component.includes('SERIES_CONCEPT_MAX_EPISODES'))
  assert.ok(!component.includes('SERIES_CONCEPT_MAX_SHOTS_PER_EPISODE'))
  assert.ok(!component.includes('SERIES_CONCEPT_MAX_TOTAL_SHOTS'))
  assert.ok(component.includes('selectedProviderCapability.value?.temporarilyBusy'))
  assert.ok(component.includes('!provider.ready && !provider.temporarilyBusy'))
  assert.ok(component.includes('刷新状态'))
  assert.ok(component.includes('next.preferredProvider'))
  assert.ok(component.includes('providerSelectionEdited.value'))
  assert.ok(component.includes("seriesConceptDraftSuitability==='experimental'"))
  assert.ok(component.includes('这只是能力提示，不会限制集数、镜头数或阻止提交'))
  assert.ok(component.includes('const generationDiagnostic = ref<SeriesConceptDiagnostic | null>(null)'))
  assert.ok(component.includes('seriesConceptDraftRequestFailure(error, requestInput.provider)'))
  assert.ok(component.includes('<details v-if="generationDiagnostic"'))
  assert.ok(component.includes('查看技术详情'))
  assert.ok(component.includes('不含故事正文、提示词、请求头或密钥'))
  assert.ok(component.includes('generationDiagnostic.details.output.redactedJsonShape'))
  assert.ok(!component.includes('v-html'))
  assert.ok(!component.includes('JSON.stringify(generationDiagnostic'))
  const failedRequestBlock = component.match(/} catch \(error\) \{[\s\S]*?} finally \{/)?.[0] || ''
  assert.ok(failedRequestBlock)
  assert.ok(!failedRequestBlock.includes('candidate.value = null'))
  assert.ok(!failedRequestBlock.includes('result.value = null'))
  assert.ok(!failedRequestBlock.includes('acceptedCandidateSnapshot.value ='))
  assert.ok(failedRequestBlock.includes('generationDiagnostic.value = failure.diagnostic'))
  const discardBlock = component.match(/const discard = \(\) => \{[\s\S]*?\n}/)?.[0] || ''
  assert.ok(discardBlock.includes('generationDiagnostic.value = null'))
  assert.ok(studio.includes('<SeriesConceptDraftWorkbench :style-hint="newProjectStyleHint" @accept="handleSeriesConceptAccepted"/>'))
  assert.ok(studio.includes('newProject.value = { ...newProject.value, ...seriesConceptAcceptedFields(candidate) }'))
  assert.ok(studio.includes('class="button primary" :disabled="createLoading" @click="createProject"'))
})
