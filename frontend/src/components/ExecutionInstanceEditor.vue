<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ConfigurationAuthoringShell from './ConfigurationAuthoringShell.vue'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import type { ExecutionInstance } from '@/services/executionRegistry'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { authoringIssues, authoringResponseStatus, isExplicitAuthoringRejection, type AuthoringIssue } from '@/services/configurationAuthoring'

const props = defineProps<{ instance: ExecutionInstance | null }>()
const emit = defineEmits<{ saved: [instance: ExecutionInstance, recovered: boolean]; dirty: [value: boolean]; processing: [value: boolean]; uncertain: [value: boolean] }>()
const original = props.instance ? { ...props.instance } : null
const baseline = { key: original?.key ?? '', name: original?.name ?? '', description: original?.description ?? '' }
const draft = ref({ ...baseline })
const busy = ref(false), reading = ref(false), error = ref(''), recoveryError = ref(''), unresolved = ref(false)
const issues = ref<AuthoringIssue[]>([]), records = ref<ExecutionInstance[]>([])
const recoveryPage = ref(0), hasMore = ref(false), readDone = ref(false)
const feedback = ref<HTMLElement | null>(null)
let disposed = false
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(baseline))
const canSave = computed(() => dirty.value && !busy.value && !reading.value && !unresolved.value && Boolean(draft.value.key && draft.value.name))
const sections = computed(() => [
  { key: 'identity', title: '名称与用途', description: '执行实例把可复用能力组织成版本；这里仅维护实例身份，不重复配置模型和提示词。', summary: draft.value.name || '尚未填写名称' },
  { key: 'versions', title: '下一步：配置具体版本', description: '版本负责输入准备、模型能力调用和输出约定；配方和独立素材任务选择精确版本。', summary: original ? '不修改已有版本及引用' : '创建后进入首个版本配置' },
])
const status = computed(() => busy.value ? '正在保存实例信息…' : reading.value ? '正在回读实例记录，不会保存或创建…'
  : unresolved.value ? '保存结果待核对 · 已阻止重复提交' : error.value ? '保存失败 · 已填内容保留'
  : original ? `实例信息 · 修订 ${original.revision}${dirty.value ? ' · 有未保存修改' : ''}` : '新建实例 · 尚未保存')
watch(dirty, value => emit('dirty', value), { immediate: true, flush: 'sync' })
watch(() => busy.value || reading.value, value => emit('processing', value), { immediate: true, flush: 'sync' })
watch(unresolved, value => emit('uncertain', value), { immediate: true, flush: 'sync' })
watch(error, async value => { if (!value || issues.value.length) return; await nextTick(); feedback.value?.focus(); feedback.value?.scrollIntoView({ block: 'nearest' }) })
function beforeLeave(event: BeforeUnloadEvent) { if (dirty.value || busy.value || unresolved.value) { event.preventDefault(); event.returnValue = '' } }
onMounted(() => { if (typeof window !== 'undefined') window.addEventListener('beforeunload', beforeLeave) })
onBeforeUnmount(() => { disposed = true; if (typeof window !== 'undefined') window.removeEventListener('beforeunload', beforeLeave) })
function checkIdentity(row: ExecutionInstance, expectedId?: string) {
  if (row.key !== (original?.key ?? draft.value.key) || expectedId && row.id !== expectedId) throw Error('实例身份不一致，未载入返回记录。')
  return row
}
async function save() {
  if (!canSave.value) return
  busy.value = true; error.value = ''; issues.value = []
  try {
    const changes = original ? {
      expected_revision: original.revision,
      ...(draft.value.name !== baseline.name ? { name: draft.value.name } : {}),
      ...(draft.value.description !== baseline.description ? { description: draft.value.description } : {}),
    } : null
    const row = original ? await executionRegistryApi.updateInstance(original.id, changes!) : await executionRegistryApi.createInstance({ ...draft.value })
    checkIdentity(row, original?.id)
    if (!disposed) { emit('processing', false); emit('dirty', false); emit('saved', row, false) }
  } catch (cause) {
    if (disposed) return
    error.value = apiErrorSummary(cause)
    issues.value = authoringIssues(cause, { key: 'identity', name: 'identity', description: 'identity' })
    unresolved.value = !isExplicitAuthoringRejection(cause) || authoringResponseStatus(cause) === 409
  } finally { busy.value = false }
}
async function readRecords(nextPage = 1) {
  if (!unresolved.value || busy.value || reading.value) return
  reading.value = true; recoveryError.value = ''
  if (nextPage === 1) { records.value = []; readDone.value = false; hasMore.value = false }
  try {
    if (original) {
      const row = checkIdentity(await executionRegistryApi.instance(original.id), original.id)
      if (!disposed) { records.value = [row]; hasMore.value = false; readDone.value = true }
    } else {
      const result = await executionRegistryApi.instances(nextPage, draft.value.key)
      if (disposed) return
      records.value = nextPage === 1 ? result.results : [...records.value, ...result.results.filter(row => !records.value.some(existing => existing.id === row.id))]
      recoveryPage.value = nextPage; hasMore.value = Boolean(result.next); readDone.value = true
    }
  } catch (cause) { if (!disposed) recoveryError.value = apiErrorSummary(cause) }
  finally { reading.value = false }
}
async function useRecord(candidate: ExecutionInstance) {
  if (busy.value || reading.value || !unresolved.value || candidate.key !== draft.value.key) return
  reading.value = true; recoveryError.value = ''
  try {
    const row = checkIdentity(await executionRegistryApi.instance(candidate.id), candidate.id)
    if (!disposed) { emit('processing', false); emit('dirty', false); emit('uncertain', false); emit('saved', row, true) }
  } catch (cause) { if (!disposed) recoveryError.value = apiErrorSummary(cause) }
  finally { reading.value = false }
}
</script>

