<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  AlertTriangle, Check, Clock3, LoaderCircle, RefreshCw,
  ShieldCheck, Sparkles, Trash2, WandSparkles,
} from 'lucide-vue-next'
import { seriesConceptDraftRepository } from '@/services/seriesConceptDraftRepository'
import {
  cloneSeriesConceptCandidate,
  SERIES_CONCEPT_MAX_IDEA_CHARS,
  seriesConceptCandidateErrors,
  seriesConceptDraftInputErrors,
  seriesConceptPlanningEstimate,
  seriesConceptDraftRequestFailure,
  seriesConceptSourceSnapshot,
  type SeriesConceptDiagnostic,
} from '@/services/seriesConceptDraft'
import {
  promptProvider,
  REMOTE_PROMPT_EGRESS_NOTICE,
} from '@/services/providerCapabilities'
import type {
  PromptProviderId,
  SeriesConceptCandidate,
  SeriesConceptDraftInput,
  SeriesConceptDraftResult,
  SeriesConceptGenerationCapability,
} from '@/types'

const props = withDefaults(defineProps<{ styleHint?: string }>(), { styleHint: '' })
const emit = defineEmits<{ accept: [candidate: SeriesConceptCandidate] }>()

const idea = ref('')
const episodeCount = ref(3)
const shotsPerEpisode = ref(3)
const episodeDurationSeconds = ref(60)
const episodeDurationEdited = ref(false)
const selectedProvider = ref<PromptProviderId>('local_qwen')
const providerSelectionEdited = ref(false)
const capability = ref<SeriesConceptGenerationCapability | null>(null)
const capabilityLoading = ref(false)
const capabilityError = ref('')
const generating = ref(false)
const generationError = ref('')
const generationDiagnostic = ref<SeriesConceptDiagnostic | null>(null)
const notice = ref('')
const attempt = ref(0)
const result = ref<SeriesConceptDraftResult | null>(null)
const candidate = ref<SeriesConceptCandidate | null>(null)
const generatedSourceSnapshot = ref('')
const generatedCandidateSnapshot = ref('')
const acceptedCandidateSnapshot = ref('')
const generatedAt = ref('')
let requestSequence = 0
let capabilitySequence = 0

const selectedProviderCapability = computed(() => promptProvider(
  capability.value?.providers,
  selectedProvider.value,
))
const currentInput = computed<SeriesConceptDraftInput>(() => ({
  idea: idea.value,
  provider: selectedProvider.value,
  episodeCount: Number(episodeCount.value),
  shotsPerEpisode: Number(shotsPerEpisode.value),
  episodeDurationSeconds: Number(episodeDurationSeconds.value),
  styleHint: props.styleHint,
  attempt: Math.min(100, attempt.value + 1),
}))
const inputErrors = computed(() => seriesConceptDraftInputErrors(
  currentInput.value,
  capability.value || undefined,
))
const planningEstimate = computed(() => seriesConceptPlanningEstimate(
  currentInput.value,
  selectedProviderCapability.value,
))
const candidateErrors = computed(() => seriesConceptCandidateErrors(candidate.value))
const currentSourceSnapshot = computed(() => seriesConceptSourceSnapshot(currentInput.value))
const sourceChanged = computed(() => Boolean(
  result.value
  && generatedSourceSnapshot.value
  && generatedSourceSnapshot.value !== currentSourceSnapshot.value,
))
const candidateEdited = computed(() => Boolean(
  candidate.value
  && generatedCandidateSnapshot.value
  && JSON.stringify(candidate.value) !== generatedCandidateSnapshot.value,
))
const candidateAccepted = computed(() => Boolean(
  candidate.value
  && acceptedCandidateSnapshot.value
  && JSON.stringify(candidate.value) === acceptedCandidateSnapshot.value,
))
const generationReady = computed(() => Boolean(
  capability.value?.supported
  && (
    selectedProviderCapability.value?.ready
    || selectedProviderCapability.value?.temporarilyBusy
  )
  && !inputErrors.value.length,
))
const canAccept = computed(() => Boolean(candidate.value && !candidateErrors.value.length && !generating.value))
const providerEgressNotice = computed(() => selectedProvider.value === 'deepseek_api'
  ? selectedProviderCapability.value?.dataEgressNotice || REMOTE_PROMPT_EGRESS_NOTICE
  : '')
