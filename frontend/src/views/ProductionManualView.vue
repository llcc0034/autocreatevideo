<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  AlertTriangle, ArrowRight, BookOpenCheck, CheckCircle2, ChevronDown,
  CircleHelp, ExternalLink, ListChecks, Search, ServerCog,
  ShieldCheck, Sparkles, X,
} from 'lucide-vue-next'
import {
  productionManual, searchableIncidentText, searchableManualText,
  type ManualIncident, type ManualStep,
} from '@/content/productionManual'

const route = useRoute()
const router = useRouter()
const query = ref('')
const openSteps = ref(new Set<string>(productionManual.steps.map((step) => step.id)))

const normalizedQuery = computed(() => query.value.trim().toLocaleLowerCase('zh-CN'))
const visibleSteps = computed(() => normalizedQuery.value
  ? productionManual.steps.filter((step) => searchableManualText(step).includes(normalizedQuery.value))
  : productionManual.steps)
const visibleIncidents = computed(() => normalizedQuery.value
  ? productionManual.incidents.filter((incident) => searchableIncidentText(incident).includes(normalizedQuery.value))
  : productionManual.incidents)
const resultCount = computed(() => visibleSteps.value.length + visibleIncidents.value.length)

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
onMounted(() => { void syncHash() })
</script>

<template>
  <div class="manual-page page-stack">
    <section class="manual-hero panel">
      <div class="manual-hero-copy">
        <span class="eyebrow"><BookOpenCheck :size="13"/> 当前版本操作手册 / LIVE OPERATOR HANDBOOK</span>
        <h2>{{ productionManual.title }}</h2>
        <p>{{ productionManual.subtitle }}。按钮、输入示例、成功标志和异常恢复均以当前版本为准。</p>
        <div class="manual-version"><span>手册版本 {{ productionManual.version }}</span><span>更新日期 {{ productionManual.updatedAt }}</span><span>内容 Schema v{{ productionManual.schemaVersion }}</span></div>
      </div>
      <div class="manual-hero-actions">
        <button class="button primary" @click="scrollToId('manual-step-create-project')"><Sparkles :size="15"/>从第 1 步开始</button>
        <RouterLink class="button ghost" to="/manual/settings"><ServerCog :size="15"/>系统设置新手图文指南</RouterLink>
        <button class="button ghost" @click="scrollToId('manual-troubleshooting')"><CircleHelp :size="15"/>异常处理</button>
      </div>
    </section>

    <section class="manual-runtime-grid">
      <article class="panel">
        <Sparkles :size="18"/><span><small>AI 文本生产</small><strong>版本化模型路由</strong></span><i>仅由 ProductionRun 调度 · 审核与费用门禁可追踪</i>
      </article>
      <article class="panel">
        <ShieldCheck :size="18"/><span><small>提示词标准化</small><strong>项目级本地部署</strong></span><i>选择已烟测 prompt.normalize deployment · 必须人工确认</i>
      </article>
      <article class="panel">
        <ServerCog :size="18"/><span><small>媒体执行池</small><strong>本地 ComfyUI / 云异步 Provider</strong></span><i>实际可用性以主机探测、部署 smoke 与冻结路由为准</i>
      </article>
      <article class="panel">
        <ListChecks :size="18"/><span><small>运行参数来源</small><strong>{{ productionManual.defaults.videoResolution }}</strong></span><i>最终值以运行快照为准</i>
      </article>
    </section>

    <section class="manual-search panel">
      <Search :size="17"/><input v-model="query" aria-label="搜索操作手册" placeholder="搜索步骤、状态或错误，例如：needs_review、上传契约、uncertain、逐集母版…"><button v-if="query" title="清空搜索" @click="query='' "><X :size="15"/></button><span>{{ resultCount }} 条结果</span>
    </section>

    <div class="manual-layout">
      <aside class="manual-toc panel">
        <header><span class="eyebrow">完整生产路径 / PRODUCTION PATH</span><strong>完整制作路径</strong></header>
        <nav>
          <button v-for="step in productionManual.steps" :key="step.id" :class="{ muted: normalizedQuery && !visibleSteps.includes(step) }" @click="scrollToId(stepAnchor(step))"><b>{{ String(step.number).padStart(2,'0') }}</b><span>{{ step.title }}</span></button>
        </nav>
        <button class="manual-toc-errors" @click="scrollToId('manual-troubleshooting')"><AlertTriangle :size="13"/>异常处理矩阵</button>
      </aside>

      <main class="manual-content">
        <section class="manual-flow-intro">
          <span class="eyebrow">从创建到交付 / START TO DELIVERY</span><h3>{{ productionManual.steps.length }} 步完成逐集交付</h3><p>首次按已发布工作流（workflow）声明的最小可用规格验证真实链路，再按已激活配置与运行快照生产。每一步看到成功标志后再进入下一步。</p>
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

        <section v-if="!visibleSteps.length" class="manual-empty panel"><Search :size="24"/><strong>没有匹配的制作步骤</strong><p>可尝试“翻译”“首尾帧”“ProductionRun”或错误提示中的关键词。</p></section>

        <section id="manual-troubleshooting" class="manual-troubleshooting">
          <header><span class="eyebrow">异常处理手册 / INCIDENT PLAYBOOK</span><h3>异常处理矩阵</h3><p>先确认症状和原因，再执行恢复动作。不要在状态不明时重复创建生产任务。</p></header>
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
