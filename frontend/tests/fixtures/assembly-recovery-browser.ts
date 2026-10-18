// Original UI component; simulated receipts only. No live API, queue or model.
import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import ManualAssemblyReview from '../../src/components/ManualAssemblyReview.vue'
import BaseModal from '../../src/components/BaseModal.vue'
import { api } from '../../src/services/api'
import { platformApi } from '../../src/services/platformApi'
import '../../src/styles.css'
import '../../src/platform.css'

api.defaults.adapter = async config => {throw Error('隔离验证禁止真实 API：'+config.url)}
const id=(n:number)=>`10000000-0000-0000-0000-${String(n).padStart(12,'0')}`, sha='a'.repeat(64)
const run:any={id:id(1),project:id(2),status:'awaiting_input',planSource:'manual',executionPlan:{sha256:sha},stages:[{id:id(15),stageKey:'assembly'}]}
const options:any={runId:run.id,runPlanSha256:sha,actions:[{stageKey:'assembly',action:'assemble',scopeKinds:['episode']}]}
const inventory:any={run_id:run.id,run_plan_sha256:sha,contract_sha256:sha,frozen:true,blockers:[],approval:{revision:1},
  authors:{},author_contracts:{},author_schemas:{assembly_output:{type:'object'},assembly_timing:{type:'object'},assembly_media:{type:'object'}},
  episodes:[{episode_id:id(3),number:1,title:'隔离样例 · 已有片段',assembly_link_id:id(8),
    job:{id:id(9),status:'failed',attempts:1,max_attempts:1,error_message:'隔离编码失败（不是现用任务）',updated_at:'2026-10-06T00:00:00Z'},
    shots:[{shot_id:id(4),number:1,title:'镜头一',duration_ms:2500,media_kind:'image',source:{artifact_id:id(6),kind:'image',
      artifact_sha256:sha,manifest_sha256:sha,decision_id:id(7),decision_revision:1,scope:{kind:'shot',id:id(4)}}}]}],
  output:{width:1280,height:720,fps:24,media_profile:{schema_version:1},media_profile_key:'delivery.assembly_media_profile',
    media_profile_sha256:sha,max_attempts:1,label_enabled:false,label_text:''}}
const count=ref(0), same=ref(true), pending=ref(false), busy=ref(false), open=ref(true)
let original='', failReads=false
platformApi.getManualAssembly=async()=>{if(failReads)throw Error('隔离模拟：最新任务读取失败');return structuredClone(inventory)}
platformApi.performManualProductionAction=async(_run,body)=>{
  count.value++;const encoded=JSON.stringify(body)
  if(!original)original=encoded;else same.value=original===encoded
  if(count.value===1){failReads=true;throw Error('隔离模拟：原请求响应丢失')}
  failReads=false;inventory.episodes[0].job={...inventory.episodes[0].job,id:id(10),status:'queued',attempts:0,error_message:''}
  return {action:'assemble',result_kind:'assembly_link',target_id:id(8)} as any
}
const router=createRouter({history:createMemoryHistory(),routes:[{name:'workflows',path:'/workflows',component:{render:()=>h('p','隔离导航')}}]})
createApp({setup(){return()=>h('main',{style:'padding:24px'},[
  h('h1','M8-04 隔离组件验证'),h('p','仅模拟失联与回执；真实 API、模型、装配调用均为 0。'),
  h('p',`模拟提交次数 ${count.value} · 原正文一致 ${same.value} · 未决保护 ${pending.value}`),
  h('button',{class:'button',onClick:()=>open.value=true},'打开隔离装配'),
  h(BaseModal,{open:open.value,title:'装配恢复隔离验证',width:'1000px',dismissible:!pending.value&&!busy.value,
    onClose:()=>{if(!pending.value&&!busy.value)open.value=false}},
    {default:()=>open.value?h(ManualAssemblyReview,{run,options,onBusy:(v:boolean)=>busy.value=v,onPending:(v:boolean)=>pending.value=v}):null}),
])}}).use(createPinia()).use(router).mount('#app')
