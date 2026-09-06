import { isContractObject } from './modelCapabilityContract'
import type { PromptProfileFieldRule, PromptProfileOptions, PromptProfileVersion } from './platformTypes'

// Read saved values only. Relationships come from the server's primitive catalog,
// never from template prose, model names, or a client-side list of primitives.
export function rulePrimitive(rule: PromptProfileFieldRule, options?: PromptProfileOptions) {
  if (rule.sourceSelector !== 'projection.output') return undefined
  return options?.sourceProjectionPrimitives?.find(item => item.value === rule.projectionPrimitive)
}

export function hasRuleTemplates(profile: PromptProfileVersion, rule: PromptProfileFieldRule, options?: PromptProfileOptions) {
  const primitive = rulePrimitive(rule, options)
  if (!primitive?.renderingTemplateKeys?.length) return false
  return primitiveConfigGroups(profile, [rule], options).some(group => group.key === primitive.engineKey && Boolean(group.templates))
}

export function ruleContentMethod(rule: PromptProfileFieldRule, hasTemplates = true) {
  if (rule.generationMode === 'translate_only') return '按语言策略翻译后，再用于字段内容'
  if (rule.generationMode === 'model_generated') {
    return rule.generationOutputSemantics === 'contribution'
      ? (hasTemplates ? '先由模型生成内容，再按模板拼接' : '先由模型生成内容，再由原语整理') : '由模型生成字段内容'
  }
  return rule.sourceSelector === 'projection.output' ? (hasTemplates ? '读取素材与任务信息，按模板拼接' : '按原语规则整理字段内容') : '直接读取字段来源'
}

export function primitiveConfigGroups(profile: PromptProfileVersion, rules: PromptProfileFieldRule[], options?: PromptProfileOptions) {
  const projection = profile.sourceProjectionConfig
  const saved = isContractObject(projection) && isContractObject(projection.primitive_options) ? projection.primitive_options : {}
  const versionRules = rules.filter(rule => rule.profileVersion === profile.id)
  const keys = new Set(Object.keys(saved))
  for (const rule of versionRules) {
    const primitive = rulePrimitive(rule, options)
    if (primitive?.engineKey) keys.add(primitive.engineKey)
  }
  return [...keys].map(key => {
    const config = saved[key]
    const rendering = isContractObject(config) ? config.rendering : undefined
    const templates = isContractObject(rendering) && isContractObject(rendering.templates) ? rendering.templates : undefined
    return { key, config, rendering, templates, saved: Object.prototype.hasOwnProperty.call(saved, key),
      rules: versionRules.filter(rule => rulePrimitive(rule, options)?.engineKey === key) }
  })
}

export function savedTemplateRows(templates: Record<string, unknown> | undefined, options?: PromptProfileOptions, keys?: string[]) {
  return (keys ?? Object.keys(templates ?? {})).map(key => {
    const exists = Boolean(templates && Object.prototype.hasOwnProperty.call(templates, key))
    const value = templates?.[key]
    const help = options?.referenceRenderingFields?.find(item => item.key === key)
    return { key, value, help, label: help?.label || key,
      state: !exists ? '未保存' : typeof value !== 'string' ? '格式异常' : value.trim() ? '已保存' : '已留空' }
  })
}
