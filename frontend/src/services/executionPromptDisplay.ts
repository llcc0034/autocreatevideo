import { api, unwrap } from './api'

export interface ExecutionPromptDisplay {
  preparationId: string; preparationRevision: number; mode: 'preview' | 'frozen' | 'unavailable' | 'diagnostic'
  promptMode: 'profile' | 'manual' | 'none'; contentKind: 'channels' | 'messages' | 'not_applicable' | 'unavailable'
  requestSha256: string | null; frozenAt: string | null
  profileBasis: { versionId: string; version: number; contractSha256: string } | null
  channels: { key: string; text: string; characterCount: number }[]
  messages: { role: 'system' | 'user' | 'assistant'; content: string; characterCount: number }[]
  lengthDiagnostics: { channel: string; characterCount: number; maxChars: number | null; overBy: number
    limitSources: { source: string; maxChars: number | null }[]
    fragments: { fieldKey: string; label: string; fieldDefinitionVersionId: string; sourceSelector: string; maxChars: number; textChars: number; separatorChars: number }[] }[]
  fieldBasis: { unitKey: string; fieldKey: string; fieldValueId: string; revision: number; origin: 'adopted' | 'candidate'; status: string }[]
  sources: { references: { label: string; name: string; category: string; guidance: string }[]
    dialogue: { unitKey: string; subject: string; text: string; language?: string; startMs: number; endMs: number; voiceover: boolean; delivery: string }[] }
  issues: string[]
}
const invalid = (): never => { throw new Error('完整提示词响应不完整或与当前准备不一致，请重新读取') }
const record = (v: unknown): Record<string, unknown> => v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : invalid()
const text = (v: unknown): string => typeof v === 'string' ? v : invalid()
const integer = (v: unknown, min = 1): number => typeof v === 'number' && Number.isSafeInteger(v) && v >= min ? v : invalid()
const list = (v: unknown): unknown[] => Array.isArray(v) ? v : invalid()
const uuid = (v: string): boolean => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v)
export function adaptExecutionPromptDisplay(raw: unknown, id: string, revision: number): ExecutionPromptDisplay {
  const r = record(raw), sources = record(r.sources)
  if (r.preparation_id !== id || r.preparation_revision !== revision || !['preview', 'frozen', 'unavailable', 'diagnostic'].includes(text(r.mode))) invalid()
  const mode = r.mode as ExecutionPromptDisplay['mode']
  if (!['profile', 'manual', 'none'].includes(text(r.prompt_mode))
    || !['channels', 'messages', 'not_applicable', 'unavailable'].includes(text(r.content_kind))) invalid()
  const promptMode = r.prompt_mode as ExecutionPromptDisplay['promptMode'], contentKind = r.content_kind as ExecutionPromptDisplay['contentKind']
  const basis = r.profile_basis === null ? null : record(r.profile_basis)
  const profileBasis = basis === null ? null : { versionId: text(basis.version_id), version: integer(basis.version), contractSha256: text(basis.contract_sha256) }
  if (profileBasis && (!uuid(profileBasis.versionId) || !/^[a-f0-9]{64}$/.test(profileBasis.contractSha256))) invalid()
  if (mode === 'unavailable' ? profileBasis !== null : ['preview', 'diagnostic'].includes(mode) && profileBasis === null) invalid()
  if (mode !== 'unavailable' && (promptMode === 'profile') !== (profileBasis !== null)) invalid()
  const channels = list(r.channels).map(v => {
    const row = record(v), content = text(row.text), characterCount = integer(row.character_count, 0)
    if (characterCount !== Array.from(content).length) invalid()
    return { key: text(row.key), text: content, characterCount }
  })
  if (channels.some(row => !row.key) || new Set(channels.map(row => row.key)).size !== channels.length) invalid()
  const messages = list(r.messages).map(v => {
    const row = record(v), role = text(row.role), content = text(row.content), characterCount = integer(row.character_count, 0)
    if (!['system', 'user', 'assistant'].includes(role) || characterCount !== Array.from(content).length) invalid()
    return { role: role as ExecutionPromptDisplay['messages'][number]['role'], content, characterCount }
  })
  const issues = list(r.issues).map(text)
  if (mode === 'frozen') {
    if (!/^[a-f0-9]{64}$/.test(text(r.request_sha256)) || !Number.isFinite(Date.parse(text(r.frozen_at)))) invalid()
  } else if (r.request_sha256 !== null || r.frozen_at !== null) invalid()
  if (mode === 'unavailable') {
    if (contentKind !== 'unavailable' || channels.length || messages.length || !issues.length) invalid()
  } else {
    if (mode === 'diagnostic' ? !issues.length : issues.length) invalid()
    if (promptMode === 'none') {
      if (mode !== 'frozen' || channels.length || !['messages', 'not_applicable'].includes(contentKind)) invalid()
      if (contentKind === 'messages' ? !messages.some(row => row.role === 'user' && row.content.trim()) : messages.length !== 0) invalid()
    } else if (contentKind !== 'channels' || !channels.length || messages.length || (mode !== 'frozen' && promptMode !== 'profile')) invalid()
  }
  const lengthDiagnostics = list(r.length_diagnostics).map(v => {
    const row = record(v), channel = text(row.channel), characterCount = integer(row.character_count, 0)
    const maxChars = row.max_chars === null ? null : integer(row.max_chars), overBy = integer(row.over_by, 0)
    const fragments = list(row.fragments).map(v => {
      const f = record(v)
      const result = { fieldKey: text(f.field_key), label: text(f.label), fieldDefinitionVersionId: text(f.field_definition_version_id), sourceSelector: text(f.source_selector), maxChars: integer(f.max_chars), textChars: integer(f.text_chars, 0), separatorChars: integer(f.separator_chars, 0) }
      if (!result.fieldKey || !result.sourceSelector || !uuid(result.fieldDefinitionVersionId) || result.textChars > result.maxChars) invalid()
      return result
    })
    const limitSources = list(row.limit_sources).map(v => { const item = record(v); const source = text(item.source)
      if (!source) invalid()
      return { source, maxChars: item.max_chars === null ? null : integer(item.max_chars) }
    })
    if (new Set(limitSources.map(item => item.source)).size !== limitSources.length) invalid()
    const maxima = limitSources.flatMap(item => item.maxChars === null ? [] : [item.maxChars])
    if (limitSources.length && maxChars !== (maxima.length ? Math.min(...maxima) : null)) invalid()
    if (channels.find(c => c.key === channel)?.characterCount !== characterCount
      || overBy !== (maxChars === null ? 0 : Math.max(0, characterCount - maxChars))
      || fragments.reduce((n, f) => n + f.textChars + f.separatorChars, 0) !== characterCount) invalid()
    return { channel, characterCount, maxChars, overBy, fragments, limitSources }
  })
  if (profileBasis === null ? lengthDiagnostics.length !== 0 : lengthDiagnostics.length !== channels.length) invalid()
  if (mode === 'diagnostic' ? !lengthDiagnostics.some(d => d.overBy > 0) : lengthDiagnostics.some(d => d.overBy > 0)) invalid()
  if (new Set(lengthDiagnostics.map(d => d.channel)).size !== lengthDiagnostics.length) invalid()
  const fieldBasis = list(r.field_basis).map(v => {
    const row = record(v), origin = text(row.origin), status = text(row.status)
    if (!['adopted', 'candidate'].includes(origin) || !['pending', 'confirmed'].includes(status)
      || (origin === 'adopted' && status !== 'confirmed') || (mode === 'frozen' && origin !== 'adopted')) invalid()
    return { unitKey: text(row.unit_key), fieldKey: text(row.field_key), fieldValueId: text(row.field_value_id),
      revision: integer(row.revision), origin: origin as 'adopted' | 'candidate', status }
  })
  if (new Set(fieldBasis.map(row => JSON.stringify([row.unitKey, row.fieldKey]))).size !== fieldBasis.length) invalid()
  if (promptMode === 'none' && fieldBasis.length) invalid()
  return { preparationId: id, preparationRevision: integer(r.preparation_revision), mode,
    promptMode, contentKind, messages,
    requestSha256: r.request_sha256 as string | null, frozenAt: r.frozen_at as string | null, profileBasis, channels, lengthDiagnostics, fieldBasis, issues,
    sources: {
      references: list(sources.references).map(v => { const row = record(v); return { label: text(row.label), name: text(row.name), category: text(row.category), guidance: text(row.guidance) } }),
      dialogue: list(sources.dialogue).map(v => { const row = record(v)
        if (typeof row.voiceover !== 'boolean') invalid()
        const startMs = integer(row.start_ms, 0), endMs = integer(row.end_ms, 0)
        if (endMs < startMs) invalid()
        return { unitKey: text(row.unit_key), subject: text(row.subject), text: text(row.text), startMs, endMs,
          voiceover: row.voiceover as boolean, delivery: text(row.delivery), ...('language' in row ? { language: text(row.language) } : {}) }
      }),
    } }
}
export const executionPromptApi = {
  async get(id: string, revision: number) {
    const raw = (await api.get(`/execution-preparations/${encodeURIComponent(id)}/prompt-display/`,
      { params: { expected_preparation_revision: revision } })).data
    return adaptExecutionPromptDisplay(unwrap(raw), id, revision)
  },
}
