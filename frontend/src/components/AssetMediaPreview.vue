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
  const requestController = new AbortController()
  controller = requestController; loadingScript.value = true
  try {
    const response = await fetch(requestedSource, { credentials: 'same-origin', signal: requestController.signal })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    scriptText.value = await response.text()
    reportSourceLoaded(requestedSource)
  } catch (error) {
    if ((error as Error).name !== 'AbortError') {
      scriptError.value = '剧本预览链接已过期或文件不可读取，正在刷新访问地址。'
      reportSourceError(requestedSource)
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
    <span v-if="loadingScript">读取预览中…</span><span v-else-if="scriptError">{{ scriptError }}</span>
    <pre v-else-if="!compact">{{ scriptText }}</pre><p v-else>{{ asset.description || '剧本素材' }}</p>
  </div>
  <div v-else class="asset-video-placeholder"><Video :size="36"/><span>暂不支持的素材预览</span></div>
</template>

<style scoped>
video{width:100%;height:100%;object-fit:contain;background:#07090c}.asset-audio-preview,.asset-script-preview,.asset-video-placeholder{width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:18px;background:radial-gradient(circle at 50% 30%,rgba(104,197,167,.12),transparent 55%),#0b0e12;color:#8fc9b7}.asset-audio-preview strong,.asset-script-preview strong{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#d5dadc;font-size:12px}.asset-audio-preview audio{width:min(100%,420px)}.asset-audio-preview.compact audio{display:none}.asset-script-preview span,.asset-video-placeholder span{font-size:9px;color:#747c82}.asset-script-preview pre{width:100%;max-height:100%;overflow:auto;margin:0;padding:14px;border:1px solid #242a30;border-radius:7px;background:#090b0e;color:#bcc2c5;white-space:pre-wrap;font:10px/1.65 ui-monospace,monospace}.asset-script-preview.compact{align-items:flex-start;justify-content:flex-end}.asset-script-preview.compact p{display:-webkit-box;overflow:hidden;margin:0;color:#747b81;font-size:9px;line-height:1.5;-webkit-box-orient:vertical;-webkit-line-clamp:3}
</style>
