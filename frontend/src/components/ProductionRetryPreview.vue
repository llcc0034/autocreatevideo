<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { sameRetryContext, type RetryContext, type RetryPreview } from '@/services/productionRetryPreview'
import { taskCenterTarget } from '@/services/taskCenter'
import PlatformStatusPill from './PlatformStatusPill.vue'

const props = defineProps<{ runId: string; context: RetryContext }>()
const emit = defineEmits<{ ready: [value: boolean] }>()
const data = ref<RetryPreview | null>(null), loading = ref(false), error = ref(''), page = ref(1)
let requestId = 0
async function load(target = page.value) {
  const request = ++requestId
  emit('ready', false)
  loading.value = true; error.value = ''; data.value = null
  try {
    const result = await platformApi.getProductionRetryPreview(props.runId, props.context.expectedStageId, target)
    if (request !== requestId) return
    if (!sameRetryContext(props.context, result.retryContext)) throw Error('失败调用范围已变化，请关闭确认框并刷新运行后重新确认。')
    data.value = result; page.value = target
    emit('ready', result.can_submit)
  } catch (e) {
    if (request === requestId) error.value = apiErrorSummary(e, '读取重试影响失败')
  } finally { if (request === requestId) loading.value = false }
}
watch(() => [props.runId, props.context], () => { page.value = 1; void load(1) }, { immediate: true, deep: true })
onBeforeUnmount(() => { requestId++; emit('ready', false) })
const labels = { retry: '可重试', skip: '跳过', attention: '需处理' }
</script>

<template>
  <section aria-label="阶段重试影响" class="retry-impact">
    <div class="retry-impact-heading"><strong>本次重试影响</strong><button class="button ghost small" :disabled="loading" @click="load()">重新读取</button></div>
    <p v-if="loading" role="status">正在读取原任务与输入，不会提交模型或装配。</p>
    <p v-else-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <template v-else-if="data">
      <p>{{ data.message }}</p>
      <p v-if="data.reason" class="platform-alert warning" role="alert">{{ data.reason }}</p>
      <p v-if="data.mode === 'assembly_jobs'" class="muted">{{ data.assembly_count }} 个当前装配任务 · 消费输入 SHA：{{ data.retryContext.expectedInputSha256 || '未冻结' }} · 第 {{ page }} 页</p>
      <p v-else class="muted">{{ data.call_count }} 个当前主调用 · {{ data.preparation_count }} 个尚未提交的准备 · 共 {{ data.count }} 项，第 {{ page }} 页</p>
      <p v-if="!data.count" class="muted">当前没有已登记的模型调用或输入准备；其他阶段任务按原阶段流程处理。</p>
      <ul v-else class="retry-impact-list">
        <li v-for="item in data.results" :key="item.kind + item.id">
          <div class="retry-impact-heading"><strong>{{ labels[item.disposition] }}</strong><PlatformStatusPill :status="item.status"/></div>
          <p>{{ item.message }}</p>
          <template v-if="item.kind === 'assembly'">
            <p>第 {{ item.episode_number }} 集 · 装配任务 {{ item.id }}</p>
            <small>原消费输入 SHA：{{ item.consumer_input_sha256 || '未记录' }}</small>
            <p v-if="item.error_message" class="platform-alert warning">{{ item.error_message }}</p>
          </template>
          <small>执行单元：{{ item.unit_key }}<span v-if="item.error_code"> · {{ item.error_code }}</span></small>
          <RouterLink v-if="item.kind === 'assembly'" :to="taskCenterTarget({kind:'assembly',id:item.id,runId})">查看原装配任务</RouterLink>
          <RouterLink v-else-if="item.preparation_id" :to="taskCenterTarget({kind:'preparation',id:item.preparation_id,runId})">查看准备与调用</RouterLink>
        </li>
      </ul>
      <nav v-if="data.next || data.previous" aria-label="重试影响分页" class="retry-impact-heading">
        <button class="button ghost small" :disabled="!data.previous" @click="load(page - 1)">上一页</button>
        <button class="button ghost small" :disabled="!data.next" @click="load(page + 1)">下一页</button>
      </nav>
      <small class="muted">只读预览；“可重试”表示调用满足当前检查。提交仍核对阶段、完整失败集合和当前执行条件，不会因查看或翻页创建调用。</small>
    </template>
  </section>
</template>

<style scoped>
.retry-impact { min-width:0; margin-top:16px; }
.retry-impact-heading { display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap; }
.retry-impact-list { list-style:none; padding:0; margin:12px 0; }
.retry-impact-list li { padding:12px 0; border-bottom:1px solid var(--border); }
.retry-impact p,.retry-impact small { overflow-wrap:anywhere; }
.retry-impact small { display:block; }
.retry-impact-list a { display:inline-block; padding:8px 0; }
</style>
