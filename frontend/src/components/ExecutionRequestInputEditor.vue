<script setup lang="ts">
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import type { Asset } from '@/types'
import type { ExecutionInput, ExecutionScalar } from '@/services/executionRegistry'
import { executionRuleDescription, type ExecutionRequestDraft, type ExecutionRequestFormContract, type ExecutionSelectedMediaSource, type ExecutionReviewedShotSource } from '@/services/executionRequestForm'
import ExactReferencePreview from './ExactReferencePreview.vue'
import ExecutionScalarEditor from './ExecutionScalarEditor.vue'
import ExecutionParameterEvidence from './ExecutionParameterEvidence.vue'
import ExecutionInputAssetPicker from './ExecutionInputAssetPicker.vue'
import StructuredJsonEditor from './StructuredJsonEditor.vue'
import ExecutionContextFieldPicker from './ExecutionContextFieldPicker.vue'
import ExecutionReviewedVisualSource from './ExecutionReviewedVisualSource.vue'
import ExecutionInputSuggestions from './ExecutionInputSuggestions.vue'
import CatalogChoiceField from './CatalogChoiceField.vue'
import ConfigurationDiscardNotice from './ConfigurationDiscardNotice.vue'
import { jsonAllowedTypes } from '@/services/structuredInputForm'
import { inputSourcePolicies, type InputSourceCatalog } from '@/services/inputSourcePolicies'
import { apiErrorSummary } from '@/services/apiErrorPresentation'

const props = withDefaults(defineProps<{ contract: ExecutionRequestFormContract<string | null>; disabled?: boolean; projectId?: string; runId?: string; stageKey?: string; productionScope?: { kind: string; id: string }; selectedSources?: ExecutionSelectedMediaSource[]; reviewedShotSources?: ExecutionReviewedShotSource[]; managedInputKeys?: string[] }>(), { managedInputKeys: () => [] })
const draft = defineModel<ExecutionRequestDraft>({ required: true })
const emit = defineEmits<{ busy: [value: boolean]; validity: [errors: string[]] }>()
const assetNames = ref<Record<string, string>>({})
const literalErrors = ref<Record<string, string[]>>({})
const busySources = ref<Record<string, boolean>>({})
type InputChange = { kind: 'source'; key: string; value: string } | { kind: 'media'; key: string; itemKey: string } | { kind: 'subject'; id: string }
const pendingChange = ref<(InputChange & { message: string }) | null>(null)
const inputsLocked = computed(() => !!props.disabled || !!pendingChange.value)
const requestBusy = computed(() => Object.values(busySources.value).some(Boolean))
function pairedUses(itemKeys: string[]) {
  return Object.entries(draft.value.media).flatMap(([key, rows]) => rows.flatMap((row, index) => row.pairedTo && itemKeys.includes(row.pairedTo) ? [`${inputLabel(key)}第 ${index + 1} 项`] : []))
}
function pairingImpact(itemKeys: string[]) {
  const uses = pairedUses(itemKeys)
  return uses.length ? `受影响的配对：${uses.join('、')}。这些输入保留，但需要重新选择父素材；不会自动改配到其他视频。` : '没有其他输入配对此项。'
}
function missingParent(spec: ExecutionInput, itemKey: string | null) {
  return !!itemKey && !!spec.semantic_contract.paired_with_input_key && !draft.value.media[spec.semantic_contract.paired_with_input_key]?.some(row => row.itemKey === itemKey)
}
function applyPendingChange() {
  if (props.disabled || requestBusy.value || !pendingChange.value) return
  const change = pendingChange.value
  if (change.kind === 'source') {
    const spec = props.contract.inputs.find(row => row.key === change.key)
    if (!spec?.media_source_policy?.allow_override || !sourceModes(spec).some(row => row.value === change.value)) {
      pendingChange.value = null
      sourceError.value = '来源配置已变化，未更换选择；请重新读取并核对来源。'
      return
    }
    draft.value.mediaSourceModes = { ...draft.value.mediaSourceModes, [change.key]: change.value }
    draft.value.media[change.key] = []
  } else if (change.kind === 'media') {
    draft.value.media[change.key] = draft.value.media[change.key]!.filter(row => row.itemKey !== change.itemKey)
  } else {
    draft.value.subjects = draft.value.subjects.filter(row => row.id !== change.id)
    for (const rows of Object.values(draft.value.media)) for (const row of rows) if (row.subjectId === change.id) row.subjectId = null
  }
  pendingChange.value = null
}
const sourceCatalog=ref<InputSourceCatalog|null>(null),sourceError=ref(''),sourceLoading=ref(false)
let sourceSequence=0
const hasSourcePolicies=computed(()=>props.contract.inputs.some(row=>row.media_source_policy))
async function loadSources(){const token=++sourceSequence;sourceLoading.value=true;sourceError.value='';try{const result=await inputSourcePolicies.options();if(token===sourceSequence)sourceCatalog.value=result}catch(cause){if(token===sourceSequence)sourceError.value=apiErrorSummary(cause)}finally{if(token===sourceSequence)sourceLoading.value=false}}
onMounted(()=>{if(hasSourcePolicies.value)void loadSources()})
watch(hasSourcePolicies,value=>{if(value&&!sourceCatalog.value&&!sourceLoading.value)void loadSources()})
onBeforeUnmount(()=>{sourceSequence++})
const mediaMode=(spec:ExecutionInput)=>spec.media_source_policy?draft.value.mediaSourceModes?.[spec.key]:undefined
function sourceModes(spec:ExecutionInput){return (sourceCatalog.value?.sources||[]).filter(row=>row.available&&row.kinds.includes(spec.kind)&&spec.media_source_policy?.allowed_sources.includes(row.value))}
function changeMediaMode(spec:ExecutionInput,event:Event){
  const select=event.target as HTMLSelectElement, previous=mediaMode(spec)
  const value=select.value
  select.value=previous||''
  if(inputsLocked.value||requestBusy.value||!spec.media_source_policy?.allow_override
    || value===previous || !sourceModes(spec).some(row=>row.value===value))return
  const rows=draft.value.media[spec.key]||[]
  if(rows.length){pendingChange.value={kind:'source',key:spec.key,value,message:`更换「${inputLabel(spec.key)}」的来源将移除本次 ${rows.length} 项选择；素材库文件不删除。${pairingImpact(rows.map(row=>row.itemKey))}`};return}
  draft.value.mediaSourceModes={...draft.value.mediaSourceModes,[spec.key]:value}
  draft.value.media[spec.key]=[]
}
function inputBusy(key: string, value: boolean) { busySources.value[key]=value; emit('busy',Object.values(busySources.value).some(Boolean)) }
function contextOptions(spec: ExecutionInput) { return props.contract.contextFields.filter(row=>row.kind===spec.kind
  && props.contract.sourceConstraints[spec.key]!.kinds.includes('context_field')
  && (props.contract.sourceConstraints[spec.key]!.selectors === null || props.contract.sourceConstraints[spec.key]!.selectors!.includes(row.selector_key))
  && ['script_revision','visual_rules_approval'].includes(row.revision_kind) && row.scope_kind==='project' && spec.semantic_contract.allowed_scope_kinds.includes('project')
  && (spec.kind!=='json' || !props.contract.structureSchemas?.[spec.key]?.some(schema=>schema.type)
    || !!row.value_schema?.type && jsonAllowedTypes([...(props.contract.structureSchemas?.[spec.key] || []),row.value_schema]).length>0)) }
