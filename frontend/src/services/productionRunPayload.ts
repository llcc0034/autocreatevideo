import type { JsonObject, ProductionRunCreateInput } from './platformTypes'

const required = (value: string, label: string): string => {
  const normalized = value.trim()
  if (!normalized) throw new Error(`${label}不能为空`)
  return normalized
}

const positiveInteger = (value: number | null | undefined, label: string): number => {
  if (!Number.isInteger(value) || Number(value) < 1) throw new Error(`${label}必须是正整数`)
  return Number(value)
}

export const buildProductionRunRequestPayload = (input: ProductionRunCreateInput): JsonObject => {
  const payload: JsonObject = {
    project_id: required(input.projectId, '项目'),
    recipe_version_id: required(input.recipeVersionId, '配方版本'),
    idempotency_key: required(input.idempotencyKey, '幂等键'),
    config_overrides: input.configOverrides ?? {},
  }
  if (input.allowExternalCalls !== null && input.allowExternalCalls !== undefined) payload.allow_external_calls = input.allowExternalCalls
  if (input.budgetLimit !== null && input.budgetLimit !== undefined && input.budgetLimit.trim()) payload.budget_limit = input.budgetLimit.trim()
  if (input.maxGenerationTasks !== null && input.maxGenerationTasks !== undefined) payload.max_generation_tasks = positiveInteger(input.maxGenerationTasks, '任务上限')
  if (input.workflowSelection) {
    if (input.workflowSelection.mode === 'uniform') {
      const routeIndex = input.workflowSelection.routeIndex
      if (!Number.isInteger(routeIndex) || routeIndex < 0 || routeIndex > 15) throw new Error('整集工作流 route 必须是 0 到 15 的整数')
      payload.workflow_selection = {
        schema_version: 1,
        mode: 'uniform',
        route_index: routeIndex,
        shot_routes: [],
      }
    } else {
      const seen = new Set<string>()
      const shotRoutes = input.workflowSelection.shotRoutes.map((item) => {
        const shotId = required(item.shotId, '镜头')
        if (seen.has(shotId)) throw new Error('同一镜头不能重复选择工作流 route')
        seen.add(shotId)
        if (!Number.isInteger(item.routeIndex) || item.routeIndex < 0 || item.routeIndex > 15) throw new Error('逐镜头工作流 route 必须是 0 到 15 的整数')
        return { shot_id: shotId, route_index: item.routeIndex }
      })
      if (!shotRoutes.length) throw new Error('逐镜头工作流必须覆盖至少一个镜头')
      payload.workflow_selection = {
        schema_version: 1,
        mode: 'per_shot',
        route_index: null,
        shot_routes: shotRoutes,
      }
    }
  }

  if (input.scriptMode === 'ai') {
    payload.idea = required(input.idea || '', '故事想法')
    payload.style_hint = (input.styleHint || '').trim()
    payload.episode_count = positiveInteger(input.episodeCount, '集数')
    payload.shots_per_episode = positiveInteger(input.shotsPerEpisode, '每集镜数')
    payload.episode_duration_seconds = positiveInteger(input.episodeDurationSeconds, '单集时长')
    return payload
  }

  if (input.scriptMode === 'confirmed_revision') {
    payload.script_revision_id = required(input.scriptRevisionId || '', '已确认剧本版本')
    return payload
  }

  if (input.scriptMode === 'downstream') {
    payload.episode_ids = [...new Set(input.episodeIds || [])].filter(Boolean).sort()
    return payload
  }

  const unreachable: never = input.scriptMode
  throw new Error(`不支持的剧本输入模式: ${String(unreachable)}`)
}
