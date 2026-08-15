<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { AlertTriangle, Check, LoaderCircle, RefreshCw, Sparkles } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import ShotDetailDraftComparison from '@/components/ShotDetailDraftComparison.vue'
import { shotDetailDraftRepository } from '@/services/shotDetailDraftRepository'
import { promptProvider, REMOTE_PROMPT_EGRESS_NOTICE } from '@/services/providerCapabilities'
import type {
  CreativeGenerationCapability, Episode, PromptProviderId, Shot,
  ShotDetailDraftBatch, ShotDetailDraftBatchApplyResult, ShotDetailDraftBatchDetail,
  ShotDetailDraftLanguageMode,
} from '@/types'

const props = defineProps<{
  open: boolean
  projectId: string
  episode: Episode
  shots: Shot[]
  initialMode: 'expand' | 'apply'
  blockedReason?: string
  persistEpisode: () => Promise<unknown>
}>()
const emit = defineEmits<{
  close: []
  applied: [result: ShotDetailDraftBatchApplyResult, languageMode: ShotDetailDraftLanguageMode]
}>()

const capability = ref<CreativeGenerationCapability | null>(null)
const provider = ref<PromptProviderId>('local_qwen')
const variationNote = ref('')
const detail = ref<ShotDetailDraftBatchDetail | null>(null)
const loading = ref(false)
const creating = ref(false)
const applying = ref(false)
const error = ref('')
const notice = ref('')
const confirmation = ref<Record<string, unknown> | null>(null)
const pendingRequestId = ref('')
const languageMode = ref<ShotDetailDraftLanguageMode>('chinese_only')
const englishReviewAcknowledged = ref(false)
const reviewAcknowledged = ref(false)
let pollTimer: number | undefined
let requestEpoch = 0
let reconciledAppliedBatchId = ''

const selectedProvider = computed(() => promptProvider(capability.value?.providers, provider.value))
const providerBusy = computed(() => selectedProvider.value?.temporarilyBusy === true)
const providerUsable = computed(() => selectedProvider.value?.ready === true || providerBusy.value)
const batch = computed(() => detail.value?.batch || null)
const isActive = computed(() => ['queued', 'running'].includes(batch.value?.generationStatus || ''))
const eligibleItems = computed(() => detail.value?.items.filter((item) => item.status !== 'skipped') || [])
const canApply = computed(() => Boolean(
  batch.value && batch.value.applyStatus !== 'complete' && eligibleItems.value.length
  && eligibleItems.value.every((item) => item.status === 'succeeded' && item.draft),
))
const reviewIssueCount = computed(() => eligibleItems.value.reduce(
  (sum, item) => sum + (item.draft?.reviewSummary.issueCount || 0), 0,
))
const requiresEditCount = computed(() => eligibleItems.value.reduce(
  (sum, item) => sum + (item.draft?.reviewSummary.requiresEditCount || 0), 0,
))
const progress = computed(() => {
  const current = batch.value
  if (!current?.totalCount) return 0
  return Math.round(((current.succeededCount + current.failedCount + current.staleCount + current.skippedCount) / current.totalCount) * 100)
})
const providerEgressNotice = computed(() => provider.value === 'deepseek_api'
  ? selectedProvider.value?.dataEgressNotice || REMOTE_PROMPT_EGRESS_NOTICE : '')

const errorBody = (cause: unknown): Record<string, any> => {
  const row = cause && typeof cause === 'object' ? cause as Record<string, any> : {}
  return row.response?.data && typeof row.response.data === 'object' ? row.response.data : {}
}
const errorMessage = (cause: unknown, fallback: string) => {
  const body = errorBody(cause)
  const message = String(body.detail || (cause instanceof Error ? cause.message : '') || '').trim()
  return message || fallback
}
const uuid = () => {
  if (typeof crypto?.randomUUID === 'function') return crypto.randomUUID()
  throw new Error('当前浏览器不支持创建安全请求标识，请升级浏览器后重试。')
}
const shotFor = (shotId: string) => props.shots.find((shot) => shot.id === shotId)

const loadCapability = async (epoch = requestEpoch) => {
  const next = await shotDetailDraftRepository.capability()
  if (epoch !== requestEpoch || !props.open) return
  capability.value = next
  if (!capability.value.providers?.some((item) => item.id === provider.value)) {
    provider.value = capability.value.defaultProvider || 'local_qwen'
  }
}

