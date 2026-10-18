import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from './api'
import { apiErrorSummary } from './apiErrorPresentation'
import { canonicalSettingsResourceUrl } from './executionPath'

export const relationKinds = { field: '字段身份', field_version: '字段定义版本', structure: '输入／输出结构', source: '内容来源版本', execution: '执行实例版本', model: '模型版本', deployment: '模型部署', workflow: '工作流', prompt: '提示词策略', output: '业务输出版本', projector: 'Projector', recipe: '生产配方' } as const
export type RelationKind = keyof typeof relationKinds
export interface RelationGroup { key: string; label: string; kind: RelationKind; count: number; url: string }
export interface RelationRow { id: string; kind: RelationKind; label: string; status: string; config_url: string; overview_url: string; acceptance_url: string; groups: RelationGroup[]; notices: string[]; capabilities: string[] }
export interface RelationPage { count: number; next: string | null; previous: string | null; generated_at: string; context_label: string; results: RelationRow[] }

const record = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('关联目录返回了无效结构')
  return value as Record<string, unknown>
}
const string = (value: unknown): string => { if (typeof value !== 'string') throw new Error('关联目录缺少字段'); return value }
const count = (value: unknown): number => { if (!Number.isSafeInteger(value) || Number(value) < 0) throw new Error('关联数量无效'); return Number(value) }
const kind = (value: unknown): RelationKind => { const key = string(value); if (!Object.prototype.hasOwnProperty.call(relationKinds, key)) throw new Error('未知关联对象'); return key as RelationKind }
const localUrl = (value: unknown, optional = false) => {
  const url = string(value)
  if (optional && !url) return ''
  if (!/^\/settings(?:[/?]|$)/.test(url) || /[\\\r\n]/.test(url)) throw new Error('关联导航地址无效')
  return url
}
export function adaptRelationPage(value: unknown): RelationPage {
  const outer = record(value)
  const page = outer.data === undefined ? outer : record(outer.data)
  if (!Array.isArray(page.results) || page.results.length > 50) throw new Error('关联分页结果无效')
  return {
    count: count(page.count), next: page.next === null ? null : string(page.next), previous: page.previous === null ? null : string(page.previous),
    generated_at: string(page.generated_at), context_label: string(page.context_label),
    results: page.results.map((value) => {
      const row = record(value)
      if (!Array.isArray(row.groups) || !Array.isArray(row.notices) || !Array.isArray(row.capabilities)) throw new Error('关联摘要不完整')
      return { id: string(row.id), kind: kind(row.kind), label: string(row.label), status: string(row.status),
        config_url: localUrl(row.config_url), overview_url: localUrl(row.overview_url), acceptance_url: localUrl(row.acceptance_url, true),
        capabilities: row.capabilities.map(string), notices: row.notices.map(string), groups: row.groups.map((value) => {
          const group = record(value)
          return { key: string(group.key), label: string(group.label), kind: kind(group.kind), count: count(group.count), url: localUrl(group.url) }
        }) }
    }),
  }
}
export const relationOverviewUrl = (kind: RelationKind, id?: string) => '/settings/acceptance-smoke?' + new URLSearchParams({ view: 'relations', kind, ...(id ? { ids: id } : {}) })
export const relationConfigUrl = (row: RelationRow) => {
  const url = new URL(canonicalSettingsResourceUrl(row.config_url), 'https://navigation.invalid')
  url.searchParams.set('relation_context', `${row.kind}:${row.id}`)
  return url.pathname + url.search + url.hash
}
export async function getRelationPage(params: Record<string, string | number>, signal?: AbortSignal) {
  const response = await api.get('/registry-relations/', { params, signal })
  return adaptRelationPage(response.data)
}

/** Batch visible rows, cancel obsolete navigation; never show stale rows as current. */
export function useRegistryRelations(kind: RelationKind, items: () => { id: string; updatedAt?: string }[]) {
  const rows = ref<Record<string, RelationRow>>({})
  const error = ref(''); const loading = ref(false)
  const key = computed(() => JSON.stringify(items().map(item => [item.id, item.updatedAt || ''])))
  let serial = 0; let controller: AbortController | undefined
  const reload = async () => {
    const current = ++serial
    controller?.abort(); controller = new AbortController()
    const signal = controller.signal
    rows.value = {}; error.value = ''
    const ids = [...new Set(items().map(item => item.id))]
    loading.value = ids.length > 0
    if (!ids.length) return
    try {
      const result: Record<string, RelationRow> = {}
      // Two bounded requests at a time, not one request per row.
      for (let start = 0; start < ids.length; start += 100) {
        const chunks = [ids.slice(start, start + 50), ids.slice(start + 50, start + 100)].filter(chunk => chunk.length)
        const pages = await Promise.all(chunks.map(async chunk => {
          const found: RelationRow[] = []; const seen = new Set<string>(); let total: number | undefined
          // Security configuration may reduce the requested page size below 50.
          for (let number = 1; number <= chunk.length; number++) {
            const page = await getRelationPage({ kind, ids: chunk.join(','), page_size: 50, page: number }, signal)
            if (total !== undefined && total !== page.count) throw new Error('关联目录在分页期间发生变化，请重新读取')
            total = page.count
            for (const row of page.results) {
              if (row.kind !== kind || !chunk.includes(row.id) || seen.has(row.id)) throw new Error('关联分页包含重复或错误对象')
              seen.add(row.id); found.push(row)
            }
            if (!page.next) {
              if (found.length !== total) throw new Error('关联分页结果不完整，请重新读取')
              return found
            }
            if (!page.results.length) throw new Error('关联分页未取得进展')
          }
          throw new Error('关联分页超过预期范围')
        }))
        if (current !== serial) return
        for (const page of pages) for (const row of page) result[row.id] = row
      }
      if (ids.some(id => !result[id])) throw new Error('部分关联对象已删除或不存在，请重新读取配置目录')
      rows.value = result
    } catch (cause) {
      if (current === serial) error.value = apiErrorSummary(cause, '关联信息读取失败')
    } finally { if (current === serial) loading.value = false }
  }
  watch(key, () => { void reload() }, { immediate: true })
  onBeforeUnmount(() => { serial++; controller?.abort() })
  return { rows, error, loading, reload }
}
