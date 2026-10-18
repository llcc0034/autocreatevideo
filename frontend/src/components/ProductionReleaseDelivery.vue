<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { readReleaseDelivery, type ReleaseDelivery, type ReleaseDeliveryIdentity, type ReleaseMaster } from '@/services/releaseDelivery'
import { verifyAssemblyMedia } from '@/services/assemblyMedia'
import type { GeneratedArtifact } from '@/services/platformTypes'

const props = defineProps<ReleaseDeliveryIdentity>()
const opened = ref(false), busy = ref(''), error = ref(''), notice = ref('')
const detail = ref<ReleaseDelivery | null>(null), previews = ref<Record<string, GeneratedArtifact>>({})
const mediaErrors = ref<Record<string, string>>({})
const manifestDownload = ref<{ url: string; name: string } | null>(null)
function clearManifest() {
  if (manifestDownload.value) URL.revokeObjectURL(manifestDownload.value.url)
  manifestDownload.value = null
}
let sequence = 0
function identity(): ReleaseDeliveryIdentity { return { bundleId: props.bundleId, runId: props.runId, projectId: props.projectId } }
async function verified() {
  const expected = identity()
  return readReleaseDelivery(await platformApi.getVerifiedReleaseDocument(expected.bundleId), expected)
}
async function load() {
  if (busy.value) return
  const token = ++sequence; opened.value = true; busy.value = 'read'; error.value = ''; notice.value = ''
  detail.value = null; previews.value = {}; mediaErrors.value = {}
  clearManifest()
  try { const result = await verified(); if (token === sequence) detail.value = result }
  catch (cause) { if (token === sequence) error.value = apiErrorSummary(cause) }
  finally { if (token === sequence) busy.value = '' }
}
function save(url: string, filename = '') {
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename
  document.body.appendChild(anchor); anchor.click(); anchor.remove()
}
async function media(master: ReleaseMaster, download = false) {
  if (busy.value || !detail.value) return
  const token = sequence; busy.value = master.artifactId; error.value = ''; notice.value = ''
  delete previews.value[master.artifactId]; delete mediaErrors.value[master.artifactId]
  try {
    // Recheck the original sealed bundle before refreshing the exact file URL.
    const current = await verified()
    if (token !== sequence) return
    const row = current.masters.find(item => item.artifactId === master.artifactId && item.episodeId === master.episodeId && item.sha256 === master.sha256)
    if (!row) throw new Error('本次母版不再符合已读取清单，请重新读取')
    const artifact = verifyAssemblyMedia(await platformApi.getArtifact(row.artifactId), row, props.runId)
    if (token !== sequence) return
    detail.value = current; previews.value[row.artifactId] = artifact
    if (download) { save(artifact.downloadUrl); notice.value = '已请求下载本集母版；浏览器保存结果不由平台代确认。' }
  } catch (cause) {
    if (token === sequence) { error.value = apiErrorSummary(cause); detail.value = null; previews.value = {}; clearManifest() }
  } finally { if (token === sequence) busy.value = '' }
}
const mediaEvents = (artifact: GeneratedArtifact) => ({ error: () => {
  if (previews.value[artifact.id] === artifact) mediaErrors.value[artifact.id] = '媒体无法播放。请刷新原文件链接；不会自动替换为另一成片。'
} })
async function exportManifest() {
  if (busy.value || !detail.value) return
  const token = sequence; busy.value = 'export'; error.value = ''; notice.value = ''
  clearManifest()
  try {
    const current = await verified()
    if (token !== sequence) return
    detail.value = current
    const url = URL.createObjectURL(new Blob([JSON.stringify(current.document, null, 2)], { type: 'application/json;charset=utf-8' }))
    manifestDownload.value = { url, name: `release-${props.bundleId}.json` }
    save(url, manifestDownload.value.name)
    notice.value = '已请求导出完整签名清单；媒体文件通过各集下载，不是整包视频归档，也未对外发布。'
  } catch (cause) { if (token === sequence) { error.value = apiErrorSummary(cause); detail.value = null; previews.value = {}; clearManifest() } }
  finally { if (token === sequence) busy.value = '' }
}
watch(() => `${props.bundleId}:${props.runId}:${props.projectId}`, () => {
  sequence++; opened.value = false; busy.value = ''; error.value = ''; notice.value = ''; detail.value = null; previews.value = {}; mediaErrors.value = {}
  clearManifest()
})
onBeforeUnmount(() => { sequence++; clearManifest() })
</script>

<template>
  <section class="release-delivery" aria-label="封存成片与交付清单">
    <header><strong>封存成片与交付清单</strong><button class="button ghost small" :disabled="Boolean(busy)" @click="load">{{ busy === 'read' ? '核验中…' : opened ? '重新核验清单' : '查看成片与导出' }}</button></header>
    <p v-if="opened">只读取本发布包的确切母版；封存记录不等于文件当前可用。不会重跑装配或发布到外部平台。</p>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="detail">
      <p>本次文件核验通过 · {{ detail.verifiedAt }} · {{ detail.masters.length }} 集母版</p>
      <button class="button ghost small" :disabled="Boolean(busy)" @click="exportManifest">导出完整签名清单 JSON</button>
      <a v-if="manifestDownload" :href="manifestDownload.url" :download="manifestDownload.name">若未自动保存，下载本次已核验清单 JSON</a>
      <article v-for="master in detail.masters" :key="master.artifactId">
        <p>剧集 ID：<code>{{ master.episodeId }}</code></p>
        <small>母版 {{ master.artifactId }} · SHA-256 {{ master.sha256 }}</small>
        <div class="release-delivery-actions"><button class="button ghost small" :disabled="Boolean(busy)" @click="media(master)">{{ busy === master.artifactId ? '读取中…' : '播放 / 刷新本集母版' }}</button><button class="button ghost small" :disabled="Boolean(busy) || Boolean(mediaErrors[master.artifactId])" @click="media(master,true)">下载本集母版</button></div>
        <template v-if="previews[master.artifactId]">
          <p>{{ previews[master.artifactId]!.objectKey }} · {{ previews[master.artifactId]!.sizeBytes }} 字节</p>
          <video v-if="!mediaErrors[master.artifactId]" :key="previews[master.artifactId]!.previewUrl" :src="previews[master.artifactId]!.previewUrl" controls preload="metadata" v-on="mediaEvents(previews[master.artifactId]!)"/>
          <p v-else class="platform-alert danger" role="alert">{{ mediaErrors[master.artifactId] }}</p>
        </template>
      </article>
    </template>
  </section>
</template>

<style scoped>
.release-delivery{display:grid;gap:12px;min-width:0;padding:14px;border:1px solid var(--border);border-radius:8px}.release-delivery header,.release-delivery-actions{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap}.release-delivery p{margin:0}.release-delivery p,.release-delivery small,.release-delivery code{overflow-wrap:anywhere}.release-delivery article{display:grid;gap:10px;border-top:1px solid var(--border);padding-top:12px}.release-delivery video{width:100%;max-height:480px}.release-delivery small{color:var(--text-secondary)}
</style>
