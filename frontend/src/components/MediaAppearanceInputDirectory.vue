<script setup lang="ts">
import { appearanceDifferenceLabels, type MediaAppearanceInputOptions } from '@/services/mediaAppearanceInputs'
defineProps<{ options: MediaAppearanceInputOptions; labels: Record<string, { label: string }> }>()
</script>

<template>
  <details class="appearance-input-directory">
    <summary>查看本实例的外观输入目录（只读）</summary>
    <p>来自本次选定的精确实例合同，不读取设定或素材正文，不是可运行或变体生成通过的证明。</p>
    <h4>可承载主图的图片输入</h4>
    <p v-if="!options.primary_inputs.length">没有实际主图片端口；图片输出能力不等于可接收主图。</p>
    <ul v-else><li v-for="row in options.primary_inputs" :key="row.input_key">{{ labels[row.input_key]?.label || row.input_key }}<code>{{ row.input_key }} → {{ row.target_key }}</code></li></ul>
    <h4>原输入中的差异来源</h4>
    <p v-if="!options.difference_sources.length">没有可引用的差异正文来源。辅助上下文不能代替差异正文。</p>
    <ul v-else><li v-for="row in options.difference_sources" :key="JSON.stringify(row)">
      {{ appearanceDifferenceLabels[row.kind] }} · {{ labels[row.key]?.label || row.key }}
      <code>{{ row.key }}<template v-if="'field_key' in row"> / {{ row.field_key }}</template></code>
      <span v-if="row.kind === 'reference_field'">还须选择本次原输入中的确切单元，不能按名称猜测。</span>
    </li></ul>
    <p>正式准备仍须核对确切主图版本、实际文件与端口，并验证差异文字非空且不是默认补值。目录不会替你选择或复制正文。</p>
  </details>
</template>

<style scoped>
.appearance-input-directory{padding:12px;border:1px solid var(--border);border-radius:8px;min-width:0}.appearance-input-directory summary{cursor:pointer}.appearance-input-directory p,.appearance-input-directory li{font-size:12px;overflow-wrap:anywhere}.appearance-input-directory h4{font-size:13px;margin:12px 0 6px}.appearance-input-directory ul{padding-left:20px}.appearance-input-directory li{margin:8px 0}.appearance-input-directory code,.appearance-input-directory span{display:block;overflow-wrap:anywhere}
</style>
