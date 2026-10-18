<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Eye, RefreshCw, Save } from 'lucide-vue-next'
import { executionRegistryApi, loadExecutionEditorCatalogs } from '@/services/executionRegistryApi'
import { adaptExecutionConfiguration, executionDraftPatch, type ExecutionAuthoringOptions, type ExecutionConfiguration, type ExecutionContract, type ExecutionInstance, type ExecutionSelectionOptions, type ExecutionVersion, type ExecutionVersionDetail } from '@/services/executionRegistry'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { authoringIssues, authoringResponseStatus, isExplicitAuthoringRejection, type AuthoringIssue } from '@/services/configurationAuthoring'
import { platformApi } from '@/services/platformApi'
import type { JsonObject, ModelDeployment, PromptProfileVersion, WorkflowTemplateVersion, PromptSourceOption } from '@/services/platformTypes'
import ExecutionConfigurationEditor from './ExecutionConfigurationEditor.vue'
import BusinessOutputManager from './BusinessOutputManager.vue'
import ConfigurationAuthoringShell from './ConfigurationAuthoringShell.vue'
import RegistryResourceExplorer from './RegistryResourceExplorer.vue'
import { businessOutputs, type BusinessOutputVersion } from '@/services/businessOutputs'

const props = defineProps<{ instance: ExecutionInstance; version: ExecutionVersionDetail | null; deployments: ModelDeployment[]; sourceVersion?: ExecutionVersionDetail | null }>()
const emit = defineEmits<{ saved: [version: ExecutionVersionDetail]; recovered: [version: ExecutionVersionDetail]; review: []; dirty: [value: boolean]; processing: [value: boolean]; uncertain: [value: boolean] }>()
const draft = ref<ExecutionConfiguration>(props.version ? JSON.parse(JSON.stringify(props.version.configuration)) : {
  deployment_id: '', operation: '', mode: '', prompt_mode: '' as ExecutionConfiguration['prompt_mode'], prompt_profile_version_id: null, workflow_version_id: null, inputs: [], parameters: [], auxiliaries: [],
})
const options = ref<ExecutionAuthoringOptions | null>(null)
const outputsOpen = ref(false), selectedOutput = ref<BusinessOutputVersion | null>(null), outputError = ref('')
function chooseOutput(row: BusinessOutputVersion) {
  if (readonly.value || busy.value || unresolved.value || loading.value || pendingSelection.value || row.status !== 'published' || !supportedParserKeys.value.includes(row.parser_key)) return
  draft.value.output_version_id = row.id; selectedOutput.value = row; outputError.value = ''; outputsOpen.value = false
}
watch(() => draft.value.output_version_id, async id => {
  selectedOutput.value = null; outputError.value = ''
  if (!id) return
  try {
    const row = await businessOutputs.detail(id)
    if (disposed || id !== draft.value.output_version_id) return
    if (row.id !== id) throw new Error('输出版本身份不匹配，未采用返回详情；原引用保持不变。')
    selectedOutput.value = row
  }
  catch (cause) { if (!disposed && id === draft.value.output_version_id) outputError.value = apiErrorSummary(cause) }
}, { immediate: true })
const profileInputs = ref<JsonObject>()
const inputForms = ref<ExecutionContract['input_forms']>()
const sourceHelp = ref<PromptSourceOption[]>([])
const sourceHelpError = ref('')
const choices = ref<ExecutionSelectionOptions | null>(null)
const profiles = ref<PromptProfileVersion[]>([])
const workflows = ref<WorkflowTemplateVersion[]>([])
const auxiliaryVersions = ref<ExecutionVersion[]>([])
const busy = ref(false), loading = ref(false), error = ref(''), catalogError = ref('')
const issues = ref<AuthoringIssue[]>([])
const recoveryPanel = ref<HTMLElement | null>(null), saveError = ref<HTMLElement | null>(null)
type SaveAttempt = { configuration: ExecutionConfiguration; version: ExecutionVersionDetail | null; revision: number }
const unresolved = ref<SaveAttempt | null>(null), recovering = ref(false), recoveryError = ref(''), recoveryNote = ref('')
const recoveryVersion = ref<ExecutionVersionDetail | null>(null), recoveryVersions = ref<ExecutionVersion[]>([])
const recoveryPage = ref(0), recoveryHasMore = ref(false), retryAllowed = ref(false), revisionConflict = ref(false)
const errorSections = { deployment_id: 'capability', deployment: 'capability', workflow_version_id: 'capability', workflow_version: 'capability', operation: 'capability', mode: 'capability', selection: 'capability',
  inputs: 'inputs', parameters: 'inputs', auxiliaries: 'inputs', prompt_mode: 'inputs', prompt_profile_version_id: 'inputs', prompt_profile_version: 'inputs', output_version_id: 'output', output_version: 'output' }
