<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(defineProps<{ src?: string; alt?: string; accent?: string; label?: string }>(), { src: '', alt: '', accent: '#d99b53', label: 'REFERENCE ASSET' })

const placeholder = computed(() => {
  const safe = (props.label || props.alt || 'REFERENCE ASSET').replace(/[<>&]/g, '').slice(0, 24)
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="720" height="960"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#0b0e12"/><stop offset=".54" stop-color="${props.accent}" stop-opacity=".34"/><stop offset="1" stop-color="#080a0d"/></linearGradient><filter id="n"><feTurbulence baseFrequency=".7" numOctaves="3" stitchTiles="stitch"/><feColorMatrix values="0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 0 .055 0"/></filter></defs><rect width="100%" height="100%" fill="url(#g)"/><circle cx="495" cy="280" r="180" fill="none" stroke="${props.accent}" stroke-opacity=".22" stroke-width="2"/><path d="M50 715 Q275 360 680 680" fill="none" stroke="${props.accent}" stroke-opacity=".42" stroke-width="3"/><rect width="100%" height="100%" filter="url(#n)" opacity=".7"/><text x="48" y="845" fill="#fff" font-size="30" font-family="sans-serif" font-weight="600">${safe}</text><text x="48" y="888" fill="#fff" fill-opacity=".45" font-size="16" font-family="sans-serif" letter-spacing="3">LOCAL REFERENCE FRAME</text></svg>`
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
})
const current = ref(props.src || placeholder.value)
watch(() => props.src, (value) => { current.value = value || placeholder.value })
const fallback = () => { if (current.value !== placeholder.value) current.value = placeholder.value }
const retrySource = () => {
  if (props.src && current.value === placeholder.value) current.value = props.src
}
onMounted(() => {
  window.addEventListener('focus', retrySource)
  window.addEventListener('online', retrySource)
})
onBeforeUnmount(() => {
  window.removeEventListener('focus', retrySource)
  window.removeEventListener('online', retrySource)
})
</script>

<template><img :src="current" :alt="alt" loading="lazy" @error="fallback"></template>
