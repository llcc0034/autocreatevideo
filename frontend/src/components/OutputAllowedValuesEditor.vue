<script setup lang="ts">
import {computed,watch} from 'vue'
import type {JsonObject} from '@/services/platformTypes'
import {outputAllowedValueErrors,type OutputAllowedValueField} from '@/services/outputAllowedValues'
const props=defineProps<{modelValue:JsonObject;fields:OutputAllowedValueField[];disabled?:boolean}>()
const emit=defineEmits<{'update:modelValue':[value:JsonObject];validity:[errors:string[]]}>()
const errors=computed(()=>outputAllowedValueErrors(props.modelValue,props.fields))
watch(errors,value=>emit('validity',value),{immediate:true,deep:true})
function limit(field:OutputAllowedValueField,enabled:boolean){
  if(props.disabled)return
  const next={...props.modelValue}
  if(enabled)next[field.key]=[...field.choices];else delete next[field.key]
  emit('update:modelValue',next)
}
function select(field:OutputAllowedValueField,choice:string|boolean,enabled:boolean){
  if(props.disabled||!Array.isArray(props.modelValue[field.key]))return
  const current=props.modelValue[field.key] as (string|boolean)[]
  emit('update:modelValue',{...props.modelValue,[field.key]:field.choices.filter(v=>v===choice?enabled:current.includes(v))})
}
</script>
<template>
  <section class="allowed-values">
    <h4>模型输出允许取值</h4>
    <p>默认接受平台支持的全部枚举。启用限定后，只从下列选项选择；不允许手填 Key、别名或路径。保存后生成相应格式示例，示例不是生成结果。</p>
    <p>这是输出内容约定，不是预算、次数或长度额度。响应不符合时保留原文供人工处理，不自动替换；数量与长度继续使用下方质量建议。</p>
    <details v-for="field in fields" :key="field.key">
      <summary>{{field.label}} · {{modelValue[field.key]===undefined?'全部合法取值':`${(modelValue[field.key] as unknown[]).length} 项允许`}}</summary>
      <fieldset :disabled="disabled"><legend>{{field.label}} · color_design.{{field.key}}</legend>
        <label><input type="checkbox" :checked="modelValue[field.key]!==undefined" @change="limit(field,($event.target as HTMLInputElement).checked)"/>限定{{field.label}}取值</label>
        <div v-if="modelValue[field.key]!==undefined" class="choices"><label v-for="choice in field.choices" :key="String(choice)"><input type="checkbox" :checked="(modelValue[field.key] as unknown[]).includes(choice)" @change="select(field,choice,($event.target as HTMLInputElement).checked)"/>{{typeof choice==='boolean'?(choice?'是（true）':'否（false）'):choice}}</label></div>
      </fieldset>
    </details>
    <ul v-if="errors.length" role="alert"><li v-for="error in errors" :key="error">{{error}}</li></ul>
  </section>
</template>
<style scoped>
.allowed-values{display:grid;gap:var(--config-space-md);min-width:0;font:var(--config-body-size)/1.6 var(--font)}.allowed-values h4{font-size:var(--config-section-size);margin:0}.allowed-values p{margin:0;color:var(--text-secondary)}.allowed-values fieldset{min-width:0;border:1px solid var(--border);padding:var(--config-space-md)}.allowed-values label{display:flex;align-items:center;gap:var(--config-space-sm);min-height:var(--config-control-height)}.allowed-values input{width:auto;accent-color:var(--accent)}.choices{display:flex;gap:var(--config-space-xs) var(--config-space-lg);flex-wrap:wrap}.allowed-values summary{cursor:pointer;min-height:var(--config-control-height);overflow-wrap:anywhere}.allowed-values legend{overflow-wrap:anywhere}.allowed-values [role=alert]{color:var(--warning)}.allowed-values input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:640px){.allowed-values{--config-control-height:44px}}
</style>
