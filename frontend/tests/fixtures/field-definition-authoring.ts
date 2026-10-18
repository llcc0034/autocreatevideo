import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView} from 'vue-router'
import FieldMaintenanceSettings from '../../src/components/FieldMaintenanceSettings.vue'
import {useAuthStore} from '../../src/stores/auth'
import {fieldDefinitionVersions} from '../../src/services/fieldDefinitionVersions'
import {platformApi} from '../../src/services/platformApi'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'
const reads=ref(0),writes=ref(0),blocked=ref(0),mode=ref('success'),copy=(x:any)=>JSON.parse(JSON.stringify(x))
function deny():never{blocked.value++;throw Error('隔离页禁止真实网络、模型、数据和配置操作')}
api.interceptors.request.use(()=>deny());api.defaults.adapter=async()=>deny()
window.fetch=async()=>deny();XMLHttpRequest.prototype.send=()=>deny();navigator.sendBeacon=()=>deny()
for(const group of [platformApi,fieldDefinitionVersions])for(const key of Object.keys(group))(group as any)[key]=async()=>deny()
const identity:any={id:'field-fixture',key:'shot.action',isActive:true,displayOrder:0}
const version=(id:string,status:string,n:number)=>({id,field_definition:identity.id,field_key:identity.key,version:n,status,label:'镜头动作',category:'镜头创作',value_type:'text',unit:'',description:'描述动作与时间；保留人物原名 🟢，实际内容在任务中提供。',impact:'结构与策略共用此定义，不自动更新既有引用。',value_options:[],preview_example:'服务器类型样例（不是默认值）'})
const rows:any[]=[version('definition-v1','published',1),version('definition-v7','published',7)]
platformApi.fieldDefinitionPage=async()=>{reads.value++;return{count:1,results:[copy(identity)],hasNext:false,hasPrevious:false}}
platformApi.getFieldDefinition=async id=>{reads.value++;if(id!==identity.id)throw{response:{status:404}};return copy(identity)}
fieldDefinitionVersions.options=async()=>{reads.value++;return{value_types:[{value:'text',label:'文本'},{value:'integer',label:'整数'},{value:'enum',label:'枚举'},{value:'object',label:'对象'}],statuses:[{value:'draft',label:'草稿'},{value:'published',label:'已启用'},{value:'retired',label:'已停用'}]}}
fieldDefinitionVersions.list=async field=>{reads.value++;if(field!==identity.id)throw{response:{status:404}};return copy(rows)}
fieldDefinitionVersions.get=async id=>{reads.value++;const row=rows.find(r=>r.id===id);if(!row)throw{response:{status:404}};return copy(row)}
async function write(action:()=>any){writes.value++;await new Promise(r=>setTimeout(r,250));if(mode.value==='reject')throw{response:{status:400,data:{error:{code:'invalid',fields:{label:['隔离校验：请检查名称']}}}}};if(mode.value==='unknown-empty')throw{response:{status:504}};const result=action();if(mode.value==='unknown-saved')throw{response:{status:504}};return copy(result)}
fieldDefinitionVersions.create=(field,content,source)=>write(()=>{if(field!==identity.id||source&&!rows.some(r=>r.id===source))throw{response:{status:400}};const n=Math.max(...rows.map(r=>r.version))+1;const created={...version('definition-v'+n,'draft',n),...content};rows.push(created);return created})
fieldDefinitionVersions.update=(id,content)=>write(()=>{const target=rows.find(r=>r.id===id);if(!target||target.status!=='draft')throw{response:{status:400}};Object.assign(target,content);return target})
fieldDefinitionVersions.transition=(id,action)=>write(()=>{const target=rows.find(r=>r.id===id);if(!target)throw{response:{status:404}};target.status=action==='publish'?'published':'retired';return target})
api.get=async(url:string,options:any)=>{
  if(url!=='/registry-relations/')return deny()
  reads.value++;const kind=options?.params?.kind,id=options?.params?.ids
  const row=kind==='field'&&id===identity.id?identity:kind==='field_version'?rows.find(r=>r.id===id):null
  if(!row)throw{response:{status:404}}
  return{data:{count:1,next:null,previous:null,results:[{id,kind,label:row.label||row.key,status:row.status||'active',config_url:'/settings?section=fields',overview_url:`/settings/acceptance-smoke?view=relations&kind=${kind}&ids=${id}`,acceptance_url:'',groups:[],notices:[],capabilities:[]}],generated_at:'2026-10-09T12:00:00Z',context_label:'隔离关系'}} as any
}
const Screen={render:()=>h('main',{style:'padding:16px'},[h('h1',{style:'font-size:20px'},'字段定义作者 · 纯内存隔离'),h('p',`内存写入 ${writes.value} · 读取 ${reads.value} · 网络守卫 ${blocked.value}`),h('label',['写入模式',h('select',{value:mode.value,onChange:(e:Event)=>mode.value=(e.target as HTMLSelectElement).value},['success','reject','unknown-empty','unknown-saved'].map(v=>h('option',{value:v},v)))]),h(FieldMaintenanceSettings)])}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Screen},{path:'/settings',component:Screen}]})
const pinia=createPinia(),auth=useAuthStore(pinia);auth.user={id:902,username:'fixture',isStaff:true,isSuperuser:true} as any
createApp({render:()=>h(RouterView)}).use(pinia).use(router).mount('#app')
