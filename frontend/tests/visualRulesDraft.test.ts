import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  VISUAL_RULE_COLORS,
  VISUAL_RULE_CONTRAST,
  VISUAL_RULE_EXPOSURE,
  VISUAL_RULE_LIGHTING,
  VISUAL_RULE_PALETTE_RELATIONS,
  VISUAL_RULE_PROGRESSION,
  VISUAL_RULE_SATURATION,
  adaptVisualRulesDraftApplyResult,
  adaptVisualRulesDraftResult,
  visualRulesColorDesignErrors,
  visualRulesColorDesignPayload,
  visualRulesDraftApplyPayload,
  visualRulesDraftError,
  visualRulesDraftGeneratePayload,
  visualRulesDraftValidationErrors,
  visualRulesLocalContextSnapshot,
  visualRulesProjectPersistenceFields,
  visualRulesVariationNoteErrors,
} from '../src/services/visualRulesDraft.ts'
import type { SeriesProject, StylePreset, VisualRulesColorDesign } from '../src/types/index.ts'

const project = (): SeriesProject => ({
  id: '11111111-1111-4111-8111-111111111111',
  title: '听雨不归人', logline: '雨夜追查旧案', masterScript: '第1集：雨夜试剑', isFavorite: false,
  continuitySummary: 'Current continuity', translationGlossary: { 玄纹剑: 'Xuanwen Sword' },
  styleId: 'wuxia', status: 'writing', aspectRatio: '9:16', resolution: '720p',
  width: 768, height: 1344, defaultMode: 'Ref2VA',
  bible: {
    negativePrompt: 'watermark', colorScript: 'Current color',
    characters: [{ id: 'c1', name: '沈砚', description: '剑客', visualPrompt: '灰白窄袖袍', referenceAssetIds: [], locked: true }],
    scenes: [], props: [],
  },
  episodes: [{ id: 'e1', number: 1, title: '雨夜试剑', synopsis: '沈砚进客栈', script: '不参与视觉规则', status: 'draft', multiShot: true, audioEnabled: true, shots: [], createdAt: '2026-08-09T00:00:00Z' }],
  createdAt: '2026-08-09T00:00:00Z', updatedAt: '2026-08-09T00:00:00Z',
})

const style: StylePreset = {
  id: 'wuxia', slug: 'wuxia', name: '武侠', englishName: 'Wuxia', category: '真人影视',
  description: '冷峻雨夜江湖', accent: '#d99b48', gradient: '', tags: [],
  assetCounts: { character: 0, scene: 0, prop: 0 }, promptPrefix: 'cinematic wuxia',
}

const colorDesign: VisualRulesColorDesign = {
  baseColors: ['teal', 'charcoal'], accentColor: 'amber', paletteRelation: 'cinematic',
  lighting: 'low_key', contrast: 'restrained', saturation: 'muted', exposure: 'dark',
  progression: 'cool_to_warm', preserveNaturalSkinExposure: true,
}

