// Formal components with explicit synthetic metadata; all real transport blocked.
import {createApp,h,ref} from 'vue'
import {createRouter,createWebHistory,RouterView} from 'vue-router'
import ManualAssemblyReview from '../../src/components/ManualAssemblyReview.vue'
import {platformApi} from '../../src/services/platformApi'
import {businessOutputs} from '../../src/services/businessOutputs'
import {dataStructures} from '../../src/services/dataStructures'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'

api.interceptors.request.use(()=>{throw new Error('隔离页面禁止真实业务API')})
const id=(n:number)=>`10000000-0000-0000-0000-${String(n).padStart(12,'0')}`,sha='a'.repeat(64)
const run:any={id:id(1),project:id(2),status:'awaiting_input',planSource:'manual',executionPlan:{sha256:sha}}
const options:any={runId:run.id,runPlanSha256:sha,actions:[{stageKey:'assembly',action:'assemble',scopeKinds:['episode']}]}
const geometry={type:'object',required:['width','height','fps'],additionalProperties:false,properties:{width:{type:'integer'},height:{type:'integer'},fps:{type:'integer'}}}
const source={type:'object',required:['宽','高','帧率'],additionalProperties:true,properties:{宽:{type:'integer'},高:{type:'integer'},帧率:{type:'integer'}}}
const rule={output_version_id:id(20),expected_contract_sha256:sha,source_text:' {"宽":640,"高":360,"帧率":30,"备注":"  保留原值  "} ',
  mapping:{schema_version:1,node:{kind:'object',fields:Object.fromEntries([['width','宽'],['height','高'],['fps','帧率']].map(([key,name])=>[key,{kind:'path',scope:'root',path:[name]}]))}}}
const base:any={run_id:run.id,run_plan_sha256:sha,contract_sha256:sha,frozen:false,blockers:[],approval:{revision:1},
  authors:{},author_contracts:{},author_schemas:{assembly_output:geometry,assembly_timing:{type:'object',properties:{episodes:{type:'array'}}},assembly_media:{type:'object',properties:{episodes:{type:'array'}}}},
  episodes:[{episode_id:id(3),number:1,title:'隔离选片示例',assembly_link_id:null,job:null,shots:[{shot_id:id(4),number:1,title:'已有图片片段',duration_ms:2500,media_kind:'image',source:{artifact_id:id(6),kind:'image',artifact_sha256:sha,manifest_sha256:sha,decision_id:id(7),decision_revision:1,scope:{kind:'shot',id:id(4)}}}]}],
  output:{width:640,height:360,fps:30,media_profile:{schema_version:1},media_profile_key:'隔离编码配置',media_profile_sha256:sha,max_attempts:2,label_enabled:false,label_text:''}}
let frozen=false,offline=false
// Explicit transport simulation for the two-episode UI boundary. Backend
// encoding/sealing evidence lives in test_manual_assembly_multiepisode.py.
const multi=new URLSearchParams(location.search).has('multiepisode')
let requests:any[]=[],secondQueued=false
const rootMapping={schema_version:1,node:{kind:'path',scope:'root',path:[]}}
const mediaRule={...rule,mapping:rootMapping,output_version_id:id(22),source_text:JSON.stringify({
  episodes:[1,2].map(number=>({number,shots:[{number:1,artifact_id:id(number===1?6:16)}]}))})}
const timingRule={...rule,mapping:rootMapping,output_version_id:id(23),source_text:JSON.stringify({
  episodes:[1,2].map(number=>({number,shots:[{number:1,duration_ms:2500,episode_start_ms:0,episode_end_ms:2500}]}))})}
