<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { workspaceSection } from '@/services/productionWorkspacePresentation'
import { productionModeLabel } from '@/services/productionRunPresentation'
import { productionDate, productionVersionName } from '@/services/projectVersionContents'
import type { ProductionRecipe, ProductionRun } from '@/services/platformTypes'
import PlatformStatusPill from './PlatformStatusPill.vue'
import ProductionVersionContents from './ProductionVersionContents.vue'
import ProductionWorkspaceHeader from './ProductionWorkspaceHeader.vue'
const props = defineProps<{ projectId: string; title: string; runs: ProductionRun[]; loading: boolean; error: string; openWorkspace?: boolean; gradient?: string; coverUrl?: string }>()
const emit = defineEmits<{ retry: [] }>()
const route = useRoute(), router = useRouter()
const requested = computed(() => !props.openWorkspace && typeof route.query.run === 'string' ? route.query.run : '')
const detail = ref<ProductionRun | null>(null), recipe = ref<ProductionRecipe | null>(null)
const busy = ref(false), detailError = ref(''), recipeError = ref(''), search = ref(''), page = ref(1)
const matching = computed(() => props.runs.filter(run => `${productionVersionName(run)} ${productionModeLabel(run)} ${productionDate(run.createdAt)} ${run.id}`.includes(search.value.trim())))
const visible = computed(() => matching.value.slice((page.value-1)*12,page.value*12))
let serial = 0
function open(id: string) {
  if (props.openWorkspace && id) { return router.push({path:`/projects/${props.projectId}/production/${id}`,query:typeof route.query.step==='string'?{step:route.query.step}:{}}) }
  return router.push({ path: `/projects/${props.projectId}`, query: id ? { run: id } : {} }) }
async function load() {
  const token = ++serial, id = requested.value, projectId = props.projectId
  detail.value = null; recipe.value = null; detailError.value = ''; recipeError.value = ''; busy.value = false
  if (!id || props.loading || props.error) return
  if (!props.runs.some(row => row.id === id && row.project === projectId)) { detailError.value = '指定制作版本不属于本项目或已不可访问，请返回列表选择；未自动替换其他版本。'; return }
  busy.value = true
  try {
    const row = await platformApi.getProductionRun(id)
    if (token !== serial) return
    if (row.project !== projectId || row.id !== id) throw new Error('制作版本与当前项目不一致。')
    detail.value = row
    if (row.recipeVersion) {
      try { const value = await platformApi.getRecipe(row.recipeVersion); if (token === serial) {
        if (value.id !== row.recipeVersion) throw new Error('配方引用不一致。')
        recipe.value = value
      } } catch (cause) { if (token === serial) recipeError.value = apiErrorSummary(cause, '配方名称读取失败') }
    }
  } catch (cause) { if (token === serial) detailError.value = apiErrorSummary(cause) }
  finally { if (token === serial) busy.value = false }
}
watch([requested, () => props.projectId, () => props.loading, () => props.error, () => props.runs], load, {immediate:true})
watch(search, () => { page.value=1 })
watch(() => matching.value.length, count => { page.value = Math.min(page.value, Math.max(1, Math.ceil(count / 12))) })
onBeforeUnmount(() => { serial++ })
</script>
<template>
  <section class="project-version-workspace" :aria-label="`${title} · 制作版本`">
    <ProductionWorkspaceHeader v-if="requested" :title="title" mode="readonly" :run="detail" :gradient="gradient" :cover-url="coverUrl" back-label="返回制作版本列表" @back="open('')">
      <template #context><p v-if="recipe">引用配方：{{recipe.name}} · v{{recipe.version}}</p><p v-if="recipeError" role="alert">{{recipeError}}，版本内容仍可独立查看。</p></template>
      <RouterLink v-if="detail" class="button ghost small" :to="{path:`/projects/${projectId}/production/${detail.id}`,query:{step:workspaceSection(route.query.section)}}">进入生产工作台</RouterLink><button class="button ghost small" :disabled="loading||busy" @click="emit('retry')">重新读取</button>
    </ProductionWorkspaceHeader>
    <header v-else class="version-page-heading">
      <div><span class="eyebrow">制作档案 / PRODUCTION VERSIONS</span><h3>制作版本</h3><p>每次制作独立保存，选择后查看剧本、设定、分镜与成片。</p></div>
      <div class="version-actions"><button class="button ghost small" :disabled="loading||busy" @click="emit('retry')">重新读取</button></div>
    </header>
    <p v-if="loading" role="status">正在读取本项目制作版本…</p>
    <p v-else-if="error" class="platform-alert danger" role="alert">{{error}} <button class="button ghost small" @click="emit('retry')">重试读取列表</button></p>
    <template v-else-if="!requested">
      <div class="version-list-heading"><span class="source-count">{{runs.length}} 次制作</span><label class="field"><span>查找制作版本</span><input v-model="search" placeholder="名称标识、日期或制作方式"/></label></div>
      <p v-if="!runs.length">本项目尚无制作版本。新建制作可选择配方自动生产或人工逐步制作。</p>
      <p v-else-if="!matching.length">没有符合条件的制作版本。</p>
      <div class="version-list"><article v-for="run in visible" :key="run.id" class="panel"><header><button type="button" class="version-name" @click="open(run.id)">{{productionVersionName(run)}}</button><PlatformStatusPill :status="run.status"/></header><dl><div><dt>制作方式</dt><dd>{{productionModeLabel(run)}}</dd></div><div><dt>创建时间（北京时间）</dt><dd>{{productionDate(run.createdAt)}}</dd></div><div><dt>结束时间</dt><dd>{{run.finishedAt ? productionDate(run.finishedAt) : '尚未结束'}}</dd></div><div><dt>成果</dt><dd>{{run.releaseBundleId?'已生成发行候选':'尚无封存发行候选'}}</dd></div></dl><button class="button ghost small" @click="open(run.id)">{{openWorkspace ? '进入此版本生产工作台' : '查看本版本内容'}}</button></article></div>
      <nav v-if="matching.length>12" class="version-pagination" aria-label="制作版本分页"><button class="button ghost small" :disabled="page<=1" @click="page--">上一页版本</button><span>第 {{page}} 页 / 共 {{matching.length}} 个版本</span><button class="button ghost small" :disabled="page*12>=matching.length" @click="page++">下一页版本</button></nav>
      <p class="version-note">每次制作是独立记录，不等同于配方 v1 / v2，也不自动继承上一版结果。当前未设置自定义名称，以固定标识区分。</p>
    </template>
    <template v-else>
      <p v-if="busy" role="status">正在读取选定版本…</p><p v-if="detailError" role="alert">{{detailError}} <button class="button ghost small" :disabled="busy" @click="load">重试读取本版本</button></p>
      <template v-if="detail"><p v-if="detail.blockedReason" class="platform-alert warning" role="alert">{{detail.blockedReason}}</p><ProductionVersionContents :key="detail.id+detail.updatedAt" :run="detail"/></template>
    </template>
  </section>