watch(() => [unresolved.value, error.value], async () => {
  await nextTick()
  const panel = unresolved.value ? recoveryPanel.value : !issues.value.length && error.value ? saveError.value : null
  panel?.scrollIntoView({ block: 'nearest' }); panel?.focus({ preventScroll: true })
})
type SelectionField = 'deployment_id' | 'workflow_version_id' | 'operation' | 'mode' | 'prompt_mode' | 'prompt_profile_version_id'
const pendingSelection = ref<{ field: SelectionField; value: string } | null>(null)
let request = 0
let disposed = false
onBeforeUnmount(() => { request++; disposed = true })
const readonly = computed(() => Boolean(props.version && props.version.status !== 'draft'))
const configurationChanged = computed(() => Boolean(unresolved.value) || (props.version ? JSON.stringify(draft.value) !== JSON.stringify(props.version.configuration) : true))
watch(configurationChanged, value => emit('dirty', value), { immediate: true })
// A successful save replaces this editor. Report completion before that remount
// can dispose queued watchers, and initialise the replacement's parent state.
watch(() => busy.value || recovering.value, value => emit('processing', value), { immediate: true, flush: 'sync' })
watch(() => Boolean(unresolved.value), value => emit('uncertain', value), { immediate: true, flush: 'sync' })
const editingLocked = computed(() => readonly.value || loading.value || busy.value || Boolean(unresolved.value) || Boolean(pendingSelection.value))
function beforeLeave(event: BeforeUnloadEvent) {
  if (!configurationChanged.value && !busy.value) return
  event.preventDefault(); event.returnValue = ''
}
onMounted(() => { if (typeof window !== 'undefined') window.addEventListener('beforeunload', beforeLeave) })
onBeforeUnmount(() => { if (typeof window !== 'undefined') window.removeEventListener('beforeunload', beforeLeave) })
const target = computed(() => choices.value?.target)
const supportedParserKeys = computed(() => target.value?.operations.find(row => row.value === draft.value.operation)?.output_parser_keys ?? [])
const eligibleWorkflows = computed(() => workflows.value.filter(w => w.status === 'published' && w.modelProfileVersion === target.value?.model_profile_version_id && w.validatedDeployment === draft.value.deployment_id))
const eligibleProfiles = computed(() => profiles.value.filter(p => p.status === 'published' && p.modelFamily === target.value?.model_family && (!p.mode || p.mode === draft.value.mode)))
const promptPurpose = computed(() => profiles.value.find(p => p.id === draft.value.prompt_profile_version_id))
const selectionComplete = computed(() => Boolean(choices.value && !catalogError.value && !loading.value && draft.value.deployment_id && draft.value.operation && draft.value.prompt_mode
  && (!target.value?.workflow_required || draft.value.workflow_version_id) && (!target.value?.modes.length || draft.value.mode)
  && (draft.value.prompt_mode !== 'profile' || draft.value.prompt_profile_version_id)))
