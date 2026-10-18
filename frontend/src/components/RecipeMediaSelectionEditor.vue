<script setup lang="ts">
import {computed,onBeforeUnmount,onMounted,ref,watch} from 'vue'
import type {ExecutionInput} from '@/services/executionRegistry'
import {executionMediaPolicy} from '@/services/executionMediaSourcePolicy'
import {inputSourcePolicies,selectedMediaSource,type InputSourceCatalog,type MediaSourceSelection} from '@/services/inputSourcePolicies'
import {apiErrorSummary} from '@/services/apiErrorPresentation'
import MediaSourceSelectionEditor from './MediaSourceSelectionEditor.vue'

const props=defineProps<{modelValue:MediaSourceSelection;input:ExecutionInput;disabled?:boolean}>()
const emit=defineEmits<{'update:modelValue':[MediaSourceSelection];validity:[string[]]}>()
const policy=computed(()=>executionMediaPolicy(props.input))
const catalog=ref<InputSourceCatalog|null>(null),error=ref(''),loading=ref(false),busy=ref(false)
let sequence=0
async function load(){
  const request=++sequence;loading.value=true;error.value=''
  try{const value=await inputSourcePolicies.options();if(request===sequence)catalog.value=value}
  catch(cause){if(request===sequence)error.value=apiErrorSummary(cause)}
  finally{if(request===sequence)loading.value=false}
}
onMounted(load);onBeforeUnmount(()=>{sequence++})
const issues=computed(()=>{
  if(!policy.value)return ['请先在精确实例中配置媒体来源策略']
  if(loading.value)return ['正在读取素材来源目录']
  if(error.value)return [error.value]
  if(busy.value)return ['素材正在上传，完成后再保存配方']
  try{
    const value=props.modelValue
    if(!['asset_library','asset_upload'].includes(value?.source))return ['固定素材只能选择素材库或上传入库；上游产物需要运行上下文']
    selectedMediaSource(policy.value,value.source===policy.value.default_source?value:{source:policy.value.default_source,items:[]},
      value.source===policy.value.default_source?undefined:value)
    return []
  }catch(cause){return [cause instanceof Error?cause.message:'素材选择无效']}
})
watch(()=>JSON.stringify(issues.value),value=>emit('validity',JSON.parse(value)),{immediate:true})
function update(value:MediaSourceSelection){if(!props.disabled||busy.value)emit('update:modelValue',value)}
</script>
<template>
  <section aria-label="配方固定素材">
    <p>每个制作单元使用此处明确选择的素材作为输入，仍会执行生成；这不是直接复用为成片。数量、用途和改选权限沿用精确实例。</p>
    <p v-if="loading" role="status">正在读取素材来源…</p>
    <p v-if="error" role="alert">{{error}} <button type="button" class="button ghost small" :disabled="loading" @click="load">重试读取</button></p>
    <MediaSourceSelectionEditor v-if="catalog&&policy" :model-value="modelValue" :policy="policy" :catalog="catalog" mode="input"
      :supported-sources="['asset_library','asset_upload']" :disabled="disabled||busy||loading||Boolean(error)"
      @update:model-value="update" @busy="busy=$event"/>
    <p v-for="issue in issues" :key="issue" role="alert">{{issue}}</p>
  </section>
</template>
