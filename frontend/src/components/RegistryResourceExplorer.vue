<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { getRelationPage, relationConfigUrl, relationKinds, relationOverviewUrl, type RelationKind, type RelationPage } from '@/services/registryRelations'
import { registryBrowserUrl, registryHierarchy, registryRoles } from '@/services/registryHierarchy'
import { readResourceDetails, runtimeResourceKinds, type ResourceDetails, type ResourceKind } from '@/services/registryResourceDetails'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import PlatformStatusPill from './PlatformStatusPill.vue'
import ReadonlyDataValue from './ReadonlyDataValue.vue'

defineProps<{ label: string }>()
const trail = ref<{ url: string; label: string; resource?: {kind: ResourceKind; id:string} }[]>([])
const page = ref<RelationPage | null>(null), details = ref<ResourceDetails | null>(null)
const loading = ref(false), detailLoading = ref(false), error = ref(''), detailError = ref('')
const panel = ref<HTMLElement | null>(null)
let opener: HTMLElement | null = null, sequence = 0, controller: AbortController | undefined
const current = computed(() => trail.value[trail.value.length - 1])
const exactRow = computed(() => {
  if (!current.value || !page.value || page.value.results.length !== 1) return null
  return new URL(current.value.url, 'https://navigation.invalid').searchParams.get('ids') === page.value.results[0]?.id ? page.value.results[0] : null
})
async function focusPanel() { await nextTick(); panel.value?.focus({ preventScroll: true }); panel.value?.scrollIntoView({ block: 'start' }) }
async function load() {
  if (!current.value) return
  const serial = ++sequence
  controller?.abort(); controller = new AbortController()
  page.value = null; details.value = null; error.value = ''; detailError.value = ''
  if (current.value.resource) {
    const {kind,id}=current.value.resource
    loading.value=false; detailLoading.value=true
    try {
      const result=await readResourceDetails(kind,id)
      if(serial===sequence){details.value=result;if(result&&current.value)current.value.label=result.name}
    } catch(cause) {if(serial===sequence)detailError.value=apiErrorSummary(cause,'资源详情读取失败')}
    finally {if(serial===sequence)detailLoading.value=false}
    return
  }
  const query = new URL(current.value.url, 'https://navigation.invalid').searchParams
  const kind = query.get('kind') as RelationKind
  const ids = query.get('ids')?.split(',')
  loading.value = true; detailLoading.value = Boolean(ids?.length === 1)
  const relationRead = (async () => {
    try {
      const result = await getRelationPage({ ...Object.fromEntries([...query].filter(([key]) => key !== 'view')), page_size: 25 }, controller!.signal)
      if (result.results.some(row => row.kind !== kind || ids && !ids.includes(row.id))) throw new Error('关联目录返回其他对象，未采用返回结果。')
      if (serial === sequence) page.value = result
    } catch (cause) { if (serial === sequence) error.value = apiErrorSummary(cause, '使用关系读取失败') }
    finally { if (serial === sequence) loading.value = false }
  })()
  const detailRead = (async () => {
    if (ids?.length !== 1) return
    try {
      const result = await readResourceDetails(kind, ids[0]!)
      if (serial === sequence) {
        details.value = result
        if (result && current.value) current.value.label = `${result.name}${result.version === undefined ? '' : ` v${result.version}`}`
      }
    } catch (cause) { if (serial === sequence) detailError.value = apiErrorSummary(cause, '资源详情读取失败') }
    finally { if (serial === sequence) detailLoading.value = false }
  })()
  await Promise.all([relationRead, detailRead])
}
function navigate(url: string, label: string) {
  const safe = registryBrowserUrl(url)
  if (!safe) { error.value = '资源下钻地址无效，未离开当前上下文。'; return }
  const index = trail.value.findIndex(entry => entry.url === safe)
  trail.value = index >= 0 ? trail.value.slice(0, index + 1) : [...trail.value, { url: safe, label }]
  void load(); void focusPanel()
}
function inspect(kind: ResourceKind, id: string, event?: Event) {
  if (!id || !(Object.prototype.hasOwnProperty.call(relationKinds, kind)||Object.prototype.hasOwnProperty.call(runtimeResourceKinds,kind))) return
  if (!trail.value.length) opener = (event?.currentTarget as HTMLElement | null) || null
  if(Object.prototype.hasOwnProperty.call(runtimeResourceKinds,kind)) {
    const url=`resource:${kind}:${encodeURIComponent(id)}`,index=trail.value.findIndex(entry=>entry.url===url)
    trail.value=index>=0?trail.value.slice(0,index+1):[...trail.value,{url,label:runtimeResourceKinds[kind as keyof typeof runtimeResourceKinds],resource:{kind,id}}]
    void load();void focusPanel();return
  }
  navigate(relationOverviewUrl(kind as RelationKind, id), relationKinds[kind as RelationKind])
}
function ancestor(index: number) { trail.value = trail.value.slice(0, index + 1); void load(); void focusPanel() }
async function returnToOwner() {
  sequence++; controller?.abort(); trail.value = []; page.value = null; details.value = null
  await nextTick(); if (opener?.isConnected) { opener.focus({ preventScroll: true }); opener.scrollIntoView({ block: 'nearest' }) }
  opener = null
}
function changePage(delta: number) {
  if (!current.value) return
  const url = new URL(current.value.url, 'https://navigation.invalid')
  url.searchParams.set('page', String(Number(url.searchParams.get('page') || 1) + delta))
  current.value.url = url.pathname + url.search
  void load(); void focusPanel()
}
onBeforeUnmount(() => { sequence++; controller?.abort() })
</script>

