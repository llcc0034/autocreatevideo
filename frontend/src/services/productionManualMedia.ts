import type { GeneratedArtifact, JsonObject, ProductionRun } from './platformTypes'
import type { ManualActionOptions, ManualScopeKind, ManualTarget } from './productionManual'
import type { Asset } from '@/types'

const uuid = (value: unknown): string => {
  if (typeof value !== 'string' || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(value)) throw new Error('候选缺少有效对象标识')
  return value
}
const digest = (value: unknown): string => {
  if (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) throw new Error('候选摘要不完整，请重新读取')
  return value
}
export function readManualMediaCandidate(artifact: GeneratedArtifact, run: ProductionRun) {
  const raw = artifact.manualMediaCandidate
  const stage = run.stages?.find(row => row.stageKey === 'media_generation')
  if (run.planSource !== 'manual' || !raw || !stage || artifact.productionRun !== run.id || artifact.stageRun !== stage.id
      || raw.run_id !== run.id || raw.stage_key !== stage.stageKey || raw.run_plan_sha256 !== run.executionPlan?.sha256
      || !['image', 'video', 'audio'].includes(artifact.kind) || !['available', 'selected'].includes(artifact.status)) throw new Error('候选不属于本轮人工媒体制作，或尚未完成归档核验')
  const scope = raw.scope as Record<string, unknown> | null
  if (!scope || !['project', 'episode', 'shot'].includes(String(scope.kind)) || typeof raw.unit_key !== 'string' || !raw.unit_key
      || !Number.isSafeInteger(raw.selection_revision) || Number(raw.selection_revision) < 0) throw new Error('候选制作对象或采用修订不完整')
  if (raw.source_kind !== 'execution' && raw.source_kind !== 'asset') throw new Error('候选缺少真实来源类型')
  const execution = raw.source_kind === 'execution'
  if ((execution && raw.source_asset_id !== null) || (!execution && (raw.preparation_id !== null || raw.request_sha256 !== null))) throw new Error('素材采用与模型执行来源混用')
  const result = { scope: { kind: scope.kind as ManualScopeKind, id: uuid(scope.id) }, unitKey: raw.unit_key,
    sourceKind: raw.source_kind, sourceAssetId: execution ? null : uuid(raw.source_asset_id),
    revision: Number(raw.selection_revision), preparationId: execution ? uuid(raw.preparation_id) : null, requestSha256: execution ? digest(raw.request_sha256) : null,
    selectedArtifactId: raw.selected_artifact_id === null ? null : uuid(raw.selected_artifact_id),
    decisionId: raw.selection_decision_id === null ? null : uuid(raw.selection_decision_id),
    runPlanSha256: digest(raw.run_plan_sha256) }
  if ((result.revision === 0 && (result.selectedArtifactId !== null || result.decisionId !== null))
      || (result.revision > 0 && (!result.selectedArtifactId || !result.decisionId))
      || (artifact.status === 'selected') !== (result.selectedArtifactId === artifact.id)
      || (result.scope.kind === 'project' && result.scope.id !== run.project)) throw new Error('候选采用记录与当前状态不一致')
  return result
}

export function buildManualMediaAdoption(artifact: GeneratedArtifact, run: ProductionRun, options: ManualActionOptions, key: string): JsonObject {
  const candidate = readManualMediaCandidate(artifact, run)
  if (!key.trim() || options.runId !== run.id || options.runPlanSha256 !== candidate.runPlanSha256
      || !options.actions.some(row => row.stageKey === 'media_generation' && row.action === 'adopt')) throw new Error('请重新读取本轮媒体采用入口')
  return { action: 'adopt', stage_key: 'media_generation', scope: candidate.scope, unit_key: candidate.unitKey,
    expected_run_plan_sha256: candidate.runPlanSha256, expected_selection_revision: candidate.revision, idempotency_key: key,
    payload: { source: { kind: 'artifact', artifact_id: uuid(artifact.id), expected_sha256: digest(artifact.sha256), expected_manifest_sha256: digest(artifact.manifestMetadataSha256) } } }
}

