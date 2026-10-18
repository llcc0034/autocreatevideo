<script setup lang="ts">
import type {TaskSystemMessageTrace} from '../services/taskCenter'
defineProps<{trace:TaskSystemMessageTrace}>()
const labels={matched:'与组装记录一致',mismatch:'与组装记录不一致',unavailable:'来源无法核对'}
</script>
<template>
 <div class="system-message-trace">
  <p role="status">{{labels[trace.state]}} · {{trace.message}}</p>
  <ol v-if="trace.rows.length"><li v-for="row in trace.rows" :key="row.start">
   <strong>{{row.label}} · {{labels[row.state]}}</strong>
   <p>{{row.source}}</p>
   <code>system · messages.0 · 第 {{row.start+1}}–{{row.end}} 字符（{{row.end-row.start}} 字符）</code>
  </li></ol>
  <p v-if="trace.rows.length">位置按原消息的Unicode字符计数，含分段标签和换行，不是Token数；脱敏展示可能改变可见长度。完整正文请查看下方“实际提示词”，这里不复制正文或改变原任务结果。</p>
 </div>
</template>
<style scoped>
.system-message-trace{min-width:0;overflow-wrap:anywhere;font-size:13px;line-height:1.8}.system-message-trace ol{padding-left:24px}.system-message-trace li{padding:10px 0;border-bottom:1px solid var(--border)}.system-message-trace p{margin:6px 0}.system-message-trace code{white-space:normal}
</style>