test('visual rules draft adapters keep the three independent fields and field modes', () => {
  const adapted = adaptVisualRulesDraftResult({
    project_id: project().id, context_fingerprint: 'a'.repeat(64), context_current: true,
    draft: { negative_prompt: 'no watermark', color_script: 'cool cyan-gray', continuity_summary: 'stable identity and architecture' },
    field_modes: { negative_prompt: 'deterministic_guard', color_script: 'structured_design', continuity_summary: 'deterministic_guard' },
    color_design: visualRulesColorDesignPayload(colorDesign),
    warnings: ['review'],
    source_summary: { project_fields: ['title'], production_setting_fields: ['color_script'], style_included: true, character_count: 1, scene_count: 0, prop_count: 0, episode_summary_count: 1, episode_scripts_included: false, shots_included: false },
    generation: { attempt: 2, direction_note_provided: false, variation_note_provided: true, generation_fingerprint: 'f'.repeat(64) },
    runtime: { provider: 'local_qwen', model: 'Qwen', loaded_on_demand: true, unloaded_after_request: true, review_required: true, persisted: false, elapsed_ms: 1200, repair_attempted: true, repair_succeeded: true },
  })
  assert.deepEqual(adapted.draft, { negativePrompt: 'no watermark', colorScript: 'cool cyan-gray', continuitySummary: 'stable identity and architecture' })
  assert.deepEqual(adapted.fieldModes, { negativePrompt: 'deterministic_guard', colorScript: 'structured_design', continuitySummary: 'deterministic_guard' })
  assert.deepEqual(adapted.colorDesign, colorDesign)
  assert.equal(adapted.runtime.persisted, false)
  assert.equal(adapted.runtime.unloadedAfterRequest, true)

  const applied = adaptVisualRulesDraftApplyResult({
    project_id: project().id, applied_context_fingerprint: 'a'.repeat(64), current_context_fingerprint: 'b'.repeat(64), updated_at: '2026-08-09T12:00:00Z',
    applied: { negative_prompt: 'guard', color_script: 'color', continuity_summary: 'continuity' },
  })
  assert.deepEqual(applied.applied, { negativePrompt: 'guard', colorScript: 'color', continuitySummary: 'continuity' })
})

test('visual rules request payloads preserve attempt, variation and independent apply fields', () => {
  assert.deepEqual(visualRulesDraftGeneratePayload({ attempt: 101, variationNote: '  colder rain  ', expectedContextFingerprint: 'a'.repeat(64) }), {
    attempt: 100, variation_note: 'colder rain', expected_context_fingerprint: 'a'.repeat(64),
  })
  assert.deepEqual(visualRulesDraftApplyPayload({
    contextFingerprint: 'a'.repeat(64), generationFingerprint: 'f'.repeat(64), attempt: 2,
    variationNote: 'colder rain', colorDesign,
  }), {
    context_fingerprint: 'a'.repeat(64),
    generation_fingerprint: 'f'.repeat(64),
    attempt: 2,
    direction_note: '',
    variation_note: 'colder rain',
    color_design: visualRulesColorDesignPayload(colorDesign),
  })
  assert.deepEqual(visualRulesProjectPersistenceFields(project()), {
    negativePrompt: 'watermark', colorScript: 'Current color', continuitySummary: 'Current continuity',
  })
})

test('structured color design rejects empty, duplicate, unknown and extra values', () => {
  assert.deepEqual(visualRulesColorDesignErrors(visualRulesColorDesignPayload(colorDesign)), [])
  assert.ok(visualRulesColorDesignErrors({ ...visualRulesColorDesignPayload(colorDesign), base_colors: [] }).some((item) => item.includes('1–3')))
  assert.ok(visualRulesColorDesignErrors({ ...visualRulesColorDesignPayload(colorDesign), base_colors: ['teal', 'teal'] }).some((item) => item.includes('不能重复')))
  assert.ok(visualRulesColorDesignErrors({ ...visualRulesColorDesignPayload(colorDesign), lighting: 'detective_scene' }).some((item) => item.includes('光线枚举无效')))
  assert.ok(visualRulesColorDesignErrors({ ...visualRulesColorDesignPayload(colorDesign), injected_fact: 'silver key' }).some((item) => item.includes('未知字段')))
  assert.ok(visualRulesColorDesignErrors({ ...visualRulesColorDesignPayload(colorDesign), accent_color: 'red\u200b' }).some((item) => item.includes('强调色枚举无效')))
})

