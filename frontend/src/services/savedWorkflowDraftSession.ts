export type SavedWorkflowImportKind = 'template_version' | 'execution_snapshot' | 'ignored'
export type SavedWorkflowOutputKind = 'image' | 'video' | 'audio' | 'text'

export interface SavedWorkflowDraftSessionItem {
  sourceScopeSchemaVersion: number
  sourceScopeSha256: string
  sourceSha256: string
  selected: boolean
  importKind: SavedWorkflowImportKind
  primaryOutputNodeId: string
  modelProfileVersionId: string
  useExistingTemplate: boolean
  templateId: string
  newTemplateName: string
  newTemplateSlug: string
  outputKind: SavedWorkflowOutputKind
  objectInfoSchemaSha256: string
  convertedGraphSha256: string
  parameterSchemaSha256: string
  structuralContractSha256: string
}

export interface SavedWorkflowDraftSession {
  deploymentId: string
  updatedAt: string
  items: Record<string, SavedWorkflowDraftSessionItem>
}

export interface SavedWorkflowDraftSourceIdentity {
  sourceScopeSchemaVersion: number
  sourceScopeSha256: string
  sourceSha256: string
}

export const savedWorkflowDraftMatchesSource = (
  draft: SavedWorkflowDraftSessionItem | undefined,
  source: SavedWorkflowDraftSourceIdentity,
): boolean => Boolean(
  draft
  && draft.sourceScopeSchemaVersion === source.sourceScopeSchemaVersion
  && draft.sourceScopeSha256 === source.sourceScopeSha256
  && draft.sourceSha256 === source.sourceSha256
)

interface SavedWorkflowDraftLedger {
  schemaVersion: 3
  userId: string
  sessions: SavedWorkflowDraftSession[]
}

export interface SavedWorkflowDraftStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

const SHA256 = /^[0-9a-f]{64}$/
const SLUG = /^[a-z][a-z0-9-]{1,99}$/
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,190}$/
const IMPORT_KINDS = new Set<SavedWorkflowImportKind>([
  'template_version', 'execution_snapshot', 'ignored',
])
const OUTPUT_KINDS = new Set<SavedWorkflowOutputKind>(['image', 'video', 'audio', 'text'])
const STORAGE_PREFIX = 'autoprompt.saved-workflow-import.v3.'
const RETIRED_STORAGE_PREFIXES = [
  'autoprompt.saved-workflow-import.v1.',
  'autoprompt.saved-workflow-import.v2.',
]
export const SAVED_WORKFLOW_STORAGE_ERROR = '浏览器无法保存工作流导入会话；请检查站点存储权限或可用空间后重试。'

const storageKey = (userId: string) => `${STORAGE_PREFIX}${encodeURIComponent(userId)}`
const retireLegacyStorage = (storage: SavedWorkflowDraftStorage, userId: string) => {
  for (const prefix of RETIRED_STORAGE_PREFIXES) {
    try { storage.removeItem(`${prefix}${encodeURIComponent(userId)}`) }
    catch { /* The v3 path never reads or rewrites a retired session contract. */ }
  }
}
const safeText = (value: unknown, maximum: number): value is string => (
  typeof value === 'string'
  && value.length <= maximum
  && !/[\u0000-\u001f\u007f]/.test(value)
)
const safeId = (value: unknown, options: { required?: boolean } = {}): value is string => (
  typeof value === 'string'
  && (!value ? options.required !== true : SAFE_ID.test(value))
)

const safeSourcePath = (value: unknown): value is string => {
  if (!safeText(value, 500) || !value || value.startsWith('/') || value.includes('\\')) return false
  const parts = value.split('/')
  return !parts.some((part) => !part || part === '.' || part === '..' || part.includes(':'))
}

