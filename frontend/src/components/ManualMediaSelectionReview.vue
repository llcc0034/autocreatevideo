<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { buildManualMediaReview, readManualMediaReview, type ManualMediaReview } from '@/services/productionManualMedia'
import { manualScopeLabels as scopeLabels, type ManualActionOptions } from '@/services/productionManual'
import type { JsonObject, ProductionRun } from '@/services/platformTypes'

const props = defineProps<{ run: ProductionRun; options: ManualActionOptions }>()
const emit = defineEmits<{ changed: []; busy: [value: boolean] }>()
const detail = ref<ManualMediaReview | null>(null), loading = ref(false), submitting = ref(false), error = ref(''), notice = ref('')
const checked = ref(false), comment = ref(''), pending = ref<JsonObject | null>(null), key = ref(crypto.randomUUID())
const labels = { not_reviewed: '尚未审核', current: '选片、交付分镜与内容依据均未变化', changed: '内容依据或选片已变化，需要重新核对是否复用' }
let sequence = 0
async function load() {
  if (submitting.value) return
  const token = ++sequence; loading.value = true; error.value = ''; detail.value = null; checked.value = false
  try {
    const raw = await platformApi.getManualMediaReview(props.run.id)
    if (token !== sequence) return
    detail.value = readManualMediaReview(raw, props.run)
    pending.value = null; key.value = crypto.randomUUID()
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) loading.value = false }
}
async function approve() {
  if (loading.value || submitting.value || ['succeeded','failed','cancelled'].includes(props.run.status)
      || (!pending.value && (!detail.value || !checked.value))) return
  submitting.value = true; error.value = ''; notice.value = ''; emit('busy', true)
  const token = sequence
  try {
    pending.value ??= buildManualMediaReview(detail.value!, props.run, props.options, key.value, comment.value)
    const result = await platformApi.performManualProductionAction(props.run.id, pending.value)
    if (result.action !== 'approve' || result.result_kind !== 'approval_decision' || typeof result.target_id !== 'string'
        || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(result.target_id)) throw new Error('未返回正式审核记录，请重试同一请求核对结果')
    if (token !== sequence) return
    pending.value = null; checked.value = false; notice.value = '已记录本次选片审核；未自动开始装配或其他模型调用。'
    emit('changed')
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { submitting.value = false; emit('busy', false) }
  if (notice.value) await load()
}
watch(() => props.run.id, () => { pending.value = null; notice.value = ''; void load() }, { immediate: true })
onBeforeUnmount(() => { sequence++ })
</script>

<template>
  <section class="manual-selection-review">
    <header><h4>审核本轮已采用媒体</h4><button class="button ghost small" :disabled="loading || submitting" @click="load">重新读取完整选片</button></header>
    <p>先查看并采用媒体，再结合本轮剧本、人物场景设定、影视规则和分镜核对是否适合交付。内容或上游审核修订变化后，需要明确重新审核；不适合复用的媒体请返回媒体制作创建新候选。</p>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="loading" role="status">正在读取完整选片与审核记录…</p>
    <template v-if="detail">
      <p :class="{ 'platform-alert warning': detail.approval.status === 'changed' }">{{ labels[detail.approval.status] }} · {{ detail.items.length }} 项 · 审核修订 {{ detail.approval.revision }}</p>
      <p v-if="detail.approval.changes.length" role="status">已变化：{{ detail.approval.changes.join('、') }}。原生成输入保留不变；重新确认表示你已检查旧结果仍适合当前内容，不代表重新运行过模型。</p>
      <p v-if="!detail.items.length">尚未采用媒体；不能批准空集合。</p>
      <ul v-else aria-label="本次审核的完整选片"><li v-for="item in detail.items" :key="item.id"><strong>{{ scopeLabels[item.scopeKind] }} {{ item.scopeId }}</strong><span>{{ item.kind }} · 候选 {{ item.id }} · 采用修订 {{ item.revision }}</span></li></ul>
      <section aria-label="本次审核的交付分镜">
        <h4>交付分镜 · 按装配顺序</h4>
        <p v-if="!detail.timeline.length">当前交付范围尚无剧集；采用媒体不代表已经具备装配条件。</p>
        <div v-for="episode in detail.timeline" :key="episode.id">
          <strong>第 {{ episode.number }} 集 · {{ episode.title || '未命名' }}</strong>
          <p v-if="!episode.shots.length">尚无镜头，需补齐后重新审核。</p>
          <ol><li v-for="shot in episode.shots" :key="shot.id">镜头 {{ shot.number }} · {{ shot.title || '未命名' }} · {{ shot.mediaKind || '未配置媒体类型' }} · {{ shot.durationMs !== null && shot.durationMs > 0 ? `${shot.durationMs / 1000} 秒` : '未配置有效时长' }}<span v-if="shot.episodeStartMs !== null && shot.episodeEndMs !== null"> · 本集 {{ shot.episodeStartMs / 1000 }}–{{ shot.episodeEndMs / 1000 }} 秒</span><span v-else> · 按时长顺序排列</span></li></ol>
        </div>
      </section>
      <p>此处确认选片是否适合当前交付分镜；不会重写原提示词、输入素材或生成记录，也不会自动重生成。</p>
      <label><input v-model="checked" type="checkbox" :disabled="submitting || Boolean(pending) || !detail.items.length">我已结合当前剧本、设定、影视规则与分镜，确认上述候选仍适合本次交付</label>
      <label>审核说明（可选）<textarea v-model="comment" rows="2" :disabled="submitting || Boolean(pending)"/></label>
      <button class="button primary small" :disabled="loading || submitting || ['succeeded','failed','cancelled'].includes(run.status) || (!pending && (!checked || !detail.items.length))" @click="approve">{{ submitting ? '正在提交审核…' : pending ? '重试同一审核请求' : '确认本次选片审核' }}</button>
    </template>
  </section>
</template>

<style scoped>
.manual-selection-review{display:grid;gap:12px}.manual-selection-review header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.manual-selection-review h4,.manual-selection-review p{margin:0}.manual-selection-review ul{padding-left:20px;max-height:420px;overflow:auto}.manual-selection-review li,.manual-selection-review label{display:grid;gap:6px}.manual-selection-review li{padding:8px 0;overflow-wrap:anywhere}.manual-selection-review label:has(input){display:flex;align-items:center}.manual-selection-review textarea{width:100%}
</style>
