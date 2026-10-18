<script setup lang="ts">
import { X } from 'lucide-vue-next'
import { nextTick, onBeforeUnmount, ref, useId, watch } from 'vue'

const props = withDefaults(
  defineProps<{ open: boolean; title?: string; eyebrow?: string; width?: string; dismissible?: boolean; configuration?: boolean; preserveContent?: boolean }>(),
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
      && element.getClientRects().length > 0
      && !element.closest('[inert],[aria-hidden="true"]')
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
      <div v-if="open || preserveContent" v-show="open" :inert="!open" :aria-hidden="!open || undefined" class="modal-backdrop" @mousedown.self="close" @keydown="onKeydown">
        <section
          ref="dialogRef"
          class="modal-card"
          :class="{ 'configuration-modal': configuration }"
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

<style scoped>
.configuration-modal>.modal-header h2{font-family:var(--font);font-size:var(--config-title-size);font-weight:600;letter-spacing:0;overflow-wrap:anywhere}
.configuration-modal>.modal-header .eyebrow{font-size:var(--config-help-size)!important;letter-spacing:0}
.configuration-modal>.modal-header .icon-btn{min-width:var(--config-control-height);min-height:var(--config-control-height)}
.configuration-modal>.modal-body{padding:var(--config-space-xl);font-family:var(--font);font-size:var(--config-body-size);--config-sticky-offset:calc(-1 * var(--config-space-xl))}
.configuration-modal>.modal-body :deep(.button){min-height:var(--config-control-height);font-size:var(--config-body-size);white-space:normal;max-width:100%}
.configuration-modal>.modal-body :deep(summary){min-height:var(--config-control-height);font-size:var(--config-body-size);align-content:center;overflow-wrap:anywhere}
.configuration-modal>.modal-footer :deep(.button){min-height:var(--config-control-height);font-size:var(--config-body-size)}
@media(max-width:640px){.configuration-modal{--config-control-height:44px}}
</style>
