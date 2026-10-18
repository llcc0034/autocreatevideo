<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowUpRight, ShieldCheck } from 'lucide-vue-next'
import PlatformStatusPill from './PlatformStatusPill.vue'
import RegistryResourceExplorer from './RegistryResourceExplorer.vue'
import { registryHierarchy, registryRoles, readRegistryTrail, descendRegistry, type RegistryTrailEntry } from '@/services/registryHierarchy'
import { getRelationPage, relationConfigUrl, relationKinds, relationOverviewUrl, type RelationKind, type RelationPage } from '@/services/registryRelations'
import { apiErrorSummary } from '@/services/apiErrorPresentation'

const route = useRoute(); const router = useRouter()
const data = ref<RelationPage | null>(null); const loading = ref(false); const error = ref(''); const search = ref('')
const kind = computed(() => String(route.query.kind || 'model'))
const page = computed(() => Number(route.query.page || 1))
const query = computed<Record<string,string|number>>(() => ({ kind: kind.value, page: page.value, page_size: 25,
  ...Object.fromEntries(['ids', 'source_kind', 'source_id', 'relation', 'search'].filter(key => route.query[key]).map(key => [key, String(route.query[key])])) }))
const sourceUrl = computed(() => route.query.source_kind && route.query.source_id && Object.prototype.hasOwnProperty.call(relationKinds, String(route.query.source_kind))
  ? relationOverviewUrl(String(route.query.source_kind) as RelationKind, String(route.query.source_id)) : '')
const trail = ref<RegistryTrailEntry[]>([])
const exactObject = computed(() => typeof route.query.ids==='string' && !route.query.ids.includes(','))
const currentLabel = computed(() => exactObject.value ? data.value?.results[0]?.label || '精确对象' : data.value?.context_label || relationKinds[kind.value as RelationKind] || '配置目录')
function descend(url: string) {
  const next = descendRegistry(trail.value,{label:currentLabel.value,url:route.fullPath},url)
  return router.push({ ...router.resolve(next.url), state:{registryTrail:next.trail.map(entry=>({...entry}))} })
}
function ancestor(index: number) {
  const entry=trail.value[index]
  if (entry) void router.push({ ...router.resolve(entry.url), state:{registryTrail:trail.value.slice(0,index).map(item=>({...item}))} })
}
let sequence = 0; let controller: AbortController | undefined
const load = async () => {
  const serial = ++sequence; controller?.abort(); controller = new AbortController()
  const params = query.value; data.value = null; error.value = ''; loading.value = true
  try {
    const result = await getRelationPage(params, controller.signal)
    const ids = typeof params.ids==='string' ? params.ids.split(',') : null
    if (result.results.some(row=>row.kind!==params.kind || (ids && !ids.includes(row.id)))) throw new Error('关联目录返回了其他对象，请重新读取')
    if (sequence === serial) data.value = result
  } catch (cause) { if (sequence === serial) error.value = apiErrorSummary(cause, '关联目录读取失败') }
  finally { if (sequence === serial) loading.value = false }
}
const trailState = () => ({registryTrail:trail.value.map(entry=>({...entry}))})
const changePage = (page: number) => router.push({ path: route.path, query: { ...route.query, page: String(page) }, state:trailState() })
const submit = () => router.push({ path: route.path, query: { ...route.query, search: search.value || undefined, page: '1' }, state:trailState() })
watch(() => route.fullPath, () => {
  trail.value=readRegistryTrail(router.options.history.state.registryTrail)
  search.value = String(route.query.search || ''); void load()
}, { immediate: true })
onBeforeUnmount(() => { sequence++; controller?.abort() })
</script>

