import type { JsonObject, ProductionRun } from './platformTypes'
import type { ManualActionOptions } from './productionManual'

const record = (v: unknown): JsonObject => {
  if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('成片清单不完整，请重新读取')
  return v as JsonObject
}
const text = (v: unknown): string => {
  if (typeof v !== 'string') throw new Error('成片说明缺失')
  return v
}
const uuid = (v: unknown): string => {
  if (typeof v !== 'string' || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(v)) throw new Error('成片对象标识无效')
  return v
}
const sha = (v: unknown): string => {
  if (typeof v !== 'string' || !/^[a-f0-9]{64}$/.test(v)) throw new Error('成片摘要缺失')
  return v
}
const integer = (v: unknown, minimum: number): number => {
  if (!Number.isSafeInteger(v) || Number(v) < minimum) throw new Error('成片数量或修订无效')
  return Number(v)
}
export interface ManualDeliveryDetail {
  runPlanSha256: string; contractSha256: string; revision: number; blockers: string[]
  episodes: { id: string; number: number; title: string; jobId: string | null; artifactId: string | null;
    sha256: string | null; manifestSha256: string | null; sizeBytes: number | null; mimeType: string | null }[]
  bundle: { id: string; status: string; manifestSha256: string; sealedAt: string | null } | null
}
export function readManualDelivery(value: unknown, run: ProductionRun): ManualDeliveryDetail {
  const raw = record(value), contract = record(raw.contract)
  if (run.planSource !== 'manual' || raw.run_id !== run.id || raw.run_plan_sha256 !== run.executionPlan?.sha256
      || contract.run_plan_sha256 !== raw.run_plan_sha256 || contract.schema_version !== 1 || contract.kind !== 'manual_delivery'
      || !Array.isArray(raw.blockers) || !Array.isArray(contract.episodes)) throw new Error('成片清单不属于本轮冻结计划')
  sha(contract.assembly_contract_sha256)
  const ids = new Set<string>(), artifacts = new Set<string>()
  const episodes = contract.episodes.map(value => {
    const row = record(value), id = uuid(row.episode_id)
    if (ids.has(id)) throw new Error('成片剧集重复')
    ids.add(id)
    const artifactId = row.artifact_id === null ? null : uuid(row.artifact_id)
    if (artifactId && artifacts.has(artifactId)) throw new Error('母版重复用于多个剧集')
    if (artifactId) artifacts.add(artifactId)
    const jobId = row.assembly_job_id === null ? null : uuid(row.assembly_job_id)
    if (artifactId && !jobId) throw new Error('母版缺少真实装配任务')
    if (!artifactId && ['sha256','manifest_sha256','size_bytes','mime_type'].some(key => row[key] !== null)) throw new Error('缺失母版仍返回了成品信息')
    return { id, number: integer(row.number, 1), title: text(row.title), jobId, artifactId,
      sha256: artifactId ? sha(row.sha256) : null, manifestSha256: artifactId ? sha(row.manifest_sha256) : null,
      sizeBytes: artifactId ? integer(row.size_bytes, 1) : null, mimeType: artifactId ? text(row.mime_type) : null }
  })
  const bundle = raw.bundle === null ? null : record(raw.bundle)
  if (bundle && !['draft','sealed','released'].includes(String(bundle.status))) throw new Error('发布包状态无法识别')
  return { runPlanSha256: sha(raw.run_plan_sha256), contractSha256: sha(raw.contract_sha256), revision: integer(contract.selection_revision, 0),
    blockers: raw.blockers.map(text), episodes, bundle: bundle ? { id: uuid(bundle.id), status: text(bundle.status),
      manifestSha256: bundle.status === 'draft' ? text(bundle.manifest_sha256) : sha(bundle.manifest_sha256),
      sealedAt: bundle.sealed_at === null ? null : text(bundle.sealed_at) } : null }
}
export function buildManualDelivery(detail: ManualDeliveryDetail, run: ProductionRun, options: ManualActionOptions, key: string, comment: string): JsonObject {
  if (!key.trim() || detail.blockers.length || !detail.episodes.length || detail.episodes.some(row => !row.artifactId)
      || ['succeeded','failed','cancelled'].includes(run.status) || detail.bundle?.status === 'sealed' || detail.bundle?.status === 'released'
      || run.planSource !== 'manual' || options.runId !== run.id || options.runPlanSha256 !== detail.runPlanSha256
      || run.executionPlan?.sha256 !== detail.runPlanSha256
      || !options.actions.some(row => row.stageKey === 'delivery' && row.action === 'deliver' && row.scopeKinds.includes('project')))
    throw new Error('请先核对本轮全部成片并处理阻断项')
  return { action: 'deliver', stage_key: 'delivery', scope: { kind: 'project', id: run.project }, unit_key: `project:${run.project}`,
    expected_run_plan_sha256: detail.runPlanSha256, expected_selection_revision: detail.revision, idempotency_key: key,
    payload: { expected_contract_sha256: detail.contractSha256, comment } }
}
