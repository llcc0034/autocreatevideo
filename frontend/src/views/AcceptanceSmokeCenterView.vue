<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AlertTriangle, ChevronLeft, ChevronRight, RefreshCw, Search, Server, ShieldCheck, Workflow } from 'lucide-vue-next'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import DeploymentCapabilitySmoke from '@/components/DeploymentCapabilitySmoke.vue'
import SettingsPrimaryNav from '@/components/SettingsPrimaryNav.vue'
import RegistryRelationsPanel from '@/components/RegistryRelationsPanel.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { platformApi } from '@/services/platformApi'
import type {
  AcceptanceSmokeDeploymentRow,
  AcceptanceSmokeEvidenceSummary,
  AcceptanceSmokePage,
  AcceptanceSmokeSubject,
  AcceptanceSmokeWorkflowRow,
} from '@/services/platformTypes'
import {
  deploymentAcceptanceNeedsAttention,
  deploymentHealthDisplayStatus,
  workflowAcceptanceEvidenceState,
  workflowAcceptanceNeedsAttention,
  workflowAssociationLabel,
} from '@/services/workflowAcceptance'

const route = useRoute()
const router = useRouter()
const pageSize = 25
const pageData = ref<AcceptanceSmokePage | null>(null)
const loadedQueryKey = ref('')
const loading = ref(false)
const refreshing = ref(false)
const error = ref('')
const loadedAt = ref('')
const searchDraft = ref('')
let requestSequence = 0

const subject = computed<AcceptanceSmokeSubject>(() => route.query.subject === 'deployment' ? 'deployment' : 'workflow')
const showRelations = computed(() => route.query.view === 'relations' || (!route.query.subject && !route.query.deployment_id && !route.query.workflow_id))
const page = computed(() => {
  const value = Number(route.query.page || 1)
  return Number.isSafeInteger(value) && value > 0 ? value : 1
})
const search = computed(() => String(route.query.search || '').trim())
const deploymentId = computed(() => String(route.query.deployment_id || '').trim())
const queryKey = computed(() => JSON.stringify({
  subject: subject.value,
  page: page.value,
  pageSize,
  search: search.value,
  deploymentId: deploymentId.value,
  workflowId: String(route.query.workflow_id || ''),
  showRelations: showRelations.value,
}))
const resultIsCurrent = computed(() => Boolean(pageData.value && loadedQueryKey.value === queryKey.value))
const rows = computed(() => resultIsCurrent.value ? pageData.value?.results || [] : [])
const workflowRows = computed(() => rows.value.filter((row): row is AcceptanceSmokeWorkflowRow => row.subject === 'workflow'))
const deploymentRows = computed(() => rows.value.filter((row): row is AcceptanceSmokeDeploymentRow => row.subject === 'deployment'))
const pageCount = computed(() => Math.max(1, Math.ceil((pageData.value?.count || 0) / pageSize)))
const pageStart = computed(() => pageData.value?.count ? ((page.value - 1) * pageSize) + 1 : 0)
const pageEnd = computed(() => Math.min(page.value * pageSize, pageData.value?.count || 0))
const workflowAttentionCount = computed(() => workflowRows.value.filter(workflowAcceptanceNeedsAttention).length)
const deploymentAttentionCount = computed(() => deploymentRows.value.filter(deploymentAcceptanceNeedsAttention).length)

