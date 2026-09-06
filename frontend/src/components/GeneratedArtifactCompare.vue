<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { AlertTriangle, Check, Download, Image as ImageIcon, LoaderCircle, RefreshCw, Video } from 'lucide-vue-next'
import PlatformStatusPill from './PlatformStatusPill.vue'
import { comparisonParameterRows, reconcileArtifactSelection, synchronizeVideos, toggleArtifactSelection } from '@/services/artifactComparison'
import { presentPlatformStatus } from '@/services/platformAdapters'
import { platformApi } from '@/services/platformApi'
import { artifactRoleLabel } from '@/services/presentationLabels'
import type { ArtifactPreview, GeneratedArtifact } from '@/services/platformTypes'

const props = defineProps<{ runId: string; artifacts: GeneratedArtifact[] }>()
const emit = defineEmits<{ selected: [artifact: GeneratedArtifact] }>()
const previews = ref<Record<string, ArtifactPreview | null>>({})
const errors = ref<Record<string, string>>({})
const loading = ref<Record<string, boolean>>({})
const selecting = ref('')
const selectedIds = ref<string[]>([])
const videos = new Map<string, HTMLVideoElement>()
let syncing = false
const visibleArtifacts = computed(() => selectedIds.value.flatMap((id) => {
  const artifact = props.artifacts.find((item) => item.id === id)
  return artifact ? [artifact] : []
}))
const message = (cause: unknown) => cause instanceof Error ? cause.message : '媒体预览请求失败'
const loadPreview = async (artifact: GeneratedArtifact) => {
  loading.value[artifact.id] = true; errors.value[artifact.id] = ''
  try { const preview = await platformApi.getArtifactPreview(artifact); previews.value[artifact.id] = preview; if (!preview?.previewUrl) errors.value[artifact.id] = '服务端任务未返回可用的签名预览 URL。' }
  catch (cause) { errors.value[artifact.id] = message(cause); previews.value[artifact.id] = null }
  finally { loading.value[artifact.id] = false }
}
const loadAll = () => Promise.all(visibleArtifacts.value.map(loadPreview))
const syncCandidateSelection = () => {
  selectedIds.value = reconcileArtifactSelection(props.artifacts, selectedIds.value)
}
const toggleCandidate = (artifactId: string, event: Event) => {
  const checked = (event.target as HTMLInputElement).checked
  selectedIds.value = toggleArtifactSelection(selectedIds.value, artifactId, checked, props.artifacts.length)
}
watch(() => props.artifacts.map((item) => `${item.id}:${item.status}`).join('|'), syncCandidateSelection, { immediate: true })
watch(() => visibleArtifacts.value.map((item) => `${item.id}:${item.status}`).join('|'), () => { void loadAll() }, { immediate: true })
const setVideoRef = (id: string) => (element: unknown) => { if (element instanceof HTMLVideoElement) videos.set(id, element); else videos.delete(id) }
const synchronize = async (sourceId: string, mode: 'play' | 'pause' | 'seek') => {
  if (syncing || !videos.has(sourceId)) return; syncing = true
  try { await synchronizeVideos(sourceId, mode, videos) } finally { window.setTimeout(() => { syncing = false }, 0) }
}
const select = async (artifact: GeneratedArtifact) => { selecting.value = artifact.id; errors.value[artifact.id] = ''; try { emit('selected', await platformApi.selectArtifact(props.runId, artifact.id)) } catch (cause) { errors.value[artifact.id] = message(cause) } finally { selecting.value = '' } }
const parameterRows = computed(() => comparisonParameterRows(
  visibleArtifacts.value.map((artifact) => previews.value[artifact.id]?.parameters || {}),
))
</script>

