<script setup lang="ts">
import { computed } from 'vue'
import type { JsonObject } from '@/services/platformTypes'
import { parameterRangeFields, updateParameterRange } from '@/services/workflowParameterRanges'
const props = defineProps<{ modelValue?: unknown; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: JsonObject] }>()
const range = computed(() => props.modelValue && typeof props.modelValue === 'object' && !Array.isArray(props.modelValue) ? props.modelValue as JsonObject : {})
</script>
<template>
  <details class="parameter-range" :open="Object.keys(range).length > 0">
    <summary>运行参数范围与默认值</summary>
    <p>仅数值参数适用。留空继承节点／引擎合同；填写后只能收窄，不能扩大其能力。默认值必须合法，不会自动截断。</p>
    <div class="range-fields">
      <label v-for="field in parameterRangeFields" :key="field.key"><span>{{ field.label }}</span><input type="number" :step="field.key.startsWith('alignment_') ? 1 : 'any'" :value="range[field.key] ?? ''" :disabled="disabled" placeholder="继承合同" @input="emit('update:modelValue',updateParameterRange(range,field.key,($event.target as HTMLInputElement).value))"/></label>
    </div>
    <p>例如 LoRA 强度 0–1、步长 0.05；帧数 5 + 17k 填模数 17、余数 5。只有最终合同开放的参数能在运行时调整，发布修改后需重新生成并验收工作流。</p>
  </details>
</template>
<style scoped>
.parameter-range{grid-column:1/-1;border-top:1px solid var(--border-color,#29333b);padding-top:12px}.parameter-range summary{cursor:pointer}.parameter-range p{font-size:12px;color:var(--text-muted,#96a5b3);margin:10px 0}.range-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.range-fields label{display:grid;gap:6px}.range-fields input{min-width:0;min-height:36px;width:100%}
</style>
