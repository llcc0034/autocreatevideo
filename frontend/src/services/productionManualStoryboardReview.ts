import type { JsonObject, ProductionRun } from './platformTypes'
import type { ManualActionOptions } from './productionManual'
import type { ExactReferenceSelection } from './exactReferencePreview'
import {readStoryboardReviewDisplay, type StoryboardReviewDisplay} from './storyboardReviewDisplay'
import type { ExecutionReviewedShotSource } from './executionRequestForm'

type Reference = { reference_key:string;source_key:string;kind:string;source:{kind:'asset';asset_id:string};
  purpose:string|null;subject_ref:{kind:string;id:string}|null;paired_reference_key:string|null }
type Shot = JsonObject & { id:string;number:number;title:string;reference_uses:JsonObject[] }
export interface ManualStoryboardReferenceReview {
  display: StoryboardReviewDisplay | null
  run_id:string;run_plan_sha256:string;ready:boolean;reason:string;contract_sha256:string|null
  contract:{schema_version:number;kind:string;canonical:{episodes:{id:string;number:number;title:string;shots:Shot[]}[]};
    assets:{id:string;name:string;kind:string;mime_type:string;checksum:string}[];manual_basis:JsonObject}|null
  references:{shot_id:string;items:Reference[]}[]
  source_options:{value:string;label:string;kind:string}[]
  approval:{revision:number;decision_id:string|null;snapshot_sha256:string|null;status:'not_reviewed'|'current'|'changed'}
}
const object=(value:unknown):value is JsonObject=>!!value&&typeof value==='object'&&!Array.isArray(value)
const uuid=(v:unknown):v is string=>typeof v==='string'&&/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(v)
const sha=(v:unknown):v is string=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v)
/** The existing review is the catalog; selecting it does not approve anything. */
export function manualGenerationShotSources(detail: ManualStoryboardReferenceReview, shotId: string): ExecutionReviewedShotSource[] {
  if (!detail.ready || detail.approval.status !== 'current' || !detail.approval.decision_id || !detail.approval.snapshot_sha256)
    throw Error(detail.reason || '本轮逐镜参考尚未审核或已变化，请先核对原参考清单')
  const row = detail.references.find(row=>row.shot_id===shotId)
  if (!row) throw Error('当前制作镜头不在本轮审核范围内')
  return row.items.map(item=>{
    const preview = manualStoryboardReferencePreview(detail, shotId, item)
    if (!preview.selection) throw Error(preview.error)
    return {runId:detail.run_id, shotId, referenceKey:item.reference_key, assetId:item.source.asset_id,
      kind:item.kind, name:detail.contract!.assets.find(asset=>asset.id===item.source.asset_id)!.name,
      subjectRef:item.subject_ref ? {...item.subject_ref} : null, creativePurpose:item.purpose,
      review:{decision_id:detail.approval.decision_id!, snapshot_sha256:detail.approval.snapshot_sha256!}, preview:preview.selection}
  })
}
/** Read the original canonical use, not a live shot or a guessed library version. */
export function manualStoryboardReferencePreview(detail: ManualStoryboardReferenceReview, shotId: string, item: Reference): {
  selection: ExactReferenceSelection | null; sourceLabel: string; error: string
} {
  try {
    const asset = detail.contract?.assets.find(row => row.id === item.source.asset_id)
    if (!asset || !sha(asset.checksum)) throw Error('原审核合同缺少素材文件指纹')
    const selection: ExactReferenceSelection = {assetId: asset.id, assetSha256: asset.checksum,
      mediaKind: item.kind, referenceVersionId: null}
    if (!item.reference_key.startsWith('shot-use:')) throw Error('参考素材缺少原独立用途身份，不读取旧附加素材池')
    const shot = detail.contract?.canonical.episodes.flatMap(row => row.shots).find(row => row.id === shotId)
    const matches = Array.isArray(shot?.reference_uses) ? shot.reference_uses.filter(row => object(row)
      && 'shot-use:' + row.use_key === item.reference_key) : []
    if (matches.length !== 1 || !object(matches[0])) throw Error('原审核合同缺少此独立用途，未补查当前镜头冒充原选择')
    const use = matches[0]
    if (use.media_kind !== item.kind || use.purpose !== item.purpose || use.asset_sha256 !== asset.checksum
      || !['existing','upload','generated'].includes(String(use.source_mode))
      || (use.reference_version_id !== null && !uuid(use.reference_version_id))
      || (use.reference_version_id ? use.asset_id !== null : use.asset_id !== asset.id))
      throw Error('原用途与审核素材、版本或来源不一致，未改用直接素材')
    selection.referenceVersionId = use.reference_version_id as string | null
    return {selection, sourceLabel: {existing:'已有素材',upload:'正式上传',generated:'本轮生成'}[String(use.source_mode)]!, error: ''}
  } catch (cause) {return {selection: null, sourceLabel: '', error: (cause as Error).message}}
}
export function readManualStoryboardReferences(raw:unknown,run:ProductionRun):ManualStoryboardReferenceReview {
  if(!object(raw)||run.planSource!=='manual'||raw.run_id!==run.id||!sha(raw.run_plan_sha256)
    ||raw.run_plan_sha256!==run.executionPlan?.sha256||typeof raw.ready!=='boolean'||typeof raw.reason!=='string'
    ||!Array.isArray(raw.references)||!Array.isArray(raw.source_options)||!object(raw.approval))throw Error('逐镜审核与本轮计划不一致')
  const sources=new Map<string,string>()
  for(const row of raw.source_options){
    if(!object(row)||typeof row.value!=='string'||!row.value||sources.has(row.value)
      ||typeof row.label!=='string'||!row.label.trim()||!['image','video','audio'].includes(String(row.kind)))throw Error('参考来源目录缺失或重复')
    sources.set(row.value,String(row.kind))
  }
  const a=raw.approval
  if(!Number.isSafeInteger(a.revision)||Number(a.revision)<0||!['not_reviewed','current','changed'].includes(String(a.status))
    ||(a.revision===0?a.status!=='not_reviewed'||a.decision_id!==null||a.snapshot_sha256!==null:
      a.status==='not_reviewed'||!uuid(a.decision_id)||!sha(a.snapshot_sha256))
    ||(a.status==='current'&&!raw.ready)
    ||(Number(a.revision)>0&&raw.ready&&(a.status==='current')!==(a.snapshot_sha256===raw.contract_sha256)))throw Error('逐镜审核决定不完整')
  if(!raw.ready){
    if(!raw.reason||raw.contract!==null||raw.contract_sha256!==null||raw.references.length||raw.display!==null)throw Error('未就绪的逐镜审核不能伪造合同')
    return raw as unknown as ManualStoryboardReferenceReview
  }
  const c=raw.contract
  if(raw.reason||!sha(raw.contract_sha256)||!object(c)||c.schema_version!==1||c.kind!=='manual_storyboard_review'
    ||c.stage_key!=='storyboard_review'||c.formal_object_type!=='storyboard'||!sha(c.canonical_sha256)
    ||!object(c.manual_basis)||c.manual_basis.run_plan_sha256!==raw.run_plan_sha256||!object(c.canonical)
    ||!Array.isArray(c.canonical.episodes)||!c.canonical.episodes.length||!Array.isArray(c.assets))throw Error('逐镜审核合同缺失')
  const shots=new Set<string>(),assets=new Set<string>(),episodes=new Set<string>()
  for(const item of c.assets){
    if(!object(item)||!uuid(item.id)||assets.has(item.id)||['name','kind','mime_type','checksum'].some(k=>typeof item[k]!=='string'))throw Error('审核素材身份不完整')
    assets.add(item.id)
  }
  for(const episode of c.canonical.episodes){
    if(!object(episode)||!uuid(episode.id)||episodes.has(episode.id)||typeof episode.title!=='string'
      ||!Number.isSafeInteger(episode.number)||Number(episode.number)<1||!Array.isArray(episode.shots)||!episode.shots.length)throw Error('审核范围存在缺失或重复剧集')
    episodes.add(episode.id)
    for(const shot of episode.shots){
      if(!object(shot)||!uuid(shot.id)||shots.has(shot.id)||typeof shot.title!=='string'
        ||!Number.isSafeInteger(shot.number)||Number(shot.number)<1
        ||!Array.isArray(shot.reference_uses)
        ||['character_ids','visual_reference_character_ids','reference_asset_ids','reference_bindings'].some(key=>key in shot))throw Error('镜头用途缺失或含旧混合关系')
      shots.add(shot.id)
    }
  }
  const listed=new Set<string>(),usedAssets=new Set<string>()
  for(const row of raw.references){
    if(!object(row)||!uuid(row.shot_id)||!shots.has(row.shot_id)||listed.has(row.shot_id)||!Array.isArray(row.items))throw Error('逐镜参考清单不完整')
    listed.add(row.shot_id);const keys=new Set<string>()
    for(const item of row.items){
      if(!object(item)||typeof item.reference_key!=='string'||!item.reference_key||keys.has(item.reference_key)
        ||typeof item.source_key!=='string'||sources.get(item.source_key)!==item.kind||!object(item.source)
        ||item.source.kind!=='asset'||!uuid(item.source.asset_id)||!assets.has(item.source.asset_id)
        ||!(item.purpose===null||typeof item.purpose==='string')
        ||!(item.subject_ref===null||object(item.subject_ref)&&typeof item.subject_ref.kind==='string'&&uuid(item.subject_ref.id))
        ||!(item.paired_reference_key===null||typeof item.paired_reference_key==='string'))throw Error('参考素材用途或身份缺失')
      keys.add(item.reference_key)
      usedAssets.add(item.source.asset_id)
    }
    for(const item of row.items)if(item.paired_reference_key!==null&&!keys.has(item.paired_reference_key))throw Error('配套音频缺少对应视频')
  }
  if(listed.size!==shots.size)throw Error('必须包含本轮全部镜头，包括没有参考素材的镜头')
  if(usedAssets.size!==assets.size)throw Error('审核素材清单包含未被用途选定的附件')
  readStoryboardReviewDisplay(raw.display,[...shots])
  const detail=raw as unknown as ManualStoryboardReferenceReview
  for(const shot of detail.references)for(const item of shot.items){
    const preview=manualStoryboardReferencePreview(detail,shot.shot_id,item)
    if(preview.error)throw Error(preview.error)
  }
  return detail
}
export function buildManualStoryboardReferenceApproval(detail:ManualStoryboardReferenceReview,run:ProductionRun,options:ManualActionOptions,
  key:string,checked:string[],comment:string):JsonObject {
  readManualStoryboardReferences(detail,run)
  if(!detail.ready||!key.trim()||['succeeded','failed','cancelled'].includes(run.status)||options.runId!==run.id
    ||options.runPlanSha256!==detail.run_plan_sha256||!options.actions.some(row=>row.stageKey==='storyboard_review'&&row.action==='approve')
    ||checked.length!==detail.references.length||new Set(checked).size!==checked.length||detail.references.some(row=>!checked.includes(row.shot_id)))throw Error('请逐镜核对当前全部参考素材后再确认')
  return {action:'approve',stage_key:'storyboard_review',scope:{kind:'project',id:run.project},unit_key:`project:${run.project}`,
    expected_run_plan_sha256:detail.run_plan_sha256,expected_selection_revision:detail.approval.revision,idempotency_key:key,
    payload:{expected_contract_sha256:detail.contract_sha256,reviewed_shot_ids:[...checked].sort(),comment}}
}
