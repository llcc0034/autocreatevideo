<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { Boxes, CircleAlert, Cpu, Database, GitBranch, LoaderCircle, Route, ShieldCheck } from 'lucide-vue-next'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import { reasonCodeLabel } from '@/services/presentationLabels'
import { buildProductionRecipeFlow } from '@/services/productionRecipeFlow'
import type { ProductionRecipeFlowRoute } from '@/services/productionRecipeFlow'
import type { ModelDeployment, ModelProfileVersion, ProductionReadinessRecipe, ProductionRecipe, ProductionRecipeEditorOptions, WorkflowTemplateVersion } from '@/services/platformTypes'

const props = withDefaults(defineProps<{
  recipe: ProductionRecipe
  deployments?: ModelDeployment[]
  modelProfiles?: ModelProfileVersion[]
  workflows?: WorkflowTemplateVersion[]
  workflowDetailStates?: Record<string, 'loading' | 'loaded' | 'error'>
  workflowDetailErrors?: Record<string, string>
  recipeEditorOptions?: ProductionRecipeEditorOptions | null
  readiness?: ProductionReadinessRecipe | null
  readinessReportState?: 'loaded' | 'unavailable' | 'error'
  compact?: boolean
  showRaw?: boolean
}>(), {
  deployments: () => [],
  modelProfiles: () => [],
  workflows: () => [],
  workflowDetailStates: () => ({}),
  workflowDetailErrors: () => ({}),
  recipeEditorOptions: null,
  readiness: null,
  readinessReportState: 'unavailable',
  compact: false,
  showRaw: false,
})

const flow = computed(() => buildProductionRecipeFlow(props.recipe.definition, {
  deployments: props.deployments,
  modelProfiles: props.modelProfiles,
  workflows: props.workflows,
  workflowDetailStates: props.workflowDetailStates,
  workflowDetailErrors: props.workflowDetailErrors,
  recipeEditorOptions: props.recipeEditorOptions,
}))
const selectedStageId = ref('')
const selectedRouteId = ref('')
watch(flow, (projection) => {
  if (!projection.stages.some((stage) => stage.id === selectedStageId.value)) selectedStageId.value = projection.stages[0]?.id || ''
}, { immediate: true })
const selectedStage = computed(() => flow.value.stages.find((stage) => stage.id === selectedStageId.value) || null)
watch(selectedStage, (stage) => {
  if (!stage?.routes.some((route) => route.id === selectedRouteId.value)) selectedRouteId.value = stage?.routes[0]?.id || ''
}, { immediate: true })
const selectedRoute = computed(() => selectedStage.value?.routes.find((route) => route.id === selectedRouteId.value) || null)
const stageDetailId = computed(() => `recipe-${props.recipe.id}-stage-detail`)
const pretty = (value: unknown) => JSON.stringify(value, null, 2)
const hasValues = (value: Record<string, unknown>) => Object.keys(value).length > 0
const routeChoiceLabel = (route: ProductionRecipeFlowRoute) => {
  if (selectedStage.value?.routingMode === 'ordered_fallback') return `优先级 ${route.index + 1}`
  if (selectedStage.value?.routingMode === 'manual_choice') return `候选 ${route.index + 1}`
  return selectedStage.value?.routes.length === 1 ? '固定路由' : `路由 ${route.index + 1}`
}
const stageSummary = (stage: (typeof flow.value.stages)[number]) => {
  const route = stage.routes[0]
  if (!route) return stage.modeLabel
  if (route.modelIdentifier) return route.modelIdentifier
  return route.modelProfileLabel || route.modelDeploymentLabel
}
const selectStageAt = async (index: number, focus = false) => {
  const stage = flow.value.stages[index]
  if (!stage) return
  selectedStageId.value = stage.id
  if (focus) {
    await nextTick()
    document.querySelector<HTMLElement>(`[data-recipe-stage="${props.recipe.id}:${index}"]`)?.focus()
  }
}
const onStageKeydown = (event: KeyboardEvent, index: number) => {
  let nextIndex: number | null = null
  if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = Math.min(index + 1, flow.value.stages.length - 1)
  else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = Math.max(index - 1, 0)
  else if (event.key === 'Home') nextIndex = 0
  else if (event.key === 'End') nextIndex = flow.value.stages.length - 1
  if (nextIndex === null) return
  event.preventDefault()
  void selectStageAt(nextIndex, true)
}
const readinessMessage = computed(() => {
  if (props.readiness) {
    return props.readiness.reasonCodes.length
      ? props.readiness.reasonCodes.map(reasonCodeLabel).join('、')
      : '服务端当前评估没有返回阻断原因。'
  }
  if (props.recipe.status !== 'published') return '该版本不在当前 published 配方生产就绪评估范围内。'
  if (props.readinessReportState === 'loaded') return '该已发布版本未出现在本次服务端就绪响应中，当前状态无法判断。'
  if (props.readinessReportState === 'error') return '生产就绪事实读取失败；不能由生命周期或流程投影推断。'
  return '当前账号未读取该版本的生产就绪事实；不能由生命周期或流程投影推断。'
})
</script>

