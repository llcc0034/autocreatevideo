<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { candidateSelectionLabel, readStructuredTextDetail, structuredTextStep } from '@/services/productionStructuredText'
import { taskCenterTarget } from '@/services/taskCenter'
import type { GeneratedArtifact, ProductionRun, StageRun } from '@/services/platformTypes'
import StructuredJsonEditor from './StructuredJsonEditor.vue'
import PlatformStatusPill from './PlatformStatusPill.vue'
import { createArtifactSelectionCommand, type ArtifactSelectionConfirmed, type ArtifactSelectionProtection } from '@/services/artifactSelectionCommand'

const props = defineProps<{ run: ProductionRun; stage: StageRun; disabled?: boolean }>()
const emit = defineEmits<{ changed: [value: ArtifactSelectionConfirmed]; protection: [value: ArtifactSelectionProtection] }>()
const command = createArtifactSelectionCommand((...args) => platformApi.selectArtifact(...args),
  () => ({runId: props.run.id, scopeId: props.stage.id}), value => emit('protection', value), value => {
    stale.value = true; emit('changed', value)
  })
const selectionState = command.state
const submitting = computed(() => selectionState.submitting)
const context = computed(() => {
  try { return { step: structuredTextStep(props.run, props.stage), error: '' } }
  catch (cause) { return { step: null, error: apiErrorSummary(cause) } }
})
const rows = ref<GeneratedArtifact[]>([]), selected = ref<GeneratedArtifact | null>(null)
const detail = ref<ReturnType<typeof readStructuredTextDetail> | null>(null)
const page = ref(1), count = ref<number | null>(null), hasNext = ref(false), hasPrevious = ref(false)
const loading = ref(false), detailLoading = ref(false)
const error = ref(''), notice = ref(''), stale = ref(false)
let sequence = 0, detailSequence = 0
const locked = computed(() => Boolean(props.disabled || selectionState.pending) || submitting.value)
const canSelect = computed(() => !locked.value && !loading.value && !detailLoading.value && !stale.value && !error.value
  && !!context.value.step && !!detail.value && selected.value?.status === 'available'
  && !['succeeded', 'failed', 'cancelled', 'cancel_requested'].includes(props.run.status)
  && !['succeeded', 'failed', 'cancelled'].includes(props.stage.status))
