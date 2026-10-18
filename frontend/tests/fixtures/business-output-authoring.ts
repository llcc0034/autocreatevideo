import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView,useRoute} from 'vue-router'
import {useAuthStore} from '../../src/stores/auth'
import BusinessOutputManager from '../../src/components/BusinessOutputManager.vue'
import FieldMaintenanceSettings from '../../src/components/FieldMaintenanceSettings.vue'
import {outputMaintenanceSession,semanticFixtureReport} from './output-maintenance-session'
import {businessOutputs} from '../../src/services/businessOutputs'
import {dataStructures} from '../../src/services/dataStructures'
import {api} from '../../src/services/api'
import {adaptVisualRulesOutputOptions} from '../../src/services/visualRulesOutput'
import {visualRulesOutputRaw,outputFieldSourceOptions,outputResponseKeyPolicy} from '../visualRulesOutputFixture'
import {scriptFixture} from './script-semantics-data'
import {scriptSemantics} from '../../src/services/scriptSemantics'
import {draftOutputSourceOptions} from '../../src/services/outputFieldSources'
import '../../src/styles.css'
const opened=ref(true),reads=ref(0),writes=ref(0),blocked=ref(0),copy=(v:any)=>JSON.parse(JSON.stringify(v))
const deny=()=>{blocked.value++;throw new Error('隔离验证：禁止未声明网络与写入')}
window.fetch=async()=>deny();XMLHttpRequest.prototype.open=function(){deny()};navigator.sendBeacon=()=>deny();api.interceptors.request.use(()=>deny());api.defaults.adapter=async()=>deny()
for(const client of [businessOutputs,dataStructures,scriptSemantics])for(const key of Object.keys(client))(client as any)[key]=async()=>deny()
const structure:any={id:'fixture-structure',name:'响应内容结构 · 原中文 🟢',structure_key:'structure-family',version:3,status:'published',description:'这是公共定义，不是模型结果',nodes:[]}
const base:any={id:'output-v1',output_key:'fixture-family',version:1,revision:2,name:'通用JSON响应 · 隔离',description:'精确结构 → JSON解析 → 执行实例使用',parser_key:'structured_json.v1',data_structure_version:structure.id,status:'published',contract_sha256:'a'.repeat(64),quality_advisories:{},parser_contract:{key:'structured_json.v1',format:'json'}}
const scriptStructure={...copy(structure),id:'script-structure',name:'嵌套剧本响应结构 · 原字段',structure_key:'script-structure-family'}
const visual:any={...copy(base),id:'visual-v1',output_key:'visual-family',name:'影视规则 · 隔离复杂路径',parser_key:'visual_rules.v2',quality_advisories:{schema_version:1,negative_constraints_max_items:0},parser_contract:{key:'visual_rules.v2',operation:visualRulesOutputRaw.operation,response_schema_version:2,fields:copy(visualRulesOutputRaw.outputs),response_example:copy(visualRulesOutputRaw.response_example),allowed_values:{},response_keys:{continuity_summary:'rules.0.summary',color_script:'rules.1.palette',negative_prompt:'rules.2.avoid'}}}
const semantic:any={...copy(base),id:'semantic-v1',output_key:'semantic-family',name:'嵌套剧本 · 隔离复杂映射',parser_key:'script_semantics.v1',data_structure_version:scriptStructure.id,parser_contract:{key:'script_semantics.v1',format:'json',mapping:copy(scriptFixture.layouts.b.mapping)}}
visual.parser_contract.response_example={schema_version:2,rules:[{summary:visualRulesOutputRaw.response_example.continuity_summary},{palette:copy(visualRulesOutputRaw.response_example.color_design)},{avoid:[]}]}
const rows:any[]=[copy(base),visual,semantic]
const maintenance=outputMaintenanceSession(()=>reads.value++,()=>writes.value++,deny,[structure,scriptStructure])
businessOutputs.list=async(page=1,status='',search='',family)=>{reads.value++;const found=rows.filter(r=>(!status||r.status===status)&&r.name.includes(search)&&(!family||r.output_key===family));return{count:found.length,results:copy(found.slice(page-1,page)),hasNext:page<found.length,hasPrevious:page>1}}
businessOutputs.options=async()=>{reads.value++;return{parsers:[{key:'structured_json.v1',label:'通用JSON结构',description:'字段与类型由公共结构维护',configuration:'structure'},{key:'visual_rules.v2',label:'影视规则',description:'响应内容转换为业务规则',configuration:'visual_rules'},{key:'script_semantics.v1',label:'剧本语义',description:'精确结构中的字段映射为实体、分集和镜头声明',configuration:'script_semantics'}],editor:adaptVisualRulesOutputOptions(visualRulesOutputRaw),parser:{...base.parser_contract,key:'visual_rules.v2',fields:visualRulesOutputRaw.outputs,response_example:visualRulesOutputRaw.response_example},allowedValueFields:[{key:'base_colors',label:'基础色',choices:['black','teal']},{key:'preserve_natural_skin_exposure',label:'肤色曝光保留',choices:[true,false]}],fieldSourceOptions:outputFieldSourceOptions,responseKeyPolicy:outputResponseKeyPolicy} as any}
businessOutputs.detail=async id=>{reads.value++;const row=rows.find(r=>r.id===id);if(!row)throw{response:{status:404}};return copy(row)}
businessOutputs.save=async(data,source)=>{writes.value++;await new Promise(r=>setTimeout(r,150));if(data.name.startsWith('拒绝'))throw{response:{status:400,data:{error:{fields:{data_structure_version:['隔离校验：请复核此结构版本']}}}}};let row=source?rows.find(r=>r.id===source.id):null
 if(row)Object.assign(row,copy(data),{revision:row.revision+1});else{row={...copy(base),...copy(data),id:'new-'+rows.length,output_key:'new-family-'+rows.length,version:1,revision:1,status:'draft'};rows.push(row)}
 if(data.parser_key==='visual_rules.v2'){const draft=draftOutputSourceOptions(adaptVisualRulesOutputOptions(visualRulesOutputRaw),data.field_sources||{},outputFieldSourceOptions,data.response_keys||{});row.parser_contract={...copy(visual.parser_contract),...copy(data),response_example:copy(draft.responseExample),fields:visualRulesOutputRaw.outputs.map(field=>({...field,response_field:data.field_sources?.[field.field]||field.response_field}))}}
 if(data.parser_key==='script_semantics.v1')row.parser_contract={key:'script_semantics.v1',format:'json',mapping:copy(data.semantic_mapping)}
 if(data.name.startsWith('超时'))throw{response:{status:504}};return copy(row)}
