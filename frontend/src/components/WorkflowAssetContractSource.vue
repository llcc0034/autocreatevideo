<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import type { JsonObject, ModelProfileVersion, WorkflowProjectorRecommendation, WorkflowProjectorOptions } from '@/services/platformTypes'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { isJsonObject } from '@/services/workflowProjectorDefinition'
import { modelAssetQuantitySource, modelQuantityChoices } from '@/services/workflowAssetContractSource'
import { frozenAssetQuantity, type ProjectorWorkflowContext } from '@/services/workflowProjectorContext'

const props = defineProps<{
  slot: JsonObject
  group?: JsonObject | null
  definition: JsonObject
  recommendation?: WorkflowProjectorRecommendation | null
  modelProfiles?: ModelProfileVersion[]
  modelProfilesError?: string
  initialModelProfileId?: string
  readOnly?: boolean
  workflowContext?: ProjectorWorkflowContext
  comparisonManaged?: boolean
  comparisonProfile?: ModelProfileVersion | null
  comparisonBusy?: boolean
  comparisonError?: string
  quantityCatalog?: WorkflowProjectorOptions['definitionEditor']['choices']['modelQuantityInputs']
  allowQuantitySelection?: boolean
  disabled?: boolean
}>()
const emit = defineEmits<{ 'select-quantity': [value: { conditioning_mode: string; model_input: string }] }>()
const expanded = ref(false)
const selectedId = ref(props.initialModelProfileId || '')
const profile = ref<ModelProfileVersion | null>(null)
const busy = ref(false)
const error = ref('')
const hasWorkflowContext = computed(() => Boolean(props.workflowContext && props.workflowContext.state !== 'none'))
const displayedProfile = computed(() => props.comparisonManaged ? props.comparisonProfile || null : profile.value)
const displayedBusy = computed(() => props.comparisonManaged ? props.comparisonBusy : busy.value)
const displayedError = computed(() => props.comparisonManaged ? props.comparisonError : error.value)
const frozenQuantity = computed(() => frozenAssetQuantity(props.workflowContext, props.slot.key))
let requestId = 0
const invalidate = () => { requestId++; profile.value = null; error.value = ''; busy.value = false }
watch(selectedId, invalidate, { flush: 'sync' })
watch(() => props.initialModelProfileId, value => { selectedId.value = value || '' })
watch(() => props.slot.contract_source, () => { expanded.value = false; invalidate() })
watch([hasWorkflowContext, () => props.comparisonManaged], invalidate, { flush: 'sync' })
onBeforeUnmount(() => { requestId++ })
const readModel = async () => {
  const id = selectedId.value
  if (!id || hasWorkflowContext.value || props.comparisonManaged) return
  const request = ++requestId
  profile.value = null; error.value = ''; busy.value = true
  try {
    const result = await platformApi.getModelProfile(id)
    if (request !== requestId) return
    if (result.id !== id) throw new Error('返回的模型版本与所选版本不一致')
    profile.value = result
  } catch (cause) {
    if (request === requestId) error.value = apiErrorSummary(cause)
  } finally { if (request === requestId) busy.value = false }
}
watch([expanded, selectedId], ([open, id]) => {
  if (open && id && props.slot.contract_source === 'model_profile' && !hasWorkflowContext.value && !props.comparisonManaged) void readModel()
})
const source = computed(() => {
  const result = modelAssetQuantitySource(displayedProfile.value, props.group || null)
  if (!result.message && props.allowQuantitySelection && props.quantityCatalog
    && !props.quantityCatalog.some(row => row.value === props.group?.model_input && row.mediaKind === props.slot.media_kind)) {
    return { ...result, message: '当前数量输入与本组媒体类型不匹配，请显式重新选择；原配置未自动改写。' }
  }
  return result
})
const canEditQuantity = computed(() => Boolean(props.allowQuantitySelection && !props.readOnly && !props.disabled
  && !hasWorkflowContext.value && !props.comparisonManaged && props.slot.contract_source === 'model_profile'))
