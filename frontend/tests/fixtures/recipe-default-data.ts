import {unusedLanguageForm} from './executionLanguage'

export const recipeDefaultSchema={type:'object',properties:{count:{type:'integer',title:'镜头数量',default:2,minimum:1}},required:['count'],additionalProperties:false}
export const recipeDefaultOptions={sourceKindOptions:[{value:'structured',label:'字段装配'}],structureNodeOptions:[
  {value:'object',label:'按字段组成对象'},{value:'literal',label:'明确固定值'}],textOutputOptions:[],parameterSourceOptions:[],parameterPolicyOptions:[],scopeOptions:[],referenceSourceOptions:[]}
export function recipeDefaultContract(withStructure=true){
  const version_id=withStructure?'fixture-structure':'fixture-native'
  return {version_id,sha256:(withStructure?'a':'b').repeat(64),revision:1,frozen:true,context_fields:[],
    subject_categories:[{value:'character',label:'人物'},{value:'scene',label:'场景'},{value:'prop',label:'道具'}],language_form:unusedLanguageForm(),input_forms:{custom:{label:'自定义镜头计划',description:'',suggestions:[],schemas:[recipeDefaultSchema]}},
    definition:{version_id,configuration:{deployment_id:'fixture',operation:'prompt.fields.generate',mode:'',workflow_version_id:null,prompt_mode:'none',prompt_profile_version_id:null,
      inputs:[{key:'custom',position:0,kind:'json',required:true,min_items:1,max_items:1,...(withStructure?{data_structure_version_id:'fixture-exact-v1'}:{}),
        semantic_contract:{schema_version:1,allowed_purposes:[],allowed_subject_kinds:[],allowed_scope_kinds:['request'],paired_with_input_key:null,allow_repeated_asset:false},
        targets:[{target_kind:'operation_input',target_key:'payload',is_primary:true}]}],parameters:[],auxiliaries:[]},
      interface:{output:{kind:'text'},input_purposes:{},parameters:{},prompt:{mode:'none',channels:{}},
        input_targets:{payload:{kind:'json',value_schema:{type:'object',required:['count'],properties:{count:{type:'integer',minimum:1}},additionalProperties:false}}}}}}
}
export function recipeDefaultRoute(withStructure=true){const contract=recipeDefaultContract(withStructure);return{
  execution_instance_version_id:contract.version_id,expected_definition_sha256:contract.sha256,
  input_sources:{custom:{kind:'structured',template:{kind:'object',fields:{}}}},parameter_policies:[]}}
