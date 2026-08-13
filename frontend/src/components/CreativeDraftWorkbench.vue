<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import {
  AlertTriangle, Box, Check, Clock3, Languages, LoaderCircle, Lock,
  MapPin, RotateCcw, Save, ShieldCheck, Sparkles, UserRound, WandSparkles,
} from 'lucide-vue-next'
import { creativeDraftRepository } from '@/services/creativeDraftRepository'
import { promptProvider, REMOTE_PROMPT_EGRESS_NOTICE } from '@/services/providerCapabilities'
import {
  CREATIVE_DRAFT_ENGLISH_LIMITS,
  creativeDraftEditorWarnings,
  creativeFailureRecovery,
  creativeDraftIsEditable,
  creativeDraftIsEmptyFailure,
  creativeDraftRequestError,
  creativeDraftReviewIssueAction,
  creativeDraftReviewIssueTitle,
  creativeSafeFailureDiagnostic,
} from '@/services/creativeDraft'
import type {
  CreativeDraft, CreativeDraftCreativity, CreativeDraftData, CreativeDraftLanguageMode,
  CreativeDraftPlacement, CreativeDraftReviewField, CreativeDraftReviewIssue,
  CreativeGenerationCapability, SeriesProject,
  PromptProviderId,
} from '@/types'

const props = withDefaults(defineProps<{
  project: SeriesProject
  initialEpisodeId?: string
}>(), { initialEpisodeId: '' })
const emit = defineEmits<{ applied: [episodeId: string] }>()

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const selectedEpisodeId = ref(props.initialEpisodeId || props.project.episodes[0]?.id || '')
const capability = ref<CreativeGenerationCapability | null>(null)
const selectedProvider = ref<PromptProviderId>('local_qwen')
const drafts = ref<CreativeDraft[]>([])
const selectedDraftId = ref('')
const workingData = ref<CreativeDraftData | null>(null)
const targetShotCount = ref(4)
const creativity = ref<CreativeDraftCreativity>('balanced')
const variationNote = ref('')
const placement = ref<CreativeDraftPlacement>('replace')
const selectedShotKeys = ref<string[]>([])
const englishReviewAcknowledged = ref(false)
const loading = ref(false)
const capabilityLoading = ref(false)
const action = ref<'generate'|'save'|'regenerate'|'confirm'|'apply'|''>('')
const actionShotKey = ref('')
const error = ref('')
const notice = ref('')
type PendingRiskAction =
  | { kind: 'confirm' }
  | { kind: 'apply', languageMode: CreativeDraftLanguageMode, shotKeys: string[] }
const pendingRiskAction = ref<PendingRiskAction | null>(null)
const riskDialogOpen = ref(false)
const riskAcknowledged = ref(false)

const activeEpisode = computed(() => props.project.episodes.find((item) => item.id === selectedEpisodeId.value))
const activeDraft = computed(() => drafts.value.find((item) => item.id === selectedDraftId.value) || null)
const editable = computed(() => creativeDraftIsEditable(activeDraft.value))
const emptyFailedDraft = computed(() => creativeDraftIsEmptyFailure(activeDraft.value))
const failedDraftDiagnostic = computed(() => creativeSafeFailureDiagnostic(activeDraft.value?.runtimeMetadata))
const failedDraftErrorCode = computed(() => failedDraftDiagnostic.value.code)
const failedDraftReason = computed(() => (
  creativeFailureRecovery(failedDraftErrorCode.value, activeDraft.value?.contextCurrent !== false)
  || error.value
  || '本地 Qwen 未返回可审核的结构化镜头。请一键重试整稿；若持续失败，改为 1–2 镜或缩短本次创作要求。正式分镜未修改。'
))
const dirty = computed(() => Boolean(
  activeDraft.value && workingData.value
  && JSON.stringify(activeDraft.value.draftData) !== JSON.stringify(workingData.value),
))
const localWarnings = computed(() => workingData.value ? creativeDraftEditorWarnings(workingData.value) : [])
const reviewIssues = computed(() => activeDraft.value?.reviewIssues || [])
const actionCoverageIssues = computed(() => reviewIssues.value.filter((item) => (
  /script_action_coverage_low|action_coverage_low|script.*action.*coverage/i.test(item.code)
)))
const actionCoverageSummary = computed(() => {
  const issues = actionCoverageIssues.value
  if (!issues.length) return ''
  const shotIndexes = new Set(issues.map((item) => item.shotIndex).filter((index) => index !== undefined))
  return shotIndexes.size === issues.length
    ? `${shotIndexes.size} 个镜头收到动作覆盖提醒`
    : `本稿收到 ${issues.length} 项动作覆盖提醒`
})
const reviewIssueCount = computed(() => activeDraft.value?.reviewSummary.issueCount || reviewIssues.value.length)
const requiresEditCount = computed(() => activeDraft.value?.reviewSummary.requiresEditCount
  || reviewIssues.value.filter((item) => item.severity === 'requires_edit').length)
const displayedValidationStatus = computed(() => dirty.value ? 'pending' : (activeDraft.value?.validation.status || 'pending'))
const selectedProviderCapability = computed(() => promptProvider(capability.value?.providers, selectedProvider.value))
const generationReady = computed(() => Boolean(selectedProviderCapability.value?.ready ?? (
  selectedProvider.value === 'local_qwen' && capability.value?.supported && capability.value.configured
  && capability.value.modelDownloaded && capability.value.modelVerified
)))
const providerEgressNotice = computed(() => selectedProvider.value === 'deepseek_api'
  ? selectedProviderCapability.value?.dataEgressNotice || REMOTE_PROMPT_EGRESS_NOTICE
  : '')
const minShots = computed(() => capability.value?.minShots || 1)
const maxShots = computed(() => capability.value?.maxShots || 4)
const baselineOutputBudget = computed(() => capability.value?.baselineMaxNewTokens || 0)
const configuredOutputBudget = computed(() => (
  capability.value?.configuredMaxNewTokens || capability.value?.maxNewTokens || 0
))
const normalizedTargetShotCount = computed(() => (
  Math.min(maxShots.value, Math.max(minShots.value, Number(targetShotCount.value) || minShots.value))
))
const targetDurationRange = computed(() => ({
  min: normalizedTargetShotCount.value * 5,
  max: normalizedTargetShotCount.value * 15,
}))
const selectedCount = computed(() => selectedShotKeys.value.length)
const totalDuration = computed(() => (workingData.value?.shots || []).reduce((sum, shot) => sum + shot.durationMs, 0) / 1000)
const canConfirm = computed(() => Boolean(
  activeDraft.value?.canConfirm && !dirty.value && activeDraft.value.contextCurrent,
))
const canApply = computed(() => Boolean(
  activeDraft.value?.canApply && activeDraft.value.status === 'confirmed' && !dirty.value
  && activeDraft.value.contextCurrent && !requiresEditCount.value,
))
const contextEstimate = computed(() => (
  props.project.logline.length + props.project.masterScript.length + (activeEpisode.value?.synopsis.length || 0) + (activeEpisode.value?.script?.length || 0)
))
const visibleSourceTooLong = computed(() => Boolean(
  capability.value?.maxSourceChars && contextEstimate.value > capability.value.maxSourceChars,
))
const statusLabels: Record<CreativeDraft['status'], string> = {
  draft: '创作草稿', confirmed: '已确认审核', applied: '已应用', superseded: '历史版本', failed: '生成失败',
}
const creativityLabels: Record<CreativeDraftCreativity, string> = {
  conservative: '保守补全', balanced: '均衡创作', bold: '大胆变化',
}
const dialogueReviewFields: CreativeDraftReviewField[] = ['dialogueSpeakerId', 'dialogueTextZh', 'dialogueDeliveryZh', 'dialogueDeliveryEn']
const relationReviewFields: CreativeDraftReviewField[] = ['characterIds', 'sceneId', 'propIds']

const firstActionSentence = (value: string): string => {
  const normalized = value.replace(/\s+/g, ' ').trim()
  if (!normalized) return '待补充连续动作链'
  return normalized.match(/^.*?[。！？!?；;]/u)?.[0].trim() || normalized
}

