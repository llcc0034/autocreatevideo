<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowUpRight, ShieldCheck } from 'lucide-vue-next'
import PlatformStatusPill from './PlatformStatusPill.vue'
import RegistryRelationSummary from './RegistryRelationSummary.vue'
import { getRelationPage, relationConfigUrl, relationKinds, relationOverviewUrl, type RelationKind, type RelationPage } from '@/services/registryRelations'
import { apiErrorSummary } from '@/services/apiErrorPresentation'

const route = useRoute(); const router = useRouter()
const data = ref<RelationPage | null>(null); const loading = ref(false); const error = ref(''); const search = ref('')
const kind = computed(() => String(route.query.kind || 'model'))
const page = computed(() => Number(route.query.page || 1))
const query = computed(() => ({ kind: kind.value, page: page.value, page_size: 25,
  ...Object.fromEntries(['ids', 'source_kind', 'source_id', 'relation', 'search'].filter(key => route.query[key]).map(key => [key, String(route.query[key])])) }))
const sourceUrl = computed(() => route.query.source_kind && route.query.source_id && Object.prototype.hasOwnProperty.call(relationKinds, String(route.query.source_kind))
  ? relationOverviewUrl(String(route.query.source_kind) as RelationKind, String(route.query.source_id)) : '')
let sequence = 0; let controller: AbortController | undefined
const load = async () => {
  const serial = ++sequence; controller?.abort(); controller = new AbortController()
  const params = query.value; data.value = null; error.value = ''; loading.value = true
  try {
    const result = await getRelationPage(params, controller.signal)
    if (sequence === serial) data.value = result
  } catch (cause) { if (sequence === serial) error.value = apiErrorSummary(cause, '关联目录读取失败') }
  finally { if (sequence === serial) loading.value = false }
}
const changePage = (page: number) => router.push({ path: route.path, query: { ...route.query, page: String(page) } })
const submit = () => router.push({ path: route.path, query: { ...route.query, search: search.value || undefined, page: '1' } })
watch(() => JSON.stringify(query.value), () => { search.value = String(route.query.search || ''); void load() }, { immediate: true })
onBeforeUnmount(() => { sequence++; controller?.abort() })
</script>

<template>
  <section class="relation-overview" aria-label="能力与关联总览">
    <div class="platform-alert info"><p>查看已配置的关联，点击关联项继续查看或配置。任务实际使用的配置，请查看对应任务的执行记录。</p></div>
    <details class="relation-help"><summary>这些关联有什么区别？</summary><ul>
      <li>提示词绑定：规定策略适用于哪些模型、用途和模式；适用模型是符合该绑定范围的模型。</li>
      <li>引用配方：配置中使用了此对象的生产配方，不是任务执行次数。</li>
      <li>校验工作流：以此实例为校验目标的工作流；其他同模型工作流尚未关联此实例。是否验收通过，请进入验收页查看。</li>
      <li>引用 Projector：工作流合同保存的方案版本，历史版本不会替换为新版本。</li>
    </ul></details>
    <nav class="kind-tabs" aria-label="关联对象类型"><RouterLink v-for="(label,key) in relationKinds" :key="key" :to="relationOverviewUrl(key)" :aria-current="kind===key?'page':undefined">{{ label }}</RouterLink></nav>
    <form class="relation-search" @submit.prevent="submit"><label>搜索当前对象<input v-model="search" type="search" placeholder="名称、标识或模型族"/></label><button class="button ghost small">搜索</button><button class="button ghost small" type="button" :disabled="loading" @click="load">重新读取</button></form>
    <div v-if="sourceUrl||route.query.ids" class="relation-context"><span>{{ data?.context_label || '精确关联筛选' }}</span><RouterLink v-if="sourceUrl" :to="sourceUrl">返回来源对象</RouterLink><RouterLink :to="relationOverviewUrl((Object.prototype.hasOwnProperty.call(relationKinds,kind)?kind:'model') as RelationKind)">清除关联筛选</RouterLink></div>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <p v-else-if="loading" class="platform-loading">正在读取当前页的配置关系…</p>
    <template v-else-if="data">
      <p class="muted">共 {{ data.count }} 个对象 · 读取于 {{ new Date(data.generated_at).toLocaleString('zh-CN') }} · 包含停用 / 历史记录</p>
      <article v-for="row in data.results" :key="row.id" class="relation-card">
        <header>
          <div class="relation-identity"><strong>{{ row.label }}</strong><p class="muted">声明能力 / 引擎：{{ row.capabilities.join('、') || '详见对象合同' }}</p></div>
          <div class="relation-actions">
            <PlatformStatusPill :status="row.status" :bilingual="false"/>
            <RouterLink class="button ghost small" :to="relationConfigUrl(row)" :aria-label="`${row.label} · 查看配置`"><ArrowUpRight :size="14" aria-hidden="true"/>查看配置</RouterLink>
            <RouterLink v-if="row.acceptance_url" class="button ghost small" :to="row.acceptance_url" :aria-label="`${row.label} · 查看验收与下一步`"><ShieldCheck :size="14" aria-hidden="true"/>查看验收</RouterLink>
          </div>
        </header>
        <RegistryRelationSummary :row="row" expanded/>
      </article>
      <div v-if="!data.results.length" class="platform-empty">此筛选下没有关联对象。来源对象尚未配置关联、记录已删除或搜索无匹配；不表示系统整体无此能力。请返回来源对象的配置页核对。</div>
      <nav v-if="data.count>25" class="relation-pagination" aria-label="关联目录分页"><button class="button ghost small" :disabled="!data.previous" @click="changePage(page-1)">上一页</button><span>{{ page }} / {{ Math.ceil(data.count/25) }}</span><button class="button ghost small" :disabled="!data.next" @click="changePage(page+1)">下一页</button></nav>
    </template>
  </section>
</template>

<style scoped>
.relation-overview{display:grid;gap:12px}
.kind-tabs,.relation-search,.relation-context,.relation-pagination{display:flex;align-items:center;flex-wrap:wrap;gap:8px}
.kind-tabs a{min-height:36px;padding:8px 12px;border:1px solid var(--border);border-radius:6px;color:var(--text-secondary);font-size:12px}
.kind-tabs a:hover{background:var(--surface)}.kind-tabs a[aria-current]{border-color:var(--accent);background:var(--accent-soft);color:var(--accent)}
.relation-search label{display:grid;gap:5px;min-width:min(340px,100%);font-size:12px}
input{min-height:36px;padding:9px;border:1px solid var(--border-strong);background:var(--bg-soft);border-radius:6px;color:var(--text)}
.relation-search{align-items:end}.relation-context{font-size:12px}
.relation-card{padding:12px 14px;border:1px solid var(--border);border-radius:8px;background:var(--bg-soft);min-width:0}
.relation-card header{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:10px}
.relation-identity{flex:1;min-width:min(260px,100%);display:grid;gap:4px}
.relation-identity strong{font-size:13px;overflow-wrap:anywhere}
.relation-identity p{margin:0;overflow-wrap:anywhere}
.relation-actions{display:flex;align-items:center;flex-wrap:wrap;gap:6px}
.relation-actions>.button{min-height:36px;gap:5px}
.relation-actions>.platform-status{margin-right:4px}
.muted{color:var(--text-secondary);font-size:11px}.relation-pagination{justify-content:center}
a:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media(max-width:620px){.relation-search{align-items:stretch;flex-direction:column}.relation-actions{width:100%}.relation-actions>.button{flex:1;justify-content:center}}
</style>
