import type {
  SeriesProject,
  StylePreset,
  VisualRulesColor,
  VisualRulesColorDesign,
  VisualRulesContrast,
  VisualRulesDraftApplyResult,
  VisualRulesDraftData,
  VisualRulesDraftResult,
  VisualRulesExposure,
  VisualRulesLighting,
  VisualRulesPaletteRelation,
  VisualRulesProgression,
  VisualRulesSaturation,
} from '@/types'

const CJK = /[\u3400-\u4dbf\u4e00-\u9fff]/
const LATIN_LETTER = /\p{Script=Latin}/u
const LETTER = /\p{L}/u

const objectValue = (value: unknown): Record<string, any> => (
  value && typeof value === 'object' ? value as Record<string, any> : {}
)

export const VISUAL_RULE_COLORS: VisualRulesColor[] = ['black','charcoal','gray','slate','ivory','white','brown','sepia','navy','blue','cyan','teal','green','amber','gold','copper','orange','red','crimson','magenta','violet']
export const VISUAL_RULE_PALETTE_RELATIONS: VisualRulesPaletteRelation[] = ['monochromatic','analogous','complementary','neutral','cinematic']
export const VISUAL_RULE_LIGHTING: VisualRulesLighting[] = ['soft_diffuse','hard_directional','low_key','high_key','practical','volumetric','rim_accent','balanced_ambient']
export const VISUAL_RULE_CONTRAST: VisualRulesContrast[] = ['low','restrained','medium','high']
export const VISUAL_RULE_SATURATION: VisualRulesSaturation[] = ['desaturated','muted','natural','selective','saturated']
export const VISUAL_RULE_EXPOSURE: VisualRulesExposure[] = ['dark','balanced','bright']
export const VISUAL_RULE_PROGRESSION: VisualRulesProgression[] = ['stable','cool_to_warm','warm_to_cool','dark_to_bright','bright_to_dark','muted_to_saturated','saturated_to_muted']

export const visualRulesColorDesignLabel = (value: string): string => ({
  none:'无独立强调色', black:'黑色', charcoal:'炭灰', gray:'灰色', slate:'板岩灰', ivory:'象牙白', white:'白色', brown:'棕色', sepia:'褐色', navy:'藏蓝', blue:'蓝色', cyan:'青色', teal:'蓝绿色', green:'绿色', amber:'琥珀色', gold:'金色', copper:'铜色', orange:'橙色', red:'红色', crimson:'绯红', magenta:'品红', violet:'紫罗兰',
  monochromatic:'单色关系', analogous:'邻近色关系', complementary:'互补色关系', neutral:'中性色关系', cinematic:'电影化关系',
  soft_diffuse:'柔和漫射光', hard_directional:'硬质方向光', low_key:'低调光', high_key:'高调光', practical:'实景光源', volumetric:'体积光', rim_accent:'受控轮廓光', balanced_ambient:'均衡环境光',
  low:'低', restrained:'克制', medium:'中等', high:'高', desaturated:'去饱和', muted:'低饱和', natural:'自然', selective:'选择性饱和', saturated:'高饱和', dark:'偏暗', balanced:'均衡', bright:'明亮',
  stable:'跨集稳定', cool_to_warm:'由冷到暖', warm_to_cool:'由暖到冷', dark_to_bright:'由暗到亮', bright_to_dark:'由亮到暗', muted_to_saturated:'由低饱和到高饱和', saturated_to_muted:'由高饱和到低饱和',
}[value] || value.replace(/_/g, ' '))

const COLOR_DESIGN_KEYS = ['base_colors','accent_color','palette_relation','lighting','contrast','saturation','exposure','progression','preserve_natural_skin_exposure'] as const
const unsafeNoteCharacters = (value: string) => /[\u200B-\u200F\u202A-\u202E\u2060-\u206F\uFEFF]/.test(value) || /\p{Extended_Pictographic}/u.test(value)