function literalAllowed(spec: ExecutionInput) { return props.contract.sourceConstraints[spec.key]!.kinds.includes('literal') }
function projectSnapshot(spec: ExecutionInput) { return props.contract.sourceConstraints[spec.key]!.kinds.includes('project_snapshot') }
function suggestions(key: string) { return (props.contract.inputForms?.[key]?.suggestions ?? []).filter(choice =>
  !choice.requires_production_scope || props.stageKey === 'bible_image_generation' && props.runId
    && props.productionScope && ['character','scene','prop'].includes(props.productionScope.kind)) }
function contextSource(key: string, selectorKey: string) {
  if(inputsLocked.value) return
  draft.value.contexts[key]=selectorKey ? {selectorKey,projectId:props.projectId ?? '',revisionId:''} : null
}
// Enumerating own keys tracks additions/deletions on Vue's reactive draft.
const has = (object: object, key: string) => Object.keys(object).includes(key)
const subjectsAllowed = computed(() => props.contract.inputs.some(row => row.semantic_contract.allowed_subject_kinds.includes('external')))
const kindLabel = (kind: string) => ({ text: '文字', json: '结构化内容', image: '图片', video: '视频', audio: '音频' })[kind] ?? kind
const inputLabel = (key:string) => props.contract.inputForms?.[key]?.label || key
const compactInput = (spec:ExecutionInput) => {
  const types=jsonAllowedTypes(props.contract.structureSchemas?.[spec.key] || [])
  return spec.kind==='json' && types.length===1 && ['number','boolean'].includes(types[0]!.value) && !contextOptions(spec).length
}
function addMedia(spec: ExecutionInput) {
  if (inputsLocked.value || draft.value.media[spec.key]!.length >= spec.max_items) return
  draft.value.media[spec.key]!.push({ itemKey: crypto.randomUUID(), assetId: '', purpose: null, pairedTo: null, subjectId: null })
}
function removeMedia(key: string, itemKey: string) {
  if (inputsLocked.value || requestBusy.value) return
  const row = draft.value.media[key]?.find(row => row.itemKey === itemKey)
  if (!row) return
  pendingChange.value = { kind:'media', key, itemKey, message:`仅移除「${inputLabel(key)}」的本次选择，不删除素材库文件。${pairingImpact([itemKey])}` }
}
function setAsset(key: string, item: string, asset: Asset) {
  // The parent feeds our own upload/read busy state back as disabled. Accept that
  // exact row's receipt; other disabled rows and pending input changes stay locked.
  if (pendingChange.value || props.disabled && !busySources.value[`media:${item}`]) return
  const row = draft.value.media[key]?.find(row => row.itemKey === item)
  if (!row) return
  delete row.selectedOutput
  delete row.videoAudio
  delete row.reviewedShot
  row.assetId = asset.id; assetNames.value[asset.id] = asset.name
}
function setSelectedSource(key: string, item: string, decisionId: string) {
  if (inputsLocked.value) return
  const row = draft.value.media[key]?.find(row => row.itemKey === item)
  if (!row) return
  const source = props.selectedSources?.find(value => value.decisionId === decisionId)
  row.assetId = ''
  delete row.videoAudio
  delete row.reviewedShot
  if (source) row.selectedOutput = { ...source }
  else delete row.selectedOutput
}
function setVideoAudio(key: string, item: string, enabled: boolean) {
  if (inputsLocked.value) return
  const row = draft.value.media[key]?.find(value => value.itemKey === item)
  if (!row) return
  row.videoAudio = enabled
  row.assetId = ''; row.subjectId = null
  delete row.selectedOutput
  delete row.reviewedShot
}
function shotSources(spec: ExecutionInput) {
  return (props.reviewedShotSources || []).filter(source=>source.kind===spec.kind && source.runId===props.runId
    && props.productionScope?.kind==='shot' && source.shotId===props.productionScope.id
    && spec.semantic_contract.allowed_scope_kinds.includes('shot')
    && (!source.subjectRef || spec.semantic_contract.allowed_subject_kinds.includes(source.subjectRef.kind)))
}
function setShotSource(spec: ExecutionInput, item: string, referenceKey: string) {
  if (inputsLocked.value) return
  const row = draft.value.media[spec.key]?.find(row=>row.itemKey===item)
  if (!row) return
  const source = shotSources(spec).find(source=>source.referenceKey===referenceKey)
  delete row.selectedOutput; delete row.videoAudio; delete row.reviewedShot
  row.assetId = ''; row.subjectId = null
  if (source) { row.reviewedShot = source; row.assetId = source.assetId }
  // Creative purpose is not a target-interface enum. Keep the user's per-call choice.
}
function parameter(key: string, value: ExecutionScalar) { if (!inputsLocked.value) draft.value.parameters[key] = value }
function toggleParameter(key: string, enabled: boolean) {
  if (inputsLocked.value) return
  if (enabled) draft.value.parameters[key] = null
  else delete draft.value.parameters[key]
}
function removeSubject(id: string) {
  if (inputsLocked.value || requestBusy.value) return
  const subject = draft.value.subjects.find(row => row.id === id)
  if (!subject) return
  const count = Object.values(draft.value.media).flat().filter(row => row.subjectId === id).length
  pendingChange.value = { kind:'subject', id, message:`移除「${subject.name || '未命名主体'}」的本次说明，解除 ${count} 项本次素材关联。素材选择和素材库文件保留，不修改项目 Bible。` }
}
function addSubject() { if (!inputsLocked.value) draft.value.subjects.push({ id: crypto.randomUUID(), category: '', name: '', description: '' }) }
function validity(key: string, errors: string[]) { literalErrors.value[key] = errors }
watch([literalErrors, () => draft.value.literals, () => draft.value.contexts, pendingChange], () => emit('validity',
  [...Object.entries(literalErrors.value).filter(([key]) => !props.managedInputKeys.includes(key) && draft.value.literals[key]?.enabled && !draft.value.contexts[key]).flatMap(([, errors]) => errors), ...(pendingChange.value ? ['请先完成当前输入变更的确认，或继续编辑。'] : [])]
), { deep: true })
</script>

