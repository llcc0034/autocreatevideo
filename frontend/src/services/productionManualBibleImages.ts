import type { Asset, BibleImageLatestCandidate } from '@/types'
import type { JsonObject, ProductionRun } from './platformTypes'
import type { ManualActionOptions } from './productionManual'
import { parseBibleImageCandidateManifest, type BibleImageReviewPayload } from './productionRunReview'
import { reviewedBibleImageBindings, type BibleImageReviewSelection } from './bibleImageSelections'

export interface ManualBibleImageReview {
  run_id: string; run_plan_sha256: string; candidates: JsonObject; candidates_sha256: string
  targets: { scope: { kind: string; id: string }; name: string; description: string;
    primary_setting: { appearance_id: string; setting_revision_id: string; revision: number; content_sha256: string } | null;
    anchor_asset_id: string | null; reference_asset_ids: string[] }[]
  targets_sha256: string; maximum_selected_images: number
  content_basis: { script_sha256: string; bible_sha256: string; visual_rules_sha256: string; upstream_reviews_sha256: string }
  content_basis_sha256: string
  approval: { revision: number; decision_id: string | null; snapshot_sha256: string | null; status: 'not_reviewed' | 'current' | 'changed'; changes: string[] }
  review: BibleImageReviewPayload
}
const object = (value: unknown): JsonObject => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('参考图审核合同缺失，请重新读取')
  return value as JsonObject
}
const sha = (value: unknown) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const uuid = (value: unknown) => typeof value === 'string' && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(value)
export function readManualBibleImageReview(value: unknown, run: ProductionRun): ManualBibleImageReview {
  const raw = object(value), decision = object(raw.approval), basis = object(raw.content_basis)
  if (run.planSource !== 'manual' || raw.run_id !== run.id || !sha(raw.run_plan_sha256)
    || raw.run_plan_sha256 !== run.executionPlan?.sha256 || !sha(raw.candidates_sha256) || !sha(raw.targets_sha256)
    || !Number.isSafeInteger(raw.maximum_selected_images) || Number(raw.maximum_selected_images) < 1
    || !Array.isArray(raw.targets)) throw new Error('参考图审核与本轮计划或数量配置不一致')
  const basisKeys = ['script_sha256','bible_sha256','visual_rules_sha256','upstream_reviews_sha256']
  if (!sha(raw.content_basis_sha256) || Object.keys(basis).length !== basisKeys.length
    || basisKeys.some(key => !sha(basis[key]))) throw new Error('参考图审核缺少完整内容依据，请重新读取')
  const parsed = parseBibleImageCandidateManifest(raw.candidates, String(raw.candidates_sha256))
  if (!parsed.value) throw new Error(parsed.error)
  if (parsed.value.generationStageId !== run.stages?.find(stage => stage.stageKey === 'bible_image_generation')?.id) throw new Error('参考图候选不属于本轮生成步骤')
  if (!Number.isSafeInteger(decision.revision) || Number(decision.revision) < 0
    || !['not_reviewed','current','changed'].includes(String(decision.status))
    || (decision.revision === 0 ? decision.status !== 'not_reviewed' || decision.decision_id !== null || decision.snapshot_sha256 !== null
      : decision.status === 'not_reviewed' || !uuid(decision.decision_id) || !sha(decision.snapshot_sha256))) throw new Error('参考图审核记录不完整')
  if (!Array.isArray(decision.changes) || decision.changes.some(value => typeof value !== 'string' || !value.trim())
    || (decision.status === 'changed') !== Boolean(decision.changes.length)) throw new Error('参考图审核变化原因不完整')
  const keys = new Set<string>()
  for (const value of raw.targets) {
    const target = object(value), scope = object(target.scope), key = `${scope.kind}:${scope.id}`
    if (keys.has(key) || !parsed.value.entities.some(entity => entity.entityType === scope.kind && entity.entityId === scope.id)
      || target.project_id !== run.project || target.schema_version !== 2 || 'visual_identity' in target
      || ['name','description'].some(field => typeof target[field] !== 'string')
      || (target.anchor_asset_id !== null && !uuid(target.anchor_asset_id)) || !Array.isArray(target.reference_asset_ids)
      || target.reference_asset_ids.some(id => !uuid(id)) || new Set(target.reference_asset_ids).size !== target.reference_asset_ids.length) throw new Error('实体内容或当前图片关系缺失、越界或重复')
    if (target.primary_setting !== null) {
      const primary = object(target.primary_setting)
      if (Object.keys(primary).length !== 4 || !uuid(primary.appearance_id) || !uuid(primary.setting_revision_id)
        || !Number.isSafeInteger(primary.revision) || Number(primary.revision) < 1 || !sha(primary.content_sha256)) throw new Error('主图设定修订依据不完整，请重新读取')
    }
    keys.add(key)
  }
  if (keys.size !== parsed.value.entities.length) throw new Error('实体目录与当前图片关系不完整')
  return { ...raw, review: parsed.value } as unknown as ManualBibleImageReview
}
export function buildManualBibleImageApproval(detail: ManualBibleImageReview, run: ProductionRun, options: ManualActionOptions,
  selections: Record<string, BibleImageReviewSelection>, assets: Asset[], history: Record<string, BibleImageLatestCandidate>, key: string, comment: string): JsonObject {
  const verified = readManualBibleImageReview(detail, run)
  if (options.runId !== run.id || options.runPlanSha256 !== detail.run_plan_sha256 || !key.trim()
    || !options.actions.some(row => row.stageKey === 'bible_image_review' && row.action === 'approve')
    || ['succeeded','failed','cancelled'].includes(run.status)) throw new Error('此轮尚不能执行参考图审核')
  const bindings = reviewedBibleImageBindings(verified.review.entities, selections, assets, history, detail.maximum_selected_images)
  return { action: 'approve', stage_key: 'bible_image_review', unit_key: `project:${run.project}`, scope: { kind: 'project', id: run.project },
    expected_run_plan_sha256: detail.run_plan_sha256, expected_selection_revision: detail.approval.revision, idempotency_key: key,
    payload: { expected_candidates_sha256: detail.candidates_sha256, expected_targets_sha256: detail.targets_sha256,
      expected_content_basis_sha256: detail.content_basis_sha256, bindings, comment } }
}
