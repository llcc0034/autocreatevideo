<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { AlertTriangle, Check, RefreshCw, Sparkles } from 'lucide-vue-next'
import { repository } from '@/services/repository'
import {
  buildScriptBibleSelections,
  createScriptBibleCandidateDrafts,
  scriptBibleCandidateSelectable,
  scriptBibleConflictMessage,
  scriptBibleErrorMessage,
  scriptBibleErrorCode,
  reconcileScriptBibleSelectionAfterRefresh,
} from '@/services/scriptBibleCandidates'
import type {
  ScriptBibleApplyResult,
  ScriptBibleCandidate,
  ScriptBibleCandidatesResult,
  ScriptBibleEntityType,
} from '@/types'

const props = defineProps<{ projectId: string; sourceDirty?: boolean }>()
const emit = defineEmits<{
  state: [value: { pendingCount: number; hasBlock: boolean }]
  applied: [value: ScriptBibleApplyResult]
  applyBusy: [value: boolean]
}>()

const root = ref<HTMLElement | null>(null)
const result = ref<ScriptBibleCandidatesResult | null>(null)
const drafts = ref(createScriptBibleCandidateDrafts([]))
const selectedIds = ref<Set<string>>(new Set())
const loading = ref(false)
const applying = ref(false)
const loadError = ref('')
const applyError = ref('')
const appliedNotice = ref('')
let requestVersion = 0
let applyVersion = 0
let recommendedSeeded = false

const labels: Record<ScriptBibleEntityType, string> = {
  character: '人物',
  scene: '场景',
  prop: '道具',
}

const groupedCandidates = computed(() => ([
  { type: 'character' as const, label: '人物建议', rows: result.value?.candidates.filter((item) => item.entityType === 'character') || [] },
  { type: 'scene' as const, label: '场景建议', rows: result.value?.candidates.filter((item) => item.entityType === 'scene') || [] },
  { type: 'prop' as const, label: '道具建议', rows: result.value?.candidates.filter((item) => item.entityType === 'prop') || [] },
]))
const pendingCount = computed(() => result.value?.candidates.filter((item) => !item.confirmed).length || 0)
const hasBlock = computed(() => result.value?.hasBlock === true)
const selectableCandidates = computed(() => result.value?.candidates.filter(scriptBibleCandidateSelectable) || [])
const selectedCount = computed(() => selectedIds.value.size)
const selectedValidationError = computed(() => {
  if (!result.value || !selectedIds.value.size) return ''
  try {
    buildScriptBibleSelections(result.value.candidates, selectedIds.value, drafts.value)
    return ''
  } catch (error) {
    return error instanceof Error ? error.message : '候选字段无效。'
  }
})

watch([pendingCount, hasBlock], ([nextPendingCount, nextHasBlock]) => {
  emit('state', { pendingCount: nextPendingCount, hasBlock: nextHasBlock })
}, { immediate: true })

