// Formal components, static isolated responses only; no API or actual submission.
import {createApp,h,ref} from 'vue'
import FrozenTaskReferences from '../../src/components/FrozenTaskReferences.vue'
import {frozenCorrespondenceRows} from './frozen-correspondence'
import '../../src/styles.css'
import '../../src/platform.css'
const rows=frozenCorrespondenceRows(), state=ref('available')
const task=()=>({id:'isolated',inputContractStatus:state.value==='unavailable'?'unavailable':'available',
  referenceAssets:state.value==='empty'?[]:rows.slice(0,4),contextMediaUses:state.value==='empty'?[]:rows.slice(4)})
createApp({setup(){return()=>h('main',{style:'max-width:1100px;padding:20px;margin:auto'},[
  h('h1','M6 原请求对应 · 隔离验收'),h('p','正式只读组件；样例不是业务记录，没有业务API、真实审核或模型调用。'),
  h('label',['展示状态 ',h('select',{value:state.value,onChange:(e:Event)=>state.value=(e.target as HTMLSelectElement).value},
    ['available','empty','unavailable'].map(value=>h('option',{value},value)))]),
  h(FrozenTaskReferences,{task:task() as any})])}}).mount('#app')
