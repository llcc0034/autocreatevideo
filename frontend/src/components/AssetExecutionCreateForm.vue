<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { AssetGenerationJob, AssetGenerationJobType, AssetGenerationMediaType, AssetGenerationOption, BibleDraftEntityType } from '@/types'
import { repository } from '@/services/repository'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import { adaptExecutionConfiguration, type ExecutionConfiguration } from '@/services/executionRegistry'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { isDefinitiveCreationRejection } from '@/services/productionOnboarding'
import { buildExecutionRequestFixture, emptyExecutionRequestDraft, executionRequestFormContract,
  type ExecutionRequestDraft, type ExecutionRequestFormContract } from '@/services/executionRequestForm'
import type { AssetGenerationJobCreateInput } from '@/services/assetGenerationPayload'
import ExecutionRequestInputEditor from './ExecutionRequestInputEditor.vue'
import AssetAppearanceTargetEditor from './AssetAppearanceTargetEditor.vue'
import { appearanceTargetErrors, type AssetAppearanceTarget } from '@/services/assetAppearanceTarget'
import { readMediaAppearanceInputOptions, type MediaAppearanceInputOptions } from '@/services/mediaAppearanceInputs'
import MediaAppearanceInputDirectory from './MediaAppearanceInputDirectory.vue'
import RegistryResourceExplorer from './RegistryResourceExplorer.vue'
import ConfigurationDiscardNotice from './ConfigurationDiscardNotice.vue'
import { mediaKindLabel } from '@/services/presentationLabels'

const props = defineProps<{
  mediaType: AssetGenerationMediaType
  metadata: { name: string; description: string; styleId: string; imageCategory: BibleDraftEntityType | 'reference' }
  promptCandidate?: string
}>()
const emit = defineEmits<{ created: [job: AssetGenerationJob]; busy: [v: boolean]; dirty: [v: boolean]; locked: [v: boolean]; unknown: [v: boolean] }>()
const jobType = ref<AssetGenerationJobType>('asset')
const options = ref<Awaited<ReturnType<typeof repository.getAssetGenerationOptions>> | null>(null), page = ref(1)
const selected = ref<AssetGenerationOption | null>(null), selectedId = ref('')
const contract = ref<ExecutionRequestFormContract | null>(null), draft = ref<ExecutionRequestDraft | null>(null)
const configuration = ref<ExecutionConfiguration | null>(null)
const optionsLoading = ref(false), contractLoading = ref(false), submitting = ref(false), uploading = ref(false)
const error = ref(''), optionsError = ref(''), inputErrors = ref<string[]>([]), reviewed = ref(false)
const allowExternal = ref(false), candidateChannel = ref(''), initial = ref('null')
const pending = ref<AssetGenerationJobCreateInput | null>(null)
const recovering = ref(false), recoveryMessage = ref('')
type InputChange = { kind: 'instance'; id: string } | { kind: 'purpose'; value: AssetGenerationJobType } | { kind: 'candidate'; channel: string; text: string }
const inputChange = ref<InputChange | null>(null)
const changeMessage = computed(() => inputChange.value?.kind === 'candidate'
  ? `替换 ${inputChange.value.channel} 的原正文；其他输入与授权不变。候选只填入当前表单，不会创建任务。`
  : '切换会清空本次实例输入、外观目标和调用授权；素材名称、描述和上次生成的候选保留。尚未提交任何新请求。')
