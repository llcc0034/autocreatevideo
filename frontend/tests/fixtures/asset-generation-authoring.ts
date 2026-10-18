import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView} from 'vue-router'
import AssetLibraryView from '../../src/views/AssetLibraryView.vue'
import ExecutionInstanceRegistry from '../../src/components/ExecutionInstanceRegistry.vue'
import {useAuthStore} from '../../src/stores/auth'
import {repository} from '../../src/services/repository'
import {executionRegistryApi} from '../../src/services/executionRegistryApi'
import {api} from '../../src/services/api'
import {platformApi} from '../../src/services/platformApi'
import {generationOptions as options,generationDefinition,generationContract,generationMaintenanceDefinition,generationMaintenanceContract,profileId,auxiliaryIds} from './assetGenerationScenarios'
import {mediaAssets,mediaSources} from './execution-media-authoring-data'
import {inputSourcePolicies} from '../../src/services/inputSourcePolicies'
import {isolatedMaintenanceDestination} from './exact-maintenance-window'
import '../../src/styles.css'
import '../../src/platform.css'
const reads=ref(0),writes=ref(0),blocked=ref(0),outcome=ref('success'),copy=(v:any)=>JSON.parse(JSON.stringify(v))
const deny=()=>{blocked.value++;throw Error('隔离生成验证：禁止未声明网络及写入')}
const maintenanceParams=new URLSearchParams(location.search),maintenance=maintenanceParams.has('maintenance')
const maintenanceOption=options.find(row=>row.kind==='video'&&row.promptMode==='manual')!
const instanceId='52000000-0000-4000-8000-000000000001'
const instance={id:instanceId,key:'isolated-video-manual',name:maintenanceOption.name,revision:1,description:'仅内存验收',is_active:true}
const exactMaintenanceRoute=`/settings?section=executions&execution_version=${maintenanceOption.versionId}&relation_context=${encodeURIComponent('execution:'+maintenanceOption.versionId)}`
const maintenanceRoute=maintenanceParams.get('maintenance_route')||''
if(maintenanceRoute&&maintenanceRoute!==exactMaintenanceRoute)deny()
const definition=(row:any)=>maintenance&&row.versionId===maintenanceOption.versionId?generationMaintenanceDefinition(row):generationDefinition(row)
const contract=(row:any)=>maintenance&&row.versionId===maintenanceOption.versionId?generationMaintenanceContract(row):generationContract(row)
const versionRecord=(row:any)=>({id:row.versionId,instance_id:instanceId,instance_name:row.name,version:1,revision:1,status:'published',...definition(row),configuration:definition(row),definition_sha256:row.definitionSha256})
window.fetch=async()=>deny();XMLHttpRequest.prototype.open=function(){deny()};navigator.sendBeacon=()=>deny()
for(const client of [repository,executionRegistryApi,platformApi])for(const key of Object.keys(client))(client as any)[key]=async()=>deny()
repository.getAssets=async()=>{reads.value++;return[]}
repository.getAssetGenerationOptions=async(job,media)=>{reads.value++;const results=copy(options.filter(row=>row.kind===(job==='prompt'?'prompt':media)));return{count:results.length,next:null,previous:null,results}}
executionRegistryApi.contract=async(id)=>{reads.value++;return contract(options.find(row=>row.versionId===id))}
executionRegistryApi.version=async(id)=>{reads.value++;const row=options.find(row=>row.versionId===id);if(!row&&!auxiliaryIds.includes(id))return deny();return row?versionRecord(row) as any:{id,instance_name:`隔离辅助 ${auxiliaryIds.indexOf(id)+1}`,version:1,status:'published',configuration:definition({operation:'prompt.fields.generate',promptMode:'manual',kind:'prompt'})} as any}
platformApi.getPromptProfileVersion=async id=>{reads.value++;if(id!==profileId)return deny();return{id,name:'隔离策略 · 原文与字段处理',version:1,status:'published',operation:'text',description:'仅配置展示，无实际辅助调用',sourceProjectionKey:'declarative_v1',inputStructureVersion:null,sourceProjectionConfig:{creative_source:'本次输入'},fieldRuleCount:0,channelLimits:{positive_prompt:4096}} as any}
platformApi.listPromptProfileFieldRules=async id=>{reads.value++;if(id!==profileId)return deny();return[]}
api.interceptors.request.use(config=>{if(config.method==='get'&&config.url==='/registry-relations/')return config;return deny()})
api.defaults.adapter=async config=>{reads.value++;const {ids:id,kind}=config.params as any;const row=options.find(r=>r.versionId===id);if(!row&&id!==profileId&&!auxiliaryIds.includes(id))return deny();return{config,status:200,statusText:'OK',headers:{},data:{count:1,next:null,previous:null,generated_at:'2026-10-10T02:00:00Z',context_label:'隔离',results:[{id,kind,label:row?.name||'精确隔离引用',status:'published',config_url:kind==='prompt'?`/settings?section=prompts`:`/settings?section=executions&execution_version=${id}`,overview_url:`/settings/acceptance-smoke?view=relations&kind=${kind}&ids=${id}`,acceptance_url:'',groups:[],notices:['仅内存隔离配置，不是模型可用性证明'],capabilities:[]}]}}}
const jobs:any[]=[{id:'fixture-prompt',jobType:'prompt',status:'succeeded',mediaType:'video',resultText:'候选原文 中文 🟢 — preserve exact facts.',styleId:''}]
repository.createAssetGenerationJob=async payload=>{
 writes.value++;await new Promise(r=>setTimeout(r,200))
 if(outcome.value==='reject')throw{isAxiosError:true,response:{status:400,data:{detail:'隔离必需项拒绝；原输入保留'}}}
 const job={id:`fixture-job-${writes.value}`,status:'preparing',executionInstanceVersionId:payload.executionInstanceVersionId,jobType:payload.jobType,mediaType:payload.mediaType,preparation:{requestKey:payload.idempotencyKey},fixture:copy(payload.fixture)}
 jobs.push(job);if(outcome.value==='unknown')throw{response:{status:504}}
 return copy(job)
}
repository.getAssetGenerationJobs=async()=>{reads.value++;return copy(jobs)}
repository.getAssetGenerationJob=async id=>{reads.value++;return copy(jobs.find(j=>j.id===id))}
if(maintenance){
 inputSourcePolicies.options=async()=>{reads.value++;return copy(mediaSources)}
 repository.getAssetPage=async()=>{reads.value++;const results=copy(mediaAssets.filter(row=>row.type==='image'));return{count:results.length,next:null,previous:null,results}}
 executionRegistryApi.instances=async()=>{reads.value++;return{count:1,next:null,previous:null,results:[copy(instance)]}}
 executionRegistryApi.instance=async id=>{if(id!==instanceId)return deny();reads.value++;return copy(instance)}
 executionRegistryApi.versions=async()=>{reads.value++;return{count:1,next:null,previous:null,results:[versionRecord(maintenanceOption)]} as any}
 document.addEventListener('click',event=>{const link=(event.target as Element)?.closest('a[target="_blank"]') as HTMLAnchorElement|null;if(!link)return;event.preventDefault();let destination:string;try{destination=isolatedMaintenanceDestination(link.href,location.href,exactMaintenanceRoute)}catch{return deny()}window.open(destination,'_blank','noopener')})
}
const footer=()=>h('output',{style:'position:fixed;bottom:0;left:0;right:0;z-index:9000;pointer-events:none;background:#18312b;padding:5px;font-size:12px'},`内存隔离 · 写入 ${writes.value} · 读取 ${reads.value} · 网络守卫 ${blocked.value}`)
const Screen={render:()=>h('main',{style:'padding:16px'},[h('h1','素材生成作者 · 内存隔离'),h('p','提交仅写内存，不连接后台或模型。'),h('label',['隔离回执情形',h('select',{value:outcome.value,onChange:(e:any)=>outcome.value=e.target.value},['success','reject','unknown'].map(value=>h('option',{value},value)))]),h('button',{class:'button ghost',onClick:()=>router.push({name:'assets',query:{promptJob:'fixture-prompt'}})},'使用隔离视频提示词候选'),h(AssetLibraryView),footer()])}
const MaintenanceScreen={render:()=>h('main',{style:'padding:16px;min-width:0'},[h('h1','精确执行实例维护 · 隔离只读'),h('p',{'data-fixture-destination':'',style:'overflow-wrap:anywhere'},exactMaintenanceRoute),h(ExecutionInstanceRegistry,{deployments:[]}),footer()])}
const pinia=createPinia();useAuthStore(pinia).user={id:905,username:'fixture',isStaff:true,isSuperuser:true} as any
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',redirect:'/assets'},{path:'/assets',name:'assets',component:Screen},{path:'/settings',component:MaintenanceScreen},{path:'/workflows',name:'workflows',component:{render:()=>h('main',[h('h1','已读取隔离任务回执；未执行模型'),h('pre',{'aria-label':'原创建请求（内存）',style:'white-space:pre-wrap;overflow-wrap:anywhere'},JSON.stringify(jobs.at(-1),null,2)),h('a',{href:'#',onClick:(e:Event)=>{e.preventDefault();router.push('/assets')}},'返回隔离素材库'),footer()])}}]})
await router.push(maintenanceRoute||'/assets');await router.isReady()
createApp({render:()=>h(RouterView)}).use(pinia).use(router).mount('#app')
