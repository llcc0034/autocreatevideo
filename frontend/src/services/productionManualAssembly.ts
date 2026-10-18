import type { JsonObject, ProductionRun } from './platformTypes'
import type { ManualActionOptions } from './productionManual'

const record = (value: unknown): JsonObject => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('装配信息不完整，请重新读取')
  return value as JsonObject
}
const uuid = (value: unknown): string => {
  if (typeof value !== 'string' || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(value)) throw new Error('装配对象标识无效')
  return value
}
const digest = (value: unknown): string => {
  if (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) throw new Error('装配摘要缺失')
  return value
}
const integer = (value: unknown, minimum: number): number => {
  if (!Number.isSafeInteger(value) || Number(value) < minimum) throw new Error('装配数值不完整')
  return Number(value)
}
const text = (value: unknown): string => {
  if (typeof value !== 'string') throw new Error('装配说明未返回')
  return value
}
export interface ManualAssemblyEpisode {
  id: string; number: number; title: string; linkId: string | null
  shots: { id: string; number: number; title: string; durationMs: number; kind: string; artifactId: string | null }[]
  job: { id: string; status: string; attempts: number; maxAttempts: number; error: string; updatedAt: string } | null
}
export interface ManualAssemblyDetail {
  runPlanSha256: string; contractSha256: string; frozen: boolean; blockers: string[]; revision: number
  episodes: ManualAssemblyEpisode[]
  authors: JsonObject; authorContracts: JsonObject; authorSchemas: JsonObject
  output: { width: number; height: number; fps: number; profile: JsonObject; profileKey: string; profileSha256: string; maxAttempts: number; labelEnabled: boolean; label: string }
}
export function readManualAssembly(value: unknown, run: ProductionRun): ManualAssemblyDetail {
  const raw = record(value), output = record(raw.output), approval = record(raw.approval)
  if (run.planSource !== 'manual' || raw.run_id !== run.id || raw.run_plan_sha256 !== run.executionPlan?.sha256
      || typeof raw.frozen !== 'boolean' || !Array.isArray(raw.blockers) || !Array.isArray(raw.episodes)
      || typeof output.label_enabled !== 'boolean') throw new Error('装配信息不属于本轮冻结计划')
  const ids = new Set<string>(), shots = new Set<string>()
  const authors = record(raw.authors), authorContracts = record(raw.author_contracts), authorSchemas = record(raw.author_schemas)
  for (const key of ['assembly_output', 'assembly_timing', 'assembly_media']) record(authorSchemas[key])
  if (Object.keys(authors).some(key => !['assembly_output', 'assembly_timing', 'assembly_media'].includes(key))
      || JSON.stringify(Object.keys(authors).sort()) !== JSON.stringify(Object.keys(authorContracts).sort())) throw new Error('装配映射与冻结合同不一致')
  for (const [key, rule] of Object.entries(authors)) {
    const value = record(rule), contract = record(authorContracts[key]), reference = record(contract.output_reference)
    uuid(value.output_version_id); digest(value.expected_contract_sha256); text(value.source_text); record(value.mapping)
    if (reference.id !== value.output_version_id || reference.sha256 !== value.expected_contract_sha256
        || record(contract.projection).valid !== true) throw new Error('装配输出版本或映射结果无效')
  }
  const episodes = raw.episodes.map(value => {
    const row = record(value), id = uuid(row.episode_id)
    if (ids.has(id) || !Array.isArray(row.shots)) throw new Error('剧集装配成员重复或缺失')
    ids.add(id)
    const members = row.shots.map(value => {
      const shot = record(value), key = uuid(shot.shot_id)
      if (shots.has(key)) throw new Error('装配镜头重复')
      shots.add(key)
      const source = shot.source === null ? null : record(shot.source)
      if (source) {
        const scope = record(source.scope)
        if (scope.kind !== 'shot' || scope.id !== key || source.kind !== shot.media_kind) throw new Error('已采用媒体与镜头不一致')
        digest(source.artifact_sha256); digest(source.manifest_sha256); uuid(source.decision_id)
      }
      return { id: key, number: integer(shot.number, 1), title: text(shot.title), durationMs: integer(shot.duration_ms, 0),
        kind: text(shot.media_kind), artifactId: source ? uuid(source.artifact_id) : null }
    })
    const job = row.job === null ? null : record(row.job)
    const linkId = row.assembly_link_id === null ? null : uuid(row.assembly_link_id)
    if (Boolean(job) !== Boolean(linkId)) throw new Error('装配任务与关联记录不一致')
    if (job && !['planned','queued','running','succeeded','failed'].includes(String(job.status))) throw new Error('未识别的装配任务状态')
    return { id, number: integer(row.number, 1), title: text(row.title), linkId, shots: members,
      job: job ? { id: uuid(job.id), status: text(job.status), attempts: integer(job.attempts, 0),
        maxAttempts: integer(job.max_attempts, 1), error: text(job.error_message), updatedAt: text(job.updated_at) } : null }
  })
  return { runPlanSha256: digest(raw.run_plan_sha256), contractSha256: digest(raw.contract_sha256), frozen: raw.frozen,
    blockers: raw.blockers.map(text), revision: integer(approval.revision, 0), episodes, authors, authorContracts, authorSchemas,
    output: { width: integer(output.width, 1), height: integer(output.height, 1), fps: integer(output.fps, 1),
      profile: record(output.media_profile), profileKey: text(output.media_profile_key), profileSha256: digest(output.media_profile_sha256),
      maxAttempts: integer(output.max_attempts, 1), labelEnabled: output.label_enabled, label: text(output.label_text) } }
}
export function buildManualAssembly(detail: ManualAssemblyDetail, episodeId: string, run: ProductionRun, options: ManualActionOptions, key: string): JsonObject {
  const episode = detail.episodes.find(row => row.id === episodeId)
  if (!episode || detail.blockers.length || !key.trim() || ['succeeded','failed','cancelled'].includes(run.status)
      || detail.runPlanSha256 !== run.executionPlan?.sha256 || options.runId !== run.id || options.runPlanSha256 !== detail.runPlanSha256
      || !options.actions.some(row => row.stageKey === 'assembly' && row.action === 'assemble' && row.scopeKinds.includes('episode'))
      || (episode.job && episode.job.status !== 'failed')) throw new Error('请先核对本轮选片、装配参数和实际任务状态')
  return { action: 'assemble', stage_key: 'assembly', scope: { kind: 'episode', id: episode.id }, unit_key: `episode:${episode.id}`,
    expected_run_plan_sha256: detail.runPlanSha256, expected_selection_revision: detail.revision, idempotency_key: key,
    payload: { expected_contract_sha256: detail.contractSha256, expected_assembly_job_id: episode.job?.id ?? null,
      ...(Object.keys(detail.authors).length ? { authors: JSON.parse(JSON.stringify(detail.authors)) } : {}) } }
}
