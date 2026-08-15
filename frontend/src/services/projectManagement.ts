import type { ContinuityBible, SeriesProject } from '@/types'

const REDO_TITLE_SUFFIX = ' · 重做'
const REDO_TITLE_BASE_MAX_LENGTH = 194

export const PROJECT_COVER_ACCEPT = 'image/jpeg,image/png,image/webp'
export const PROJECT_COVER_MAX_BYTES = 10 * 1024 * 1024

export type ProjectCoverFile = Pick<File, 'name' | 'size' | 'type'>

/** Fast client check only; the backend remains authoritative and inspects file bytes. */
export const projectCoverFileError = (file: ProjectCoverFile): string | null => {
  if (!PROJECT_COVER_ACCEPT.split(',').includes(file.type.toLowerCase())) return '封面仅支持 JPEG、PNG 或 WebP 图片。'
  if (!Number.isFinite(file.size) || file.size <= 0) return '封面文件为空，请重新选择。'
  if (file.size > PROJECT_COVER_MAX_BYTES) return '封面文件不能超过 10 MB。'
  return null
}

export type ProjectRedoPayload = Pick<
  SeriesProject,
  | 'title'
  | 'logline'
  | 'masterScript'
  | 'isFavorite'
  | 'continuitySummary'
  | 'translationGlossary'
  | 'styleId'
  | 'status'
  | 'aspectRatio'
  | 'resolution'
  | 'defaultMode'
  | 'bible'
>

/** Return a fresh Bible so redo drafts never share mutable arrays with a source project. */
export const createEmptyProjectBible = (): ContinuityBible => ({
  characters: [],
  scenes: [],
  props: [],
  negativePrompt: '',
  colorScript: '',
})

const redoTitle = (title: string): string => {
  const normalized = title.trim() || '未命名连续剧'
  const base = Array.from(normalized).slice(0, REDO_TITLE_BASE_MAX_LENGTH).join('').trimEnd()
  return `${base}${REDO_TITLE_SUFFIX}`
}

/**
 * Build the intentionally small create payload used by “基于此项目重做”.
 * Runtime/task output, IDs, episodes and timestamps are excluded by construction.
 */
export const buildProjectRedoPayload = (project: SeriesProject): ProjectRedoPayload => ({
  title: redoTitle(project.title),
  logline: project.logline,
  masterScript: project.masterScript,
  isFavorite: false,
  continuitySummary: '',
  translationGlossary: {},
  styleId: project.styleId,
  status: 'draft',
  aspectRatio: project.aspectRatio,
  resolution: project.resolution,
  defaultMode: project.defaultMode,
  bible: createEmptyProjectBible(),
})

/** Invalid and missing dates return null instead of leaking Invalid Date into the UI. */
export const safeProjectTimestamp = (value: unknown): number | null => {
  if (typeof value === 'string') {
    if (!value.trim()) return null
    const timestamp = Date.parse(value)
    return Number.isFinite(timestamp) ? timestamp : null
  }
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (value instanceof Date) {
    const timestamp = value.getTime()
    return Number.isFinite(timestamp) ? timestamp : null
  }
  return null
}

export const formatProjectUpdatedAt = (value: unknown, locale = 'zh-CN'): string => {
  const timestamp = safeProjectTimestamp(value)
  if (timestamp === null) return '时间未知'
  try {
    return new Intl.DateTimeFormat(locale, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }).format(timestamp)
  } catch {
    return '时间未知'
  }
}

/** Favorite projects are pinned; each group is ordered by newest valid update time. */
export const sortProjectsForManagement = <T extends Pick<SeriesProject, 'isFavorite' | 'updatedAt'>>(
  projects: readonly T[],
): T[] => projects
  .map((project, index) => ({ project, index }))
  .sort((left, right) => {
    const favoriteDifference = Number(Boolean(right.project.isFavorite)) - Number(Boolean(left.project.isFavorite))
    if (favoriteDifference) return favoriteDifference
    const leftTimestamp = safeProjectTimestamp(left.project.updatedAt) ?? Number.NEGATIVE_INFINITY
    const rightTimestamp = safeProjectTimestamp(right.project.updatedAt) ?? Number.NEGATIVE_INFINITY
    return rightTimestamp - leftTimestamp || left.index - right.index
  })
  .map(({ project }) => project)

/** Both values are trimmed, then compared exactly and case-sensitively. */
export const isProjectDeleteConfirmed = (projectTitle: string, confirmation: string): boolean => {
  const expected = projectTitle.trim()
  return Boolean(expected) && confirmation.trim() === expected
}

export interface ProjectCompletion {
  completed: number
  total: number
  percent: number
}

export const projectEpisodeSummary = (
  project: Pick<SeriesProject, 'episodes'>,
): Pick<SeriesProject, 'episodeCount' | 'completedEpisodeCount' | 'totalDurationSeconds'> => ({
  episodeCount: project.episodes.length,
  completedEpisodeCount: project.episodes.filter((episode) => episode.status === 'completed').length,
  totalDurationSeconds: project.episodes.reduce(
    (total, episode) => total + episode.shots.reduce((sum, shot) => sum + shot.duration, 0),
    0,
  ),
})

export const applyProjectEpisodeSummary = (project: SeriesProject): SeriesProject => {
  Object.assign(project, projectEpisodeSummary(project))
  return project
}

export const createSessionEpoch = () => {
  let current = 0
  return {
    capture: () => current,
    invalidate: () => { current += 1 },
    isCurrent: (epoch: number) => epoch === current,
  }
}

export class SessionInvalidatedError extends Error {
  constructor() {
    super('The active session changed while the request was in flight.')
    this.name = 'SessionInvalidatedError'
  }
}

export const projectCompletion = (project: Pick<SeriesProject, 'episodes' | 'episodeCount' | 'completedEpisodeCount' | 'detailLoaded'>): ProjectCompletion => {
  const detailSummary = project.detailLoaded || project.episodes.length > 0
    ? projectEpisodeSummary(project)
    : null
  const total = detailSummary?.episodeCount ?? project.episodeCount ?? 0
  const completed = detailSummary?.completedEpisodeCount ?? project.completedEpisodeCount ?? 0
  return { completed, total, percent: total ? Math.round((completed / total) * 100) : 0 }
}