const canSave = computed(() => !readonly.value && selectionComplete.value && configurationChanged.value && !busy.value && !unresolved.value && !pendingSelection.value)
const authoringSections = computed(() => [
  { key: 'capability', title: '能力与实现', description: '选择要使用的能力及兼容部署。工作流是适用时的实现，内部节点不在这里重新接线。',
    summary: draft.value.operation || '尚未选择能力' },
  { key: 'inputs', title: '输入准备', description: '素材、创作字段、提示词处理和运行参数共同组成模型输入；只配置来源与规则，不在这里启动任务。',
    summary: choices.value?.prompt_modes.find(row => row.value === draft.value.prompt_mode)?.label || '尚未选择提示词方式' },
  { key: 'output', title: '输出约定', description: '查看结果如何解析并提供给下游。这里是配置约定，不是一次生成的实际产物。',
    summary: outputError.value ? '输出版本详情读取失败' : draft.value.output_version_id ? (selectedOutput.value ? `${selectedOutput.value.name} v${selectedOutput.value.version}` : '已引用输出版本，正在核对详情') : '沿所选能力核对输出' },
])
// Session-only comparison of explicit configuration values, not a new contract or lineage writer.
const successorChanges = computed(() => {
  const source = props.sourceVersion
  if (!source || !props.version || source.id === props.version.id || source.instance_id !== props.version.instance_id) return null
  const labels: Record<keyof ExecutionConfiguration, string> = {
    deployment_id: '模型部署', operation: '执行能力', mode: '执行模式', workflow_version_id: '工作流版本',
    prompt_mode: '提示词方式', prompt_profile_version_id: '提示词策略版本', inputs: '输入与来源', parameters: '参数覆盖', auxiliaries: '辅助实例', output_version_id: '输出版本',
  }
  return authoringSections.value.map(section => ({ title: section.title, fields: (Object.keys(labels) as (keyof ExecutionConfiguration)[])
    .filter(key => errorSections[key] === section.key && JSON.stringify(source.configuration[key]) !== JSON.stringify(draft.value[key]))
    .map(key => labels[key]) })).filter(section => section.fields.length)
})
const authoringStatus = computed(() => busy.value ? '正在保存草稿，请勿重复提交…' : recovering.value ? '正在回读服务端记录，不会提交或覆盖配置…'
  : unresolved.value ? (revisionConflict.value ? '修订冲突 · 需核对服务端当前版本' : '保存结果待核对 · 已暂停重复提交') : loading.value ? '正在读取配置目录…'
  : error.value ? '保存失败，表单内容已保留。请检查错误后再提交。' : readonly.value ? '只读版本 · 不修改原配置'
    : !props.version ? '新建版本 · 尚未保存' : configurationChanged.value ? `草稿 v${props.version.version} · 有未保存修改`
      : `草稿 v${props.version.version} · 已保存，修订 ${props.version.revision}`)
const saveLabel = computed(() => busy.value ? '正在保存…' : !props.version ? '创建草稿并配置输入'
  : !options.value ? '保存组合并配置输入' : '保存草稿')
const pendingSelectionLabel = computed(() => {
  const pending = pendingSelection.value
  if (!pending) return ''
  if (pending.field === 'prompt_profile_version_id') {
    const profile = profiles.value.find(row => row.id === pending.value)
    if (profile) return `${profile.name} v${profile.version}`
  }
  return pending.value || '不选择'
})

async function load() {
  const serial = ++request; loading.value = true; catalogError.value = ''
  choices.value = null
  try {
    const catalogs = await loadExecutionEditorCatalogs(props.version, draft.value)
    if (serial !== request) return
    choices.value = catalogs.choices; options.value = catalogs.options; auxiliaryVersions.value = catalogs.auxiliaryVersions
    profileInputs.value = catalogs.profileInputs; inputForms.value = catalogs.inputForms
    profiles.value = catalogs.profiles; workflows.value = catalogs.workflows
  } catch (cause) { if (serial === request) { catalogError.value = apiErrorSummary(cause); options.value = null; choices.value = null; profileInputs.value = undefined; inputForms.value = undefined } }
  finally { if (serial === request) loading.value = false }
}

