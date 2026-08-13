import type { Shot } from '@/types'

/**
 * Preserve an applied high-detail value during ordinary shot synchronization
 * without teaching legacy in-memory shots to clear it accidentally.  Omitted
 * and undefined mean "do not change the server value"; an owned empty string
 * is the explicit operator intent to clear it.
 */
export const shotDetailedDescriptionPayload = (shot: Shot): { detailed_description?: string } => (
  Object.prototype.hasOwnProperty.call(shot, 'detailedDescription')
  && typeof shot.detailedDescription === 'string'
    ? { detailed_description: shot.detailedDescription }
    : {}
)

/** Keep the raw API presence/empty-string distinction when restoring a shot. */
export const adaptShotDetailedDescription = (raw: Record<string, unknown>): Pick<Shot, 'detailedDescription'> => (
  Object.prototype.hasOwnProperty.call(raw, 'detailed_description')
  && typeof raw.detailed_description === 'string'
    ? { detailedDescription: raw.detailed_description }
    : {}
)
