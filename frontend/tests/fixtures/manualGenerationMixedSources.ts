// Isolated contract/review examples, never production approvals or media.
import {contract, id, input, review, sha} from './manualGenerationShotSources.ts'

export function mixedFixture() {
  const detail = structuredClone(review)
  const uses = [
    {key:'picture', kind:'image', asset:6, description:'外观，不是首帧', mode:'upload'},
    {key:'video-a', kind:'video', asset:7, description:'走位参考', mode:'existing'},
    {key:'video-b', kind:'video', asset:8, description:'运镜参考', mode:'existing'},
    {key:'audio-sync', kind:'audio', asset:9, description:'配套音轨', mode:'existing'},
    {key:'audio-ambient', kind:'audio', asset:9, description:'同文件独立环境参考', mode:'existing'},
  ]
  detail.source_options = ['image','video','audio'].map(kind=>({value:kind,label:kind,kind}))
  detail.contract.canonical.episodes[0].shots[0].reference_uses = uses.map(row=>({use_key:row.key,
    media_kind:row.kind,purpose:row.description,source_mode:row.mode,reference_version_id:null,asset_id:id(row.asset),asset_sha256:sha}))
  detail.contract.assets = uses.filter((row,index,rows)=>rows.findIndex(other=>other.asset===row.asset)===index)
    .map(row=>({id:id(row.asset),name:row.key,kind:'reference',mime_type:{image:'image/png',video:'video/mp4',audio:'audio/wav'}[row.kind],checksum:sha}))
  detail.references[0].items = uses.map(row=>({reference_key:`shot-use:${row.key}`,source_key:row.kind,kind:row.kind,
    source:{kind:'asset',asset_id:id(row.asset)},purpose:row.description,subject_ref:null,paired_reference_key:null}))
  const specs = [
    {key:'pictures',kind:'image',max:1,parent:null,purposes:[]},
    {key:'clips',kind:'video',max:2,parent:null,purposes:['motion_camera_reference']},
    {key:'paired',kind:'audio',max:1,parent:'clips',purposes:['audio_reference']},
    {key:'ambient',kind:'audio',max:1,parent:null,purposes:['audio_reference']},
  ].map((row,position)=>({...structuredClone(input),key:row.key,kind:row.kind,position,max_items:row.max,
    semantic_contract:{...structuredClone(input.semantic_contract),paired_with_input_key:row.parent,allowed_purposes:row.purposes},
    targets:[{target_kind:'workflow_slot',target_key:row.key,is_primary:true}]}))
  const value = {...structuredClone(contract),inputs:specs}
  value.inputs[0].media_source_policy = {schema_version:1,allowed_sources:['asset_upload'],default_source:'asset_upload',allow_override:false,order:'provided'}
  return {review:detail,contract:value}
}
