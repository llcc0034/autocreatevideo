import type { ReleaseBundle, ReleaseSelection } from '@/types'

const fail = (field: string): never => { throw new Error(`发布包 ${field} 缺失或不符合当前产物合同，请重新读取或检查服务版本`) }
const object = (value: unknown, field: string): Record<string, any> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fail(field)
  return value as Record<string, any>
}
const id = (value: unknown, field: string): string => {
  if (typeof value !== 'string' || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(value)) return fail(field)
  return value
}
const text = (value: unknown, field: string): string => typeof value === 'string' ? value : fail(field)
const optionalText = (value: unknown, field: string): string | undefined => value === null || value === '' ? undefined : text(value, field)

export function adaptReleaseSelection(value: unknown): ReleaseSelection {
  const raw = object(value, 'selection')
  if (Object.keys(raw).sort().join(',') !== 'episodes,series_artifact_id,shots' || raw.series_artifact_id !== null) fail('selection')
  const read = (key: 'shots' | 'episodes', memberKey: 'shot_id' | 'episode_id') => {
    if (!Array.isArray(raw[key])) return fail(key)
    const seen = new Set<string>()
    return raw[key].map((entry: unknown) => {
      const row = object(entry, key)
      if (Object.keys(row).sort().join(',') !== ['artifact_id', memberKey].sort().join(',')) fail(key)
      const memberId = id(row[memberKey], memberKey), artifactId = id(row.artifact_id, 'artifact_id')
      if (seen.has(memberId)) fail(`${key} 重复成员`)
      seen.add(memberId)
      return { memberId, artifactId }
    })
  }
  return {
    shots: read('shots', 'shot_id').map(row => ({ shotId: row.memberId, artifactId: row.artifactId })),
    episodes: read('episodes', 'episode_id').map(row => ({ episodeId: row.memberId, artifactId: row.artifactId })),
  }
}

export function adaptReleaseBundle(value: unknown): ReleaseBundle {
  const raw = object(value, '对象'), selection = adaptReleaseSelection(raw.selection)
  if (!Number.isInteger(raw.version) || raw.version < 1) fail('version')
  if (!['draft', 'sealed', 'released', 'superseded'].includes(raw.status)) fail('status')
  if (!['draft', 'verified', 'invalid'].includes(raw.verification_status)) fail('verification_status')
  let assemblySources: ReleaseBundle['assemblySources']
  if (raw.verification_status === 'verified') {
    const manifest = object(raw.manifest, 'manifest')
    if (manifest.schema !== 'autoprompt-production-release-bundle-v3' || manifest.production_run_id !== raw.production_run
        || !Array.isArray(manifest.assemblies) || manifest.assemblies.length !== selection.episodes.length) fail('manifest')
    const seen = new Set<string>()
    assemblySources = manifest.assemblies.map((entry: unknown) => {
      const row = object(entry, 'assemblies'), episodeId = id(row.episode_id, 'episode_id')
      const artifactId = id(row.generated_artifact_id, 'generated_artifact_id'), assemblyId = id(row.assembly_id, 'assembly_id')
      if (row.scope !== 'episode' || seen.has(episodeId)
          || !selection.episodes.some(selected => selected.episodeId === episodeId && selected.artifactId === artifactId)) fail('assemblies')
      seen.add(episodeId)
      return { episodeId, artifactId, assemblyId }
    })
  }
  return {
    id: id(raw.id, 'id'), projectId: id(raw.project, 'project'), productionRunId: id(raw.production_run, 'production_run'),
    version: raw.version, title: text(raw.title, 'title'), notes: text(raw.notes, 'notes'), status: raw.status,
    selection, assemblySources, manifestSha256: optionalText(raw.manifest_sha256, 'manifest_sha256'),
    verificationStatus: raw.verification_status, sealedAt: optionalText(raw.sealed_at, 'sealed_at'),
    releasedAt: optionalText(raw.released_at, 'released_at'), createdAt: text(raw.created_at, 'created_at'), updatedAt: text(raw.updated_at, 'updated_at'),
  }
}
