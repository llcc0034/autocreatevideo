// Formal editor and choices; isolated catalogs/local state. All API blocked.
import {createApp,h,ref} from 'vue'
import ProductionRecipeEditor from '../../src/components/ProductionRecipeEditor.vue'
import RecipeExecutionRouteChoices from '../../src/components/RecipeExecutionRouteChoices.vue'
import {executionRegistryApi} from '../../src/services/executionRegistryApi'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'
api.interceptors.request.use(()=>{throw new Error('隔离夹具禁止真实业务API')})
executionRegistryApi.versions=async()=>({results:[],next:null,previous:null,count:0})
const options:any={definitionSchemaVersion:3,effectiveLimits:{maxRoutingRoutes:3,maxVariants:5,maxGenerationTasks:30},stageOptions:[{
  value:'bible_image_generation',label:'主图与变体',repeatable:false,modes:['existing_pipeline'],routingModes:['existing_pipeline'],requiresRouting:true,decisionPolicy:null,
  candidateEvaluatorOptions:[{value:null,label:'人工选择候选'},{value:'first_succeeded',label:'按候选序号自动选择成功项'}]}],
  interactionModeOptions:[{value:'assisted',label:'人工辅助制作'}],approvalOptions:[{value:'required',label:'人工审核'}],routingModeOptions:[{value:'fixed',label:'固定路线'},{value:'manual_choice',label:'人工选路'}],
  materialSourceOptions:[{value:'generate',label:'模型生成（默认）'},{value:'existing',label:'已有成图（零调用）'},{value:'upload',label:'上传成图（零调用）'}],
  materialSelectionOptions:[{value:'manual',label:'人工选择'},{value:'unique_version',label:'自动使用唯一确切版本，否则人工接管'}],
  fieldReviewPolicyOptions:[],sourceKindOptions:[],structureNodeOptions:[],textOutputOptions:[],referenceSourceOptions:[],parameterPolicyOptions:[],parameterSourceOptions:[],scopeOptions:[],presets:[]}
const routing=(count:number)=>({mode:'fixed',variant_count:count,evaluator:null,routes:[]})
createApp({setup(){
  const draft=ref<any>({schema_version:3,interaction_mode:'assisted',stages:[{key:'bible_image_generation',mode:'existing_pipeline',approval:'required',routing:routing(2),variant_routing:routing(3)}]})
  const errors=ref<string[]>([]),choice=ref('尚无动作')
  const route={execution_instance_version_id:'11111111-1111-4111-8111-111111111111',expected_definition_sha256:'a'.repeat(64),input_sources:{},parameter_policies:[]}
  const run:any={id:'isolated-run',planSource:'recipe',status:'awaiting_approval',executionPlan:{sha256:'b'.repeat(64),value:{recipe:{definition:{...draft.value,stages:[{
    ...draft.value.stages[0],routing:{...routing(2),mode:'manual_choice',routes:[route,route]},variant_routing:{...routing(3),mode:'manual_choice',routes:[route,route]}}]}}}},
    stages:[{id:'stage',stageKey:'bible_image_generation',status:'pending',selectedRouteIndex:1,selectedVariantRouteIndex:null}]}
  return()=>h('main',{style:{maxWidth:'980px',margin:'20px auto',padding:'16px',background:'#101820',color:'#e6edf3'}},[
    h('h1','主图与变体配置 · 隔离验收'),h('p','只测试正式组件的本地状态；未保存业务配置，不创建运行或调用模型。'),
    h(ProductionRecipeEditor,{modelValue:draft.value,options,'onUpdate:modelValue':(v:any)=>draft.value=v,onValidity:(v:string[])=>errors.value=v}),
    h('output',{id:'draft-result'},`主图候选 ${draft.value.stages[0].routing?.variant_count??'无生成路线'}；变体候选 ${draft.value.stages[0].variant_routing?.variant_count??'无生成路线'}；${errors.value.join('；')||'草稿可保存，未提交'}`),
    h(RecipeExecutionRouteChoices,{run,busy:false,onSelect:(v:any)=>choice.value=JSON.stringify(v)}),
    h('output',{id:'choice-result',style:{overflowWrap:'anywhere'}},choice.value),
  ])
}}).mount('#app')