const providerSuitabilityNotice = computed(() => (
  selectedProviderCapability.value?.seriesConceptDraftSuitability === 'experimental'
    ? selectedProviderCapability.value.seriesConceptDraftSuitabilityNotice
      || '所选通道对完整连续剧严格结构生成属于实验级；可能出现结构不完整，失败不会修改正式表单。'
    : ''
))
const diagnosticPhaseLabel = (value: string): string => ({
  request_validation: '输入校验',
  request_protocol: '请求协议',
  resource_busy: '共享资源',
  runtime_availability: '通道可用性',
  provider_runtime: '上游运行',
  provider_transport: '上游网络或请求',
  provider_output: '上游输出',
  provider_decode: '结构解析',
  provider_validation: '兼容模板校验',
  parent_process: '本地进程等待',
  transport_decode: '本地进程协议解析',
  child_process_exit: '本地进程退出',
  model_load: '模型加载',
  inference: '模型推理',
  response_validation: '响应安全校验',
  runtime: '运行阶段',
}[value] || '运行阶段')
const diagnosticFinishReasonLabel = (value: string): string => ({
  length: '达到输出长度后停止',
  stop: '正常停止',
  content_filter: '上游中止输出',
}[value] || '其他停止原因')
const returnedShotDurationRange = computed(() => {
  const durations = result.value?.format.shotDurationsMs.flat() || []
  if (!durations.length) return ''
  const minimum = Math.min(...durations) / 1_000
  const maximum = Math.max(...durations) / 1_000
  return minimum === maximum ? `${minimum.toLocaleString()} 秒/镜` : `${minimum.toLocaleString()}–${maximum.toLocaleString()} 秒/镜`
})
const bibleSections = computed(() => {
  if (!candidate.value) return []
  return [
    { key: 'characters', label: '角色', items: candidate.value.bible.characters },
    { key: 'scenes', label: '场景', items: candidate.value.bible.scenes },
    { key: 'props', label: '道具', items: candidate.value.bible.props },
  ] as const
})
const bibleEntityCount = computed(() => bibleSections.value.reduce(
  (sum, section) => sum + section.items.length,
  0,
))
const status = computed(() => {
  if (generating.value) return { label: `${selectedProviderCapability.value?.label || '所选通道'}生成中`, tone: 'working' }
  if (generationError.value && candidate.value) return { label: '生成失败 · 已保留上一候选', tone: 'danger' }
  if (generationError.value || capabilityError.value) return { label: '生成通道异常 · 可继续手工填写', tone: 'danger' }
  if (candidateAccepted.value) return { label: '已填入 · 尚未创建项目', tone: 'success' }
  if (candidate.value) return { label: sourceChanged.value ? '候选依据已变化 · 仍可采用' : '候选待人工确认', tone: 'warning' }
  if (capabilityLoading.value) return { label: '正在读取模型能力', tone: 'working' }
  if (selectedProviderCapability.value?.temporarilyBusy) return { label: '共享资源暂忙 · 可稍后生成', tone: 'warning' }
  if (!generationReady.value) return { label: '所选通道未就绪 · 不影响手工创建', tone: 'muted' }
  return { label: '可生成独立候选', tone: 'ready' }
})

const loadCapability = async () => {
  const sequence = ++capabilitySequence
  capabilityLoading.value = true
  capabilityError.value = ''
  try {
    const next = await seriesConceptDraftRepository.capability()
    if (sequence !== capabilitySequence) return
    capability.value = next
    if (!episodeDurationEdited.value) episodeDurationSeconds.value = next.defaultEpisodeDurationSeconds
    const preferred = promptProvider(next.providers, next.preferredProvider)
    const configuredDefault = promptProvider(next.providers, next.defaultProvider)
    const fallbackReady = next.providers.find((provider) => (
      provider.ready || provider.temporarilyBusy
    ))
    const selectedStillAvailable = promptProvider(next.providers, selectedProvider.value)
    if (!providerSelectionEdited.value || !selectedStillAvailable) {
      selectedProvider.value = preferred?.ready || preferred?.temporarilyBusy
        ? next.preferredProvider
        : configuredDefault?.ready || configuredDefault?.temporarilyBusy
          ? next.defaultProvider
          : fallbackReady?.id || next.defaultProvider
    }
  } catch {
    if (sequence !== capabilitySequence) return
    capability.value = null
    capabilityError.value = '无法读取本地模型与 DeepSeek 通道状态；你仍可直接手工填写并创建项目。'
  } finally {
    if (sequence === capabilitySequence) capabilityLoading.value = false
  }
}

const generate = async () => {
  if (generating.value) return
  if (inputErrors.value.length) {
    generationDiagnostic.value = null
    generationError.value = `${inputErrors.value[0]}手工表单和上一版候选均未修改。`
    return
  }
  if (!generationReady.value) {
    generationDiagnostic.value = null
    generationError.value = '所选模型通道尚未就绪；请切换可用通道、检查配置，或继续手工填写。'
    return
  }

  const nextAttempt = Math.min(100, attempt.value + 1)
  const requestInput: SeriesConceptDraftInput = { ...currentInput.value, attempt: nextAttempt }
  const requestSnapshot = seriesConceptSourceSnapshot(requestInput)
  const sequence = ++requestSequence
  attempt.value = nextAttempt
  generating.value = true
  generationError.value = ''
  generationDiagnostic.value = null
  notice.value = ''
  try {
    const next = await seriesConceptDraftRepository.generate(requestInput)
    if (sequence !== requestSequence) return
    result.value = next
    candidate.value = cloneSeriesConceptCandidate(next.candidate)
    generatedCandidateSnapshot.value = JSON.stringify(candidate.value)
    acceptedCandidateSnapshot.value = ''
    generatedSourceSnapshot.value = requestSnapshot
    generatedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    notice.value = '候选已生成并与正式表单隔离；请编辑、核对后再明确确认填入。'
  } catch (error) {
    if (sequence !== requestSequence) return
    const failure = seriesConceptDraftRequestFailure(error, requestInput.provider)
    generationError.value = failure.message
    generationDiagnostic.value = failure.diagnostic
  } finally {
    if (sequence === requestSequence) generating.value = false
  }
}