const appearanceTarget = ref<AssetAppearanceTarget | null>(null)
const primaryAssetId = ref<string | null>(null)
const appearanceInputs = ref<MediaAppearanceInputOptions | null>(null)
const appearanceErrors = computed(() => draft.value ? appearanceTargetErrors(appearanceTarget.value, draft.value, appearanceInputs.value, primaryAssetId.value) : [])
let optionSequence = 0, contractSequence = 0, submitSequence = 0
onBeforeUnmount(() => { optionSequence++; contractSequence++; submitSequence++ })
const busy = computed(() => submitting.value || uploading.value || contractLoading.value || recovering.value)
const requestLocked = computed(() => busy.value || !!pending.value)
const locked = computed(() => requestLocked.value || !!inputChange.value)
const dirty = computed(() => !!pending.value || JSON.stringify(draft.value) !== initial.value || allowExternal.value || !!appearanceTarget.value)
const visibleOptions = computed(() => {
  const rows = options.value?.results ?? []
  return selected.value && !rows.some(row => row.versionId === selected.value!.versionId) ? [selected.value, ...rows] : rows
})
// A standalone asset request has no ProductionRun authority. Keep the actual
// source requirement visible instead of replacing reviewed content with text.
const productionSourceInputs = computed(() => Object.entries(draft.value?.contexts ?? {}).flatMap(([key, source]) =>
  draft.value?.literals[key]?.enabled && source && contract.value?.contextFields.some(field =>
    field.selector_key === source.selectorKey && field.revision_kind === 'visual_rules_approval')
    ? [contract.value?.inputForms?.[key]?.label || key] : []))
watch(busy, v => emit('busy', v))
watch(locked, v => emit('locked', v))
watch(dirty, v => emit('dirty', v))
watch(() => !!pending.value && !submitting.value, value => emit('unknown', value))
watch([draft, allowExternal, appearanceTarget, () => props.metadata], () => { if (!pending.value) reviewed.value = false }, { deep: true })

