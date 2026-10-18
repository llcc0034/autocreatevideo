<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { WorkflowProjectorVersion } from '@/services/platformTypes'
import { frozenAssetQuantity, useWorkflowProjectorContext } from '@/services/workflowProjectorContext'
import { isJsonObject } from '@/services/workflowProjectorDefinition'
import { assetGroupForSlot } from '@/services/workflowAssetContractSource'
import { mediaKindLabel, operationLabel } from '@/services/presentationLabels'
import RegistryResourceExplorer from './RegistryResourceExplorer.vue'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import WorkflowPublicContract from './WorkflowPublicContract.vue'

const props = defineProps<{ version: WorkflowProjectorVersion; readOnly: boolean; workflowId?: string }>()
const { context, reload } = useWorkflowProjectorContext(() => ({
  workflowId: props.readOnly ? props.workflowId : undefined, projector: props.version,
}))
const visited = ref(false)
watch(() => [props.version.id, props.readOnly], () => { visited.value = false })
const definition = computed(() => props.version.definition)
const slots = computed(() => Array.isArray(definition.value.asset_slots) && definition.value.asset_slots.every(isJsonObject) ? definition.value.asset_slots : null)
const parameters = computed(() => Array.isArray(definition.value.parameter_policies) && definition.value.parameter_policies.every(isJsonObject) ? definition.value.parameter_policies : null)
const config = computed(() => isJsonObject(definition.value.projection) && isJsonObject(definition.value.projection.config) ? definition.value.projection.config : {})
// Display saved declarations only. No model resolution, defaulting or graph compilation.
const quantity = (slot: Record<string, unknown>) => {
  const group = assetGroupForSlot(definition.value, slot.key)
  if (!group) return null
  return slot.contract_source === 'model_profile'
    ? { '模型模式': group.conditioning_mode, '模型输入键': group.model_input }
    : { '本方案下限': group.minimum_items, '本方案上限': group.maximum_items }
}
</script>

