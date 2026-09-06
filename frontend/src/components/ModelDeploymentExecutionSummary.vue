<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { BookOpenCheck, Workflow, ShieldCheck, RefreshCw } from 'lucide-vue-next'
import ConfigurationRelationButton from './ConfigurationRelationButton.vue'
import DeploymentCapabilitySmoke from './DeploymentCapabilitySmoke.vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { deploymentWorkflowSignal, workflowExecutionSignal, workflowExecutionUrl } from '@/services/modelDeploymentExecution'
import { modelPromptSettingsRoute, promptBindingModelFamily } from '@/services/modelPromptBindings'
import { modeLabel, operationLabel } from '@/services/presentationLabels'
import type { AcceptanceSmokeDeploymentRow, AcceptanceSmokeWorkflowRow, ModelDeployment, ModelProfileVersion, PromptProfileBinding } from '@/services/platformTypes'

const props = defineProps<{
  deployment: ModelDeployment; profile: ModelProfileVersion; acceptance: AcceptanceSmokeDeploymentRow | null
  acceptanceError?: string; acceptanceLoading?: boolean; bindings: PromptProfileBinding[]; bindingsLoading: boolean; bindingsError: string
}>()
defineEmits<{ showBindings: []; retryBindings: [] }>()
const latestAcceptance = ref<AcceptanceSmokeDeploymentRow | null>(null)
const localReadSucceeded = ref(false)
const signal = computed(() => deploymentWorkflowSignal(loading.value ? null : latestAcceptance.value, props.acceptanceError || error.value))
const activeBindings = computed(() => props.bindings.filter(row => row.isActive && row.promptProfileStatus === 'published'))
const workflows = ref<AcceptanceSmokeWorkflowRow[]>([])
const loading = ref(false); const error = ref(''); const page = ref(1); const count = ref(0); const hasNext = ref(false)
const currentWorkflows = computed(() => workflows.value.filter(row => row.association.validatedDeploymentMatches && row.workflow.status !== 'retired'))
const otherWorkflows = computed(() => workflows.value.filter(row => !currentWorkflows.value.includes(row)))
let serial = 0
const refresh = async (requestedPage = 1) => {
  const current = ++serial
  workflows.value = []; latestAcceptance.value = null; error.value = ''; loading.value = true; localReadSucceeded.value = false
  const deploymentId = props.deployment.id
  try {
    // Refresh the count and the named evidence together; do not retain a green
    // aggregate from page entry after a newer workflow check has failed.
    const [response, aggregate] = await Promise.all([
      props.deployment.providerDriver === 'comfyui'
        ? platformApi.getAcceptanceSmokePage({ subject: 'workflow', deploymentId, page: requestedPage, pageSize: 5 })
        : Promise.resolve({ results: [], count: 0, next: null }),
      platformApi.getAcceptanceSmokePage({ subject: 'deployment', deploymentId, page: 1, pageSize: 1 }),
    ])
    if (current !== serial) return
    const instanceRow = aggregate.results[0]
    if (aggregate.count !== 1 || aggregate.results.length !== 1 || instanceRow?.subject !== 'deployment' || instanceRow.deployment.id !== deploymentId) throw new Error('实例验收摘要缺失或不匹配，请重新读取')
    const rows: AcceptanceSmokeWorkflowRow[] = []
    for (const row of response.results) {
      if (row.subject !== 'workflow' || row.association.deploymentId !== deploymentId) throw new Error('工作流验收返回了不匹配的实例，请重新读取')
      rows.push(row)
    }
    workflows.value = rows; latestAcceptance.value = instanceRow; count.value = response.count; page.value = requestedPage; hasNext.value = Boolean(response.next); localReadSucceeded.value = true
  } catch (cause) {
    if (current === serial) error.value = apiErrorSummary(cause, '实例验收读取失败')
  } finally { if (current === serial) loading.value = false }
}
watch(() => [props.deployment.id, props.deployment.updatedAt, props.acceptance, props.acceptanceError] as const, () => {
  serial += 1; workflows.value = []; error.value = ''; latestAcceptance.value = props.acceptance; loading.value = false; localReadSucceeded.value = false
  if (props.deployment.providerDriver === 'comfyui') void refresh()
}, { immediate: true })
onBeforeUnmount(() => { serial += 1 })
</script>

