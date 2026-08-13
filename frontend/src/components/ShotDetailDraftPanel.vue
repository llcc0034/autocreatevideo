<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { AlertTriangle, Check, ChevronDown, FileText, Languages, LoaderCircle, RotateCcw, Save, Sparkles, WandSparkles } from 'lucide-vue-next'
import { shotDetailDraftRepository } from '@/services/shotDetailDraftRepository'
import { promptProvider, REMOTE_PROMPT_EGRESS_NOTICE } from '@/services/providerCapabilities'
import {
  shotDetailDraftEditorWarnings,
  shotDetailDraftIsEditable,
  shotDetailDraftRequestError,
  shotDetailDraftReviewIssueTitle,
} from '@/services/shotDetailDraft'
import type {
  Shot, ShotDetailDraft, ShotDetailDraftApplyResult, ShotDetailDraftData,
  ShotDetailDraftLanguageMode, ShotDetailDraftReviewIssue,
  CreativeGenerationCapability, PromptProviderId,
} from '@/types'

const props = withDefaults(defineProps<{
  projectId: string
  shot: Shot
  blockedReason?: string
}>(), { blockedReason: '' })

const emit = defineEmits<{ applied: [result: ShotDetailDraftApplyResult] }>()

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const MAX_ZH = 500
const MAX_EN = 750
const containsCjk = (value: string) => /[\u3400-\u4dbf\u4e00-\u9fff]/.test(value)

const drafts = ref<ShotDetailDraft[]>([])
const selectedDraftId = ref('')
const workingData = ref<ShotDetailDraftData | null>(null)
const variationNote = ref('')
const capability = ref<CreativeGenerationCapability | null>(null)
const selectedProvider = ref<PromptProviderId>('local_qwen')
const englishReviewAcknowledged = ref(false)
const reviewAcknowledged = ref(false)
const loading = ref(false)
const expanded = ref(false)
type DetailAction = 'generate' | 'save' | 'regenerate' | 'confirm' | 'apply' | ''
type DetailActionToken = { id: number; shotKey: string }
const action = ref<DetailAction>('')
const error = ref('')
const notice = ref('')
let loadSequence = 0
let actionSequence = 0

const currentShotKey = () => `${props.projectId}:${props.shot.id}`
const isShotCurrent = (shotKey: string) => currentShotKey() === shotKey
const beginAction = (kind: Exclude<DetailAction, ''>): DetailActionToken => {
  const token = { id: ++actionSequence, shotKey: currentShotKey() }
  action.value = kind
  return token
}
const isActionCurrent = (token: DetailActionToken) => token.id === actionSequence && isShotCurrent(token.shotKey)
const finishAction = (token: DetailActionToken) => {
  if (token.id === actionSequence) action.value = ''
}

const activeDraft = computed(() => drafts.value.find((item) => item.id === selectedDraftId.value) || null)
const editable = computed(() => shotDetailDraftIsEditable(activeDraft.value))
const dirty = computed(() => Boolean(
  activeDraft.value && workingData.value
  && JSON.stringify(activeDraft.value.draftData) !== JSON.stringify(workingData.value),
))
const localWarnings = computed(() => workingData.value ? shotDetailDraftEditorWarnings(workingData.value) : [])
const displayedValidationStatus = computed(() => dirty.value ? 'pending' : activeDraft.value?.validation.status || 'pending')
const reviewIssues = computed(() => activeDraft.value?.reviewIssues || [])
const reviewIssueCount = computed(() => activeDraft.value?.reviewSummary.issueCount || reviewIssues.value.length)
const requiresEditCount = computed(() => activeDraft.value?.reviewSummary.requiresEditCount
  || reviewIssues.value.filter((item) => item.severity === 'requires_edit').length)
