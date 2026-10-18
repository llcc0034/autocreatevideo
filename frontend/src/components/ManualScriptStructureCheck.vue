<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import type { ManualActionOptions, ManualScriptStructureCheck } from '@/services/productionManual'

const props = defineProps<{ runId: string; options: ManualActionOptions | null }>()
const result = ref<ManualScriptStructureCheck | null>(null), busy = ref(false), error = ref('')
let request = 0
const basis = (value: Pick<ManualActionOptions, 'runId' | 'currentScript' | 'currentScriptDecisionId'>) =>
  JSON.stringify([value.runId, value.currentScript?.id ?? null, value.currentScript?.sha256 ?? null, value.currentScriptDecisionId])
const stale = computed(() => Boolean(result.value && props.options && basis(result.value) !== basis(props.options)))
const current = computed(() => Boolean(result.value && props.options && !stale.value))
function reset() { request++; result.value = null; error.value = ''; busy.value = false }
watch(() => props.runId, reset)
onBeforeUnmount(reset)
async function check() {
  if (busy.value || !props.options || props.options.runId !== props.runId) return
  const serial = ++request, runId = props.runId
  busy.value = true; error.value = ''; result.value = null
  try {
    const checked = await platformApi.getManualScriptStructure(runId)
    if (serial !== request) return
    if (checked.runId !== runId) throw new Error('检查结果不属于当前轮次，请重新读取')
    result.value = checked
  } catch (cause) { if (serial === request) error.value = apiErrorSummary(cause) }
  finally { if (serial === request) busy.value = false }
}
</script>

<template>
  <section class="script-structure-check" aria-label="已采用剧本结构检查">
    <button type="button" class="button ghost small" :disabled="busy || !options" @click="check">{{ busy ? '正在核对已保存结构…' : '检查已采用剧本结构（不调用模型）' }}</button>
    <p v-if="error" role="alert">检查未完成：{{ error }}</p>
    <p v-else-if="!result && !busy">“剧本结构化”步骤仍在等待，不代表必须再执行。检查本次采用的修订是否已有结构化结果。</p>
    <p v-if="stale" role="status">本轮采用依据已变化，下面的旧检查不能用于当前剧本；请重新检查。</p>
    <p v-else-if="result && !options" role="status">当前采用状态尚未读取；下面仅保留上次检查依据，暂不能确认仍适用。</p>
    <div v-if="result" :class="{ 'stale-check': stale }">
      <p v-if="current" role="status">{{ result.reason }}</p>
      <p v-if="result.status === 'structured' && current">已保存 {{ result.episodeCount }} 集、{{ result.shotCount }} 个镜头的结构化声明。数量为 0 表示尚无对应声明，不代表分镜已创建；无需仅因步骤等待而重复解析。</p>
      <p v-if="result.status === 'unstructured' && current">可在剧本来源中为新修订选择输出结构并保存，或按需生成结构化候选后采用。仅有 JSON 或旧文本标记，不等于已保存字段映射结果；也可手动创建分镜。</p>
      <p v-if="result.currentScript">检查依据：修订 {{ result.currentScript.id }} · 采用决定 {{ result.currentScriptDecisionId }}</p>
      <details v-if="result.currentScript"><summary>内容指纹与检查说明</summary><p>SHA-256：{{ result.currentScript.sha256 }}</p><p v-if="result.reasonCode">{{ result.reasonCode }}：{{ result.reason }}</p></details>
      <p>只核对本次采用的原结构化结果，不重新解析原文、不创建 Bible 或分镜、不改变步骤状态，也不代表内容质量或下游已通过验收。</p>
    </div>
  </section>
</template>

<style scoped>
.script-structure-check{display:grid;gap:10px;min-width:0;padding:12px;border:1px solid var(--border-color,#30363d);border-radius:10px}.script-structure-check p{margin:8px 0;overflow-wrap:anywhere}.script-structure-check .button{justify-self:start;white-space:normal;text-align:left}.stale-check{opacity:.8}
</style>
