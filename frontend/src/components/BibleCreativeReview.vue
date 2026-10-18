<script setup lang="ts">
import { bibleCreativeFields, type BibleCreativeContent } from '@/services/productionManualBible'
import type { JsonObject } from '@/services/platformTypes'

defineProps<{ content: BibleCreativeContent }>()
const sections = { characters: '人物', scenes: '场景', props: '道具' }
function extra(row: JsonObject, section: keyof BibleCreativeContent) {
  return Object.fromEntries(Object.entries(row).filter(([key]) => !['id', 'name', 'is_locked', 'metadata',
    ...Object.keys(bibleCreativeFields[section])].includes(key)))
}
</script>

<template>
  <section class="bible-creative" aria-label="项目现有完整创作设定">
    <h5>项目现有完整创作设定</h5>
    <p>以下全部条目都会纳入本次审核，不只包含与剧本候选同名的条目。共同身份与外观正文见上方主图／变体的确切设定修订；后续输入映射读取审核引用。主图和参考图另行审核。</p>
    <section v-for="(label, section) in sections" :key="section">
      <h6>{{ label }} · {{ content[section].length }} 项</h6>
      <p v-if="!content[section].length">当前没有已有{{ label }}设定。</p>
      <article v-for="row in content[section]" :key="String(row.id)">
        <header><strong>{{ row.name }}</strong><span>{{ row.is_locked ? '已锁定' : '未锁定' }}</span></header>
        <small>条目 ID：{{ row.id }}</small>
        <dl><template v-for="(fieldLabel, key) in bibleCreativeFields[section]" :key="key">
          <dt>{{ fieldLabel }}</dt><dd>{{ row[key] === '' ? '未填写' : row[key] }}</dd>
        </template></dl>
        <details><summary>附加记录（metadata）</summary><pre>{{ JSON.stringify(row.metadata, null, 2) }}</pre></details>
        <details v-if="Object.keys(extra(row, section)).length"><summary>其他返回字段</summary><pre>{{ JSON.stringify(extra(row, section), null, 2) }}</pre></details>
      </article>
    </section>
  </section>
</template>

<style scoped>
.bible-creative,.bible-creative section{display:grid;gap:10px}.bible-creative article{display:grid;gap:8px;border:1px solid var(--border);border-radius:8px;padding:12px;min-width:0}.bible-creative header{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.bible-creative h5,.bible-creative h6,.bible-creative p{margin:0}.bible-creative h6{font-size:inherit}.bible-creative dl{display:grid;grid-template-columns:minmax(6em,auto) minmax(0,1fr);gap:6px 12px;margin:0}.bible-creative dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.bible-creative dt,.bible-creative small{color:var(--text-muted)}.bible-creative pre{white-space:pre-wrap;overflow-wrap:anywhere}@media(max-width:600px){.bible-creative dl{grid-template-columns:1fr}.bible-creative dd{margin-bottom:6px}}
</style>
