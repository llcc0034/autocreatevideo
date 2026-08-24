import type { ConfigDefinition, JsonObject } from './platformTypes'
import { parseConfigValue } from './configValueSchema'

export interface SystemConfigDraftInput {
  definitions: ConfigDefinition[]
  draftText: Record<string, string>
  dirtyKeys: Iterable<string>
  unsetKeys: Iterable<string>
  secretSelections: Record<string, string>
  isSuperuser: boolean
}

export const buildSystemConfigChangeSet = (input: SystemConfigDraftInput): {
  values: JsonObject
  unsetKeys: string[]
} => {
  const definitions = new Map(input.definitions.map((item) => [item.key, item]))
  const unsetKeys = [...new Set(input.unsetKeys)].filter((key) => definitions.has(key)).sort()
  const unset = new Set(unsetKeys)
  const values: JsonObject = {}
  for (const key of new Set(input.dirtyKeys)) {
    const definition = definitions.get(key)
    if (!definition || !definition.isActive || unset.has(key)) continue
    if (definition.isSecret) {
      const credentialId = input.secretSelections[key]
      if (input.isSuperuser && credentialId) values[key] = { credential_id: credentialId }
      continue
    }
    const parsed = parseConfigValue(definition, input.draftText[key] ?? '')
    if (parsed.error) throw new Error(parsed.error)
    values[key] = parsed.value as JsonObject[string]
  }
  return { values, unsetKeys }
}

export const effectiveSourceLabel = (source: {
  source?: string
  scope?: string
  revision?: number
} | undefined): string => {
  if (!source) return '未解析'
  const origin = source.source || source.scope || 'unknown'
  return source.revision ? `${origin} · revision ${source.revision}` : origin
}
