<script setup lang="ts">
import { X } from 'lucide-vue-next'
import { nextTick, onBeforeUnmount, ref, useId, watch } from 'vue'

const props = withDefaults(
  defineProps<{ open: boolean; title?: string; eyebrow?: string; width?: string; dismissible?: boolean }>(),
  { dismissible: true },
)
const emit = defineEmits<{ close: [] }>()

const dialogRef = ref<HTMLElement | null>(null)
const titleId = `modal-title-${useId()}`
let returnFocusElement: HTMLElement | null = null

const focusableSelector = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])'
].join(',')

const focusableElements = () => {
  if (!dialogRef.value) return []
  return Array.from(dialogRef.value.querySelectorAll<HTMLElement>(focusableSelector)).filter((element) => {
    const style = window.getComputedStyle(element)
    return element.getAttribute('aria-hidden') !== 'true'
      && style.display !== 'none'
      && style.visibility !== 'hidden'
      && !element.closest('[inert]')
  })
}

const focusDialog = () => {
  const firstFocusable = focusableElements()[0]
  ;(firstFocusable || dialogRef.value)?.focus()
}

const restoreFocus = () => {
  if (returnFocusElement?.isConnected) returnFocusElement.focus()
  returnFocusElement = null
}

const close = () => { if (props.dismissible) emit('close') }

const onKeydown = (event: KeyboardEvent) => {
  const dialog = dialogRef.value
  if (!dialog) return

  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    close()
    return
  }

  if (event.key !== 'Tab') return
  const focusable = focusableElements()
  if (!focusable.length) {
    event.preventDefault()
    dialog.focus()
    return
  }

  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  const active = document.activeElement
  if (event.shiftKey && (active === first || active === dialog || !dialog.contains(active))) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (active === last || active === dialog || !dialog.contains(active))) {
    event.preventDefault()
    first.focus()
  }
}

watch(
  () => props.open,
  async (open) => {
    if (open) {
      returnFocusElement = document.activeElement instanceof HTMLElement ? document.activeElement : null
      await nextTick()
      if (props.open) focusDialog()
      return
    }

    await nextTick()
    if (!props.open) restoreFocus()
  },
  { immediate: true, flush: 'post' }
)

onBeforeUnmount(restoreFocus)
</script>

<template>
  <Teleport to="body">
    <Transition name="modal">
      <div v-if="open" class="modal-backdrop" @mousedown.self="close" @keydown="onKeydown">
        <section
          ref="dialogRef"
          class="modal-card"
          role="dialog"
          aria-modal="true"
          :aria-labelledby="titleId"
          tabindex="-1"
          :style="{ maxWidth: width || '620px' }"
        >
          <header class="modal-header"><div><span v-if="eyebrow" class="eyebrow">{{ eyebrow }}</span><h2 :id="titleId">{{ title }}</h2></div><button type="button" class="icon-btn" :disabled="!dismissible" aria-label="关闭" @click="close"><X :size="18" /></button></header>
          <div class="modal-body"><slot /></div>
          <footer v-if="$slots.footer" class="modal-footer"><slot name="footer" /></footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
