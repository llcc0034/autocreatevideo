<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { repository } from '@/services/repository'
const props = defineProps<{ id?: string; assetId?: string; runId: string; sha256: string; label: string }>()
const result = ref<{kind:string;previewUrl:string;downloadUrl?:string} | null>(null), busy = ref(false), error = ref('')
let epoch = 0
async function load() {
  const token = ++epoch; result.value = null; error.value = ''; busy.value = true
  try {
    if (!props.sha256) throw new Error('本轮未保存文件摘要，不能确认预览是否为原文件。')
    if (props.assetId) {
      const row = await repository.getAsset(props.assetId)
      if (token !== epoch) return
      if (row.id !== props.assetId || row.checksum !== props.sha256) throw new Error('素材与本轮保存的引用不一致，未替换为当前素材。')
      if (!row.mediaUrl) throw new Error('此素材尚无可用原文件预览地址。')
      result.value = {kind: row.mimeType?.split('/')[0] || '', previewUrl: row.mediaUrl, downloadUrl: row.downloadUrl}
    } else {
      if (!props.id) throw new Error('本轮未保存产物引用。')
      const row = await platformApi.getArtifact(props.id)
      if (token !== epoch) return
      if (row.id !== props.id || row.productionRun !== props.runId || row.sha256 !== props.sha256) throw new Error('素材与本轮保存的引用不一致，未替换为当前素材。')
      if (!row.previewUrl) throw new Error('此素材尚无可用预览地址。')
      result.value = row
    }
  } catch (cause) { if (token === epoch) error.value = apiErrorSummary(cause) }
  finally { if (token === epoch) busy.value = false }
}
watch(() => [props.id, props.assetId, props.runId, props.sha256], load, { immediate: true })
onBeforeUnmount(() => { epoch++ })
</script>
<template>
  <div class="production-artifact-preview">
    <p v-if="busy" role="status">正在读取{{ label }}…</p>
    <template v-if="result && !error">
      <img v-if="result.kind==='image'" :src="result.previewUrl" :alt="label" loading="lazy" @error="error='图片暂不可用，请刷新原文件预览。'"/>
      <video v-else-if="['video','assembly'].includes(result.kind)" :src="result.previewUrl" controls preload="metadata" @error="error='视频暂不可播放，请刷新原文件预览。'"/>
      <audio v-else-if="result.kind==='audio'" :src="result.previewUrl" controls preload="metadata" @error="error='音频暂不可播放，请刷新原文件预览。'"/>
      <a :href="result.downloadUrl" v-if="result.downloadUrl" download>下载本次文件</a>
    </template>
    <p v-if="error" role="alert">{{ error }}</p>
    <button v-if="error" type="button" class="button ghost small" :disabled="busy" @click="load">重新读取原文件</button>
  </div>
</template>
<style scoped>
.production-artifact-preview{display:grid;gap:8px;min-width:0}.production-artifact-preview :is(img,video){display:block;width:100%;max-height:380px;object-fit:contain;background:var(--bg-soft);border-radius:6px}.production-artifact-preview p{overflow-wrap:anywhere}.production-artifact-preview a{color:var(--accent)}
</style>
