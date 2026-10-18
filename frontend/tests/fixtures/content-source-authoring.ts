import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView,useRoute} from 'vue-router'
import FieldMaintenanceSettings from '../../src/components/FieldMaintenanceSettings.vue'
import {authorMaintenanceSession} from './author-maintenance-session'
import ContentSourceManager from '../../src/components/ContentSourceManager.vue'
import {useAuthStore} from '../../src/stores/auth'
import {contentSources} from '../../src/services/contentSources'
import {dataStructures} from '../../src/services/dataStructures'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'
const writes=ref(0),reads=ref(0),blocked=ref(0),mode=ref('success'),open=ref(true),copy=(x:any)=>JSON.parse(JSON.stringify(x))
function deny():never{blocked.value++;throw Error('隔离夹具禁止真实网络、配置或模型操作')}
api.interceptors.request.use(()=>deny());api.defaults.adapter=async()=>deny()
window.fetch=async()=>deny();XMLHttpRequest.prototype.send=()=>deny();navigator.sendBeacon=()=>deny()
for(const group of [contentSources,dataStructures])for(const key of Object.keys(group))(group as any)[key]=async()=>deny()
const reader={protocol:'request_text/v1',selector:'request.theme',scope_kind:'request',value_kind:'text',snapshot_policy:'confirmed_request'}
const row=(id:string,status:string,version:number)=>({id,source_key:'source-fixture',version,name:'连续剧故事构想',description:'读取本次确认的创作构想，保留中文与原始名称。',reader_selector:'request.theme',reader:copy(reader),status,revision:2,data_structure_version:'structure-fixture',contract_sha256:'a'.repeat(64),contract_size_bytes:450})
const rows:any[]=[row('published-source','published',1)]
const structure:any={id:'structure-fixture',structure_key:'text-fixture',name:'文字原值结构',description:'公共文字字段根节点；此处不是项目正文。',version:1,status:'published',nodes:[{node_key:'root',parent_key:null,field_definition_version:'field-fixture',property_name:'',position:0,required:true,allow_null:false,allow_blank:false,allow_empty:false,has_default:false,default_value:null,constraints:{}}]}
contentSources.list=async(page,status,search,family)=>{reads.value++;const list=rows.filter(r=>(!status||r.status===status)&&(!family||r.source_key===family)&&(!search||r.name.includes(search)));return{count:list.length,next:null,results:copy(list)}}
contentSources.get=async id=>{reads.value++;const value=rows.find(r=>r.id===id);if(!value)throw{response:{status:404}};return copy(value)}
contentSources.readers=async()=>{reads.value++;return[{value:'request.theme',label:'本次故事构想',group_label:'本次请求输入',description:'从当前任务填写并确认的构想中读取文字。',record_location:'创建执行准备 → 本次故事构想',execution_usage:'保存准备时冻结原值，不改名或翻译。',reader:copy(reader)}]}
async function write(action:()=>any){writes.value++;await new Promise(r=>setTimeout(r,250));if(mode.value==='reject')throw{response:{status:400,data:{error:{code:'invalid',message:'隔离校验拒绝',fields:{name:['请核对业务名称']}}}}};if(mode.value==='unknown-empty')throw Error('隔离响应未知');const value=action();if(mode.value==='unknown-saved')throw{response:{status:504}};return copy(value)}
contentSources.save=(payload,original)=>write(()=>{if(original){const target=rows.find(r=>r.id===original.id);if(!target||target.status!=='draft'||target.revision!==original.revision)throw{response:{status:400}};Object.assign(target,payload,{revision:target.revision+1});return target}const created={...row('created-'+writes.value,'draft',1),source_key:'new-family-'+writes.value,...payload};rows.push(created);return created})
contentSources.action=(source,action)=>write(()=>{const target=rows.find(r=>r.id===source.id);if(!target||target.revision!==source.revision)throw{response:{status:400}};if(action==='successor'){const created={...copy(target),id:'successor-'+writes.value,version:target.version+1,status:'draft',revision:1};rows.push(created);return created}target.status=action==='publish'?'published':'retired';target.revision++;return target})
dataStructures.list=async()=>{reads.value++;return{hasNext:false,hasPrevious:false,count:1,results:[copy(structure)]}}
dataStructures.get=async id=>{reads.value++;if(id!==structure.id)throw{response:{status:404}};return copy(structure)}
api.get=async(url:string,options:any)=>{
  if(url!=='/registry-relations/')return deny()
  reads.value++;const kind=options?.params?.kind,id=options?.params?.ids
  const value=kind==='source'?rows.find(r=>r.id===id):kind==='structure'&&id===structure.id?structure:null
  if(!value)throw{response:{status:404}}
  return{data:{count:1,next:null,previous:null,results:[{id,kind,label:value.name,status:value.status,config_url:`/settings?section=fields&${kind==='structure'?'structure_version':'source_version'}=${id}`,overview_url:`/settings/acceptance-smoke?view=relations&kind=${kind}&ids=${id}`,acceptance_url:'',groups:[],notices:[],capabilities:[]}],generated_at:'2026-10-09T11:30:00Z',context_label:'隔离关系'}} as any
}
const maintenance=authorMaintenanceSession(structure,{type:'string'},()=>reads.value++,()=>writes.value++,deny)
const Maintenance={setup(){const route=useRoute();return()=>h(FieldMaintenanceSettings,{structureVersionId:String(route.query.structure_version||'')})}}
const Screen={render:()=>h('main',[h('div',{style:'padding:16px'},[
  h('h1',{style:'font-size:20px'},'内容来源作者 · 纯内存隔离'),
  h('label',['写入模式',h('select',{value:mode.value,onChange:(e:Event)=>mode.value=(e.target as HTMLSelectElement).value},['success','reject','unknown-empty','unknown-saved'].map(v=>h('option',{value:v},v)))]),
  h('button',{onClick:()=>open.value=true},'打开内容来源')]),open.value?h(ContentSourceManager,{onClose:()=>open.value=false}):null])}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Screen},{path:'/settings',component:maintenance?.route?Maintenance:Screen}]})
const pinia=createPinia(),auth=useAuthStore(pinia);auth.user={id:901,username:'fixture',isStaff:true,isSuperuser:true} as any
await router.push(maintenance?.route||'/');await router.isReady()
createApp({render:()=>h('main',[h('p',{'data-fixture-evidence':''},`内存写入 ${writes.value} · 读取 ${reads.value} · 网络守卫 ${blocked.value}`),h(RouterView),...(maintenance?[h('details',[h('summary','跨维护隔离目录回读'),h('pre',{'data-maintenance-receipts':''},JSON.stringify(maintenance.report(),null,2))])]:[])])}).use(pinia).use(router).mount('#app')
