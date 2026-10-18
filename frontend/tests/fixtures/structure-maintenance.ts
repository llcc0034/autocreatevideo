// Real components with isolated memory only. No business API, credentials or model calls.
import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView,useRoute} from 'vue-router'
import DataStructureManager from '../../src/components/DataStructureManager.vue'
import FieldMaintenanceSettings from '../../src/components/FieldMaintenanceSettings.vue'
import {authorMaintenanceSession} from './author-maintenance-session'
import {platformApi} from '../../src/services/platformApi'
import {useAuthStore} from '../../src/stores/auth'
import {api} from '../../src/services/api'
import {dataStructures} from '../../src/services/dataStructures'
import {fieldDefinitionVersions} from '../../src/services/fieldDefinitionVersions'
import '../../src/styles.css'
import '../../src/platform.css'
const copy=(value:any)=>JSON.parse(JSON.stringify(value)),reads=ref(0),writes=ref(0),blocked=ref(0),opened=ref(true)
function deny():never{blocked.value++;throw Error('隔离页面禁止真实网络')}
api.interceptors.request.use(()=>deny());api.defaults.adapter=async()=>deny()
window.fetch=async()=>deny();XMLHttpRequest.prototype.send=()=>deny();navigator.sendBeacon=()=>deny()
for(const group of [dataStructures,fieldDefinitionVersions,platformApi])for(const key of Object.keys(group))(group as any)[key]=async()=>deny()
const defaults={parent_key:null,field_definition_version:null,property_name:'',position:0,required:false,allow_null:false,allow_blank:true,allow_empty:true,has_default:false,default_value:null,constraints:{}}
const fields=['object','text'].map((kind,index)=>({id:'field-v'+index,field_definition:'field-'+index,field_key:'fixture_'+kind,version:1,status:'published' as const,label:['创作输入','镜头动作'][index]!,category:'fixture',value_type:kind as 'text'|'object',unit:'',description:'隔离公共定义，不是实际任务内容',impact:'',value_options:[],preview_example:null}))
const base={structure_key:'fixture-family',name:'创作输入 · 精确旧版（隔离）',description:'公共字段 → 结构 → 内容来源／策略／输出；原中文 🟢 不翻译。',nodes:[{...defaults,node_key:'root',field_definition_version:fields[0]!.id},{...defaults,node_key:'motion',parent_key:'root',property_name:'motion_note',field_definition_version:fields[1]!.id,position:1,has_default:true,default_value:''}]}
const rows:any[]=[{...copy(base),id:'structure-v1',version:1,status:'published'},{...copy(base),id:'structure-v7',version:7,status:'draft',name:'同家族已有草稿 v7'}]
dataStructures.options=async()=>{reads.value++;return{node_defaults:copy(defaults),statuses:[{value:'draft',label:'草稿'},{value:'published',label:'可用'},{value:'retired',label:'停用'}],value_schemas:{text:{type:'string'},object:{type:'object'}},constraint_schemas:{text:{type:'object',properties:{min_length:{type:'integer',minimum:0,title:'最少字符'}},additionalProperties:false},object:{type:'object',properties:{},additionalProperties:false}}}}
dataStructures.list=async(page,search,status,family)=>{reads.value++;const matches=rows.filter(row=>row.name.includes(search)&&(!status||row.status===status)&&(!family||row.structure_key===family)),size=family?1:20;return{count:matches.length,results:copy(matches.slice((page-1)*size,page*size)),hasNext:page*size<matches.length,hasPrevious:page>1}}
dataStructures.get=async id=>{reads.value++;const row=rows.find(r=>r.id===id);if(!row)throw{response:{status:404}};return copy(row)}
async function write(name:string,action:()=>any){writes.value++;await new Promise(r=>setTimeout(r,200));if(name.startsWith('拒绝'))throw{response:{status:400,data:{error:{code:'invalid',fields:{'nodes.motion.property_name':['隔离校验：请复核此节点属性名']}}}}};const row=action();if(name.startsWith('超时'))throw{response:{status:504}};return copy(row)}
dataStructures.create=(content,source)=>write(content.name||'',()=>{const original=rows.find(r=>r.id===source),family=original?.structure_key||'new-family-'+rows.length,version=Math.max(0,...rows.filter(r=>r.structure_key===family).map(r=>r.version))+1,row={...copy(original||base),...copy(content),structure_key:family,id:'structure-'+(rows.length+1),version,status:'draft'};rows.push(row);return row})
dataStructures.update=(id,content)=>write(content.name||'',()=>{const row=rows.find(r=>r.id===id);Object.assign(row,copy(content));return row})
dataStructures.transition=(id,action)=>write('',()=>{const row=rows.find(r=>r.id===id);row.status=action==='publish'?'published':'retired';return row})
dataStructures.schema=async()=>{reads.value++;return{valid:true,json_schema:{type:'object'},errors:{}}}
dataStructures.preview=async(id,value)=>{reads.value++;return{preview_only:true,structure_version_id:id,valid:true,value,defaults:[],errors:{}}}
fieldDefinitionVersions.catalog=async(_page,search)=>{reads.value++;const matches=fields.filter(r=>r.label.includes(search));return{count:matches.length,results:copy(matches)}}
fieldDefinitionVersions.get=async id=>{reads.value++;const row=fields.find(r=>r.id===id);if(!row)throw{response:{status:404}};return copy(row)}
api.get=async(url:string,options:any)=>{
  if(url!=='/registry-relations/')return deny()
  reads.value++;const id=options?.params?.ids,kind=options?.params?.kind,row=(kind==='structure'?rows:fields).find(r=>r.id===id)
  if(!row)throw{response:{status:404}}
  return{data:{count:1,next:null,previous:null,results:[{id,kind,label:row.name||row.label,status:row.status,config_url:'/settings?section=fields&'+(kind==='structure'?'structure_version='+id:'field_definition='+row.field_definition+'&field_version='+id),overview_url:'/settings/acceptance-smoke?view=relations&kind='+kind+'&ids='+id,acceptance_url:'',groups:[],notices:[],capabilities:[]}],generated_at:'2026-10-09T13:30:00Z',context_label:'隔离关系'}} as any
}
const structureOptions=dataStructures.options
const maintenance=authorMaintenanceSession(rows[0],{type:'object',properties:{motion_note:{type:'string'}}},()=>reads.value++,()=>writes.value++,deny,{identity:{id:'field-1',key:'fixture_text',isActive:true,displayOrder:0},version:fields[1]})
dataStructures.options=structureOptions
const Maintenance={setup(){const route=useRoute();return()=>h(FieldMaintenanceSettings,{fieldDefinitionId:String(route.query.field_definition||''),fieldVersionId:String(route.query.field_version||'')})}}
const Screen={render:()=>h('main',{style:'padding:16px'},[h('h1','结构作者 · 内存隔离'),h('p','名称以“拒绝”开头模拟节点错误，以“超时”开头模拟保存成功但回执丢失。'),h('button',{class:'button primary',onClick:()=>opened.value=true},'打开结构作者'),opened.value?h(DataStructureManager,{onClose:()=>opened.value=false}):null,h('output',{style:'position:fixed;bottom:0;left:0;right:0;z-index:9000;pointer-events:none;background:#18312b;padding:5px;font-size:12px'},'隔离内存 · 写入 '+writes.value+' · 读取 '+reads.value+' · 网络守卫 '+blocked.value)])}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Screen},{path:'/settings',component:maintenance?.route?Maintenance:Screen}]})
const pinia=createPinia(),auth=useAuthStore(pinia);auth.user={id:904,username:'fixture',isStaff:true,isSuperuser:true} as any
await router.push(maintenance?.route||'/');await router.isReady()
createApp({render:()=>{const revision=reads.value+writes.value;return h('div',[h(RouterView),maintenance?h('details',{'data-maintenance-evidence':revision},[h('summary','隔离维护与原值核对'),h('p',{'data-fixture-evidence':''},`内存写入 ${writes.value} · 读取 ${reads.value} · 网络守卫 ${blocked.value}`),h('pre',{style:'white-space:pre-wrap;overflow-wrap:anywhere'},JSON.stringify(maintenance.report(),null,2))]):null])}}).use(pinia).use(router).mount('#app')
