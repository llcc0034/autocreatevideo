<script setup lang="ts">
import { X } from 'lucide-vue-next'
defineProps<{ open: boolean; title?: string; eyebrow?: string; width?: string }>()
defineEmits<{ close: [] }>()
</script>

<template>
  <Teleport to="body">
    <Transition name="modal">
      <div v-if="open" class="modal-backdrop" @mousedown.self="$emit('close')">
        <section class="modal-card" :style="{ maxWidth: width || '620px' }">
          <header class="modal-header"><div><span v-if="eyebrow" class="eyebrow">{{ eyebrow }}</span><h2>{{ title }}</h2></div><button class="icon-btn" aria-label="关闭" @click="$emit('close')"><X :size="18" /></button></header>
          <div class="modal-body"><slot /></div>
          <footer v-if="$slots.footer" class="modal-footer"><slot name="footer" /></footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
