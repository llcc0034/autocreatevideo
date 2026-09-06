<script setup lang="ts">
import { computed, ref } from 'vue'
import type { WorkflowResolutionOptions } from '@/services/platformTypes'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { resolutionPolicyErrors } from '@/services/workflowResolution'

const props = defineProps<{ contract?: Record<string, unknown>; options?: WorkflowResolutionOptions }>()
const fetched = ref<WorkflowResolutionOptions>(); const loading = ref(false); const error = ref('')
const options = computed(() => props.options || fetched.value)
const strategy = computed(() => {
  const value = props.contract?.resolution_strategy
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
})
const configured = computed(() => strategy.value?.adapter === options.value?.configuredAdapter)
const sizes = computed(() => configured.value ? strategy.value?.sizes : options.value?.nativeSizes)
const native = computed(() => strategy.value?.adapter === options.value?.nativeAdapter)
const evidenceError = computed(() => {
  const value = strategy.value
  if (!value || !options.value) return ''
  if (!native.value && !configured.value) return '此规则未注册为当前可展示的图片缩放规则；不能据此推断目标尺寸。'
  if (!Array.isArray(value.asset_semantic_keys) || value.asset_semantic_keys.length !== 1
    || !Array.isArray(value.node_ids) || value.node_ids.length !== 1
    || typeof value.source_node_id !== 'string' || typeof value.primary_node_id !== 'string'
    || !Array.isArray(value.size_path) || !value.size_path.length) return '冻结的尺寸来源证据不完整，请重新生成合同草稿。'
  return configured.value ? resolutionPolicyErrors({adapter:value.adapter,sizes:value.sizes},options.value).join('；') : ''
})
const readCatalog = async () => {
  if (loading.value || props.options) return
  loading.value = true; error.value = ''
  try { fetched.value = (await platformApi.getWorkflowProjectorOptions()).imageResolution }
  catch (cause) { error.value = apiErrorSummary(cause) }
  finally { loading.value = false }
}
const toggle = (event: Event) => { if ((event.target as HTMLDetailsElement).open && !options.value) void readCatalog() }
const list = (value: unknown) => Array.isArray(value) ? value.map(String).join(' → ') : '未记录'
</script>

<template>
  <details v-if="strategy?.mode==='source_image_derived'" class="resolution-inspector" @toggle="toggle">
    <summary>查看尺寸规则与来源</summary>
    <p v-if="loading" role="status">正在读取尺寸规则目录…</p>
    <div v-else-if="error&&!options" role="alert"><p class="platform-inline-error">{{ error }}</p><button type="button" class="button ghost small" @click="readCatalog">重新读取规则</button></div>
    <template v-else-if="options">
      <p v-if="evidenceError" role="alert">{{ evidenceError }}</p>
      <template v-else>
        <dl><div><dt>目标尺寸来源</dt><dd>{{ configured?'Projector 版本配置':'原生 FluxKontextImageScale 规则' }}</dd></div><div><dt>执行算法（只读）</dt><dd><code>{{ strategy.adapter }}</code></dd></div><div><dt>决定尺寸的素材槽</dt><dd>{{ list(strategy.asset_semantic_keys) }}</dd></div><div><dt>来源图片 / 缩放 / 主输出 API 节点</dt><dd>{{ strategy.source_node_id }} / {{ list(strategy.node_ids) }} / {{ strategy.primary_node_id }}</dd></div></dl>
        <p>{{ options.algorithm }}</p>
        <p>候选尺寸（px）：<span v-for="(size,index) in sizes as Array<{width:number;height:number}>" :key="index">{{ index?'、':'' }}{{ size.width }} × {{ size.height }}</span></p>
        <p>主输出 → 来源图片的尺寸链（子图已展开的 API 节点 ID）：{{ list(strategy.size_path) }}</p>
        <p>{{ configured?'执行时将尺寸链上的缩放节点编译为 ImageScale，使用以上冻结配置。':'执行时保留原生缩放节点。' }}实际参考素材在运行时选择；真实宽高及最终输出尺寸记入运行证据，产物尺寸不符会失败。</p>
        <p>调整入口：Projector 新建或后继草稿 → 执行引擎与接线 → 图片输出尺寸。保存并发布后，重新生成本工作流合同、提升新版本并重新验收；不会修改历史版本。</p>
      </template>
    </template>
    <details><summary>冻结尺寸合同 JSON</summary><pre>{{ JSON.stringify(strategy,null,2) }}</pre></details>
  </details>
</template>

<style scoped>
.resolution-inspector{min-width:0;padding:10px 12px;border:1px solid #303b46;border-radius:6px}.resolution-inspector summary{cursor:pointer;min-height:36px;display:list-item;align-content:center}.resolution-inspector p{line-height:1.6;overflow-wrap:anywhere}.resolution-inspector dl{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.resolution-inspector dt{color:#96a4b2;font-size:12px}.resolution-inspector dd{margin:5px 0;overflow-wrap:anywhere}.resolution-inspector pre{overflow:auto;max-height:300px;white-space:pre-wrap;overflow-wrap:anywhere}@media(max-width:700px){.resolution-inspector dl{grid-template-columns:1fr}}
</style>