const quantityOptions = computed(() => modelQuantityChoices(displayedProfile.value, props.slot.media_kind, props.quantityCatalog))
const selectedQuantity = ref('')
watch([displayedProfile, () => props.slot.media_kind, () => props.slot.key, () => props.slot.contract_source, canEditQuantity], () => { selectedQuantity.value = '' }, { flush: 'sync' })
const chosenQuantity = computed(() => quantityOptions.value.choices.find(row => row.value === selectedQuantity.value))
const applyQuantity = () => {
  if (!canEditQuantity.value || displayedBusy.value || displayedError.value || !chosenQuantity.value) return
  emit('select-quantity', { conditioning_mode: chosenQuantity.value.modeKey, model_input: chosenQuantity.value.inputKey })
}
const modelInputs = (mode: JsonObject) => isJsonObject(mode.inputs) ? Object.entries(mode.inputs).filter(([,value]) => isJsonObject(value)) as [string, JsonObject][] : []
const roleFacts = computed(() => {
  const roles = isJsonObject(props.definition.node_roles) ? props.definition.node_roles : {}
  const group = props.group
  const names = group ? [group.source_role, group.output_role, group.reducer_role,
    ...(Array.isArray(group.branch_roles) ? group.branch_roles : []),
    ...(Array.isArray(group.target_roles) ? group.target_roles : [])] : []
  return Object.fromEntries([...new Set(names.filter((value): value is string => typeof value === 'string' && Boolean(value)))].map(name => [name, roles[name] ?? null]))
})
const pretty = (value: unknown) => JSON.stringify(value, null, 2)
</script>

