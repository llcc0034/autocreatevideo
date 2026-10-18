<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { buildManualMediaAdoption, manualMediaAdoptionResult, readManualMediaCandidate } from '@/services/productionManualMedia'
import { manualScopeLabels, type ManualActionOptions } from '@/services/productionManual'
import type { GeneratedArtifact, JsonObject, ProductionRun } from '@/services/platformTypes'
import PlatformStatusPill from './PlatformStatusPill.vue'
import ManualAssetAdoption from './ManualAssetAdoption.vue'

const props = defineProps<{ run: ProductionRun; options: ManualActionOptions }>()
const emit = defineEmits<{ changed: []; busy: [value: boolean] }>()
const rows = ref<GeneratedArtifact[]>([]), selected = ref<GeneratedArtifact | null>(null)
const candidate = ref<ReturnType<typeof readManualMediaCandidate> | null>(null)
const page = ref(1), count = ref<number | null>(null), next = ref(false), previous = ref(false)
const loading = ref(false), submitting = ref(false), error = ref(''), notice = ref(''), reviewed = ref(false)
const pending = ref<JsonObject | null>(null), requestKey = ref(crypto.randomUUID())
const scopeLabel = computed(() => candidate.value ? manualScopeLabels[candidate.value.scope.kind] : '')
const terminal = computed(() => ['succeeded', 'failed', 'cancelled'].includes(props.run.status))
const assetBusy = ref(false)
const actionBusy = computed(() => submitting.value || assetBusy.value)
watch(actionBusy, value => emit('busy', value))
let sequence = 0
async function load(targetPage = 1) {
  if (actionBusy.value) return
  const token = ++sequence; loading.value = true; error.value = ''; selected.value = null; candidate.value = null
  rows.value = []; count.value = null; reviewed.value = false; pending.value = null; requestKey.value = crypto.randomUUID()
  try {
    const stage = props.run.stages?.find(row => row.stageKey === 'media_generation')
    if (!stage) throw new Error('本轮缺少正式媒体制作步骤')
    const result = await platformApi.listArtifactPage({ production_run: props.run.id, stage_run: stage.id, page: targetPage })
    if (token !== sequence) return
    rows.value = result.results; page.value = targetPage; count.value = result.count; next.value = result.hasNext; previous.value = result.hasPrevious
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) loading.value = false }
}
async function choose(row: GeneratedArtifact) {
  if (actionBusy.value || pending.value) return
  const token = ++sequence; loading.value = true; error.value = ''; selected.value = null; candidate.value = null; reviewed.value = false
  try {
    const detail = await platformApi.getArtifact(row.id)
    if (token !== sequence) return
    if (detail.sha256 !== row.sha256 || detail.manifestMetadataSha256 !== row.manifestMetadataSha256) throw new Error('候选内容已变化，请重新读取清单')
    candidate.value = readManualMediaCandidate(detail, props.run); selected.value = detail
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) loading.value = false }
}
async function adopt() {
  if (loading.value || actionBusy.value || terminal.value || (!pending.value && (!selected.value || !reviewed.value))) return
  const token = sequence; submitting.value = true; error.value = ''; notice.value = ''
  try {
    pending.value ??= buildManualMediaAdoption(selected.value!, props.run, props.options, requestKey.value)
    const result = await platformApi.performManualProductionAction(props.run.id, pending.value)
    manualMediaAdoptionResult(result)
    if (token !== sequence) return
    pending.value = null; requestKey.value = crypto.randomUUID(); notice.value = '已采用此候选；尚未批准本步骤或开始装配。'
    selected.value = null; candidate.value = null; reviewed.value = false; emit('changed')
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { submitting.value = false }
}
watch(() => props.run.id, () => { void load() }, { immediate: true })
onBeforeUnmount(() => { sequence++ })
</script>

<template>
  <section class="manual-media-adoption">
    <p>选择本轮已归档候选并查看制作对象。采用不代表整步审核通过，也不会自动开始装配。</p>
    <div v-if="error" class="platform-alert danger" role="alert">{{ error }}</div>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="loading" role="status">正在读取并核对候选…</p>
    <div class="candidate-toolbar"><span v-if="count !== null">共 {{ count }} 个候选 · 第 {{ page }} 页</span><button class="button ghost small" :disabled="loading || actionBusy" @click="load(page)">重新读取</button></div>
    <p v-if="!loading && !error && count === 0">本轮暂无媒体候选，可创建生成准备，或直接采用下方素材库已有媒体。</p>
    <div class="candidate-list"><button v-for="row in rows" :key="row.id" class="button ghost candidate-row" :disabled="loading || actionBusy || Boolean(pending)" :aria-pressed="selected?.id === row.id" @click="choose(row)"><span>{{ row.kind }} · 候选 {{ row.variantIndex }} · {{ row.id.slice(0,8) }}</span><PlatformStatusPill :status="row.status"/></button></div>
    <div class="candidate-toolbar"><button class="button ghost small" :disabled="!previous || loading || actionBusy || Boolean(pending)" @click="load(page - 1)">上一页</button><button class="button ghost small" :disabled="!next || loading || actionBusy || Boolean(pending)" @click="load(page + 1)">下一页</button></div>
    <section v-if="selected && candidate" class="candidate-detail">
      <h4>{{ scopeLabel }} · {{ candidate.scope.id }}</h4>
      <p v-if="candidate.sourceKind === 'execution'">模型生成 · 执行准备 {{ candidate.preparationId }} · 当前采用修订 {{ candidate.revision }}</p>
      <p v-else>素材库采用 · 原素材 {{ candidate.sourceAssetId }} · 当前采用修订 {{ candidate.revision }}</p>
      <img v-if="selected.kind === 'image' && selected.previewUrl" :src="selected.previewUrl" alt="选中的媒体候选">
      <video v-else-if="selected.kind === 'video' && selected.previewUrl" :src="selected.previewUrl" controls preload="metadata"></video>
      <audio v-else-if="selected.kind === 'audio' && selected.previewUrl" :src="selected.previewUrl" controls preload="metadata"></audio>
      <p v-else>当前候选没有可用的预览链接；请先核对归档文件，不要将无预览当作预览通过。</p>
      <a v-if="selected.downloadUrl" :href="selected.downloadUrl">下载候选文件</a>
      <label><input v-model="reviewed" type="checkbox" :disabled="actionBusy || Boolean(pending)">我已核对候选内容和制作对象</label>
      <button class="button primary small" :disabled="actionBusy || terminal || (!pending && !reviewed)" @click="adopt">{{ submitting ? '正在采用…' : pending ? '重试同一采用请求' : candidate.selectedArtifactId === selected.id ? '确认当前采用结果' : '采用此候选' }}</button>
    </section>
    <ManualAssetAdoption :run="run" :options="options" :disabled="loading || submitting || Boolean(pending) || terminal" @busy="assetBusy=$event" @changed="selected=null;candidate=null;reviewed=false;emit('changed')"/>
  </section>
</template>

<style scoped>
.manual-media-adoption,.candidate-detail{display:grid;gap:12px}.candidate-toolbar,.candidate-row{display:flex;gap:12px;align-items:center;justify-content:space-between}.candidate-list{display:grid;gap:8px}.candidate-row[aria-pressed=true]{border-color:var(--accent)}.candidate-detail h4,.candidate-detail p{margin:0;overflow-wrap:anywhere}.candidate-detail img,.candidate-detail video{max-width:100%;max-height:420px;object-fit:contain}.candidate-detail audio{max-width:100%}.candidate-detail label{display:flex;gap:8px;align-items:center}
</style>