const loadBatch = async (batchId: string, epoch = requestEpoch) => {
  const next = await shotDetailDraftRepository.batchDetailAll(batchId)
  if (epoch !== requestEpoch || !props.open) return
  detail.value = next
  provider.value = next.batch.provider
  if (
    next.batch.applyStatus === 'complete'
    && next.appliedShots.length
    && reconciledAppliedBatchId !== next.batch.id
  ) {
    reconciledAppliedBatchId = next.batch.id
    emit('applied', {
      batch: next.batch,
      items: next.items,
      appliedShots: next.appliedShots,
      translationWritten: next.translationWritten,
    }, next.translationWritten ? 'bilingual_confirmed' : 'chinese_only')
  }
}

const schedulePoll = () => {
  window.clearTimeout(pollTimer)
  if (!props.open || !isActive.value || !batch.value) return
  pollTimer = window.setTimeout(async () => {
    const epoch = requestEpoch
    try { await loadBatch(batch.value!.id, epoch) }
    catch (cause) { if (epoch === requestEpoch) error.value = errorMessage(cause, '刷新整集扩写进度失败。') }
    finally { if (epoch === requestEpoch) schedulePoll() }
  }, 2500)
}

const restoreLatest = async (epoch = requestEpoch) => {
  const rows = await shotDetailDraftRepository.listBatches(props.projectId, props.episode.id)
  if (epoch !== requestEpoch || !props.open) return
  if (rows[0]) await loadBatch(rows[0].id, epoch)
  else detail.value = null
}

const refresh = async () => {
  if (!batch.value) return restoreLatest()
  loading.value = true; error.value = ''
  try { await loadBatch(batch.value.id) }
  catch (cause) { error.value = errorMessage(cause, '刷新整集扩写状态失败。') }
  finally { loading.value = false; schedulePoll() }
}

const create = async (acknowledged = false) => {
  if (props.blockedReason) { error.value = props.blockedReason; return }
  creating.value = true; error.value = ''; notice.value = ''
  try {
    await loadCapability()
    if (!providerUsable.value) throw new Error(selectedProvider.value?.notice || '所选扩写通道尚未就绪。')
    await props.persistEpisode()
    pendingRequestId.value ||= uuid()
    const created = await shotDetailDraftRepository.createBatch(props.projectId, {
      episodeId: props.episode.id,
      requestId: pendingRequestId.value,
      provider: provider.value,
      variationNote: variationNote.value.trim(),
      confirmationAcknowledged: acknowledged,
    })
    confirmation.value = null
    await loadBatch(created.id)
    notice.value = providerBusy.value
      ? '资源暂时占用，批次已入队，服务可用后会自动重试。'
      : `已创建 ${created.totalCount} 镜整集扩写批次；通道已锁定为 ${selectedProvider.value?.label || created.provider}。`
    schedulePoll()
  } catch (cause) {
    const body = errorBody(cause)
    if (body.code === 'shot_detail_batch_confirmation_required') {
      confirmation.value = body.confirmation || {}
      error.value = ''
    } else {
      error.value = errorMessage(cause, '创建整集扩写批次失败；没有修改任何正式镜头。')
      // Preserve the idempotency key when the response is unknown. Retrying
      // the same request can safely recover an already-created batch.
    }
  } finally { creating.value = false }
}

const prepareNewBatch = () => {
  if (isActive.value) return
  detail.value = null
  confirmation.value = null
  pendingRequestId.value = ''
  error.value = ''
  notice.value = ''
  variationNote.value = ''
  englishReviewAcknowledged.value = false
  reviewAcknowledged.value = false
}

const applyBatch = async () => {
  if (!batch.value || !canApply.value) return
  if (requiresEditCount.value) {
    error.value = `当前候选仍有 ${requiresEditCount.value} 项必须修正的关联问题，后端会原子阻止整集应用。`
    return
  }
  if (reviewIssueCount.value && !reviewAcknowledged.value) {
    error.value = '请先阅读并确认整集候选的人工核验项。'
    return
  }
  if (languageMode.value === 'bilingual_confirmed' && !englishReviewAcknowledged.value) {
    error.value = '采用已审中英前，请确认已逐镜核对英文。'
    return
  }
  applying.value = true; error.value = ''; notice.value = ''
  try {
    const result = await shotDetailDraftRepository.applyBatch(batch.value.id, {
      expectedBatchRevision: batch.value.revision,
      items: eligibleItems.value.map((item) => ({
        itemId: item.id, draftId: item.draft!.id, expectedRevision: item.draft!.revision,
      })),
      languageMode: languageMode.value,
      englishReviewAcknowledged: languageMode.value === 'bilingual_confirmed',
      reviewAcknowledged: reviewIssueCount.value ? reviewAcknowledged.value : false,
    })
    detail.value = {
      batch: result.batch,
      items: result.items,
      pagination: { count: result.items.length },
      appliedShots: result.appliedShots,
      translationWritten: result.translationWritten,
    }
    reconciledAppliedBatchId = result.batch.id
    emit('applied', result, languageMode.value)
    notice.value = languageMode.value === 'bilingual_confirmed'
      ? `已原子应用 ${result.batch.appliedCount} 个视频镜头的已审英文扩写。`
      : `已原子应用 ${result.batch.appliedCount} 个视频镜头的中文扩写；请继续完成翻译审核。`
  } catch (cause) {
    error.value = errorMessage(cause, '整集应用失败；事务未写入任何正式镜头。')
    await refresh()
    if (batch.value?.applyStatus === 'complete' && detail.value?.appliedShots.length) {
      error.value = ''
      notice.value = '应用响应中断，但已从服务端恢复正式镜头结果；相关提示词已失效。'
    }
  } finally { applying.value = false }
}

