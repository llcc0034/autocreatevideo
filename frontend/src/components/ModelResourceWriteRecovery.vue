<script setup lang="ts">
import { modelResourceEditable, modelResourceLabel, type ModelResourceRow } from '@/services/modelResourceRecovery'
defineProps<{ busy:boolean; error:string; checked:boolean; records:ModelResourceRow[] }>()
defineEmits<{read:[];load:[string]}>()
</script>
<template>
  <section class="model-write-recovery" aria-label="模型资源写入结果核对">
    <strong>请求结果待核对 · 不会自动重复提交</strong>
    <p>先读取原目标或同族候选。没有记录不证明未写入；同名或最新版本不等于本次请求成功。选择载入时会再次读取确切记录。</p>
    <button class="button ghost" :disabled="busy" @click="$emit('read')">{{busy?'正在只读核对…':'只读核对保存结果'}}</button>
    <p v-if="error" role="alert">{{error}}</p>
    <p v-if="checked&&!records.length">未发现匹配记录；结果仍未确认，不自动重发。</p>
    <article v-for="row in records" :key="row.id"><strong>{{modelResourceLabel(row)}}</strong><small>{{row.id}}</small><button class="button ghost" :disabled="busy||!modelResourceEditable(row)" @click="$emit('load',row.id)">明确载入此记录</button></article>
  </section>
</template>
<style scoped>
.model-write-recovery{display:grid;gap:var(--config-space-md);padding:var(--config-space-lg);border:1px solid var(--accent);border-radius:var(--config-radius);font-size:var(--config-body-size);overflow-wrap:anywhere}.model-write-recovery p{margin:0}.model-write-recovery article{display:grid;gap:var(--config-space-sm);padding-top:var(--config-space-md);border-top:1px solid var(--border)}.model-write-recovery small{font-size:var(--config-help-size);color:var(--text-secondary)}.model-write-recovery button{justify-self:start;white-space:normal;min-height:var(--config-control-height)}
</style>