<template>
  <section class="recipe-flow" :class="{ compact }">
    <template v-if="compact">
      <header class="compact-heading">
        <div><GitBranch :size="13"/><strong>阶段声明顺序</strong><small>{{ flow.stageCount }} 个阶段</small></div>
        <span v-if="flow.issues.length"><CircleAlert :size="12"/>{{ flow.issues.length }} 项结构提示</span>
      </header>
      <div v-if="flow.stages.length" class="recipe-flow-scroll compact-scroll">
        <ol class="recipe-flow-track compact-track" aria-label="阶段声明顺序">
          <li v-for="stage in flow.stages" :key="stage.id">
            <div class="recipe-flow-node compact-node" :class="[`phase-${stage.phase}`, { issue: stage.issues.length }]">
              <span><em>{{ stage.index + 1 }}</em>{{ stage.phaseLabel }}</span>
              <strong>{{ stage.label }}</strong>
              <small>{{ stageSummary(stage) }}</small>
            </div>
            <i v-if="stage.index < flow.stages.length - 1" class="order-separator" aria-hidden="true"/>
          </li>
        </ol>
      </div>
      <p v-else class="recipe-flow-empty">当前记录没有可展示的阶段声明。</p>
    </template>

    <template v-else>
      <header class="recipe-flow-overview">
        <div>
          <span>配方阶段概览</span>
          <strong>{{ flow.stageCount ? `${flow.firstStageLabel} 至 ${flow.lastStageLabel}` : '未发现可展示阶段' }}</strong>
          <small>{{ recipe.recipeKey }} · schema v{{ flow.schemaVersion || '—' }}<template v-if="flow.interactionMode"> · {{ flow.interactionMode }}</template></small>
        </div>
        <dl>
          <div><dt>阶段</dt><dd>{{ flow.stageCount }}</dd></div>
          <div><dt>配方路由</dt><dd>{{ flow.routeCount }}</dd></div>
          <div><dt>审核/策略门禁</dt><dd>{{ flow.reviewGateCount }}</dd></div>
          <div><dt>结构解析</dt><dd :class="{ warning: flow.issues.length }">{{ flow.issues.length ? `${flow.issues.length} 项提示` : '未发现结构问题' }}</dd></div>
        </dl>
        <div class="recipe-flow-lifecycle"><small>版本生命周期</small><PlatformStatusPill :status="recipe.status"/></div>
      </header>

      <section class="recipe-readiness-fact" :class="readiness?.state || 'unknown'">
        <div><span>该版本生产就绪</span><strong>{{ readiness ? (readiness.state === 'ready' ? '当前服务端评估通过' : readiness.state === 'blocked' ? '当前服务端评估阻断' : '服务端状态无法识别') : '未获得可用结论' }}</strong><small>{{ readinessMessage }}</small></div>
        <PlatformStatusPill v-if="readiness" :status="readiness.state"/>
        <em v-else>unknown</em>
      </section>

      <details v-if="flow.issues.length" class="recipe-flow-issues">
        <summary><CircleAlert :size="13"/>结构与目录解析提示 · {{ flow.issues.length }} 项</summary>
        <ul><li v-for="issue in flow.issues" :key="issue">{{ issue }}</li></ul>
      </details>

      <section class="stage-order-section">
        <header><div><GitBranch :size="13"/><strong>阶段声明顺序</strong></div><small>用于选择下钻阶段，不代表阶段间输入输出依赖。</small></header>
        <div v-if="flow.stages.length" class="recipe-flow-scroll">
          <ol class="recipe-flow-track" role="tablist" aria-label="阶段声明顺序" aria-orientation="horizontal">
            <li v-for="stage in flow.stages" :key="stage.id">
              <button
                type="button"
                role="tab"
                class="recipe-flow-node"
                :class="[`phase-${stage.phase}`, { active: selectedStageId === stage.id, issue: stage.issues.length }]"
                :data-recipe-stage="`${recipe.id}:${stage.index}`"
                :aria-selected="selectedStageId === stage.id"
                :aria-controls="stageDetailId"
                :tabindex="selectedStageId === stage.id ? 0 : -1"
                @click="selectStageAt(stage.index)"
                @keydown="onStageKeydown($event, stage.index)"
              >
                <span><em>{{ stage.index + 1 }}</em>{{ stage.phaseLabel }}</span>
                <strong>{{ stage.label }}</strong>
                <small>{{ stageSummary(stage) }}</small>
              </button>
              <i v-if="stage.index < flow.stages.length - 1" class="order-separator" aria-hidden="true"/>
            </li>
          </ol>
        </div>
        <p v-else class="recipe-flow-empty">当前服务端记录没有可投影的 stages；请先修复配方结构。</p>
      </section>

      <section v-if="selectedStage" :id="stageDetailId" class="recipe-stage-detail" role="tabpanel" :aria-label="`阶段 ${selectedStage.index + 1} ${selectedStage.label} 详情`">
        <header class="stage-detail-heading">
          <div><span>阶段 {{ selectedStage.index + 1 }} · {{ selectedStage.phaseLabel }}</span><strong>{{ selectedStage.label }}</strong><code>{{ selectedStage.key || 'missing_key' }}</code></div>
          <b>{{ selectedStage.approvalLabel }}</b>
        </header>

        <dl class="stage-meta">
          <div><dt>执行方式</dt><dd>{{ selectedStage.modeLabel }}<code>{{ selectedStage.mode || 'missing' }}</code></dd></div>
          <div><dt>路由策略</dt><dd>{{ selectedStage.routingModeLabel }}<code>{{ selectedStage.routingMode || (selectedStage.hasRouting ? 'missing' : 'none') }}</code></dd></div>
          <div><dt>候选变体</dt><dd>{{ selectedStage.hasRouting ? (selectedStage.variantCount ?? '缺失') : '不适用' }}</dd></div>
          <div v-if="selectedStage.decisionPolicy"><dt>决策策略</dt><dd><code>{{ selectedStage.decisionPolicy }}</code></dd></div>
        </dl>

        <section v-if="selectedStage.routes.length > 1" class="route-selector">
          <header><Route :size="13"/><div><strong>选择要审阅的配方候选路由</strong><small>这里只切换合同视图，不代表某次生产实际选择。</small></div></header>
          <div role="tablist" aria-label="配方候选路由">
            <button v-for="route in selectedStage.routes" :key="route.id" type="button" role="tab" :aria-selected="selectedRouteId === route.id" :class="{ active: selectedRouteId === route.id, issue: route.issues.length }" @click="selectedRouteId = route.id">{{ routeChoiceLabel(route) }}<small>{{ route.modelIdentifier || route.modelDeploymentLabel }}</small></button>
          </div>
        </section>

        <div class="stage-fact-flow">
          <section class="stage-fact input-fact">
            <header><span>01</span><Database :size="15"/><div><strong>输入合同</strong><small>节点可接收什么，不展示某次运行值</small></div></header>
            <template v-if="selectedRoute">
              <div v-if="selectedRoute.workflowDetailState === 'loading'" class="fact-state"><LoaderCircle class="spin" :size="15"/>正在读取工作流版本的完整输入合同…</div>
              <div v-else-if="selectedRoute.workflowDetailState === 'error'" class="fact-state error"><CircleAlert :size="15"/><span>输入合同读取失败。{{ selectedRoute.workflowDetailError }}</span></div>
              <div v-else-if="selectedRoute.workflowDetailState === 'missing'" class="fact-state error"><CircleAlert :size="15"/><span>配方声明了工作流版本 ID，但当前目录无法解析，不能展示输入合同。</span></div>
              <div v-else-if="selectedRoute.workflowDetailState === 'summary'" class="fact-state"><CircleAlert :size="15"/><span>当前只有工作流摘要，尚未读取字段级输入合同。</span></div>
              <template v-else-if="selectedRoute.workflowDetailState === 'loaded'">
                <div class="contract-meta"><span>schema v{{ selectedRoute.inputContractSchemaVersion ?? '缺失' }}</span><span>{{ selectedRoute.inputs.length }} 个参数</span><span>{{ selectedRoute.semanticBindingCount }} 个节点绑定</span></div>
                <ul v-if="selectedRoute.inputs.length" class="contract-list">
                  <li v-for="input in selectedRoute.inputs" :key="input.semanticKey">
                    <div><strong>{{ input.semanticKey }}</strong><b :class="{ required: input.required }">{{ input.required ? '必填' : '可选' }}</b></div>
                    <small>{{ input.valueType }}<template v-if="input.nodeId"> · 节点 {{ input.nodeId }} / {{ input.inputName }}</template></small>
                    <p v-if="input.constraints.length">{{ input.constraints.join(' · ') }}</p>
                  </li>
                </ul>
                <div v-else class="fact-state"><span>该工作流合同没有声明运行输入参数。</span></div>
                <details v-if="selectedRoute.dynamicInputs.length" class="fact-details">
                  <summary>动态参考输入 · {{ selectedRoute.dynamicInputs.length }} 项模式约束</summary>
                  <ul><li v-for="input in selectedRoute.dynamicInputs" :key="`${input.modeKey}:${input.semanticKey}`"><strong>{{ input.modeLabel }}</strong><code>{{ input.semanticKey }}</code><span>数量 {{ input.minimumItems ?? 0 }}–{{ input.maximumItems ?? '不限' }}</span></li></ul>
                </details>
              </template>
              <div v-else class="fact-state"><span>该执行路由不使用可读取的 ComfyUI 工作流输入合同；配方本身也未声明字段级输入。</span></div>
              <small class="fact-source">来源：工作流版本合同；实际值由 ProductionRun / StageRun 在运行时冻结。</small>
            </template>
            <div v-else class="fact-state"><span>该阶段没有配方路由，也没有字段级输入合同；不能从前一声明阶段推断输入。</span></div>
          </section>

          <section class="stage-fact execution-fact">
            <header><span>02</span><Cpu :size="15"/><div><strong>模型与执行绑定</strong><small>配方 ID + 当前目录解析结果</small></div></header>
            <template v-if="selectedRoute">
              <article class="execution-card">
                <div><Boxes :size="14"/><span>模型</span><PlatformStatusPill v-if="selectedRoute.modelProfileStatus" :status="selectedRoute.modelProfileStatus"/></div>
                <strong>{{ selectedRoute.modelIdentifier || '模型标识无法解析' }}</strong>
                <small>{{ selectedRoute.modelProfileLabel }} · {{ selectedRoute.modelModality || '模态未声明' }}</small>
                <code>{{ selectedRoute.modelProfileVersionId || 'missing model profile version ID' }}</code>
              </article>
              <article class="execution-card">
                <div><Cpu :size="14"/><span>部署</span><PlatformStatusPill v-if="selectedRoute.modelDeploymentStatus" :status="selectedRoute.modelDeploymentStatus"/></div>
                <strong>{{ selectedRoute.modelDeploymentLabel }}</strong>
                <small>{{ selectedRoute.providerDriver || '驱动无法解析' }}<template v-if="selectedRoute.providerKey"> · {{ selectedRoute.providerKey }}</template></small>
                <code>{{ selectedRoute.modelDeploymentId || 'missing deployment ID' }}</code>
              </article>
              <article class="execution-card">
                <div><GitBranch :size="14"/><span>工作流</span><PlatformStatusPill v-if="selectedRoute.workflowStatus" :status="selectedRoute.workflowStatus"/></div>
                <strong>{{ selectedRoute.workflowVersionLabel }}</strong>
                <small v-if="selectedRoute.workflowVersionId">生命周期按当前工作流版本原样展示</small>
                <small v-else>此部署驱动不使用工作流，或当前目录无法判断。</small>
                <code>{{ selectedRoute.workflowVersionId || 'not applicable' }}</code>
              </article>
              <details v-if="selectedRoute.modelRequirements.length" class="fact-details model-files">
                <summary>工作流使用的模型文件 · {{ selectedRoute.modelRequirements.length }} 项</summary>
                <ul><li v-for="requirement in selectedRoute.modelRequirements" :key="`${requirement.role}:${requirement.alias}`"><strong>{{ requirement.role }} · {{ requirement.alias }}</strong><code>{{ requirement.expectedFilename || '文件名未声明' }}</code><span>{{ requirement.required ? '必需' : '可选' }}</span></li></ul>
              </details>
              <ul v-if="selectedRoute.issues.length" class="route-issues"><li v-for="issue in selectedRoute.issues" :key="issue">{{ issue }}</li></ul>
            </template>
            <div v-else class="fact-state"><ShieldCheck :size="15"/><span>该阶段由平台 {{ selectedStage.modeLabel }} 执行，配方没有绑定独立模型、部署或工作流。</span></div>
          </section>

          <section class="stage-fact output-fact">
            <header><span>03</span><ShieldCheck :size="15"/><div><strong>输出与后续声明</strong><small>合同输出与配方顺序分开表达</small></div></header>
            <template v-if="selectedRoute">
              <template v-if="selectedRoute.workflowDetailState === 'loaded'">
                <ul v-if="selectedRoute.outputs.length" class="contract-list output-list">
                  <li v-for="output in selectedRoute.outputs" :key="output.key"><div><strong>{{ output.key }}</strong><b :class="{ required: output.required }">{{ output.required ? '必需输出' : '可选输出' }}</b></div><small>{{ output.kind }}<template v-if="output.nodeId"> · 节点 {{ output.nodeId }}</template></small></li>
                </ul>
                <div v-else class="fact-state"><span>工作流详情已读取，但合同没有声明输出节点。</span></div>
              </template>
              <div v-else-if="selectedRoute.workflowOutputKind" class="fact-state"><span>工作流摘要声明输出类型为 <strong>{{ selectedRoute.workflowOutputKind }}</strong>；字段级输出合同尚不可用。</span></div>
              <div v-else class="fact-state"><span>当前没有可验证的字段级输出合同。</span></div>
            </template>
            <div v-else class="fact-state"><span>配方没有为该平台阶段声明输出合同；具体产物需在 ProductionRun 中查看。</span></div>
            <div class="next-declaration">
              <span>下一声明阶段（非依赖）</span>
              <template v-if="selectedStage.nextDeclaration"><strong>{{ selectedStage.nextDeclaration.index + 1 }} · {{ selectedStage.nextDeclaration.label }}</strong><code>{{ selectedStage.nextDeclaration.key }}</code></template>
              <strong v-else>没有后续声明项</strong>
              <small>这里只表示 stages[] 顺序，不证明上方输出会成为下一阶段输入。</small>
            </div>
          </section>
        </div>

        <details v-if="selectedStage.evaluator !== null || hasValues(selectedStage.extras)" class="recipe-stage-contract">
          <summary>阶段扩展合同</summary>
          <pre>{{ pretty({ evaluator: selectedStage.evaluator, ...selectedStage.extras }) }}</pre>
        </details>
      </section>

      <details v-if="showRaw" class="recipe-flow-raw">
        <summary>原始配方 JSON · 高级审计</summary>
        <p>上方只投影可证明的声明与当前目录解析结果；这里保留完整 definition 供精确审计。</p>
        <pre>{{ pretty(recipe.definition) }}</pre>
      </details>
    </template>
  </section>
