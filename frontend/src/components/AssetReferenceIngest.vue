<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { assetReferenceApi, type AssetReferenceSeries, type ReferenceSeriesPage } from '@/services/assetReferenceApi'
import { createReferenceIngest } from '@/services/assetReferenceIngest'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import ExecutionInputAssetPicker from './ExecutionInputAssetPicker.vue'
import AssetMediaPreview from './AssetMediaPreview.vue'

const emit = defineEmits<{ close: []; changed: []; busy: [value: boolean] }>()
const flow = createReferenceIngest(assetReferenceApi), state = flow.state
const mode = ref<'existing' | 'create'>('existing'), role = ref<'primary' | 'variant'>('primary')
const name = ref(''), search = ref(''), page = ref(1), directory = ref<ReferenceSeriesPage | null>(null)
const parent = ref<AssetReferenceSeries | null>(null), loading = ref(false), error = ref(''), uploadBusy = ref(false)
const roles = ref<Awaited<ReturnType<typeof assetReferenceApi.roles>>>([])
const kind = ref<'image' | 'video' | 'audio'>('image')
const busy = computed(() => state.busy || uploadBusy.value)
let sequence = 0
let disposed = false
watch(busy, value => emit('busy', value))
onBeforeUnmount(() => { disposed = true; sequence++; flow.dispose(); emit('busy', false) })
async function loadRoles() { try { const result = await assetReferenceApi.roles(); if(!disposed) { roles.value = result; error.value = '' } } catch(cause) { if(!disposed) error.value = apiErrorSummary(cause) } }
onMounted(loadRoles)
const choosingParent = computed(() => mode.value === 'create' && role.value === 'variant')
const roleLabel = (value: string) => roles.value.find(item => item.value === value)?.display_name || value
async function load(next = 1) {
  if (busy.value) return
  const token = ++sequence; loading.value = true; error.value = ''
  try {
    const result = await assetReferenceApi.listSeries({page: next, search: search.value, ...(choosingParent.value ? {role: 'primary' as const} : {})})
    if (token === sequence) { directory.value = result; page.value = next }
  } catch(cause) { if (token === sequence) error.value = apiErrorSummary(cause) }
  finally { if (token === sequence) loading.value = false }
}
function changeMode() {
  sequence++; directory.value = null; loading.value = false; parent.value = null; error.value = ''
  flow.selectSeries(null)
}
watch([mode, role], changeMode)
function choose(series: AssetReferenceSeries) {
  if (busy.value || loading.value) return
  if (choosingParent.value) parent.value = series
  else flow.selectSeries(series)
}
async function create() {
  if (!name.value.trim() || !roles.value.length || busy.value || (choosingParent.value && !parent.value)) return
  await flow.create({name: name.value.trim(), role: role.value, primary_series_id: choosingParent.value ? parent.value!.id : null})
}
async function attach() {
  await flow.attach()
  if (state.result) emit('changed')
}
watch(kind, () => { if(!busy.value){state.asset = null; state.result = null; state.error = ''} })
</script>

