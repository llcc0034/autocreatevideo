<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { executionStorageApi, type ExecutionStorageMetrics } from '@/services/executionStorageMetrics'
import { apiErrorSummary } from '@/services/apiErrorPresentation'

const props = defineProps<{ preparationId: string; revision: number; disabled: boolean }>()
const result = ref<ExecutionStorageMetrics | null>(null), loading = ref(false), error = ref(''), opened = ref(false)
let serial = 0
function reset() { serial++; result.value = null; error.value = ''; loading.value = false; opened.value = false }
watch(() => [props.preparationId, props.revision], reset)
onBeforeUnmount(() => { serial++ })
async function load() {
  if (props.disabled || loading.value) return
  const request = ++serial, id = props.preparationId, revision = props.revision
  opened.value = true; loading.value = true; result.value = null; error.value = ''
  try {
    const value = await executionStorageApi.get(id, revision)
    if (request === serial) result.value = value
  } catch (cause) { if (request === serial) error.value = apiErrorSummary(cause) }
  finally { if (request === serial) loading.value = false }
}
</script>

<template>
  <section class="storage-metrics" aria-label="准备字节明细">
    <button type="button" class="button ghost" :disabled="disabled || loading" :aria-expanded="opened" @click="opened ? reset() : load()">
      {{ loading ? '正在读取字节明细…' : opened ? '收起准备字节明细' : '查看准备字节明细' }}
    </button>
    <template v-if="opened">
      <p v-if="loading" role="status">正在读取当前修订的原始存储元数据…</p>
      <div v-if="error" class="platform-alert danger" role="alert">{{ error }}<button type="button" class="button ghost small" :disabled="disabled || loading" @click="load">重新读取字节明细</button></div>
      <template v-if="result">
        <p>准备修订 {{ result.preparationRevision }} · 读取时间 {{ new Date(result.observedAt).toLocaleString() }}</p>
        <p>仅读取保存时记录的字节元数据，不加载正文。JSON 按规范化 UTF-8 计数，不是数据库物理占用；请求文件按原清单记录，不代表本次已校验文件。</p>
        <p>上限为当前配置，不是历史冻结上限。各项可能含相同内容，不合计；不含字段历史，也不重复计算各次调用对同一请求文件的引用。完整正文请使用原“完整提示词与系统组装”入口。</p>
        <ul class="byte-parts">
          <li v-for="item in result.items" :key="item.key">
            <strong>{{ item.label }}</strong>
            <span v-if="item.state === 'not_generated'">未生成 · 无字节记录</span>
            <span v-else>已记录 {{ item.sizeBytes!.toLocaleString() }} 字节</span>
            <span>当前上限 {{ item.currentLimitBytes.toLocaleString() }} 字节</span>
            <span v-if="item.overCurrentLimitBytes" class="over-limit" role="status">比当前上限多 {{ item.overCurrentLimitBytes.toLocaleString() }} 字节；原值未截断</span>
            <details v-if="item.state === 'recorded'"><summary>计数依据与原始摘要</summary>
              <p>{{ item.basis === 'canonical_json_utf8' ? '规范化 JSON UTF-8' : '规范化请求文件 UTF-8' }} · 结构版本 {{ item.schemaVersion }}</p>
              <p>SHA256：{{ item.sha256 }}</p>
            </details>
          </li>
        </ul>
      </template>
    </template>
  </section>
</template>

<style scoped>
.storage-metrics { min-width:0; margin:12px 0; } p { line-height:1.6; overflow-wrap:anywhere; }
.byte-parts { padding:0; list-style:none; display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr)); }
.byte-parts li { min-width:0; padding:12px; border:1px solid var(--border-color,#354149); border-radius:8px; display:flex; flex-direction:column; gap:6px; overflow-wrap:anywhere; }
.over-limit { color:var(--danger,#df7777); } summary { cursor:pointer; min-height:36px; } .button { white-space:normal; }
</style>
