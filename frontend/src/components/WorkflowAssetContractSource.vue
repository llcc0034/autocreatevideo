<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import type { JsonObject, ModelProfileVersion, WorkflowProjectorRecommendation } from '@/services/platformTypes'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { isJsonObject } from '@/services/workflowProjectorDefinition'
import { modelAssetQuantitySource } from '@/services/workflowAssetContractSource'
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
}>()
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
const source = computed(() => modelAssetQuantitySource(displayedProfile.value, props.group || null))
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
    <button type="button" class="button ghost small" :aria-expanded="expanded" @click="expanded=!expanded">{{ expanded?'收起来源详情':'查看数量合同来源' }}</button>
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
        <p>来源是模型版本的 specification.conditioning_contract，不是模型文件名或运行实例。这里选择版本仅供查看，不会绑定模型，也不会修改 Projector。</p>
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
            <dl><div><dt>对照版本 / ID</dt><dd>{{ displayedProfile.profileKey }} v{{ displayedProfile.version }} · {{ displayedProfile.status }}<br/>{{ displayedProfile.id }}</dd></div><div><dt>模型能力 SHA</dt><dd>{{ displayedProfile.specificationSha256 }}</dd></div><div><dt>本组查找路径</dt><dd><code>specification.conditioning_contract.modes[key={{ group?.conditioning_mode || '未填写' }}].inputs[{{ group?.model_input || '未填写' }}]</code></dd></div></dl>
            <p v-if="!source.message&&source.input">该版本匹配到的数量范围：<strong>{{ source.input.minimum_items }}–{{ source.input.maximum_items }} 份</strong>。生成工作流合同时仍需选择同一精确模型版本并校验节点接线。</p>
            <div class="source-table" v-if="source.modes.length"><table><caption>此版本实际声明的模式与数量输入</caption><thead><tr><th>模式 Key</th><th>输入 Key</th><th>最少</th><th>最多</th></tr></thead><tbody><template v-for="(mode,index) in source.modes" :key="index"><tr v-for="[key,input] in modelInputs(mode)" :key="key"><td>{{ mode.key }}</td><td>{{ key }}</td><td>{{ input.minimum_items ?? '未声明' }}</td><td>{{ input.maximum_items ?? '未声明' }}</td></tr></template></tbody></table></div>
            <details v-if="source.contract"><summary>查看模型数量合同原文（只读）</summary><pre>{{ pretty(source.contract) }}</pre></details>
            <p>若所需模型版本没有这份合同，不能猜测或借用其他模型的范围。可使用经节点验证的“Projector 中配置”数量规则；查看其他版本仅用于对比。</p>
          </template>
        </template>
      </template>
      <p v-else role="alert">尚未选择有效的数量合同来源。</p>
    </div>
  </section>
</template>

<style scoped>
.asset-contract-source,.source-content{display:grid;gap:10px;min-width:0}.asset-contract-source>button{justify-self:start}.source-content{border-left:2px solid #9a753f;padding:10px 12px;background:#0b1118;font-size:11px;color:#aab9c7;line-height:1.65}.source-content p{margin:0}.source-content strong{color:#d7e1e9}.source-content label{display:grid;gap:5px}.source-content select{width:100%;min-width:0;background:#080d12;color:#d7e1e9;padding:8px;border:1px solid #354350;border-radius:6px}.source-content dl{display:grid;gap:8px;margin:0}.source-content dt{color:#8697a7}.source-content dd{margin:0;overflow-wrap:anywhere}.source-content details{min-width:0}.source-content summary{cursor:pointer;min-height:36px;display:flex;align-items:center;color:#adc8e0}.source-content pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:300px;overflow:auto;padding:10px;border:1px solid #293642;font-size:10px}.source-warning{color:#e2bb7d}.source-table{overflow:auto}.source-table table{width:100%;border-collapse:collapse;text-align:left}.source-table th,.source-table td{padding:6px;border-bottom:1px solid #293642;overflow-wrap:anywhere}.source-table caption{text-align:left;padding:6px 0}.source-content button{justify-self:start}
</style>
