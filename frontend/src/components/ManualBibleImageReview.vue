<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Asset, BibleImageLatestCandidate } from '@/types'
import type { JsonObject, ProductionRun } from '@/services/platformTypes'
import type { ManualActionOptions } from '@/services/productionManual'
import type { BibleImageReviewSelection } from '@/services/bibleImageSelections'
import { readManualBibleImageReview, buildManualBibleImageApproval, type ManualBibleImageReview } from '@/services/productionManualBibleImages'
import { platformApi } from '@/services/platformApi'
import { repository } from '@/services/repository'
import { apiErrorPresentation, apiErrorSummary } from '@/services/apiErrorPresentation'
import { isDefinitiveCreationRejection } from '@/services/productionOnboarding'
import BibleImageSelectionEditor from './BibleImageSelectionEditor.vue'
import BaseModal from './BaseModal.vue'

const props = defineProps<{ run: ProductionRun; options: ManualActionOptions; open: boolean }>()
const emit = defineEmits<{ busy: [value: boolean]; changed: []; close: []; retained: [value: { dirty: boolean; pending: boolean }] }>()
const detail = ref<ManualBibleImageReview | null>(null), assets = ref<Asset[]>([])
const history = ref<Record<string, BibleImageLatestCandidate>>({}), selections = ref<Record<string, BibleImageReviewSelection>>({})
const loading = ref(false), submitting = ref(false), error = ref(''), notice = ref(''), comment = ref('')
const pending = ref<JsonObject | null>(null), key = ref(crypto.randomUUID())
const current = ref(false)
const readAt = ref(''), reopened = ref(false)
const dirty = computed(() => Boolean(comment.value.trim()) || Object.values(selections.value).some(row =>
  row.selectedArtifactIds.length || row.selectedAssetIds.length || row.anchorArtifactId || row.anchorAssetId))
