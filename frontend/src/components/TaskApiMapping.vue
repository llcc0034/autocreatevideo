<script setup lang="ts">
import type {TaskApiMapping} from '@/services/taskCenter'
defineProps<{trace:TaskApiMapping}>()
const labels={included:'进入映射正文',omitted:'未提供或 null，不进入正文',unmapped:'未配置映射，不进入正文'}
</script>
<template>
 <section class="api-mapping">
  <p><strong>{{trace.state==='verified'?'配置摘要核对一致':'无法核对映射'}}</strong> · {{trace.code}}</p>
  <p>{{trace.message}}</p>
  <details><summary>映射依据</summary><p>精确部署 {{trace.deployment_id}}</p><p>原配置摘要 {{trace.configuration_sha256}}</p></details>
  <dl v-if="trace.rows.length">
   <div v-for="row in trace.rows" :key="row.key"><dt>标准体 <code>{{row.key}}</code></dt><dd>
    <p>供应商 JSON 顶层字段：<code v-if="row.field!==null">{{row.field}}</code><span v-else>未配置</span></p>
    <p>{{labels[row.state]}}</p>
   </dd></div>
  </dl>
  <p class="muted">此处仅显示映射位置，不重复提示词或参数值；原值请核对本页参数、实际提示词与解析结果。不会重新发送、解析或改变任务状态。</p>
 </section>
</template>
<style scoped>
.api-mapping{min-width:0;overflow-wrap:anywhere}.api-mapping p{margin:.45rem 0}.api-mapping dl{margin:1rem 0}.api-mapping dl>div{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr);gap:.75rem;padding:.6rem 0;border-bottom:1px solid var(--border)}.api-mapping dd{margin:0;min-width:0}.api-mapping code{white-space:normal}.api-mapping summary{cursor:pointer;min-height:36px;align-content:center}.api-mapping summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:640px){.api-mapping dl>div{grid-template-columns:minmax(0,1fr);gap:0}}
</style>
