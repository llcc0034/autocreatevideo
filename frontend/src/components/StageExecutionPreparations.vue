<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink, onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { taskCenterTarget } from '@/services/taskCenter'
import { executionPreparationApi } from '@/services/executionPreparationApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import type { ExecutionPreparationSummary } from '@/services/executionPreparation'
import type { ProductionRun } from '@/services/platformTypes'
import ExecutionPreparationWorkbench from './ExecutionPreparationWorkbench.vue'
import PlatformStatusPill from './PlatformStatusPill.vue'
import BaseModal from './BaseModal.vue'
import PreparationUnitHistory from './PreparationUnitHistory.vue'
import ExecutionFieldBulkReview from './ExecutionFieldBulkReview.vue'

const props = defineProps<{ stageId: string; runId?: string; manualRun?: ProductionRun }>()
const emit = defineEmits<{ changed: []; busy: [value: boolean] }>()
const page = ref(1), loading = ref(false), error = ref(''), selected = ref<ExecutionPreparationSummary | null>(null)
const result = ref<Awaited<ReturnType<typeof executionPreparationApi.groups>> | null>(null)
const expandedUnit = ref<string | null>(null)
const dirty = ref(false), busy = ref(false)
const kind = ref<'primary' | 'auxiliary'>('primary'), navigating = ref(false), navigationError = ref('')
const selectedLabel = ref('')
const checkedIds = ref<string[]>([]), bulkIds = ref<string[]>([]), bulkNotice = ref('')
const canBulkSelect = (item: ExecutionPreparationSummary) => item.unitKey.startsWith('shot:') && !!item.executionInstanceVersion
  && item.requestFrozenAt === null && item.status !== 'cancelled'
function togglePage() {
  const ids = result.value?.results.filter(group => canBulkSelect(group.latest)).map(group => group.latest.id) || []
  checkedIds.value = ids.every(id => checkedIds.value.includes(id)) ? [] : ids.slice(0, 50)
}
function bulkReviewed(count: number) {
  bulkIds.value = []; checkedIds.value = []; setBusy(false)
  bulkNotice.value = `已确认并采用 ${count} 个字段。后续执行遵循原运行授权与资源检查；可继续检查阶段。`
  changed()
}
let serial = 0, navigationSerial = 0
onBeforeUnmount(() => { serial++; navigationSerial++ })
const canLeave = () => !busy.value && ((!dirty.value && !bulkIds.value.length) || window.confirm('执行准备有尚未保存或未提交的内容，确定离开？'))
onBeforeRouteLeave(canLeave)
onBeforeRouteUpdate(canLeave)
function setBusy(value: boolean) { busy.value = value; emit('busy', value) }
async function load(targetPage = 1) {
  const current = ++serial, stage = props.stageId
  loading.value = true; error.value = ''; result.value = null; page.value = targetPage
  checkedIds.value = []
  try {
    const data = await executionPreparationApi.groups(stage, targetPage, kind.value)
    if (current === serial) result.value = data
  } catch (cause) { if (current === serial) error.value = apiErrorSummary(cause) }
  finally { if (current === serial) loading.value = false }
}
function close() {
  if (!canLeave()) return
  navigationSerial++; navigating.value = false; navigationError.value = ''
  selected.value = null; dirty.value = false; void load(page.value)
}
function select(item: ExecutionPreparationSummary, label: string) {
  if (!canLeave()) return
  navigationSerial++; navigating.value = false; navigationError.value = ''; dirty.value = false
  selected.value = item; selectedLabel.value = label
}
async function openParent(id: string) {
  if (!selected.value || !canLeave() || navigating.value) return
  const request = ++navigationSerial, stage = selected.value.stageRun
  navigating.value = true; navigationError.value = ''
  try {
    const parent = await executionPreparationApi.get(id)
    if (request !== navigationSerial) return
    if (parent.id !== id || parent.stageRun !== stage || parent.presentation?.kind !== 'primary'
        || !parent.executionInstanceVersion) throw new Error('主准备关联与当前阶段不一致，请重新读取')
    select(parent, parent.presentation.label)
  } catch (cause) { if (request === navigationSerial) navigationError.value = apiErrorSummary(cause) }
  finally { if (request === navigationSerial) navigating.value = false }
}
function switchKind(value: 'primary' | 'auxiliary') { expandedUnit.value = null; kind.value = value; void load() }
function changed() { void load(page.value); emit('changed') }
watch(() => props.stageId, () => { expandedUnit.value = null; void load() }, { immediate: true })
</script>

