import type { JsonObject, StageRun } from './platformTypes'



export interface BibleImageReviewCandidate {
  artifactId: string
  variantGroup: string
  variantIndex: number
  eligibility: 'eligible'
  sha256: string
  manifestSha256: string
  attemptId: string
  preparationId: string
  executionInstanceVersionId: string
  requestSha256: string
  promptProfileVersionId: string | null
  targetSnapshotSha256: string
  originProductionRunId: string
  historical: boolean
}

export interface BibleImageFulfilledMaterial {
  assetId: string
  assetName: string
  checksum: string
  mimeType: string
  appearanceRole: 'primary' | 'variant'
  reference: JsonObject
  referenceVersionId: string
  source: 'existing' | 'upload' | 'generated'
}

export interface BibleImageReviewEntity {
  entityType: 'character' | 'scene' | 'prop'
  entityId: string
  entityName: string
  targetContentSha256: string
  candidates: BibleImageReviewCandidate[]
  fulfilledMaterials?: BibleImageFulfilledMaterial[]
}

export interface BibleImageReviewPayload {
  snapshotSha256: string
  generationStageId: string
  entities: BibleImageReviewEntity[]
}

export type ReviewParseResult<T> = { value: T; error: '' } | { value: null; error: string }

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const SHA256 = /^[0-9a-f]{64}$/
const record = (value: unknown): JsonObject | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : null
const failed = <T>(error: string): ReviewParseResult<T> => ({ value: null, error })
const text = (value: unknown): string | null => typeof value === 'string' ? value : null
const uuid = (value: unknown): string | null => typeof value === 'string' && UUID.test(value) ? value : null
const hash = (value: unknown): string | null => typeof value === 'string' && SHA256.test(value) ? value : null
const stringList = (value: unknown): string[] | null => Array.isArray(value) && value.every((item) => typeof item === 'string') ? value as string[] : null
const objectList = (value: unknown): JsonObject[] | null => Array.isArray(value) && value.every((item) => record(item) !== null) ? value as JsonObject[] : null
const sourceStage = (stages: StageRun[], key: string): StageRun | null => [...stages].sort((a, b) => b.ordinal - a.ordinal).find((stage) => stage.stageKey === key && stage.status === 'succeeded') || null

export const parseBibleImageReviewPayload = (
  stages: StageRun[],
): ReviewParseResult<BibleImageReviewPayload> => {
  const stage = sourceStage(stages, 'bible_image_generation')
  if (!stage) return failed('缺少已成功的 bible_image_generation 阶段，不能核验参考图候选。')
  if (!hash(stage.outputSnapshotSha256)) return failed('bible_image_generation 输出快照 hash 缺失或格式错误。')
  const snapshot = record(stage.outputSnapshot)
  if (!snapshot || snapshot.stage_key !== 'bible_image_generation' || snapshot.formal_object_type !== 'bible_image_candidate_manifest') {
    return failed('bible_image_generation 正式输出快照缺失或类型不匹配。')
  }
  return parseBibleImageCandidateManifest(snapshot.canonical, stage.outputSnapshotSha256)
}