const canConfirm = computed(() => Boolean(
  activeDraft.value?.canConfirm && !dirty.value && activeDraft.value.contextCurrent
  && (!reviewIssueCount.value || reviewAcknowledged.value),
))
const canApply = computed(() => Boolean(
  activeDraft.value?.canApply && !dirty.value && activeDraft.value.status === 'confirmed'
  && activeDraft.value.contextCurrent && !requiresEditCount.value
  && (!reviewIssueCount.value || reviewAcknowledged.value),
))
const appliedDraft = computed(() => drafts.value.find((item) => item.status === 'applied') || null)
const hasAppliedDetail = computed(() => Boolean(props.shot.detailedDescription?.trim() || appliedDraft.value))
const formalDetailNeedsTranslation = computed(() => containsCjk(props.shot.detailedDescription || ''))
const panelBodyId = computed(() => `shot-detail-draft-body-${props.shot.id}`)
const selectedProviderCapability = computed(() => promptProvider(capability.value?.providers, selectedProvider.value))
const providerReady = computed(() => selectedProviderCapability.value?.ready ?? true)
const providerEgressNotice = computed(() => selectedProvider.value === 'deepseek_api'
  ? selectedProviderCapability.value?.dataEgressNotice || REMOTE_PROMPT_EGRESS_NOTICE
  : '')
const charCount = (value: string, max: number) => `${value.length} / ${max}`
const reviewIssueLocation = (issue: ShotDetailDraftReviewIssue): string => (
  issue.field === 'detailZh' ? '中文候选' : issue.field === 'detailEn' ? '英文候选' : '当前镜头候选'
)
const statusLabel: Record<ShotDetailDraft['status'], string> = {
  draft: '待审核', confirmed: '已确认', applied: '已应用', superseded: '历史版本', failed: '生成失败',
}

const selectDraft = (draft: ShotDetailDraft) => {
  selectedDraftId.value = draft.id
  workingData.value = clone(draft.draftData)
  variationNote.value = draft.variationNote
  englishReviewAcknowledged.value = false
  reviewAcknowledged.value = false
  error.value = ''
  notice.value = ''
}

const replaceDraft = (draft: ShotDetailDraft) => {
  drafts.value = drafts.value.map((item) => item.id === draft.id ? draft : item)
  if (!drafts.value.some((item) => item.id === draft.id)) drafts.value.unshift(draft)
  selectDraft(draft)
}

const load = async (preferredId = '') => {
  if (!props.projectId || !props.shot.id) return
  const requestProjectId = props.projectId
  const requestShotId = props.shot.id
  const requestSequence = ++loadSequence
  const stillCurrent = () => requestSequence === loadSequence
    && props.projectId === requestProjectId && props.shot.id === requestShotId
  loading.value = true
  try {
    const next = await shotDetailDraftRepository.list(requestProjectId, requestShotId)
    if (!stillCurrent()) return
    drafts.value = next
    const selected = next.find((item) => item.id === preferredId)
      || next.find((item) => ['draft', 'confirmed', 'failed'].includes(item.status))
      || next[0]
    if (selected) selectDraft(selected)
    else { selectedDraftId.value = ''; workingData.value = null }
  } catch (cause) {
    if (!stillCurrent()) return
    error.value = shotDetailDraftRequestError(cause, '无法读取当前镜头的高细节草稿。')
  } finally { if (stillCurrent()) loading.value = false }
}

const loadCapability = async () => {
  try {
    capability.value = await shotDetailDraftRepository.capability()
    if (!capability.value.providers?.some((provider) => provider.id === selectedProvider.value)) {
      selectedProvider.value = capability.value.defaultProvider || 'local_qwen'
    }
  } catch (cause) {
    error.value = shotDetailDraftRequestError(cause, '无法读取扩写通道能力。')
  }
}

const generate = async () => {
  if (props.blockedReason) { error.value = props.blockedReason; return }
  const token = beginAction('generate')
  const requestProjectId = props.projectId
  const requestShotId = props.shot.id
  error.value = ''; notice.value = ''
  try {
    const draft = await shotDetailDraftRepository.generate(requestProjectId, {
      shotId: requestShotId,
      variationNote: variationNote.value.trim(),
      provider: selectedProvider.value,
    })
    if (!isActionCurrent(token)) return
    expanded.value = true
    await load(draft.id)
  } catch (cause) {
    // The backend may retain a failed audit record. Re-read it rather than
    // hiding the failure behind a transient toast.
    if (!isActionCurrent(token)) return
    await load()
    if (!isActionCurrent(token)) return
    error.value = shotDetailDraftRequestError(cause, '逐镜高细节候选生成失败；当前正式镜头未修改。')
  } finally { finishAction(token) }
}

