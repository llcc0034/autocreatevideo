<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { AlertTriangle, Check, Download, Image as ImageIcon, LoaderCircle, RefreshCw, Video } from 'lucide-vue-next'
import PlatformStatusPill from './PlatformStatusPill.vue'
import BaseModal from './BaseModal.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { materialDependencyActionLabel, materialSelectionPayload, type RecipeMaterialSelectionContext } from '@/services/recipeMaterialSelection'
import { comparisonParameterRows, reconcileArtifactSelection, synchronizeVideos, toggleArtifactSelection } from '@/services/artifactComparison'
import { presentPlatformStatus } from '@/services/platformAdapters'
import { platformApi } from '@/services/platformApi'
import { artifactRoleLabel } from '@/services/presentationLabels'
import type { ArtifactPreview, GeneratedArtifact } from '@/services/platformTypes'
import { createArtifactSelectionCommand, type ArtifactSelectionConfirmed, type ArtifactSelectionProtection } from '@/services/artifactSelectionCommand'

const props = defineProps<{ runId: string; scopeId?: string; artifacts: GeneratedArtifact[]; materialSelection?: boolean; disabled?: boolean }>()
const emit = defineEmits<{ selected: [value: ArtifactSelectionConfirmed]; protection: [value: ArtifactSelectionProtection] }>()
const command = createArtifactSelectionCommand((...args) => platformApi.selectArtifact(...args),
  () => ({runId: props.runId, scopeId: props.scopeId ?? 'media'}), value => emit('protection', value), value => {
    pendingMaterial.value = null; materialContext.value = null; emit('selected', value)
  })
const selectionState = command.state
const selecting = computed(() => selectionState.submitting ? selectionState.pending?.body.artifact_id ?? '' : '')
const selectionLocked = computed(() => Boolean(props.disabled || selectionState.pending || selectionState.submitting))
watch(() => [props.runId, props.scopeId], () => command.reset(), {flush: 'sync'})
const previews = ref<Record<string, ArtifactPreview | null>>({})
const errors = ref<Record<string, string>>({})
const mediaErrors = ref<Record<string, string>>({})
const loading = ref<Record<string, boolean>>({})
const selectedIds = ref<string[]>([])
const pendingMaterial = ref<GeneratedArtifact | null>(null)
const materialContext = ref<RecipeMaterialSelectionContext | null>(null)
const materialError = ref('')
const materialLoading = ref(false)
let sequence = 0
let previewSequence = 0
let previewRequestId = 0
const previewRequests = new Map<string, number>()
const videos = new Map<string, HTMLVideoElement>()
let syncing = false
const visibleArtifacts = computed(() => selectedIds.value.flatMap((id) => {
  const artifact = props.artifacts.find((item) => item.id === id)
  return artifact ? [artifact] : []
}))
const message = (cause: unknown) => apiErrorSummary(cause, '媒体请求失败')
const usableStatus = (status: string) => status === 'available' || status === 'selected'
const canSelectCandidate = (artifact: GeneratedArtifact) => usableStatus(artifact.status)
  && !loading.value[artifact.id] && !mediaErrors.value[artifact.id]
  && Boolean(previews.value[artifact.id]?.previewUrl)