<template>
  <section class="deployment-execution-summary" aria-label="实例烟测与关联配置">
    <div v-if="deployment.providerDriver==='comfyui'" class="execution-workflows">
      <header>
        <span><ShieldCheck :size="14" aria-hidden="true"/>工作流烟测</span>
        <span class="execution-signal" :class="signal.tone" :title="signal.description">{{ signal.label }}</span>
        <button class="button ghost tiny" :disabled="loading" aria-label="重新读取工作流烟测" @click="refresh()"><RefreshCw :size="12"/></button>
      </header>
      <p v-if="acceptanceError" role="alert">{{ acceptanceError }}</p>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-else-if="loading" role="status">正在读取对应工作流…</p>
      <template v-else>
        <p v-if="!workflows.length">尚无对应工作流，可在“管理工作流关联”中配置并验收。</p>
        <ConfigurationRelationButton v-for="row in currentWorkflows" :key="row.workflow.id" :icon="Workflow"
          label="工作流"
          :name="`${row.workflow.templateName} v${row.workflow.version}`"
          :status="workflowExecutionSignal(row,deployment.id).label" :tone="workflowExecutionSignal(row,deployment.id).tone"
          :description="workflowExecutionSignal(row,deployment.id).description" :to="workflowExecutionUrl(row,deployment.id)"/>
        <details v-if="otherWorkflows.length"><summary>本页其他候选 / 历史（{{ otherWorkflows.length }}）</summary>
          <ConfigurationRelationButton v-for="row in otherWorkflows" :key="row.workflow.id" :icon="Workflow"
            label="候选 / 历史" :name="`${row.workflow.templateName} v${row.workflow.version}`"
            :status="workflowExecutionSignal(row,deployment.id).label" :tone="workflowExecutionSignal(row,deployment.id).tone"
            :description="workflowExecutionSignal(row,deployment.id).description" :to="workflowExecutionUrl(row,deployment.id)"/>
        </details>
        <div v-if="count>workflows.length || page>1" class="workflow-pages">
          <span>共 {{ count }} 项 · 第 {{ page }} 页（含候选与历史）</span>
          <button class="button ghost tiny" :disabled="loading || page===1" @click="refresh(page-1)">上一页</button>
          <button class="button ghost tiny" :disabled="loading || !hasNext" @click="refresh(page+1)">下一页</button>
        </div>
      </template>
    </div>
    <DeploymentCapabilitySmoke v-else :row="latestAcceptance" :loading="loading || acceptanceLoading" :error="error || (!localReadSucceeded ? acceptanceError : '')" @refresh="refresh()"/>
    <div class="execution-prompts">
      <header><span><BookOpenCheck :size="14" aria-hidden="true"/>适用提示词策略</span><button class="button ghost tiny" @click="$emit('showBindings')">{{ bindingsLoading || bindingsError ? '查看绑定' : `全部 ${bindings.length} 条绑定` }}</button></header>
      <p v-if="bindingsError" role="alert">{{ bindingsError }} <button class="button ghost tiny" @click="$emit('retryBindings')">重试</button></p>
      <p v-else-if="bindingsLoading" role="status">正在读取提示词绑定…</p>
      <template v-else>
        <p v-if="!promptBindingModelFamily(profile)">模型族未配置：仅显示精确版本绑定，目录可能不完整。</p>
        <p v-if="!activeBindings.length">暂无启用且已发布的适用策略。<RouterLink :to="modelPromptSettingsRoute(profile.id,'',deployment.id)">配置提示词绑定</RouterLink></p>
        <ConfigurationRelationButton v-for="binding in activeBindings.slice(0,2)" :key="binding.id" :icon="BookOpenCheck"
          :label="operationLabel(binding.operation)" :name="`${binding.promptProfileName || binding.promptProfileKey} v${binding.promptProfileVersionNumber}`"
          :status="binding.mode ? modeLabel(binding.mode) : '通用模式'" tone="info"
          description="模型版本的适用策略，不代表某次任务实际选用；执行时按操作、模式及绑定选择冻结。"
          :to="modelPromptSettingsRoute(profile.id,binding.promptProfileVersion,deployment.id)"/>
        <button v-if="activeBindings.length>2" class="button ghost tiny" @click="$emit('showBindings')">查看全部 {{ activeBindings.length }} 条启用策略</button>
        <p>按操作与模式选择；任务实际使用的策略见执行记录。</p>
      </template>
    </div>
  </section>
</template>

<style scoped>
.deployment-execution-summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;grid-column:1/-1;min-width:0;padding-top:10px;border-top:1px solid var(--border)}
.execution-workflows,.execution-prompts{display:grid;align-content:start;gap:6px;min-width:0}
summary{cursor:pointer;font-size:11px;min-height:30px;line-height:30px;color:var(--text-secondary)}details>.configuration-relation{margin-top:5px}summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
header,header>span,.workflow-pages{display:flex;gap:6px;align-items:center;flex-wrap:wrap}header{min-height:30px}header>span:first-child{font-weight:600;color:var(--text)}header>button{margin-left:auto}
.execution-signal{font-size:11px;color:var(--text-secondary)}.execution-signal.linked{color:var(--green)}.execution-signal.warning{color:var(--accent)}.execution-signal.danger{color:var(--red)}
p{margin:0;font-size:11px;color:var(--text-secondary);line-height:1.6;overflow-wrap:anywhere}p[role=alert]{color:var(--red)}.workflow-pages{font-size:10px;color:var(--text-secondary)}
@media(max-width:1100px){.deployment-execution-summary{grid-template-columns:minmax(0,1fr)}}
</style>
