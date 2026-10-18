<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import type { ExecutionParameterMappings } from '@/services/executionParameterMappings'
const props = defineProps<{ versionId: string; revision: number; sha256: string }>()
const expanded = ref(false), loading = ref(false), error = ref('')
const result = ref<ExecutionParameterMappings | null>(null)
let serial = 0
watch(() => [props.versionId, props.revision, props.sha256], () => { serial++; result.value = null; error.value = ''; loading.value = false })
onBeforeUnmount(() => { serial++ })
async function load() {
  if (loading.value) return
  const request = ++serial, id = props.versionId, revision = props.revision, hash = props.sha256
  loading.value = true; error.value = ''; result.value = null
  try {
    const data = await executionRegistryApi.parameterMappings(id, hash)
    if (request !== serial) return
    if (data.version_id !== id || data.revision !== revision || data.definition_sha256 !== hash) throw new Error('实例修订或合同已变化，请重新复核完整合同。')
    result.value = data
  } catch (e) { if (request === serial) error.value = apiErrorSummary(e) }
  finally { if (request === serial) loading.value = false }
}
</script>

<template>
  <details class="parameter-mappings" @toggle="expanded=($event.target as HTMLDetailsElement).open">
    <summary>参数目标映射 · 工作流节点 / API 字段</summary>
    <div v-if="expanded" class="mapping-body">
      <p>只读查询本合同的标量参数声明目标；不是新配置入口，不含素材动态接线、提示词或服务器内部参数，也不代表实际调用成功。</p>
      <button type="button" class="button ghost small" :disabled="loading" @click="load">{{ loading?'读取中…':result?'重新核对映射':'读取参数目标映射' }}</button>
      <p v-if="error" role="alert" class="mapping-error">{{ error }}</p>
      <template v-if="result">
        <p role="status">{{ result.message }}</p>
        <p v-if="result.basis_id">{{ result.basis_kind==='workflow'?'工作流版本':'部署' }}：<code>{{ result.basis_id }}</code></p>
        <details><summary>核对凭据</summary><p>状态：{{ result.code }}</p><p>定义 SHA：<code>{{ result.definition_sha256 }}</code></p><p v-if="result.basis_sha256">映射依据 SHA：<code>{{ result.basis_sha256 }}</code></p></details>
        <template v-if="result.status==='verified'">
          <p v-if="!result.rows.length">此合同明确没有可调标量参数，未推断其他字段的目标。</p>
          <dl v-else class="mapping-rows">
            <div v-for="row in result.rows" :key="row.key"><dt>{{ row.key }}</dt><dd>
              <p v-if="!row.destinations.length" class="mapping-error">未记录此参数的声明目标，不能按名称推断。</p>
              <ul v-else><li v-for="destination in row.destinations" :key="JSON.stringify(destination)"><template v-if="destination.node_id!==null">节点 <code>{{ destination.node_id }}</code> → 输入 </template><template v-else>JSON 顶层字段 </template><code>{{ destination.field }}</code></li></ul>
            </dd></div>
          </dl>
        </template>
      </template>
      <p v-else-if="!loading&&!error">尚未查询；展开不会创建准备、运行烟测或调用模型。</p>
    </div>
  </details>
</template>

<style scoped>
.parameter-mappings{border-top:1px solid var(--border);min-width:0;font-size:12px;line-height:1.7}.parameter-mappings summary{cursor:pointer;min-height:36px;align-content:center;overflow-wrap:anywhere}.parameter-mappings summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.mapping-body{display:grid;gap:10px;min-width:0}.mapping-body p{margin:0;overflow-wrap:anywhere;color:var(--text-secondary)}.mapping-body code{overflow-wrap:anywhere}.mapping-body button{justify-self:start}.mapping-body .mapping-error{color:var(--danger)}.mapping-rows{margin:0}.mapping-rows>div{display:grid;grid-template-columns:minmax(120px,1fr) minmax(0,2fr);gap:12px;border-top:1px solid var(--border);padding:8px 0}.mapping-rows dt{font-weight:600;overflow-wrap:anywhere}.mapping-rows dd{margin:0;min-width:0}.mapping-rows ul{margin:0;padding-left:18px;overflow-wrap:anywhere}@media(max-width:600px){.mapping-rows>div{grid-template-columns:1fr;gap:4px}}
</style>
