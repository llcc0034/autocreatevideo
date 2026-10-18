// Actual formal components; API calls below are local fixtures, never live mutations.
import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {createMemoryHistory,createRouter} from 'vue-router'
import {api} from '../../src/services/api'
import {platformApi} from '../../src/services/platformApi'
import {executionRegistryApi} from '../../src/services/executionRegistryApi'
import ManualGenerationPreparation from '../../src/components/ManualGenerationPreparation.vue'
import {id,options,rawContract,review,run,sha,target} from './manualGenerationShotSources'
import '../../src/styles.css'
import '../../src/platform.css'
api.interceptors.request.use(()=>{throw Error('隔离夹具禁止真实业务 API')})
const failed=ref(false),reads=ref(0),submits=ref(0),submitted=ref(''),narrow=ref(false)
executionRegistryApi.versions=async()=>({results:[{id:id(10),instance_name:'隔离双用途实例',version:1,operation:'video.reference_to_video',workflow_version_id:id(11)} as any],count:1,next:null,previous:null})
executionRegistryApi.contract=async()=>structuredClone(rawContract)
platformApi.listProductionTargets=async()=>({runId:run.id,runPlanSha256:sha,results:[target],count:1,next:null,previous:null})
platformApi.getManualStoryboardReview=async()=>{reads.value++;if(failed.value)throw Error('隔离失败：原审核清单无法读取');return structuredClone(review)}
platformApi.getManualMediaReview=async()=>{throw Error('此夹具仅验证已审核镜头用途，不伪造生成候选')}
platformApi.performManualProductionAction=async(_run,value)=>{submits.value++;submitted.value=JSON.stringify(value,null,2)
  throw {response:{status:400,data:{error:{code:'fixture_no_write',message:'隔离验证已捕获请求；未保存、未调用模型'}}}}}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]})
createApp({setup(){return()=>h('main',{style:{maxWidth:narrow.value?'390px':'1100px',margin:'16px auto',padding:'16px'}},[
  h('h1','隔离：手动镜头确切来源'),h('p','正式组件 + 本地夹具；同一文件的两份版本用途。所有真实 API 禁止。'),
  h('button',{class:'button',onClick:()=>narrow.value=!narrow.value},narrow.value?'宽屏':'390px 窄屏'),
  h('button',{class:'button',onClick:()=>failed.value=!failed.value},failed.value?'恢复目录读取':'模拟目录读取失败'),
  h('p',{role:'status'},`目录读取 ${reads.value} 次 · 请求捕获 ${submits.value} 次 · 真实写入 0`),
  h(ManualGenerationPreparation,{run,options,initialStageKey:'media_generation'}),
  submitted.value?h('pre',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},submitted.value):null])}}).use(createPinia()).use(router).mount('#app')
