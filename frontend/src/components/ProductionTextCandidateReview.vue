<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RefreshCw } from 'lucide-vue-next'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary, apiErrorPresentation } from '@/services/apiErrorPresentation'
import { jsonInputErrors } from '@/services/structuredInputForm'
import StructuredJsonEditor from './StructuredJsonEditor.vue'
import { readTextCandidateDetail, textCandidateSource } from '@/services/productionTextCandidates'
import { buildManualTextApproval, type ManualActionOptions } from '@/services/productionManual'
import { stageLabel } from '@/services/presentationLabels'
import { taskCenterTarget, taskTime } from '@/services/taskCenter'
import ManualScriptIdentity from './ManualScriptIdentity.vue'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import type { GeneratedArtifact, JsonObject, ProductionRun, StageRun } from '@/services/platformTypes'

const props = defineProps<{ run: ProductionRun; stage: StageRun; currentRules: Record<string, string | undefined>; manualOptions?: ManualActionOptions; readOnly?: boolean }>()
const emit = defineEmits<{ changed: []; busy: [value: boolean] }>()
const rows = ref<GeneratedArtifact[]>([]), selected = ref<GeneratedArtifact | null>(null)
const detail = ref<ReturnType<typeof readTextCandidateDetail> | null>(null)
const source = ref<StageRun | null>(null)
const page = ref(1), count = ref<number | null>(null), hasNext = ref(false), hasPrevious = ref(false)
const loading = ref(false), detailLoading = ref(false), submitting = ref(false), error = ref('')
const listReadFailed = ref(false), readNotice = ref('')
const reviewed = ref(false), comment = ref(''), decision = ref('approve')
const edits = ref<Record<string, string>>({}), checked = ref<Record<string, boolean>>({})
const editingScript = ref(false), scriptDraft = ref<unknown>(null), draftErrors = ref<string[]>([])
const fieldErrors = ref<Record<string, string[]>>({})
const scriptErrors = computed(() => editingScript.value && detail.value?.jsonSchema
  ? [...draftErrors.value, ...jsonInputErrors(scriptDraft.value, [detail.value.jsonSchema], '剧本字段')] : [])