const hydrateModal = async () => {
  const epoch = ++requestEpoch
  loading.value = true; error.value = ''; notice.value = ''; confirmation.value = null
  pendingRequestId.value = ''; languageMode.value = 'chinese_only'
  englishReviewAcknowledged.value = false; reviewAcknowledged.value = false
  try { await Promise.all([loadCapability(epoch), restoreLatest(epoch)]) }
  catch (cause) { if (epoch === requestEpoch) error.value = errorMessage(cause, '无法读取整集扩写状态。') }
  finally {
    if (epoch === requestEpoch) {
      loading.value = false
      schedulePoll()
    }
  }
}

watch(() => props.open, (value) => {
  if (value) void hydrateModal()
  else { requestEpoch += 1; window.clearTimeout(pollTimer) }
})
watch(() => [props.projectId, props.episode.id], () => {
  if (props.open) void hydrateModal()
})
watch(() => batch.value?.generationStatus, schedulePoll)
onBeforeUnmount(() => window.clearTimeout(pollTimer))
</script>

<template>
  <BaseModal :open="props.open" :title="initialMode==='apply' ? '整集应用高细节扩写' : '整集高细节扩写'" eyebrow="EPISODE HIGH-DETAIL DRAFT" width="1120px" @close="emit('close')">
    <div class="episode-detail-modal" :aria-busy="loading||creating||applying">
      <p class="scope-note">仅视频镜头进入扩写队列；图片镜头会明确标记为“已跳过”。批次创建时冻结扩写前内容，应用时必须一次提交全部视频候选，任一镜头失败都不会部分写入。</p>
      <div v-if="error" class="modal-alert error" role="alert"><AlertTriangle :size="15"/><span>{{ error }}</span></div>
      <div v-if="notice" class="modal-alert success" role="status"><Check :size="15"/><span>{{ notice }}</span></div>
      <div v-if="confirmation" class="confirmation" role="alertdialog" aria-labelledby="detail-confirm-title">
        <div><strong id="detail-confirm-title">镜头数超过当前通道的推荐单批数量</strong><small>这不是整剧能力限制。继续后会保留全部镜头，只会增加预算与等待时间。</small><code>{{ confirmation.video_shot_count || 0 }} 个视频镜头 / 建议单批 {{ confirmation.recommended_max_shots || '未提供' }} 个</code></div>
        <button class="button primary small" :disabled="creating" @click="create(true)">确认预算与时间，继续整集扩写</button>
      </div>

      <section v-if="batch||initialMode==='expand'" class="batch-controls">
        <label><span>处理通道</span><select v-model="provider" :disabled="Boolean(batch)||creating" @focus="() => loadCapability()" @change="() => loadCapability()"><option v-for="item in capability?.providers || []" :key="item.id" :value="item.id" :disabled="!item.ready&&!item.temporarilyBusy">{{ item.label }}{{ item.temporarilyBusy ? '（资源占用，可排队）' : item.ready ? '' : '（未就绪）' }}</option></select></label>
        <label class="variation"><span>整集补充要求（可选）</span><input v-model="variationNote" :disabled="Boolean(batch)||creating" placeholder="例如：加强雨夜材质与动作节奏，但不新增角色"></label>
        <button v-if="!batch" class="button primary" :disabled="creating||loading||!providerUsable||Boolean(blockedReason)" :title="blockedReason" @click="create(false)"><LoaderCircle v-if="creating" class="spin" :size="14"/><Sparkles v-else :size="14"/>{{ creating ? '正在保存并创建批次…' : '保存整集并开始扩写' }}</button>
        <template v-else>
          <button class="button ghost" :disabled="loading" @click="refresh"><RefreshCw :class="{spin:loading}" :size="14"/>刷新状态</button>
          <button v-if="!isActive" class="button ghost" :disabled="creating" @click="prepareNewBatch"><Sparkles :size="14"/>准备新批次</button>
        </template>
      </section>
      <p v-if="initialMode==='expand'&&providerEgressNotice&&!batch" class="egress"><AlertTriangle :size="13"/><span><strong>外部 API 数据发送提示：</strong>{{ providerEgressNotice }}</span></p>
      <p v-if="initialMode==='expand'&&providerBusy&&!batch" class="busy-note">资源暂时占用，提交后会入队并自动重试。</p>
      <p v-if="initialMode==='expand'&&capability&&!providerUsable&&!batch" class="unready" role="alert">{{ selectedProvider?.notice || '所选扩写通道尚未永久配置完成。' }}</p>

      <template v-if="batch&&detail">
        <section class="batch-summary">
          <div><strong>{{ batch.provider==='deepseek_api' ? 'DeepSeek API' : '本地 Qwen' }}</strong><small>批次 {{ batch.id.slice(0,8) }} · 通道在创建后锁定</small></div>
          <div class="progress-copy"><b>{{ progress }}%</b><span>{{ batch.succeededCount }} 成功 · {{ batch.pendingCount }} 待处理 · {{ batch.failedCount }} 失败 · {{ batch.staleCount }} 已变化 · {{ batch.skippedCount }} 跳过</span></div>
          <div class="progress" role="progressbar" aria-label="整集扩写进度" :aria-valuenow="progress" aria-valuemin="0" aria-valuemax="100"><i :style="{width:`${progress}%`}"></i></div>
        </section>
        <p v-if="batch.failedCount||batch.staleCount" class="retry-note"><AlertTriangle :size="13"/><span>当前后端没有逐镜重试接口，失败项不会伪装成成功。可查看原因后准备新批次，重新选择通道并冻结全剧集。</span><button class="button ghost small" :disabled="isActive||creating" @click="prepareNewBatch">准备新批次</button></p>
        <div class="comparison-list">
          <ShotDetailDraftComparison v-for="item in detail.items" :key="item.id" :item="item" :shot="shotFor(item.shotId)"/>
        </div>

        <section class="apply-zone">
          <header><div><strong>原子应用整集</strong><small>所有非跳过视频镜头必须都有成功候选；提交完整 item / draft / revision 集合，后端一次事务写入。</small></div><b>{{ eligibleItems.length }} 个视频镜头</b></header>
          <p v-if="batch.applyStatus==='complete'" class="applied"><Check :size="14"/>本批次已完成整集应用。</p>
          <p v-else-if="!canApply" class="apply-blocked"><AlertTriangle :size="14"/>整集候选尚未全部成功，暂不能应用；已生成内容仍可逐镜查看。</p>
          <div class="language-options">
            <label><input v-model="languageMode" type="radio" value="chinese_only" :disabled="!canApply||applying"><span><strong>采用中文正式字段</strong><small>应用后进入现有翻译审核，再确认英文。</small></span></label>
            <label><input v-model="languageMode" type="radio" value="bilingual_confirmed" :disabled="!canApply||applying"><span><strong>采用已审中英</strong><small>仅在逐镜核对英文后选择。</small></span></label>
          </div>
          <label v-if="reviewIssueCount" class="ack"><input v-model="reviewAcknowledged" type="checkbox" :disabled="!canApply||applying"><span>我已逐镜阅读 {{ reviewIssueCount }} 项人工核验提示；其中 {{ requiresEditCount }} 项需修正后才能应用。</span></label>
          <label v-if="languageMode==='bilingual_confirmed'" class="ack"><input v-model="englishReviewAcknowledged" type="checkbox" :disabled="!canApply||applying"><span>我已逐镜核对英文中的人物、方位、动作、运镜、时间顺序、道具与结尾状态。</span></label>
          <button class="button primary apply-button" :disabled="!canApply||applying||Boolean(requiresEditCount)||(Boolean(reviewIssueCount)&&!reviewAcknowledged)||(languageMode==='bilingual_confirmed'&&!englishReviewAcknowledged)" @click="applyBatch"><LoaderCircle v-if="applying" class="spin" :size="14"/><Check v-else :size="14"/>{{ applying ? '正在原子应用整集…' : `应用全部 ${eligibleItems.length} 个视频镜头` }}</button>
        </section>
      </template>
      <div v-else-if="loading" class="loading"><LoaderCircle class="spin" :size="16"/>读取整集扩写状态…</div>
      <div v-else-if="initialMode==='apply'" class="empty"><strong>当前剧集还没有整集扩写批次</strong><p>请从“整集扩写”创建批次，候选全部完成后再原子应用。</p></div>
    </div>
  </BaseModal>
