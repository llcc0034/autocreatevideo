<script setup lang="ts">
import { computed } from 'vue'
import { capabilityFieldLabel, contractScalar, isContractObject } from '@/services/modelCapabilityContract'

const props = withDefaults(defineProps<{ value: unknown; labelForKey?: (key: string) => string }>(), {
  labelForKey: capabilityFieldLabel,
})
const fields = computed(() => isContractObject(props.value) ? Object.entries(props.value) : [])
</script>

<template>
  <dl v-if="fields.length" class="contract-fields">
    <div v-for="[key, value] in fields" :key="key" class="contract-field" :class="{group: isContractObject(value) || (Array.isArray(value) && value.some(item => isContractObject(item) || Array.isArray(item)))}">
      <dt :title="key">{{ labelForKey(key) }}<small v-if="labelForKey(key) !== key">{{ key }}</small></dt>
      <dd><ModelContractFields :value="value" :label-for-key="labelForKey" /></dd>
    </div>
  </dl>
  <span v-else-if="isContractObject(value)" class="empty-value">空对象（未声明子项）</span>
  <template v-else-if="Array.isArray(value)">
    <span v-if="!value.length" class="empty-value">空列表（0 项）</span>
    <ul v-else class="contract-values">
      <li v-for="(item, index) in value" :key="index"><ModelContractFields :value="item" :label-for-key="labelForKey" /></li>
    </ul>
  </template>
  <span v-else-if="typeof value === 'string' && value.length > 0 && !value.trim()" class="contract-value"><code>{{ JSON.stringify(value) }}</code> <span class="empty-value">（空白字符，按 JSON 转义显示）</span></span>
  <span v-else class="contract-value">{{ contractScalar(value) }}</span>
</template>

<style scoped>
.contract-fields{display:grid;margin:0;gap:8px;min-width:0}
.contract-field{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr);gap:12px;padding:6px 0;border-bottom:1px solid var(--border)}
.contract-field.group{grid-template-columns:minmax(0,1fr)}
.group>dd{border-left:1px solid var(--border-strong);padding-left:12px}
dt{color:var(--text-secondary);overflow-wrap:anywhere}dt small{display:block;font-size:10px;overflow-wrap:anywhere}dd{margin:0;min-width:0}
.contract-value{white-space:pre-wrap;overflow-wrap:anywhere}.empty-value{color:var(--text-secondary)}
.contract-values{padding-left:18px;margin:0;display:grid;gap:8px;overflow-wrap:anywhere}
@media(max-width:700px){.contract-field{grid-template-columns:minmax(0,1fr);gap:4px}}
</style>