const message = (cause: unknown) => apiErrorSummary(cause, '验收与烟测事实读取失败')
const dateTime = (value: string) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
const shortHash = (value: string) => value ? value.slice(0, 12) : '—'
const deploymentLabel = (row: AcceptanceSmokeWorkflowRow | AcceptanceSmokeDeploymentRow) => {
  const deployment = row.deployment
  if (!deployment) return '尚未绑定 Deployment'
  return deployment.name || deployment.key || deployment.id
}
const evidenceStatus = (value: AcceptanceSmokeEvidenceSummary) => value.fresh ? 'fresh' : value.state || value.storedState || 'not_run'
const actionKindLabel = (kind: string) => ({
  live_validate: 'Live Validate', workflow_smoke: 'Workflow Smoke', publish: '发布',
  deployment_smoke: 'Deployment Smoke', runtime_host_probe: '运行主机探测',
  deployment_probe: '实例探测', view_workflow_associations: '查看 Workflow 关联',
  resolve_workflow_smoke_uncertain: '处理待消歧 Workflow Smoke',
}[kind] || kind)
const reasonCodeLabel = (code: string) => ({
  ready: '可执行',
  validation_target_unavailable: '当前实例或模型合同不可用于校验',
  workflow_validation_precheck_failed: '工作流合同或实例目标预检失败',
  workflow_smoke_resolution_required: '需先处理待消歧任务',
  workflow_not_validated_for_selected_deployment: '需先对当前实例执行 Live Validate',
  live_validation_stale: 'Live Validate 未完成或已失效',
  workflow_smoke_stale: 'Workflow Smoke 未通过或已失效',
  attempt_in_progress_or_resolution_required: '已有执行中或待消歧任务',
  deployment_smoke_not_applicable: '当前实例不支持直接 Smoke',
  model_profile_contract_invalid: '模型合同不可核验',
  deployment_target_invalid: '实例目标不可核验',
  deployment_inactive: '实例未启用',
  use_workflow_smoke: '请通过精确 Workflow 完成验收与烟测',
  deployment_smoke_not_configured: '模型未声明可用的直接 Smoke operation',
}[code] || code || '不可用')
const actionSummary = (row: AcceptanceSmokeWorkflowRow | AcceptanceSmokeDeploymentRow) => row.actions
  .map((action) => `${actionKindLabel(action.kind)}${action.enabled ? '' : `（${reasonCodeLabel(action.reasonCode)}）`}`)
  .join('、')
const workflowFocus = (row: AcceptanceSmokeWorkflowRow): 'validate' | 'smoke' | 'publish' => {
  if (row.smoke.latestAttempt?.resolutionRequired) return 'smoke'
  if (!row.association.validatedDeploymentMatches) return 'validate'
  if (workflowAcceptanceEvidenceState(row.validation) !== 'fresh') return 'validate'
  if (!row.smoke.fresh || row.smoke.storedState !== 'passed') return 'smoke'
  return 'publish'
}
const workflowActionLabel = (row: AcceptanceSmokeWorkflowRow) => {
  if (row.workflow.status === 'published' || row.workflow.status === 'retired') return '查看验收证据'
  if (row.smoke.latestAttempt?.resolutionRequired) return '先处理待消歧 Smoke'
  const focus = workflowFocus(row)
  if (focus === 'validate') return row.validation.storedState === 'passed' ? '重新 Live Validate' : '继续 Live Validate'
  if (focus === 'smoke') return '填写并创建 Workflow Smoke'
  return '核对发布'
}
const workflowTarget = (row: AcceptanceSmokeWorkflowRow) => ({
  name: 'workflow-version-editor',
  params: { workflowVersionId: row.workflow.id },
  query: {
    focus: workflowFocus(row),
    ...(row.deployment?.id ? { deployment_id: row.deployment.id } : {}),
  },
})
const deploymentPrimaryAction = (row: AcceptanceSmokeDeploymentRow): { to: string; label: string } | null => {
  if (row.deployment.providerDriver === 'comfyui') {
    const associationAction = row.actions.find((action) => action.kind === 'view_workflow_associations' && action.enabled)
    return associationAction ? { to: associationAction.uiUrl, label: '查看关联 Workflow 验收' } : null
  }
  const smokeAction = row.deploymentSmoke.operations
    .map((operation) => operation.action)
    .find((action) => action.enabled)
  if (smokeAction) return { to: smokeAction.uiUrl, label: '前往模型实例 Smoke' }
  const probeAction = row.actions.find((action) => action.kind === 'deployment_probe' && action.enabled)
  return probeAction ? { to: probeAction.uiUrl, label: '前往模型实例探测' } : null
}
const deploymentPrimaryTarget = (row: AcceptanceSmokeDeploymentRow) => deploymentPrimaryAction(row)?.to || ''
const deploymentPrimaryLabel = (row: AcceptanceSmokeDeploymentRow) => deploymentPrimaryAction(row)?.label || '当前无可执行动作'

