<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { ExecutionInputSuggestion, ExecutionInputSuggestionChoice } from '@/services/executionRegistry'
import type { ExecutionRequestDraft } from '@/services/executionRequestForm'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import { applyExecutionInputSuggestion } from '@/services/executionInputSuggestions'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { repository } from '@/services/repository'
import type { SeriesProject } from '@/types'

const props = defineProps<{ versionId: string; definitionSha256: string; inputKey: string; projectId?: string;
  choices: ExecutionInputSuggestionChoice[]; disabled?: boolean; snapshot?: boolean;
  production?: { runId: string; scope: { kind: string; id: string } } }>()
const row = defineModel<ExecutionRequestDraft['literals'][string]>({ required: true })
const emit = defineEmits<{ busy: [value: boolean] }>()
const preview = ref<ExecutionInputSuggestion | null>(null), error = ref(''), loading = ref(false)
const projects = ref<SeriesProject[]>([]), selectedProject = ref(''), projectsLoaded = ref(false)
const effectiveProject = computed(()=>props.projectId || (props.snapshot ? selectedProject.value : undefined))
let request = 0
let prefillAttempted = false
async function loadProjects() {
  if (props.disabled || loading.value) return
  const current = ++request
  loading.value = true; emit('busy',true); error.value = ''
  try {
    const rows = await repository.getProjects({ownedOnly:true})
    if (current === request) { projects.value = rows; projectsLoaded.value = true }
  } catch(e) { if(current === request) error.value = apiErrorSummary(e) }
  finally { if(current === request) { loading.value=false; emit('busy',false) } }
}
async function show(choice: ExecutionInputSuggestionChoice, prefill = false) {
  if (props.disabled || loading.value || (choice.requires_project && !effectiveProject.value)) return
  const current = ++request
  loading.value = true; emit('busy', true); error.value = ''; preview.value = null
  try {
    const initialValue = row.value.value
    const result = await executionRegistryApi.inputSuggestion(props.versionId, props.inputKey, choice.key, effectiveProject.value,
      choice.requires_production_scope ? props.production : undefined)
    if (current !== request) return
    if (result.input_key !== props.inputKey || result.definition_sha256 !== props.definitionSha256) throw new Error('输入合同已变化，请重新选择执行实例')
    if (prefill && initialValue === '' && row.value.value === '' && !row.value.reference) {
      applyExecutionInputSuggestion(row.value, result, {sha256:props.definitionSha256}, props.inputKey)
    } else preview.value = result
  } catch (e) { if (current === request) error.value = apiErrorSummary(e) }
  finally { if (current === request) { loading.value = false; emit('busy', false) } }
}
function apply() {
  if (props.disabled || loading.value || !preview.value) return
  try {
    applyExecutionInputSuggestion(row.value, preview.value, { sha256: props.definitionSha256 }, props.inputKey)
    preview.value = null; error.value = ''
  } catch (e) { error.value = apiErrorSummary(e) }
}
function clearPreview() { request++; loading.value = false; emit('busy', false); preview.value = null; error.value = '' }
// Compare the actual identities, not a freshly created array/context object on
// each parent busy render. An unchanged context must keep its in-flight read.
watch([() => props.versionId, () => props.definitionSha256, () => props.inputKey, effectiveProject,
  () => props.production?.runId, () => props.production?.scope.kind, () => props.production?.scope.id], () => {
  clearPreview()
  prefillAttempted = false
  // Keep typed text, but don't claim its old receipt belongs to the new context.
  if (row.value.reference) { delete row.value.reference; error.value = '项目或实例已变化，已保留填写内容；如需记录引用来源，请重新引用。' }
})
watch(() => [props.disabled, props.choices, props.production?.runId, props.production?.scope.kind,
  props.production?.scope.id, effectiveProject.value], () => {
  const choices = props.choices.filter(choice => choice.prefill && choice.requires_production_scope)
  if (prefillAttempted || props.disabled || loading.value || props.snapshot || !props.production || !effectiveProject.value
    || choices.length !== 1 || row.value.value !== '' || row.value.reference) return
  prefillAttempted = true
  void show(choices[0]!, true)
}, {immediate:true})
onBeforeUnmount(clearPreview)
</script>

