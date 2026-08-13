import axios, { type AxiosError } from 'axios'

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api/v1',
  timeout: 12000,
  headers: { 'Content-Type': 'application/json' }
})

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('cineforge_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

type RetryConfig = NonNullable<AxiosError['config']> & { _cineforgeRetry?: boolean }
let refreshRequest: Promise<string> | null = null

const errorText = (value: unknown): string[] => {
  if (typeof value === 'string') return [value]
  if (Array.isArray(value)) return value.flatMap(errorText)
  if (value && typeof value === 'object') return Object.entries(value as Record<string, unknown>).flatMap(([key, item]) => {
    if (key === 'code' || key === 'status') return []
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
    const refresh = localStorage.getItem('cineforge_refresh_token')
    if (refresh) {
      config._cineforgeRetry = true
      try {
        if (!refreshRequest) {
          refreshRequest = api.post('/auth/token/refresh/', { refresh }).then((response) => {
            const tokens = response.data as { access: string; refresh?: string }
            localStorage.setItem('cineforge_token', tokens.access)
            if (tokens.refresh) localStorage.setItem('cineforge_refresh_token', tokens.refresh)
            return tokens.access
          }).finally(() => { refreshRequest = null })
        }
        const access = await refreshRequest
        config.headers.Authorization = `Bearer ${access}`
        return api.request(config)
      } catch {
        // The refresh response is normalized below after local credentials are cleared.
      }
    }
    localStorage.removeItem('cineforge_token')
    localStorage.removeItem('cineforge_refresh_token')
    window.dispatchEvent(new CustomEvent('cineforge:unauthorized'))
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
