<script setup lang="ts">
import { computed } from 'vue'
import { presentPlatformStatus } from '@/services/platformAdapters'

const props = defineProps<{ status: string }>()
const presentation = computed(() => presentPlatformStatus(props.status))
</script>

<template>
  <span class="platform-status" :class="presentation.tone" :title="status || '服务端未返回状态'">
    {{ presentation.label }}<small v-if="presentation.tone === 'unknown'">{{ status }}</small>
  </span>
</template>