<template>
  <section class="artifact-compare">
    <header><div><strong>同组版本对比</strong><span>并排 {{ visibleArtifacts.length }} / 候选 {{ artifacts.length }} · 可选择 2–4 个</span></div><button class="button ghost tiny" @click="loadAll"><RefreshCw :size="12"/>刷新签名预览</button></header>
    <div v-if="artifacts.length>4" class="artifact-candidate-picker" role="group" aria-label="选择并排比较的产物版本"><span>比较候选</span><label v-for="artifact in artifacts" :key="artifact.id" :class="{active:selectedIds.includes(artifact.id)}"><input type="checkbox" :checked="selectedIds.includes(artifact.id)" :disabled="selectedIds.includes(artifact.id)?selectedIds.length<=2:selectedIds.length>=4" @change="toggleCandidate(artifact.id,$event)"/><b>V{{ artifact.variantIndex }}</b><small>{{ presentPlatformStatus(artifact.status).label }}</small></label></div>
    <div class="artifact-compare-grid" :style="{'--artifact-count':visibleArtifacts.length}">
      <article v-for="artifact in visibleArtifacts" :key="artifact.id" :class="{selected:artifact.status==='selected'}">
        <header><span><b>V{{ artifact.variantIndex }}</b><small>{{ artifactRoleLabel(artifact.role) }}</small></span><PlatformStatusPill :status="artifact.status"/></header>
        <div class="artifact-media">
          <div v-if="loading[artifact.id]" class="artifact-media-state"><LoaderCircle class="spin" :size="20"/>读取真实媒体…</div>
          <img v-else-if="previews[artifact.id]?.previewUrl && (artifact.kind==='image'||artifact.mimeType.startsWith('image/'))" :src="previews[artifact.id]?.previewUrl" :alt="artifact.objectKey"/>
          <video v-else-if="previews[artifact.id]?.previewUrl && (artifact.kind==='video'||artifact.mimeType.startsWith('video/'))" :ref="setVideoRef(artifact.id)" :src="previews[artifact.id]?.previewUrl" controls preload="metadata" @play="synchronize(artifact.id,'play')" @pause="synchronize(artifact.id,'pause')" @seeked="synchronize(artifact.id,'seek')"></video>
          <div v-else class="artifact-media-state danger"><component :is="artifact.kind==='video'?Video:ImageIcon" :size="24"/><span>{{ errors[artifact.id] || '该 artifact 没有浏览器可预览媒体。' }}</span></div>
        </div>
        <div v-if="errors[artifact.id] && previews[artifact.id]?.previewUrl" class="artifact-inline-error"><AlertTriangle :size="12"/>{{ errors[artifact.id] }}</div>
        <dl><div><dt>文件</dt><dd>{{ artifact.objectKey }}</dd></div><div><dt>内容摘要（SHA-256）</dt><dd><code>{{ artifact.sha256.slice(0,16) || '—' }}</code></dd></div><div><dt>任务状态</dt><dd>{{ previews[artifact.id]?.backendStatus ? presentPlatformStatus(previews[artifact.id]!.backendStatus).label : '—' }}</dd></div></dl>
        <footer><a v-if="previews[artifact.id]?.downloadUrl" class="button ghost tiny" :href="previews[artifact.id]?.downloadUrl" target="_blank" rel="noopener"><Download :size="12"/>下载</a><button v-if="artifact.status!=='selected'" class="button primary tiny" :disabled="Boolean(selecting)" @click="select(artifact)">{{ selecting===artifact.id?'选择中…':'选为本组产物' }}</button><span v-else><Check :size="12"/>当前选择</span></footer>
      </article>
    </div>
    <details class="artifact-parameter-diff" :open="parameterRows.length>0"><summary>已归档脱敏参数差异 · {{ parameterRows.length }} 项</summary><div v-if="parameterRows.length" class="platform-table-wrap"><table class="platform-table"><thead><tr><th>参数</th><th v-for="artifact in visibleArtifacts" :key="artifact.id">V{{ artifact.variantIndex }}</th></tr></thead><tbody><tr v-for="row in parameterRows" :key="row.key"><td><code>{{ row.key }}</code></td><td v-for="(value,index) in row.values" :key="index"><code>{{ value }}</code></td></tr></tbody></table></div><p v-else>服务端归档的正式比较参数一致或未提供；页面不会补造参数。</p></details>
  </section>
</template>
