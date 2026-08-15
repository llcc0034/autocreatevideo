import axios, { type AxiosError } from 'axios'

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api/v1',
  timeout: 12000,
  headers: { 'Content-Type': 'application/json' }
})

type RetryConfig = NonNullable<AxiosError['config']> & { _cineforgeRetry?: boolean; _cineforgeRequestToken?: string }
let refreshRequest: { refreshToken: string; request: Promise<string> } | null = null

api.interceptors.request.use((config: RetryConfig) => {
  const token = localStorage.getItem('cineforge_token') || ''
  config._cineforgeRequestToken = token
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

const normalizeApiError = (error: AxiosError) => {
  const data = error.response?.data as { error?: { details?: unknown; message?: unknown } | string; detail?: unknown } | undefined
  const source = typeof data?.error === 'object' ? (data.error.details ?? data.error.message) : (data?.error ?? data?.detail ?? data)
  const messages = errorText(source)
  if (messages.length) error.message = messages.join('；')
  return error
}

api.interceptors.response.use((response) => response, async (error: AxiosError) => {
  const config = error.config as RetryConfig | undefined
  const isAuthRequest = String(config?.url || '').includes('/auth/')
  if (error.response?.status === 401 && config && !isAuthRequest && !config._cineforgeRetry) {
    const requestAccess = config._cineforgeRequestToken || ''
    if (requestAccess !== (localStorage.getItem('cineforge_token') || '')) {
      return Promise.reject(normalizeApiError(error))
    }
    const refresh = localStorage.getItem('cineforge_refresh_token')
    if (refresh) {
      config._cineforgeRetry = true
      try {
        if (!refreshRequest || refreshRequest.refreshToken !== refresh) {
          const request = api.post('/auth/token/refresh/', { refresh }).then((response) => {
            if (
              requestAccess !== (localStorage.getItem('cineforge_token') || '')
              || refresh !== localStorage.getItem('cineforge_refresh_token')
            ) throw new Error('Authentication session changed while refreshing credentials.')
            const tokens = response.data as { access: string; refresh?: string }
            localStorage.setItem('cineforge_token', tokens.access)
            if (tokens.refresh) localStorage.setItem('cineforge_refresh_token', tokens.refresh)
            return tokens.access
          }).finally(() => {
            if (refreshRequest?.request === request) refreshRequest = null
          })
          refreshRequest = { refreshToken: refresh, request }
        }
        const access = await refreshRequest.request
        if (access !== localStorage.getItem('cineforge_token')) {
          return Promise.reject(normalizeApiError(error))
        }
        config.headers.Authorization = `Bearer ${access}`
        return api.request(config)
      } catch {
        // The refresh response is normalized below after local credentials are cleared.
      }
    }
    if (requestAccess === (localStorage.getItem('cineforge_token') || '')) {
      localStorage.removeItem('cineforge_token')
      localStorage.removeItem('cineforge_refresh_token')
      window.dispatchEvent(new CustomEvent('cineforge:unauthorized'))
    }
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
