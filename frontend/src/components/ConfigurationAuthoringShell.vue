<script setup lang="ts">
import { nextTick, ref, useId, watch } from 'vue'
import type { AuthoringIssue } from '@/services/configurationAuthoring'

const props = defineProps<{
  label: string
  status: string
  busy?: boolean
  issues?: AuthoringIssue[]
  sections: { key: string; title: string; description: string; summary?: string }[]
}>()
const root = ref<HTMLElement | null>(null)
const feedback = ref<HTMLElement | null>(null)
const prefix = `configuration-${useId()}`
function locate(key: string, field?: string) {
  const section = Array.from(root.value?.querySelectorAll<HTMLElement>('[data-authoring-section]') || [])
    .find(element => element.dataset.authoringSection === key)
  const path = field?.replace(/\[(\d+)\]/g, '.$1')
  const fields = Array.from(section?.querySelectorAll<HTMLElement>('[data-authoring-field]') || [])
  const match = path ? fields.filter(element => {
    const key = element.dataset.authoringField!
    return path === key || path.startsWith(`${key}.`)
  }).sort((a, b) => b.dataset.authoringField!.length - a.dataset.authoringField!.length)[0] : undefined
  const anchor = match || section
  for (let parent = anchor?.parentElement; parent && parent !== root.value; parent = parent.parentElement) {
    if (parent.tagName === 'DETAILS') (parent as HTMLDetailsElement).open = true
  }
  anchor?.scrollIntoView({ block: 'start' })
  const control = match?.querySelector<HTMLElement>('input:not(:disabled),select:not(:disabled),textarea:not(:disabled),button:not(:disabled)')
  ;(control || anchor)?.focus({ preventScroll: true })
}
watch(() => props.issues, async issues => {
  if (!issues?.length) return
  await nextTick()
  feedback.value?.scrollIntoView({ block: 'nearest' }); feedback.value?.focus({ preventScroll: true })
})
</script>

<template>
  <div ref="root" class="configuration-authoring" :aria-label="label" :aria-busy="Boolean(busy)">
    <div class="configuration-authoring-status" role="status">{{ status }}</div>
    <nav class="configuration-authoring-nav" :aria-label="`${label} · 分段定位`">
      <button v-for="(section,index) in sections" :key="section.key" type="button"
        :aria-controls="`${prefix}-${section.key}`" @click="locate(section.key)">
        <span class="configuration-authoring-number" aria-hidden="true">{{ index+1 }}</span>
        <span><strong>{{ section.title }}</strong><small v-if="section.summary">{{ section.summary }}</small></span>
      </button>
    </nav>
    <div v-if="issues?.length" ref="feedback" class="configuration-authoring-errors" tabindex="-1" role="alert">
      <strong>请检查以下配置，已填内容保留：</strong>
      <ul><li v-for="(issue,index) in issues" :key="`${index}:${issue.field}`">
        <span>{{ issue.field }}：{{ issue.message }}</span>
        <button v-if="issue.section" type="button" class="button ghost small" @click="locate(issue.section,issue.field)">定位{{ sections.find(section=>section.key===issue.section)?.title }}</button>
      </li></ul>
    </div>
    <slot name="feedback" />
    <!-- Sections stay mounted and visible: navigation cannot discard local edits
         or leave required native form controls hidden and impossible to focus. -->
    <section v-for="section in sections" :id="`${prefix}-${section.key}`" :key="section.key"
      :data-authoring-section="section.key" :aria-labelledby="`${prefix}-${section.key}-title`" tabindex="-1"
      class="configuration-authoring-section">
      <header><h3 :id="`${prefix}-${section.key}-title`">{{ section.title }}</h3><p>{{ section.description }}</p></header>
      <slot :name="section.key" />
    </section>
    <footer v-if="$slots.actions" class="configuration-authoring-actions"><slot name="actions" /></footer>
  </div>
</template>

<style scoped>
.configuration-authoring{display:grid;gap:var(--config-space-xl);min-width:0;font-family:var(--font);font-size:var(--config-body-size);line-height:1.6;letter-spacing:0}
.configuration-authoring-status{color:var(--text-secondary);font-size:var(--config-help-size)}
.configuration-authoring-errors{color:var(--red);overflow-wrap:anywhere}.configuration-authoring-errors ul{padding-left:20px}.configuration-authoring-errors li{margin-top:var(--config-space-sm)}.configuration-authoring-errors button{margin:var(--config-space-xs);white-space:normal}
.configuration-authoring-nav{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:var(--config-space-sm)}
.configuration-authoring-nav button{display:flex;align-items:flex-start;gap:var(--config-space-sm);min-width:0;min-height:var(--config-control-height);padding:var(--config-space-md);text-align:left;background:var(--surface);border:1px solid var(--border-strong);border-radius:var(--config-radius);cursor:pointer}
.configuration-authoring-nav button:hover{border-color:var(--accent)}
.configuration-authoring-nav strong{font-size:var(--config-body-size);font-weight:600}.configuration-authoring-nav small{display:block;color:var(--text-secondary);font-size:var(--config-help-size);overflow-wrap:anywhere}
.configuration-authoring-number{color:var(--accent);font-weight:600}
.configuration-authoring-section{min-width:0;display:grid;gap:var(--config-space-lg);border-top:1px solid var(--border-strong);padding-top:var(--config-space-xl);scroll-margin-block:var(--config-space-lg)}
.configuration-authoring-section>header{display:grid;gap:var(--config-space-xs)}
.configuration-authoring-section>header h3{font-size:var(--config-section-size);font-weight:600}
.configuration-authoring-section>header p{color:var(--text-secondary);font-size:var(--config-help-size)}
.configuration-authoring :deep(p),.configuration-authoring :deep(small),.configuration-authoring :deep(code){overflow-wrap:anywhere}
.configuration-authoring :deep(label){font-size:var(--config-body-size)}
.configuration-authoring :deep(summary){min-height:var(--config-control-height);align-content:center;font-size:var(--config-body-size);overflow-wrap:anywhere}
.configuration-authoring :deep(input:not([type=checkbox]):not([type=radio])),.configuration-authoring :deep(select),.configuration-authoring :deep(textarea){min-width:0;max-width:100%;font-family:var(--font);font-size:var(--config-body-size);min-height:var(--config-control-height);padding:var(--config-space-sm);border:1px solid var(--border-strong);border-radius:var(--config-radius);background:var(--bg-soft);color:var(--text)}
.configuration-authoring :deep(.button){min-height:var(--config-control-height);font-size:var(--config-body-size);border-radius:var(--config-radius)}
.configuration-authoring :deep(:focus-visible){outline:2px solid var(--accent);outline-offset:3px}
.configuration-authoring-actions{position:sticky;bottom:var(--config-sticky-offset,0);display:flex;align-items:center;gap:var(--config-space-sm);flex-wrap:wrap;padding:var(--config-space-md) 0;border-top:1px solid var(--border-strong);background:var(--surface);z-index:1}
.configuration-authoring-actions :deep(p){flex-basis:100%;color:var(--text-secondary);font-size:var(--config-help-size)}
@media(max-width:640px){.configuration-authoring{--config-control-height:44px}.configuration-authoring-nav{grid-template-columns:1fr}.configuration-authoring-nav button{padding:var(--config-space-sm) var(--config-space-md)}}
</style>
