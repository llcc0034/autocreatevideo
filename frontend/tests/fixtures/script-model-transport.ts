// Isolated component fixture: no business API, persistence, publication or model calls.
import {createApp,h,ref} from 'vue'
import Editor from '../../src/components/ModelScriptContractsEditor.vue'
import {adaptScriptContracts} from '../../src/services/modelScriptContracts'
import {scriptContractFixture} from '../helpers/scriptContractFixture'
import type {JsonObject} from '../../src/services/platformTypes'
import '../../src/styles.css'
import '../../src/platform.css'
const options=adaptScriptContracts(scriptContractFixture)
const chatOptions={schemaVersion:1,temperatureMinimum:0,responseFormatTypes:[{value:'json_object',label:'JSON对象'}],assemblyDescription:'测试目录：原样传给模型接口。',boundaryDescription:'仅验证页面，不证明模型兼容。'}
createApp({setup(){
  const value=ref<JsonObject>({capabilities:['script.generate','script.structure'],series_concept:{timeout_seconds:30,chat_request_options:{temperature:0}},script_structure:{timeout_seconds:45,chat_request_options:{}}})
  const readonly=ref(false),missing=ref(false),errors=ref<string[]>([])
  return()=>h('main',{style:{maxWidth:'1000px',margin:'16px auto',padding:'14px',color:'#eef2f8',background:'#101820'}},[
    h('h1','M2 模型传输 · 隔离测试'),h('p','本页只使用明确标记的测试目录，不保存模型或请求业务API。'),
    h('button',{onClick:()=>readonly.value=!readonly.value},readonly.value?'切换为草稿':'切换为只读'),
    h('button',{onClick:()=>missing.value=!missing.value},missing.value?'恢复测试目录':'模拟目录缺失'),
    h(Editor,{modelValue:value.value,options:missing.value?null:options,chatOptions,disabled:readonly.value,
      'onUpdate:modelValue':(next:JsonObject)=>value.value=next,onValidity:(next:string[])=>errors.value=next}),
    h('p',{role:'status'},errors.value.length?`不可保存：${errors.value.join('；')}`:'隔离草稿校验通过（未保存）'),
    h('details',[h('summary','当前未保存测试值'),h('pre',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},JSON.stringify(value.value,null,2))]),
  ])
}}).mount('#app')
