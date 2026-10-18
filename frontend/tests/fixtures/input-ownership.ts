import {createApp,h,ref} from 'vue'
import ProductionRecipeRouteEditor from '../../src/components/ProductionRecipeRouteEditor.vue'
import {executionRegistryApi} from '../../src/services/executionRegistryApi'
import {api} from '../../src/services/api'
import {ownershipContract,ownershipOptions,ownershipRoute} from './input-ownership-data'
import type {ExecutionContract} from '../../src/services/executionRegistry'
import type {JsonObject,ProductionRecipeEditorOptions} from '../../src/services/platformTypes'
import '../../src/styles.css'
import '../../src/platform.css'
api.interceptors.request.use(()=>{throw new Error('隔离夹具禁止真实业务API请求')})
executionRegistryApi.contract=async()=>ownershipContract() as ExecutionContract
createApp({setup(){
  const route=ref<JsonObject>(ownershipRoute()),disabled=ref(false)
  return()=>h('main',{style:{maxWidth:'900px',margin:'16px auto',padding:'12px',minWidth:0}},[
    h('h1','输入配置归属 · 隔离验证'),h('p','正式配方组件、测试合同；不保存业务配置，不执行生成。'),
    h('button',{class:'button ghost small',onClick:()=>disabled.value=!disabled.value},disabled.value?'切换可编辑':'切换只读'),
    h(ProductionRecipeRouteEditor,{modelValue:route.value,options:ownershipOptions as unknown as ProductionRecipeEditorOptions,priorStages:[],disabled:disabled.value,'onUpdate:modelValue':(value:JsonObject)=>route.value=value}),
    h('output',{'aria-label':'步骤参数原文',style:{display:'block',overflowWrap:'anywhere'}},JSON.stringify(route.value.parameter_policies)),
  ])
}}).mount('#app')
