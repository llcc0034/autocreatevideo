<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { dataStructures, type DataStructureSummary } from '@/services/dataStructures'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import DataStructureValuePreview from './DataStructureValuePreview.vue'

const props = defineProps<{ modelValue: string; disabled?: boolean; previewOnly?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [string] }>()
const rows = ref<DataStructureSummary[]>([]), selected = ref<DataStructureSummary | null>(null)
const page = ref(1), count = ref(0), search = ref('')
const busy = ref(false), selectionBusy = ref(false), error = ref(''), selectionError = ref('')
const previewOpen = ref(false)
let serial = 0, selectionSerial = 0
async function chooseVersion(event: Event) {
  const target = event.target as HTMLSelectElement
  if (!props.disabled) emit('update:modelValue', target.value)
  // The owner can reject a dependent mapping change; keep the native control
  // on the accepted value even when no prop change triggers a Vue patch.
  await nextTick()
  target.value = props.modelValue
}
onBeforeUnmount(() => { serial++; selectionSerial++ })
function openOptions(event: Event) {
  if (!props.disabled && (event.target as HTMLDetailsElement).open && !rows.value.length && !busy.value) return load()
}
async function load() {
  const request = ++serial
  busy.value = true; error.value = ''; rows.value = []; count.value = 0
  try {
    const result = await dataStructures.list(page.value, search.value, props.previewOnly ? '' : 'published')
    if (request === serial) { rows.value = result.results; count.value = result.count }
  } catch (cause) {
    if (request === serial) error.value = apiErrorSummary(cause, '结构版本目录读取失败')
  } finally { if (request === serial) busy.value = false }
}
async function loadSelection() {
  const request = ++selectionSerial
  previewOpen.value=false
  selected.value = null; selectionError.value = ''; selectionBusy.value = Boolean(props.modelValue)
  if (!props.modelValue) return
  try {
    const result = await dataStructures.get(props.modelValue)
    if (request === selectionSerial) selected.value = result
  } catch (cause) {
    if (request === selectionSerial) selectionError.value = apiErrorSummary(cause, '所选结构版本读取失败')
  } finally { if (request === selectionSerial) selectionBusy.value = false }
}
watch(() => props.modelValue, loadSelection, { immediate: true })
</script>

<template>
  <div class="structure-select">
    <p v-if="selectionBusy">正在读取已选结构…</p>
    <p v-else-if="selected">已选：{{ selected.name }} · v{{ selected.version }}<span v-if="selected.status !== 'published'"> · {{ selected.status }}（仅可试配，不用于新发布）</span></p>
    <p v-else-if="!modelValue">尚未选择结构版本</p>
    <p v-if="selectionError" role="alert">{{ selectionError }} <button type="button" @click="loadSelection">重新读取已选结构</button></p>
    <details v-if="!disabled" @toggle="openOptions">
      <summary>选择确切结构版本</summary>
      <div class="structure-toolbar">
        <label>搜索结构名称<input v-model="search" :disabled="busy" @keydown.enter.prevent="page=1; load()" /></label>
        <button type="button" :disabled="busy" @click="page=1; load()">查询／刷新结构</button>
      </div>
      <p v-if="busy" role="status">正在读取结构目录…</p>
      <p v-if="error" role="alert">{{ error }}</p>
      <label>结构版本
        <select :value="modelValue" :disabled="busy || Boolean(error)" @change="chooseVersion">
          <option value="">请选择，不自动使用最新版</option>
          <option v-if="modelValue && !rows.some(row => row.id === modelValue)" :value="modelValue" disabled>{{ selected ? `${selected.name} · v${selected.version}` : '已选版本不在本页' }}</option>
          <option v-for="row in rows" :key="row.id" :value="row.id">{{ row.name }} · v{{ row.version }}{{ previewOnly ? ' · '+row.status : '' }}</option>
        </select>
      </label>
      <p v-if="!busy && !error && !rows.length">当前条件没有{{ previewOnly ? '' : '已发布' }}结构；不能用空结构代替。</p>
      <div class="structure-toolbar">
        <button type="button" :disabled="busy || page===1" @click="page--; load()">上一页</button>
        <span>第 {{ page }} 页 · 共 {{ count }} 项</span>
        <button type="button" :disabled="busy || page*20>=count" @click="page++; load()">下一页</button>
      </div>
    </details>
    <small>结构引用字段定义的确切版本；后续新版本不会替换本次选择。</small>
    <details v-if="selected && !previewOnly" @toggle="previewOpen=($event.target as HTMLDetailsElement).open">
      <summary>试填结构样例（不保存、不执行）</summary>
      <DataStructureValuePreview v-if="previewOpen" :key="selected.id" :structure-version-id="selected.id"/>
    </details>
  </div>
</template>

<style scoped>
.structure-select{display:grid;gap:.5rem;min-width:0}.structure-select p{margin:0;overflow-wrap:anywhere}.structure-select details{display:grid;gap:.6rem}.structure-select summary{cursor:pointer}.structure-select label{display:grid;gap:.4rem}.structure-toolbar{display:flex;gap:.6rem;align-items:end;flex-wrap:wrap;margin:.6rem 0}.structure-select input,.structure-select select{box-sizing:border-box;width:100%;min-width:0;background:var(--bg-input,#0c1015);color:inherit;border:1px solid var(--border-color,#3b4551);border-radius:6px;padding:.65rem}.structure-select button{min-height:36px;color:inherit;background:var(--bg-secondary,#181e24);border:1px solid var(--border-color,#46525f);border-radius:6px;padding:.5rem}.structure-select button:disabled{opacity:.45}.structure-select :is(input,select,button,summary):focus-visible{outline:2px solid var(--accent-color,#e4ad60);outline-offset:2px}
</style>