const saveCurrent = async (shotKey = currentShotKey()): Promise<ShotDetailDraft | null> => {
  if (!isShotCurrent(shotKey)) return null
  const current = activeDraft.value
  if (!current || !workingData.value) return null
  if (!dirty.value) return current
  if (!editable.value) throw new Error('当前版本不可编辑；请以它重新生成新的高细节候选。')
  const saved = await shotDetailDraftRepository.update(current.id, current.revision, workingData.value)
  if (!isShotCurrent(shotKey)) return null
  replaceDraft(saved)
  return saved
}

const save = async () => {
  const token = beginAction('save')
  error.value = ''; notice.value = ''
  try { await saveCurrent(token.shotKey) }
  catch (cause) { if (isActionCurrent(token)) error.value = shotDetailDraftRequestError(cause, '高细节候选保存失败。') }
  finally { finishAction(token) }
}

const regenerate = async () => {
  const current = activeDraft.value
  if (!current) { await generate(); return }
  const token = beginAction('regenerate')
  error.value = ''; notice.value = ''
  try {
    const saved = await saveCurrent(token.shotKey) || current
    if (!isActionCurrent(token)) return
    if (!saved.canRegenerate) throw new Error('当前版本不能重新生成，请刷新后重试。')
    const next = await shotDetailDraftRepository.regenerate(saved.id, {
      expectedRevision: saved.revision,
      variationNote: variationNote.value.trim(),
      provider: selectedProvider.value,
    })
    if (!isActionCurrent(token)) return
    await load(next.id)
  } catch (cause) {
    if (isActionCurrent(token)) error.value = shotDetailDraftRequestError(cause, '重新生成高细节候选失败；当前正式镜头未修改。')
  } finally { finishAction(token) }
}

const confirm = async () => {
  const current = activeDraft.value
  if (!current) return
  const token = beginAction('confirm')
  error.value = ''; notice.value = ''
  try {
    const saved = await saveCurrent(token.shotKey) || current
    if (!isActionCurrent(token)) return
    const confirmed = await shotDetailDraftRepository.confirm(
      saved.id, saved.revision, reviewIssueCount.value ? reviewAcknowledged.value : false,
    )
    if (!isActionCurrent(token)) return
    replaceDraft(confirmed)
    notice.value = '候选已确认，下一步可采用中文，或勾选英文核对后采用已审中英。'
  } catch (cause) {
    if (isActionCurrent(token)) error.value = shotDetailDraftRequestError(cause, '高细节候选确认失败。')
  } finally { finishAction(token) }
}

const apply = async (languageMode: ShotDetailDraftLanguageMode) => {
  const current = activeDraft.value
  if (!current || !canApply.value) return
  if (languageMode === 'bilingual_confirmed' && !englishReviewAcknowledged.value) {
    error.value = '采用已审中英前，请勾选“已逐项核对英文”。'
    return
  }
  const token = beginAction('apply')
  error.value = ''; notice.value = ''
  try {
    const result = await shotDetailDraftRepository.apply(current.id, {
      expectedRevision: current.revision,
      languageMode,
      englishReviewAcknowledged: languageMode === 'bilingual_confirmed' ? true : undefined,
      reviewAcknowledged: reviewIssueCount.value ? reviewAcknowledged.value : false,
    })
    // A request begun for Shot A may resolve after the editor switches to Shot B.
    // The formal A update is still emitted so its local prompt is invalidated, but
    // no A draft state is rendered in B's panel.
    if (!isShotCurrent(token.shotKey)) { emit('applied', result); return }
    if (!isActionCurrent(token)) return
    replaceDraft(result.draft)
    expanded.value = false
    notice.value = languageMode === 'bilingual_confirmed'
      ? '已将确认后的英文高细节说明写入当前正式镜头。请在右侧重新生成并复检 H3 提示词。'
      : '已写入中文正式字段。请在上方“翻译审核”中确认英文后，再重新生成并复检 H3 提示词。'
    emit('applied', result)
  } catch (cause) {
    if (isActionCurrent(token)) error.value = shotDetailDraftRequestError(cause, '应用高细节说明失败；当前正式镜头未修改。')
  } finally { finishAction(token) }
}

