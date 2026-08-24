export interface ApiErrorPresentation {
  code: string
  message: string
  fields: Record<string, string[]>
}

const MAX_MESSAGE_LENGTH = 500
const MAX_FIELD_COUNT = 12
const MAX_FIELD_MESSAGES = 3
const SENSITIVE_KEY = /(?:stack|trace|exception|provider(?:_|-)?(?:response|payload|body|error)|request(?:_|-)?(?:payload|body)|raw|secret|token|password|api(?:_|-)?key|authorization)/i
const RESERVED_KEY = new Set(['code', 'status', 'status_code', 'message', 'detail', 'error', 'details'])

const record = (value: unknown): Record<string, unknown> | null => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
)

const cleanText = (value: unknown, limit = MAX_MESSAGE_LENGTH): string => {
  if (typeof value !== 'string') return ''
  const normalized = value.replace(/\s+/g, ' ').trim()
  if (!normalized) return ''
  if (/(?:<!doctype\s+html\b|<html\b|<body\b|<script\b)/i.test(normalized)) return ''
  if (/(?:traceback \(most recent call last\)|\bat\s+\S+\s+\([^)]*:\d+:\d+\)|\b(?:stack|exception)\s*trace\b)/i.test(normalized)) return ''
  return normalized.length <= limit ? normalized : `${normalized.slice(0, Math.max(0, limit - 1))}…`
}

const codeText = (value: unknown): string => {
  if (typeof value !== 'string') return ''
  const code = value.trim()
  return /^[a-zA-Z][a-zA-Z0-9_.-]{0,95}$/.test(code) ? code : ''
}

const stringMessages = (value: unknown): string[] => {
  if (typeof value === 'string') return cleanText(value, 240) ? [cleanText(value, 240)] : []
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => typeof item === 'string' ? [cleanText(item, 240)] : [])
    .filter(Boolean)
    .slice(0, MAX_FIELD_MESSAGES)
}

const fieldMessages = (...values: unknown[]): Record<string, string[]> => {
  const fields: Record<string, string[]> = {}
  for (const value of values) {
    const row = record(value)
    if (!row) continue
    for (const [key, item] of Object.entries(row)) {
      if (Object.keys(fields).length >= MAX_FIELD_COUNT) return fields
      if (RESERVED_KEY.has(key) || SENSITIVE_KEY.test(key)) continue
      const messages = stringMessages(item)
      if (messages.length) fields[key] = messages
    }
  }
  return fields
}

export const apiErrorPresentation = (
  cause: unknown,
  fallback = '请求未完成，请重试；若持续出现请联系管理员。',
): ApiErrorPresentation => {
  const root = record(cause)
  const response = record(root?.response)
  const data = record(response?.data) || record(root?.data)
  const nestedError = record(data?.error)
  const details = record(nestedError?.details) || record(data?.details)
  const status = Number(response?.status || root?.status || 0)
  const headers = record(response?.headers)
  const contentType = cleanText(headers?.['content-type'], 100).toLowerCase()
  const htmlResponse = contentType.includes('text/html')
    || (typeof response?.data === 'string' && !cleanText(response.data))

  const code = codeText(nestedError?.code) || codeText(data?.code) || codeText(details?.code)
  const fields = fieldMessages(data, data?.fields, nestedError?.fields, details)
  const explicitMessage = cleanText(nestedError?.message)
    || cleanText(data?.message)
    || cleanText(data?.detail)
    || cleanText(details?.message)
    || cleanText(details?.detail)
  const localMessage = response ? '' : cleanText(root?.message)
  const message = htmlResponse
    ? (status >= 500
      ? '服务端发生内部错误，请重试；若持续出现请查看服务日志。'
      : `请求失败（HTTP ${status || '未知'}）`)
    : explicitMessage || localMessage || fallback

  return { code, message, fields }
}

export const apiErrorSummary = (cause: unknown, fallback?: string): string => {
  const presentation = apiErrorPresentation(cause, fallback)
  const fieldSummary = Object.entries(presentation.fields)
    .map(([field, messages]) => `${field}: ${messages.join('；')}`)
    .join('；')
  return [presentation.code ? `[${presentation.code}]` : '', presentation.message, fieldSummary]
    .filter(Boolean)
    .join(' ')
}

export const safePresentationText = (value: unknown, fallback = ''): string => cleanText(value) || fallback
