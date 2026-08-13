<script setup lang="ts">
defineProps<{ modelValue: boolean; disabled?: boolean }>()
defineEmits<{ 'update:modelValue': [value: boolean] }>()
</script>
<template><button type="button" class="switch" :class="{ active: modelValue }" :disabled="disabled" @click="$emit('update:modelValue', !modelValue)"><span></span></button></template>
<style scoped>
.switch{width:42px;height:23px;padding:3px;border:1px solid var(--border-strong);border-radius:999px;background:#242830;cursor:pointer;transition:.2s}.switch span{display:block;width:15px;height:15px;border-radius:50%;background:#8d929b;transition:.22s;box-shadow:0 1px 3px #000}.switch.active{background:rgba(221,156,74,.25);border-color:rgba(237,176,92,.6)}.switch.active span{transform:translateX(18px);background:var(--accent)}.switch:disabled{opacity:.45;cursor:not-allowed}
</style>
