<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { isDefinitiveCreationRejection } from '@/services/productionOnboarding'
import { taskCenterTarget } from '@/services/taskCenter'
import type { JsonObject, ProductionRun } from '@/services/platformTypes'
import type { ManualActionOptions } from '@/services/productionManual'
import { buildManualAssembly, readManualAssembly, type ManualAssemblyDetail } from '@/services/productionManualAssembly'
import PlatformStatusPill from './PlatformStatusPill.vue'
import RecipeAssemblyOutputEditor from './RecipeAssemblyOutputEditor.vue'

const props = defineProps<{ run: ProductionRun; options: ManualActionOptions }>()
const emit = defineEmits<{ changed: []; busy: [value: boolean]; pending: [value: boolean] }>()
const detail = ref<ManualAssemblyDetail | null>(null), loading = ref(false), submitting = ref(false)
const error = ref(''), notice = ref(''), checked = ref(false)
const pending = ref<{ episodeId: string; body: JsonObject } | null>(null)
const detailFresh = ref(false)
watch(() => Boolean(pending.value), value => emit('pending', value), {flush:'sync'})
function beforeUnload(event: BeforeUnloadEvent) {
  if (!pending.value && !submitting.value) return
  event.preventDefault(); event.returnValue = ''
}
onMounted(() => { if (typeof window !== 'undefined' && window.addEventListener) window.addEventListener('beforeunload', beforeUnload) })
const confirmation = ref<{ episodeId: string; runId: string; sequence: number } | null>(null)
const rereadConfirmation = ref(false)
const authors = ref<JsonObject>({}), authorErrors = ref<Record<string, string[]>>({})
const authorInvalid = computed(() => Object.values(authorErrors.value).some(rows => rows.length))
const previewReady = computed(() => detailFresh.value && Boolean(detail.value) && JSON.stringify(authors.value) === JSON.stringify(detail.value?.authors)
  && !authorInvalid.value)
let sequence = 0
function setAuthor(key: string, value: JsonObject | undefined) {
  if (loading.value || submitting.value || pending.value || detail.value?.frozen) return
  const next = {...authors.value}
  if (value === undefined) delete next[key]; else next[key] = value
  authors.value = next; sequence++; checked.value = false; confirmation.value = null
  error.value = ''; notice.value = ''
}
async function preview() {
  if (!detail.value || detail.value.frozen || pending.value || submitting.value || loading.value || authorInvalid.value) return
  const token = ++sequence; loading.value = true; checked.value = false; confirmation.value = null; error.value = ''
  const draft = JSON.parse(JSON.stringify(authors.value))
  try {
    const raw = await platformApi.previewManualAssembly(props.run.id, detail.value.runPlanSha256, draft)
    if (token !== sequence) return
    detail.value = readManualAssembly(raw, props.run)
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) loading.value = false }
}
async function load() {
  if (submitting.value) return
  const token = ++sequence; loading.value = true; error.value = ''; detailFresh.value = false; checked.value = false
  confirmation.value = null; rereadConfirmation.value = false
  try {
    const value = await platformApi.getManualAssembly(props.run.id)
    if (token !== sequence) return
    detail.value = readManualAssembly(value, props.run)
    detailFresh.value = true
    if (detail.value.frozen) { authors.value = JSON.parse(JSON.stringify(detail.value.authors)); authorErrors.value = {} }
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) loading.value = false }
}
async function submit(episodeId: string, confirmed = false) {
  if (submitting.value || loading.value || (!pending.value && (!detail.value || !checked.value))) return
  if (!pending.value && !previewReady.value) return
  if (!pending.value && ['succeeded', 'failed', 'cancelled'].includes(props.run.status)) return
  const episode = detail.value?.episodes.find(row => row.id === episodeId)
  if ((!pending.value && !episode) || (pending.value && pending.value.episodeId !== episodeId)) return
  const token = sequence
  error.value = ''; notice.value = ''
  if (!pending.value) {
    if (!detail.value || !episode || detail.value.blockers.length || (episode.job && episode.job.status !== 'failed')) return
    if (!confirmed) {
      confirmation.value = { episodeId, runId: props.run.id, sequence }
      return
    }
    if (confirmation.value?.episodeId !== episodeId || confirmation.value.runId !== props.run.id
      || confirmation.value.sequence !== sequence) return
  }
  confirmation.value = null; rereadConfirmation.value = false
  const recovering = Boolean(pending.value)
  submitting.value = true; emit('busy', true)
  try {
    if (!pending.value) pending.value = { episodeId, body: buildManualAssembly(detail.value!, episodeId, props.run, props.options, crypto.randomUUID()) }
    const result = await platformApi.performManualProductionAction(props.run.id, pending.value.body)
    if (result.action !== 'assemble' || result.result_kind !== 'assembly_link' || typeof result.target_id !== 'string'
        || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(result.target_id)) throw new Error('未返回正式装配关联，请重试同一请求核对结果')
    if (token !== sequence) return
    pending.value = null; checked.value = false; notice.value = '已提交装配；请查看下面的后台任务状态。尚未批准交付。'
    emit('changed')
  } catch (e) { if (token === sequence) {
    error.value = apiErrorSummary(e)
    // A rejection now cannot disprove execution of an earlier lost response.
    if (!recovering && isDefinitiveCreationRejection(e)) { pending.value = null; checked.value = false; detailFresh.value = false }
  } }
  finally { submitting.value = false; emit('busy', false) }
  if (notice.value) await load()
}
async function reread(confirmed = false) {
  if (submitting.value || loading.value) return
  if (pending.value && !confirmed) { rereadConfirmation.value = true; return }
  if (pending.value && !rereadConfirmation.value) return
  await load()
  // A job inventory is not a command receipt. Keep the original request until
  // its own idempotent endpoint confirms that command.
}
watch(() => props.run.id, () => { detail.value = null; authors.value = {}; authorErrors.value = {}; pending.value = null; confirmation.value = null; rereadConfirmation.value = false; notice.value = ''; void load() }, { immediate: true })
onBeforeUnmount(() => {
  sequence++
  if (typeof window !== 'undefined' && window.removeEventListener) window.removeEventListener('beforeunload', beforeUnload)
})
</script>