async function changedSelection(field: SelectionField, value: string) {
  if (readonly.value || busy.value || unresolved.value || loading.value || pendingSelection.value || (draft.value[field] || '') === value) return
  if (draft.value.inputs.length || draft.value.parameters.length || draft.value.auxiliaries.length) {
    pendingSelection.value = { field, value }
    return
  }
  await applySelection(field, value)
}
function cancelSelection() { pendingSelection.value = null }
async function confirmSelection() {
  if (readonly.value || busy.value || unresolved.value || loading.value || !pendingSelection.value) return
  const { field, value } = pendingSelection.value
  pendingSelection.value = null
  await applySelection(field, value)
}
async function applySelection(field: SelectionField, value: string) {
  draft.value = { ...draft.value, [field]: value || (field.endsWith('_version_id') ? null : ''), inputs: [], parameters: [], auxiliaries: [] }
  options.value = null
  profileInputs.value = undefined; inputForms.value = undefined
  if (field === 'deployment_id') { draft.value.workflow_version_id = null; draft.value.operation = ''; draft.value.mode = ''; draft.value.prompt_profile_version_id = null }
  if (field === 'workflow_version_id') { draft.value.operation = ''; draft.value.mode = ''; draft.value.prompt_profile_version_id = null }
  if (field === 'mode' || field === 'prompt_mode') draft.value.prompt_profile_version_id = null
  if (['operation', 'deployment_id', 'workflow_version_id'].includes(field)) delete draft.value.output_version_id
  if (field === 'deployment_id' || field === 'workflow_version_id') {
    const serial = ++request; loading.value = true; catalogError.value = ''
    choices.value = null
    try {
      const metadata = await executionRegistryApi.choices(draft.value.deployment_id || undefined, draft.value.workflow_version_id || undefined)
      if (serial === request) choices.value = metadata
    } catch (cause) { if (serial === request) { catalogError.value = apiErrorSummary(cause); choices.value = null } }
    finally { if (serial === request) loading.value = false }
  }
}
async function save() {
  if (!canSave.value) return
  error.value = ''; issues.value = []
  // Local shape errors are known not to have sent a request. Transport or
  // response-adapter failures after the writer starts have an unknown outcome.
  let configuration: ExecutionConfiguration
  try { configuration = adaptExecutionConfiguration(draft.value) }
  catch (cause) { error.value = apiErrorSummary(cause); return }
  await submitAttempt(JSON.parse(JSON.stringify({ configuration, version: props.version, revision: props.version?.revision ?? props.instance.revision })))
}
async function submitAttempt(attempt: SaveAttempt) {
  busy.value = true; error.value = ''; issues.value = []; retryAllowed.value = false
  try {
    const saved = attempt.version
      ? await executionRegistryApi.updateVersion(attempt.version.id, executionDraftPatch(attempt.version, attempt.configuration))
      : await executionRegistryApi.createVersion(props.instance.id, attempt.revision, attempt.configuration)
    if (saved.instance_id !== props.instance.id || (attempt.version && saved.id !== attempt.version.id)) throw Error('保存响应身份不匹配，需回读原对象核对。')
    if (!disposed) { unresolved.value = null; emit('saved', saved) }
  } catch (cause) {
    if (disposed) return
    error.value = apiErrorSummary(cause)
    issues.value = authoringIssues(cause, errorSections)
    revisionConflict.value = authoringResponseStatus(cause) === 409
    unresolved.value = !isExplicitAuthoringRejection(cause) || revisionConflict.value ? attempt : null
    recoveryVersion.value = null; recoveryVersions.value = []; recoveryHasMore.value = false; recoveryPage.value = 0
    recoveryError.value = ''; recoveryNote.value = ''
  }
  finally { busy.value = false }
}
async function readRecoveryVersions(page: number) {
  const result = await executionRegistryApi.versions(page, { instance: props.instance.id })
  if (result.results.some(row => row.instance_id !== props.instance.id)) throw Error('版本列表身份不匹配，未采用返回记录。')
  if (disposed) return
  recoveryVersions.value = page === 1 ? result.results : [...recoveryVersions.value, ...result.results.filter(row => !recoveryVersions.value.some(existing => existing.id === row.id))]
  recoveryPage.value = page; recoveryHasMore.value = Boolean(result.next)
}
async function readSaveOutcome() {
  const attempt = unresolved.value
  if (!attempt || busy.value || recovering.value) return
  recovering.value = true; recoveryError.value = ''; recoveryNote.value = ''; retryAllowed.value = false
  recoveryVersion.value = null; recoveryVersions.value = []; recoveryHasMore.value = false
  try {
    if (attempt.version) {
      const current = await executionRegistryApi.version(attempt.version.id)
      if (current.id !== attempt.version.id || current.instance_id !== props.instance.id) throw Error('版本身份不匹配，未采用返回详情。')
      if (disposed) return
      recoveryVersion.value = current
      retryAllowed.value = current.revision === attempt.revision && current.status === 'draft'
      recoveryNote.value = retryAllowed.value ? '服务端仍是提交前的草稿修订。可按原修订重试同一份内容；若原请求随后完成，服务端修订校验会拒绝重复写入。'
        : '服务端版本已变化。请比较当前配置与本次提交；不会猜测变更来源，也不会自动覆盖你的内容。'
    } else {
      const family = await executionRegistryApi.instance(props.instance.id)
      if (family.id !== props.instance.id) throw Error('实例身份不匹配，未采用返回记录。')
      if (disposed) return
      retryAllowed.value = family.revision === attempt.revision
      recoveryNote.value = retryAllowed.value ? '实例仍是提交前的修订。可用原修订重试同一份内容；不会改用最新修订重复创建。'
        : '实例修订已变化。下列是该实例的服务端版本，不代表本次请求的回执；请明确选择并核对，系统不会自动认领最新版本。'
      await readRecoveryVersions(1)
    }
  } catch (cause) { if (!disposed) { recoveryError.value = apiErrorSummary(cause); retryAllowed.value = false } }
  finally { recovering.value = false }
}
async function moreRecoveryVersions() {
  if (!unresolved.value || recovering.value || !recoveryHasMore.value) return
  recovering.value = true; recoveryError.value = ''
  try { await readRecoveryVersions(recoveryPage.value + 1) }
  catch (cause) { if (!disposed) recoveryError.value = apiErrorSummary(cause) }
  finally { recovering.value = false }
}
async function inspectRecoveryVersion(row: ExecutionVersion) {
  if (!unresolved.value || busy.value || recovering.value || !recoveryVersions.value.some(item => item.id === row.id)) return
  recovering.value = true; recoveryError.value = ''; recoveryVersion.value = null
  try {
    const current = await executionRegistryApi.version(row.id)
    if (current.id !== row.id || current.instance_id !== props.instance.id) throw Error('版本身份不匹配，未采用返回详情。')
    if (!disposed) recoveryVersion.value = current
  } catch (cause) { if (!disposed) recoveryError.value = apiErrorSummary(cause) }
  finally { recovering.value = false }
}
async function retrySameAttempt() {
  if (!unresolved.value || !retryAllowed.value || busy.value || recovering.value) return
  await submitAttempt(unresolved.value)
}
function useRecoveryVersion() {
  if (!unresolved.value || !recoveryVersion.value || busy.value || recovering.value) return
  // Explicitly load an existing version, not an assertion that our write made it.
  emit('recovered', recoveryVersion.value)
}
onMounted(load)
onMounted(async () => {
  try { const catalog = await platformApi.getPromptProfileOptions(); if (!disposed) sourceHelp.value = catalog.sourceSelectors }
  catch (cause) { if (!disposed) sourceHelpError.value = apiErrorSummary(cause) }
})
</script>

