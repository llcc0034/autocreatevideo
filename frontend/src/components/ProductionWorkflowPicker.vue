<script setup lang="ts">
import { computed, watch } from 'vue'
import { AlertTriangle, CheckCircle2, Gauge, Workflow } from 'lucide-vue-next'
import type { H3Mode, ProductionWorkflowTemplate } from '@/types'
import { normalizeWorkflowParameterValue } from '@/services/workflowParameters'

const props = withDefaults(defineProps<{
  templates: ProductionWorkflowTemplate[]
  h3Mode?: H3Mode
  loading?: boolean
  error?: string
  disabled?: boolean
}>(), { h3Mode: 'Ref2VA', loading: false, error: '', disabled: false })

const templateId = defineModel<string>('templateId', { default: '' })
const parameters = defineModel<Record<string, number>>('parameters', { default: () => ({}) })
const matchingTemplates = computed(() => props.templates.filter((template) => !template.isDefault && template.h3Modes.includes(props.h3Mode)))
const selectedTemplate = computed(() => matchingTemplates.value.find((template) => template.id === templateId.value))
const parameterEntries = computed(() => Object.entries(selectedTemplate.value?.userParameters || {}))

const selectTemplate = (id: string) => {
  templateId.value = id
  const template = matchingTemplates.value.find((item) => item.id === id)
  parameters.value = Object.fromEntries(Object.entries(template?.userParameters || {}).map(([key, definition]) => [key, definition.default]))
}

watch(matchingTemplates, (templates) => {
  if (!props.loading && !props.error && !props.disabled && templateId.value && !templates.some((template) => template.id === templateId.value)) selectTemplate('')
})

const normalizeParameter = (key: string) => {
  const definition = selectedTemplate.value?.userParameters[key]
  if (!definition) return
  parameters.value = {
    ...parameters.value,
    [key]: normalizeWorkflowParameterValue(parameters.value[key], definition),
  }
}
</script>

<template>
  <div class="production-workflow-picker">
    <div class="workflow-choice-grid">
      <button type="button" :disabled="disabled" :aria-pressed="!templateId" :class="{ active: !templateId }" @click="selectTemplate('')">
        <span class="choice-icon"><CheckCircle2 :size="18"/></span>
        <span><strong>官方默认</strong><small>沿用系统按镜头模式自动匹配的稳定工作流</small></span>
        <b>DEFAULT</b>
      </button>
      <button v-for="template in matchingTemplates" :key="template.id" type="button" :disabled="disabled" :aria-pressed="templateId === template.id" :aria-label="`${template.name}${template.maturity === 'experimental' ? '，实验工作流' : ''}`" :class="{ active: templateId === template.id }" @click="selectTemplate(template.id)">
        <span class="choice-icon"><Workflow :size="18"/></span>
        <span><strong>{{ template.name }}</strong><small>{{ template.slug }} · v{{ template.version }}</small></span>
        <b :class="{ experimental: template.maturity === 'experimental' }">{{ template.maturity === 'experimental' ? '实验' : templateId === template.id ? '已选择' : '可选' }}</b>
      </button>
    </div>
    <p v-if="loading" class="picker-status" role="status">正在读取可用工作流…</p>
    <p v-else-if="error" class="picker-status warning" role="alert"><AlertTriangle :size="13"/>自定义工作流暂不可用，仍可使用官方默认：{{ error }}</p>
    <p v-else-if="!matchingTemplates.length" class="picker-status">当前没有可用的 {{ h3Mode }} 自定义工作流，继续使用官方默认。</p>

    <div v-if="selectedTemplate && parameterEntries.length" class="workflow-parameter-panel">
      <p v-if="selectedTemplate.maturity==='experimental'" class="experimental-notice" role="note"><AlertTriangle :size="13"/>实验工作流：提交前仍需通过当前 ComfyUI 插件与模型能力预检，不代表已稳定验收。</p>
      <header><Gauge :size="16"/><div><strong>工作流参数</strong><small>仅应用到本次 {{ h3Mode }} 视频任务</small></div></header>
      <label v-for="[key, definition] in parameterEntries" :key="key">
        <span><strong>{{ definition.label || key }}</strong><small>{{ definition.help || key }}</small></span>
        <input
          v-model.number="parameters[key]"
          type="number"
          :min="definition.min"
          :max="definition.max"
          :step="definition.step || (definition.type === 'integer' ? 1 : 0.01)"
          :disabled="disabled"
          :aria-label="definition.label || key"
          @blur="normalizeParameter(key)"
        />
      </label>
    </div>
  </div>
