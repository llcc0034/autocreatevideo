import {createApp,h,ref} from 'vue'
import {createRouter,createMemoryHistory,RouterView,useRoute} from 'vue-router'
import {createPinia} from 'pinia'
import {useAuthStore} from '../../src/stores/auth'
import FieldMaintenanceSettings from '../../src/components/FieldMaintenanceSettings.vue'
import {authorMaintenanceSession} from './author-maintenance-session'
import PromptProfileSettings from '../../src/components/PromptProfileSettings.vue'
import {platformApi} from '../../src/services/platformApi'
import {adaptPromptProfileOptions,adaptPromptProfileVersion,adaptPromptProfileFieldRule} from '../../src/services/platformAdapters'
import {fieldDefinitionVersions} from '../../src/services/fieldDefinitionVersions'
import {dataStructures} from '../../src/services/dataStructures'
import {api} from '../../src/services/api'
import {executionRegistryApi} from '../../src/services/executionRegistryApi'
import '../../src/styles.css'
import '../../src/platform.css'

// This page runs original writers against memory only. No production API allowed.
const reads=ref(0),writes=ref(0),blocked=ref(0),mode=ref('success')
const copy=(v:any)=>JSON.parse(JSON.stringify(v))
const complex = new URLSearchParams(location.search).get('complex') === '1'
const matrix = new URLSearchParams(location.search).get('matrix') === '1'
const receipts=ref<{id:string;payload:unknown}[]>([])
const requiredTemplateKeys=['reference','entity_reference','retention','reference_usage','shot','reviewed_shot','summary','shot_opening','shot_transition']
const source='request.structured_input',choice=(value:string,label=value)=>({value,label})
const structure={id:'structure-v2',structure_key:'fixture',version:2,name:'动作输入结构',status:'published' as const,description:'独立定义，不保存项目值'}
const schema={type:'object',properties:{motion:{type:'string',title:'动作描述'}}}
const field={id:'field',key:'custom_motion',isActive:true,displayOrder:0,createdAt:'',updatedAt:''}
const version={id:'field-v1',field_definition:field.id,field_key:field.key,version:1,status:'published',label:'自定义动作',category:'test',impact:'示例',value_type:'text',value_options:[],preview_example:'人物向窗边转身',description:'独立文本定义',unit:'',validation_rules:{},contract_sha256:'a'.repeat(64),contract_size_bytes:100}
const options=adaptPromptProfileOptions({operations:['video.generate'],model_families:['fixture'],modes:['Ref2VA'],custom_entry_fields:[],
 source_selectors:[{...choice(source,'结构化输入字段'),group_label:'本次输入',description:'按精确结构路径读取',record_location:'本次调用',execution_usage:'原值装配'}],
 channels:[choice('positive_prompt','正向提示词')],label_styles:[choice('none','不显示字段名')],separators:[choice('newline','换行')],
 generation_modes:[choice('passthrough','原值透传'),choice('model_generated','辅助生成'),choice('translate_only','仅翻译')],
 generation_leaf_operations:['prompt.fields.generate','prompt.fields.translate'],
 generation_source_selectors:[{...choice(source,'结构化输入'),scope_type:'request',group:'input',group_label:'输入',description:'确切路径',availability:'提供结构输入时',scope:'本次输入',status:'active',generation_modes:['model_generated','translate_only']}],
 generation_context_selectors:[{...choice('project.style','项目风格'),description:'仅显式选择时使用'}],generation_output_semantics:[choice('final','最终字段值')],
 source_projection_keys:[choice('direct_v1','直接装配')],source_projection_primitives:[],reference_rendering_fields:[],reference_rendering_text_slots:[],language_policies:[choice('en','英文'),choice('preserve_source','保留原文')],length_units:[choice('none','不限长度')],generation_validators:[],model_profile_versions:[],
 rule_system_defaults:{label_style:'none',separator:'newline',required:false,omit_if_empty:true,max_chars:4000,generation_output_semantics:'final',generation_required:false,language_policy:'preserve_source',length_unit:'none'},
})
if(matrix){
 options.customEntryFields.push('language_policy')
 options.generationModes.push(choice('server_locked','服务端锁定'))
 options.languagePolicies.push(choice('bilingual:en+zh-CN','中英双语'))
}
if(complex){
 options.sourceProjectionKeys.push(choice('declarative_v1','声明式装配'))
 options.sourceSelectors.push({...options.sourceSelectors[0]!,...choice('projection.output','原语输出')})
 options.generationOutputSemantics.push(choice('contribution','投影贡献'))
 options.sourceProjectionPrimitives.push({...choice('fixture.motion','动作合成原语'),description:'仅隔离夹具目录',engineKey:'reference_context_ir_v1',renderingTemplateKeys:['motion'],contributionSourceSelectors:[source]})
 options.referenceRenderingFields.push({key:'motion',label:'动作原文模板',description:'目录明确关联动作原语，不按名称猜测。',requirement:'占位符原样保留',assembly:'动作字段',placeholders:['label'],placeholderHelp:{label:{label:'原始名称',description:'来自冻结参考事实，不翻译',example:'林岚'}},example:{template:'{label} 向前移动',result:'林岚 向前移动'}})
 for(const key of requiredTemplateKeys)options.referenceRenderingFields.push({key,label:`隔离模板 ${key}`,description:'原作者必填分支的内存夹具，不是生产推荐文案。',requirement:'本夹具填入固定文字',assembly:'由实际原语目录确定',placeholders:[],placeholderHelp:{},example:{template:'隔离固定文字',result:'隔离固定文字'}})
}
const raw={id:'published-v1',profile_key:'fixture_profile',version:1,name:'隔离已发布策略',description:'原值输入 → 自定义动作字段 → 正向通道',status:'published',review_state:'ready',model_family:'fixture',operation:'video.generate',mode:'Ref2VA',source_projection_key:'direct_v1',source_projection_config:{},channel_limits:{},input_structure_version:structure.id,contract_sha256:'b'.repeat(64),field_rule_count:1}
if(complex)Object.assign(raw,{source_projection_key:'declarative_v1',source_projection_config:{schema_version:5,primitive_options:{reference_context_ir_v1:{rendering:{templates:{motion:'林岚🙂 {label} 保持原样',...Object.fromEntries(requiredTemplateKeys.map(key=>[key,'隔离固定文字']))},prompt_template_key:'fixture-template',separator:'\n',list_prefix:'',task_separator:' + ',unreferenced_task:'独立输入',unit_text_bindings:{},reference_rules:[]}}},quality_advisories:{}}})
const records:any[]=[copy(raw),{...copy(raw),id:'draft-v2',version:2,name:'隔离策略草稿',status:'draft'}]
const baseRule={id:'rule-published',profile_version:raw.id,field_definition:field.id,field_definition_version:version.id,field_key:field.key,field_label:version.label,field_value_type:'text',source_selector:source,source_path:['motion'],channel:'positive_prompt',position:1,label_style:'none',separator:'newline',required:false,has_default:false,omit_if_empty:true,max_chars:4000,generation_mode:'passthrough',generation_source_selector:source,generation_context_selectors:[],generation_instruction:'',generation_output_semantics:'final',generation_required:false,language_policy:'preserve_source',length_unit:'none',generation_validator_keys:[]}
if(complex)Object.assign(baseRule,{source_selector:'projection.output',projection_primitive:'fixture.motion',generation_mode:'translate_only',generation_output_semantics:'contribution',generation_instruction:'Translate only the selected creative text.',language_policy:'en'})
const rules:any[]=[copy(baseRule),{...copy(baseRule),id:'rule-draft',profile_version:'draft-v2'}]
const publishedBefore=JSON.stringify([records[0],rules[0]])
function deny():never{blocked.value++;throw Error('隔离页面禁止未声明请求')}
api.interceptors.request.use(()=>deny());api.defaults.adapter=async()=>deny()
window.fetch=async()=>deny();XMLHttpRequest.prototype.send=()=>deny();navigator.sendBeacon=()=>deny()
for(const key of Object.keys(platformApi))(platformApi as any)[key]=async()=>deny()
for(const key of Object.keys(executionRegistryApi))(executionRegistryApi as any)[key]=async()=>deny()
executionRegistryApi.version=async id=>{if(!complex||!['owner-v1','owner-v2','aux-v1'].includes(id))return deny();reads.value++;if(mode.value==='reject'&&id==='aux-v1')throw Error('隔离辅助读取失败');return {id,instance_id:id,instance_name:id==='aux-v1'?'隔离辅助实例':'同名主实例',version:id==='owner-v2'?2:1,revision:1,status:id==='owner-v1'?'retired':'published',deployment_id:'fixture-deployment',operation:id==='aux-v1'?'prompt.fields.generate':'video.reference_to_video',mode:'',prompt_mode:id==='aux-v1'?'none':'profile',prompt_profile_version_id:id==='aux-v1'?null:raw.id,prompt_operation:null,prompt_profile_mode:null,workflow_version_id:null,published_at:null,published_by_id:null,created_at:'',updated_at:'',configuration:{deployment_id:'fixture-deployment',operation:'video.reference_to_video',mode:'',prompt_mode:id==='aux-v1'?'none':'profile',prompt_profile_version_id:id==='aux-v1'?null:raw.id,workflow_version_id:null,inputs:[],parameters:[],auxiliaries:id==='owner-v2'?[{purpose:'prompt.fields.generate',target_version_id:'aux-v1'},{purpose:'prompt.translate',target_version_id:'aux-v1'}]:[]}}}
platformApi.listFieldDefinitions=async()=>{reads.value++;return [copy(field)]}
platformApi.listPromptProfileVersions=async()=>{reads.value++;return records.map(x=>adaptPromptProfileVersion(copy(x)))}
platformApi.getPromptProfileVersion=async id=>{reads.value++;return adaptPromptProfileVersion(copy(records.find(x=>x.id===id)))}
platformApi.listPromptProfileFieldRules=async profile=>{reads.value++;return rules.filter(x=>!profile||x.profile_version===profile).map(x=>adaptPromptProfileFieldRule(copy(x)))}
platformApi.getPromptProfileOptions=async()=>copy(options)
fieldDefinitionVersions.get=async id=>{if(id!==version.id)throw{response:{status:404}};return copy(version)};fieldDefinitionVersions.list=async()=>[copy(version)]
dataStructures.list=async()=>({hasNext:false,hasPrevious:false,count:1,results:[copy(structure)]});dataStructures.get=async()=>({...copy(structure),nodes:[{node_key:'motion',parent_key:null,field_definition_version:version.id,property_name:'motion',position:1,required:false,allow_null:false,allow_blank:true,allow_empty:false,has_default:false,default_value:null,constraints:{}}]})
dataStructures.schema=async()=>({valid:true,json_schema:copy(schema),errors:{}})
platformApi.previewPromptProfileVersion=async()=>{reads.value++;throw Error('隔离预览仅到请求边界，不伪造服务端拼装结果')}
api.get=async(url:string,config:any)=>{
 if(url!=='/registry-relations/')return deny()
 reads.value++;const kind=config?.params?.kind,consumer=complex&&config?.params?.source_kind==='prompt'&&config?.params?.relation==='executions'
 const ids=consumer?(config.params.source_id===raw.id?[config.params.page===2?'owner-v2':'owner-v1']:[]):String(config?.params?.ids||'').split(',')
 return {data:{count:consumer?(config.params.source_id===raw.id?2:0):ids.length,next:consumer&&ids.length&&config.params.page!==2?'next':null,previous:consumer&&config.params.page===2?'previous':null,generated_at:'2026-10-10T00:00:00Z',context_label:'隔离',results:ids.map(id=>({id,kind,label:id.startsWith('owner')?'同名主实例 · '+id:id,status:id==='owner-v1'?'retired':'published',config_url:kind==='execution'?`/settings?section=executions&execution_version=${id}`:`/settings?section=fields&${kind==='structure'?'structure_version':`field_definition=${field.id}&field_version`}=${id}`,overview_url:`/settings/acceptance-smoke?view=relations&kind=${kind}&ids=${id}`,acceptance_url:'',groups:[],notices:[],capabilities:[]}))}} as any
}
async function write(action:()=>any){writes.value++;const outcome=mode.value;await new Promise(done=>setTimeout(done,250));if(outcome==='reject')throw {response:{status:400,data:{name:['隔离服务端要求调整名称']}}};const row=action();if(outcome==='unknown-written')throw {response:{status:504}};return copy(row)}
platformApi.createPromptProfileVersion=async payload=>adaptPromptProfileVersion(await write(()=>{const row={...copy(raw),...copy(payload),id:`created-${writes.value}`,version:1,status:'draft',review_state:'needs_review',field_rule_count:0};records.push(row);return row}))
platformApi.updatePromptProfileVersion=async(id,payload)=>adaptPromptProfileVersion(await write(()=>{const row=records.find(x=>x.id===id);Object.assign(row,copy(payload));return row}))
platformApi.clonePromptProfileSuccessor=async(id,sha,name)=>adaptPromptProfileVersion(await write(()=>{const parent=records.find(x=>x.id===id);if(parent.contract_sha256!==sha)throw {response:{status:409}};const row={...copy(parent),id:`successor-${writes.value}`,version:1+Math.max(...records.filter(x=>x.profile_key===parent.profile_key).map(x=>x.version)),name,status:'draft'};records.push(row);for(const rule of rules.filter(x=>x.profile_version===id))rules.push({...copy(rule),id:`copy-${writes.value}-${rule.id}`,profile_version:row.id});return row}))
platformApi.updatePromptProfileFieldRule=async(id,payload)=>{receipts.value.push({id,payload:copy(payload)});return adaptPromptProfileFieldRule(await write(()=>{const row=rules.find(x=>x.id===id);Object.assign(row,copy(payload));return row}))}
platformApi.createPromptProfileFieldRule=async payload=>adaptPromptProfileFieldRule(await write(()=>{const row={...copy(baseRule),...copy(payload),id:`rule-${writes.value}`};rules.push(row);records.find(x=>x.id===row.profile_version).field_rule_count++;return row}))
const maintenance=authorMaintenanceSession(structure,schema,()=>reads.value++,()=>writes.value++,deny,{identity:field,version})
const Maintenance={setup(){const route=useRoute();return()=>h(FieldMaintenanceSettings,{structureVersionId:String(route.query.structure_version||''),fieldDefinitionId:String(route.query.field_definition||''),fieldVersionId:String(route.query.field_version||'')})}}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:maintenance?.route?Maintenance:PromptProfileSettings}]})
const pinia=createPinia();useAuthStore(pinia).user={id:906,username:'fixture',isStaff:true,isSuperuser:true} as any
await router.push(maintenance?.route||'/');await router.isReady()
createApp({setup:()=>()=>h('main',{style:'max-width:1250px;margin:auto;padding:12px;min-width:0'},[
 h('h1',{style:'font-size:20px'},'策略完整作者 · 隔离验证'),h('p',{'data-fixture-evidence':'',role:'status'},`内存写入 ${writes.value} · 只读 ${reads.value} · 网络守卫 ${blocked.value} · 版本 ${records.length} · 不写现用库`),
 h('label',['保存结果场景',h('select',{value:mode.value,onChange:(e:Event)=>mode.value=(e.target as HTMLSelectElement).value},['success','reject','unknown-written'].map(v=>h('option',{value:v},v)))]),
 h(RouterView),
 ...(maintenance?[h('details',[h('summary','跨维护隔离目录回读'),h('pre',{'data-maintenance-receipts':''},JSON.stringify(maintenance.report(),null,2))])]:[]),
 ...(matrix?[h('details',{'data-fixture-receipts':''},[h('summary','隔离保存回执与来源核对'),h('pre',JSON.stringify({receipts:receipts.value,publishedUnchanged:JSON.stringify([records[0],rules[0]])===publishedBefore,rules},null,2))])]:[]),
])}).use(pinia).use(router).mount('#app')