<template>
  <section class="manual-assembly">
    <header><h4>按已审核选片装配</h4><button class="button ghost small" :disabled="loading || submitting" @click="reread()">重新读取选片与任务</button></header>
    <p>每集单独发起，使用现有装配队列；完成后仍须明确交付。已提交装配的轮次不能换片或重剪。</p>
    <p v-if="loading" role="status">正在读取完整选片、输出配置与装配任务…</p>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}<span v-if="detail && !detailFresh"> · 保留上次读取结果，尚未刷新，不能据此创建新请求。</span></p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <section v-if="pending" class="platform-alert warning" aria-label="装配请求结果未知">
      <p>装配请求尚未取得确定回执，可能已入队；保留原请求键和正文，不创建新的装配意图。</p>
      <button class="button primary small" :disabled="loading || submitting" @click="submit(pending.episodeId)">{{submitting ? '正在核对…' : '重试同一请求'}}</button>
    </section>
    <section v-if="rereadConfirmation" class="platform-alert warning" role="region" aria-label="重新核对装配请求">
      <p>只读取当前选片与任务，不会放弃原请求，也不代表已确认该请求结果。之后仍须核对原请求回执。</p>
      <button class="button ghost small" :disabled="submitting || loading" @click="rereadConfirmation = false">保留原请求</button>
      <button class="button secondary small" :disabled="submitting || loading" @click="reread(true)">确认重新核对任务</button>
    </section>
    <template v-if="detail">
      <RouterLink v-if="!pending" :to="taskCenterTarget({category:'production',runId:run.id,projectId:run.project,stageId:run.stages?.find(row=>row.stageKey==='assembly')?.id})">查看本轮装配历史</RouterLink>
      <details><summary>{{detail.frozen ? '查看已冻结装配字段映射' : '配置可选装配字段映射'}}</summary>
        <RecipeAssemblyOutputEditor v-for="key in (['assembly_output','assembly_timing','assembly_media'] as const)" :key="key"
          :model-value="authors[key] as JsonObject | undefined" :schema="detail.authorSchemas[key] as JsonObject"
          :purpose="key==='assembly_media'?'media':key==='assembly_timing'?'timing':'output'" :disabled="detail.frozen || submitting || loading || Boolean(pending)"
          :frozen-contract="detail.frozen ? detail.authorContracts[key] as JsonObject | undefined : undefined"
          @update:model-value="setAuthor(key,$event)" @validity="authorErrors[key]=$event"/>
        <button v-if="!detail.frozen" class="button secondary small" :disabled="loading || submitting || Boolean(pending) || authorInvalid" @click="preview">预览装配映射与完整选片</button>
        <p v-if="!previewReady" role="status">映射草稿尚未通过本轮预览，不能提交装配。预览不保存、不入队；原文保留。</p>
      </details>
      <p>{{ detail.frozen ? '本轮已冻结的输出配置' : '本次待确认输出配置' }}：{{ detail.output.width }} × {{ detail.output.height }} · {{ detail.output.fps }} FPS · 任务尝试上限 {{ detail.output.maxAttempts }}</p>
      <p>可见标识：{{ detail.output.labelEnabled ? detail.output.label : '关闭' }} · 媒体配置 {{ detail.output.profileKey }}</p>
      <details><summary>编码参数与配置摘要</summary><dl><template v-for="(value, key) in detail.output.profile" :key="key"><dt>{{ key }}</dt><dd>{{ value }}</dd></template></dl><p>{{ detail.output.profileSha256 }}</p></details>
      <ul v-if="detail.blockers.length" class="platform-alert warning"><li v-for="item in detail.blockers" :key="item">{{ item }}</li></ul>
      <label><input v-model="checked" type="checkbox" :disabled="loading || !previewReady || submitting || Boolean(pending) || Boolean(confirmation) || Boolean(detail.blockers.length)">我已核对逐镜选片、镜头时长和输出参数</label>
      <p v-if="!detail.episodes.length">本轮还没有可装配的剧集。</p>
      <section v-for="episode in detail.episodes" :key="episode.id" class="manual-assembly-episode">
        <header><strong>第 {{ episode.number }} 集 · {{ episode.title }}</strong><PlatformStatusPill v-if="episode.job" :status="episode.job.status"/><span v-else>尚未提交</span></header>
        <details><summary>查看 {{ episode.shots.length }} 个镜头与已采用来源</summary><ul><li v-for="shot in episode.shots" :key="shot.id">镜头 {{ shot.number }} · {{ shot.title }} · {{ shot.durationMs }} 毫秒 · {{ shot.kind }}<br>已采用媒体：{{ shot.artifactId ?? '尚未选择' }}</li></ul></details>
        <p v-if="episode.job">任务 {{ episode.job.id }} · 尝试 {{ episode.job.attempts }} / {{ episode.job.maxAttempts }} · 更新于 {{ episode.job.updatedAt }}</p>
        <RouterLink v-if="episode.job && !pending" :to="taskCenterTarget({kind:'assembly',id:episode.job.id,runId:run.id,projectId:run.project})">查看此装配任务</RouterLink>
        <p v-if="episode.job?.error" class="platform-alert danger" role="alert">{{ episode.job.error }}</p>
        <button v-if="!pending && (!episode.job || episode.job.status === 'failed')" class="button primary small"
          :disabled="loading || submitting || Boolean(confirmation) || ['succeeded','failed','cancelled'].includes(run.status) || !previewReady || !checked || Boolean(detail.blockers.length)"
          @click="submit(episode.id)">{{ submitting ? '正在提交…' : episode.job ? '重试失败装配' : '提交本集装配' }}</button>
        <section v-if="confirmation?.episodeId === episode.id" class="platform-alert warning" role="region" aria-label="确认本集装配">
          <p>第 {{ episode.number }} 集 · {{ episode.title }}：{{ episode.job ? '重试这项失败装配' : '提交后台装配' }}，使用上述已审核选片及 {{ detail.output.width }} × {{ detail.output.height }}、{{ detail.output.fps }} FPS 输出参数。提交后换片须新建制作轮次；装配完成不等于交付。</p>
          <button class="button ghost small" :disabled="submitting || loading" @click="confirmation = null">暂不装配</button>
          <button class="button primary small" :disabled="submitting || loading" @click="submit(episode.id, true)">确认提交本集装配</button>
        </section>
      </section>
    </template>
  </section>
</template>

<style scoped>
.manual-assembly{display:grid;gap:14px}.manual-assembly header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.manual-assembly p,.manual-assembly h4{margin:0;overflow-wrap:anywhere}.manual-assembly label{display:flex;gap:8px;align-items:center}.manual-assembly-episode{border-top:1px solid var(--border);padding-top:12px;display:grid;gap:10px}.manual-assembly li{padding:5px 0;overflow-wrap:anywhere}.manual-assembly dl{display:grid;grid-template-columns:minmax(100px,1fr) minmax(0,2fr);gap:8px}.manual-assembly dd{margin:0;overflow-wrap:anywhere}
</style>
