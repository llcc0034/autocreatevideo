<script setup lang="ts">
import ModelReferenceTopologyEditor from './ModelReferenceTopologyEditor.vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { ConditioningContractEditorOptions, JsonObject, ModelProfileVersion } from '@/services/platformTypes'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { conditioningContractErrors, contractObject, contractRows, newConditioningMode, updateConditioningQuantity } from '@/services/modelConditioningContract'
import { numericInputDrafts } from '@/services/numericInputDrafts'
const props = defineProps<{ modelValue?: unknown; options?: ConditioningContractEditorOptions | null; modelProfiles: ModelProfileVersion[]; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: JsonObject | undefined]; validity: [errors: string[]] }>()
const contract = computed(() => contractObject(props.modelValue))
const modes = computed(() => contractRows(contract.value.modes))
const kind = ref('')
const copyId = ref('')
const loading = ref(false)
const error = ref('')
let request = 0
onBeforeUnmount(() => { request++ })
watch(copyId, () => { request++; loading.value = false; error.value = '' })
const availableModels = computed(() => props.modelProfiles.filter(row => ['image','video','multimodal'].includes(row.modality) && row.status !== 'retired'))
const blocked = computed(() => props.disabled || loading.value || !props.options)
let nextModeId = 0
const modeIds = ref<number[]>([])
const topologyErrors = ref<Record<number,string[]>>({})
watch(() => modes.value.length, count => {
  modeIds.value = Array.from({length:count},(_,i)=>modeIds.value[i]??nextModeId++)
  for(const id of Object.keys(topologyErrors.value))if(!modeIds.value.includes(Number(id)))delete topologyErrors.value[Number(id)]
}, {immediate:true,flush:'sync'})
const quantityFields = [{key:'minimum_items',label:'最少数量'},{key:'maximum_items',label:'最多数量'}]
const quantityKey = (index:number,slot:string,field:string) => JSON.stringify([modeIds.value[index],slot,field])
const quantityLabel = (index:number,slot:string,field:string) => `素材模式 ${index+1} ${props.options?.inputSlots.find(row=>row.value===slot)?.label||slot} ${quantityFields.find(row=>row.key===field)?.label||field}`
const numbers = numericInputDrafts(() => modes.value.flatMap((_,index) => (props.options?.inputSlots||[]).flatMap(slot => quantityFields.map(field => ({key:quantityKey(index,slot.value,field.key),label:quantityLabel(index,slot.value,field.key)})))),()=>blocked.value)
const errors = computed(() => [...conditioningContractErrors(props.modelValue, props.options),...numbers.errors.value,...modeIds.value.flatMap(id=>topologyErrors.value[id]||[])])
watch(errors, value => emit('validity', value), { immediate: true })
function setModes(value: JsonObject[]) {
  if (blocked.value || !props.options) return
  emit('update:modelValue', value.length ? { ...contract.value, schema_version: props.options.schemaVersion, adapter_key: props.options.adapterKey, modes: value } : undefined)
}
function change(index: number, field: string, value: unknown) { setModes(modes.value.map((row, i) => i === index ? { ...row, [field]: value } : row)) }
function quantity(index: number, slot: string, field: string, event: Event) {
  if(!numbers.capture(quantityKey(index,slot,field),event))return
  const raw=(event.target as HTMLInputElement).value
  setModes(modes.value.map((row,i)=>i===index?updateConditioningQuantity(row,slot,field,raw===''?null:Number(raw)):row))
}
function removeMode(index:number) {
  if(blocked.value||!Number.isInteger(index)||index<0||index>=modes.value.length)return
  const id=modeIds.value[index]!
  numbers.remapKeys(key=>JSON.parse(key)[0]===id?undefined:key)
  modeIds.value.splice(index,1);delete topologyErrors.value[id]
  setModes(modes.value.filter((_,i)=>i!==index))
}
function add() { if (props.options && kind.value) setModes([...modes.value, newConditioningMode(props.options, kind.value, modes.value)]) }
async function copyContract() {
  if (blocked.value || !copyId.value) return
  if (modes.value.length && !window.confirm('将替换当前草稿中的素材合同（不会改动其他模型规格）。是否继续？')) return
  const ticket = ++request
  loading.value = true; error.value = ''
  try {
    const source = await platformApi.getModelProfile(copyId.value)
    if (ticket !== request || props.disabled || !props.options) return
    const value = source.specification.conditioning_contract
    if (!value || !contractRows(contractObject(value).modes).length) throw new Error('所选模型版本没有素材数量合同，当前草稿未改变')
    loading.value=false
    numbers.remapKeys(()=>undefined);topologyErrors.value={}
    modeIds.value=contractRows(contractObject(value).modes).map(()=>nextModeId++)
    emit('update:modelValue', JSON.parse(JSON.stringify(value)) as JsonObject)
  } catch (exc) { if (ticket === request) error.value = apiErrorSummary(exc) }
  finally { if (ticket === request) loading.value = false }
}
const bounds = (mode: JsonObject, slot: string) => contractObject(contractObject(mode.inputs)[slot])
</script>
<template>
  <section class="conditioning-editor">
    <header><strong>模型素材数量合同</strong><span>发布后冻结 · specification.conditioning_contract</span></header>
    <p>这里声明模型支持的素材模式与数量，不是本次运行选几份素材。选择“模型版本合同”的 Projector 按模式标识及槽位读取此处；发布模型后需重新生成、验收工作流。</p>
    <p v-if="!options" role="alert">素材合同选项目录未读取，暂不能编辑。请重新读取系统设置。</p>
    <div class="toolbar">
      <label>复制已有模型合同<select v-model="copyId" :disabled="disabled"><option value="">请选择模型版本</option><option v-for="model in availableModels" :key="model.id" :value="model.id">{{ model.profileKey }} v{{ model.version }} · {{ model.status }}</option></select></label>
      <button type="button" class="button ghost small" :disabled="blocked || !copyId" @click="copyContract">{{ loading ? '读取中…' : '复制素材合同' }}</button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <article v-for="(mode,index) in modes" :key="modeIds[index]">
      <div class="mode-fields">
        <label>模式标识<input :value="mode.key" :pattern="options?.identifierPattern" :disabled="blocked" @input="change(index,'key',($event.target as HTMLInputElement).value)"/><small>Projector 引用此标识，例如 Ref2VA；不要随意改动已有引用。</small></label>
        <label>模式名称<input :value="mode.label" :disabled="blocked" @input="change(index,'label',($event.target as HTMLInputElement).value)"/></label>
        <label>素材形态<select :value="mode.input_kind" :disabled="blocked" @change="change(index,'input_kind',($event.target as HTMLSelectElement).value)"><option v-for="choice in options?.inputKinds" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
        <button type="button" class="button ghost small" :disabled="blocked" @click="removeMode(index)">删除此模式</button>
      </div>
      <label>说明<input :value="mode.description" :disabled="blocked" @input="change(index,'description',($event.target as HTMLInputElement).value)"/></label>
      <div class="flags"><label><input type="checkbox" :checked="mode.requires_entity_reference_bible === true" :disabled="blocked" @change="change(index,'requires_entity_reference_bible',($event.target as HTMLInputElement).checked)"/>需要实体参考素材库</label><label><input type="checkbox" :checked="mode.supports_native_audio === true" :disabled="blocked" @change="change(index,'supports_native_audio',($event.target as HTMLInputElement).checked)"/>支持原生音频输出</label></div>
      <div class="quantity-table" role="region" aria-label="素材数量配置，可横向滚动" tabindex="0"><div class="quantity-row"><strong>素材槽</strong><strong>最少数量</strong><strong>最多数量</strong></div>
        <div v-for="slot in options?.inputSlots" :key="slot.value" class="quantity-row"><span>{{ slot.label }}<small>{{ slot.value }}</small></span><label v-for="field in quantityFields" :key="field.key"><input type="number" min="0" :max="options?.maximumItems" step="1" :aria-label="quantityLabel(index,slot.value,field.key)" :aria-invalid="numbers.invalid(quantityKey(index,slot.value,field.key))" :value="numbers.value(quantityKey(index,slot.value,field.key),bounds(mode,slot.value)[field.key])" :disabled="blocked" @input="quantity(index,slot.value,field.key,$event)"/><button v-if="numbers.invalid(quantityKey(index,slot.value,field.key))" type="button" class="button ghost small" :disabled="blocked" :aria-label="`撤销 ${quantityLabel(index,slot.value,field.key)} 的未完成数字输入`" @click="numbers.discard(quantityKey(index,slot.value,field.key))">撤销未完成输入（保留原配置）</button></label></div>
      </div>
      <p>0–0 表示不支持；最少＝最多表示固定数量；最少＜最多表示允许动态数量。图片总数是总体上限，不会自动等于各槽之和。</p>
      <ModelReferenceTopologyEditor :mode="mode" :options="options" :disabled="blocked" @update:model-value="change(index,'dynamic_reference_topology',$event)" @validity="topologyErrors[modeIds[index]!]=$event"/>
    </article>
    <div class="toolbar"><label>新增素材模式<select v-model="kind" :disabled="blocked"><option value="">请选择素材形态</option><option v-for="choice in options?.inputKinds" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label><button type="button" class="button ghost small" :disabled="blocked || !kind || modes.length >= (options?.maximumModes || 0)" @click="add">添加模式</button></div>
    <p v-if="!modes.length">尚未声明素材数量合同。新增模式会自动补齐槽位和系统字段，各槽初始为 0；请按已确认的模型能力填写，不会自动推测上限。</p>
    <ul v-if="errors.length" role="alert"><li v-for="item in errors" :key="item">{{ item }}</li></ul>
  </section>
