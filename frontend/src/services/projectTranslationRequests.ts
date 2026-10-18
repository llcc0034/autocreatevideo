import { api, unwrap } from './api'
import { adaptAssetGenerationOptions } from './assetGenerationAdapters'
import { buildExecutionRequestFixture, type ExecutionRequestDraft, type ExecutionRequestFormContract } from './executionRequestForm'
import type { PromptTranslationEntry, PromptTranslationField } from '@/types'

const fail = (name: string): never => { throw new Error(`翻译任务响应不可核验：${name}`) }
const record = (v: unknown): Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : fail('对象')
const text = (r: Record<string, unknown>, key: string): string => typeof r[key] === 'string' ? r[key] as string : fail(key)
const id = (r: Record<string, unknown>, key: string): string => /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i.test(text(r,key)) ? text(r,key) : fail(key)
const integer = (r: Record<string, unknown>, key: string, min = 0): number => Number.isSafeInteger(r[key]) && (r[key] as number) >= min ? r[key] as number : fail(key)
const timestamp = (r: Record<string, unknown>, key: string): string => Number.isFinite(Date.parse(text(r,key))) ? text(r,key) : fail(key)
const nullable = (r: Record<string, unknown>, key: string, read = text) => r[key] === null ? null : read(r,key)
export interface TranslationRequestEntry {
  scope_type: string; scope_id: string; field_key: string; source_text: string; expected_entry_id: string | null; expected_revision: number
}
export function translationRequestEntries(fields: PromptTranslationField[], current: PromptTranslationEntry[]): TranslationRequestEntry[] {
  const identity = (scope: unknown, scopeId: unknown, field: unknown) => {
    if (![scope,scopeId,field].every(value => typeof value === 'string' && value)) throw new Error('字段身份不完整，请重新读取')
    return JSON.stringify([scope,scopeId,field])
  }
  const seen = new Set<string>()
  if (!fields.length) throw new Error('请选择已保存的项目字段')
  return fields.map(field => {
    const key = identity(field.scopeType,field.scopeId,field.fieldKey)
    if (seen.has(key) || !field.sourceText.trim()) throw new Error('所选字段重复或没有原文')
    seen.add(key)
    const candidates = current.filter(row => identity(row.scopeType,row.scopeId,row.fieldKey) === key)
    if (candidates.length > 1) throw new Error('当前候选身份重复，请重新读取')
    const row = candidates[0]
    return {scope_type:field.scopeType,scope_id:id({id:field.scopeId},'id'),field_key:field.fieldKey,
      source_text:field.sourceText,expected_entry_id:row ? id({id:row.id},'id') : null,
      expected_revision:row ? integer({revision:row.revision},'revision',1) : 0}
  })
}
export function translationManagedInput(contract: ExecutionRequestFormContract): string {
  const items = contract.inputs.filter(row => row.targets.some(target => target.target_kind === 'operation_input' && target.target_key === 'items'))
  if (contract.operation !== 'prompt.normalize' || items.length !== 1 || items[0]!.kind !== 'json') throw new Error('该实例没有唯一的独立翻译条目输入')
  return items[0]!.key
}
export function translationFixture(contract: ExecutionRequestFormContract, draft: ExecutionRequestDraft) {
  return buildExecutionRequestFixture(contract,draft,{managedInputKeys:[translationManagedInput(contract)]})
}
export function readTranslationRequest(value: unknown) {
  const r = record(value), status = text(r,'status')
  if (!['preparing','queued','submitting','submitted','running','cancel_requested','succeeded','failed','cancelled','uncertain'].includes(status)) fail('status')
  return {id:id(r,'id'),jobId:id(r,'job_id'),preparationId:id(r,'preparation_id'),versionId:id(r,'execution_instance_version_id'),
    status,createdAt:timestamp(r,'created_at'),sourceCount:integer(r,'source_count',1)}
}
export type TranslationRequest = ReturnType<typeof readTranslationRequest>
export function readTranslationSource(value: unknown) {
  const r = record(value), adoptedEntryId = nullable(r,'adopted_entry_id',id), adoptedAt = nullable(r,'adopted_at',timestamp)
  const adoptedRevision = r.adopted_revision === null ? null : integer(r,'adopted_revision',1)
  if ((adoptedEntryId === null) !== (adoptedAt === null) || (adoptedAt === null) !== (adoptedRevision === null)) fail('采用回执')
  return {id:id(r,'id'),position:integer(r,'position'),scopeType:text(r,'scope_type'),scopeId:id(r,'scope_id'),fieldKey:text(r,'field_key'),
    expectedRevision:integer(r,'expected_revision'),adoptedEntryId,adoptedRevision,adoptedAt}
}
export type TranslationSource = ReturnType<typeof readTranslationSource>
export function readTranslationCandidate(value: unknown) {
  const r = record(value)
  if (typeof r.current !== 'boolean') fail('current')
  return {source:readTranslationSource(r.source),sourceText:text(r,'source_text'),text:text(r,'text'),current:r.current as boolean,reason:text(r,'reason')}
}
export function readTranslationPage<T extends {id: string}>(value: unknown, read: (r: unknown) => T) {
  const r = record(value)
  if (!Array.isArray(r.results)) fail('results')
  const rows = (r.results as unknown[]).map(read), count = integer(r,'count')
  if (new Set(rows.map(row => row.id)).size !== rows.length || count < rows.length) fail('分页数量')
  return {count,next:nullable(r,'next'),previous:nullable(r,'previous'),results:rows}
}
const base = (project: string) => `/projects/${encodeURIComponent(project)}/translations/`
const sourcePath = (project: string, request: string, source: string) => `${base(project)}requests/${encodeURIComponent(request)}/sources/${encodeURIComponent(source)}/`
export const projectTranslationApi = {
  async options(project: string, page: number) {
    const raw = record((await api.get(base(project)+'options/',{params:{page}})).data)
    return {...adaptAssetGenerationOptions(raw),sourceLang:text(raw,'source_lang'),targetLang:text(raw,'target_lang'),maxEntries:integer(raw,'max_entries',1)}
  },
  async list(project: string, page: number) { return readTranslationPage((await api.get(base(project)+'requests/',{params:{page}})).data,readTranslationRequest) },
  async create(project: string, body: object, key: string) {
    if (!key.trim()) throw new Error('缺少本次请求标识')
    return readTranslationRequest(unwrap((await api.post(base(project)+'requests/',body,{headers:{'Idempotency-Key':key}})).data))
  },
  async sources(project: string, request: string, page: number) { return readTranslationPage((await api.get(`${base(project)}requests/${encodeURIComponent(request)}/sources/`,{params:{page}})).data,readTranslationSource) },
  async candidate(project: string, request: string, source: string) { return readTranslationCandidate(unwrap((await api.get(sourcePath(project,request,source))).data)) },
  async adopt(project: string, request: string, source: string) { return readTranslationSource(unwrap((await api.post(sourcePath(project,request,source),{})).data)) },
}
