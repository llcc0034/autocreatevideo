<script setup lang="ts">
import type { ProjectorRecoveryRow } from '@/services/projectorAuthoringRecovery'
defineProps<{ busy: boolean; error: string; checked: boolean; records: ProjectorRecoveryRow[] }>()
defineEmits<{ read: []; load: [id: string] }>()
</script>
<template>
  <section class="projector-recovery" aria-label="Projector 写入结果核对">
    <strong>提交结果待核对 · 已阻止重复提交</strong>
    <p>读取原目标或同族记录，再明确选择载入。没有记录不证明未写入；不按名称或最新版本认定本次成功。</p>
    <button class="button ghost" :disabled="busy" @click="$emit('read')">{{ busy ? '正在只读核对…' : '只读核对保存结果' }}</button>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="checked && !records.length">未找到匹配记录，结果仍未确认；不会自动重发。</p>
    <article v-for="row in records" :key="row.id">
      <strong>{{ 'projector' in row ? `${row.projectorKey} · v${row.version} · ${row.status}` : `${row.name} · 方案族` }}</strong>
      <small>{{ row.id }}</small>
      <button class="button ghost" :disabled="busy || ('status' in row && row.status !== 'draft')" @click="$emit('load',row.id)">明确载入此记录</button>
    </article>
  </section>
</template>
<style scoped>
.projector-recovery{display:grid;gap:var(--config-space-md);padding:var(--config-space-lg);border:1px solid var(--accent);border-radius:var(--config-radius);font-size:var(--config-body-size);overflow-wrap:anywhere}.projector-recovery p{margin:0}.projector-recovery article{display:grid;gap:var(--config-space-sm);border-top:1px solid var(--border);padding-top:var(--config-space-md)}.projector-recovery small{font-size:var(--config-help-size);color:var(--text-secondary)}.projector-recovery button{justify-self:start;white-space:normal}
</style>
