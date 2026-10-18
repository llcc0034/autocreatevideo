<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { verifyAssemblyMedia } from '@/services/assemblyMedia'
import type { JsonObject, ProductionRun } from '@/services/platformTypes'
import type { ManualActionOptions } from '@/services/productionManual'
import { buildManualDelivery, readManualDelivery, type ManualDeliveryDetail } from '@/services/productionManualDelivery'
import PlatformStatusPill from './PlatformStatusPill.vue'

const props = defineProps<{ run: ProductionRun; options: ManualActionOptions }>()
const emit = defineEmits<{ changed: []; busy: [value: boolean] }>()
const detail = ref<ManualDeliveryDetail | null>(null), loading = ref(false), submitting = ref(false)
const error = ref(''), notice = ref(''), checked = ref(false), comment = ref(''), pending = ref<JsonObject | null>(null)
const previews = ref<Record<string, { url: string; download: string }>>({}), previewBusy = ref('')
let sequence = 0, disposed = false
async function load() {
  if (submitting.value) return
  const token = ++sequence; loading.value = true; error.value = ''; checked.value = false; detail.value = null; previews.value = {}; previewBusy.value = ''
  try {
    const raw = await platformApi.getManualDelivery(props.run.id)
    if (token !== sequence) return
    detail.value = readManualDelivery(raw, props.run)
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) loading.value = false }
}
async function preview(episode: ManualDeliveryDetail['episodes'][number], download = false) {
  if (!episode.artifactId || previewBusy.value || submitting.value || loading.value) return
  const token = sequence; previewBusy.value = episode.id; error.value = ''
  delete previews.value[episode.id]
  try {
    const artifact = await platformApi.getArtifact(episode.artifactId)
    if (token !== sequence) return
    verifyAssemblyMedia(artifact, {
      artifactId: episode.artifactId, sha256: episode.sha256!,
      manifestSha256: episode.manifestSha256!, sizeBytes: episode.sizeBytes!,
    }, props.run.id)
    previews.value[episode.id] = { url: artifact.previewUrl, download: artifact.downloadUrl }
    if (download) {
      const link = document.createElement('a')
      link.href = artifact.downloadUrl; link.download = ''; document.body.appendChild(link); link.click(); link.remove()
    }
  } catch (e) { if (token === sequence) { checked.value = false; error.value = apiErrorSummary(e) } }
  finally { if (token === sequence) previewBusy.value = '' }
}
async function submit() {
  if (!detail.value || loading.value || submitting.value || previewBusy.value || (!pending.value && !checked.value)) return
  if (!pending.value && !window.confirm(`确认本轮 ${detail.value.episodes.length} 集成片并封存发布包？\n将结束本轮制作；不会重新转码或发布到外部平台。`)) return
  const token = sequence; submitting.value = true; emit('busy', true); error.value = ''; notice.value = ''
  try {
    if (!pending.value) pending.value = buildManualDelivery(detail.value, props.run, props.options, crypto.randomUUID(), comment.value)
    const result = await platformApi.performManualProductionAction(props.run.id, pending.value)
    if (result.action !== 'deliver' || result.result_kind !== 'release_bundle' || typeof result.target_id !== 'string'
        || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(result.target_id)) throw new Error('未返回正式发布包，请重试同一请求核对结果')
    if (token === sequence) {
      pending.value = null; checked.value = false
      notice.value = `已核验并封存发布包 ${result.target_id}；未发布到外部平台。`
      emit('changed')
    }
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { submitting.value = false; if (!disposed) emit('busy', false) }
  if (!disposed && (notice.value || token !== sequence)) await load()
}
async function reread() {
  if (pending.value && !window.confirm('上次响应不确定。重新读取不会撤销封存，也不会丢弃原请求；仍需重试同一请求核对正式结果。继续？')) return
  await load()
}
watch([() => props.run.id, () => props.run.executionPlan?.sha256], () => {
  sequence++; pending.value = null; notice.value = ''; error.value = ''; comment.value = ''; checked.value = false
  detail.value = null; previews.value = {}; previewBusy.value = ''
  void load()
}, { immediate: true })
onBeforeUnmount(() => { disposed = true; sequence++ })
</script>

<template>
  <section class="manual-delivery">
    <header><h4>确认成片并封存</h4><button class="button ghost small" :disabled="loading || submitting" @click="reread">重新读取成片清单</button></header>
    <p>先核对逐集母版。提交时核验已有文件、签名与选片；不重新转码，不发布到外部平台。</p>
    <p v-if="loading" role="status">正在读取本轮成片清单…</p>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="detail">
      <p v-if="detail.bundle">已记录发布包 {{ detail.bundle.id }} · <PlatformStatusPill :status="detail.bundle.status"/> · 封存时间 {{ detail.bundle.sealedAt ?? '尚未封存' }}。这是已记录状态，不代表刚刚重新核验了文件。</p>
      <ul v-if="detail.blockers.length" class="platform-alert warning"><li v-for="item in detail.blockers" :key="item">{{ item }}</li></ul>
      <article v-for="episode in detail.episodes" :key="episode.id">
        <header><strong>第 {{ episode.number }} 集 · {{ episode.title }}</strong><button v-if="episode.artifactId" class="button ghost small" :disabled="Boolean(previewBusy) || loading || submitting" @click="preview(episode)">{{ previewBusy === episode.id ? '读取预览…' : '预览本集成片' }}</button></header>
        <p>{{ episode.artifactId ? `已归档母版 ${episode.artifactId} · ${episode.sizeBytes} 字节` : '尚无已归档母版，请先完成装配' }}</p>
        <video v-if="previews[episode.id]" :src="previews[episode.id]?.url" controls preload="metadata" @error="checked = false; error = '成片无法播放，请重新读取预览或检查媒体文件；不要确认封存。'"/>
        <button v-if="episode.artifactId" class="button ghost small" :disabled="Boolean(previewBusy) || loading || submitting" @click="preview(episode, true)">下载本集成片核对</button>
      </article>
      <template v-if="!detail.bundle || detail.bundle.status === 'draft' || pending">
        <label><input v-model="checked" type="checkbox" :disabled="submitting || Boolean(pending) || Boolean(detail.blockers.length)">我已核对本轮全部成片，同意结束本轮并封存发布包</label>
        <label>确认说明（可选）<textarea v-model="comment" :disabled="submitting || Boolean(pending)"/></label>
        <button class="button primary" :disabled="loading || submitting || Boolean(previewBusy) || (!pending && (!checked || Boolean(detail.blockers.length) || ['succeeded','failed','cancelled'].includes(run.status)))" @click="submit">{{ submitting ? '正在核验并封存…' : pending ? '重试同一封存请求' : '确认成片并封存' }}</button>
      </template>
    </template>
  </section>
</template>

<style scoped>
.manual-delivery{display:grid;gap:14px}.manual-delivery header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.manual-delivery p,.manual-delivery h4{margin:0;overflow-wrap:anywhere}.manual-delivery label{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.manual-delivery article{border-top:1px solid var(--border);padding-top:12px;display:grid;gap:10px}.manual-delivery video{width:100%;max-height:480px}.manual-delivery textarea{width:100%}
</style>