const loadCandidates = async (): Promise<{ pendingCount: number; hasBlock: boolean } | null> => {
  const version = ++requestVersion
  const projectId = props.projectId
  loading.value = true
  loadError.value = ''
  appliedNotice.value = ''
  try {
    const next = await repository.getScriptBibleCandidates(projectId)
    if (version !== requestVersion || projectId !== props.projectId) return null
    const nextDrafts = createScriptBibleCandidateDrafts(next.candidates)
    for (const candidate of next.candidates) {
      const current = drafts.value[candidate.candidateId]
      if (current) nextDrafts[candidate.candidateId] = current
    }
    const selection = reconcileScriptBibleSelectionAfterRefresh(
      next.candidates,
      selectedIds.value,
      recommendedSeeded,
    )
    recommendedSeeded = selection.recommendedSeeded
    result.value = next
    applyError.value = ''
    drafts.value = nextDrafts
    selectedIds.value = selection.selectedIds
    const state = {
      pendingCount: next.candidates.filter((item) => !item.confirmed).length,
      hasBlock: next.hasBlock,
    }
    emit('state', state)
    return state
  } catch (error) {
    if (version !== requestVersion || projectId !== props.projectId) return null
    loadError.value = scriptBibleErrorMessage(error, '读取剧本 Bible 候选失败，请稍后重试。')
    return null
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

watch(() => props.projectId, () => {
  requestVersion += 1
  applyVersion += 1
  applying.value = false
  emit('applyBusy', false)
  result.value = null
  drafts.value = createScriptBibleCandidateDrafts([])
  selectedIds.value = new Set()
  recommendedSeeded = false
  loadError.value = ''
  applyError.value = ''
  void loadCandidates()
}, { immediate: true })

const candidateDraft = (candidateId: string) => drafts.value[candidateId]
const toggleCandidate = (candidate: ScriptBibleCandidate) => {
  if (!scriptBibleCandidateSelectable(candidate) || applying.value) return
  const next = new Set(selectedIds.value)
  if (next.has(candidate.candidateId)) next.delete(candidate.candidateId)
  else next.add(candidate.candidateId)
  selectedIds.value = next
  applyError.value = ''
  appliedNotice.value = ''
}
const replaceSelection = (ids: string[]) => { selectedIds.value = new Set(ids) }
const selectRecommended = () => replaceSelection(selectableCandidates.value.filter((item) => item.recommended).map((item) => item.candidateId))
const selectAll = () => replaceSelection(selectableCandidates.value.map((item) => item.candidateId))

const applySelected = async () => {
  if (!result.value || applying.value) return
  if (props.sourceDirty) {
    applyError.value = '完整剧本尚未保存。请先保存剧本并刷新候选，再确认加入 Bible。'
    return
  }
  applyError.value = ''
  appliedNotice.value = ''
  let selections
  try {
    selections = buildScriptBibleSelections(result.value.candidates, selectedIds.value, drafts.value)
  } catch (error) {
    applyError.value = error instanceof Error ? error.message : '候选字段无效。'
    return
  }
  const version = ++applyVersion
  const projectId = props.projectId
  const sourceFingerprint = result.value.sourceFingerprint
  applying.value = true
  emit('applyBusy', true)
  try {
    const applied = await repository.applyScriptBibleCandidates(
      projectId,
      sourceFingerprint,
      selections,
    )
    // A successful server write must always reach the parent so it can refresh
    // the authoritative project store, even if this panel changed projects or
    // the local master script became dirty while the request was in flight.
    emit('applied', applied)
    if (
      version !== applyVersion
      || projectId !== props.projectId
      || sourceFingerprint !== result.value?.sourceFingerprint
    ) return
    const appliedIds = new Set(selections.map((selection) => selection.candidateId))
    result.value = {
      ...result.value,
      candidates: result.value.candidates.map((candidate) => appliedIds.has(candidate.candidateId)
        ? { ...candidate, existing: true, confirmed: true, conflictCode: null }
        : candidate),
    }
    selectedIds.value = new Set()
    appliedNotice.value = `已确认 ${applied.counts.requested} 项：新增 ${applied.counts.created} 项，复用 ${applied.counts.alreadyExists} 项。`
    if (props.sourceDirty) {
      appliedNotice.value += ' 应用期间母稿又有未保存修改；项目服务端事实已刷新，请保存母稿后再刷新候选。'
    }
  } catch (error) {
    if (version !== applyVersion || projectId !== props.projectId) return
    applyError.value = scriptBibleErrorMessage(error, '确认候选失败，服务端未写入任何内容。')
    if (scriptBibleErrorCode(error) === 'script_bible_candidates_stale') {
      applyError.value += ' 点击刷新后，同一候选的当前编辑仍会保留。'
    }
  } finally {
    if (version === applyVersion && projectId === props.projectId) {
      applying.value = false
      emit('applyBusy', false)
    }
  }
}

const focus = async () => {
  await nextTick()
  root.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  root.value?.focus({ preventScroll: true })
}

defineExpose({ refresh: loadCandidates, focus })
</script>

<template>
  <section ref="root" class="panel script-bible-candidates" tabindex="-1" aria-labelledby="script-bible-candidates-title">
    <header>
      <div>
        <span class="eyebrow">SCRIPT BIBLE REVIEW</span>
        <h3 id="script-bible-candidates-title">从完整剧本确认 Bible 建议</h3>
        <p>这里只生成可编辑候选；读取、勾选和编辑都不会写入。仅点击“确认所选并加入 Bible”后，服务端才会增量新增或复用实体，既有 Bible 不会被删除。</p>
      </div>
      <button type="button" class="button ghost small" :disabled="loading||applying" @click="loadCandidates">
        <RefreshCw :size="14" :class="{ spinning: loading }"/>刷新候选
      </button>
    </header>

    <div v-if="loading&&!result" class="candidate-state"><div class="loader-ring"></div><span>正在从已保存的完整剧本读取建议…</span></div>
    <div v-else-if="loadError&&!result" class="candidate-state error" role="alert">
      <AlertTriangle :size="18"/><span>{{ loadError }}</span><button type="button" @click="loadCandidates">重试</button>
    </div>
    <template v-else-if="result">
      <div class="candidate-summary">
        <span><strong>{{ result.candidates.length }}</strong> 项候选</span>
        <span><strong>{{ pendingCount }}</strong> 项待处理</span>
        <span v-if="pendingCount" class="blocked"><AlertTriangle :size="13"/>拆解前需确认或明确跳过</span>
        <span v-else class="ready"><Check :size="13"/>当前无待确认阻断</span>
      </div>
      <div class="candidate-toolbar">
        <span>已选 {{ selectedCount }} / {{ selectableCandidates.length }}</span>
        <button type="button" :disabled="applying" @click="selectRecommended">选择推荐项</button>
        <button type="button" :disabled="applying" @click="selectAll">选择全部可确认项</button>
        <button type="button" :disabled="applying||!selectedCount" @click="replaceSelection([])">清空选择</button>
      </div>

      <div v-if="loadError" class="candidate-inline-error" role="alert"><AlertTriangle :size="14"/>{{ loadError }}</div>
      <div v-if="sourceDirty" class="candidate-inline-error" role="alert"><AlertTriangle :size="14"/>完整剧本有未保存修改；保存并刷新后才能确认这些候选。</div>
      <div v-if="applyError" class="candidate-inline-error" role="alert"><AlertTriangle :size="14"/>{{ applyError }}</div>
      <div v-if="appliedNotice" class="candidate-inline-success" role="status"><Check :size="14"/>{{ appliedNotice }}</div>

      <div v-if="result.candidates.length" class="candidate-groups">
        <section v-for="group in groupedCandidates" :key="group.type" v-show="group.rows.length" class="candidate-group">
          <header><h4>{{ group.label }}</h4><span>{{ group.rows.length }} 项</span></header>
          <article v-for="candidate in group.rows" :key="candidate.candidateId" class="candidate-card" :class="{ selected:selectedIds.has(candidate.candidateId), unavailable:!scriptBibleCandidateSelectable(candidate) }">
            <label class="candidate-select">
              <input
                type="checkbox"
                :checked="selectedIds.has(candidate.candidateId)"
                :disabled="!scriptBibleCandidateSelectable(candidate)||applying"
                :aria-label="`选择${labels[candidate.entityType]}候选 ${candidate.name}`"
                @change="toggleCandidate(candidate)"
              >
              <span><strong>{{ candidate.name }}</strong><small>声明于第 {{ candidate.declarationLine }} 行 · 出现 {{ candidate.occurrence }} 次</small></span>
            </label>
            <div class="candidate-statuses">
              <span v-if="candidate.recommended">推荐</span>
              <span v-if="candidate.existing" class="existing">Bible 已存在</span>
              <span v-if="candidate.confirmed" class="confirmed">已人工确认</span>
            </div>
            <p v-if="candidate.conflictCode" class="candidate-conflict"><AlertTriangle :size="13"/>{{ scriptBibleConflictMessage(candidate.conflictCode) }}</p>
            <div v-if="candidateDraft(candidate.candidateId)" class="candidate-fields">
              <label>名称<input v-model="candidateDraft(candidate.candidateId).name" maxlength="160" :disabled="!scriptBibleCandidateSelectable(candidate)||applying"></label>
              <label>基础描述<textarea v-model="candidateDraft(candidate.candidateId).description" rows="2" maxlength="2000" :disabled="!scriptBibleCandidateSelectable(candidate)||applying"></textarea></label>
              <label>视觉锚点（英文）<textarea v-model="candidateDraft(candidate.candidateId).visualIdentityEn" rows="3" maxlength="3000" :disabled="!scriptBibleCandidateSelectable(candidate)||applying"></textarea></label>
            </div>
            <details v-if="candidate.evidence.length" class="candidate-evidence">
              <summary>查看 {{ candidate.evidence.length }} 条剧本依据</summary>
              <ul><li v-for="evidence in candidate.evidence" :key="`${evidence.line}-${evidence.kind}-${evidence.excerpt}`"><span>第 {{ evidence.line }} 行</span>{{ evidence.excerpt }}</li></ul>
            </details>
          </article>
        </section>
      </div>
      <div v-else class="candidate-state"><Sparkles :size="20"/><span>完整剧本中没有可审核的 Bible 声明。</span></div>

      <details v-if="result.diagnostics.length" class="candidate-diagnostics">
        <summary>解析提示（{{ result.diagnostics.length }}）</summary>
        <ul><li v-for="diagnostic in result.diagnostics" :key="`${diagnostic.code}-${diagnostic.line||0}-${diagnostic.candidateId||''}`"><strong>{{ diagnostic.severity.toUpperCase() }}</strong><span v-if="diagnostic.line">第 {{ diagnostic.line }} 行 · </span>{{ diagnostic.detail || diagnostic.code }}</li></ul>
      </details>
      <footer>
        <span v-if="selectedValidationError" class="selection-error">{{ selectedValidationError }}</span>
        <span v-else>确认只会提交当前勾选项和你在上方看到的最终字段。</span>
        <button type="button" class="button primary" :disabled="applying||sourceDirty||!selectedCount||Boolean(selectedValidationError)" @click="applySelected">
          <RefreshCw v-if="applying" :size="15" class="spinning"/><Check v-else :size="15"/>{{ applying?'正在确认…':`确认所选并加入 Bible（${selectedCount}）` }}
        </button>
      </footer>
    </template>
  </section>
</template>

<style scoped>
.script-bible-candidates{display:flex;flex-direction:column;gap:14px;padding:20px;outline:none}.script-bible-candidates>header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}.script-bible-candidates h3{margin:4px 0 5px}.script-bible-candidates p{margin:0;color:#7d8790;font-size:11px;line-height:1.65}.candidate-state{min-height:110px;display:flex;align-items:center;justify-content:center;gap:10px;color:#77818a}.candidate-state.error,.candidate-inline-error{color:#ff7d87}.candidate-state button,.candidate-toolbar button{border:0;background:none;color:#d89b48;cursor:pointer}.candidate-summary,.candidate-toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.candidate-summary span,.candidate-toolbar{padding:9px 11px;border:1px solid #242a30;border-radius:7px;background:#0b0f13;color:#8d969e;font-size:10px}.candidate-summary strong{color:#e4e7ea}.candidate-summary .blocked{color:#efb164}.candidate-summary .ready{color:#69c992}.candidate-toolbar{justify-content:flex-end}.candidate-toolbar>span{margin-right:auto}.candidate-toolbar button:disabled{opacity:.45;cursor:not-allowed}.candidate-inline-error,.candidate-inline-success{display:flex;align-items:flex-start;gap:7px;padding:10px 12px;border-radius:7px;font-size:11px}.candidate-inline-error{border:1px solid #5a2930;background:#211116}.candidate-inline-success{border:1px solid #28523b;background:#102319;color:#71d49a}.candidate-groups{display:flex;flex-direction:column;gap:16px}.candidate-group>header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}.candidate-group h4{margin:0;font-size:12px}.candidate-group>header span{color:#737d85;font-size:10px}.candidate-card{position:relative;display:grid;grid-template-columns:minmax(190px,.7fr) minmax(0,1.7fr);gap:12px;padding:14px;border:1px solid #252b31;border-radius:8px;background:#0c1014}.candidate-card+.candidate-card{margin-top:8px}.candidate-card.selected{border-color:#8d632e;box-shadow:inset 3px 0 #d89b48}.candidate-card.unavailable{background:#0b0e11}.candidate-select{display:flex;align-items:flex-start;gap:9px}.candidate-select input{margin-top:3px;accent-color:#d89b48}.candidate-select span{display:flex;flex-direction:column;gap:4px}.candidate-select strong{font-size:12px}.candidate-select small{color:#707981;font-size:9px}.candidate-statuses{position:absolute;right:12px;top:11px;display:flex;gap:5px}.candidate-statuses span{padding:3px 6px;border:1px solid #59431f;border-radius:9px;color:#ddb062;font-size:8px}.candidate-statuses .existing{border-color:#31516b;color:#76b8df}.candidate-statuses .confirmed{border-color:#2e6042;color:#71d49a}.candidate-conflict{grid-column:1/-1!important;display:flex;gap:6px;color:#ef9a72!important}.candidate-fields{display:grid;gap:8px;padding-top:22px}.candidate-fields label{display:flex;flex-direction:column;gap:4px;color:#8e979f;font-size:9px}.candidate-fields input,.candidate-fields textarea{width:100%;box-sizing:border-box;border:1px solid #293039;border-radius:6px;background:#090d10;color:#d9dde0;padding:8px 9px;font:inherit;resize:vertical}.candidate-fields :disabled{opacity:.62}.candidate-evidence{grid-column:1/-1;border-top:1px solid #20262c;padding-top:9px;color:#737d85;font-size:9px}.candidate-evidence summary{cursor:pointer}.candidate-evidence ul,.candidate-diagnostics ul{margin:8px 0 0;padding-left:18px}.candidate-evidence li{margin:4px 0}.candidate-evidence li span{margin-right:7px;color:#b88a4b}.candidate-diagnostics{color:#77818a;font-size:9px}.candidate-diagnostics summary{cursor:pointer}.candidate-diagnostics li{margin:5px 0}.candidate-diagnostics strong{margin-right:6px;color:#d89b48}.script-bible-candidates>footer{display:flex;align-items:center;justify-content:space-between;gap:16px;padding-top:4px}.script-bible-candidates>footer>span{color:#747d85;font-size:9px}.script-bible-candidates>footer .selection-error{color:#ff8b94}@media(max-width:800px){.script-bible-candidates>header,.script-bible-candidates>footer{align-items:stretch;flex-direction:column}.candidate-card{grid-template-columns:1fr}.candidate-fields{padding-top:0}.candidate-statuses{position:static;grid-row:2}.candidate-toolbar{justify-content:flex-start}.candidate-toolbar>span{width:100%;margin:0}}
</style>
