<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { readReviewedVisualSource, type ReviewedVisualSource } from '@/services/executionReviewedVisualSource'
import type { ExecutionRequestDraft } from '@/services/executionRequestForm'

const props = defineProps<{ runId?: string; stageKey?: string; projectId?: string; disabled?: boolean }>()
const model = defineModel<NonNullable<ExecutionRequestDraft['contexts'][string]>>({ required: true })
const emit = defineEmits<{ busy: [value: boolean] }>()
const preview = ref<ReviewedVisualSource | null>(null), busy = ref(false), error = ref('')
let sequence = 0
watch([() => props.runId, () => props.stageKey, () => props.projectId, () => model.value.selectorKey], () => {
  sequence++; preview.value = null; error.value = ''; busy.value = false; emit('busy', false)
  model.value = { ...model.value, projectId: props.projectId || '', revisionId: '' }
})
onBeforeUnmount(() => { sequence++; emit('busy', false) })
async function load() {
  if (props.disabled || busy.value || !props.runId || !props.stageKey || !props.projectId) return
  const token = ++sequence; busy.value = true; emit('busy', true); error.value = ''
  preview.value = null; model.value = { ...model.value, revisionId: '' }
  try {
    const value = await platformApi.getReviewedVisualSources(props.runId, props.stageKey)
    if (token === sequence) preview.value = readReviewedVisualSource(value, props.runId, props.stageKey, props.projectId, model.value.selectorKey)
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) { busy.value = false; emit('busy', false) } }
}
function confirm() {
  if (props.disabled || busy.value || !preview.value) return
  model.value = { ...model.value, projectId: props.projectId!, revisionId: preview.value.decisionId }
}
</script>

<template>
  <div class="reviewed-source">
    <p v-if="!runId || !stageKey">此来源需要项目生产步骤及已审核结果。组合烟测请先选择来源轮次与参考步骤；基础资源烟测不提供生产审核上下文。</p>
    <template v-else>
      <button type="button" class="button ghost small" :disabled="disabled || busy" @click="load">读取本轮已审核规则</button>
      <p v-if="busy" role="status">正在核对原任务与审核版本…</p>
      <p v-if="error" role="alert">{{ error }}</p>
      <template v-if="preview">
        <p>{{ preview.label }} · 审核修订 {{ preview.revision }} · {{ model.revisionId === preview.decisionId ? '已确认引用' : '待确认引用' }}</p>
        <pre aria-label="已审核规则预览">{{ preview.content || '（已审核为空）' }}</pre>
        <details><summary>查看原任务与审核来源</summary><dl><dt>任务</dt><dd>{{ preview.attemptId }}</dd><dt>候选</dt><dd>{{ preview.artifactId }}</dd><dt>审核决定</dt><dd>{{ preview.decisionId }}</dd><dt>输出字段</dt><dd>{{ preview.field }}</dd></dl></details>
        <details><summary>原生产输出合同 · {{ preview.outputVersion.name }} · v{{ preview.outputVersion.version }}</summary><dl><dt>输出版本 ID / SHA-256</dt><dd>{{ preview.outputVersion.id }} / {{ preview.outputVersion.sha256 }}</dd><dt>原响应位置 / 格式</dt><dd>{{ preview.fieldContract.response_path }} / {{ preview.fieldContract.response_format }}</dd><dt>转换规则</dt><dd>{{ preview.fieldContract.transformation }}</dd><dt>精确来源标识</dt><dd>{{ preview.fieldContract.source_selector }}</dd></dl></details>
        <p>引用审核后的这一个字段，不引用完整剧本。保存时重新核对审核版本和项目归属，不自动改用新版本。</p>
        <button type="button" class="button primary small" :disabled="disabled || busy || model.revisionId === preview.decisionId" @click="confirm">确认引用此审核版本</button>
      </template>
    </template>
  </div>
</template>

<style scoped>
.reviewed-source{display:grid;gap:10px;min-width:0}.reviewed-source p{margin:0}.reviewed-source pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:240px;overflow:auto}.reviewed-source dd{margin:0;overflow-wrap:anywhere}.reviewed-source dl{display:grid;gap:6px}
</style>
