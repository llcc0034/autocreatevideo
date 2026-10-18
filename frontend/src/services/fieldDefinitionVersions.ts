import { api, fetchAllPages, unwrap } from './api'

export interface FieldDefinitionVersionSummary {
  id: string; field_definition: string; field_key: string; version: number
  status: 'draft' | 'published' | 'retired'; label: string; category: string
  value_type: '' | 'text' | 'long_text' | 'integer' | 'decimal' | 'boolean' | 'enum' | 'object' | 'list' | 'media'
  unit: string
}
export interface FieldDefinitionVersion extends FieldDefinitionVersionSummary {
  description: string; impact: string; value_options: string[]; preview_example: unknown
}
export interface FieldDefinitionContent {
  label: string; category: string; value_type: FieldDefinitionVersionSummary['value_type'] | ''
  unit: string; description: string; impact: string; value_options: string[]
}
export const fieldDefinitionVersions = {
  async options() {
    return unwrap<{ value_types: { value: FieldDefinitionVersionSummary['value_type']; label: string }[]; statuses: { value: string; label: string }[] }>((await api.get('/field-definition-versions/options/')).data)
  },
  async create(fieldDefinition: string, content: FieldDefinitionContent, sourceVersion?: string) {
    return unwrap<FieldDefinitionVersion>((await api.post('/field-definition-versions/', { field_definition: fieldDefinition, ...content, ...(sourceVersion ? { source_version: sourceVersion } : {}) })).data)
  },
  async update(id: string, content: Partial<FieldDefinitionContent>) {
    return unwrap<FieldDefinitionVersion>((await api.patch(`/field-definition-versions/${id}/`, content)).data)
  },
  async transition(id: string, action: 'publish' | 'retire') {
    return unwrap<FieldDefinitionVersion>((await api.post(`/field-definition-versions/${id}/${action}/`, {})).data)
  },
  list(fieldDefinition: string) {
    return fetchAllPages<FieldDefinitionVersionSummary>('/field-definition-versions/', { field_definition: fieldDefinition, page_size: 50 })
  },
  async catalog(page: number, search: string) {
    return unwrap<{count: number; results: FieldDefinitionVersionSummary[]}>((await api.get('/field-definition-versions/', {params: {page, page_size: 20, status: 'published', search}})).data)
  },
  async get(id: string) {
    return unwrap<FieldDefinitionVersion>((await api.get(`/field-definition-versions/${id}/`)).data)
  },
}
