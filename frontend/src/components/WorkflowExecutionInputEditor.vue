<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { AlertTriangle, Plus, RefreshCw, Upload, X } from 'lucide-vue-next'
import type { WorkflowExecutionInputSchema, WorkflowSmokeFixtureParameter } from '@/services/platformTypes'
import {
  compatibleWorkflowAssets,
  compatibleDynamicWorkflowAssets,
  createWorkflowExecutionInputDraft,
  type WorkflowExecutionInputDraft,
  type WorkflowInputAssetBindingTarget,
  workflowInputDraftKey,
  workflowExecutionParameterStep,
  type WorkflowInputAssetCandidate,
  type WorkflowInputAssetUploadRequest,
} from '@/services/workflowExecutionInputs'
import {
  workflowExecutionParameterControl,
  workflowExecutionValueFromControl,
} from '@/services/workflowSmokeFixturePresentation'

const props = defineProps<{
  schema: WorkflowExecutionInputSchema
  assets: WorkflowInputAssetCandidate[]
  disabled?: boolean
  assetsLoading?: boolean
  assetsError?: string
  allowAssetUpload?: boolean
  assetUploadPendingKey?: string
}>()
const emit = defineEmits<{
  refreshAssets: []
  reset: []
  requestAssetUpload: [request: WorkflowInputAssetUploadRequest]
}>()
const draft = defineModel<WorkflowExecutionInputDraft>({ required: true })
const groupCounts = ref<Record<string, number>>({})
const resetGroupCounts = () => {
  groupCounts.value = Object.fromEntries((props.schema.assetGroups || []).map((group) => [
    group.key, Math.max(group.minItems, ...group.semanticKeys.map((key, i) => draft.value.assetIds[key] ? i + 1 : 0)),
  ]))
}
watch(() => [props.schema, draft.value.assetIds], resetGroupCounts, { immediate: true })
const resizeGroup = (key: string, delta: number) => {
  const group = props.schema.assetGroups?.find((row) => row.key === key)
  if (!group || props.disabled) return
  const count = Math.max(group.minItems, Math.min(group.maxItems, (groupCounts.value[key] ?? group.minItems) + delta))
  groupCounts.value[key] = count
  group.semanticKeys.slice(count).forEach((semanticKey) => { draft.value.assetIds[semanticKey] = '' })
}
const visibleAssetSlots = computed(() => {
  const groups = props.schema.assetGroups || []
  const groupedKeys = new Set(groups.flatMap((group) => group.semanticKeys))
  const slots = new Map(props.schema.assetSlots.map((slot) => [slot.semanticKey, slot]))
  return [
    ...props.schema.assetSlots.filter((slot) => !groupedKeys.has(slot.semanticKey)),
    ...groups.flatMap((group) => group.semanticKeys.slice(0, groupCounts.value[group.key] ?? group.minItems).flatMap((key) => slots.has(key) ? [slots.get(key)!] : [])),
  ]
})
const dynamic = computed(() => (
  props.schema.dynamicReferenceModes[
    String(draft.value.values.CONDITIONING_MODE || '')
  ] || props.schema.dynamicReferences
))
const compatible = (kind: 'image' | 'video' | 'audio') => compatibleWorkflowAssets(props.assets, kind)
const dynamicCompatible = (kind: 'video' | 'audio', usage: 'paired' | 'standalone' = 'standalone') => (
  dynamic.value
    ? compatibleDynamicWorkflowAssets(props.assets, kind, dynamic.value.conditioningMode, usage)
    : []
)
const topology = computed(() => dynamic.value?.conditioningMode.dynamicReferenceTopology || null)
const videoPurposes = computed(() => topology.value?.videos.purposes || [])
const audioPurposes = computed(() => topology.value?.audios.purposes || [])
const pairingRule = (purpose: string) => {
  const matches = (topology.value?.pairingRules || []).filter((rule) => rule.sourcePurposeKeys.includes(purpose))
  return matches.length === 1 ? matches[0] : undefined
}
const supportsEmbeddedAudio = (purpose: string) => pairingRule(purpose)?.allowEmbeddedSource === true
const supportsExternalAudio = (purpose: string) => pairingRule(purpose)?.allowExternalTarget === true
const pairedAudioPurposes = (purpose: string) => {
  const allowed = new Set(pairingRule(purpose)?.targetPurposeKeys || [])
  return audioPurposes.value.filter((item) => allowed.has(item.key))
}
const parameterOptionIndex = (parameter: WorkflowSmokeFixtureParameter) => (
  parameter.options.findIndex((option) => Object.is(option, draft.value.values[parameter.semanticKey]))
)
const setParameter = (parameter: WorkflowSmokeFixtureParameter, event: Event) => {
  const target = event.target as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement
  const value = workflowExecutionValueFromControl(parameter, target.value)
  draft.value = {
    ...draft.value,
    values: { ...draft.value.values, [parameter.semanticKey]: value ?? null },
  }
}
const reset = () => {
  draft.value = createWorkflowExecutionInputDraft(props.schema)
  resetGroupCounts()
  emit('reset')
}
const addPicture = () => {
  if (!dynamic.value || draft.value.pictures.length >= dynamic.value.pictures.maxItems) return
  draft.value.pictures.push({
    key: workflowInputDraftKey(), assetId: '',
    category: dynamic.value.pictures.categories[0] || 'character',
  })
}
const addVideo = () => {
  if (!dynamic.value || draft.value.videos.length >= dynamic.value.videos.maxItems) return
  const purpose = dynamic.value.videos.defaultPurposeKey
  const defaultAudioPurpose = pairingRule(purpose)?.defaultTargetPurposeKey
    || dynamic.value.audios.defaultPurposeKey
  draft.value.videos.push({
    key: workflowInputDraftKey(), assetId: '',
    purpose,
    useEmbeddedAudio: false, audioAssetId: '',
    audioPurpose: defaultAudioPurpose,
  })
}
const addAudio = () => {
  if (!dynamic.value || draft.value.audios.length >= dynamic.value.audios.maxItems) return
  draft.value.audios.push({
    key: workflowInputDraftKey(), assetId: '', purpose: dynamic.value.audios.defaultPurposeKey,
  })
}
const setEmbeddedAudio = (index: number, enabled: boolean) => {
  const item = draft.value.videos[index]
  if (!item) return
  item.useEmbeddedAudio = enabled
  if (enabled) item.audioAssetId = ''
}
const setExternalAudio = (index: number, assetId: string) => {
  const item = draft.value.videos[index]
  if (!item) return
  item.audioAssetId = assetId
  if (assetId) item.useEmbeddedAudio = false
}
const setVideoPurpose = (index: number, purpose: string) => {
  const item = draft.value.videos[index]
  if (!item) return
  item.purpose = purpose
  const rule = pairingRule(purpose)
  if (!rule?.allowEmbeddedSource) item.useEmbeddedAudio = false
  if (!rule?.allowExternalTarget) item.audioAssetId = ''
  item.audioPurpose = rule?.defaultTargetPurposeKey || ''
}
const uploadRequestKey = (target: WorkflowInputAssetBindingTarget) => (
  `${target.kind}:${target.kind === 'asset_slot' ? target.semanticKey : target.rowKey}`
)
const requestAssetUpload = (
  mediaKind: WorkflowInputAssetUploadRequest['mediaKind'],
  target: WorkflowInputAssetBindingTarget,
) => emit('requestAssetUpload', {
  key: uploadRequestKey(target),
  mediaKind,
  target,
})
const uploadButtonLabel = (target: WorkflowInputAssetBindingTarget) => (
  props.assetUploadPendingKey === uploadRequestKey(target) ? '上传中…' : '本地上传'
)
const mediaLabel = (kind: WorkflowInputAssetUploadRequest['mediaKind']) => ({
  image: '图片', video: '视频', audio: '音频',
}[kind])
</script>

