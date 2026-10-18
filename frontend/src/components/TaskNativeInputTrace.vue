<script setup lang="ts">
import type { TaskNativeInputTrace } from '../services/taskCenter'
defineProps<{trace:TaskNativeInputTrace}>()
const labels={matched:'已登记位置一致',mismatch:'记录不一致',partial:'部分可核对',unavailable:'无法核对'}
</script>
<template>
 <div class="native-input-trace">
  <p role="status">{{labels[trace.state]}} · {{trace.message}}</p>
  <p v-if="trace.unmapped_count" class="unmapped">另有 {{trace.unmapped_count}} 个未解释的JSON字段，请核对下方原消息；没有忽略或删除这些内容。</p>
  <dl><div v-for="row in trace.rows" :key="row.destination"><dt>{{row.label}} · {{labels[row.state]}}</dt><dd>
   <code>{{row.source}}</code><span aria-hidden="true"> → </span><code>{{row.destination}}</code>
   <p v-if="row.state==='unavailable'">冻结计划未记录该位置，不能将消息中的空值反推为用户填写或策略配置。</p>
   <p v-else-if="row.state==='mismatch'">原消息该位置缺失，或类型／值与计划不一致；请核对原文，不改变任务状态。</p>
  </dd></div></dl>
  <p>位置指向本次冻结记录，不是重新读取项目。这里只展示对应关系，完整正文保留在“实际提示词”；字段审计副本若曾随历史消息发送，会单独列出，不静默去重。</p>
 </div>
</template>
<style scoped>
.native-input-trace{min-width:0;overflow-wrap:anywhere;font-size:13px;line-height:1.8}.native-input-trace p{margin:8px 0}.native-input-trace dl{margin:12px 0}.native-input-trace dl>div{display:grid;grid-template-columns:minmax(120px,1fr) minmax(0,2fr);gap:12px;padding:10px 0;border-bottom:1px solid var(--border)}.native-input-trace dt{font-weight:600}.native-input-trace dd{margin:0;min-width:0}.native-input-trace code{white-space:normal}.unmapped{color:var(--warning,var(--accent))}@media(max-width:640px){.native-input-trace dl>div{grid-template-columns:1fr;gap:6px}}
</style>
