import {createApp,h,ref} from 'vue'
import BusinessOutputDefinitionView from '../../src/components/BusinessOutputDefinitionView.vue'
import ScriptSemanticOutputEditor from '../../src/components/ScriptSemanticOutputEditor.vue'
import {scriptSemantics,type ScriptSemanticOptions} from '../../src/services/scriptSemantics'
import {dataStructures} from '../../src/services/dataStructures'
import {api} from '../../src/services/api'
import {scriptFixture} from './script-semantics-data'
import type {BusinessOutputVersion} from '../../src/services/businessOutputs'
import '../../src/styles.css'
import '../../src/platform.css'

const failure=ref(false),draft=ref(false),epoch=ref(0),reads=ref(0),blocked=ref(0)
const deny=()=>{blocked.value++;throw new Error('隔离页面禁止业务网络与写入')}
window.fetch=async()=>deny();XMLHttpRequest.prototype.open=function(){deny()};navigator.sendBeacon=()=>deny();api.interceptors.request.use(()=>deny())
scriptSemantics.options=async()=>{reads.value++;if(failure.value)throw new Error('隔离目录读取失败');return structuredClone(scriptFixture.options) as ScriptSemanticOptions}
dataStructures.schema=async()=>({valid:true,json_schema:structuredClone(scriptFixture.layouts.b.schema),errors:{}})
scriptSemantics.preview=async()=>deny()
const mapping=ref<any>(structuredClone(scriptFixture.layouts.b.mapping))
const row:BusinessOutputVersion={id:'isolated-semantic-guide',output_key:'isolated',version:3,revision:1,name:'剧本业务输出 · 隔离说明',description:'仅UI验证；无业务调用',parser_key:'script_semantics.v1',status:'published',data_structure_version:'isolated-structure',contract_sha256:'a'.repeat(64),parser_contract:{key:'script_semantics.v1',format:'json',mapping:mapping.value},quality_advisories:{}}
createApp({setup(){return()=>h('main',{style:'max-width:1100px;margin:24px auto;padding:16px'},[
  h('h1','剧本业务说明 · 隔离页面'),h('p','明确模拟目录／字段配置，不读取业务数据、不调用模型。'),
  h('div',{style:'display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px'},[
    h('button',{onClick:()=>{draft.value=!draft.value;epoch.value++}},draft.value?'查看只读定义':'查看草稿配置'),
    h('button',{onClick:()=>{failure.value=!failure.value;epoch.value++}},failure.value?'恢复目录':'模拟目录失败'),
  ]),
  draft.value?h(ScriptSemanticOutputEditor,{key:epoch.value,modelValue:mapping.value,'onUpdate:modelValue':value=>mapping.value=value,structureId:'isolated-structure',version:null}):h(BusinessOutputDefinitionView,{key:epoch.value,definition:row}),
  h('output',{style:'display:block;margin-top:20px'},`隔离读取 ${reads.value} · 禁止的请求 ${blocked.value} · 业务写入 0`),
])}}).mount('#app')
