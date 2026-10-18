// Formal editor with isolated catalogs/local state; no saved business data.
import {createApp,h,ref} from 'vue'
import ProductionRecipeEditor from '../../src/components/ProductionRecipeEditor.vue'
import {executionRegistryApi} from '../../src/services/executionRegistryApi'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'
api.interceptors.request.use(()=>{throw new Error('隔离夹具禁止真实业务API')})
executionRegistryApi.versions=async()=>({results:[],next:null,previous:null,count:0})
const options:any={definitionSchemaVersion:3,effectiveLimits:{maxRoutingRoutes:3,maxVariants:5,maxGenerationTasks:30},stageOptions:[{
  value:'bible_image_generation',label:'主图与变体',repeatable:false,modes:['existing_pipeline'],routingModes:['existing_pipeline'],requiresRouting:true,decisionPolicy:null,
  candidateEvaluatorOptions:[{value:null,label:'人工选择候选'},{value:'first_succeeded',label:'按候选序号自动选择成功项'}]}],
  materialSourceOptions:[{value:'generate',label:'模型生成（默认）'},{value:'existing',label:'已有成图（零调用）'},{value:'upload',label:'上传成图（零调用）'}],
  materialSelectionOptions:[{value:'manual',label:'人工选择'},{value:'unique_version',label:'自动使用唯一确切版本，否则人工接管'}],
  interactionModeOptions:[{value:'assisted',label:'人工辅助制作'}],approvalOptions:[{value:'required',label:'人工审核'}],routingModeOptions:[{value:'fixed',label:'固定路线'},{value:'manual_choice',label:'人工选路'}],
  fieldReviewPolicyOptions:[],sourceKindOptions:[],structureNodeOptions:[],textOutputOptions:[],referenceSourceOptions:[],parameterPolicyOptions:[],parameterSourceOptions:[],scopeOptions:[],presets:[]}
const initial=()=>({schema_version:3,interaction_mode:'assisted',stages:[{key:'bible_image_generation',mode:'existing_pipeline',approval:'required',
  routing:{mode:'fixed',variant_count:2,evaluator:null,routes:[]},variant_routing:{mode:'manual_choice',variant_count:3,evaluator:null,routes:[]}}]})
createApp({setup(){
  const draft=ref<any>(initial()),errors=ref<string[]>([]),narrow=ref(false),disabled=ref(false)
  return()=>h('main',{style:{maxWidth:narrow.value?'390px':'980px',margin:'20px auto',padding:'16px',background:'#101820',color:'#e6edf3'}},[
    h('h1','成图来源配置 · 隔离验收'),h('p','正式组件、本地草稿、所有业务 API 禁用。未保存配方，未创建运行或调用模型。'),
    h('button',{onClick:()=>narrow.value=!narrow.value},narrow.value?'宽屏布局':'390px 窄屏布局'),
    h('button',{onClick:()=>disabled.value=!disabled.value},disabled.value?'恢复编辑':'只读模式'),
    h('button',{onClick:()=>draft.value=initial()},'重置隔离草稿'),
    h(ProductionRecipeEditor,{modelValue:draft.value,options,disabled:disabled.value,'onUpdate:modelValue':(v:any)=>draft.value=v,onValidity:(v:string[])=>errors.value=v}),
    h('output',{id:'source-result',style:{display:'block',overflowWrap:'anywhere'}},JSON.stringify(draft.value.stages[0])),
    h('output',{id:'validation-result',style:{display:'block',overflowWrap:'anywhere'}},errors.value.join('；')||'草稿可保存，未提交'),
  ])
}}).mount('#app')
