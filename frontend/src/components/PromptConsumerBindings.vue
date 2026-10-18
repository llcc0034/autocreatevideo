<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { getRelationPage, type RelationPage } from '@/services/registryRelations'
import { readPromptConsumerBindings } from '@/services/promptConsumerBindings'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'

const props = defineProps<{ profileId: string }>()
const emit = defineEmits<{ inspect: [id: string, event: MouseEvent] }>()
const expanded = ref(false), loading = ref(false), bindingLoading = ref(false)
const page = ref(1), error = ref(''), bindingError = ref(''), selectedId = ref('')
const data = ref<RelationPage | null>(null)
const detail = ref<Awaited<ReturnType<typeof readPromptConsumerBindings>> | null>(null)
let pageSerial = 0, bindingSerial = 0
let controller: AbortController | undefined
function clearBinding() { bindingSerial++; selectedId.value = ''; detail.value = null; bindingError.value = ''; bindingLoading.value = false }
function reset() { pageSerial++; controller?.abort(); data.value = null; error.value = ''; loading.value = false; page.value = 1; clearBinding() }
async function load(targetPage = page.value) {
  const serial = ++pageSerial; controller?.abort(); controller = new AbortController()
  clearBinding(); data.value = null; error.value = ''; loading.value = true; page.value = targetPage
  try {
    const result = await getRelationPage({ kind: 'execution', source_kind: 'prompt', source_id: props.profileId, relation: 'executions', page: targetPage, page_size: 25 }, controller.signal)
    if (result.results.some(row => row.kind !== 'execution')) throw new Error('使用方目录返回了其他类型，不能用于绑定追踪')
    if (serial === pageSerial) data.value = result
  } catch (cause) { if (serial === pageSerial) error.value = apiErrorSummary(cause, '使用方目录读取失败') }
  finally { if (serial === pageSerial) loading.value = false }
}
async function select(id: string) {
  if (!data.value?.results.some(row => row.id === id)) return
  clearBinding(); selectedId.value = id; bindingLoading.value = true
  const serial = bindingSerial
  try {
    const result = await readPromptConsumerBindings(props.profileId, id)
    if (serial === bindingSerial) detail.value = result
  } catch (cause) { if (serial === bindingSerial) bindingError.value = apiErrorSummary(cause, '使用方绑定读取失败') }
  finally { if (serial === bindingSerial) bindingLoading.value = false }
}
function toggle(event: Event) {
  expanded.value = (event.target as HTMLDetailsElement).open
  if (expanded.value) void load(1)
  else reset()
}
watch(() => props.profileId, () => { reset(); if (expanded.value) void load(1) })
onBeforeUnmount(reset)
</script>

<template>
  <details class="prompt-consumer-bindings" @toggle="toggle">
    <summary>使用此策略的主实例与实际辅助绑定</summary>
    <p>先选择一个确切使用方，再查看该版本保存的辅助用途与目标。不是可选模型目录，也不是本次任务的调用记录；版本状态不等于运行健康。</p>
    <p v-if="loading" role="status">正在读取使用方 · 第 {{ page }} 页…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <button v-if="!loading" type="button" class="button ghost" @click="load()">重新读取使用方</button>
    <template v-if="data">
      <p>共 {{ data.count }} 个已保存引用 · 第 {{ page }} 页 · 包含草稿和退役版本</p>
      <p v-if="!data.results.length">本页没有引用此确切策略版本的使用方。</p>
      <div class="consumer-list"><button v-for="row in data.results" :key="row.id" type="button" class="button ghost consumer-choice" :aria-pressed="selectedId === row.id" @click="select(row.id)"><span>{{ row.label }}</span><PlatformStatusPill :status="row.status" /></button></div>
      <nav v-if="data.previous || data.next" aria-label="策略使用方分页"><button type="button" class="button ghost" :disabled="!data.previous" @click="load(page-1)">上一页</button><button type="button" class="button ghost" :disabled="!data.next" @click="load(page+1)">下一页</button></nav>
      <p v-if="!selectedId">尚未选择使用方；不会自动采用首项。</p>
    </template>
    <p v-if="bindingLoading" role="status">正在读取所选版本的辅助绑定…</p>
    <p v-if="bindingError" role="alert">{{ bindingError }} <button type="button" class="button ghost" @click="select(selectedId)">重读所选绑定</button></p>
    <section v-if="detail" aria-label="已选使用方的辅助绑定">
      <header><strong>{{ detail.owner.instance_name }} · v{{ detail.owner.version }}</strong><button type="button" class="button ghost" @click="emit('inspect',detail.owner.id,$event)">查看主实例配置</button></header>
      <p v-if="!detail.bindings.length">所选版本未配置辅助实例；这不表示策略无需辅助能力。</p>
      <article v-for="binding in detail.bindings" :key="binding.purpose">
        <strong>{{ binding.purpose }}</strong><code>{{ binding.target_version_id }}</code>
        <p v-if="binding.error" role="alert">{{ binding.error }} · 保留绑定标识，不用其他实例替代。</p>
        <template v-else-if="binding.target"><span>{{ binding.target.instance_name }} · v{{ binding.target.version }}</span><PlatformStatusPill :status="binding.target.status" /><button type="button" class="button ghost" @click="emit('inspect',binding.target_version_id,$event)">查看辅助实例配置</button></template>
      </article>
    </section>
  </details>
</template>

<style scoped>
.prompt-consumer-bindings{min-width:0;border:1px solid var(--border);border-radius:var(--radius-sm,6px);padding:16px;font-size:var(--config-body-size,14px);line-height:1.6}summary{font-weight:600;min-height:36px;cursor:pointer}p{color:var(--text-secondary)}p,code,span,strong{overflow-wrap:anywhere}code{font-size:var(--config-help-size,12px)}
.consumer-list,section{display:grid;gap:12px;min-width:0}.consumer-choice,header,nav{display:flex;flex-wrap:wrap;align-items:center;gap:12px;justify-content:space-between}.consumer-choice{white-space:normal;text-align:left;min-width:0}.consumer-choice[aria-pressed=true]{border-color:var(--accent)}nav{justify-content:flex-start;margin:12px 0}section{margin-top:16px}article{display:grid;justify-items:start;gap:8px;border-top:1px solid var(--border);padding-top:12px;min-width:0}button{max-width:100%;white-space:normal}[role=alert]{color:var(--warning)}
</style>