<template>
  <slot v-if="!readOnly" />
  <RegistryResourceExplorer v-else :key="version.id" label="Projector 概览" v-slot="{inspect}">
    <div class="projector-presentation">
      <header><h3>工作流解释方案 · 输入如何成为可调用合同</h3><p>解释节点图、映射输入并声明输出，不是一次 AI 调用，也不归某个模型独占。以下为精确版本 v{{ version.version }}；新版本不会自动替换现有工作流。</p></header>
      <section aria-label="Projector 工作流上下文">
        <h4>此工作流如何使用它</h4>
        <p v-if="context.state==='none'">当前从公共方案目录查看，未指定工作流。下面是方案规则，不是某条工作流的冻结执行范围。</p>
        <p v-else-if="context.state==='loading'" role="status">正在核对工作流冻结引用与模型版本…</p>
        <div v-else-if="context.state==='error'" role="alert"><p>{{ context.message }}。未采用其他版本，方案定义仍可只读查看。</p><button type="button" class="button ghost small" @click="reload">重新核对工作流上下文</button></div>
        <template v-else-if="context.state==='ready'">
          <p>{{ context.workflow.templateName }} v{{ context.workflow.version }} → 本 Projector v{{ version.version }} → 冻结输入与输出合同</p>
          <p>绑定模型：{{ context.model.profileKey }} v{{ context.model.version }}。模型是此工作流的能力来源，不是 Projector 的独占依赖。</p>
          <button type="button" class="button ghost small" @click="inspect('model',context.model.id,$event)">查看绑定模型能力 · v{{ context.model.version }}</button>
          <details><summary>查看此工作流的冻结输入、参数与输出</summary><WorkflowPublicContract :workflow="context.workflow" /></details>
        </template>
      </section>
      <section aria-label="Projector 素材与文本规则"><h4>方案声明的输入</h4>
        <p>素材在任务中提供；这里配置的是读取与映射规则。模型来源的数量需结合工作流冻结合同，不在查看时重新计算。</p>
        <p v-if="!slots" role="alert">未返回素材槽声明或条目格式异常，不能解释为不需要素材。</p><p v-else-if="!slots.length">此版本未声明素材槽。</p>
        <div class="projector-parameter-grid"><article v-for="(slot,index) in slots" :key="index"><strong>{{ slot.key }} · {{ mediaKindLabel(String(slot.media_kind)) }}</strong><dl><div><dt>数量来源</dt><dd>{{ slot.contract_source==='model_profile'?'绑定模型数量声明':slot.contract_source==='workflow'?'Projector 中配置':slot.contract_source }}</dd></div><div><dt>动态素材</dt><dd><ReadonlyDataValue :value="slot.dynamic" /></dd></div><div v-for="(value,label) in quantity(slot)" :key="label"><dt>{{ label }}</dt><dd><ReadonlyDataValue :value="value" /></dd></div><div v-if="frozenAssetQuantity(context,slot.key)"><dt>此工作流已冻结数量</dt><dd>{{ frozenAssetQuantity(context,slot.key)?.minimum }}–{{ frozenAssetQuantity(context,slot.key)?.maximum }} 份</dd></div></dl><p v-if="slot.contract_source==='model_profile'">本方案不重复填写模型范围；显示的冻结数量只取自此工作流，不用当前模型重新计算。</p><p v-if="!quantity(slot)">未声明可识别的动态数量组；固定槽数量以生成后的工作流合同为准。</p></article></div>
        <dl><div><dt>能力规则</dt><dd>{{ typeof config.capability==='string' ? operationLabel(config.capability) : '未在通用投影配置中声明' }}</dd></div><div><dt>提示词模板引用</dt><dd><ReadonlyDataValue :value="config.prompt_template_keys" label="提示词模板引用" /></dd></div><template v-if="isJsonObject(config.static_semantics)"><div v-for="(value,key) in config.static_semantics" :key="key"><dt>固定语义 · {{ key }}</dt><dd><ReadonlyDataValue :value="value" /></dd></div></template></dl>
      </section>
      <section aria-label="Projector 参数规则"><h4>参数如何提供</h4><p>运行时输入、固定值与节点目录约束按本版本声明区分；完整生效范围以生成后的工作流合同为准。</p>
        <p v-if="!parameters" role="alert">未返回参数规则或条目格式异常，不能推断节点参数可调。</p><p v-else-if="!parameters.length">此版本未声明参数策略。</p>
        <div class="projector-parameter-grid"><article v-for="(row,index) in parameters" :key="index"><strong>{{ row.semantic_key }}</strong><dl><div><dt>提供规则</dt><dd>{{ row.policy==='fixed'?'固定值':row.policy==='runtime'?'运行时可调':row.policy==='object_info'?'节点目录约束':row.policy }}</dd></div><div v-if="row.policy==='fixed'"><dt>固定值</dt><dd><ReadonlyDataValue :value="row.value" /></dd></div><template v-else-if="isJsonObject(row.constraints)"><div v-for="(value,key) in row.constraints" :key="key"><dt>{{ key }}</dt><dd><ReadonlyDataValue :value="value" /></dd></div></template></dl><p v-if="row.policy!=='fixed' && row.constraints===undefined">此方案未另设范围；具体输入约束查看生成后的工作流合同。</p></article></div>
      </section>
      <section aria-label="Projector 输出规则"><h4>输出与使用方</h4><p>固定产物类型：{{ version.outputKind ? mediaKindLabel(version.outputKind) : '由识别到的主输出节点决定' }}。这不是已生成的媒体。</p><p>工作流生成合同时冻结此版本及定义 SHA；调整应创建后继草稿，再显式生成新的工作流版本。</p><button type="button" class="button ghost small" @click="inspect('projector',version.id,$event)">查看方案定义与使用关系</button></section>
      <details @toggle="visited ||= ($event.target as HTMLDetailsElement).open"><summary>实现详情 · 节点、接线与完整配置（只读）</summary><slot v-if="visited" :workflow-context="context" :reload-context="reload" /></details>
    </div>
  </RegistryResourceExplorer>
</template>

<style scoped>
.projector-presentation{min-width:0;display:grid;gap:var(--config-space-lg);font:var(--config-body-size)/1.65 var(--font);letter-spacing:0;overflow-wrap:anywhere}.projector-presentation h3{font-size:var(--config-section-size);font-weight:600;margin:0}.projector-presentation h4{font-size:var(--config-body-size);font-weight:600;margin:0}.projector-presentation p{margin:4px 0;color:var(--text-secondary)}.projector-presentation>section{border-top:1px solid var(--border);padding-top:var(--config-space-lg)}.projector-presentation article{min-width:0;margin:12px 0}.projector-parameter-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:16px}.projector-presentation dl{display:grid;gap:12px;margin:12px 0}.projector-presentation dt{font-weight:600}.projector-presentation dd{margin:0}.projector-presentation summary{min-height:36px;cursor:pointer;padding:8px 0;font-weight:600}.projector-presentation .button{min-height:36px;white-space:normal;max-width:100%}.projector-presentation [role=alert]{color:var(--red)}.projector-presentation :focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:640px){.projector-presentation summary,.projector-presentation .button{min-height:44px}}
</style>