const safeDomToken = (value: string): string => value.replace(/[^a-zA-Z0-9_-]/g, '-').slice(0, 80)
const shotDomId = (shotIndex: number): string => `creative-shot-${safeDomToken(workingData.value?.shots[shotIndex]?.clientKey || String(shotIndex))}`
const fieldDomId = (shotIndex: number, field: CreativeDraftReviewField): string => `${shotDomId(shotIndex)}-${field}`
const targetShotIndex = (issue: CreativeDraftReviewIssue): number | undefined => {
  if (issue.shotIndex !== undefined) return issue.shotIndex
  const shots = workingData.value?.shots || []
  if (dialogueReviewFields.includes(issue.field as CreativeDraftReviewField)) {
    const dialogueIndex = shots.findIndex((shot) => shot.dialogue.length > 0)
    return dialogueIndex >= 0 ? dialogueIndex : undefined
  }
  return shots.length ? 0 : undefined
}
const issuesForField = (shotIndex: number, field: CreativeDraftReviewField): CreativeDraftReviewIssue[] => (
  reviewIssues.value.filter((item) => targetShotIndex(item) === shotIndex && item.field === field)
)
const reviewIssueLocation = (issue: CreativeDraftReviewIssue): string => {
  const resolvedShotIndex = targetShotIndex(issue)
  const shot = resolvedShotIndex === undefined ? undefined : workingData.value?.shots[resolvedShotIndex]
  const fieldLabels: Partial<Record<CreativeDraftReviewField, string>> = {
    titleZh: '中文标题', titleEn: '英文标题', narrativeZh: '中文叙事', narrativeEn: '英文叙事',
    actionZh: '中文连续动作链', actionEn: '英文动作链摘要', cameraZh: '中文运镜', cameraEn: '英文运镜',
    continuityZh: '中文连续性', continuityEn: '英文连续性', soundscapeZh: '中文声景', soundscapeEn: '英文声景',
    musicZh: '中文配乐', musicEn: '英文配乐', sceneId: '场景引用', characterIds: '人物引用', propIds: '道具引用',
    dialogueSpeakerId: '对白说话人 / 原文', dialogueTextZh: '对白正文', dialogueDeliveryZh: '中文对白方式', dialogueDeliveryEn: '英文对白方式',
  }
  const scope = issue.shotIndex === undefined
    ? (resolvedShotIndex === undefined ? '整份草稿' : '全部镜头')
    : `镜头 ${shot?.order ?? issue.shotIndex + 1}`
  return `${scope}${issue.field ? ` · ${fieldLabels[issue.field] || '相关字段'}` : ''}`
}
const jumpToReviewIssue = async (issue: CreativeDraftReviewIssue) => {
  const resolvedShotIndex = targetShotIndex(issue)
  if (resolvedShotIndex === undefined) return
  const section = document.getElementById(shotDomId(resolvedShotIndex)) as HTMLDetailsElement | null
  if (section) section.open = true
  await nextTick()
  const target = (issue.field ? document.getElementById(fieldDomId(resolvedShotIndex, issue.field)) : null) || section
  target?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  const control = target?.querySelector<HTMLElement>('textarea,input,select')
    || (dialogueReviewFields.includes(issue.field as CreativeDraftReviewField)
      ? section?.querySelector<HTMLElement>('.dialogue-lock textarea')
      : undefined)
  control?.focus({ preventScroll: true })
}

const errorMessage = (cause: unknown, fallback: string) => creativeDraftRequestError(cause, fallback)
const persistedDraftIdFromError = (cause: unknown): string => {
  if (!cause || typeof cause !== 'object') return ''
  const data = (cause as { response?: { data?: { draft?: { id?: unknown } } } }).response?.data
  return String(data?.draft?.id || '')
}

const selectDraft = (draft: CreativeDraft) => {
  selectedDraftId.value = draft.id
  workingData.value = clone(draft.draftData)
  selectedShotKeys.value = draft.draftData.shots.map((shot) => shot.clientKey)
  englishReviewAcknowledged.value = false
  riskDialogOpen.value = false
  riskAcknowledged.value = false
  pendingRiskAction.value = null
  error.value = ''; notice.value = ''
}

const loadDrafts = async (preferredId = '') => {
  if (!selectedEpisodeId.value) { drafts.value = []; selectedDraftId.value = ''; workingData.value = null; return }
  loading.value = true; error.value = ''
  try {
    drafts.value = await creativeDraftRepository.list(props.project.id, selectedEpisodeId.value)
    const selected = drafts.value.find((item) => item.id === preferredId)
      || drafts.value.find((item) => ['draft', 'confirmed'].includes(item.status))
      || drafts.value[0]
    if (selected) selectDraft(selected)
    else { selectedDraftId.value = ''; workingData.value = null }
  } catch (cause) { error.value = errorMessage(cause, '无法读取本集的本地 Qwen 创作草稿。') }
  finally { loading.value = false }
}

const loadCapability = async () => {
  capabilityLoading.value = true
  try {
    capability.value = await creativeDraftRepository.capability()
    if (!capability.value.providers?.some((provider) => provider.id === selectedProvider.value)) {
      selectedProvider.value = capability.value.defaultProvider || 'local_qwen'
    }
    targetShotCount.value = Math.min(maxShots.value, Math.max(minShots.value, targetShotCount.value))
  } catch (cause) { error.value = errorMessage(cause, '无法读取本地 Qwen 创作能力。') }
  finally { capabilityLoading.value = false }
}

const generate = async () => {
  if (!activeEpisode.value || !generationReady.value) return
  action.value = 'generate'; error.value = ''; notice.value = ''
  try {
    const draft = await creativeDraftRepository.generate(props.project.id, {
      episodeId: activeEpisode.value.id,
      targetShotCount: Math.min(maxShots.value, Math.max(minShots.value, Number(targetShotCount.value))),
      creativity: creativity.value,
      variationNote: variationNote.value.trim(),
      provider: selectedProvider.value,
    })
    await loadDrafts(draft.id)
  } catch (cause) {
    const message = errorMessage(cause, '本地 Qwen 创作草稿生成失败，正式分镜未被修改。')
    // The server deliberately persists a failed candidate for repair/retry
    // before returning 502. Reconcile so it never becomes invisible to users.
    await loadDrafts(persistedDraftIdFromError(cause))
    error.value = message
  }
  finally { action.value = '' }
}

const replaceDraft = (draft: CreativeDraft) => {
  drafts.value = drafts.value.map((item) => item.id === draft.id ? draft : item)
  if (!drafts.value.some((item) => item.id === draft.id)) drafts.value.unshift(draft)
  selectDraft(draft)
}

const saveCurrent = async (): Promise<CreativeDraft | null> => {
  const current = activeDraft.value
  if (!current || !workingData.value) return null
  if (!dirty.value) return current
  if (!editable.value) throw new Error('当前版本不可编辑；请从它重新生成新草稿。')
  const saved = await creativeDraftRepository.update(current.id, current.revision, workingData.value)
  replaceDraft(saved)
  return saved
}

const save = async () => {
  action.value = 'save'; error.value = ''; notice.value = ''
  try { await saveCurrent() }
  catch (cause) { error.value = errorMessage(cause, '草稿保存失败。') }
  finally { action.value = '' }
}

const regenerate = async (shotKeys: string[] = []) => {
  const current = activeDraft.value
  if (!current) return
  action.value = 'regenerate'; actionShotKey.value = shotKeys[0] || ''; error.value = ''; notice.value = ''
  try {
    const saved = await saveCurrent() || current
    if (!saved.canRegenerate) throw new Error('当前草稿已不能重新生成，请刷新版本记录。')
    const next = await creativeDraftRepository.regenerate(saved.id, {
      expectedRevision: saved.revision,
      targetShotCount: shotKeys.length ? undefined : targetShotCount.value,
      creativity: creativity.value,
      variationNote: variationNote.value.trim(),
      shotKeys,
      provider: selectedProvider.value,
    })
    await loadDrafts(next.id)
  } catch (cause) {
    const message = errorMessage(cause, '重新生成失败；原草稿和正式分镜均保持不变。')
    await loadDrafts(persistedDraftIdFromError(cause) || current.id)
    error.value = message
  }
  finally { action.value = ''; actionShotKey.value = '' }
}

const confirmReview = async (reviewAcknowledged = false) => {
  const current = activeDraft.value
  if (!current) return
  action.value = 'confirm'; error.value = ''; notice.value = ''
  try {
    const saved = await saveCurrent() || current
    replaceDraft(await creativeDraftRepository.confirm(saved.id, saved.revision, reviewAcknowledged))
  } catch (cause) { error.value = errorMessage(cause, '草稿审核确认失败。') }
  finally { action.value = '' }
}