businessOutputs.action=async(source,action)=>{writes.value++;const row=rows.find(r=>r.id===source.id);if(!row||row.revision!==source.revision)throw{response:{status:400}}
 if(action==='successor'){if(rows.some(r=>r.output_key===row.output_key&&r.version>row.version))throw{response:{status:400}};const next={...copy(row),id:'successor-'+rows.length,version:row.version+1,revision:1,status:'draft'};rows.push(next);return copy(next)}
 row.status=action==='publish'?'published':'retired';row.revision++;return copy(row)}
scriptSemantics.options=async()=>{reads.value++;return copy(scriptFixture.options) as any}
scriptSemantics.preview=async(row,text)=>{reads.value++;if(text==='读取失败')throw new Error('隔离预览失败');return semanticFixtureReport(rows.find(value=>value.id===row.id),row,text) as any}
api.get=async(url:string,options:any)=>{if(url!=='/registry-relations/')return deny();reads.value++;const kind=options?.params?.kind,id=options?.params?.ids,row=kind==='structure'?maintenance.rows().find(row=>row.id===id):rows.find(r=>r.id===id);if(!row||row.id!==id)throw{response:{status:404}};return{data:{count:1,next:null,previous:null,results:[{id,kind,label:row.name,status:row.status,config_url:kind==='structure'?'/settings?section=fields&structure_version='+id:'/settings?section=executions&output_version='+id,overview_url:'/settings/acceptance-smoke?view=relations&kind='+kind+'&ids='+id,acceptance_url:'',groups:[],notices:[],capabilities:[]}],generated_at:'2026-10-10T01:00:00Z',context_label:'隔离定义'}} as any}
const Screen={render:()=>h('main',{style:'padding:16px'},[h('h1','业务输出作者 · 内存隔离'),h('p','名称以拒绝／超时开头模拟明确错误／保存后丢失回执；不连接现用后台。'),h('button',{class:'button primary',onClick:()=>opened.value=true},'打开输出作者'),opened.value?h(BusinessOutputManager,{onClose:()=>opened.value=false}):null,h('output',{style:'position:fixed;bottom:0;left:0;right:0;z-index:9000;pointer-events:none;background:#18312b;padding:5px;font-size:12px'},'隔离内存 · 写入 '+writes.value+' · 读取 '+reads.value+' · 网络守卫 '+blocked.value)])}
const pinia=createPinia();useAuthStore(pinia).user={id:905,username:'fixture',isStaff:true,isSuperuser:true} as any
const Maintenance={setup(){const route=useRoute();return()=>h('main',{style:'padding:16px'},[h('h1','结构维护 · 仅隔离目录'),h('p','测试传输重定向；实际组件与精确路由参数。关闭此窗口后返回原输出作者，原引用不自动改变。'),h(FieldMaintenanceSettings,{structureVersionId:String(route.query.structure_version||'')}),h('output','隔离维护 · 写入 '+writes.value+' · 读取 '+reads.value+' · 网络守卫 '+blocked.value)])}}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Screen},{path:'/settings',component:maintenance.maintenanceRoute?Maintenance:Screen},{path:'/settings/acceptance-smoke',component:Screen}]})
router.push(maintenance.maintenanceRoute||'/').then(()=>createApp({render:()=>h(RouterView)}).use(pinia).use(router).mount('#app'))
