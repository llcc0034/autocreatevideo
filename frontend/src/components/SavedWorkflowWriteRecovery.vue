<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { SavedWorkflowPendingImport } from '@/services/savedWorkflowRecovery'
import type { ComfySavedWorkflowPreview } from '@/services/platformTypes'
import WorkflowPublicContract from '@/components/WorkflowPublicContract.vue'
import ReadonlyDataValue from '@/components/ReadonlyDataValue.vue'
const props = defineProps<{
  pending: SavedWorkflowPendingImport[]
  results: Array<{ id: string; path: string; detail: string; versionId: string; preview?: ComfySavedWorkflowPreview }>
  error: string
  busy: boolean
  disabled: boolean
}>()
const emit = defineEmits<{ readback: [] }>()
const panel = ref<HTMLElement | null>(null)
let requested = false
function readback() { requested = true; emit('readback') }
watch(() => props.busy, async (busy, previous) => {
  if (!busy && previous && requested) {
    requested = false
    await nextTick()
    // Completion removes the read button. Keep keyboard users at the actual
    // result instead of losing focus to the document body.
    panel.value?.focus()
  }
})
</script>
<template>
  <section v-if="pending.length || results.length || error" ref="panel" tabindex="-1" class="workflow-import-recovery" aria-label="核对未确认的工作流导入" :aria-busy="busy">
    <h3>核对未确认的工作流导入</h3>
    <p>保留 {{ pending.length }} 项原请求。这里只读取平台已保存的来源、原请求回执与完整合同，不扫描 ComfyUI、不生成合同、不重新提交。</p>
    <p v-if="error" class="platform-inline-error" role="alert">{{ error }}</p>
    <p v-if="busy" role="status">正在逐项核对原来源、合同草稿、前驱及保存去向…</p>
    <ul v-if="pending.length">
      <li v-for="item in pending" :key="item.id">
        <strong>{{ item.input.sourcePath }}</strong> · {{ item.contractRequest ? '合同草稿生成' : '来源登记／版本提升' }} · 保存结果待核对
        <details><summary>原请求身份与去向</summary>
          <dl><dt>提交时间</dt><dd>{{ item.startedAt }}</dd><dt>原部署</dt><dd>{{ item.deploymentId }}</dd>
            <dt>合同草稿</dt><dd>{{ item.contractRequest ? '回执未确认，不按最新版本认领' : item.input.contractDraft || '不适用：仅登记来源' }}</dd>
            <template v-if="item.contractRequest"><dt>不可变候选</dt><dd>{{ item.contractRequest.candidateSourceId }}</dd><dt>原主输出</dt><dd>{{ item.input.primaryOutputNodeIds?.join('、') }}</dd><dt>原模型版本</dt><dd>{{ item.input.modelProfileVersion || '未选择' }}</dd></template>
            <dt>原前驱</dt><dd>{{ item.input.successorOf || '新导入，不是后继' }}</dd>
            <dt>保存去向</dt><dd>{{ item.contractRequest ? '平台执行合同草稿，尚未提升工作流版本' : item.input.template || item.input.newTemplate?.name || item.input.importKind }}</dd>
            <dt>源文件校验值</dt><dd>{{ item.input.expectedSha256 }}</dd></dl>
        </details>
      </li>
    </ul>
    <ul v-if="results.length" aria-live="polite"><li v-for="row in results" :key="row.id"><strong>{{ row.path }}</strong><p>{{ row.detail }}</p>
      <details v-if="row.preview"><summary>查看已核对的原合同 · revision {{ row.preview.contractDraftRevision }}</summary>
        <WorkflowPublicContract :preview="row.preview" />
        <ReadonlyDataValue label="原合同完整输入约定" :value="row.preview.suggestedInputContract" />
      </details>
      <RouterLink v-if="row.versionId" :to="`/settings/workflows/${row.versionId}`" target="_blank" rel="noopener">查看已核对版本（新窗口）</RouterLink>
    </li></ul>
    <button v-if="pending.length || error" type="button" class="button ghost small" :disabled="disabled || busy" @click="readback">{{ busy ? '正在核对原请求…' : '只读核对原请求' }}</button>
    <p v-if="pending.length || error" class="configuration-help">刷新后仍保留未确认请求；这些来源暂停重复生成合同、登记或提升。其他来源可继续配置，未提交批次需由你明确提交。</p>
    <p v-else class="configuration-help">原请求核对完成。只恢复上方已经核对的记录；其他未提交项目不会自动保存。</p>
  </section>
</template>
<style scoped>
.workflow-import-recovery { border-block: 1px solid var(--border); padding: var(--config-space-lg) 0; overflow-wrap: anywhere; font-family:var(--font); font-size:var(--config-body-size); font-weight:400; line-height:1.6; }
h3 { font-size:var(--config-section-size); font-weight:600; margin:0 0 var(--config-space-sm); }
ul { padding-left:20px; } li { margin:var(--config-space-md) 0; } p { margin:var(--config-space-sm) 0; }
details,.configuration-help { font-size:var(--config-help-size); } dl { display:grid; grid-template-columns:minmax(80px,auto) 1fr; gap:var(--config-space-sm); } dd { margin:0; min-width:0; }
summary { min-height:var(--config-control-height); align-content:center; cursor:pointer; font-size:var(--config-body-size); }
summary:focus-visible,.workflow-import-recovery:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
button { min-height:var(--config-control-height); font-size:var(--config-body-size); white-space:normal; }
@media(max-width: 640px) { .workflow-import-recovery { --config-control-height:44px; } dl { grid-template-columns: 1fr; } }
</style>
