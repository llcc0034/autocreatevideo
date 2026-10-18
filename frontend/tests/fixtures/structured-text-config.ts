// Real configuration components with explicit in-memory catalogs; all API writes fail.
import {createApp,h} from 'vue'
import {createRouter,createMemoryHistory} from 'vue-router'
import ExecutionVersionEditor from '../../src/components/ExecutionVersionEditor.vue'
import ResourceSmokeCreateForm from '../../src/components/ResourceSmokeCreateForm.vue'
import {executionRegistryApi} from '../../src/services/executionRegistryApi'
import {resourceSmokeApi} from '../../src/services/executionResourceSmoke'
import {businessOutputs} from '../../src/services/businessOutputs'
import {platformApi} from '../../src/services/platformApi'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'

api.defaults.adapter=async()=>{throw Error('隔离夹具禁止实际API读写和模型执行')}
const choices={prompt_modes:[{value:'manual',label:'人工完整提示词'}],version_statuses:[],input_kinds:[],target_kinds:[],parameter_types:[],parameter_policies:[],seed_policies:[],identity_options:{subject_kinds:[],scope_kinds:[]},
 target:{deployment_id:'fixture-deployment',model_profile_version_id:'fixture-model',model_family:'fixture',driver:'openai_compatible',workflow_required:false,workflow:null,operations:[
  {value:'text.structured',label:'通用结构化文本',output_parser_keys:['structured_json.v1']},
  {value:'prompt.draft',label:'提示词起草',output_parser_keys:[]},
 ],modes:[]}}
executionRegistryApi.choices=async()=>choices
resourceSmokeApi.choices=async()=>choices
platformApi.listPromptProfileVersions=async()=>[]
platformApi.listWorkflowVersions=async()=>[]
platformApi.getPromptProfileOptions=async()=>({sourceSelectors:[]}) as never
const outputs=['布局A：对象根／动态字段','布局B：列表根／自定义字段'].map((name,i)=>({id:`fixture-output-${i}`,output_key:`fixture-${i}`,name,description:'明确标注的隔离输出目录，不是实际业务配置',version:i+1,revision:1,status:'published' as const,parser_key:'structured_json.v1',parser_contract:{key:'structured_json.v1',format:'json'},data_structure_version:`fixture-structure-${i}`,quality_advisories:{},contract_sha256:'a'.repeat(64),contract_size_bytes:100,contract_schema_version:2,created_at:'',updated_at:'',published_at:null,published_by:null}))
businessOutputs.list=async()=>({count:outputs.length,hasNext:false,hasPrevious:false,results:outputs}) as never
businessOutputs.detail=async id=>{const row=outputs.find(r=>r.id===id);if(!row)throw Error('隔离输出不存在');return row as never}
businessOutputs.options=async()=>({parsers:[{key:'structured_json.v1',label:'通用 JSON 结构',configuration:'structure',description:'隔离结构目录'}],editor:null,parser:null,allowedValueFields:[],fieldSourceOptions:[],responseKeyPolicy:null}) as never
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
createApp({setup(){return()=>h('main',{style:{maxWidth:'1080px',margin:'12px auto',padding:'12px',overflowWrap:'anywhere'}},[
 h('h1','通用结构化文本 · 隔离配置验证'),h('p','真实组件＋模拟目录。仅选择本地草稿；保存、读取输入合同均显式失败，不写业务库、不运行模型。'),
 h('h2','执行实例'),h(ExecutionVersionEditor,{instance:{id:'fixture-instance',revision:1} as never,version:null,deployments:[{id:'fixture-deployment',name:'隔离文本部署',providerDriver:'openai_compatible',status:'unknown'}] as never}),
 h('h2','基础资源验收配置'),h(ResourceSmokeCreateForm,{target:{kind:'deployment',id:'fixture-deployment'}}),
])}}).use(router).mount('#app')
