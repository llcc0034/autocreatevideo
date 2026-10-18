<script setup lang="ts">
defineProps<{ messages: string[] }>()
</script>

<template>
  <section v-if="messages.length" class="quality-notices" aria-label="内容提醒（不阻断采用）" role="status">
    <strong>内容提醒（不阻断采用）</strong>
    <ul><li v-for="(message, index) in messages" :key="index">{{ message }}</li></ul>
    <p>提醒不会修改正文或单独阻止确认、采用；接口、文件和映射错误仍须处理。</p>
  </section>
</template>

<style scoped>
.quality-notices{min-width:0;max-width:100%;overflow-wrap:anywhere}
ul{margin:6px 0;padding-left:20px}p{margin:0;color:var(--text-muted)}
</style>
