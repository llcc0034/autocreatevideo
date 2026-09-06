<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Plus, RefreshCw, Trash2 } from 'lucide-vue-next'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import type { ModelDeployment, NodeMetadataCatalog } from '@/services/platformTypes'
import { nodeIdleObject, nodeIdleOverrideErrors, nodeIdleSeconds, validNodeIdleLimits, type NodeIdleLimits } from '@/services/modelNodeIdleOverrides'

const props = defineProps<{ modelValue?: unknown; providerId: string; deployments: ModelDeployment[]; limits?: NodeIdleLimits | null; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: Record<string, number>]; validity: [errors: string[]] }>()
type Row = { id: number; nodeType: string; minutes: string }
const rows = ref<Row[]>([])
let nextId = 0
let lastEmitted: string | undefined
watch(() => JSON.stringify(props.modelValue), value => {
  const ownUpdate = value === lastEmitted && lastEmitted !== undefined
  lastEmitted = undefined
  if (ownUpdate) return
  rows.value = nodeIdleObject(props.modelValue) ? Object.entries(props.modelValue).map(([nodeType, seconds]) => ({
    id: ++nextId, nodeType, minutes: typeof seconds === 'number' && Number.isFinite(seconds) ? String(seconds / 60) : '',
  })) : []
}, { immediate: true })
const malformed = computed(() => props.modelValue != null && !nodeIdleObject(props.modelValue))
const sourceId = ref('')
const loading = ref(false)
const loadError = ref('')
const catalog = ref<NodeMetadataCatalog | null>(null)
const search = ref('')
let request = 0
const sources = computed(() => props.deployments.filter(item => item.providerDriver === 'comfyui'
  && item.providerProfileId === props.providerId && item.status === 'active'))
const selectedSource = computed(() => sources.value.find(item => item.id === sourceId.value))
watch(() => [props.providerId, sourceId.value, selectedSource.value?.updatedAt, selectedSource.value?.id, props.disabled], () => {
  request++; loading.value = false; catalog.value = null; loadError.value = ''; search.value = ''
})
watch(() => props.providerId, () => { sourceId.value = '' })
onBeforeUnmount(() => { request++ })
async function loadCatalog() {
  if (props.disabled || loading.value || !selectedSource.value) return
  const ticket = ++request
  loading.value = true; catalog.value = null; loadError.value = ''
  try {
    const result = await platformApi.getAllNodeMetadata(selectedSource.value.id)
    if (ticket === request && !props.disabled) catalog.value = result
  } catch (error) { if (ticket === request) loadError.value = apiErrorSummary(error) }
  finally { if (ticket === request) loading.value = false }
}
const nodeTypes = computed(() => new Set(catalog.value?.items.map(item => item.classType) || []))
const options = computed(() => {
  const query = search.value.trim().toLocaleLowerCase()
  return (catalog.value?.items || []).filter(item => !query || `${item.classType} ${item.displayName} ${item.category}`.toLocaleLowerCase().includes(query))
})
const rowOptions = (row: Row) => (catalog.value?.items || []).filter(item => options.value.includes(item) || item.classType === row.nodeType)
const values = computed(() => Object.fromEntries(rows.value.map(row => [row.nodeType, nodeIdleSeconds(row.minutes)])))
const errors = computed(() => {
  if (malformed.value) return nodeIdleOverrideErrors(props.modelValue, props.limits)
  const result = nodeIdleOverrideErrors(values.value, props.limits)
  if (new Set(rows.value.map(row => row.nodeType.trim())).size !== rows.value.length) result.push('同一节点类型只能设置一条等待规则')
  return result
})
watch(errors, value => emit('validity', value), { immediate: true, deep: true })
const blocked = computed(() => props.disabled || malformed.value || !validNodeIdleLimits(props.limits))
function commit() {
  if (props.disabled || errors.value.length) return
  lastEmitted = JSON.stringify(values.value)
  emit('update:modelValue', values.value as Record<string, number>)
}
function add() {
  if (blocked.value || !catalog.value?.items.length || rows.value.length >= (props.limits?.maxOverrides || 0)) return
  rows.value.push({ id: ++nextId, nodeType: '', minutes: '' })
}
function changeType(id: number, type: string) {
  const row = rows.value.find(item => item.id === id)
  if (blocked.value || !row || !nodeTypes.value.has(type) || rows.value.some(item => item.id !== id && item.nodeType === type)) return
  row.nodeType = type; commit()
}
function changeMinutes(id: number, minutes: string) {
  const row = rows.value.find(item => item.id === id)
  if (blocked.value || !row) return
  row.minutes = minutes; commit()
}
function remove(id: number) {
  if (props.disabled || malformed.value) return
  rows.value = rows.value.filter(row => row.id !== id); commit()
}
</script>

