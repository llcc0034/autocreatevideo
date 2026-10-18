import type { ProductionManualOptions, ProductionManualRunCreateInput, ProductionRunCreateInput, ScriptRevision } from './platformTypes'
import { buildProductionRunRequestPayload } from './productionRunPayload'

/** Validate before submission, then detach arrays and overrides from editable form state. */
export function snapshotProductionRunInput(input: ProductionRunCreateInput): ProductionRunCreateInput {
  buildProductionRunRequestPayload(input)
  return JSON.parse(JSON.stringify(input)) as ProductionRunCreateInput
}

export function onboardingManualInput(options: ProductionManualOptions, input: {
  projectId: string; idempotencyKey: string; revision: ScriptRevision | null
  allowExternalCalls: boolean
}): ProductionManualRunCreateInput {
  if (options.projectId !== input.projectId || !options.scopeKinds.includes('project') || !options.deliveryKinds.includes('episode_masters')) {
    throw new Error('当前项目的人工制作目录不支持整项目逐集交付。')
  }
  if (input.revision && (!options.initialContentKinds.includes('script_revision') || input.revision.status !== 'confirmed')) {
    throw new Error('必须先确认导入剧本，才能将其作为初始内容。')
  }
  return { planSource: 'manual', projectId: input.projectId, idempotencyKey: input.idempotencyKey,
    allowExternalCalls: input.allowExternalCalls, configOverrides: {},
    manualPlan: { stageCatalogKey: String(options.stageCatalog.value.key), expectedStageCatalogSha256: options.stageCatalog.sha256,
      scope: { kind: 'project' }, delivery: { kind: 'episode_masters' }, initialContent: input.revision ? {
        kind: 'script_revision', id: input.revision.id, expectedSha256: input.revision.contentSha256,
      } : null } }
}

/** Retain the exact request across an uncertain response, never issue a new key. */
export function isDefinitiveCreationRejection(cause: unknown): boolean {
  const status = (cause as { response?: { status?: number } })?.response?.status
  return typeof status === 'number' && status >= 400 && status < 500 && ![408, 409, 429].includes(status)
}
