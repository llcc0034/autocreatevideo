<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { assetAppearanceApi, primaryTarget, variantTarget, referenceUnitKeys, type AssetAppearanceTarget, type AssetAppearanceOption, type AssetAppearancePage } from '@/services/assetAppearanceTarget'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { appearanceDifferenceLabels, type MediaAppearanceInputOptions } from '@/services/mediaAppearanceInputs'
import type { ExecutionRequestDraft } from '@/services/executionRequestForm'
import type { AssetReferenceSeries, AssetReferenceVersion } from '@/services/assetReferenceApi'
import type { Asset } from '@/types'
import AssetReferenceBrowser from './AssetReferenceBrowser.vue'

const props = defineProps<{ modelValue: AssetAppearanceTarget | null; disabled: boolean; draft: ExecutionRequestDraft; options: MediaAppearanceInputOptions | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: AssetAppearanceTarget | null]; basis: [assetId: string | null] }>()
const selected = ref<AssetAppearanceOption | null>(null), directory = ref<AssetAppearancePage | null>(null)
const search = ref(''), page = ref(1), loading = ref(false), error = ref('')
const primary = ref<{ version: AssetReferenceVersion; series: AssetReferenceSeries } | null>(null)
const browsing = ref(false)
const roleLabel = computed(() => props.modelValue?.role === 'variant' ? '变体' : '主图')
const inputItems = computed(() => (props.options?.primary_inputs ?? []).flatMap(input =>
  (props.draft.media[input.input_key] ?? []).map(item => ({ input_key: input.input_key, item_key: item.itemKey, assetId: item.assetId }))))
const selectedPrimaryInput = computed(() => inputItems.value.find(row =>
  row.input_key === props.modelValue?.primary_input?.input_key && row.item_key === props.modelValue?.primary_input?.item_key))
const differenceIndex = computed(() => {
  const target = props.modelValue
  if (target?.role !== 'variant' || !target.difference_input) return -1
  const source = target.difference_input
  return props.options?.difference_sources.findIndex(row => row.kind === source.kind && row.key === source.key
    && (!('field_key' in row) || ('field_key' in source && row.field_key === source.field_key))) ?? -1
})
const unitKeys = computed(() => props.modelValue?.role === 'variant' && props.modelValue.difference_input?.kind === 'reference_field'
  ? referenceUnitKeys(props.draft, props.modelValue.difference_input.key) : [])
let sequence = 0
onBeforeUnmount(() => { sequence++ })
watch(() => props.modelValue?.role, () => {
  sequence++; selected.value = null; directory.value = null; loading.value = false; error.value = ''; search.value = ''
  primary.value = null; browsing.value = false; emit('basis', null)
})
function changeRole(role: string) {
  if (props.disabled) return
  emit('update:modelValue', role === 'primary' ? primaryTarget(null) : role === 'variant' ? variantTarget() : null)
}
function choosePrimary(asset: Asset, reference: { version: AssetReferenceVersion; series: AssetReferenceSeries }) {
  if (props.disabled || props.modelValue?.role !== 'variant' || reference.series.role !== 'primary'
    || reference.version.series_id !== reference.series.id || reference.version.asset.id !== asset.id) return
  primary.value = reference; browsing.value = false; emit('basis', asset.id)
  emit('update:modelValue', { ...props.modelValue, primary_version_id: reference.version.id, primary_input: { input_key: '', item_key: '' } })
}
function chooseInput(index: number) {
  if (props.disabled || props.modelValue?.role !== 'variant') return
  const row = inputItems.value[index]
  emit('update:modelValue', { ...props.modelValue, primary_input: { input_key: row?.input_key ?? '', item_key: row?.item_key ?? '' } })
}
function chooseDifference(index: number) {
  if (props.disabled || props.modelValue?.role !== 'variant') return
  const row = props.options?.difference_sources[index]
  emit('update:modelValue', { ...props.modelValue, difference_input: !row ? null
    : row.kind === 'reference_field' ? { ...row, kind: 'reference_field', unit_key: '' } : { ...row } })
}
function chooseUnit(key: string) {
  if (props.disabled || props.modelValue?.role !== 'variant' || props.modelValue.difference_input?.kind !== 'reference_field'
    || (key && !unitKeys.value.includes(key))) return
  emit('update:modelValue', { ...props.modelValue, difference_input: { ...props.modelValue.difference_input, unit_key: key } })
}
async function load(targetPage = 1) {
  if (props.disabled || !props.modelValue) return
  const token = ++sequence; loading.value = true; error.value = ''
  try {
    const result = await assetAppearanceApi.options(targetPage, search.value, props.modelValue.role)
    if (token !== sequence) return
    directory.value = result; page.value = targetPage
  } catch (cause) { if (token === sequence) error.value = apiErrorSummary(cause) }
  finally { if (token === sequence) loading.value = false }
}
function choose(row: AssetAppearanceOption | null) {
  if (props.disabled || !props.modelValue || loading.value || (row && (error.value || !directory.value?.results.includes(row)))) return
  selected.value = row
  emit('update:modelValue', { ...props.modelValue, setting_revision_id: row?.setting_revision_id ?? null })
}
</script>

