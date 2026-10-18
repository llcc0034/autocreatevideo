// Actual route editor, isolated contracts only. No business API or model calls.
import {createApp,h,ref} from 'vue'
import ProductionRecipeRouteEditor from '../../src/components/ProductionRecipeRouteEditor.vue'
import {executionRegistryApi} from '../../src/services/executionRegistryApi'
import {api} from '../../src/services/api'
import {recipeDefaultContract,recipeDefaultOptions,recipeDefaultRoute} from './recipe-default-data'
import type {ExecutionContract} from '../../src/services/executionRegistry'
import type {JsonObject,ProductionRecipeEditorOptions} from '../../src/services/platformTypes'
import '../../src/styles.css'
import '../../src/platform.css'
api.interceptors.request.use(()=>{throw new Error('隔离夹具禁止真实API请求')})
executionRegistryApi.contract=async(id)=>recipeDefaultContract(id==='fixture-structure') as unknown as ExecutionContract
createApp({setup(){
  const selected=ref(true),disabled=ref(false),route=ref<JsonObject>(recipeDefaultRoute()),errors=ref<string[]>([])
  return()=>h('main',{style:{maxWidth:'920px',margin:'16px auto',padding:'12px',color:'#e6edf3',background:'#101820'}},[
    h('h1','配方默认值 · 隔离验证'),h('p','实际配方编辑器、测试合同；不保存业务配置，不执行生成。'),
    h('button',{class:'btn btn-secondary',onClick:()=>{selected.value=!selected.value;const next=recipeDefaultRoute(selected.value);route.value={...route.value,execution_instance_version_id:next.execution_instance_version_id,expected_definition_sha256:next.expected_definition_sha256}}},'切换结构引用'),
    h('button',{class:'btn btn-secondary',onClick:()=>disabled.value=!disabled.value},disabled.value?'切换可编辑':'切换只读'),
    h('p',selected.value?'已引用确切结构':'未引用结构；普通目标default不是补值授权'),
    h(ProductionRecipeRouteEditor,{modelValue:route.value,options:recipeDefaultOptions as unknown as ProductionRecipeEditorOptions,priorStages:[],disabled:disabled.value,
      'onUpdate:modelValue':(value:JsonObject)=>route.value=value,onValidity:(value:string[])=>errors.value=value}),
    h('output',{'aria-label':'模板原文',style:{display:'block',overflowWrap:'anywhere'}},JSON.stringify(route.value.input_sources)),
    h('p',{'aria-label':'校验结果'},errors.value.length?errors.value.join('；'):'配置预检通过；不是模型执行成功'),
  ])
}}).mount('#app')