async function loadOptions(targetPage = 1) {
  const sequence = ++optionSequence; optionsLoading.value = true; optionsError.value = ''; options.value = null
  try {
    const result = await repository.getAssetGenerationOptions(jobType.value, props.mediaType, targetPage)
    if (sequence !== optionSequence) return
    options.value = result; page.value = targetPage
  } catch (cause) { if (sequence === optionSequence) optionsError.value = apiErrorSummary(cause) }
  finally { if (sequence === optionSequence) optionsLoading.value = false }
}
async function choose(id: string, event?: Event) {
  // Native selection changes before confirmation. Restore it even when the
  // choice is rejected without a reactive state change to trigger rendering.
  if (event) (event.target as HTMLSelectElement).value = selectedId.value
  if (locked.value || id === selectedId.value) return
  if (id && !visibleOptions.value.some(row => row.versionId === id)) { error.value = '所选实例不在当前目录中，请刷新目录后明确选择。'; return }
  if (dirty.value) { inputChange.value = { kind: 'instance', id }; return }
  await applyChoice(id)
}
async function applyChoice(id: string) {
  const option = visibleOptions.value.find(row => row.versionId === id)
  if (id && !option) { error.value = '所选实例不在当前目录中，请刷新目录后明确选择。'; return }
  selected.value = option ?? null; selectedId.value = id
  contract.value = null; configuration.value = null; draft.value = null; initial.value = 'null'; error.value = ''
  appearanceTarget.value = null
  primaryAssetId.value = null
  appearanceInputs.value = null
  allowExternal.value = false; reviewed.value = false; inputErrors.value = []; candidateChannel.value = ''
  contractSequence++
  await retryContract()
}
async function retryContract() {
  // Retry only an absent contract, never discard an existing editable draft or
  // alter a pending idempotent submission. Reuse the exact catalog identity.
  if (locked.value || contract.value || !selected.value) return
  const option = selected.value, id = selectedId.value
  if (id !== option.versionId) return
  const sequence = ++contractSequence
  error.value = ''
  contractLoading.value = true
  try {
    const raw = await executionRegistryApi.contract(id)
    const result = executionRequestFormContract(raw)
    const directory = jobType.value === 'asset' && props.mediaType === 'image' ? readMediaAppearanceInputOptions(raw.appearance_inputs) : null
    if (sequence !== contractSequence) return
    if (result.versionId !== id || result.sha256 !== option.definitionSha256 || result.operation !== option.operation || result.promptMode !== option.promptMode) {
      throw new Error('输入合同与选定实例目录不一致，请重新选择')
    }
    configuration.value = adaptExecutionConfiguration(raw.definition.configuration)
    appearanceInputs.value = directory
    contract.value = result; draft.value = emptyExecutionRequestDraft(result); initial.value = JSON.stringify(draft.value)
  } catch (cause) { if (sequence === contractSequence) error.value = apiErrorSummary(cause) }
  finally { if (sequence === contractSequence) contractLoading.value = false }
}
async function changeJobType(value: AssetGenerationJobType) {
  if (locked.value || value === jobType.value) return
  if (dirty.value) { inputChange.value = { kind: 'purpose', value }; return }
  await applyJobType(value)
}
async function applyJobType(value: AssetGenerationJobType) {
  contractSequence++; contract.value = null; configuration.value = null; draft.value = null; initial.value = 'null'
  selected.value = null; selectedId.value = ''; allowExternal.value = false; reviewed.value = false
  appearanceTarget.value = null
  primaryAssetId.value = null
  appearanceInputs.value = null
  candidateChannel.value = ''
  jobType.value = value; error.value = ''; inputErrors.value = []; await loadOptions()
}
function applyCandidate() {
  if (locked.value || !draft.value || contract.value?.promptMode !== 'manual' || !props.promptCandidate
    || !contract.value.channels.some(row => row.key === candidateChannel.value)) return
  if (draft.value.channels[candidateChannel.value]) {
    inputChange.value = { kind: 'candidate', channel: candidateChannel.value, text: props.promptCandidate }; return
  }
  draft.value.channels[candidateChannel.value] = props.promptCandidate
}
async function confirmChange() {
  if (requestLocked.value || !inputChange.value) return
  const change = inputChange.value; inputChange.value = null
  if (change.kind === 'instance') await applyChoice(change.id)
  else if (change.kind === 'purpose') await applyJobType(change.value)
  else if (draft.value && contract.value?.promptMode === 'manual' && contract.value.channels.some(row => row.key === change.channel)) draft.value.channels[change.channel] = change.text
}
async function submit() {
  if (busy.value || inputChange.value || !contract.value || !draft.value || (!pending.value && (!reviewed.value || inputErrors.value.length || appearanceErrors.value.length || productionSourceInputs.value.length))) return
  if (pending.value) { await readUnknown(); return }
  const sequence = ++submitSequence
  error.value = ''
  try {
    if (!pending.value) {
      if (appearanceTarget.value && (jobType.value !== 'asset' || props.mediaType !== 'image')) throw new Error('只有图片素材任务可声明外观目标')
      if (jobType.value === 'asset' && !props.metadata.name.trim()) throw new Error('请填写素材名称')
      if (jobType.value === 'asset' && props.mediaType === 'image' && props.metadata.imageCategory !== 'reference' && !props.metadata.description.trim()) {
        throw new Error('人物、场景和道具图片必须填写基础描述')
      }
      pending.value = {
        jobType: jobType.value, mediaType: props.mediaType, executionInstanceVersionId: contract.value.versionId,
        expectedDefinitionSha256: contract.value.sha256, fixture: buildExecutionRequestFixture(contract.value, draft.value),
        name: props.metadata.name, description: props.metadata.description, styleId: props.metadata.styleId || undefined,
        imageCategory: props.metadata.imageCategory, allowExternalCalls: allowExternal.value,
        idempotencyKey: crypto.randomUUID(),
        ...(appearanceTarget.value ? { appearanceTarget: JSON.parse(JSON.stringify(appearanceTarget.value)) as AssetAppearanceTarget } : {}),
      }
    }
    submitting.value = true
    const job = await repository.createAssetGenerationJob(pending.value)
    if (sequence !== submitSequence) return
    if (!matchesRequest(job, pending.value)) throw new Error('返回任务与原请求身份不一致，请只读核对原请求；不会重新创建。')
    pending.value = null; submitting.value = false; emit('busy',false); emit('locked',false); emit('dirty',false)
    emit('created', job)
  } catch (cause) {
    if (sequence !== submitSequence) return
    error.value = apiErrorSummary(cause)
    if (pending.value && isDefinitiveCreationRejection(cause)) { pending.value = null; reviewed.value = false }
  } finally { if (sequence === submitSequence) submitting.value = false }
}
function matchesRequest(job: AssetGenerationJob, request: AssetGenerationJobCreateInput) {
  return job.executionInstanceVersionId === request.executionInstanceVersionId && job.jobType === request.jobType
    && job.mediaType === request.mediaType && job.preparation?.requestKey === request.idempotencyKey
}
async function readUnknown() {
  if (busy.value || !pending.value) return
  const request = pending.value, sequence = ++submitSequence
  recovering.value = true; error.value = ''; recoveryMessage.value = '正在只读查找原请求及其任务回执…'
  try {
    const rows = (await repository.getAssetGenerationJobs()).filter(job => job.preparation?.requestKey === request.idempotencyKey)
    if (sequence !== submitSequence) return
    if (rows.length !== 1) {
      recoveryMessage.value = rows.length ? '同一请求出现多个记录，不能自动确认；原输入与请求键保留。' : '尚未查到原请求。不能判定创建失败；原输入与请求键保留，不重发创建。'
      return
    }
    const job = await repository.getAssetGenerationJob(rows[0]!.id)
    if (sequence !== submitSequence) return
    if (job.id !== rows[0]!.id || !matchesRequest(job, request)) throw new Error('任务详情与原请求身份不一致，未采用回执。')
    pending.value = null; recovering.value = false; emit('busy', false); emit('locked', false); emit('dirty', false); emit('unknown', false)
    emit('created', job)
  } catch (cause) { if (sequence === submitSequence) { error.value = apiErrorSummary(cause); recoveryMessage.value = '核对失败；原请求保留，不重新创建。' } }
  finally { if (sequence === submitSequence) recovering.value = false }
}
onMounted(() => { void loadOptions() })
</script>

