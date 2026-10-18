import { isContractObject } from './modelCapabilityContract'
import type { PromptProfileFieldRule, PromptProfileOptions, PromptProfileVersion } from './platformTypes'

// Read saved values only. Relationships come from the server's primitive catalog,
// never from template prose, model names, or a client-side list of primitives.
export type PromptRulePath = Pick<PromptProfileFieldRule, 'sourceSelector' | 'projectionPrimitive' | 'generationMode' | 'generationOutputSemantics' | 'generationSourceSelector' | 'fieldKey' | 'fieldLabel' | 'channel' | 'position'>
  & Partial<Pick<PromptProfileFieldRule, 'sourcePath' | 'languagePolicy' | 'generationInstruction' | 'generationContextSelectors'>>

export function rulePrimitive(rule: Pick<PromptRulePath, 'sourceSelector' | 'projectionPrimitive'>, options?: PromptProfileOptions) {
  if (rule.sourceSelector !== 'projection.output') return undefined
  return options?.sourceProjectionPrimitives?.find(item => item.value === rule.projectionPrimitive)
}

export function hasRuleTemplates(profile: PromptProfileVersion, rule: PromptProfileFieldRule, options?: PromptProfileOptions) {
  const primitive = rulePrimitive(rule, options)
  if (!primitive?.renderingTemplateKeys?.length) return false
  return primitiveConfigGroups(profile, [rule], options).some(group => group.key === primitive.engineKey && Boolean(group.templates))
}

export function ruleContentMethod(rule: Pick<PromptRulePath, 'generationMode' | 'generationOutputSemantics' | 'sourceSelector'>, hasTemplates = false) {
  if (['translate_only', 'model_generated'].includes(rule.generationMode)) {
    if (rule.generationOutputSemantics === 'final') return rule.generationMode === 'translate_only'
      ? '按语言策略翻译，译文直接作为最终字段值' : '由模型生成最终字段值'
    if (rule.generationOutputSemantics !== 'contribution') return '生成结果用途未明确，不能推断后续路径'
    const action = rule.generationMode === 'translate_only' ? '先按语言策略翻译' : '先由模型生成内容'
    return `${action}，再${hasTemplates ? '作为贡献按模板拼接' : '作为贡献交给原语整理'}`
  }
  if (!['passthrough', 'server_locked'].includes(rule.generationMode)) return '内容生成方式未明确，不能推断处理路径'
  return rule.sourceSelector === 'projection.output' ? (hasTemplates ? '读取素材与任务信息，按模板拼接' : '按原语规则整理字段内容') : '直接读取字段来源'
}

// Describes the configured path only; does not compile, validate or resolve values.
export function ruleProcessesProjection(rule: Pick<PromptRulePath, 'sourceSelector' | 'generationMode' | 'generationOutputSemantics'>) {
  return rule.sourceSelector === 'projection.output' && (
    ['passthrough', 'server_locked'].includes(rule.generationMode)
    || (['model_generated', 'translate_only'].includes(rule.generationMode) && rule.generationOutputSemantics === 'contribution'))
}

export function ruleTemplatePath(profile: Pick<PromptProfileVersion, 'sourceProjectionConfig'>, rule: PromptRulePath, options?: PromptProfileOptions) {
  const primitive = rulePrimitive(rule, options)
  const projection = profile.sourceProjectionConfig
  const engines = isContractObject(projection) && isContractObject(projection.primitive_options) ? projection.primitive_options : {}
  const config = primitive ? engines[primitive.engineKey] : undefined
  const rendering = isContractObject(config) ? config.rendering : undefined
  const templates = isContractObject(rendering) && isContractObject(rendering.templates) ? rendering.templates : undefined
  return { primitive, active: ruleProcessesProjection(rule),
    templates: primitive ? savedTemplateRows(templates, options, primitive.renderingTemplateKeys) : [] }
}

export function templateRuleUsages(key: string, profileId: string, rules: PromptProfileFieldRule[], options?: PromptProfileOptions) {
  if (!profileId) return []
  return rules.filter(rule => rule.profileVersion === profileId && ruleProcessesProjection(rule)
    && rulePrimitive(rule, options)?.renderingTemplateKeys.includes(key))
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
