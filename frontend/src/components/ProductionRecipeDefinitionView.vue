<script setup lang="ts">
import { computed } from 'vue'
import type { JsonObject, ProductionRecipeEditorOptions } from '@/services/platformTypes'
import { recipeObject as obj } from '@/services/productionRecipeAuthoring'
import { recipeConfigurationSteps, recipeSourceSummary, recipeOutputSourceSummary } from '@/services/recipeConfigurationPresentation'
import { modeLabel } from '@/services/presentationLabels'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import RegistryResourceExplorer from './RegistryResourceExplorer.vue'
const props = defineProps<{ definition: JsonObject; options?: ProductionRecipeEditorOptions | null }>()
const steps = computed(() => recipeConfigurationSteps(props.definition, props.options))
const outputLabels: Record<string, string> = { assembly_output: '装配输出', assembly_timing: '装配时序', assembly_media: '已采用片段', delivery_metadata: '交付标题与说明' }
</script>

<template>
  <RegistryResourceExplorer label="配方数据流" v-slot="{ inspect }">
    <section class="recipe-definition-view" aria-label="配方步骤与数据流">
      <p>按保存顺序编排；下方连线只表示明确配置的来源引用，不表示已经执行或自动采用。</p>
      <p>制作方式：{{ definition.interaction_mode === '' ? '尚未选择' : options?.interactionModeOptions.find(row => row.value === definition.interaction_mode)?.label ?? definition.interaction_mode ?? '未声明' }}</p>
      <p v-if="definition.schema_version !== 3" role="alert">此记录不是当前 schema3 合同；不转换或猜测旧步骤，请查看原始合同。</p>
      <p v-if="!Array.isArray(definition.stages)" role="alert">步骤声明缺失，不能视为没有步骤。</p>
      <p v-else-if="!steps.length">尚未配置步骤。</p>
      <ol v-else class="recipe-steps">
        <li v-for="step in steps" :key="step.index">
          <header><h3>{{ step.label }}</h3><span>{{ step.mode }}</span></header>
          <p>阶段审核：{{ step.approval }}<template v-if="step.stage.decision_policy !== undefined"> · {{ step.stage.decision_policy }}</template></p>
          <p v-if="!step.groups.length">{{ step.expectsBinding ? '此执行方式需要实例绑定，当前尚未配置；不能视为已就绪。' : '此步骤未记录模型调用绑定；按其实际职责与合同处理，不虚构执行实例。' }}</p>
          <section v-for="group in step.groups" :key="group.key" class="recipe-bindings">
            <h4>{{ group.label }}能力</h4>
            <p>{{ modeLabel(group.routing.mode) }} · 候选数 {{ group.routing.variant_count ?? '未声明' }} · 选择规则 {{ group.routing.evaluator === null ? '人工选择' : group.routing.evaluator ?? '未声明' }}</p>
            <p v-if="!group.routes.length">绑定中未记录执行实例。</p>
            <article v-for="(route, at) in group.routes" :key="at">
              <button v-if="typeof route.execution_instance_version_id === 'string' && route.execution_instance_version_id" class="button ghost" type="button" @click="inspect('execution', route.execution_instance_version_id, $event)">查看{{ group.label === '执行' ? '' : group.label }}执行实例 {{ at + 1 }} · 输入、处理与输出</button>
              <p v-else role="alert">缺少确切执行实例身份，不按名称或当前版本补齐。</p>
              <h4>本步骤给实例提供什么</h4>
              <p v-if="!Object.keys(obj(route.input_sources)).length">未记录输入来源绑定；是否需要输入以该精确实例合同为准。</p>
              <dl><div v-for="(source, target) in obj(route.input_sources)" :key="target"><dt>{{ recipeSourceSummary(source, options) }}</dt><dd>→ {{ target }}<details><summary>来源原值与映射</summary><ReadonlyDataValue :value="source" :label="String(target)"/></details></dd></div></dl>
              <details><summary>步骤参数与精确绑定</summary><ReadonlyDataValue :value="{ execution_instance_version_id: route.execution_instance_version_id, expected_definition_sha256: route.expected_definition_sha256, parameter_policies: route.parameter_policies }" label="已保存绑定"/></details>
            </article>
          </section>
          <template v-for="(label, key) in outputLabels" :key="key">
            <section v-if="step.stage[key] !== undefined" class="recipe-bindings"><h4>{{ label }}</h4>
              <p>{{ recipeOutputSourceSummary(step.stage[key], options) }}</p>
              <button v-if="typeof obj(step.stage[key]).output_version_id === 'string'" type="button" class="button ghost" @click="inspect('output', String(obj(step.stage[key]).output_version_id), $event)">查看{{ label }}定义</button>
              <details><summary>确切输出版本、原文与字段映射</summary><ReadonlyDataValue :value="step.stage[key]" :label="label"/></details>
            </section>
          </template>
          <details v-if="['material_sources', 'material_selection', 'shot_reference_selection', 'field_review'].some(key => step.stage[key] !== undefined)">
            <summary>素材来源、选材与字段确认规则</summary>
            <ReadonlyDataValue v-for="key in ['material_sources', 'material_selection', 'shot_reference_selection', 'field_review'].filter(key => step.stage[key] !== undefined)" :key="key" :value="step.stage[key]" :label="key"/>
          </details>
          <section class="recipe-consumers"><h4>结果去向</h4>
            <ul v-if="step.consumers.length"><li v-for="(consumer, at) in step.consumers" :key="at">{{ consumer.output ?? '所声明来源' }} → {{ consumer.label }} / {{ consumer.target }} <strong v-if="!consumer.orderValid" role="alert">引用顺序异常：来源应在使用方之前</strong></li></ul>
            <p v-else>未记录其他步骤对本步骤的显式输入引用；不推断相邻步骤一定消费其结果。实例输出合同可从能力详情查看。</p>
          </section>
          <details><summary>本步骤完整声明与身份</summary><ReadonlyDataValue :value="step.stage" label="原始步骤"/></details>
        </li>
      </ol>
    </section>
  </RegistryResourceExplorer>