watch(() => `${props.projectId}:${props.shot.id}`, () => {
  loadSequence += 1
  actionSequence += 1; action.value = ''
  drafts.value = []; selectedDraftId.value = ''; workingData.value = null
  variationNote.value = ''; englishReviewAcknowledged.value = false
  reviewAcknowledged.value = false
  error.value = ''; notice.value = ''; expanded.value = false
  void load()
})
watch(() => JSON.stringify(workingData.value), () => {
  if (dirty.value) englishReviewAcknowledged.value = false
  if (dirty.value) reviewAcknowledged.value = false
})
onMounted(async () => { await loadCapability(); await load() })
</script>

<template>
  <section class="shot-detail-draft" :class="{ expanded }" aria-labelledby="shot-detail-draft-heading" :aria-busy="loading||Boolean(action)">
    <header class="detail-header">
      <div class="detail-heading">
        <div><WandSparkles :size="17"/></div>
        <span>
          <small>SINGLE-SHOT HIGH-DETAIL DRAFT</small>
          <strong id="shot-detail-draft-heading">逐镜高细节扩写</strong>
          <p>只扩写当前镜头的“镜头运动、动作与画面描述”；人物、场景、道具、连续性与对白只作只读约束，不改写基础字段。</p>
        </span>
      </div>
      <button class="detail-toggle" type="button" :aria-expanded="expanded" :aria-controls="panelBodyId" @click="expanded=!expanded">
        {{ expanded ? '收起草稿' : hasAppliedDetail ? '查看版本' : '开始扩写' }} <ChevronDown :size="13" :class="{ rotate: expanded }"/>
      </button>
    </header>

    <div v-if="notice" class="detail-notice success" role="status" aria-live="polite"><Check :size="14"/><span>{{ notice }}</span></div>
    <div v-if="error" class="detail-notice error" role="alert"><AlertTriangle :size="14"/><span>{{ error }}</span></div>

    <div v-if="hasAppliedDetail&&!expanded" class="applied-summary">
      <Check :size="16"/>
      <span><strong>高细节说明已应用到当前正式镜头</strong><small>{{ formalDetailNeedsTranslation ? '当前为中文正式字段，需在上方翻译审核确认英文后才能用于生产。' : '当前为已确认英文；重新生成并复检 H3 提示词即可生效。' }}</small></span>
      <button type="button" @click="expanded=true">查看或重新扩写</button>
    </div>

    <div v-if="expanded" :id="panelBodyId" class="detail-body">
      <div class="detail-context">
        <FileText :size="15"/>
        <span><strong>扩写焦点：镜头运动、动作与画面描述</strong><small>当前镜头、前后连续性、已绑定 Bible、锁定对白、全剧视觉规则与 canonical 术语均只作约束；候选不会改写基础 camera、action、叙事字段或对白正文。</small></span>
      </div>

      <div class="detail-generation-row">
        <label class="detail-provider"><span>处理通道</span><select v-model="selectedProvider" :disabled="Boolean(action)"><option v-for="provider in capability?.providers || []" :key="provider.id" :value="provider.id" :disabled="!provider.ready">{{ provider.label }}{{ provider.ready ? '' : '（未就绪）' }}</option></select></label>
        <label><span>本次补充要求（可选）</span><input v-model="variationNote" :disabled="Boolean(action)" placeholder="例如：加强雨水在衣料、地面和剑鞘上的层次变化"></label>
        <button class="detail-primary" type="button" :disabled="Boolean(action)||loading||Boolean(blockedReason)||!providerReady" :title="blockedReason" @click="generate">
          <LoaderCircle v-if="action==='generate'" class="spin" :size="14"/><Sparkles v-else :size="14"/>
          {{ action==='generate' ? '正在生成候选…' : activeDraft ? '新建扩写版本' : '生成高细节候选' }}
        </button>
      </div>
      <p v-if="providerEgressNotice" class="detail-egress"><AlertTriangle :size="12"/><span><strong>外部 API 数据发送提示：</strong>{{ providerEgressNotice }}</span></p>
      <p v-if="capability&&!providerReady" class="detail-blocked"><AlertTriangle :size="12"/>{{ selectedProviderCapability?.notice || '所选扩写通道尚未配置。' }}</p>
      <p v-if="blockedReason" class="detail-blocked"><AlertTriangle :size="12"/>{{ blockedReason }}</p>

      <div v-if="loading" class="detail-loading"><LoaderCircle class="spin" :size="15"/>读取当前镜头的高细节版本…</div>
      <div v-else-if="!activeDraft||!workingData" class="detail-empty">
        <Sparkles :size="18"/><strong>尚未生成高细节候选</strong><p>候选会先进入此处审核；确认应用前，正式分镜与 H3 提示词均保持不变。</p>
      </div>
      <template v-else>
        <div class="detail-version-row" aria-label="高细节草稿版本">
          <span>版本</span>
          <button v-for="draft in drafts" :key="draft.id" type="button" :class="{ active: draft.id===activeDraft.id }" @click="selectDraft(draft)">V{{ draft.generationNo }} · {{ statusLabel[draft.status] }}</button>
          <small>{{ activeDraft.provider || 'local_qwen' }} · {{ activeDraft.model || 'Qwen' }}</small>
        </div>

        <div v-if="!activeDraft.contextCurrent" class="detail-notice error"><AlertTriangle :size="14"/><span>当前镜头、Bible、上下文或术语已变化，本版本不能确认或应用；请新建扩写版本。</span></div>
        <div class="detail-validation" :class="[displayedValidationStatus,{review:reviewIssueCount}]">
          <Check v-if="!dirty&&!reviewIssueCount&&['passed','ready'].includes(displayedValidationStatus)" :size="14"/><AlertTriangle v-else :size="14"/>
          <span><strong>{{ dirty ? '修改待保存校验' : reviewIssueCount ? `已生成 · 待人工核验（${reviewIssueCount} 项）` : displayedValidationStatus==='failed' ? '未产生可编辑候选' : '已生成 · 可人工确认' }}</strong><small>{{ dirty ? '保存后会重新整理风险提示；内容风险不会阻止保存。' : reviewIssueCount ? '这些是小模型可能产生的偏差。可直接修改、重新扩写，或在明确核验后继续。' : '中文最多 500 字，英文最多 750 字符；确认后仍由人工决定是否应用。' }}</small></span>
        </div>
        <section v-if="reviewIssues.length" class="detail-review-issues" aria-label="高细节候选人工核验项">
          <header><span><AlertTriangle :size="14"/><strong>建议人工核验</strong><small>{{ requiresEditCount ? `${requiresEditCount} 项关联问题需在正式应用前修正；` : '' }}其余内容风险可由你决定修改、重新扩写或知情继续。</small></span><b>{{ reviewIssueCount }} 项</b></header>
          <ol>
            <li v-for="(issue, issueIndex) in reviewIssues" :key="`${issue.code}-${issue.path}-${issueIndex}`" :class="issue.severity">
              <span><small>{{ reviewIssueLocation(issue) }}</small><strong>{{ shotDetailDraftReviewIssueTitle(issue) }}</strong><em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></span>
            </li>
          </ol>
        </section>
        <ul v-if="localWarnings.length" class="detail-warnings advisory">
          <li v-for="message in localWarnings" :key="`local-${message}`">{{ message }}</li>
        </ul>

        <div class="detail-editor">
          <label><span>中文高细节说明 <b>{{ charCount(workingData.detailZh, MAX_ZH) }}</b></span><textarea v-model="workingData.detailZh" rows="10" :readonly="!editable" :disabled="Boolean(action)" placeholder="本地 Qwen 的中文候选会显示在这里；用于人工审阅，不会直接进入 H3 英文导演描述。"></textarea></label>
          <label class="english"><span>English high-detail direction · 人工审核 <b>{{ charCount(workingData.detailEn, MAX_EN) }}</b></span><textarea v-model="workingData.detailEn" rows="10" :readonly="!editable" :disabled="Boolean(action)" placeholder="确认的英文才可成为 H3 的正式高细节说明。"></textarea></label>
        </div>

        <footer class="detail-actions">
          <div class="detail-actions-main">
            <span v-if="dirty" class="dirty">有未保存修改</span><span v-else>修订 {{ activeDraft.revision }} · {{ activeDraft.updatedAt ? new Date(activeDraft.updatedAt).toLocaleString('zh-CN') : '刚刚更新' }}</span>
            <button type="button" :disabled="Boolean(action)||!dirty||!editable" @click="save"><LoaderCircle v-if="action==='save'" class="spin" :size="12"/><Save v-else :size="12"/>保存并校验</button>
            <button type="button" :disabled="Boolean(action)||!activeDraft.canRegenerate||Boolean(blockedReason)" :title="blockedReason" @click="regenerate"><LoaderCircle v-if="action==='regenerate'" class="spin" :size="12"/><RotateCcw v-else :size="12"/>重新扩写</button>
            <button class="confirm" type="button" :disabled="Boolean(action)||!canConfirm" @click="confirm"><LoaderCircle v-if="action==='confirm'" class="spin" :size="12"/><Check v-else :size="12"/>确认候选</button>
          </div>
          <div class="detail-apply-zone">
            <div><strong>应用到当前正式镜头</strong><small>唯一会写入 H3 长导演说明的步骤；应用后须重新生成并复检提示词。</small></div>
            <label v-if="reviewIssueCount" class="detail-review-ack"><input v-model="reviewAcknowledged" type="checkbox" :disabled="Boolean(action)||dirty"><span>我已人工核验上述风险，知悉候选只扩写镜头运动、动作与画面描述；确认或应用将记录本次知情操作。</span></label>
            <p v-if="requiresEditCount" class="detail-requires-edit"><AlertTriangle :size="12"/>当前含 {{ requiresEditCount }} 项关联问题：可先确认和继续编辑，但正式应用前必须修正。</p>
            <label><input v-model="englishReviewAcknowledged" type="checkbox" :disabled="!canApply"><span>我已逐项核对英文中的人物、方位、动作、运镜、时间顺序、道具状态与结尾状态。</span></label>
            <div>
              <button type="button" :disabled="Boolean(action)||!canApply" @click="apply('chinese_only')"><Languages :size="13"/>采用中文，转上方翻译审核</button>
              <button class="bilingual" type="button" :disabled="Boolean(action)||!canApply||!englishReviewAcknowledged" @click="apply('bilingual_confirmed')"><Check :size="13"/>采用已审中英</button>
            </div>
          </div>
        </footer>
      </template>
    </div>
  </section>
