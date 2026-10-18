// Formal editor/builder with isolated fixtures; block all real API requests.
import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {createMemoryHistory,createRouter} from 'vue-router'
import {api} from '../../src/services/api'
import {inputSourcePolicies} from '../../src/services/inputSourcePolicies'
import {emptyExecutionRequestDraft,prepareExecutionSelectedSources} from '../../src/services/executionRequestForm'
import {buildManualGeneration} from '../../src/services/productionManual'
import {readManualStoryboardReferences,manualGenerationShotSources} from '../../src/services/productionManualStoryboardReview'
import ExecutionRequestInputEditor from '../../src/components/ExecutionRequestInputEditor.vue'
import {id,options,run,target} from './manualGenerationShotSources'
import {mixedFixture} from './manualGenerationMixedSources'
import '../../src/styles.css'
import '../../src/platform.css'
api.interceptors.request.use(()=>{throw Error('隔离夹具禁止真实业务API')})
inputSourcePolicies.options=async()=>({schema_version:1,sources:[{value:'asset_upload',label:'正式上传素材',kinds:['image','video','audio'],available:true}],orders:[]}) as any
const fixture=mixedFixture(),contract=fixture.contract
const sources=manualGenerationShotSources(readManualStoryboardReferences(fixture.review,run),id(4))
const draft=ref(emptyExecutionRequestDraft(contract)),narrow=ref(false),error=ref(''),payload=ref<any>(null)
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]})
async function validate(){error.value='';payload.value=null;try{payload.value=buildManualGeneration({projectId:run.project,options,
  stageKey:'media_generation',target,contract,draft:await prepareExecutionSelectedSources(draft.value),idempotencyKey:'isolated-browser'}).payload}catch(cause){error.value=(cause as Error).message}}
createApp({setup(){return()=>h('main',{style:{maxWidth:narrow.value?'390px':'1100px',margin:'16px auto',padding:'16px'}},[
  h('h1','隔离：M6混合镜头输入'),h('p','正式编辑器与请求构造器。审核、素材目录为夹具；所有真实API禁止，不保存准备、不调用模型。'),
  h('button',{class:'button',onClick:()=>narrow.value=!narrow.value},narrow.value?'宽屏':'390px 窄屏'),
  h('button',{class:'button',onClick:()=>draft.value.media.clips.reverse()},'反转视频顺序（夹具）'),
  h('button',{class:'button',onClick:validate},'校验并显示请求（不发送）'),
  error.value?h('p',{role:'alert'},error.value):null,
  payload.value?h('section',{role:'status'},[h('h2','请求校验通过 · 真实写入0'),
    h('p',`图片来源：${payload.value.inputs.pictures.selection.source}；视频顺序：${payload.value.inputs.clips.items.map((row:any)=>row.asset_id.slice(-2)).join(' → ')}`),
    h('p',`配套音频：${payload.value.inputs.paired.items[0].kind}；范围：${payload.value.inputs.paired.items[0].scope_ref.kind}；配对位置：${payload.value.inputs.clips.items.findIndex((row:any)=>row.item_key===payload.value.inputs.paired.items[0].paired_to)+1}`),
    h('details',[h('summary','查看原始请求JSON'),h('pre',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},JSON.stringify(payload.value,null,2))])]):null,
  h(ExecutionRequestInputEditor,{contract,modelValue:draft.value,'onUpdate:modelValue':value=>draft.value=value,
    runId:run.id,productionScope:target.scope,reviewedShotSources:sources}),
])}}).use(createPinia()).use(router).mount('#app')
