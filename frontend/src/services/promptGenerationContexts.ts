import type { JsonObject } from './platformTypes'
import { pathLabel, pathSchema, type StructurePath } from './structurePaths'

export const structuredContextPrefix = 'request.structured_input:'
export const structuredContextSelector = (path: StructurePath) => structuredContextPrefix + JSON.stringify(path)
export function structuredContextPath(selector: string): StructurePath | null {
  if (!selector.startsWith(structuredContextPrefix)) return null
  try {
    const path: unknown = JSON.parse(selector.slice(structuredContextPrefix.length))
    if (Array.isArray(path) && path.every(token => typeof token === 'string' || typeof token === 'number' && Number.isSafeInteger(token) && token >= 0)
      && structuredContextSelector(path) === selector) return path
  } catch { /* invalid saved paths remain visible and cannot be saved */ }
  return null
}
export function structuredContextLabel(selector: string): string {
  const path = structuredContextPath(selector)
  return path === null ? selector : `输入字段 ${pathLabel(path)}`
}
export function contextSelectorError(selector: string, known: Set<string>, schema: JsonObject | null, modelGeneration: boolean): string {
  if (known.has(selector)) return ''
  const path = structuredContextPath(selector)
  if (path === null) return '生成上下文包含未登记或格式错误的来源'
  if (!modelGeneration) return '结构字段参考须选择生成或翻译'
  if (!schema) return '请先读取绑定的输入结构，再选择附加字段'
  return pathSchema(schema, path).error
}