</template>

<style scoped>
.detail-provider{max-width:150px;flex:0 0 150px!important}.detail-generation-row select{width:100%;min-height:34px;padding:7px;border:1px solid #29333e;border-radius:5px;background:#080c11;color:#c4cdd5;font-size:10px}.detail-egress{display:flex;align-items:flex-start;gap:5px;margin:0;padding:8px 9px;border:1px solid rgba(217,164,79,.25);border-radius:6px;background:rgba(217,164,79,.06);color:#cfaa72;font-size:9px;line-height:1.45}
.shot-detail-draft{display:grid;gap:10px;padding:12px;border:1px solid rgba(142,104,217,.22);border-radius:8px;background:linear-gradient(135deg,rgba(84,61,139,.08),rgba(11,15,20,.94) 52%)}.detail-header,.detail-heading,.detail-toggle,.detail-generation-row,.detail-context,.detail-version-row,.detail-validation,.detail-actions,.detail-actions-main,.detail-apply-zone>div,.detail-apply-zone>div>div,.applied-summary{display:flex;align-items:center}.detail-header{justify-content:space-between;gap:11px}.detail-heading{min-width:0;gap:8px}.detail-heading>div{width:32px;height:32px;display:grid;flex:none;place-items:center;border-radius:8px;background:rgba(142,104,217,.14);color:#b8a1f0}.detail-heading>span{min-width:0;display:grid;gap:2px}.detail-heading small{font:8px/1.3 Consolas,monospace;letter-spacing:.11em;color:#967bce}.detail-heading strong{font-size:12px}.detail-heading p{margin:0;color:#74808a;font-size:9px;line-height:1.45}.detail-toggle,.detail-primary,.detail-actions button,.applied-summary button{min-height:31px;padding:6px 9px;border:1px solid rgba(142,104,217,.32);border-radius:6px;background:#171422;color:#c7b5ee;font-size:10px;font-weight:700;cursor:pointer}.detail-toggle{white-space:nowrap;gap:4px;background:#10121a}.detail-toggle svg{transition:transform .16s}.detail-toggle svg.rotate{transform:rotate(180deg)}.detail-body{display:grid;gap:10px}.detail-context{align-items:flex-start;gap:7px;padding:9px;border:1px solid rgba(96,190,160,.16);border-radius:6px;background:rgba(96,190,160,.04);color:#7eb59f}.detail-context span{display:grid;gap:3px}.detail-context strong{font-size:10px}.detail-context small{font-size:9px;line-height:1.5;color:#789086}.detail-generation-row{align-items:end;gap:8px}.detail-generation-row label{flex:1;display:grid;gap:4px}.detail-generation-row label>span{font-size:9px;color:#78838d}.detail-generation-row input,.detail-editor textarea{width:100%;box-sizing:border-box;padding:8px;border:1px solid #29333e;border-radius:5px;background:#080c11;color:#c4cdd5;font:10px/1.55 inherit;outline:none}.detail-generation-row input:focus,.detail-editor textarea:focus{border-color:#7357b8}.detail-generation-row input:disabled{opacity:.6}.detail-primary{background:#654da5;color:#fff;white-space:nowrap}.detail-primary:disabled,.detail-actions button:disabled{opacity:.42;cursor:not-allowed}.detail-blocked{display:flex;gap:5px;align-items:flex-start;margin:0;color:#d68a7d;font-size:9px;line-height:1.45}.detail-loading,.detail-empty{display:flex;align-items:center;gap:7px;padding:10px;border:1px solid #252f38;border-radius:6px;background:#090d12;color:#7c8791;font-size:10px}.detail-empty{align-items:center;justify-content:center;min-height:74px;flex-direction:column}.detail-empty p{margin:0;font-size:9px}.detail-version-row{gap:6px;overflow:auto}.detail-version-row>span{color:#78838d;font-size:9px}.detail-version-row button{padding:4px 7px;border:1px solid #29333e;border-radius:12px;background:#0b1015;color:#7a8790;font-size:9px;white-space:nowrap;cursor:pointer}.detail-version-row button.active{border-color:rgba(142,104,217,.48);background:rgba(142,104,217,.12);color:#c6b1f0}.detail-version-row small{margin-left:auto;color:#65717b;font-size:9px;white-space:nowrap}.detail-validation{align-items:flex-start;gap:7px;padding:8px 9px;border:1px solid rgba(217,164,79,.22);border-radius:6px;background:rgba(217,164,79,.04);color:#caa05f}.detail-validation.passed{border-color:rgba(96,198,164,.22);background:rgba(96,198,164,.04);color:#74c9ad}.detail-validation.review{border-color:rgba(218,158,73,.34);background:rgba(218,158,73,.065);color:#d8a75f}.detail-validation span{display:grid;gap:2px}.detail-validation small{font-size:8px;color:#74817f;line-height:1.45}.detail-warnings{display:grid;gap:4px;margin:0;padding:8px 8px 8px 23px;border-radius:6px;background:#090d11;color:#c49c67;font-size:9px}.detail-warnings.advisory{color:#b79461}.detail-review-issues{display:grid;gap:8px;padding:9px;border:1px solid rgba(218,158,73,.24);border-radius:7px;background:rgba(218,158,73,.045)}.detail-review-issues>header{display:flex;align-items:flex-start;justify-content:space-between;gap:9px}.detail-review-issues>header>span{display:grid;grid-template-columns:auto 1fr;gap:2px 6px;color:#d5a45f}.detail-review-issues>header>span svg{grid-row:1/3}.detail-review-issues>header small{color:#837665;font-size:8px;line-height:1.45}.detail-review-issues>header>b{padding:3px 7px;border-radius:10px;background:rgba(218,158,73,.11);color:#d8aa68;font-size:9px;white-space:nowrap}.detail-review-issues ol{display:grid;gap:5px;margin:0;padding:0;list-style:none}.detail-review-issues li{display:flex;gap:7px;padding:7px 8px;border:1px solid rgba(255,255,255,.055);border-radius:6px;background:#090d11}.detail-review-issues li.requires_edit{border-color:rgba(223,101,90,.24);background:rgba(223,101,90,.045)}.detail-review-issues li>span{display:grid;gap:2px}.detail-review-issues li small{color:#7c8790;font-size:8px}.detail-review-issues li strong{color:#c4a16d;font-size:9px}.detail-review-issues li.requires_edit strong{color:#dd8378}.detail-review-issues li em{color:#7f8991;font-size:8px;font-style:normal;line-height:1.45}.detail-editor{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.detail-editor label{display:grid;gap:5px}.detail-editor label>span{display:flex;align-items:center;justify-content:space-between;gap:7px;color:#8a95a0;font-size:9px}.detail-editor label.english>span{color:#a997df}.detail-editor b{font:9px Consolas,monospace;color:#68747d}.detail-editor textarea{min-height:172px;resize:vertical}.detail-editor textarea:read-only{color:#929da6;background:#090d11}.detail-actions{align-items:stretch;justify-content:space-between;gap:10px}.detail-actions-main{align-items:flex-start;flex-wrap:wrap;gap:6px;color:#6f7a83;font-size:9px}.detail-actions-main .dirty{color:#d2a46a}.detail-actions button{display:inline-flex;align-items:center;justify-content:center;gap:5px;background:#111821;color:#afbec8;border-color:#2d3a45}.detail-actions button.confirm{border-color:rgba(95,197,164,.35);color:#8ad3bb}.detail-apply-zone{display:grid;gap:6px;max-width:400px;padding:9px;border:1px solid rgba(129,105,215,.22);border-radius:6px;background:rgba(129,105,215,.045)}.detail-apply-zone>div:first-child{align-items:flex-start;flex-direction:column;gap:2px}.detail-apply-zone strong{font-size:10px}.detail-apply-zone small{font-size:8px;line-height:1.45;color:#75808a}.detail-apply-zone label{display:flex;align-items:flex-start;gap:5px;color:#87939c;font-size:8px;line-height:1.45}.detail-apply-zone .detail-review-ack{padding:7px;border:1px solid rgba(218,158,73,.24);border-radius:5px;background:rgba(218,158,73,.055);color:#c8a467}.detail-apply-zone .detail-review-ack input{margin-top:2px}.detail-requires-edit{display:flex;align-items:flex-start;gap:5px;margin:0;padding:7px;border:1px solid rgba(223,101,90,.22);border-radius:5px;background:rgba(223,101,90,.05);color:#da8378;font-size:8px;line-height:1.45}.detail-apply-zone>div:last-child{gap:6px;flex-wrap:wrap}.detail-apply-zone .bilingual{border-color:rgba(95,197,164,.35);background:#15352d;color:#a5e3ce}.detail-notice{display:flex;align-items:flex-start;gap:7px;padding:8px 9px;border-radius:6px;font-size:9px;line-height:1.5}.detail-notice.success{border:1px solid rgba(96,198,164,.2);background:rgba(96,198,164,.05);color:#80c9af}.detail-notice.error{border:1px solid rgba(223,101,90,.25);background:rgba(223,101,90,.06);color:#df8177}.applied-summary{gap:8px;padding:10px;border:1px solid rgba(96,198,164,.22);border-radius:7px;background:rgba(96,198,164,.05);color:#73c9ad}.applied-summary>span{min-width:0;display:grid;gap:2px}.applied-summary strong{font-size:10px}.applied-summary small{color:#7d998e;font-size:9px;line-height:1.45}.applied-summary button{margin-left:auto;border-color:rgba(96,198,164,.28);background:#10251f;color:#8bd3ba;white-space:nowrap}.spin{animation:detail-spin 1s linear infinite}@keyframes detail-spin{to{transform:rotate(360deg)}}
@media(max-width:900px){.detail-header,.detail-actions{align-items:stretch;flex-direction:column}.detail-toggle{align-self:flex-end}.detail-editor{grid-template-columns:1fr}.detail-actions,.detail-apply-zone{max-width:none}.detail-apply-zone{width:100%;box-sizing:border-box}.detail-generation-row{align-items:stretch;flex-direction:column}.detail-primary{width:100%}.detail-version-row small{display:none}}
</style>
