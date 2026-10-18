import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView} from 'vue-router'
import SettingsView from '../../src/views/SettingsView.vue'
import {useAuthStore} from '../../src/stores/auth'
import {platformApi} from '../../src/services/platformApi'
import {api} from '../../src/services/api'
import {adaptWorkflowProjectorOptions} from '../../src/services/platformAdapters'
import {rawOptions,definition} from './projector-authoring-data'
import {recommendation,recommendationSource} from './projector-recommendation-data'
import {referenceFixture} from './projector-reference-data'
import {scan} from './workflow-import-data'
import '../../src/styles.css'
import '../../src/platform.css'
const writes=ref(0),reads=ref(0),blocked=ref(0),recommendations=ref(0),lastSaved=ref<any>(null),mode=ref('success'),copy=(x:any)=>JSON.parse(JSON.stringify(x))
function deny():never{blocked.value++;throw Error('隔离夹具禁止真实网络、模型与主机调用')}
api.interceptors.request.use(()=>deny());api.defaults.adapter=async()=>deny();window.fetch=async()=>deny();XMLHttpRequest.prototype.send=()=>deny();navigator.sendBeacon=()=>deny()
for(const name of Object.keys(platformApi))(platformApi as any)[name]=async()=>deny()
const referenceKind=new URLSearchParams(location.search).get('reference')
const reference=referenceKind==='references'||referenceKind==='classified'?referenceFixture(referenceKind):null
const selectedOptions=reference?.options||copy(rawOptions)
const all:typeof rawOptions.definition_editor.sections[0]['fields']=selectedOptions.definition_editor.sections[0]!.fields
selectedOptions.definition_editor.sections=[
  {key:'public',label:'输入、参数与输出',description:'对外合同规则',fields:all.filter(f=>['output_nodes','parameter_policies','asset_slots','projection'].includes(f.path))},
  {key:'matching',label:'识别与内部实现',description:'节点和结构仅按需下钻',fields:all.filter(f=>!['output_nodes','parameter_policies','asset_slots','projection','$'].includes(f.path))},
  {key:'advanced',label:'高级复核 JSON',description:'同一份规则，不自动应用',fields:all.filter(f=>f.path==='$')},
]
const options=adaptWorkflowProjectorOptions(selectedOptions)
const family:any={id:'fixture-family',key:'fixture.projector',name:'隔离示例工作流方案',description:'内存测试数据',isActive:true}
const base:any={id:'fixture-v1',projector:family.id,projectorKey:family.key,version:1,status:'published',isCurrent:true,engineKey:'generic.fixed',engineVersion:1,providerDriverType:'comfyui',operation:'workflow.import',outputKind:'image',definition:{...copy(definition),projection:{kind:'generic_fixed',config:{capability:'image.text_to_image'}}},definitionSha256:'1'.repeat(64),definitionSizeBytes:620}
if(reference)Object.assign(base,{engineKey:reference.engineKey,outputKind:reference.outputKind,definition:copy(reference.definition)})
const families:any[]=[family],versions:any[]=[base]
const originalBase=JSON.stringify(base)
const read=(value:any)=>async()=>{reads.value++;return copy(value)}
for(const name of ['listWorkflowVersions','listModelProfiles','listProviders','listDeployments','listLegacyWorkflowTemplates','listRuntimeHostProbeAttempts'])(platformApi as any)[name]=read([])
platformApi.getModelProfileRegistryOptions=read({});platformApi.resolveConfiguration=read({values:{'workflow.comfyui_saved_workflow_directory':'workflows'},sources:{}})
platformApi.scanComfySavedWorkflows=read(scan([recommendationSource]))
platformApi.recommendWorkflowProjector=async()=>{if(reference)return deny();recommendations.value++;await new Promise(r=>setTimeout(r,200));return recommendation()}
if(reference){platformApi.listModelProfiles=read([reference.model]);platformApi.getModelProfile=async(id)=>{if(id!==reference.model.id)return deny();reads.value++;return copy(reference.model)}}
platformApi.listWorkflowProjectors=read(families);platformApi.listWorkflowProjectorVersions=read(versions);platformApi.getWorkflowProjectorOptions=read(options)
platformApi.getWorkflowProjectorVersion=async(id)=>{reads.value++;const row=versions.find(r=>r.id===id);if(!row)throw{response:{status:404}};return copy(row)}
async function write(kind:string,action:()=>any){writes.value++;await new Promise(r=>setTimeout(r,200));if(mode.value==='reject')throw{response:{status:400,data:{message:'隔离合同字段校验失败',fields:{'definition.parameter_policies':['隔离必需字段错误，请核对参数规则']}}}};const result=action();if(mode.value===kind+'-unknown')throw{response:{status:504}};return copy(result)}
platformApi.createWorkflowProjector=payload=>write('family',()=>{const row={id:'family-'+writes.value,key:payload.key,name:payload.name,description:payload.description,isActive:true};families.push(row);return row})
const dto=(payload:any)=>Object.fromEntries(Object.entries({engineKey:payload.engine_key,engineVersion:payload.engine_version,providerDriverType:payload.provider_driver_type,operation:payload.operation,outputKind:payload.output_kind,definition:payload.definition===undefined?undefined:copy(payload.definition)}).filter(([,value])=>value!==undefined))
platformApi.createWorkflowProjectorVersion=payload=>write('v1',()=>{lastSaved.value=copy(payload);const owner=families.find(f=>f.id===payload.projector);const row={...copy(base),id:'version-'+writes.value,projector:owner.id,projectorKey:owner.key,status:'draft',isCurrent:false,...dto(payload)};versions.push(row);return row})
platformApi.createNextWorkflowProjectorVersion=id=>write('clone',()=>{const source=versions.find(v=>v.id===id);const row={...copy(source),id:'successor-'+writes.value,version:source.version+1,status:'draft',isCurrent:false};versions.push(row);return row})
platformApi.updateWorkflowProjectorVersion=(id,payload)=>write('save',()=>{lastSaved.value=copy(payload);const row=versions.find(v=>v.id===id);Object.assign(row,dto(payload));return row})
api.get=async(url:string,request:any)=>{
  if(url==='/registry-relations/'){reads.value++;return{data:{count:0,next:null,previous:null,results:[],generated_at:'2026-10-09T00:00:00Z',context_label:'隔离关系'}} as any}
  return deny()
}
const pinia=createPinia(),auth=useAuthStore(pinia);auth.user={id:'fixture-operator',isStaff:true,isSuperuser:true} as any
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/settings',component:SettingsView}]})
await router.push('/settings?section=workflows');await router.isReady()
createApp({setup:()=>()=>h('main',{style:'max-width:1400px;margin:auto;padding:12px;min-width:0'},[
  h('h1',{style:'font-size:18px'},'Projector 新建、草稿与后继 · 隔离验证'),
  h('p',{'data-fixture-evidence':'',role:'status'},`内存写入 ${writes.value} · 只读 ${reads.value} · 网络拦截 ${blocked.value} · 不写现用库`),
  h('p',{'data-recommendation-evidence':''},`隔离推荐 ${recommendations.value} · 原发布版本${JSON.stringify(base)===originalBase?'不变':'被改动'}`),
  lastSaved.value&&h('details',[h('summary','隔离保存正文（只读证据）'),h('pre',{'data-saved-definition':'',style:'white-space:pre-wrap;overflow-wrap:anywhere'},JSON.stringify(lastSaved.value,null,2))]),
  h('label',['保存结果场景',h('select',{value:mode.value,onChange:(e:Event)=>mode.value=(e.target as HTMLSelectElement).value},['success','reject','family-unknown','v1-unknown','clone-unknown','save-unknown'].map(value=>h('option',{value},value)))]),h(RouterView),
])}).use(pinia).use(router).mount('#app')
