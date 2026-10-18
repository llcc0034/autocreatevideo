<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Asset } from '@/types'
import type { ProductionRun } from '@/services/platformTypes'
import type { ManualActionOptions, ManualTargetPage } from '@/services/productionManual'
import { platformApi } from '@/services/platformApi'
import { repository } from '@/services/repository'
import { assetReferenceApi } from '@/services/assetReferenceApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { createManualShotReferenceEditor, selectShotReference, selectRecipeUpstreamReference, recipeReferenceCandidateReason,
  readSavedShotReference, shotReferenceReplacementReason, type ReferenceIdentity, type RecipeReferenceCandidate } from '@/services/productionManualShotReferences'
import {createShotReferenceBatch} from '@/services/shotReferenceBatch'
import ShotReferenceBatchPreview from './ShotReferenceBatchPreview.vue'
import BaseModal from './BaseModal.vue'
import ExecutionInputAssetPicker from './ExecutionInputAssetPicker.vue'
import AssetMediaPreview from './AssetMediaPreview.vue'

const props = defineProps<{open: boolean; run: ProductionRun; options?: ManualActionOptions}>()
const emit = defineEmits<{close: []; changed: []; busy: [boolean]; retained: [{dirty: boolean; pending: boolean}]}>()
const selectionApi = {read: (runId: string, shotId: string) => props.run.planSource === 'recipe'
    ? platformApi.getRecipeShotReferences(runId, shotId) : platformApi.getManualShotReferences(runId, shotId), submit: platformApi.performManualProductionAction},
  selectionChanged = () => {state.stale = true; emit('changed')}
