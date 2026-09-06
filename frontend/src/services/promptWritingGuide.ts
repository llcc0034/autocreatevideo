import type {
  PromptWritingGuideFieldRule,
  PromptWritingGuideProfile,
} from '../types/index.ts'

export type { PromptWritingGuideFieldRule, PromptWritingGuideProfile }

export const DIRECT_PROMPT_SOURCE_PROJECTION_KEY = 'direct_v1'
export const CLASSIFIED_REFERENCE_CONTEXT_PROJECTION_KEY = 'classified_reference_context_ir_v1'

export interface PromptWritingGuideContract {
  profile: PromptWritingGuideProfile | null
  adapterKey: string
  adapterLabel: string
  adapterDescription: string
  fieldRules: PromptWritingGuideFieldRule[]
  showClassifiedReferenceExample: boolean
}

const adapterGuide = (adapterKey: string) => {
  if (adapterKey === CLASSIFIED_REFERENCE_CONTEXT_PROJECTION_KEY) return {
    label: '分类参考上下文投影器 / Classified reference context projection',
    description: '先由受控投影器生成分类参考上下文，再严格按当前 Profile 的字段顺序、标签、分隔符与通道装配。',
    showClassifiedReferenceExample: true,
  }
  if (adapterKey === DIRECT_PROMPT_SOURCE_PROJECTION_KEY) return {
    label: '通用字段直装配 / Direct field assembly',
    description: '不套用模型专属段落；只按当前 Profile 冻结的字段来源、顺序、标签、分隔符、通道和长度上限装配。',
    showClassifiedReferenceExample: false,
  }
  return {
    label: `受控来源投影器 / Controlled source projection · ${adapterKey}`,
    description: '以当前已发布 Profile 和服务端受控投影器合同为准；页面不会根据模型名称猜测写作结构。',
    showClassifiedReferenceExample: false,
  }
}

export const promptWritingGuideContract = (
  profile?: PromptWritingGuideProfile | null,
): PromptWritingGuideContract => {
  const normalizedProfile = profile || null
  const adapterKey = String(
    normalizedProfile?.sourceProjectionKey || DIRECT_PROMPT_SOURCE_PROJECTION_KEY,
  ).trim() || DIRECT_PROMPT_SOURCE_PROJECTION_KEY
  const guide = adapterGuide(adapterKey)
  return {
    profile: normalizedProfile,
    adapterKey,
    adapterLabel: guide.label,
    adapterDescription: guide.description,
    fieldRules: [...(normalizedProfile?.fieldRules || [])].sort(
      (left, right) => left.position - right.position || left.fieldKey.localeCompare(right.fieldKey),
    ),
    showClassifiedReferenceExample: guide.showClassifiedReferenceExample,
  }
}
