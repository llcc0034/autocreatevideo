<script setup lang="ts">
import { computed } from 'vue'
import { presentPlatformStatus } from '@/services/platformAdapters'

const props = withDefaults(defineProps<{ status: string; bilingual?: boolean; contextLabel?: string }>(), { bilingual: true })
const presentation = computed(() => presentPlatformStatus(props.status))
const label = computed(() => presentation.value.tone === 'unknown' ? presentation.value.label : props.contextLabel || presentation.value.label)
</script>

<template>
  <span class="platform-status" :class="presentation.tone" :title="status ? `${label}（${status}）` : '服务端未返回状态'">
    {{ label }}<small v-if="status && (bilingual || presentation.tone === 'unknown')">（{{ status }}）</small>
  </span>
</template>