</template>

<style scoped>
.episode-detail-modal{display:grid;gap:12px}.scope-note{margin:0;color:#7e8992;font-size:10px;line-height:1.55}.modal-alert,.confirmation,.egress,.retry-note{display:flex;align-items:flex-start;gap:8px;padding:9px;border:1px solid rgba(218,158,73,.25);border-radius:6px;background:rgba(218,158,73,.06);color:#d0a565;font-size:10px;line-height:1.5}.modal-alert.success{border-color:rgba(96,198,164,.24);background:rgba(96,198,164,.06);color:#7dceb1}.modal-alert.error{border-color:rgba(223,101,90,.26);background:rgba(223,101,90,.06);color:#df8177}.confirmation{align-items:center;justify-content:space-between}.confirmation>div{display:grid;gap:3px}.confirmation small{color:#9a8467}.confirmation code{font-size:9px}.batch-controls{display:flex;align-items:end;gap:8px}.batch-controls label{display:grid;gap:4px}.batch-controls label>span{color:#7c8790;font-size:9px}.batch-controls select,.batch-controls input{min-height:34px;box-sizing:border-box;padding:7px 8px;border:1px solid #29343e;border-radius:5px;background:#080c11;color:#c3cdd5}.batch-controls .variation{flex:1}.batch-controls .variation input{width:100%}.busy-note,.unready{margin:0;color:#7fb8dc;font-size:9px}.unready{color:#df8177}.batch-summary{display:grid;grid-template-columns:auto 1fr;gap:7px 16px;padding:10px;border:1px solid #29333d;border-radius:7px;background:#0a0f14}.batch-summary>div:first-child{display:grid;gap:2px}.batch-summary small{color:#6f7b84;font-size:8px}.progress-copy{display:flex;align-items:center;justify-content:flex-end;gap:8px;color:#82909a;font-size:9px}.progress-copy b{color:#77c9ad;font-size:13px}.progress{grid-column:1/-1;height:5px;overflow:hidden;border-radius:3px;background:#1c252d}.progress i{display:block;height:100%;background:#61bfa0;transition:width .2s}.retry-note{align-items:center}.retry-note span{flex:1}.comparison-list{display:grid;gap:8px;max-height:52vh;overflow:auto;padding-right:3px}.apply-zone{display:grid;gap:9px;padding:11px;border:1px solid rgba(128,104,216,.28);border-radius:7px;background:rgba(128,104,216,.055)}.apply-zone>header{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.apply-zone>header div{display:grid;gap:3px}.apply-zone>header strong{font-size:11px}.apply-zone>header small{color:#7d8791;font-size:8px}.apply-zone>header>b{color:#ad9ade;font-size:9px}.language-options{display:grid;grid-template-columns:1fr 1fr;gap:8px}.language-options label,.ack{display:flex;align-items:flex-start;gap:7px;padding:8px;border:1px solid #2a333c;border-radius:6px;background:#090d12;color:#9ba5ae;font-size:9px}.language-options span{display:grid;gap:2px}.language-options small{color:#6f7982;font-size:8px}.ack{border-color:rgba(218,158,73,.22);background:rgba(218,158,73,.04);color:#c7a166}.apply-button{justify-self:end}.applied,.apply-blocked{display:flex;align-items:center;gap:6px;margin:0;color:#77c9ad;font-size:9px}.apply-blocked{color:#d4a05c}.loading,.empty{display:flex;align-items:center;justify-content:center;gap:7px;min-height:100px;color:#7b8790;font-size:10px}.empty{flex-direction:column}.empty p{margin:0}.spin{animation:episode-detail-spin 1s linear infinite}@keyframes episode-detail-spin{to{transform:rotate(360deg)}}@media(max-width:720px){.batch-controls,.confirmation,.retry-note{align-items:stretch;flex-direction:column}.batch-controls>*{width:100%}.batch-summary{grid-template-columns:1fr}.progress-copy{justify-content:flex-start;flex-wrap:wrap}.progress{grid-column:1}.language-options{grid-template-columns:1fr}.apply-button{width:100%}.comparison-list{max-height:none}}
</style>
