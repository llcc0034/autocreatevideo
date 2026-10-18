// Formal route/editor controls with isolated catalogs; no business writes.
import {createApp,h,ref} from 'vue'
import ProductionRecipeRouteEditor from '../../src/components/ProductionRecipeRouteEditor.vue'
import {executionRegistryApi} from '../../src/services/executionRegistryApi'
import {inputSourcePolicies} from '../../src/services/inputSourcePolicies'
import {repository} from '../../src/services/repository'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'

api.interceptors.request.use(()=>{throw new Error('隔离夹具禁止真实API请求')})
const spec={key:'images',position:0,kind:'image',required:true,min_items:1,max_items:2,
  media_source_policy:{schema_version:1,allowed_sources:['asset_library','asset_upload','selected_output'],default_source:'asset_library',allow_override:true,order:'provided'},
  semantic_contract:{schema_version:1,allowed_purposes:[],allowed_subject_kinds:[],allowed_scope_kinds:['request'],paired_with_input_key:null,allow_repeated_asset:false},
  targets:[{target_kind:'workflow_slot',target_key:'images',is_primary:true}]}
executionRegistryApi.contract=async()=>({version_id:'fixture',revision:1,sha256:'a'.repeat(64),frozen:true,context_fields:[],input_forms:{},subject_categories:[{value:'character',label:'人物'}],
  language_form:{source:{required:false,fields:[]},target:{required:false,fields:[]},fixed_fields:[],choices:[],allow_custom:true},definition:{version_id:'fixture',
  configuration:{deployment_id:'fixture',workflow_version_id:'fixture',operation:'video.reference_to_video',mode:'fixture',prompt_mode:'none',prompt_profile_version_id:null,inputs:[spec],parameters:[],auxiliaries:[]},
  interface:{output:{contract:{primary:{kind:'video'}}},input_purposes:{image:[]},parameters:{},prompt:{mode:'none',channels:{}}}}} as any)
inputSourcePolicies.options=async()=>({schema_version:1,configuration_owner:'instance',description:'隔离目录',orders:[{value:'provided',label:'原顺序'}],sources:[
  {value:'asset_library',label:'素材库已有素材',available:true},{value:'asset_upload',label:'上传本地文件后入库',available:true},{value:'selected_output',label:'上游已选择产物',available:true},
  {value:'bible_reference',label:'Bible主图／变体版本',available:false}].map(row=>({...row,kinds:['image'],reader:'asset',requires:[],description:'隔离目录'}))})
const asset={id:'12345678-1234-1234-1234-123456789012',name:'隔离图片甲',mimeType:'image/png',isActive:true,checksum:'a'.repeat(64),metadata:{}} as any
repository.getAssetPage=async()=>({count:1,next:null,previous:null,results:[asset]});repository.getAsset=async()=>asset
repository.createAsset=async()=>{throw new Error('隔离夹具不上传')}
const options={sourceKindOptions:[{value:'media_selection',label:'固定素材（素材库／上传入库）'},{value:'reviewed_references',label:'当前镜头已审核参考清单'}],textOutputOptions:[],parameterSourceOptions:[],referenceSourceOptions:[],parameterPolicyOptions:[],scopeOptions:[],structureNodeOptions:[]} as any
createApp({setup(){
  const route=ref({execution_instance_version_id:'fixture',expected_definition_sha256:'a'.repeat(64),input_sources:{images:{kind:'media_selection',selection:{source:'asset_library',items:[]}}},parameter_policies:[]})
  const readonly=ref(false),issues=ref<string[]>([])
  return()=>h('main',{style:{maxWidth:'960px',padding:'16px',margin:'20px auto',background:'#101820',color:'#e6edf3'}},[
    h('h1','M1-08 配方固定素材隔离验证'),h('p','正式配方路线编辑器；只有本地草稿，不保存、不上传、不生成。'),
    h('button',{class:'button ghost small',onClick:()=>readonly.value=!readonly.value},readonly.value?'切换可编辑':'切换只读'),
    h(ProductionRecipeRouteEditor,{modelValue:route.value,options,priorStages:[],stageKey:'media_generation',disabled:readonly.value,
      'onUpdate:modelValue':(value:any)=>route.value=value,onValidity:(values:string[])=>issues.value=values}),
    h('output',{role:'status'},issues.value.length?issues.value.join('；'):'本地输入有效（未保存）'),
    h('pre',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},JSON.stringify(route.value,null,2)),
  ])
}}).mount('#app')