const applyDraft = async (
  languageMode: CreativeDraftLanguageMode,
  shotKeys: string[] = [],
  reviewAcknowledged = false,
) => {
  const current = activeDraft.value
  if (!current || !canApply.value) return
  if (languageMode === 'bilingual_confirmed' && !englishReviewAcknowledged.value) {
    error.value = '采用双语前，请勾选“已逐项核对英文”声明。'; return
  }
  if (shotKeys.length && !shotKeys.every((key) => current.draftData.shots.some((shot) => shot.clientKey === key))) return
  action.value = 'apply'; actionShotKey.value = shotKeys.length === 1 ? shotKeys[0] : ''; error.value = ''; notice.value = ''
  try {
    const result = await creativeDraftRepository.apply(current.id, {
      expectedRevision: current.revision,
      languageMode,
      placement: shotKeys.length ? 'append' : placement.value,
      shotKeys,
      englishReviewAcknowledged: languageMode === 'bilingual_confirmed' ? true : undefined,
      reviewAcknowledged,
    })
    replaceDraft(result.draft)
    notice.value = languageMode === 'bilingual_confirmed'
      ? `已创建 ${result.appliedShotIds.length} 个正式分镜，并写入 ${result.translationsWritten} 条人工确认英文；尚未创建生产任务。`
      : `已创建 ${result.appliedShotIds.length} 个正式中文分镜；未写入英文翻译，后续可继续使用翻译审核流程。`
    emit('applied', result.episodeId || selectedEpisodeId.value)
  } catch (cause) { error.value = errorMessage(cause, '应用草稿失败；正式分镜未被修改。') }
  finally { action.value = ''; actionShotKey.value = '' }
}

const closeRiskDialog = () => {
  riskDialogOpen.value = false
  riskAcknowledged.value = false
  pendingRiskAction.value = null
}

const requestConfirm = () => {
  if (!reviewIssueCount.value) { void confirmReview(false); return }
  pendingRiskAction.value = { kind: 'confirm' }
  riskAcknowledged.value = false
  riskDialogOpen.value = true
}

const requestApply = (languageMode: CreativeDraftLanguageMode, shotKeys: string[] = []) => {
  if (!reviewIssueCount.value) { void applyDraft(languageMode, shotKeys, false); return }
  pendingRiskAction.value = { kind: 'apply', languageMode, shotKeys: [...shotKeys] }
  riskAcknowledged.value = false
  riskDialogOpen.value = true
}

const continueRiskAction = async () => {
  const pending = pendingRiskAction.value
  if (!pending || !riskAcknowledged.value) return
  riskDialogOpen.value = false
  pendingRiskAction.value = null
  if (pending.kind === 'confirm') await confirmReview(true)
  else await applyDraft(pending.languageMode, pending.shotKeys, true)
  riskAcknowledged.value = false
}

const entityName = (kind: 'character'|'scene'|'prop', id: string) => {
  const rows = kind === 'character' ? props.project.bible.characters : kind === 'scene' ? props.project.bible.scenes : props.project.bible.props
  return rows.find((item) => item.id === id || (kind === 'character' && item.stableSpeakerId === id))?.name || `未知 ${id.slice(0, 8)}`
}
const setDuration = (shotIndex: number, value: unknown) => {
  if (!workingData.value) return
  const seconds = Math.min(15, Math.max(5, Number(value || 5)))
  workingData.value.shots[shotIndex].durationMs = Math.round(seconds * 1000)
}
const setIncludeAudio = (shotIndex: number, enabled: boolean) => {
  if (!workingData.value) return
  const shot = workingData.value.shots[shotIndex]
  if (!enabled && shot.dialogue.length) {
    notice.value = `镜头 ${shot.order} 含有锁定对白。已保留你的静音选择和对白原文，保存后会列为人工检查项。`
  }
  shot.includeAudio = enabled
  if (!enabled) {
    shot.soundscapeZh = ''; shot.soundscapeEn = ''; shot.musicZh = ''; shot.musicEn = ''
  }
}
const toggleShot = (key: string) => {
  selectedShotKeys.value = selectedShotKeys.value.includes(key)
    ? selectedShotKeys.value.filter((item) => item !== key)
    : [...selectedShotKeys.value, key]
}
const toggleAll = () => {
  const all = workingData.value?.shots.map((shot) => shot.clientKey) || []
  selectedShotKeys.value = selectedShotKeys.value.length === all.length ? [] : all
}

watch(() => props.project.episodes.map((item) => item.id).join('|'), () => {
  if (!props.project.episodes.some((item) => item.id === selectedEpisodeId.value)) {
    selectedEpisodeId.value = props.initialEpisodeId || props.project.episodes[0]?.id || ''
  }
})
watch(selectedEpisodeId, () => loadDrafts())
watch(() => JSON.stringify(workingData.value), () => {
  if (dirty.value) {
    englishReviewAcknowledged.value = false
    riskAcknowledged.value = false
  }
})
onMounted(async () => { await loadCapability(); await loadDrafts() })
</script>

