<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { taskCenterApi } from '@/services/taskCenterApi'
import { taskTime, type TaskRow, type TaskKind } from '@/services/taskCenter'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import TaskCenterDetail from './TaskCenterDetail.vue'

const props = defineProps<{ projectId: string; shotId: string }>()
const open = ref(false), loading = ref(false), busy = ref(false), error = ref('')
const rows = ref<TaskRow[]>([]), nextCursor = ref<string | null>(null), previousCursor = ref<string | null>(null)
const selected = ref<{ id: string; attemptId?: string } | null>(null)
let sequence = 0
function cursor(link: string | null) {
  if (link === null) return null
  const value = new URL(link, 'http://pagination.invalid').searchParams.get('cursor')
  if (!value) throw Error('历史记录分页缺少原游标，不能重读首页冒充下一页')
  return value
}
async function load(page?: string) {
  if (busy.value || loading.value) return
  const token = ++sequence, projectId = props.projectId, shotId = props.shotId
  open.value = true; loading.value = true; error.value = ''; selected.value = null
  rows.value = []; nextCursor.value = null; previousCursor.value = null
  try {
    const result = await taskCenterApi.list({ category: 'production', project_id: projectId,
      unit_key: `shot:${shotId}`, page_size: 10, ...(page ? { cursor: page } : {}) })
    if (result.results.some(row => row.kind !== 'preparation' || row.project_id !== projectId || !row.production_run_id))
      throw Error('历史记录不属于本项目的生产准备，已停止显示')
    const next = cursor(result.next), previous = cursor(result.previous)
    if (token !== sequence) return
    rows.value = result.results; nextCursor.value = next; previousCursor.value = previous
  } catch (cause) { if (token === sequence) error.value = apiErrorSummary(cause) }
  finally { if (token === sequence) loading.value = false }
}
function choose(kind: TaskKind, id: string, attemptId?: string) {
  if (busy.value || loading.value) return
  // Cross-preparation auxiliary navigation belongs to the full task center.
  // This embedded view remains within this page's exact primary preparations.
  if (kind !== 'preparation' || !rows.value.some(row => row.id === id)) {
    error.value = '其他准备请从本轮制作台查看；此处不离开本镜历史范围'; return
  }
  error.value = ''; selected.value = { id, attemptId }
}
watch([() => props.projectId, () => props.shotId], () => {
  sequence++; rows.value = []; selected.value = null; open.value = false; loading.value = false
  busy.value = false; error.value = ''; nextCursor.value = null; previousCursor.value = null
})
onBeforeUnmount(() => { sequence++ })
</script>

<template>
  <section class="shot-preparation-history" aria-label="本镜全驱动准备历史">
    <h4>本镜执行准备历史 · 所有驱动 · 只读</h4>
    <p>复用原任务中心，包含尚未建立工作流队列任务的准备。按原制作轮次和实例版本选择，不自动选择最新记录；未冻结、失败和已执行分别显示。</p>
    <button type="button" class="button ghost small" :disabled="loading || busy" @click="load()">{{open ? '重新读取本镜准备历史' : '查看本镜准备历史（所有驱动）'}}</button>
    <p v-if="loading" role="status">正在读取原准备记录…</p>
    <p v-if="error" role="alert" class="platform-alert warning">{{error}}</p>
    <template v-if="open && !loading">
      <p v-if="!rows.length && !error">本镜没有可见的生产准备记录；不表示其他镜头或任务未执行。</p>
      <label v-if="rows.length" class="field"><span>选择原准备（本页 {{rows.length}} 条）</span>
        <select :value="selected?.id || ''" :disabled="busy" @change="($event.target as HTMLSelectElement).value ? choose('preparation', ($event.target as HTMLSelectElement).value) : selected=null">
          <option value="">请选择原记录</option>
          <option v-for="row in rows" :key="row.id" :value="row.id">{{taskTime(row.created_at)}} · {{row.title}} · {{row.execution_instance_name || '原实例未记录'}} v{{row.execution_version ?? '未知'}} · {{row.status}}</option>
        </select>
      </label>
      <div v-if="nextCursor || previousCursor" class="pages">
        <button type="button" class="button ghost small" :disabled="busy || !previousCursor" @click="previousCursor && load(previousCursor)">上一页准备</button>
        <button type="button" class="button ghost small" :disabled="busy || !nextCursor" @click="nextCursor && load(nextCursor)">下一页准备</button>
      </div>
      <TaskCenterDetail v-if="selected" :key="selected.id" kind="preparation" :id="selected.id" :attempt-id="selected.attemptId"
        readonly :expected-project-id="projectId" @select="choose" @busy="busy=$event"/>
    </template>
  </section>
</template>

<style scoped>
.shot-preparation-history{display:grid;gap:12px;min-width:0}.shot-preparation-history h4,.shot-preparation-history p{margin:0}.shot-preparation-history p{font-size:13px;line-height:1.7}.shot-preparation-history select{width:100%;min-width:0}.shot-preparation-history button{justify-self:start;white-space:normal}.pages{display:flex;gap:8px;flex-wrap:wrap}
</style>
