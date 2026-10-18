// Episode timing only. Source-media trimming has a separate contract.
export interface ShotTiming { duration: number; episodeStartMs?: number | null; episodeEndMs?: number | null }

export function timingMilliseconds(seconds: number): number {
  const value = Math.round(seconds * 1000)
  if (!Number.isSafeInteger(value) || value / 1000 !== seconds) throw new Error('时间须精确到毫秒')
  return value
}

export function shotTimingPayload(shot: ShotTiming) {
  const start = shot.episodeStartMs ?? null, end = shot.episodeEndMs ?? null
  const duration = timingMilliseconds(shot.duration)
  if (!Number.isSafeInteger(duration) || duration <= 0) throw new Error('镜头时长须大于零并精确到毫秒')
  if (start !== null || end !== null) {
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start === null || end === null || start < 0 || end <= start) {
      throw new Error('剧集内起止时间须成对填写，且结束晚于开始')
    }
    if (end - start !== duration) throw new Error('镜头时长必须等于结束减开始；请调整区间或改为只填时长')
  }
  return { episode_start_ms: start, episode_end_ms: end }
}

export function readShotTiming(raw: { episode_start_ms?: unknown; episode_end_ms?: unknown }, duration: number) {
  // API always supplies both nullable fields. Missing fields are not a null pair.
  if (raw.episode_start_ms === undefined || raw.episode_end_ms === undefined) throw new Error('服务端未返回镜头时间合同，请重新读取')
  const value = {duration, episodeStartMs: raw.episode_start_ms as number | null, episodeEndMs: raw.episode_end_ms as number | null}
  shotTimingPayload(value)
  return { episodeStartMs: value.episodeStartMs, episodeEndMs: value.episodeEndMs }
}
