import type { FormCatalogOption, JsonObject } from './platformTypes'
import { initializeMapping, mappingHasDefault, mappingObject as obj } from './recipeStructureMapping'
import { jsonChildSchemas, jsonPropertyOptions, jsonRules } from './structuredInputForm'
import { pathSchema } from './structurePaths'

export interface MappingPreviewContext { schema: JsonObject; scopes: FormCatalogOption[] }

// Schema navigation only; mapping evaluation always stays in the shared backend service.
export function previewMappingSchema(node: JsonObject, root: JsonObject, item?: JsonObject): JsonObject {
  if (node.kind === 'path') {
    const origin = node.scope === 'root' ? root : node.scope === 'item' ? item : undefined
    if (!origin) return {}
    const result = pathSchema(origin, node.path)
    return result.error ? {} : result.schema
  }
  if (node.kind === 'lookup') {
    const collection = previewMappingSchema(obj(node.collection), root, item)
    const row = jsonChildSchemas([collection], '0', true)[0]
    if (!row) return {}
    const result = pathSchema(row, node.value_path)
    return result.error ? {} : result.schema
  }
  return {}
}

export function initializePreviewMapping(kind: string, schema: JsonObject, defaults: JsonObject[]): JsonObject {
  if (kind === 'path') return {kind, scope: 'root'}
  if (kind === 'lookup') return {kind, collection: {}, key: {}}
  if (kind === 'object') {
    const required = new Set(jsonRules([schema], 'object').flatMap(rule => Array.isArray(rule.required) ? rule.required : []))
    // Optional semantic fields are opt-in; an empty mapping would incorrectly require a source.
    const keys = jsonPropertyOptions([schema]).filter(key => required.has(key) && !mappingHasDefault(defaults, key))
    return {kind, fields: Object.fromEntries(keys.map(key => [key, {}]))}
  }
  return initializeMapping(kind, schema, defaults)
}
