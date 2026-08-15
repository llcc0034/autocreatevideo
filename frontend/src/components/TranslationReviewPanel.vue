<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { AlertTriangle, Check, ChevronDown, Languages, LoaderCircle, RotateCcw, ShieldCheck, Undo2 } from 'lucide-vue-next'
import { repository } from '@/services/repository'
import { promptProvider, REMOTE_PROMPT_EGRESS_NOTICE } from '@/services/providerCapabilities'
import {
  createTranslationLoadGate,
  reconcileTranslationDrafts,
  translationEntryIdentity,
  translationFieldIdentity,
  translationReviewRequestError,
  translationReviewStatus,
} from '@/services/translationReview'
import type {
  PromptTranslationEntry,
  PromptTranslationField,
  PromptTranslationPromotedField,
  ShotTranslationPromotionResult,
  TranslationCapability,
  TranslationReviewStatus,
  PromptProviderId,
} from '@/types'

const props = withDefaults(defineProps<{
  projectId: string
  episodeId?: string
  shotId?: string
  fields: PromptTranslationField[]
  compact?: boolean
  title?: string
  refreshKey?: number
  repairLegacyNegativePrompt?: boolean
  blockedReason?: string
}>(), { episodeId: '', shotId: '', compact: false, title: '本地 Qwen 英文译文人工审核', refreshKey: 0, repairLegacyNegativePrompt: false, blockedReason: '' })

const emit = defineEmits<{
  changed: []
  promoted: [result: ShotTranslationPromotionResult]
  promotionBusy: [busy: boolean]
}>()
const loading = ref(false)
const bulkTranslating = ref(false)
const promotingShot = ref(false)
const error = ref('')
const entries = ref<PromptTranslationEntry[]>([])
const capability = ref<TranslationCapability | null>(null)
const selectedProvider = ref<PromptProviderId>('local_qwen')
const busy = ref<Record<string, boolean>>({})
const drafts = ref<Record<string, string>>({})
const promotedFieldKeys = ref<Record<string, boolean>>({})
let promotionOperationCount = 0
const loadGate = createTranslationLoadGate()
const CJK = /[\u3400-\u4dbf\u4e00-\u9fff]/
const scopeLabels: Record<PromptTranslationField['scopeType'], string> = {
  project: '项目级', episode: '剧集', shot: '分镜', character: '角色', scene: '场景', prop: '道具',
}

const fieldWasPromoted = (field: PromptTranslationField) => Boolean(promotedFieldKeys.value[translationFieldIdentity(field)])
const isPromotableShotFieldKey = (fieldKey: string) => (
  ['shot.narrative', 'shot.camera', 'shot.continuity_notes', 'shot.sound_effects', 'shot.music'].includes(fieldKey)
  || /^shot\.dialogue\.\d+\.delivery$/.test(fieldKey)
)
const isPromotableShotField = (field: PromptTranslationField) => (
  field.scopeType === 'shot' && field.scopeId === props.shotId && isPromotableShotFieldKey(field.fieldKey)
)
const setPromotionOperation = (active: boolean) => {
  promotionOperationCount = Math.max(0, promotionOperationCount + (active ? 1 : -1))
  emit('promotionBusy', promotionOperationCount > 0)
}
const translatableFields = computed(() => props.fields.filter((field) => field.sourceText.trim() && (CJK.test(field.sourceText) || fieldWasPromoted(field))))
const entryByIdentity = computed(() => new Map(entries.value.map((entry) => [translationEntryIdentity(entry), entry])))
const entryFor = (field: PromptTranslationField) => entryByIdentity.value.get(translationFieldIdentity(field))
const stateFor = (field: PromptTranslationField) => fieldWasPromoted(field)
  ? 'confirmed'
  : translationReviewStatus(field, entryFor(field), Boolean(busy.value[translationFieldIdentity(field)]))
