import { api, unwrap } from './api'
import type { FormCatalogOption, JsonObject } from './platformTypes'

export interface StructureMappingOptions {
  schema_version: number; nodes: FormCatalogOption[]; scopes: FormCatalogOption[]
}
export interface StructureMappingPreview {
  preview_only: true; valid: boolean; parsed: boolean
  source_structure_version_id: string; target_structure_version_id: string
  raw_value: unknown; source_value: unknown; value: unknown
  defaults: StructureValuePreview['defaults']; provenance: JsonObject[]; warnings: JsonObject[]
  errors: Record<string, string[]>
}

export interface StructureValuePreview {
  preview_only: true
  structure_version_id: string
  valid: boolean
  value: unknown
  errors: Record<string, string[]>
  defaults: { path: string; node_key: string; structure_version_id: string; field_definition_version_id: string; source: 'configured_default'; value: unknown }[]
}

export interface DataStructureSummary {
  id: string
  structure_key: string
  version: number
  name: string
  status: 'draft' | 'published' | 'retired'
}

export interface StructureNode {
  node_key: string; parent_key: string | null; field_definition_version: string | null
  property_name: string; position: number; required: boolean; allow_null: boolean; allow_blank: boolean; allow_empty: boolean
  has_default: boolean; default_value: unknown; constraints: JsonObject
}
export interface DataStructureContent { name: string; description: string; nodes: StructureNode[] }
export interface DataStructureDetail extends DataStructureSummary, DataStructureContent {}
export interface StructureAuthoringOptions {
  node_defaults: Omit<StructureNode, 'node_key'>
  statuses: { value: string; label: string }[]
  constraint_schemas: Record<string, JsonObject>; value_schemas: Record<string, JsonObject>
}

export const dataStructures = {
  async list(page: number, search: string, status = 'published', structureKey?: string) {
    const response = (await api.get('/data-structure-versions/', {
      params: { page, page_size: 20, ...(status ? {status} : {}), search, ...(structureKey ? {structure_key: structureKey} : {}) },
    })).data
    const result = (response.data || response) as { count: number; results: DataStructureSummary[]; next: string|null; previous: string|null }
    if (!Number.isSafeInteger(result.count) || result.count < 0 || !Array.isArray(result.results) || ![result.next,result.previous].every(link => link === null || typeof link === 'string' && link.length > 0)) throw new Error('结构版本目录或分页响应不完整')
    return {count:result.count, results:result.results, hasNext:result.next!==null, hasPrevious:result.previous!==null}
  },
  async get(id: string) {
    return unwrap<DataStructureDetail>((await api.get(`/data-structure-versions/${id}/`)).data)
  },
  async options() { return unwrap<StructureAuthoringOptions>((await api.get('/data-structure-versions/authoring-options/')).data) },
  async mappingOptions() { return unwrap<StructureMappingOptions>((await api.get('/data-structure-versions/mapping-options/')).data) },
  async mappingPreview(target: string, source: string, sourceText: string, mapping: JsonObject) {
    return unwrap<StructureMappingPreview>((await api.post(`/data-structure-versions/${target}/mapping-preview/`, {
      source_structure_version: source, source_text: sourceText, mapping,
    })).data)
  },
  async create(content: Partial<DataStructureContent>, source?: string) {
    return unwrap<DataStructureDetail>((await api.post('/data-structure-versions/', {...content, ...(source ? {source_version: source} : {})})).data)
  },
  async update(id: string, content: Partial<DataStructureContent>) {
    return unwrap<DataStructureDetail>((await api.patch(`/data-structure-versions/${id}/`, content)).data)
  },
  async transition(id: string, action: 'publish' | 'retire') {
    return unwrap<DataStructureDetail>((await api.post(`/data-structure-versions/${id}/${action}/`, {})).data)
  },
  async schema(id: string) {
    return unwrap<{valid:boolean;json_schema:JsonObject|null;errors:Record<string,string[]>}>((await api.get(`/data-structure-versions/${id}/schema-preview/`)).data)
  },
  async preview(id: string, value: unknown) {
    return unwrap<StructureValuePreview>((await api.post(`/data-structure-versions/${id}/value-preview/`, {value})).data)
  },
}
