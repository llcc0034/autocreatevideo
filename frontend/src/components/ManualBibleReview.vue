<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import BibleContentReview from './BibleContentReview.vue'
import { buildManualBibleAction, readManualBibleReview, type ManualBibleReview } from '@/services/productionManualBible'
import type { ManualActionOptions } from '@/services/productionManual'
import type { JsonObject, ProductionRun } from '@/services/platformTypes'

const props = defineProps<{ run: ProductionRun; options: ManualActionOptions }>()
const emit = defineEmits<{ changed: []; busy: [value: boolean] }>()
const detail = ref<ManualBibleReview | null>(null), loading = ref(false), submitting = ref(false), error = ref(''), notice = ref('')
const checked = ref<string[]>([]), comment = ref(''), pending = ref<JsonObject | null>(null), key = ref(crypto.randomUUID())
const reviewedProjectBible = ref(false), checkedSettings = ref<string[]>([])
const labels = { not_reviewed: '尚未审核写入', current: '与已审核内容一致', changed: '来源或内容已变化，需要重新审核' }
const terminal = computed(() => ['succeeded','failed','cancelled'].includes(props.run.status))
const approvable = computed(() => detail.value?.source.status === 'current' && detail.value.settings.every(row => checkedSettings.value.includes(row.review_key))
  && reviewedProjectBible.value && !detail.value.candidates.some(row => row.conflict_code) && detail.value.candidates.every(row => checked.value.includes(row.id)))
let sequence = 0
async function load() {
  if (submitting.value) return
  const token = ++sequence; loading.value = true; error.value = ''; detail.value = null; checked.value = []
  reviewedProjectBible.value = false; checkedSettings.value = []
  try {
    const raw = await platformApi.getBibleReview(props.run.id)
    if (token !== sequence) return
    detail.value = readManualBibleReview(raw, props.run)
    pending.value = null; key.value = crypto.randomUUID()
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) loading.value = false }
}
async function submit(action: 'adopt' | 'approve') {
  if (loading.value || submitting.value || terminal.value || !detail.value
    || (pending.value && pending.value.action !== action) || (!pending.value && action === 'approve' && !approvable.value)) return
  submitting.value = true; error.value = ''; notice.value = ''; emit('busy', true)
  const token = sequence
  try {
    pending.value ??= buildManualBibleAction(detail.value, props.run, props.options, action, key.value, checked.value, comment.value, reviewedProjectBible.value, checkedSettings.value)
    const result = await platformApi.performManualProductionAction(props.run.id, pending.value)
    if (result.action !== action || result.result_kind !== 'approval_decision' || typeof result.target_id !== 'string'
      || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(result.target_id)) throw new Error('未返回正式人工决策，请重试同一请求核对结果')
    if (token !== sequence) return
    pending.value = null
    notice.value = action === 'adopt' ? '已解析所选剧本的候选，请逐项审核；尚未写入人物、场景或道具。' : '已按审核创建或复用条目；未自动生成图片或执行下一步骤。'
    emit('changed')
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { submitting.value = false; emit('busy', false) }
  if (notice.value) await load()
}
watch(() => props.run.id, () => { pending.value = null; notice.value = ''; void load() }, { immediate: true })
onBeforeUnmount(() => { sequence++ })
</script>

<template>
  <section class="manual-bible-review">
    <header><h4>人物、场景与道具 · 来源和审核</h4><button class="button ghost small" :disabled="loading || submitting" @click="load">重新读取来源和完整候选</button></header>
    <p>先明确采用本轮剧本中的声明，再逐项核对。解析候选不调用模型，也不会自动创建条目；审核后才写入。</p>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="loading" role="status">正在读取 Bible 来源和审核记录…</p>
    <template v-if="detail">
      <p>{{ detail.current_script ? `当前剧本：修订 ${detail.current_script.revision} · ${String(detail.current_script.id).slice(0,8)}` : '尚未采用剧本，请先通过剧本输入或剧本候选审核明确采用。' }}</p>
      <p v-if="detail.source.status === 'changed'" class="platform-alert warning">下面是先前来源的候选，已不匹配当前剧本；不能直接审核。</p>
      <button class="button ghost small" :disabled="loading || submitting || terminal || !detail.current_script || (!!pending && pending.action !== 'adopt')" @click="submit('adopt')">{{ pending?.action === 'adopt' ? '重试同一来源采用请求' : '采用当前剧本声明并解析候选' }}</button>
      <p>{{ labels[detail.approval.status as keyof typeof labels] }} · {{ detail.candidates.length }} 项 · 审核修订 {{ detail.approval.revision }}</p>
      <BibleContentReview :content="detail" v-model:candidates="checked" v-model:settings="checkedSettings" v-model:project="reviewedProjectBible"
        :disabled="loading || submitting || terminal || Boolean(pending) || detail.source.status !== 'current'" />
      <label>操作说明（可选）<textarea v-model="comment" rows="2" :disabled="submitting || terminal || Boolean(pending)"/></label>
      <button class="button primary small" :disabled="loading || submitting || terminal || (pending ? pending.action !== 'approve' : !approvable)" @click="submit('approve')">{{ submitting ? '正在提交…' : pending?.action === 'approve' ? '重试同一审核请求' : '确认已核对条目并写入' }}</button>
    </template>
  </section>
</template>

<style scoped>
.manual-bible-review{display:grid;gap:12px}.manual-bible-review header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.manual-bible-review h4,.manual-bible-review h5,.manual-bible-review p{margin:0}.manual-bible-review ul{list-style:none;padding:0;display:grid;gap:12px}.manual-bible-review li{padding:14px;border:1px solid var(--border);border-radius:8px;display:grid;gap:8px;overflow-wrap:anywhere}.manual-bible-review label{display:grid;gap:6px}.manual-bible-review label:has(input){display:flex;align-items:center}.manual-bible-review textarea{width:100%}.manual-bible-review .content{white-space:pre-wrap}.manual-bible-review .existing{padding:10px;border-left:2px solid var(--border);display:grid;gap:6px}
</style>