const statusProfiles: Record<TranslationReviewStatus, { label: string; tone: string }> = {
  untranslated: { label: '未翻译', tone: 'muted' },
  translating: { label: '翻译中', tone: 'working' },
  pending_review: { label: '待审核', tone: 'warning' },
  confirmed: { label: '已确认', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  source_changed: { label: '源文已变更', tone: 'danger' },
}
const statusFor = (field: PromptTranslationField) => fieldWasPromoted(field)
  ? { label: '已同步到正式字段', tone: 'success' }
  : statusProfiles[stateFor(field)]
const draftChanged = (field: PromptTranslationField) => {
  const entry = entryFor(field)
  return Boolean(entry && String(drafts.value[entry.id] || '').trim() !== entry.translatedText.trim())
}
const collapseConfirmed = (field: PromptTranslationField) => (
  stateFor(field) === 'confirmed' && !draftChanged(field)
)
const attentionFields = computed(() => translatableFields.value.filter((field) => !collapseConfirmed(field)))
const collapsedConfirmedFields = computed(() => translatableFields.value.filter(collapseConfirmed))
const orderedFields = computed(() => [...attentionFields.value, ...collapsedConfirmedFields.value])
const attentionCount = computed(() => attentionFields.value.length)
const pendingCount = computed(() => translatableFields.value.filter((field) => stateFor(field) === 'pending_review').length)
const confirmedCount = computed(() => translatableFields.value.filter((field) => stateFor(field) === 'confirmed').length)
const failedCount = computed(() => translatableFields.value.filter((field) => stateFor(field) === 'failed').length)
const sourceChangedCount = computed(() => translatableFields.value.filter((field) => stateFor(field) === 'source_changed').length)
const fieldsNeedingTranslation = computed(() => translatableFields.value.filter((field) => stateFor(field) !== 'confirmed'))
const promotableEntries = computed(() => translatableFields.value.flatMap((field) => {
  const entry = entryFor(field)
  const promotable = field.scopeType === 'shot'
    && field.scopeId === props.shotId
    && entry?.status === 'confirmed'
    && entry.contextCurrent !== false
    && !fieldWasPromoted(field)
    && isPromotableShotFieldKey(field.fieldKey)
  return promotable && entry ? [entry] : []
}))
const selectedProviderCapability = computed(() => promptProvider(capability.value?.providers, selectedProvider.value))
const capabilityReady = computed(() => selectedProviderCapability.value?.ready ?? (
  selectedProvider.value === 'local_qwen'
    && capability.value?.enabled === true && capability.value?.configured === true
    && capability.value?.modelDownloaded === true && capability.value?.modelVerified === true
))
const providerEgressNotice = computed(() => selectedProvider.value === 'deepseek_api'
  ? selectedProviderCapability.value?.dataEgressNotice || REMOTE_PROMPT_EGRESS_NOTICE
  : '')
const reviewMutationBlocked = computed(() => Boolean(props.blockedReason))
const includesProjectFields = computed(() => translatableFields.value.some((field) => field.scopeType === 'project'))
const entryError = (field: PromptTranslationField) => {
  const entry = entryFor(field)
  if (!entry?.error) return ''
  const code = String(entry.runtimeMetadata?.error_code || entry.runtimeMetadata?.last_attempt_error_code || '').trim()
  return code ? `[${code.toUpperCase()}] ${entry.error}` : entry.error
}
const translateActionLabel = (field: PromptTranslationField) => {
  if (stateFor(field) === 'source_changed') return '重新翻译生成候选'
  return entryFor(field) ? '重新翻译' : '翻译此字段'
}

const load = async () => {
  if (!props.projectId) return
  const requestSequence = loadGate.begin()
  loading.value = true; error.value = ''
  try {
    const [nextCapability, nextEntries] = await Promise.all([
      repository.getTranslationCapability(),
      repository.getProjectTranslations(props.projectId, { episodeId: props.episodeId, shotId: props.shotId }),
    ])
    if (!loadGate.isCurrent(requestSequence)) return
    capability.value = nextCapability
    if (!nextCapability.providers?.some((provider) => provider.id === selectedProvider.value)) {
      selectedProvider.value = nextCapability.defaultProvider || 'local_qwen'
    }
    const previousEntries = entries.value
    entries.value = nextEntries
    drafts.value = reconcileTranslationDrafts(drafts.value, previousEntries, nextEntries)
  } catch (cause) {
    if (!loadGate.isCurrent(requestSequence)) return
    error.value = cause instanceof Error ? cause.message : '无法读取翻译审核状态'
  } finally {
    if (loadGate.isCurrent(requestSequence)) loading.value = false
  }
}

const promotedFieldsFrom = (rows: PromptTranslationEntry[]) => {
  const byIdentity = new Map<string, PromptTranslationPromotedField>()
  for (const field of rows.flatMap((row) => row.promotedFields || [])) {
    byIdentity.set(`${field.fieldKey}:${field.dialogueIndex ?? ''}`, field)
  }
  return [...byIdentity.values()]
}

const rememberPromotedFields = (fields: PromptTranslationPromotedField[], shotId = props.shotId) => {
  if (!fields.length) return
  promotedFieldKeys.value = {
    ...promotedFieldKeys.value,
    ...Object.fromEntries(fields.map((field) => [`shot:${shotId}:${field.fieldKey}`, true])),
  }
}

const upsertEntries = (
  rows: PromptTranslationEntry[],
  options: { emitPromotion?: boolean; emitChanged?: boolean } = {},
) => {
  // A translate/retranslate/confirm/revoke response is newer than every GET
  // already in flight. Invalidate those responses before applying the row and
  // clear their spinner because their finally blocks are now intentionally stale.
  loadGate.invalidate()
  loading.value = false
  const previousEntries = entries.value
  const next = new Map(entries.value.map((entry) => [entry.id, entry]))
  for (const row of rows) {
    const previous = next.get(row.id)
    next.set(row.id, previous ? {
      ...previous, ...row,
      fieldKey: row.fieldKey || previous.fieldKey,
      scopeType: row.scopeType || previous.scopeType,
      scopeId: row.scopeId || previous.scopeId,
      sourceText: row.sourceText || previous.sourceText,
    } : row)
  }
  entries.value = [...next.values()]
  // Rows passed here are direct responses to translate/retranslate/confirm/
  // revoke. Always show the authoritative response candidate, even when the
  // same entry id previously left an empty draft after a failed attempt.
  drafts.value = reconcileTranslationDrafts(
    drafts.value, previousEntries, entries.value, rows.map((row) => row.id),
  )
  const promoted = promotedFieldsFrom(rows)
  const promotedShotId = rows.find((row) => row.scopeType === 'shot' && row.scopeId && row.promotedFields?.length)?.scopeId || props.shotId
  rememberPromotedFields(promoted, promotedShotId)
  if (promoted.length && options.emitPromotion !== false) {
    emit('promoted', { shotId: promotedShotId, promotedFields: promoted, entries: rows, shotPatch: {} })
  }
  if (options.emitChanged !== false) emit('changed')
}

const translateAll = async () => {
  if (reviewMutationBlocked.value) { error.value = props.blockedReason; return }
  const fields = fieldsNeedingTranslation.value
  if (!fields.length || !capabilityReady.value) return
  const requestContext = `${props.projectId}:${props.episodeId}:${props.shotId}`
  bulkTranslating.value = true; error.value = ''
  busy.value = { ...busy.value, ...Object.fromEntries(fields.map((field) => [translationFieldIdentity(field), true])) }
  try {
    const rows = await repository.translateProjectFields(props.projectId, fields, selectedProvider.value)
    if (requestContext === `${props.projectId}:${props.episodeId}:${props.shotId}`) upsertEntries(rows)
  }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '批量翻译失败，中文原文未被修改' }
  finally {
    const next = { ...busy.value }; for (const field of fields) delete next[translationFieldIdentity(field)]
    busy.value = next; bulkTranslating.value = false
  }
}

