<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { Asset } from '@/types'
import { assetReferenceApi, type ReferenceSeriesPage } from '@/services/assetReferenceApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
const props = defineProps<{ asset: Asset }>()
const relations = ref<ReferenceSeriesPage | null>(null), loading = ref(false), error = ref(''), page = ref(1)
const mediaFacts = computed(() => {
  const metadata = props.asset.metadata || {}
  const number = (key: string, unit: string) => {
    const value = metadata[key]
    return value === undefined || value === null ? '未记录' : typeof value === 'number' && Number.isFinite(value) && value >= 0 ? `${value} ${unit}` : '记录格式异常'
  }
  const text = (key: string) => metadata[key] === undefined || metadata[key] === null || metadata[key] === '' ? '未记录' : typeof metadata[key] === 'string' ? String(metadata[key]) : '记录格式异常'
  const rows = [{ label: '文件大小', value: number('size_bytes', '字节') }]
  if (['video', 'audio'].includes(props.asset.type)) rows.push({ label: '时长', value: number('duration_ms', '毫秒') })
  if (props.asset.type === 'video') rows.push({ label: '来源帧率', value: number('source_fps', 'fps') }, { label: '内嵌音频', value: metadata.has_audio === true ? '有' : metadata.has_audio === false ? '无' : metadata.has_audio === undefined || metadata.has_audio === null ? '未记录' : '记录格式异常' })
  if (props.asset.type === 'script') rows.push({ label: '上传原格式', value: text('script_source_format') }, { label: '入库处理', value: metadata.script_normalization === 'utf8_markdown_v1' ? 'UTF-8 Markdown 规范化' : text('script_normalization') })
  return rows
})
let sequence = 0
watch(() => props.asset.id, () => { sequence++; relations.value = null; error.value = ''; loading.value = false; page.value = 1 })
async function loadRelations(next = 1) {
  const current = ++sequence, id = props.asset.id
  loading.value = true; error.value = ''; relations.value = null
  try {
    const result = await assetReferenceApi.listSeries({ asset_id: id, search: '', page: next })
    if (current !== sequence || id !== props.asset.id) return
    relations.value = result; page.value = next
  } catch (cause) { if (current === sequence) error.value = apiErrorSummary(cause) }
  finally { if (current === sequence) loading.value = false }
}
</script>
<template>
  <section class="asset-facts" aria-label="素材事实与使用范围">
    <section><h3>媒体信息</h3><dl><dt>文件类型</dt><dd>{{ asset.mimeType || '未记录' }}</dd><template v-for="fact in mediaFacts" :key="fact.label"><dt>{{ fact.label }}</dt><dd>{{ fact.value }}</dd></template><dt>库内状态</dt><dd>{{ asset.isActive === false ? '已停用' : asset.isActive === true ? '启用' : '未提供' }}</dd><dt>最近更新</dt><dd>{{ asset.updatedAt || '未记录' }}</dd></dl><p class="help">来自此素材已保存的媒体记录；未记录不等于 0，也不使用生成参数推算实际文件信息。</p></section>
    <section><h3>来源说明</h3><p>{{ asset.sourceInfo?.trim() || '来源未记录（历史素材）' }}</p><p v-if="asset.metadata?.cleared_history_origin">原业务来源历史已清理；素材原字节与来源说明独立保留，不需要恢复旧执行配置。</p><p class="help">来源由系统记录，编辑素材信息不会重写来源或重新生成文件。</p></section>
    <section><h3>使用与关系</h3><p>素材可由新任务引用；它不是执行实例，也没有配置发布或后继流程。</p><p class="help">此处只查询包含当前素材的主图／变体系列，不代表全部项目或任务引用；没有系列不等于没有其他使用方。</p>
      <button type="button" class="button ghost" :disabled="loading" @click="loadRelations()">{{ loading ? '正在查询关系…' : '查询主图 / 变体关系' }}</button>
      <p v-if="error" role="alert">{{ error }}</p>
      <template v-if="relations"><p role="status">查到 {{ relations.count }} 个系列 · 第 {{ page }} 页</p><ul><li v-for="row in relations.results" :key="row.id"><strong>{{ row.name }}</strong> · {{ row.role === 'primary' ? '主图系列' : '变体系列' }} · {{ row.version_count }} 个版本<details><summary>系列标识</summary><code>{{ row.id }}</code></details></li></ul><p v-if="!relations.results.length">本页没有包含此素材的系列。</p><nav aria-label="素材关系分页"><button class="button ghost" :disabled="loading || !relations.previous" @click="loadRelations(page-1)">上一页</button><button class="button ghost" :disabled="loading || !relations.next" @click="loadRelations(page+1)">下一页</button></nav></template>
    </section>
    <details><summary>技术标识与完整性</summary><dl><dt>素材 ID</dt><dd><code>{{ asset.id }}</code></dd><dt>文件摘要</dt><dd><code>{{ asset.checksum || '未提供' }}</code></dd><dt>素材信息摘要</dt><dd><code>{{ asset.metadataSha256 || '未提供' }}</code></dd></dl><p class="help">摘要来自服务端记录；显示摘要不等于本次重新校验了文件。</p></details>
  </section>
</template>
<style scoped>
.asset-facts{display:grid;gap:var(--config-space-xl);font:var(--config-body-size)/1.6 var(--font);min-width:0}.asset-facts section{display:grid;gap:var(--config-space-sm);border-top:1px solid var(--border);padding-top:var(--config-space-lg)}.asset-facts h3{font-size:var(--config-section-size);font-weight:600}.asset-facts p,.asset-facts dd,.asset-facts li{overflow-wrap:anywhere}.asset-facts dl{display:grid;grid-template-columns:100px minmax(0,1fr);gap:8px}.asset-facts dd{margin:0}.asset-facts dt,.asset-facts .help{color:var(--text-secondary);font-size:var(--config-help-size)}.asset-facts nav{display:flex;gap:8px;flex-wrap:wrap}.asset-facts summary{cursor:pointer}.asset-facts .button{font-size:var(--config-body-size);min-height:var(--config-control-height)}.asset-facts [role=alert]{color:var(--red)}.asset-facts ul{padding-left:20px}@media(max-width:640px){.asset-facts{--config-control-height:44px}}
</style>
