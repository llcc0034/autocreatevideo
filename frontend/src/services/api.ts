import axios, { type AxiosError } from 'axios'
import { apiErrorPresentation } from './apiErrorPresentation'
import { AuthSessionChangedError, readAuthSession, refreshAuthSession, type AuthSessionSnapshot } from './authRefresh'

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api/v1',
  headers: { 'Content-Type': 'application/json' }
})

type RetryConfig = NonNullable<AxiosError['config']> & { _cineforgeRetry?: boolean; _cineforgeRequestSession?: AuthSessionSnapshot }

api.interceptors.request.use((config: RetryConfig) => {
  const session = readAuthSession(localStorage)
  if (config._cineforgeRetry && config._cineforgeRequestSession?.userId !== session.userId) throw new AuthSessionChangedError()
  const token = session.access
  config._cineforgeRequestSession = session
  if (token) config.headers.Authorization = `Bearer ${token}`
  else delete config.headers.Authorization
  return config
})

const errorText = (value: unknown): string[] => {
  if (typeof value === 'string') return [value]
  if (Array.isArray(value)) return value.flatMap(errorText)
  if (value && typeof value === 'object') return Object.entries(value as Record<string, unknown>).flatMap(([key, item]) => {
    if (['code', 'status', 'status_code', 'entry_id', 'field_key', 'scope_id'].includes(key)) return []
    const messages = errorText(item)
    return messages.map((message) => ['detail', 'message', 'error', 'details'].includes(key) ? message : `${key}: ${message}`)
  })
  return []
}

export const safeApiErrorMessage = (
  value: unknown,
  status?: number,
  contentType = '',
): string => {
  const presentation = apiErrorPresentation({
    response: {
      status,
      headers: { 'content-type': contentType },
      data: typeof value === 'string' ? { detail: value } : value,
    },
  }, '')
  if (presentation.message) return presentation.message
  const message = errorText(value).slice(0, 20).join('；').trim()
  return message.length <= 500 ? message : `${message.slice(0, 499)}…`
}

const normalizeApiError = (error: AxiosError) => {
  const message = apiErrorPresentation(error, '').message
  if (message) error.message = message
  return error
}

const responseErrorCodes = (value: unknown): string[] => {
  if (!value || typeof value !== 'object') return []
  const row = value as Record<string, unknown>
  const nested = row.error && typeof row.error === 'object' ? row.error as Record<string, unknown> : {}
  const details = nested.details && typeof nested.details === 'object' ? nested.details as Record<string, unknown> : {}
  return [row.code, nested.code, details.code].filter((item): item is string => typeof item === 'string')
}

api.interceptors.response.use((response) => response, async (error: AxiosError) => {
  const config = error.config as RetryConfig | undefined
  const requestUrl = String(config?.url || '')
  const isCredentialExchange = [
    '/auth/token/', '/auth/token/refresh/', '/auth/register/',
  ].some((path) => requestUrl.includes(path))
  if (
    error.response?.status === 403
    && responseErrorCodes(error.response.data).includes('password_change_required')
  ) {
    window.dispatchEvent(new CustomEvent('cineforge:password-change-required'))
  }
  if (error.response?.status === 401 && config && !isCredentialExchange && !config._cineforgeRetry) {
    config._cineforgeRetry = true
    const expired = config._cineforgeRequestSession
    if (!expired) return Promise.reject(normalizeApiError(error))
    const access = await refreshAuthSession({
      expired,
      storage: localStorage,
      withLock: (work) => {
        if (!navigator.locks) throw new Error('当前浏览器不支持安全登录续期，请使用支持 Web Locks 的浏览器重新登录。')
        return navigator.locks.request('cineforge:auth-refresh', work)
      },
      exchange: async (refresh) => (await api.post('/auth/token/refresh/', { refresh })).data,
      isRejected: (cause) => axios.isAxiosError(cause) && [400, 401].includes(cause.response?.status || 0),
      onInvalidated: () => window.dispatchEvent(new CustomEvent('cineforge:unauthorized')),
    })
    if (access !== localStorage.getItem('cineforge_token')) throw new AuthSessionChangedError()
    config.headers.Authorization = `Bearer ${access}`
    return api.request(config)
  }
  return Promise.reject(normalizeApiError(error))
})

export const unwrap = <T>(payload: unknown): T => {
  const body = payload as { data?: unknown; results?: unknown }
  return (body?.results ?? body?.data ?? body) as T
}

/** Follow DRF pagination until `next` is null; array responses remain supported. */
export const fetchAllPages = async <T>(endpoint: string, params: Record<string, unknown> = {}): Promise<T[]> => {
  const items: T[] = []
  const seen = new Set<string>()
  let next: string | null = endpoint
  let first = true

  for (let pageNumber = 0; next && pageNumber < 10_000; pageNumber += 1) {
    if (seen.has(next)) throw new Error(`分页响应出现循环：${next}`)
    seen.add(next)
    const response = await api.get(next, { params: first ? params : undefined })
    first = false
    const outer = response.data as { data?: unknown }
    const page = (outer?.data && !Array.isArray(outer.data) ? outer.data : response.data) as { results?: T[]; next?: string | null }
    if (Array.isArray(page)) return [...items, ...page]
    if (!Array.isArray(page?.results)) throw new Error(`分页响应缺少 results：${next}`)
    items.push(...page.results)
    next = typeof page.next === 'string' && page.next ? page.next : null
  }

  if (next) throw new Error('分页超过 10000 页，已停止以防止异常循环')
  return items
}
