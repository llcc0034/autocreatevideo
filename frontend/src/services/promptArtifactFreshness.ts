import type { H3PromptPayload, PromptTranslationEntry, Shot } from '@/types'

type ShotTranslationScope = Pick<Shot, 'id' | 'characterIds' | 'sceneId' | 'propIds'>

const timestamp = (value?: string): number => {
  const parsed = Date.parse(String(value || ''))
  return Number.isFinite(parsed) ? parsed : 0
}

/** Whether one translation row can affect this shot's compiler input. */
export const translationAppliesToShot = (
  entry: PromptTranslationEntry,
  projectId: string,
  shot: ShotTranslationScope,
): boolean => {
  const scopeId = String(entry.scopeId || '')
  if (entry.scopeType === 'project') return scopeId === projectId
  if (entry.scopeType === 'shot') return scopeId === shot.id
  if (entry.scopeType === 'character') return shot.characterIds.includes(scopeId)
  if (entry.scopeType === 'scene') return Boolean(shot.sceneId && scopeId === shot.sceneId)
  if (entry.scopeType === 'prop') return shot.propIds.includes(scopeId)
  return false
}

/**
 * Persisted prompt artifacts are immutable. A compiler upgrade always makes an
 * old artifact unsuitable for the current preview. Translation mutations that
 * happened after the artifact are also relevant because confirmed overlays are
 * part of the backend prompt input fingerprint.
 */
export const promptArtifactNeedsRegeneration = (
  artifact: H3PromptPayload | undefined,
  projectId: string,
  shot: ShotTranslationScope,
  translations: PromptTranslationEntry[] = [],
): boolean => {
  if (!artifact) return false
  if (artifact.compiler_current === false) return true
  const artifactCreatedAt = timestamp(artifact.artifact_created_at)
  if (!artifactCreatedAt) return false
  return translations.some((entry) => (
    translationAppliesToShot(entry, projectId, shot)
    && timestamp(entry.updatedAt || entry.confirmedAt) > artifactCreatedAt
  ))
}
