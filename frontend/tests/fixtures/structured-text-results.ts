// Original component with in-memory API responses only; no real request can leave.
import {createApp,h,ref} from 'vue'
import {createRouter,createMemoryHistory} from 'vue-router'
import ProductionStructuredTextResults from '../../src/components/ProductionStructuredTextResults.vue'
import {platformApi} from '../../src/services/platformApi'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'

api.interceptors.request.use(()=>{throw new Error('隔离夹具禁止真实API请求')})
const hash='a'.repeat(64)
const output={id:'output',sha256:hash,data_structure:{id:'structure'}}
const hint=(label:string,type:string)=>({title:label,'x-field-definition':{label,value_type:type,field_definition_version_id:label}})
const schema={type:'object',...hint('候选输出','object'),properties:{count:{type:'integer',...hint('数量','integer')},enabled:{type:'boolean',...hint('是否启用','boolean')},note:{anyOf:[{type:'string',...hint('说明','string')},{type:'null'}]}}}
const artifact:any={id:'candidate',productionRun:'run',stageRun:'first',role:'structured_text_candidate',status:'available',variantGroup:'group',variantIndex:1,sha256:hash,manifestMetadataSha256:hash,
  textCandidate:{operation:'text.structured',preparation_id:'prep',candidate:{schema_version:1,output_version_id:'output',output_contract_sha256:hash,structure_version_id:'structure',value:{count:0,enabled:false,note:null,extra:'实际返回的未映射字段仍保留'},defaults:[{path:'response.count',value:0,structure_version_id:'structure'}]},output_contract:output,json_schema:schema,structure_warnings:[{path:'response.extra',message:'未映射字段保留，供人工查看'}]}}
let fail=false,empty=false
platformApi.listArtifactPage=async()=>{if(fail)throw Error('隔离清单读取失败');return{results:empty?[]:[{...artifact}],count:empty?0:1,hasNext:false,hasPrevious:false}}
platformApi.getArtifact=async()=>structuredClone(artifact)
platformApi.selectArtifact=async()=>{artifact.status='selected';return {id:crypto.randomUUID(),action:'select',result_kind:'artifact_selection',target_id:crypto.randomUUID(),created_at:new Date().toISOString()} as any}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}},{name:'workflows',path:'/fixture-tasks',component:{render:()=>h('p','隔离任务路由：不读取任何真实任务')}}]})
createApp({setup(){
  const stage=ref<any>({id:'first',stageKey:'first',ordinal:1,status:'awaiting_input',artifactSelectionDecisions:[]})
  const run=ref<any>({id:'run',planSource:'recipe',status:'awaiting_input',executionPlan:{sha256:hash,value:{recipe:{definition:{stages:[{key:'first',kind:'structured_text',name:'镜头资料整理',routing:{evaluator:null}}]}}}}})
  const busy=ref(false)
  function changed(){stage.value={...stage.value,artifactSelectionDecisions:[{id:'decision',variantGroup:'group',revision:1,artifact:'candidate',selectionMode:'manual',selectedBy:'fixture-actor'}]}}
  return()=>h('main',{style:{maxWidth:'1000px',margin:'20px auto',padding:'16px',background:'#101820',color:'#e6edf3'}},[
    h('h1','通用文本结果 · 隔离验证'),h('p','只操作内存夹具；不保存业务配置、不提交模型、不审核真实内容。'),
    h('nav',{style:{display:'flex',flexWrap:'wrap',gap:'8px',margin:'12px 0'}},[
      h('button',{class:'button ghost small',disabled:busy.value,onClick:()=>{fail=!fail}},'切换读取失败'),
      h('button',{class:'button ghost small',disabled:busy.value,onClick:()=>{empty=!empty}},'切换无候选'),
      h('button',{class:'button ghost small',disabled:busy.value,onClick:()=>{artifact.status='selected';stage.value={...stage.value,artifactSelectionDecisions:[{id:'auto',variantGroup:'group',revision:2,artifact:'candidate',selectionMode:'automatic',selectedBy:null}]}}},'演示自动选择事实'),
    ]),
    h(ProductionStructuredTextResults,{run:run.value,stage:stage.value,disabled:busy.value,onProtection:(value:any)=>busy.value=value.active,onChanged:changed}),
  ])
}}).use(router).mount('#app')
