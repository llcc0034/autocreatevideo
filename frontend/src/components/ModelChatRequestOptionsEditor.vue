<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ChatRequestOptionsEditorOptions, JsonObject } from '@/services/platformTypes'
import { chatObject, chatExtensions, commonChatOptionErrors, replaceChatExtensions } from '@/services/modelChatRequestOptions'
import StructuredJsonEditor from './StructuredJsonEditor.vue'
import { chatRequestOptionErrors } from '@/services/registryResourceSchema'

const props = defineProps<{modelValue?: unknown; options?: ChatRequestOptionsEditorOptions | null; disabled?: boolean; label: string; sourcePath?: string}>()
const emit = defineEmits<{'update:modelValue': [value: JsonObject]; validity: [errors: string[]]}>()
const blocked = computed(() => props.disabled || !props.options)
const value = computed(() => chatObject(props.modelValue) ? props.modelValue : {})
const format = computed(() => chatObject(value.value.response_format) ? value.value.response_format : {})
const otherExtensions = computed(() => chatExtensions(value.value, ['temperature', 'response_format']))
const responseExtensions = computed(() => chatExtensions(format.value, ['type']))
const ownError = ref(''), numericError = ref(''), extensionErrors = ref<string[]>([]), formatErrors = ref<string[]>([])
const errors = computed(() => [...commonChatOptionErrors(props.modelValue, props.options), ...chatRequestOptionErrors(props.modelValue), ownError.value, numericError.value, ...extensionErrors.value, ...(chatObject(value.value.response_format) ? formatErrors.value : [])].filter(Boolean))
watch(() => JSON.stringify(errors.value), errors => emit('validity', JSON.parse(errors)), {immediate: true})
watch(() => value.value.temperature, () => {numericError.value = ''})
const temperatureMode = computed(() => !Object.prototype.hasOwnProperty.call(value.value, 'temperature') ? 'omit' : value.value.temperature === null ? 'null' : 'number')
const formatMode = computed(() => !Object.prototype.hasOwnProperty.call(value.value, 'response_format') ? 'omit' : value.value.response_format === null ? 'null' : `type:${String(format.value.type ?? 'invalid')}`)
const unknownFormat = computed(() => formatMode.value.startsWith('type:') && !props.options?.responseFormatTypes.some(row => `type:${row.value}` === formatMode.value))
function commit(next: JsonObject) { if (!blocked.value) { ownError.value = ''; emit('update:modelValue', next) } }
function set(key: string, next: unknown) { const copy = {...value.value}; if (next === undefined) delete copy[key]; else copy[key] = next; commit(copy) }
function temperatureModeChanged(mode: string) {
  if (blocked.value || !['omit', 'null', 'number'].includes(mode)) return
  numericError.value = ''
  set('temperature', mode === 'omit' ? undefined : mode === 'null' ? null : typeof value.value.temperature === 'number' ? value.value.temperature : props.options!.temperatureMinimum)
}
function temperatureChanged(raw: string) {
  if (blocked.value) return
  const number = Number(raw)
  if (!raw.trim() || !Number.isFinite(number) || number < props.options!.temperatureMinimum) { numericError.value = '采样温度不是有效范围内的数值；草稿未更新，请修正或选择不发送'; return }
  numericError.value = ''; set('temperature', number)
}
function formatChanged(mode: string) {
  if (blocked.value || (!['omit', 'null'].includes(mode) && !props.options?.responseFormatTypes.some(row => row.value === mode))) return
  set('response_format', mode === 'omit' ? undefined : mode === 'null' ? null : {...format.value, type: mode})
}
function extensionsChanged(extras: unknown, nested = false) {
  if (blocked.value) return
  try {
    const next = replaceChatExtensions(nested ? format.value : value.value, extras, nested ? ['type'] : ['temperature', 'response_format'])
    if (nested) set('response_format', next); else commit(next)
  } catch (error) { ownError.value = String(error) }
}
</script>