const translateOne = async (field: PromptTranslationField) => {
  if (reviewMutationBlocked.value) { error.value = props.blockedReason; return }
  if (!capabilityReady.value) return
  const requestContext = `${props.projectId}:${props.episodeId}:${props.shotId}`
  const key = translationFieldIdentity(field); busy.value = { ...busy.value, [key]: true }; error.value = ''
  try {
    const current = entryFor(field)
    const row = current
      ? await repository.retranslateProjectField(current.id, field.sourceText, selectedProvider.value)
      : (await repository.translateProjectFields(props.projectId, [field], selectedProvider.value))[0]
    if (row && requestContext === `${props.projectId}:${props.episodeId}:${props.shotId}`) upsertEntries([row])
  } catch (cause) { error.value = translationReviewRequestError(cause, `${field.label}翻译失败，中文原文未被修改。`) }
  finally { const next = { ...busy.value }; delete next[key]; busy.value = next }
}

const confirm = async (field: PromptTranslationField) => {
  if (reviewMutationBlocked.value) { error.value = props.blockedReason; return }
  if (stateFor(field) === 'source_changed') {
    error.value = `${field.label}的旧译文已失效，不能确认；请先重新翻译生成候选。`
    return
  }
  const entry = entryFor(field); if (!entry) return
  const translatedText = String(drafts.value[entry.id] || '').trim()
  if (!translatedText) { error.value = `${field.label}的英文译文不能为空`; return }
  const key = translationFieldIdentity(field); busy.value = { ...busy.value, [key]: true }; error.value = ''
  const promotesFormalShotField = isPromotableShotField(field)
  const requestShotId = props.shotId
  if (promotesFormalShotField) setPromotionOperation(true)
  try {
    const row = await repository.confirmProjectTranslation(entry.id, translatedText, entry.revision)
    if (requestShotId && requestShotId !== props.shotId) {
      const promotedFields = promotedFieldsFrom([row])
      if (promotedFields.length) emit('promoted', { shotId: row.scopeId || requestShotId, promotedFields, entries: [row], shotPatch: {} })
    } else upsertEntries([row])
  }
  catch (cause) { error.value = translationReviewRequestError(cause, `${field.label}确认失败，请刷新后重试。`) }
  finally {
    const next = { ...busy.value }; delete next[key]; busy.value = next
    if (promotesFormalShotField) setPromotionOperation(false)
  }
}