<template>
  <RegistryResourceExplorer label="执行实例表单" v-slot="{ inspect }">
  <form class="execution-version-form" @submit.prevent="save">
    <ConfigurationAuthoringShell label="执行实例配置" :sections="authoringSections" :status="authoringStatus" :busy="busy||loading||recovering" :issues="issues">
    <template #feedback>
    <section v-if="successorChanges" class="platform-alert" aria-label="后继配置变更范围">
      <strong>与本次来源 v{{ sourceVersion?.version }} 相比</strong>
      <ul v-if="successorChanges.length"><li v-for="section in successorChanges" :key="section.title">{{ section.title }}：{{ section.fields.join('、') }}</li></ul>
      <p v-else>配置尚未改变，可在下面调整独立草稿。</p>
      <p>比较包含当前未保存修改；保存状态以作者提示为准。仅显示本次创建上下文，不推断历史版本的来源。</p>
    </section>
    <p v-if="error&&!unresolved" ref="saveError" class="platform-inline-error" role="alert" tabindex="-1">{{ error }}</p>
    <section v-if="unresolved" ref="recoveryPanel" class="platform-alert warning" aria-label="核对保存结果" tabindex="-1">
      <strong>{{ revisionConflict?'本次请求遇到修订冲突':'未收到可确认的保存结果' }}</strong>
      <p>已保留并锁定本次提交内容。先回读原对象，不要重新新建；回读不会保存、发布或运行模型。</p>
      <details v-if="error"><summary>查看返回错误（不代表尚未写入）</summary><p>{{ error }}</p></details>
      <button type="button" class="button ghost small" :disabled="busy||recovering" @click="readSaveOutcome">{{ recovering?'正在回读…':'回读服务端记录' }}</button>
      <p v-if="recoveryNote" role="status">{{ recoveryNote }}</p>
      <p v-if="recoveryError" role="alert">回读失败：{{ recoveryError }}。结果仍待核对，不会自动重发。</p>
      <details><summary>本次提交的配置 · 原修订 {{ unresolved.revision }}</summary><pre>{{ JSON.stringify(unresolved.configuration,null,2) }}</pre></details>
      <div v-if="recoveryVersions.length"><p>服务端版本（仅供核对）：</p><button v-for="row in recoveryVersions" :key="row.id" type="button" class="button ghost small" :disabled="busy||recovering" @click="inspectRecoveryVersion(row)">查看 {{ row.instance_name }} v{{ row.version }} · {{ row.status }} · 修订 {{ row.revision }}</button></div>
      <button v-if="recoveryHasMore" type="button" class="button ghost small" :disabled="busy||recovering" @click="moreRecoveryVersions">读取更多版本</button>
      <div v-if="recoveryVersion"><p>已回读 {{ recoveryVersion.instance_name }} v{{ recoveryVersion.version }} · {{ recoveryVersion.status }} · 修订 {{ recoveryVersion.revision }}</p><details><summary>查看服务端当前配置</summary><pre>{{ JSON.stringify(recoveryVersion.configuration,null,2) }}</pre></details><button type="button" class="button ghost small" :disabled="busy||recovering" @click="useRecoveryVersion">加载此版本（替换本地表单）</button></div>
      <button v-if="retryAllowed" type="button" class="button primary small" :disabled="busy||recovering" @click="retrySameAttempt">按原修订重试本次内容</button>
    </section>
    <div v-if="catalogError" class="platform-alert danger" role="alert"><p>选项目录未能读取：{{ catalogError }}</p><button type="button" class="button ghost small" :disabled="loading" @click="load"><RefreshCw :size="14"/>重试目录</button></div>
    <section v-if="pendingSelection" class="platform-alert warning" role="region" aria-label="更换执行组合确认">
      <strong>确认更换为：{{ pendingSelectionLabel }}</strong>
      <p>更换组合会清空本表单的输入接线、参数覆盖和辅助配置。保存后按新策略重新派生输入；物理槽位及覆盖需要重新核对。此处确认不保存服务端、不运行模型。</p>
      <button type="button" class="button ghost small" @click="cancelSelection">保留原组合</button>
      <button type="button" class="button primary small" @click="confirmSelection">确认更换组合</button>
    </section>
    </template>
    <template #capability>
    <fieldset :disabled="editingLocked" class="execution-version-grid">
      <label data-authoring-field="deployment_id">模型部署<select :value="draft.deployment_id" required @change="changedSelection('deployment_id',($event.target as HTMLSelectElement).value)"><option value="">请选择模型部署</option><option v-if="draft.deployment_id&&!deployments.some(d=>d.id===draft.deployment_id)" :value="draft.deployment_id">当前引用 {{ draft.deployment_id }}（目录不可见）</option><option v-for="deployment in deployments" :key="deployment.id" :value="deployment.id">{{ deployment.name }} · {{ deployment.providerDriver }} · {{ deployment.status }}</option></select></label>
      <label data-authoring-field="workflow_version_id" v-if="target?.workflow_required||draft.workflow_version_id">工作流版本<select :value="draft.workflow_version_id||''" required @change="changedSelection('workflow_version_id',($event.target as HTMLSelectElement).value)"><option value="">请选择已发布工作流</option><option v-if="draft.workflow_version_id&&!eligibleWorkflows.some(w=>w.id===draft.workflow_version_id)" :value="draft.workflow_version_id">当前引用 {{ draft.workflow_version_id }}</option><option v-for="workflow in eligibleWorkflows" :key="workflow.id" :value="workflow.id">{{ workflow.templateName }} v{{ workflow.version }}</option></select></label>
      <label data-authoring-field="operation">执行能力<select :value="draft.operation" required @change="changedSelection('operation',($event.target as HTMLSelectElement).value)"><option value="">请选择目标声明的能力</option><option v-if="draft.operation&&!target?.operations.some(o=>o.value===draft.operation)" :value="draft.operation">{{ draft.operation }}（当前引用）</option><option v-for="choice in target?.operations" :key="choice.value" :value="choice.value">{{ choice.label }} · {{ choice.value }}</option></select></label>
      <label data-authoring-field="mode">执行模式<select :value="draft.mode" :required="Boolean(target?.modes.length)" @change="changedSelection('mode',($event.target as HTMLSelectElement).value)"><option value="">{{ target?.modes.length?'请选择模式':'目标未声明模式' }}</option><option v-if="draft.mode&&!target?.modes.some(o=>o.value===draft.mode)" :value="draft.mode">{{ draft.mode }}（当前引用）</option><option v-for="choice in target?.modes" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
    </fieldset>
    <div class="execution-resource-links"><button v-if="draft.deployment_id" type="button" class="button ghost small" @click="inspect('deployment',draft.deployment_id,$event)">查看所选部署</button><button v-if="target?.model_profile_version_id" type="button" class="button ghost small" @click="inspect('model',target.model_profile_version_id,$event)">查看模型能力定义</button><button v-if="draft.workflow_version_id" type="button" class="button ghost small" @click="inspect('workflow',draft.workflow_version_id,$event)">查看工作流输入与输出</button></div>
    </template>
    <template #inputs>
    <fieldset :disabled="editingLocked" class="execution-version-grid">
      <label data-authoring-field="prompt_mode">提示词方式<select :value="draft.prompt_mode" required @change="changedSelection('prompt_mode',($event.target as HTMLSelectElement).value)"><option value="">请选择提示词方式</option><option v-for="choice in choices?.prompt_modes" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
      <label data-authoring-field="prompt_profile_version_id" v-if="draft.prompt_mode==='profile'">提示词策略版本<select :value="draft.prompt_profile_version_id||''" required @change="changedSelection('prompt_profile_version_id',($event.target as HTMLSelectElement).value)"><option value="">请选择已发布策略</option><option v-if="draft.prompt_profile_version_id&&!eligibleProfiles.some(p=>p.id===draft.prompt_profile_version_id)" :value="draft.prompt_profile_version_id">当前引用 {{ draft.prompt_profile_version_id }}</option><option v-for="profile in eligibleProfiles" :key="profile.id" :value="profile.id">{{ profile.name }} v{{ profile.version }} · {{ profile.operation }}</option></select></label>
    </fieldset>
    <p v-if="draft.prompt_mode==='profile'">提示词用途（只读）：{{ promptPurpose?.operation || version?.prompt_operation || '选择策略后显示' }}。策略负责文本输入的处理与组装，辅助实例仅在规则需要时使用。</p>
    <p v-else-if="draft.prompt_mode==='manual'">本实例使用人工完整提示词。具体正文在创建生成任务时填写；不需要另配策略或辅助 AI。</p>
    <p v-else-if="draft.prompt_mode==='none'">此组合明确不使用提示词；仍需按所选能力配置其他输入与参数。</p>
    <div class="execution-resource-links"><button v-if="draft.prompt_mode==='profile'&&draft.prompt_profile_version_id" type="button" class="button ghost small" @click="inspect('prompt',draft.prompt_profile_version_id,$event)">查看策略来源、字段与通道</button><button v-for="input in draft.inputs.filter(item=>item.data_structure_version_id)" :key="input.key" type="button" class="button ghost small" @click="inspect('structure',input.data_structure_version_id!,$event)">查看 {{ input.key }} 的结构定义</button><button v-for="auxiliary in draft.auxiliaries" :key="auxiliary.purpose" type="button" class="button ghost small" @click="inspect('execution',auxiliary.target_version_id,$event)">查看辅助实例 · {{ auxiliary.purpose }}</button></div>
    <p v-if="sourceHelpError" role="alert">来源说明未读取：{{ sourceHelpError }}。配置值未改动，请重新打开页面读取说明。</p>
    <template v-if="choices&&version"><ExecutionConfigurationEditor v-model="draft" :options="options" :profile-inputs="profileInputs" :input-forms="inputForms" :choices="choices" :auxiliary-versions="auxiliaryVersions" :sources="sourceHelp" :readonly="editingLocked||!options"/></template>
    <p v-if="!version&&!readonly">先选择能力、实现和提示词方式，再点击“创建草稿并配置输入”。保存后读取该组合的服务端目录，不会生成或运行模型。</p>
    <p v-if="version&&!readonly&&!options&&!catalogError">组合已在本表单变更。请先保存组合，再按新目录配置输入与参数；不会继续使用旧目录。</p>
    </template>
    <template #output>
    <section data-authoring-field="output_version_id" tabindex="-1" v-if="supportedParserKeys.length||draft.output_version_id" class="platform-alert" aria-label="业务输出版本">
      <strong>确切业务输出版本</strong>
      <p>{{ selectedOutput ? `${selectedOutput.name} · v${selectedOutput.version} · ${selectedOutput.status}` : draft.output_version_id || '尚未选择。发布及新执行前须选择已发布输出版本。' }}</p>
      <p v-if="outputError" role="alert">输出版本读取失败：{{ outputError }}；不会替换为最新版本。</p>
      <p>解析字段、质量建议及版本摘要随本次合同冻结；发布新输出版本不会自动改绑已有执行实例。</p>
      <button v-if="draft.output_version_id" type="button" class="button ghost small" @click="inspect('output',draft.output_version_id,$event)">查看所选输出定义</button>
      <button type="button" class="button ghost small" :disabled="busy||loading||Boolean(unresolved)||Boolean(pendingSelection)||(!readonly&&!supportedParserKeys.length)" @click="outputsOpen=true">{{ readonly?'查看输出版本':'选择／管理输出版本' }}</button>
    </section>
    <BusinessOutputManager v-if="outputsOpen" :choose="!readonly" :initial-id="draft.output_version_id" :supported-parser-keys="supportedParserKeys" @select="chooseOutput" @close="outputsOpen=false"/>
    <p v-if="!supportedParserKeys.length&&!draft.output_version_id">当前目录未提供可选业务输出解析器，不在此新增一份输出定义。保存后可在“复核完整合同”查看目标原有输出；尚未读取的声明不补猜。</p>
    </template>
    <template #actions><p>保存仅更新草稿。发布、校验与实际生成是独立操作，不会自动开始。</p><button v-if="!readonly" type="submit" class="button primary small" :disabled="!canSave"><Save :size="14"/>{{ saveLabel }}</button><button v-if="version" type="button" class="button ghost small" :disabled="configurationChanged||busy" :title="configurationChanged?'请先保存草稿后复核':'读取精确完整定义，不运行模型'" @click="emit('review')"><Eye :size="14"/>复核完整合同</button></template>
    </ConfigurationAuthoringShell>
  </form>
  </RegistryResourceExplorer>
