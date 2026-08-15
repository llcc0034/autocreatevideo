<script setup lang="ts">
defineProps<{ compact?: boolean }>()
</script>

<template>
  <div class="brand-mark" :class="{ compact }">
    <div class="brand-symbol"><span></span><span></span><span></span></div>
    <div v-if="!compact" class="brand-copy"><strong>AutoPrompt</strong><small>AI PRODUCTION</small></div>
  </div>
</template>

<style scoped>
.brand-mark{display:flex;align-items:center;gap:11px}.brand-symbol{width:34px;height:34px;border:1px solid rgba(232,174,92,.45);border-radius:10px;display:flex;align-items:flex-end;justify-content:center;gap:3px;padding:8px;background:linear-gradient(145deg,rgba(241,186,103,.16),rgba(241,186,103,.02));box-shadow:inset 0 1px rgba(255,255,255,.08)}.brand-symbol span{display:block;width:4px;border-radius:3px 3px 1px 1px;background:linear-gradient(#ffd99b,#c47b31)}.brand-symbol span:nth-child(1){height:10px}.brand-symbol span:nth-child(2){height:17px}.brand-symbol span:nth-child(3){height:13px}.brand-copy{display:flex;flex-direction:column;line-height:1}.brand-copy strong{font-family:var(--font-display);font-size:17px;letter-spacing:.02em;color:#f6f1e8}.brand-copy small{font-size:7px;letter-spacing:.27em;color:var(--text-muted);margin-top:5px}.compact .brand-symbol{width:36px;height:36px}
</style>
