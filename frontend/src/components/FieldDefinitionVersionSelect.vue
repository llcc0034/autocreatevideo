<script setup lang="ts">
import { onBeforeUnmount, ref, useId, watch } from 'vue'
import { fieldDefinitionVersions, type FieldDefinitionVersion, type FieldDefinitionVersionSummary } from '@/services/fieldDefinitionVersions'
import { apiErrorSummary } from '@/services/apiErrorPresentation'

const props = defineProps<{ fieldDefinition?: string; modelValue: string; disabled?: boolean; global?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [string]; selected: [FieldDefinitionVersion | null] }>()
const selectId = useId()
const rows = ref<FieldDefinitionVersionSummary[]>([])
const busy = ref(false)
const error = ref('')
const page = ref(1), count = ref(0), search = ref('')
const selectedVersion = ref<FieldDefinitionVersion | null>(null)
let serial = 0
onBeforeUnmount(() => { serial++ })
const load = async () => {
  const request = ++serial
  rows.value = []; error.value = ''; selectedVersion.value = null; emit('selected', null)
  if (!props.fieldDefinition && !props.global) { busy.value = false; return }
  busy.value = true
  try {
    const [versions, selected] = await Promise.all([
      props.global ? fieldDefinitionVersions.catalog(page.value, search.value) : fieldDefinitionVersions.list(props.fieldDefinition!),
      props.modelValue ? fieldDefinitionVersions.get(props.modelValue) : null,
    ])
    if (request !== serial) return
    const entries = Array.isArray(versions) ? versions : versions.results
    count.value = Array.isArray(versions) ? versions.length : versions.count
    rows.value = entries.filter(row => row.status === 'published' || row.id === props.modelValue)
    if (selected && !props.global && selected.field_definition !== props.fieldDefinition) throw new Error('所选版本不属于当前字段，请重新选择')
    selectedVersion.value = selected
    emit('selected', selected)
  } catch (cause) {
    if (request === serial) error.value = apiErrorSummary(cause, '字段定义版本读取失败')
  } finally { if (request === serial) busy.value = false }
}
watch(() => [props.fieldDefinition, props.modelValue], load, { immediate: true })
</script>

<template>
  <div class="field-version-select">
    <label :for="selectId">字段定义版本 <i>必填</i></label>
    <span v-if="global"><input v-model="search" aria-label="搜索字段名称或标识" :disabled="busy || disabled" @keydown.enter.prevent="page=1; load()" /><button type="button" :disabled="busy || disabled" @click="page=1; load()">查询字段版本</button></span>
    <select :id="selectId" :value="modelValue" :disabled="disabled || busy || (!fieldDefinition && !global) || Boolean(error)" @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value)">
      <option value="">{{ busy ? '正在读取版本…' : '请选择确切版本，不自动使用最新版' }}</option>
      <option v-if="modelValue && !rows.some(row => row.id === modelValue)" :value="modelValue" disabled>{{ selectedVersion ? `${selectedVersion.label} · v${selectedVersion.version}` : '已选版本不在本页' }}</option>
      <option v-for="row in rows" :key="row.id" :value="row.id" :disabled="row.status !== 'published'">v{{ row.version }} · {{ row.label }} · {{ row.value_type }}{{ row.unit ? ` · ${row.unit}` : '' }}{{ row.status === 'published' ? '' : ' · 不再可选' }}</option>
    </select>
    <button v-if="!global" type="button" class="button ghost" :disabled="disabled || busy || !fieldDefinition" @click="load">{{ busy ? '正在读取版本…' : '刷新字段版本目录' }}</button>
    <span v-if="global"><button type="button" :disabled="disabled || busy || page<=1" @click="page--; load()">上一页</button> 第 {{ page }} 页 · 共 {{ count }} 项 <button type="button" :disabled="disabled || busy || page*20>=count" @click="page++; load()">下一页</button></span>
    <small>使用处冻结所选版本的含义、类型和单位；新版本不会改变已有引用。</small>
    <small v-if="!busy && fieldDefinition && !error && !rows.length">该字段尚无可用定义版本，请先维护并启用字段版本。</small>
    <small v-if="error" role="alert">{{ error }} <button v-if="global" type="button" class="btn" :disabled="disabled || busy" @click="load">重新读取</button></small>
  </div>
</template>

<style scoped>
.field-version-select{display:grid;gap:6px;min-width:0}.field-version-select>span{display:flex;gap:6px;flex-wrap:wrap;align-items:center}.field-version-select select{max-width:100%;min-width:0}
</style>
