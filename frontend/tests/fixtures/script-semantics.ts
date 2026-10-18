// Isolated UI adapter: no business HTTP calls, writes, publication or model execution.
import {createApp,h,ref} from 'vue'
import ScriptSemanticOutputEditor from '../../src/components/ScriptSemanticOutputEditor.vue'
import {scriptSemantics,type ScriptSemanticOptions,type ScriptSemanticPreview} from '../../src/services/scriptSemantics'
import {dataStructures} from '../../src/services/dataStructures'
import type {BusinessOutputVersion} from '../../src/services/businessOutputs'
import type {JsonObject} from '../../src/services/platformTypes'
import {scriptFixture} from './script-semantics-data'
import '../../src/styles.css'
import '../../src/platform.css'
const layout=ref<'a'|'b'>('a'),mapping=ref<JsonObject>(structuredClone(scriptFixture.layouts.a.mapping)),saved=ref(true),readonly=ref(false)
scriptSemantics.options=async()=>structuredClone(scriptFixture.options) as ScriptSemanticOptions
dataStructures.schema=async()=>({valid:true,json_schema:structuredClone(scriptFixture.layouts[layout.value].schema),errors:{}})
scriptSemantics.preview=async(row,text)=>{
  if(text==='网络失败')throw new Error('隔离网络错误（无HTTP请求）')
  const fixture=scriptFixture.layouts[layout.value],report=structuredClone(fixture.report) as unknown as ScriptSemanticPreview
  Object.assign(report,{output_version_id:row.id,output_revision:row.revision,output_contract_sha256:row.contract_sha256})
  if(text!=='夹具样例'){Object.assign(report,{valid:false,parsed:false,value:null,raw_value:null,source_value:null,provenance:[],errors:{response_text:['隔离页面只回放已通过后端测试的样例；输入“夹具样例”查看。']}})}
  return report
}
createApp({setup(){return()=>{
  const fixture=scriptFixture.layouts[layout.value]
  const version:BusinessOutputVersion={id:'isolated-output-'+layout.value,output_key:'isolated',version:1,revision:1,name:'隔离输出',description:'仅UI验证',parser_key:'script_semantics.v1',status:readonly.value?'published':'draft',data_structure_version:fixture.report.source_structure_version_id,contract_sha256:'a'.repeat(64)}
  return h('main',{style:{maxWidth:'1000px',margin:'16px auto',padding:'14px',color:'#eef2f8',background:'#101820'}},[
    h('h1','M2 剧本语义映射 · 隔离页面'),h('p','明确模拟目录和报告（仅展示两条路径证据）；不读取或写入业务数据。输入“夹具样例”回放后端隔离样例，“网络失败”查看错误。'),
    h('label',['布局',h('select',{value:layout.value,onChange:(event:Event)=>{layout.value=(event.target as HTMLSelectElement).value as 'a'|'b';mapping.value=structuredClone(scriptFixture.layouts[layout.value].mapping);saved.value=true}},[h('option',{value:'a'},'A：entities / episodes'),h('option',{value:'b'},'B：world.cast / chapters')])]),
    h('button',{onClick:()=>{saved.value=true}},'模拟保存当前配置（仅内存）'),h('button',{onClick:()=>readonly.value=!readonly.value},readonly.value?'切回草稿':'模拟已发布只读'),
    h(ScriptSemanticOutputEditor,{modelValue:mapping.value,'onUpdate:modelValue':(value:JsonObject)=>{mapping.value=value;saved.value=false},structureId:version.data_structure_version!,version,disabled:readonly.value,previewDisabled:!saved.value}),
  ])
}}}).mount('#app')
