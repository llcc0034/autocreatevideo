import { recipeHierarchyOptions } from './recipe-hierarchy-data'
import { recipeDefaultContract, recipeDefaultRoute, recipeDefaultSchema } from './recipe-default-data'

// Representative service responses for UI acceptance, not live contracts or media.
export const complexOutputId='00000000-0000-4000-8000-000000000101'
export const complexStructureId='00000000-0000-4000-8000-000000000102'
export const complexSha='c'.repeat(64)
const integer={type:'integer',minimum:1}
const episode=(shot:unknown)=>({type:'object',required:['episodes'],additionalProperties:false,properties:{episodes:{type:'array',minItems:1,items:{type:'object',required:['number','shots'],additionalProperties:false,properties:{number:integer,shots:{type:'array',minItems:1,items:shot}}}}}})
export const complexSchemas={
  geometry:{type:'object',required:['width','height','fps'],additionalProperties:false,properties:{width:integer,height:integer,fps:integer}},
  timing:episode({type:'object',required:['number','duration_ms','episode_start_ms','episode_end_ms'],additionalProperties:false,properties:{number:integer,duration_ms:integer,episode_start_ms:{type:['integer','null'],minimum:0},episode_end_ms:{type:['integer','null'],minimum:0}}}),
  media:episode({type:'object',required:['number','artifact_id'],additionalProperties:false,properties:{number:integer,artifact_id:{type:'string',format:'uuid'}}}),
  delivery:{type:'object',required:['title','notes'],additionalProperties:false,properties:{title:{type:'string'},notes:{type:'string'}}},
}
export const complexRaw=' {"geometry":{"width":640,"height":360,"fps":30},"timing":{"episodes":[{"number":1,"shots":[{"number":1,"duration_ms":3000,"episode_start_ms":null,"episode_end_ms":null}]}]},"media":{"episodes":[{"number":1,"shots":[{"number":1,"artifact_id":"00000000-0000-4000-8000-000000000103"}]}]},"delivery":{"title":"  雨夜 🌧  ","notes":"原文\\r\\nΔ"},"extra":false} '
export const complexSchema={type:'object',required:Object.keys(complexSchemas),additionalProperties:true,properties:complexSchemas}
export const complexOutput={id:complexOutputId,output_key:'fixture-plan',name:'隔离制作计划输出',version:3,status:'published',revision:2,description:'隔离UI公共定义，无实际生成',parser_key:'structured_json.v1',parser_contract:{key:'structured_json.v1',format:'json'},data_structure_version:complexStructureId,contract_sha256:complexSha,quality_advisories:{}}
export function complexStructure(id:string){return {id,structure_key:'fixture-plan',name:id===complexStructureId?'隔离制作计划结构':'隔离镜头数量结构',version:2,status:'published',description:'字段定义，不是业务值',nodes:[{node_key:'root',parent_key:null,position:0,property_name:'制作计划',field_definition_version:null,required:true,allow_null:false,allow_blank:false,allow_empty:false,has_default:false,constraints:{}}]}}
export function complexContract(){const value=recipeDefaultContract();return {...value,definition:{...value.definition,configuration:{...value.definition.configuration,output_version_id:complexOutputId},output_contract:{output_version_id:complexOutputId,expected_contract_sha256:complexSha}}}}
export const complexInputSchema=recipeDefaultSchema
export const complexSourceOptions=['plan_a','plan_b'].map((stageKey,index)=>({stageKey,outputKey:`structured_text.${complexOutputId}`,kind:'json',scopeKind:'project',label:`${index+1} · ${stageKey} 已采用制作计划`,outputVersionId:complexOutputId,outputContractSha256:complexSha,structureVersionId:complexStructureId,valueSchema:complexSchema}))
export function complexRule(key:keyof typeof complexSchemas,upstream=false){return {output_version_id:complexOutputId,expected_contract_sha256:complexSha,mapping:{schema_version:1,node:{kind:'path',scope:'root',path:[key]}},...(upstream?{source:{kind:'selected_output',stage_key:'plan_b',output_key:`structured_text.${complexOutputId}`,scope:{kind:'project'},selection_policy:'reviewed_selected'}}:{source_text:complexRaw})}}
export function complexDefinition(){return {schema_version:3,interaction_mode:'assisted',stages:[
  ...['plan_a','plan_b'].map((key,index)=>({key,kind:'custom_text',name:`制作计划 ${index+1}`,mode:'ai',approval:'auto',routing:{mode:'fixed',variant_count:2,evaluator:null,routes:[{...recipeDefaultRoute(),input_sources:{custom:{kind:'structured',template:{kind:'object',fields:{count:{kind:'literal',value:2}}}}}}]}})),
  {key:'assembly',mode:'existing_pipeline',approval:'auto',assembly_output:complexRule('geometry'),assembly_timing:complexRule('timing',true),assembly_media:complexRule('media')},
  {key:'delivery',mode:'existing_pipeline',approval:'auto',delivery_metadata:complexRule('delivery',true)},
]}}
export const complexRecipeOptions={...recipeHierarchyOptions,
  stageOptions:[...recipeHierarchyOptions.stageOptions,...['assembly','delivery'].map(value=>({value,label:value==='assembly'?'成片装配':'交付封存',repeatable:false,modes:['existing_pipeline','manual'],routingModes:[],candidateEvaluatorOptions:[],decisionPolicy:null}))],
  assemblyOutputSchema:complexSchemas.geometry,assemblyTimingSchema:complexSchemas.timing,assemblyMediaSchema:complexSchemas.media,deliveryMetadataSchema:complexSchemas.delivery,
  presets:[{value:'fixture_complex',label:'隔离生成与装配交付',stages:complexDefinition().stages}],
}