<template>
  <div class="registry-resource-explorer">
    <!-- Keep the owner mounted, including unsaved values and expanded controls. -->
    <div v-show="!current" class="resource-owner"><slot :inspect="inspect"/></div>
    <section v-if="current" ref="panel" class="resource-inspector" aria-label="公共资源只读详情" tabindex="-1" :aria-busy="loading || detailLoading">
      <nav class="resource-breadcrumbs" aria-label="资源下钻路径">
        <button type="button" class="button ghost small" @click="returnToOwner">返回{{ label }}</button>
        <template v-for="(entry,index) in trail" :key="entry.url"><span aria-hidden="true">›</span><button v-if="index < trail.length - 1" type="button" class="button ghost small" @click="ancestor(index)">{{ entry.label }}</button><span v-else aria-current="page">{{ entry.label }}</span></template>
      </nav>
      <p class="resource-note">只读查看精确引用的当前记录，不改写原页面的冻结合同。原页面仍保留未提交内容；查看不会保存、改绑版本或触发执行。维护入口在新窗口打开，完成后返回并显式选择版本。</p>
      <button type="button" class="button ghost small" :disabled="loading || detailLoading" @click="load">重新读取此资源</button>
      <p v-if="detailLoading" role="status">正在读取资源定义…</p>
      <p v-if="detailError" class="platform-alert danger" role="alert">{{ detailError }} 原引用保持，不使用同名或最新版本替代。</p>
      <article v-if="details" class="resource-definition">
        <header><h3>{{ details.name }}<small v-if="details.version !== undefined">v{{ details.version }}</small></h3><PlatformStatusPill :status="details.status" :bilingual="false"/></header>
        <p v-if="exactRow" class="resource-note">{{ registryRoles[exactRow.kind] }}</p>
        <section v-for="section in details.sections" :key="section.title" class="resource-section"><h4>{{ section.title }}</h4><p v-if="section.description" class="resource-note">{{ section.description }}</p><dl v-if="section.facts.length"><div v-for="(fact,index) in section.facts" :key="index"><dt>{{ fact.label }}</dt><dd><ReadonlyDataValue :value="fact.value" :label="fact.label"/></dd></div></dl><p v-else>此版本明确没有此类配置项。</p></section>
        <details><summary>资源标识（精确定位）</summary><ReadonlyDataValue :value="details.id" label="资源标识"/></details>
        <section v-if="details.links || details.relationScope" class="resource-section"><h4>明确引用与使用范围</h4><p v-if="details.relationScope" class="resource-note">{{details.relationScope}}</p><div class="resource-links"><button v-for="link in details.links" :key="`${link.kind}:${link.id}`" type="button" class="button ghost small" @click="inspect(link.kind,link.id)">{{link.label}} · 查看定义</button></div><p v-if="details.links?.length===0">上述范围内没有明确引用，不代表其他范围也没有使用。</p></section>
        <a v-if="details.maintenanceUrl" class="button ghost small" :href="details.maintenanceUrl" target="_blank" rel="noopener">前往唯一维护页（新窗口）</a>
      </article>
      <p v-if="loading" role="status">正在读取依赖与使用关系…</p>
      <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
      <template v-if="page">
        <p class="resource-note">关系读取于 {{ new Date(page.generated_at).toLocaleString('zh-CN') }} · 共 {{ page.count }} 个对象</p>
        <template v-if="exactRow">
          <p v-if="!details && !detailLoading && !detailError">此类型的语义详情请在唯一维护页查看。下面是当前配置关系，不是完整定义或实际执行记录。</p>
          <a class="button ghost small" :href="relationConfigUrl(exactRow)" target="_blank" rel="noopener">前往唯一维护页（新窗口）</a>
          <ul v-if="exactRow.notices.length"><li v-for="notice in exactRow.notices" :key="notice">{{ notice }}</li></ul>
          <section v-for="layer in registryHierarchy(exactRow)" :key="layer.key" class="resource-section"><h4>{{ layer.label }}</h4><p class="resource-note">{{ layer.description }}</p><div class="resource-links"><button v-for="group in layer.groups" :key="group.key" type="button" class="button ghost small" @click="navigate(group.url, group.label)">{{ group.label }} · {{ group.count }} 项 ›</button></div></section>
        </template>
        <template v-else>
          <ul class="resource-results"><li v-for="row in page.results" :key="row.id"><button type="button" class="button ghost small" @click="inspect(row.kind, row.id)">{{ row.label }} · 查看定义</button><PlatformStatusPill :status="row.status" :bilingual="false"/></li></ul>
          <p v-if="!page.results.length">指定对象不存在、已删除或此范围没有关联。原引用保持；不自动选择其他对象。</p>
          <nav v-if="page.next || page.previous" class="resource-links" aria-label="资源关系分页"><button type="button" class="button ghost small" :disabled="!page.previous || loading" @click="changePage(-1)">上一页</button><span>本页 {{ page.results.length }} 项 / 共 {{ page.count }} 项</span><button type="button" class="button ghost small" :disabled="!page.next || loading" @click="changePage(1)">下一页</button></nav>
        </template>
      </template>
    </section>
  </div>
