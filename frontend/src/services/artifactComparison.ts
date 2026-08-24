import type { JsonObject } from './platformTypes'

export interface ArtifactCandidate {
  id: string
  status: string
}

export interface ParameterDifferenceRow {
  key: string
  values: string[]
}

export interface SynchronizableVideo {
  currentTime: number
  paused: boolean
  play: () => Promise<unknown>
  pause: () => void
}

export const reconcileArtifactSelection = (
  artifacts: ArtifactCandidate[],
  selectedIds: string[],
): string[] => {
  const ids = artifacts.map((item) => item.id)
  const selectedByServer = artifacts
    .filter((item) => item.status === 'selected')
    .map((item) => item.id)
  const preserved = [
    ...selectedByServer,
    ...selectedIds.filter((id) => ids.includes(id) && !selectedByServer.includes(id)),
  ].slice(0, 4)
  const target = Math.min(4, ids.length)
  for (const id of ids) {
    if (preserved.length >= target) break
    if (!preserved.includes(id)) preserved.push(id)
  }
  return preserved
}

export const toggleArtifactSelection = (
  selectedIds: string[],
  artifactId: string,
  checked: boolean,
  artifactCount: number,
): string[] => {
  if (checked) {
    if (selectedIds.includes(artifactId) || selectedIds.length >= 4) return selectedIds
    return [...selectedIds, artifactId]
  }
  if (!selectedIds.includes(artifactId) || selectedIds.length <= Math.min(2, artifactCount)) {
    return selectedIds
  }
  return selectedIds.filter((id) => id !== artifactId)
}

const flatten = (value: JsonObject, prefix = ''): Record<string, string> => {
  const result: Record<string, string> = {}
  for (const [key, item] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (item && typeof item === 'object' && !Array.isArray(item)) {
      Object.assign(result, flatten(item as JsonObject, path))
    } else {
      result[path] = JSON.stringify(item)
    }
  }
  return result
}

export const comparisonParameterRows = (values: JsonObject[]): ParameterDifferenceRow[] => {
  const flattened = values.map((value) => flatten(value))
  const keys = [...new Set(flattened.flatMap((row) => Object.keys(row)))]
  return keys
    .filter((key) => new Set(flattened.map((row) => row[key] ?? '—')).size > 1)
    .map((key) => ({ key, values: flattened.map((row) => row[key] ?? '—') }))
}

export const synchronizeVideos = async (
  sourceId: string,
  mode: 'play' | 'pause' | 'seek',
  videos: ReadonlyMap<string, SynchronizableVideo>,
): Promise<void> => {
  const source = videos.get(sourceId)
  if (!source) return
  for (const [id, video] of videos) {
    if (id === sourceId) continue
    if (Math.abs(video.currentTime - source.currentTime) > 0.08) {
      video.currentTime = source.currentTime
    }
    if (mode === 'play' && video.paused) await video.play().catch(() => undefined)
    if (mode === 'pause' && !video.paused) video.pause()
  }
}