</template>

<style scoped>
.recipe-definition-view{display:grid;gap:var(--config-space-md);font-family:var(--font);font-size:var(--config-body-size);min-width:0}.recipe-definition-view p,.recipe-definition-view dl,.recipe-definition-view dd,.recipe-definition-view h3,.recipe-definition-view h4{margin:0}.recipe-definition-view p{color:var(--text-secondary)}.recipe-steps{list-style:none;padding:0;margin:0;display:grid;gap:var(--config-space-xl)}.recipe-steps>li{display:grid;gap:var(--config-space-md);border-top:1px solid var(--border-strong);padding-top:var(--config-space-lg);min-width:0}.recipe-steps header{display:flex;gap:var(--config-space-md);justify-content:space-between;flex-wrap:wrap}.recipe-steps h3{font-size:var(--config-section-size)}.recipe-steps h4{font-size:var(--config-body-size)}.recipe-bindings,.recipe-bindings article,.recipe-consumers{display:grid;gap:var(--config-space-sm);min-width:0}.recipe-bindings article{border-left:2px solid var(--border-strong);padding-left:var(--config-space-md)}.recipe-bindings dl>div{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:var(--config-space-sm);padding-block:var(--config-space-sm)}.recipe-definition-view :is(p,span,dt,dd,li,summary,button){overflow-wrap:anywhere;white-space:pre-wrap}.recipe-definition-view summary{cursor:pointer;min-height:var(--config-control-height);align-content:center}.recipe-definition-view .button{justify-self:start;max-width:100%;text-align:left;min-height:var(--config-control-height);font-size:var(--config-body-size)}.recipe-definition-view :focus-visible{outline:2px solid var(--accent);outline-offset:3px}.recipe-definition-view [role=alert]{color:var(--danger,#f79d9d)}@media(max-width:640px){.recipe-definition-view{--config-control-height:44px}.recipe-bindings dl>div{grid-template-columns:1fr}}
</style>