<template>
  <section class="creative-workbench panel">
    <header class="creative-header">
      <div class="creative-title">
        <div><WandSparkles :size="21"/></div>
        <span><small>LOCAL QWEN STORY AUTHORING</small><strong>结构化双语分镜草稿</strong><p>根据全剧说明、剧本、本集梗概、风格、Bible 与术语表补全；生成结果先进入草稿沙箱，不会直接生产。</p></span>
      </div>
      <span v-if="capability" :class="['capability-pill',{ready:generationReady}]">
        {{ generationReady ? `${selectedProviderCapability?.label || '所选通道'}已就绪` : '创作通道未就绪' }}<template v-if="selectedProvider==='local_qwen'"> · {{ capability.device.toUpperCase() }} 按需加载</template>
      </span>
    </header>

    <div v-if="!project.episodes.length" class="creative-empty">
      <Sparkles :size="22"/><strong>请先新增一个剧集</strong><p>剧集标题和梗概是本地 Qwen 定位本集剧情的必要上下文。</p>
    </div>
    <template v-else>
      <div class="source-snapshot">
        <div><span>创作对象</span><select v-model="selectedEpisodeId"><option v-for="episode in project.episodes" :key="episode.id" :value="episode.id">第 {{ episode.number }} 集 · {{ episode.title }}</option></select></div>
        <div><span>权威输入 · 服务端只读</span><p><b>全剧：</b>{{ project.logline || '未填写一句话说明' }}</p><p><b>本集：</b>{{ activeEpisode?.synopsis || '未填写本集梗概' }}</p><small>全剧母稿 {{ project.masterScript.length }} 字 · 本集剧本 {{ activeEpisode?.script?.length || 0 }} 字 · 本页可见源约 {{ contextEstimate }} 字；服务端还会加入风格、Bible 和 canonical 术语。</small></div>
      </div>

      <div class="generation-controls">
        <label><span>处理通道</span><select v-model="selectedProvider" :disabled="Boolean(action)"><option v-for="provider in capability?.providers || []" :key="provider.id" :value="provider.id" :disabled="!provider.ready">{{ provider.label }}{{ provider.ready ? '' : '（未就绪）' }}</option></select><small>{{ selectedProvider==='deepseek_api' ? '外部 API，通常响应更快。' : '默认本地处理，不发送项目上下文。' }}</small></label>
        <label><span>生成镜数</span><input v-model.number="targetShotCount" type="number" :min="minShots" :max="maxShots"><small>{{ normalizedTargetShotCount }} 镜总时长约 {{ targetDurationRange.min }}–{{ targetDurationRange.max }} 秒（每镜 5–15 秒）；多个快速动作可组成一条连续动作链。</small></label>
        <label><span>创意幅度</span><select v-model="creativity"><option value="conservative">保守补全 · 忠于原剧本</option><option value="balanced">均衡创作 · 推荐</option><option value="bold">大胆变化 · 仍锁定事实</option></select><small>不会改写人物身份、对白正文或事件顺序。</small></label>
        <label class="variation"><span>本次创作要求（可选）</span><input v-model="variationNote" placeholder="例如：加强雨夜压迫感，运镜更克制；不要新增角色"><small>只影响构图、运镜、节奏和声景，不替代剧本事实。</small></label>
        <button class="creative-primary" :disabled="Boolean(action)||capabilityLoading||!generationReady||!activeEpisode||visibleSourceTooLong" title="4 镜实测约 12 分钟；CPU 子进程最长运行 15 分钟，服务端最迟 16 分钟结束，页面等待至 17 分钟" @click="generate">
          <LoaderCircle v-if="action==='generate'" class="spin" :size="14"/><Sparkles v-else :size="14"/>{{ action==='generate'?'本地生成中，请勿重复提交…':'生成新草稿' }}
        </button>
      </div>
      <div v-if="providerEgressNotice" class="creative-alert egress"><AlertTriangle :size="14"/><span><strong>外部 API 数据发送提示：</strong>{{ providerEgressNotice }}</span></div>
      <div v-if="capability" class="creative-budget-guide">
        <Clock3 :size="15"/>
        <span>
          <strong>1–2 镜更适合反复迭代；4 镜实测约 12 分钟</strong>
          <small>输出容量由镜数 + 剧本中的权威对白共同决定。<template v-if="baselineOutputBudget&&configuredOutputBudget">{{ maxShots }} 镜无权威对白时基础量约 {{ baselineOutputBudget }}，本机单次上限 {{ configuredOutputBudget }}；</template>预计超过上限会在加载模型前要求拆段。若运行后仍提示容量用尽或未进入修复，请改为 1–2 镜并按对白边界拆分。</small>
        </span>
      </div>
      <div class="action-timing-guide"><Clock3 :size="15"/><span><strong>动作密集时可提高每镜时长，或压缩单个动作节奏</strong><small>镜数不会把每镜锁死为 5 秒：例如 2 镜最多可承载 30 秒。系统会在 5–15 秒内分配每镜时长；同一连贯动作弧可包含多个快速动作。最终仍请人工核对动作可读性与剧本覆盖。</small></span></div>
      <div v-if="capability&&!generationReady" class="creative-alert"><AlertTriangle :size="14"/><span>{{ selectedProviderCapability?.notice || capability.notice || '所选创作通道尚未配置。' }}</span></div>
      <div v-if="visibleSourceTooLong" class="creative-alert error"><AlertTriangle :size="14"/><span>当前可见剧本源已超过本地模型 {{ capability?.maxSourceChars }} 字上限；请拆分本集剧本或缩短全剧母稿后再生成。</span></div>
      <div v-if="notice" class="creative-alert success"><Check :size="14"/><span>{{ notice }}</span></div>
      <div v-if="error&&!emptyFailedDraft" class="creative-alert error"><AlertTriangle :size="14"/><span>{{ error }}</span></div>

      <div v-if="loading" class="creative-loading"><LoaderCircle class="spin" :size="17"/>读取创作草稿…</div>
      <div v-else-if="!activeDraft||!workingData" class="creative-empty">
        <WandSparkles :size="24"/><strong>本集还没有 AI 创作草稿</strong><p>点击“生成新草稿”。在确认和应用前，正式剧集与生产提示词不会发生任何变化。</p>
      </div>
      <template v-else>
        <div class="draft-history">
          <span>版本记录</span><button v-for="draft in drafts" :key="draft.id" :class="{active:draft.id===activeDraft.id}" @click="selectDraft(draft)">V{{ draft.generationNo }} · {{ statusLabels[draft.status] }}</button>
          <small>{{ activeDraft.provider || 'local_qwen' }} · {{ activeDraft.model || 'Qwen' }}</small>
        </div>

        <div v-if="emptyFailedDraft" class="creative-failed-empty">
          <div class="failed-empty-icon"><AlertTriangle :size="21"/></div>
          <div><small>FAILED AUDIT RECORD · 0 USABLE SHOTS</small><strong>本次生成未产出可编辑镜头</strong><div v-if="failedDraftDiagnostic.code" class="failed-safe-diagnostic"><code>{{ failedDraftDiagnostic.code }}</code><span v-if="failedDraftDiagnostic.path">问题位置：<code>{{ failedDraftDiagnostic.path }}</code></span></div><p>{{ failedDraftReason }}</p><p>“0 镜”只是保留用于追踪的失败记录，不是可保存的空草稿；正式分镜、提示词和生产任务均未修改。</p></div>
          <button v-if="activeDraft.contextCurrent&&activeDraft.canRegenerate" type="button" :disabled="Boolean(action)" @click="regenerate()"><LoaderCircle v-if="action==='regenerate'" class="spin" :size="14"/><RotateCcw v-else :size="14"/>{{ action==='regenerate'?'正在重试整稿…':'一键重试整稿' }}</button>
          <button v-else type="button" :disabled="Boolean(action)||!generationReady||visibleSourceTooLong" @click="generate"><LoaderCircle v-if="action==='generate'" class="spin" :size="14"/><Sparkles v-else :size="14"/>按当前故事生成新草稿</button>
        </div>
        <template v-else>
          <div v-if="!activeDraft.contextCurrent" class="creative-alert error"><AlertTriangle :size="14"/><span>项目说明、剧本、Bible 或术语表已变化。本版本失效且不能应用，请重新生成。</span></div>
          <div v-else-if="activeDraft.status==='failed'&&editable" class="creative-alert"><AlertTriangle :size="14"/><span>该失败版本已经包含可编辑镜头。根据下方结构错误修正中英字段并点击“保存并校验”；若不希望人工修复，可直接点击“整稿重新生成”。</span></div>
        <div class="validation-strip" :class="[displayedValidationStatus,{review:reviewIssueCount}]">
          <ShieldCheck v-if="!dirty&&!reviewIssueCount&&['passed','ready'].includes(displayedValidationStatus)" :size="15"/><AlertTriangle v-else :size="15"/>
          <span>
            <strong>{{ dirty?'当前修改待重新检查':reviewIssueCount?`已生成 · 待人工修正（${reviewIssueCount} 项）`:displayedValidationStatus==='failed'?'历史结构校验未通过':'已生成 · 可确认' }}</strong>
            <small>{{ dirty?'保存后会重新计算问题清单；保存草稿本身不受内容风险限制。':reviewIssueCount?'这些是小模型可能产生的偏差，可直接修改、逐镜重生，或在明确知情后继续。':'当前没有发现需要特别提醒的内容问题，仍建议人工浏览后确认。' }}</small>
          </span>
          <b>{{ workingData.shots.length }} 镜 · {{ totalDuration.toFixed(1) }} 秒</b>
        </div>
        <section v-if="reviewIssues.length" class="review-issues" aria-label="草稿问题清单">
          <header><span><AlertTriangle :size="14"/><strong>建议人工检查</strong><small>{{ requiresEditCount ? `${requiresEditCount} 项引用问题需在正式应用前修正；` : '' }}其余内容风险可由你决定修正、重生或知情采用。</small></span><b>{{ reviewIssueCount }} 项</b></header>
          <ol>
            <li v-for="(issue,issueIndex) in reviewIssues" :key="`${issue.code}-${issue.path}-${issueIndex}`" :class="issue.severity">
              <span><small>{{ reviewIssueLocation(issue) }}</small><strong>{{ creativeDraftReviewIssueTitle(issue) }}</strong><em>{{ issue.suggestionZh || creativeDraftReviewIssueAction(issue) }}</em></span>
              <button v-if="targetShotIndex(issue)!==undefined" type="button" @click="jumpToReviewIssue(issue)">{{ issue.shotIndex===undefined?'查看相关镜头':'跳到字段' }}</button>
            </li>
          </ol>
        </section>
        <div v-if="actionCoverageSummary" class="action-coverage-summary"><AlertTriangle :size="14"/><span><strong>{{ actionCoverageSummary }}</strong><small>这是人工核验提示，不会阻止草稿流转。当前没有可核实的逐动作总数，因此不显示推测性的 X/Y；请跳到对应镜头，对照原剧本核对连续动作链。</small></span></div>
        <ul v-if="localWarnings.length" class="validation-list advisory">
          <li v-for="message in localWarnings" :key="`local-${message}`">{{ message }}</li>
        </ul>

        <div class="episode-copy-editor">
          <header><div><Languages :size="15"/><span><strong>本集标题与梗概 · 正式来源</strong><small>由正式剧集只读注入；创作草稿不会生成、修改或应用这些字段。中文仍是权威源，英文仅供可选查看，不会直接进入 H3 prompt。</small></span></div><span :class="['draft-status',activeDraft.status,{review:reviewIssueCount}]">{{ activeDraft.status==='draft'&&reviewIssueCount ? `已生成 · 待人工修正 ${reviewIssueCount}` : statusLabels[activeDraft.status] }}</span></header>
          <div class="bilingual-grid compact-fields"><label class="source-readonly"><span>中文标题 · 正式来源</span><input :value="activeEpisode?.title??workingData.episode.titleZh" readonly aria-readonly="true"></label><label class="english source-readonly"><span>English title · 可选只读</span><input :value="workingData.episode.titleEn" readonly aria-readonly="true" placeholder="未生成英文标题；不影响分镜或视频。"></label><label class="source-readonly"><span>中文梗概 · 正式来源</span><textarea :value="activeEpisode?.synopsis??workingData.episode.synopsisZh" rows="3" readonly aria-readonly="true"></textarea></label><label class="english source-readonly"><span>English synopsis · 可选只读</span><textarea :value="workingData.episode.synopsisEn" rows="3" readonly aria-readonly="true" placeholder="未生成英文摘要，不影响分镜或视频；本栏仅供只读查看。"></textarea><small v-if="!workingData.episode.synopsisEn.trim()" class="optional-synopsis-note">未生成英文摘要，不影响分镜或视频；本栏仅供只读查看。</small></label></div>
        </div>

        <div class="shot-selection-bar">
          <button class="plain" @click="toggleAll"><Check :size="12"/>{{ selectedCount===workingData.shots.length?'取消全选':'全选' }}</button>
          <span>已选 {{ selectedCount }}/{{ workingData.shots.length }} 镜；可单镜重新生成或只应用所选镜头。</span>
          <button :disabled="Boolean(action)||!activeDraft.canRegenerate||!selectedCount" @click="regenerate(selectedShotKeys)"><RotateCcw :size="12"/>重生成已选</button>
        </div>

        <div class="creative-shots">
          <details v-for="(shot,index) in workingData.shots" :id="shotDomId(index)" :key="shot.clientKey" class="creative-shot" :open="index===0">
            <summary>
              <input type="checkbox" :checked="selectedShotKeys.includes(shot.clientKey)" @click.stop @change="toggleShot(shot.clientKey)">
              <span class="shot-number">{{ String(shot.order).padStart(2,'0') }}</span>
              <span><strong>{{ shot.titleZh || `镜头 ${shot.order}` }}</strong><small>{{ shot.titleEn || 'English title pending' }}</small><small class="shot-action-summary">动作：{{ firstActionSentence(shot.actionZh) }}</small></span>
              <span class="shot-metrics"><Clock3 :size="12"/>{{ (shot.durationMs/1000).toFixed(1) }}s · {{ shot.h3Mode }} · {{ shot.includeAudio?'含音频':'静音' }}</span>
            </summary>
            <div class="shot-body">
              <div class="shot-rules"><ShieldCheck :size="14"/><span>质量合同：5–15 秒、一个连贯动作弧、一个有动机的运镜；动作弧可包含多个节奏清楚的快速连续动作，叙事写清 setup → action chain → end state，连续性写清 in/out。人物、场景、道具和原对白被锁定。</span><button :disabled="Boolean(action)||!activeDraft.canRegenerate" @click="regenerate([shot.clientKey])"><LoaderCircle v-if="action==='regenerate'&&actionShotKey===shot.clientKey" class="spin" :size="12"/><RotateCcw v-else :size="12"/>仅重新生成此镜</button></div>
              <div class="shot-settings"><label :id="fieldDomId(index,'durationMs')"><span>时长（秒）</span><input type="number" min="5" max="15" step="0.5" :value="shot.durationMs/1000" :disabled="!editable" @change="setDuration(index,($event.target as HTMLInputElement).value)"></label><label :id="fieldDomId(index,'h3Mode')"><span>H3 模式意图</span><select v-model="shot.h3Mode" :disabled="!editable"><option value="T2VA">T2VA 文生视频</option><option value="I2VA">I2VA 首帧</option><option value="L2VA">L2VA 尾帧</option><option value="FL2VA">FL2VA 首尾帧</option><option value="Ref2VA">Ref2VA 参考生视频</option></select><small>没有合规参考槽位时，应用会安全降为 T2VA；之后可在分镜编辑器绑定首尾帧。</small></label><label :id="fieldDomId(index,'includeAudio')" class="audio-toggle"><input :checked="shot.includeAudio" type="checkbox" :disabled="!editable" @change="setIncludeAudio(index,($event.target as HTMLInputElement).checked)"><span>原生音频</span></label></div>
              <div class="bible-locks"><span><Lock :size="12"/>Bible 白名单 · 只读</span><i v-for="id in shot.characterIds" :key="`c-${id}`"><UserRound :size="11"/>{{ entityName('character',id) }}</i><i v-if="shot.sceneId"><MapPin :size="11"/>{{ entityName('scene',shot.sceneId) }}</i><i v-for="id in shot.propIds" :key="`p-${id}`"><Box :size="11"/>{{ entityName('prop',id) }}</i><small v-if="!shot.characterIds.length&&!shot.sceneId&&!shot.propIds.length">本镜未绑定 Bible 实体；如不符合剧本，请仅重新生成此镜。</small><div v-for="field in relationReviewFields" :id="fieldDomId(index,field)" :key="field" class="relation-review-anchor"><small v-for="issue in issuesForField(index,field)" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></div></div>
              <div class="bilingual-grid">
                <label :id="fieldDomId(index,'titleZh')"><span>中文镜头标题</span><input v-model="shot.titleZh" :disabled="!editable"><small v-for="issue in issuesForField(index,'titleZh')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                <label :id="fieldDomId(index,'titleEn')" class="english"><span>English shot title</span><input v-model="shot.titleEn" :disabled="!editable"><small v-for="issue in issuesForField(index,'titleEn')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                <label :id="fieldDomId(index,'narrativeZh')"><span>中文叙事结构 · setup → 主动作 → end state</span><textarea v-model="shot.narrativeZh" rows="4" :disabled="!editable"></textarea><small v-for="issue in issuesForField(index,'narrativeZh')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                <label :id="fieldDomId(index,'narrativeEn')" class="english"><span class="bounded-field-title"><b>English narrative structure</b><small :class="{ warning: shot.narrativeEn.length >= CREATIVE_DRAFT_ENGLISH_LIMITS.narrative.editor, overflow: shot.narrativeEn.length > CREATIVE_DRAFT_ENGLISH_LIMITS.narrative.editor }">生成≤{{ CREATIVE_DRAFT_ENGLISH_LIMITS.narrative.generation }} · 编辑 {{ shot.narrativeEn.length }}/{{ CREATIVE_DRAFT_ENGLISH_LIMITS.narrative.editor }}</small></span><textarea v-model="shot.narrativeEn" rows="4" :maxlength="CREATIVE_DRAFT_ENGLISH_LIMITS.narrative.editor" :disabled="!editable"></textarea><small v-for="issue in issuesForField(index,'narrativeEn')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                <label :id="fieldDomId(index,'actionZh')"><span>本镜连续动作链 / 主动作摘要</span><textarea v-model="shot.actionZh" rows="3" :disabled="!editable"></textarea><small v-for="issue in issuesForField(index,'actionZh')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                <label :id="fieldDomId(index,'actionEn')" class="english"><span class="bounded-field-title"><b>English action chain / primary-action summary</b><small :class="{ warning: shot.actionEn.length >= CREATIVE_DRAFT_ENGLISH_LIMITS.action.editor, overflow: shot.actionEn.length > CREATIVE_DRAFT_ENGLISH_LIMITS.action.editor }">生成≤{{ CREATIVE_DRAFT_ENGLISH_LIMITS.action.generation }} · 编辑 {{ shot.actionEn.length }}/{{ CREATIVE_DRAFT_ENGLISH_LIMITS.action.editor }}</small></span><textarea v-model="shot.actionEn" rows="3" :maxlength="CREATIVE_DRAFT_ENGLISH_LIMITS.action.editor" :disabled="!editable"></textarea><small v-for="issue in issuesForField(index,'actionEn')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                <label :id="fieldDomId(index,'cameraZh')"><span>中文单一运镜 + 动机</span><textarea v-model="shot.cameraZh" rows="3" :disabled="!editable"></textarea><small v-for="issue in issuesForField(index,'cameraZh')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                <label :id="fieldDomId(index,'cameraEn')" class="english"><span>English single camera move + motivation</span><textarea v-model="shot.cameraEn" rows="3" :disabled="!editable"></textarea><small v-for="issue in issuesForField(index,'cameraEn')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                <label :id="fieldDomId(index,'continuityZh')"><span>中文 continuity in / out</span><textarea v-model="shot.continuityZh" rows="3" :disabled="!editable"></textarea><small v-for="issue in issuesForField(index,'continuityZh')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                <label :id="fieldDomId(index,'continuityEn')" class="english"><span>English continuity in / out</span><textarea v-model="shot.continuityEn" rows="3" :disabled="!editable"></textarea><small v-for="issue in issuesForField(index,'continuityEn')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                <template v-if="shot.includeAudio">
                  <label :id="fieldDomId(index,'soundscapeZh')"><span>中文整体声景</span><textarea v-model="shot.soundscapeZh" rows="3" :disabled="!editable"></textarea><small v-for="issue in issuesForField(index,'soundscapeZh')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                  <label :id="fieldDomId(index,'soundscapeEn')" class="english"><span>English soundscape</span><textarea v-model="shot.soundscapeEn" rows="3" :disabled="!editable"></textarea><small v-for="issue in issuesForField(index,'soundscapeEn')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                  <label :id="fieldDomId(index,'musicZh')"><span>中文非叙事配乐</span><textarea v-model="shot.musicZh" rows="3" :disabled="!editable"></textarea><small v-for="issue in issuesForField(index,'musicZh')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                  <label :id="fieldDomId(index,'musicEn')" class="english"><span>English non-diegetic music</span><textarea v-model="shot.musicEn" rows="3" :disabled="!editable"></textarea><small v-for="issue in issuesForField(index,'musicEn')" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></label>
                </template>
              </div>
              <div v-if="shot.dialogue.length" class="dialogue-lock"><header><Lock :size="13"/><span><strong>原剧本对白锁定</strong><small>模型和人工均不可在草稿中改写对白正文；只可调整中英 delivery。</small></span></header><div v-for="field in dialogueReviewFields" :id="fieldDomId(index,field)" :key="field" class="dialogue-review-anchor"><small v-for="issue in issuesForField(index,field)" :key="issue.code" class="field-review-issue"><AlertTriangle :size="11"/>{{ creativeDraftReviewIssueTitle(issue) }}<em v-if="issue.suggestionZh">{{ issue.suggestionZh }}</em></small></div><article v-for="(line,lineIndex) in shot.dialogue" :key="`${line.speakerId}-${lineIndex}`"><span>{{ entityName('character',line.speakerId) }}<small>{{ line.voiceover?'画外音':'画内对白' }}</small></span><label><b>中文对白 · 只读</b><textarea :value="line.textZh" rows="2" readonly></textarea></label><label><b>中文 delivery</b><textarea v-model="line.deliveryZh" rows="2" :disabled="!editable"></textarea></label><label class="english"><b>English delivery</b><textarea v-model="line.deliveryEn" rows="2" :disabled="!editable"></textarea></label></article></div>
            </div>
          </details>
        </div>

        <footer class="draft-actions">
          <div><span v-if="dirty" class="dirty-dot">有未保存修改 · 保存后问题清单会重新计算</span><span v-else>修订 {{ activeDraft.revision }} · {{ activeDraft.updatedAt ? new Date(activeDraft.updatedAt).toLocaleString('zh-CN') : '刚刚更新' }}</span><button :disabled="Boolean(action)||!dirty||!editable" title="保存始终允许；服务端会重新计算人工检查项" @click="save"><LoaderCircle v-if="action==='save'" class="spin" :size="12"/><Save v-else :size="12"/>保存并重新检查</button><button :disabled="Boolean(action)||!activeDraft.canRegenerate" @click="regenerate()"><RotateCcw :size="12"/>整稿重新生成</button><button class="confirm" :disabled="Boolean(action)||!canConfirm" @click="requestConfirm"><Check :size="12"/>{{ reviewIssueCount?'查看风险并确认':'确认整份草稿' }}</button></div>
          <div class="apply-zone"><header><span><strong>应用到正式分镜</strong><small>这是唯一会改变正式剧集的步骤；内容风险不会锁死按钮，但会要求再次明确确认。</small></span><select v-model="placement" :disabled="!canApply"><option value="replace">替换本集现有分镜</option><option value="append">追加到本集末尾</option></select></header><div v-if="requiresEditCount" class="requires-edit-note"><AlertTriangle :size="13"/>还有 {{ requiresEditCount }} 项未知素材、说话人或数据引用。它们不是内容审核，但为避免写入无效 ID，正式应用前必须替换、删除或重新生成对应镜头。</div><label class="review-ack"><input v-model="englishReviewAcknowledged" type="checkbox" :disabled="!canApply"><span>我已逐项核对英文中的人物、方位、动作、运镜、数字、道具部位和连续性；允许写入已确认翻译。</span></label><div><button :disabled="Boolean(action)||!canApply||!selectedCount" @click="requestApply('chinese_only',selectedShotKeys.length===workingData.shots.length?[]:selectedShotKeys)"><Languages :size="13"/>{{ selectedCount===workingData.shots.length?'整批仅采用中文':`仅采用中文 · ${selectedCount} 镜` }}</button><button class="bilingual-apply" :disabled="Boolean(action)||!canApply||!selectedCount||!englishReviewAcknowledged" @click="requestApply('bilingual_confirmed',selectedShotKeys.length===workingData.shots.length?[]:selectedShotKeys)"><ShieldCheck :size="13"/>{{ selectedCount===workingData.shots.length?'采用已审中英整集':`采用已审中英 · ${selectedCount} 镜` }}</button></div></div>
        </footer>
        </template>
      </template>
    </template>
    <div v-if="riskDialogOpen&&pendingRiskAction" class="review-risk-backdrop" @click.self="closeRiskDialog">
      <section class="review-risk-dialog" role="dialog" aria-modal="true" aria-labelledby="review-risk-title">
        <header><AlertTriangle :size="18"/><span><strong id="review-risk-title">草稿仍有 {{ reviewIssueCount }} 项待人工判断</strong><small>{{ pendingRiskAction.kind==='confirm'?'确认后仍可在应用前复核。':'继续后会把当前选择写入正式分镜。' }}</small></span></header>
        <p>这些提示不代表草稿不可用，而是本地小模型可能存在的偏差。你可以返回修改、逐镜重生，或在理解风险后继续。</p>
        <ul><li v-for="(issue,index) in reviewIssues.slice(0,6)" :key="`dialog-${issue.code}-${index}`"><b>{{ reviewIssueLocation(issue) }}</b><span>{{ creativeDraftReviewIssueTitle(issue) }}</span></li></ul>
        <small v-if="reviewIssues.length>6" class="more-review-issues">另有 {{ reviewIssues.length-6 }} 项，请在页面问题清单中查看。</small>
        <div v-if="requiresEditCount" class="requires-edit-note"><AlertTriangle :size="13"/>其中 {{ requiresEditCount }} 项涉及未知素材、说话人或数据引用；后端可能要求先替换或删除后才能正式应用。</div>
        <label class="risk-ack"><input v-model="riskAcknowledged" type="checkbox"><span>我已阅读以上提示，确认由我人工判断，并愿意承担继续使用当前草稿的风险。</span></label>
        <footer><button type="button" @click="closeRiskDialog">返回修改</button><button class="continue" type="button" :disabled="!riskAcknowledged||Boolean(action)" @click="continueRiskAction"><Check :size="13"/>{{ pendingRiskAction.kind==='confirm'?'知情并确认草稿':'知情并继续应用' }}</button></footer>
      </section>
    </div>
  </section>
