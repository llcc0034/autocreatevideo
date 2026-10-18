import type { JsonObject, ProductionRun } from './platformTypes'
import type { ManualActionOptions } from './productionManual'
import { readShotTiming } from './shotTiming'

export interface ManualStoryboardDetail {
  run_id: string; run_plan_sha256: string; content_sha256: string
  content: { schema_version: number; run_plan_sha256: string; script: JsonObject | null; script_decision_id: string | null;
    episodes: { id: string; number: number; title: string; synopsis: string; shots: (JsonObject & {
      id: string; number: number; title: string; duration_ms: number; media_type: string; episode_start_ms: number | null; episode_end_ms: number | null })[] }[] }
  adoption: { revision: number; decision_id: string | null; snapshot_sha256: string | null;
    content_sha256: string | null; status: 'not_adopted' | 'current' | 'changed'; source: JsonObject | null }
  materialization: { ready: boolean; reason: string; sha256: string | null; contract: (JsonObject & {
    parameters: JsonObject; source_basis: JsonObject & { script: JsonObject }; run_config_sha256: string }) | null }
}
const obj = (value: unknown): JsonObject => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('分镜内容缺失，请重新读取')
  return value as JsonObject
}
const sha = (value: unknown): boolean => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const uuid = (value: unknown): boolean => typeof value === 'string' && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(value)
function readMaterializationContract(value: unknown, planSha256: unknown) {
  const contract = obj(value), basis = obj(contract.source_basis), script = obj(basis.script), parameters = obj(contract.parameters)
  if (contract.schema_version !== 3 || !sha(contract.run_config_sha256) || basis.schema_version !== 2
    || basis.run_plan_sha256 !== planSha256 || !sha(basis.resolved_relations_sha256)
    || !uuid(basis.script_decision_id) || !uuid(script.id) || !sha(script.sha256)
    || !Array.isArray(basis.script_stages) || !basis.script_stages.length
    || basis.script_stages.some(value => !uuid(obj(value).stage_id) || !sha(obj(value).output_snapshot_sha256))
    || !uuid(obj(basis.bible_review).decision_id) || !uuid(obj(basis.bible_review).stage_run_id)
    || obj(basis.source).schema_version !== 3 || obj(basis.source).kind !== 'script_reference_bible'
    || !sha(obj(basis.source).declaration_sha256)
    || !sha(obj(obj(basis.source).structure_source).document_sha256)
    || ['default_conditioning_mode','episode_count_hint','auto_split_trigger_chars','auto_split_target_chars','prop_alias_policy'].some(key => key in parameters)
    || !['image','video'].includes(String(parameters.default_shot_media_type))
    || ['default_shot_duration_ms','video_shot_min_duration_ms','video_shot_max_duration_ms']
      .some(key => !Number.isSafeInteger(parameters[key]) || Number(parameters[key]) < 1)
    || ['multi_shot','include_audio','default_first_frame_continuity','default_last_frame_continuity'].some(key => typeof parameters[key] !== 'boolean')) throw new Error('结构化来源、Bible采用依据或创建参数不完整')
  return { basis, script }
}
export function readManualStoryboard(value: unknown, run: ProductionRun): ManualStoryboardDetail {
  const raw = obj(value), content = obj(raw.content), adoption = obj(raw.adoption), materialization = obj(raw.materialization)
  if (run.planSource !== 'manual' || raw.run_id !== run.id || !sha(raw.content_sha256) || !sha(raw.run_plan_sha256)
    || raw.run_plan_sha256 !== run.executionPlan?.sha256 || content.schema_version !== 3
    || content.run_plan_sha256 !== raw.run_plan_sha256 || !Array.isArray(content.episodes)
    || (content.script === null ? content.script_decision_id !== null
      : !uuid(content.script_decision_id) || !uuid(obj(content.script).id) || !sha(obj(content.script).sha256))) throw new Error('分镜来源与本轮冻结范围不一致')
  if (!Number.isSafeInteger(adoption.revision) || Number(adoption.revision) < 0 || !['not_adopted','current','changed'].includes(String(adoption.status))
    || (adoption.revision === 0 ? adoption.status !== 'not_adopted' || adoption.decision_id !== null || adoption.snapshot_sha256 !== null || adoption.content_sha256 !== null
      : adoption.status === 'not_adopted' || !uuid(adoption.decision_id) || !sha(adoption.snapshot_sha256) || !sha(adoption.content_sha256))
    || (adoption.revision !== 0 && (adoption.status === 'current') !== (adoption.content_sha256 === raw.content_sha256))) throw new Error('分镜采用记录与当前内容不一致')
  if (adoption.revision === 0 ? adoption.source !== null : !['existing_storyboard','confirmed_script'].includes(String(obj(adoption.source).kind))) throw new Error('分镜采用来源缺失')
  if (adoption.revision !== 0) {
    const source = obj(adoption.source)
    if (source.kind === 'confirmed_script') {
      if (Object.keys(source).sort().join(',') !== 'contract,kind,sha256' || !sha(source.sha256)) throw new Error('分镜采用来源合同不完整')
      const { basis, script } = readMaterializationContract(source.contract, raw.run_plan_sha256)
      if (adoption.status === 'current' && (basis.script_decision_id !== content.script_decision_id
        || script.id !== obj(content.script).id || script.sha256 !== obj(content.script).sha256)) throw new Error('当前分镜采用与结构来源不一致')
    } else if (Object.keys(source).length !== 1) throw new Error('已有分镜采用来源包含未知字段')
  }
  if (typeof materialization.ready !== 'boolean' || typeof materialization.reason !== 'string'
    || (materialization.ready ? !sha(materialization.sha256) || materialization.reason !== ''
      : materialization.contract !== null || materialization.sha256 !== null || !materialization.reason)) throw new Error('剧本拆稿条件尚未明确读取')
  if (materialization.ready) {
    const { basis, script } = readMaterializationContract(materialization.contract, raw.run_plan_sha256)
    if (basis.script_decision_id !== content.script_decision_id || script.id !== obj(content.script).id
      || script.sha256 !== obj(content.script).sha256 || content.episodes.length) throw new Error('结构化来源与当前剧本采用不一致')
  }
  const episodes = new Set<string>(), shots = new Set<string>()
  for (const value of content.episodes) {
    const episode = obj(value)
    if (!uuid(episode.id) || episodes.has(String(episode.id)) || !Number.isSafeInteger(episode.number) || Number(episode.number) < 1
      || typeof episode.title !== 'string' || typeof episode.synopsis !== 'string' || !Array.isArray(episode.shots)) throw new Error('分镜剧集列表缺失或重复')
    episodes.add(String(episode.id))
    for (const value of episode.shots) {
      const shot = obj(value)
      if (!uuid(shot.id) || shots.has(String(shot.id)) || !Number.isSafeInteger(shot.number) || Number(shot.number) < 1
        || !Number.isSafeInteger(shot.duration_ms) || Number(shot.duration_ms) < 1 || !['image','video'].includes(String(shot.media_type))
        || typeof shot.title !== 'string'
        || ['reference_asset_ids','reference_bindings','character_ids','visual_reference_character_ids'].some(key => key in shot)
        || !Array.isArray(shot.dialogue) || !Array.isArray(shot.character_relations)
        || !Array.isArray(shot.prop_ids) || shot.prop_ids.some(id => !uuid(id))) throw new Error('镜头内容不完整或混入旧关系/素材绑定，不能直接采用')
      const positions = new Set<string>(), visible = new Set<string>(), speakers = new Set<number>()
      for (const value of shot.character_relations) {
        const relation = obj(value), role = relation.role, position = relation.position, identity = relation.character_id
        if (Object.keys(relation).sort().join(',') !== 'character_id,position,role'
          || typeof role !== 'string' || !['visible','speaker'].includes(role) || !Number.isSafeInteger(position) || Number(position) < 0 || !uuid(identity)
          || positions.has(`${role}:${position}`) || role === 'visible' && visible.has(String(identity))) throw new Error('出镜/发声关系缺失或重复')
        positions.add(`${role}:${position}`)
        if (role === 'visible') visible.add(String(identity)); else speakers.add(Number(position))
      }
      if (speakers.size !== shot.dialogue.length || shot.dialogue.some((_, index) => !speakers.has(index)))
        throw new Error('原对白缺少逐行发言者关系，不能从旧人物集合推断')
      shots.add(String(shot.id))
      readShotTiming(shot, Number(shot.duration_ms) / 1000)
    }
  }
  return raw as unknown as ManualStoryboardDetail
}

