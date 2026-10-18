<script setup lang="ts">
import { computed, ref } from 'vue'
import type { JsonObject } from '@/services/platformTypes'
import { pathSchema, pathType, pathLabel, type StructurePath } from '@/services/structurePaths'
import { jsonChildSchemas, jsonPropertyOptions } from '@/services/structuredInputForm'

const props=defineProps<{schema:JsonObject;modelValue:unknown;disabled?:boolean;label?:string}>()
const emit=defineEmits<{'update:modelValue':[StructurePath]}>()
const indexDraft=ref(''),localError=ref('')
const selected=computed(()=>pathSchema(props.schema,props.modelValue))
const tokens=computed(()=>Array.isArray(props.modelValue)?props.modelValue as StructurePath:[])
const properties=computed(()=>jsonPropertyOptions([selected.value.schema]))
function commit(path:StructurePath){if(!props.disabled){localError.value='';indexDraft.value='';emit('update:modelValue',path)}}
function chooseProperty(key:string){if(properties.value.includes(key))commit([...tokens.value,key])}
function selectProperty(event:Event){
  const select=event.currentTarget as HTMLSelectElement,key=select.value
  select.value=''
  chooseProperty(key)
}
function chooseIndex(){
  if(props.disabled)return
  if(!/^(0|[1-9][0-9]*)$/.test(indexDraft.value)||!Number.isSafeInteger(Number(indexDraft.value))){localError.value='请输入明确的非负整数下标（从0开始）';return}
  commit([...tokens.value,Number(indexDraft.value)])
}
</script>
<template>
  <fieldset class="schema-path" :disabled="disabled">
    <legend>{{ label || '来源字段路径' }}</legend>
    <p>当前：<code>{{ Array.isArray(modelValue)?pathLabel(tokens):'尚未选择' }}</code></p>
    <p v-if="selected.error" role="alert">{{ selected.error }}；已保存路径未修改。</p>
    <div class="path-actions">
      <button type="button" class="button ghost tiny" @click="commit([])">使用整个值</button>
      <button v-if="tokens.length" type="button" class="button ghost tiny" @click="commit(tokens.slice(0,-1))">返回上一级</button>
    </div>
    <label v-if="!selected.error&&pathType(selected.schema)==='object'&&properties.length">继续选择对象属性
      <select value="" @change="selectProperty"><option value="">请选择目录字段</option><option v-for="key in properties" :key="key" :value="key">{{ jsonChildSchemas([selected.schema],key,false)[0]?.title || key }} · {{ key }}</option></select>
    </label>
    <div v-if="!selected.error&&pathType(selected.schema)==='array'">
      <label>固定条目的下标（逐条处理请使用列表映射）<input v-model="indexDraft" inputmode="numeric" aria-label="列表下标" /></label>
      <button type="button" class="button ghost tiny" @click="chooseIndex">选择此下标</button>
    </div>
    <p v-if="!selected.error&&!pathType(selected.schema)">来源未声明子字段结构，仅可读取整体；不会按实际值或目标结构猜测路径。</p>
    <p v-if="localError" role="alert">{{ localError }}</p>
  </fieldset>
</template>
<style scoped>
.schema-path{min-width:0;margin:4px 0;padding:8px;border:1px solid var(--border,#303743);display:grid;gap:8px}.schema-path label{display:grid;gap:5px;min-width:0}.schema-path select,.schema-path input{width:100%;min-width:0;min-height:36px;background:var(--bg-input,#090c10);color:inherit;border:1px solid var(--border,#303743);padding:6px}.schema-path p{margin:0;overflow-wrap:anywhere}.schema-path code{white-space:pre-wrap;overflow-wrap:anywhere}.path-actions{display:flex;flex-wrap:wrap;gap:6px}
</style>
