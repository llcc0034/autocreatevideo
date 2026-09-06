<script setup lang="ts">
import { computed } from 'vue'
import { presentPlatformStatus } from '@/services/platformAdapters'

const props = withDefaults(defineProps<{ status: string; bilingual?: boolean }>(), { bilingual: true })
const presentation = computed(() => presentPlatformStatus(props.status))
</script>

<template>
  <span class="platform-status" :class="presentation.tone" :title="status ? `${presentation.label}（${status}）` : '服务端未返回状态'">
    {{ presentation.label }}<small v-if="status && (bilingual || presentation.tone === 'unknown')">（{{ status }}）</small>
  </span>
</template>