const parseItem = (value: unknown): SavedWorkflowDraftSessionItem | null => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const row = value as Record<string, unknown>
  if (
    !Number.isSafeInteger(row.sourceScopeSchemaVersion)
    || Number(row.sourceScopeSchemaVersion) < 1
    || typeof row.sourceScopeSha256 !== 'string'
    || !SHA256.test(row.sourceScopeSha256)
    || typeof row.sourceSha256 !== 'string' || !SHA256.test(row.sourceSha256)
    || typeof row.selected !== 'boolean'
    || !IMPORT_KINDS.has(row.importKind as SavedWorkflowImportKind)
    || !safeId(row.primaryOutputNodeId)
    || !safeId(row.modelProfileVersionId)
    || typeof row.useExistingTemplate !== 'boolean'
    || !safeId(row.templateId)
    || !safeText(row.newTemplateName, 200)
    || typeof row.newTemplateSlug !== 'string'
    || (row.newTemplateSlug !== '' && !SLUG.test(row.newTemplateSlug))
    || !OUTPUT_KINDS.has(row.outputKind as SavedWorkflowOutputKind)
  ) return null
  const objectInfoSchemaSha256 = row.objectInfoSchemaSha256 ?? ''
  const convertedGraphSha256 = row.convertedGraphSha256 ?? ''
  const parameterSchemaSha256 = row.parameterSchemaSha256 ?? ''
  const structuralContractSha256 = row.structuralContractSha256 ?? ''
  if (
    typeof objectInfoSchemaSha256 !== 'string'
    || (objectInfoSchemaSha256 !== '' && !SHA256.test(objectInfoSchemaSha256))
    || typeof convertedGraphSha256 !== 'string'
    || (convertedGraphSha256 !== '' && !SHA256.test(convertedGraphSha256))
    || typeof parameterSchemaSha256 !== 'string'
    || (parameterSchemaSha256 !== '' && !SHA256.test(parameterSchemaSha256))
    || typeof structuralContractSha256 !== 'string'
    || (structuralContractSha256 !== '' && !SHA256.test(structuralContractSha256))
  ) return null
  return {
    sourceScopeSchemaVersion: Number(row.sourceScopeSchemaVersion),
    sourceScopeSha256: row.sourceScopeSha256,
    sourceSha256: row.sourceSha256,
    selected: row.selected,
    importKind: row.importKind as SavedWorkflowImportKind,
    primaryOutputNodeId: row.primaryOutputNodeId,
    modelProfileVersionId: row.modelProfileVersionId,
    useExistingTemplate: row.useExistingTemplate,
    templateId: row.templateId,
    newTemplateName: row.newTemplateName,
    newTemplateSlug: row.newTemplateSlug,
    outputKind: row.outputKind as SavedWorkflowOutputKind,
    objectInfoSchemaSha256,
    convertedGraphSha256,
    parameterSchemaSha256,
    structuralContractSha256,
  }
}

export interface SavedWorkflowReviewIdentity {
  deploymentId: string
  sourceScopeSchemaVersion: number
  sourceScopeSha256: string
  sourcePath: string
  sourceSha256: string
  modelProfileVersionId: string
  primaryOutputNodeId: string
  objectInfoSchemaSha256: string
  convertedGraphSha256: string
}

export type SavedWorkflowReviewReconciliation = 'exact' | 'missing' | 'changed'

export interface SavedWorkflowStructuralReviewHashes {
  objectInfoSchemaSha256: string
  convertedGraphSha256: string
  parameterSchemaSha256: string
  structuralContractSha256: string
}

export const reconcileSavedWorkflowReviewHashes = (
  persisted: SavedWorkflowStructuralReviewHashes,
  current: SavedWorkflowStructuralReviewHashes,
): SavedWorkflowReviewReconciliation => {
  const keys: Array<keyof SavedWorkflowStructuralReviewHashes> = [
    'objectInfoSchemaSha256', 'convertedGraphSha256',
    'parameterSchemaSha256', 'structuralContractSha256',
  ]
  if (keys.some((key) => !SHA256.test(persisted[key]) || !SHA256.test(current[key]))) {
    return 'missing'
  }
  return keys.every((key) => persisted[key] === current[key]) ? 'exact' : 'changed'
}