const discard = () => {
  if (generating.value) return
  result.value = null
  candidate.value = null
  generatedSourceSnapshot.value = ''
  generatedCandidateSnapshot.value = ''
  acceptedCandidateSnapshot.value = ''
  generatedAt.value = ''
  generationError.value = ''
  generationDiagnostic.value = null
  notice.value = '候选已放弃；正式故事基础没有被修改。'
}

const accept = () => {
  if (!candidate.value || !canAccept.value) return
  const accepted = cloneSeriesConceptCandidate(candidate.value)
  emit('accept', accepted)
  acceptedCandidateSnapshot.value = JSON.stringify(accepted)
  notice.value = sourceChanged.value
    ? '你已人工确认采用依据已变化的候选，并填入故事基础；项目尚未创建，请继续核对。'
    : '候选已填入故事基础；项目尚未创建，请继续核对后点击“创建并进入工作室”。'
}

onMounted(loadCapability)
onBeforeUnmount(() => {
  requestSequence += 1
  capabilitySequence += 1
})
</script>

<template>
  <section class="series-concept-workbench">
    <header>
      <div class="series-concept-title">
        <span class="series-concept-icon"><WandSparkles :size="19"/></span>
        <span><small>AI SERIES CONCEPT</small><strong>从一个想法生成故事基础</strong><p>一次生成相互一致的项目名、梗概和兼容自动分集分镜的完整剧本；结果不会自动写入或创建项目。</p></span>
      </div>
      <span class="series-concept-status" :class="status.tone">
        <LoaderCircle v-if="generating || capabilityLoading" :size="12" class="spinning"/>
        <Check v-else-if="candidateAccepted" :size="12"/>
        <AlertTriangle v-else-if="status.tone==='danger'" :size="12"/>
        <Sparkles v-else :size="12"/>{{ status.label }}
      </span>
    </header>

    <div class="series-concept-inputs">
      <label class="idea-input"><span>故事想法</span><textarea v-model="idea" rows="4" :maxlength="SERIES_CONCEPT_MAX_IDEA_CHARS" placeholder="例如：失忆女剑客每听见一段雨声，就会看见一位陌生人的死亡记忆；她必须在记忆反噬前找到真正的自己。"></textarea><small>{{ idea.length.toLocaleString() }}/{{ SERIES_CONCEPT_MAX_IDEA_CHARS.toLocaleString() }} · 只用于本次候选，不会直接保存</small></label>
      <div class="series-concept-options">
        <label><span>模型通道</span><select v-model="selectedProvider" :disabled="generating || capabilityLoading" @change="providerSelectionEdited=true"><option v-for="provider in capability?.providers || []" :key="provider.id" :value="provider.id" :disabled="!provider.ready && !provider.temporarilyBusy">{{ provider.label }}{{ provider.seriesConceptDraftSuitability==='recommended' ? '（推荐）' : provider.seriesConceptDraftSuitability==='experimental' ? '（实验级）' : '' }}{{ provider.temporarilyBusy ? '（暂忙）' : provider.ready ? '' : '（未就绪）' }}</option></select><small>{{ selectedProvider==='deepseek_api' ? '外部 API，故事想法和风格参考会发往 DeepSeek。' : '本地处理，不向外部模型发送本次文本。' }}</small></label>
        <label><span>计划集数</span><input v-model.number="episodeCount" type="number" min="1"><small>正整数；不设产品数量上限</small></label>
        <label><span>每集镜头</span><input v-model.number="shotsPerEpisode" type="number" min="1"><small>正整数；受单集时长与最短单镜时长约束</small></label>
        <label><span>单集时长</span><input v-model.number="episodeDurationSeconds" type="number" min="1" @input="episodeDurationEdited=true"><small>{{ capability ? `默认 ${capability.defaultEpisodeDurationSeconds} 秒 · 单镜 ${capability.minimumShotDurationMs/1000}–${capability.maximumShotDurationMs/1000} 秒` : '默认 60 秒 · 以服务端能力为准' }}</small></label>
        <button type="button" class="button primary generate-button" :disabled="generating || capabilityLoading || !generationReady" @click="generate"><LoaderCircle v-if="generating" :size="14" class="spinning"/><Sparkles v-else :size="14"/>{{ generating ? '生成中，请勿重复提交…' : result ? '重新生成候选' : '生成故事基础候选' }}</button>
      </div>
      <small v-if="props.styleHint" class="style-source">当前视觉风格会作为生成参考；切换风格后，旧候选会标记为“依据已变化”。</small>
    </div>

    <section class="series-concept-planning" aria-label="生成资源粗略估算">
      <header><span><strong>提交前资源预估</strong><small>前端粗估 · 仅供规划，不是精确耗时、报价或服务端承诺</small></span></header>
      <div>
        <article><small>预计镜头总数</small><strong>{{ planningEstimate.totalShots.toLocaleString() }} 镜</strong><span>{{ episodeCount || 0 }} 集 × {{ shotsPerEpisode || 0 }} 镜；计划总时长 {{ planningEstimate.plannedTotalDurationSeconds.toLocaleString() }} 秒</span></article>
        <article><small>预计生成耗时</small><strong>{{ planningEstimate.estimatedWaitLevel }}</strong><span>仅按镜头数量分档；设备吞吐、API 网络与排队会改变实际等待</span></article>
        <article><small>预计输出规模</small><strong>约 {{ planningEstimate.estimatedOutputTokensLower.toLocaleString() }}–{{ planningEstimate.estimatedOutputTokensUpper.toLocaleString() }} token</strong><span><template v-if="planningEstimate.estimatedOutputHintUtilizationPercent!==undefined">约为当前模型参考值的 {{ planningEstimate.estimatedOutputHintUtilizationPercent }}%；可超过 100%，仅提示资源压力，不会禁用生成；</template>生成后以服务端估算为准</span></article>
        <article><small>本地资源压力</small><strong>{{ selectedProvider==='deepseek_api' ? '低 · 外部 API 通道' : planningEstimate.localResourcePressureLevel }}</strong><span>{{ selectedProvider==='deepseek_api' ? '仅表示本地模型不运行；Django、网络与浏览器仍有开销' : '相对档位；实际由模型、设备、上下文和并发决定' }}</span></article>
        <article><small>DeepSeek 可能费用级别</small><strong>{{ planningEstimate.deepseekCostLevel }}</strong><span>{{ selectedProvider==='deepseek_api' ? '仅按预计输出 token 的相对量级分档；未读取实时价格，不是账单' : '当前选择本地 Qwen，不会提交 DeepSeek API 请求' }}</span></article>
      </div>
    </section>

    <div v-if="providerEgressNotice" class="series-concept-message egress"><AlertTriangle :size="14"/><span><strong>外部 API 数据发送提示</strong><small>{{ providerEgressNotice }}</small></span></div>
    <div v-if="providerSuitabilityNotice" class="series-concept-message warning"><AlertTriangle :size="14"/><span><strong>所选模型为实验级</strong><small>{{ providerSuitabilityNotice }}这只是能力提示，不会限制集数、镜头数或阻止提交。</small></span></div>
    <div v-if="capabilityError" class="series-concept-message danger" role="alert"><AlertTriangle :size="14"/><span><strong>模型能力读取失败</strong><small>{{ capabilityError }}</small></span><button type="button" :disabled="capabilityLoading" @click="loadCapability"><RefreshCw :size="12"/>重试</button></div>
    <div v-else-if="selectedProviderCapability?.temporarilyBusy" class="series-concept-message warning"><Clock3 :size="14"/><span><strong>所选通道暂时繁忙</strong><small>仍可直接尝试生成；若服务端继续返回繁忙，可一键重试，上一版候选和正式表单都不会被修改。</small></span><button type="button" :disabled="capabilityLoading || generating" @click="loadCapability"><RefreshCw :size="12"/>刷新状态</button></div>
    <div v-else-if="capability && !selectedProviderCapability?.ready" class="series-concept-message warning"><AlertTriangle :size="14"/><span><strong>所选通道未就绪</strong><small>{{ selectedProviderCapability?.notice || capability.notice || '请完成模型配置或切换其他已就绪通道。' }}这不会阻止你手工填写和创建项目。</small></span><button type="button" :disabled="capabilityLoading || generating" @click="loadCapability"><RefreshCw :size="12"/>重新检查</button></div>
    <div v-if="inputErrors.length" class="series-concept-message warning"><AlertTriangle :size="14"/><span><strong>请调整本次生成输入</strong><small v-for="item in inputErrors" :key="item">{{ item }}</small></span></div>
    <div v-if="generationError" class="series-concept-message danger" role="alert">
      <AlertTriangle :size="14"/>
      <div class="series-concept-message-copy">
        <strong>{{ candidate ? '本次生成失败，上一版候选仍在' : '候选生成失败' }}</strong>
        <small>{{ generationError }}</small>
        <details v-if="generationDiagnostic" class="series-concept-diagnostic">
          <summary>查看技术详情</summary>
          <small class="diagnostic-note">以下内容已脱敏，不含故事正文、提示词、请求头或密钥。</small>
          <dl class="diagnostic-grid">
            <div><dt>诊断编号</dt><dd>{{ generationDiagnostic.diagnosticId }}</dd></div>
            <div><dt>模型通道</dt><dd>{{ generationDiagnostic.provider==='deepseek_api' ? 'DeepSeek API' : '本地 Qwen' }}</dd></div>
            <div><dt>错误代码</dt><dd>{{ generationDiagnostic.errorCode }}</dd></div>
            <div><dt>失败阶段</dt><dd>{{ diagnosticPhaseLabel(generationDiagnostic.failurePhase) }}（{{ generationDiagnostic.failurePhase }}）</dd></div>
            <div v-if="generationDiagnostic.finishReason"><dt>停止原因</dt><dd>{{ diagnosticFinishReasonLabel(generationDiagnostic.finishReason) }}（{{ generationDiagnostic.finishReason }}）</dd></div>
            <div v-if="generationDiagnostic.usage"><dt>Token 用量</dt><dd>输入 {{ generationDiagnostic.usage.promptTokens ?? '—' }} / 输出 {{ generationDiagnostic.usage.completionTokens ?? '—' }} / 合计 {{ generationDiagnostic.usage.totalTokens ?? '—' }}</dd></div>
            <div v-if="generationDiagnostic.details.attempts!==undefined"><dt>请求次数</dt><dd>{{ generationDiagnostic.details.attempts }}</dd></div>
            <div v-if="generationDiagnostic.details.elapsedMs!==undefined"><dt>耗时</dt><dd>{{ generationDiagnostic.details.elapsedMs.toLocaleString() }} ms</dd></div>
            <div v-if="generationDiagnostic.details.httpStatus!==undefined"><dt>上游 HTTP</dt><dd>{{ generationDiagnostic.details.httpStatus }}</dd></div>
            <div v-if="generationDiagnostic.details.incomplete!==undefined"><dt>输出不完整</dt><dd>{{ generationDiagnostic.details.incomplete ? '是' : '否' }}</dd></div>
            <div v-if="generationDiagnostic.details.requestedMaxTokens!==undefined"><dt>本次输出预算</dt><dd>{{ generationDiagnostic.details.requestedMaxTokens.toLocaleString() }} token</dd></div>
            <div v-if="generationDiagnostic.details.configuredMaxTokens!==undefined"><dt>模型能力配置</dt><dd>{{ generationDiagnostic.details.configuredMaxTokens.toLocaleString() }} token</dd></div>
            <div v-if="generationDiagnostic.details.configuredTimeoutSeconds!==undefined"><dt>超时配置</dt><dd>{{ generationDiagnostic.details.configuredTimeoutSeconds.toLocaleString() }} 秒</dd></div>
            <div v-if="generationDiagnostic.details.validationErrorCode"><dt>校验代码</dt><dd>{{ generationDiagnostic.details.validationErrorCode }}</dd></div>
            <div v-if="generationDiagnostic.details.validationPath"><dt>校验位置</dt><dd>{{ generationDiagnostic.details.validationPath }}</dd></div>
            <div v-if="generationDiagnostic.details.repairAttempted!==undefined"><dt>尝试修复</dt><dd>{{ generationDiagnostic.details.repairAttempted ? '是' : '否' }}</dd></div>
            <div v-if="generationDiagnostic.details.repairSucceeded!==undefined"><dt>修复成功</dt><dd>{{ generationDiagnostic.details.repairSucceeded ? '是' : '否' }}</dd></div>
            <div v-if="generationDiagnostic.details.repairReasonCode"><dt>修复原因</dt><dd>{{ generationDiagnostic.details.repairReasonCode }}</dd></div>
            <div v-if="generationDiagnostic.details.output?.characterCount!==undefined"><dt>输出字符数</dt><dd>{{ generationDiagnostic.details.output.characterCount.toLocaleString() }}</dd></div>
            <div v-if="generationDiagnostic.details.output?.utf8ByteCount!==undefined"><dt>UTF-8 字节数</dt><dd>{{ generationDiagnostic.details.output.utf8ByteCount.toLocaleString() }}</dd></div>
            <div v-if="generationDiagnostic.details.output?.sha256"><dt>输出 SHA-256</dt><dd>{{ generationDiagnostic.details.output.sha256 }}</dd></div>
            <div v-if="generationDiagnostic.details.output?.jsonDecodeLine!==undefined"><dt>JSON 解析位置</dt><dd>第 {{ generationDiagnostic.details.output.jsonDecodeLine }} 行，第 {{ generationDiagnostic.details.output.jsonDecodeColumn ?? '—' }} 列，字符位 {{ generationDiagnostic.details.output.jsonDecodePosition ?? '—' }}</dd></div>
          </dl>
          <template v-if="generationDiagnostic.details.output?.redactedJsonShape">
            <small class="diagnostic-shape-label">脱敏后的 JSON 结构</small>
            <pre class="diagnostic-shape">{{ generationDiagnostic.details.output.redactedJsonShape }}</pre>
          </template>
        </details>
      </div>
      <button type="button" :disabled="generating || !generationReady" @click="generate"><RefreshCw :size="12"/>重试</button>
    </div>
    <div v-if="notice" class="series-concept-message success" role="status"><ShieldCheck :size="14"/><span><strong>{{ candidateAccepted ? '已明确确认填入' : '正式表单仍未修改' }}</strong><small>{{ notice }}</small></span></div>
    <div v-if="sourceChanged" class="series-concept-message warning"><AlertTriangle :size="14"/><span><strong>候选的生成依据已变化</strong><small>故事想法、模型通道、集数、镜头数、单集时长或视觉风格与生成时不同。你仍可人工确认采用，也可以按当前输入重新生成。</small></span></div>
    <div v-if="result?.warnings.length" class="series-concept-message warning"><AlertTriangle :size="14"/><span><strong>人工核对提醒 · 不阻止采用</strong><small v-for="warning in result.warnings" :key="warning">{{ warning }}</small></span></div>

    <section v-if="candidate && result" class="series-concept-candidate">
      <header>
        <span><small>EDITABLE CANDIDATE</small><strong>独立可编辑候选</strong><p>服务端已按兼容模板生成；你在这里的修改不会影响正式表单，直到点击下方确认按钮。</p></span>
        <div class="candidate-meta"><em>{{ result.runtime.provider==='deepseek_api'?'DeepSeek API':'本地 Qwen' }} · {{ result.runtime.model }}</em><em>{{ result.format.episodeCount }} 集 / {{ result.format.shotCount }} 镜 · 单集预算 {{ result.format.episodeDurationSeconds }} 秒</em><em>实际总时长 {{ result.format.totalDurationMs/1000 }} 秒 · {{ returnedShotDurationRange }}</em><em>{{ result.format.templateId }} · {{ generatedAt }}</em></div>
      </header>
      <section class="server-resource-estimate">
        <header><span><strong>服务端本次资源估算</strong><small>来自已受理请求的后端预算，仍不是实际耗时或最终 API 账单</small></span></header>
        <div><span><small>总镜头</small><strong>{{ result.resourceEstimate.totalShots.toLocaleString() }}</strong></span><span><small>估算输入字符</small><strong>{{ result.resourceEstimate.estimatedInputChars.toLocaleString() }}</strong></span><span><small>预计输出 token</small><strong>{{ result.resourceEstimate.estimatedOutputTokens.toLocaleString() }}</strong></span><span><small>当前模型参考值</small><strong>{{ result.resourceEstimate.configuredOutputTokenHint.toLocaleString() }}</strong></span><span><small>参考利用率</small><strong>{{ result.resourceEstimate.outputHintUtilizationPercent.toLocaleString() }}%</strong></span></div>
      </section>
      <label><span>候选项目名称</span><input v-model="candidate.title" maxlength="60" :disabled="generating"><small>{{ candidate.title.length }}/60</small></label>
      <label><span>候选一句话梗概</span><textarea v-model="candidate.logline" rows="3" maxlength="500" :disabled="generating"></textarea><small>{{ candidate.logline.length }}/500</small></label>
      <section class="series-concept-bible-preview" aria-label="模型生成的连续性 Bible 候选">
        <header>
          <span><strong>连续性 Bible 候选</strong><small>已写入完整剧本前置块；创建项目后仍需在 Bible 审核页人工确认，才会进入正式角色、场景和道具库。</small></span>
          <em>{{ bibleEntityCount.toLocaleString() }} 项</em>
        </header>
        <div>
          <article v-for="section in bibleSections" :key="section.key">
            <h4>{{ section.label }} <small>{{ section.items.length }}</small></h4>
            <ul v-if="section.items.length">
              <li v-for="item in section.items.slice(0, 4)" :key="`${section.key}:${item.name}`">
                <strong>{{ item.name }}</strong>
                <span>{{ item.description || '—' }}</span>
                <code>{{ item.visualIdentityEn || 'visual anchor pending' }}</code>
              </li>
            </ul>
            <p v-else>本次候选未提取{{ section.label }}；可在创建后手工补充。</p>
            <small v-if="section.items.length > 4">还有 {{ (section.items.length - 4).toLocaleString() }} 项会随完整剧本进入审核流程。</small>
          </article>
        </div>
      </section>
      <label><span>候选完整剧本</span><textarea v-model="candidate.masterScript" rows="14" :disabled="generating" class="master-script"></textarea><small>{{ candidate.masterScript.length.toLocaleString() }} 字符 · 不会静默截断；生成时已通过 {{ result.format.templateId }} 兼容校验</small></label>
      <div v-if="candidateEdited" class="candidate-edited"><Clock3 :size="13"/><span>候选已被人工编辑。兼容标记对应服务端生成原稿；请重点核对你对剧本格式的修改。</span></div>
      <div v-if="candidateErrors.length" class="candidate-errors"><AlertTriangle :size="13"/><span><small v-for="item in candidateErrors" :key="item">{{ item }}</small></span></div>
      <footer><span>确认只会一次性填入上方三个正式字段，不会保存或创建项目。</span><div><button type="button" class="button ghost" :disabled="generating" @click="discard"><Trash2 :size="13"/>放弃候选</button><button type="button" class="button primary" :disabled="!canAccept" @click="accept"><Check :size="14"/>确认并填入故事基础</button></div></footer>
    </section>
  </section>