const editor = createManualShotReferenceEditor(selectionApi, () => props.run, () => props.options, () => emit('changed'))
const {state} = editor
const batch = createShotReferenceBatch(selectionApi, () => props.run, () => props.options, selectionChanged)
const targets = ref<ManualTargetPage|null>(null), page = ref(1), targetsLoading = ref(false), targetsError = ref('')
const useId = ref(''), shotLabel = ref(''), pickerBusy = ref(false), selectedError = ref('')
const preview = ref<{asset: Asset; label: string}|null>(null), previewError = ref(''), previewLoading = ref(false)
const use = computed(() => state.detail?.state.uses.find(row => row.id === useId.value))
const recipeDetail = computed(() => state.detail && 'selection_rule' in state.detail ? state.detail : null)
const sourceRule = computed(() => recipeDetail.value?.selection_rule.rule)
const replacementReason = computed(() => state.detail && use.value ? shotReferenceReplacementReason(state.detail, use.value) : '')
const busy = computed(() => state.submitting || pickerBusy.value || batch.state.loading || batch.state.submitting)
const pending = computed(() => !!state.pending || batch.pending())
const locked = computed(() => busy.value || state.loading || pending.value)
const terminal = computed(() => ['succeeded','failed','cancelled'].includes(props.run.status))
const editable = computed(() => !locked.value && !state.stale && !state.conflict && !terminal.value && state.detail?.editability.allowed && !replacementReason.value)
const dirty = computed(() => !!state.selection || batch.state.shots.some(shot=>shot.status!=='confirmed'))
const sourceNames = {existing: '已有素材', upload: '本地上传', generated: '本轮生成', '': '待选择'}
const mediaNames = {image: '图片', video: '视频', audio: '音频'}
let targetSequence = 0, previewSequence = 0
let selectionSequence = 0
watch(busy, value => emit('busy', value), {flush: 'sync'})
watch([dirty, pending], ([dirty, pending]) => emit('retained', {dirty, pending}), {flush: 'sync', immediate: true})
function discard() { return !state.selection || window.confirm('放弃当前未提交的用途选择？已入库的上传文件会保留，不会自动删除。') }
async function loadTargets(next = page.value) {
  if (locked.value) return
  const token = ++targetSequence; targetsLoading.value = true; targetsError.value = ''
  try {
    const result = await platformApi.listProductionTargets(props.run.id, 'shot', next)
    if (token !== targetSequence) return
    if (result.runId !== props.run.id || result.runPlanSha256 !== props.run.executionPlan?.sha256
      || result.results.some(row => row.scope.kind !== 'shot' || row.unitKey !== `shot:${row.scope.id}`)) throw Error('镜头目录与本轮冻结范围不一致')
    targets.value = result; page.value = next
  } catch (cause) { if (token === targetSequence) targetsError.value = apiErrorSummary(cause) }
  finally { if (token === targetSequence) targetsLoading.value = false }
}
async function chooseShot(id: string, label: string) {
  if (locked.value || !discard()) return
  selectionSequence++; shotLabel.value = label; useId.value = ''; selectedError.value = ''; await editor.load(id)
}
async function reread() { if (!locked.value && discard()) { selectedError.value = ''; await editor.load(state.shotId) } }
function chooseUse(event: Event) {
  const control = event.target as HTMLSelectElement
  if (locked.value || !discard()) {control.value = useId.value; return}
  selectionSequence++; state.selection = null; selectedError.value = ''; useId.value = control.value
}
function select(asset: Asset, source: 'asset_library'|'asset_upload', reference?: ReferenceIdentity) {
  // The picker emits while its final upload/selection busy signal is still true.
  if (!use.value || state.pending || state.submitting || state.stale || state.conflict || terminal.value || !state.detail?.editability.allowed || replacementReason.value) return
  if (sourceRule.value && (sourceRule.value.source === 'upstream' || sourceRule.value.source !== (source === 'asset_upload' ? 'upload' : 'existing'))) return
  try {state.selection = selectShotReference(use.value, asset, source, reference); selectedError.value = ''}
  catch (cause) {selectedError.value = apiErrorSummary(cause)}
}
async function selectUpstream(candidate: RecipeReferenceCandidate) {
  if (!editable.value || !recipeDetail.value || !use.value) return
  const token = ++selectionSequence, detail = recipeDetail.value, row = use.value
  pickerBusy.value = true; selectedError.value = ''
  try {
    const selection = await selectRecipeUpstreamReference({getVersion: assetReferenceApi.getVersion, getAsset: repository.getAsset}, detail, row, candidate)
    if (token === selectionSequence && state.detail === detail && use.value?.id === row.id) state.selection = selection
  } catch (cause) {if (token === selectionSequence) selectedError.value = apiErrorSummary(cause)}
  finally {if (token === selectionSequence) pickerBusy.value = false}
}
async function loadPreview() {
  const token = ++previewSequence, row = use.value; preview.value = null; previewError.value = ''; previewLoading.value = false
  if (!row?.source_mode) return
  previewLoading.value = true
  try {
    const value = await readSavedShotReference({getVersion: assetReferenceApi.getVersion, getSeries: assetReferenceApi.getSeries, getAsset: repository.getAsset}, row)
    if (token === previewSequence) preview.value = value
  } catch (cause) {if (token === previewSequence) previewError.value = apiErrorSummary(cause)}
  finally {if (token === previewSequence) previewLoading.value = false}
}
function addBatch() {
  if (!editable.value || !state.detail || !state.selection || !use.value || previewLoading.value) return
  try {
    batch.add(state.detail, shotLabel.value, state.selection, preview.value?.label ?? (use.value.source_mode ? `原素材未能读取：${previewError.value || '请重读核对'}` : '待选择（无原素材）'))
    state.selection = null; selectedError.value = ''
  } catch (cause) {selectedError.value = apiErrorSummary(cause)}
}
watch(() => state.detail, () => {if (!state.detail?.state.uses.some(row => row.id === useId.value)) useId.value = ''})
watch(use, () => {void loadPreview()})
onMounted(() => {void loadTargets(1)})
function beforeUnload(event: BeforeUnloadEvent) {if (dirty.value || pending.value || busy.value) {event.preventDefault(); event.returnValue = ''}}
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onBeforeUnmount(() => {targetSequence++; previewSequence++; selectionSequence++; editor.dispose(); batch.dispose(); window.removeEventListener('beforeunload', beforeUnload); emit('busy', false)})
</script>