export function buildManualAssetAdoption(asset: Asset, mediaKind: 'image' | 'video' | 'audio', target: ManualTarget,
    detail: ManualMediaReview, run: ProductionRun, options: ManualActionOptions, key: string): JsonObject {
  const action = options.actions.find(row => row.stageKey === 'media_generation' && row.action === 'adopt')
  if (run.planSource !== 'manual' || options.runId !== run.id || options.runPlanSha256 !== run.executionPlan?.sha256
      || detail.runPlanSha256 !== options.runPlanSha256 || !action?.scopeKinds.includes(target.scope.kind)
      || target.unitKey !== `${target.scope.kind}:${uuid(target.scope.id)}` || !key.trim()
      || (target.scope.kind === 'project' && target.scope.id !== run.project)) throw new Error('请重新读取本轮制作对象和采用状态')
  if (asset.isActive === false || !asset.mimeType?.startsWith(`${mediaKind}/`) || asset.metadata?.usage === 'project_cover') throw new Error('素材不可用或类型不符合本次采用')
  const members = detail.items.filter(row => row.unitKey === target.unitKey)
  if (members.length > 1 || members.some(row => row.scopeKind !== target.scope.kind || row.scopeId !== target.scope.id)) throw new Error('制作对象的采用记录不一致')
  return {action:'adopt',stage_key:'media_generation',scope:{...target.scope},unit_key:target.unitKey,
    expected_run_plan_sha256:detail.runPlanSha256,expected_selection_revision:members[0]?.revision ?? 0,idempotency_key:key,
    payload:{source:{kind:'asset',asset_id:uuid(asset.id),expected_sha256:digest(asset.checksum),media_kind:mediaKind}}}
}

export function manualMediaAdoptionResult(raw: JsonObject): string {
  if (raw.action !== 'adopt' || raw.result_kind !== 'artifact_selection') throw new Error('未返回正式采用记录，请重试同一请求核对结果')
  return uuid(raw.target_id)
}

