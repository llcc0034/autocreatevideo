import {rawOptions,definition} from './projector-authoring-data.ts'

// Memory-only author fixtures. These graphs are not runtime/compiler acceptance evidence.
const copy=(value:any)=>JSON.parse(JSON.stringify(value))
const field=(path:string,control:string)=>({path,control,label:path,help:'隔离目录中的配置项',required:true,read_only:false})
export function referenceFixture(kind:'references'|'classified') {
  const options:any=copy(rawOptions), classified=kind==='classified'
  const engineKey=`generic.${kind}`, projectionKind=`generic_${kind}`
  const adapters={compiler:classified?'generic.comfy/v3':'generic.comfy/v2',output:'comfy.primary/v1',dynamic:classified?'generic.classified_references/v1':'generic.asset_groups/v1'}
  const keys=['images','videos','video_audios','audios'],media=['image','video','audio','audio'],inputs=['entity_images','reference_videos','paired_video_audios','reference_audios']
  const draft:any={...copy(definition),adapters,node_roles:{loader:{node_type:'FixtureLoad',cardinality:'one'},receiver:{node_type:'FixtureReceive',cardinality:'one'},merge:{node_type:'FixtureMerge',cardinality:'one'}},
    asset_slots:(classified?keys:['ordered','merged']).map((key,index)=>({key,media_kind:classified?media[index]:'image',dynamic:true,contract_source:index===1?'model_profile':'workflow'})),
    projection:{kind:projectionKind,config:classified?{
      capability:'video.reference_to_video',conditioning_mode:'FixtureMode',target_role:'receiver',prompt_template_keys:['fixture.reference'],static_semantics:{'audio.enabled':false},
      dynamic_groups:Object.fromEntries(keys.map((key,index)=>[key,{prefix:`${key}.`,input_names:[],mode_input:inputs[index],minimum_items:index===1?null:0,maximum_items:index===1?null:2}]))
    }:{capability:'image.image_to_image',groups:['ordered','merged'].map((key,index)=>({key,source_role:'loader',branch_roles:['loader'],output_role:'loader',output_index:0,target_roles:['receiver'],input_names:index?['combined']:['ref_0','ref_1'],wiring:index?'reduce':'ordered_ports',reducer_role:index?'merge':'',reducer_inputs:index?['left','right']:[],reducer_output_index:0,minimum_items:index?null:0,maximum_items:index?null:2,conditioning_mode:index?'FixtureMode':'',model_input:index?'total_images':''}))}}
  }
  if(classified)draft.output_nodes={FixtureSaveVideo:'video'}
  const blank:any=copy(options.engines[0].creation_template)
  blank.definition.adapters=adapters
  blank.definition.projection={kind:projectionKind,config:classified?{capability:'',conditioning_mode:'',target_role:'',prompt_template_keys:[],static_semantics:{},dynamic_groups:Object.fromEntries(keys.map((key,index)=>[key,{prefix:'',input_names:[],mode_input:inputs[index],minimum_items:null,maximum_items:null}]))}:{groups:[]}}
  options.engines=[{...options.engines[0],key:engineKey,label:classified?'通用分类参考素材接线':'通用动态多素材接线',projection_kind:projectionKind,adapter_contract:adapters,creation_template:blank,projection_fields:classified?[field('projection.config','classified_reference_config')]:[field('projection.config.capability','execution_capability'),field('projection.config.groups','asset_group_list')]}]
  for(const [key,value] of Object.entries(adapters))options.adapters[key]=[{value,label:value}]
  const choices=options.definition_editor.choices
  choices.media_kinds=Array.from(new Set(media)).map(value=>({value,label:value}))
  choices.contract_sources=[{value:'workflow',label:'Projector 中配置'},{value:'model_profile',label:'模型版本合同'}]
  choices.asset_group_wirings=[{value:'ordered_ports',label:'顺序端口'},{value:'reduce',label:'逐项合并'}]
  choices.model_quantity_inputs=[{value:'total_images',label:'图片总数',media_kind:'image'},...inputs.map((value,index)=>({value,label:value,media_kind:media[index]}))]
  choices.execution_capabilities.push({value:'video.reference_to_video',label:'参考生视频'})
  choices.output_kinds.push({value:'video',label:'视频'})
  options.output_kinds.push({value:'video',label:'视频'})
  const model:any={id:'fixture-reference-model',profileKey:'fixture-reference',version:7,status:'published',name:'隔离参考模型',description:'仅内存作者验证',provider:'fixture-provider',providerKey:'fixture-provider',modelIdentifier:'fixture-reference',modality:'video',publishedAt:'2026-10-10T00:00:00Z',publishedBy:'fixture-operator',specificationSchemaVersion:1,specificationSizeBytes:600,specificationSha256:'7'.repeat(64),specification:{capabilities:['video.reference_to_video','image.image_to_image'],conditioning_contract:{modes:[{key:'FixtureMode',label:'隔离参考模式',input_kind:'classified_references',inputs:Object.fromEntries([...inputs,'total_images'].map(key=>[key,{minimum_items:0,maximum_items:4}]))}]}}}
  return {options,definition:draft,engineKey,outputKind:classified?'video':'image',model}
}
