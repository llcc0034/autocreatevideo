import { api, unwrap } from './api'
import type { JsonObject } from './platformTypes'

export interface ContentSourceSummary {
  id: string; source_key: string; version: number; name: string
  reader_selector: string; status: 'draft' | 'published' | 'retired'
  revision: number; data_structure_version: string | null
}
export interface ContentSourceVersion extends ContentSourceSummary {
  description: string; reader: JsonObject; contract_sha256: string; contract_size_bytes: number
}
export interface SourceReaderOption {
  value: string; label: string; group_label: string; description: string
  record_location: string; execution_usage: string; reader: JsonObject
}
export const contentSources = {
  async list(page: number, status: string, search: string, sourceKey?: string) {
    const response = (await api.get('/content-source-versions/', { params: { page, page_size: 20, status: status || undefined, search, source_key: sourceKey || undefined } })).data
    const value = (response.data || response) as { count: number; next?: string | null; results: ContentSourceSummary[] }
    if (!Array.isArray(value.results) || typeof value.count !== 'number') throw new Error('来源版本列表响应不完整')
    return value
  },
  async get(id: string) { return unwrap<ContentSourceVersion>((await api.get(`/content-source-versions/${id}/`)).data) },
  async readers() { return unwrap<SourceReaderOption[]>((await api.get('/content-source-versions/readers/')).data) },
  async save(data: { name: string; description: string; reader_selector: string; data_structure_version: string }, source: ContentSourceVersion | null) {
    const changes = source ? Object.fromEntries(Object.entries(data).filter(([key,value]) => value !== (source[key as keyof ContentSourceVersion] ?? ''))) : data
    return unwrap<ContentSourceVersion>((source
      ? await api.patch(`/content-source-versions/${source.id}/`, { ...changes, expected_revision: source.revision })
      : await api.post('/content-source-versions/', data)).data)
  },
  async action(source: ContentSourceVersion, action: 'publish' | 'successor' | 'retire') {
    return unwrap<ContentSourceVersion>((await api.post(`/content-source-versions/${source.id}/${action}/`, { expected_revision: source.revision })).data)
  },
}