<template>
  <section class="appearance-target" aria-label="图片外观目标">
    <label>生成外观角色<select :value="modelValue?.role ?? ''" :disabled="disabled" @change="changeRole(($event.target as HTMLSelectElement).value)"><option value="">不声明角色</option><option value="primary">主图</option><option value="variant">变体</option></select></label>
    <p>准备只核对并冻结声明；生成成功后按声明登记素材系列版本，不代表审核采用。变体必须实际提供主图和差异，不能回退文生图。</p>
    <template v-if="modelValue?.role==='variant'">
      <p v-if="primary">依据主图：{{ primary.series.name }} · v{{ primary.version.version }} · {{ primary.version.asset.name }}</p><p v-else>尚未选择确切主图版本。</p>
      <button type="button" class="button ghost small" :disabled="disabled" @click="browsing=!browsing">{{ browsing ? '收起版本浏览' : '选择主图素材版本' }}</button>
      <AssetReferenceBrowser v-if="browsing" selection-kind="image" selection-role="primary" :disabled="disabled" @selected="choosePrimary"/>
      <label>主图在原输入中的位置<select :disabled="disabled" :value="inputItems.findIndex(row=>row.input_key===modelValue!.primary_input?.input_key && row.item_key===modelValue!.primary_input?.item_key)" @change="chooseInput(Number(($event.target as HTMLSelectElement).value))"><option :value="-1">请选择原输入项</option><option v-for="(row,index) in inputItems" :key="JSON.stringify([row.input_key,row.item_key])" :value="index">{{ row.input_key }} / {{ row.item_key }} · {{ row.assetId || '尚未选图' }}</option></select></label>
      <p v-if="selectedPrimaryInput">实际主图输入：<code>{{ selectedPrimaryInput.input_key }}</code> / <code>{{ selectedPrimaryInput.item_key }}</code> · 原素材 <code>{{ selectedPrimaryInput.assetId || '尚未选图' }}</code></p>
      <p>先在下方原输入中选择或上传图片，再指定同一主图所在项。上传不会自动建立主图版本；可在素材库关系管理中登记。</p>
      <label>变体差异来源<select :value="differenceIndex" :disabled="disabled" @change="chooseDifference(Number(($event.target as HTMLSelectElement).value))"><option :value="-1">请选择原来源，不另填第二份正文</option><option v-for="(row,index) in options?.difference_sources ?? []" :key="JSON.stringify(row)" :value="index">{{ appearanceDifferenceLabels[row.kind] }} · {{ row.key }}{{ 'field_key' in row ? ' / '+row.field_key : '' }}</option></select></label>
      <template v-if="modelValue.difference_input?.kind==='reference_field'"><label>差异参考单元<select :value="modelValue.difference_input.unit_key" :disabled="disabled" @change="chooseUnit(($event.target as HTMLSelectElement).value)"><option value="">请选择确切单元</option><option v-for="key in unitKeys" :key="key" :value="key">{{ key }}</option></select></label><p v-if="!unitKeys.length" role="alert">原输入尚无可选单元。请提供原参考输入；上下文来源不猜测单元标识。</p></template>
    </template>
    <template v-if="modelValue">
      <p>关联设定可留空。选择只固定来源修订，不代替下方实际输入、提示词或人工采用。</p>
      <p v-if="selected">已选：{{ selected.project_title }} / {{ selected.entity_name }} / {{ selected.name }} · 修订 {{ selected.revision }}</p>
      <p v-else>未关联设定，不会自动查找或补齐。</p>
      <button v-if="selected" type="button" class="button ghost small" :disabled="disabled || loading" @click="choose(null)">移除设定引用</button>
      <div class="appearance-controls"><label>搜索{{ roleLabel }}设定<input v-model="search" maxlength="160" :disabled="disabled || loading" @keydown.enter.prevent="load(1)"/></label><button type="button" class="button ghost small" :disabled="disabled || loading" @click="load(1)">查询{{ roleLabel }}设定</button></div>
      <p v-if="loading" role="status">正在读取{{ roleLabel }}设定…</p>
      <p v-if="error" role="alert">{{ error }}{{ directory ? '（保留上次目录，不可从陈旧目录选择）' : '' }}</p>
      <button v-if="error" type="button" class="button ghost small" :disabled="disabled || loading" @click="load(page)">重试读取{{ roleLabel }}设定</button>
      <template v-if="directory">
        <p>共 {{ directory.count }} 个{{ roleLabel }}设定 · 第 {{ page }} 页。刷新目录不会替换已选修订。</p>
        <p v-if="!directory.results.length">没有匹配的{{ roleLabel }}设定；可修改搜索或不关联设定。</p>
        <ul><li v-for="row in directory.results" :key="row.setting_revision_id"><span>{{ row.project_title }} / {{ row.entity_name }} / {{ row.name }}（{{ row.reference_name }}）· 修订 {{ row.revision }}</span><button type="button" class="button ghost small" :disabled="disabled || loading || !!error" @click="choose(row)">选择此修订</button></li></ul>
        <nav class="appearance-controls" :aria-label="roleLabel+'设定分页'"><button type="button" class="button ghost small" :disabled="disabled || loading || !directory.previous" @click="load(page-1)">上一页</button><button type="button" class="button ghost small" :disabled="disabled || loading || !directory.next" @click="load(page+1)">下一页</button></nav>
      </template>
      <details v-if="modelValue.setting_revision_id"><summary>确切设定修订标识</summary><code>{{ modelValue.setting_revision_id }}</code></details>
    </template>
  </section>
</template>

<style scoped>
.appearance-target select{width:100%;max-width:100%;min-width:0}
.appearance-target{display:grid;gap:8px;min-width:0;padding:12px;border:1px solid var(--border);border-radius:8px}.appearance-target p{margin:0}.appearance-target p,.appearance-target li,.appearance-target code{overflow-wrap:anywhere;font-size:12px}.appearance-target label{display:grid;gap:6px;min-width:0}.appearance-target input{max-width:100%;min-width:0}.primary-check{display:flex!important;align-items:center;gap:8px}.primary-check input{width:auto}.appearance-controls,.appearance-target li{display:flex;gap:8px;align-items:end;flex-wrap:wrap}.appearance-target ul{list-style:none;margin:0;padding:0}.appearance-target li{justify-content:space-between;padding:8px 0}.appearance-target button{white-space:normal}@media(max-width:640px){.appearance-controls{display:grid;grid-template-columns:minmax(0,1fr)}.appearance-target li{align-items:start;flex-direction:column}}
</style>
