<script setup lang="ts">
import type { Component } from 'vue'
import { RouterLink, type RouteLocationRaw } from 'vue-router'
import { ChevronRight } from 'lucide-vue-next'

defineProps<{
  label: string
  name?: string
  status: string
  tone: 'linked' | 'info' | 'warning' | 'danger' | 'muted'
  description: string
  to?: RouteLocationRaw
  icon?: Component
  expanded?: boolean
}>()
defineEmits<{ activate: [] }>()
</script>

<template>
  <component
    :is="to ? RouterLink : 'button'" :to="to" :type="to ? undefined : 'button'"
    class="configuration-relation" :class="[`relation-${tone}`, { 'with-name': name }]"
    :title="`${label}${name ? ` · ${name}` : ''} · ${status}。${description}`"
    :aria-label="`${label}${name ? `：${name}` : ''}，${status}。${description}`"
    :aria-haspopup="to ? undefined : 'dialog'" :aria-expanded="to ? undefined : expanded"
    @click="$emit('activate')"
  >
    <span class="relation-kind"><component :is="icon" v-if="icon" :size="13" aria-hidden="true"/><span>{{ label }}</span></span>
    <span v-if="name" class="relation-name">{{ name }}</span>
    <span class="relation-state"><span class="relation-dot" aria-hidden="true"/>{{ status }}</span>
    <ChevronRight :size="13" class="relation-chevron" aria-hidden="true"/>
  </component>
</template>

<style scoped>
.configuration-relation {
  --relation-signal: var(--text-secondary);
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  min-height: 36px;
  padding: 5px 9px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  font: inherit;
  font-size: 11px;
  line-height: 1.5;
  text-align: left;
  text-decoration: none;
  cursor: pointer;
  transition: background-color .15s, border-color .15s;
}
.with-name { display: grid; grid-template-columns: 76px minmax(0, 1fr) auto 13px; }
.configuration-relation:hover { border-color: var(--border-strong); background: var(--surface-2); }
.configuration-relation:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.relation-kind, .relation-state { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.relation-kind { flex: 1; min-width: 0; color: var(--text-secondary); }
.relation-kind svg, .relation-chevron { flex: none; }
.relation-kind>span, .relation-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.relation-state { flex: none; color: var(--relation-signal); font-size: 11px; font-variant-numeric: tabular-nums; }
.relation-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.relation-chevron { color: var(--text-secondary); }
.relation-linked { --relation-signal: var(--green); }
.relation-info { --relation-signal: var(--cyan); }
.relation-warning { --relation-signal: var(--accent); }
.relation-danger { --relation-signal: var(--red); }
.relation-muted .relation-dot { border: 1px solid currentColor; background: transparent; }
@media (prefers-reduced-motion: reduce) { .configuration-relation { transition: none; } }
</style>
