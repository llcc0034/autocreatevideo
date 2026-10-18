import { recipeDefaultContract, recipeDefaultRoute } from './recipe-default-data'
export const recipeHierarchyContract = recipeDefaultContract
export const recipeHierarchyOptions = {
  definitionSchemaVersion: 3, effectiveLimits: { maxRoutingRoutes: 3, maxVariants: 4, maxGenerationTasks: 24 },
  stageOptions: [
    { value: 'script_generate', label: '剧本生成', repeatable: false, modes: ['ai', 'manual'], routingModes: ['ai'], candidateEvaluatorOptions: [], decisionPolicy: null },
    { value: 'script_review', label: '剧本审核', repeatable: false, modes: ['review'], routingModes: [], candidateEvaluatorOptions: [], decisionPolicy: null },
    { value: 'custom_text', label: '自定义文本步骤', repeatable: true, modes: ['ai'], routingModes: ['ai'], candidateEvaluatorOptions: [{ value: null, label: '人工选择' }, { value: 'first_succeeded', label: '首个成功候选' }], decisionPolicy: null },
  ],
  interactionModeOptions: [{ value: 'assisted', label: '人工辅助' }, { value: 'unattended', label: '无人值守' }],
  approvalOptions: [{ value: 'auto', label: '不另设阶段审核' }, { value: 'required', label: '需要审核' }],
  routingModeOptions: [{ value: 'fixed', label: '单一实例' }, { value: 'ordered_fallback', label: '按顺序切换' }],
  materialSourceOptions: [], materialSelectionOptions: [], fieldReviewPolicyOptions: [], shotReferenceSourceOptions: [],
  sourceKindOptions: [{ value: 'structured', label: '字段装配' }, { value: 'selected_output', label: '前序已采用输出' }, { value: 'literal', label: '明确原值' }],
  structureNodeOptions: [{ value: 'object', label: '组成对象' }, { value: 'literal', label: '明确原值' }],
  textOutputOptions: [], parameterPolicyOptions: [], parameterSourceOptions: [], scopeOptions: [], referenceSourceOptions: [],
  presets: [{ value: 'fixture-two', label: '隔离两步骤预设', stages: [
    { key: 'script_generate', mode: 'ai', approval: 'auto' }, { key: 'script_review', mode: 'review', approval: 'required' },
  ] }],
}
export const recipeHierarchyDefinition = () => ({ schema_version: 3, interaction_mode: 'assisted', stages: [
  { key: 'script_generate', mode: 'ai', approval: 'auto', routing: { mode: 'fixed', variant_count: 1, evaluator: null, routes: [recipeDefaultRoute()] } },
  { key: 'script_review', mode: 'review', approval: 'required' },
] })