<template>
  <BaseModal :open="open" :title="run.planSource === 'recipe' ? '配方制作 · 单镜 / 多镜素材选择' : '人工制作 · 单镜 / 多镜素材选择'" width="1000px" :dismissible="!busy" @close="emit('close')">
    <section class="shot-selection" aria-label="单镜素材选择">
      <p>单镜保存或把明确用途加入多镜批量选择，再勾选并核对影响。只改勾选用途，不改 Bible 默认主图，不代表审核通过。上传先正式入库；此处保存不会调用模型。</p>
      <p v-if="terminal" class="platform-alert warning" role="status">本轮已结束，不能提交新选择；结果未知的原请求仍可核对。</p>
      <p v-if="state.pending" class="platform-alert warning" role="status">原请求结果未知：选择已锁定，请使用原请求核对。关闭面板会保留；离开或刷新浏览器会丢失本页请求。</p>
      <p v-if="state.conflict" class="platform-alert warning" role="status">基线冲突，本次未保存。请重新读取当前用途后再选择，不会自动覆盖。</p>
      <p v-if="state.error" class="platform-alert danger" role="alert">{{ state.error }}</p>
      <p v-if="state.notice" role="status">{{ state.notice }}</p>
      <div class="shot-selection-layout">
        <section aria-label="本轮镜头目录">
          <h3>本轮镜头</h3><button class="button ghost small" :disabled="locked || targetsLoading" @click="loadTargets()">刷新镜头目录</button>
          <p v-if="targetsLoading" role="status">正在读取镜头…</p><p v-if="targetsError" role="alert">{{ targetsError }}；保留的列表不是本次新结果。</p>
          <template v-if="targets"><p>共 {{ targets.count }} 镜 · 第 {{ page }} 页</p>
            <ul><li v-for="target in targets.results" :key="target.scope.id"><button class="button ghost small" :aria-pressed="state.shotId === target.scope.id" :disabled="locked || targetsLoading || !!targetsError" @click="chooseShot(target.scope.id, target.label)">{{ target.label }}</button></li></ul>
            <p v-if="!targets.count">本轮尚无镜头，请先完成分镜创建与采用。</p>
            <nav class="selection-actions" aria-label="镜头分页"><button class="button ghost small" :disabled="locked || targetsLoading || !targets.previous" @click="loadTargets(page-1)">上一页</button><button class="button ghost small" :disabled="locked || targetsLoading || !targets.next" @click="loadTargets(page+1)">下一页</button></nav>
          </template>
        </section>
        <section aria-label="本镜用途与选择">
          <h3>{{ shotLabel || '请选择一个镜头' }}</h3>
          <button v-if="state.shotId" class="button ghost small" :disabled="locked" @click="reread">重新读取本镜用途</button>
          <p v-if="state.loading" role="status">正在读取选材基线…</p>
          <template v-if="state.detail">
            <p :class="{'platform-alert warning': !state.detail.editability.allowed}" role="status">{{ state.detail.editability.reason }}</p>
            <p v-if="sourceRule" role="status">本轮冻结来源：{{ sourceRule.source === 'upstream' ? '本轮上游结果' : sourceNames[sourceRule.source] }} · {{ sourceRule.selection === 'unique_setting' ? '自动匹配唯一确切设定，未匹配用途由人工接管' : '人工选择' }} · {{ sourceRule.allow_override ? '允许替换本镜已有选择' : '不允许替换已有选择，仍可首次选材' }}。</p>
            <p v-if="state.stale" role="status">以下为上次读取，当前值尚未核实；不能提交新选择。</p>
            <p v-if="!state.detail.state.uses.length">本镜没有声明参考用途；不会自动新建用途或猜测主图。</p>
            <label v-else>本镜用途<select aria-label="本镜用途" :value="useId" :disabled="locked" @change="chooseUse"><option value="">请选择用途</option><option v-for="row in state.detail.state.uses" :key="row.id" :value="row.id">{{ row.position + 1 }} · {{ row.use_key }} · {{ row.purpose }} · {{ mediaNames[row.media_kind] }} · {{ sourceNames[row.source_mode] }}</option></select></label>
            <template v-if="use">
              <p>{{ use.use_key }} · {{ mediaNames[use.media_kind] }} · 创作用途：{{ use.purpose || '未填写（可选）' }}</p>
              <p>保存的来源：{{ sourceNames[use.source_mode] }}。本次只提交所选用途，其余用途不变。</p>
              <p v-if="replacementReason" class="platform-alert warning" role="status">{{ replacementReason }}</p>
              <p v-if="previewLoading" role="status">正在读取确切素材预览…</p>
              <p v-if="previewError" class="platform-alert danger" role="alert">{{ previewError }}<button class="button ghost small" :disabled="previewLoading" @click="loadPreview">重读此素材</button></p>
              <div v-if="preview" class="selection-preview"><strong>{{ preview.label }}</strong><AssetMediaPreview :asset="preview.asset" controls @source-error="previewError='素材预览文件读取失败；请重读此素材，不会寻找替代版本。'"/></div>
              <template v-if="recipeDetail && sourceRule?.source === 'upstream'">
                <p>候选仅来自原冻结上游；目录不代表文件可用。明确选择时读取确切版本与素材，保存时服务端再次核验。</p>
                <p v-if="!recipeDetail.upstream_candidates.length" role="status">本轮上游没有候选；不能改用库内或上传替代。</p>
                <ul aria-label="本轮上游候选"><li v-for="candidate in recipeDetail.upstream_candidates" :key="`${candidate.source.field}:${candidate.reference_version_id}:${candidate.source.reference.decision_id || candidate.source.reference.fulfillment_id}`">
                  <span>{{ candidate.name }} · 版本 {{ candidate.version }} · {{ sourceNames[candidate.source_mode] }}</span>
                  <p v-if="recipeReferenceCandidateReason(use, candidate)" role="status">{{ recipeReferenceCandidateReason(use, candidate) }}</p>
                  <button class="button ghost small" :disabled="!editable || Boolean(recipeReferenceCandidateReason(use, candidate))" @click="selectUpstream(candidate)">选择此上游版本</button>
                </li></ul>
              </template>
              <ExecutionInputAssetPicker v-else :key="use.id" :kind="use.media_kind" :disabled="!editable" :allow-library="!sourceRule || sourceRule.source === 'existing'" :allow-upload="!sourceRule || sourceRule.source === 'upload'" @selected="select" @busy="pickerBusy=$event"/>
              <p v-if="selectedError" role="alert">{{ selectedError }}</p>
              <div v-if="state.selection" class="selection-preview"><strong>{{ state.pending ? '待核对请求中的选择' : '尚未保存' }}：{{ state.selection.label }}</strong><p>{{ sourceNames[state.selection.choice.source_mode] }}；保存时服务端重新核对确切设定、权限及实际文件。</p><AssetMediaPreview :asset="state.selection.asset" controls @source-error="selectedError='所选素材预览读取失败；未据此替换素材，保存时仍由服务端核对实际文件。'"/></div>
            </template>
          </template>
          <button v-if="state.pending || state.selection" class="button primary" :disabled="busy || state.loading || (!state.pending && (!editable || batch.state.shots.length>0))" @click="editor.submit()">{{ state.submitting ? '正在提交…' : state.pending ? '核对原选材请求' : '保存本用途选择' }}</button>
          <button v-if="state.selection && !state.pending" class="button ghost" :disabled="!editable || previewLoading" @click="addBatch">加入多镜批量选择</button>
          <p v-if="batch.state.shots.length && state.selection" role="status">已有批量草稿，请加入下方预览统一核对；不再另发单用途请求改变批量基线。</p>
        </section>
      </div>
      <ShotReferenceBatchPreview :batch="batch" :disabled="state.submitting || state.loading || pickerBusy || !!state.pending"/>
    </section>
  </BaseModal>
