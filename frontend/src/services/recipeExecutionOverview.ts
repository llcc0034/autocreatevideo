import { productionRecipeInteractionMode } from './platformAdapters'

export interface RecipeExecutionRouteSummary {
  versionId: string
  definitionSha256: string
  inputKeys: string[]
  parameterCount: number
}

export interface RecipeExecutionStageSummary {
  key: string
  name?: string
  kind?: string
  mode: string
  approval: string
  routingMode: string | null
  routes: RecipeExecutionRouteSummary[]
  variantRouting?: { routingMode: string; routes: RecipeExecutionRouteSummary[] }
}

const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

// Read only the frozen recipe. This is a display-shape check, not execution admission.
export function recipeExecutionOverview(definition: unknown): { stages: RecipeExecutionStageSummary[]; error: string } {
  try {
    if (!object(definition) || definition.schema_version !== 3) throw new Error('配方不是当前 schema3 合同，请重新配置；不会转换旧工作流选项。')
    if (productionRecipeInteractionMode(definition) === 'invalid') throw new Error('配方交互模式缺失或不受支持，不能创建生产运行。')
    if (!Array.isArray(definition.stages) || !definition.stages.length) throw new Error('配方尚未声明生产阶段。')
    const seen = new Set<string>()
    const stages = definition.stages.map((stage): RecipeExecutionStageSummary => {
      if (!object(stage) || !text(stage.key) || !text(stage.mode) || !text(stage.approval) || seen.has(stage.key)) {
        throw new Error('配方阶段信息缺失或重复，不能展示完整执行配置。')
      }
      seen.add(stage.key)
      const result: RecipeExecutionStageSummary = { key: stage.key, mode: stage.mode, approval: stage.approval, routingMode: null, routes: [] }
      if (stage.kind !== undefined) {
        if (!text(stage.kind) || !text(stage.name)) throw new Error(`步骤 ${stage.key} 缺少职责或显示名称。`)
        result.kind = stage.kind
        result.name = stage.name
      }
      for (const routingKey of ['routing','variant_routing']) {
      if (stage[routingKey] === undefined) continue
      if (routingKey==='variant_routing' && (stage.key!=='bible_image_generation'||stage.mode!=='existing_pipeline')) throw new Error('只有素材主图/变体阶段可以配置变体执行实例。')
      const routing = stage[routingKey]
      if (!object(routing) || !text(routing.mode) || !Array.isArray(routing.routes) || !routing.routes.length) {
        throw new Error(`阶段 ${stage.key} 的执行实例配置不完整。`)
      }
      const routes = routing.routes.map((route): RecipeExecutionRouteSummary => {
        if (!object(route) || Object.keys(route).sort().join(',') !== 'execution_instance_version_id,expected_definition_sha256,input_sources,parameter_policies'
          || typeof route.execution_instance_version_id !== 'string' || !uuid.test(route.execution_instance_version_id)
          || typeof route.expected_definition_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(route.expected_definition_sha256)
          || !object(route.input_sources) || !Object.values(route.input_sources).every(object)
          || !Array.isArray(route.parameter_policies) || !route.parameter_policies.every(object)) {
          throw new Error(`阶段 ${stage.key} 缺少有效的精确实例版本、摘要或输入参数配置。`)
        }
        return { versionId: route.execution_instance_version_id, definitionSha256: route.expected_definition_sha256,
          inputKeys: Object.keys(route.input_sources), parameterCount: route.parameter_policies.length }
      })
      if(routingKey==='variant_routing')result.variantRouting={routingMode:routing.mode,routes}
      else {result.routingMode=routing.mode;result.routes=routes}
      }
      return result
    })
    return { stages, error: '' }
  } catch (error) {
    return { stages: [], error: error instanceof Error ? error.message : '无法读取配方执行配置。' }
  }
}
