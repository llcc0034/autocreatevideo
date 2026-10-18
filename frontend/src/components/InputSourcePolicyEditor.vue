<script setup lang="ts">
import {computed} from 'vue'
import {sourcePolicyErrors,type InputSourceCatalog,type MediaSourcePolicy} from '@/services/inputSourcePolicies'

const props=withDefaults(defineProps<{modelValue:MediaSourcePolicy;catalog:InputSourceCatalog;purposes:{value:string;label:string}[];disabled?:boolean;showConstraints?:boolean}>(),{showConstraints:true})
const emit=defineEmits<{'update:modelValue':[MediaSourcePolicy]}>()
const choices=computed(()=>props.catalog.sources.filter(row=>row.kinds.includes(props.modelValue.media_kind)))
const errors=computed(()=>sourcePolicyErrors(props.modelValue,props.catalog))
const unknown=computed(()=>props.modelValue.allowed_sources.filter(key=>!choices.value.some(row=>row.value===key)))
function update(changes:Partial<MediaSourcePolicy>){if(!props.disabled)emit('update:modelValue',{...props.modelValue,...changes})}
function source(key:string,checked:boolean){
  if(checked&&(!choices.value.some(row=>row.value===key&&row.available)||props.modelValue.allowed_sources.includes(key)))return
  update({allowed_sources:checked?[...props.modelValue.allowed_sources,key]:props.modelValue.allowed_sources.filter(value=>value!==key)})
}
function purpose(key:string,checked:boolean){update({allowed_purposes:checked?[...props.modelValue.allowed_purposes,key]:props.modelValue.allowed_purposes.filter(value=>value!==key)})}
function quantity(key:'min_items'|'max_items',value:string){update({[key]:value.trim()===''?NaN:Number(value)})}
</script>
<template>
  <fieldset class="source-policy" :disabled="disabled">
    <legend>输入来源策略</legend>
    <p>此组件只编辑所属配置的草稿。默认来源只决定取值方式，不会自动选择素材或在失败后换图。</p>
    <label v-for="row in choices" :key="row.value" class="choice">
      <input type="checkbox" :checked="modelValue.allowed_sources.includes(row.value)" :disabled="!row.available&&!modelValue.allowed_sources.includes(row.value)" @change="source(row.value,($event.target as HTMLInputElement).checked)"/>
      <span>{{row.label}}<small>{{row.available?row.description:'不可用：'+row.description}}</small></span>
    </label>
    <p v-for="key in unknown" :key="key" role="alert">未登记的原来源：{{key}} <button type="button" class="button ghost small" @click="source(key,false)">移除此来源</button></p>
    <label>默认来源<select :value="modelValue.default_source" @change="update({default_source:($event.target as HTMLSelectElement).value})">
      <option value="">请选择</option><option v-if="modelValue.default_source&&!choices.some(row=>row.available&&row.value===modelValue.default_source&&modelValue.allowed_sources.includes(row.value))" :value="modelValue.default_source" disabled>原默认来源不可用：{{modelValue.default_source}}</option>
      <option v-for="row in choices.filter(row=>row.available&&modelValue.allowed_sources.includes(row.value))" :key="row.value" :value="row.value">{{row.label}}</option>
    </select></label>
    <label class="choice"><input type="checkbox" :checked="modelValue.allow_override" @change="update({allow_override:($event.target as HTMLInputElement).checked})"/>允许本次改选来源及素材（整组替换，不自动合并）</label>
    <div v-if="showConstraints" class="quantities"><label>至少多少项<input type="number" min="0" step="1" :value="Number.isFinite(modelValue.min_items)?modelValue.min_items:''" @input="quantity('min_items',($event.target as HTMLInputElement).value)"/></label><label>最多多少项<input type="number" min="0" step="1" :value="Number.isFinite(modelValue.max_items)?modelValue.max_items:''" @input="quantity('max_items',($event.target as HTMLInputElement).value)"/></label></div>
    <fieldset v-if="showConstraints"><legend>允许用途（来自所属目标目录）</legend>
      <label v-for="row in purposes" :key="row.value" class="choice"><input type="checkbox" :checked="modelValue.allowed_purposes.includes(row.value)" @change="purpose(row.value,($event.target as HTMLInputElement).checked)"/>{{row.label}}</label>
      <p v-if="!purposes.length">此处未声明用途；每项明确记录无用途，不推断为外观或动作参考。</p>
      <p v-for="key in modelValue.allowed_purposes.filter(value=>!purposes.some(row=>row.value===value))" :key="key" role="alert">原用途已不在目录：{{key}} <button type="button" class="button ghost small" @click="purpose(key,false)">移除此用途</button></p>
    </fieldset>
    <label>素材顺序<select :value="modelValue.order" @change="update({order:($event.target as HTMLSelectElement).value as 'provided'})"><option v-for="row in catalog.orders" :key="row.value" :value="row.value">{{row.label}}</option></select></label>
    <p v-for="error in errors" :key="error" role="alert">{{error}}</p>
  </fieldset>
</template>
<style scoped>
.source-policy{display:grid;gap:12px;min-width:0}.source-policy label{display:grid;gap:6px;min-width:0}.source-policy .choice{display:flex;align-items:start;gap:8px;min-height:36px}.source-policy small{display:block;line-height:1.6}.source-policy p{margin:0;font-size:12px;overflow-wrap:anywhere}.source-policy [role=alert]{color:var(--warning,#e9aa52)}.quantities{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.source-policy input,.source-policy select{min-width:0;max-width:100%}.source-policy input[type=number],.source-policy select{min-height:36px}.source-policy :is(button,input,select):focus-visible{outline:2px solid var(--accent-color,#e4ad60);outline-offset:2px}@media(max-width:520px){.quantities{grid-template-columns:1fr}}
</style>
