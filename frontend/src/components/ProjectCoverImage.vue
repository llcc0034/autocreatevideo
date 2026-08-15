<script setup lang="ts">
import { ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  src?: string
  alt: string
  refresh?: () => Promise<string | undefined>
  eager?: boolean
}>(), { src: '', refresh: undefined, eager: false })

const emit = defineEmits<{ unavailable: [] }>()
const currentSrc = ref(props.src)
const refreshing = ref(false)
const refreshedOnce = ref(false)

watch(() => props.src, (value) => {
  if (value !== currentSrc.value) {
    currentSrc.value = value
    if (!refreshing.value) refreshedOnce.value = false
  }
})

const handleError = async () => {
  if (refreshing.value) return
  if (!props.refresh || refreshedOnce.value) {
    currentSrc.value = ''
    emit('unavailable')
    return
  }
  refreshing.value = true
  refreshedOnce.value = true
  const expiredUrl = currentSrc.value
  try {
    const renewedUrl = await props.refresh()
    if (renewedUrl && renewedUrl !== expiredUrl) currentSrc.value = renewedUrl
    else {
      currentSrc.value = ''
      emit('unavailable')
    }
  } catch {
    currentSrc.value = ''
    emit('unavailable')
  } finally {
    refreshing.value = false
  }
}
const handleLoad = () => {
  refreshedOnce.value = false
}
</script>

<template>
  <img v-if="currentSrc" :src="currentSrc" :alt="alt" :loading="eager ? 'eager' : 'lazy'" @load="handleLoad" @error="handleError">
</template>
