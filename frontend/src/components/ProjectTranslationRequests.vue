<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { isAxiosError } from 'axios'
import BaseModal from './BaseModal.vue'
import ExecutionRequestInputEditor from './ExecutionRequestInputEditor.vue'
import ExecutionPreparationWorkbench from './ExecutionPreparationWorkbench.vue'
import PlatformStatusPill from './PlatformStatusPill.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import { emptyExecutionRequestDraft, executionRequestFormContract, type ExecutionRequestDraft, type ExecutionRequestFormContract } from '@/services/executionRequestForm'
import { projectTranslationApi, translationFixture, translationManagedInput, translationRequestEntries, type TranslationRequest, type TranslationSource, type TranslationRequestEntry } from '@/services/projectTranslationRequests'
import type { AssetGenerationOption, PromptTranslationEntry, PromptTranslationField } from '@/types'

const props = defineProps<{ projectId: string; fields: PromptTranslationField[]; entries: PromptTranslationEntry[]; blockedReason?: string }>()
const emit = defineEmits<{ adopted: [] }>()
const open = ref(false), creating = ref(false), busy = ref(false), preparationBusy = ref(false), preparationDirty = ref(false)
const uploading = ref(false), optionsLoading = ref(false), contractLoading = ref(false), historyLoading = ref(false), sourcesLoading = ref(false), candidateLoading = ref(false)
const error = ref(''), historyError = ref(''), sourceError = ref(''), notice = ref(''), inputErrors = ref<string[]>([])
const sources = ref<TranslationRequestEntry[]>([]), selectedId = ref(''), selectedOption = ref<AssetGenerationOption | null>(null)
const selectedSourceIndices = ref<number[]>([])
const selectedSources = computed(() => sources.value.filter((_, index) => selectedSourceIndices.value.includes(index)))
const options = ref<Awaited<ReturnType<typeof projectTranslationApi.options>> | null>(null), optionPage = ref(1)
const contract = ref<ExecutionRequestFormContract | null>(null), draft = ref<ExecutionRequestDraft | null>(null)
const allowExternal = ref(false), reviewed = ref(false)
const history = ref<Awaited<ReturnType<typeof projectTranslationApi.list>> | null>(null), historyPage = ref(1)
const selected = ref<TranslationRequest | null>(null), sourcePage = ref(1)
const sourceRows = ref<Awaited<ReturnType<typeof projectTranslationApi.sources>> | null>(null)
const candidate = ref<Awaited<ReturnType<typeof projectTranslationApi.candidate>> | null>(null)
const pending = ref<{projectId: string; key: string; body: object; versionId: string} | null>(null)
let alive = true, optionSequence = 0, contractSequence = 0, historySequence = 0, sourceSequence = 0, candidateSequence = 0
onBeforeUnmount(() => { alive = false; optionSequence++; contractSequence++; historySequence++; sourceSequence++; candidateSequence++ })
const locked = computed(() => busy.value || uploading.value || contractLoading.value || !!pending.value || preparationBusy.value)
const visibleOptions = computed(() => selectedOption.value && !options.value?.results.some(row => row.versionId === selectedOption.value!.versionId)
  ? [selectedOption.value,...(options.value?.results ?? [])] : options.value?.results ?? [])
watch([draft,allowExternal,selectedSourceIndices], () => { if (!pending.value) reviewed.value = false }, {deep:true})
const sourceSelectionError = computed(() => !selectedSources.value.length ? '请至少选择一项原文'
  : options.value && selectedSources.value.length > options.value.maxEntries ? `本批最多 ${options.value.maxEntries} 项，请取消部分选择后提交；不会自动拆批调用模型。` : '')