const promoteConfirmedShot = async () => {
  if (reviewMutationBlocked.value) { error.value = props.blockedReason; return }
  if (!props.shotId || (!promotableEntries.value.length && !props.repairLegacyNegativePrompt)) return
  promotingShot.value = true; error.value = ''
  const requestShotId = props.shotId
  setPromotionOperation(true)
  try {
    const result = await repository.promoteShotTranslations(props.shotId, promotableEntries.value.map((entry) => ({
      entryId: entry.id, expectedRevision: entry.revision,
    })), { repairLegacyNegativePrompt: props.repairLegacyNegativePrompt })
    if (requestShotId === props.shotId) {
      upsertEntries(result.entries, { emitPromotion: false, emitChanged: false })
      rememberPromotedFields(result.promotedFields, result.shotId)
    }
    emit('promoted', result)
    if (requestShotId === props.shotId) emit('changed')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '同步正式英文分镜字段失败；现有本地编辑未被修改。'
  } finally { promotingShot.value = false; setPromotionOperation(false) }
}

const revoke = async (field: PromptTranslationField) => {
  if (reviewMutationBlocked.value) { error.value = props.blockedReason; return }
  const entry = entryFor(field); if (!entry) return
  const key = translationFieldIdentity(field); busy.value = { ...busy.value, [key]: true }; error.value = ''
  try {
    upsertEntries([await repository.revokeProjectTranslation(entry.id)])
    const nextPromoted = { ...promotedFieldKeys.value }
    delete nextPromoted[translationFieldIdentity(field)]
    promotedFieldKeys.value = nextPromoted
  }
  catch (cause) { error.value = cause instanceof Error ? cause.message : `${field.label}撤销失败` }
  finally { const next = { ...busy.value }; delete next[key]; busy.value = next }
}

