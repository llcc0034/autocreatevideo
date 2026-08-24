export const legacyShotVersionKey = (episodeId: string): string => {
  const id = String(episodeId || '').trim()
  if (!id) throw new Error('缺少剧集标识，无法确定初始分镜版本。')
  return `legacy:${id}:initial`
}

export const normalizeShotVersionScopeKey = (
  value: string | undefined,
  episodeId: string,
): string => String(value || '').trim() || legacyShotVersionKey(episodeId)
