<script setup lang="ts">
const props=defineProps<{modelValue:unknown;type:string;label:string;disabled?:boolean;choices?:unknown[]}>()
const emit=defineEmits<{'update:modelValue':[value:unknown]}>()
function update(raw:string){if(!props.disabled)emit('update:modelValue',props.choices?props.choices[Number(raw)]:props.type==='boolean'?(raw===''?undefined:raw==='true'):props.type==='string'?raw:raw===''?undefined:Number(raw))}
</script>
<template>
  <select v-if="choices" :aria-label="label" :disabled="disabled" :value="choices.indexOf(modelValue)<0?'':String(choices.indexOf(modelValue))" @change="update(($event.target as HTMLSelectElement).value)"><option value="" disabled>请选择</option><option v-for="(value,index) in choices" :key="index" :value="String(index)">{{ JSON.stringify(value) }}</option></select>
  <select v-else-if="type==='boolean'" :aria-label="label" :disabled="disabled" :value="typeof modelValue==='boolean'?String(modelValue):''" @change="update(($event.target as HTMLSelectElement).value)"><option value="">请选择</option><option value="true">是（true）</option><option value="false">否（false）</option></select>
  <textarea v-else-if="type==='string'" :aria-label="label" :disabled="disabled" :value="typeof modelValue==='string'?modelValue:''" rows="2" @input="update(($event.target as HTMLTextAreaElement).value)"/>
  <input v-else type="number" :step="type==='integer'?1:'any'" :aria-label="label" :disabled="disabled" :value="typeof modelValue==='number'?modelValue:''" @input="update(($event.target as HTMLInputElement).value)"/>
</template>
<style scoped>
input,select,textarea{box-sizing:border-box;width:100%;min-width:0;min-height:38px}textarea{resize:vertical}
</style>
