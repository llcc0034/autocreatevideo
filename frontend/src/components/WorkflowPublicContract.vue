<script setup lang="ts">
import { computed } from 'vue'
import type { ComfySavedWorkflowPreview, WorkflowTemplateVersion } from '@/services/platformTypes'
import ReadonlyDataValue from '@/components/ReadonlyDataValue.vue'
import { mediaKindLabel } from '@/services/presentationLabels'

// Two existing server DTOs, not a new compiler or client-side contract resolver.
const props = defineProps<{ workflow?: WorkflowTemplateVersion; preview?: ComfySavedWorkflowPreview }>()
const schema = computed(() => props.workflow?.workflowInputSchema)
const unavailable = computed(() => props.workflow
  ? props.workflow.workflowInputSchemaError || (!schema.value ? '未返回冻结输入说明' : !schema.value.available ? `输入说明不可核验：${schema.value.reasonCode}` : '')
  : !props.preview ? '尚未生成合同草稿' : '')
const parameters = computed(() => unavailable.value ? [] : props.workflow ? schema.value!.parameters : props.preview!.configurableParameters)
const slots = computed(() => unavailable.value ? [] : props.workflow ? schema.value!.assetSlots : props.preview!.assetDependencies.map(row => ({
  semanticKey: row.suggestedSemanticKey, mediaKind: row.mediaKind, inputSlot: row.inputSlot,
  minItems: row.minItems, maxItems: row.maxItems, required: row.isRequired,
})))
const modes = computed(() => {
  if (unavailable.value || !schema.value) return []
  const rows = new Map(Object.entries(schema.value.dynamicReferenceModes))
  if (schema.value.dynamicReferences) rows.set(schema.value.dynamicReferences.mode || 'default', schema.value.dynamicReferences)
  return [...rows].map(([key, value]) => ({ key, value }))
})
const selectedOutputs = computed(() => (props.preview?.primaryOutputNodeIds || []).map(id => ({
  id, candidate: props.preview?.outputCandidates.find(row => row.canvasNodeId === id),
})))
</script>

