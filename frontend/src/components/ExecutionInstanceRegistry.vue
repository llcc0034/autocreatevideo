<script setup lang="ts">
import { taskCenterTarget } from '@/services/taskCenter'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Copy, Eye, Plus, RefreshCw, Search } from 'lucide-vue-next'
import BaseModal from './BaseModal.vue'
import BusinessOutputManager from './BusinessOutputManager.vue'
import PlatformStatusPill from './PlatformStatusPill.vue'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import ExecutionVersionEditor from './ExecutionVersionEditor.vue'
import ExecutionInstanceEditor from './ExecutionInstanceEditor.vue'
import ConfigurationDiscardNotice from './ConfigurationDiscardNotice.vue'
import ExecutionPathOverview from './ExecutionPathOverview.vue'
import ExecutionParameterContract from './ExecutionParameterContract.vue'
import ExecutionParameterMappings from './ExecutionParameterMappings.vue'
import ExecutionSmokeEvidence from './ExecutionSmokeEvidence.vue'
import RegistryRelationSummary from './RegistryRelationSummary.vue'
import RegistryDeletionReviewModal from './RegistryDeletionReviewModal.vue'
import { useRegistryRelations } from '@/services/registryRelations'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import { executionLifecyclePayload, type ExecutionContract, type ExecutionInstance, type ExecutionPage, type ExecutionVersion, type ExecutionVersionDetail } from '@/services/executionRegistry'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { isExplicitAuthoringRejection } from '@/services/configurationAuthoring'
import type { ModelDeployment } from '@/services/platformTypes'

defineProps<{ deployments: ModelDeployment[] }>()
const page = ref(1), versionPage = ref(1), query = ref(''), appliedQuery = ref('')
const outputsOpen = ref(false)
const instances = ref<ExecutionPage<ExecutionInstance> | null>(null), versions = ref<ExecutionPage<ExecutionVersion> | null>(null)
const selected = ref<ExecutionInstance | null>(null), detail = ref<ExecutionVersionDetail | null>(null)
const contract = ref<ExecutionContract | null>(null)
const evidenceVersion = ref<ExecutionVersion | null>(null)
const evidenceBusy = ref(false), evidenceDirty = ref(false)
const evidenceLeave = ref<'close' | 'configuration' | null>(null)
const loading = ref(false), versionLoading = ref(false), detailLoading = ref(false), busy = ref(false)
const error = ref(''), versionError = ref(''), detailError = ref(''), notice = ref('')
const editorOpen = ref(false), editorKey = ref(0), contractOpen = ref(false), familyOpen = ref(false)
const versionView = ref<'overview' | 'editor'>('overview')
const pathLoading = ref(false), pathError = ref('')
const editorDirty = ref(false), editorBusy = ref(false), editorUncertain = ref(false)
const familySource = ref<ExecutionInstance | null>(null), familyDirty = ref(false), familyUncertain = ref(false)
const cloneUnknown = ref<ExecutionVersionDetail | null>(null), cloneSource = ref<ExecutionVersionDetail | null>(null)
const cloneRecords = ref<ExecutionVersion[]>([]), cloneReading = ref(false), cloneReadError = ref(''), cloneReadDone = ref(false)
const clonePage = ref(0), cloneMore = ref(false)
const leaveTarget = ref<'family' | 'version' | null>(null)
const leaveMessage = computed(() => leaveTarget.value === 'family'
  ? familyUncertain.value ? '保存结果未知。关闭不会撤销可能已保存的实例；请先核对目录，勿重复新建。' : '关闭将丢弃未保存的实例信息。'
  : cloneUnknown.value ? '后继创建结果未知。关闭不会撤销可能已创建的草稿；请先回读版本目录，不要重复创建。'
  : editorUncertain.value ? '保存结果未知。关闭只丢弃本地核对上下文，不会撤销可能已完成的保存；请先查看版本记录。' : '关闭将丢弃此表单尚未保存的修改。')
const action = ref<'publish' | 'retire' | null>(null), reason = ref('')
const route = useRoute()
const router = useRouter()
const deletionTarget = ref<{ type: 'model_execution_instance' | 'model_execution_instance_version'; id: string; title: string } | null>(null)
const versionRelations = useRegistryRelations('execution', () => versionLoading.value || versionError.value ? [] : versions.value?.results || [])
const contextError = ref('')
let listRequest = 0, versionRequest = 0, detailRequest = 0, contextRequest = 0
let disposed = false
onBeforeUnmount(() => { disposed = true; listRequest++; versionRequest++; detailRequest++; contextRequest++ })
const detailTitle = computed(() => detail.value ? `${detail.value.instance_name} · v${detail.value.version}` : `${selected.value?.name || ''} · ${detailLoading.value ? '读取精确版本' : '新版本草稿'}`)