</template>

<style scoped>
.series-concept-workbench{margin:14px 0 18px;overflow:hidden;border:1px solid rgba(111,88,177,.3);border-radius:10px;background:linear-gradient(135deg,rgba(94,65,153,.08),rgba(9,13,17,.94) 42%)}
.series-concept-workbench>header{padding:13px 14px;display:flex;align-items:flex-start;justify-content:space-between;gap:14px;border-bottom:1px solid rgba(111,88,177,.2)}
.series-concept-title{display:flex;align-items:flex-start;gap:9px}.series-concept-title>span:last-child{display:flex;flex-direction:column;gap:3px}.series-concept-icon{width:30px;height:30px;display:flex;align-items:center;justify-content:center;flex:0 0 auto;border-radius:8px;background:rgba(133,94,211,.13);color:#ad86ef}.series-concept-title small,.series-concept-candidate>header small{color:#9b75d5;font-size:7px;letter-spacing:.14em}.series-concept-title strong,.series-concept-candidate>header strong{color:#d2d7db;font-size:11px}.series-concept-title p,.series-concept-candidate>header p{margin:0;color:#747d85;font-size:8px;line-height:1.5}
.series-concept-status{min-height:24px;padding:0 8px;display:flex;align-items:center;gap:5px;flex:0 0 auto;border:1px solid #303740;border-radius:999px;color:#858e95;font-size:8px}.series-concept-status.ready,.series-concept-status.success{border-color:rgba(88,194,159,.35);color:#69c8a8}.series-concept-status.working,.series-concept-status.warning{border-color:rgba(218,154,70,.36);color:#dda350}.series-concept-status.danger{border-color:rgba(233,107,119,.36);color:#e27e89}
.series-concept-inputs{padding:12px 14px 10px;display:flex;flex-direction:column;gap:9px}.series-concept-inputs label{display:flex;min-width:0;flex-direction:column;gap:5px}.series-concept-inputs label>span,.series-concept-candidate label>span{color:#919aa1;font-size:9px}.series-concept-inputs textarea,.series-concept-inputs input,.series-concept-inputs select,.series-concept-candidate textarea,.series-concept-candidate input{width:100%;box-sizing:border-box;border:1px solid #2a3239;border-radius:7px;background:#080c10;color:#cbd0d4;font:9px/1.55 inherit}.series-concept-inputs textarea,.series-concept-candidate textarea{padding:8px 9px;resize:vertical}.series-concept-inputs input,.series-concept-inputs select,.series-concept-candidate input{height:33px;padding:0 9px}.series-concept-inputs small,.series-concept-candidate label>small{color:#646e75;font-size:7px;line-height:1.45}.series-concept-options{display:grid;grid-template-columns:minmax(165px,1.35fr) repeat(3,minmax(90px,.65fr)) auto;align-items:end;gap:8px}.series-concept-options .generate-button{height:33px;white-space:nowrap}.series-concept-inputs .style-source{color:#76828a}
.series-concept-planning{margin:0 14px 10px;padding:11px;border:1px solid rgba(83,155,196,.22);border-radius:8px;background:rgba(83,155,196,.035)}.series-concept-planning>header{margin-bottom:8px}.series-concept-planning>header span{display:flex;align-items:baseline;justify-content:space-between;gap:10px}.series-concept-planning>header strong{color:#91bdd4;font-size:9px}.series-concept-planning>header small{color:#718894;font-size:7px}.series-concept-planning>div{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:7px}.series-concept-planning article{padding:8px;display:flex;min-width:0;flex-direction:column;gap:4px;border:1px solid rgba(83,155,196,.16);border-radius:7px;background:#0a1014}.series-concept-planning article small{color:#708693;font-size:7px}.series-concept-planning article strong{color:#a9c5d3;font-size:9px;line-height:1.4}.series-concept-planning article span{color:#65747d;font-size:7px;line-height:1.45}
.series-concept-message{margin:0 14px 9px;padding:9px 10px;display:flex;align-items:flex-start;gap:8px;border:1px solid rgba(90,195,160,.24);border-radius:7px;background:rgba(90,195,160,.045);color:#65c4a4}.series-concept-message>span,.series-concept-message-copy{display:flex;min-width:0;flex:1;flex-direction:column;gap:3px}.series-concept-message strong{font-size:9px}.series-concept-message small{font-size:8px;line-height:1.5}.series-concept-message.warning,.series-concept-message.egress{border-color:rgba(218,154,70,.28);background:rgba(218,154,70,.045);color:#d09a51}.series-concept-message.egress{border-color:rgba(83,155,196,.25);background:rgba(83,155,196,.04);color:#7fb4d0}.series-concept-message.danger{border-color:rgba(234,105,119,.28);background:rgba(234,105,119,.05);color:#df7b86}.series-concept-message button{display:flex;align-items:center;gap:4px;border:0;background:transparent;color:inherit;font-size:8px;cursor:pointer}.series-concept-message button:disabled{opacity:.45;cursor:not-allowed}
.series-concept-diagnostic{margin-top:4px;padding-top:5px;border-top:1px solid rgba(234,105,119,.18);color:#b7a1a5}.series-concept-diagnostic summary{width:max-content;cursor:pointer;color:#e19aa2;font-size:8px;font-weight:600}.series-concept-diagnostic .diagnostic-note{display:block;margin-top:5px;color:#8f7e82}.diagnostic-grid{margin:7px 0 0;display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 9px}.diagnostic-grid>div{display:contents}.diagnostic-grid dt{color:#927f83;font-size:7px}.diagnostic-grid dd{min-width:0;margin:0;color:#c7b7ba;font:7px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}.diagnostic-shape-label{display:block;margin-top:7px;color:#927f83}.diagnostic-shape{max-height:180px;margin:4px 0 0;padding:7px;overflow:auto;border:1px solid rgba(234,105,119,.16);border-radius:5px;background:#080b0e;color:#bcaeb1;font:7px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.series-concept-candidate{margin:3px 14px 14px;padding:12px;display:flex;flex-direction:column;gap:10px;border:1px solid #28313a;border-radius:9px;background:#0b1014}.series-concept-candidate>header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.series-concept-candidate>header>span{display:flex;min-width:0;flex-direction:column;gap:3px}.candidate-meta{display:flex;align-items:flex-end;flex-direction:column;gap:3px}.candidate-meta em{color:#78838b;font-size:7px;font-style:normal}.server-resource-estimate{padding:9px;border:1px solid rgba(90,195,160,.2);border-radius:7px;background:rgba(90,195,160,.03)}.server-resource-estimate>header span{display:flex;align-items:baseline;justify-content:space-between;gap:10px}.server-resource-estimate>header strong{color:#79c9ad;font-size:8px}.server-resource-estimate>header small{color:#67857b;font-size:7px}.server-resource-estimate>div{margin-top:7px;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px}.server-resource-estimate>div>span{display:flex;min-width:0;flex-direction:column;gap:3px}.server-resource-estimate>div small{color:#6e817a;font-size:7px}.server-resource-estimate>div strong{color:#9cc8b9;font-size:9px}.series-concept-bible-preview{padding:9px;border:1px solid rgba(133,94,211,.2);border-radius:7px;background:rgba(133,94,211,.035)}.series-concept-bible-preview>header{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.series-concept-bible-preview>header span{display:flex;min-width:0;flex-direction:column;gap:3px}.series-concept-bible-preview>header strong{color:#bba0e4;font-size:8px}.series-concept-bible-preview>header small{color:#7e748e;font-size:7px;line-height:1.45}.series-concept-bible-preview>header em{color:#bba0e4;font-size:8px;font-style:normal}.series-concept-bible-preview>div{margin-top:8px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}.series-concept-bible-preview article{min-width:0;padding:7px;border:1px solid rgba(133,94,211,.16);border-radius:6px;background:#090d12}.series-concept-bible-preview h4{margin:0 0 6px;color:#c5b4df;font-size:8px}.series-concept-bible-preview h4 small{color:#8f7fb0}.series-concept-bible-preview ul{margin:0;padding:0;display:flex;flex-direction:column;gap:6px;list-style:none}.series-concept-bible-preview li{display:flex;min-width:0;flex-direction:column;gap:2px}.series-concept-bible-preview li strong{color:#cfd5da;font-size:8px}.series-concept-bible-preview li span{color:#7d878e;font-size:7px;line-height:1.4}.series-concept-bible-preview li code{color:#8f80b2;font:7px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.series-concept-bible-preview article>p{margin:0;color:#65707a;font-size:7px}.series-concept-bible-preview article>small{display:block;margin-top:6px;color:#7d748b;font-size:7px}.series-concept-candidate label{display:flex;flex-direction:column;gap:5px}.series-concept-candidate .master-script{min-height:250px;white-space:pre-wrap}.series-concept-candidate textarea:disabled,.series-concept-candidate input:disabled{opacity:.65}.candidate-edited,.candidate-errors{padding:8px 9px;display:flex;align-items:flex-start;gap:7px;border:1px solid rgba(218,154,70,.22);border-radius:7px;color:#c49350;font-size:8px;line-height:1.5}.candidate-errors{border-color:rgba(234,105,119,.25);color:#dc7782}.candidate-errors>span{display:flex;flex-direction:column;gap:2px}.candidate-errors small{font-size:8px}.series-concept-candidate>footer{padding-top:10px;display:flex;align-items:center;justify-content:space-between;gap:12px;border-top:1px solid #252d34;color:#6d767d;font-size:8px}.series-concept-candidate>footer>div{display:flex;gap:7px}.series-concept-candidate .button,.series-concept-options .button{display:inline-flex;align-items:center;justify-content:center;gap:5px}
.spinning{animation:series-concept-spin .85s linear infinite}@keyframes series-concept-spin{to{transform:rotate(360deg)}}
@media(max-width:1150px){.series-concept-planning>div{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:900px){.series-concept-workbench>header,.series-concept-candidate>header,.series-concept-candidate>footer{align-items:stretch;flex-direction:column}.series-concept-status{width:max-content}.candidate-meta{align-items:flex-start}.series-concept-options{grid-template-columns:repeat(2,minmax(0,1fr))}.series-concept-options .generate-button{width:100%}.series-concept-planning>header span,.server-resource-estimate>header span{align-items:flex-start;flex-direction:column}.server-resource-estimate>div,.series-concept-bible-preview>div{grid-template-columns:repeat(3,minmax(0,1fr))}.series-concept-candidate>footer>div{justify-content:flex-end}}
@media(max-width:560px){.series-concept-options,.series-concept-planning>div,.server-resource-estimate>div,.series-concept-bible-preview>div{grid-template-columns:1fr}.series-concept-candidate>footer>div{flex-direction:column}.series-concept-candidate>footer .button{width:100%}}
</style>
