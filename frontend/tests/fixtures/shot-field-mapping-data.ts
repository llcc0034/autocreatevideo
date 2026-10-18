import { recipeDefaultContract, recipeDefaultRoute, recipeDefaultOptions } from './recipe-default-data'

// Explicit isolated registry examples, not saved business configuration.
export const versionA='11111111-1111-4111-8111-111111111111', versionB='22222222-2222-4222-8222-222222222222'
export const definitionA='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', definitionB='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const field=(node:string,title:string,type:string,extra={})=>({type,title,'x-node-key':node,
  'x-field-definition':{field_definition_version_id:node==='tone'?definitionA:definitionB,key:node,version:1},...extra})
export const sourceSchema=field('root','原结构','object',{properties:{
  '情绪.原文':field('tone','情绪描述','string'),
  movement:field('movement','动作计划','object',{properties:{lines:field('lines','动作顺序','array',{items:field('line','动作原文','string')})}}),
}})
export const sourceSchemaB=field('root','另一确切版本','object',{properties:{tone:field('other-tone','情绪描述','string')}})
export const targetSchema={type:'object',title:'策略独立输入',properties:{tone:{type:'string',title:'情绪输入'},moves:{type:'array',title:'动作输入',items:{type:'string'}}},required:['tone','moves'],additionalProperties:false}
export const structures=[versionA,versionB].map((id,index)=>({id,structure_key:'isolated-shot',version:index+1,name:'隔离分镜结构',status:'published' as const,description:'隔离目录',nodes:[]}))
export function leaf(key='tone',version=versionA){return {kind:'shot_field',scope:{kind:'shot'},field:{structure_version_id:version,node_key:key,field_definition_version_id:key==='tone'?definitionA:definitionB}}}
export function contract(){const value:any=recipeDefaultContract();value.definition.configuration.operation='video.generate';value.definition.configuration.inputs[0].semantic_contract.allowed_scope_kinds=['shot'];value.input_forms.custom.schemas=[targetSchema];value.definition.interface.input_targets.payload.value_schema=targetSchema;return value}
export function route(){const value:any=recipeDefaultRoute();value.input_sources.custom.template={kind:'object',fields:{tone:leaf(),moves:{kind:'each',source:{...leaf('movement'),path:['lines']},item:{kind:'item_field',path:[]}}}};return value}
export const options={...recipeDefaultOptions,structureNodeOptions:[{value:'object',label:'按字段组成对象'},{value:'shot_field',label:'独立分镜字段'},{value:'context_field',label:'受控内容字段'},{value:'each',label:'逐条映射列表'},{value:'item_field',label:'当前条目字段'},{value:'literal',label:'固定值'}]}
