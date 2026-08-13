<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  AlertTriangle, ArrowRight, BookOpenCheck, CheckCircle2, ChevronDown,
  CircleHelp, ExternalLink, ListChecks, RefreshCw, Search, ServerCog,
  ShieldCheck, Sparkles, X,
} from 'lucide-vue-next'
import { api } from '@/services/api'
import {
  productionManual, searchableIncidentText, searchableManualText,
  type ManualIncident, type ManualStep,
} from '@/content/productionManual'

type CapabilityState = {
  configured?: boolean
  enabled?: boolean
  model?: string
  device?: string
  gpu_layers?: number
  cuda_requested?: boolean
  gpu_ready?: boolean
  gpu_context_smoke_passed?: boolean
  runtime_probe_error_code?: string
  min_shots?: number
  max_shots?: number
  baseline_max_new_tokens?: number
  max_new_tokens?: number
  configured_max_new_tokens?: number
  loaded_on_demand?: boolean
  unloaded_after_request?: boolean
}

const route = useRoute()
const router = useRouter()
const query = ref('')
const capabilityLoading = ref(false)
const capabilityError = ref('')
const creativeCapability = ref<CapabilityState | null>(null)
const translationCapability = ref<CapabilityState | null>(null)
const openSteps = ref(new Set<string>(productionManual.steps.map((step) => step.id)))

const normalizedQuery = computed(() => query.value.trim().toLocaleLowerCase('zh-CN'))
const visibleSteps = computed(() => normalizedQuery.value
  ? productionManual.steps.filter((step) => searchableManualText(step).includes(normalizedQuery.value))
  : productionManual.steps)
const visibleIncidents = computed(() => normalizedQuery.value
  ? productionManual.incidents.filter((incident) => searchableIncidentText(incident).includes(normalizedQuery.value))
  : productionManual.incidents)
const resultCount = computed(() => visibleSteps.value.length + visibleIncidents.value.length)

const isReady = (capability: CapabilityState | null) => Boolean(capability?.configured && capability?.enabled !== false)
const creativeShotRange = computed(() => creativeCapability.value?.min_shots && creativeCapability.value?.max_shots
  ? `${creativeCapability.value.min_shots}–${creativeCapability.value.max_shots} 镜`
  : '以当前能力接口为准')
const creativeBudgetLabel = computed(() => {
  const baseline = Number(creativeCapability.value?.baseline_max_new_tokens || 0)
  const limit = Number(
    creativeCapability.value?.configured_max_new_tokens || creativeCapability.value?.max_new_tokens || 0,
  )
  if (!baseline || !limit) return '容量按镜数与对白动态计算'
  return `${creativeCapability.value?.max_shots || 4} 镜无对白约 ${baseline} · 上限 ${limit}`
})
const modelLabel = computed(() => creativeCapability.value?.model || translationCapability.value?.model || '读取中')
const deviceLabel = computed(() => String(creativeCapability.value?.device || translationCapability.value?.device || '—').toUpperCase())
const deviceRuntimeLabel = computed(() => {
  const capability = creativeCapability.value?.cuda_requested
    ? creativeCapability.value
    : translationCapability.value
  if (!capability?.cuda_requested) return 'CPU · 按需加载'
  if (capability.gpu_ready) {
    const layers = Number(capability.gpu_layers)
    return `CUDA 已验证 · ${layers === -1 ? '全部层卸载' : `${layers} 层卸载`}`
  }
  return `CUDA 未就绪 · ${capability.runtime_probe_error_code || '等待运行时烟测'}`
})

const fetchCapabilities = async () => {
  capabilityLoading.value = true
  capabilityError.value = ''
  const [creative, translation] = await Promise.allSettled([
    api.get(productionManual.capabilityEndpoints.creative),
    api.get(productionManual.capabilityEndpoints.translation),
  ])
  creativeCapability.value = creative.status === 'fulfilled' ? creative.value.data?.data || creative.value.data : null
  translationCapability.value = translation.status === 'fulfilled' ? translation.value.data?.data || translation.value.data : null
  if (creative.status === 'rejected' || translation.status === 'rejected') {
    capabilityError.value = '部分运行能力暂时无法读取；操作步骤仍可使用，提交前请到系统设置刷新状态。'
  }
  capabilityLoading.value = false
}