function selectSource(index: number, checked: boolean) {
  if (locked.value || !Number.isInteger(index) || index < 0 || index >= sources.value.length) return
  selectedSourceIndices.value = checked ? [...new Set([...selectedSourceIndices.value,index])] : selectedSourceIndices.value.filter(value => value !== index)
}
function selectAllSources(checked: boolean) {
  if (!locked.value) selectedSourceIndices.value = checked ? sources.value.map((_, index) => index) : []
}
function sourceLabel(row: TranslationRequestEntry) {
  return props.fields.find(field => field.scopeType === row.scope_type && field.scopeId === row.scope_id && field.fieldKey === row.field_key)?.label ?? row.field_key
}
function canDiscard() { return !locked.value && (!preparationDirty.value || window.confirm('放弃执行准备中尚未保存的文字？已保存任务不会删除。')) }
function close() {
  if (!canDiscard() || (creating.value && draft.value && !window.confirm('关闭将放弃此处尚未提交的翻译配置，是否继续？'))) return
  open.value = false; preparationDirty.value = false
}
async function loadOptions(page = optionPage.value) {
  const token = ++optionSequence; optionsLoading.value = true; options.value = null; error.value = ''
  try { const result = await projectTranslationApi.options(props.projectId,page); if (alive && token === optionSequence) { options.value = result; optionPage.value = page } }
  catch (e) { if (alive && token === optionSequence) error.value = apiErrorSummary(e) }
  finally { if (alive && token === optionSequence) optionsLoading.value = false }
}
async function choose(id: string) {
  if (locked.value || id === selectedId.value) return
  if (draft.value && !window.confirm('更换实例会清空本次参数、提示词和授权，是否继续？')) return
  const option = visibleOptions.value.find(row => row.versionId === id)
  selectedId.value = id; selectedOption.value = option ?? null; contract.value = null; draft.value = null
  allowExternal.value = false; reviewed.value = false; error.value = ''; inputErrors.value = []
  const token = ++contractSequence
  if (!option) return
  contractLoading.value = true
  try {
    const value = executionRequestFormContract(await executionRegistryApi.contract(id))
    if (!alive || token !== contractSequence) return
    if (value.versionId !== id || value.sha256 !== option.definitionSha256) throw new Error('合同与所选实例版本不一致，请重新读取')
    translationManagedInput(value)
    contract.value = value; draft.value = emptyExecutionRequestDraft(value)
  } catch (e) { if (alive && token === contractSequence) error.value = apiErrorSummary(e) }
  finally { if (alive && token === contractSequence) contractLoading.value = false }
}
async function begin(fields: PromptTranslationField[]) {
  if (props.blockedReason || !canDiscard()) { error.value = props.blockedReason ?? ''; return }
  if (creating.value && draft.value && !window.confirm('重新选择原文将放弃尚未提交的翻译配置，是否继续？')) return
  open.value = true; error.value = ''; notice.value = ''
  try { sources.value = translationRequestEntries(fields,props.entries) } catch (e) { error.value = apiErrorSummary(e); return }
  selectedSourceIndices.value = sources.value.map((_, index) => index)
  creating.value = true; selected.value = null; candidate.value = null; sourceRows.value = null; sourceSequence++; candidateSequence++
  contract.value = null; draft.value = null; selectedId.value = ''; selectedOption.value = null; contractSequence++
  allowExternal.value = false; reviewed.value = false; inputErrors.value = []; preparationDirty.value = false
  await loadOptions(1)
}
async function loadHistory(page = historyPage.value) {
  const token = ++historySequence; historyLoading.value = true; historyError.value = ''; history.value = null
  try { const result = await projectTranslationApi.list(props.projectId,page); if (alive && token === historySequence) { history.value = result; historyPage.value = page } }
  catch (e) { if (alive && token === historySequence) historyError.value = apiErrorSummary(e) }
  finally { if (alive && token === historySequence) historyLoading.value = false }
}
async function showHistory() {
  if (!canDiscard()) return
  if (creating.value && draft.value && !window.confirm('放弃尚未提交的翻译配置并查看任务？')) return
  creating.value = false; open.value = true; error.value = ''; await loadHistory(1)
}
async function loadSources(page = 1) {
  if (!selected.value) return
  const token = ++sourceSequence, requestId = selected.value.id
  sourcesLoading.value = true; sourceError.value = ''; sourceRows.value = null; candidate.value = null; candidateSequence++; candidateLoading.value = false
  try { const result = await projectTranslationApi.sources(props.projectId,requestId,page); if (alive && token === sourceSequence) { sourceRows.value = result; sourcePage.value = page } }
  catch (e) { if (alive && token === sourceSequence) sourceError.value = apiErrorSummary(e) }
  finally { if (alive && token === sourceSequence) sourcesLoading.value = false }
}
async function selectRequest(row: TranslationRequest) {
  if (!canDiscard()) return
  selected.value = row; preparationDirty.value = false; await loadSources(1)
}
async function readCandidate(row: TranslationSource) {
  if (!selected.value || busy.value) return
  const token = ++candidateSequence, requestId = selected.value.id
  candidate.value = null; sourceError.value = ''; candidateLoading.value = true
  try {
    const result = await projectTranslationApi.candidate(props.projectId,requestId,row.id)
    if (!alive || token !== candidateSequence) return
    if (result.source.id !== row.id) throw new Error('返回候选与所选字段不一致')
    candidate.value = result
  } catch (e) { if (alive && token === candidateSequence) sourceError.value = apiErrorSummary(e) }
  finally { if (alive && token === candidateSequence) candidateLoading.value = false }
}
async function submit() {
  if (busy.value || uploading.value || props.blockedReason) return
  error.value = ''
  try {
    if (!pending.value) {
      if (!contract.value || !draft.value || !options.value || !reviewed.value || inputErrors.value.length) return
      if (sourceSelectionError.value) throw new Error(sourceSelectionError.value)
      pending.value = {projectId:props.projectId,key:crypto.randomUUID(),versionId:contract.value.versionId,body:{
        execution_instance_version_id:contract.value.versionId,expected_definition_sha256:contract.value.sha256,
        entries:JSON.parse(JSON.stringify(selectedSources.value)),source_lang:options.value.sourceLang,target_lang:options.value.targetLang,
        request:translationFixture(contract.value,draft.value),field_values:[],allow_external_calls:allowExternal.value}}
    }
    const request = pending.value; busy.value = true
    const row = await projectTranslationApi.create(request.projectId,request.body,request.key)
    if (!alive) return
    if (row.versionId !== request.versionId) throw new Error('返回任务与所选实例不一致，请核对同一请求')
    pending.value = null; creating.value = false; selected.value = row; notice.value = '任务已创建。请在执行准备中核对输入并继续；译文返回后仍需明确采用和人工审核。'
    await Promise.all([loadHistory(1),loadSources(1)])
  } catch (e) {
    if (alive) {
      error.value = apiErrorSummary(e)
      if (isAxiosError(e) && [400,401,403,404,409,422].includes(e.response?.status ?? 0)) { pending.value = null; reviewed.value = false }
    }
  } finally { if (alive) busy.value = false }
}
async function adopt() {
  if (!selected.value || !candidate.value?.current || candidate.value.source.adoptedEntryId || busy.value || props.blockedReason) return
  if (!window.confirm('将此译文放入原字段的待审核候选？不会自动确认或改写项目原文。')) return
  const sourceId = candidate.value.source.id, requestId = selected.value.id
  busy.value = true; sourceError.value = ''
  try {
    const receipt = await projectTranslationApi.adopt(props.projectId,requestId,sourceId)
    if (!alive) return
    if (receipt.id !== sourceId || !receipt.adoptedEntryId) throw new Error('采用回执不可核验，请重新读取候选')
    candidate.value = null; notice.value = '已放入待审核候选。请回到原字段检查译文，再决定是否确认。'; emit('adopted'); await loadSources(sourcePage.value)
  } catch (e) { if (alive) sourceError.value = apiErrorSummary(e) }
  finally { if (alive) busy.value = false }
}
defineExpose({begin})
</script>