<template>
  <div class="node-idle-editor">
    <p>通常无需设置。仅在确认某类 ComfyUI 节点正常计算、却因长时间没有有效进展反馈而提前超时时添加。不改变生成参数，也不能突破任务总超时。</p>
    <p v-if="!rows.length && !malformed" class="hint">未设置特殊规则，沿用生效的默认等待时限；工作流有更具体规则时优先使用。无需为此创建运行实例。</p>
    <p v-if="malformed" role="alert">现有节点等待规则格式异常，未自动清空；请在完整模型规格中修复 node_type_overrides。</p>
    <p v-if="!validNodeIdleLimits(limits)" role="alert">安全上限尚未读取，暂不能新增或修改时长；请重新读取系统设置。不设置规则不受影响。</p>
    <div class="catalog-source">
      <label>节点目录来源（仅供查询）<select v-model="sourceId" :disabled="disabled || !sources.length"><option value="">选择同服务提供方的 ComfyUI 实例</option><option v-for="item in sources" :key="item.id" :value="item.id">{{ item.name || item.key }}</option></select></label>
      <button type="button" class="button ghost small" :disabled="disabled || loading || !selectedSource" @click="loadCatalog"><RefreshCw :size="13"/>{{ loading ? '读取中…' : '读取节点目录' }}</button>
    </div>
    <p class="hint">只读取节点类型，不关联新模型，不加载模型文件或启动生成。</p>
    <p v-if="!sources.length" class="hint">当前服务提供方没有可查询的启用 ComfyUI 实例，可先保持不设置；已有规则仍保留。</p>
    <p v-if="loadError" role="alert">节点目录读取失败：{{ loadError }}。可重新读取；未替换任何已配置规则。</p>
    <label v-if="catalog">筛选节点类型<input v-model="search" type="search" :disabled="disabled" placeholder="搜索节点名称或类型"/><small>已读取 {{ catalog.totalCount }} 类，筛选匹配 {{ options.length }} 类。</small></label>
    <p v-if="catalog && !catalog.items.length" class="hint">目录中没有节点类型，暂不能新增规则。</p>
    <div v-for="row in rows" :key="row.id" class="idle-row">
      <label>节点类型<select :value="row.nodeType" :disabled="blocked || !catalog" @change="changeType(row.id, ($event.target as HTMLSelectElement).value)"><option value="">请选择节点类型</option><option v-if="row.nodeType && !nodeTypes.has(row.nodeType)" :value="row.nodeType">{{ row.nodeType }}（已有配置）</option><option v-for="item in rowOptions(row)" :key="item.classType" :value="item.classType" :disabled="rows.some(other => other.id !== row.id && other.nodeType === item.classType)">{{ item.displayName && item.displayName !== item.classType ? `${item.displayName} · ${item.classType}` : item.classType }}</option></select><small v-if="row.nodeType && !nodeTypes.has(row.nodeType)">{{ catalog ? '所选目录未找到此类型，原值保留；请核对目录来源。' : '尚未读取节点目录，原值保留。' }}</small></label>
      <label>等待时长（分钟）<input type="text" inputmode="decimal" :value="row.minutes" :disabled="blocked" @input="changeMinutes(row.id, ($event.target as HTMLInputElement).value)"/><small v-if="nodeIdleSeconds(row.minutes) !== null">将保存 {{ nodeIdleSeconds(row.minutes) }} 秒</small><small v-else>必填，须可换算为正整数秒。</small></label>
      <button type="button" class="button ghost small" :aria-label="`移除 ${row.nodeType || '未完成'} 等待规则`" :disabled="disabled || malformed" @click="remove(row.id)"><Trash2 :size="14"/>移除</button>
    </div>
    <button type="button" class="button ghost small add-rule" :disabled="blocked || !catalog?.items.length || rows.length >= (limits?.maxOverrides || 0)" @click="add"><Plus :size="13"/>添加等待规则</button>
    <small v-if="validNodeIdleLimits(limits)" class="hint">最多 {{ limits.maxOverrides }} 条，每条不超过 {{ limits.maxSeconds / 60 }} 分钟（{{ limits.maxSeconds }} 秒）。留空不设置规则；0 不是无限等待。</small>
    <ul v-if="errors.length" role="alert"><li v-for="error in errors" :key="error">{{ error }}</li></ul>
  </div>
</template>

<style scoped>
.node-idle-editor{display:grid;gap:12px;padding:0 12px 12px;color:var(--text);font-size:12px;line-height:1.6;min-width:0}.node-idle-editor p{margin:0}.node-idle-editor label{display:grid;gap:6px;min-width:0}.node-idle-editor input,.node-idle-editor select{width:100%;min-width:0;min-height:36px;padding:8px;border:1px solid var(--border-strong);border-radius:6px;background:var(--surface);color:var(--text)}.node-idle-editor small,.hint{color:var(--text-secondary)}.catalog-source{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:end;gap:10px}.idle-row{display:grid;grid-template-columns:minmax(0,2fr) minmax(120px,1fr) auto;align-items:start;gap:10px;border-top:1px solid var(--border);padding-top:12px}.idle-row>button{margin-top:25px}.add-rule{justify-self:start}.node-idle-editor [role=alert]{color:var(--accent);overflow-wrap:anywhere}.node-idle-editor :focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:760px){.catalog-source,.idle-row{grid-template-columns:minmax(0,1fr)}.idle-row>button{margin-top:0;justify-self:start}}
</style>
