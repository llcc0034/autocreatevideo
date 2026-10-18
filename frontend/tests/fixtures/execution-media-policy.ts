// Isolated formal controls: all I/O replaced here, no business or model requests.
import {createApp,h,ref,computed} from 'vue'
import ExecutionMediaSourcePolicyEditor from '../../src/components/ExecutionMediaSourcePolicyEditor.vue'
import ExecutionRequestInputEditor from '../../src/components/ExecutionRequestInputEditor.vue'
import {inputSourcePolicies,type InputSourceCatalog} from '../../src/services/inputSourcePolicies'
import {emptyExecutionRequestDraft,buildExecutionRequestFixture,type ExecutionRequestFormContract} from '../../src/services/executionRequestForm'
import type {ExecutionInput} from '../../src/services/executionRegistry'
import {repository} from '../../src/services/repository'
import {api} from '../../src/services/api'
import type {Asset} from '../../src/types'
import '../../src/styles.css'
import '../../src/platform.css'

api.interceptors.request.use(()=>{throw new Error('隔离夹具禁止真实API请求')})
const catalog:InputSourceCatalog={schema_version:1,configuration_owner:'instance',description:'隔离目录',orders:[{value:'provided',label:'按明确提供的顺序'}],sources:[
  {value:'asset_library',label:'素材库已有素材',kinds:['image'],available:true,reader:'asset',requires:['owner'],description:'确切素材'},
  {value:'asset_upload',label:'本地上传后入库',kinds:['image'],available:true,reader:'asset',requires:['owner'],description:'原上传事实'},
  {value:'selected_output',label:'上游已选择产物',kinds:['image'],available:true,reader:'selected_output',requires:['production_run'],description:'确切产物'},
  {value:'bible_reference',label:'Bible主图／变体素材版本',kinds:['image'],available:false,reader:null,requires:['asset_reference_version'],description:'M9/M5尚未接入'},
]}
inputSourcePolicies.options=async()=>catalog
const asset={id:'12345678-1234-1234-1234-123456789012',name:'隔离参考图',isActive:true,checksum:'a'.repeat(64),mimeType:'image/png',metadata:{}} as Asset
repository.getAssetPage=async()=>({count:1,next:null,previous:null,results:[asset]})
repository.createAsset=async()=>{throw new Error('隔离夹具不执行上传')}
createApp({setup(){
  const spec=ref<ExecutionInput>({key:'reference_images',kind:'image',position:0,required:true,min_items:1,max_items:2,
    media_source_policy:{schema_version:1,allowed_sources:['asset_library','asset_upload','selected_output'],default_source:'asset_library',allow_override:true,order:'provided'},
    semantic_contract:{schema_version:1,allowed_purposes:[],allowed_subject_kinds:[],allowed_scope_kinds:['request'],paired_with_input_key:null,allow_repeated_asset:false},
    targets:[{target_kind:'workflow_slot',target_key:'images',is_primary:true}]})
  const contract=computed<ExecutionRequestFormContract>(()=>({versionId:'fixture',sha256:'a'.repeat(64),operation:'video.reference_to_video',mode:'fixture',outputKind:'video',workflowVersionId:'fixture',inputs:[spec.value],
    parameters:[],channels:[],promptMode:'none',serverOwnedInputs:[],contextFields:[],sourceConstraints:{},purposes:{image:[]},subjectCategories:[{value:'character',label:'角色'}],
    languageForm:{source:{required:false,fields:[]},target:{required:false,fields:[]},fixed_fields:[],choices:[],allow_custom:true}}))
  const draft=ref(emptyExecutionRequestDraft(contract.value)),status=ref(''),readonly=ref(false),busy=ref(false)
  function preview(){try{status.value='仅本地构造请求（未提交）：'+JSON.stringify(buildExecutionRequestFixture(contract.value,draft.value))}catch(cause){status.value=String(cause)}}
  return()=>h('main',{style:{maxWidth:'960px',margin:'20px auto',padding:'16px',background:'#101820',color:'#e6edf3'}},[
    h('h1','M1-08 实例媒体来源隔离验证'),h('p','正式组件 + 隔离目录，不保存配置、不上传文件、不创建准备或调用模型。数量固定1–2；用途未声明。'),
    h('button',{class:'button ghost small',onClick:()=>readonly.value=!readonly.value},readonly.value?'切换可编辑':'切换只读'),
    h(ExecutionMediaSourcePolicyEditor,{input:spec.value,purposes:[],disabled:readonly.value,'onUpdate:policy':(policy:ExecutionInput['media_source_policy'])=>{spec.value={...spec.value,media_source_policy:policy};draft.value=emptyExecutionRequestDraft(contract.value);status.value='配置草稿已改变；此隔离夹具重新开始填写本次请求'}}),
    h('h2','本次请求（不提交）'),h(ExecutionRequestInputEditor,{contract:contract.value,modelValue:draft.value,disabled:readonly.value,'onUpdate:modelValue':(value:typeof draft.value)=>draft.value=value,onBusy:(value:boolean)=>busy.value=value}),
    h('button',{class:'button primary small',disabled:busy.value,onClick:preview},'预览请求形状'),h('output',{role:'status',style:{display:'block',overflowWrap:'anywhere'}},status.value),
  ])
}}).mount('#app')
