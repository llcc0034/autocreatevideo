<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { repository } from '@/services/repository'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import type { TaskInputPrompt, Workflow } from '@/types'
import FrozenTaskReferences from './FrozenTaskReferences.vue'
import ShotPreparationHistory from './ShotPreparationHistory.vue'
const props = defineProps<{ projectId: string; shotId: string }>()
const open = ref(false), loading = ref(false), error = ref(''), tasks = ref<Workflow[]>([]), selectedId = ref('')
const task = ref<Workflow | null>(null), prompt = ref<TaskInputPrompt | null>(null)
const parameters = computed(() => (task.value?.inputParameters?.parameters || []) as Array<{key:string;value:unknown;value_source:string}>)
let sequence = 0
async function load() {
  const token = ++sequence, projectId = props.projectId, shotId = props.shotId
  open.value = true; loading.value = true; error.value = ''; tasks.value = []; selectedId.value = ''; task.value = null; prompt.value = null
  try {
    const rows = await repository.getShotGenerationTasks(projectId, shotId)
    if (rows.some(row => row.projectId !== projectId || row.execution?.unitKey !== `shot:${shotId}`)) throw new Error('任务范围与当前分镜不一致，已停止显示。')
    if (token !== sequence) return
    tasks.value = rows
    if (rows.length === 1) { selectedId.value = rows[0]!.id; await select() }
  } catch (cause) { if (token === sequence) error.value = apiErrorSummary(cause) }
  finally { if (token === sequence) loading.value = false }
}
async function select() {
  const token = ++sequence, id = selectedId.value, projectId = props.projectId, shotId = props.shotId
  task.value = null; prompt.value = null; error.value = ''; loading.value = false
  if (!id) return
  loading.value = true
  try {
    const detail = await repository.getWorkflow(id)
    if (detail.id !== id || detail.projectId !== projectId || detail.execution?.unitKey !== `shot:${shotId}`) throw new Error('任务不属于所选记录或当前分镜。')
    const result = await repository.getTaskInputPrompt(id)
    const inputs = detail.inputParameters
    if (detail.inputContractStatus !== 'available' || !inputs
      || result.taskId !== (detail.taskId || detail.id) || result.preparationId !== detail.execution.preparationId
      || result.preparationId !== inputs.preparation_id || result.attemptId !== inputs.attempt_id
      || result.requestSha256 !== inputs.request_sha256 || result.executionInstanceVersionId !== inputs.execution_instance_version_id)
      throw new Error('提示词、素材绑定与任务原请求证据不一致，已停止显示。')
    if (token === sequence) { task.value = detail; prompt.value = result }
  } catch (cause) { if (token === sequence) error.value = apiErrorSummary(cause) }
  finally { if (token === sequence) loading.value = false }
}
watch([() => props.projectId, () => props.shotId], () => { sequence++; tasks.value=[]; selectedId.value=''; task.value=null; prompt.value=null; error.value=''; loading.value=false; open.value=false })
onBeforeUnmount(() => { sequence++ })
</script>

<template>
  <section class="panel shot-execution-evidence">
    <ShotPreparationHistory :project-id="projectId" :shot-id="shotId"/>
    <header><div><h3>工作流历史请求 · 配置、素材与提示词</h3><p>下方是 ComfyUI 原素材预览；其他驱动及尚未入队的记录可从上方“本镜执行准备历史”读取。按具体任务读取原冻结输入，与当前分镜及待审核选择分开，不修改审核依据或执行调用。</p></div><button type="button" class="button ghost small" :disabled="loading" @click="load">{{ open ? '重新读取执行记录' : '查看本镜执行记录' }}</button></header>
    <template v-if="open">
      <p v-if="loading" role="status">正在读取分镜执行证据…</p>
      <p v-if="error" role="alert">{{ error }}</p>
      <label v-if="tasks.length" class="field"><span>选择要查看的任务（不会自动选择其他轮次）</span><select v-model="selectedId" :disabled="loading" @change="select"><option value="">请选择</option><option v-for="row in tasks" :key="row.id" :value="row.id">{{ row.createdAt }} · 制作轮次 {{ row.execution?.productionRunId.slice(0,8) }} · {{ row.backendStatus }} · {{ row.id.slice(0,8) }}</option></select></label>
      <p v-else-if="!loading && !error">本镜尚无已创建的生成任务。准备中的字段和输入请在生产运行的“执行准备”中查看。</p>
      <template v-if="task && prompt">
        <p><strong>{{ prompt.promptMode==='manual' ? '人工完整提示词' : prompt.promptMode==='profile' ? '提示词策略组装' : '不使用提示词' }}</strong> · 执行实例版本 {{ prompt.executionInstanceVersionId }}</p>
        <p v-if="prompt.promptProfileVersionId">提示词策略版本 {{ prompt.promptProfileVersionId }}</p>
        <p v-if="prompt.promptMode==='manual'">本次直接使用以下完整提示词，没有通过策略逐字段组装。创作字段为空或尚未翻译，不代表此次提交内容为空。</p>
        <p v-else-if="prompt.promptMode==='profile'">这里是任务实际使用的策略结果；候选生成、翻译和确认过程请查看本轮对应执行准备。当前编辑字段不替代此冻结结果。</p>
        <article v-for="channel in prompt.channels" :key="channel.key"><strong>{{ channel.key }} · {{ channel.characterCount }} 字符</strong><pre>{{ channel.text }}</pre><small>来源：{{ channel.valueSource }}</small></article>
        <FrozenTaskReferences :key="task.id" :task="task"/>
        <details><summary>原运行参数</summary><dl><template v-for="parameter in parameters" :key="parameter.key"><dt>{{ parameter.key }}</dt><dd>{{ parameter.value }} · {{ parameter.value_source }}</dd></template></dl></details>
        <RouterLink :to="`/projects/${projectId}/production/${task.execution!.productionRunId}`" class="button ghost small">打开这轮制作与执行准备</RouterLink>
      </template>
    </template>
  </section>
</template>

<style scoped>
.shot-execution-evidence{padding:16px;display:grid;gap:12px;min-width:0}.shot-execution-evidence header{display:flex;gap:12px;justify-content:space-between;align-items:center;flex-wrap:wrap}.shot-execution-evidence h3,.shot-execution-evidence p{margin:0}.shot-execution-evidence p,.shot-execution-evidence small{color:var(--text-muted);line-height:1.6}.shot-execution-evidence pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:12px;background:var(--surface-2);max-height:320px;overflow:auto}.shot-execution-evidence select{width:100%}.shot-execution-evidence dl{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr);gap:8px}.shot-execution-evidence dd{margin:0}.shot-execution-evidence li,.shot-execution-evidence dd,.shot-execution-evidence dt{overflow-wrap:anywhere}
</style>
