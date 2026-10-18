import { createApp, h, ref } from 'vue'
import ProductionRecipeEditor from '../../src/components/ProductionRecipeEditor.vue'
import { dataStructures } from '../../src/services/dataStructures'
import { executionRegistryApi } from '../../src/services/executionRegistryApi'
import { api } from '../../src/services/api'
import { versionA, sourceSchema, sourceSchemaB, structures, route, contract, options } from './shot-field-mapping-data'
import '../../src/styles.css'
import '../../src/platform.css'

const failed=ref(false),blocked=ref(0),readonly=ref(false),narrow=ref(false)
const confirmResponse=ref(false),confirmation=ref('')
// Explicit isolated response only. Native dialog handling is not a business approval.
window.confirm=message=>{confirmation.value=String(message);return confirmResponse.value}
api.interceptors.request.use(()=>{blocked.value++;throw Error('隔离页面禁止真实业务API')})
dataStructures.list=async()=>({hasNext:false,hasPrevious:false,count:structures.length,results:structures})
dataStructures.get=async id=>{const row=structures.find(row=>row.id===id);if(!row)throw Error('隔离结构不存在');return row}
dataStructures.schema=async id=>{if(failed.value)throw Error('隔离目录暂时不可用');return {valid:true,json_schema:id===versionA?sourceSchema:sourceSchemaB,errors:{}}}
executionRegistryApi.versions=async()=>({count:0,results:[],next:null,previous:null})
executionRegistryApi.contract=async()=>contract()
const editorOptions:any={...options,definitionSchemaVersion:3,effectiveLimits:{maxRoutingRoutes:3,maxVariants:4,maxGenerationTasks:20},
  stageOptions:[{value:'storyboard_review',label:'分镜审核',repeatable:false,modes:['review'],requiresRouting:false,routingModes:[],decisionPolicy:null,candidateEvaluatorOptions:[]},
    {value:'media_generation',label:'镜头生成',repeatable:false,modes:['existing_pipeline'],requiresRouting:true,routingModes:['existing_pipeline'],decisionPolicy:null,candidateEvaluatorOptions:[{value:'first_succeeded',label:'自动选择成功候选'}]}],
  interactionModeOptions:[{value:'assisted',label:'辅助制作'}],approvalOptions:[{value:'required',label:'人工审核'},{value:'auto',label:'自动推进'}],routingModeOptions:[{value:'fixed',label:'固定路线'}],
  fieldReviewPolicyOptions:[],materialSourceOptions:[],materialSelectionOptions:[],presets:[]}
const initial=()=>({schema_version:3,interaction_mode:'assisted',stages:[{key:'storyboard_review',mode:'review',approval:'required'},
  {key:'media_generation',mode:'existing_pipeline',approval:'auto',routing:{mode:'fixed',variant_count:1,evaluator:'first_succeeded',routes:[route()]}}]})
createApp({setup(){
  const draft=ref<any>(initial()),issues=ref<string[]>([]),saved=ref('')
  return()=>h('main',{style:{maxWidth:narrow.value?'390px':'1050px',margin:'16px auto',padding:'12px',background:'#101820',color:'#e6edf3'}},[
    h('h1','配方独立字段 · 隔离验证'),h('p','正式配方组件＋内存目录；保存仅写内存，不发布、不创建运行、不调用模型。'),
    h('button',{onClick:()=>narrow.value=!narrow.value},narrow.value?'恢复桌面布局':'390px窄屏布局'),
    h('button',{onClick:()=>readonly.value=!readonly.value},readonly.value?'恢复编辑':'只读模式'),
    h('button',{onClick:()=>{failed.value=!failed.value;draft.value=initial()}},failed.value?'恢复目录并重置夹具':'模拟目录失败并重置夹具'),
    h('button',{onClick:()=>{draft.value=initial();draft.value.stages[1].routing.routes[0].input_sources.custom.template.fields.tone={kind:'context_field',selector_key:'shot.narrative',scope:{kind:'shot'}}}},'载入已退役固定字段夹具'),
    h('label',{},['隔离确认框响应',h('select',{value:String(confirmResponse.value),onChange:(event:Event)=>confirmResponse.value=(event.target as HTMLSelectElement).value==='true'},[
      h('option',{value:'false'},'取消变更'),h('option',{value:'true'},'确认变更')])]),
    h('p',{},confirmation.value?'隔离确认记录：'+confirmation.value:'隔离确认尚未触发；此处使用可控内存响应，不代表用户验收。'),
    h(ProductionRecipeEditor,{key:String(failed.value),modelValue:draft.value,options:editorOptions,disabled:readonly.value,
      'onUpdate:modelValue':(value:any)=>draft.value=value,onValidity:(value:string[])=>issues.value=value}),
    h('p',{role:'status',id:'mapping-status'},issues.value.join('；')||'隔离草稿校验通过（未提交业务）'),
    h('button',{disabled:readonly.value||Boolean(issues.value.length),onClick:()=>{saved.value=JSON.stringify(draft.value)}},'保存隔离草稿到内存'),
    h('p',{},saved.value?'内存保存完成（不是业务配方保存）':'尚未保存隔离草稿'),
    h('output',{id:'api-block-count'},'真实API拦截数：'+blocked.value),
    h('details',{},[h('summary','查看实际草稿字段身份'),h('pre',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},JSON.stringify(draft.value.stages[1].routing.routes[0].input_sources,null,2))]),
  ])
}}).mount('#app')
