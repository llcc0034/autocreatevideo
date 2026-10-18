// In-memory authoring only; every real HTTP request is rejected.
import {createApp,h,ref} from 'vue'
import ProductionRecipeEditor from '../../src/components/ProductionRecipeEditor.vue'
import {platformApi} from '../../src/services/platformApi'
import {executionRegistryApi} from '../../src/services/executionRegistryApi'
import {api} from '../../src/services/api'
import {recipeDefaultContract} from './recipe-default-data'
import '../../src/styles.css'
import '../../src/platform.css'

api.interceptors.request.use(()=>{throw Error('隔离夹具禁止真实API请求')})
const fail=ref(false),readonly=ref(false)
const schema={type:'object',properties:{count:{type:'integer',title:'数量',minimum:0}},required:['count'],additionalProperties:false}
executionRegistryApi.versions=async()=>({results:[],next:null,previous:null,count:0})
executionRegistryApi.contract=async(id)=>{
  const contract:any=recipeDefaultContract()
  contract.version_id=id;contract.definition.version_id=id
  contract.definition.configuration.operation='text.structured'
  contract.definition.configuration.inputs[0].semantic_contract.allowed_scope_kinds=['project']
  contract.input_forms.custom.schemas=[schema];contract.definition.interface.input_targets.payload.value_schema=schema
  if(id==='producer')contract.definition.configuration.inputs=[]
  return contract
}
platformApi.getRecipeSourceOptions=async(definition)=>{
  await new Promise(resolve=>setTimeout(resolve,150))
  if(fail.value)throw Error('显式隔离目录读取失败')
  return (definition.stages as any[]).map((stage,index)=>({stageKey:stage.key,outputKey:'structured_text.shared',kind:'json',scopeKind:'project',label:`${index+1} · ${stage.name} · 样例结构 v1`,
    value_schema:stage.key==='layout-b'?{type:'object',properties:{result:{type:'object',properties:{amount:{type:'integer',title:'数量B'}}}}}:{type:'object',properties:{'first.layout':{type:'object',properties:{count:{type:'integer',title:'数量A'},flag:{type:'boolean',title:'开关'}}}}}}))
}
const route=(id:string,input_sources:any={})=>({execution_instance_version_id:id,expected_definition_sha256:'a'.repeat(64),input_sources,parameter_policies:[]})
const step=(key:string,id='producer',inputs:any={})=>({key,kind:'structured_text',name:'同名步骤',mode:'ai',approval:'auto',routing:{mode:'fixed',variant_count:1,evaluator:null,routes:[route(id,inputs)]}})
const options:any={definitionSchemaVersion:3,effectiveLimits:{maxRoutingRoutes:3,maxVariants:4,maxGenerationTasks:20},
  materialSourceOptions:['generate','existing','upload'].map(value=>({value,label:value})),
  materialSelectionOptions:[{value:'manual',label:'人工选择'},{value:'unique_version',label:'自动使用唯一确切版本，否则人工接管'}],
  stageOptions:[{value:'structured_text',label:'通用结构化文本',repeatable:true,modes:['ai'],requiresRouting:true,routingModes:['ai'],decisionPolicy:null,candidateEvaluatorOptions:[{value:null,label:'人工选择'},{value:'first_succeeded',label:'自动选择'}]}],
  interactionModeOptions:[{value:'assisted',label:'辅助制作'}],approvalOptions:[{value:'auto',label:'自动推进'}],routingModeOptions:[{value:'fixed',label:'固定路线'}],
  fieldReviewPolicyOptions:[],sourceKindOptions:[{value:'structured',label:'按合同逐字段映射'},{value:'selected_output',label:'上游整体输出'}],
  structureNodeOptions:[{value:'object',label:'组成对象'},{value:'selected_output',label:'上游已选择结果'},{value:'literal',label:'固定值'}],
  textOutputOptions:[],referenceSourceOptions:[],parameterPolicyOptions:[],parameterSourceOptions:[],scopeOptions:[{value:'project',label:'本轮项目'}],presets:[]}
createApp({setup(){
  const draft=ref<any>({schema_version:3,interaction_mode:'assisted',stages:[step('layout-a'),step('layout-b'),step('consumer','consumer',{
    custom:{kind:'structured',template:{kind:'object',fields:{count:{kind:'selected_output',stage_key:'layout-a',output_key:'structured_text.shared',scope:{kind:'project'},selection_policy:'reviewed_selected',path:['first.layout','count']}}}},
  })]})
  const issues=ref<string[]>([])
  function reload(){draft.value.stages[0].name=draft.value.stages[0].name==='同名步骤'?'同名步骤·刷新':'同名步骤'}
  return()=>h('main',{style:{maxWidth:'1080px',margin:'16px auto',padding:'16px',background:'#101820',color:'#e6edf3'}},[
    h('h1','配方动态来源 · 隔离验证'),h('p','仅内存草稿与目录；所有真实API已拦截。不保存、发布或调用模型。'),
    h('button',{class:'button ghost small',onClick:()=>{fail.value=!fail.value;reload()}},fail.value?'恢复目录':'模拟目录失败'),
    h('button',{class:'button ghost small',onClick:()=>readonly.value=!readonly.value},readonly.value?'切换编辑':'切换只读'),
    h(ProductionRecipeEditor,{modelValue:draft.value,options,disabled:readonly.value,'onUpdate:modelValue':(value:any)=>draft.value=value,onValidity:(value:string[])=>issues.value=value}),
    h('p',{role:'status'},issues.value.join('；')||'隔离草稿映射有效（未保存）'),
    h('pre',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},JSON.stringify(draft.value.stages[2]?.routing.routes[0].input_sources,null,2)),
  ])
}}).mount('#app')