export interface ManualMediaReview {
  selection: JsonObject
  selectionSha256: string
  runPlanSha256: string
  timeline: { id: string; number: number; title: string; shots: { id: string; number: number; title: string; durationMs: number | null; mediaKind: string; episodeStartMs: number | null; episodeEndMs: number | null }[] }[]
  items: { id: string; decisionId: string; kind: string; scopeKind: ManualScopeKind; scopeId: string; unitKey: string; revision: number }[]
  approval: { revision: number; decisionId: string | null; status: 'not_reviewed' | 'current' | 'changed'; selectionSha256: string | null; changes: string[] }
}
const record = (value: unknown): JsonObject => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('选片审核合同不完整，请重新读取')
  return value as JsonObject
}
const keys = (value: JsonObject, expected: string[]) => {
  if (Object.keys(value).sort().join('|') !== [...expected].sort().join('|')) throw new Error('选片审核合同字段不匹配')
}
export function readManualMediaReview(value: unknown, run: ProductionRun): ManualMediaReview {
  const raw = record(value), selection = record(raw.selection), approval = record(raw.approval)
  keys(raw, ['run_id', 'run_plan_sha256', 'selection', 'selection_sha256', 'approval'])
  keys(selection, ['schema_version', 'run_plan_sha256', 'source_stage_id', 'items', 'delivery_timeline', 'content_basis'])
  keys(approval, ['revision', 'decision_id', 'status', 'selection_sha256', 'changes'])
  const basis = record(selection.content_basis)
  keys(basis, ['script_sha256', 'storyboard_sha256', 'bible_sha256', 'visual_rules_sha256', 'upstream_reviews_sha256'])
  Object.values(basis).forEach(digest)
  if (!Array.isArray(approval.changes) || approval.changes.some(value => typeof value !== 'string' || !value.trim())
      || (approval.status !== 'changed' && approval.changes.length > 0)) throw new Error('选片审核变化说明不完整')
  const stage = run.stages?.find(row => row.stageKey === 'media_generation')
  if (run.planSource !== 'manual' || raw.run_id !== run.id || !stage || selection.source_stage_id !== stage.id
      || selection.schema_version !== 4 || raw.run_plan_sha256 !== run.executionPlan?.sha256
      || selection.run_plan_sha256 !== raw.run_plan_sha256 || !Array.isArray(selection.items)) throw new Error('选片不属于本轮冻结计划')
  if (!Array.isArray(selection.delivery_timeline)) throw new Error('缺少本次交付分镜，请重新读取')
  const episodes = new Set<string>(), shots = new Set<string>()
  const timeline = selection.delivery_timeline.map(value => {
    const episode = record(value)
    keys(episode, ['episode_id', 'number', 'title', 'shots'])
    const id = uuid(episode.episode_id)
    if (episodes.has(id) || !Number.isSafeInteger(episode.number) || typeof episode.title !== 'string' || !Array.isArray(episode.shots)) throw new Error('交付剧集信息不完整')
    episodes.add(id)
    return { id, number: Number(episode.number), title: episode.title, shots: episode.shots.map(value => {
      const shot = record(value)
      keys(shot, ['shot_id', 'number', 'title', 'duration_ms', 'media_kind', 'episode_start_ms', 'episode_end_ms'])
      const id = uuid(shot.shot_id)
      if (shots.has(id) || !Number.isSafeInteger(shot.number) || typeof shot.title !== 'string' || typeof shot.media_kind !== 'string'
          || (shot.duration_ms !== null && !Number.isSafeInteger(shot.duration_ms))) throw new Error('交付镜头信息不完整')
      shots.add(id)
      const start = shot.episode_start_ms, end = shot.episode_end_ms
      if ((start !== null || end !== null) && (!Number.isSafeInteger(start) || !Number.isSafeInteger(end)
          || Number(start) < 0 || Number(end) <= Number(start) || Number(end) - Number(start) !== shot.duration_ms)) throw new Error('交付镜头起止时间与时长不一致')
      return { id, number: Number(shot.number), title: shot.title, durationMs: shot.duration_ms === null ? null : Number(shot.duration_ms), mediaKind: shot.media_kind,
        episodeStartMs: start === null ? null : Number(start), episodeEndMs: end === null ? null : Number(end) }
    }) }
  })
  const groups = new Set<string>(), artifacts = new Set<string>()
  const items = selection.items.map(value => {
    const row = record(value), scope = record(row.scope)
    keys(row, ['artifact_id', 'kind', 'artifact_sha256', 'manifest_sha256', 'decision_id', 'decision_revision', 'variant_group', 'scope', 'unit_key'])
    keys(scope, ['kind', 'id'])
    const id = uuid(row.artifact_id), scopeId = uuid(scope.id)
    uuid(row.decision_id); digest(row.artifact_sha256); digest(row.manifest_sha256)
    if (!['image','video','audio'].includes(String(row.kind)) || !['project','episode','shot'].includes(String(scope.kind))
        || typeof row.unit_key !== 'string' || !row.unit_key || typeof row.variant_group !== 'string' || !row.variant_group
        || !Number.isSafeInteger(row.decision_revision) || Number(row.decision_revision) < 1
        || artifacts.has(id) || groups.has(row.variant_group) || (scope.kind === 'project' && scopeId !== run.project)) throw new Error('选片成员、制作对象或采用修订无效')
    groups.add(row.variant_group); artifacts.add(id)
    return { id, decisionId: uuid(row.decision_id), kind: String(row.kind), scopeKind: scope.kind as ManualScopeKind, scopeId, unitKey: row.unit_key, revision: Number(row.decision_revision) }
  })
  const state = { revision: Number(approval.revision), status: approval.status as ManualMediaReview['approval']['status'],
    decisionId: approval.decision_id === null ? null : uuid(approval.decision_id),
    selectionSha256: approval.selection_sha256 === null ? null : digest(approval.selection_sha256), changes: approval.changes as string[] }
  const selectionSha256 = digest(raw.selection_sha256)
  if (!Number.isSafeInteger(approval.revision) || state.revision < 0 || !['not_reviewed', 'current', 'changed'].includes(state.status)
      || (state.revision === 0 ? state.status !== 'not_reviewed' || state.decisionId !== null || state.selectionSha256 !== null
        : state.status === 'not_reviewed' || state.decisionId === null || state.selectionSha256 === null
          || (state.status === 'current') !== (state.selectionSha256 === selectionSha256))) throw new Error('选片审核结果与当前集合不一致')
  return { selection: JSON.parse(JSON.stringify(selection)), selectionSha256, items, timeline, approval: state, runPlanSha256: digest(raw.run_plan_sha256) }
}

export function buildManualMediaReview(detail: ManualMediaReview, run: ProductionRun, options: ManualActionOptions, key: string, comment: string): JsonObject {
  if (!detail.items.length || !key.trim() || detail.runPlanSha256 !== run.executionPlan?.sha256 || options.runId !== run.id
      || options.runPlanSha256 !== detail.runPlanSha256 || !options.actions.some(row => row.stageKey === 'artifact_selection' && row.action === 'approve')) throw new Error('请先读取并核对本轮完整的非空选片清单')
  return { action: 'approve', stage_key: 'artifact_selection', scope: { kind: 'project', id: run.project }, unit_key: `project:${run.project}`,
    expected_run_plan_sha256: detail.runPlanSha256, expected_selection_revision: detail.approval.revision, idempotency_key: key,
    payload: { selection: detail.selection, expected_selection_sha256: detail.selectionSha256, comment } }
}
