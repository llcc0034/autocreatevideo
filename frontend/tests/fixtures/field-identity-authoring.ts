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
const pageSize=ref(20)
function deny():never{blocked.value++;throw Error('隔离页禁止真实网络和配置写入')}
api.interceptors.request.use(()=>deny());api.defaults.adapter=async()=>deny()
window.fetch=async()=>deny();XMLHttpRequest.prototype.send=()=>deny();navigator.sendBeacon=()=>deny()
for(const group of [platformApi,fieldDefinitionVersions])for(const key of Object.keys(group))(group as any)[key]=async()=>deny()
const rows:any[]=[{id:'identity-original',key:'shot.action',isActive:true,displayOrder:1}]
platformApi.fieldDefinitionPage=async(page,search,inactive)=>{reads.value++;const matches=rows.filter(row=>(inactive||row.isActive)&&row.key.includes(search)),size=pageSize.value;return{count:matches.length,results:copy(matches.slice((page-1)*size,page*size)),hasNext:page*size<matches.length,hasPrevious:page>1}}
platformApi.getFieldDefinition=async id=>{reads.value++;const row=rows.find(r=>r.id===id);if(!row)throw{response:{status:404}};return copy(row)}
async function write(action:()=>any){writes.value++;await new Promise(r=>setTimeout(r,300));if(mode.value==='reject')throw{response:{status:400,data:{error:{code:'invalid',fields:{key:['隔离校验：唯一键格式不符']}}}}};if(mode.value==='unknown-empty')throw{response:{status:504}};const row=action();if(mode.value==='unknown-saved')throw{response:{status:504}};return copy(row)}
platformApi.createFieldDefinition=payload=>write(()=>{if(rows.some(r=>r.key===payload.key))throw{response:{status:400}};const row={id:'identity-'+(rows.length+1),key:payload.key,isActive:false,displayOrder:rows.length+1};rows.push(row);return row})
platformApi.updateFieldDefinition=(id,payload)=>write(()=>{const row=rows.find(r=>r.id===id);if(!row)throw{response:{status:404}};row.isActive=payload.is_active;return row})
fieldDefinitionVersions.options=async()=>{reads.value++;return{value_types:[{value:'text',label:'文本'}],statuses:[{value:'draft',label:'草稿'},{value:'published',label:'已启用'}]}}
fieldDefinitionVersions.list=async()=>{reads.value++;return[]}
api.get=async(url:string,options:any)=>{
  if(url!=='/registry-relations/')return deny()
  reads.value++;const id=options?.params?.ids,row=rows.find(r=>r.id===id)
  if(!row||options?.params?.kind!=='field')throw{response:{status:404}}
  return{data:{count:1,next:null,previous:null,results:[{id,kind:'field',label:row.key,status:row.isActive?'active':'inactive',config_url:'/settings?section=fields',overview_url:'/settings/acceptance-smoke?view=relations&kind=field&ids='+id,acceptance_url:'',groups:[],notices:[],capabilities:[]}],generated_at:'2026-10-09T12:30:00Z',context_label:'隔离关系'}} as any
}
const Screen={render:()=>h('main',{style:'padding:16px'},[h('h1',{style:'font-size:20px'},'字段身份作者 · 纯内存隔离'),h('p',`内存写入 ${writes.value} · 读取 ${reads.value} · 网络守卫 ${blocked.value}`),h('label',['写入模式',h('select',{'aria-label':'写入模式',value:mode.value,onChange:(e:Event)=>mode.value=(e.target as HTMLSelectElement).value},['success','reject','unknown-empty','unknown-saved'].map(v=>h('option',{value:v},v)))]),h('label',['隔离服务每页条数',h('select',{'aria-label':'隔离服务每页条数',value:pageSize.value,onChange:(e:Event)=>pageSize.value=Number((e.target as HTMLSelectElement).value)},[1,20].map(v=>h('option',{value:v},v)))]),h(FieldMaintenanceSettings)])}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Screen},{path:'/settings',component:Screen}]})
const pinia=createPinia(),auth=useAuthStore(pinia);auth.user={id:903,username:'fixture',isStaff:true,isSuperuser:true} as any
createApp({render:()=>h(RouterView)}).use(pinia).use(router).mount('#app')
