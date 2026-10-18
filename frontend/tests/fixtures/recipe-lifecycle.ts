import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory, RouterView, useRoute } from 'vue-router'
import FieldMaintenanceSettings from '../../src/components/FieldMaintenanceSettings.vue'
import { authorMaintenanceSession } from './author-maintenance-session'
import SettingsView from '../../src/views/SettingsView.vue'
import { useAuthStore } from '../../src/stores/auth'
import { platformApi } from '../../src/services/platformApi'
import { api } from '../../src/services/api'
import { recipeHierarchyOptions } from './recipe-hierarchy-data'
import { executionRegistryApi } from '../../src/services/executionRegistryApi'
import { businessOutputs } from '../../src/services/businessOutputs'
import { dataStructures } from '../../src/services/dataStructures'
import { complexDefinition, complexRecipeOptions, complexSourceOptions, complexOutput, complexSchema, complexStructure, complexStructureId, complexInputSchema, complexContract } from './recipe-complex-data'
import '../../src/styles.css'
import '../../src/platform.css'

// Actual SettingsView and its original writers, with in-memory API boundaries only.
const writes=ref(0),reads=ref(0),blocked=ref(0),mode=ref('success')
const copy=(x:any)=>JSON.parse(JSON.stringify(x))
const complex=new URLSearchParams(location.search).get('complex')==='1'
const definition=complex?complexDefinition():{schema_version:3,interaction_mode:'assisted',stages:[{key:'script_generate',mode:'manual',approval:'auto'},{key:'script_review',mode:'review',approval:'required'}]}
const source={id:'fixture-source',recipeKey:'fixture_family',version:1,name:'隔离发布来源',status:'published',definition:copy(definition),definitionSha256:'a'.repeat(64),definitionSchemaVersion:3,definitionSizeBytes:300}
const records:any[]=[source,{...copy(source),id:'fixture-draft',version:2,name:'隔离原草稿',status:'draft'}]
const sourceBefore=JSON.stringify(source),receipts=ref<any[]>([]),offline=ref(false)
function deny():never{blocked.value++;throw Error('隔离夹具禁止真实网络或其他操作')}
api.interceptors.request.use(()=>deny());api.defaults.adapter=async()=>deny()
window.fetch=async()=>deny();XMLHttpRequest.prototype.send=()=>deny();navigator.sendBeacon=()=>deny()
for(const client of [platformApi,executionRegistryApi,businessOutputs,dataStructures])for(const name of Object.keys(client)) (client as any)[name]=async()=>deny()
platformApi.listModelProfiles=platformApi.listDeployments=platformApi.listWorkflowVersions=async()=>[]
platformApi.listRecipes=async()=>{reads.value++;return copy(records)}
platformApi.getRecipe=async id=>{reads.value++;const row=records.find(row=>row.id===id);if(!row)throw {response:{status:404}};return copy(row)}
platformApi.getRecipeEditorOptions=async()=>{reads.value++;return copy(complex?complexRecipeOptions:{...recipeHierarchyOptions,presets:[{value:'fixture_manual',label:'隔离人工两步骤',stages:definition.stages}]})}
platformApi.getProductionReadiness=async()=>({status:'unconfigured',recipes:[],reasonCodes:[]}) as any
platformApi.getRecipeSourceOptions=async()=>{reads.value++;return complex?copy(complexSourceOptions):[]}
executionRegistryApi.contract=async id=>{reads.value++;if(id!=='fixture-structure')return deny();return copy(complexContract()) as any}
executionRegistryApi.version=async id=>{reads.value++;if(id!=='fixture-structure')return deny();return {id,instance:'fixture-execution',instance_key:'fixture-execution',instance_name:'隔离结构化制作计划',version:2,status:'published',operation:'prompt.fields.generate',prompt_mode:'none',configuration:complexContract().definition.configuration} as any}
executionRegistryApi.versions=async()=>{reads.value++;return {results:[await executionRegistryApi.version('fixture-structure')],count:1,next:null,previous:null}}
businessOutputs.detail=async id=>{reads.value++;if(offline.value)throw Error('隔离输出读取失败');if(id!==complexOutput.id)return deny();return copy(complexOutput) as any}
dataStructures.schema=async id=>{reads.value++;return {valid:true,json_schema:copy(id===complexStructureId?complexSchema:complexInputSchema),errors:{}} as any}
dataStructures.get=async id=>{reads.value++;return copy(complexStructure(id)) as any}
dataStructures.mappingOptions=async()=>{reads.value++;return {schema_version:1,nodes:['object','path','literal'].map(value=>({value,label:value})),scopes:[{value:'root',label:'整个来源'},{value:'item',label:'当前条目'}]}}
api.get=async(url:string,request:any)=>{
  if(url!=='/registry-relations/')return deny()
  reads.value++
  const ids=String(request?.params?.ids||'').split(',').filter(Boolean),kind=request?.params?.kind
  const rows=ids.map(id=>({id,kind,label:`隔离${kind}精确资源`,status:'published',groups:[],notices:[],capabilities:[],config_url:`/settings?section=${kind==='structure'?'fields':'executions'}&${kind==='execution'?'execution_version':kind==='output'?'output_version':'structure_version'}=${id}`,overview_url:`/settings/acceptance-smoke?view=relations&kind=${kind}&ids=${id}`,acceptance_url:''}))
  return {data:{count:rows.length,next:null,previous:null,results:rows,generated_at:'2026-10-10T00:00:00+08:00',context_label:'隔离关系'}} as any
}
async function write(action:()=>any,receipt:unknown){
  writes.value++;const scenario=mode.value
  receipts.value.push({attempt:writes.value,scenario,request:copy(receipt)})
  await new Promise(resolve=>setTimeout(resolve,350))
  if(scenario==='reject')throw {response:{status:400,data:{name:['隔离服务端明确拒绝，请调整名称']}}}
  if(scenario==='timeout-unwritten')throw Error('隔离请求超时，结果未知')
  const row=action()
  if(scenario==='timeout-written')throw {response:{status:504}}
  return copy(row)
}
platformApi.createRecipe=(payload)=>write(()=>{
  const row={...copy(source),id:`fixture-created-${writes.value}`,recipeKey:payload.recipe_key,version:1,name:payload.name,definition:copy(payload.definition),status:'draft'}
  records.push(row);return row
},{action:'create',payload})
platformApi.updateRecipe=(id,payload)=>write(()=>{
  const row=records.find(row=>row.id===id)
  if(!row||row.status!=='draft')throw {response:{status:400}}
  Object.assign(row,copy(payload));return row
},{action:'patch',id,payload})
platformApi.cloneRecipeSuccessor=(id,sha)=>write(()=>{
  const original=records.find(row=>row.id===id)
  if(!original||original.definitionSha256!==sha)throw {response:{status:409}}
  const row={...copy(original),id:`fixture-successor-${writes.value}`,version:1+Math.max(...records.filter(row=>row.recipeKey===original.recipeKey).map(row=>row.version)),status:'draft'}
  records.push(row);return row
},{action:'clone',id,sha})
const pinia=createPinia(),auth=useAuthStore(pinia)
auth.user={id:'fixture-operator',isStaff:true,isSuperuser:true} as any
const maintenance=authorMaintenanceSession(complexStructure(complexStructureId),complexSchema,()=>reads.value++,()=>writes.value++,deny)
const Maintenance={setup(){const route=useRoute();return()=>h(FieldMaintenanceSettings,{structureVersionId:String(route.query.structure_version||'')})}}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/settings',component:maintenance?.route?Maintenance:SettingsView},{path:'/settings/acceptance-smoke',name:'settings-acceptance-smoke',component:{render:()=>h('p','隔离只读关系占位')}},{path:'/workflows',name:'workflows',component:{render:()=>h('p','隔离不执行任务')}},{path:'/manual/settings',component:{render:()=>h('p','隔离指南')}}]})
await router.push(maintenance?.route||'/settings?section=routing');await router.isReady()
createApp({setup:()=>()=>h('main',{style:'max-width:1400px;margin:auto;padding:12px;min-width:0'},[
  h('h1',{style:'font-size:18px'},'配方完整作者 · 隔离验证'),
  h('p',{'data-fixture-evidence':'',role:'status'},`内存写入 ${writes.value} · 只读 ${reads.value} · 网络拦截 ${blocked.value} · 记录 ${records.length} · 不写现用库`),
  h('label',['保存结果场景',h('select',{value:mode.value,onChange:(e:Event)=>{mode.value=(e.target as HTMLSelectElement).value}},['success','reject','timeout-unwritten','timeout-written'].map(value=>h('option',{value},value)))]),
  h('label',[h('input',{type:'checkbox',checked:offline.value,onChange:(e:Event)=>{offline.value=(e.target as HTMLInputElement).checked}}),'模拟输出读取失败']),
  h(RouterView),
  ...(maintenance?[h('details',[h('summary','跨维护隔离目录回读'),h('pre',{'data-maintenance-receipts':''},JSON.stringify(maintenance.report(),null,2))])]:[]),
  h('details',[h('summary','隔离写入与原来源核对'),h('pre',{'data-recipe-receipts':'',style:'white-space:pre-wrap;overflow-wrap:anywhere'},JSON.stringify({sourceUnchanged:JSON.stringify(source)===sourceBefore,records,receipts:receipts.value},null,2))]),
])}).use(pinia).use(router).mount('#app')