<template>
  <div class="execution-request-inputs">
    <p class="input-guide">填写本次生成内容。字段和格式由所选执行实例决定；请先核对输入，再按当前页面提示提交。是否使用工作流或提示词策略由实例配置决定，无需重复选择。</p>
    <ConfigurationDiscardNotice v-if="pendingChange" title="确认本次输入变更" :message="pendingChange.message" :busy="disabled || requestBusy" action-label="确认变更" @keep="pendingChange=null" @discard="applyPendingChange"/>
    <fieldset v-for="spec in contract.inputs" :key="spec.key" :disabled="inputsLocked" :class="{'compact-input':compactInput(spec)}">
      <legend>{{ inputLabel(spec.key) }} <span class="input-requirement">{{ spec.required || spec.min_items ? '必填' : '可选' }}</span></legend>
      <p v-if="contract.inputForms?.[spec.key]?.description" class="input-description">{{ contract.inputForms[spec.key]!.description }}</p>
      <details class="input-requirements"><summary>查看输入要求</summary><p>字段标识：{{ spec.key }} · {{ kindLabel(spec.kind) }} · 数量 {{ spec.min_items }} ～ {{ spec.max_items }}</p><p>对应目标：{{ spec.targets.map(row => row.target_key).join('、') }}</p><p>填写后的内容会按这些目标传入所选实例；不会自动补写缺失内容。</p></details>
      <p v-if="managedInputKeys.includes(spec.key)">由本页选择的真实项目字段提供。提交时服务端核对原文与修订，不需要手工填写 JSON。</p>
      <template v-else-if="spec.kind === 'text' || spec.kind === 'json'">
        <template v-if="contract.serverOwnedInputs.includes(spec.key)">
          <p>此输入由服务端按已发布规则组装，不接受手工编辑。请使用本页的字段测试来源配置。</p>
          <details><summary>查看已组装输入</summary><pre>{{ JSON.stringify(draft.literals[spec.key]?.value,null,2) }}</pre></details>
        </template>
        <label v-else-if="!spec.required && !spec.min_items" class="request-check"><input v-model="draft.literals[spec.key]!.enabled" type="checkbox"/>提供这项输入</label>
        <template v-if="draft.literals[spec.key]!.enabled && !contract.serverOwnedInputs.includes(spec.key)">
          <ExecutionInputSuggestions v-if="projectSnapshot(spec) && contract.versionId" snapshot
            v-model="draft.literals[spec.key]!" :version-id="contract.versionId" :definition-sha256="contract.sha256"
            :input-key="spec.key" :project-id="projectId" :choices="contract.inputForms?.[spec.key]?.suggestions ?? []" :disabled="inputsLocked"
            @busy="inputBusy(`project:${spec.key}`,$event)"/>
          <template v-else>
          <p v-if="contextOptions(spec).length === 1 && !literalAllowed(spec) && draft.contexts[spec.key]?.selectorKey === contextOptions(spec)[0]?.selector_key">来源已由策略指定：{{ contextOptions(spec)[0]?.label }}。请预览并确认要引用的审核版本。</p>
          <label v-else-if="contextOptions(spec).length" class="field">内容从哪里来<select :value="draft.contexts[spec.key]?.selectorKey ?? ''" @change="contextSource(spec.key,($event.target as HTMLSelectElement).value)"><option value="">{{ literalAllowed(spec) ? '本次填写' : '请选择真实上下文来源' }}</option><option v-for="option in contextOptions(spec)" :key="option.selector_key" :value="option.selector_key">{{ option.label }}</option></select><span>选择已有内容时，需要指定已确认的版本；只有允许本次填写的输入可以修改正文。</span></label>
          <ExecutionReviewedVisualSource v-if="draft.contexts[spec.key] && contextOptions(spec).find(row=>row.selector_key===draft.contexts[spec.key]?.selectorKey)?.revision_kind==='visual_rules_approval'"
            v-model="draft.contexts[spec.key]!" :run-id="runId" :stage-key="stageKey" :project-id="projectId" :disabled="inputsLocked" @busy="inputBusy(`context:${spec.key}`,$event)"/>
          <ExecutionContextFieldPicker v-else-if="draft.contexts[spec.key]" :key="draft.contexts[spec.key]!.selectorKey" v-model="draft.contexts[spec.key]!" :project-id="projectId" :description="contextOptions(spec).find(row=>row.selector_key===draft.contexts[spec.key]?.selectorKey)?.description" :disabled="inputsLocked" @busy="inputBusy(`context:${spec.key}`,$event)"/>
          <p v-else-if="!literalAllowed(spec)">此业务输入需要真实的已确认上下文；请选择来源，不接受手工伪造快照。</p>
          <template v-else-if="spec.kind === 'text'">
            <ExecutionInputSuggestions v-if="contract.versionId && suggestions(spec.key).length"
              v-model="draft.literals[spec.key]!" :version-id="contract.versionId" :definition-sha256="contract.sha256"
              :input-key="spec.key" :project-id="projectId" :choices="suggestions(spec.key)" :disabled="inputsLocked"
              :production="runId && productionScope ? {runId,scope:productionScope} : undefined"
              @busy="inputBusy(`suggestion:${spec.key}`,$event)"/>
            <label class="field">填写{{ inputLabel(spec.key) }}<textarea :value="draft.literals[spec.key]!.value as string" rows="3" :placeholder="`请填写${inputLabel(spec.key)}`" @input="draft.literals[spec.key]!.value=($event.target as HTMLTextAreaElement).value"/><span>可修改正文或在末尾补充本轮要求，以这里的最终内容提交。</span></label>
          </template>
          <StructuredJsonEditor v-else v-model="draft.literals[spec.key]!.value" :label="inputLabel(spec.key)" hide-label :schemas="contract.structureSchemas?.[spec.key]" :default-schemas="spec.data_structure_version_id ? contract.structureSchemas?.[spec.key]?.slice(0,1) : undefined" :disabled="inputsLocked" @validity="validity(spec.key,$event)"/>
          </template>
        </template>
      </template>
      <template v-else-if="['image','video','audio'].includes(spec.kind)">
        <template v-if="spec.media_source_policy">
          <label>本次素材来源<select :value="mediaMode(spec)" :disabled="inputsLocked||sourceLoading||!sourceCatalog||!spec.media_source_policy.allow_override||Object.values(busySources).some(Boolean)" @change="changeMediaMode(spec,$event)">
            <option v-if="!sourceModes(spec).some(row=>row.value===mediaMode(spec))" :value="mediaMode(spec)">{{mediaMode(spec)||'尚未选择来源'}}</option>
            <option v-for="source in sourceModes(spec)" :key="source.value" :value="source.value" :disabled="source.value==='selected_output'&&selectedSources===undefined">{{source.label}}{{source.value===spec.media_source_policy.default_source?'（配置默认）':''}}</option>
          </select></label>
          <p>{{spec.media_source_policy.allow_override?'允许本次整组改选，不合并不同来源。':'此输入使用配置的默认来源，不允许本次改选。'}} 默认来源不自动选择素材。</p>
          <p v-if="sourceLoading" role="status">正在读取来源目录…</p>
          <p v-if="sourceError" role="alert">{{sourceError}} <button type="button" class="button ghost small" :disabled="sourceLoading" @click="loadSources">重新读取来源目录</button></p>
          <p v-if="mediaMode(spec)==='selected_output'&&selectedSources===undefined" role="alert">此入口没有提供上游已选择产物；请从支持运行上下文的业务入口使用，或按配置允许的方式改选。</p>
        </template>
        <p v-if="!spec.semantic_contract.allowed_scope_kinds.includes('request')" class="platform-alert warning">此槽位要求真实项目上下文，不接受独立素材；请明确选择本镜已审核用途。</p>
        <article v-for="(row,index) in draft.media[spec.key]" :key="row.itemKey" class="input-media-row">
          <p>第 {{ index + 1 }} 项 · {{ row.videoAudio ? '配对视频原音' : row.selectedOutput ? `已采用产物 ${row.selectedOutput.artifactId}` : row.assetId ? assetNames[row.assetId] ?? row.assetId : '尚未选择素材' }}</p>
          <label v-if="reviewedShotSources !== undefined && !row.videoAudio && (!spec.media_source_policy || ['asset_library','asset_upload'].includes(mediaMode(spec)||''))">本镜已审核用途<select :value="row.reviewedShot?.referenceKey || ''" @change="setShotSource(spec,row.itemKey,($event.target as HTMLSelectElement).value)">
            <option value="">不使用本镜用途来源</option>
            <option v-for="source in shotSources(spec)" :key="source.referenceKey" :value="source.referenceKey">{{source.name}} · {{source.referenceKey}} · {{source.creativePurpose || '未填写创作用途'}}</option>
            <option v-if="row.reviewedShot && !shotSources(spec).some(source=>source.referenceKey===row.reviewedShot!.referenceKey && source.review.decision_id===row.reviewedShot!.review.decision_id && source.review.snapshot_sha256===row.reviewedShot!.review.snapshot_sha256)" :value="row.reviewedShot.referenceKey" disabled>原选择不在当前审核清单，请重新选择</option>
          </select></label>
          <p v-if="reviewedShotSources !== undefined && mediaMode(spec)==='asset_upload'">上传来源仍由服务端核对正式上传记录；选择已审核用途不会把已有或生成素材改记为上传。</p>
          <template v-if="row.reviewedShot">
            <p>原创作用途：{{row.reviewedShot.creativePurpose || '未填写'}}。保留确切版本与设定；下方接口用途需独立选择，不自动推断首帧。</p>
            <ExactReferencePreview :selection="row.reviewedShot.preview" :name="row.reviewedShot.name"/>
          </template>
          <label v-if="!spec.media_source_policy && spec.kind === 'audio' && spec.semantic_contract.paired_with_input_key" class="request-check"><input type="checkbox" :checked="!!row.videoAudio" @change="setVideoAudio(spec.key,row.itemKey,($event.target as HTMLInputElement).checked)"/>使用配对视频的原音（仍需选择下方父视频和音频用途）</label>
          <p v-if="row.videoAudio">服务端核对实际音轨；目标支持时直接读取，否则按冻结合同处理配套音频。不会把视频冒充音频素材，也不会在预览时提取。</p>
          <label v-if="selectedSources !== undefined && !row.reviewedShot && !row.videoAudio && (!spec.media_source_policy||mediaMode(spec)==='selected_output')">素材来源<select :value="row.selectedOutput?.decisionId ?? ''" @change="setSelectedSource(spec.key,row.itemKey,($event.target as HTMLSelectElement).value)">
            <option value="">{{spec.media_source_policy?'请选择确切上游产物':'从素材库选择或上传'}}</option>
            <option v-for="source in selectedSources.filter(value=>value.kind===spec.kind)" :key="source.decisionId" :value="source.decisionId">本轮已审核 · {{ source.unitKey }} · {{ source.artifactId.slice(0,8) }} · 采用 v{{ source.revision }}</option>
            <option v-if="row.selectedOutput && !selectedSources.some(value=>value.artifactId===row.selectedOutput!.artifactId && value.decisionId===row.selectedOutput!.decisionId)" :value="row.selectedOutput.decisionId" disabled>原选择已不在当前审核清单，请重新选择</option>
          </select></label>
          <ExecutionInputAssetPicker v-if="!row.reviewedShot && !row.selectedOutput && !row.videoAudio && spec.semantic_contract.allowed_scope_kinds.includes('request') && (!spec.media_source_policy||['asset_library','asset_upload'].includes(mediaMode(spec)||''))" :kind="spec.kind as 'image'|'video'|'audio'" :allow-library="!spec.media_source_policy||mediaMode(spec)==='asset_library'" :allow-upload="!spec.media_source_policy||mediaMode(spec)==='asset_upload'" :purpose="contract.purposes[spec.kind]?.find(option=>option.value===row.purpose)?.label || row.purpose" :disabled="inputsLocked" @busy="inputBusy(`media:${row.itemKey}`,$event)" @selected="setAsset(spec.key,row.itemKey,$event)"/>
          <label v-if="spec.semantic_contract.allowed_purposes.length">素材用途<select v-model="row.purpose"><option :value="null">请选择用途</option><option v-for="purpose in spec.semantic_contract.allowed_purposes" :key="purpose" :value="purpose">{{ contract.purposes[spec.kind]?.find(option => option.value === purpose)?.label ?? purpose }}</option></select></label>
          <label v-if="spec.semantic_contract.paired_with_input_key">配对哪一项{{ inputLabel(spec.semantic_contract.paired_with_input_key) }}<select v-model="row.pairedTo"><option :value="null">请选择本次父素材</option><option v-if="missingParent(spec,row.pairedTo)" :value="row.pairedTo" disabled>原配对已移除，请重新选择</option><option v-for="(parent,parentIndex) in draft.media[spec.semantic_contract.paired_with_input_key]" :key="parent.itemKey" :value="parent.itemKey">第 {{ parentIndex + 1 }} 项 · {{ parent.selectedOutput?.artifactId ?? assetNames[parent.assetId] ?? (parent.assetId || '尚未选择素材') }}</option></select><span>按本次具体使用项配对；同一素材重复使用时也分别选择，不按文件名自动配对。</span></label>
          <p v-if="missingParent(spec,row.pairedTo)" class="platform-alert warning" role="alert">原父素材选择已移除。此项内容仍保留，请重新配对或移除此项选择后再提交。</p>
          <label v-if="!row.reviewedShot && !row.videoAudio && spec.semantic_contract.allowed_subject_kinds.includes('external')">代表哪个本次主体<select v-model="row.subjectId"><option :value="null">不关联主体</option><option v-for="subject in draft.subjects" :key="subject.id" :value="subject.id">{{ subject.name || '请先填写主体名称' }}</option></select></label>
          <button type="button" class="button danger small" @click="removeMedia(spec.key,row.itemKey)">移除此项选择</button>
        </article>
        <button type="button" class="button ghost small" :disabled="inputsLocked || draft.media[spec.key]!.length >= spec.max_items" @click="addMedia(spec)">添加一项{{ kindLabel(spec.kind) }}</button>
      </template>
      <p v-else class="platform-alert danger">当前页面尚未接入此输入类型，不会忽略后提交。</p>
    </fieldset>
    <fieldset v-if="subjectsAllowed" :disabled="inputsLocked"><legend>本次主体说明</legend><p>在素材行关联对应主体。语义类别用于提示词组装，不创建或采用项目 Bible；素材用途仍须符合当前策略。</p>
      <article v-for="subject in draft.subjects" :key="subject.id" class="input-media-row"><label>主体语义类别<select v-model="subject.category"><option value="" disabled>请选择类别</option><option v-for="choice in contract.subjectCategories" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label><label>名称<input v-model="subject.name"/></label><label>真实描述<textarea v-model="subject.description" rows="2"/></label><button type="button" class="button danger small" @click="removeSubject(subject.id)">移除主体</button></article>
      <button type="button" class="button ghost small" @click="addSubject">添加主体说明</button>
    </fieldset>
    <p class="help">预算与调用次数由系统资源策略动态提醒。原生文本输出额度在保存准备时从系统配置读取；已保存请求不随配置变化改写。</p>
    <fieldset v-if="contract.parameters.length" :disabled="inputsLocked"><legend>本次运行参数</legend>
      <div v-for="row in contract.parameters" :key="row.key" class="parameter-row"><strong>{{ row.key }}</strong><ExecutionParameterEvidence :entry="row.evidence"/>
        <template v-if="!has(row.rule,'const') && row.rule.seed_policy !== 'generate_once'">
          <label class="request-check"><input type="checkbox" :checked="has(draft.parameters,row.key)" @change="toggleParameter(row.key,($event.target as HTMLInputElement).checked)"/>为本次填写参数（不勾选时使用合同明确默认，必需且无默认仍须填写）</label>
          <template v-if="has(draft.parameters,row.key)">
            <label v-if="row.rule.enum">可选值<select :value="row.rule.enum.findIndex(value => Object.is(value,draft.parameters[row.key]))" @change="parameter(row.key,row.rule.enum![Number(($event.target as HTMLSelectElement).value)]!)"><option value="-1" disabled>请选择</option><option v-for="(value,index) in row.rule.enum" :key="index" :value="index">{{ JSON.stringify(value) }}</option></select></label>
            <ExecutionScalarEditor v-else :model-value="draft.parameters[row.key]!" :value-type="row.rule.type" :label="row.key" :disabled="inputsLocked" @update:model-value="parameter(row.key,$event)"/>
          </template>
        </template>
      </div>
    </fieldset>
    <fieldset :disabled="inputsLocked"><legend>提示词处理</legend>
      <p v-if="contract.promptMode === 'profile'">按实例固定的策略准备字段并组装；审核与采用按配置处理，缺失或待处理内容在准备工作台继续，不在此处再传一份完整提示词。</p>
      <p v-else-if="contract.promptMode === 'none'">合同明确无需提示词，不会额外生成或自动改写提示词。</p>
      <template v-else><div v-for="row in contract.channels" :key="row.key"><p>{{ row.key }} · {{ executionRuleDescription(row.rule) }}</p><template v-if="!has(row.rule,'const')"><label class="request-check"><input type="checkbox" :checked="has(draft.channels,row.key)" @change="($event.target as HTMLInputElement).checked ? draft.channels[row.key]='' : delete draft.channels[row.key]"/>填写此完整通道</label><label v-if="has(draft.channels,row.key)">{{ row.key }}<textarea v-model="draft.channels[row.key]" rows="4"/></label></template></div></template>
      <div class="request-columns">
        <div v-for="item in [{key:'sourceLanguage' as const, label:'输入语言', rule:contract.languageForm.source}, {key:'targetLanguage' as const, label:'提示词目标语言', rule:contract.languageForm.target}]" :key="item.key" class="language-context">
          <label v-if="item.rule.fields.length">{{ item.label }} · {{ item.rule.required ? '必填' : '使用下列字段时填写' }}
            <CatalogChoiceField v-model="draft[item.key]" :options="contract.languageForm.choices" :allow-custom="contract.languageForm.allow_custom" :disabled="inputsLocked" custom-label="其他语言（填写语言代码）" custom-placeholder="例如 ja、fr、zh-CN"/>
          </label>
          <p v-else>{{ item.label }} · 无需填写</p>
          <p v-if="item.rule.fields.length" class="input-description">{{ item.key==='sourceLanguage' ? '按原语言保留' : '按所选语言处理' }}：{{ item.rule.fields.join('、') }}。</p>
          <p v-else class="input-description">当前合同不使用此语言设置。</p>
        </div>
      </div>
      <p v-if="contract.languageForm.fixed_fields.length" class="input-description">策略已固定语言：{{ contract.languageForm.fixed_fields.map(row=>`${row.label}（${row.language}）`).join('；') }}，无需重复选择。</p>
      <details class="input-requirements"><summary>语言如何选择？</summary><p>按上面列出的字段选择实际语言。例如创意和风格都用英文时，需要声明的输入语言选“英文”；其他输入不会因此被翻译或改写。这里只控制提示词策略字段，不是最终剧本、对白或视频的输出语言设置。</p><p>输入语言只作用于“按原语言保留”的字段，其他字段可使用自身实际语言。若上面多个受约束字段分别使用不同语言，当前共用语言声明不能分别表达，请先将这些字段统一为同一种语言；平台不会自动检测或翻译。</p></details>
    </fieldset>
  </div>
