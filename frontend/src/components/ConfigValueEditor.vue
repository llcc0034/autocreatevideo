<script setup lang="ts">
import { computed } from 'vue'
import { configValueConstraintSummary, configValueEditorKind, parseConfigValue, type ConfigResourceOption } from '@/services/configValueSchema'
import type { ConfigDefinition } from '@/services/platformTypes'

const props = defineProps<{ definition: ConfigDefinition; modelValue: string; disabled?: boolean; resourceOptions?: ConfigResourceOption[] }>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; change: [] }>()
const kind = computed(() => configValueEditorKind(props.definition))
const validation = computed(() => parseConfigValue(props.definition, props.modelValue || ''))
const parsedValue = computed(() => validation.value.error ? null : validation.value.value)
const enumOptions = computed(() => Array.isArray(props.definition.valueSchema?.enum) ? props.definition.valueSchema.enum : [])
const scalarValue = computed(() => typeof parsedValue.value === 'string' || typeof parsedValue.value === 'number' ? String(parsedValue.value) : '')
const update = (value: string) => { emit('update:modelValue', value); emit('change') }
const updateString = (event: Event) => update(JSON.stringify((event.target as HTMLInputElement).value))
const updateRaw = (event: Event) => update((event.target as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement).value)
</script>

<template>
  <div class="config-value-editor" :class="{ invalid: Boolean(validation.error) }">
    <select v-if="kind==='boolean'" :value="modelValue" :disabled="disabled" @change="updateRaw">
      <option value="true">启用（true）</option><option value="false">停用（false）</option>
    </select>
    <select v-else-if="kind==='enum'" :value="modelValue" :disabled="disabled" @change="updateRaw">
      <option v-for="option in enumOptions" :key="JSON.stringify(option)" :value="JSON.stringify(option)">{{ typeof option==='string' ? option : JSON.stringify(option) }}</option>
    </select>
    <select v-else-if="kind==='resource'" :value="modelValue" :disabled="disabled" @change="updateRaw">
      <option :value="JSON.stringify('')">请选择满足依赖的已发布资源</option>
      <option v-for="option in resourceOptions || []" :key="option.value" :value="JSON.stringify(option.value)" :disabled="option.disabled">
        {{ option.label }}{{ option.reason ? ` · ${option.reason}` : '' }}
      </option>
    </select>
    <p v-if="kind==='resource' && !(resourceOptions || []).some((option)=>!option.disabled)" class="config-resource-empty">
      暂无满足条件的资源；请先到“模型”完成发布、启用、凭据与真实 smoke。
    </p>
    <input v-else-if="kind==='number'" :value="scalarValue" type="number" :step="definition.valueType==='integer'?1:'any'" :min="definition.valueSchema.minimum!==undefined&&Number.isFinite(Number(definition.valueSchema.minimum))?Number(definition.valueSchema.minimum):undefined" :max="definition.valueSchema.maximum!==undefined&&Number.isFinite(Number(definition.valueSchema.maximum))?Number(definition.valueSchema.maximum):undefined" :disabled="disabled" @input="updateRaw"/>
    <input v-else-if="kind==='string'" :value="scalarValue" type="text" :pattern="typeof definition.valueSchema.pattern==='string'?definition.valueSchema.pattern:undefined" :minlength="Number.isInteger(Number(definition.valueSchema.minLength))?Number(definition.valueSchema.minLength):undefined" :maxlength="Number.isInteger(Number(definition.valueSchema.maxLength))?Number(definition.valueSchema.maxLength):undefined" :disabled="disabled" @input="updateString"/>
    <textarea v-else :value="modelValue" rows="5" spellcheck="false" :disabled="disabled" @input="updateRaw"></textarea>
    <small>{{ configValueConstraintSummary(definition) }}</small>
    <p v-if="validation.error" class="config-value-error">{{ validation.error }}</p>
  </div>
</template>

<style scoped>
.config-value-editor{display:grid;gap:6px}.config-value-editor input,.config-value-editor select,.config-value-editor textarea{width:100%;border:1px solid #2a3039;border-radius:8px;background:#0b0e12;color:#f7f8fa;padding:10px 12px}.config-value-editor small{color:#75808d}.config-value-editor.invalid input,.config-value-editor.invalid select,.config-value-editor.invalid textarea{border-color:#c95757}.config-value-error{margin:0;color:#ff8b8b;font-size:11px}
.config-resource-empty{margin:0;color:#d89a52;font-size:11px}
</style>