<template>
  <section class="platform-card stage-preparations">
    <header><div><strong>本阶段执行准备与字段确认</strong><p>按实际执行单元查看。字段生成、翻译、人工修改与确认均在同一份执行准备中处理；查看不会提交模型。</p></div>
      <RouterLink v-if="runId && stageId" class="button ghost small" :to="taskCenterTarget({ category: 'production', runId, stageId })">查看本阶段原调用</RouterLink>
      <button class="button ghost small" :disabled="loading || busy" @click="load(page)">刷新</button></header>
    <nav class="preparation-tabs" aria-label="执行准备分类">
      <button type="button" class="button ghost" :aria-pressed="kind === 'primary'" :disabled="busy || loading" @click="switchKind('primary')">主准备 · 正文审核</button>
      <button type="button" class="button ghost" :aria-pressed="kind === 'auxiliary'" :disabled="busy || loading" @click="switchKind('auxiliary')">提示词辅助任务</button>
    </nav>
    <p class="section-help">{{ kind === 'primary' ? '在镜头主准备中阅读生成正文、修改并明确采用；辅助生成成功不代表已审核。' : '这些调用负责生成或翻译输入字段，不是媒体生成结果。正文审核在对应主准备中进行。' }}</p>
    <p v-if="loading" role="status">正在读取执行准备…</p>
    <p v-else-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <template v-else-if="result">
      <div v-if="kind === 'primary' && result.count" class="bulk-toolbar">
        <button type="button" class="button ghost small" :disabled="busy" @click="togglePage">全选 / 清空本页可复核镜头</button>
        <span>已选 {{ checkedIds.length }} 镜（最多50镜；翻页清空选择）</span>
        <button type="button" class="button primary small" :disabled="busy || !checkedIds.length" @click="bulkIds = [...checkedIds]">批量确认并采用</button>
      </div>
      <p v-if="bulkNotice" role="status">{{ bulkNotice }}</p>
      <p v-if="!result.count">{{ kind === 'primary' ? '本阶段尚未创建主准备。请先通过当前生产阶段的正式操作开始准备。' : '本阶段尚无提示词辅助任务。' }}</p>
      <article v-for="group in result.results" :key="group.unitKey">
        <div class="preparation-label"><label v-if="kind === 'primary'" class="bulk-choice"><input v-model="checkedIds" type="checkbox" :value="group.latest.id"
          :disabled="busy || !canBulkSelect(group.latest) || (!checkedIds.includes(group.latest.id) && checkedIds.length >= 50)" :aria-label="`批量复核 ${group.label}`"/><strong>{{ group.label }}</strong></label><strong v-else>{{ group.label }}</strong>
          <p v-if="group.latest.presentation.task">最近一次调用：<PlatformStatusPill :status="group.latest.presentation.task.status"/> · 第 {{ group.latest.presentation.task.attemptNumber }} 次
            <span v-if="group.latest.presentation.task.errorCode"> · {{ group.latest.presentation.task.errorCode }}</span></p>
          <p>{{ group.latest.outcome.message }}</p>
          <p v-if="group.latest.reason">{{ group.latest.reason }}</p>
          <details><summary>最近一次请求与内部标识</summary><small>单元：{{ group.latest.unitKey }}<br/>请求：{{ group.latest.requestKey }} · 用途：{{ group.latest.purpose }}<br/>准备：{{ group.latest.id }}</small></details>
        </div>
        <div class="row-actions"><span>准备：<PlatformStatusPill :status="group.latest.status"/></span>
        <button type="button" class="button ghost small" :aria-expanded="expandedUnit === group.unitKey" :disabled="busy" @click="expandedUnit = expandedUnit === group.unitKey ? null : group.unitKey">{{ expandedUnit === group.unitKey ? '收起' : '执行版本 / 历史' }}（{{ group.versionCount }}）</button>
        <button class="button ghost small" :disabled="busy || !group.latest.executionInstanceVersion" @click="select(group.latest, group.latest.presentation.label)">{{ kind === 'auxiliary' ? '查看最新辅助任务' : '查看正文 / 审核字段' }}</button></div>
        <span v-if="!group.latest.executionInstanceVersion">尚未确定执行实例</span>
        <PreparationUnitHistory v-if="expandedUnit === group.unitKey" :key="`${stageId}:${kind}:${group.unitKey}`" :stage-id="stageId" :kind="kind" :unit-key="group.unitKey" :latest-id="group.latest.id" :busy="busy" @select="item => select(item, item.presentation.label)"/>
      </article>
      <footer v-if="result.count"><span>共 {{ result.count }} 个制作对象 · 第 {{ page }} 页</span>
        <button class="button ghost small" :disabled="!result.previous || busy" @click="load(page - 1)">上一页</button>
        <button class="button ghost small" :disabled="!result.next || busy" @click="load(page + 1)">下一页</button></footer>
    </template>
    <BaseModal :open="Boolean(selected?.executionInstanceVersion)" :title="selectedLabel" width="1100px" :dismissible="!busy" @close="close">
      <template v-if="selected?.executionInstanceVersion">
        <p v-if="navigating" role="status">正在读取对应主准备…</p>
        <p v-if="navigationError" class="platform-alert danger" role="alert">{{ navigationError }}</p>
        <p v-if="selected.stageRun !== stageId" role="status">当前生产阶段已变化；此处仍是已打开的原阶段准备，不会自动替换您的编辑内容。</p>
        <ExecutionPreparationWorkbench :key="selected.id" :preparation-id="selected.id" :version-id="selected.executionInstanceVersion" :purpose="selected.purpose"
          :manual-run="manualRun" allow-preparation-navigation @open-parent="openParent" @busy="setBusy" @dirty="dirty = $event" @changed="changed"/>
      </template>
    </BaseModal>
    <ExecutionFieldBulkReview v-if="bulkIds.length" :preparation-ids="bulkIds" @busy="setBusy"
      @close="bulkIds = []; setBusy(false)" @confirmed="bulkReviewed"/>
  </section>