<template>
  <section class="asset-execution-form" :aria-busy="busy">
    <ConfigurationDiscardNotice v-if="inputChange" title="更改本次输入前请确认" :message="changeMessage" :busy="requestLocked" :action-label="inputChange.kind==='candidate'?'替换选定通道正文':'清空本次输入并切换'" @keep="inputChange=null" @discard="confirmChange"/>
    <RegistryResourceExplorer label="素材生成表单" :inert="!!inputChange" v-slot="{ inspect }">
    <section class="generation-section" aria-label="选择生成能力">
    <h4>1 · 选择生成能力</h4>
    <p>先选择执行实例版本，再填写它声明的素材、参数和提示词。目录可选不等于本次输入已通过验收；任务创建后统一检查实际规格。</p>
    <div class="form-actions"><button type="button" class="button ghost" :aria-pressed="jobType==='asset'" :disabled="locked" @click="changeJobType('asset')">生成素材</button><button type="button" class="button ghost" :aria-pressed="jobType==='prompt'" :disabled="locked" @click="changeJobType('prompt')">生成提示词候选</button></div>
    <p v-if="jobType==='prompt'">候选是独立文本产物，不会自动替换另一实例的策略字段或最终提示词。</p>
    <p v-if="optionsLoading" role="status">正在读取实例目录…</p>
    <p v-if="optionsError" class="platform-alert danger" role="alert">{{ optionsError }}</p>
    <label>执行实例版本<select :value="selectedId" :disabled="locked || optionsLoading" @change="choose(($event.target as HTMLSelectElement).value, $event)">
      <option value="">请选择，不自动采用默认实例</option><option v-for="row in visibleOptions" :key="row.versionId" :value="row.versionId">{{ row.name }} · v{{ row.version }} · {{ row.operation }}{{ row.mode ? ' / '+row.mode : '' }}</option>
    </select></label>
    <div class="form-actions"><button type="button" class="button ghost small" :disabled="locked || optionsLoading" @click="loadOptions(page)">刷新目录</button><template v-if="options"><span>共 {{ options.count }} 个版本 · 第 {{ page }} 页</span><button type="button" class="button ghost small" :disabled="locked || optionsLoading || !options.previous" @click="loadOptions(page-1)">上一页</button><button type="button" class="button ghost small" :disabled="locked || optionsLoading || !options.next" @click="loadOptions(page+1)">下一页</button></template></div>
    <p v-if="options && !options.count">暂无匹配产物的已发布实例。请在系统设置配置并发布对应执行实例；此处不回退到基础运行资源。</p>
    <p v-if="contractLoading" role="status">正在读取精确版本输入合同…</p>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <button v-if="selected && !contract" type="button" class="button ghost small" :disabled="uploading || submitting || !!pending" :aria-disabled="contractLoading" @click="retryContract">{{ contractLoading ? '正在读取输入合同…' : '重试读取所选实例输入合同' }}</button>
    <button v-if="selected" type="button" class="button ghost small" @click="inspect('execution',selected.versionId,$event)">查看所选实例定义、实现与使用方</button>
    </section>
    <template v-if="contract && draft">
      <section class="generation-section" aria-label="本次输入与处理">
      <h4>2 · 本次输入与处理</h4>
      <p>这里只填写本次任务的值，不修改执行实例、字段定义或底层工作流。参数、必需项和来源限制均来自所选版本合同。</p>
      <template v-if="contract.promptMode==='profile'">
        <p>本次输入 → 策略字段处理 → 提示词通道 → 主模型。按已绑定策略保留、生成、翻译或组装；不需要在这里再填写一份完整提示词。</p>
        <button v-if="configuration?.prompt_profile_version_id" type="button" class="button ghost small" @click="inspect('prompt',configuration.prompt_profile_version_id,$event)">查看本次引用的策略与字段规则</button>
        <p v-else role="status">合同未声明策略版本，不能推断处理规则；请查看所选实例定义。</p>
        <ul v-if="configuration?.auxiliaries.length" class="generation-auxiliaries" aria-label="策略绑定的辅助能力">
          <li v-for="auxiliary in configuration.auxiliaries" :key="auxiliary.purpose"><span>{{ auxiliary.purpose }}</span><button type="button" class="button ghost small" @click="inspect('execution',auxiliary.target_version_id,$event)">查看此用途的辅助执行实例</button></li>
        </ul>
        <p v-else>此版本未配置辅助执行实例；是否需要生成或翻译以策略规则为准，不会自动寻找其他模型。</p>
        <p>以上是配置引用，不是已执行结果。创建后按策略需要准备字段；审核与采用按配置处理，仅缺失或待处理内容需要到准备工作台继续。</p>
      </template>
      <p v-else-if="contract.promptMode==='manual'">此实例接收完整人工文本通道，不会自动使用辅助 AI。可明确采用已有候选；需要策略或辅助生成时，请选择已配置相应能力的实例。</p>
      <p v-else>此实例明确不使用提示词，只提供下方合同声明的输入与参数。</p>
      <div v-if="productionSourceInputs.length" class="platform-alert warning source-boundary" role="alert">
        <p>当前输入“{{ productionSourceInputs.join('、') }}”需要生产轮次中的已审核输出。此独立素材入口不提供生产审核上下文，不能创建该请求。</p>
        <p>请在<a href="/projects">连续剧项目</a>的对应制作步骤使用此实例；或明确选择适用于独立素材的实例。仅可选输入可取消提供，不会自动改选实例、复制审核正文或读取整份剧本。</p>
      </div>
      <details v-if="promptCandidate"><summary>上次生成的提示词候选（仅人工通道可明确采用）</summary><pre>{{ promptCandidate }}</pre><template v-if="contract.promptMode==='manual'"><label>放入人工通道<select v-model="candidateChannel" :disabled="locked"><option value="">请选择目标通道</option><option v-for="row in contract.channels" :key="row.key" :value="row.key">{{ row.key }}</option></select></label><button type="button" class="button ghost small" :disabled="locked || !candidateChannel" @click="applyCandidate">采用到选定通道</button></template><p v-else>当前不是完整人工通道模式，候选不会自动注入。策略字段按原规则准备；无需提示词模式不接收此候选。</p></details>
      <AssetAppearanceTargetEditor v-if="jobType==='asset' && mediaType==='image'" v-model="appearanceTarget" :disabled="locked" :draft="draft" :options="appearanceInputs" @basis="primaryAssetId=$event"/>
      <MediaAppearanceInputDirectory v-if="jobType==='asset' && mediaType==='image' && appearanceInputs" :options="appearanceInputs" :labels="contract.inputForms || {}"/>
      <ExecutionRequestInputEditor v-model="draft" :contract="contract" :disabled="locked" @busy="uploading=$event" @validity="inputErrors=$event"/>
      <ul v-if="inputErrors.length" role="alert"><li v-for="item in inputErrors" :key="item">{{ item }}</li></ul>
      <ul v-if="appearanceErrors.length" role="alert"><li v-for="item in appearanceErrors" :key="item">{{ item }}</li></ul>
      </section>
      <section class="generation-section" aria-label="输出与创建任务">
      <h4>3 · 输出与创建任务</h4>
      <dl class="generation-output"><div><dt>合同输出</dt><dd>{{ mediaKindLabel(contract.outputKind) }}</dd></div><div><dt>成功结果去向</dt><dd>{{ jobType==='prompt'?'独立提示词候选；需明确选择目标通道，不会自动替换已有正文或策略字段。':'素材库；任务成功并完成输出校验后才产生素材，不以请求登记代替生成成功。' }}</dd></div></dl>
      <details><summary>本次引用的确切版本与合同</summary><p>实例版本 {{ contract.versionId }}</p><p>定义 SHA-256 {{ contract.sha256 }}</p><p>执行能力 {{ contract.operation }}{{ contract.mode ? ' / '+contract.mode : '' }}</p></details>
      <p>预算、调用次数与输出额度由系统资源策略统一管理。超过提醒值时保留准备，等待人工确认；不会自动重试。</p>
      <p>创建后先检查准备条件；条件满足时任务可实际执行。下方外部调用选项不是本地模型的暂停开关。</p>
      <label class="request-check"><input v-model="allowExternal" type="checkbox" :disabled="locked"/>允许本次输入发送到外部模型，可能产生实际费用</label>
      <label class="request-check"><input v-model="reviewed" type="checkbox" :disabled="locked"/>已核对实例、输入与授权，确认创建任务</label>
      <div v-if="pending && !submitting" class="platform-alert warning" role="status"><p>创建结果尚未确认，任务可能已登记。原输入已锁定；请先只读核对，不要关闭或刷新表单后重复创建。</p><p>原请求标识 {{ pending.idempotencyKey }}</p><p v-if="recoveryMessage">{{ recoveryMessage }}</p><a href="/workflows?category=asset" target="_blank" rel="noopener">查看素材任务（新窗口）</a></div>
      <button type="button" class="button primary" :disabled="busy || (!pending && (!reviewed || !!inputErrors.length || !!appearanceErrors.length || !!productionSourceInputs.length))" @click="submit">{{ submitting?'正在创建…':recovering?'正在只读核对…':pending?'只读核对原请求':'创建生成任务' }}</button>
      </section>
    </template>
    </RegistryResourceExplorer>
  </section>
