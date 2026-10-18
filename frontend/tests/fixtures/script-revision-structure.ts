// Display-only fixture: API transport is disabled; parser correctness is tested by Django.
import {createApp,h,ref} from 'vue'
import ScriptRevisionStructurePanel from '../../src/components/ScriptRevisionStructurePanel.vue'
import {scriptRevisionStructures as service,type ScriptRevisionStructure} from '../../src/services/scriptRevisionStructures'
import {api} from '../../src/services/api'
import {platformApi} from '../../src/services/platformApi'
import '../../src/styles.css'
import '../../src/platform.css'

api.interceptors.request.use(()=>{throw new Error('隔离页面禁止真实HTTP请求')})
const fail=ref(false),revision=ref('isolated-revision-a'),notice=ref('')
const saved=new Map<string,ScriptRevisionStructure>()
const option={id:'isolated-output-a',output_key:'isolated-output',version:1,name:'隔离 JSON 布局 A',description:'仅UI样例'}
service.options=async()=>({count:1,results:[option]})
service.read=async id=>saved.get(id)||null
service.save=async(id,output)=>{
  if(fail.value)throw new Error('隔离解析失败：references[0].entity_key 未声明（原文未变）')
  const value={document_sha256:'b'.repeat(64),can_revise:true,json_schema:{type:'object',properties:{人物:{type:'array',items:{type:'object',properties:{名称:{type:'string'},变体:{type:'array',items:{type:'string'}}}}},额外创作字段:{type:'string'},画外音:{type:'boolean'},镜头序号:{type:'integer'}}},document:{schema_version:1,revision:{id,source_id:'isolated-source',content_sha256:'a'.repeat(64)},output:{...option,id:output},projection:{validation_scope:'structure_mapping_and_relations',source_value:{人物:[{名称:'样例人物',变体:['校服','外套']}],额外创作字段:'转身后停顿',画外音:false,镜头序号:0},value:{entities:[{key:'actor-a',name:'样例人物'}],episodes:[]},defaults:[],provenance:[{path:['人物',0,'名称']}],warnings:[]}}}
  saved.set(id,value);return value
}
let sequence=0
platformApi.createScriptRevision=async(source,content,basis)=>{
  if(fail.value)throw {response:{status:400,data:{'source.额外创作字段':['隔离校验失败，草稿保留']}}}
  if(!basis)throw Error('隔离样例要求明确依据')
  const id=`isolated-edited-${++sequence}`,original=saved.get(basis.revision_id)!
  const copy=JSON.parse(JSON.stringify(original));copy.document.revision.id=id;copy.document.projection.source_value=JSON.parse(content)
  saved.set(id,copy)
  return {id,source,status:'preview',content,contentSha256:'a'.repeat(64),revision:sequence+1} as any
}
createApp({setup(){return()=>h('main',{style:{maxWidth:'900px',margin:'16px auto',padding:'12px'}},[
  h('h1','剧本 JSON 导入 · 隔离页面'),h('p','仅真实组件＋内存回执；未调用后端、模型或业务写入。'),
  h('button',{class:'button ghost small',onClick:()=>{fail.value=!fail.value}},fail.value?'恢复模拟成功':'模拟解析失败'),
  h('button',{class:'button ghost small',onClick:()=>{revision.value=revision.value==='isolated-revision-a'?'isolated-revision-b':'isolated-revision-a'}},'切换预览修订'),
  h('pre',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},'{ "人物": [{ "名称": "样例人物" }], "画外音": false, "镜头序号": 0 }'),
  h('p',notice.value),
  h(ScriptRevisionStructurePanel,{revisionId:revision.value,contentSha256:'a'.repeat(64),status:'preview','onRevision-created':(created:any)=>{
    revision.value=created.id;notice.value='隔离回执：已另存新预览；原修订保留，未调用真实API或采用。'
  }}),
])}}).mount('#app')
