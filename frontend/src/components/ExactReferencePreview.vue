<script setup lang="ts">
import {onBeforeUnmount, ref, watch} from 'vue'
import AssetMediaPreview from './AssetMediaPreview.vue'
import {assetReferenceApi} from '@/services/assetReferenceApi'
import {repository} from '@/services/repository'
import {apiErrorSummary} from '@/services/apiErrorPresentation'
import {readExactReferencePreview, type ExactReferenceSelection} from '@/services/exactReferencePreview'

const props = defineProps<{selection: ExactReferenceSelection; name?: string}>()
const opened = ref(false), loading = ref(false), error = ref(''), mediaError = ref(false)
const result = ref<Awaited<ReturnType<typeof readExactReferencePreview>> | null>(null)
const sourceLabel = (value: string) => ({existing:'选用已有素材',upload:'正式上传',generated:'模型生成'}[value] || '来源未提供')
let sequence = 0
async function read() {
  if (loading.value) return
  const token = ++sequence, selection = {...props.selection}
  opened.value = true; loading.value = true; error.value = ''; mediaError.value = false; result.value = null
  try {
    const value = await readExactReferencePreview({...assetReferenceApi, getAsset: repository.getAsset}, selection)
    if (token !== sequence) return
    if (!value.asset.mediaUrl && !value.asset.thumbnail) throw Error('原素材没有可读取的预览地址')
    result.value = value
  } catch (cause) {if (token === sequence) error.value = apiErrorSummary(cause)}
  finally {if (token === sequence) loading.value = false}
}
function close() {sequence++; opened.value = false; result.value = null; loading.value = false; error.value = ''; mediaError.value = false}
watch(() => JSON.stringify(props.selection), close)
onBeforeUnmount(close)
</script>

<template>
  <section class="exact-reference-preview" aria-label="确切素材只读预览">
    <strong>{{result?.label || name || '展开读取素材名称与预览'}}</strong>
    <span>{{selection.referenceVersionId ? (selection.version ? '素材版本 v' + selection.version : '已保存的确切素材版本') : '直接素材（按原文件指纹）'}}</span>
    <div class="preview-actions">
      <button type="button" class="button ghost small" :disabled="loading" @click="read">{{loading ? '读取中…' : opened ? '重新读取原素材' : '查看确切素材'}}</button>
      <button v-if="opened" type="button" class="button ghost small" @click="close">收起预览</button>
    </div>
    <template v-if="opened">
      <p v-if="error" class="platform-alert danger" role="alert">{{error}}。未改用其他版本；请重新核对原选择。</p>
      <template v-if="result">
        <p v-if="result.version">系列：{{result.series!.name}} · {{result.series!.role === 'variant' ? '变体' : '主图'}} · 版本 {{result.version.version}} · 取得方式：{{sourceLabel(result.version.source)}}</p>
        <p v-else>此选择直接引用素材，未补造主图、变体或版本关系。</p>
        <div class="preview-media"><AssetMediaPreview :asset="result.asset" controls @source-error="mediaError=true" @source-loaded="mediaError=false"/></div>
        <a v-if="result.asset.mediaUrl" :href="result.asset.mediaUrl" target="_blank" rel="noopener noreferrer">打开本次原素材</a>
        <p v-if="mediaError" class="platform-alert danger" role="alert">原素材预览不可读取或地址已失效。可重新读取原素材，不会切换到其他版本。</p>
        <p>只读预览。名称为当前素材库名称；选择依据仍是下方原身份与文件指纹，不代表审核通过或已被模型使用。</p>
      </template>
      <details><summary>原选择身份与文件指纹</summary><p>素材：{{selection.assetId || result?.asset.id || '待读取原版本'}}<br>版本：{{selection.referenceVersionId || '直接素材'}}<br>SHA256：{{selection.assetSha256}}</p></details>
    </template>
  </section>
</template>

<style scoped>
.exact-reference-preview{display:grid;gap:8px;min-width:0;overflow-wrap:anywhere;border:1px solid var(--border);border-radius:8px;padding:12px}.exact-reference-preview p{margin:0}.preview-actions{display:flex;gap:8px;flex-wrap:wrap}.preview-actions button{min-height:36px;white-space:normal}.preview-media{width:100%;max-width:400px;height:320px;min-width:0;overflow:hidden;border-radius:6px;background:var(--bg-soft)}.preview-media :deep(img){object-fit:contain;width:100%;height:100%}.exact-reference-preview summary{cursor:pointer;min-height:36px}
</style>
