// Synthetic original TaskMediaInspector; no live requests or business writes.
import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import Inspector from '../../src/components/TaskMediaInspector.vue'
import { adaptTasks } from '../../src/services/backendAdapters'
import { productionIssuePresentation } from '../../src/services/productionRunPresentation'
import { api } from '../../src/services/api'
import baseStyles from '../../src/styles.css?inline'
import '../../src/workflow-settings.css'
import '../../src/responsive.css'
import '../../src/platform.css'

export function statusTask(status: 'cancelled' | 'cancel_requested') {
  return adaptTasks([{id:'synthetic-cancel-task',kind:'video',status,project_title:'合成取消状态',project:'synthetic-project',
    created_at:'2026-10-04T00:00:00Z',finished_at:status==='cancelled'?'2026-10-04T00:01:00Z':null,
    attempts:1,progress:75,progress_source:'comfyui',comfy_progress:75,timeline:[],output_media:[],
    execution:{production_run_id:'synthetic-run',stage_run_id:'synthetic-stage',stage_key:'media_generation',stage_ordinal:1,
      unit_key:'synthetic-unit',attempt_id:'synthetic-attempt',attempt_number:1,preparation_id:'synthetic-preparation',
      execution_instance_version_id:'synthetic-instance',operation:'video.generate'},
    input_contract_status:'available',input_contract_code:'',input_parameters:{preparation_id:'synthetic-preparation',
      attempt_id:'synthetic-attempt',execution_instance_version_id:'synthetic-instance',workflow_version_id:'synthetic-workflow',
      request_sha256:'a'.repeat(64),operation:'video.generate',output_kind:'video',prompt_mode:'manual',
      prompt_profile_version_id:null,parameters:[],media_uses:[],prompt_channels:[]},
  }])[0]
}
// Browser bundles use vue({features:{prodDevtools:true}}) only in this isolated
// preview so setup refs remain accessible, as they are in the SSR test build.
export function statusInspector(status: 'cancelled' | 'cancel_requested', capture: (state:any)=>void=()=>{}) {
  return {...Inspector,setup(props:any,context:any){
    const state=(Inspector as any).setup(props,context)
    state.selected.value=statusTask(status);capture(state);return state
  }}
}
if(typeof document!=='undefined') {
  const blocked=ref(0), selected=ref<'cancelled'|'cancel_requested'>('cancelled')
  const block=():never=>{blocked.value++;throw Error('隔离夹具禁止网络、存储及业务写入')}
  api.interceptors.request.use(block);api.defaults.adapter=async()=>block()
  window.fetch=async()=>block();XMLHttpRequest.prototype.send=block;navigator.sendBeacon=block
  for(const key of ['localStorage','sessionStorage'])Object.defineProperty(window,key,{configurable:true,value:{
    getItem:(name:string)=>key==='localStorage'&&['cineforge_token','cineforge_user'].includes(name)?null:block(),
    setItem:block,removeItem:block,clear:block,
  }})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]})
  const app=createApp({render:()=>h('main',{class:'fixture-shell workflow-page',onClickCapture:(e:MouseEvent)=>{
    if((e.target as Element)?.closest('a')){e.preventDefault();e.stopPropagation()}
  }},[
    h('style',baseStyles.replace(/@import\s*(?:url\([^)]*\)|"[^"]*"|'[^']*')\s*;/g,'')),
    h('h1','M7-08 恢复状态 · 原组件合成验证'),
    h('p','仅验证原媒体详情组件与共享文案，不是完整 ProductionRun 页面或真实取消；禁止网络和业务写入。'),
    h('p',{role:'status'},`已阻断请求 ${blocked.value} · 业务写入 0`),
    h('nav',{class:'fixture-controls'},(['cancelled','cancel_requested'] as const).map(status=>h('button',{
      class:'button ghost',type:'button','aria-pressed':selected.value===status,onClick:()=>{selected.value=status},
    },status==='cancelled'?'合成已取消':'合成取消中'))),
    h('p',productionIssuePresentation('external_call_not_authorized','', 'blocked').summary),
    h(statusInspector(selected.value),{key:selected.value}),
  ])}).use(createPinia()).use(router)
  void router.push('/').then(()=>router.isReady()).then(()=>app.mount('#app'))
  window.addEventListener('pagehide',()=>app.unmount(),{once:true})
}
