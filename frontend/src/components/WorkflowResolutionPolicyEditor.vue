<script setup lang="ts">
import { computed, ref } from 'vue'
import type { JsonObject, WorkflowResolutionOptions, WorkflowResolutionSize } from '@/services/platformTypes'
import { resolutionPolicyErrors } from '@/services/workflowResolution'

const props = defineProps<{ config: JsonObject; options: WorkflowResolutionOptions; disabled?: boolean; readOnly?: boolean }>()
const emit = defineEmits<{ 'update:config': [value: JsonObject] }>()
const configured = computed(() => Object.prototype.hasOwnProperty.call(props.config, 'image_resolution'))
const policy = computed(() => props.config.image_resolution as JsonObject | undefined)
const sizes = computed(() => Array.isArray(policy.value?.sizes) ? policy.value!.sizes as WorkflowResolutionSize[] : [])
const width = ref<number | ''>(''); const height = ref<number | ''>(''); const error = ref('')
const save = (rows: WorkflowResolutionSize[]) => {
  if (props.disabled || props.readOnly) return
  error.value = ''
  emit('update:config', {...props.config, image_resolution: {adapter: props.options.configuredAdapter, sizes: rows}})
}
const changeMode = (event: Event) => {
  if (props.disabled || props.readOnly) return
  error.value = ''
  if ((event.target as HTMLSelectElement).value === 'configured') save([])
  else { const config = {...props.config}; delete config.image_resolution; emit('update:config', config) }
}
const selected = (row: WorkflowResolutionSize) => sizes.value.some(size => size.width === row.width && size.height === row.height)
const toggle = (row: WorkflowResolutionSize) => save(selected(row)
  ? sizes.value.filter(size => size.width !== row.width || size.height !== row.height)
  : [...sizes.value, {width: row.width, height: row.height}])
const addCustom = () => {
  const rows = [...sizes.value, {width: Number(width.value), height: Number(height.value)}]
  error.value = resolutionPolicyErrors({adapter: props.options.configuredAdapter, sizes: rows}, props.options).join('；')
  if (!error.value) { save(rows); width.value = ''; height.value = '' }
}
</script>

<template>
  <div class="resolution-policy">
    <label>尺寸选择方式<select :value="configured?'configured':'native'" :disabled="disabled||readOnly" @change="changeMode"><option v-for="mode in options.modes" :key="mode.value" :value="mode.value">{{ mode.label }}</option></select></label>
    <p>{{ configured?'单组为固定输出尺寸；多组按原图宽高比选择。修改只进入当前 Projector 草稿，发布后需重新生成工作流合同并验收。':'未覆盖原节点尺寸：符合来源链的 FluxKontextImageScale 使用其原生尺寸组；其他工作流保持自身规则。' }}</p>
    <details><summary>查看缩放规则与适用边界</summary><p>{{ options.algorithm }}</p><code>{{ configured?options.configuredAdapter:options.nativeAdapter }}</code><p>仅支持固定素材槽、LoadImage → FluxKontextImageScale 直接或经同一 VAE 编解码及受支持采样器驱动的图片输出。配置尺寸时执行图使用 ImageScale；Live Validate 会检查全部候选尺寸。此处不支持动态素材组改写尺寸链。</p><p>建议从目标工作流生成专用 Projector 建议后配置；改变通用兜底方案会影响其他新导入工作流。配置与来源不兼容会阻断，不会静默忽略。</p><p v-if="!configured">原生尺寸：{{ options.nativeSizes.map(size=>`${size.width}×${size.height}`).join('、') }}</p></details>
    <template v-if="configured">
      <fieldset v-if="!readOnly" :disabled="disabled"><legend>选择尺寸预设（可多选）</legend><div class="resolution-presets"><label v-for="preset in options.presets" :key="`${preset.width}x${preset.height}`"><input type="checkbox" :checked="selected(preset)" :disabled="!selected(preset)&&sizes.length>=options.limits.maxCandidates" @change="toggle(preset)"/>{{ preset.label }}</label></div></fieldset>
      <div v-if="!readOnly" class="resolution-custom"><label>自定义宽度（px）<input v-model.number="width" type="number" :min="options.limits.minDimension" :max="options.limits.maxDimension" :step="options.limits.multiple" :disabled="disabled"/></label><label>自定义高度（px）<input v-model.number="height" type="number" :min="options.limits.minDimension" :max="options.limits.maxDimension" :step="options.limits.multiple" :disabled="disabled"/></label><button type="button" class="button ghost small" :disabled="disabled||sizes.length>=options.limits.maxCandidates" @click="addCustom">添加尺寸</button></div>
      <small v-if="!readOnly">宽高 {{ options.limits.minDimension }}–{{ options.limits.maxDimension }}，{{ options.limits.multiple }} 的倍数；总像素 ≤ {{ options.limits.maxPixels }}；最多 {{ options.limits.maxCandidates }} 组。</small>
      <p v-if="!sizes.length" role="status">尚未选择目标尺寸，不能保存此配置。</p>
      <ul v-else aria-label="配置的目标尺寸"><li v-for="(size,index) in sizes" :key="index">{{ size.width }} × {{ size.height }} px <button v-if="!readOnly" type="button" class="button ghost tiny" :disabled="disabled" :aria-label="`移除 ${size.width}×${size.height}`" @click="save(sizes.filter((_,i)=>i!==index))">移除</button></li></ul>
    </template>
    <p v-if="error" class="platform-inline-error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.resolution-policy{display:grid;gap:10px;min-width:0}.resolution-policy p{margin:0;line-height:1.6}.resolution-policy label{display:grid;gap:6px}.resolution-policy select,.resolution-policy input[type=number]{width:100%;min-height:36px;background:var(--bg-input,#080c10);color:inherit;border:1px solid #303b46;border-radius:6px;padding:7px}.resolution-policy fieldset{min-width:0;border:1px solid #303b46}.resolution-presets{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}.resolution-presets label{display:flex;align-items:center;gap:7px;min-height:36px}.resolution-custom{display:grid;grid-template-columns:1fr 1fr auto;gap:10px;align-items:end}.resolution-policy code{overflow-wrap:anywhere}.resolution-policy summary{cursor:pointer;min-height:36px}.resolution-policy ul{display:flex;gap:10px;flex-wrap:wrap;padding:0;list-style:none}.resolution-policy li{display:flex;align-items:center;gap:6px}@media(max-width:700px){.resolution-custom{grid-template-columns:1fr}}
</style>
