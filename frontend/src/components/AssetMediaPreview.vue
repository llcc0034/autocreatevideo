<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { FileText, Music2, Video } from 'lucide-vue-next'
import AssetImage from '@/components/AssetImage.vue'
import type { Asset } from '@/types'

const props = withDefaults(defineProps<{
  asset: Asset
  compact?: boolean
  controls?: boolean
}>(), { compact: false, controls: false })
const emit = defineEmits<{
  sourceError: [payload: { assetId: string; source: string }]
  sourceLoaded: [payload: { assetId: string; source: string }]
}>()

const scriptText = ref('')
const scriptError = ref('')
const loadingScript = ref(false)
let controller: AbortController | null = null
const source = computed(() => props.asset.mediaUrl || props.asset.thumbnail || '')
const scriptUnavailable = computed(() => !source.value ? '未提供剧本预览地址' : '')
const reportSourceError = (failedSource = source.value) => { if (failedSource) emit('sourceError', { assetId: props.asset.id, source: failedSource }) }
const reportSourceLoaded = (loadedSource = source.value) => { if (loadedSource) emit('sourceLoaded', { assetId: props.asset.id, source: loadedSource }) }

watch(() => [props.asset.id, props.asset.type, source.value, props.compact], async () => {
  controller?.abort(); controller = null
  scriptText.value = ''; scriptError.value = ''
  if (props.asset.type !== 'script' || props.compact || !source.value) {
    loadingScript.value = false
    return
  }
  const requestedSource = source.value
  const requestedId = props.asset.id
  const requestController = new AbortController()
  controller = requestController; loadingScript.value = true
  try {
    const response = await fetch(requestedSource, { credentials: 'same-origin', signal: requestController.signal })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const text = await response.text()
    if (controller !== requestController || requestController.signal.aborted || props.asset.id !== requestedId || source.value !== requestedSource) return
    scriptText.value = text
    emit('sourceLoaded', { assetId: requestedId, source: requestedSource })
  } catch (error) {
    if (controller === requestController && !requestController.signal.aborted && props.asset.id === requestedId && source.value === requestedSource && (error as Error).name !== 'AbortError') {
      scriptError.value = '剧本文件暂不可读取，请重新读取素材；原引用不变。'
      emit('sourceError', { assetId: requestedId, source: requestedSource })
    }
  } finally { if (controller === requestController) { controller = null; loadingScript.value = false } }
}, { immediate: true })

onBeforeUnmount(() => controller?.abort())
</script>

<template>
  <AssetImage v-if="['character','scene','prop','image'].includes(asset.type)" :src="source" :alt="asset.name" :label="asset.name" @source-error="reportSourceError" @source-loaded="reportSourceLoaded"/>
  <video v-else-if="asset.type==='video'" :src="source" :controls="controls" :preload="compact?'none':'metadata'" playsinline @error="reportSourceError(source)" @loadedmetadata="reportSourceLoaded(source)"/>
  <div v-else-if="asset.type==='audio'" class="asset-audio-preview" :class="{compact}">
    <Music2 :size="compact?28:48"/><strong>{{ asset.name }}</strong><audio :src="source" :controls="controls" :preload="compact?'none':'metadata'" @error="reportSourceError(source)" @loadedmetadata="reportSourceLoaded(source)"/>
  </div>
  <div v-else-if="asset.type==='script'" class="asset-script-preview" :class="{compact}">
    <FileText :size="compact?28:42"/>
    <strong>{{ asset.name }}</strong>
    <span v-if="loadingScript" role="status">读取预览中…</span><span v-else-if="scriptError" role="alert">{{ scriptError }}</span><span v-else-if="!compact&&scriptUnavailable" role="status">{{ scriptUnavailable }}</span>
    <pre v-else-if="!compact">{{ scriptText }}</pre><p v-else>{{ asset.description || '剧本素材' }}</p>
  </div>
  <div v-else class="asset-video-placeholder"><Video :size="36"/><span>暂不支持的素材预览</span></div>
</template>

<style scoped>
video{width:100%;height:100%;object-fit:contain;background:var(--bg-soft)}.asset-audio-preview,.asset-script-preview,.asset-video-placeholder{width:100%;height:100%;min-width:0;min-height:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;padding:16px;background:var(--bg-soft);color:var(--text-secondary);font:var(--config-body-size)/1.6 var(--font)}.asset-audio-preview strong,.asset-script-preview strong{max-width:100%;overflow-wrap:anywhere;color:var(--text);font-size:var(--config-body-size)}.asset-audio-preview audio{width:min(100%,420px);min-width:0}.asset-audio-preview.compact audio{display:none}.asset-script-preview span,.asset-video-placeholder span{font-size:var(--config-help-size)}.asset-script-preview pre{width:100%;min-height:0;overflow:auto;margin:0;padding:12px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);white-space:pre-wrap;overflow-wrap:anywhere;font:var(--config-body-size)/1.65 var(--font)}.asset-script-preview.compact{align-items:flex-start;justify-content:flex-end}.asset-script-preview.compact p{display:-webkit-box;overflow:hidden;margin:0;font-size:var(--config-help-size);line-height:1.5;-webkit-box-orient:vertical;-webkit-line-clamp:3}.asset-script-preview [role=alert]{color:var(--red)}
</style>