<template>
  <section class="workflow-input-editor" :aria-busy="Boolean(assetUploadPendingKey)">
    <header>
      <span><strong>工作流输入</strong><small>控件、数量、用途和配对规则均来自当前不可变工作流版本声明。<template v-if="allowAssetUpload"> 本地文件上传后会进入素材库并自动用于当前槽位。</template></small></span>
      <span class="workflow-input-actions">
        <button v-if="schema.assetSlots.length||schema.dynamicReferences||Object.keys(schema.dynamicReferenceModes).length" type="button" class="button ghost tiny" :disabled="disabled||assetsLoading" @click="emit('refreshAssets')"><RefreshCw :size="11" :class="{spin:assetsLoading}"/>重读素材</button>
        <button type="button" class="button ghost tiny" :disabled="disabled" @click="reset"><RefreshCw :size="11"/>恢复默认</button>
      </span>
    </header>
    <div v-if="!schema.available" class="workflow-input-alert"><AlertTriangle :size="14"/><span>输入合同不可用：{{ schema.reasonCode || 'workflow_input_contract_missing' }}</span></div>
    <template v-else>
      <div v-if="schema.parameters.length" class="workflow-input-grid">
        <label v-for="parameter in schema.parameters" :key="parameter.semanticKey" class="field" :class="{wide:workflowExecutionParameterControl(parameter)==='textarea'}">
          <span>{{ parameter.semanticKey }}<template v-if="parameter.required"> *</template></span>
          <span v-if="workflowExecutionParameterControl(parameter)==='fixed'" class="input-help">{{ parameter.options[0] }} · 合同固定值</span>
          <select v-else-if="workflowExecutionParameterControl(parameter)==='select'" :value="parameterOptionIndex(parameter)" :disabled="disabled" @change="setParameter(parameter,$event)"><option v-if="parameter.required&&parameterOptionIndex(parameter)<0" value="-1" disabled>请选择</option><option v-if="!parameter.required" value="-1">不设置</option><option v-for="(option,index) in parameter.options" :key="`${parameter.semanticKey}:${index}`" :value="index">{{ option }}</option></select>
          <select v-else-if="workflowExecutionParameterControl(parameter)==='boolean'" :value="String(draft.values[parameter.semanticKey] ?? '')" :disabled="disabled" @change="setParameter(parameter,$event)"><option v-if="!parameter.required" value="">不设置</option><option value="true">是</option><option value="false">否</option></select>
          <textarea v-else-if="workflowExecutionParameterControl(parameter)==='textarea'" :value="String(draft.values[parameter.semanticKey] ?? '')" rows="5" :disabled="disabled" @input="setParameter(parameter,$event)"></textarea>
          <input v-else :type="workflowExecutionParameterControl(parameter)==='number'?'number':'text'" :value="draft.values[parameter.semanticKey] ?? ''" :min="parameter.min ?? undefined" :max="parameter.max ?? undefined" :step="workflowExecutionParameterStep(parameter)" :disabled="disabled" @input="setParameter(parameter,$event)"/>
          <small>类型 {{ parameter.type }}<template v-if="parameter.min!==null||parameter.max!==null"> · 范围 {{ parameter.min ?? '—' }}–{{ parameter.max ?? '—' }}</template><template v-if="parameter.alignmentModulus"> · 合法值 {{ parameter.alignmentRemainder ?? 0 }} + {{ parameter.alignmentModulus }}k</template><template v-if="parameter.selectionGroup"> · 联动 {{ parameter.selectionGroup }}</template></small>
        </label>
      </div>

      <div v-for="group in schema.assetGroups || []" :key="group.key" class="field">
        <span>{{ group.key }} · {{ mediaLabel(group.mediaKind) }} · 允许 {{ group.minItems }}–{{ group.maxItems }} 份</span>
        <span class="workflow-asset-picker">
          <button type="button" class="button ghost tiny" :disabled="disabled||(groupCounts[group.key] ?? group.minItems)<=group.minItems" :aria-label="`减少 ${group.key} 素材`" @click="resizeGroup(group.key,-1)">减少素材</button>
          <span aria-live="polite">显示 {{ groupCounts[group.key] ?? group.minItems }} 个槽 · 已选 {{ group.semanticKeys.filter(key=>draft.assetIds[key]).length }} 份</span>
          <button type="button" class="button ghost tiny" :disabled="disabled||(groupCounts[group.key] ?? group.minItems)>=group.maxItems" :aria-label="`增加 ${group.key} 素材`" @click="resizeGroup(group.key,1)"><Plus :size="11"/>增加素材</button>
        </span>
        <small>运行数量以实际已选为准，按显示顺序接入且不能跳过中间项；减少数量会清除末尾选择，不删除素材库文件。未选择的分支不会执行。</small>
      </div>
      <div v-if="visibleAssetSlots.length" class="workflow-input-grid asset-slots">
        <div v-for="slot in visibleAssetSlots" :key="slot.semanticKey" class="field"><span>{{ slot.semanticKey }}<template v-if="slot.required"> *</template></span><span class="workflow-asset-picker"><select v-model="draft.assetIds[slot.semanticKey]" :aria-label="slot.semanticKey" :disabled="disabled||assetsLoading"><option value="">{{ slot.required?'请选择素材':'不绑定素材' }}</option><option v-for="asset in compatible(slot.mediaKind)" :key="asset.id" :value="asset.id">{{ asset.name }} · {{ asset.checksum?.slice(0,12) || '无摘要' }}</option></select><button v-if="allowAssetUpload" type="button" class="button ghost tiny" :disabled="disabled" :aria-label="`为 ${slot.semanticKey} 上传本地${mediaLabel(slot.mediaKind)}`" title="上传后进入素材库并自动选中" @click.prevent="requestAssetUpload(slot.mediaKind,{kind:'asset_slot',semanticKey:slot.semanticKey})"><Upload :size="11"/>{{ uploadButtonLabel({kind:'asset_slot',semanticKey:slot.semanticKey}) }}</button></span><small>{{ slot.mediaKind }}/* · input slot {{ slot.inputSlot }} · {{ slot.category }}</small><small v-if="!assetsLoading&&!compatible(slot.mediaKind).length" class="input-error">没有符合当前工作流合同的 {{ slot.mediaKind }} 素材<template v-if="allowAssetUpload">，可从本地上传</template>。</small></div>
      </div>

      <section v-if="dynamic" class="workflow-dynamic-inputs">
        <header><span><strong>动态参考 · {{ dynamic.mode }}</strong><small>同一素材只能使用一次；视频音轨的用途与配对规则来自冻结合同。</small></span><span class="workflow-input-actions"><button type="button" class="button ghost tiny" :disabled="disabled||draft.pictures.length>=dynamic.pictures.maxItems" @click="addPicture"><Plus :size="11"/>图片</button><button type="button" class="button ghost tiny" :disabled="disabled||draft.videos.length>=dynamic.videos.maxItems" @click="addVideo"><Plus :size="11"/>视频</button><button type="button" class="button ghost tiny" :disabled="disabled||draft.audios.length>=dynamic.audios.maxItems" @click="addAudio"><Plus :size="11"/>音频</button></span></header>
        <article v-for="(item,index) in draft.pictures" :key="item.key" class="workflow-reference-row"><div class="field"><span>参考图片 {{ index+1 }}</span><span class="workflow-asset-picker"><select v-model="item.assetId" :aria-label="`参考图片 ${index+1}`" :disabled="disabled||assetsLoading"><option value="">请选择图片</option><option v-for="asset in compatible('image')" :key="asset.id" :value="asset.id">{{ asset.name }}</option></select><button v-if="allowAssetUpload" type="button" class="button ghost tiny" :disabled="disabled" :aria-label="`为参考图片 ${index+1} 上传本地图片`" title="上传后进入素材库并自动选中" @click.prevent="requestAssetUpload('image',{kind:'picture',rowKey:item.key})"><Upload :size="11"/>{{ uploadButtonLabel({kind:'picture',rowKey:item.key}) }}</button></span></div><label class="field"><span>分类</span><select v-model="item.category" :disabled="disabled"><option v-for="category in dynamic.pictures.categories" :key="category" :value="category">{{ category }}</option></select></label><button type="button" class="button danger tiny" :disabled="disabled" @click="draft.pictures.splice(index,1)"><X :size="11"/>移除</button></article>
        <article v-for="(item,index) in draft.videos" :key="item.key" class="workflow-reference-row video"><div class="field"><span>参考视频 {{ index+1 }}</span><span class="workflow-asset-picker"><select v-model="item.assetId" :aria-label="`参考视频 ${index+1}`" :disabled="disabled||assetsLoading"><option value="">请选择符合时长/帧率合同的视频</option><option v-for="asset in dynamicCompatible('video')" :key="asset.id" :value="asset.id">{{ asset.name }}</option></select><button v-if="allowAssetUpload" type="button" class="button ghost tiny" :disabled="disabled" :aria-label="`为参考视频 ${index+1} 上传本地视频`" title="上传后进入素材库并自动选中" @click.prevent="requestAssetUpload('video',{kind:'video',rowKey:item.key})"><Upload :size="11"/>{{ uploadButtonLabel({kind:'video',rowKey:item.key}) }}</button></span></div><label class="field"><span>视频用途</span><select :value="item.purpose" :disabled="disabled" @change="setVideoPurpose(index,($event.target as HTMLSelectElement).value)"><option v-for="purpose in videoPurposes" :key="purpose.key" :value="purpose.key" :title="purpose.description">{{ purpose.label }} · {{ purpose.key }}（{{ purpose.minimumItems }}–{{ purpose.maximumItems }}）</option></select></label><label v-if="supportsEmbeddedAudio(item.purpose)" class="workflow-input-check"><input :checked="item.useEmbeddedAudio" type="checkbox" :disabled="disabled" @change="setEmbeddedAudio(index,($event.target as HTMLInputElement).checked)"/>使用内嵌音轨</label><div v-if="supportsExternalAudio(item.purpose)" class="field"><span>外配音轨</span><span class="workflow-asset-picker"><select :value="item.audioAssetId" :aria-label="`参考视频 ${index+1} 的外配音轨`" :disabled="disabled||assetsLoading" @change="setExternalAudio(index,($event.target as HTMLSelectElement).value)"><option value="">不绑定</option><option v-for="asset in dynamicCompatible('audio','paired')" :key="asset.id" :value="asset.id">{{ asset.name }}</option></select><button v-if="allowAssetUpload" type="button" class="button ghost tiny" :disabled="disabled" :aria-label="`为参考视频 ${index+1} 上传本地外配音轨`" title="上传后进入素材库并自动选中" @click.prevent="requestAssetUpload('audio',{kind:'paired_audio',rowKey:item.key})"><Upload :size="11"/>{{ uploadButtonLabel({kind:'paired_audio',rowKey:item.key}) }}</button></span></div><label v-if="item.useEmbeddedAudio||item.audioAssetId" class="field"><span>音轨用途</span><select v-model="item.audioPurpose" :disabled="disabled"><option v-for="purpose in pairedAudioPurposes(item.purpose)" :key="purpose.key" :value="purpose.key" :title="purpose.description">{{ purpose.label }} · {{ purpose.key }}</option></select></label><button type="button" class="button danger tiny" :disabled="disabled" @click="draft.videos.splice(index,1)"><X :size="11"/>移除</button></article>
        <article v-for="(item,index) in draft.audios" :key="item.key" class="workflow-reference-row"><div class="field"><span>独立参考音频 {{ index+1 }}</span><span class="workflow-asset-picker"><select v-model="item.assetId" :aria-label="`独立参考音频 ${index+1}`" :disabled="disabled||assetsLoading"><option value="">请选择符合时长合同的音频</option><option v-for="asset in dynamicCompatible('audio')" :key="asset.id" :value="asset.id">{{ asset.name }}</option></select><button v-if="allowAssetUpload" type="button" class="button ghost tiny" :disabled="disabled" :aria-label="`为独立参考音频 ${index+1} 上传本地音频`" title="上传后进入素材库并自动选中" @click.prevent="requestAssetUpload('audio',{kind:'audio',rowKey:item.key})"><Upload :size="11"/>{{ uploadButtonLabel({kind:'audio',rowKey:item.key}) }}</button></span></div><label class="field"><span>音频用途</span><select v-model="item.purpose" :disabled="disabled"><option v-for="purpose in audioPurposes" :key="purpose.key" :value="purpose.key" :title="purpose.description">{{ purpose.label }} · {{ purpose.key }}（{{ purpose.minimumItems }}–{{ purpose.maximumItems }}）</option></select></label><button type="button" class="button danger tiny" :disabled="disabled" @click="draft.audios.splice(index,1)"><X :size="11"/>移除</button></article>
        <small>数量范围：图片 {{ dynamic.pictures.minItems }}–{{ dynamic.pictures.maxItems }} · 视频 {{ dynamic.videos.minItems }}–{{ dynamic.videos.maxItems }} · 音频 {{ dynamic.audios.minItems }}–{{ dynamic.audios.maxItems }} · 物理素材上限 {{ dynamic.maximumPhysicalAssets }}</small>
      </section>
      <p v-if="assetsError" class="input-error">{{ assetsError }}</p>
    </template>
  </section>
</template>

<style scoped>
.workflow-input-editor{display:flex;flex-direction:column;gap:11px;padding:12px;border:1px solid rgba(104,197,167,.22);border-radius:8px;background:rgba(104,197,167,.035)}
.workflow-input-editor>header,.workflow-dynamic-inputs>header{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.workflow-input-editor header>span:first-child{display:flex;flex-direction:column;gap:3px}.workflow-input-editor header strong{font-size:10px;color:#b8d6cc}.workflow-input-editor header small,.workflow-input-editor small{font-size:8px;line-height:1.5;color:#6f827c}.workflow-input-actions{display:flex;gap:6px;flex-wrap:wrap}.workflow-input-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.workflow-input-grid .wide{grid-column:1/-1}.workflow-asset-picker{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px;align-items:center}.workflow-asset-picker>.button{white-space:nowrap;min-height:38px}.workflow-dynamic-inputs{display:flex;flex-direction:column;gap:9px;padding-top:9px;border-top:1px solid rgba(104,197,167,.16)}.workflow-reference-row{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr) auto;gap:8px;align-items:end;padding:9px;border:1px solid var(--border);border-radius:7px;background:#0b0e12}.workflow-reference-row.video{grid-template-columns:repeat(2,minmax(0,1fr))}.workflow-reference-row.video>.button{justify-self:end}.workflow-input-check{display:flex;align-items:center;gap:7px;min-height:38px;color:#9ca6aa;font-size:9px}.workflow-input-alert{display:flex;gap:8px;color:#e4868d}.input-error{color:#e4868d!important}.spin{animation:spin .9s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:780px){.workflow-input-grid,.workflow-reference-row,.workflow-reference-row.video{grid-template-columns:1fr}.workflow-input-editor>header,.workflow-dynamic-inputs>header{flex-direction:column}}
@media(max-width:520px){.workflow-asset-picker{grid-template-columns:1fr}.workflow-asset-picker>.button{width:100%}}
</style>
