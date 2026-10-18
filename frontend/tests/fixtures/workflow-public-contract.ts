import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {createMemoryHistory,createRouter,RouterView} from 'vue-router'
import WorkflowVersionEditorView from '../../src/views/WorkflowVersionEditorView.vue'
import WorkflowPublicContract from '../../src/components/WorkflowPublicContract.vue'
import {platformApi} from '../../src/services/platformApi'
import {repository} from '../../src/services/repository'
import {adaptWorkflowTemplateVersion} from '../../src/services/platformAdapters'
import {api} from '../../src/services/api'
import {workflowContractFixture,workflowPreviewFixture} from './workflow-public-contract-data'
import '../../src/styles.css'
import '../../src/platform.css'

const blocked=ref(0),reads=ref(0),mode=ref('saved'),preview=ref(workflowPreviewFixture())
function deny():never{blocked.value++;throw Error('隔离夹具禁止真实网络或写入')}
api.interceptors.request.use(()=>deny());api.defaults.adapter=async()=>deny()
window.fetch=async()=>deny();XMLHttpRequest.prototype.send=()=>deny();navigator.sendBeacon=()=>deny()
for(const name of Object.keys(platformApi))(platformApi as any)[name]=async()=>deny()
const saved={...adaptWorkflowTemplateVersion({}),...workflowContractFixture()}
platformApi.getWorkflowVersion=async()=>{reads.value++;return structuredClone(saved) as any}
for(const name of ['listDeployments','listProviders','listModelProfiles','listWorkflowSmokeAttempts'])(platformApi as any)[name]=async()=>{reads.value++;return []}
platformApi.getAcceptanceSmokePage=async()=>{reads.value++;return {count:1,next:null,previous:null,results:[{subject:'workflow',workflow:{id:saved.id},validation:{state:'not_run',fresh:false,evidenceSha256:''},smoke:{fresh:false}}]} as any}
repository.getProductionCapability=async()=>{reads.value++;return {effectiveLimits:{auditNoteMinCharacters:1,auditNoteMaxCharacters:1000}} as any}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/settings/workflows/:workflowVersionId',component:WorkflowVersionEditorView},{path:'/:pathMatch(.*)*',component:{render:()=>h('p','隔离夹具不挂载其他维护页面')}}]})
await router.push('/settings/workflows/'+saved.id);await router.isReady()
createApp({render:()=>h('main',{style:'padding:16px;max-width:1200px;margin:auto;min-width:0'},[
  h('p',{role:'status'},`隔离展示：只读 ${reads.value}；写入 0；网络守卫 ${blocked.value}。非真实导入或验收。`),
  h('div',{style:'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px'},[
    h('button',{class:'button ghost',onClick:()=>mode.value='saved'},'已保存版本实页'),
    h('button',{class:'button ghost',onClick:()=>{mode.value='preview';preview.value=workflowPreviewFixture()}},'导入草稿合同组件'),
    h('button',{class:'button ghost',onClick:()=>{mode.value='preview';preview.value={...workflowPreviewFixture(),primaryOutputNodeIds:['missing']}}},'缺失主输出示例'),
  ]),
  mode.value==='saved'?h(RouterView):h(WorkflowPublicContract,{preview:preview.value as any}),
])}).use(createPinia()).use(router).mount('#app')
