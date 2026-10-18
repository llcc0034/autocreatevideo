<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { AlertTriangle, ChevronDown, RefreshCw } from 'lucide-vue-next'
import { repository } from '@/services/repository'
import type { TaskObservationPage } from '@/services/taskObservations'

const props = defineProps<{ taskId: string }>()
const open = ref(false)
const loading = ref(false)
const error = ref('')
const result = ref<TaskObservationPage | null>(null)
const page = ref(1)
let sequence = 0
async function load(targetPage = 1) {
  const request = ++sequence
  const taskId = props.taskId
  loading.value = true
  error.value = ''
  result.value = null
  try {
    const next = await repository.getTaskObservations(taskId, targetPage)
    if (request === sequence && taskId === props.taskId) { result.value = next; page.value = targetPage }
  } catch (cause) {
    if (request === sequence) error.value = cause instanceof Error ? cause.message : '读取检查记录失败'
  } finally { if (request === sequence) loading.value = false }
}
function toggle() { open.value = !open.value; if (open.value) void load() }
watch(() => props.taskId, () => { ++sequence; open.value = false; loading.value = false; result.value = null; error.value = ''; page.value = 1 })
onBeforeUnmount(() => { ++sequence })
const time = (value: string) => new Date(value).toLocaleString()
const codeLabel: Record<string, string> = {
  contract_unavailable: '无法读取执行端节点目录', contract_invalid: '节点检查未通过，详见任务失败原因',
  comfyui_resource_release_unconfirmed: '未能确认资源释放结果', comfyui_resource_release_rejected: '执行端拒绝资源释放',
  comfyui_resource_release_exclusive_worker_required: '未满足独占执行条件，未发起资源释放',
}
</script>

<template>
  <section class="task-checks panel">
    <button type="button" class="checks-toggle" :aria-expanded="open" @click="toggle"><strong>执行检查记录</strong><span>{{ open ? '收起' : '查看' }}<ChevronDown :size="14"/></span></button>
    <div v-if="open" class="checks-body">
      <p>显示每次实际完成的节点检查与资源释放。检查通过不代表烟测或生成成功；“领取次数”是 worker 接手次数，不是付费调用次数。</p>
      <button type="button" :disabled="loading" @click="load(page)"><RefreshCw :size="13"/>刷新</button>
      <p v-if="loading" role="status">正在读取检查记录…</p>
      <p v-else-if="error" class="checks-error" role="alert"><AlertTriangle :size="16"/>{{ error }}</p>
      <template v-else-if="result">
        <p v-if="!result.count">尚无检查记录。可能尚未执行到此步骤或未启用资源释放，不等于检查通过。</p>
        <article v-for="item in result.results" :key="item.id">
          <header><strong>{{ item.kind === 'preflight' ? '提交前节点检查' : '提交前资源释放' }}</strong><span :class="item.status">{{ item.status === 'passed' ? '通过' : '失败' }}</span></header>
          <p>{{ time(item.startedAt) }} → {{ time(item.finishedAt) }} · 第 {{ item.claimNumber }} 次领取</p>
          <p v-if="item.kind === 'preflight' && item.status === 'passed'">节点 {{ item.nodeCount }} 个 · 连接 {{ item.linkCount }} 条</p>
          <p v-if="item.kind === 'resource_release'">卸载模型：{{ item.unloadModels ? '是' : '否' }} · 释放内存：{{ item.freeMemory ? '是' : '否' }} · HTTP：{{ item.httpStatus ?? '未取得回执' }}</p>
          <p v-if="item.errorCode" class="checks-error">{{ codeLabel[item.errorCode] || item.errorCode }}</p>
          <details><summary>查看证据摘要</summary><dl><dt>请求摘要</dt><dd>{{ item.requestSha256 }}</dd><dt>领取开始</dt><dd>{{ time(item.claimStartedAt) }}</dd><template v-if="item.schemaSha256"><dt>节点目录摘要</dt><dd>{{ item.schemaSha256 }}</dd></template><template v-if="item.policySha256"><dt>资源释放策略摘要</dt><dd>{{ item.policySha256 }}</dd></template></dl></details>
        </article>
        <footer v-if="result.count"><span>共 {{ result.count }} 条 · 第 {{ page }} 页</span><button type="button" :disabled="!result.hasPrevious" @click="load(page-1)">上一页</button><button type="button" :disabled="!result.hasNext" @click="load(page+1)">下一页</button></footer>
      </template>
    </div>
  </section>
</template>

<style scoped>
.task-checks{padding:16px}.checks-toggle{display:flex;align-items:center;justify-content:space-between;width:100%;border:0;background:transparent;color:inherit;cursor:pointer}.checks-toggle span,.checks-body button{display:inline-flex;align-items:center;gap:6px}.checks-body{margin-top:12px}.checks-body p{font-size:12px;line-height:1.6;color:var(--text-secondary)}article{margin-top:10px;padding:12px;border:1px solid var(--border);border-radius:8px}header,footer{display:flex;align-items:center;gap:12px;flex-wrap:wrap}header{justify-content:space-between}footer{margin-top:12px}.passed{color:var(--green)}.failed,.checks-error{color:var(--red)!important}summary{cursor:pointer;font-size:12px}dl{display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px 12px;font-size:12px}dd{margin:0;overflow-wrap:anywhere}.checks-body button{padding:5px 10px;border:1px solid var(--border);border-radius:6px;background:transparent;color:inherit}.checks-body button:disabled{opacity:.45}
</style>