export const visualRulesColorDesignErrors = (payload: unknown): string[] => {
  const raw = objectValue(payload)
  const keys = Object.keys(raw)
  const unexpected = keys.filter((key) => !COLOR_DESIGN_KEYS.includes(key as typeof COLOR_DESIGN_KEYS[number]))
  const missing = COLOR_DESIGN_KEYS.filter((key) => !keys.includes(key))
  const errors: string[] = []
  if (unexpected.length) errors.push(`color_design 含未知字段：${unexpected.join(', ')}`)
  if (missing.length) errors.push(`color_design 缺少字段：${missing.join(', ')}`)
  const baseColors = Array.isArray(raw.base_colors) ? raw.base_colors.map(String) : []
  if (baseColors.length < 1 || baseColors.length > 3) errors.push('主色必须选择 1–3 个')
  if (new Set(baseColors).size !== baseColors.length) errors.push('主色不能重复')
  const invalidBase = baseColors.filter((value) => !VISUAL_RULE_COLORS.includes(value as VisualRulesColor))
  if (invalidBase.length) errors.push(`主色含未知枚举：${invalidBase.join(', ')}`)
  if (![...VISUAL_RULE_COLORS, 'none'].includes(String(raw.accent_color) as VisualRulesColor|'none')) errors.push('强调色枚举无效')
  if (!VISUAL_RULE_PALETTE_RELATIONS.includes(raw.palette_relation)) errors.push('色彩关系枚举无效')
  if (!VISUAL_RULE_LIGHTING.includes(raw.lighting)) errors.push('光线枚举无效')
  if (!VISUAL_RULE_CONTRAST.includes(raw.contrast)) errors.push('对比度枚举无效')
  if (!VISUAL_RULE_SATURATION.includes(raw.saturation)) errors.push('饱和度枚举无效')
  if (!VISUAL_RULE_EXPOSURE.includes(raw.exposure)) errors.push('曝光枚举无效')
  if (!VISUAL_RULE_PROGRESSION.includes(raw.progression)) errors.push('跨集变化枚举无效')
  if (typeof raw.preserve_natural_skin_exposure !== 'boolean') errors.push('自然肤色曝光选项必须是布尔值')
  return errors
}

export const adaptVisualRulesColorDesign = (payload: unknown): VisualRulesColorDesign => {
  const errors = visualRulesColorDesignErrors(payload)
  if (errors.length) throw new Error(errors.join('；'))
  const raw = objectValue(payload)
  return {
    baseColors: raw.base_colors.map(String) as VisualRulesColor[],
    accentColor: String(raw.accent_color) as VisualRulesColor|'none',
    paletteRelation: raw.palette_relation as VisualRulesPaletteRelation,
    lighting: raw.lighting as VisualRulesLighting,
    contrast: raw.contrast as VisualRulesContrast,
    saturation: raw.saturation as VisualRulesSaturation,
    exposure: raw.exposure as VisualRulesExposure,
    progression: raw.progression as VisualRulesProgression,
    preserveNaturalSkinExposure: raw.preserve_natural_skin_exposure,
  }
}

export const visualRulesColorDesignPayload = (design: VisualRulesColorDesign) => ({
  base_colors: [...design.baseColors], accent_color: design.accentColor, palette_relation: design.paletteRelation,
  lighting: design.lighting, contrast: design.contrast, saturation: design.saturation, exposure: design.exposure,
  progression: design.progression, preserve_natural_skin_exposure: design.preserveNaturalSkinExposure,
})

export const visualRulesColorDesignSummary = (design: VisualRulesColorDesign): string => [
  `主色 ${design.baseColors.map(visualRulesColorDesignLabel).join(' + ')}`, `强调色 ${visualRulesColorDesignLabel(design.accentColor)}`,
  visualRulesColorDesignLabel(design.paletteRelation), visualRulesColorDesignLabel(design.lighting),
  `对比度 ${visualRulesColorDesignLabel(design.contrast)}`, `饱和度 ${visualRulesColorDesignLabel(design.saturation)}`,
  `曝光 ${visualRulesColorDesignLabel(design.exposure)}`, visualRulesColorDesignLabel(design.progression),
  design.preserveNaturalSkinExposure ? '保持自然肤色曝光' : '肤色跟随整体曝光',
].join(' · ')

export const visualRulesVariationNoteErrors = (value: string): string[] => unsafeNoteCharacters(value)
  ? ['本次视觉倾向不能包含 emoji、双向控制符或零宽字符']
  : []

