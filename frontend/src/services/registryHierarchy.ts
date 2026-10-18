import type { RelationKind, RelationRow } from './registryRelations'

type Layer = 'inputs' | 'processing' | 'definition' | 'consumers' | 'related'
export const registryRoles: Record<RelationKind,string> = {
  recipe:'业务编排：组合阶段与执行实例，配置上游输入、素材选择和审核。确定性处理与独立审核不冒充模型调用。',
  execution:'一次生成能力的配置入口：输入准备（含文本处理）→ 模型能力调用 → 输出。主目标和辅助目标均引用精确实例版本。',
  field:'公共字段身份：统一字段的稳定标识。含义、类型等在定义版本维护；实际值、必填和默认值由使用处决定。',
  field_version:'字段定义：保存此版本的含义与类型，供不同结构和策略引用，不保存某次任务的输入值。',
  structure:'公共输入／输出结构：由确切字段版本及结构节点组成，可被来源、策略、输出或执行实例复用。',
  source:'内容来源：说明从何处按何结构读取事实；不是生成模型，也不把缺失事实补成成功结果。',
  prompt:'文本输入处理方案：按字段规则读取、按需生成或翻译，再组装文本通道。需要 AI 的字段由执行实例所配置的辅助目标处理；策略本身不保证调用 AI。',
  model:'模型能力定义：声明可接受的输入、参数与输出能力。实际连接在模型部署；关联工作流是可复用实现，不表示模型独占它。',
  deployment:'模型部署：连接精确模型版本、服务提供方及运行资源，不另维护业务字段或决定本次提示词。实际组合由执行实例固定。',
  workflow:'可复用工作流实现：内部节点、接线及工具由已存图定义，上层使用其暴露的输入、输出和参数；不是每个模型调用都需要工作流。',
  projector:'工作流解释与投影方案：把节点图中的已支持规则映射成平台可用合同；不是另一轮 AI 调用。不能把导入任意工作流等同于已自动理解全部节点。',
  output:'公共业务输出定义：约定结果解析与结构字段，供执行实例引用；它是输出合同，不是一次生成的实际产物。',
}
const placements: Partial<Record<RelationKind, Record<string, Layer>>> = {
  field: { versions:'definition', structures:'consumers', prompts:'consumers' },
  field_version: { field:'inputs', structures:'consumers', prompts:'consumers' },
  structure: { fields:'definition', sources:'consumers', outputs:'consumers', prompts:'consumers', executions:'consumers' },
  source: { structure:'inputs', prompts:'consumers' },
  model: { deployments:'consumers', workflows:'related', executions:'consumers' },
  deployment: { model:'inputs', validated:'related', candidates:'related', executions:'consumers' },
  workflow: { model:'inputs', deployment:'related', projector:'processing', executions:'consumers' },
  prompt: { structure:'inputs', sources:'inputs', fields:'definition', executions:'consumers' },
  output: { structure:'definition', executions:'consumers' },
  execution: { deployment:'processing', workflow:'processing', prompt:'inputs', structures:'inputs', auxiliaries:'inputs', output:'definition', consumers:'consumers', recipes:'consumers' },
  projector: { workflows:'consumers' },
  recipe: { executions:'inputs' },
}
export const hierarchyLayers = [
  { key:'inputs', label:'输入与依赖', description:'声明的配置来源；不是这次任务传入的值。' },
  { key:'processing', label:'处理与实现', description:'精确配置依赖，不表示运行顺序或已执行。' },
  { key:'definition', label:'输出与定义', description:'结构和字段是可复用定义，不是生成结果。' },
  { key:'consumers', label:'配置使用方', description:'引用此资源的去重对象，不是调用次数或全部历史消费者。' },
  { key:'related', label:'相关配置与校验关联', description:'同模型、校验目标等关联；不保证兼容、被选中或验收通过。' },
] as const

export const registryHierarchy = (row: RelationRow) => hierarchyLayers.map(layer => ({...layer,
  groups: row.groups.filter(group => (placements[row.kind]?.[group.key] || 'related')===layer.key),
})).filter(layer => layer.groups.length)

export interface RegistryTrailEntry { label: string; url: string }
const relationQueryKeys = new Set(['view','kind','ids','source_kind','source_id','relation','search','page'])
export function registryBrowserUrl(value: unknown): string | null {
  if (typeof value!=='string' || !value.startsWith('/settings/acceptance-smoke?') || /[\\\r\n]/.test(value)) return null
  const url = new URL(value,'https://navigation.invalid')
  if (url.pathname!=='/settings/acceptance-smoke' || url.searchParams.get('view')!=='relations' || url.hash || [...url.searchParams.keys()].some(key=>!relationQueryKeys.has(key))) return null
  return url.pathname+url.search
}
export function readRegistryTrail(value: unknown): RegistryTrailEntry[] {
  if (!Array.isArray(value) || value.length>24) return []
  const result: RegistryTrailEntry[] = []
  for (const item of value) {
    if (!item || typeof item!=='object' || typeof item.label!=='string' || !registryBrowserUrl(item.url)) return []
    result.push({label:item.label,url:item.url})
  }
  return result
}
export function descendRegistry(trail: RegistryTrailEntry[], current: RegistryTrailEntry, target: string) {
  const url = registryBrowserUrl(target)
  if (!url) throw new Error('关联下钻地址无效')
  const cycle = trail.findIndex(entry=>entry.url===url)
  const entries = cycle>=0 ? trail.slice(0,cycle) : target===current.url ? trail : [...trail,current].slice(-24)
  return { url, trail:entries }
}
