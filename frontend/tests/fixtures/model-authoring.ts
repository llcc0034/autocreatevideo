import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView} from 'vue-router'
import SettingsView from '../../src/views/SettingsView.vue'
import {useAuthStore} from '../../src/stores/auth'
import {platformApi} from '../../src/services/platformApi'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'
import {integerConstraintsFixture} from './textConstraints'
import {scriptContractFixture} from '../helpers/scriptContractFixture'
import {adaptScriptContracts} from '../../src/services/modelScriptContracts'
import {asyncMediaFixture} from '../helpers/asyncMediaFixture'
import {adaptAsyncMediaOptions} from '../../src/services/modelAsyncMedia'

const writes=ref(0),reads=ref(0),blocked=ref(0),mode=ref('success'),copy=(x:any)=>JSON.parse(JSON.stringify(x))
function deny():never {blocked.value++;throw Error('隔离夹具禁止真实网络、主机或模型调用')}
api.interceptors.request.use(()=>deny());api.defaults.adapter=async()=>deny()
window.fetch=async()=>deny();XMLHttpRequest.prototype.send=()=>deny();navigator.sendBeacon=()=>deny()
for(const name of Object.keys(platformApi))(platformApi as any)[name]=async()=>deny()
const provider:any={id:'provider-fixture',key:'fixture_connection',name:'隔离服务连接',driverType:'comfyui',driverConfig:{base_url:'http://example.invalid',timeout_seconds:30,poll_interval_seconds:2,max_poll_seconds:300,capability_ttl_seconds:300},isActive:true,modalities:['image'],operations:['image.generate'],updatedAt:'2026-10-09T00:00:00Z'}
const profile=(id:string,version:number,status:string)=>({id,profileKey:'fixture_model',version,status,provider:provider.id,modelIdentifier:'fixture-image',modality:'image',specification:{schema_version:1,model_family:'fixture_model',capabilities:['image.generate'],execution_monitoring:{schema_version:1}},createdAt:'2026-10-09T00:00:00Z',updatedAt:'2026-10-09T00:00:00Z'})
const profiles:any[]=[profile('model-old',1,'published'),profile('model-new',2,'published'),profile('model-draft',3,'draft')]
const providers:any[]=[provider],deployments:any[]=[{id:'deployment-fixture',key:'fixture_deployment',name:'隔离部署',profileVersion:'model-old',runtimeHost:null,runtimeConfig:{},status:'active',priority:0,isDefault:false,providerDriver:'comfyui',updatedAt:'2026-10-09T00:00:00Z'}]
const artifacts:any[]=[{id:'artifact-fixture',profileVersion:'model-draft',role:'checkpoint',logicalName:'fixture-model-file',filename:'fixture.safetensors',storageUri:'',inventoryFolder:'',inventoryNodeType:'',inventoryInputName:'',sha256:'',sizeBytes:0,status:'unverified'}]
const options:any={specificationSchemaVersion:1,modalities:[{value:'image',label:'图片'}],modelFamilies:['fixture_model'],capabilities:[{value:'image.generate',label:'图片生成',modalities:['image'],driverTypes:['comfyui'],legacy:false}],providerDrivers:[{value:'comfyui',label:'ComfyUI',modalities:['image'],operations:['image.generate']}],artifactRoles:[{value:'checkpoint',label:'主模型文件'}]}
const matrix=new URLSearchParams(location.search).has('matrix'),receipt=ref<unknown>(null)
const maintenanceParams=new URLSearchParams(location.search)
const maintenanceRoute=maintenanceParams.get('maintenance_route')||''
const resourcePaths=maintenanceParams.has('resources')
const hosts:any[]=resourcePaths?[{id:'host-a',key:'fixture_a',name:'隔离默认主机',status:'active'},{id:'host-b',key:'fixture_b',name:'隔离精确停用主机',status:'disabled'}]:[]
if(resourcePaths){deployments[0].runtimeHost='host-b';provider.credentialConfigured=false}
if(maintenanceParams.get('maintenance')==='1')document.addEventListener('click',event=>{
  const anchor=(event.target as Element)?.closest('a[target="_blank"]') as HTMLAnchorElement|null
  if(!anchor)return
  event.preventDefault()
  const target=new URL(anchor.href)
  const q=target.searchParams
  const modelTarget=q.get('section')==='models'&&(profiles.some(row=>row.id===q.get('model_profile_version'))||resourcePaths&&providers.some(row=>row.id===q.get('provider')))
  const hostTarget=resourcePaths&&q.get('section')==='runtime'&&hosts.some(row=>row.id===q.get('host'))
  if(target.origin!==location.origin||target.pathname!=='/settings'||!modelTarget&&!hostTarget)return deny()
  const isolated=new URL(location.href);isolated.searchParams.set('maintenance_route',target.pathname+target.search)
  window.open(isolated,'_blank','noopener')
})
if(matrix){
  options.scriptContracts=adaptScriptContracts(scriptContractFixture)
  profiles.splice(2,1);artifacts.splice(0)
  provider.driverConfig.pre_submit_resource_release={schema_version:1,enabled:true,unload_models:true,free_memory:true}
  deployments[0].runtimeConfig={timeout_seconds:45,pre_submit_resource_release:{schema_version:1,enabled:false,unload_models:false,free_memory:false}}
  const slots=['total_images','first_frame','last_frame','entity_images','reference_videos','paired_video_audios','reference_audios']
  options.conditioningContract={schemaVersion:1,adapterKey:'media_conditioning_v1',inputKinds:[{value:'classified_references',label:'多参考素材'}],inputSlots:slots.map(value=>({value,label:value})),maximumItems:32,maximumModes:10,identifierPattern:'^[A-Za-z][A-Za-z0-9_.-]{0,63}$',modeTemplate:{key:'',label:'',description:'',input_kind:'',requires_entity_reference_bible:false,supports_native_audio:false,inputs:Object.fromEntries(slots.map(slot=>[slot,{minimum_items:0,maximum_items:0}]))}}
  options.mediaConstraints={schemaVersion:1,resolutionFields:['width','height'].flatMap(axis=>['minimum','maximum'].map(bound=>({key:`${bound}_${axis}`,label:`${bound==='minimum'?'最小':'最大'}${axis==='width'?'宽度':'高度'}`,unit:'像素',minimum:1,maximum:2048}))),resolutionPairs:[['minimum_width','maximum_width'],['minimum_height','maximum_height']],fps:{minimum:1,maximum:60,maximumItems:4},durationMs:{minimum:1,maximum:60000}}
  const constraints=integerConstraintsFixture()
  options.textExecution={schemaVersion:1,integerConstraints:{valueFields:constraints.value_fields,seedPolicies:constraints.seed_policies},operations:[{value:'prompt.draft',label:'提示词起草'}],parameters:[{value:'seed',label:'随机种子',required:false,requireMaximum:false,template:{type:'integer',required:false}}]}
  options.chatRequestOptions={schemaVersion:1,temperatureMinimum:0,responseFormatTypes:[{value:'text',label:'文本'},{value:'json_object',label:'JSON对象'}],assemblyDescription:'业务输出结构由执行实例引用',boundaryDescription:'此处不复制输出字段'}
  options.modalities.push({value:'text',label:'文本'});options.modelFamilies.push('fixture_text')
  options.capabilities.push({value:'prompt.draft',label:'提示词起草',modalities:['text'],driverTypes:['openai_compatible'],legacy:false})
  options.providerDrivers.push({value:'openai_compatible',label:'文本服务',modalities:['text'],operations:['prompt.draft']})
  providers.push({...copy(provider),id:'provider-text',key:'fixture_text_connection',name:'隔离文本连接',driverType:'openai_compatible',driverConfig:{base_url:'http://example.invalid',chat_path:'/chat',timeout_seconds:30,capability_ttl_seconds:300},modalities:['text'],operations:['prompt.draft']})
  profiles.push({...profile('model-text',1,'published'),profileKey:'fixture_text',provider:'provider-text',modelIdentifier:'fixture-text',modality:'text',specification:{schema_version:1,model_family:'fixture_text',capabilities:['prompt.draft'],text_execution:{schema_version:1,operations:{'prompt.draft':{parameters:{seed:{type:'integer',required:false,minimum:0,maximum:1000,default:0}},chat_request_options:{temperature:0}}}}}})
  if(new URLSearchParams(location.search).has('extra')){
    options.asyncMedia=adaptAsyncMediaOptions(asyncMediaFixture)
    options.capabilities.push({value:'script.generate',label:'剧本生成',modalities:['text'],driverTypes:['openai_compatible'],legacy:false},{value:'video.text_to_video',label:'文生视频',modalities:['video'],driverTypes:['async_rest'],legacy:false})
    options.modalities.push({value:'video',label:'视频'})
    options.providerDrivers.push({value:'async_rest',label:'异步媒体服务',modalities:['video'],operations:['video.text_to_video']})
    providers.push({...copy(provider),id:'provider-async',key:'fixture_async_connection',name:'隔离异步连接',driverType:'async_rest',driverConfig:{request_field_map:{prompt:'text',width:'w',audio:'with_audio'}},modalities:['video'],operations:['video.text_to_video']})
    profiles.push({...profile('model-async',1,'published'),profileKey:'fixture_async',provider:'provider-async',modelIdentifier:'fixture-video',modality:'video',specification:{schema_version:1,model_family:'fixture_async',capabilities:['video.text_to_video'],async_media:{schema_version:1,operations:{'video.text_to_video':{output_kind:'video',request_schema:{type:'object',additionalProperties:false,required:['prompt','width'],properties:{prompt:{type:'string',minLength:1,maxLength:100},width:{type:'integer',minimum:64,maximum:512},audio:{type:'boolean'}}},smoke_inputs:{prompt:'fixture only',width:64,audio:false}}}}}})
    options.modelFamilies.push('fixture_async')
  }
}
const originals=JSON.stringify({providers,profiles:profiles.filter(row=>row.status==='published')})
const sourcesUnchanged=ref(true)
for(const name of ['listRuntimeHosts','listSecretBindings','listAcceptanceSmokeDeploymentRows','listDeploymentSmokeAttempts'])(platformApi as any)[name]=async()=>[]
if(resourcePaths){
  platformApi.listRuntimeHosts=async()=>copy(hosts)
  platformApi.getLatestRuntimeSnapshot=async()=>{throw{response:{status:404}}}
  platformApi.listRuntimeHostProbeAttempts=async()=>[]
  platformApi.resolveConfiguration=async()=>({values:{},sources:{}}) as any
}
platformApi.getAcceptanceSmokePage=async()=>({count:0,next:null,previous:null,results:[]}) as any
platformApi.listProviders=async()=>{reads.value++;return copy(providers)}
platformApi.listModelProfiles=async()=>{reads.value++;return copy(profiles)}
platformApi.getModelProfile=async id=>{reads.value++;const row=profiles.find(r=>r.id===id);if(!row)throw{response:{status:404}};return copy(row)}
platformApi.listDeployments=async()=>{reads.value++;return copy(deployments)}
platformApi.getDeployment=async id=>{reads.value++;const row=deployments.find(r=>r.id===id);if(!row)throw{response:{status:404}};return copy(row)}
platformApi.listModelArtifacts=async()=>{reads.value++;return copy(artifacts)}
platformApi.getModelProfileRegistryOptions=async()=>copy(options)
const map=(p:any)=>Object.fromEntries(Object.entries(p).map(([k,v])=>[({profile_key:'profileKey',model_identifier:'modelIdentifier',profile_version:'profileVersion',runtime_host:'runtimeHost',runtime_config:'runtimeConfig',driver_type:'driverType',driver_config:'driverConfig',is_active:'isActive',is_default:'isDefault',logical_name:'logicalName',storage_uri:'storageUri',size_bytes:'sizeBytes',inventory_folder:'inventoryFolder',inventory_node_type:'inventoryNodeType',inventory_input_name:'inventoryInputName'} as any)[k]||k,v]))
async function write(action:()=>any) {if(maintenanceRoute)return deny();writes.value++;await new Promise(r=>setTimeout(r,300));if(mode.value==='reject')throw{response:{status:400,data:{name:['隔离服务端明确拒绝此名称']}}};if(mode.value==='timeout-unwritten')throw Error('隔离响应未知');const row=action();receipt.value=copy(row);sourcesUnchanged.value=originals===JSON.stringify({providers,profiles:profiles.filter(row=>row.status==='published')});if(mode.value==='timeout-written')throw{response:{status:504}};return copy(row)}
for(const [create,update,rows] of [['createProvider','updateProvider',providers],['createDeployment','updateDeployment',deployments],['createModelArtifact','updateModelArtifact',artifacts]] as const){
  (platformApi as any)[create]=(payload:any)=>write(()=>{const row={id:'created-'+writes.value,...map(payload)};rows.push(row);return row})
  ;(platformApi as any)[update]=(id:string,payload:any)=>write(()=>{const row=rows.find(r=>r.id===id);if(!row)throw{response:{status:404}};Object.assign(row,map(payload));return row})
}
platformApi.createNextModelProfile=(id,payload)=>write(()=>{const source=profiles.find(r=>r.id===id);if(!source)throw{response:{status:404}};const row={...copy(source),...map(payload),id:'successor-'+writes.value,version:matrix?source.version+1:4+writes.value,status:'draft'};profiles.push(row);return row})
platformApi.createModelProfile=payload=>write(()=>{const row={...profile('created-'+writes.value,1,'draft'),...map(payload)};profiles.push(row);return row})
platformApi.updateModelProfile=(id,payload)=>write(()=>{const row=profiles.find(r=>r.id===id);if(!row||row.status!=='draft')throw{response:{status:400}};Object.assign(row,map(payload));return row})
api.get=async(url:string,request:any)=>{
  if(url==='/registry-relations/'){
    reads.value++
    const kind=request?.params?.kind,ids=String(request?.params?.ids||'').split(',')
    const rows=(kind==='model'?profiles:kind==='deployment'?deployments:[]).filter(r=>ids.includes(r.id))
    const results=rows.map(r=>({id:r.id,kind,label:r.profileKey? r.profileKey+' v'+r.version:r.name,status:r.status,config_url:'/settings?section=models&'+(kind==='model'?'model_profile_version=':'deployment=')+r.id,overview_url:'/settings/acceptance-smoke?view=relations&kind='+kind+'&ids='+r.id,acceptance_url:'',groups:[],notices:[],capabilities:[]}))
    return{data:{count:results.length,next:null,previous:null,results,generated_at:'2026-10-09T00:00:00Z',context_label:'隔离关系'}} as any
  }
  if(url==='/model-profile-versions/'){reads.value++;return{data:{count:profiles.length,next:null,previous:null,results:profiles.map(r=>({id:r.id,profile_key:r.profileKey,provider:r.provider,version:r.version,status:r.status,model_identifier:r.modelIdentifier,modality:r.modality,specification:r.specification}))}} as any}
  return deny()
}
const pinia=createPinia(),auth=useAuthStore(pinia);auth.user={id:'fixture-operator',isStaff:true,isSuperuser:true} as any
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/settings',component:SettingsView}]})
await router.push(maintenanceRoute||'/settings?section=models');await router.isReady()
createApp({setup:()=>()=>h('main',{style:'max-width:1400px;margin:auto;padding:12px;min-width:0'},[
h('h1',{style:'font-size:18px'},'模型与运行资源作者 · 隔离验证'),
h('p',{'data-fixture-evidence':'',role:'status'},`内存写入 ${writes.value} · 只读 ${reads.value} · 网络拦截 ${blocked.value} · 模型版本 ${profiles.length} · 不写现用库`),
h('p',{'data-source-evidence':''},`原发布模型与连接不变：${sourcesUnchanged.value}`),
h('details',[h('summary','最近内存保存回执（非服务端）'),h('pre',{style:'white-space:pre-wrap;overflow-wrap:anywhere'},JSON.stringify(receipt.value,null,2))]),
h('label',['保存结果场景',h('select',{value:mode.value,onChange:(e:Event)=>mode.value=(e.target as HTMLSelectElement).value},['success','reject','timeout-unwritten','timeout-written'].map(value=>h('option',{value},value)))]),h(RouterView)])}).use(pinia).use(router).mount('#app')

