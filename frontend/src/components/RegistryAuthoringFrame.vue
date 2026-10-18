<script setup lang="ts">
import ConfigurationAuthoringShell from './ConfigurationAuthoringShell.vue'
import type { AuthoringIssue } from '@/services/configurationAuthoring'
defineProps<{ enabled: boolean; status: string; busy?: boolean; issues?: AuthoringIssue[]; sections: {key:string;title:string;description:string}[] }>()
</script>
<template>
  <ConfigurationAuthoringShell v-if="enabled" label="模型与运行资源配置" :status="status" :busy="busy" :issues="issues" :sections="sections">
    <template v-for="section in sections" :key="section.key" #[section.key]><slot :name="section.key" /></template>
    <template #feedback><slot name="feedback" /></template>
    <template v-if="$slots.actions" #actions><slot name="actions" /></template>
  </ConfigurationAuthoringShell>
  <div v-else><slot name="all" /><slot name="feedback" /></div>
</template>
