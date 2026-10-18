// Real authoring components; no business HTTP, saves, uploads or model calls.
import {createApp,h,ref} from 'vue'
import ProductionRecipeRouteEditor from '../../src/components/ProductionRecipeRouteEditor.vue'
import {executionRegistryApi} from '../../src/services/executionRegistryApi'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'
api.interceptors.request.use(()=>{throw new Error('隔离夹具禁止真实业务API')})
const semantic={schema_version:1,allowed_purposes:[],allowed_subject_kinds:[],allowed_scope_kinds:['request'],paired_with_input_key:null,allow_repeated_asset:false}
const input=(key:string,kind:string,target:string)=>({key,kind,position:0,required:true,min_items:1,max_items:1,semantic_contract:{...semantic},targets:[{target_kind:target,target_key:key,is_primary:true}]})
const image={...input('primary_image','image','workflow_slot'),semantic_contract:{...semantic,allowed_purposes:['identity']},media_source_policy:{schema_version:1,allowed_sources:['asset_library'],default_source:'asset_library',allow_override:false,order:'provided'}}
const schema={type:'object',properties:{change:{type:'string'}},required:['change'],additionalProperties:false}
let failure=false
executionRegistryApi.contract=async()=>{
  if(failure)throw new Error('隔离目录读取失败，请重试')
  return {version_id:'fixture',revision:1,sha256:'a'.repeat(64),frozen:true,context_fields:[],input_forms:{},subject_categories:[{value:'character',label:'人物'}],
    appearance_inputs:{primary_inputs:[{input_key:'primary_image',target_key:'primary_image'}],difference_sources:[{kind:'structured_field',key:'details',field_key:'change'}]},
    language_form:{source:{required:false,fields:[]},target:{required:false,fields:[]},fixed_fields:[],choices:[],allow_custom:true},
    definition:{version_id:'fixture',configuration:{deployment_id:'fixture',workflow_version_id:'fixture',operation:'image.image_to_image',mode:'fixture',prompt_mode:'profile',prompt_profile_version_id:'fixture',inputs:[image,input('details','json','profile_input')],parameters:[],auxiliaries:[]},
      interface:{output:{contract:{primary:{kind:'image'}}},input_purposes:{image:[{value:'identity',label:'身份参考',description:'保持身份'}]},parameters:{},prompt:{mode:'profile',channels:{}},profile_inputs:{details:{value_schema:schema}}}}} as any
}
const choice=(value:string,label:string)=>({value,label})
const options:any={sourceKindOptions:[choice('material_primary','本轮确切已选主图'),choice('material_difference','当前已审核变体的差异'),choice('structured','按合同逐字段映射')],
  structureNodeOptions:[choice('object','按字段组成对象'),choice('literal','明确固定值'),choice('material_difference','当前已审核变体的差异')],textOutputOptions:[],parameterSourceOptions:[],parameterPolicyOptions:[],referenceSourceOptions:[],scopeOptions:[]}
createApp({setup(){
  const route=ref<any>({execution_instance_version_id:'fixture',expected_definition_sha256:'a'.repeat(64),input_sources:{primary_image:{kind:'material_primary',item_key:'main',purpose:'identity'},details:{kind:'structured',template:{kind:'object',fields:{change:{kind:'material_difference'}}}}},parameter_policies:[]})
  const role=ref('variant'),readonly=ref(false),revision=ref(0),issues=ref<string[]>([])
  return()=>h('main',{style:{maxWidth:'960px',margin:'16px auto',padding:'16px',background:'#101820',color:'#e6edf3'}},[
    h('h1','配方动态素材来源 · 隔离验收'),h('p','只验证正式编辑器本地草稿；没有保存、生成或业务API。混合变体派发尚未开放。'),
    h('button',{onClick:()=>role.value=role.value==='variant'?'primary':'variant'},role.value==='variant'?'切换主图路线':'切换变体路线'),
    h('button',{onClick:()=>readonly.value=!readonly.value},readonly.value?'解除只读':'设为只读'),
    h('button',{onClick:()=>{failure=!failure;revision.value++}},failure?'恢复目录':'模拟目录失败'),
    h(ProductionRecipeRouteEditor,{key:revision.value,modelValue:route.value,options,stageKey:'bible_image_generation',appearanceRole:role.value,priorStages:[{key:'bible_review'}],disabled:readonly.value,
      'onUpdate:modelValue':(value:any)=>route.value=value,onValidity:(value:string[])=>issues.value=value}),
    h('output',{id:'issues'},issues.value.join('；')||'本地草稿已配置（未保存）'),
    h('pre',{id:'draft',style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},JSON.stringify(route.value.input_sources,null,2))])
}}).mount('#app')
