<script setup lang="ts">
import { computed } from 'vue'
import ReadonlyDataValue from './ReadonlyDataValue.vue'

const props = defineProps<{ diagnostics: Record<string, unknown> }>()
type Issue = { path: string; message: string; semanticPath?: string; origin?: string }
const details = computed(() => {
  const errors = props.diagnostics.structure_errors
  if (errors === undefined) return { rows: [] as Issue[], invalid: false }
  const rows: Issue[] = [], covered = new Set<string>()
  const key = (path: string, message: string) => JSON.stringify([path, message])
  if (!errors || typeof errors !== 'object' || Array.isArray(errors)) return { rows, invalid: true }
  const relations = props.diagnostics.relation_errors ?? []
  if (!Array.isArray(relations)) return { rows, invalid: true }
  for (const relation of relations) {
    if (!relation || typeof relation !== 'object' || typeof relation.path !== 'string' ||
        typeof relation.message !== 'string' ||
        (relation.source_path !== undefined && typeof relation.source_path !== 'string') ||
        (relation.origin_kind !== undefined && typeof relation.origin_kind !== 'string')) return { rows: [], invalid: true }
    const path = relation.source_path ?? relation.path
    rows.push({ path, message: relation.message,
      semanticPath: path === relation.path ? undefined : relation.path,
      origin: relation.origin_kind === 'literal' ? '配置字面值（非原文字段）' : undefined })
    covered.add(key(path, relation.message)); covered.add(key(relation.path, relation.message))
  }
  for (const [path, messages] of Object.entries(errors)) {
    if (!Array.isArray(messages) || messages.some(message => typeof message !== 'string')) return { rows: [], invalid: true }
    for (const message of messages) if (!covered.has(key(path, message))) rows.push({ path, message })
  }
  return { rows, invalid: false }
})
</script>

<template>
  <section v-if="details.invalid || details.rows.length" class="response-diagnostics" aria-label="返回内容字段诊断">
    <template v-if="details.invalid">
      <p role="alert">字段诊断格式异常，无法可靠定位；请查看保留的诊断记录。</p>
      <details><summary>查看诊断记录</summary><ReadonlyDataValue :value="diagnostics" /></details>
    </template>
    <template v-else>
      <h4>需修正的字段或引用 · {{ details.rows.length }} 项</h4>
      <ul><li v-for="(issue, index) in details.rows" :key="index">
        <code>{{ issue.path }}</code><p>{{ issue.message }}</p>
        <small v-if="issue.semanticPath">对应业务位置：{{ issue.semanticPath }}</small>
        <small v-if="issue.origin">{{ issue.origin }}</small>
      </li></ul>
    </template>
  </section>
</template>

<style scoped>
.response-diagnostics{min-width:0;max-width:100%;overflow-wrap:anywhere}
h4,p{margin:0}ul{margin:8px 0;padding-left:20px;display:grid;gap:10px}
code{white-space:pre-wrap;word-break:break-word}small{display:block;color:var(--text-muted)}
summary{cursor:pointer;min-height:36px;padding-top:8px}
</style>