<template>
  <RegistryResourceExplorer :key="route.fullPath" label="关联目录" v-slot="{ inspect }">
  <section class="relation-overview" aria-label="能力与关联总览">
    <div class="platform-alert info"><p>查看已配置的关联，点击关联项继续查看或配置。任务实际使用的配置，请查看对应任务的执行记录。</p></div>
    <nav v-if="trail.length" class="relation-breadcrumbs" aria-label="公共资源下钻路径"><template v-for="(entry,index) in trail" :key="`${index}-${entry.url}`"><button class="button ghost tiny" type="button" @click="ancestor(index)">{{ entry.label }}</button><span aria-hidden="true">›</span></template><span aria-current="page">{{ currentLabel }}</span></nav>
    <details class="relation-help"><summary>这些关联有什么区别？</summary><ul>
      <li>执行实例版本：固定模型部署、工作流（适用时）和提示词方式；策略模式固定一个策略版本，不按模型族推测。</li>
      <li>辅助执行实例：只按配置用途生成或翻译字段，不替代主执行实例；可以反向查看哪些实例引用了它。</li>
      <li>引用配方：配置中使用了此对象的生产配方，不是任务执行次数。</li>
      <li>字段与结构：按确切定义版本和结构节点读取引用；来源、输出和策略继续关联实例与配方。包含草稿和停用版本，不按同名或最新版本替换；不是全部历史请求的影响扫描。</li>
      <li>校验工作流：以此模型部署为校验目标的工作流，不等于执行实例的组合烟测通过。实际配置看执行实例，验收看对应烟测记录。</li>
      <li>引用 Projector：工作流合同保存的方案版本，历史版本不会替换为新版本。</li>
      <li>业务输出版本：精确输出协议与引用执行实例的双向关系。后继版本不会替换旧引用；可沿实例继续查配方，不等于全部运行期消费者或审核记录。</li>
    </ul></details>
    <details :open="!trail.length&&!exactObject&&!sourceUrl"><summary>公共资源目录 · {{ relationKinds[kind as RelationKind] || '未知类型' }}</summary><nav class="kind-tabs" aria-label="关联对象类型"><RouterLink v-for="(label,key) in relationKinds" :key="key" :to="relationOverviewUrl(key)" :aria-current="kind===key?'page':undefined">{{ label }}</RouterLink></nav></details>
    <form class="relation-search" @submit.prevent="submit"><label>搜索当前对象<input v-model="search" type="search" placeholder="名称、标识或模型族"/></label><button class="button ghost small">搜索</button><button class="button ghost small" type="button" :disabled="loading" @click="load">重新读取</button></form>
    <div v-if="sourceUrl||route.query.ids" class="relation-context"><span>{{ data?.context_label || '精确关联筛选' }}</span><RouterLink v-if="sourceUrl" :to="sourceUrl">返回来源对象</RouterLink><RouterLink :to="relationOverviewUrl((Object.prototype.hasOwnProperty.call(relationKinds,kind)?kind:'model') as RelationKind)">清除关联筛选</RouterLink></div>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <p v-else-if="loading" class="platform-loading">正在读取当前页的配置关系…</p>
    <template v-else-if="data">
      <p class="muted">共 {{ data.count }} 个对象 · 读取于 {{ new Date(data.generated_at).toLocaleString('zh-CN') }} · 包含停用 / 历史记录</p>
      <article v-for="row in data.results" :key="row.id" class="relation-card">
        <header>
          <div class="relation-identity"><strong>{{ row.label }}</strong><p class="muted">声明能力 / 协议 / 引擎：{{ row.capabilities.join('、') || '详见对象合同' }}</p></div>
          <div class="relation-actions">
            <PlatformStatusPill :status="row.status" :bilingual="false"/>
            <button class="button ghost small" type="button" :aria-label="`${row.label} · 查看定义与使用方`" @click="inspect(row.kind, row.id, $event)">查看定义与使用方</button>
            <a class="button ghost small" :href="relationConfigUrl(row)" target="_blank" rel="noopener" :aria-label="`${row.label} · 前往维护（新窗口）`"><ArrowUpRight :size="14" aria-hidden="true"/>前往维护（新窗口）</a>
            <RouterLink v-if="row.acceptance_url" class="button ghost small" :to="row.acceptance_url" :aria-label="`${row.label} · 查看验收与下一步`"><ShieldCheck :size="14" aria-hidden="true"/>查看验收</RouterLink>
          </div>
        </header>
        <template v-if="exactObject">
          <p class="relation-purpose">{{ registryRoles[row.kind] }} 查看定义与使用方可原位下钻，返回保留本目录的筛选与分页；修改在唯一维护页进行。</p>
          <ul v-if="row.notices.length" class="relation-notices"><li v-for="notice in row.notices" :key="notice">{{ notice }}</li></ul>
          <section v-for="layer in registryHierarchy(row)" :key="layer.key" class="relation-layer"><h3>{{ layer.label }}</h3><p class="muted">{{ layer.description }}</p><div class="relation-group-buttons"><button v-for="group in layer.groups" :key="group.key" type="button" class="button ghost small" @click="descend(group.url)">{{ group.label }} · {{ group.count }} 项 <span aria-hidden="true">›</span></button></div></section>
        </template>
        <template v-else><ul v-if="row.notices.length" class="relation-notices"><li v-for="notice in row.notices" :key="notice">{{ notice }}</li></ul><button type="button" class="button ghost small" @click="descend(row.overview_url)">查看层级与依赖 <span aria-hidden="true">›</span></button></template>
      </article>
      <div v-if="!data.results.length" class="platform-empty">{{ exactObject&&!route.query.search?'指定对象不存在或已删除，不能解释为没有依赖。':'此筛选下没有关联对象；不表示系统整体无此能力。请清除搜索或返回来源对象核对。' }}</div>
      <nav v-if="data.next||data.previous" class="relation-pagination" aria-label="关联目录分页"><button class="button ghost small" :disabled="!data.previous" @click="changePage(page-1)">上一页</button><span>第 {{ page }} 页 · 本页 {{ data.results.length }} 项 · 匹配共 {{ data.count }} 项</span><button class="button ghost small" :disabled="!data.next" @click="changePage(page+1)">下一页</button></nav>
    </template>
  </section>
  </RegistryResourceExplorer>
