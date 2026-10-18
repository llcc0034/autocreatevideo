// Formal component, isolated catalog and local draft only. No business API writes.
import {createApp,h,ref} from 'vue'
import ProductionRecipeEditor from '../../src/components/ProductionRecipeEditor.vue'
import {executionRegistryApi} from '../../src/services/executionRegistryApi'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'

api.interceptors.request.use(()=>{throw new Error('隔离夹具禁止真实API请求')})
executionRegistryApi.versions=async()=>({results:[],next:null,previous:null,count:0})
const native=(value:string)=>({value,label:value,repeatable:false,candidateEvaluatorOptions:[],modes:['review'],requiresRouting:false,routingModes:[],decisionPolicy:null})
const options={definitionSchemaVersion:3,effectiveLimits:{maxRoutingRoutes:3,maxVariants:4,maxGenerationTasks:30},
  materialSourceOptions:['generate','existing','upload'].map(value=>({value,label:value})),
  materialSelectionOptions:[{value:'manual',label:'人工选择'},{value:'unique_version',label:'自动使用唯一确切版本，否则人工接管'}],
  stageOptions:[native('script_review'),native('storyboard_review'),{value:'structured_text',label:'通用结构化文本',repeatable:true,modes:['ai'],requiresRouting:true,routingModes:['ai'],decisionPolicy:null,
    candidateEvaluatorOptions:[{value:null,label:'人工选择候选'},{value:'first_succeeded',label:'按候选序号自动选择成功项'}]}],
  interactionModeOptions:[{value:'assisted',label:'人工辅助制作'},{value:'unattended',label:'无人值守制作'}],approvalOptions:[{value:'auto',label:'自动推进'},{value:'required',label:'人工审核'}],
  routingModeOptions:[{value:'fixed',label:'固定路线'}],fieldReviewPolicyOptions:[],sourceKindOptions:[],structureNodeOptions:[],textOutputOptions:[],referenceSourceOptions:[],parameterPolicyOptions:[],parameterSourceOptions:[],scopeOptions:[],presets:[]}
const step=(key:string)=>({key,kind:'structured_text',name:'同名步骤',mode:'ai',approval:'auto',routing:{mode:'fixed',variant_count:1,evaluator:null,routes:[]}})
createApp({setup(){
  const draft=ref<any>({schema_version:3,interaction_mode:'assisted',stages:[{key:'script_review',mode:'review',approval:'required'},step('fixture-one'),step('fixture-two'),{key:'storyboard_review',mode:'review',approval:'required'}]})
  const readonly=ref(false),errors=ref<string[]>([])
  return()=>h('main',{style:{maxWidth:'1000px',padding:'16px',margin:'20px auto',background:'#101820',color:'#e6edf3'}},[
    h('h1','可重复配方步骤 · 隔离验证'),h('p','正式编辑器，仅本地草稿；不保存配方、不创建运行、不调用模型。'),
    h('button',{class:'button ghost small',onClick:()=>readonly.value=!readonly.value},readonly.value?'切换可编辑':'切换只读'),
    h(ProductionRecipeEditor,{modelValue:draft.value,options,disabled:readonly.value,'onUpdate:modelValue':(value:any)=>draft.value=value,onValidity:(value:string[])=>errors.value=value}),
    h('output',{role:'status'},errors.value.join('；')||'本地草稿有效（未保存；路线未配置）'),
    h('pre',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},JSON.stringify(draft.value,null,2)),
  ])
}}).mount('#app')