/** Shared candidate parser; manual review does not invent a succeeded Stage. */
export const parseBibleImageCandidateManifest = (value: unknown, snapshotSha256: string): ReviewParseResult<BibleImageReviewPayload> => {
  if (!hash(snapshotSha256)) return failed('参考图候选摘要缺失。')
  const canonical = record(value)
  const generationStageId = uuid(canonical?.generation_stage_id)
  if (!canonical || !generationStageId || !Array.isArray(canonical.entities) || canonical.entities.length === 0) {
    return failed('Bible 参考图候选快照没有完整实体清单。')
  }
  const entities: BibleImageReviewEntity[] = []
  const entityKeys = new Set<string>()
  const artifactIds = new Set<string>()
  const references = new Map<string, Record<string, unknown>>()
  for (const field of ['material_fulfillments', 'material_generations']) {
    const values = canonical[field] ?? []
    if (!Array.isArray(values)) return failed('已履约素材版本清单无效。')
    for (const value of values) {
      const ref = record(value)
      const id = uuid(ref?.fulfillment_id) || uuid(ref?.decision_id)
      if (!ref || !id || references.has(id)) return failed('已履约素材选择标识缺失或重复。')
      references.set(id, ref)
    }
  }
  const consumed = new Set<string>()
  for (const entityValue of canonical.entities) {
    const entity = record(entityValue)
    const entityType = text(entity?.entity_type)
    const entityId = uuid(entity?.entity_id)
    const entityName = text(entity?.entity_name)
    const targetContentSha256 = hash(entity?.target_content_sha256)
    if (!entity || !['character', 'scene', 'prop'].includes(entityType || '') || !entityId || !entityName?.trim() || !targetContentSha256 || !Array.isArray(entity.candidates)) {
      return failed('Bible 参考图候选实体标识、来源或候选列表无效。')
    }
    const entityKey = `${entityType}:${entityId}`
    if (entityKeys.has(entityKey)) return failed('Bible 参考图候选快照重复声明同一实体。')
    entityKeys.add(entityKey)
    const candidates: BibleImageReviewCandidate[] = []
    for (const candidateValue of entity.candidates) {
      const candidate = record(candidateValue)
      const artifactId = uuid(candidate?.artifact_id)
      const variantGroup = text(candidate?.variant_group)
      const variantIndex = Number(candidate?.variant_index)
      const eligibility = text(candidate?.eligibility)
      const sha256 = hash(candidate?.sha256)
      const attemptId = uuid(candidate?.attempt_id)
      const preparationId = uuid(candidate?.preparation_id)
      const executionInstanceVersionId = uuid(candidate?.execution_instance_version_id)
      const requestSha256 = hash(candidate?.request_sha256)
      const manifestSha256 = hash(candidate?.manifest_sha256)
      const promptProfileVersionId = uuid(candidate?.prompt_profile_version_id)
      const targetSnapshotSha256 = hash(candidate?.target_snapshot_sha256)
      const originProductionRunId = uuid(candidate?.origin_production_run_id)
      if (!candidate || !artifactId || artifactIds.has(artifactId) || !variantGroup?.trim() || !Number.isInteger(variantIndex) || variantIndex < 1 || eligibility !== 'eligible' || !sha256 || !attemptId || !preparationId || !executionInstanceVersionId || !requestSha256 || !manifestSha256 || (candidate.prompt_profile_version_id !== null && !promptProfileVersionId) || !targetSnapshotSha256 || !originProductionRunId || typeof candidate.historical !== 'boolean') {
        return failed('Bible 参考图候选的执行准备、实例版本或归档证据无效。')
      }
      artifactIds.add(artifactId)
      candidates.push({ artifactId, variantGroup, variantIndex, eligibility, sha256, manifestSha256, attemptId, preparationId, executionInstanceVersionId, requestSha256, promptProfileVersionId, targetSnapshotSha256, originProductionRunId, historical: candidate.historical })
    }
    const materials: BibleImageFulfilledMaterial[] = []
    if (entity.fulfilled_materials !== undefined && !Array.isArray(entity.fulfilled_materials)) return failed('已履约素材展示清单无效。')
    for (const value of (entity.fulfilled_materials as unknown[] | undefined) ?? []) {
      const item = record(value), ref = record(item?.reference)
      const id = uuid(ref?.fulfillment_id) || uuid(ref?.decision_id)
      const original = id ? references.get(id) : null
      const assetId = uuid(item?.asset_id), checksum = hash(item?.checksum), mimeType = text(item?.mime_type)
      const referenceVersionId = uuid(ref?.reference_version_id), source = text(ref?.source)
      if (!item || !ref || !id || !original || consumed.has(id) || !assetId || !checksum || !mimeType?.startsWith('image/')
        || !text(item.asset_name)?.trim() || !['primary', 'variant'].includes(String(item.appearance_role))
        || !referenceVersionId || !['existing', 'upload', 'generated'].includes(source || '')
        || !text(ref.unit_key) || !text(ref.requirement_key)
        || Object.keys(ref).length !== Object.keys(original).length || Object.entries(original).some(([key, value]) => ref[key] !== value)) {
        return failed('已履约素材的归属、来源或确切版本与冻结清单不一致。')
      }
      consumed.add(id)
      materials.push({ assetId, assetName: item.asset_name as string, checksum, mimeType,
        appearanceRole: item.appearance_role as BibleImageFulfilledMaterial['appearanceRole'], reference: ref as JsonObject,
        referenceVersionId, source: source as BibleImageFulfilledMaterial['source'] })
    }
    if (materials.filter(item => item.appearanceRole === 'primary').length > 1) return failed('同一实体不能声明多个已履约主图。')
    if (materials.some(item => materials.some(other => other.assetId === item.assetId && (other.checksum !== item.checksum || other.mimeType !== item.mimeType)))) return failed('同一素材的冻结摘要或类型不一致。')
    entities.push({ entityType: entityType as BibleImageReviewEntity['entityType'], entityId, entityName, targetContentSha256, candidates,
      ...(materials.length ? { fulfilledMaterials: materials } : {}) })
  }
  if (consumed.size !== references.size) return failed('已履约素材缺少对应实体展示，不能忽略后继续审核。')
  return { value: { snapshotSha256, generationStageId, entities }, error: '' }
}
