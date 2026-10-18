import type { JsonObject, ProductionRun } from './platformTypes'
import type { ManualActionOptions } from './productionManual'

type Decision = { revision: number; decision_id: string | null; snapshot_sha256: string | null; status: string }
export type BibleCreativeContent = { characters: JsonObject[]; scenes: JsonObject[]; props: JsonObject[] }
const commonFields = { slug: '条目标识', description: '内容描述', negative_constraints: '排除要求' }
export const bibleCreativeFields = {
  characters: { ...commonFields, stable_speaker_id: '说话人标识', role: '角色定位', age_appearance: '年龄外观',
    face_identity: '面部特征', wardrobe: '服装', voice_profile: '声音特征', personality: '性格' },
  scenes: { ...commonFields, location: '地点', time_of_day: '时段', lighting: '光线', color_palette: '色板', ambient_sound: '环境声' },
  props: { ...commonFields, material: '材质', distinctive_marks: '独特标记', continuity_state: '连续性状态' },
}
export interface BibleReviewContent {
  project_bible_sha256: string
  project_bible: BibleCreativeContent
  candidates: { id: string; entity_type: 'character' | 'scene' | 'prop'; name: string; description: string; identity_anchor: string | null;
    action: 'create' | 'reuse'; conflict_code: string | null; existing: JsonObject | null }[]
  settings: { review_key: string; kind: 'stored' | 'declaration'; entity_type: 'character' | 'scene' | 'prop';
    entity_name: string; entity_key: string | null; appearance_role: 'primary' | 'variant'; variant_key: string | null;
    name: string; reference_name: string; identity_anchor: string; appearance_anchor: string;
    source_facts: JsonObject | null; setting: JsonObject | null; primary_setting: JsonObject | null }[]
}
export interface ManualBibleReview extends BibleReviewContent {
  plan_source: 'manual'; run_id: string; run_plan_sha256: string; current_script: JsonObject | null
  source: Decision & { content: JsonObject | null; candidate_sha256: string | null }
  approval: Decision
}
export interface RecipeBibleReview extends BibleReviewContent {
  plan_source: 'recipe'; run_id: string; run_plan_sha256: string; source_snapshot_sha256: string | null
}
const obj = (value: unknown): JsonObject => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Bible 审核对象缺失，请重新读取')
  return value as JsonObject
}
const sha = (value: unknown): boolean => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const uuid = (value: unknown): boolean => typeof value === 'string' && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(value)
const script = (value: unknown): boolean => value === null || (obj(value).kind === 'script_revision' && uuid(obj(value).id) && sha(obj(value).sha256))
function decision(value: unknown, empty: string): void {
  const row = obj(value)
  if (!Number.isSafeInteger(row.revision) || Number(row.revision) < 0 || ![empty, 'current', 'changed'].includes(String(row.status))
    || (row.revision === 0 ? row.status !== empty || row.decision_id !== null || row.snapshot_sha256 !== null
      : row.status === empty || !uuid(row.decision_id) || !sha(row.snapshot_sha256))) throw new Error('Bible 采用或审核记录不完整')
}
function readContent(raw: JsonObject): void {
  if (!sha(raw.project_bible_sha256) || !Array.isArray(raw.candidates) || !Array.isArray(raw.settings)) throw new Error('完整 Bible 审核集合缺失')
  const bible = obj(raw.project_bible), entityIds = new Set<string>()
  if (Object.keys(bible).sort().join(',') !== 'characters,props,scenes') throw new Error('完整 Bible 创作设定缺失')
  for (const section of Object.keys(bibleCreativeFields) as (keyof BibleCreativeContent)[]) {
    const rows = bible[section]
    if (!Array.isArray(rows)) throw new Error('Bible 创作设定必须列出完整条目')
    for (const value of rows) {
      const row = obj(value)
      if (!uuid(row.id) || entityIds.has(String(row.id)) || typeof row.name !== 'string' || !row.name.trim()
        || 'visual_identity' in row || typeof row.is_locked !== 'boolean'
        || Object.keys(bibleCreativeFields[section]).some(key => typeof row[key] !== 'string')) {
        throw new Error('Bible 创作设定不完整，不能略过声音或其他字段')
      }
      obj(row.metadata); entityIds.add(String(row.id))
    }
  }
  const ids = new Set<string>()
  for (const value of raw.candidates) {
    const item = obj(value)
    if (typeof item.id !== 'string' || !item.id || ids.has(item.id)
      || !['character', 'scene', 'prop'].includes(String(item.entity_type)) || !['create', 'reuse'].includes(String(item.action))
      || typeof item.name !== 'string' || !item.name || typeof item.description !== 'string' || 'visual_identity' in item
      || (item.identity_anchor !== null && typeof item.identity_anchor !== 'string')
      || (item.conflict_code !== null && (typeof item.conflict_code !== 'string' || !item.conflict_code))) throw new Error('Bible 候选内容不完整或重复')
    ids.add(item.id)
    if (item.existing !== null) {
      const existing = obj(item.existing)
      if (!uuid(existing.id) || 'visual_identity' in existing || ['name','description'].some(key => typeof existing[key] !== 'string')
        || typeof existing.is_locked !== 'boolean') throw new Error('Bible 已有内容缺失，不能假称复用成功')
    } else if (item.action === 'reuse') throw new Error('Bible 复用对象缺失')
  }
  const settingKeys = new Set<string>()
  for (const value of raw.settings) {
    const item = obj(value)
    if (typeof item.review_key !== 'string' || !item.review_key || settingKeys.has(item.review_key)
      || !['stored','declaration'].includes(String(item.kind)) || !['primary','variant'].includes(String(item.appearance_role))
      || !['character','scene','prop'].includes(String(item.entity_type))
      || ['name','entity_name','reference_name'].some(key => typeof item[key] !== 'string' || !String(item[key]).trim())
      || ['identity_anchor','appearance_anchor'].some(key => typeof item[key] !== 'string')) throw new Error('主图与变体的完整设定缺失或重复')
    settingKeys.add(item.review_key)
    if (item.kind === 'declaration') {
      const facts = obj(item.source_facts)
      if (item.setting !== null || item.primary_setting !== null || !uuid(facts.script_revision_id)
        || typeof facts.already_materialized !== 'boolean' || typeof facts.primary_reference_name !== 'string'
        || typeof facts.semantic_path !== 'string' || !/^value\.entities\[\d+\]$/.test(facts.semantic_path)) throw new Error('原始声明来源不完整')
      const source = obj(facts.structure_source), output = obj(source.output), reference = obj(source.script)
      if (source.kind !== 'confirmed_script_structure' || source.schema_version !== 1
        || !sha(source.document_sha256) || !sha(source.configuration_sha256)
        || !uuid(output.id) || !sha(output.sha256) || !Number.isSafeInteger(output.version) || Number(output.version) < 1
        || reference.kind !== 'script_revision' || reference.id !== facts.script_revision_id || !sha(reference.sha256)) {
        throw new Error('声明缺少已保存结构化依据')
      }
      for (const key of ['description','identity_anchor','appearance_anchor']) {
        if (facts[key] !== null && typeof facts[key] !== 'string') throw new Error('已映射声明字段应为文本或明确未声明')
      }
    } else {
      const setting = obj(item.setting)
      if (!uuid(setting.setting_revision_id) || !uuid(setting.appearance_id) || !sha(setting.content_sha256)
        || !Number.isSafeInteger(setting.revision) || Number(setting.revision) < 1) throw new Error('当前设定修订证据缺失')
      if (item.appearance_role === 'variant') {
        const primary = obj(item.primary_setting)
        if (!uuid(primary.setting_revision_id) || !sha(primary.content_sha256)
          || ['identity_anchor','appearance_anchor','reference_name'].some(key => typeof primary[key] !== 'string')) throw new Error('变体所依赖的明确主图设定正文缺失')
      } else if (item.primary_setting !== null) throw new Error('主图不应依赖另一主图设定')
    }
  }
}
function readIdentity(raw: JsonObject, run: ProductionRun, planSource: 'manual' | 'recipe') {
  if (run.planSource !== planSource || raw.plan_source !== planSource || raw.run_id !== run.id || !sha(raw.run_plan_sha256)
    || raw.run_plan_sha256 !== run.executionPlan?.sha256) throw new Error('Bible 审核与本轮计划不一致')
  readContent(raw)
}
export function readManualBibleReview(value: unknown, run: ProductionRun): ManualBibleReview {
  const raw = obj(value), source = obj(raw.source)
  readIdentity(raw, run, 'manual')
  decision(source, 'not_adopted'); decision(raw.approval, 'not_reviewed')
  if (!script(raw.current_script) || !script(source.content) || (source.revision === 0 ? source.content !== null
    || source.candidate_sha256 !== null || (raw.candidates as unknown[]).length !== 0
    : source.content === null || !sha(source.candidate_sha256))) throw new Error('Bible 候选缺少明确来源')
  if (source.status === 'current' && (obj(source.content).id !== obj(raw.current_script).id
    || obj(source.content).sha256 !== obj(raw.current_script).sha256)) throw new Error('Bible 来源并非本轮当前剧本')
  return raw as unknown as ManualBibleReview
}
export function readRecipeBibleReview(value: unknown, run: ProductionRun): RecipeBibleReview {
  const raw = obj(value)
  readIdentity(raw, run, 'recipe')
  if (raw.source_snapshot_sha256 !== null && !sha(raw.source_snapshot_sha256)) throw new Error('Bible 声明来源摘要缺失')
  return raw as unknown as RecipeBibleReview
}
export function buildBibleConfirmation(detail: BibleReviewContent, sourceSha: string | null,
  reviewedIds: string[], reviewedSettings: string[], reviewedProjectBible: boolean): JsonObject {
  readContent(detail as unknown as JsonObject)
  const exact = (selected: string[], expected: string[]) => selected.length === expected.length
    && new Set(selected).size === selected.length && expected.every(id => selected.includes(id))
  if (!reviewedProjectBible || detail.candidates.some(row => row.conflict_code)
    || !exact(reviewedIds, detail.candidates.map(row => row.id))
    || !exact(reviewedSettings, detail.settings.map(row => row.review_key))) throw new Error('请核对完整实体声明、主图与变体设定以及项目创作设定')
  return { expected_source_snapshot_sha256: sourceSha, expected_project_bible_sha256: detail.project_bible_sha256,
    reviewed_candidate_ids: [...reviewedIds].sort(), reviewed_setting_keys: [...reviewedSettings].sort(), reviewed_project_bible: true }
}