const replaceQuery = async (patch: Record<string, string | number | undefined>) => {
  const next = { ...route.query } as Record<string, unknown>
  Object.entries(patch).forEach(([key, value]) => {
    if (value === undefined || value === '') delete next[key]
    else next[key] = String(value)
  })
  await router.replace({ path: route.path, query: next as Record<string, string> })
}
const selectSubject = (value: AcceptanceSmokeSubject) => router.push({ path: route.path, query: { subject: value } })
const submitSearch = () => replaceQuery({ search: searchDraft.value.trim() || undefined, page: 1 })
const clearDeploymentFilter = () => replaceQuery({ deployment_id: undefined, page: 1 })
const goPage = (value: number) => {
  if (value < 1 || value > pageCount.value || value === page.value) return
  void replaceQuery({ page: value })
}

const load = async (force = false) => {
  const key = queryKey.value
  const sequence = ++requestSequence
  if (showRelations.value) { loading.value = false; refreshing.value = false; pageData.value = null; return }
  const canPreserve = force && loadedQueryKey.value === key && Boolean(pageData.value)
  if (canPreserve) refreshing.value = true
  else {
    loading.value = true
    pageData.value = null
    loadedQueryKey.value = ''
  }
  error.value = ''
  try {
    const result = await platformApi.getAcceptanceSmokePage({
      subject: subject.value,
      page: page.value,
      pageSize,
      search: search.value,
      deploymentId: deploymentId.value,
      workflowId: String(route.query.workflow_id || ''),
    })
    if (sequence !== requestSequence || key !== queryKey.value) return
    pageData.value = result
    loadedQueryKey.value = key
    loadedAt.value = new Date().toISOString()
  } catch (cause) {
    if (sequence !== requestSequence || key !== queryKey.value) return
    error.value = message(cause)
  } finally {
    if (sequence === requestSequence) {
      loading.value = false
      refreshing.value = false
    }
  }
}

watch(search, (value) => { searchDraft.value = value }, { immediate: true })
watch(queryKey, () => { void load() }, { immediate: true })
</script>