</template>

<style scoped>
.production-workflow-picker{margin-top:11px}.workflow-choice-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.workflow-choice-grid button{min-height:66px;padding:9px;display:grid;grid-template-columns:34px minmax(0,1fr) auto;align-items:center;gap:8px;border:1px solid var(--border);border-radius:7px;background:#0d1014;color:#747a82;text-align:left;cursor:pointer}.workflow-choice-grid button:hover:not(:disabled){border-color:#343a42}.workflow-choice-grid button:disabled,.workflow-parameter-panel input:disabled{cursor:not-allowed;opacity:.58}.workflow-choice-grid button.active{border-color:rgba(230,167,90,.4);background:rgba(230,167,90,.035);color:#c7c0b6}.choice-icon{width:32px;height:32px;display:flex;align-items:center;justify-content:center;border-radius:6px;background:#171b20;color:#747b84}.active .choice-icon{color:var(--accent);background:rgba(230,167,90,.08)}.workflow-choice-grid button>span:nth-child(2){min-width:0;display:flex;flex-direction:column;gap:4px}.workflow-choice-grid strong{font-size:9px}.workflow-choice-grid small{overflow:hidden;color:#5e646c;font-size:7px;text-overflow:ellipsis;white-space:nowrap}.workflow-choice-grid b{padding:3px 5px;border-radius:4px;background:#171b20;color:#666d75;font-size:6px}.workflow-choice-grid .active b{color:var(--accent)}.picker-status{margin:8px 0 0!important;display:flex;align-items:center;gap:5px;color:#626971!important}.picker-status.warning{color:#b99164!important}.workflow-parameter-panel{margin-top:9px;padding:10px;border:1px solid rgba(159,140,255,.14);border-radius:7px;background:rgba(159,140,255,.025)}.workflow-parameter-panel header{display:flex;align-items:center;gap:7px;margin-bottom:8px;color:var(--violet)}.workflow-parameter-panel header div{display:flex;flex-direction:column;gap:2px}.workflow-parameter-panel header strong{font-size:9px}.workflow-parameter-panel header small{font-size:7px;color:#686276}.workflow-parameter-panel label{min-height:42px;padding:5px 0;display:flex;align-items:center;justify-content:space-between;gap:12px;border-top:1px solid rgba(255,255,255,.035)}.workflow-parameter-panel label>span{display:flex;flex-direction:column;gap:3px}.workflow-parameter-panel label strong{font-size:8px}.workflow-parameter-panel label small{font-size:7px;color:#5f656d}.workflow-parameter-panel input{width:96px;height:29px;padding:0 8px;border:1px solid var(--border);border-radius:5px;background:#0b0e12;color:#bfc3c8;font:9px Consolas,monospace}.workflow-parameter-panel input:focus{outline:none;border-color:rgba(230,167,90,.45)}@media(max-width:640px){.workflow-choice-grid{grid-template-columns:1fr}}
.workflow-choice-grid b.experimental{background:rgba(232,178,92,.1);color:#d5a867}.experimental-notice{margin:0 0 8px!important;padding:7px;display:flex;align-items:flex-start;gap:5px;border:1px solid rgba(232,178,92,.2);border-radius:5px;background:rgba(232,178,92,.04);color:#bd9763!important;line-height:1.45}
</style>
