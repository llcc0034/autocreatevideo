<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { isAxiosError } from 'axios'
import BaseModal from './BaseModal.vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { distributionStates, distributionRecoveryRequest, readDistributionPage, readDistributionPhase, type DistributionPhase } from '@/services/taskDistribution'
import type { JsonObject, ProductionRun } from '@/services/platformTypes'

const props = defineProps<{ run: ProductionRun }>()
const open = ref(false), busy = ref(false), loading = ref(false), error = ref(''), notice = ref(''), page = ref(1)
const detail = ref<ReturnType<typeof readDistributionPage> | null>(null), pending = ref<JsonObject | null>(null)
let sequence = 0
async function load(targetPage = page.value) {
  if (busy.value) return
  const token = ++sequence; detail.value = null; error.value = ''; loading.value = true
  try {
    const result = readDistributionPage(await platformApi.getTaskDistributions(props.run.id, targetPage))
    if (token === sequence) { detail.value = result; page.value = targetPage }
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) loading.value = false }
}
function show() { open.value = true; void load(1) }
function close() { if (!busy.value) { open.value = false; sequence++; loading.value = false; detail.value = null } }
async function submit(row?: DistributionPhase) {
  if (busy.value || loading.value || (!pending.value && !row)) return
  if (!pending.value) {
    if (['succeeded','failed','cancelled'].includes(props.run.status)) return
    if (!window.confirm(`重新处理任务 ${row!.taskId} 的分发副本？\n仅按当前媒体配置处理本地副本，不重新生成、不改母版。完成后仍按本轮既定流程继续，人工制作仍需确认封存。`)) return
    pending.value = distributionRecoveryRequest(row!)
  }
  const request = pending.value!, token = sequence, runId = props.run.id
  let confirmed = false
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = readDistributionPhase(await platformApi.retryTaskDistribution(runId, request))
    if (result.taskId !== request.task_id || result.revision !== Number(request.expected_revision) + 1) throw new Error('未返回本次副本处理修订，请核对同一请求')
    if (token === sequence) { confirmed = true; pending.value = null; notice.value = `已核对副本处理请求：${distributionStates[result.state]}。未重新调用模型。` }
  } catch (e) {
    if (token === sequence) {
      error.value = apiErrorSummary(e)
      if (isAxiosError(e) && [400,403,404,409,422].includes(e.response?.status || 0)) {
        pending.value = null; detail.value = null
      }
    }
  }
  finally { busy.value = false }
  if (token === sequence && confirmed) await load()
}
watch(() => props.run.id, () => { sequence++; open.value = false; detail.value = null; pending.value = null; notice.value = ''; error.value = ''; loading.value = false })
onBeforeUnmount(() => { sequence++ })
</script>

<template>
  <section class="distribution-recovery-entry">
    <button class="button ghost small" @click="show">查看分发副本 / 恢复处理</button>
    <BaseModal :open="open" :dismissible="!busy" title="分发副本处理" eyebrow="原任务的本地处理阶段" width="850px" @close="close">
      <p>只处理本轮已成功图片、视频任务的分发副本，不重新调用模型，不改变母版或选片。本次请求不直接封存发布包；完成后配方仍按既定流程继续，人工制作仍需确认成片。下载和封存仍会重新核验实际文件。</p>
      <p v-if="loading" role="status">正在读取副本处理记录…</p>
      <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
      <p v-if="notice" role="status">{{ notice }}</p>
      <div v-if="pending" class="platform-alert"><p>上次请求尚未核对完成。读取列表不会撤销它，请先重试同一请求；不会额外安排另一轮处理。</p><button class="button ghost small" :disabled="busy || loading" @click="submit()">核对同一请求</button></div>
      <template v-if="detail">
        <p>本轮共 {{ detail.count }} 个已成功图片 / 视频任务 · 第 {{ page }} 页</p>
        <p v-if="!detail.rows.length">暂无需要展示的原任务。</p>
        <article v-for="row in detail.rows" :key="row.taskId" class="distribution-recovery-row">
          <strong>{{ row.kind === 'video' ? '视频' : '图片' }} · {{ distributionStates[row.state] }}</strong>
          <code>{{ row.taskId }}</code><small>处理修订 {{ row.revision }} · {{ row.finishedAt || row.startedAt || row.requestedAt || '尚未安排' }}</small>
          <p v-if="row.error" role="alert">{{ row.error }}</p>
          <button v-if="['not_requested','failed'].includes(row.state)" class="button ghost small" :disabled="busy || loading || Boolean(pending) || ['succeeded','failed','cancelled'].includes(run.status)" @click="submit(row)">仅重新处理副本</button>
        </article>
        <nav aria-label="副本处理分页"><button class="button ghost small" :disabled="busy || loading || !detail.hasPrevious" @click="load(page - 1)">上一页</button><button class="button ghost small" :disabled="busy || loading || !detail.hasNext" @click="load(page + 1)">下一页</button></nav>
      </template>
      <template #footer><button class="button ghost" :disabled="busy || loading" @click="load()">重新读取</button><button class="button ghost" :disabled="busy" @click="close">关闭</button></template>
    </BaseModal>
  </section>
</template>

<style scoped>
.distribution-recovery-entry{margin-block:12px}.distribution-recovery-row{display:grid;gap:8px;padding:14px 0;border-top:1px solid var(--border)}.distribution-recovery-row code{overflow-wrap:anywhere}.distribution-recovery-row p{margin:0}.distribution-recovery-row button{justify-self:start}nav{display:flex;gap:12px}
</style>