<template>
  <div class="platform-page acceptance-smoke-center">
    <header class="platform-page-header">
      <div>
        <span class="eyebrow">验收与烟测 / ACCEPTANCE &amp; SMOKE</span>
        <h2>能力关联与验收中心</h2>
        <p>集中查看模型实例与 Workflow 的关联、历史验收、当前证据新鲜度和下一步动作。此页只读；真实提交仍在各对象唯一执行页完成。</p>
      </div>
      <button v-if="!showRelations" class="button ghost small" type="button" :disabled="loading||refreshing" @click="load(true)"><RefreshCw :size="14" :class="{spin:refreshing}"/>{{ refreshing ? '刷新中…' : '重新读取' }}</button>
    </header>

    <SettingsPrimaryNav/>

    <nav class="acceptance-subject-tabs" aria-label="验收对象类型">
      <button type="button" :class="{active:showRelations}" :aria-pressed="showRelations" @click="router.push({path:route.path,query:{view:'relations'}})"><span><strong>能力与关联总览</strong><small>配置关系、缺口说明与原页面入口</small></span></button>
      <button type="button" :class="{active:!showRelations&&subject==='workflow'}" :aria-pressed="!showRelations&&subject==='workflow'" @click="selectSubject('workflow')"><Workflow :size="16"/><span><strong>Workflow 验收</strong><small>Live Validate、Workflow Smoke 与发布门禁</small></span></button>
      <button type="button" :class="{active:!showRelations&&subject==='deployment'}" :aria-pressed="!showRelations&&subject==='deployment'" @click="selectSubject('deployment')"><Server :size="16"/><span><strong>模型实例 Smoke</strong><small>实例健康、直接 Smoke 与反向 Workflow 关联</small></span></button>
    </nav>

    <RegistryRelationsPanel v-if="showRelations"/>
    <template v-else>
    <section class="acceptance-query" aria-label="验收与烟测筛选">
      <form @submit.prevent="submitSearch">
        <label><span>搜索当前对象</span><div><Search :size="14"/><input v-model="searchDraft" type="search" :placeholder="subject==='workflow'?'工作流名称、slug、版本或 ID':'实例名称、key、驱动或 ID'"/></div></label>
        <button class="button primary small" type="submit">查询</button>
      </form>
      <div v-if="deploymentId" class="acceptance-filter-chip"><span>Deployment 筛选：<code>{{ deploymentId }}</code></span><button type="button" @click="clearDeploymentFilter">清除</button></div>
      <div v-if="route.query.workflow_id" class="acceptance-filter-chip"><span>精确工作流筛选：{{ route.query.workflow_id }}</span><button type="button" @click="replaceQuery({workflow_id:undefined,page:1})">清除</button></div>
      <small v-if="loadedAt">页面读取时间：{{ dateTime(loadedAt) }}；证据发生时间以每条记录为准。</small>
    </section>

    <div v-if="error" class="platform-alert danger" role="alert"><AlertTriangle :size="16"/><div><strong>{{ resultIsCurrent ? '刷新失败，仍显示上次成功结果' : '验收与烟测事实不可用' }}</strong><p>{{ error }}</p></div><button class="button ghost small" type="button" @click="load(true)">重试</button></div>
    <div v-if="loading" class="platform-loading"><RefreshCw class="spin" :size="20"/>正在读取服务端验收投影…</div>

    <template v-else-if="resultIsCurrent">
      <section class="acceptance-page-summary" aria-label="当前页状态摘要">
        <article><span>全部结果</span><strong>{{ pageData?.count || 0 }}</strong><small>当前显示 {{ pageStart }}–{{ pageEnd }}</small></article>
        <article><span>本页需要处理</span><strong>{{ subject==='workflow' ? workflowAttentionCount : deploymentAttentionCount }}</strong><small>未完成、失效、失败或待消歧</small></article>
        <article v-if="subject==='workflow'"><span>本页已冻结实例</span><strong>{{ workflowRows.filter((row)=>row.association.validatedDeploymentMatches).length }}</strong><small>不含仅 Profile 兼容</small></article>
        <article v-else><span>本页已验收 Workflow</span><strong>{{ deploymentRows.reduce((total,row)=>total+row.workflowAssociationSummary.validatedCount,0) }}</strong><small>按 Deployment 反向汇总</small></article>
      </section>

      <section v-if="subject==='workflow'" class="acceptance-record-list" aria-label="Workflow 验收记录">
        <article v-for="row in workflowRows" :key="row.workflow.id" class="acceptance-record">
          <header><div><span class="acceptance-kind"><Workflow :size="13"/>Workflow</span><strong>{{ row.workflow.templateName || row.workflow.templateSlug }} · v{{ row.workflow.version }}</strong><small><code>{{ row.workflow.id }}</code> · {{ row.workflow.outputKind }} · {{ row.workflow.isCurrent ? '当前版本' : '非当前版本' }}</small></div><PlatformStatusPill :status="row.workflow.status"/></header>
          <div class="acceptance-record-facts">
            <section><span>模型与实例关联</span><strong>{{ workflowAssociationLabel(row.association) }}</strong><small>Profile <code>{{ row.workflow.modelProfileVersionId }}</code></small><small v-if="row.deployment">{{ deploymentLabel(row) }} · <code>{{ row.deployment.id }}</code></small><small v-else>尚未冻结具体 Deployment</small></section>
            <section><span>Live Validate</span><PlatformStatusPill :status="evidenceStatus(row.validation)"/><small>{{ row.validation.message || row.validation.reasonCode || '服务端未返回说明' }}</small><small>历史状态 {{ row.validation.storedState || 'not_run' }} · evidence <code :title="row.validation.evidenceSha256">{{ shortHash(row.validation.evidenceSha256) }}</code></small><small v-if="row.validation.storedTargetSha256||row.validation.currentTargetSha256">target <code :title="row.validation.storedTargetSha256">{{ shortHash(row.validation.storedTargetSha256) }}</code> → <code :title="row.validation.currentTargetSha256">{{ shortHash(row.validation.currentTargetSha256) }}</code></small></section>
            <section><span>Workflow Smoke</span><PlatformStatusPill :status="evidenceStatus(row.smoke)"/><small>{{ row.smoke.message || row.smoke.reasonCode || '服务端未返回说明' }}</small><small v-if="row.smoke.evidenceDeploymentId">历史证据实例 <code>{{ row.smoke.evidenceDeploymentId }}</code></small><small v-if="row.smoke.latestAttempt">attempt {{ row.smoke.latestAttempt.id.slice(0,8) }} · {{ row.smoke.latestAttempt.status }}<template v-if="row.smoke.latestAttempt.resolutionRequired"> · 待消歧</template></small><small v-else>尚无 Smoke attempt</small></section>
          </div>
          <footer><small v-if="row.actions.length">服务端建议：{{ actionSummary(row) }}</small><RouterLink class="button primary small" :to="workflowTarget(row)"><ShieldCheck :size="13"/>{{ workflowActionLabel(row) }}</RouterLink></footer>
        </article>
      </section>

      <section v-else class="acceptance-record-list" aria-label="模型实例 Smoke 记录">
        <article v-for="row in deploymentRows" :key="row.deployment.id" class="acceptance-record">
          <header><div><span class="acceptance-kind"><Server :size="13"/>Deployment</span><strong>{{ deploymentLabel(row) }}</strong><small><code>{{ row.deployment.id }}</code> · {{ row.deployment.providerDriver }} · Profile <code>{{ row.deployment.profileVersionId }}</code></small></div><PlatformStatusPill :status="row.deployment.status"/></header>
          <div class="acceptance-record-facts">
            <section><span>实例健康</span><PlatformStatusPill :status="deploymentHealthDisplayStatus(row)"/><small v-if="row.deployment.profileContractState!=='current'">模型合同不可核验 · {{ row.deployment.profileContractReasonCode }}</small><small v-else-if="row.deployment.targetState!=='current'">实例目标不可核验 · {{ row.deployment.targetReasonCode }}</small><small v-if="row.health">{{ row.health.targetMatches ? '快照目标与当前实例一致' : '快照目标与当前实例不一致' }} · {{ row.health.fresh ? '证据有效' : '证据过期或漂移' }}</small><small v-else>尚无健康快照历史</small><small v-if="row.deployment.runtimeHostId">Runtime Host <code>{{ row.deployment.runtimeHostId }}</code></small><small v-else>远端或未绑定 Runtime Host</small></section>
            <section><template v-if="row.deployment.providerDriver==='comfyui'"><span>实例直接 Smoke</span><PlatformStatusPill status="not_applicable"/><small>ComfyUI 实例 Smoke 不适用；请通过精确 Workflow 完成 Live Validate 与真实 Smoke。</small></template><DeploymentCapabilitySmoke v-else :row="row" :loading="refreshing" :error="error" @refresh="load(true)"/></section>
            <section><span>Workflow 反向关联</span><strong>已验收 {{ row.workflowAssociationSummary.validatedCount }} · 仅 Profile 兼容 {{ row.workflowAssociationSummary.compatibleOnlyCount }}</strong><small>校验有效 {{ row.workflowAssociationSummary.freshValidationCount }} · 漂移 {{ row.workflowAssociationSummary.driftedValidationCount }}</small><small>Smoke 历史 {{ row.workflowAssociationSummary.smokeHistoryCount }} · 有效 {{ row.workflowAssociationSummary.freshSmokeCount }} · 待消歧 {{ row.workflowAssociationSummary.unresolvedSmokeCount }}</small></section>
          </div>
          <footer><small v-if="row.actions.length">服务端建议：{{ actionSummary(row) }}</small><RouterLink v-if="deploymentPrimaryTarget(row)" class="button primary small" :to="deploymentPrimaryTarget(row)"><ShieldCheck :size="13"/>{{ deploymentPrimaryLabel(row) }}</RouterLink><button v-else class="button primary small" type="button" disabled><ShieldCheck :size="13"/>当前无可执行动作</button></footer>
        </article>
      </section>

      <div v-if="!rows.length" class="platform-empty acceptance-empty">
        <strong>{{ search||deploymentId ? '当前筛选没有结果' : subject==='workflow' ? '尚无 Workflow 验收对象' : '尚无模型实例 Smoke 对象' }}</strong>
        <span>{{ search||deploymentId ? '清除筛选或调整搜索条件后重试。' : subject==='workflow' ? '先从系统设置的工作流目录扫描并导入版本。' : '先发布模型版本并创建运行实例。' }}</span>
      </div>

      <nav v-if="(pageData?.count||0)>pageSize" class="acceptance-pagination" aria-label="验收与烟测分页">
        <button class="button ghost small" type="button" :disabled="!pageData?.previous||loading" @click="goPage(page-1)"><ChevronLeft :size="13"/>上一页</button>
        <span>第 {{ page }} / {{ pageCount }} 页 · 共 {{ pageData?.count || 0 }} 条</span>
        <button class="button ghost small" type="button" :disabled="!pageData?.next||loading" @click="goPage(page+1)">下一页<ChevronRight :size="13"/></button>
      </nav>
    </template>
    </template>
  </div>