<template>
  <section class="workflow-public-contract" aria-label="工作流对外输入与输出">
    <header><h3>对外输入、可调参数与输出</h3><p>{{ workflow ? '读取此工作流版本的冻结声明' : '读取当前服务端合同草稿，不代表已保存为工作流版本' }}。具体素材和参数值由执行实例及本次任务按合同提供；此处不执行模型。</p></header>
    <p v-if="unavailable" role="alert">{{ unavailable }}；不能解释为没有输入。</p>
    <template v-else>
      <section aria-label="工作流素材输入"><h4>素材输入</h4>
        <ul v-if="slots.length"><li v-for="(row,index) in slots" :key="`${row.semanticKey}:${index}`"><strong>{{ row.semanticKey }}</strong> · {{ mediaKindLabel(row.mediaKind) }} · {{ row.required ? '必填' : '可选' }} · {{ row.minItems }}–{{ row.maxItems }} 个<small>输入槽位 {{ row.inputSlot }}；具体素材尚由调用方提供，不在工作流版本内选图。</small></li></ul>
        <p v-else>当前{{ workflow ? '冻结输入说明' : '草稿识别结果' }}未声明固定素材槽。</p>
        <article v-for="row in modes" :key="row.key"><strong>动态参考 · {{ row.key }}</strong><p>图片 {{ row.value.pictures.minItems }}–{{ row.value.pictures.maxItems }}；视频 {{ row.value.videos.minItems }}–{{ row.value.videos.maxItems }}；音频 {{ row.value.audios.minItems }}–{{ row.value.audios.maxItems }}；最多 {{ row.value.maximumPhysicalAssets }} 个实际素材。</p><ReadonlyDataValue label="参考类别、用途及配套音频约束" :value="row.value" /></article>
        <ReadonlyDataValue v-if="schema?.assetGroups?.length" label="素材组合约束" :value="schema.assetGroups" />
        <p v-if="preview">动态输入规则以服务端草稿合同为准；未提升版本前不假装已取得冻结输入说明。</p>
      </section>
      <section aria-label="工作流可调参数"><h4>可调参数</h4><p>只列已声明项；范围、枚举和对齐条件同时生效。建议值不是本次任务已填值，也不等同默认值。</p>
        <div class="workflow-parameter-list"><article v-for="(row,index) in parameters" :key="`${row.semanticKey}:${index}`"><strong>{{ row.semanticKey }}</strong><span>{{ row.type }}<template v-if="'required' in row"> · {{ row.required ? '必填' : '可选' }}</template></span>
          <dl><div v-if="row.min !== null || row.max !== null"><dt>范围</dt><dd>{{ row.min ?? '未声明下限' }} – {{ row.max ?? '未声明上限' }}</dd></div><div v-if="row.multipleOf !== null"><dt>倍数要求</dt><dd>{{ row.multipleOf }}</dd></div><div v-if="row.alignmentModulus !== null"><dt>对齐要求</dt><dd>{{ row.alignmentRemainder ?? '未声明余数' }} + {{ row.alignmentModulus }}k</dd></div><div v-if="row.options.length"><dt>允许值</dt><dd><ReadonlyDataValue :value="row.options" /></dd></div><div v-if="row.selectionGroup"><dt>联动组</dt><dd>{{ row.selectionGroup }}</dd></div><div v-if="'minLength' in row && row.minLength != null"><dt>最小长度</dt><dd>{{ row.minLength }}</dd></div><div v-if="'maxLength' in row && row.maxLength != null"><dt>最大长度</dt><dd>{{ row.maxLength }}</dd></div></dl>
          <template v-if="'sourceValue' in row"><dl><div><dt>画布原值</dt><dd><ReadonlyDataValue :value="row.sourceValue" /></dd></div><div><dt>节点目录默认值</dt><dd><ReadonlyDataValue :value="row.defaultValue" /></dd></div><div><dt>服务端建议值</dt><dd><ReadonlyDataValue :value="row.recommendedSafeValue" /></dd></div></dl></template>
          <template v-else><span>合同建议值</span><ReadonlyDataValue :value="row.recommendedValue" /></template>
        </article></div><p v-if="!parameters.length">当前说明未列出可调参数；不推断节点中的其他值允许修改。</p>
      </section>
    </template>
    <section aria-label="工作流输出"><h4>输出与下游使用</h4>
      <p v-if="workflow">声明产物：{{ workflow.outputKind ? mediaKindLabel(workflow.outputKind) : '未声明输出类型' }}。由引用此精确版本的执行实例调用；配方或素材任务使用其结果。</p>
      <template v-else-if="preview"><p v-if="!selectedOutputs.length">尚未明确主输出；不会自动选择第一个候选。</p><ul v-else><li v-for="row in selectedOutputs" :key="row.id">主输出 {{ row.id }} · {{ row.candidate?.suggestedOutputKind ? mediaKindLabel(row.candidate.suggestedOutputKind) : '媒体类型待识别' }}<small v-if="!row.candidate">此主输出未在返回目录中找到，不能用其他候选替代。</small></li></ul><p>输出候选不是已生成的媒体；节点解析、识别阻断和提升结果仍以原服务端回执为准。</p></template>
      <p v-else>输出合同尚未读取。</p>
    </section>
  </section>
</template>

<style scoped>
.workflow-public-contract{display:grid;gap:var(--config-space-lg);min-width:0;font-family:var(--font);font-size:var(--config-body-size);line-height:1.6;letter-spacing:0}header,section,article{min-width:0}header h3{font-size:var(--config-section-size);font-weight:600}h4,strong{font-size:var(--config-body-size);font-weight:600}p,small,span,dt{color:var(--text-secondary)}p{margin:4px 0}small{display:block;font-size:var(--config-help-size)}section{border-top:1px solid var(--border);padding-top:var(--config-space-md)}ul{padding-left:20px}li{margin:8px 0}p,li,dd,strong,span{overflow-wrap:anywhere}.workflow-parameter-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:var(--config-space-lg)}article{display:grid;align-content:start;gap:8px;padding:12px 0;border-bottom:1px solid var(--border)}dl{margin:0;display:grid;gap:6px}dl>div{display:grid;grid-template-columns:110px minmax(0,1fr);gap:8px}dd{margin:0;min-width:0}summary{cursor:pointer;min-height:36px;padding:6px 0}[role=alert]{color:var(--red)}
@media(max-width:640px){summary{min-height:44px}dl>div{grid-template-columns:1fr;gap:2px}}
</style>