</template>

<style scoped>
.registry-resource-explorer,.resource-owner,.resource-inspector{min-width:0}.resource-inspector{display:grid;gap:var(--config-space-lg);font:var(--config-body-size)/1.65 var(--font);overflow-wrap:anywhere}.resource-breadcrumbs,.resource-links,.resource-definition header,.resource-results li{display:flex;align-items:center;gap:var(--config-space-sm);flex-wrap:wrap}.resource-inspector .button{font:500 var(--config-body-size)/1.5 var(--font);white-space:normal;max-width:100%;text-align:left;min-height:var(--config-control-height)}.resource-inspector>button,.resource-inspector>a{justify-self:start}.resource-breadcrumbs{border-bottom:1px solid var(--border);padding-bottom:var(--config-space-md)}.resource-note{font-size:var(--config-help-size);color:var(--text-secondary);margin:0}.resource-definition h3,.resource-section h4{font-size:var(--config-section-size);font-weight:600;margin:0}.resource-definition h3 small{margin-left:8px;font-size:var(--config-help-size)}.resource-section{border-top:1px solid var(--border);padding:var(--config-space-lg) 0;margin-top:var(--config-space-lg)}.resource-section dl{margin:var(--config-space-md) 0 0;display:grid;gap:var(--config-space-md)}.resource-section dt{font-weight:600}.resource-section dd{margin:0}.resource-inspector summary{cursor:pointer;min-height:var(--config-control-height);align-content:center}.resource-results{list-style:none;padding:0;margin:0;display:grid;gap:var(--config-space-md)}.resource-inspector :focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:640px){.resource-inspector{--config-control-height:44px}}
</style>