async function loadInstances(requestedPage = page.value, search = appliedQuery.value) {
  const serial = ++listRequest; loading.value = true; error.value = ''
  try {
    const result = await executionRegistryApi.instances(requestedPage, search)
    if (serial !== listRequest) return
    instances.value = result; page.value = requestedPage; appliedQuery.value = search
    if (selected.value) selected.value = result.results.find(r => r.id === selected.value!.id) || selected.value
  } catch (cause) { if (serial === listRequest) error.value = apiErrorSummary(cause) }
  finally { if (serial === listRequest) loading.value = false }
}
async function choose(instance: ExecutionInstance, fromContext = false) {
  if (busy.value) return
  if (!fromContext) contextRequest++
  selected.value = instance; versions.value = null; versionPage.value = 1
  await loadVersions()
}
async function loadVersions(requestedPage = versionPage.value) {
  const instanceId = selected.value?.id
  if (!instanceId) return
  const serial = ++versionRequest; versionLoading.value = true; versionError.value = ''
  try {
    const [current, result] = await Promise.all([executionRegistryApi.instance(instanceId), executionRegistryApi.versions(requestedPage, { instance: instanceId })])
    if (serial !== versionRequest || instanceId !== selected.value?.id) return
    selected.value = current; versions.value = result; versionPage.value = requestedPage
  } catch (cause) { if (serial === versionRequest) versionError.value = apiErrorSummary(cause) }
  finally { if (serial === versionRequest) versionLoading.value = false }
}
async function openVersion(version: ExecutionVersion) {
  if (busy.value || editorBusy.value || (editorOpen.value && editorDirty.value) || cloneReading.value || cloneUnknown.value) return
  leaveTarget.value = null
  editorDirty.value = false; editorUncertain.value = false
  cloneSource.value = null
  contextRequest++
  const serial = ++detailRequest; detailLoading.value = true; detailError.value = ''; contract.value = null; detail.value = null; editorOpen.value = true
  try {
    const result = await executionRegistryApi.version(version.id)
    if (result.id !== version.id || result.instance_id !== version.instance_id) throw Error('返回的版本身份与所选对象不一致。')
    if (serial === detailRequest && editorOpen.value) {
      detail.value = result; editorKey.value++; versionView.value = result.status==='draft' ? 'editor' : 'overview'
      if (versionView.value==='overview') await readPath()
    }
  } catch (cause) { if (serial === detailRequest) detailError.value = apiErrorSummary(cause) }
  finally { if (serial === detailRequest) detailLoading.value = false }
}
async function readPath() {
  const viewed = detail.value
  if (!viewed) return
  const serial = detailRequest
  pathLoading.value = true; pathError.value = ''; contract.value = null
  try {
    const result = await executionRegistryApi.contract(viewed.id)
    if (serial !== detailRequest || detail.value?.id !== viewed.id || !editorOpen.value) return
    executionLifecyclePayload(viewed,result); contract.value=result
  } catch (cause) { if (serial===detailRequest) pathError.value=apiErrorSummary(cause,'链路合同读取失败') }
  finally { if (serial===detailRequest) pathLoading.value=false }
}
function showPath() {
  if (editorDirty.value || editorBusy.value || busy.value) return
  versionView.value='overview'; void readPath()
}
function evidenceConfiguration() {
  requestEvidenceLeave('configuration')
}
function closeEvidence() {
  requestEvidenceLeave('close')
}
function requestEvidenceLeave(destination: 'close' | 'configuration') {
  if (evidenceBusy.value) return
  evidenceLeave.value = destination
  if (!evidenceDirty.value) confirmEvidenceLeave()
}
function confirmEvidenceLeave() {
  if (evidenceBusy.value || !evidenceLeave.value) return
  const destination = evidenceLeave.value, version = evidenceVersion.value
  evidenceLeave.value = null
  evidenceVersion.value = null; evidenceDirty.value = false
  if (destination === 'configuration' && version) void openVersion(version)
}
function closeEditor() {
  if (busy.value || editorBusy.value || cloneReading.value) return
  if (cloneUnknown.value || editorDirty.value || editorUncertain.value) { leaveTarget.value = 'version'; return }
  finishCloseEditor()
}
function finishCloseEditor() {
  leaveTarget.value = null
  detailRequest++; editorOpen.value = false; detailLoading.value = false; action.value = null; contractOpen.value = false
  editorDirty.value = false; editorUncertain.value = false
  cloneUnknown.value = null; cloneSource.value = null
}
function createVersion() { if (busy.value || cloneReading.value || cloneUnknown.value) return; cloneSource.value = null; contextRequest++; detailRequest++; detail.value = null; contract.value = null; detailError.value = ''; detailLoading.value = false; editorKey.value++; versionView.value='editor'; editorOpen.value = true }
async function saved(version: ExecutionVersionDetail, recovered = false) {
  leaveTarget.value = null
  versionView.value='editor'
  detail.value = version; contract.value = null; editorKey.value++; notice.value = recovered
    ? `已载入服务端 ${version.instance_name} v${version.version} · ${version.status}，未重新保存或认定变更来源。`
    : `已保存 ${version.instance_name} v${version.version} 草稿，尚未发布。`
  await loadVersions()
}
async function review() {
  if (!detail.value || busy.value) return
  const viewed = detail.value; busy.value = true; detailError.value = ''; contract.value = null
  try {
    const result = await executionRegistryApi.contract(viewed.id)
    executionLifecyclePayload(viewed, result)
    contract.value = result; contractOpen.value = true
  } catch (cause) { detailError.value = apiErrorSummary(cause) }
  finally { busy.value = false }
}
async function clone() {
  if (!detail.value || busy.value || editorBusy.value || editorDirty.value || cloneUnknown.value || cloneReading.value) return
  const source = detail.value
  let payload: { expected_revision: number; expected_definition_sha256: string | null }
  try { payload = source.status === 'draft' ? { expected_revision: source.revision, expected_definition_sha256: null } : executionLifecyclePayload(source, contract.value) }
  catch (cause) { detailError.value = apiErrorSummary(cause); return }
  busy.value = true; detailError.value = ''; cloneSource.value = null
  try {
    const result = await executionRegistryApi.clone(source.id, payload)
    if (result.id === source.id || result.instance_id !== source.instance_id || result.status !== 'draft') throw Error('后继响应身份或状态不符合独立草稿，需回读核对。')
    if (disposed) return
    cloneSource.value = source; contractOpen.value = false; await saved(result)
    notice.value = `已基于 ${source.instance_name} v${source.version} 创建独立草稿 v${result.version}，原版本及使用方引用不变。`
  } catch (cause) {
    if (disposed) return
    detailError.value = apiErrorSummary(cause)
    if (!isExplicitAuthoringRejection(cause)) {
      cloneUnknown.value = source; cloneRecords.value = []; cloneReadDone.value = false; cloneMore.value = false; cloneReadError.value = ''; contractOpen.value = false
    }
  }
  finally { busy.value = false }
}
async function readCloneRecords(pageNumber = 1) {
  const source = cloneUnknown.value
  if (!source || cloneReading.value || busy.value) return
  cloneReading.value = true; cloneReadError.value = ''
  if (pageNumber === 1) { cloneRecords.value = []; cloneMore.value = false; cloneReadDone.value = false }
  try {
    const result = await executionRegistryApi.versions(pageNumber, { instance: source.instance_id })
    if (result.results.some(row => row.instance_id !== source.instance_id)) throw Error('版本列表包含其他实例，未合并展示。')
    if (disposed || cloneUnknown.value?.id !== source.id) return
    cloneRecords.value = pageNumber === 1 ? result.results : [...cloneRecords.value, ...result.results.filter(row => !cloneRecords.value.some(existing => existing.id === row.id))]
    clonePage.value = pageNumber; cloneMore.value = Boolean(result.next); cloneReadDone.value = true
  } catch (cause) { if (!disposed) cloneReadError.value = apiErrorSummary(cause) }
  finally { cloneReading.value = false }
}
async function loadCloneRecord(candidate: ExecutionVersion) {
  const source = cloneUnknown.value
  if (!source || cloneReading.value || busy.value || candidate.instance_id !== source.instance_id || candidate.id === source.id) return
  cloneReading.value = true; cloneReadError.value = ''
  try {
    const result = await executionRegistryApi.version(candidate.id)
    if (result.id !== candidate.id || result.instance_id !== source.instance_id) throw Error('精确版本身份不符，未载入。')
    if (disposed || cloneUnknown.value?.id !== source.id) return
    cloneUnknown.value = null; cloneSource.value = null; detailError.value = ''; await saved(result, true)
  } catch (cause) { if (!disposed) cloneReadError.value = apiErrorSummary(cause) }
  finally { cloneReading.value = false }
}
async function lifecycle() {
  if (!detail.value || !action.value || busy.value) return
  busy.value = true; detailError.value = ''
  try {
    const payload = executionLifecyclePayload(detail.value, contract.value)
    const result = action.value === 'publish' ? await executionRegistryApi.publish(detail.value.id, payload) : await executionRegistryApi.retire(detail.value.id, { ...payload, reason: reason.value })
    detail.value = result; editorKey.value++; contractOpen.value = false; action.value = null
    notice.value = result.status === 'published' ? '精确版本已发布。发布不代表组合烟测通过，也不触发生成。' : '版本已退役，新调用不得再选用此版本。'
    await loadVersions()
  } catch (cause) { detailError.value = apiErrorSummary(cause) }
  finally { busy.value = false }
}
function openFamily(editing: boolean) {
  if (busy.value || cloneUnknown.value) return
  leaveTarget.value = null
  familySource.value = editing && selected.value ? { ...selected.value } : null
  familyDirty.value = false; familyUncertain.value = false; familyOpen.value = true
}
function closeFamily() {
  if (busy.value) return
  if (familyDirty.value || familyUncertain.value) { leaveTarget.value = 'family'; return }
  familyOpen.value = false
}
function discardAuthoring() {
  if (busy.value || editorBusy.value || cloneReading.value) return
  if (leaveTarget.value === 'family') { familyOpen.value = false; familyDirty.value = false; familyUncertain.value = false; leaveTarget.value = null }
  else if (leaveTarget.value === 'version') finishCloseEditor()
}
async function familySaved(result: ExecutionInstance, recovered: boolean) {
  const created = !familySource.value && !recovered
  leaveTarget.value = null
  familyOpen.value = false; familyDirty.value = false; familyUncertain.value = false; busy.value = false
  selected.value = result; versions.value = null
  notice.value = recovered ? '已载入所选服务端实例，未重新保存或认定本次请求成功。' : created ? '实例信息已创建；请继续配置首个版本。尚未创建版本或触发执行。' : '实例信息已保存；已有版本配置与使用方引用不变。'
  if (created) createVersion()
  await Promise.all([loadInstances(), loadVersions(1)])
}
async function toggleInstance() {
  if (!selected.value || busy.value) return
  const viewed = selected.value
  if (!window.confirm(`${viewed.is_active?'停用':'启用'}执行实例“${viewed.name}”？此操作影响后续调用的实例门禁，不改写已发布版本内容，也不运行烟测。`)) return
  busy.value = true; error.value = ''
  try { selected.value = await executionRegistryApi.updateInstance(viewed.id, { expected_revision: viewed.revision, is_active: !viewed.is_active }); await loadInstances() }
  catch (cause) { error.value = apiErrorSummary(cause) }
  finally { busy.value = false }
}
async function followVersionContext() {
  const serial = ++contextRequest
  const id = route.query.execution_version
  contextError.value = ''
  if (!id) return
  if (typeof id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) {
    contextError.value = '执行版本地址无效，请返回关联目录重新选择。'; return
  }
  if (familyOpen.value || cloneUnknown.value || editorBusy.value || editorDirty.value || evidenceBusy.value || evidenceDirty.value || busy.value || deletionTarget.value) {
    contextError.value = '请先完成或关闭当前配置，再重新读取关联版本。'; return
  }
  // Navigation reads the exact version even when it is not on the first page.
  // Do not search by name or choose the first/latest version as a fallback.
  detailRequest++; detail.value = null; editorOpen.value = false; evidenceVersion.value = null
  try {
    const value = await executionRegistryApi.version(id)
    if (serial !== contextRequest) return
    if (value.id !== id) throw new Error('返回的执行版本与关联地址不一致')
    const instance = await executionRegistryApi.instance(value.instance_id)
    if (serial !== contextRequest) return
    if (instance.id !== value.instance_id) throw new Error('执行实例归属不一致')
    if (familyOpen.value || cloneUnknown.value || editorDirty.value || editorBusy.value || evidenceDirty.value || evidenceBusy.value || busy.value || deletionTarget.value) throw new Error('当前操作已变化，请完成后重新读取关联版本')
    await choose(instance, true)
    if (serial !== contextRequest) return
    if (route.query.execution_view === 'smoke' && value.status !== 'draft') evidenceVersion.value = value
    else { detail.value = value; contract.value = null; detailError.value = ''; detailLoading.value = false; editorKey.value++; editorOpen.value = true
      versionView.value=value.status==='draft' ? 'editor' : 'overview'
      if (versionView.value==='overview') await readPath()
    }
  } catch (cause) { if (serial === contextRequest) contextError.value = apiErrorSummary(cause, '关联版本读取失败') }
}
function reviewDeletion(kind: 'version' | 'instance') {
  if (busy.value || editorBusy.value || editorDirty.value || evidenceBusy.value || evidenceDirty.value || deletionTarget.value) return
  if (kind === 'version') {
    if (!detail.value || detail.value.status !== 'draft') return
    deletionTarget.value = { type: 'model_execution_instance_version', id: detail.value.id, title: `删除草稿 · ${detailTitle.value}` }
  } else {
    if (!selected.value || selected.value.is_active) return
    deletionTarget.value = { type: 'model_execution_instance', id: selected.value.id, title: `删除空实例 · ${selected.value.name}` }
  }
  contextRequest++
}
async function deletionCompleted() {
  const deleted = deletionTarget.value
  if (!deleted) return
  contextRequest++; detailRequest++; versionRequest++; listRequest++
  deletionTarget.value = null; detail.value = null; contract.value = null; editorOpen.value = false
  editorDirty.value = false; action.value = null; contractOpen.value = false; contextError.value = ''
  if (deleted.type === 'model_execution_instance') { selected.value = null; versions.value = null }
  if (route.query.execution_version === deleted.id || deleted.type === 'model_execution_instance') {
    const query = { ...route.query }; delete query.execution_version; delete query.execution_view
    await router.replace({ query })
  }
  notice.value = '已永久删除复核目标及清单中的已确认配置。引用的共享资源未删除。'
  await loadInstances(1)
  if (selected.value) await loadVersions(1)
}
watch(() => [route.query.execution_version, route.query.execution_view], () => { void followVersionContext() }, { immediate: true })
function beforeLeave(event: BeforeUnloadEvent) { if (cloneUnknown.value || busy.value) { event.preventDefault(); event.returnValue = '' } }
onMounted(() => { void loadInstances(); if (typeof window !== 'undefined') window.addEventListener('beforeunload', beforeLeave) })
onBeforeUnmount(() => { if (typeof window !== 'undefined') window.removeEventListener('beforeunload', beforeLeave) })
</script>

