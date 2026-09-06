<script setup lang="ts">
import { computed } from 'vue'
import type { ModelProfileRegistryOptions, ModelProfileVersion } from '@/services/platformTypes'
import { operationLabel } from '@/services/presentationLabels'
import ModelContractFields from './ModelContractFields.vue'
import { capabilityFieldLabel, contractFieldsExcept, contractScalar, isContractObject, quantityDeclaration } from '@/services/modelCapabilityContract'

const props = defineProps<{ profile: ModelProfileVersion; options?: ModelProfileRegistryOptions | null }>()
const specification = computed(() => isContractObject(props.profile.specification) ? props.profile.specification : {})
const capabilities = computed(() => specification.value.capabilities)
const conditioning = computed(() => specification.value.conditioning_contract)
const modes = computed(() => isContractObject(conditioning.value) && Array.isArray(conditioning.value.modes) ? conditioning.value.modes : [])
const remaining = computed(() => contractFieldsExcept(specification.value, ['schema_version', 'model_family', 'capabilities', 'conditioning_contract']))
const conditioningMeta = computed(() => contractFieldsExcept(conditioning.value, ['modes']))
const modeFields = (mode: unknown) => contractFieldsExcept(mode, ['key', 'label', 'description', 'input_kind', 'inputs', 'dynamic_reference_topology'])
const modeSlots = (mode: Record<string, unknown>) => [...new Set([
  ...(props.options?.conditioningContract.inputSlots.map(slot => slot.value) || []),
  ...Object.keys(isContractObject(mode.inputs) ? mode.inputs : {}),
])]
const slotBounds = (mode: Record<string, unknown>, key: string) => isContractObject(mode.inputs) ? mode.inputs[key] : undefined
const bound = (value: unknown, key: string) => isContractObject(value) ? value[key] : undefined
const capabilityLabel = (value: unknown) => typeof value === 'string'
  ? props.options?.capabilities.find(row => row.value === value)?.label || operationLabel(value) : contractScalar(value)
</script>

