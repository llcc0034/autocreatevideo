<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import InputSourcePolicyEditor from './InputSourcePolicyEditor.vue'
import { inputSourcePolicies, type InputSourceCatalog, type MediaSourcePolicy } from '@/services/inputSourcePolicies'
import { executionMediaPolicy, type ExecutionMediaSourcePolicy } from '@/services/executionMediaSourcePolicy'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import type { ExecutionInput } from '@/services/executionRegistry'

const props=defineProps<{input:ExecutionInput;purposes:{value:string;label:string}[];disabled?:boolean}>()
const emit=defineEmits<{'update:policy':[ExecutionMediaSourcePolicy|null]}>()
const policy=computed(()=>executionMediaPolicy(props.input)),catalog=ref<InputSourceCatalog|null>(null),error=ref(''),loading=ref(false)
let sequence=0
async function load(){const token=++sequence;loading.value=true;error.value='';try{const result=await inputSourcePolicies.options();if(token===sequence)catalog.value=result}catch(cause){if(token===sequence)error.value=apiErrorSummary(cause)}finally{if(token===sequence)loading.value=false}}
watch(()=>!!policy.value,enabled=>{if(enabled&&!catalog.value)void load()},{immediate:true})
onBeforeUnmount(()=>{sequence++})
function enable(){if(!props.disabled)emit('update:policy',{schema_version:1,allowed_sources:[],default_source:'',allow_override:false,order:'provided'})}
function update(value:MediaSourcePolicy){if(!props.disabled)emit('update:policy',{schema_version:1,allowed_sources:value.allowed_sources,default_source:value.default_source,allow_override:value.allow_override,order:value.order})}
</script>
<template>
  <section class="media-policy-editor">
    <template v-if="policy">
      <p>数量与用途沿用此输入的定义，不重复保存。顺序使用明确的列表顺序。配对视频原音不属于独立素材来源；需要该方式时不配置此来源策略。</p>
      <p v-if="loading" role="status">正在读取来源目录…</p>
      <p v-if="error" role="alert">{{error}} <button type="button" class="button ghost small" :disabled="loading" @click="load">重新读取来源目录</button></p>
      <InputSourcePolicyEditor v-if="catalog" :model-value="policy" :catalog="catalog" :purposes="purposes" :disabled="disabled" :show-constraints="false" @update:model-value="update"/>
      <p v-else-if="disabled">已保存来源：{{policy.allowed_sources.join('、')}}；默认：{{policy.default_source}}；允许改选：{{policy.allow_override?'是':'否'}}。</p>
      <button v-if="!disabled" type="button" class="button ghost small" @click="emit('update:policy',null)">移除额外来源限制</button>
    </template>
    <template v-else><p>未额外限制来源方式。素材类型、数量、用途、关联和执行目标要求仍生效。</p><button v-if="!disabled" type="button" class="button ghost small" @click="enable">配置媒体来源方式</button></template>
  </section>
</template>
<style scoped>.media-policy-editor{display:grid;gap:10px;min-width:0}.media-policy-editor p{font-size:12px;line-height:1.7;overflow-wrap:anywhere;margin:0}</style>
