import type { Episode, Shot } from '@/types'

export const shotNarrativeDurationSeconds = (shot: Pick<Shot, 'duration'>): number => {
  const value = Number(shot.duration)
  return Number.isFinite(value) && value > 0 ? value : 0
}

export const episodeNarrativeDurationSeconds = (episode: Pick<Episode, 'shots'>): number => (
  episode.shots.reduce((total, shot) => total + shotNarrativeDurationSeconds(shot), 0)
)

export const seriesNarrativeDurationSeconds = (episodes: Array<Pick<Episode, 'shots'>>): number => (
  episodes.reduce((total, episode) => total + episodeNarrativeDurationSeconds(episode), 0)
)

export const formatNarrativeDuration = (seconds: number): string => {
  const value = Number.isFinite(seconds) ? Math.max(0, seconds) : 0
  return Number.isInteger(value) ? String(value) : value.toFixed(1).replace(/\.0$/, '')
}