const multiAuthors={assembly_output:rule,assembly_timing:timingRule,assembly_media:mediaRule}
if(multi){
  frozen=true
  base.episodes[0].title='第一集已完成 · 隔离状态'
  base.episodes[0].assembly_link_id=id(8)
  base.episodes[0].job={id:id(9),status:'succeeded',attempts:1,max_attempts:2,error_message:'',updated_at:'2026-10-05T00:00:00Z'}
  base.episodes.push({...structuredClone(base.episodes[0]),episode_id:id(13),number:2,title:'第二集待提交 · 隔离状态',assembly_link_id:null,job:null,
    shots:[{...structuredClone(base.episodes[0].shots[0]),shot_id:id(14),source:{...base.episodes[0].shots[0].source,artifact_id:id(16),decision_id:id(17),scope:{kind:'shot',id:id(14)}}}]})
}
const contract=()=>({output_reference:{id:id(20),sha256:sha,data_structure:{id:id(21),json_schema:source}},projection:{valid:true,raw_value:JSON.parse(rule.source_text),output:{width:640,height:360,fps:30},provenance:[{source:'隔离字段映射'}]}})
platformApi.getManualAssembly=async()=>{if(offline)throw new Error('隔离读取失败；未返回新结果');return {...structuredClone(base),frozen,
  authors:multi?multiAuthors:frozen?{assembly_output:rule}:{},author_contracts:multi?Object.fromEntries(Object.entries(multiAuthors).map(([key,value])=>[key,
    {...contract(),output_reference:{...contract().output_reference,id:value.output_version_id,
      data_structure:{id:id(key==='assembly_output'?21:key==='assembly_media'?24:25),json_schema:key==='assembly_output'?source:base.author_schemas[key]}},
      projection:{...contract().projection,raw_value:JSON.parse(value.source_text),output:key==='assembly_output'?contract().projection.output:JSON.parse(value.source_text)}}])):frozen?{assembly_output:contract()}: {}}}
platformApi.previewManualAssembly=async()=>{throw new Error('隔离预览拒绝（用于草稿保留/恢复验证）；未保存、未入队')}
platformApi.performManualProductionAction=async(_run,body)=>{
  if(!multi)throw new Error('隔离页面禁止装配写入')
  if(JSON.stringify(body.scope)!==JSON.stringify({kind:'episode',id:id(13)}))throw new Error('不得重发第一集')
  requests.push(JSON.parse(JSON.stringify(body)))
  if(requests.length===1)throw new Error('隔离响应丢失：只可核对同一请求，未访问真实API')
  if(JSON.stringify(requests[0])!==JSON.stringify(body))throw new Error('请求被改变，拒绝模拟回放')
  secondQueued=true;base.episodes[1].assembly_link_id=id(18)
  base.episodes[1].job={id:id(19),status:'queued',attempts:0,max_attempts:2,error_message:'',updated_at:'2026-10-05T00:00:01Z'}
  return {action:'assemble',result_kind:'assembly_link',target_id:id(18)} as any
}
const output:any={id:id(20),output_key:'isolated',version:1,name:'隔离宽高帧率',description:'非现用合同',parser_key:'structured_json.v1',status:'published',revision:1,contract_sha256:sha,data_structure_version:id(21),quality_advisories:{},parser_contract:{key:'structured_json.v1',format:'json'}}
businessOutputs.list=async()=>({count:1,results:[output],hasNext:false,hasPrevious:false})
businessOutputs.options=async()=>({editor:null,parser:null,parsers:[{key:'structured_json.v1',label:'通用JSON',description:'隔离目录',configuration:'structure'}],allowedValueFields:[],fieldSourceOptions:[],responseKeyPolicy:null} as any)
businessOutputs.detail=async()=>output
dataStructures.schema=async()=>({valid:true,json_schema:source,errors:{}})
dataStructures.mappingOptions=async()=>({schema_version:1,nodes:[{value:'object',label:'对象'},{value:'path',label:'字段路径'},{value:'literal',label:'固定值'}],scopes:[{value:'root',label:'整个来源'},{value:'item',label:'当前条目'}]})
const Fixture={setup(){const key=ref(0),narrow=ref(false),completedReads=ref(0)
  return()=>h('main',{style:{width:narrow.value?'390px':'100%',maxWidth:'100%',boxSizing:'border-box',margin:'auto',padding:'16px',background:'#101820',color:'#e6edf3'}},[
    h('h1','M8人工装配 · 隔离组件验证'),h('p','合成选片与映射；禁止所有真实API、保存、装配和模型调用。'),
    h('button',{onClick:()=>narrow.value=!narrow.value},narrow.value?'恢复宽屏':'390px窄屏'),
    !multi&&h('button',{onClick:()=>{frozen=!frozen;key.value++}},frozen?'返回未冻结示例':'查看冻结映射示例'),
    h('button',{onClick:()=>{offline=!offline;key.value++}},offline?'恢复读取':'模拟读取失败'),
    h(ManualAssemblyReview,{key:key.value,run,options,onChanged:()=>{completedReads.value++}}),
    multi&&h('p',{key:completedReads.value},secondQueued?'隔离回放已核对同一请求；第一集保持原任务，第二集仅模拟排队，未交付。':'双集模拟：第二集首次丢失响应，重试必须复用同一请求。'),
  ])}}
const router=createRouter({history:createWebHistory(),routes:[{path:'/:pathMatch(.*)*',component:Fixture}]})
createApp({render:()=>h(RouterView)}).use(router).mount('#app')