</template>

<style scoped>
.recipe-flow {
  display: grid;
  gap: 10px;
  min-width: 0;
  color: #aeb8c2;
}
.compact-heading,
.compact-heading > div,
.compact-heading > span,
.stage-order-section > header,
.stage-order-section > header > div {
  display: flex;
  align-items: center;
}
.compact-heading,
.stage-order-section > header {
  justify-content: space-between;
  gap: 10px;
}
.compact-heading > div,
.compact-heading > span,
.stage-order-section > header > div { gap: 6px; }
.compact-heading strong,
.stage-order-section > header strong { color: #adb7c1; font-size: 10px; }
.compact-heading small,
.stage-order-section > header small { color: #687581; font-size: 9px; }
.compact-heading > span { color: #b48757; font-size: 9px; }

.recipe-flow-overview {
  display: grid;
  grid-template-columns: minmax(240px, 1.35fr) minmax(420px, 2fr) auto;
  align-items: stretch;
  overflow: hidden;
  border: 1px solid #252c35;
  border-radius: 8px;
  background: #090d11;
}
.recipe-flow-overview > div:first-child {
  display: grid;
  align-content: center;
  gap: 3px;
  padding: 11px 12px;
  border-right: 1px solid #252c35;
}
.recipe-flow-overview > div:first-child span { color: #7b8794; font-size: 9px; }
.recipe-flow-overview > div:first-child strong { color: #d0d6dd; font-size: 12px; }
.recipe-flow-overview > div:first-child small {
  color: #68737f;
  font: 8px Consolas, monospace;
  overflow-wrap: anywhere;
}
.recipe-flow-overview dl {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin: 0;
}
.recipe-flow-overview dl div { padding: 10px; border-right: 1px solid #252c35; }
.recipe-flow-overview dt { color: #6d7782; font-size: 8px; }
.recipe-flow-overview dd { margin: 5px 0 0; color: #b8c1ca; font: 11px Consolas, monospace; }
.recipe-flow-overview dd.warning { color: #d5a25f; }
.recipe-flow-overview > :last-child { align-self: center; margin: 0 10px; }
.recipe-flow-lifecycle { display: grid; justify-items: end; gap: 4px; }
.recipe-flow-lifecycle > small { color: #68737f; font-size: 9px; }

.recipe-readiness-fact {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 11px;
  border: 1px solid #29313a;
  border-radius: 8px;
  background: #090d11;
}
.recipe-readiness-fact > div { display: grid; min-width: 0; gap: 3px; }
.recipe-readiness-fact span { color: #77828e; font-size: 9px; }
.recipe-readiness-fact strong { color: #b9c2cb; font-size: 10px; }
.recipe-readiness-fact small { color: #69747f; font-size: 9px; line-height: 1.45; overflow-wrap: anywhere; }
.recipe-readiness-fact.blocked { border-color: rgba(211,107,115,.3); background: rgba(211,107,115,.035); }
.recipe-readiness-fact.ready { border-color: rgba(105,196,147,.28); background: rgba(105,196,147,.03); }
.recipe-readiness-fact > em { color: #7b8691; font: 9px Consolas, monospace; font-style: normal; }
.recipe-flow-issues { border: 1px solid rgba(213,162,95,.25); border-radius: 7px; background: rgba(213,162,95,.035); }
.recipe-flow-issues summary { display: flex; align-items: center; gap: 6px; padding: 8px 10px; color: #c39a65; font-size: 9px; cursor: pointer; }
.recipe-flow-issues ul { margin: 0; padding: 0 26px 9px; color: #9a8061; font-size: 9px; line-height: 1.55; }
.stage-order-section { display: grid; gap: 7px; min-width: 0; }

.recipe-flow-scroll {
  min-width: 0;
  overflow-x: auto;
  padding: 8px;
  border: 1px solid #252c35;
  border-radius: 8px;
  background: #070a0e;
  scrollbar-color: #343c46 transparent;
  scroll-snap-type: x proximity;
}
.recipe-flow-track { display: flex; align-items: center; min-width: max-content; margin: 0; padding: 0; list-style: none; }
.recipe-flow-track > li { display: flex; align-items: center; }
.recipe-flow-node {
  display: grid;
  width: 142px;
  min-height: 78px;
  gap: 5px;
  padding: 8px;
  border: 1px solid #303844;
  border-top: 2px solid #6d7b8a;
  border-radius: 7px;
  background: #0d1217;
  color: inherit;
  text-align: left;
  cursor: pointer;
  scroll-snap-align: start;
}
.recipe-flow-node:hover { background: #121820; }
.recipe-flow-node:focus-visible { outline: 2px solid rgba(230,167,90,.72); outline-offset: 2px; }
.recipe-flow-node.active { border-color: #c28a49; box-shadow: 0 0 0 2px rgba(194,138,73,.12); }
.recipe-flow-node.issue { border-left-color: #d36b73; }
.recipe-flow-node.phase-script { border-top-color: #6f92ba; }
.recipe-flow-node.phase-preproduction { border-top-color: #a47cc2; }
.recipe-flow-node.phase-production { border-top-color: #c59456; }
.recipe-flow-node.phase-delivery { border-top-color: #5eae82; }
.recipe-flow-node > span { display: flex; align-items: center; gap: 6px; color: #77828e; font-size: 8px; }
.recipe-flow-node > span em { display: grid; place-items: center; width: 17px; height: 17px; border-radius: 50%; background: #1b222a; color: #adb7c1; font-style: normal; }
.recipe-flow-node > strong,
.recipe-flow-node > small { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.recipe-flow-node > strong { color: #c4cbd2; font-size: 10px; }
.recipe-flow-node > small { color: #75818d; font-size: 8px; }
.order-separator { position: relative; width: 18px; height: 1px; margin: 0 5px; background: #343d46; }
.order-separator::after { position: absolute; top: -2px; right: 0; width: 5px; height: 5px; border-radius: 50%; background: #46515c; content: ""; }
.compact-scroll { padding: 7px; }
.compact-node { width: 126px; min-height: 64px; cursor: default; }
.compact-node:hover { background: #0d1217; }

.recipe-stage-detail { min-width: 0; overflow: hidden; border: 1px solid #29313a; border-radius: 8px; background: #0a0e12; }
.stage-detail-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; border-bottom: 1px solid #252c35; }
.stage-detail-heading > div { display: grid; grid-template-columns: auto minmax(0,1fr); align-items: center; min-width: 0; gap: 3px 8px; }
.stage-detail-heading span { color: #b48650; font-size: 8px; }
.stage-detail-heading strong { color: #c8d0d8; font-size: 12px; }
.stage-detail-heading code { grid-column: 2; color: #6e7985; font-size: 8px; overflow-wrap: anywhere; }
.stage-detail-heading > b { color: #c29a68; font-size: 9px; font-weight: 500; }
.stage-meta { display: grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); margin: 0; border-bottom: 1px solid #252c35; }
.stage-meta > div { padding: 8px 11px; border-right: 1px solid #252c35; }
.stage-meta dt { color: #697480; font-size: 8px; }
.stage-meta dd { display: grid; gap: 2px; margin: 4px 0 0; color: #aeb7c0; font-size: 9px; }
.stage-meta code { color: #687480; font-size: 8px; overflow-wrap: anywhere; }

.route-selector { display: grid; gap: 8px; padding: 10px 11px; border-bottom: 1px solid #252c35; }
.route-selector > header { display: flex; align-items: flex-start; gap: 7px; }
.route-selector > header > div { display: grid; gap: 2px; }
.route-selector strong { color: #adb7c1; font-size: 9px; }
.route-selector small { color: #687581; font-size: 8px; }
.route-selector > div { display: flex; gap: 6px; overflow-x: auto; }
.route-selector button { display: grid; min-width: 130px; gap: 2px; padding: 7px 8px; border: 1px solid #2d3540; border-radius: 6px; background: #090d11; color: #aeb7c0; text-align: left; font-size: 9px; cursor: pointer; }
.route-selector button.active { border-color: #b27e43; background: rgba(178,126,67,.08); }
.route-selector button.issue { border-left-color: #d36b73; }

.stage-fact-flow {
  display: grid;
  grid-template-columns: minmax(0,1.15fr) minmax(0,1fr) minmax(0,.92fr);
  gap: 9px;
  min-width: 0;
  padding: 11px;
}
.stage-fact { min-width: 0; align-self: start; overflow: hidden; border: 1px solid #28313a; border-radius: 8px; background: #080c10; }
.stage-fact > header { display: grid; grid-template-columns: 23px 16px minmax(0,1fr); align-items: center; min-height: 48px; gap: 6px; padding: 8px 9px; border-bottom: 1px solid #242c34; }
.stage-fact > header > span { display: grid; place-items: center; width: 21px; height: 21px; border-radius: 50%; background: #182029; color: #98a4af; font: 8px Consolas,monospace; }
.stage-fact > header > div { display: grid; gap: 2px; }
.stage-fact > header strong { color: #c0c8d0; font-size: 10px; }
.stage-fact > header small { color: #66727e; font-size: 8px; }
.input-fact > header { border-top: 2px solid #668eb8; }
.execution-fact > header { border-top: 2px solid #b4844d; }
.output-fact > header { border-top: 2px solid #5fa77d; }
.contract-meta { display: flex; flex-wrap: wrap; gap: 5px; padding: 7px 8px; border-bottom: 1px solid #202831; }
.contract-meta span { padding: 3px 5px; border-radius: 4px; background: #111821; color: #7d8995; font-size: 8px; }
.contract-list { display: grid; max-height: 360px; gap: 5px; overflow: auto; margin: 0; padding: 6px; list-style: none; }
.contract-list li { padding: 7px; border: 1px solid #222b34; border-radius: 6px; background: #0b1015; }
.contract-list li > div { display: flex; align-items: flex-start; justify-content: space-between; gap: 6px; }
.contract-list strong { color: #b5bec7; font: 9px Consolas,monospace; overflow-wrap: anywhere; }
.contract-list b { color: #727e89; font-size: 8px; font-weight: 500; }
.contract-list b.required { color: #c18c51; }
.contract-list small { display: block; margin-top: 3px; color: #77838f; font-size: 8px; overflow-wrap: anywhere; }
.contract-list p { margin: 4px 0 0; color: #8c765b; font-size: 8px; }
.fact-state { display: flex; align-items: flex-start; min-height: 58px; gap: 7px; padding: 10px; color: #788591; font-size: 9px; line-height: 1.5; }
.fact-state.error { color: #b8757a; }
.fact-state strong { color: #b8c1ca; }
.fact-source { display: block; padding: 7px 9px; border-top: 1px solid #202831; color: #5f6b77; font-size: 8px; line-height: 1.45; }
.execution-card { display: grid; gap: 4px; margin: 7px; padding: 8px; border: 1px solid #222b34; border-radius: 6px; background: #0b1015; }
.execution-card > div { display: flex; align-items: center; gap: 5px; color: #76838f; font-size: 8px; }
.execution-card > div > :last-child { margin-left: auto; }
.execution-card > strong { color: #b9c2ca; font-size: 10px; overflow-wrap: anywhere; }
.execution-card > small { color: #74808c; font-size: 8px; line-height: 1.4; overflow-wrap: anywhere; }
.execution-card > code { color: #626f7b; font-size: 8px; overflow-wrap: anywhere; }
.fact-details { margin: 7px; border: 1px solid #222b34; border-radius: 6px; background: #0b1015; }
.fact-details summary { padding: 7px 8px; color: #87939e; font-size: 8px; cursor: pointer; }
.fact-details ul { display: grid; gap: 5px; margin: 0; padding: 0 8px 8px; list-style: none; }
.fact-details li { display: grid; gap: 2px; padding-top: 5px; border-top: 1px solid #202831; color: #74808c; font-size: 8px; }
.fact-details strong { color: #9ea8b1; font-size: 8px; }
.fact-details code { color: #65727e; overflow-wrap: anywhere; }
.route-issues { margin: 7px; padding: 7px 7px 7px 22px; border: 1px solid rgba(211,107,115,.25); border-radius: 6px; color: #a97075; font-size: 8px; line-height: 1.45; }
.next-declaration { display: grid; gap: 4px; margin: 7px; padding: 9px; border: 1px solid #26312c; border-radius: 6px; background: rgba(95,167,125,.035); }
.next-declaration > span { color: #668273; font-size: 8px; }
.next-declaration > strong { color: #aab9b0; font-size: 10px; }
.next-declaration code { color: #65766d; font-size: 8px; overflow-wrap: anywhere; }
.next-declaration small { color: #5d6b63; font-size: 8px; line-height: 1.4; }
.recipe-stage-contract,
.recipe-flow-raw { overflow: hidden; margin: 0 11px 11px; border: 1px solid #252d36; border-radius: 7px; background: #080b0f; }
.recipe-stage-contract summary,
.recipe-flow-raw summary { padding: 9px 10px; color: #8d98a4; font-size: 9px; cursor: pointer; }
.recipe-stage-contract pre,
.recipe-flow-raw pre { max-height: 340px; overflow: auto; margin: 0; padding: 10px; border-top: 1px solid #252d36; color: #9ca7b3; font: 9px/1.55 Consolas,monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.recipe-flow-raw { margin: 0; }
.recipe-flow-raw > p { margin: 0; padding: 0 10px 9px; color: #6e7984; font-size: 9px; }
.recipe-flow-empty { margin: 0; padding: 14px; border: 1px dashed #313943; border-radius: 8px; color: #8d6f55; font-size: 10px; }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 960px) {
  .recipe-flow-overview { grid-template-columns: 1fr; }
  .recipe-flow-overview > div:first-child { border-right: 0; border-bottom: 1px solid #252c35; }
  .recipe-flow-overview > :last-child { justify-self: start; margin: 0 10px 10px; }
  .stage-fact-flow { grid-template-columns: 1fr; }
  .contract-list { max-height: 280px; }
}
@media (max-width: 620px) {
  .recipe-flow-overview dl { grid-template-columns: repeat(2,1fr); }
  .stage-order-section > header,
  .stage-detail-heading { align-items: flex-start; flex-direction: column; }
  .recipe-flow-node { width: 128px; min-height: 74px; }
  .compact-node { width: 116px; min-height: 62px; }
  .stage-meta { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .stage-fact-flow { padding: 8px; }
  .route-selector > div { padding-bottom: 3px; }
}
</style>