watch(() => [props.projectId, props.episodeId, props.shotId], load)
// The parent advances refreshKey only after an explicit server-side action
// such as auto-generating a new Bible visual candidate. Ordinary source edits
// must not reload here because that could erase an unsaved operator draft.
watch(() => props.refreshKey, load)
watch(() => props.fields.map((field) => `${translationFieldIdentity(field)}:${field.sourceText}`).join('|'), () => {
  // Source changes are derived locally and shown immediately; the stored
  // confirmed translation stays intact until the operator explicitly retranslates.
}, { flush: 'sync' })
onMounted(load)
</script>

<template>
  <section class="translation-review" :class="{ compact }">
    <header>
      <div class="translation-heading"><div><Languages :size="18"/></div><span><strong>{{ title }}</strong><small>中文原文永久保留；只有人工确认且源文未变化的英文译文才可进入正式 prompt。<template v-if="shotId"> 本面板审核英文候选；确认镜头导演字段后会原子同步到下方正式表单，镜头标题和对白正文仍保留中文，两处不是重复输入区。</template><template v-if="includesProjectFields"> 当前面板只审核项目级全剧字段；人物、场景和道具字段请在各自 Bible 面板审核。</template></small></span></div>
      <div class="translation-header-actions">
        <label v-if="capability?.providers?.length" class="translation-provider"><span>处理通道</span><select v-model="selectedProvider" :disabled="bulkTranslating||Object.keys(busy).length>0"><option v-for="provider in capability.providers" :key="provider.id" :value="provider.id" :disabled="!provider.ready">{{ provider.label }}{{ provider.ready ? '' : '（未就绪）' }}</option></select></label>
        <button v-if="shotId&&(promotableEntries.length||repairLegacyNegativePrompt)" type="button" class="secondary" :disabled="loading||promotingShot||bulkTranslating||reviewMutationBlocked" :title="blockedReason||''" @click="promoteConfirmedShot">
          <LoaderCircle v-if="promotingShot" class="spin" :size="13"/><Check v-else :size="13"/>
          {{ promotingShot?'正在同步与审计…':repairLegacyNegativePrompt?(promotableEntries.length?'同步英文并审计负面约束':'审计并恢复负面约束'):'一键同步已确认英文到正式字段' }}
        </button>
        <button type="button" :disabled="loading||bulkTranslating||promotingShot||!fieldsNeedingTranslation.length||!capabilityReady||reviewMutationBlocked" :title="blockedReason||''" @click="translateAll">
          <LoaderCircle v-if="bulkTranslating" class="spin" :size="13"/><Languages v-else :size="13"/>
          {{ bulkTranslating?'正在翻译…':'一键翻译待处理' }}
        </button>
      </div>
    </header>
    <div v-if="providerEgressNotice" class="translation-egress"><AlertTriangle :size="14"/><span><strong>外部 API 数据发送提示：</strong>{{ providerEgressNotice }}</span></div>
    <div v-if="blockedReason" class="translation-error"><AlertTriangle :size="14"/><span><strong>canonical 术语碰撞 · 翻译确认与正式字段同步已暂停：</strong> {{ blockedReason }} 请先回到“项目总览 → 全剧视觉规则”修正并保存；系统不会自动猜译名。</span></div>
    <div v-if="repairLegacyNegativePrompt" class="translation-recovery"><AlertTriangle :size="14"/><span><strong>来源匹配的创作草稿负面约束待审计：</strong>保存和生成已暂停。点击上方按钮后，服务端只会在 applied CreativeDraft 来源和值逐字匹配时清除；若这是您手工填写的真实缺陷词，请直接改写为英文负面约束后保存。</span></div>
    <div class="translation-safety"><ShieldCheck :size="14"/><span><strong>本地 4B 候选不可直接生产：</strong>必须逐项核对专名、人物性别、左/右手、道具部位、数值与比例、景别与运镜、动作先后和镜头终态，确认无误后再点“确认译文”。对白、歌词和真实屏显文字保留原文，只翻译对白括号中的 delivery 与导演字段。新建 Bible 时会自动补写英文 canonical glossary；缺失项可在“项目总览 → 全剧视觉规则”一键补全，译法不满意时再人工修改（如“玄纹剑 = Xuanwen Sword”）。未配置时人物、场景和道具名会使用 S1、scene:slug、prop:slug 等稳定技术标识，而不会自由翻译。默认本地模型是 ModelScope <code>Qwen/Qwen3-4B-GGUF</code>，并非 DashScope Qwen-MT API。</span></div>
    <div v-if="capability&&!capabilityReady" class="translation-error"><AlertTriangle :size="14"/>{{ selectedProviderCapability?.notice || capability.message || '所选处理通道尚未配置，当前只能查看原文和审核历史。' }}</div>
    <div v-if="error" class="translation-error"><AlertTriangle :size="14"/>{{ error }}</div>
    <div v-if="loading" class="translation-loading"><LoaderCircle class="spin" :size="16"/>读取翻译审核状态…</div>
    <template v-else>
      <div v-if="translatableFields.length" class="translation-summary"><span :class="{ attention: attentionCount }">待处理 <b>{{ attentionCount }}</b></span><span v-if="pendingCount">待审核 <b>{{ pendingCount }}</b></span><span>已确认 <b>{{ confirmedCount }}</b></span><span v-if="failedCount" class="changed">失败 <b>{{ failedCount }}</b></span><span v-if="sourceChangedCount" class="changed">源文已变更 <b>{{ sourceChangedCount }}</b></span><small>待处理始终展开 · 已确认默认折叠 · {{ selectedProviderCapability?.label || capability?.provider || '本地 Qwen' }}</small></div>
      <div v-if="translatableFields.length" class="translation-list">
        <component :is="collapseConfirmed(field)?'details':'article'" v-for="field in orderedFields" :key="translationFieldIdentity(field)" :class="[`state-${statusFor(field).tone}`,{ 'confirmed-collapsed': collapseConfirmed(field) }]">
          <component :is="collapseConfirmed(field)?'summary':'header'"><div><span>{{ scopeLabels[field.scopeType] }}</span><strong>{{ field.label }}</strong><small>{{ field.help || (field.impact==='continuity'?'影响跨镜头一致性':'直接影响生成提示词') }}</small></div><div class="translation-entry-state"><b class="translation-status" :class="statusFor(field).tone"><LoaderCircle v-if="stateFor(field)==='translating'" class="spin" :size="11"/>{{ statusFor(field).label }}</b><em v-if="collapseConfirmed(field)">展开核对 <ChevronDown :size="12"/></em></div></component>
          <div class="translation-columns">
            <label><span>中文原文 · 只读</span><textarea :value="field.sourceText" rows="4" readonly/></label>
            <label :class="{ stale: stateFor(field)==='source_changed' }"><span>{{ stateFor(field)==='source_changed'?'旧英文译文 · 已失效（只读）':'英文译文 · 人工可编辑' }}</span><textarea v-if="entryFor(field)" v-model="drafts[entryFor(field)!.id]" rows="4" :disabled="stateFor(field)==='translating'" :readonly="stateFor(field)==='source_changed'" placeholder="本地 Qwen 英文候选将在这里显示；请核对人物、服装、方位、动作和镜头语言。"/><textarea v-else rows="4" disabled placeholder="尚未翻译"/></label>
          </div>
          <p v-if="stateFor(field)==='source_changed'" class="source-warning"><AlertTriangle :size="12"/>中文源文、canonical 术语表或身份上下文已变化。旧译文仅供对照，不能确认也不会进入正式 prompt；请点击“重新翻译生成候选”，核对新候选后再确认。</p>
          <p v-if="entryError(field)" class="source-warning"><AlertTriangle :size="12"/>{{ entryError(field) }}</p>
          <footer><span v-if="entryFor(field)">修订 {{ entryFor(field)?.revision }}<template v-if="entryFor(field)?.confirmedAt"> · 确认于 {{ new Date(entryFor(field)!.confirmedAt!).toLocaleString('zh-CN') }}</template><template v-if="fieldWasPromoted(field)"> · 已同步到下方正式英文分镜字段</template></span><span v-else>尚无译文记录</span><div><button type="button" class="secondary" :disabled="Boolean(busy[translationFieldIdentity(field)])||!capabilityReady||fieldWasPromoted(field)||reviewMutationBlocked" :title="blockedReason||''" @click="translateOne(field)"><RotateCcw :size="12"/>{{ translateActionLabel(field) }}</button><button v-if="entryFor(field)&&stateFor(field)!=='source_changed'&&(stateFor(field)!=='confirmed'||draftChanged(field))" type="button" :disabled="Boolean(busy[translationFieldIdentity(field)])||reviewMutationBlocked" :title="blockedReason||''" @click="confirm(field)"><Check :size="12"/>{{ stateFor(field)==='confirmed'?'保存修改并确认':'确认译文并同步正式字段' }}</button><button v-if="stateFor(field)==='confirmed'" type="button" class="secondary" :title="blockedReason||(fieldWasPromoted(field)?'撤销只改变译文审核状态，不会回滚已经同步的正式英文；如需修改请直接编辑下方正式字段。':'撤销译文确认不会改动正式分镜字段。')" :disabled="Boolean(busy[translationFieldIdentity(field)])||reviewMutationBlocked" @click="revoke(field)"><Undo2 :size="12"/>撤销译文确认（不回滚正式英文）</button></div></footer>
        </component>
      </div>
      <div v-else class="translation-empty"><Check :size="15"/>当前范围没有需要本地 Qwen 翻译的中文高影响导演字段。名称、标题和管理描述只用于标识或创作管理，不会因此创建翻译任务。</div>
    </template>
  </section>