<template>
  <form class="execution-instance-form" @submit.prevent="save">
    <ConfigurationAuthoringShell label="执行实例信息" :status="status" :busy="busy||reading" :issues="issues" :sections="sections">
      <template #feedback>
        <div v-if="error&&!issues.length" ref="feedback" tabindex="-1" role="alert" class="platform-alert danger">{{ unresolved?'保存结果需要回读核对；不会自动重发。':error }}<details v-if="unresolved"><summary>原错误详情</summary>{{ error }}</details></div>
        <section v-if="unresolved" class="instance-recovery" aria-label="核对实例保存结果">
          <p>仅回读记录，不按名称或最新版本认领保存结果。载入会放弃本地未保存修改，且不证明该记录由此次请求创建；没有查到也不代表保存未发生。</p>
          <button type="button" class="button ghost small" :disabled="reading" @click="readRecords()">{{ reading?'正在核对…':'回读实例记录' }}</button>
          <p v-if="recoveryError" role="alert">{{ recoveryError }}</p>
          <p v-if="readDone&&!records.length">本页未查到记录；请勿重复新建，可关闭后在实例目录核对。</p>
          <article v-for="record in records" :key="record.id"><strong>{{ record.name }}</strong><p>{{ record.key }} · 修订 {{ record.revision }} · {{ record.is_active?'已启用':'已停用' }}</p><p>{{ record.description }}</p><small>{{ record.id }}</small><button type="button" class="button ghost small" :disabled="reading||record.key!==draft.key" @click="useRecord(record)">载入此实例，放弃本地修改</button></article>
          <button v-if="hasMore" type="button" class="button ghost small" :disabled="reading" @click="readRecords(recoveryPage+1)">查看更多核对记录</button>
        </section>
      </template>
      <template #identity>
        <fieldset :disabled="busy||reading||unresolved" class="instance-fields">
          <label data-authoring-field="name">显示名称<input v-model="draft.name" maxlength="160" required/><small>用于配方、素材任务和配置目录中识别此能力。</small></label>
          <label data-authoring-field="description">用途说明<textarea v-model="draft.description" rows="3"/><small>说明适用场景；具体输入值在项目或任务中提供。</small></label>
          <label data-authoring-field="key">唯一标识<input v-model="draft.key" pattern="[a-z][a-z0-9_.\-]{1,99}" maxlength="100" required :disabled="Boolean(original)"/><small>小写字母开头，2–100字符。创建后不可修改，不按名称自动生成。</small></label>
        </fieldset>
      </template>
      <template #versions><ol class="instance-next"><li>选择能力与兼容实现</li><li>配置输入来源、提示词处理及参数</li><li>核对输出并保存版本</li></ol><p>保存实例信息不会发布版本、启动校验、Smoke或生成。{{ original?'此处修改名称和用途，不覆盖已有版本配置。':'创建成功后进入空版本表单；只有继续保存才会创建版本。' }}</p></template>
      <template #actions><button type="submit" class="button primary small" :disabled="!canSave">{{ busy?'正在保存…':original?'保存实例信息':'创建实例并配置首个版本' }}</button></template>
    </ConfigurationAuthoringShell>
  </form>
</template>

<style scoped>
.execution-instance-form{min-width:0}.instance-fields{display:grid;gap:var(--config-space-lg);border:0;margin:0;padding:0;min-width:0}.instance-fields label{display:grid;gap:var(--config-space-sm)}.instance-fields small,.execution-instance-form p,.instance-recovery small{font-size:var(--config-help-size);line-height:1.6;color:var(--text-secondary)}.instance-next{padding-left:22px;font-size:var(--config-body-size)}.instance-recovery{display:grid;gap:var(--config-space-md);overflow-wrap:anywhere}.instance-recovery article{display:grid;gap:var(--config-space-sm);border-top:1px solid var(--border);padding-top:var(--config-space-md)}.instance-recovery button{white-space:normal;justify-self:start}
</style>