<template>
  <section class="execution-registry">
    <button type="button" class="button ghost small" @click="outputsOpen=true">管理业务输出版本</button>
    <BusinessOutputManager v-if="outputsOpen" @close="outputsOpen=false"/>
    <p>选择精确版本，查看输入准备 → 模型能力调用 → 输出。公共资源按需下钻，配置关系不等于真实执行或烟测结果。</p>
    <p v-if="notice" class="platform-alert success" role="status">{{ notice }}</p>
    <div v-if="contextError" class="platform-alert danger" role="alert">{{ contextError }}<button type="button" class="button ghost small" @click="followVersionContext">重新读取关联版本</button></div>
    <div v-if="error" class="platform-alert danger" role="alert"><p>{{ error }}<br/>若列表已显示，仍是上次成功读取的结果。</p><button type="button" class="button ghost small" :disabled="loading" @click="loadInstances()">重试列表</button></div>
    <form class="execution-registry-toolbar" @submit.prevent="loadInstances(1,query)"><label>搜索执行实例<input v-model="query" placeholder="名称、标识或说明"/></label><button class="button ghost small" :disabled="loading"><Search :size="14"/>搜索</button><button type="button" class="button ghost small" :disabled="loading" @click="loadInstances()"><RefreshCw :size="14"/>重新读取</button><button type="button" class="button primary small" :disabled="busy" @click="openFamily(false)"><Plus :size="14"/>新建执行实例</button></form>
    <div class="execution-registry-layout">
      <div class="execution-instance-list" :aria-busy="loading">
        <p v-if="loading">正在读取执行实例…</p><p v-else-if="instances?.count===0">当前搜索下没有执行实例</p>
        <button v-for="instance in instances?.results" :key="instance.id" type="button" :class="['execution-instance-row',{selected:selected?.id===instance.id}]" :disabled="busy" :aria-pressed="selected?.id===instance.id" @click="choose(instance)"><strong>{{ instance.name }}</strong><small>{{ instance.key }}</small><span>{{ instance.is_active?'已启用':'已停用' }}</span></button>
        <nav v-if="instances" class="execution-pagination" aria-label="执行实例分页"><span>共 {{ instances.count }} 项 · 第 {{ page }} 页</span><button type="button" class="button ghost tiny" :disabled="loading||!instances.previous" @click="loadInstances(page-1)">上一页</button><button type="button" class="button ghost tiny" :disabled="loading||!instances.next" @click="loadInstances(page+1)">下一页</button></nav>
      </div>
      <div class="execution-version-list" :aria-busy="versionLoading">
        <p v-if="!selected">选择执行实例，查看全部版本与精确配置。</p>
        <template v-else>
          <header><div><h3>{{ selected.name }}</h3><p>{{ selected.description || '未填写说明' }}</p></div><button type="button" class="button ghost small" :disabled="busy||versionLoading" @click="openFamily(true)">编辑信息</button><button type="button" class="button ghost small" :disabled="busy||versionLoading" @click="toggleInstance">{{ selected.is_active?'停用实例':'启用实例' }}</button><button type="button" class="button primary small" :disabled="busy||versionLoading||Boolean(versionError)" @click="createVersion"><Plus :size="14"/>新建版本</button></header>
          <button v-if="!selected.is_active" type="button" class="button danger small" :disabled="busy||versionLoading||Boolean(versionError)" @click="reviewDeletion('instance')">删除空实例（复核）</button>
          <p v-if="versionLoading">正在读取版本…</p><div v-if="versionError" class="platform-alert danger" role="alert"><p>{{ versionError }}；已显示内容不是新读取结果。</p><button type="button" class="button ghost small" :disabled="versionLoading" @click="loadVersions()">重试版本</button></div>
          <p v-if="versions?.count===0">尚未配置版本。新建草稿后，按目标合同配置输入与参数，再复核发布。</p>
          <div class="execution-table-wrap" v-if="versions?.results.length"><table><thead><tr><th>版本</th><th>执行能力 / 模式</th><th>提示词用途</th><th>操作</th></tr></thead><tbody><tr v-for="version in versions.results" :key="version.id"><td>v{{ version.version }}<PlatformStatusPill :status="version.status"/></td><td><strong>{{ version.operation }}</strong><small>{{ version.mode || '未声明模式' }}</small></td><td><strong>{{ version.prompt_operation ?? (version.prompt_mode==='manual'?'人工完整提示词':'无需提示词') }}</strong><small>{{ version.prompt_profile_mode || '' }}</small></td><td><button type="button" class="button ghost small" :disabled="busy" @click="openVersion(version)"><Eye :size="14"/>{{ version.status==='draft'?'配置草稿':'查看配置' }}</button><button v-if="version.status !== 'draft'" type="button" class="button ghost small" :disabled="busy" @click="evidenceVersion = version">烟测与验收</button><RouterLink v-if="version.status !== 'draft'" class="button ghost small" :to="taskCenterTarget({versionId:version.id})">执行历史</RouterLink><RegistryRelationSummary :row="versionRelations.rows.value[version.id]" :loading="versionRelations.loading.value" :error="versionRelations.error.value" @retry="versionRelations.reload" condensed/></td></tr></tbody></table></div>
          <nav v-if="versions" class="execution-pagination" aria-label="执行版本分页"><span>共 {{ versions.count }} 个版本 · 第 {{ versionPage }} 页</span><button type="button" class="button ghost tiny" :disabled="versionLoading||!versions.previous" @click="loadVersions(versionPage-1)">上一页</button><button type="button" class="button ghost tiny" :disabled="versionLoading||!versions.next" @click="loadVersions(versionPage+1)">下一页</button></nav>
        </template>
      </div>
    </div>
    <BaseModal configuration :open="familyOpen" :title="familySource?'编辑执行实例信息':'新建执行实例'" eyebrow="执行能力 · 实例身份与版本" width="720px" :dismissible="!busy" @close="closeFamily"><ConfigurationDiscardNotice v-if="leaveTarget==='family'" :message="leaveMessage" :busy="busy" @keep="leaveTarget=null" @discard="discardAuthoring"/><ExecutionInstanceEditor v-if="familyOpen" :instance="familySource" @saved="familySaved" @dirty="familyDirty=$event" @processing="busy=$event" @uncertain="familyUncertain=$event"/></BaseModal>
    <BaseModal :open="evidenceVersion !== null" :title="evidenceVersion ? `${evidenceVersion.instance_name} · v${evidenceVersion.version} · 组合烟测` : ''" width="1120px" :dismissible="!evidenceBusy" @close="closeEvidence">
      <ExecutionSmokeEvidence v-if="evidenceVersion" :key="evidenceVersion.id" :version-id="evidenceVersion.id" :can-create="evidenceVersion.status==='published'" @configuration="evidenceConfiguration" @busy="evidenceBusy = $event" @dirty="evidenceDirty = $event"/>
    </BaseModal>
    <BaseModal configuration :open="editorOpen&&!contractOpen" :title="detailTitle" eyebrow="执行实例版本 · 精确组合配置" width="1120px" :dismissible="!busy&&!editorBusy&&!cloneReading" @close="closeEditor">
      <ConfigurationDiscardNotice v-if="leaveTarget==='version'" :message="leaveMessage" :busy="busy||editorBusy||cloneReading" @keep="leaveTarget=null" @discard="discardAuthoring"/>
      <p v-if="detailLoading">正在读取精确版本…</p><p v-if="detailError&&!cloneUnknown" class="platform-inline-error" role="alert">{{ detailError }}</p>
      <p v-if="busy" role="status">正在提交配置操作，请勿重复点击…</p>
      <section v-if="cloneSource" class="execution-successor-context" aria-label="本次后继来源"><strong>独立后继草稿 · 基于 {{ cloneSource.instance_name }} v{{ cloneSource.version }}</strong><p>原版本和配方等使用方引用保持不变。以下仅编辑新草稿；保存、发布及真实执行是不同动作。</p><details><summary>本次创建的精确来源</summary><p>{{ cloneSource.id }} · 修订 {{ cloneSource.revision }}</p></details></section>
      <section v-if="cloneUnknown" class="execution-successor-context" aria-label="核对后继创建结果">
        <strong role="alert">后继创建结果未知 · 已阻止重复创建</strong><p>来源：{{ cloneUnknown.instance_name }} v{{ cloneUnknown.version }}。先回读本实例的版本，不按名称或最新版猜测结果。载入记录不证明本次请求创建了它。</p><details><summary>原错误详情</summary>{{ detailError }}</details>
        <button type="button" class="button ghost small" :disabled="cloneReading" @click="readCloneRecords()">{{ cloneReading?'正在回读版本…':'回读版本目录' }}</button><p v-if="cloneReadError" role="alert">{{ cloneReadError }}</p><p v-if="cloneReadDone&&!cloneRecords.length">本页没有记录；不代表请求未完成，请勿重复创建。</p>
        <article v-for="record in cloneRecords" :key="record.id"><strong>{{ record.instance_name }} v{{ record.version }} · {{ record.status }}</strong><p>{{ record.operation }} · {{ record.mode }} · 创建 {{ record.created_at || '时间未提供' }}</p><small>{{ record.id }}</small><button type="button" class="button ghost small" :disabled="cloneReading||record.id===cloneUnknown.id" @click="loadCloneRecord(record)">{{ record.id===cloneUnknown.id?'原来源版本':'查看并载入此精确版本' }}</button></article>
        <button v-if="cloneMore" type="button" class="button ghost small" :disabled="cloneReading" @click="readCloneRecords(clonePage+1)">查看更多版本</button>
      </section>
      <nav v-if="detail&&!detailLoading&&!cloneUnknown" class="execution-registry-toolbar" aria-label="执行实例版本视图"><button type="button" class="button ghost small" :aria-pressed="versionView==='overview'" :disabled="editorDirty||editorBusy||busy" @click="showPath">链路与输入输出</button><button type="button" class="button ghost small" :aria-pressed="versionView==='editor'" :disabled="busy||pathLoading" @click="versionView='editor'">{{ detail.status==='draft'?'编辑组合配置':'查看组合定义' }}</button></nav>
      <template v-if="versionView==='overview'&&!detailLoading&&!cloneUnknown">
        <p v-if="pathLoading" role="status">正在读取此精确版本的链路合同…</p>
        <div v-else-if="pathError" class="platform-alert danger" role="alert">{{ pathError }}<button type="button" class="button ghost small" @click="readPath">重新读取链路</button></div>
        <ExecutionPathOverview v-else-if="detail&&contract" :key="contract.sha256" :version="detail" :contract="contract"/>
      </template>
      <div v-if="versionView==='editor'&&selected&&!detailLoading&&!cloneUnknown&&!(!detail&&detailError)" :inert="busy||cloneReading" :aria-busy="busy||cloneReading" class="execution-editor-surface"><ExecutionVersionEditor :key="editorKey" :instance="selected" :version="detail" :source-version="cloneSource" :deployments="deployments" @saved="saved" @recovered="saved($event,true)" @review="review" @dirty="editorDirty=$event" @processing="editorBusy=$event" @uncertain="editorUncertain=$event"/></div>
      <button v-if="detail?.status==='draft'&&!cloneUnknown" type="button" class="button danger small" :disabled="busy||editorBusy||editorDirty" @click="reviewDeletion('version')">删除草稿（复核）</button>
      <div v-if="detail&&!cloneUnknown" class="execution-registry-toolbar"><button type="button" class="button ghost small" :disabled="busy||editorBusy||editorDirty||(detail.status!=='draft'&&!contract)" :title="editorDirty?'请先保存当前草稿':detail.status!=='draft'&&!contract?'先复核冻结合同':'创建独立后继草稿，不改原版本'" @click="clone"><Copy :size="14"/>创建后继草稿</button></div>
    </BaseModal>
    <BaseModal configuration :open="contractOpen" :title="`${detailTitle} · 合同复核`" width="1120px" :dismissible="!busy" @close="contractOpen=false;action=null">
      <template v-if="contract&&detail">
        <p>{{ contract.frozen?'此处是该版本的冻结合同，不重新计算历史配置。':'此处是刚读取的完整草稿定义，发布时会再次检查修订与SHA。' }}</p><p class="execution-contract-digest">合同 SHA：<code>{{ contract.sha256 }}</code> · {{ contract.size_bytes }} B</p><p>执行能力：{{ detail.operation }}；提示词用途：{{ detail.prompt_operation ?? '不使用策略' }}。配置复核成功不代表可提交或组合烟测通过。</p>
        <ExecutionParameterContract :key="`parameters-${contract.sha256}`" :interface="contract.definition.interface"/>
        <ExecutionParameterMappings :key="`mappings-${contract.version_id}-${contract.revision}-${contract.sha256}`" :version-id="contract.version_id" :revision="contract.revision" :sha256="contract.sha256"/>
        <ReadonlyDataValue :key="contract.sha256" :value="contract.definition" label="完整合同（只读）"/>
        <p v-if="detailError" class="platform-inline-error" role="alert">{{ detailError }}</p>
        <form v-if="action" @submit.prevent="lifecycle" class="execution-lifecycle-form"><p>{{ action==='publish'?'发布将冻结该精确版本的组合和输入合同；不自动烟测或生成。':'退役后不能用于新请求；不会改写版本配置。' }}</p><label v-if="action==='retire'">退役原因<input v-model="reason" required maxlength="191"/></label><button class="button primary small" :disabled="busy">{{ busy?'正在提交…':action==='publish'?'确认发布此版本':'确认退役此版本' }}</button><button type="button" class="button ghost small" :disabled="busy" @click="action=null">取消</button></form>
        <div v-else class="execution-registry-toolbar"><button v-if="detail.status==='draft'" type="button" class="button primary small" :disabled="busy" @click="action='publish'">发布此版本</button><button v-if="detail.status==='published'" type="button" class="button danger small" :disabled="busy" @click="action='retire';reason=''">退役此版本</button><button type="button" class="button ghost small" :disabled="busy" @click="clone">创建后继草稿</button></div>
      </template>
    </BaseModal>
    <BaseModal :open="evidenceLeave !== null" title="放弃未提交的烟测表单修改？" :dismissible="!evidenceBusy" @close="evidenceLeave=null">
      <p>关闭将丢弃尚未保存的字段或新烟测表单修改，不会删除已创建的任务，也不会取消正在执行的模型调用。</p>
      <p>如果提交结果未知，请先继续编辑并用同一请求确认结果，避免重复创建任务。</p>
      <template #footer><button type="button" class="button ghost small" :disabled="evidenceBusy" @click="evidenceLeave=null">继续编辑</button><button type="button" class="button danger small" :disabled="evidenceBusy" @click="confirmEvidenceLeave">放弃修改并继续</button></template>
    </BaseModal>
  </section>
  <RegistryDeletionReviewModal v-if="deletionTarget" :open="true" :resource-type="deletionTarget.type" :object-id="deletionTarget.id" :title="deletionTarget.title" @close="deletionTarget=null" @completed="deletionCompleted"/>