test('structured color enums exactly match the backend serializer contract', () => {
  assert.deepEqual(VISUAL_RULE_COLORS, ['black','charcoal','gray','slate','ivory','white','brown','sepia','navy','blue','cyan','teal','green','amber','gold','copper','orange','red','crimson','magenta','violet'])
  assert.deepEqual(VISUAL_RULE_PALETTE_RELATIONS, ['monochromatic','analogous','complementary','neutral','cinematic'])
  assert.deepEqual(VISUAL_RULE_LIGHTING, ['soft_diffuse','hard_directional','low_key','high_key','practical','volumetric','rim_accent','balanced_ambient'])
  assert.deepEqual(VISUAL_RULE_CONTRAST, ['low','restrained','medium','high'])
  assert.deepEqual(VISUAL_RULE_SATURATION, ['desaturated','muted','natural','selective','saturated'])
  assert.deepEqual(VISUAL_RULE_EXPOSURE, ['dark','balanced','bright'])
  assert.deepEqual(VISUAL_RULE_PROGRESSION, ['stable','cool_to_warm','warm_to_cool','dark_to_bright','bright_to_dark','muted_to_saturated','saturated_to_muted'])
})

test('variation note rejects emoji, bidi and zero-width control characters before API use', () => {
  assert.deepEqual(visualRulesVariationNoteErrors('cool cyan with restrained amber'), [])
  assert.ok(visualRulesVariationNoteErrors('cool cyan 🎬').length)
  assert.ok(visualRulesVariationNoteErrors('cool\u202Ecyan').length)
  assert.ok(visualRulesVariationNoteErrors('cool\u200Bcyan').length)
})

test('busy, conflict and runtime errors read the unified nested detail envelope', () => {
  const cases = [
    ['visual_rules_resource_busy', 'resource_busy', '视频任务执行中'],
    ['visual_rules_source_conflict', 'source_conflict', '故事源已变化'],
    ['visual_rules_runtime_unavailable', 'runtime_unavailable', '模型严格校验失败'],
  ] as const
  for (const [code, kind, detail] of cases) {
    const adapted = visualRulesDraftError({ response: { status: code === 'visual_rules_runtime_unavailable' ? 503 : 409, data: { error: { code, details: { detail: [detail] } } } } })
    assert.equal(adapted.kind, kind)
    assert.equal(adapted.message, detail)
  }
})

test('candidate validation requires complete English-only server-rendered values', () => {
  assert.deepEqual(visualRulesDraftValidationErrors({ negativePrompt: 'watermark', colorScript: 'cool cyan color and soft light', continuitySummary: 'stable identity' }), [])
  const errors = visualRulesDraftValidationErrors({ negativePrompt: '', colorScript: 'Avoid red camera shots with холодный light', continuitySummary: 'stable identity' })
  assert.ok(errors.includes('全局负面提示词不能为空'))
  assert.ok(errors.includes('色彩脚本含非拉丁文字；请使用纯英文后再采用'))
  assert.deepEqual(visualRulesDraftValidationErrors({
    negativePrompt: 'watermark',
    colorScript: 'The series uses a neutral palette built from gray, with no separate accent color. The lighting treatment is soft diffuse.',
    continuitySummary: 'Maintain consistent project-wide visual style and environmental logic across episodes.',
  }), [])
})

test('local drift guard tracks visual sources and episode summaries but ignores scripts and shots', () => {
  const initial = project()
  const snapshot = visualRulesLocalContextSnapshot(initial, style)

  const episodeSummaryChanged = structuredClone(initial)
  episodeSummaryChanged.episodes[0].synopsis = '梗概已修改'
  assert.notEqual(visualRulesLocalContextSnapshot(episodeSummaryChanged, style), snapshot)

  const scriptOnlyChanged = structuredClone(initial)
  scriptOnlyChanged.episodes[0].script = '剧集剧本变化不参与本功能'
  assert.equal(visualRulesLocalContextSnapshot(scriptOnlyChanged, style), snapshot)

  const independentContinuityChanged = structuredClone(initial)
  independentContinuityChanged.continuitySummary = 'Independent continuity changed'
  assert.notEqual(visualRulesLocalContextSnapshot(independentContinuityChanged, style), snapshot)
})

test('the full repository check gate runs the visual rules regression suite', () => {
  const checkScript = readFileSync(new URL('../../scripts/check.ps1', import.meta.url), 'utf8')
  assert.ok(checkScript.includes('npm run test:visual-rules'))
  assert.ok(checkScript.includes("Assert-NativeSuccess 'Global visual rules draft tests'"))
})
