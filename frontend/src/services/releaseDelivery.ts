import { adaptReleaseSelection } from './releaseBundles'

export type ReleaseDeliveryIdentity = { bundleId: string; runId: string; projectId: string }
export type ReleaseMaster = { episodeId: string; artifactId: string; assemblyId: string; sha256: string }
export type ReleaseDelivery = { document: Record<string, unknown>; title: string; verifiedAt: string; masters: ReleaseMaster[] }
const fail = (): never => { throw new Error('交付清单缺失、未实时核验或与本轮发布包不一致，请重新读取') }
const record = (value: unknown): Record<string, any> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : fail()
const uuid = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(value)
const digest = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)

export function readReleaseDelivery(value: unknown, identity: ReleaseDeliveryIdentity): ReleaseDelivery {
  const raw = record(value), verification = record(raw.verification)
  if (raw.schema !== 'autoprompt-production-release-bundle-v3' || raw.delivery_scope !== 'episode_masters'
      || raw.bundle_id !== identity.bundleId || raw.production_run_id !== identity.runId || raw.project_id !== identity.projectId
      || ![identity.bundleId, identity.runId, identity.projectId].every(uuid) || typeof raw.title !== 'string'
      || verification.status !== 'verified' || verification.signature_verified !== true || verification.live_verified !== true
      || typeof verification.verified_at !== 'string' || !Number.isFinite(Date.parse(verification.verified_at))
      || !digest(record(raw.integrity).payload_sha256)) fail()
  const selection = adaptReleaseSelection(raw.selection)
  if (!selection.episodes.length || !selection.shots.length || !Array.isArray(raw.assemblies)
      || raw.assemblies.length !== selection.episodes.length) fail()
  const seen = new Set<string>(), artifacts = new Set<string>()
  const masters = raw.assemblies.map((item: unknown): ReleaseMaster => {
    const row = record(item)
    if (row.scope !== 'episode' || !uuid(row.episode_id) || !uuid(row.generated_artifact_id) || !uuid(row.assembly_id)
        || !digest(row.artifact_sha256) || seen.has(row.episode_id) || artifacts.has(row.generated_artifact_id)
        || !selection.episodes.some(entry => entry.episodeId === row.episode_id && entry.artifactId === row.generated_artifact_id)) fail()
    seen.add(row.episode_id); artifacts.add(row.generated_artifact_id)
    return { episodeId: row.episode_id, artifactId: row.generated_artifact_id, assemblyId: row.assembly_id, sha256: row.artifact_sha256 }
  })
  // Export the server's complete verified document, not a reconstructed list or visible page.
  return { document: raw, title: raw.title, verifiedAt: verification.verified_at, masters }
}

