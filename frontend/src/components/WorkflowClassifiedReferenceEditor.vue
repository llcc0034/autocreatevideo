<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { numericInputDrafts } from '@/services/numericInputDrafts'
import type { JsonObject, ModelProfileVersion, WorkflowProjectorOptions, WorkflowProjectorRecommendation } from '@/services/platformTypes'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { cloneWorkflowProjectorDefinition, isJsonObject } from '@/services/workflowProjectorDefinition'
import { assetGroupForSlot, classifiedQuantityComparison } from '@/services/workflowAssetContractSource'
import WorkflowAssetContractSource from './WorkflowAssetContractSource.vue'
import { frozenAssetQuantity, projectorModelComparison, type ProjectorWorkflowContext } from '@/services/workflowProjectorContext'

const props = defineProps<{ modelValue: JsonObject; options: WorkflowProjectorOptions; recommendation?: WorkflowProjectorRecommendation | null; disabled?: boolean; readOnly?: boolean; workflowContext?: ProjectorWorkflowContext; modelProfiles?: ModelProfileVersion[]; modelProfilesError?: string; initialModelProfileId?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: JsonObject]; validity: [errors: string[]] }>()
const disabled = computed(() => props.disabled || props.readOnly)
const obj = (value: unknown): JsonObject => isJsonObject(value) ? value : {}
const rows = (value: unknown): JsonObject[] => Array.isArray(value) ? value.filter(isJsonObject) : []
const config = computed(() => obj(obj(props.modelValue.projection).config))
const groups = computed(() => obj(config.value.dynamic_groups))
const slots = computed(() => rows(props.modelValue.asset_slots))
const roles = computed(() => Object.entries(obj(props.modelValue.node_roles)).filter(([, value]) => obj(value).cardinality === 'one').map(([key]) => key))
const selectedId = ref(props.initialModelProfileId || '')
const profile = ref<ModelProfileVersion | null>(null)
const error = ref('')
const busy = ref(false)
const comparisonOpen = ref(!props.readOnly)
const hasWorkflowContext = computed(() => Boolean(props.workflowContext && props.workflowContext.state !== 'none'))
let serial = 0
const readModel = async () => {
  const id = selectedId.value, request = ++serial
  profile.value = null; error.value = ''; busy.value = Boolean(id)
  if (!id || hasWorkflowContext.value) { busy.value = false; return }
  try {
    const value = await platformApi.getModelProfile(id)
    if (request !== serial) return
    if (value.id !== id) throw new Error('模型版本返回身份不一致')
    profile.value = value
  } catch (cause) { if (request === serial) error.value = apiErrorSummary(cause) }
  finally { if (request === serial) busy.value = false }
}
watch(() => props.initialModelProfileId, value => { selectedId.value = value || '' })
watch([selectedId, comparisonOpen, hasWorkflowContext], () => {
  serial++; profile.value = null; error.value = ''; busy.value = false
  if (comparisonOpen.value && !hasWorkflowContext.value) void readModel()
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => { serial++ })
const modes = computed(() => rows(obj(profile.value?.specification.conditioning_contract).modes).filter(row => row.input_kind === 'classified_references'))
const capabilities = computed(() => Array.isArray(profile.value?.specification.capabilities) ? profile.value!.specification.capabilities.map(String) : [])
const comparison = computed(() => projectorModelComparison(config.value, profile.value, busy.value, error.value))
const text = (event: Event) => (event.target as HTMLInputElement).value
const labels: Record<string, string> = { images: '参考图片', videos: '参考视频', video_audios: '视频配套音频', audios: '独立参考音频' }
const mediaKinds: Record<string, string> = { images: 'image', videos: 'video', video_audios: 'audio', audios: 'audio' }
const change = (field: string, value: unknown) => {
  if (props.disabled || props.readOnly) return
  const definition = cloneWorkflowProjectorDefinition(props.modelValue)
  obj(definition.projection).config = { ...config.value, [field]: value }
  emit('update:modelValue', definition)
}
const changeGroup = (key: string, field: string, value: unknown) => {
  const row = { ...obj(groups.value[key]), [field]: value }
  if (field === 'prefix' && value) row.input_names = []
  if (field === 'input_names' && Array.isArray(value) && value.length) row.prefix = ''
  change('dynamic_groups', { ...groups.value, [key]: row })
}
const slot = (key: string) => slots.value.find(row => row.key === key)
const numberKey = (key: string, field: string) => JSON.stringify([key,field])
const quantityFields = [{key:'minimum_items',label:'最少数量'},{key:'maximum_items',label:'最多数量'}]
const numberLabel = (key: string, field: { label: string }) => `${labels[key] || key} · ${field.label}`
const numbers = numericInputDrafts(() => Object.keys(groups.value).filter(key => slot(key)?.contract_source === 'workflow').flatMap(key => quantityFields.map(field => ({key:numberKey(key,field.key),label:numberLabel(key,field)}))), () => disabled.value)
watch(numbers.errors, errors => emit('validity',errors), { immediate: true })
const numeric = (key: string, field: string, event: Event) => {
  if (numbers.capture(numberKey(key,field),event)) changeGroup(key,field,text(event) === '' ? null : Number(text(event)))
}
const quantities = computed(() => Object.fromEntries(Object.keys(groups.value).map(key => [key,
  classifiedQuantityComparison(profile.value, assetGroupForSlot(props.modelValue, key), slot(key)),
])))
const source = (key: string, value: string) => {
  if (props.disabled || props.readOnly) return
  for (const field of quantityFields) numbers.discard(numberKey(key,field.key))
  const definition = cloneWorkflowProjectorDefinition(props.modelValue)
  definition.asset_slots = [ ...slots.value.filter(row => row.key !== key), { key, media_kind: mediaKinds[key], dynamic: true, contract_source: value } ]
  const nextConfig = obj(obj(definition.projection).config)
  const row = obj(obj(nextConfig.dynamic_groups)[key])
  Object.assign(row, value === 'model_profile' ? { minimum_items: null, maximum_items: null } : { minimum_items: 0, maximum_items: 0 })
  emit('update:modelValue', definition)
}
const inputs = computed(() => props.recommendation?.catalog.inputsByNodeType[String(obj(obj(props.modelValue.node_roles)[String(config.value.target_role)]).node_type)] || [])
const staticAudio = computed(() => obj(config.value.static_semantics)['audio.enabled'])
const setAudio = (value: string) => {
  const next = { ...obj(config.value.static_semantics) }
  if (value === '') delete next['audio.enabled']; else next['audio.enabled'] = value === 'true'
  change('static_semantics', next)
}
</script>

<template>
  <section class="classified-config projector-reference-editor">
    <p>这里统一配置四类动态素材。素材文件在运行时选择；模型名称、节点类型和端口均为方案数据。当前分类模式用于视频工作流，多图编辑可使用“通用动态多素材接线”。</p>
    <details v-if="!hasWorkflowContext" :open="comparisonOpen" @toggle="comparisonOpen=($event.target as HTMLDetailsElement).open">
      <summary>{{ readOnly ? '选择模型进行对照（可选）' : '从模型声明选择能力与模式' }}</summary>
      <div v-if="comparisonOpen" class="fields">
        <label>对照模型版本<select v-model="selectedId"><option value="">请选择对照版本</option><option v-for="model in modelProfiles || []" :key="model.id" :value="model.id">{{ model.profileKey }} · v{{ model.version }}</option></select></label>
        <button type="button" :disabled="busy || !selectedId" @click="readModel">{{ busy ? '读取中…' : '重新读取对照模型' }}</button>
      </div>
      <template v-if="comparisonOpen">
        <p :role="comparison.state==='error'||comparison.state==='mismatch'?'alert':'status'">{{ comparison.label }}。仅对照能力与模式，不代表在线校验或烟测结果。</p>
        <p v-if="error || modelProfilesError" role="alert">{{ error || modelProfilesError }}</p>
      </template>
    </details>
    <div class="fields">
      <template v-if="readOnly"><label>能力标识（已发布规则）<output>{{ config.capability }}</output></label><label>参考模式（已发布规则）<output>{{ config.conditioning_mode }}</output></label></template>
      <template v-else>
        <label>能力标识<select :value="config.capability" :disabled="disabled || !profile" @change="change('capability',text($event))"><option value="">请选择模型声明的能力</option><option v-if="config.capability && !capabilities.includes(String(config.capability))" :value="config.capability">{{ config.capability }}（当前配置值）</option><option v-for="key in capabilities" :key="key">{{ key }}</option></select></label>
        <label>参考模式<select :value="config.conditioning_mode" :disabled="disabled || !profile" @change="change('conditioning_mode',text($event))"><option value="">请选择分类参考模式</option><option v-if="config.conditioning_mode && !modes.some(row=>row.key===config.conditioning_mode)" :value="config.conditioning_mode">{{ config.conditioning_mode }}（当前配置值）</option><option v-for="row in modes" :key="String(row.key)" :value="row.key">{{ row.label }} · {{ row.key }}</option></select></label>
      </template>
      <label>原图是否输出音频<select :value="staticAudio === undefined ? '' : String(staticAudio)" :disabled="disabled" @change="setAudio(text($event))"><option value="">不声明</option><option value="true">输出音频</option><option value="false">不输出音频</option></select><small>这是图的固定事实，不会自动增加或删除音频节点。</small></label>
    </div>
    <label>允许的提示词模板键（可留空不限制，每行一个）<textarea :value="Array.isArray(config.prompt_template_keys) ? config.prompt_template_keys.join('\n') : ''" :disabled="disabled" rows="2" @change="change('prompt_template_keys',text($event).split(/\r?\n/).map(v=>v.trim()).filter(Boolean))"/></label>
    <details class="reference-implementation">
      <summary>实现详情 · 接收素材的节点角色</summary>
      <label>接收素材的目标角色<select :value="config.target_role" :disabled="disabled" @change="change('target_role',text($event))"><option value="">请选择唯一节点角色</option><option v-for="role in roles" :key="role">{{ role }}</option></select></label>
      <p>各媒体组按此角色的真实端口接入；不会按模型名称推测或补写节点。</p>
    </details>
    <article v-for="(value,key) in groups" :key="key">
      <strong>{{ labels[key] || key }}</strong>
      <p v-if="workflowContext?.state==='ready'">工作流冻结数量：<strong v-if="frozenAssetQuantity(workflowContext,key)">{{ frozenAssetQuantity(workflowContext,key)!.minimum }}–{{ frozenAssetQuantity(workflowContext,key)!.maximum }} 份</strong><span v-else role="alert">该素材组的冻结数量缺失或无效，请查看工作流合同。</span></p>
      <small>模型数量输入：{{ obj(value).mode_input }}<template v-if="key==='video_audios'">；端口按视频序号对应，可有空位。</template></small>
      <div v-if="!hasWorkflowContext && profile" class="quantity-comparison">
        <p>对照模型：{{ profile.profileKey }} v{{ profile.version }} · {{ config.conditioning_mode }}</p>
        <p v-if="quantities[key]?.model">模型输入范围：{{ quantities[key]!.model!.minimum }}–{{ quantities[key]!.model!.maximum }} 份<template v-if="quantities[key]?.total">；图片总量：{{ quantities[key]!.total!.minimum }}–{{ quantities[key]!.total!.maximum }} 份</template></p>
        <p :role="quantities[key]?.state==='conflict'?'alert':'status'">{{ quantities[key]?.message || '数量范围与对照模型一致。' }}</p>
        <small>仅比较当前表单数量，不是保存、在线校验或 Smoke 结果；用途、配对、预处理、总量与实际节点端口仍须共同满足。</small>
      </div>
      <div class="fields">
        <label>数量合同来源<select :value="slot(key)?.contract_source || ''" :disabled="disabled" @change="source(key,text($event))"><option value="">请选择来源</option><option v-for="choice in options.definitionEditor.choices.contractSources" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
        <template v-if="slot(key)?.contract_source==='workflow'"><label v-for="field in quantityFields" :key="field.key">{{ field.label }}<input type="number" min="0" :value="numbers.value(numberKey(key,field.key),obj(value)[field.key])" :aria-label="numberLabel(key,field)" :aria-invalid="numbers.invalid(numberKey(key,field.key))" :disabled="disabled" @input="numeric(key,field.key,$event)"/><button v-if="numbers.invalid(numberKey(key,field.key))" type="button" :disabled="disabled" :aria-label="`撤销未完成输入：${numberLabel(key,field)}`" @click="numbers.discard(numberKey(key,field.key))">撤销未完成输入</button></label></template>
      </div>
      <WorkflowAssetContractSource v-if="slot(key)" :slot="slot(key)!" :group="assetGroupForSlot(modelValue,key)" :definition="modelValue" :recommendation="recommendation" :model-profiles="modelProfiles" :model-profiles-error="modelProfilesError" :initial-model-profile-id="selectedId" :read-only="readOnly" :workflow-context="workflowContext" :comparison-managed="true" :comparison-profile="profile" :comparison-busy="busy" :comparison-error="error"/>
      <details class="reference-implementation">
        <summary>实现详情 · {{ labels[key] || key }}的端口映射</summary>
        <div class="fields">
          <label>动态端口完整前缀（从 0 编号）<input :value="obj(value).prefix" :disabled="disabled" @change="changeGroup(key,'prefix',text($event).trim())"/><small>与固定端口列表二选一。填写前缀会清空列表。</small></label>
          <label>或：固定端口列表（按顺序，每行一个）<textarea :value="Array.isArray(obj(value).input_names) ? (obj(value).input_names as unknown[]).join('\n') : ''" :disabled="disabled" rows="2" @change="changeGroup(key,'input_names',text($event).split(/\r?\n/).map(v=>v.trim()).filter(Boolean))"/></label>
        </div>
      </details>
    </article>
    <p v-for="message in numbers.errors.value" :key="message" role="alert">{{ message }}</p>
    <small v-if="readOnly">以上接线规则随此 Projector 版本发布；具体节点端口的验证结果，以对应工作流的验收证据为准。</small>
    <small v-else>已扫描的目标输入：{{ inputs.join('、') || '当前会话没有节点目录；请从工作流候选生成推荐后核对' }}。生成合同和在线校验会核对端口能力，不能用更大的数量绕过节点限制。</small>
    <p>帧数、FPS、步长和 LoRA 强度{{ readOnly?'可在“参数与素材 → 参数策略”查看':'在“参数与素材 → 参数策略”配置' }}，不在这里重复维护。</p>
    <p>分类参考的“Projector 中配置”只允许在精确模型能力内收窄数量，不能覆盖或修正模型合同；若收窄后与用途、配对或预处理规则冲突，将阻止生成合同，不会自动删除这些规则。</p>
  </section>
</template>

<style src="./projectorReferenceEditor.css"></style>