export function buildManualStoryboardAdoption(detail: ManualStoryboardDetail, run: ProductionRun, options: ManualActionOptions,
  key: string, checked: string[], comment: string): JsonObject {
  readManualStoryboard(detail, run)
  const ids = detail.content.episodes.flatMap(episode => episode.shots.map(shot => shot.id))
  if (!detail.content.episodes.length || detail.content.episodes.some(episode => !episode.shots.length)
    || ids.length !== checked.length || new Set(checked).size !== checked.length || ids.some(id => !checked.includes(id))) throw new Error('请补齐本轮分镜并逐镜核对，不能采用空集合')
  if (options.runId !== run.id || options.runPlanSha256 !== detail.run_plan_sha256 || !key.trim()
    || !options.actions.some(row => row.stageKey === 'storyboard' && row.action === 'adopt')
    || ['succeeded','failed','cancelled'].includes(run.status)) throw new Error('本轮尚不能采用分镜内容')
  return { action: 'adopt', stage_key: 'storyboard', scope: { kind: 'project', id: run.project }, unit_key: `project:${run.project}`,
    expected_run_plan_sha256: detail.run_plan_sha256, expected_selection_revision: detail.adoption.revision, idempotency_key: key,
    payload: { source: { kind: 'existing_storyboard' }, expected_content_sha256: detail.content_sha256, reviewed_shot_ids: [...checked].sort(), comment } }
}

export function buildManualStoryboardFromScript(detail: ManualStoryboardDetail, run: ProductionRun, options: ManualActionOptions,
  key: string, confirmed: boolean, comment: string): JsonObject {
  readManualStoryboard(detail, run)
  if (!confirmed || !detail.materialization.ready || !detail.materialization.contract || detail.content.episodes.length
    || options.runId !== run.id || options.runPlanSha256 !== detail.run_plan_sha256 || !key.trim()
    || !options.actions.some(row => row.stageKey === 'storyboard' && row.action === 'adopt')
    || ['succeeded','failed','cancelled'].includes(run.status)) throw new Error('请先核对已采用剧本和拆稿参数；不能覆盖已有分镜')
  return { action:'adopt',stage_key:'storyboard',scope:{kind:'project',id:run.project},unit_key:`project:${run.project}`,
    expected_run_plan_sha256:detail.run_plan_sha256,expected_selection_revision:detail.adoption.revision,idempotency_key:key,
    payload:{source:{kind:'confirmed_script',expected_materialization_sha256:detail.materialization.sha256},
      expected_content_sha256:detail.content_sha256,comment} }
}