<template>
  <section class="chat-options" :aria-label="label">
    <header><strong>{{ label }}</strong><code>{{ sourcePath || 'chat_request_options' }}</code></header>
    <p v-if="options">{{ options.assemblyDescription }}</p>
    <p v-if="options">{{ options.boundaryDescription }}</p>
    <button v-if="modelValue === undefined" type="button" :disabled="blocked" @click="commit({})">声明空请求选项（不额外发送参数）</button>
    <template v-if="chatObject(modelValue)">
      <div class="chat-options-fields">
        <label>采样温度 · temperature
          <select :value="temperatureMode" :disabled="blocked" @change="temperatureModeChanged(($event.target as HTMLSelectElement).value)">
            <option value="omit">不发送此字段</option><option value="null">发送 null（不是 0）</option><option value="number">数值（首次设置为 {{ options?.temperatureMinimum }}）</option>
          </select>
          <input v-if="temperatureMode === 'number'" type="number" aria-label="采样温度数值" :value="String(value.temperature)" :min="options?.temperatureMinimum" step="any" :disabled="blocked" @input="temperatureChanged(($event.target as HTMLInputElement).value)"/>
        </label>
        <label>响应格式 · response_format.type
          <select :value="formatMode" :disabled="blocked" @change="formatChanged(($event.target as HTMLSelectElement).value.replace(/^type:/, ''))">
            <option value="omit">不发送此字段</option><option value="null">发送 null</option>
            <option v-if="unknownFormat" :value="formatMode" disabled>已配置的非通用格式：{{ formatMode }}（需核实）</option>
            <option v-for="choice in options?.responseFormatTypes || []" :key="choice.value" :value="`type:${choice.value}`">{{ choice.label }}</option>
          </select>
        </label>
      </div>
      <p v-if="format.type==='json_schema'">输出结构由执行实例所选业务输出版本派生，无需另填 Schema。这里只配置接口格式选项；没有业务输出的协议不能使用此格式，格式声明也不代表模型已支持。</p>
      <details v-if="chatObject(value.response_format)"><summary>响应格式接口选项（{{ Object.keys(responseExtensions).length }} 项）</summary>
        <p>type 由上方选择；json_schema 内只维护 name、strict、description，不接受 schema。未填 name 时使用确切业务输出身份。切换格式不静默删除已有选项，请明确清理不适用配置。</p>
        <StructuredJsonEditor :model-value="responseExtensions" label="响应格式接口选项" fixed-object :disabled="blocked" @update:model-value="extensionsChanged($event, true)" @validity="formatErrors = $event"/>
      </details>
      <details><summary>其他接口扩展项（{{ Object.keys(otherExtensions).length }} 项）</summary>
        <p>尚未表单化的供应商扩展仍需按接口文档配置；不能重复定义上方字段。没有自动补入 thinking、采样参数或供应商默认值。</p>
        <StructuredJsonEditor :model-value="otherExtensions" label="其他接口扩展项" fixed-object :disabled="blocked" @update:model-value="extensionsChanged($event)" @validity="extensionErrors = $event"/>
      </details>
    </template>
    <p v-if="errors.length" role="alert">{{ errors.join('；') }}</p>
  </section>
</template>

<style scoped>
.chat-options{display:grid;gap:10px;min-width:0;padding:12px;border:1px solid #303743;border-radius:8px;background:#0b0e12;overflow-wrap:anywhere}.chat-options header{display:flex;flex-wrap:wrap;gap:8px;justify-content:space-between}.chat-options p{margin:0;color:#9aa8b5;font-size:12px;line-height:1.6}.chat-options-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.chat-options label{display:grid;gap:8px;align-content:start;min-width:0}.chat-options select,.chat-options input{box-sizing:border-box;min-width:0;width:100%;min-height:38px;background:#10141a;color:#dbe3eb;border:1px solid #39434f;border-radius:6px;padding:8px}.chat-options summary{cursor:pointer}.chat-options [role=alert]{color:#ff9999}.chat-options :focus-visible{outline:2px solid #efb65c;outline-offset:2px}@media(max-width:760px){.chat-options-fields{grid-template-columns:1fr}}
</style>
