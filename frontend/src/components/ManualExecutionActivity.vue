<script setup lang="ts">
import type { ManualExecutionActivity } from '@/services/productionManual'
import { taskCenterTarget } from '@/services/taskCenter'
defineProps<{ activity?: ManualExecutionActivity; loading?: boolean; runId: string }>()
defineEmits<{ inspect: [] }>()
</script>

<template>
  <div class="execution-activity" aria-label="步骤与真实模型调用">
    <span v-if="loading">正在读取调用状态…</span>
    <span v-else-if="!activity" role="status">调用状态未能读取，请刷新制作状态。</span>
    <template v-else>
      <span>{{ activity.lifecycleLabel }}（不代表模型正在运行）</span>
      <span v-if="!activity.calls.length">尚未发起模型调用</span>
      <span v-for="call in activity.calls" :key="`${call.role}:${call.status}`">{{ call.roleLabel }}：{{ call.label }} {{ call.count }} 次</span>
      <details v-if="activity.preparations.length"><summary>准备记录</summary><span v-for="prep in activity.preparations" :key="prep.status">{{ prep.label }} {{ prep.count }} 项 </span></details>
      <button v-if="activity.calls.length || activity.preparations.length" type="button" class="button ghost tiny" @click="$emit('inspect')">查看已有准备与结果</button>
      <RouterLink class="button ghost tiny" :to="taskCenterTarget({ category: 'production', runId, stageId: activity.stageId })">查看本步骤任务</RouterLink>
    </template>
  </div>
</template>

<style scoped>
.execution-activity{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:12px}.execution-activity>span:first-child{color:var(--text-muted)}
</style>