<template>
  <div class="input-suggestions">
    <template v-if="snapshot && !projectId">
      <button type="button" class="button ghost small" :disabled="disabled || loading" @click="loadProjects">{{ projectsLoaded ? '刷新来源项目' : '选择来源项目' }}</button>
      <label v-if="projectsLoaded" class="field">来源项目<select v-model="selectedProject" :disabled="disabled || loading"><option value="">请选择项目</option><option v-for="project in projects" :key="project.id" :value="project.id">{{ project.title }}</option></select></label>
      <p v-if="projectsLoaded && !projects.length" class="muted">当前账号没有可引用的项目。请先创建项目，或选择声明“本次填写”来源的执行实例。</p>
    </template>
    <div class="suggestion-actions">
      <button v-for="choice in choices" :key="choice.key" type="button" class="button ghost small"
        :disabled="disabled || loading || (choice.requires_project && !effectiveProject) || (choice.requires_production_scope && !production)" @click="show(choice)">{{ choice.label }}</button>
    </div>
    <p v-if="snapshot" class="muted">引用项目已保存的事实。需先预览确认；不会读取完整剧本，也不会调用模型。如需修改本次副本，请选择策略声明“本次填写”的实例。</p>
    <p v-else-if="choices.some(choice => choice.requires_project) && !projectId" class="muted">选择项目后可以引用项目内容；也可直接填写。</p>
    <p v-if="loading" role="status">正在读取参考内容…</p>
    <p v-if="production && choices.some(choice=>choice.prefill)" class="muted">空白输入自动填入本轮已审核 Bible 描述；请在下方审核和修改。不会覆盖已填写内容，不会调用模型。</p>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <section v-if="preview" class="suggestion-preview" aria-label="输入参考预览">
      <strong>{{ preview.label }}<template v-if="preview.origin.source_name"> · {{ preview.origin.source_name }}</template></strong>
      <p class="muted">{{ snapshot ? '核对下方项目内容，确认后保存这一份快照。之后项目改变也不会替换它。' : preview.origin.selector?.toString().startsWith('example:') ? '以下仅为填写范例，不是自动默认值。请按你的故事修改。' : '以下是本次读取的已保存正文。填入后可以修改和补充，不会改动项目原内容。' }}</p>
      <p v-if="preview.origin.project_name" class="muted">来源项目：{{ preview.origin.project_name }} · 来源更新时间：{{ preview.origin.source_updated_at }}</p>
      <p v-if="preview.origin.location" class="muted">维护位置：{{ preview.origin.location }}</p>
      <p v-if="preview.origin.note" class="muted">{{ preview.origin.note }}</p>
      <pre>{{ preview.content }}</pre>
      <p v-if="typeof row.value === 'string' && row.value.length" class="platform-alert warning">下方已有填写内容。确认替换才会覆盖；关闭预览可保留原内容。</p>
      <div class="suggestion-actions"><button type="button" class="button primary small" :disabled="disabled" @click="apply">{{ snapshot ? '确认引用此项目内容' : typeof row.value === 'string' && row.value.length ? '确认替换下方内容' : '填入下方，可继续修改' }}</button>
        <button type="button" class="button ghost small" @click="preview = null">关闭预览</button></div>
    </section>
    <p v-if="row.reference" class="source-note">已引用：{{ row.reference.label }} · {{ row.value === row.reference.content ? '尚未修改' : '已在引用基础上修改' }}。创建准备时保存此来源及下方实际内容，不随项目后续修改自动更新。</p>
    <section v-if="snapshot && row.reference" class="suggestion-preview" aria-label="已确认项目输入"><pre>{{ row.reference.content }}</pre></section>
    <details v-else-if="row.reference" class="suggestion-preview"><summary>查看引用原文（与本次修改对照）</summary><pre>{{ row.reference.content }}</pre></details>
  </div>
</template>

<style scoped>
.input-suggestions{display:grid;gap:8px;min-width:0}.suggestion-actions{display:flex;gap:8px;flex-wrap:wrap}.suggestion-preview{display:grid;gap:10px;padding:12px;border:1px solid var(--border);border-radius:8px;background:var(--bg-secondary);min-width:0}.suggestion-preview pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:220px;overflow:auto;font:inherit;line-height:1.8;margin:0}.muted,.source-note{color:var(--text-muted);font-size:12px;line-height:1.7;margin:0}
</style>