</template>

<style scoped>
.translation-review{display:grid;gap:11px;padding:13px;border:1px solid rgba(94,164,218,.2);border-radius:9px;background:rgba(35,75,105,.055)}.translation-review>header{display:flex;align-items:center;justify-content:space-between;gap:12px}.translation-heading{display:flex;align-items:center;gap:9px}.translation-heading>div{width:32px;height:32px;display:grid;place-items:center;border-radius:8px;background:rgba(94,164,218,.12);color:#79bde5}.translation-heading span{display:grid;gap:3px}.translation-heading strong{font-size:12px}.translation-heading small{color:#7f8c98;font-size:10px;line-height:1.45}.translation-review button{min-height:31px;padding:6px 10px;display:inline-flex;align-items:center;justify-content:center;gap:5px;border:1px solid rgba(94,164,218,.32);border-radius:6px;background:#327caf;color:#eef8ff;font-size:10px;font-weight:700;cursor:pointer}.translation-review button.secondary{background:#111820;color:#a9bac7;border-color:#2a3741}.translation-review button:disabled{opacity:.45;cursor:not-allowed}.translation-safety,.translation-error,.translation-recovery,.translation-loading,.translation-empty{display:flex;align-items:flex-start;gap:7px;padding:9px 10px;border-radius:6px;font-size:10px;line-height:1.55}.translation-safety{border:1px solid rgba(104,197,167,.16);background:rgba(104,197,167,.05);color:#8cb9aa}.translation-error,.source-warning{color:#e59b8f}.translation-error{border:1px solid rgba(229,112,99,.2);background:rgba(229,112,99,.06)}.translation-recovery{border:1px solid rgba(217,164,95,.24);background:rgba(217,164,95,.07);color:#c9a773}.translation-loading,.translation-empty{color:#77838e;background:#0b1015}.translation-summary{display:flex;align-items:center;gap:12px;padding:7px 9px;border:1px solid rgba(255,255,255,.05);border-radius:6px;background:#0a0f14;color:#89949e;font-size:10px}.translation-summary b{color:#dbe5ec}.translation-summary .changed{color:#e59b8f}.translation-summary small{margin-left:auto;color:#65727d}.translation-list{display:grid;gap:9px}.translation-list>article{padding:11px;border:1px solid #28333d;border-radius:7px;background:#0b1015}.translation-list>article.state-success{border-color:rgba(104,197,167,.22)}.translation-list>article.state-danger{border-color:rgba(229,112,99,.25)}.translation-list>article>header{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.translation-list>article>header>div{display:grid;grid-template-columns:auto 1fr;gap:2px 7px}.translation-list>article>header span{grid-row:1/3;padding:3px 5px;border-radius:3px;background:#151e26;color:#6f8799;font:8px Consolas,monospace}.translation-list>article>header strong{font-size:11px}.translation-list>article>header small{color:#6e7a84;font-size:9px}.translation-status{display:inline-flex;align-items:center;gap:4px;padding:4px 6px;border-radius:10px;background:#182028;color:#84919d;font-size:9px}.translation-status.warning,.translation-status.working{color:#d9a45f;background:rgba(217,164,95,.1)}.translation-status.success{color:#71c9ad;background:rgba(104,197,167,.1)}.translation-status.danger{color:#e9867b;background:rgba(229,112,99,.1)}.translation-columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:9px}.translation-columns label{display:grid;gap:5px}.translation-columns label>span{color:#73818c;font-size:9px}.translation-columns textarea{width:100%;resize:vertical;padding:8px;border:1px solid #26323c;border-radius:5px;background:#080c10;color:#c2cdd5;font:10px/1.55 Consolas,monospace;outline:none}.translation-columns textarea:focus{border-color:#477fa4}.translation-columns textarea:read-only,.translation-columns textarea:disabled{color:#8d979f;background:#090d11}.source-warning{margin:7px 0 0;display:flex;align-items:center;gap:5px;font-size:9px}.translation-list>article>footer{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:9px;color:#65727d;font-size:9px}.translation-list>article>footer>div{display:flex;gap:6px}.compact{padding:11px}.compact .translation-columns textarea{max-height:112px}@media(max-width:800px){.translation-review>header,.translation-list>article>footer{align-items:stretch;flex-direction:column}.translation-columns{grid-template-columns:1fr}.translation-summary{flex-wrap:wrap}.translation-summary small{width:100%;margin-left:0}}
.spin{animation:translation-spin 1s linear infinite}@keyframes translation-spin{to{transform:rotate(360deg)}}
.translation-summary .attention{color:#d9a45f}.translation-entry-state{display:flex!important;align-items:center!important;justify-content:flex-end;gap:7px;grid-template-columns:none!important}.translation-entry-state em{display:inline-flex;align-items:center;gap:3px;color:#73818c;font-size:9px;font-style:normal;font-weight:500}.translation-list>details{padding:11px;border:1px solid rgba(104,197,167,.22);border-radius:7px;background:#0b1015}.translation-list>details>summary{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;cursor:pointer;list-style:none}.translation-list>details>summary::-webkit-details-marker{display:none}.translation-list>details>summary>div:first-child{display:grid;grid-template-columns:auto 1fr;gap:2px 7px}.translation-list>details>summary span{grid-row:1/3;padding:3px 5px;border-radius:3px;background:#151e26;color:#6f8799;font:8px Consolas,monospace}.translation-list>details>summary strong{font-size:11px}.translation-list>details>summary small{color:#6e7a84;font-size:9px}.translation-list>details[open]>summary .translation-entry-state em svg{transform:rotate(180deg)}.translation-list>details>footer{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:9px;color:#65727d;font-size:9px}.translation-columns label.stale>span{color:#d18b80}.translation-columns label.stale textarea{border-color:rgba(229,112,99,.22);color:#8d979f;background:#090d11}
@media(max-width:800px){.translation-list>details>summary,.translation-list>details>footer{align-items:stretch;flex-direction:column}.translation-entry-state{justify-content:flex-start}}
.translation-header-actions{display:flex;align-items:center;justify-content:flex-end;gap:7px;flex-wrap:wrap}
.translation-provider{display:grid;gap:3px}.translation-provider span{font-size:8px;color:#73818c}.translation-provider select{min-height:31px;padding:5px 8px;border:1px solid #2a3741;border-radius:6px;background:#0b1015;color:#b9c6cf;font-size:10px}.translation-egress{display:flex;align-items:flex-start;gap:7px;padding:9px 10px;border:1px solid rgba(217,164,95,.24);border-radius:6px;background:rgba(217,164,95,.07);color:#d1ad74;font-size:10px;line-height:1.55}
@media(max-width:800px){.translation-header-actions{align-items:stretch;flex-direction:column}.translation-header-actions button{width:100%}}
</style>
