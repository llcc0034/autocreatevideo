<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ConditioningContractEditorOptions, JsonObject, ModelProfileVersion } from '@/services/platformTypes'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { conditioningContractErrors, contractObject, contractRows, newConditioningMode, updateConditioningQuantity } from '@/services/modelConditioningContract'
const props = defineProps<{ modelValue?: unknown; options?: ConditioningContractEditorOptions | null; modelProfiles: ModelProfileVersion[]; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: JsonObject | undefined]; validity: [errors: string[]] }>()
const contract = computed(() => contractObject(props.modelValue))
const modes = computed(() => contractRows(contract.value.modes))
const kind = ref('')
const copyId = ref('')
const loading = ref(false)
const error = ref('')
let request = 0
watch(copyId, () => { request++; loading.value = false; error.value = '' })
const errors = computed(() => conditioningContractErrors(props.modelValue, props.options))
watch(errors, value => emit('validity', value), { immediate: true })
const availableModels = computed(() => props.modelProfiles.filter(row => ['image','video','multimodal'].includes(row.modality) && row.status !== 'retired'))
const blocked = computed(() => props.disabled || loading.value || !props.options)
function setModes(value: JsonObject[]) {
  if (blocked.value || !props.options) return
  emit('update:modelValue', value.length ? { ...contract.value, schema_version: props.options.schemaVersion, adapter_key: props.options.adapterKey, modes: value } : undefined)
}
function change(index: number, field: string, value: unknown) { setModes(modes.value.map((row, i) => i === index ? { ...row, [field]: value } : row)) }
function quantity(index: number, slot: string, field: string, raw: string) { setModes(modes.value.map((row, i) => i === index ? updateConditioningQuantity(row, slot, field, raw === '' ? NaN : Number(raw)) : row)) }
function add() { if (props.options && kind.value) setModes([...modes.value, newConditioningMode(props.options, kind.value, modes.value)]) }
async function copyContract() {
  if (blocked.value || !copyId.value) return
  if (modes.value.length && !window.confirm('将替换当前草稿中的素材合同（不会改动其他模型规格）。是否继续？')) return
  const ticket = ++request
  loading.value = true; error.value = ''
  try {
    const source = await platformApi.getModelProfile(copyId.value)
    if (ticket !== request || props.disabled) return
    const value = source.specification.conditioning_contract
    if (!value || !contractRows(contractObject(value).modes).length) throw new Error('所选模型版本没有素材数量合同，当前草稿未改变')
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
    <article v-for="(mode,index) in modes" :key="index">
      <div class="mode-fields">
        <label>模式标识<input :value="mode.key" :pattern="options?.identifierPattern" :disabled="blocked" @input="change(index,'key',($event.target as HTMLInputElement).value)"/><small>Projector 引用此标识，例如 Ref2VA；不要随意改动已有引用。</small></label>
        <label>模式名称<input :value="mode.label" :disabled="blocked" @input="change(index,'label',($event.target as HTMLInputElement).value)"/></label>
        <label>素材形态<select :value="mode.input_kind" :disabled="blocked" @change="change(index,'input_kind',($event.target as HTMLSelectElement).value)"><option v-for="choice in options?.inputKinds" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
        <button type="button" class="button ghost small" :disabled="blocked" @click="setModes(modes.filter((_,i)=>i!==index))">删除此模式</button>
      </div>
      <label>说明<input :value="mode.description" :disabled="blocked" @input="change(index,'description',($event.target as HTMLInputElement).value)"/></label>
      <div class="flags"><label><input type="checkbox" :checked="mode.requires_entity_reference_bible === true" :disabled="blocked" @change="change(index,'requires_entity_reference_bible',($event.target as HTMLInputElement).checked)"/>需要实体参考素材库</label><label><input type="checkbox" :checked="mode.supports_native_audio === true" :disabled="blocked" @change="change(index,'supports_native_audio',($event.target as HTMLInputElement).checked)"/>支持原生音频输出</label></div>
      <div class="quantity-table"><div class="quantity-row"><strong>素材槽</strong><strong>最少数量</strong><strong>最多数量</strong></div>
        <div v-for="slot in options?.inputSlots" :key="slot.value" class="quantity-row"><span>{{ slot.label }}<small>{{ slot.value }}</small></span><input type="number" min="0" :max="options?.maximumItems" step="1" :aria-label="`${slot.label}最少数量`" :value="bounds(mode,slot.value).minimum_items ?? ''" :disabled="blocked" @input="quantity(index,slot.value,'minimum_items',($event.target as HTMLInputElement).value)"/><input type="number" min="0" :max="options?.maximumItems" step="1" :aria-label="`${slot.label}最多数量`" :value="bounds(mode,slot.value).maximum_items ?? ''" :disabled="blocked" @input="quantity(index,slot.value,'maximum_items',($event.target as HTMLInputElement).value)"/></div>
      </div>
      <p>0–0 表示不支持；最少＝最多表示固定数量；最少＜最多表示允许动态数量。图片总数是总体上限，不会自动等于各槽之和。</p>
      <details v-if="mode.dynamic_reference_topology"><summary>已有视频／音频预处理与配对规则（保留）</summary><p>数量修改同步到对应拓扑组；用途级限制仍需满足总量。复杂预处理和用途规则保留在同一份完整模型规格中，不会被数量表单清空。</p><pre>{{ JSON.stringify(mode.dynamic_reference_topology,null,2) }}</pre></details>
    </article>
    <div class="toolbar"><label>新增素材模式<select v-model="kind" :disabled="blocked"><option value="">请选择素材形态</option><option v-for="choice in options?.inputKinds" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label><button type="button" class="button ghost small" :disabled="blocked || !kind || modes.length >= (options?.maximumModes || 0)" @click="add">添加模式</button></div>
    <p v-if="!modes.length">尚未声明素材数量合同。新增模式会自动补齐槽位和系统字段，各槽初始为 0；请按已确认的模型能力填写，不会自动推测上限。</p>
    <ul v-if="errors.length" role="alert"><li v-for="item in errors" :key="item">{{ item }}</li></ul>
  </section>
</template>
<style scoped>
.conditioning-editor{display:grid;gap:14px;border:1px solid #354350;border-radius:10px;padding:18px;min-width:0}.conditioning-editor header,.toolbar{display:flex;flex-wrap:wrap;align-items:end;gap:12px}.conditioning-editor header{justify-content:space-between}.conditioning-editor p,small,header span{font-size:12px;color:#96a5b3;line-height:1.6}.conditioning-editor article{display:grid;gap:12px;border:1px solid #29333b;padding:14px;border-radius:8px}.mode-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.conditioning-editor label{display:grid;gap:6px;font-size:12px}.conditioning-editor input,.conditioning-editor select{min-width:0;min-height:36px;box-sizing:border-box;max-width:100%}.conditioning-editor .flags,.flags label{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.quantity-row{display:grid;grid-template-columns:minmax(150px,2fr) repeat(2,minmax(70px,1fr));gap:12px;padding:8px 0;align-items:center}.quantity-row small{display:block}.quantity-row input{width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.conditioning-editor summary{cursor:pointer}
</style>
