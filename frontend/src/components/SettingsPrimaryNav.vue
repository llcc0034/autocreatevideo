<script setup lang="ts">
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
  settingsSectionCatalog,
  type SettingsPrimaryNavigationId,
  type SettingsPrimaryNavigationItem,
  type SettingsSectionId,
} from '@/content/settingsSectionCatalog'

const route = useRoute()
const settingsSectionIds = new Set(settingsSectionCatalog.map((item) => item.id))

const navigationIcons: Record<SettingsPrimaryNavigationId, unknown> = {
  models: Boxes,
  workflows: Workflow,
  'acceptance-smoke': BadgeCheck,
  prompts: FileText,
  routing: GitBranch,
  defaults: Settings2,
  security: ShieldAlert,
  runtime: Cpu,
  governance: ShieldCheck,
}

const activeSettingsSection = (): SettingsSectionId => {
  const candidate = String(route.query.section || 'models') as SettingsSectionId
  return settingsSectionIds.has(candidate) ? candidate : 'models'
}
const isActive = (item: SettingsPrimaryNavigationItem) => item.kind === 'workspace'
  ? route.name === 'acceptance-smoke-center'
  : route.name === 'settings' && activeSettingsSection() === item.id
</script>

<template>
  <nav class="settings-center-nav" aria-label="系统设置一级导航">
    <RouterLink
      v-for="item in settingsPrimaryNavigation"
      :key="item.id"
      class="settings-center-nav-item"
      :class="{ active: isActive(item) }"
      :to="item.settingsRoute"
      :aria-current="isActive(item) ? 'page' : undefined"
    >
      <component :is="navigationIcons[item.id]" :size="17"/>
      <span><strong>{{ item.label }}</strong><small>{{ item.hint }}</small></span>
    </RouterLink>
  </nav>
</template>