</template>

<style scoped>
.asset-execution-form{display:grid;grid-template-columns:minmax(0,1fr);gap:var(--config-space-lg);min-width:0;font:var(--config-body-size)/1.65 var(--font)}.asset-execution-form p,.asset-execution-form label{font-size:var(--config-body-size);line-height:1.7;overflow-wrap:anywhere;margin:0}.asset-execution-form label{display:grid;gap:6px;min-width:0}.asset-execution-form select{min-width:0;max-width:100%}.form-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.request-check{display:flex!important;align-items:flex-start;gap:8px}.request-check input{width:auto;margin-top:5px}.asset-execution-form pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:260px;overflow:auto}
.generation-section{display:grid;gap:var(--config-space-md);min-width:0;padding:var(--config-space-lg) 0}.generation-section+.generation-section{border-top:1px solid var(--border)}.generation-section h4{font-size:var(--config-section-size);font-weight:600;margin:0}.generation-output{display:grid;gap:var(--config-space-md);margin:0}.generation-output dt{font-weight:600}.generation-output dd{margin:0;overflow-wrap:anywhere}.generation-section .button{font-size:var(--config-body-size);min-height:var(--config-control-height);white-space:normal;max-width:100%}.generation-section>button{justify-self:start}.generation-section summary{min-height:36px;align-content:center;cursor:pointer}.generation-section details p{font-size:var(--config-help-size)}.generation-section :focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:760px){.generation-section .button,.generation-section select{min-height:44px}}
.source-boundary{display:grid;gap:6px;min-width:0}.source-boundary p{margin:0}
.generation-auxiliaries{margin:0;padding-left:20px;display:grid;gap:var(--config-space-sm)}.generation-auxiliaries li{min-width:0;overflow-wrap:anywhere}.generation-auxiliaries button{margin-left:8px}
</style>
