<script setup lang="ts">
import type { ExecutionPreparationDetail } from '@/services/executionPreparation'
import ExecutionResponseDiagnostics from './ExecutionResponseDiagnostics.vue'
defineProps<{ response: NonNullable<ExecutionPreparationDetail['textResponseReview']> }>()
</script>

<template>
  <section class="returned-text" aria-label="模型实际返回内容">
    <h4>模型实际返回内容 · 未自动采用</h4>
    <p role="status">{{ response.diagnostics.summary }}</p>
    <ExecutionResponseDiagnostics :diagnostics="response.diagnostics" />
    <ul v-if="response.notices.length"><li v-for="(notice, index) in response.notices" :key="index">{{ notice.message }}</li></ul>
    <template v-if="response.fields.length">
      <section v-for="item in response.fields" :key="JSON.stringify([item.unitKey, item.fieldKey])">
        <h5>{{ item.label }}</h5>
        <pre v-if="item.text !== ''">{{ item.text }}</pre>
        <p v-else>模型实际返回了空字符串；未用默认内容补齐。</p>
      </section>
      <details><summary>查看完整原始响应（技术信息）</summary><pre>{{ response.originalText }}</pre></details>
    </template>
    <template v-else>
      <p>无法可靠拆分到具体字段，以下是实际返回原文；请人工核对后在主准备填写，不会猜测或自动补齐。</p>
      <pre v-if="response.originalText !== ''">{{ response.originalText }}</pre>
      <p v-else>本次保留的实际正文为空。</p>
    </template>
    <p>展示不代表确认或采用。质量建议由你判断；需修正的格式或引用问题，请另存人工修订，原响应仍保留。</p>
  </section>
</template>

<style scoped>
.returned-text{display:grid;gap:12px;min-width:0;max-width:100%;border:1px solid var(--border);border-radius:6px;padding:14px}
.returned-text section{min-width:0}.returned-text h4,.returned-text h5,.returned-text p{margin:0;overflow-wrap:anywhere}
.returned-text pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;max-height:420px;overflow:auto;font:inherit;line-height:1.7;padding:12px;background:var(--bg-elevated);margin:8px 0 0}
.returned-text summary{cursor:pointer}.returned-text ul{margin:0;padding-left:20px}.returned-text h5{font-size:14px}
</style>
