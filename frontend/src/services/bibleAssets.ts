import type { BibleItem } from '@/types'

/**
 * Return every image the entity may use for continuity selection while keeping
 * the canonical anchor and supplemental references as separate persisted roles.
 */
export const bibleAssetIds = (item?: BibleItem | null): string[] => [
  ...new Set([
    item?.anchorAssetId || '',
    ...(item?.referenceAssetIds || []),
  ].filter(Boolean)),
]
