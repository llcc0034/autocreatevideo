// Local state only. No business save, real model or live configuration access.
import {createApp,h,ref} from 'vue'
import ProductionRecipeEditor from '../../src/components/ProductionRecipeEditor.vue'
import {executionRegistryApi} from '../../src/services/executionRegistryApi'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'
api.interceptors.request.use(()=>{throw new Error('隔离夹具禁止真实业务API')})
executionRegistryApi.versions=async()=>({results:[],next:null,previous:null,count:0})
const options:any={definitionSchemaVersion:3,effectiveLimits:{maxRoutingRoutes:3,maxVariants:5,maxGenerationTasks:30},stageOptions:[
  {value:'bible_image_generation',label:'素材结果',repeatable:false,modes:['existing_pipeline'],routingModes:['existing_pipeline'],requiresRouting:true,decisionPolicy:null,candidateEvaluatorOptions:[]},
  {value:'storyboard_review',label:'分镜参考',repeatable:false,modes:['review'],routingModes:[],requiresRouting:false,decisionPolicy:null,candidateEvaluatorOptions:[]}],
  shotReferenceSourceOptions:[{value:'upstream',label:'本轮上游素材结果',upstreamStageKey:'bible_image_generation',selectionOptions:[{value:'manual',label:'人工选择'},{value:'unique_setting',label:'自动匹配唯一确切设定，否则人工接管'}]},
    ...[{value:'existing',label:'已有素材库'},{value:'upload',label:'本地上传素材'}].map(row=>({...row,upstreamStageKey:null,selectionOptions:[{value:'manual',label:'人工选择'}]}))],
  materialSourceOptions:[{value:'existing',label:'已有成图'},{value:'upload',label:'上传成图'},{value:'generate',label:'模型生成'}],materialSelectionOptions:[{value:'manual',label:'人工选择'}],
  interactionModeOptions:[{value:'assisted',label:'人工辅助制作'}],approvalOptions:[{value:'required',label:'人工审核'},{value:'auto',label:'自动'}],routingModeOptions:[],
  fieldReviewPolicyOptions:[],sourceKindOptions:[],structureNodeOptions:[],textOutputOptions:[],referenceSourceOptions:[],parameterPolicyOptions:[],parameterSourceOptions:[],scopeOptions:[],presets:[]}
const initial=()=>({schema_version:3,interaction_mode:'assisted',stages:[
  {key:'bible_image_generation',mode:'existing_pipeline',approval:'auto',material_sources:{primary:'existing',variant:'existing'}},
  {key:'storyboard_review',mode:'review',approval:'required'}]})
createApp({setup(){
  const draft=ref<any>(initial()), errors=ref<string[]>([]), narrow=ref(false), disabled=ref(false)
  return()=>h('main',{style:{width:narrow.value?'390px':'100%',maxWidth:'100%',boxSizing:'border-box',margin:'auto',padding:'16px',background:'#101820',color:'#e6edf3'}},[
    h('h1','逐镜来源规则 · 隔离验收'),h('p','正式配方组件；仅本地草稿，不保存、不创建运行。'),
    h('button',{onClick:()=>narrow.value=!narrow.value},narrow.value?'宽屏布局':'390px 窄屏布局'),
    h('button',{onClick:()=>disabled.value=!disabled.value},disabled.value?'恢复编辑':'只读模式'),
    h('button',{onClick:()=>draft.value=initial()},'重置隔离草稿'),
    h(ProductionRecipeEditor,{modelValue:draft.value,options,disabled:disabled.value,'onUpdate:modelValue':(value:any)=>draft.value=value,onValidity:(value:string[])=>errors.value=value}),
    h('output',{id:'policy-result',style:{display:'block',overflowWrap:'anywhere'}},JSON.stringify(draft.value.stages.at(-1))),
    h('output',{id:'validation-result'},errors.value.join('；')||'合同有效；没有提交业务保存或执行'),
  ])
}}).mount('#app')
