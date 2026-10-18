export interface AuthSessionSnapshot {
  access: string
  refresh: string
  userId: string
}

type SessionStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export const readAuthSession = (storage: SessionStorage): AuthSessionSnapshot => {
  let userId = ''
  try {
    const user = JSON.parse(storage.getItem('cineforge_user') || 'null')
    if (typeof user?.id === 'number' || typeof user?.id === 'string') userId = String(user.id)
  } catch { /* An unreadable identity cannot authorize replay of an old request. */ }
  return {
    access: storage.getItem('cineforge_token') || '',
    refresh: storage.getItem('cineforge_refresh_token') || '',
    userId,
  }
}

export class AuthSessionChangedError extends Error {
  constructor() { super('登录状态已变化，请重新执行当前操作。') }
}

export const sameAuthSession = (a: AuthSessionSnapshot, b: AuthSessionSnapshot): boolean => (
  a.access === b.access && a.refresh === b.refresh && a.userId === b.userId
)

/** Serialize rotated refresh tokens across all tabs of this origin. */
export const refreshAuthSession = async (options: {
  expired: AuthSessionSnapshot
  storage: SessionStorage
  withLock: (work: () => Promise<string>) => Promise<string>
  exchange: (refresh: string) => Promise<{ access: string; refresh?: string }>
  isRejected: (error: unknown) => boolean
  onInvalidated: () => void
}): Promise<string> => options.withLock(async () => {
  const { storage, expired } = options
  const current = readAuthSession(storage)
  if (!expired.userId || current.userId !== expired.userId || !current.access) throw new AuthSessionChangedError()
  // Another tab has already refreshed. Do not exchange its consumed predecessor again.
  if (current.access !== expired.access) return current.access
  if (!sameAuthSession(current, expired)) throw new AuthSessionChangedError()
  const invalidate = () => {
    if (!sameAuthSession(readAuthSession(storage), current)) return
    storage.removeItem('cineforge_token')
    storage.removeItem('cineforge_refresh_token')
    storage.removeItem('cineforge_user')
    options.onInvalidated()
  }
  if (!current.refresh) {
    invalidate()
    throw new AuthSessionChangedError()
  }
  try {
    const tokens = await options.exchange(current.refresh)
    if (!sameAuthSession(readAuthSession(storage), current)) throw new AuthSessionChangedError()
    if (typeof tokens.access !== 'string' || !tokens.access || (tokens.refresh !== undefined && (typeof tokens.refresh !== 'string' || !tokens.refresh))) {
      throw new Error('登录续期响应不完整，请重试。')
    }
    storage.setItem('cineforge_token', tokens.access)
    if (tokens.refresh) storage.setItem('cineforge_refresh_token', tokens.refresh)
    return tokens.access
  } catch (error) {
    // Network/5xx failures are not proof that the user's session has expired.
    if (options.isRejected(error)) invalidate()
    throw error
  }
})