export const adaptVisualRulesDraftResult = (payload: unknown): VisualRulesDraftResult => {
  const raw = objectValue(payload)
  const draft = objectValue(raw.draft)
  const sourceSummary = objectValue(raw.source_summary)
  const generation = objectValue(raw.generation)
  const runtime = objectValue(raw.runtime)
  return {
    projectId: String(raw.project_id || ''),
    contextFingerprint: String(raw.context_fingerprint || ''),
    contextCurrent: raw.context_current !== false,
    candidateLanguage: 'en',
    reviewRequired: true,
    persisted: false,
    draft: {
      negativePrompt: String(draft.negative_prompt || ''),
      colorScript: String(draft.color_script || ''),
      continuitySummary: String(draft.continuity_summary || ''),
    },
    colorDesign: adaptVisualRulesColorDesign(raw.color_design),
    fieldModes: {
      negativePrompt: 'deterministic_guard',
      colorScript: 'structured_design',
      continuitySummary: 'deterministic_guard',
    },
    sourceSummary: {
      projectFields: Array.isArray(sourceSummary.project_fields) ? sourceSummary.project_fields.map(String) : [],
      productionSettingFields: Array.isArray(sourceSummary.production_setting_fields) ? sourceSummary.production_setting_fields.map(String) : [],
      styleIncluded: sourceSummary.style_included === true,
      characterCount: Number(sourceSummary.character_count || 0),
      sceneCount: Number(sourceSummary.scene_count || 0),
      propCount: Number(sourceSummary.prop_count || 0),
      episodeSummaryCount: Number(sourceSummary.episode_summary_count || 0),
      episodeScriptsIncluded: false,
      shotsIncluded: false,
    },
    generation: {
      attempt: Math.max(1, Number(generation.attempt || 1)),
      directionNoteProvided: generation.direction_note_provided === true,
      variationNoteProvided: generation.variation_note_provided === true,
      generationFingerprint: String(generation.generation_fingerprint || ''),
    },
    warnings: Array.isArray(raw.warnings) ? raw.warnings.map(String) : [],
    runtime: {
      provider: String(runtime.provider || ''),
      model: String(runtime.model || ''),
      loadedOnDemand: runtime.loaded_on_demand === true,
      unloadedAfterRequest: runtime.unloaded_after_request === true,
      reviewRequired: runtime.review_required !== false,
      persisted: false,
      elapsedMs: Number(runtime.elapsed_ms || 0),
      repairAttempted: runtime.repair_attempted === true,
      repairSucceeded: runtime.repair_succeeded === true,
    },
  }
}

export const adaptVisualRulesDraftApplyResult = (payload: unknown): VisualRulesDraftApplyResult => {
  const raw = objectValue(payload)
  const applied = objectValue(raw.applied)
  return {
    projectId: String(raw.project_id || ''),
    appliedContextFingerprint: String(raw.applied_context_fingerprint || ''),
    currentContextFingerprint: String(raw.current_context_fingerprint || ''),
    updatedAt: String(raw.updated_at || ''),
    applied: {
      negativePrompt: String(applied.negative_prompt || ''),
      colorScript: String(applied.color_script || ''),
      continuitySummary: String(applied.continuity_summary || ''),
    },
  }
}

export const visualRulesDraftGeneratePayload = (input: {
  attempt: number
  variationNote?: string
  expectedContextFingerprint?: string
}) => ({
  attempt: Math.max(1, Math.min(100, Math.trunc(Number(input.attempt) || 1))),
  ...(input.variationNote?.trim() ? { variation_note: input.variationNote.trim() } : {}),
  ...(input.expectedContextFingerprint?.trim()
    ? { expected_context_fingerprint: input.expectedContextFingerprint.trim() }
    : {}),
})

export const visualRulesDraftApplyPayload = (input: {
  contextFingerprint: string
  generationFingerprint: string
  attempt: number
  directionNote?: string
  variationNote?: string
  colorDesign: VisualRulesColorDesign
}) => ({
  context_fingerprint: input.contextFingerprint.trim(),
  generation_fingerprint: input.generationFingerprint.trim(),
  attempt: Math.max(1, Math.min(100, Math.trunc(Number(input.attempt) || 1))),
  direction_note: input.directionNote || '',
  variation_note: input.variationNote || '',
  color_design: visualRulesColorDesignPayload(input.colorDesign),
})

export const visualRulesProjectPersistenceFields = (project: Partial<SeriesProject>) => ({
  negativePrompt: project.bible?.negativePrompt || '',
  colorScript: project.bible?.colorScript || '',
  continuitySummary: project.continuitySummary || '',
})

export const visualRulesDraftValidationErrors = (draft: VisualRulesDraftData): string[] => {
  const fields: Array<[keyof VisualRulesDraftData, string]> = [
    ['negativePrompt', '全局负面提示词'],
    ['colorScript', '色彩脚本'],
    ['continuitySummary', '连续性摘要'],
  ]
  const errors = fields.flatMap(([key, label]) => {
    const value = draft[key].trim()
    if (!value) return [`${label}不能为空`]
    if (key === 'negativePrompt' && !/^[\x00-\x7F]*$/.test(value)) return [`${label}保护项只允许 ASCII 英文缺陷短语；请重新生成`]
    if (CJK.test(value)) return [key === 'colorScript' ? `${label}仍含中文；请改成纯英文后再采用` : `${label}确定性保护项仍含中文；请重新生成`]
    if (key !== 'negativePrompt' && [...value].some((char) => LETTER.test(char) && !LATIN_LETTER.test(char))) {
      return [`${label}含非拉丁文字；请使用纯英文后再采用`]
    }
    return []
  })
  if (draft.colorScript.trim() && !/\b(?:color|palette|light|lighting|shadow|highlight|tone|contrast|saturation|exposure)\b/i.test(draft.colorScript)) {
    errors.push('色彩脚本必须明确包含 color / palette / light 等色彩或光线规则')
  }
  return errors
}

