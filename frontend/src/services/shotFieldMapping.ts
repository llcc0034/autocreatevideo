import { computed, inject, onBeforeUnmount, provide, reactive, watch, type InjectionKey } from 'vue'
import { dataStructures } from './dataStructures'
import { apiErrorSummary } from './apiErrorPresentation'
import type { JsonObject } from './platformTypes'
import { pathObject as obj, pathType } from './structurePaths'

export interface ShotFieldSchemaState { loading: boolean; error: string; schema?: JsonObject }
export type ShotFieldSchemas = Record<string, ShotFieldSchemaState>
export interface ShotFieldOption { nodeKey: string; definitionId: string; label: string; schema: JsonObject }

// Navigate the server-compiled M1 schema; do not compile a second field dictionary.
export function shotFieldOptions(schema: JsonObject): ShotFieldOption[] {
  const result: ShotFieldOption[] = []
  function walk(value: JsonObject, path: string) {
    const base = Array.isArray(value.anyOf) ? value.anyOf.map(obj).find(row => row.type !== 'null') || {} : value
    const definition = obj(base['x-field-definition'])
    if (typeof base['x-node-key'] === 'string' && typeof definition.field_definition_version_id === 'string') {
      result.push({nodeKey: base['x-node-key'], definitionId: definition.field_definition_version_id,
        label: `${base.title || definition.key} · v${definition.version} · ${path}`, schema: value})
    }
    for (const [key, child] of Object.entries(obj(base.properties))) walk(obj(child), `${path}[${JSON.stringify(key)}]`)
    if (base.items && typeof base.items === 'object') walk(obj(base.items), `${path}[]`)
  }
  walk(schema, '$')
  return result
}

export function shotFieldSelectionIssue(node: JsonObject): string {
  const field = obj(node.field)
  if (Object.keys(field).length !== 3 || !['structure_version_id', 'node_key', 'field_definition_version_id'].every(key => typeof field[key] === 'string' && field[key])) return '请选择确切结构版本及独立字段'
  if (Object.keys(obj(node.scope)).length !== 1 || obj(node.scope).kind !== 'shot') return '独立字段必须使用当前镜头范围'
  return ''
}

export function shotFieldSchema(node: JsonObject, schemas?: ShotFieldSchemas): {schema: JsonObject; error: string} {
  const invalid = shotFieldSelectionIssue(node)
  if (invalid) return {schema: {}, error: invalid}
  const field = obj(node.field), state = schemas?.[String(field.structure_version_id)]
  if (!state || state.loading) return {schema: {}, error: '正在读取独立字段的确切结构'}
  if (state.error) return {schema: {}, error: state.error}
  const choices = state.schema ? shotFieldOptions(state.schema).filter(row => row.nodeKey === field.node_key) : []
  if (choices.length !== 1 || choices[0]!.definitionId !== field.field_definition_version_id) return {schema: {}, error: '已保存的节点或字段版本不在确切结构中；原映射未修改，请重选'}
  return {schema: choices[0]!.schema, error: ''}
}

export function shotFieldNodes(node: JsonObject): JsonObject[] {
  if (node.kind === 'shot_field') return [node]
  const children = node.kind === 'object' ? Object.values(obj(node.fields)) : node.kind === 'array' ? (Array.isArray(node.items) ? node.items : [])
    : node.kind === 'each' ? [node.source, node.item] : node.kind === 'unit_map' ? [node.value] : []
  return children.flatMap(child => shotFieldNodes(obj(child)))
}

type Catalog = { states: ShotFieldSchemas; load: (id: string) => Promise<void> }
const catalogKey: InjectionKey<Catalog> = Symbol('recipe-shot-fields')

// Scoped to one route/editor lifetime, shared by its recursive fields. No values
// or latest-version lookup. Removed/superseded reads cannot restore stale state.
export function useShotFieldCatalog(roots: () => JsonObject[]): Catalog {
  const inherited = inject(catalogKey, null)
  if (inherited) return inherited
  const states = reactive<ShotFieldSchemas>({})
  let active = true
  const ids = computed(() => [...new Set(roots().flatMap(shotFieldNodes).map(node => obj(node.field).structure_version_id)
    .filter((id): id is string => typeof id === 'string' && Boolean(id)))].sort())
  async function load(id: string) {
    if (!active || !ids.value.includes(id)) return
    const state: ShotFieldSchemaState = {loading: true, error: ''}
    states[id] = state
    const current = states[id]
    try {
      const [detail, compiled] = await Promise.all([dataStructures.get(id), dataStructures.schema(id)])
      if (!active || states[id] !== current) return
      if (detail.id !== id || !['published', 'retired'].includes(detail.status)) throw new Error('独立字段须引用已发布的确切结构，不能使用草稿或替代版本')
      if (!compiled.valid || !compiled.json_schema || !pathType(compiled.json_schema)) throw new Error('确切结构无法编译；请在字段维护中检查原版本')
      current.schema = compiled.json_schema
    } catch (cause) {
      if (active && states[id] === current) current.error = apiErrorSummary(cause, '独立字段结构读取失败')
    } finally { if (active && states[id] === current) current.loading = false }
  }
  watch(() => JSON.stringify(ids.value), () => {
    for (const id of Object.keys(states)) if (!ids.value.includes(id)) delete states[id]
    for (const id of ids.value) if (!states[id]) void load(id)
  }, {immediate: true})
  onBeforeUnmount(() => { active = false })
  const catalog = {states, load}
  provide(catalogKey, catalog)
  return catalog
}