</template>

<style scoped>
.execution-resource-links{display:flex;gap:var(--config-space-sm);flex-wrap:wrap}.execution-resource-links:empty{display:none}.execution-resource-links .button{white-space:normal}
.execution-version-form section[aria-label="业务输出版本"]{display:grid;gap:8px;min-width:0;overflow-wrap:anywhere}.execution-version-form section[aria-label="业务输出版本"] button{justify-self:start}
.execution-version-form pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:var(--config-help-size);max-height:280px;overflow:auto}.execution-version-form section[aria-label="核对保存结果"] button{white-space:normal;max-width:100%}
.execution-version-form section[aria-label="核对保存结果"]>strong{font-size:var(--config-body-size)}
.execution-version-form{min-width:0}.execution-version-form p{color:var(--text-secondary);font-size:var(--config-help-size);overflow-wrap:anywhere;line-height:1.6}.execution-version-form .platform-inline-error{color:var(--red);font-size:var(--config-body-size)}.execution-version-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--config-space-lg);padding:0;margin:0;border:0;min-width:0}.execution-version-grid label{display:grid;gap:var(--config-space-sm);font-size:var(--config-body-size)}.execution-version-grid select{width:100%;min-width:0}.execution-version-form .platform-alert{display:grid;gap:var(--config-space-sm)}@media(max-width:640px){.execution-version-grid{grid-template-columns:1fr}}
</style>
