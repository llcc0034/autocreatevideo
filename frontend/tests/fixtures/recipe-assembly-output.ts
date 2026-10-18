// Explicit UI-only fixture. All business transport is blocked; no save/model.
import {createApp,h,ref} from 'vue'
import RecipeAssemblyOutputEditor from '../../src/components/RecipeAssemblyOutputEditor.vue'
import {businessOutputs} from '../../src/services/businessOutputs'
import {dataStructures} from '../../src/services/dataStructures'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'

api.interceptors.request.use(()=>{throw new Error('隔离页面禁止真实业务API')})
const timing=new URLSearchParams(location.search).get('purpose')==='timing'
const delivery=new URLSearchParams(location.search).get('purpose')==='delivery'
const media=new URLSearchParams(location.search).get('purpose')==='media'
const upstream=new URLSearchParams(location.search).get('source')==='upstream'
const outputId='00000000-0000-4000-8000-000000000001'
const upstreamOptions=['first','second'].map((stage,index)=>({stageKey:stage,outputKey:`structured_text.${outputId}`,
  kind:'json',scopeKind:'project',label:`${index+1} · ${index?'第二':'第一'}个同输出步骤`,outputVersionId:outputId,
  outputContractSha256:'a'.repeat(64),structureVersionId:'00000000-0000-4000-8000-000000000002'}))
