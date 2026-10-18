<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { executionPreparationApi } from '@/services/executionPreparationApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import type { StageExecutionPreparation } from '@/services/executionPreparation'
import PlatformStatusPill from './PlatformStatusPill.vue'

const props = defineProps<{ stageId: string; kind: 'primary' | 'auxiliary'; unitKey: string; latestId: string; busy: boolean }>()
const emit = defineEmits<{ select: [item: StageExecutionPreparation] }>()
const result = ref<Awaited<ReturnType<typeof executionPreparationApi.forStage>> | null>(null)
const loading = ref(false), error = ref(''), page = ref(1)
let serial = 0
onBeforeUnmount(() => { serial++ })
async function load(target = 1) {
  const current = ++serial
  loading.value = true; error.value = ''; page.value = target
  try {
    const data = await executionPreparationApi.forStage(props.stageId, target, props.kind, props.unitKey)
    if (current === serial) result.value = data
  } catch (cause) { if (current === serial) error.value = apiErrorSummary(cause) }
  finally { if (current === serial) loading.value = false }
}
const time = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false })
watch(() => [props.stageId, props.kind, props.unitKey, props.latestId], () => { result.value = null; void load() }, { immediate: true })
</script>

<template>
  <section class="unit-history" aria-label="执行版本与历史记录">
    <p class="history-help">按创建时间倒序。这里是执行记录，不是正文修订或采用版本；历史失败会保留，查看不会重试。</p>
    <p v-if="loading" role="status">正在读取执行历史…</p>
    <div v-else-if="error" class="platform-alert danger" role="alert">{{ error }} <button type="button" class="button ghost small" @click="load(page)">重新读取历史</button></div>
    <template v-else-if="result">
      <p v-if="!result.count">该制作对象暂无可读取的执行记录。</p>
      <div v-for="item in result.results" :key="item.id" class="history-row">
        <div><strong>{{ item.presentation.label }}</strong>
          <p><span v-if="item.id === latestId">最近一次 · </span><template v-if="item.presentation.task">第 {{ item.presentation.task.attemptNumber }} 次 · <PlatformStatusPill :status="item.presentation.task.status"/> <span v-if="item.presentation.task.errorCode"> · {{ item.presentation.task.errorCode }}</span></template><span v-else>尚未创建调用 · <PlatformStatusPill :status="item.status"/></span></p>
          <small>创建于 {{ time(item.createdAt) }}（本地时间）</small>
        </div>
        <button type="button" class="button ghost small" :disabled="busy || !item.executionInstanceVersion" @click="emit('select', item)">查看此执行记录</button>
      </div>
      <footer><span>共 {{ result.count }} 条执行记录 · 第 {{ page }} 页</span>
        <button type="button" class="button ghost small" :disabled="busy || !result.previous" @click="load(page - 1)">上一页历史</button>
        <button type="button" class="button ghost small" :disabled="busy || !result.next" @click="load(page + 1)">下一页历史</button>
      </footer>
    </template>
  </section>
</template>

<style scoped>
.unit-history { width: 100%; min-width: 0; padding: .5rem 0 .5rem 1rem; border-left: 2px solid var(--border-color); }
.history-help, small { color: var(--text-secondary); font-size: 13px; }
.history-row, footer { display: flex; gap: .75rem; align-items: center; flex-wrap: wrap; padding: .75rem 0; }
.history-row { border-bottom: 1px solid var(--border-color); }
.history-row > div { flex: 1 1 260px; min-width: 0; overflow-wrap: anywhere; }
p { margin: .5rem 0; }
footer { justify-content: flex-end; }
@media(max-width:640px) { .unit-history { padding-left: .5rem; } }
</style>