</template>

<style scoped>
.execution-contract-digest{overflow-wrap:anywhere}
.execution-lifecycle-form{display:grid;gap:var(--config-space-md)}.execution-lifecycle-form label{display:grid;gap:var(--config-space-sm)}
.execution-successor-context{display:grid;gap:var(--config-space-md);padding:var(--config-space-lg) 0;border-block:1px solid var(--border);font-size:var(--config-body-size);overflow-wrap:anywhere}.execution-successor-context article{display:grid;gap:var(--config-space-sm);border-top:1px solid var(--border);padding-top:var(--config-space-md)}.execution-successor-context button{white-space:normal;justify-self:start}.execution-successor-context strong{font-size:var(--config-body-size)}.execution-successor-context small{font-size:var(--config-help-size)}
.execution-editor-surface{min-width:0}@media(max-width:760px){.execution-successor-context button{min-height:44px}}
.execution-registry{display:grid;gap:14px;min-width:0}.execution-registry p,.execution-registry small{font-size:12px;line-height:1.6;color:var(--text-secondary);overflow-wrap:anywhere}.execution-registry-toolbar{display:flex;align-items:end;gap:8px;flex-wrap:wrap}.execution-registry-toolbar label{display:grid;gap:6px;flex:1;min-width:180px}.execution-registry-layout{display:grid;grid-template-columns:minmax(220px,28%) minmax(0,1fr);border-top:1px solid var(--border)}.execution-instance-list{padding:10px;border-right:1px solid var(--border)}.execution-version-list{padding:14px;min-width:0}.execution-instance-row{display:grid;gap:5px;text-align:left;width:100%;padding:12px;background:transparent;border:1px solid var(--border);border-radius:6px;margin-bottom:8px;color:inherit;cursor:pointer}.execution-instance-row.selected{border-color:var(--accent,#e6a75a)}.execution-instance-row strong,.execution-instance-row small{overflow-wrap:anywhere}.execution-version-list header{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.execution-version-list header>div{flex:1;min-width:150px}.execution-pagination{display:flex;align-items:center;flex-wrap:wrap;gap:8px;font-size:12px;padding:12px 0}.execution-table-wrap{overflow-x:auto}.execution-table-wrap table{width:100%;border-collapse:collapse}.execution-table-wrap th,.execution-table-wrap td{padding:12px 8px;text-align:left;font-size:12px;border-bottom:1px solid var(--border);overflow-wrap:anywhere}.execution-table-wrap small{display:block}.execution-registry code{overflow-wrap:anywhere}.execution-instance-row:focus-visible{outline:2px solid var(--accent,#e6a75a);outline-offset:2px}@media(max-width:760px){.execution-registry-layout{grid-template-columns:1fr}.execution-instance-list{border-right:0;border-bottom:1px solid var(--border)}.execution-version-list{padding:12px 0}}
</style>