</template>

<style scoped>
.failed-safe-diagnostic{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:5px}.failed-safe-diagnostic code{padding:3px 6px;border:1px solid rgba(223,101,90,.22);border-radius:4px;background:#100d10;color:#d89289;font:8px/1.35 Consolas,monospace}.failed-safe-diagnostic span{color:#918087;font-size:8px}
.creative-alert.egress{border-color:rgba(217,164,79,.3)!important;background:rgba(217,164,79,.07)!important;color:#d6aa69!important}
.creative-workbench{display:grid;gap:13px;padding:16px;border-color:rgba(116,95,191,.26);background:linear-gradient(145deg,rgba(59,43,91,.12),rgba(10,14,18,.96) 42%)}.creative-header,.creative-title,.creative-title>span,.source-snapshot,.generation-controls,.draft-history,.validation-strip,.episode-copy-editor>header,.shot-selection-bar,.creative-shot summary,.shot-rules,.bible-locks,.dialogue-lock>header,.draft-actions,.draft-actions>div,.apply-zone>header,.apply-zone>div{display:flex;align-items:center}.creative-header{justify-content:space-between;gap:15px}.creative-title{gap:11px}.creative-title>div{width:39px;height:39px;display:grid;place-items:center;border-radius:10px;background:rgba(130,105,218,.14);color:#a98fea}.creative-title>span{align-items:flex-start;flex-direction:column;gap:2px}.creative-title small{font:8px/1.3 Consolas,monospace;letter-spacing:.14em;color:#8a72c3}.creative-title strong{font-size:15px}.creative-title p{margin:0;color:#78828c;font-size:10px}.capability-pill{padding:6px 9px;border:1px solid rgba(220,107,92,.22);border-radius:15px;color:#c8786d;font-size:9px}.capability-pill.ready{border-color:rgba(101,198,164,.23);color:#70c8aa}.source-snapshot{align-items:stretch;gap:9px;padding:10px;border:1px solid rgba(255,255,255,.06);border-radius:8px;background:#090d12}.source-snapshot>div{display:grid;align-content:start;gap:5px}.source-snapshot>div:first-child{min-width:225px}.source-snapshot>div:last-child{flex:1}.source-snapshot span,.generation-controls label>span{color:#818c96;font-size:9px}.source-snapshot select,.generation-controls input,.generation-controls select,.episode-copy-editor input,.episode-copy-editor textarea,.shot-settings input,.shot-settings select,.bilingual-grid input,.bilingual-grid textarea,.dialogue-lock textarea,.apply-zone select{width:100%;padding:8px;border:1px solid #28333c;border-radius:5px;background:#090d11;color:#c9d3da;font:10px/1.5 inherit;outline:none}.source-snapshot p{margin:0;color:#afb9c0;font-size:10px}.source-snapshot small,.generation-controls small{color:#68737d;font-size:8px}.generation-controls{align-items:end;gap:9px}.generation-controls label{display:grid;gap:5px;min-width:145px}.generation-controls .variation{flex:1}.creative-primary,.generation-controls button,.shot-selection-bar button,.shot-rules button,.draft-actions button{min-height:33px;padding:7px 11px;display:inline-flex;align-items:center;justify-content:center;gap:5px;border:1px solid rgba(129,105,215,.35);border-radius:6px;background:#674fa8;color:#fff;font-size:10px;font-weight:700;cursor:pointer}.creative-primary:disabled,.generation-controls button:disabled,.shot-selection-bar button:disabled,.shot-rules button:disabled,.draft-actions button:disabled{opacity:.4;cursor:not-allowed}.creative-alert,.creative-loading,.creative-empty{display:flex;align-items:flex-start;gap:8px;padding:10px;border:1px solid rgba(219,163,87,.2);border-radius:7px;background:rgba(219,163,87,.05);color:#c49c67;font-size:10px;line-height:1.5}.creative-alert.error{border-color:rgba(223,101,90,.25);background:rgba(223,101,90,.06);color:#df8177}.creative-loading{color:#86929c;border-color:#202b34}.creative-empty{align-items:center;justify-content:center;flex-direction:column;min-height:105px;color:#75808a;border-color:#202a32;background:#090d11}.creative-empty p{margin:0}.draft-history{gap:7px;overflow:auto}.draft-history>span{color:#77828b;font-size:9px}.draft-history button{padding:5px 8px;border:1px solid #27313a;border-radius:12px;background:#0b1015;color:#77838d;font-size:9px;white-space:nowrap;cursor:pointer}.draft-history button.active{border-color:rgba(132,106,222,.42);background:rgba(132,106,222,.1);color:#b39de5}.draft-history small{margin-left:auto;color:#66717b}.validation-strip{gap:8px;padding:9px 10px;border:1px solid rgba(217,164,79,.2);border-radius:7px;background:rgba(217,164,79,.04);color:#caa05f}.validation-strip.passed{border-color:rgba(96,198,164,.2);background:rgba(96,198,164,.04);color:#74c9ad}.validation-strip span{display:grid;gap:2px}.validation-strip small{color:#75817f;font-size:8px}.validation-strip>b{margin-left:auto;font-size:10px}.validation-list{display:grid;gap:4px;margin:0;padding:9px 9px 9px 26px;border-radius:6px;background:#090d11;color:#c99b62;font-size:9px}.validation-list li.error{color:#df8177}.episode-copy-editor{padding:11px;border:1px solid #252f38;border-radius:7px;background:#0a0e13}.episode-copy-editor>header{justify-content:space-between;margin-bottom:9px}.episode-copy-editor>header>div{display:flex;align-items:center;gap:7px}.episode-copy-editor>header span{display:grid}.episode-copy-editor>header small{color:#717c86;font-size:8px}.draft-status{padding:4px 7px!important;border-radius:12px;background:#161d24;color:#89949d;font-size:9px}.draft-status.confirmed{background:rgba(95,197,164,.1);color:#73c8ac}.bilingual-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.bilingual-grid label{display:grid;gap:5px}.bilingual-grid label>span,.dialogue-lock label>b{color:#7c8791;font-size:9px;font-weight:500}.bilingual-grid label.english>span,.dialogue-lock .english>b{color:#9b87d0}.bilingual-grid .english input,.bilingual-grid .english textarea,.dialogue-lock .english textarea{border-color:rgba(133,110,209,.25);background:rgba(78,57,117,.055)}.bilingual-grid input:disabled,.bilingual-grid textarea:disabled,.episode-copy-editor input:disabled,.episode-copy-editor textarea:disabled{opacity:.7}.compact-fields textarea{min-height:72px}.optional-synopsis-note{color:#7fa899;font-size:8px;line-height:1.45}.shot-selection-bar{gap:8px;padding:7px 9px;border-radius:6px;background:#0a0e13;color:#7b8791;font-size:9px}.shot-selection-bar span{flex:1}.shot-selection-bar button{min-height:28px;background:#141b22;color:#a2aeb7;border-color:#2a3540}.shot-selection-bar button.plain{background:transparent}.creative-shots{display:grid;gap:8px}.creative-shot{border:1px solid #26313a;border-radius:8px;background:#090d12;overflow:hidden}.creative-shot[open]{border-color:rgba(132,106,222,.25)}.creative-shot summary{gap:8px;padding:10px;cursor:pointer;list-style:none}.creative-shot summary::-webkit-details-marker{display:none}.creative-shot summary>span:nth-of-type(2){display:grid;gap:1px}.creative-shot summary small{color:#737f89;font-size:8px}.shot-number{font:11px Consolas,monospace;color:#b495e9}.shot-metrics{margin-left:auto!important;display:flex!important;align-items:center!important;gap:5px!important;color:#8b969f!important;font-size:9px!important}.shot-body{display:grid;gap:10px;padding:0 10px 11px;border-top:1px solid #202931}.shot-rules{gap:7px;padding-top:9px;color:#86a59b;font-size:9px}.shot-rules span{flex:1}.shot-rules button{min-height:28px;background:#151c23;color:#aeb8c0;border-color:#2b3640}.shot-settings{display:grid;grid-template-columns:120px 180px 130px;gap:8px}.shot-settings label{display:grid;gap:4px;color:#7c8790;font-size:9px}.shot-settings .audio-toggle{display:flex;align-items:center;gap:7px;padding-top:16px}.shot-settings .audio-toggle input{width:auto}.bible-locks{flex-wrap:wrap;gap:6px;padding:7px 8px;border:1px solid rgba(94,154,204,.13);border-radius:6px;background:rgba(43,86,119,.04)}.bible-locks>span{display:flex;align-items:center;gap:4px;color:#78a0bd;font-size:9px}.bible-locks i{display:inline-flex;align-items:center;gap:3px;padding:3px 6px;border-radius:10px;background:#141c24;color:#9fadb7;font-size:8px;font-style:normal}.bible-locks small{color:#69757e}.dialogue-lock{display:grid;gap:7px;padding:9px;border:1px solid rgba(88,158,201,.16);border-radius:7px;background:rgba(35,74,101,.04)}.dialogue-lock>header{gap:6px;color:#83abc5}.dialogue-lock>header span{display:grid}.dialogue-lock>header small{color:#6d7a83;font-size:8px}.dialogue-lock article{display:grid;grid-template-columns:120px repeat(3,minmax(0,1fr));gap:7px}.dialogue-lock article>span{display:grid;align-content:start;gap:2px;color:#a9b5bd;font-size:10px}.dialogue-lock article>span small{color:#68747e;font-size:8px}.dialogue-lock label{display:grid;gap:4px}.dialogue-lock textarea:read-only{color:#9ea8af;background:#080b0e}.draft-actions{align-items:stretch;flex-direction:column;gap:10px;padding-top:3px}.draft-actions>div:first-child{gap:7px;color:#75808a;font-size:9px}.draft-actions>div:first-child>span{margin-right:auto}.draft-actions button{min-height:30px;background:#151c23;color:#aeb9c0;border-color:#2b3640}.draft-actions button.confirm{background:#466f68;color:#e9f7f3;border-color:#55887e}.dirty-dot{color:#d2a15c!important}.apply-zone{display:grid!important;gap:8px;padding:11px;border:1px solid rgba(98,198,164,.2);border-radius:8px;background:rgba(98,198,164,.035)}.apply-zone>header{justify-content:space-between}.apply-zone>header>span{display:grid}.apply-zone>header small{color:#71807c;font-size:8px}.apply-zone select{width:auto;min-width:190px}.review-ack{display:flex;align-items:flex-start;gap:7px;color:#91a49e;font-size:9px;line-height:1.5}.review-ack input{margin-top:2px}.apply-zone>div{justify-content:flex-end;gap:7px}.apply-zone button{background:#315e55;color:#ecfaf6;border-color:#457d70}.apply-zone button.bilingual-apply{background:#674fa8;border-color:#846bd1}.spin{animation:creative-spin 1s linear infinite}@keyframes creative-spin{to{transform:rotate(360deg)}}@media(max-width:900px){.creative-header,.source-snapshot,.generation-controls,.draft-actions>div:first-child{align-items:stretch;flex-direction:column}.source-snapshot>div:first-child{min-width:0}.bilingual-grid{grid-template-columns:1fr}.shot-settings{grid-template-columns:1fr}.dialogue-lock article{grid-template-columns:1fr}.draft-history small{margin-left:0}.apply-zone>header{align-items:stretch;flex-direction:column}.apply-zone select{width:100%}}
.bounded-field-title{display:flex;align-items:center;justify-content:space-between;gap:8px}.bounded-field-title b{font:inherit;font-weight:500}.bounded-field-title small{color:#7f8992;font:8px/1.2 Consolas,monospace;white-space:nowrap}.bounded-field-title small.warning{color:#d2a15c}.bounded-field-title small.overflow{color:#df8177}
.creative-budget-guide{display:flex;align-items:flex-start;gap:8px;padding:9px 10px;border:1px solid rgba(102,151,198,.2);border-radius:7px;background:rgba(45,90,128,.055);color:#78a6c5}.creative-budget-guide>span{display:grid;gap:3px}.creative-budget-guide strong{font-size:10px}.creative-budget-guide small{color:#748692;font-size:8px;line-height:1.5}
.creative-failed-empty{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:12px;align-items:center;padding:15px;border:1px solid rgba(223,101,90,.28);border-radius:9px;background:linear-gradient(135deg,rgba(223,101,90,.075),rgba(10,14,18,.92))}.failed-empty-icon{width:38px;height:38px;display:grid;place-items:center;border-radius:9px;background:rgba(223,101,90,.11);color:#df8177}.creative-failed-empty>div:nth-child(2){display:grid;gap:4px}.creative-failed-empty small{color:#b66f6a;font:8px/1.3 Consolas,monospace;letter-spacing:.1em}.creative-failed-empty strong{color:#d8dfe4;font-size:12px}.creative-failed-empty p{margin:0;color:#818b93;font-size:9px;line-height:1.5}.creative-failed-empty button{min-height:34px;padding:8px 12px;display:inline-flex;align-items:center;justify-content:center;gap:6px;border:1px solid rgba(223,101,90,.35);border-radius:6px;background:#6f3d3d;color:#fff;font-size:10px;font-weight:700;cursor:pointer}.creative-failed-empty button:disabled{opacity:.45;cursor:not-allowed}@media(max-width:900px){.creative-failed-empty{grid-template-columns:auto 1fr}.creative-failed-empty button{grid-column:1/-1}}
.shot-settings small{color:#64717a;font-size:8px;line-height:1.4}
.episode-copy-editor .source-readonly input:read-only,.episode-copy-editor .source-readonly textarea:read-only{cursor:default;color:#a7b1b8;background:#080c11}.episode-copy-editor .source-readonly input:read-only:focus,.episode-copy-editor .source-readonly textarea:read-only:focus{border-color:#28333c;outline:none}.episode-copy-editor .source-readonly textarea::placeholder,.episode-copy-editor .source-readonly input::placeholder{color:#68747d}.episode-copy-editor .optional-synopsis-note{color:#7fa899}
.creative-alert.success{border-color:rgba(96,198,164,.22);background:rgba(96,198,164,.05);color:#74c9ad}
.action-timing-guide{display:flex;align-items:flex-start;gap:8px;padding:9px 10px;border:1px solid rgba(96,198,164,.18);border-radius:7px;background:rgba(96,198,164,.035);color:#7db5a2}.action-timing-guide>span{display:grid;gap:3px}.action-timing-guide strong{font-size:10px}.action-timing-guide small{color:#74877f;font-size:8px;line-height:1.5}
.action-coverage-summary{display:flex;align-items:flex-start;gap:7px;padding:9px 10px;border:1px solid rgba(218,158,73,.24);border-radius:7px;background:rgba(218,158,73,.045);color:#d2a15c}.action-coverage-summary>span{display:grid;gap:2px}.action-coverage-summary strong{font-size:9px}.action-coverage-summary small{color:#817666;font-size:8px;line-height:1.5}.shot-action-summary{max-width:min(680px,60vw);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#9e8b6d!important}
.validation-strip.review{border-color:rgba(218,158,73,.32);background:rgba(218,158,73,.065);color:#d7a65f}.draft-status.review{background:rgba(218,158,73,.12);color:#d8aa68}
.review-issues{display:grid;gap:8px;padding:10px;border:1px solid rgba(218,158,73,.24);border-radius:8px;background:rgba(218,158,73,.045)}.review-issues>header{display:flex;align-items:center;justify-content:space-between;gap:10px}.review-issues>header>span{display:grid;grid-template-columns:auto auto;align-items:center;justify-content:start;gap:2px 6px;color:#d5a45f}.review-issues>header small{grid-column:2;color:#837665;font-size:8px}.review-issues>header>b{padding:3px 7px;border-radius:10px;background:rgba(218,158,73,.11);color:#d8aa68;font-size:9px}.review-issues ol{display:grid;gap:5px;margin:0;padding:0;list-style:none}.review-issues li{display:flex;align-items:center;gap:9px;padding:7px 8px;border:1px solid rgba(255,255,255,.055);border-radius:6px;background:#090d11}.review-issues li.requires_edit{border-color:rgba(223,101,90,.22);background:rgba(223,101,90,.045)}.review-issues li>span{display:grid;gap:2px;min-width:0;flex:1}.review-issues li small{color:#7c8790;font-size:8px}.review-issues li strong{color:#c4a16d;font-size:9px}.review-issues li.requires_edit strong{color:#dd8378}.review-issues li em{color:#7f8991;font-size:8px;font-style:normal}.review-issues li button{padding:5px 8px;border:1px solid #323b43;border-radius:5px;background:#11171d;color:#9da8b0;font-size:8px;cursor:pointer}.validation-list.advisory{color:#b29060}
.field-review-issue{display:flex!important;align-items:flex-start;gap:4px;padding:5px 6px;border:1px solid rgba(218,158,73,.2);border-radius:5px;background:rgba(218,158,73,.05);color:#c9a064!important;font-size:8px!important;line-height:1.45}.field-review-issue svg{flex:0 0 auto;margin-top:1px}.field-review-issue em{display:block;color:#849099;font-style:normal}.dialogue-review-anchor:empty,.relation-review-anchor:empty{display:none}.dialogue-review-anchor,.relation-review-anchor{display:grid;gap:4px;width:100%}
.review-risk-backdrop{position:fixed;inset:0;z-index:1200;display:grid;place-items:center;padding:20px;background:rgba(2,5,8,.76);backdrop-filter:blur(4px)}.review-risk-dialog{width:min(560px,100%);display:grid;gap:12px;padding:17px;border:1px solid rgba(218,158,73,.3);border-radius:11px;background:#0d1217;box-shadow:0 24px 80px rgba(0,0,0,.55)}.review-risk-dialog>header{display:flex;align-items:flex-start;gap:9px;color:#d8a65f}.review-risk-dialog>header>span{display:grid;gap:2px}.review-risk-dialog>header small,.review-risk-dialog>p,.more-review-issues{color:#7f8991;font-size:9px;line-height:1.5}.review-risk-dialog>p{margin:0}.review-risk-dialog>ul{display:grid;gap:5px;max-height:210px;overflow:auto;margin:0;padding:0;list-style:none}.review-risk-dialog>ul li{display:grid;grid-template-columns:115px 1fr;gap:8px;padding:7px;border-radius:5px;background:#090d11;font-size:9px}.review-risk-dialog>ul b{color:#87929b}.review-risk-dialog>ul span{color:#c5a06a}.requires-edit-note{display:flex;align-items:flex-start;gap:6px;padding:7px;border:1px solid rgba(223,101,90,.22);border-radius:5px;background:rgba(223,101,90,.05);color:#d78278;font-size:9px;line-height:1.45}.risk-ack{display:flex;align-items:flex-start;gap:7px;color:#a8b1b7;font-size:9px;line-height:1.5}.risk-ack input{margin-top:2px}.review-risk-dialog>footer{display:flex;justify-content:flex-end;gap:7px}.review-risk-dialog>footer button{min-height:31px;padding:7px 10px;border:1px solid #303941;border-radius:6px;background:#151b21;color:#aeb7bd;font-size:9px;cursor:pointer}.review-risk-dialog>footer button.continue{display:inline-flex;align-items:center;gap:5px;border-color:rgba(218,158,73,.35);background:#725226;color:#fff}.review-risk-dialog>footer button:disabled{opacity:.4;cursor:not-allowed}
@media(max-width:900px){.review-issues li{align-items:flex-start;flex-direction:column}.review-risk-dialog>ul li{grid-template-columns:1fr}.review-risk-dialog>footer{flex-direction:column-reverse}.review-risk-dialog>footer button{width:100%}}
</style>