</template>

<style scoped>
.acceptance-subject-tabs{grid-template-columns:repeat(3,minmax(0,1fr))!important}@media(max-width:620px){.acceptance-subject-tabs{grid-template-columns:1fr!important}}
.acceptance-smoke-center{max-width:1500px;margin:0 auto}.acceptance-subject-tabs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.acceptance-subject-tabs button{min-height:62px;padding:11px 13px;display:flex;align-items:center;gap:10px;border:1px solid var(--border);border-radius:8px;background:#0c1015;color:#77828d;text-align:left;cursor:pointer}.acceptance-subject-tabs button>span{min-width:0;display:grid;gap:4px}.acceptance-subject-tabs strong{color:#c0c8d0;font-size:11px}.acceptance-subject-tabs small{overflow:hidden;color:#727e89;font-size:9px;text-overflow:ellipsis;white-space:nowrap}.acceptance-subject-tabs button.active{border-color:rgba(230,167,90,.4);background:rgba(230,167,90,.07);color:var(--accent)}.acceptance-subject-tabs button:focus-visible{outline:2px solid rgba(230,167,90,.75);outline-offset:2px}.acceptance-query{padding:10px 12px;display:flex;align-items:flex-end;flex-wrap:wrap;gap:10px;border:1px solid var(--border);border-radius:8px;background:#0b0f14}.acceptance-query form{min-width:min(520px,100%);display:flex;align-items:flex-end;gap:8px}.acceptance-query form>.button,.acceptance-record>footer>.button{min-height:36px}.acceptance-query label{min-width:0;flex:1;display:grid;gap:5px;color:#8995a0;font-size:9px}.acceptance-query label>div{min-height:36px;padding:0 9px;display:flex;align-items:center;gap:7px;border:1px solid #2a323b;border-radius:7px;background:#070a0e}.acceptance-query input{min-width:0;flex:1;border:0;outline:0;background:transparent;color:#d1d8df}.acceptance-query>small{margin-left:auto;color:#69747f;font-size:8px}.acceptance-filter-chip{max-width:100%;min-height:36px;padding:6px 8px;display:flex;align-items:center;gap:8px;border:1px solid rgba(230,167,90,.28);border-radius:7px;color:#a89073;font-size:8px}.acceptance-filter-chip span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.acceptance-filter-chip button{border:0;background:transparent;color:#d2aa77;cursor:pointer}.acceptance-page-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border:1px solid var(--border);border-radius:8px;background:#0b0f14;overflow:hidden}.acceptance-page-summary article{padding:10px 12px;display:grid;gap:4px;border-right:1px solid var(--border)}.acceptance-page-summary article:last-child{border-right:0}.acceptance-page-summary span,.acceptance-page-summary small{color:#6f7a85;font-size:8px}.acceptance-page-summary strong{color:#c3cbd3;font:16px Consolas,monospace}.acceptance-record-list{display:grid;gap:9px}.acceptance-record{border:1px solid var(--border);border-radius:8px;background:#0b0f14;overflow:hidden}.acceptance-record>header{padding:10px 12px;display:flex;align-items:flex-start;justify-content:space-between;gap:12px;border-bottom:1px solid var(--border)}.acceptance-record>header>div{min-width:0;display:grid;gap:4px}.acceptance-record>header strong{overflow-wrap:anywhere;color:#cbd3da;font-size:11px}.acceptance-record>header small{overflow-wrap:anywhere;color:#727e89;font-size:8px}.acceptance-kind{display:flex;align-items:center;gap:5px;color:#8090a0;font-size:8px;text-transform:uppercase}.acceptance-record-facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}.acceptance-record-facts section{min-width:0;padding:10px 12px;display:grid;align-content:start;justify-items:start;gap:5px;border-right:1px solid var(--border)}.acceptance-record-facts section:last-child{border-right:0}.acceptance-record-facts section>span{color:#707b86;font-size:8px}.acceptance-record-facts strong{overflow-wrap:anywhere;color:#adb7c1;font-size:9px}.acceptance-record-facts small{max-width:100%;overflow-wrap:anywhere;color:#7e8994;font-size:8px;line-height:1.5}.acceptance-record code{overflow-wrap:anywhere;color:#aa8d6a}.acceptance-record>footer{min-height:48px;padding:8px 12px;display:flex;align-items:center;justify-content:flex-end;gap:10px;border-top:1px solid var(--border)}.acceptance-record>footer>small{margin-right:auto;color:#707b85;font-size:8px}.acceptance-empty{display:grid;gap:6px;padding:28px!important}.acceptance-empty strong{color:#aeb7c0}.acceptance-empty span{color:#737e88}.acceptance-pagination{display:flex;align-items:center;justify-content:center;gap:12px}.acceptance-pagination span{color:#7d8892;font-size:9px}
@media(max-width:900px){.acceptance-record-facts{grid-template-columns:1fr}.acceptance-record-facts section{border-right:0;border-bottom:1px solid var(--border)}.acceptance-record-facts section:last-child{border-bottom:0}.acceptance-query>small{width:100%;margin-left:0}}
@media(max-width:620px){.acceptance-subject-tabs,.acceptance-page-summary{grid-template-columns:1fr}.acceptance-subject-tabs small{white-space:normal}.acceptance-page-summary article{border-right:0;border-bottom:1px solid var(--border)}.acceptance-page-summary article:last-child{border-bottom:0}.acceptance-query form{align-items:stretch;flex-direction:column}.acceptance-query form>.button{width:100%;justify-content:center}.acceptance-filter-chip{width:100%;justify-content:space-between}.acceptance-record>header,.acceptance-record>footer{align-items:stretch;flex-direction:column}.acceptance-record>footer>.button{width:100%;justify-content:center}.acceptance-pagination{justify-content:space-between}.acceptance-pagination span{text-align:center}}
</style>