</template>
<style scoped>
.conditioning-editor{display:grid;gap:14px;border:1px solid #354350;border-radius:10px;padding:18px;min-width:0}.conditioning-editor header,.toolbar{display:flex;flex-wrap:wrap;align-items:end;gap:12px}.conditioning-editor header{justify-content:space-between}.conditioning-editor p,small,header span{font-size:12px;color:#96a5b3;line-height:1.6}.conditioning-editor article{display:grid;gap:12px;border:1px solid #29333b;padding:14px;border-radius:8px}.mode-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.conditioning-editor label{display:grid;gap:6px;font-size:12px}.conditioning-editor input,.conditioning-editor select{min-width:0;min-height:36px;box-sizing:border-box;max-width:100%}.conditioning-editor .flags,.flags label{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.quantity-row{display:grid;grid-template-columns:minmax(150px,2fr) repeat(2,minmax(70px,1fr));gap:12px;padding:8px 0;align-items:center}.quantity-row small{display:block}.quantity-row input{width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.conditioning-editor summary{cursor:pointer}
.conditioning-editor article,.conditioning-editor label{min-width:0}.quantity-table{min-width:0;overflow-x:auto}.quantity-row{min-width:314px}.toolbar label{max-width:100%}.conditioning-editor{overflow-wrap:anywhere}
.quantity-row label{grid-template-columns:minmax(0,1fr)}
.quantity-row label>.button{min-width:0;max-width:100%;min-height:36px;height:auto;white-space:normal}
</style>