function clearDetail() { ++detailSequence; selected.value = null; detail.value = null; detailLoading.value = false }
async function load(targetPage = 1) {
  if (submitting.value || selectionState.pending || loading.value || !context.value.step) return
  const token = ++sequence, runId = props.run.id, stageId = props.stage.id
  clearDetail(); error.value = ''; loading.value = true
  try {
    const result = await platformApi.listArtifactPage({ production_run: runId, stage_run: stageId, page: targetPage })
    if (token !== sequence) return
    if (result.results.some(row => row.productionRun !== runId || row.stageRun !== stageId || row.role !== 'structured_text_candidate')
      || new Set(result.results.map(row => row.id)).size !== result.results.length) throw new Error('候选清单与本步骤不一致，请重新读取')
    rows.value = result.results; count.value = result.count; page.value = targetPage
    hasNext.value = result.hasNext; hasPrevious.value = result.hasPrevious; stale.value = false
  } catch (cause) { if (token === sequence) { stale.value = true; error.value = apiErrorSummary(cause, '候选清单读取失败') } }
  finally { if (token === sequence) loading.value = false }
}
async function inspect(row: GeneratedArtifact) {
  if (locked.value || loading.value || stale.value) return
  clearDetail(); error.value = ''; notice.value = ''; detailLoading.value = true
  const token = detailSequence, runId = props.run.id, stageId = props.stage.id
  try {
    const result = await platformApi.getArtifact(row.id)
    if (token !== detailSequence) return
    if (result.id !== row.id || result.sha256 !== row.sha256 || result.manifestMetadataSha256 !== row.manifestMetadataSha256
      || result.status !== row.status) throw new Error('候选在读取期间已变化，请刷新清单')
    detail.value = readStructuredTextDetail(result, runId, stageId); selected.value = result
  } catch (cause) { if (token === detailSequence) error.value = apiErrorSummary(cause, '候选详情读取失败') }
  finally { if (token === detailSequence) detailLoading.value = false }
}
async function select() {
  if (!canSelect.value || !selected.value) return
  await command.submit({artifact_id: selected.value.id})
}
watch(() => selectionState.submitting, value => {
  if (!value && selectionState.notice) void load(page.value)
})
watch(() => JSON.stringify([props.run.id, props.stage.id, props.run.executionPlan?.sha256]), () => {
  command.reset()
  ++sequence; clearDetail(); loading.value = false; rows.value = []; count.value = null
  error.value = ''; notice.value = ''; stale.value = false; page.value = 1; hasNext.value = false; hasPrevious.value = false
  void load()
}, { immediate: true, flush: 'sync' })
watch(() => JSON.stringify([props.stage.status, props.stage.artifactSelectionDecisions]), () => {
  if (!submitting.value) void load(page.value)
})
onBeforeUnmount(() => { ++sequence; ++detailSequence; command.reset() })
</script>
<template>
  <section v-if="context.step || context.error" class="structured-text-results" aria-label="通用文本候选与结果" :aria-busy="loading || detailLoading || submitting">
    <header><h3>通用文本候选与结果</h3><button type="button" class="button ghost small" :disabled="locked || loading" @click="load(page)">刷新候选</button></header>
    <p v-if="context.error" role="alert">{{ context.error }}</p>
    <p v-if="selectionState.error" role="alert">{{selectionState.error}}</p>
    <p v-if="selectionState.notice" role="status">{{selectionState.notice}}</p>
    <div v-if="selectionState.pending" role="status">原选择请求尚未确认，暂停改选。<button class="button ghost small" :disabled="submitting" @click="command.submit()">核对原选择请求</button></div>
    <template v-else-if="context.step">
      <p>本步骤：{{context.step.name}} · 第 {{stage.ordinal}} 步。{{context.step.evaluator === null ? '按冻结配方由人工选择候选。' : '按冻结候选序号自动选择成功项，不是质量评分或人工审核。'}}</p>
      <p>查看不会生成或自动选择；本步骤不改写剧本、Bible 或其他业务内容。失败调用可能没有候选，实际返回仍可从原任务查看。</p>
      <RouterLink class="button ghost small" :to="taskCenterTarget({category:'production',runId:run.id,stageId:stage.id})">查看本步骤全部调用及原始返回（含失败）</RouterLink>
      <p v-if="error" role="alert">{{error}}</p><p v-if="stale">清单尚未刷新成功，已显示记录仅供参考，不能提交选择。</p>
      <p v-if="notice" role="status">{{notice}}</p><p v-if="loading" role="status">正在读取候选清单…</p>
      <p v-if="!loading && !error && !rows.length">本页暂无已归档候选，不代表模型没有返回内容；请查看原调用状态与实际返回。</p>
      <ul class="candidate-list"><li v-for="row in rows" :key="row.id"><div><strong>候选记录 {{row.variantIndex}}</strong><PlatformStatusPill :status="row.status"/><small>{{candidateSelectionLabel(stage,row)}}</small></div><button type="button" class="button ghost small" :disabled="locked || loading || stale" :aria-pressed="selected?.id===row.id" @click="inspect(row)">查看结果</button></li></ul>
      <nav aria-label="文本候选分页"><span>第 {{page}} 页 · {{count === null ? '总数未读取' : `共 ${count} 条候选`}}</span><button type="button" class="button ghost small" :disabled="locked || loading || !hasPrevious" @click="load(page-1)">上一页</button><button type="button" class="button ghost small" :disabled="locked || loading || !hasNext" @click="load(page+1)">下一页</button></nav>
      <p v-if="detailLoading" role="status">正在读取原输出结构与实际结果…</p>
      <section v-if="detail && selected" aria-label="实际解析结果">
        <p>{{candidateSelectionLabel(stage,selected)}}</p>
        <StructuredJsonEditor :model-value="detail.value" :schemas="[detail.schema]" path="response" label="解析后的实际内容" readonly/>
        <ul v-if="detail.defaults.length" aria-label="默认值来源"><li v-for="(item,index) in detail.defaults" :key="index">{{item.path}}：使用配置默认值 {{JSON.stringify(item.value)}}（不是模型返回）<details><summary>默认值来源</summary>结构版本 {{item.structureVersionId}}</details></li></ul>
        <ul v-if="detail.warnings.length" aria-label="解析提醒"><li v-for="(item,index) in detail.warnings" :key="index">{{item.path}}：{{item.message}}</li></ul>
        <button type="button" class="button primary small" :disabled="!canSelect" @click="select">{{submitting ? '正在选择…' : '选择此候选供后续步骤使用'}}</button>
        <RouterLink class="button ghost small" :to="taskCenterTarget({category:'production',kind:'preparation',id:detail.preparationId,runId:run.id,stageId:stage.id})">查看此候选的原始返回与解析诊断</RouterLink>
        <details><summary>实际解析值 JSON（包含显式默认值）</summary><pre>{{JSON.stringify(detail.value,null,2)}}</pre></details>
        <details><summary>冻结步骤、候选与输出合同（技术信息）</summary><p>步骤身份：{{context.step.key}} · 候选：{{selected.id}}</p><pre>{{JSON.stringify(detail.output,null,2)}}</pre></details>
      </section>
    </template>
  </section>
</template>
<style scoped>
.structured-text-results{display:grid;gap:12px;min-width:0;padding:16px;border:1px solid var(--border-color,#354350);border-radius:8px}.structured-text-results :is(header,nav){display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.structured-text-results :is(p,h3){margin:0}.structured-text-results :is(p,small,li,summary,pre){overflow-wrap:anywhere}.structured-text-results .button{min-height:36px;white-space:normal;width:fit-content;max-width:100%}.candidate-list{list-style:none;padding:0;margin:0;display:grid;gap:8px}.candidate-list li{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:12px 0;border-bottom:1px solid var(--border-color,#354350)}.candidate-list li>div{display:flex;flex-wrap:wrap;align-items:center;gap:8px;min-width:0}.candidate-list small{flex-basis:100%}.structured-text-results section{display:grid;gap:12px;min-width:0}.structured-text-results pre{white-space:pre-wrap;max-height:340px;overflow:auto}.structured-text-results [role=alert]{color:var(--warning,#e9aa52)}.structured-text-results :is(button,a,summary):focus-visible{outline:2px solid var(--accent-color,#e4ad60);outline-offset:2px}@media(max-width:600px){.structured-text-results{padding:10px}.candidate-list li{align-items:start;flex-direction:column}}
</style>