</template>

<style scoped>
.relation-overview{display:grid;gap:var(--config-space-md);font:var(--config-body-size)/1.65 var(--font);min-width:0;overflow-wrap:anywhere}
.relation-breadcrumbs{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:var(--config-help-size)}.relation-breadcrumbs button{white-space:normal;overflow-wrap:anywhere;max-width:100%}.relation-layer{border-top:1px solid var(--border);padding:12px 0}.relation-layer h3{font-size:var(--config-section-size);margin:0}.relation-layer p{margin:4px 0 8px}.relation-group-buttons{display:flex;gap:8px;flex-wrap:wrap}.relation-purpose,.relation-notices{font-size:var(--config-help-size);line-height:1.65;overflow-wrap:anywhere}.relation-purpose{color:var(--text-secondary)}.relation-group-buttons .button{white-space:normal;text-align:left}
.kind-tabs,.relation-search,.relation-context,.relation-pagination{display:flex;align-items:center;flex-wrap:wrap;gap:8px}
.kind-tabs a{min-height:var(--config-control-height);padding:8px 12px;border:1px solid var(--border);border-radius:var(--config-radius);color:var(--text-secondary);font-size:var(--config-body-size)}
.kind-tabs a:hover{background:var(--surface)}.kind-tabs a[aria-current]{border-color:var(--accent);background:var(--accent-soft);color:var(--accent)}
.relation-search label{display:grid;gap:5px;min-width:min(340px,100%);font-size:var(--config-body-size)}
input{min-height:var(--config-control-height);padding:9px;border:1px solid var(--border-strong);background:var(--bg-soft);border-radius:var(--config-radius);color:var(--text);font:inherit;max-width:100%;box-sizing:border-box}
.relation-search{align-items:end}.relation-context{font-size:12px}
.relation-card{padding:12px 14px;border:1px solid var(--border);border-radius:8px;background:var(--bg-soft);min-width:0}
.relation-card header{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:10px}
.relation-identity{flex:1;min-width:min(260px,100%);display:grid;gap:4px}
.relation-identity strong{font-size:var(--config-section-size);font-weight:600;overflow-wrap:anywhere}
.relation-identity p{margin:0;overflow-wrap:anywhere}
.relation-actions{display:flex;align-items:center;flex-wrap:wrap;gap:6px}
.relation-actions>.button{gap:5px}
.relation-actions>.platform-status{margin-right:4px}
.muted{color:var(--text-secondary);font-size:var(--config-help-size)}.relation-pagination{justify-content:center}
.relation-overview .button{font-size:var(--config-body-size);min-height:var(--config-control-height);white-space:normal;max-width:100%}
a:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media(max-width:620px){.relation-search{align-items:stretch;flex-direction:column}.relation-actions{width:100%}.relation-actions>.button{flex:1;justify-content:center}.relation-overview .button,.kind-tabs a,.relation-search input{min-height:44px}}
</style>