const mediaEvents = (id: string, preview: ArtifactPreview | null | undefined) => ({
  error: () => {
    // A detached player from an older signature or Run must not poison the new preview.
    if (preview && previews.value[id] === preview) {
      mediaErrors.value[id] = '媒体加载失败，可能是链接失效、文件不可读或浏览器不支持。请重新读取预览后重试。'
      videos.delete(id)
    }
  },
})
const loadPreview = async (artifact: GeneratedArtifact) => {
  const token = previewSequence
  const request = ++previewRequestId
  previewRequests.set(artifact.id, request)
  const current = () => token === previewSequence && previewRequests.get(artifact.id) === request
  previews.value[artifact.id] = null; mediaErrors.value[artifact.id] = ''; errors.value[artifact.id] = ''
  videos.delete(artifact.id)
  if (!usableStatus(artifact.status)) {
    mediaErrors.value[artifact.id] = '该候选状态不可用，不能预览或选择。'
    loading.value[artifact.id] = false
    return
  }
  loading.value[artifact.id] = true
  try {
    const preview = await platformApi.getArtifactPreview(artifact)
    if (!current()) return
    previews.value[artifact.id] = preview
    if (preview && !usableStatus(preview.backendStatus)) mediaErrors.value[artifact.id] = '服务端候选状态已不可用，请刷新候选清单。'
    else if (!preview?.previewUrl) errors.value[artifact.id] = '服务端任务未返回可用的签名预览 URL。'
  } catch (cause) { if (current()) { errors.value[artifact.id] = message(cause); previews.value[artifact.id] = null } }
  finally { if (current()) loading.value[artifact.id] = false }
}
const loadAll = () => Promise.all(visibleArtifacts.value.map(loadPreview))
const syncCandidateSelection = () => {
  selectedIds.value = reconcileArtifactSelection(props.artifacts, selectedIds.value)
}
const toggleCandidate = (artifactId: string, event: Event) => {
  const checked = (event.target as HTMLInputElement).checked
  selectedIds.value = toggleArtifactSelection(selectedIds.value, artifactId, checked, props.artifacts.length)
}
const previewContextKey = computed(() => `${props.runId}:${props.materialSelection}:${props.artifacts.map((item) => `${item.id}:${item.status}`).join('|')}`)
watch(previewContextKey, () => {
  sequence++; previewSequence++; pendingMaterial.value = null; materialContext.value = null; materialError.value = ''; materialLoading.value = false
  previews.value = {}; errors.value = {}; mediaErrors.value = {}; loading.value = {}; previewRequests.clear(); videos.clear(); syncCandidateSelection()
}, { immediate: true })
watch(() => `${previewContextKey.value}:${selectedIds.value.join('|')}`, () => { void loadAll() }, { immediate: true })
const setVideoRef = (id: string) => (element: unknown) => { if (element instanceof HTMLVideoElement) videos.set(id, element); else videos.delete(id) }
const synchronize = async (sourceId: string, mode: 'play' | 'pause' | 'seek') => {
  if (syncing || !videos.has(sourceId)) return; syncing = true
  try { await synchronizeVideos(sourceId, mode, videos) } finally { window.setTimeout(() => { syncing = false }, 0) }
}
const inspectMaterial = async (artifact: GeneratedArtifact) => {
  if (selectionLocked.value) return
  const token = ++sequence
  pendingMaterial.value = artifact; materialContext.value = null; materialError.value = ''; materialLoading.value = true
  try { const context = await platformApi.getMaterialSelectionContext(props.runId, artifact.id); if (token === sequence) materialContext.value = context }
  catch (cause) { if (token === sequence) materialError.value = message(cause) }
  finally { if (token === sequence) materialLoading.value = false }
}
const closeMaterial = () => { if (!selecting.value) { sequence++; pendingMaterial.value = null; materialContext.value = null; materialLoading.value = false } }
const select = async (artifact: GeneratedArtifact, confirmed = false) => {
  if (selectionLocked.value || !canSelectCandidate(artifact)) return
  if (props.materialSelection && !confirmed) return inspectMaterial(artifact)
  errors.value[artifact.id] = ''; materialError.value = ''
  try {
    const input = props.materialSelection ? materialSelectionPayload(materialContext.value!, artifact.id) : undefined
    await command.submit({artifact_id: artifact.id, ...(input === undefined ? {} : {material_selection: input})})
    if (selectionState.error && !selectionState.pending) materialContext.value = null
  } catch (cause) {
    materialError.value = message(cause)
  }
}
onBeforeUnmount(() => { sequence++; previewSequence++; command.reset() })
const parameterRows = computed(() => comparisonParameterRows(
  visibleArtifacts.value.map((artifact) => previews.value[artifact.id]?.parameters || {}),
))
</script>