<template>
  <div class="model-contract" :key="profile.id">
    <p class="contract-note">以下为此模型版本的能力声明，不代表烟测已通过。工作流实际可用范围以其冻结合同为准，可能小于模型上限。</p>
    <p v-if="!isContractObject(profile.specification)" role="alert">能力合同格式异常，请查看完整 JSON 并核对模型版本。</p>
    <section aria-label="声明能力">
      <h4>声明能力</h4>
      <ul v-if="Array.isArray(capabilities) && capabilities.length" class="capabilities">
        <li v-for="(capability, index) in capabilities" :key="index">{{ capabilityLabel(capability) }}</li>
      </ul>
      <p v-else-if="capabilities === undefined || (Array.isArray(capabilities) && !capabilities.length)" class="muted">尚未声明能力</p>
      <p v-else role="alert">能力列表格式异常：{{ contractScalar(capabilities) }}</p>
      <div class="identity"><span>模型族：{{ contractScalar(specification.model_family) }}</span><span>合同结构版本：{{ contractScalar(specification.schema_version) }}</span></div>
    </section>

    <section aria-label="素材能力合同">
      <h4>素材能力合同<span v-if="modes.length"> · {{ modes.length }} 个模式</span></h4>
      <p v-if="conditioning === undefined" class="muted">此版本未声明素材数量合同；这里不推测可用素材或数量范围。</p>
      <template v-else>
        <p v-if="!isContractObject(conditioning) || !modes.length" role="alert">素材合同格式异常或缺少模式声明。请核对完整规格。</p>
        <ModelContractFields v-if="!isContractObject(conditioning)" :value="conditioning" />
        <article v-for="(mode, index) in modes" :key="index" class="reference-mode">
          <template v-if="isContractObject(mode)">
            <h5>{{ contractScalar(mode.label) }} <small>{{ contractScalar(mode.key) }}</small></h5>
            <p v-if="mode.description !== undefined" class="mode-description">{{ contractScalar(mode.description) }}</p>
            <p>素材形态：{{ options?.conditioningContract.inputKinds.find(kind => kind.value === mode.input_kind)?.label || contractScalar(mode.input_kind) }}<small v-if="options?.conditioningContract.inputKinds.some(kind => kind.value === mode.input_kind)"> · {{ mode.input_kind }}</small></p>
            <ModelContractFields :value="modeFields(mode)" />
            <div v-if="isContractObject(mode.inputs)" class="quantity-scroll" tabindex="0" :aria-label="`${contractScalar(mode.key)} 素材数量表，可横向滚动`">
              <table>
                <caption>素材数量范围（份）</caption>
                <thead><tr><th scope="col">素材</th><th scope="col">最少</th><th scope="col">最多</th><th scope="col">数量含义</th></tr></thead>
                <tbody><tr v-for="slot in modeSlots(mode)" :key="slot">
                  <th scope="row">{{ capabilityFieldLabel(slot, options?.conditioningContract.inputSlots) }}<small>{{ slot }}</small></th>
                  <td>{{ contractScalar(bound(slotBounds(mode, slot), 'minimum_items')) }}</td>
                  <td>{{ contractScalar(bound(slotBounds(mode, slot), 'maximum_items')) }}</td>
                  <td>{{ quantityDeclaration(slotBounds(mode, slot), slot === 'total_images') }}
                    <details v-if="!isContractObject(slotBounds(mode, slot)) || Object.keys(contractFieldsExcept(slotBounds(mode, slot), ['minimum_items', 'maximum_items'])).length"><summary>数量声明详情</summary><ModelContractFields :value="slotBounds(mode, slot)" /></details>
                  </td>
                </tr></tbody>
              </table>
              <p v-if="!Object.keys(mode.inputs).length" role="alert">尚未声明素材槽位；不推测数量。</p>
            </div>
            <p v-else role="alert">素材数量声明缺失或格式异常：{{ contractScalar(mode.inputs) }}</p>
            <p class="muted">0–0 表示不支持；数量相同表示固定，不同表示可变。图片总数约束各图片槽合计，不是额外可选的图片数量。</p>
            <details v-if="mode.dynamic_reference_topology !== undefined" class="extra-rules">
              <summary>用途、预处理与配对规则</summary>
              <ModelContractFields :value="mode.dynamic_reference_topology" />
            </details>
          </template>
          <template v-else><p role="alert">模式 {{ index + 1 }} 格式异常</p><ModelContractFields :value="mode" /></template>
        </article>
        <details v-if="isContractObject(conditioning)" class="extra-rules"><summary>素材合同元信息</summary><ModelContractFields :value="conditioningMeta" /><ModelContractFields v-if="!Array.isArray(conditioning.modes)" :value="conditioning.modes" /></details>
      </template>
    </section>

    <section v-for="(value, key) in remaining" :key="key" :aria-label="capabilityFieldLabel(key)">
      <h4>{{ capabilityFieldLabel(key) }} <small v-if="capabilityFieldLabel(key) !== key">{{ key }}</small></h4>
      <ModelContractFields :value="value" />
    </section>
    <p class="contract-note">只读来源：此版本的 specification。未声明的字段不会补默认值；调整已发布合同需新建模型版本。完整 JSON 可用于核对原始字段。</p>
  </div>
</template>

<style scoped>
.model-contract{display:grid;gap:16px;min-width:0;color:var(--text);font-size:12px;line-height:1.6}
.model-contract section{min-width:0;border-bottom:1px solid var(--border);padding-bottom:16px}
.model-contract h4,.model-contract h5{margin:0 0 10px;font-size:13px}.model-contract p{margin:8px 0}
.model-contract small,.muted,.contract-note,.identity{color:var(--text-secondary)}.model-contract small{font-weight:normal;overflow-wrap:anywhere}
.identity{display:flex;flex-wrap:wrap;gap:8px 24px;margin-top:12px}.capabilities{list-style:none;display:flex;flex-wrap:wrap;gap:8px;margin:0;padding:0}
.capabilities li{padding:4px 8px;border:1px solid var(--border-strong);border-radius:5px;overflow-wrap:anywhere}
.reference-mode{padding:12px 0;min-width:0}.reference-mode+.reference-mode{border-top:1px solid var(--border)}
.mode-description{white-space:pre-wrap;overflow-wrap:anywhere}.quantity-scroll{max-width:100%;overflow:auto;margin-top:14px}
.quantity-scroll table{border-collapse:collapse;width:100%;min-width:420px;font-size:12px}.quantity-scroll caption{text-align:left;font-weight:600;padding:8px 0}
.quantity-scroll th,.quantity-scroll td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border);vertical-align:top}
.quantity-scroll th{font-weight:500}.quantity-scroll th small{display:block;font-size:10px}.extra-rules{margin-top:12px}
.model-contract summary{cursor:pointer;min-height:36px;align-content:center;color:var(--text)}
.model-contract :focus-visible{outline:2px solid var(--accent);outline-offset:2px}.model-contract [role=alert]{color:var(--accent)}
</style>
