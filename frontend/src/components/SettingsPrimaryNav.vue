<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import {
  BadgeCheck,
  Boxes,
  Cpu,
  FileText,
  GitBranch,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Workflow,
} from 'lucide-vue-next'
import {
  settingsPrimaryNavigation,
  settingsNavigationGroups,
  settingsSectionForQuery,
  type SettingsPrimaryNavigationId,
  type SettingsPrimaryNavigationItem,
} from '@/content/settingsSectionCatalog'

const route = useRoute()
const groups = computed(() => settingsNavigationGroups.map(group => ({ ...group,
  entries: group.items.map(id => settingsPrimaryNavigation.find(item => item.id === id)!),
})))

const navigationIcons: Record<SettingsPrimaryNavigationId, unknown> = {
  executions: GitBranch,
  models: Boxes,
  workflows: Workflow,
  fields: FileText,
  'acceptance-smoke': BadgeCheck,
  prompts: FileText,
  routing: GitBranch,
  defaults: Settings2,
  security: ShieldAlert,
  runtime: Cpu,
  governance: ShieldCheck,
}

const isActive = (item: SettingsPrimaryNavigationItem) => item.kind === 'workspace'
  ? route.name === 'acceptance-smoke-center'
  : route.name === 'settings' && settingsSectionForQuery(route.query) === item.id
</script>

<template>
  <nav class="settings-hierarchy-nav" aria-label="系统设置一级导航">
    <div v-for="group in groups" :key="group.id" class="settings-navigation-group">
      <span class="settings-navigation-group-label" :id="`settings-group-${group.id}`">{{ group.label }}</span>
      <div class="settings-center-nav" :aria-labelledby="`settings-group-${group.id}`">
    <RouterLink
      v-for="item in group.entries"
      :key="item.id"
      class="settings-center-nav-item"
      :class="{ active: isActive(item) }"
      :to="item.settingsRoute"
      :aria-current="isActive(item) ? 'page' : undefined"
    >
      <component :is="navigationIcons[item.id]" :size="17"/>
      <span><strong>{{ item.label }}</strong><small>{{ item.hint }}</small></span>
    </RouterLink>
      </div>
    </div>
  </nav>
</template>

<style scoped>
.settings-hierarchy-nav{display:grid;gap:12px}.settings-navigation-group{min-width:0}.settings-navigation-group-label{display:block;font-size:12px;color:var(--text-secondary);margin-bottom:6px}.settings-center-nav{grid-template-columns:repeat(auto-fit,minmax(190px,1fr));margin:0}.settings-center-nav-item{min-width:0}.settings-center-nav-item span{min-width:0}@media(max-width:460px){.settings-center-nav{grid-template-columns:1fr}}
</style>