const stableGlossary = (value: Record<string, string> | undefined) => Object.fromEntries(
  Object.entries(value || {}).sort(([left], [right]) => left.localeCompare(right, 'zh-CN')),
)

const stableBibleItem = (item: SeriesProject['bible']['characters'][number]) => ({
  id: item.id,
  name: item.name,
  description: item.description,
  visualPrompt: item.visualPrompt,
  ageAppearance: item.ageAppearance || '',
  faceIdentity: item.faceIdentity || '',
  wardrobe: item.wardrobe || '',
  voiceProfile: item.voiceProfile || '',
  personality: item.personality || '',
  location: item.location || '',
  timeOfDay: item.timeOfDay || '',
  lighting: item.lighting || '',
  colorPalette: item.colorPalette || '',
  ambientSound: item.ambientSound || '',
  material: item.material || '',
  distinctiveMarks: item.distinctiveMarks || '',
  continuityState: item.continuityState || '',
})

/**
 * Browser-side drift guard for unsaved edits. The service remains authoritative:
 * apply performs the same check under a database lock before writing anything.
 */
export const visualRulesLocalContextSnapshot = (project: SeriesProject, style?: StylePreset): string => JSON.stringify({
  project: {
    id: project.id,
    title: project.title,
    logline: project.logline,
    masterScript: project.masterScript,
    aspectRatio: project.aspectRatio,
    resolution: project.resolution,
    width: project.width || null,
    height: project.height || null,
    defaultMode: project.defaultMode,
    continuitySummary: project.continuitySummary || '',
  },
  productionSettings: {
    negativePrompt: project.bible.negativePrompt,
    colorScript: project.bible.colorScript,
    translationGlossary: stableGlossary(project.translationGlossary),
  },
  style: style ? {
    id: style.backendId || style.id,
    name: style.name,
    category: style.category,
    description: style.description,
    visualPrompt: style.promptPrefix,
  } : null,
  bible: {
    characters: project.bible.characters.map(stableBibleItem),
    scenes: project.bible.scenes.map(stableBibleItem),
    props: project.bible.props.map(stableBibleItem),
  },
  episodes: project.episodes.map((episode) => ({
    number: episode.number,
    title: episode.title,
    synopsis: episode.synopsis,
  })),
})

export type VisualRulesDraftErrorKind = 'resource_busy' | 'source_conflict' | 'runtime_unavailable' | 'validation' | 'unknown'

const errorDetailText = (value: unknown): string[] => {
  if (typeof value === 'string' && value.trim()) return [value.trim()]
  if (Array.isArray(value)) return value.flatMap(errorDetailText)
  if (value && typeof value === 'object') return Object.values(value as Record<string, unknown>).flatMap(errorDetailText)
  return []
}

export const visualRulesDraftError = (cause: unknown): { kind: VisualRulesDraftErrorKind; code: string; message: string } => {
  const candidate = objectValue(cause)
  const response = objectValue(candidate.response)
  const data = objectValue(response.data)
  const envelope = objectValue(data.error)
  const envelopeDetails = objectValue(envelope.details)
  const code = String(envelope.code || data.code || '')
  const status = Number(response.status || 0)
  const kind: VisualRulesDraftErrorKind = code === 'visual_rules_resource_busy'
    ? 'resource_busy'
    : code === 'visual_rules_source_conflict'
      ? 'source_conflict'
      : code === 'visual_rules_runtime_unavailable'
        ? 'runtime_unavailable'
        : status === 400
          ? 'validation'
          : 'unknown'
  const fallback = kind === 'resource_busy'
    ? '视频生产正在占用本地资源；请等任务结束后再生成全剧视觉规则。'
    : kind === 'source_conflict'
      ? '故事或 Bible 已在候选生成后发生变化；请核对并重新生成。'
      : kind === 'runtime_unavailable'
        ? '本地 Qwen 暂时不可用或候选未通过严格校验；正式字段保持不变。'
        : '全剧视觉规则候选生成失败；正式字段保持不变。'
  const detailedMessage = errorDetailText(envelopeDetails.detail || envelopeDetails).join('；')
  return { kind, code, message: detailedMessage || String(envelope.message || candidate.message || fallback) }
}