</template>

<style scoped>
.shot-selection,.shot-selection-layout>section{display:grid;gap:12px;min-width:0}.shot-selection-layout{display:grid;grid-template-columns:minmax(180px,1fr) minmax(0,2fr);gap:20px;align-items:start}.shot-selection p,.shot-selection h3{margin:0;overflow-wrap:anywhere}.shot-selection h3{font-size:15px}.shot-selection ul{list-style:none;padding:0;margin:0;display:grid;gap:8px}.shot-selection button{min-height:36px;white-space:normal;overflow-wrap:anywhere}.shot-selection li button{width:100%;text-align:left;justify-content:start}.shot-selection button[aria-pressed=true]{border-color:var(--accent);background:var(--bg-hover)}.shot-selection label{display:grid;gap:6px;min-width:0}.shot-selection select{width:100%;min-width:0;min-height:36px}.selection-actions{display:flex;gap:8px;flex-wrap:wrap}.selection-preview{display:grid;gap:8px;max-width:320px;overflow-wrap:anywhere}.selection-preview :deep(img),.selection-preview :deep(video){max-height:200px;object-fit:contain}.selection-preview :deep(audio){max-width:100%}@media(max-width:640px){.shot-selection-layout{grid-template-columns:minmax(0,1fr)}.shot-selection-layout>section:first-child{border-bottom:1px solid var(--border);padding-bottom:16px}}
</style>
