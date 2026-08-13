<script setup lang="ts">
defineProps<{ status: string; label?: string }>()
const labels: Record<string, string> = { draft: '草稿', writing: '编剧中', producing: '制作中', ready: '待生成', rendering: '生成中', completed: '已完成', queued: '排队中', running: '运行中', paused: '已暂停', failed: '失败', pending: '等待中', done: '完成' }
</script>
<template><span class="status-badge" :class="`is-${status}`"><i></i>{{ label || labels[status] || status }}</span></template>
<style scoped>
.status-badge{display:inline-flex;align-items:center;gap:6px;padding:5px 9px;border:1px solid var(--border);border-radius:999px;font-size:11px;color:var(--text-secondary);background:rgba(255,255,255,.025);white-space:nowrap}.status-badge i{width:5px;height:5px;border-radius:50%;background:#7a7f87}.is-running,.is-producing,.is-rendering{color:#e8ae63;border-color:rgba(232,174,99,.25);background:rgba(232,174,99,.07)}.is-running i,.is-producing i,.is-rendering i{background:#e8ae63;box-shadow:0 0 8px #e8ae63}.is-completed,.is-ready,.is-done{color:#70c9ad;border-color:rgba(112,201,173,.22);background:rgba(112,201,173,.06)}.is-completed i,.is-ready i,.is-done i{background:#70c9ad}.is-failed{color:#ff7b84;border-color:rgba(255,123,132,.25)}.is-failed i{background:#ff7b84}
</style>