<template>
  <section class="asset-contract-source">
    <button type="button" class="button ghost small" :aria-expanded="expanded" @click="expanded=!expanded">{{ expanded?'收起来源详情':canEditQuantity?'选择模型数量声明 / 查看来源':'查看数量合同来源' }}</button>
    <div v-if="expanded" class="source-content">
      <template v-if="hasWorkflowContext">
        <p v-if="workflowContext?.state==='loading'" role="status">正在读取工作流冻结合同…</p>
        <p v-else-if="workflowContext?.state==='error'" role="alert">{{ workflowContext.message }}。请在上方重试读取上下文。</p>
        <template v-else-if="workflowContext?.state==='ready'">
          <strong>工作流已冻结的素材数量</strong>
          <p>{{ workflowContext.workflow.templateName }} v{{ workflowContext.workflow.version }} · 绑定模型 {{ workflowContext.model.profileKey }} v{{ workflowContext.model.version }}</p>
          <dl v-if="frozenQuantity"><div><dt>执行数量范围</dt><dd>{{ frozenQuantity.minimum }}–{{ frozenQuantity.maximum }} 份</dd></div><div><dt>冻结字段</dt><dd><code>{{ frozenQuantity.path }}</code></dd></div></dl>
          <p v-else role="alert">该素材槽没有可识别的冻结数量组；请查看工作流合同，不使用当前模型范围代替。</p>
          <dl><div><dt>工作流输入合同 SHA</dt><dd>{{ workflowContext.workflow.inputContractSha256 }}</dd></div><div><dt>生成时的数量来源</dt><dd>{{ slot.contract_source==='model_profile'?'绑定模型版本合同':'Projector 中配置' }}</dd></div></dl>
          <p>以上是工作流版本保存的执行范围，不是用当前模型重新计算的预览；同时受合同的总量、用途和配对约束。</p>
          <RouterLink :to="`/settings/workflows/${workflowContext.workflow.id}`">查看完整工作流合同</RouterLink>
          <RouterLink :to="{path:'/settings',query:{section:'models',model_profile_version:workflowContext.model.id}}">查看绑定模型能力版本</RouterLink>
        </template>
      </template>
      <template v-else-if="slot.contract_source==='workflow'">
        <strong>数量在 Projector 中配置，工作流节点能力负责校验</strong>
        <p>不是另一个待查找的“工作流合同文件”，也不是本次上传素材的位置。</p>
        <template v-if="group">
          <dl><div><dt>本组声明范围</dt><dd>{{ group.minimum_items ?? '未填写' }}–{{ group.maximum_items ?? '未填写' }} 份</dd></div><div><dt>对应配置</dt><dd><code>{{ group.configuration_path || 'projection.config.groups' }}[{{ slot.key }}].minimum_items / maximum_items</code></dd></div><div><dt>目标端口</dt><dd>{{ Array.isArray(group.input_names)&&group.input_names.length?group.input_names.join('、'):group.prefix ? String(group.prefix) + '0…' : '尚未配置' }}</dd></div></dl>
          <p>{{ readOnly?'这里展示已发布 Projector 的数量规则，不是某条工作流的验收结论。':'这里展示当前表单值，不代表已保存或验收。' }}生成合同时会检查实际节点端口、连接与安全预算，再冻结为 Workflow 版本中的 {{ group.compiled_field || 'asset_groups' }} 范围。</p>
          <details><summary>查看当前槽声明、接线规则与节点角色</summary><pre>{{ pretty({asset_slot:slot,group,node_roles:roleFacts}) }}</pre></details>
        </template>
        <template v-else><p>当前槽未关联通用动态组。固定引擎会按实际素材输入是否必需生成 1..1 或 0..1 的槽；仅勾选“动态”不产生动态接线。</p><pre>{{ pretty(slot) }}</pre></template>
        <template v-if="recommendation">
          <strong>本次推荐使用的扫描证据（不是重新在线校验）</strong>
          <dl><div><dt>不可变候选 ID</dt><dd>{{ recommendation.sourceEvidence.candidateSourceId }}</dd></div><div><dt>图 SHA</dt><dd>{{ recommendation.sourceEvidence.convertedGraphSha256 }}</dd></div><div><dt>节点 Schema SHA</dt><dd>{{ recommendation.sourceEvidence.objectInfoSchemaSha256 }}</dd></div></dl>
          <details><summary>查看扫描节点、端口与连接</summary><pre>{{ pretty({node_types:recommendation.catalog.nodeTypes,input_names:recommendation.catalog.inputsByNodeType,edges:recommendation.catalog.observedEdges}) }}</pre></details>
        </template>
        <p v-else class="source-warning">当前编辑会话未携带候选扫描证据。Projector 方案本身不绑定某一个来源文件；如需核对具体图，请从已纳管候选生成推荐，或在生成合同后查看该 Workflow 版本证据。</p>
      </template>
      <template v-else-if="slot.contract_source==='model_profile'">
        <strong>模型数量声明对照（可选）</strong>
        <p>来源是模型版本的 specification.conditioning_contract，不是模型文件名或模型部署。切换对照版本不会绑定模型或修改 Projector；{{ canEditQuantity?'下方明确点击应用才修改本组模式和输入 Key。':'这里只读查看。' }}</p>
        <template v-if="!comparisonManaged">
        <label>查看哪个模型版本<select v-model="selectedId" aria-label="查看哪个模型版本"><option value="">请选择精确版本</option><option v-for="model in modelProfiles || []" :key="model.id" :value="model.id">{{ model.profileKey }} · v{{ model.version }} · {{ model.status }}</option></select></label>
        <p v-if="modelProfilesError" role="alert">模型版本目录读取失败：{{ modelProfilesError }}。请关闭弹框后重新读取目录。</p>
        <p v-else-if="!modelProfiles?.length">模型版本目录为空或尚未加载，请重新读取系统设置。</p>
        <button type="button" class="button ghost small" :disabled="!selectedId||busy" @click="readModel">{{ busy?'读取中…':profile?'重新读取此版本合同':'读取此版本合同' }}</button>
        </template>
        <p v-if="displayedError" class="source-warning" role="alert">读取失败：{{ displayedError }}。可重试读取，未使用旧结果。</p>
        <p v-else-if="displayedBusy" role="status">正在读取对照模型…</p>
        <template v-else>
          <p v-if="!displayedProfile">未加载对照模型{{ comparisonManaged?'，可在上方“选择模型进行对照”中选择':'' }}。这是可选查看，不表示已发布规则缺失或验收失败。</p>
          <p v-else-if="source.message" class="source-warning" role="alert">与所选模型不匹配：{{ source.message }}</p>
          <template v-if="displayedProfile">
            <fieldset v-if="canEditQuantity" class="quantity-picker">
              <legend>从此版本选择本组数量声明</legend>
              <p>只列出媒体类型匹配、最大数量大于 0 的已注册输入；无效范围不会列入。现有配置不会自动替换，范围不复制到 Projector。</p>
              <p v-if="quantityOptions.message" role="alert">{{ quantityOptions.message }}</p>
              <label>模型模式与数量输入<select v-model="selectedQuantity" :disabled="!quantityOptions.choices.length" aria-label="模型模式与数量输入"><option value="">请选择模式与输入</option><option v-for="choice in quantityOptions.choices" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
              <button type="button" class="button ghost small" :disabled="!chosenQuantity" @click="applyQuantity">应用所选数量声明到本组</button>
              <p>此操作仅修改草稿的查找路径，不保存模型 ID，也不代表节点支持、组合烟测通过或已保存。生成工作流时仍须校验实际绑定版本。</p>
            </fieldset>
            <dl><div><dt>对照版本 / ID</dt><dd>{{ displayedProfile.profileKey }} v{{ displayedProfile.version }} · {{ displayedProfile.status }}<br/>{{ displayedProfile.id }}</dd></div><div><dt>模型能力 SHA</dt><dd>{{ displayedProfile.specificationSha256 }}</dd></div><div><dt>本组查找路径</dt><dd><code>specification.conditioning_contract.modes[key={{ group?.conditioning_mode || '未填写' }}].inputs[{{ group?.model_input || '未填写' }}]</code></dd></div></dl>
            <p v-if="!source.message&&source.input">该版本匹配到的数量范围：<strong>{{ source.input.minimum_items }}–{{ source.input.maximum_items }} 份</strong>。生成工作流合同时仍需选择同一精确模型版本并校验节点接线。</p>
            <div class="source-table" v-if="source.modes.length"><table><caption>此版本实际声明的模式与数量输入</caption><thead><tr><th>模式 Key</th><th>输入 Key</th><th>最少</th><th>最多</th></tr></thead><tbody><template v-for="(mode,index) in source.modes" :key="index"><tr v-for="[key,input] in modelInputs(mode)" :key="key"><td>{{ mode.key }}</td><td>{{ key }}</td><td>{{ input.minimum_items ?? '未声明' }}</td><td>{{ input.maximum_items ?? '未声明' }}</td></tr></template></tbody></table></div>
            <details v-if="source.contract"><summary>查看模型数量合同原文（只读）</summary><pre>{{ pretty(source.contract) }}</pre></details>
            <p v-if="group?.compiled_field==='classified_references'">分类参考必须有精确模型合同；“Projector 中配置”只能在模型范围内收窄，不能绕过缺失或错误的模型合同。</p>
            <p v-else>若所需模型版本没有这份合同，不能猜测或借用其他模型的范围。通用动态接线可显式声明经节点验证的“Projector 中配置”数量规则，但这不是模型能力认证；查看其他版本仅用于对比。</p>
          </template>
        </template>
      </template>
      <p v-else role="alert">尚未选择有效的数量合同来源。</p>
    </div>
  </section>