const time={type:['integer','null'],minimum:0}
const timingSchema={type:'object',required:['episodes'],additionalProperties:false,properties:{episodes:{type:'array',minItems:1,items:{type:'object',required:['number','shots'],additionalProperties:false,properties:{number:{type:'integer',minimum:1},shots:{type:'array',minItems:1,items:{type:'object',required:['number','duration_ms','episode_start_ms','episode_end_ms'],additionalProperties:false,properties:{number:{type:'integer',minimum:1},duration_ms:{type:'integer',minimum:1},episode_start_ms:time,episode_end_ms:time}}}}}}}}
const deliverySchema={type:'object',required:['title','notes'],additionalProperties:false,properties:{title:{type:'string',minLength:1,maxLength:200},notes:{type:'string'}}}
const mediaSchema={type:'object',required:['episodes'],additionalProperties:false,properties:{episodes:{type:'array',minItems:1,items:{type:'object',required:['number','shots'],additionalProperties:false,properties:{number:{type:'integer',minimum:1},shots:{type:'array',minItems:1,items:{type:'object',required:['number','artifact_id'],additionalProperties:false,properties:{number:{type:'integer',minimum:1},artifact_id:{type:'string',format:'uuid'}}}}}}}}}
const source=media?{type:'object',required:['片段'],additionalProperties:true,properties:{片段:mediaSchema}}:delivery?{type:'object',required:['发行'],additionalProperties:true,properties:{发行:{type:'object',required:['名称','说明'],properties:{名称:{type:'string'},说明:{type:'string'}}}}}:timing?{type:'object',required:['时序'],additionalProperties:true,properties:{时序:timingSchema}}:{type:'object',required:['宽','高','帧率'],additionalProperties:true,properties:{宽:{type:'integer'},高:{type:'integer'},帧率:{type:'integer'}}}
const target=media?mediaSchema:delivery?deliverySchema:timing?timingSchema:{type:'object',required:['width','height','fps'],additionalProperties:false,properties:{width:{type:'integer',minimum:1,maximum:8192},height:{type:'integer',minimum:1,maximum:8192},fps:{type:'integer',minimum:1,maximum:120}}}
let offline=false
businessOutputs.detail=async(id:string)=>{if(offline)throw new Error('隔离读取失败（用于恢复验证）');return{id,output_key:id,version:1,name:media?'隔离逐集逐镜片段':delivery?'隔离交付标题与说明':timing?'隔离自定义时序':'隔离自定义宽高帧率',description:'合成配置，非现用版本',parser_key:'structured_json.v1',status:'published',revision:1,contract_sha256:'a'.repeat(64),data_structure_version:'00000000-0000-4000-8000-000000000002',quality_advisories:{},parser_contract:{key:'structured_json.v1',format:'json'}}}
dataStructures.schema=async()=>({valid:true,json_schema:source,errors:{}})
dataStructures.mappingOptions=async()=>({schema_version:1,nodes:[{value:'object',label:'对象'},{value:'path',label:'字段路径'},{value:'literal',label:'固定值'}],scopes:[{value:'root',label:'整个来源'},{value:'item',label:'当前条目'}]})
const initial=()=>({output_version_id:'00000000-0000-4000-8000-000000000001',expected_contract_sha256:'a'.repeat(64),source_text:timing?'{ "时序":{"episodes":[{"number":1,"shots":[{"number":1,"duration_ms":3000,"episode_start_ms":null,"episode_end_ms":null},{"number":2,"duration_ms":4000,"episode_start_ms":null,"episode_end_ms":null}]}]}, "备注":"原文保留" }':'{ "宽":640, "高":360, "帧率":30, "备注":"原文保留" }',mapping:{schema_version:1,node:timing?{kind:'path',scope:'root',path:['时序']}:{kind:'object',fields:Object.fromEntries([['width','宽'],['height','高'],['fps','帧率']].map(([key,name])=>[key,{kind:'path',scope:'root',path:[name]}]))}}})
createApp({setup(){
  const starting=()=>{const value:any=initial();if(delivery){value.source_text=JSON.stringify({发行:{名称:'  雨夜 / 夜🌧  ',说明:'原文\r\nΔ'},custom:false});value.mapping={schema_version:1,node:{kind:'object',fields:{title:{kind:'path',scope:'root',path:['发行','名称']},notes:{kind:'path',scope:'root',path:['发行','说明']}}}}}if(media){value.source_text=JSON.stringify({片段:{episodes:[1,2].map(number=>({number,shots:[{number:1,artifact_id:`00000000-0000-4000-8000-00000000000${number+2}`}]}))},说明:'  保留原文\r\n',custom:false});value.mapping={schema_version:1,node:{kind:'path',scope:'root',path:['片段']}}}if(upstream){delete value.source_text;value.source={kind:'selected_output',stage_key:'first',output_key:`structured_text.${outputId}`,scope:{kind:'project'},selection_policy:'reviewed_selected'}}return value}
  const draft=ref<any>(starting()),errors=ref<string[]>([]),disabled=ref(false),narrow=ref(false),key=ref(0),missing=ref(false)
  return()=>h('main',{style:{width:narrow.value?'390px':'100%',maxWidth:'100%',boxSizing:'border-box',margin:'auto',padding:'16px',background:'#101820',color:'#e6edf3'}},[
    h('h1',delivery?'M8交付映射 · 隔离页面':'M8装配映射 · 隔离页面'),h('p','正式组件、合成合同。零业务保存，零模型调用。'),
    h('button',{onClick:()=>narrow.value=!narrow.value},narrow.value?'恢复宽屏':'390px窄屏'),
    h('button',{onClick:()=>disabled.value=!disabled.value},disabled.value?'恢复编辑':'只读模式'),
    h('button',{onClick:()=>{offline=!offline;key.value++}},offline?'恢复模拟读取':'模拟读取失败'),
    h('button',{onClick:()=>{draft.value=starting();key.value++}},'重新打开原草稿'),
    ...(upstream?[h('button',{onClick:()=>missing.value=!missing.value},missing.value?'恢复上游目录':'模拟上游移除')]:[]),
    h(RecipeAssemblyOutputEditor,{key:key.value,purpose:media?'media':delivery?'delivery':timing?'timing':'output',modelValue:draft.value,schema:target,sourceOptions:upstream?(missing.value?[]:upstreamOptions):undefined,disabled:disabled.value,'onUpdate:modelValue':(value:any)=>draft.value=value,onValidity:(value:string[])=>errors.value=value}),
    h('output',{id:'mapping-errors',style:{display:'block',overflowWrap:'anywhere'}},errors.value.join('；')||(disabled.value?'页面只读；尚未请求业务保存':'页面草稿可编辑；尚未请求业务保存')),
    h('details',[h('summary','查看当前原始草稿'),h('pre',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},JSON.stringify(draft.value,null,2))]),
  ])
}}).mount('#app')