<template>
  <section class="artifact-compare">
    <p v-if="selectionState.error" role="alert">{{ selectionState.error }}</p>
    <p v-if="selectionState.notice" role="status">{{ selectionState.notice }}</p>
    <div v-if="selectionState.pending" role="status">原选择请求尚未确认，暂停改选。<button class="button ghost small" :disabled="selectionState.submitting" @click="command.submit()">核对原选择请求</button></div>
    <header><div><strong>同组版本对比</strong><span>并排 {{ visibleArtifacts.length }} / 候选 {{ artifacts.length }} · 可选择 2–4 个</span></div><button class="button ghost tiny" @click="loadAll"><RefreshCw :size="12"/>刷新签名预览</button></header>
    <div v-if="artifacts.length>4" class="artifact-candidate-picker" role="group" aria-label="选择并排比较的产物版本"><span>比较候选</span><label v-for="artifact in artifacts" :key="artifact.id" :class="{active:selectedIds.includes(artifact.id)}"><input type="checkbox" :checked="selectedIds.includes(artifact.id)" :disabled="selectedIds.includes(artifact.id)?selectedIds.length<=2:selectedIds.length>=4" @change="toggleCandidate(artifact.id,$event)"/><b>V{{ artifact.variantIndex }}</b><small>{{ presentPlatformStatus(artifact.status).label }}</small></label></div>
    <div class="artifact-compare-grid" :style="{'--artifact-count':visibleArtifacts.length}">
      <article v-for="artifact in visibleArtifacts" :key="artifact.id" :class="{selected:artifact.status==='selected'}">
        <header><span><b>V{{ artifact.variantIndex }}</b><small>{{ artifactRoleLabel(artifact.role) }}</small></span><PlatformStatusPill :status="artifact.status"/></header>
        <div class="artifact-media">
          <div v-if="loading[artifact.id]" class="artifact-media-state"><LoaderCircle class="spin" :size="20"/>读取真实媒体…</div>
          <div v-else-if="mediaErrors[artifact.id]" class="artifact-media-state danger" role="alert"><AlertTriangle :size="24"/><span>{{ mediaErrors[artifact.id] }}</span><button class="button ghost tiny" @click="loadPreview(artifact)">重新读取预览</button></div>
          <img v-else-if="previews[artifact.id]?.previewUrl && (artifact.kind==='image'||artifact.mimeType.startsWith('image/'))" :src="previews[artifact.id]?.previewUrl" :alt="artifact.objectKey" v-on="mediaEvents(artifact.id,previews[artifact.id])"/>
          <video v-else-if="previews[artifact.id]?.previewUrl && (artifact.kind==='video'||artifact.mimeType.startsWith('video/'))" :ref="setVideoRef(artifact.id)" :src="previews[artifact.id]?.previewUrl" controls preload="metadata" v-on="mediaEvents(artifact.id,previews[artifact.id])" @play="synchronize(artifact.id,'play')" @pause="synchronize(artifact.id,'pause')" @seeked="synchronize(artifact.id,'seek')"></video>
          <div v-else class="artifact-media-state danger"><component :is="artifact.kind==='video'?Video:ImageIcon" :size="24"/><span>{{ errors[artifact.id] || '该 artifact 没有浏览器可预览媒体。' }}</span></div>
        </div>
        <div v-if="errors[artifact.id] && previews[artifact.id]?.previewUrl" class="artifact-inline-error"><AlertTriangle :size="12"/>{{ errors[artifact.id] }}</div>
        <dl><div><dt>文件</dt><dd>{{ artifact.objectKey }}</dd></div><div><dt>内容摘要（SHA-256）</dt><dd><code>{{ artifact.sha256.slice(0,16) || '—' }}</code></dd></div><div><dt>任务状态</dt><dd>{{ previews[artifact.id]?.backendStatus ? presentPlatformStatus(previews[artifact.id]!.backendStatus).label : '—' }}</dd></div></dl>
        <footer><a v-if="previews[artifact.id]?.downloadUrl && !mediaErrors[artifact.id]" class="button ghost tiny" :href="previews[artifact.id]?.downloadUrl" target="_blank" rel="noopener"><Download :size="12"/>下载</a><button v-if="artifact.status!=='selected'||materialSelection" class="button primary tiny" :disabled="selectionLocked||!canSelectCandidate(artifact)" @click="select(artifact)">{{ selecting===artifact.id?'选择中…':materialSelection?'查看素材选择影响':'选为本组产物' }}</button><span v-if="artifact.status==='selected'"><Check :size="12"/>当前选择</span></footer>
      </article>
    </div>
    <details class="artifact-parameter-diff" :open="parameterRows.length>0"><summary>已归档脱敏参数差异 · {{ parameterRows.length }} 项</summary><div v-if="parameterRows.length" class="platform-table-wrap"><table class="platform-table"><thead><tr><th>参数</th><th v-for="artifact in visibleArtifacts" :key="artifact.id">V{{ artifact.variantIndex }}</th></tr></thead><tbody><tr v-for="row in parameterRows" :key="row.key"><td><code>{{ row.key }}</code></td><td v-for="(value,index) in row.values" :key="index"><code>{{ value }}</code></td></tr></tbody></table></div><p v-else>服务端归档的正式比较参数一致或未提供；页面不会补造参数。</p></details>
    <BaseModal :open="Boolean(pendingMaterial)" :dismissible="!selecting" title="阶段内素材选择" eyebrow="原候选选择记录 · 不等于审核采用" width="640px" @close="closeMaterial">
      <p v-if="materialLoading" role="status">正在读取候选与改选影响…</p>
      <p v-if="materialError" class="platform-alert danger" role="alert">{{ materialError }}</p>
      <div v-if="materialContext" class="registry-action-review">
        <p>{{ materialContext.referenceName }} · {{ materialContext.appearanceRole==='primary'?'主图':'变体' }} · 候选 {{ materialContext.candidateNumber }}</p>
        <p>只记录本外观单元的选择，不回写 Bible 审核采用。选择后原流程可按本轮已有授权继续生成；不会修改已冻结的请求或历史结果。</p>
        <p v-if="materialContext.reason" class="platform-alert" role="status">{{ materialContext.reason }}</p>
        <p v-else-if="materialContext.selectedArtifactId===materialContext.artifactId">本轮追加了候选。可明确保留当前图；会记录本次人工选择，原图、版本及历史依据保持不变。</p>
        <p v-else>提交时将再次核对当前选择及下列影响；清单或准备状态变化后须重新查看并确认。</p>
        <ul v-if="materialContext.dependentPreparations.length" aria-label="已冻结主图依据的变体准备">
          <li v-for="item in materialContext.dependentPreparations" :key="item.id" class="material-dependency-impact">
            <strong>{{ item.referenceName }} · {{ materialDependencyActionLabel(item.action) }}</strong>
            <span>{{ presentPlatformStatus(item.status).label }} · 准备修订 {{ item.revision }}</span>
            <p>{{ item.reason }}</p>
          </li>
        </ul>
        <p v-if="materialContext.dependentPreparations.some(item=>item.action==='reprepare')">确认会替代清单中主调用尚未开始的准备，并停止确定未提交的辅助任务。旧请求及已结束的辅助结果保留，不移植到新准备；本次确认不调用模型，后续由原流程按已有授权继续。</p>
      </div>
      <template #footer>
        <button class="button ghost" :disabled="Boolean(selecting)" @click="closeMaterial">返回</button>
        <button v-if="pendingMaterial" class="button ghost" :disabled="materialLoading||selectionLocked" @click="inspectMaterial(pendingMaterial)">重新读取影响</button>
        <button v-if="selectionState.pending" class="button primary" :disabled="selectionState.submitting" @click="command.submit()">核对原选择请求</button>
        <button v-else class="button primary" :disabled="!materialContext?.canSelect||materialLoading||selectionLocked||!pendingMaterial||!canSelectCandidate(pendingMaterial)" @click="pendingMaterial && select(pendingMaterial,true)">{{ selecting?'正在确认…':materialContext?.selectedArtifactId===materialContext?.artifactId?'确认保留当前图':materialContext?.dependentPreparations.length?'确认改选及依赖处理':'确认选择此候选' }}</button>
      </template>
    </BaseModal>
  </section>
</template>

<style scoped>
.material-dependency-impact { margin-block: 12px; overflow-wrap: anywhere; }
.material-dependency-impact strong, .material-dependency-impact span { display: block; }
.material-dependency-impact p { margin-block: 4px; }
</style>