</template>
<style scoped>
.project-version-workspace{min-width:0;display:grid;gap:24px;font-size:14px;line-height:1.6}.version-page-heading,.version-actions,.version-list-heading{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}.project-version-workspace :is(h2,h3,p){margin:0}.project-version-workspace h2{font-size:24px;margin:8px 0}.project-version-workspace h3{font-size:18px}.version-actions{justify-content:flex-start}.version-page-heading p,.version-note,.version-list dt{color:var(--text-muted)}.version-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,380px),1fr));gap:16px}.version-list article{display:grid;gap:16px;padding:22px;min-width:0}.version-list article header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}.version-name{background:none;color:var(--text);font-size:17px;font-weight:600;cursor:pointer;padding:0;min-height:36px}.version-name:hover{color:var(--accent)}.version-list dl{margin:0;display:grid;grid-template-columns:1fr 1fr;gap:14px}.version-list dd{margin:4px 0 0;overflow-wrap:anywhere}.version-list .button{justify-self:start}.version-pagination{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.project-version-workspace :is(button,a,input):focus-visible{outline:2px solid var(--accent);outline-offset:2px}.project-version-workspace .button{white-space:normal;min-height:38px}.project-version-workspace input{min-height:38px;max-width:100%}@media(max-width:600px){.version-actions{width:100%}.version-actions>*{flex:1 1 auto}.version-list article{padding:14px}.version-list dl{grid-template-columns:1fr}.version-page-heading h2{font-size:21px}.version-list-heading{align-items:stretch}.version-list-heading .field{width:100%}}
.version-page-heading h3{margin:5px 0 8px}.version-list article{transition:border-color .18s}.version-list article:hover{border-color:var(--accent)}.version-list-heading .source-count{color:var(--text-secondary)}.version-list dl{padding:14px 0;border-block:1px solid var(--border)}.version-list article .button{width:100%}
</style>