<template>
  <button type="button" class="button ghost small" @click="showHistory">查看翻译任务</button>
  <BaseModal :open="open" :dismissible="!locked" title="项目字段翻译" eyebrow="执行实例 · 异步候选 · 人工审核" width="1050px" @close="close">
    <div class="translation-requests">
      <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
      <p v-if="notice" role="status">{{ notice }}</p>
      <p v-if="blockedReason" role="alert">{{ blockedReason }}</p>
      <template v-if="creating">
        <p>已选 {{ selectedSources.length }} / {{ sources.length }} 项原文。所选字段及已读取的人工修订在提交时一起核对；不同步调用模型，不自动采用译文。</p>
        <details :open="!!sourceSelectionError"><summary>选择与查看本批原文</summary>
          <div class="actions"><button :disabled="locked" @click="selectAllSources(true)">全选原文</button><button :disabled="locked" @click="selectAllSources(false)">清空选择</button></div>
          <article v-for="(row,index) in sources" :key="row.scope_type+row.scope_id+row.field_key">
            <label class="check"><input type="checkbox" :checked="selectedSourceIndices.includes(index)" :disabled="locked" @change="selectSource(index,($event.target as HTMLInputElement).checked)"/>{{ sourceLabel(row) }} · {{ row.scope_type }} {{ row.scope_id.slice(0,8) }}</label><pre>{{ row.source_text }}</pre>
          </article>
        </details>
        <p v-if="sourceSelectionError" role="alert">{{ sourceSelectionError }}</p>
        <p v-if="optionsLoading || contractLoading" role="status">正在读取实例与输入合同…</p>
        <label>执行实例版本<select :value="selectedId" :disabled="locked || optionsLoading" @change="choose(($event.target as HTMLSelectElement).value)"><option value="">请选择实例，不自动采用默认值</option><option v-for="row in visibleOptions" :key="row.versionId" :value="row.versionId">{{ row.name }} · v{{ row.version }}</option></select></label>
        <div class="actions"><button class="button ghost small" :disabled="locked || optionsLoading" @click="loadOptions()">刷新实例目录</button><template v-if="options"><span>共 {{ options.count }} 个版本 · 第 {{ optionPage }} 页</span><button :disabled="locked || optionsLoading || !options.previous" @click="loadOptions(optionPage-1)">上一页</button><button :disabled="locked || optionsLoading || !options.next" @click="loadOptions(optionPage+1)">下一页</button></template></div>
        <p v-if="options && !options.count">暂无可选的独立翻译实例。请在系统设置发布对应 prompt.normalize 原生文本实例；不回退到基础模型部署。</p>
        <template v-if="contract && draft">
          <p>来源 {{ options?.sourceLang }} → {{ options?.targetLang }}；目录可选不等于本次请求已验收。</p>
          <ExecutionRequestInputEditor v-model="draft" :contract="contract" :project-id="projectId" :managed-input-keys="[translationManagedInput(contract)]" :disabled="locked" @busy="uploading=$event" @validity="inputErrors=$event"/>
          <p v-for="item in inputErrors" :key="item" role="alert">{{ item }}</p>
          <p>本次使用系统资源策略，超额时保留准备并等待人工确认。</p>
          <label class="check"><input v-model="allowExternal" type="checkbox" :disabled="locked"/>允许本次内容发送至外部模型，可能产生费用</label>
          <label class="check"><input v-model="reviewed" type="checkbox" :disabled="locked"/>已核对原文、实例、输入和授权</label>
          <p v-if="pending" role="status">提交结果未确认，请重试同一请求，不要重复创建。请求标识 {{ pending.key }}</p>
          <button class="button primary" :disabled="busy || uploading || !!blockedReason || (!pending && (!reviewed || !!inputErrors.length || !!sourceSelectionError))" @click="submit">{{ busy?'正在确认请求…':pending?'重试确认同一请求':'创建翻译任务' }}</button>
        </template>
      </template>
      <template v-else>
        <div class="actions"><button :disabled="locked || historyLoading" @click="loadHistory()">刷新任务</button><span v-if="history">共 {{ history.count }} 批 · 第 {{ historyPage }} 页</span><button v-if="history" :disabled="locked || historyLoading || !history.previous" @click="loadHistory(historyPage-1)">上一页</button><button v-if="history" :disabled="locked || historyLoading || !history.next" @click="loadHistory(historyPage+1)">下一页</button></div>
        <p v-if="historyLoading" role="status">正在读取原任务…</p><p v-if="historyError" class="platform-alert danger" role="alert">{{ historyError }}</p>
        <p v-if="history && !history.count">尚无异步翻译任务。可在字段旁选择翻译，或继续手工填写英文。</p>
        <div v-for="row in history?.results" :key="row.id" class="actions"><button :disabled="locked" @click="selectRequest(row)">{{ new Date(row.createdAt).toLocaleString() }} · {{ row.sourceCount }} 项 · 查看准备与候选</button><PlatformStatusPill :status="row.status"/></div>
        <section v-if="selected">
          <div class="actions"><strong>本批 {{ selected.sourceCount }} 项</strong><RouterLink :to="{name:'workflows',query:{category:'asset',source_id:selected.jobId}}">原任务状态、取消与重试</RouterLink></div>
          <ExecutionPreparationWorkbench :key="selected.preparationId" :preparation-id="selected.preparationId" :version-id="selected.versionId" purpose="production" @changed="loadHistory()" @busy="preparationBusy=$event" @dirty="preparationDirty=$event"/>
          <div class="actions"><button :disabled="locked || sourcesLoading" @click="loadSources(sourcePage)">刷新候选来源</button><template v-if="sourceRows"><span>共 {{ sourceRows.count }} 项 · 第 {{ sourcePage }} 页</span><button :disabled="locked || sourcesLoading || !sourceRows.previous" @click="loadSources(sourcePage-1)">上一页</button><button :disabled="locked || sourcesLoading || !sourceRows.next" @click="loadSources(sourcePage+1)">下一页</button></template></div>
          <p v-if="sourcesLoading || candidateLoading" role="status">正在读取候选…</p><p v-if="sourceError" class="platform-alert danger" role="alert">{{ sourceError }}</p>
          <div v-for="row in sourceRows?.results" :key="row.id" class="actions"><span>{{ row.position+1 }} · {{ row.fieldKey }} · {{ row.scopeType }} {{ row.scopeId.slice(0,8) }}{{ row.adoptedEntryId?' · 已采用为待审核候选':'' }}</span><button :disabled="locked || candidateLoading" @click="readCandidate(row)">查看译文</button></div>
          <article v-if="candidate"><label>本次原文<pre>{{ candidate.sourceText }}</pre></label><label>本次译文<pre>{{ candidate.text }}</pre></label><p v-if="!candidate.current" role="alert">{{ candidate.reason }}</p><button class="button primary" :disabled="locked || !candidate.current || !!candidate.source.adoptedEntryId || !!blockedReason" @click="adopt">{{ candidate.source.adoptedEntryId?'已采用，继续原字段审核':'采用为待审核候选' }}</button></article>
        </section>
      </template>
    </div>
  </BaseModal>
</template>

<style scoped>
.translation-requests{display:grid;gap:12px;min-width:0}.translation-requests section,.translation-requests article{display:grid;gap:10px;padding:12px 0;border-top:1px solid var(--border-color,#29343c)}.actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.translation-requests label{display:grid;gap:6px}.translation-requests .check{display:flex;gap:8px}.check input{width:auto}.translation-requests pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:280px;overflow:auto}.translation-requests p,.translation-requests label{font-size:12px;line-height:1.7;overflow-wrap:anywhere}
</style>