const scriptContent = computed(() => editingScript.value ? JSON.stringify(scriptDraft.value, null, 2) : undefined)
const fields = computed(() => detail.value?.reviewContract?.fields ?? [])
const visual = computed(() => props.stage.stageKey === 'visual_rules_review')
const manual = computed(() => props.run.planSource === 'manual')
// The parent may retain an opening snapshot while the live Run ends elsewhere.
// Viewing that snapshot must not retain permission to submit a new decision.
const readOnly = computed(() => props.readOnly || ['succeeded', 'failed', 'cancelled'].includes(props.run.status))
const candidateReadOnly = computed(() => readOnly.value || selected.value?.status === 'selected')
const manualState = ref<ManualActionOptions | null>(null), manualSourceId = ref('')
const pendingRequest = ref<JsonObject | null>(null), requestKey = ref(crypto.randomUUID())
const locked = computed(() => submitting.value || Boolean(pendingRequest.value))
const readBlocked = computed(() => loading.value || listReadFailed.value)
const manualSources = computed(() => {
  const keys = manualState.value?.actions.find(row => row.stageKey === props.stage.stageKey && row.action === 'approve')?.sourceStageKeys
  return props.run.stages?.filter(row => keys?.includes(row.stageKey) && row.ordinal < props.stage.ordinal) ?? []
})
const manualReviewIdentity = (state: ManualActionOptions | null) => state && JSON.stringify([
  state.runId, state.runPlanSha256,
  visual.value ? state.visualRulesReview : [state.scriptReview, state.currentScript, state.currentScriptDecisionId],
  state.actions.filter(row => row.stageKey === props.stage.stageKey && row.action === 'approve'),
])
const lengthAdvice = (key: string) => {
  const maximum = detail.value?.qualityAdvisories?.[`${key}_max_chars`]
  return typeof maximum === 'number' && (edits.value[key]?.length || 0) > maximum
    ? `已超过候选冻结建议 ${maximum} 字符；仅提醒，最终范围由服务端校验。` : ''
}
let sequence = 0, detailSequence = 0
const resetSelection = () => { ++detailSequence; selected.value = null; detail.value = null; reviewed.value = false; edits.value = {}; checked.value = {}; detailLoading.value = false; editingScript.value = false; scriptDraft.value = null; draftErrors.value = []; fieldErrors.value = {} }
const editScript = () => {
  if (candidateReadOnly.value || locked.value || readBlocked.value || !detail.value?.jsonSchema || visual.value) return
  scriptDraft.value = JSON.parse(JSON.stringify(detail.value.candidate)); editingScript.value = true
  reviewed.value = false; fieldErrors.value = {}; draftErrors.value = []
}
const changeScript = (value: unknown) => {
  if (candidateReadOnly.value || locked.value || readBlocked.value) return
  scriptDraft.value = value; reviewed.value = false; fieldErrors.value = {}; error.value = ''
}
const cancelScriptEdit = () => {
  if (locked.value) return
  editingScript.value = false; scriptDraft.value = null; reviewed.value = false; fieldErrors.value = {}; draftErrors.value = []
}
const load = async (targetPage = 1) => {
  if (submitting.value || loading.value || (pendingRequest.value && !readOnly.value)) return
  const token = ++sequence
  ++detailSequence; detailLoading.value = false
  error.value = ''; readNotice.value = ''; loading.value = true
  try {
    if (!props.run.stages) throw new Error('运行详情缺少阶段清单，请重新读取')
    const exact = manual.value ? manualSources.value.find(row => row.id === manualSourceId.value) : textCandidateSource(props.stage, props.run.stages)
    if (!exact) throw new Error('请明确选择要查看的生成步骤，不自动选择最新候选')
    const result = await platformApi.listArtifactPage({ production_run: props.run.id, stage_run: exact.id, page: targetPage })
    if (token !== sequence) return
    if (result.results.some(row => row.productionRun !== props.run.id || row.stageRun !== exact.id)
      || new Set(result.results.map(row => row.id)).size !== result.results.length) throw new Error('候选清单归属或身份不一致，请重新读取')
    if (selected.value) {
      const same = result.results.find(row => row.id === selected.value!.id && row.sha256 === selected.value!.sha256
        && row.manifestMetadataSha256 === selected.value!.manifestMetadataSha256 && row.status === selected.value!.status)
      if (source.value?.id !== exact.id || !same) {
        resetSelection(); readNotice.value = '候选清单或所选候选已变化，已取消旧选择；审核说明保留，请重新选择并核对。'
      }
    }
    source.value = exact; rows.value = result.results; count.value = result.count
    page.value = targetPage; hasNext.value = result.hasNext; hasPrevious.value = result.hasPrevious
    listReadFailed.value = false
  } catch (cause) { if (token === sequence) { listReadFailed.value = true; error.value = apiErrorSummary(cause) } }
  finally { if (token === sequence) loading.value = false }
}
const choose = async (row: GeneratedArtifact) => {
  if (locked.value || readBlocked.value) return
  resetSelection(); error.value = ''; readNotice.value = ''; detailLoading.value = true
  const token = detailSequence, runId = props.run.id, sourceId = source.value?.id
  try {
    if (!sourceId) throw new Error('请先读取有效的候选清单')
    const artifact = await platformApi.getArtifact(row.id)
    if (token !== detailSequence) return
    if (artifact.id !== row.id || artifact.sha256 !== row.sha256 || artifact.manifestMetadataSha256 !== row.manifestMetadataSha256) throw new Error('候选身份或内容在读取期间已变化，请刷新清单')
    const parsed = readTextCandidateDetail(artifact, runId, sourceId)
    if (visual.value !== (parsed.operation === 'visual_rules.generate')) throw new Error('候选职责与当前审核不一致')
    selected.value = artifact; detail.value = parsed
    if (parsed.operation === 'visual_rules.generate') {
      const candidate = parsed.candidate
      edits.value = Object.fromEntries(fields.value.map(field => [field.key, String(candidate[field.key])]))
    }
  } catch (cause) { if (token === detailSequence) error.value = `读取候选详情失败：${apiErrorSummary(cause)}。请刷新清单后重新选择候选。` }
  finally { if (token === detailSequence) detailLoading.value = false }
}
const ready = computed(() => !readBlocked.value && !detailLoading.value && !candidateReadOnly.value && selected.value?.status === 'available' && !!detail.value && !detail.value.adoptionBlockReason && !!source.value && !error.value
  && (visual.value ? fields.value.length > 0 && fields.value.every(field => checked.value[field.key] && typeof edits.value[field.key] === 'string'
    && (field.allow_blank || edits.value[field.key]?.trim())) : reviewed.value && !scriptErrors.value.length))
