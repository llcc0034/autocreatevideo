<script setup lang="ts">
import { computed } from 'vue'
import { recipeObject } from '@/services/productionRecipeAuthoring'
import type { JsonObject, ProductionRecipeEditorOptions } from '@/services/platformTypes'
import { stageLabel } from '@/services/presentationLabels'

const props = defineProps<{ modelValue: unknown; prior: JsonObject[]; options: ProductionRecipeEditorOptions; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [JsonObject] }>()
const rule = computed(() => recipeObject(props.modelValue))
const source = computed(() => props.options.shotReferenceSourceOptions?.find(row => row.value === rule.value.source))
const upstream = computed(() => props.prior.filter(row => row.key === source.value?.upstreamStageKey && row.mode === 'existing_pipeline'))
const valueOf = (event: Event) => (event.target as HTMLSelectElement).value
function update(changes: JsonObject) {
  if (!props.disabled) emit('update:modelValue', {...rule.value, ...changes})
}
function changeSource(event: Event) {
  if (props.disabled) return
  // Explicitly changing the source clears dependent choices, never grants
  // permission or automatically picks the first stage/strategy.
  emit('update:modelValue', {source: valueOf(event), source_stage_key: null, selection: '',
    allow_override: typeof rule.value.allow_override === 'boolean' ? rule.value.allow_override : null})
}
</script>

<template>
  <section class="app-form-section" aria-label="逐镜素材来源规则">
    <h4>逐镜素材来源规则</h4>
    <p class="app-help-text">仅配置本轮逐次使用的素材来源，不改变 Bible 主图，不设置模型输入用途，也不等于审核通过。在运行详情的逐镜选材中按本规则选择确切素材版本，再按配方完成参考审核；不能用旧主图绑定代替本轮选择。</p>
    <p v-if="!options.shotReferenceSourceOptions?.length" role="alert">来源目录未就绪，不能配置规则。</p>
    <template v-else>
      <p v-if="modelValue === undefined" class="app-help-text">未声明逐镜来源规则；不会自动补出选材或允许改选授权。</p>
      <div class="app-form-grid">
        <label class="app-field"><span>默认素材来源</span><select :value="rule.source ?? ''" :disabled="disabled" @change="changeSource">
          <option value="" disabled>请选择来源</option><option v-for="item in options.shotReferenceSourceOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select></label>
        <label v-if="source?.upstreamStageKey" class="app-field"><span>上游素材结果阶段</span><select :value="rule.source_stage_key ?? ''" :disabled="disabled" @change="update({source_stage_key: valueOf($event)})">
          <option value="" disabled>请选择本配方上游阶段</option><option v-for="row in upstream" :key="String(row.key)" :value="row.key">{{ stageLabel(String(row.key)) }}</option>
        </select><span v-if="!upstream.length" role="alert">前面没有可用的素材结果阶段，请先配置；不会选择之后的阶段。</span></label>
        <label class="app-field"><span>素材选择方式</span><select :value="rule.selection ?? ''" :disabled="disabled || !source" @change="update({selection: valueOf($event)})">
          <option value="" disabled>请选择方式</option><option v-for="item in source?.selectionOptions ?? []" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select></label>
        <label class="app-field"><span>允许替换已有选择</span><select :value="typeof rule.allow_override === 'boolean' ? String(rule.allow_override) : ''" :disabled="disabled || !source" @change="update({allow_override: valueOf($event) === 'true'})">
          <option value="" disabled>请明确选择</option><option value="true">允许本次改选</option><option value="false">不允许替换已有选择</option>
        </select></label>
      </div>
      <p class="app-help-text">自动规则只匹配同一确切设定修订；缺失或歧义转人工接管。禁止替换不禁止为尚未选择的用途首次选材；已有选择不会被默认规则覆盖。已经执行的请求仍只读。</p>
    </template>
  </section>
</template>

<style scoped>
.app-form-section{display:grid;gap:12px;min-width:0}.app-form-section h4,.app-help-text{margin:0}.app-help-text{color:#94a3b8;overflow-wrap:anywhere;line-height:1.5}.app-form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:12px}.app-field{display:grid;gap:6px;min-width:0;align-content:start}.app-field select{width:100%;min-width:0;min-height:36px;padding:8px;border:1px solid #303743;border-radius:6px;background:#090c10;color:#e4e9ee}.app-field select:disabled{opacity:.65}[role=alert]{color:#e7b669;overflow-wrap:anywhere}
</style>
