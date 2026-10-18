import type { JsonObject, ProductionRun, StageRun } from './platformTypes'

export const asRecord = (value: unknown): JsonObject => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}
export const asRows = (value: unknown): JsonObject[] => Array.isArray(value) ? value.map(asRecord) : []
export const savedText = (value: unknown): string => typeof value === 'string' ? value : ''
export const productionVersionName = (run: Pick<ProductionRun, 'id'>): string => `制作版本 · ${run.id.slice(0, 8)}`
export const productionDate = (value: string): string => value && Number.isFinite(Date.parse(value))
  ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '未记录'

// These are existing snapshot semantic kinds, not configurable field-name guesses.
export function savedStageContent(run: ProductionRun, kind: string, stageKey?: string): { stage: StageRun | null; value: JsonObject; error: string } {
  const matches = (run.stages || []).filter(stage => stage.outputSnapshot.formal_object_type === kind && (!stageKey || stage.stageKey === stageKey))
  if (matches.length > 1) return { stage: null, value: {}, error: '本轮存在多份对应阶段结果，请到制作台按阶段核对；未自动选择最新一份。' }
  const stage = matches[0] || null
  return { stage, value: asRecord(stage?.outputSnapshot.canonical), error: '' }
}

export function creationInput(run: ProductionRun) {
  const intent = asRecord(run.inputSnapshot?.request_intent), inputs = asRecord(intent.creation_inputs)
  const initial = asRecord(run.executionPlan?.value.initial_content)
  const importedRevisionId = savedText(intent.script_revision_id) || (initial.kind === 'script_revision' ? savedText(initial.id) : '')
  const generated = (run.stages || []).some(stage => stage.stageKey === 'script_generate' && stage.mode === 'ai')
  return { inputs, importedRevisionId, generated }
}

export function approvedScriptPreparation(run: ProductionRun): string {
  const script = savedStageContent(run, 'script')
  const review = asRecord(script.stage?.outputSnapshot.review_payload)
  // Only the preparation explicitly selected by this script decision is eligible.
  return review.script_revision_id === run.scriptRevision ? savedText(review.preparation_id) : ''
}

export function bibleMedia(run: ProductionRun) {
  const saved = savedStageContent(run, 'bible_image_bindings')
  return { ...saved, bindings: asRows(saved.value.bindings).map(binding => ({
    entityId: savedText(binding.entity_id), name: savedText(binding.entity_name),
    images: [asRecord(binding.anchor_asset), ...asRows(binding.reference_assets)].filter(asset => asset.asset_id).map(asset => ({
      assetId: savedText(asset.asset_id), sha256: savedText(asset.checksum),
    })),
  })) }
}

export function savedShot(run: ProductionRun, episode: unknown, shot: unknown) {
  const board = savedStageContent(run, 'storyboard', 'storyboard_review')
  const matches = asRows(board.value.episodes).flatMap(item => asRows(item.shots)).filter(item => {
    const source = asRecord(item.script_source)
    return source.structure_id === run.scriptRevision && source.episode_number === episode && source.shot_number === shot
  })
  if (board.error || matches.length > 1) return { error: board.error || '本轮分镜对应关系不唯一，未猜测镜头。', shot: null, artifacts: [] }
  const selected = matches[0] || null
  return { error: '', shot: selected }
}

export function selectedAttemptArtifacts(run: ProductionRun, attemptId: string) {
  const media = savedStageContent(run, 'media_manifest', 'artifact_selection')
  if (media.error) throw new Error(media.error)
  const saved = asRows(media.value.artifacts).filter(item => item.status === 'selected')
  return (run.artifacts || []).filter(item => item.generationAttempt === attemptId && saved.some(ref =>
    ref.id === item.id && ref.sha256 === item.sha256 && ref.variant_group === item.variantGroup))
}

export function semanticStructure(document: JsonObject) {
  const value = asRecord(asRecord(document.projection).value)
  return { entities: asRows(value.entities), episodes: asRows(value.episodes) }
}

/** Script-declared uses, not proof that a media selection or call has happened. */
export function declaredAppearanceUses(episodes: JsonObject[], entityKey: unknown, variantKey: unknown) {
  return episodes.flatMap(episode => asRows(episode.shots).flatMap(shot => {
    const uses = asRows(shot.references).filter(ref => ref.entity_key === entityKey && ref.variant_key === variantKey)
    return uses.length ? [{episode: episode.episode, shot: shot.shot, count: uses.length}] : []
  }))
}