watch([dirty, () => Boolean(pending.value)], ([hasDraft, hasPending]) => emit('retained', { dirty: hasDraft, pending: hasPending }), { immediate: true, flush: 'sync' })
watch(() => props.open, (open, previous) => { if (open && previous === false && readAt.value) reopened.value = true })
function close() { if (!loading.value && !submitting.value) emit('close') }
function beforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value && !pending.value && !submitting.value) return
  event.preventDefault(); event.returnValue = ''
}
onMounted(() => { if (typeof window !== 'undefined' && window.addEventListener) window.addEventListener('beforeunload', beforeUnload) })
const terminal = computed(() => ['succeeded','failed','cancelled'].includes(props.run.status))
const stateLabels = { not_reviewed: '尚未审核', current: '审核依据仍一致', changed: '内容依据、候选或实体关系已变化，请重新审核' }
let sequence = 0
async function load() {
  if (submitting.value || pending.value) return
  const token = ++sequence
  const run = props.run
  loading.value = true; current.value = false; error.value = ''; emit('busy', true)
  try {
    const [raw, candidates, library] = await Promise.all([
      platformApi.getManualBibleImageReview(run.id), repository.getBibleImageCandidates(run.project), repository.getAssets(),
    ])
    if (token !== sequence) return
    const verified = readManualBibleImageReview(raw, run)
    const previews = Object.fromEntries(candidates.candidates.flatMap(entity => entity.candidateHistory).map(row => [row.artifactId, row]))
    detail.value = verified
    assets.value = library
    history.value = previews
    selections.value = {}; comment.value = ''; key.value = crypto.randomUUID(); current.value = true
    readAt.value = new Date().toLocaleString(); reopened.value = false
  } catch (cause) { if (token === sequence) error.value = apiErrorSummary(cause) }
  finally { if (token === sequence) { loading.value = false; emit('busy', false) } }
}
async function refresh() {
  if (loading.value || submitting.value || pending.value) return
  if (dirty.value
    && !window.confirm('重新读取将清空尚未提交的参考图选择和审核说明，是否继续？')) return
  notice.value = ''
  await load()
}
async function submit() {
  if (submitting.value || loading.value || !current.value || !detail.value || terminal.value) return
  error.value = ''; notice.value = ''
  const token = sequence, retrying = Boolean(pending.value)
  try {
    pending.value ??= buildManualBibleImageApproval(detail.value, props.run, props.options, selections.value, assets.value, history.value, key.value, comment.value)
    submitting.value = true; emit('busy', true)
    const result = await platformApi.performManualProductionAction(props.run.id, pending.value)
    if (token !== sequence) return
    if (result.action !== 'approve' || result.result_kind !== 'approval_decision' || typeof result.target_id !== 'string'
      || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(result.target_id)) throw new Error('未返回正式人工决策，请重试同一请求核对结果')
    pending.value = null
    selections.value = {}; comment.value = ''
    notice.value = '已按选择写入主图与参考图；未自动生成图片、推进下一步骤或开始装配。'
    emit('changed')
  } catch (cause) {
    if (token === sequence) {
      error.value = apiErrorSummary(cause)
      const selectionConflict = (cause as { response?: { status?: number } })?.response?.status === 409
        && apiErrorPresentation(cause).code === 'manual_selection_conflict'
      // Only an initial, explicit rejection proves this command did not commit.
      // A failed reconciliation must never erase an earlier unknown outcome.
      if (!retrying && pending.value && (isDefinitiveCreationRejection(cause) || selectionConflict)) {
        pending.value = null; key.value = crypto.randomUUID(); current.value = false
      }
    }
  }
  finally { if (token === sequence) { submitting.value = false; emit('busy', false) } }
  if (token === sequence && notice.value) await load()
}
watch([() => props.run.id, () => props.run.project, () => props.run.executionPlan?.sha256], () => {
  // Invalidate even while the old context is submitting. Its late completion
  // must not unlock, refresh, or announce success in the new context.
  sequence++
  pending.value = null; notice.value = ''; error.value = ''; current.value = false
  loading.value = false; submitting.value = false; detail.value = null; assets.value = []; history.value = {}
  selections.value = {}; comment.value = ''; key.value = crypto.randomUUID()
  readAt.value = ''; reopened.value = false
  void load()
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => {
  sequence++
  if (typeof window !== 'undefined' && window.removeEventListener) window.removeEventListener('beforeunload', beforeUnload)
})
</script>
<template>
  <BaseModal :open="open" title="人工制作 · 参考图与主图审核" width="1100px" :dismissible="!loading && !submitting" @close="close">
  <section class="manual-image-review">
    <header><h4>逐项选择参考图与唯一主图</h4><button class="button ghost small" :disabled="loading || submitting || Boolean(pending)" @click="refresh">重新读取候选与当前关系</button></header>
    <p>可以混选生成候选与素材库图片，也可以只用素材库图片，不要求先调用模型。每个实体必须指定唯一主图。</p>
    <p>保留仍有效的已绑参考图；更换主图时，原主图保留为参考图。描述已变化的旧生成图片不会被保留。提交前请确认所选图片符合当前剧本、人物/场景/道具设定及影视规则；不适合时返回生成步骤创建新候选。</p>
    <p>后续步骤已有工作时，请先查看“改选影响”；旧生成输入不改写，也不会自动重跑。</p>
    <p>关闭此面板会在当前制作页保留选择、说明及待核对请求；离开或刷新浏览器不会持久保存。尚未提交的选择不是采用决定。</p>
    <p v-if="readAt">审核依据读取于 {{ readAt }}；提交时服务端仍复验当前权限、来源及修订。</p>
    <p v-if="reopened" class="platform-alert warning" role="status">已恢复当前页面的审核状态，仍沿用上次读取依据；没有重新读取或自动提交。需要更新候选时请主动重新读取。</p>
      <RouterLink :to="{name:'production-run-detail',params:{projectId:run.project,runId:run.id},query:{step:'bible'}}">管理本项目人物、场景与道具</RouterLink>
    <p v-if="loading" role="status">正在核对候选、当前图片关系与素材库…</p>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="pending && !submitting" class="platform-alert warning" role="status">本次审核结果尚未确认，候选刷新已暂停；请重试同一审核请求核对原结果，不要另建请求。重试会重新提交原请求，不是只读查询。请求标识 {{ pending.idempotency_key }}</p>
    <p v-if="detail && !current && !loading" class="platform-alert warning" role="status">候选依据尚未完成核对，以下保留上次结果和未提交选择，仅供核对；重新读取成功后才能审核写入。</p>
    <template v-if="detail">
      <p>{{ stateLabels[detail.approval.status] }} · 审核修订 {{ detail.approval.revision }} · 每个实体本次最多选 {{ detail.maximum_selected_images }} 张</p>
      <p v-if="detail.approval.changes.length" role="status">变化项：{{ detail.approval.changes.join('、') }}</p>
      <details><summary>本次核对的实体设定与当前图片关系</summary><article v-for="target in detail.targets" :key="target.scope.id"><strong>{{ target.name }}</strong><p>{{ target.description }}</p><p>{{ target.primary_setting ? `主图设定修订 v${target.primary_setting.revision}（文字依据，不代表图片已就绪）` : '尚未建立主图设定' }}</p><p>当前主图：{{ target.anchor_asset_id ?? '未绑定' }}；参考图：{{ target.reference_asset_ids.length }} 张</p><code>{{ target.reference_asset_ids.join(' / ') }}</code></article></details>
      <BibleImageSelectionEditor v-model="selections" :key="key" :entities="detail.review.entities" :assets="assets" :history="history" :disabled="loading || !current || submitting || terminal || Boolean(pending)"/>
      <label>审核说明（可选）<textarea v-model="comment" rows="2" :disabled="loading || !current || submitting || terminal || Boolean(pending)"/></label>
      <button class="button primary small" :disabled="loading || !current || submitting || terminal" @click="submit">{{ submitting ? '正在写入…' : pending ? '重试同一审核请求' : '确认所选参考图与主图并写入' }}</button>
    </template>
  </section>
  </BaseModal>
</template>
<style scoped>
.manual-image-review{display:grid;gap:12px}.manual-image-review header{display:flex;gap:12px;justify-content:space-between;flex-wrap:wrap}.manual-image-review h4,.manual-image-review p{margin:0}.manual-image-review article{display:grid;gap:6px;padding:10px;overflow-wrap:anywhere}.manual-image-review label{display:grid;gap:6px}
</style>