</template>

<style scoped>
.execution-request-inputs{display:grid;gap:14px;min-width:0;font:var(--config-body-size)/1.7 var(--font)}.execution-request-inputs fieldset{margin:0;border:1px solid var(--border);border-radius:6px;padding:12px;display:grid;grid-template-columns:minmax(0,1fr);gap:10px;min-width:0}.execution-request-inputs p,.execution-request-inputs label{font-size:var(--config-body-size);line-height:1.7;overflow-wrap:anywhere}.execution-request-inputs p{margin:0}.execution-request-inputs label{display:grid;grid-template-columns:minmax(0,1fr);gap:6px;min-width:0}.execution-request-inputs .request-check{display:flex;gap:8px;align-items:flex-start}.request-check input{width:auto;margin-top:5px}.input-media-row,.parameter-row{display:grid;grid-template-columns:minmax(0,1fr);gap:8px;padding:10px;border-top:1px solid var(--border);min-width:0;overflow-wrap:anywhere}.parameter-row>strong{min-width:0;font-weight:600;overflow-wrap:anywhere}.request-columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.execution-request-inputs textarea{resize:vertical;min-width:0;width:100%}@media(max-width:640px){.request-columns{grid-template-columns:minmax(0,1fr)}}
.execution-request-inputs legend{padding:0 8px;font-weight:600;font-size:var(--config-section-size);max-width:100%;overflow-wrap:anywhere}.input-requirement{font-size:var(--config-help-size);color:var(--text-muted);font-weight:400;margin-left:8px}.input-guide{padding:12px;background:var(--bg-secondary);border:1px solid var(--border);border-radius:8px}.execution-request-inputs .input-description,.input-requirements{color:var(--text-muted);font-size:var(--config-help-size);line-height:1.8}.input-requirements summary{cursor:pointer;width:fit-content}.execution-request-inputs :is(input:not([type=checkbox]),select,textarea){width:100%;min-width:0;max-width:100%;min-height:40px;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg-input,#080d10);color:var(--text-primary);font:inherit;box-sizing:border-box}.execution-request-inputs :is(input,select,textarea):focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:760px){.execution-request-inputs :is(input:not([type=checkbox]),select,textarea,.button){min-height:44px}}
.execution-request-inputs{grid-template-columns:repeat(3,minmax(0,1fr));align-items:start}.execution-request-inputs>*{grid-column:1/-1}.execution-request-inputs>.compact-input{grid-column:auto}@media(max-width:900px){.execution-request-inputs{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:640px){.execution-request-inputs{grid-template-columns:minmax(0,1fr)}}
.language-context{display:grid;gap:6px;min-width:0;align-content:start}.language-context :deep(select),.language-context :deep(input){width:100%;min-height:40px;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg-input,#080d10);color:var(--text-primary);font:inherit;box-sizing:border-box}.language-context :deep(:focus-visible){outline:2px solid var(--accent);outline-offset:2px}
</style>