</template>

<style scoped>
.quantity-picker{display:grid;gap:10px;min-width:0;margin:0;padding:10px;border:1px solid #354350}.quantity-picker legend{padding:0 4px}.source-content select:focus-visible,.source-content button:focus-visible{outline:2px solid #d8a051;outline-offset:2px}
.asset-contract-source,.source-content{display:grid;gap:10px;min-width:0;font-size:var(--config-body-size);letter-spacing:0}
.asset-contract-source button{justify-self:start;min-height:36px;white-space:normal;font:inherit}
.source-content{border-left:2px solid #9a753f;padding:10px 12px;background:#0b1118;color:#aab9c7;line-height:1.65}
.source-content p{margin:0;font-size:var(--config-help-size)}.source-content strong{color:#d7e1e9}
.source-content label{display:grid;gap:5px}.source-content select{width:100%;min-width:0;min-height:36px;background:#080d12;color:#d7e1e9;padding:8px;border:1px solid #354350;border-radius:var(--config-radius);font:inherit}
.source-content dl{display:grid;gap:8px;margin:0}.source-content dt{color:#8697a7}.source-content dd{margin:0;overflow-wrap:anywhere}
.source-content details{min-width:0}.source-content summary{cursor:pointer;min-height:36px;display:flex;align-items:center;color:#adc8e0}
.source-content pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:300px;overflow:auto;padding:10px;border:1px solid #293642;font-size:var(--config-help-size)}
.source-warning{color:#e2bb7d}.source-table{overflow:auto}.source-table table{width:100%;border-collapse:collapse;text-align:left}.source-table th,.source-table td{padding:6px;border-bottom:1px solid #293642;overflow-wrap:anywhere}.source-table caption{text-align:left;padding:6px 0}
@media(max-width:700px){.asset-contract-source :is(button,select,summary){min-height:44px}}
</style>
