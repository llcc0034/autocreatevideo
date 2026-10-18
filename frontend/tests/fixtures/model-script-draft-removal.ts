// In-memory authoring fixture. No business writes or model calls.
import {createApp,h,ref} from 'vue'
import ModelScriptContractsEditor from '../../src/components/ModelScriptContractsEditor.vue'
import {adaptScriptContracts} from '../../src/services/modelScriptContracts'
import {scriptContractFixture} from '../helpers/scriptContractFixture'
import {api} from '../../src/services/api'
import type {JsonObject} from '../../src/services/platformTypes'
import '../../src/styles.css'
import '../../src/platform.css'
api.defaults.adapter=async()=>{throw Error('隔离夹具禁止真实API')}
const saved:JsonObject={capabilities:['script.generate'],series_concept:{timeout_seconds:180,chat_request_options:{},minimum_shot_duration_ms:1000,field_contract:{title:{required:true}}},other:{keep:true}}
const draft=ref<JsonObject>(JSON.parse(JSON.stringify(saved))),writes=ref(0),disabled=ref(false)
createApp({setup:()=>()=>h('main',{style:{maxWidth:'960px',margin:'20px auto',padding:'16px',minWidth:0}},[
  h('h1','模型草稿移除确认 · 隔离验证'),h('p','只修改内存草稿；没有保存接口，不连接生产数据。'),
  h('label',[h('input',{type:'checkbox',checked:disabled.value,onChange:(e:Event)=>disabled.value=(e.target as HTMLInputElement).checked}),'模拟只读']),
  h(ModelScriptContractsEditor,{modelValue:draft.value,options:adaptScriptContracts(scriptContractFixture),disabled:disabled.value,'onUpdate:modelValue':(value:JsonObject)=>{draft.value=value;writes.value++}}),
  h('p',{'aria-label':'草稿变更次数'},`草稿变更次数：${writes.value}`),
  h('pre',{'aria-label':'保存值与草稿',style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},JSON.stringify({saved,draft:draft.value},null,2)),
])}).mount('#app')
