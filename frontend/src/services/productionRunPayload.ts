import type { JsonObject, ProductionRunCreateInput } from './platformTypes'

const required = (value: string, label: string): string => {
  const normalized = value.trim()
  if (!normalized) throw new Error(`${label}不能为空`)
  return normalized
}
const SAFE_REFERENCE_IDENTIFIER = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/

const positiveInteger = (value: number | null | undefined, label: string): number => {
  if (!Number.isInteger(value) || Number(value) < 1) throw new Error(`${label}必须是正整数`)
  return Number(value)
}

export const plannedAverageShotDurationMs = (
  shotsPerEpisode: number | null | undefined,
  episodeDurationSeconds: number | null | undefined,
): number => {
  const shots = positiveInteger(shotsPerEpisode, '每集镜数')
  const seconds = positiveInteger(episodeDurationSeconds, '单集时长')
  return Math.round(seconds * 1_000 / shots)
}

export const buildProductionRunRequestPayload = (input: ProductionRunCreateInput): JsonObject => {
  const limits = input.effectiveLimits
  if (!limits || !Number.isSafeInteger(limits.maxRoutingRoutes) || limits.maxRoutingRoutes < 1 || !Number.isSafeInteger(limits.maxReferenceBindings) || limits.maxReferenceBindings < 1) {
    throw new Error('服务端有效安全上限缺失或无效，已停止创建生产运行。请刷新生产能力配置后重试。')
  }
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
      if (!Number.isInteger(routeIndex) || routeIndex < 0 || routeIndex >= limits.maxRoutingRoutes) throw new Error(`整集工作流 route 必须是 0 到 ${limits.maxRoutingRoutes - 1} 的整数`)
      const seenContracts = new Set<string>()
      const selections = input.workflowSelection.promptProfileSelections.map((item) => {
        const operation = required(item.operation, 'PromptProfile operation')
        const mode = item.mode.trim()
        const promptProfileBindingId = required(item.promptProfileBindingId, 'PromptProfile binding')
        const key = JSON.stringify([operation, mode])
        if (seenContracts.has(key)) throw new Error('同一 operation/mode 不能重复选择 PromptProfile')
        seenContracts.add(key)
        return { operation, mode, prompt_profile_binding_id: promptProfileBindingId }
      }).sort((left, right) => `${left.operation}\0${left.mode}`.localeCompare(`${right.operation}\0${right.mode}`))
      if (!selections.length) throw new Error('整集工作流必须显式选择 PromptProfile binding')
      payload.workflow_selection = {
        schema_version: 2,
        mode: 'uniform',
        route_index: routeIndex,
        shot_routes: [],
        prompt_profile_selections: selections,
      }
    } else {
      const seen = new Set<string>()
      const shotRoutes = input.workflowSelection.shotRoutes.map((item) => {
        const shotId = required(item.shotId, '镜头')
        if (seen.has(shotId)) throw new Error('同一镜头不能重复选择工作流 route')
        seen.add(shotId)
        if (!Number.isInteger(item.routeIndex) || item.routeIndex < 0 || item.routeIndex >= limits.maxRoutingRoutes) throw new Error(`逐镜头工作流 route 必须是 0 到 ${limits.maxRoutingRoutes - 1} 的整数`)
        return {
          shot_id: shotId,
          route_index: item.routeIndex,
          prompt_profile_binding_id: required(item.promptProfileBindingId, 'PromptProfile binding'),
        }
      }).sort((left, right) => left.shot_id.localeCompare(right.shot_id))
      if (!shotRoutes.length) throw new Error('逐镜头工作流必须覆盖至少一个镜头')
      payload.workflow_selection = {
        schema_version: 2,
        mode: 'per_shot',
        route_index: null,
        prompt_profile_selections: [],
        shot_routes: shotRoutes,
      }
    }
  }
  if (input.bibleImagePromptProfileBindingId) {
    payload.bible_image_prompt_profile_binding_id = required(
      input.bibleImagePromptProfileBindingId,
      'Bible PromptProfile binding',
    )
  }

  const unattendedFieldsProvided = input.unattendedConfirmed !== undefined || input.referenceInputs !== undefined
  if (unattendedFieldsProvided) {
    if (input.unattendedConfirmed !== true) throw new Error('全自动运行必须显式确认')
    const references = input.referenceInputs ?? []
    if (!Array.isArray(references) || references.length > limits.maxReferenceBindings) {
      throw new Error(`全自动运行动态参考视频超过服务端有效绑定上限 ${limits.maxReferenceBindings} 条；实际数量还必须符合工作流合同`)
    }
    payload.unattended_confirmed = true
    payload.reference_inputs = references.map((reference) => {
      if (!SAFE_REFERENCE_IDENTIFIER.test(reference.purpose)) throw new Error('动态参考视频用途必须是安全 identifier')
      if (typeof reference.useEmbeddedAudio !== 'boolean') throw new Error('动态参考视频内嵌音轨开关必须是布尔值')
      return {
        asset_id: required(reference.assetId, '动态参考视频'),
        purpose: reference.purpose,
        use_embedded_audio: reference.useEmbeddedAudio,
      }
    })
  }

  if (input.scriptMode === 'ai') {
    payload.idea = required(input.idea || '', '故事想法')
    payload.style_hint = (input.styleHint || '').trim()
    payload.episode_count = positiveInteger(input.episodeCount, '集数')
    payload.shots_per_episode = positiveInteger(input.shotsPerEpisode, '每集镜数')
    payload.episode_duration_seconds = positiveInteger(input.episodeDurationSeconds, '单集时长')
    plannedAverageShotDurationMs(input.shotsPerEpisode, input.episodeDurationSeconds)
    return payload
  }

  if (input.scriptMode === 'confirmed_revision') {
    payload.script_revision_id = required(input.scriptRevisionId || '', '已确认剧本版本')
    if (input.structuredImportPlan) {
      payload.episode_count = positiveInteger(input.episodeCount, '集数')
      payload.shots_per_episode = positiveInteger(input.shotsPerEpisode, '每集镜数')
      payload.episode_duration_seconds = positiveInteger(input.episodeDurationSeconds, '单集时长')
      plannedAverageShotDurationMs(input.shotsPerEpisode, input.episodeDurationSeconds)
    }
    return payload
  }

  if (input.scriptMode === 'downstream') {
    payload.episode_ids = [...new Set(input.episodeIds || [])].filter(Boolean).sort()
    return payload
  }

  const unreachable: never = input.scriptMode
  throw new Error(`不支持的剧本输入模式: ${String(unreachable)}`)
}