const toggleStep = (id: string) => {
  const next = new Set(openSteps.value)
  next.has(id) ? next.delete(id) : next.add(id)
  openSteps.value = next
}

const scrollToId = async (id: string) => {
  await router.replace({ hash: `#${id}` })
  await nextTick()
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

const syncHash = async () => {
  const id = String(route.hash || '').replace(/^#/, '')
  if (!id) return
  if (id.startsWith('manual-step-')) {
    const next = new Set(openSteps.value); next.add(id.replace('manual-step-', '')); openSteps.value = next
  }
  await nextTick()
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

const go = (path?: string) => path && router.push(path)
const incidentAnchor = (incident: ManualIncident) => `manual-incident-${incident.id}`
const stepAnchor = (step: ManualStep) => `manual-step-${step.id}`

watch(() => route.hash, () => void syncHash())
watch(normalizedQuery, (value) => {
  if (value) openSteps.value = new Set(visibleSteps.value.map((step) => step.id))
})
onMounted(() => { void fetchCapabilities(); void syncHash() })
</script>

<template>
  <div class="manual-page page-stack">
    <section class="manual-hero panel">
      <div class="manual-hero-copy">
        <span class="eyebrow"><BookOpenCheck :size="13"/> LIVE OPERATOR HANDBOOK</span>
        <h2>{{ productionManual.title }}</h2>
        <p>{{ productionManual.subtitle }}。按钮、输入示例、成功标志和异常恢复均以当前版本为准。</p>
        <div class="manual-version"><span>手册版本 {{ productionManual.version }}</span><span>更新日期 {{ productionManual.updatedAt }}</span><span>内容 Schema v{{ productionManual.schemaVersion }}</span></div>
      </div>
      <div class="manual-hero-actions">
        <button class="button primary" @click="scrollToId('manual-step-create-project')"><Sparkles :size="15"/>从第 1 步开始</button>
        <button class="button ghost" @click="scrollToId('manual-troubleshooting')"><CircleHelp :size="15"/>异常处理</button>
      </div>
    </section>

    <section class="manual-runtime-grid">
      <article class="panel" :class="{ ready: isReady(creativeCapability) }">
        <Sparkles :size="18"/><span><small>本地创作能力</small><strong>{{ capabilityLoading ? '读取中…' : isReady(creativeCapability) ? '已就绪' : '未就绪' }}</strong></span><i>{{ creativeShotRange }} · {{ creativeBudgetLabel }}</i>
      </article>
      <article class="panel" :class="{ ready: isReady(translationCapability) }">
        <ShieldCheck :size="18"/><span><small>本地翻译审核</small><strong>{{ capabilityLoading ? '读取中…' : isReady(translationCapability) ? '已就绪' : '未就绪' }}</strong></span><i>必须人工确认</i>
      </article>
      <article class="panel">
        <ServerCog :size="18"/><span><small>当前模型 / 设备</small><strong>{{ modelLabel }}</strong></span><i>{{ deviceLabel }} · {{ deviceRuntimeLabel }}</i>
      </article>
      <article class="panel">
        <ListChecks :size="18"/><span><small>默认生产参数</small><strong>{{ productionManual.defaults.videoResolution }}</strong></span><i>倍数 {{ productionManual.defaults.resolutionMultiple }} · {{ productionManual.defaults.videoFps }} FPS</i>
      </article>
    </section>
    <div v-if="capabilityError" class="manual-capability-warning"><AlertTriangle :size="14"/>{{ capabilityError }}<button @click="fetchCapabilities"><RefreshCw :size="12"/>重试</button></div>

    <section class="manual-search panel">
      <Search :size="17"/><input v-model="query" aria-label="搜索操作手册" placeholder="搜索按钮、参数、状态或错误，例如：源文已变更、Ref2VA、10%、组装…"><button v-if="query" title="清空搜索" @click="query='' "><X :size="15"/></button><span>{{ resultCount }} 条结果</span>
    </section>

    <div class="manual-layout">
      <aside class="manual-toc panel">
        <header><span class="eyebrow">PRODUCTION PATH</span><strong>完整制作路径</strong></header>
        <nav>
          <button v-for="step in productionManual.steps" :key="step.id" :class="{ muted: normalizedQuery && !visibleSteps.includes(step) }" @click="scrollToId(stepAnchor(step))"><b>{{ String(step.number).padStart(2,'0') }}</b><span>{{ step.title }}</span></button>
        </nav>
        <button class="manual-toc-errors" @click="scrollToId('manual-troubleshooting')"><AlertTriangle :size="13"/>异常处理矩阵</button>
      </aside>

      <main class="manual-content">
        <section class="manual-flow-intro">
          <span class="eyebrow">START TO DELIVERY</span><h3>12 步完成一集视频</h3><p>建议首次先用 1 个镜头、0.1 MP 验证链路，再切换 0.2/0.3 MP 生产。每一步看到成功标志后再进入下一步。</p>
        </section>

        <article v-for="step in visibleSteps" :id="stepAnchor(step)" :key="step.id" class="manual-step panel">
          <button class="manual-step-header" :aria-expanded="openSteps.has(step.id)" @click="toggleStep(step.id)">
            <span class="manual-step-number">{{ String(step.number).padStart(2,'0') }}</span>
            <span><small>{{ step.location }}</small><strong>{{ step.title }}</strong><p>{{ step.summary }}</p></span>
            <ChevronDown :class="{ rotated: openSteps.has(step.id) }" :size="18"/>
          </button>
          <div v-if="openSteps.has(step.id)" class="manual-step-body">
            <section>
              <h4>需要输入什么</h4>
              <div class="manual-input-grid">
                <article v-for="input in step.inputs" :key="input.label"><span>{{ input.label }}</span><code>{{ input.example }}</code><p>{{ input.note }}</p></article>
              </div>
            </section>
            <section class="manual-action-grid">
              <div><h4>按什么按钮</h4><ol><li v-for="action in step.actions" :key="action">{{ action }}</li></ol></div>
              <div class="manual-success"><h4><CheckCircle2 :size="14"/>成功标志</h4><ul><li v-for="item in step.success" :key="item">{{ item }}</li></ul></div>
            </section>
            <div v-if="step.cautions?.length" class="manual-cautions"><AlertTriangle :size="15"/><span><strong>注意</strong><em v-for="item in step.cautions" :key="item">{{ item }}</em></span></div>
            <footer><span><small>下一步</small><strong>{{ step.next }}</strong></span><button v-if="step.route" class="button ghost small" @click="go(step.route)">前往该页面 <ExternalLink :size="13"/></button><button v-else-if="step.number<productionManual.steps.length" class="text-link" @click="scrollToId(stepAnchor(productionManual.steps[step.number]))">继续第 {{ step.number+1 }} 步 <ArrowRight :size="13"/></button></footer>
          </div>
        </article>

        <section v-if="!visibleSteps.length" class="manual-empty panel"><Search :size="24"/><strong>没有匹配的制作步骤</strong><p>可尝试“翻译”“首尾帧”“生成整集”或错误提示中的关键词。</p></section>

        <section id="manual-troubleshooting" class="manual-troubleshooting">
          <header><span class="eyebrow">INCIDENT PLAYBOOK</span><h3>异常处理矩阵</h3><p>先确认症状和原因，再执行恢复动作。不要在状态不明时重复创建生产任务。</p></header>
          <article v-for="incident in visibleIncidents" :id="incidentAnchor(incident)" :key="incident.id" class="manual-incident panel">
            <header><AlertTriangle :size="17"/><strong>{{ incident.symptom }}</strong></header>
            <div><section><span>常见原因</span><p>{{ incident.likelyCause }}</p></section><section><span>处理步骤</span><ol><li v-for="action in incident.actions" :key="action">{{ action }}</li></ol></section><section class="incident-confirm"><span>恢复确认</span><p><CheckCircle2 :size="13"/>{{ incident.confirmation }}</p></section></div>
          </article>
          <section v-if="!visibleIncidents.length" class="manual-empty panel"><CircleHelp :size="24"/><strong>没有匹配的异常</strong><p>清空搜索可查看完整异常矩阵。</p></section>
        </section>
      </main>
    </div>
  </div>
</template>