const submit = async () => {
  if (readOnly.value || readBlocked.value || detailLoading.value || submitting.value || (!pendingRequest.value && (selected.value?.status === 'selected' || (decision.value === 'approve' && !ready.value)))) return
  submitting.value = true; error.value = ''; emit('busy', true)
  const token = detailSequence
  try {
    if (manual.value) {
      if (!manualState.value || decision.value !== 'approve') throw new Error('此人工审核动作尚未开放')
      if (!pendingRequest.value) pendingRequest.value = buildManualTextApproval({ projectId: props.run.project, options: manualState.value,
        idempotencyKey: requestKey.value, artifactId: selected.value!.id, artifactSha256: selected.value!.sha256,
        manifestSha256: selected.value!.manifestMetadataSha256, comment: comment.value,
        ...(visual.value ? { stageKey: 'visual_rules_review', candidateOverrides: edits.value } : { stageKey: 'script_review', scriptContent: scriptContent.value }) })
      const receipt = await platformApi.performManualProductionAction(props.run.id, pendingRequest.value)
      if (token !== detailSequence) return
      if (receipt.action !== 'approve' || receipt.result_kind !== 'approval_decision' || typeof receipt.target_id !== 'string') throw new Error('未返回正式审核决策，请重试同一请求核对结果')
      pendingRequest.value = null; requestKey.value = crypto.randomUUID()
    } else {
      const approve = decision.value === 'approve'
      await platformApi.approveProductionRun(props.run.id, { stageKey: props.stage.stageKey, decision: decision.value, comment: comment.value,
      ...(approve && selected.value && source.value ? {
        textCandidate: { artifactId: selected.value.id, expectedSha256: selected.value.sha256 },
        expectedSourceSnapshotSha256: source.value.outputSnapshotSha256,
        ...(visual.value ? { candidateOverrides: edits.value } : { scriptContent: scriptContent.value }),
      } : {}) })
    }
    if (token !== detailSequence) return
    resetSelection(); emit('changed')
  } catch (cause) {
    if (token !== detailSequence) return
    error.value = apiErrorSummary(cause); fieldErrors.value = apiErrorPresentation(cause).fields
    const status = (cause as { response?: { status?: number } })?.response?.status
    if (status === 400 || status === 409) { pendingRequest.value = null; requestKey.value = crypto.randomUUID(); reviewed.value = false }
  }
  finally { submitting.value = false; emit('busy', false) }
}
const refresh = async () => {
  if (submitting.value || loading.value || (pendingRequest.value && !readOnly.value)) return
  let reviewChanged = false
  if (manual.value) {
    const token = ++sequence
    loading.value = true; error.value = ''
    try {
      const state = await platformApi.getManualProductionActions(props.run.id)
      if (token !== sequence) return
      if (state.runId !== props.run.id || state.runPlanSha256 !== props.run.executionPlan?.sha256) throw new Error('人工审核与本轮冻结计划不一致')
      reviewChanged = manualReviewIdentity(state) !== manualReviewIdentity(manualState.value)
      if (reviewChanged) { reviewed.value = false; checked.value = {} }
      manualState.value = state; pendingRequest.value = null; requestKey.value = crypto.randomUUID()
    } catch (cause) { if (token === sequence) { listReadFailed.value = true; error.value = apiErrorSummary(cause) }; return }
    finally { if (token === sequence) loading.value = false }
  }
  if (!manual.value || manualSourceId.value) {
    const request = load(page.value), token = sequence
    await request
    if (token !== sequence) return
  }
  else resetSelection()
  if (reviewChanged && selected.value) readNotice.value = '本轮审核依据已变化；保留候选与草稿，请按新依据重新核对并勾选，不能沿用之前的确认。'
}
watch(() => `${props.run.id}:${props.stage.id}:${props.stage.inputSnapshotSha256}`, () => {
  ++sequence; loading.value = false; source.value = null
  listReadFailed.value = false; readNotice.value = ''
  decision.value = 'approve'; comment.value = ''; rows.value = []; count.value = null
  pendingRequest.value = null; requestKey.value = crypto.randomUUID(); manualState.value = null; manualSourceId.value = ''; resetSelection()
  if (manual.value) {
    const state = props.manualOptions
    if (!state || state.runId !== props.run.id || state.runPlanSha256 !== props.run.executionPlan?.sha256) error.value = '人工审核缺少本轮明确状态，请重新读取'
    else { manualState.value = state; error.value = '' }
  } else void load()
}, { immediate: true })
onBeforeUnmount(() => { ++sequence; ++detailSequence })
</script>

