import type { JsonObject, ProductionRunCreateInput } from './platformTypes'
import { buildManualProductionRunPayload } from './productionManual'

const required = (value: string, label: string): string => {
  const normalized = value.trim()
  if (!normalized) throw new Error(`${label}不能为空`)
  return normalized
}

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
  if (input.planSource === 'manual') return buildManualProductionRunPayload(input)
  if (input.planSource !== 'recipe') throw new Error('请选择配方生产或人工逐步制作，不能猜测生产来源')
  const allowed = new Set(['planSource', 'projectId', 'recipeVersionId', 'idempotencyKey', 'configOverrides',
    'allowExternalCalls', 'scriptMode', 'scriptRevisionId', 'idea', 'styleHint',
    'episodeCount', 'shotsPerEpisode', 'episodeDurationSeconds', 'structuredImportPlan', 'episodeIds', 'unattendedConfirmed', 'projectSourceConfirmations'])
  if (Object.keys(input).some(key => !allowed.has(key))) throw new Error('生产请求包含未声明字段；执行配置只能来自配方冻结的实例版本。')
  const payload: JsonObject = {
    plan_source: 'recipe',
    project_id: required(input.projectId, '项目'),
    recipe_version_id: required(input.recipeVersionId, '配方版本'),
    idempotency_key: required(input.idempotencyKey, '幂等键'),
    config_overrides: input.configOverrides ?? {},
  }
  if (input.allowExternalCalls !== null && input.allowExternalCalls !== undefined) payload.allow_external_calls = input.allowExternalCalls
  if (input.projectSourceConfirmations !== undefined) {
    const seen = new Set<string>()
    if (!Array.isArray(input.projectSourceConfirmations)) throw new Error('项目来源确认必须为列表')
    payload.project_source_confirmations = input.projectSourceConfirmations.map(row => {
      if (!row || Object.keys(row).sort().join(',') !== 'executionInstanceVersionId,inputKey,reference'
        || ![row.executionInstanceVersionId,row.inputKey,row.reference].every(value=>typeof value==='string' && value.length>0)) throw new Error('项目来源需要精确实例、输入和预览凭据，不接收正文')
      const key=JSON.stringify([row.executionInstanceVersionId,row.inputKey])
      if(seen.has(key)) throw new Error('同一实例输入不能重复确认')
      seen.add(key)
      return {execution_instance_version_id:row.executionInstanceVersionId,input_key:row.inputKey,reference:row.reference}
    })
  }
  if (input.unattendedConfirmed !== undefined) {
    if (input.unattendedConfirmed !== true) throw new Error('全自动运行必须显式确认')
    payload.unattended_confirmed = true
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
    const episodes = [...new Set(input.episodeIds || [])].filter(Boolean).sort()
    if (!episodes.length) throw new Error('请明确选择至少一个已有剧集，不会自动使用全部剧集')
    payload.episode_ids = episodes
    return payload
  }

  const unreachable: never = input.scriptMode
  throw new Error(`不支持的剧本输入模式: ${String(unreachable)}`)
}
