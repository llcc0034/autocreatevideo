import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView} from 'vue-router'
import SettingsView from '../../src/views/SettingsView.vue'
import {useAuthStore} from '../../src/stores/auth'
import {platformApi} from '../../src/services/platformApi'
import {api} from '../../src/services/api'
import {copy,source,preview,scan,receipt,installReads,recoveredSource,recoveredVersion,recoveredProof,previewReceipt} from './workflow-import-data'
import '../../src/styles.css'
import '../../src/platform.css'
const writes=ref(0),reads=ref(0),blocked=ref(0),mode=ref('success'),scenario=ref('new'),epoch=ref(0),readMode=ref('found')
const fixtureKey='workflow-recovery-fixture-records'
const saved=JSON.parse(sessionStorage.getItem(fixtureKey)||'[]') as Array<{deploymentId:string;input:any}>
const contractsKey='workflow-contract-fixture-records'
const contracts=JSON.parse(sessionStorage.getItem(contractsKey)||'[]') as ReturnType<typeof previewReceipt>[]
let batchCount=0
function deny():never{blocked.value++;throw Error('隔离页面禁止真实网络、模型与主机请求')}
api.interceptors.request.use(()=>deny());api.defaults.adapter=async()=>deny();window.fetch=async()=>deny();XMLHttpRequest.prototype.send=()=>deny();navigator.sendBeacon=()=>deny()
installReads(platformApi)
for(const key of Object.keys(platformApi).filter(k=>k.startsWith('list')||k.startsWith('get')||k==='resolveConfiguration')){const original=(platformApi as any)[key];(platformApi as any)[key]=async(...args:any[])=>{reads.value++;return original(...args)}}
const inMemorySource=()=>source(scenario.value==='successor'?{imported:true,workflowVersionId:'fixture-original-v1',sourceId:'fixture-source'}:scenario.value==='unmanaged'?{candidateManaged:false,candidateSourceId:''}:{suggestedTemplate:{useExisting:false,templateId:'',name:'新参考视频方案',slug:'fixture-new',outputKind:'video'}})
platformApi.scanComfySavedWorkflows=async()=>{reads.value++;const first=inMemorySource();return scan(scenario.value==='multiple'?[first,source({sourceKey:'second',sourcePath:'workflows/second.json',sourceSha256:'7'.repeat(64)}),source({sourceKey:'third',sourcePath:'workflows/third.json',sourceSha256:'8'.repeat(64)})]:[first]) as any}
platformApi.previewComfySavedWorkflow=async(id,input)=>{
  writes.value++;await new Promise(r=>setTimeout(r,700))
  if(mode.value==='reject')throw{response:{status:400,data:{message:'隔离字段校验错误：请核对主输出，原选择保留。'}}}
  const candidate=inMemorySource().candidateSourceId
  const result=previewReceipt(id,input,candidate);if(mode.value==='candidate-only')result.actionability='candidate_only'
  contracts.push(result);sessionStorage.setItem(contractsKey,JSON.stringify(contracts))
  if(mode.value==='contract-unknown')throw Error('隔离合同回执丢失')
  return result
}
platformApi.getComfyWorkflowPreviewReceipt=async(_id,key)=>{reads.value++;if(readMode.value==='error')throw Error('隔离合同读取失败');
  const row=contracts.find(row=>row.requestReceipt?.requestKey===key)
  if(readMode.value==='empty'||!row)throw Error('尚未查到原请求回执，不代表未保存；请稍后只读核对。')
  return readMode.value==='mismatch'?{...copy(row),sourceSha256:'0'.repeat(64)}:copy(row)}
platformApi.batchImportComfySavedWorkflows=async(id,items)=>{
  writes.value++;await new Promise(r=>setTimeout(r,700))
  if(mode.value==='reject')throw{response:{status:400,data:{message:'隔离合同拒绝，未保存。'}}}
  batchCount++;saved.push(...items.map(input=>({deploymentId:id,input:copy(input)})));sessionStorage.setItem(fixtureKey,JSON.stringify(saved))
  if(mode.value==='unknown'||(mode.value==='partial-unknown'&&batchCount===2))throw Error('隔离响应丢失，实际结果未知')
  return receipt(id,items) as any
}
platformApi.listComfyWorkflowSources=async()=>{reads.value++;await new Promise(r=>setTimeout(r,700));if(readMode.value==='error')throw Error('隔离读取失败');if(readMode.value==='empty')return[]
  return Promise.all(saved.map(async row=>{const result=await recoveredProof(row.deploymentId,row.input);if(readMode.value==='mismatch')result.report.successor_of='wrong-predecessor';return result}))
}
platformApi.getWorkflowVersion=async(id)=>{reads.value++;const row=saved.find(row=>recoveredSource(row.deploymentId,row.input).versionId===id);if(!row)throw Error('隔离版本不存在');return recoveredVersion(row.input)}
api.get=async(url:string)=>{if(url==='/registry-relations/'){reads.value++;return{data:{count:0,next:null,previous:null,results:[],generated_at:'2026-10-09T00:00:00Z',context_label:'隔离关系'}} as any}return deny()}
// A test-run identity isolates pending browser storage from previous fixture
// sessions without erasing them; reloading the same URL keeps its request.
const runId=new URLSearchParams(window.location.search).get('run')||'default'
const pinia=createPinia(),auth=useAuthStore(pinia);auth.user={id:`workflow-import-fixture:${runId}`,isStaff:true,isSuperuser:true} as any
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/settings',component:SettingsView}]})
await router.push('/settings?section=workflows');await router.isReady()
function reset(){epoch.value++;writes.value=0;reads.value=0;blocked.value=0;batchCount=0}
createApp({setup:()=>()=>h('main',{style:'max-width:1400px;margin:auto;padding:12px;min-width:0'},[
  h('h1',{style:'font-size:20px'},'工作流新建与后继 · 内存隔离验证'),
  h('p',{'data-fixture-evidence':'',role:'status'},`内存写入 ${writes.value} · 读取 ${reads.value} · 网络拦截 ${blocked.value} · 不写现用库`),
  h('label',['样例类型',h('select',{value:scenario.value,onChange:(e:Event)=>{scenario.value=(e.target as HTMLSelectElement).value;reset()}},['new','successor','unmanaged','multiple'].map(value=>h('option',{value},value)))]),
  h('label',['回执场景',h('select',{value:mode.value,onChange:(e:Event)=>mode.value=(e.target as HTMLSelectElement).value},['success','reject','candidate-only','unknown','partial-unknown','contract-unknown'].map(value=>h('option',{value},value)))]),
  h('label',['核对场景',h('select',{value:readMode.value,onChange:(e:Event)=>readMode.value=(e.target as HTMLSelectElement).value},['found','empty','mismatch','error'].map(value=>h('option',{value},value)))]),
  h('button',{type:'button',onClick:()=>epoch.value++},'重新打开配置页（保留原请求）'),
  h(RouterView,{key:epoch.value}),
])}).use(pinia).use(router).mount('#app')