<template>
  <section class="platform-card text-review" :aria-label="readOnly ? '文本候选只读查看' : '文本候选审核'">
    <header><div><strong>{{ visual ? '全剧影视规则候选' : '剧本候选' }}{{ readOnly ? ' · 只读' : '审核' }}</strong><p v-if="readOnly">本轮已结束，仅查看原候选和调用记录。查看、翻页和刷新均不重新生成、不采用，也不恢复本轮运行。</p><p v-else>先选择并阅读一个候选，再批准采用。其他候选不会自动写入项目。</p></div><button class="button ghost small" :disabled="loading || submitting || (!!pendingRequest && !readOnly)" :aria-label="visual ? '刷新影视规则候选' : manual ? '重新读取当前剧本和候选' : '刷新候选'" @click="refresh"><RefreshCw :size="16"/></button></header>
    <template v-if="manual">
      <ManualScriptIdentity v-if="!visual && manualState" :options="manualState"/>
      <p v-if="visual">影视规则只依据本次明确配置的输入生成，不要求先有剧本或人物库。输入是否允许本次编辑由精确策略决定；项目事实需预览确认后冻结，不能手工冒充或自动跟随项目变化。实际输入及来源可在原调用记录中查看。采用前请核对是否仍符合需要；项目现有规则被修改时会提示冲突，刷新不会重跑或改写旧候选。</p>
      <p v-else-if="!readOnly">候选保留生成时的依据。若当前剧本或项目母稿已变化，请回到原生成步骤，明确创建新准备并审核新候选；刷新不会重新生成。修改项目标题或梗概不影响此剧本采用。</p>
      <label>查看哪个生成步骤<select v-model="manualSourceId" :disabled="locked || loading" @change="load(1)"><option value="">请选择</option><option v-for="item in manualSources" :key="item.id" :value="item.id">{{ stageLabel(item.stageKey) }}</option></select></label>
      <p v-if="pendingRequest">{{ readOnly ? '上次审核请求的结果仍需核对原记录；本轮已结束，此处不再提交或重试。' : '上次请求结果尚未确认；请重试同一审核请求核对结果，不能通过刷新清除原请求后另行采用。' }}</p>
    </template>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <p v-if="listReadFailed" class="platform-alert warning" role="status">{{ count === null ? '尚无完整候选清单，暂不可提交。请刷新候选重新核对。' : '候选清单未能完整读取；保留的是上次内容与未提交审核，暂不可提交。请刷新候选重新核对。' }}</p>
    <p v-if="readNotice" role="status">{{ readNotice }}</p>
    <p v-if="loading" role="status">正在读取候选…</p>
    <template v-else>
      <p v-if="count !== null">共 {{ count }} 个候选 · 第 {{ page }} 页</p>
      <p v-if="count === 0">没有候选产物，请查看生成阶段及原任务。</p>
      <p v-if="!visual && rows.length">列表中的候选来自所选生成步骤；候选编号不是原稿或修订编号，内容摘要相同也不表示同一个候选。已选用候选可查看，但不能再次提交采用。</p>
      <div class="candidate-list"><button v-for="row in rows" :key="row.id" class="button ghost small" :disabled="locked || readBlocked || !source || !['available','selected'].includes(row.status)" :aria-pressed="selected?.id === row.id" @click="choose(row)">候选 {{ row.variantIndex }} · {{ taskTime(row.createdAt) }} · {{ row.status === 'selected' ? '已选用 · 只读' : row.status === 'available' ? '可查看' : '不可采用' }}</button></div>
      <nav aria-label="候选分页"><button class="button ghost small" :disabled="!hasPrevious || locked || readBlocked" @click="load(page - 1)">上一页</button><button class="button ghost small" :disabled="!hasNext || locked || readBlocked" @click="load(page + 1)">下一页</button></nav>
    </template>
    <p v-if="detailLoading" role="status">正在读取并核对完整候选…</p>
    <div v-if="detail && selected">
      <p>正在查看候选 {{ selected.variantIndex }}{{ selected.status === 'selected' ? ' · 已选用，仅查看' : '' }}</p>
      <details><summary>候选身份与原调用</summary><dl><dt>候选产物 ID</dt><dd>{{ selected.id }}</dd><dt>内容 SHA-256（不是候选 ID）</dt><dd>{{ selected.sha256 }}</dd><dt>生成步骤 ID</dt><dd>{{ selected.stageRun }}</dd></dl><RouterLink class="button ghost small" :to="taskCenterTarget({category:'production',runId:run.id,stageId:selected.stageRun,sourceId:selected.generationAttempt})">查看原调用记录</RouterLink></details>
      <p v-if="detail.adoptionBlockReason" class="platform-alert warning" role="alert">{{ detail.adoptionBlockReason }}</p>
      <ul v-if="detail.warnings.length"><li v-for="(warning, index) in detail.warnings" :key="index">{{ warning }}</li></ul>
      <template v-if="detail.operation === 'visual_rules.generate'">
        <details v-if="detail.reviewContract"><summary>本次冻结的输出合同 · {{ detail.reviewContract.output.name }} · v{{ detail.reviewContract.output.version }}</summary><dl><dt>输出版本 ID</dt><dd>{{ detail.reviewContract.output.id }}</dd><dt>合同 SHA-256</dt><dd>{{ detail.reviewContract.output.sha256 }}</dd></dl><p>字段与响应位置来自原生产请求，不随当前目录更新。合同中的“已审核”是下游来源名称，不表示此候选已经采用。</p></details>
        <pre v-else>{{ JSON.stringify(detail.candidate, null, 2) }}</pre>
        <section v-for="field in fields" :key="field.key" class="rule-row">
          <strong>{{ field.label }}</strong><p v-if="Object.prototype.hasOwnProperty.call(currentRules,field.key)">页面已读取的项目值：{{ currentRules[field.key] === undefined ? '尚未读取' : currentRules[field.key] || '尚未配置' }}</p><p v-else>此字段不写回项目设置，采用值保存在本轮审核结果中供下游引用。</p>
          <details><summary>响应 → 审核 → 下游来源</summary><dl><dt>响应位置 / 格式</dt><dd>{{ field.response_path }} / {{ field.response_format }}</dd><dt>转换规则</dt><dd>{{ field.transformation }}</dd><dt>最终审核字段</dt><dd>{{ field.key }} · {{ field.value_type }}</dd><dt>批准后可引用的来源</dt><dd>{{ field.source_selector }}</dd></dl></details>
          <template v-if="candidateReadOnly"><p>模型候选原文（不代表最终人工采用值）</p><pre>{{ detail.candidate[field.key] }}</pre></template>
          <template v-else>
          <details><summary>查看模型候选原文</summary><pre>{{ detail.candidate[field.key] }}</pre></details>
          <label>最终采用值<textarea v-model="edits[field.key]" :disabled="locked || readBlocked || !!detail.adoptionBlockReason" rows="4" @input="checked[field.key] = false"/></label>
          <p v-if="field.allow_blank">此字段允许留空；仍需勾选确认。</p>
          <p v-if="lengthAdvice(field.key)" class="platform-alert warning">{{ lengthAdvice(field.key) }}</p>
          <label><input v-model="checked[field.key]" :disabled="locked || readBlocked || selected.status !== 'available' || !!detail.adoptionBlockReason" type="checkbox"/>已核对该项最终值</label>
          </template>
        </section>
      </template>
      <template v-else>
        <h3>剧本正文</h3><pre class="script-content">{{ detail.text }}</pre>
        <ReadonlyDataValue :value="detail.candidate" label="查看候选字段（不回写项目资料）"/>
        <template v-if="!candidateReadOnly && detail.jsonSchema">
          <button v-if="!editingScript" class="button ghost small" :disabled="locked || readBlocked" @click="editScript">按字段编辑</button>
          <section v-else class="script-editor" aria-label="人工编辑稿">
            <h3>人工编辑稿</h3><p>草稿尚未保存。确认采用时另存人工修订，原模型返回保持不变。</p>
            <StructuredJsonEditor :model-value="scriptDraft" :schemas="[detail.jsonSchema]" path="source" :field-errors="fieldErrors" label="剧本字段" :disabled="locked || readBlocked" @update:model-value="changeScript" @draft-validity="draftErrors = $event"/>
            <ul v-if="scriptErrors.length" role="alert"><li v-for="(message,index) in scriptErrors" :key="index">{{ message }}</li></ul>
            <dl v-if="Object.keys(fieldErrors).length" role="alert"><template v-for="(messages,path) in fieldErrors" :key="path"><dt>{{ path }}</dt><dd>{{ messages.join('；') }}</dd></template></dl>
            <button class="button ghost small" :disabled="locked" @click="cancelScriptEdit">放弃编辑，使用原候选</button>
          </section>
        </template>
        <template v-if="!readOnly">
        <label><input v-model="reviewed" :disabled="locked || readBlocked || candidateReadOnly || !!scriptErrors.length" type="checkbox"/>{{ editingScript ? '我已核对编辑稿，确认另存修订并采用' : '我已阅读并确认采用此剧本' }}</label>
        <p>采用此剧本只更新剧本修订和项目母稿，保留项目原标题与梗概；候选中的字段内容仍保留。</p>
        </template>
      </template>
    </div>
    <p v-if="readOnly" role="status">只读记录不改变候选状态。候选成功或曾被选用，不代表此轮已完成全部审核与交付。</p><p v-else-if="selected?.status === 'selected' && !pendingRequest" role="status">该候选已选用，此处只读；本轮是否仍使用它，请核对当前采用记录。</p><footer v-else><label v-if="!manual">审核决定<select v-model="decision" :disabled="submitting || readBlocked"><option value="approve">批准所选候选并继续</option><option value="reject">拒绝本阶段并终止</option></select></label><label>审核说明<textarea v-model="comment" :disabled="locked" rows="2"/></label><button class="button primary small" :disabled="submitting || readBlocked || detailLoading || (!pendingRequest && decision === 'approve' && !ready)" @click="submit">{{ submitting ? '正在提交审核…' : pendingRequest ? '重试同一审核请求' : manual ? visual ? '确认采用这些规则' : '确认采用此剧本' : decision === 'approve' ? '确认采用并继续' : '确认拒绝并终止' }}</button></footer>
  </section>
</template>

<style scoped>
.text-review { padding: 1rem; display: grid; gap: 1rem; }
header, .candidate-list, nav { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; }
header { justify-content: space-between; }
label, footer, .rule-row { display: grid; gap: .5rem; }
textarea, select { width: 100%; }
.rule-row { padding-block: 1rem; border-bottom: 1px solid var(--border, #2b323c); }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
.script-content { max-height: 32rem; overflow: auto; }
dd { margin: 0 0 .75rem; overflow-wrap: anywhere; }
label:has(input[type=checkbox]) { display: flex; align-items: center; margin-block: .75rem; }
button[aria-pressed=true] { outline: 2px solid currentColor; }
</style>