</template>

<style scoped>
.stage-preparations { padding: 1rem; }
.bulk-toolbar,.bulk-choice{display:flex;flex-wrap:wrap;align-items:center;gap:8px;min-height:36px}.bulk-toolbar{margin:12px 0}.bulk-choice input{width:18px;height:18px;flex:none}
header, article, footer { display: flex; align-items: center; gap: .75rem; }
header { flex-wrap: wrap; }
header > div, article > div { flex: 1; min-width: 0; }
header > div { flex-basis: 260px; }
small { display: block; overflow-wrap: anywhere; }
p { margin: .5rem 0; }
article { flex-wrap: wrap; padding: .75rem 0; border-bottom: 1px solid var(--border-color, #29343b); }
article > .preparation-label { flex: 1 1 260px; overflow-wrap: anywhere; }
article > .row-actions { flex: 0 1 auto; display: flex; flex-wrap: wrap; align-items: center; gap: .75rem; }
.preparation-tabs { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .75rem; }
.preparation-tabs [aria-pressed=true] { border-color: var(--accent); color: var(--accent); }
.section-help, details { color: var(--text-secondary); font-size: 13px; }
summary { cursor: pointer; }
@media(max-width:640px) { header { align-items: flex-start; } footer { flex-wrap: wrap; } article > .row-actions { flex-basis: 100%; justify-content: space-between; } }
footer { justify-content: flex-end; padding-top: .75rem; }
</style>