export function buildManualBibleAction(detail: ManualBibleReview, run: ProductionRun, options: ManualActionOptions,
  action: 'adopt' | 'approve', key: string, reviewedIds: string[], comment: string, reviewedProjectBible: boolean,
  reviewedSettings: string[]): JsonObject {
  readManualBibleReview(detail, run)
  const stageKey = action === 'adopt' ? 'bible_materialize' : 'bible_review'
  if (options.runId !== run.id || options.runPlanSha256 !== detail.run_plan_sha256 || !key.trim()
    || !options.actions.some(row => row.stageKey === stageKey && row.action === action) || !detail.current_script
    || ['succeeded', 'failed', 'cancelled'].includes(run.status)) throw new Error('此轮尚不能执行该 Bible 动作')
  const payload: JsonObject = { expected_current_script: detail.current_script, comment }
  if (action === 'approve') {
    if (reviewedProjectBible !== true) throw new Error('请先核对项目现有的完整创作设定')
    if (detail.source.status !== 'current' || detail.candidates.some(row => row.conflict_code)
      || reviewedIds.length !== detail.candidates.length || new Set(reviewedIds).size !== reviewedIds.length
      || detail.candidates.some(row => !reviewedIds.includes(row.id))) throw new Error('请逐项核对当前全部候选，并先处理内容冲突')
    Object.assign(payload, buildBibleConfirmation(detail, detail.source.snapshot_sha256, reviewedIds, reviewedSettings, reviewedProjectBible))
  }
  return { action, stage_key: stageKey, scope: { kind: 'project', id: run.project }, unit_key: `project:${run.project}`,
    expected_run_plan_sha256: detail.run_plan_sha256,
    expected_selection_revision: action === 'adopt' ? detail.source.revision : detail.approval.revision,
    idempotency_key: key, payload }
}
