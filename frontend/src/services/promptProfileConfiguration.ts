import type { PromptProfileOptions } from './platformTypes'

// Read-only presentation labels, including historical keys. Never supply values or migrate a contract here.
const labels: Record<string, string> = {
  inputStructureVersion: '输入结构版本 ID', sourcePath: '结构化输入字段路径',
  schema_version: '配置格式版本', primitive_options: '原语配置', reference_context_ir_v1: '参考上下文',
  reference_context_policy: '历史参考上下文规则', retention_analysis: '参考关系保留',
  include_subject_shot_usage: '包含主体使用的镜头', assignment_separator: '关系分隔符',
  detailed_description: '详细描述', style_opening_placement: '风格开场位置',
  style_opening_max_sentences: '风格开场最多句数', subject_reference_placement: '主体参考出现位置',
  rendering: '参考文案渲染', templates: '文案模板', separator: '分隔符', list_prefix: '列表前缀',
  reference_rules: '参考用途规则', task_separator: '任务名称分隔符', unreferenced_task: '无素材时的任务名称',
  unit_text_bindings: '单元原文路径映射',
  reference_layout: '主体编号与分段参考布局', subject_label_template: '主体编号模板',
  source_separator: '主体来源分隔符', summary_reference_template: '摘要参考模板', shot_reference_template: '逐镜参考模板',
  category: '素材类别', purpose: '参考用途', task: '任务名称', relationship: '保留方式', usage: '使用说明',
  prompt_template_key: '模板标识', quality_advisories: '质量建议',
  positive_negative_token_overlap: '正负提示词重叠检查', minimum_shared_tokens: '最少重复词数',
  minimum_overlap_ratio: '最小重叠比例', short_shot_action_density: '短镜头动作密度',
  maximum_duration_ms: '镜头时长上限（毫秒）', maximum_action_characters: '动作描述字符上限',
  minimum_clause_separators: '最少分句符数量', motion_camera_reference_guidance: '动作与运镜参考说明',
  minimum_english_words: '最少英文单词数', generic_values: '通用描述检查词',
  maxChars: '字符上限', warnAtChars: '预警字符数',
  id: '对象 ID', profileKey: '策略族标识', version: '版本号', name: '名称', description: '说明',
  modelFamily: '模型族', operation: '操作能力', mode: '模式', sourceProjectionKey: '来源投影器',
  officialReference: '登记的官方参考', status: '版本状态', reviewState: '审核状态',
  contractSchemaVersion: '合同格式版本', contractSizeBytes: '合同大小（字节）', contractSha256: '合同 SHA-256',
  fieldRuleCount: '字段规则数', publishedAt: '发布时间', publishedBy: '发布人 ID',
  createdAt: '创建时间', updatedAt: '更新时间',
  profileVersion: '所属策略版本 ID', fieldDefinition: '字段身份 ID', fieldDefinitionVersion: '字段定义版本 ID', fieldKey: '字段标识',
  fieldLabel: '字段名称', fieldValueType: '字段类型', sourceSelector: '最终内容来源',
  projectionPrimitive: '来源原语', channel: '输出通道', position: '排列顺序', labelStyle: '标签格式',
  required: '最终字段必填', omitIfEmpty: '空值时省略', generationMode: '内容生成方式',
  fixedValue: '本规则固定值', hasDefault: '启用缺失默认值', defaultValue: '本规则缺失默认值',
  generationSourceSelector: '主要生成来源', generationContextSelectors: '额外参考信息',
  generationInstruction: '生成指令', generationOutputSemantics: '生成结果用途',
  generationRequired: '必须生成成功', languagePolicy: '语言策略', lengthUnit: '长度单位',
  generationMin: '生成长度下限', generationMax: '生成长度上限', generationValidatorKeys: '校验规则',
}

export function promptConfigurationLabel(key: string, options?: PromptProfileOptions): string {
  return options?.referenceRenderingFields.find(item => item.key === key)?.label
    || options?.referenceRenderingTextSlots?.find(item => item.value === key)?.label
    || options?.channels.find(item => item.value === key)?.label
    || (Object.prototype.hasOwnProperty.call(labels, key) ? labels[key] : key)
}