export const attestationMatchesSavedWorkflowReview = (
  snapshot: Record<string, unknown>,
  review: SavedWorkflowReviewIdentity,
): boolean => {
  const primary = snapshot.primary_output_node_ids
  return Boolean(
    review.deploymentId
    && Number.isSafeInteger(review.sourceScopeSchemaVersion)
    && review.sourceScopeSchemaVersion > 0
    && SHA256.test(review.sourceScopeSha256)
    && review.sourcePath
    && SHA256.test(review.sourceSha256)
    && review.modelProfileVersionId
    && review.primaryOutputNodeId
    && SHA256.test(review.objectInfoSchemaSha256)
    && SHA256.test(review.convertedGraphSha256)
    && snapshot.source_deployment_id === review.deploymentId
    && snapshot.source_scope_schema_version === review.sourceScopeSchemaVersion
    && snapshot.source_scope_sha256 === review.sourceScopeSha256
    && snapshot.source_path === review.sourcePath
    && snapshot.source_sha256 === review.sourceSha256
    && snapshot.target_model_profile_version_id === review.modelProfileVersionId
    && Array.isArray(primary)
    && primary.length === 1
    && primary[0] === review.primaryOutputNodeId
    && snapshot.object_info_schema_sha256 === review.objectInfoSchemaSha256
    && snapshot.converted_graph_sha256 === review.convertedGraphSha256
  )
}

export interface SavedWorkflowHydrationGuard {
  active(): boolean
  run<T>(task: () => Promise<T>): Promise<T>
}

export const createSavedWorkflowHydrationGuard = (): SavedWorkflowHydrationGuard => {
  let depth = 0
  return {
    active: () => depth > 0,
    async run<T>(task: () => Promise<T>): Promise<T> {
      depth += 1
      try { return await task() }
      finally { depth = Math.max(0, depth - 1) }
    },
  }
}

const readLedger = (
  storage: SavedWorkflowDraftStorage,
  userId: string,
): SavedWorkflowDraftLedger => {
  const empty: SavedWorkflowDraftLedger = { schemaVersion: 3, userId, sessions: [] }
  if (!safeId(userId, { required: true })) return empty
  retireLegacyStorage(storage, userId)
  let parsed: unknown
  try { parsed = JSON.parse(storage.getItem(storageKey(userId)) || 'null') }
  catch { return empty }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return empty
  const raw = parsed as Record<string, unknown>
  if (raw.schemaVersion !== 3 || raw.userId !== userId || !Array.isArray(raw.sessions)) return empty
  const sessions: SavedWorkflowDraftSession[] = []
  for (const value of raw.sessions.slice(0, 20)) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) continue
    const row = value as Record<string, unknown>
    if (!safeId(row.deploymentId, { required: true }) || typeof row.updatedAt !== 'string') continue
    if (!row.items || typeof row.items !== 'object' || Array.isArray(row.items)) continue
    const items: Record<string, SavedWorkflowDraftSessionItem> = {}
    for (const [sourcePath, item] of Object.entries(row.items).slice(0, 250)) {
      if (!safeSourcePath(sourcePath)) continue
      const parsedItem = parseItem(item)
      if (parsedItem) items[sourcePath] = parsedItem
    }
    if (Object.keys(items).length) sessions.push({
      deploymentId: row.deploymentId,
      updatedAt: Number.isFinite(Date.parse(row.updatedAt)) ? row.updatedAt : '',
      items,
    })
  }
  return { schemaVersion: 3, userId, sessions }
}

export const readSavedWorkflowDraftSession = (
  storage: SavedWorkflowDraftStorage,
  userId: string,
  deploymentId: string,
): SavedWorkflowDraftSession | null => readLedger(storage, userId).sessions
  .find((session) => session.deploymentId === deploymentId) || null

export const writeSavedWorkflowDraftSession = (
  storage: SavedWorkflowDraftStorage,
  userId: string,
  deploymentId: string,
  items: Record<string, SavedWorkflowDraftSessionItem>,
  now = new Date(),
): boolean => {
  if (!safeId(userId, { required: true }) || !safeId(deploymentId, { required: true })) return false
  const sanitized: Record<string, SavedWorkflowDraftSessionItem> = {}
  for (const [sourcePath, item] of Object.entries(items).slice(0, 250)) {
    const parsed = parseItem(item)
    if (safeSourcePath(sourcePath) && parsed) sanitized[sourcePath] = parsed
  }
  const ledger = readLedger(storage, userId)
  const remaining = ledger.sessions.filter((session) => session.deploymentId !== deploymentId)
  if (Object.keys(sanitized).length) remaining.push({
    deploymentId,
    updatedAt: now.toISOString(),
    items: sanitized,
  })
  try {
    if (!remaining.length) storage.removeItem(storageKey(userId))
    else storage.setItem(storageKey(userId), JSON.stringify({
      schemaVersion: 3,
      userId,
      sessions: remaining.slice(-20),
    }))
    return true
  } catch { return false }
}