<template>
  <section class="reference-ingest panel" aria-label="主图与变体入库">
    <header><h3>主图与变体入库</h3><button class="button ghost small" :disabled="busy" @click="emit('close')">收起</button></header>
    <p class="muted">先选择或创建系列，再上传／选择素材。系列关联不会生成图片，也不会改动原素材。</p>
    <div class="reference-controls">
      <label>目标系列<select v-model="mode" :disabled="busy"><option value="existing">加入已有系列</option><option value="create">创建主图或变体系列</option></select></label>
      <template v-if="mode === 'create'">
        <label>系列角色<select v-model="role" :disabled="busy || !!state.target"><option v-for="item in roles" :key="item.value" :value="item.value">{{ item.display_name }}</option></select></label>
        <label>系列名称<input v-model="name" maxlength="160" :disabled="busy || !!state.target" placeholder="任意语言的引用名称"/></label>
      </template>
    </div>
    <button v-if="!roles.length" class="button ghost small" :disabled="busy" @click="loadRoles">重新读取系列角色</button>
    <div v-if="!state.target && (mode === 'existing' || choosingParent)">
      <form class="reference-controls" @submit.prevent="load(1)"><label>{{ choosingParent ? '搜索父主图系列' : '搜索已有系列' }}<input v-model="search" :disabled="busy"/></label><button class="button ghost small" :disabled="busy || loading">查询系列</button></form>
      <p v-if="loading" role="status">正在查询系列…</p>
      <p v-if="!directory && !loading" class="muted">请查询并选择确切系列，不会根据名称自动匹配。</p>
      <template v-if="directory">
        <p>共 {{ directory.count }} 个系列 · 第 {{ page }} 页<span v-if="error">（保留上次结果）</span></p>
        <ul><li v-for="item in directory.results" :key="item.id"><span>{{ item.name }} · {{ roleLabel(item.role) }} · {{ item.version_count }} 个版本</span><button class="button ghost small" :disabled="busy || loading" @click="choose(item)">{{ choosingParent ? '选为父主图' : '加入此系列' }}</button></li></ul>
        <p v-if="!directory.results.length">没有匹配系列，可修改搜索或创建新系列。</p>
        <nav class="reference-controls" aria-label="素材系列分页"><button class="button ghost small" :disabled="busy || loading || !directory.previous" @click="load(page-1)">上一页</button><button class="button ghost small" :disabled="busy || loading || !directory.next" @click="load(page+1)">下一页</button></nav>
      </template>
    </div>
    <p v-if="parent && !state.target">父主图系列：{{ parent.name }}</p>
    <button v-if="mode === 'create' && !state.target" class="button primary small" :disabled="busy || !roles.length || !name.trim() || (choosingParent && !parent) || state.creationUnknown" @click="create">创建系列</button>
    <p v-if="state.creationUnknown" role="status">可切换“加入已有系列”并查询刚才的名称，核对是否已经创建。</p>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}<button class="button ghost small" :disabled="busy || loading" @click="load(1)">重新查询</button></p>
    <template v-if="state.target">
      <p class="target-summary">目标：{{ state.target.name }} · {{ roleLabel(state.target.role) }}<button class="button ghost small" :disabled="busy" @click="flow.selectSeries(null)">另选系列</button></p>
      <label>素材类型<select v-model="kind" :disabled="busy"><option value="image">图片</option><option value="video">视频</option><option value="audio">音频</option></select></label>
      <ExecutionInputAssetPicker :kind="kind" :disabled="busy" @selected="flow.selectAsset" @busy="uploadBusy=$event"/>
      <template v-if="state.asset"><p>已入库素材：{{ state.asset.name }} · {{ state.source === 'upload' ? '本次上传' : '已有素材' }}</p><div class="reference-preview"><AssetMediaPreview :asset="state.asset"/></div><button class="button primary small" :disabled="busy || !!state.result" @click="attach">{{ state.busy ? '正在关联…' : '加入所选系列' }}</button></template>
    </template>
    <p v-if="state.error" class="platform-alert danger" role="alert">{{ state.error }}</p>
    <p v-if="state.result" role="status">已加入 {{ state.target?.name }}，版本 {{ state.result.version }}。素材及文件未复制；不是模型生成记录。</p>
  </section>
</template>

<style scoped>
.reference-ingest{display:grid;gap:12px;padding:20px}.reference-ingest header,.reference-controls,.target-summary{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.reference-ingest header{justify-content:space-between}.reference-ingest h3,.reference-ingest p{margin:0}.reference-ingest p,.reference-ingest li{overflow-wrap:anywhere;font-size:13px}.reference-ingest label{display:grid;gap:6px;min-width:0}.reference-ingest input,.reference-ingest select{min-height:36px;max-width:100%}.reference-ingest ul{list-style:none;padding:0;margin:0}.reference-ingest li{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid var(--border)}.reference-preview{max-width:240px;max-height:180px;overflow:hidden}.reference-controls{align-items:end}.muted{color:var(--text-muted)}@media(max-width:640px){.reference-controls{display:grid;grid-template-columns:minmax(0,1fr)}.reference-ingest li{align-items:start;flex-direction:column}.reference-ingest{padding:12px}}
</style>
