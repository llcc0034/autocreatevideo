import type {
  SeriesProject,
  VisualRulesColor,
  VisualRulesColorDesign,
  VisualRulesContrast,
  VisualRulesExposure,
  VisualRulesLighting,
  VisualRulesPaletteRelation,
  VisualRulesProgression,
  VisualRulesSaturation,
} from '@/types'

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

export const visualRulesProjectPersistenceFields = (project: Partial<SeriesProject>) => ({
  negativePrompt: project.bible?.negativePrompt || '',
  colorScript: project.bible?.colorScript || '',
  continuitySummary: project.continuitySummary || '',
})
