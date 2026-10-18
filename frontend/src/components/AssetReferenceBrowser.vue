<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import AssetMediaPreview from './AssetMediaPreview.vue'
import AssetReferenceManagement from './AssetReferenceManagement.vue'
import { assetReferenceApi } from '@/services/assetReferenceApi'
import type { AssetReferenceSeries, AssetReferenceVersion } from '@/services/assetReferenceApi'
import { createReferenceBrowser } from '@/services/assetReferenceBrowser'
import { repository } from '@/services/repository'
import { taskCenterTarget } from '@/services/taskCenter'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { inputAssetCompatible, resolveReferenceInputAsset } from '@/services/referenceInputSelection'
import type { Asset } from '@/types'
import type { MediaKind } from '@/services/inputSourcePolicies'

const props = defineProps<{ selectionKind?: MediaKind; selectionRole?: 'primary' | 'variant'; disabled?: boolean }>()
const emit = defineEmits<{ selected: [asset: Asset, reference: { version: AssetReferenceVersion; series: AssetReferenceSeries }]; busy: [value: boolean] }>()
const choosing = ref(false), selectionError = ref('')

const flow = createReferenceBrowser({...assetReferenceApi, getAsset: repository.getAsset})
const {state} = flow
watch(() => state.version?.id, () => { selectionError.value = '' })
const filters = ref({search: '', role: '' as '' | 'primary' | 'variant', media: props.selectionKind || '' as '' | MediaKind, bible: ''})
const roles = ref<Array<{value: 'primary' | 'variant'; display_name: string}>>([])
const optionsError = ref('')
const roleName = (role: string) => roles.value.find(item => item.value === role)?.display_name || role
const sourceName = {upload: '本地上传', existing: '已有素材关联', generated: '模型生成'}
let disposed = false
const canChoose = computed(() => !!props.selectionKind && !!state.asset && !state.previewLoading
  && !state.versionsLoading && !state.previewError && inputAssetCompatible(state.asset, props.selectionKind)
  && (!props.selectionRole || state.selected?.role === props.selectionRole))
async function choose() {
  if (props.disabled || choosing.value || !canChoose.value || !state.version || !state.selected || !props.selectionKind) return
  const expected = {...state.version, asset: {...state.version.asset}}
  const series = { ...state.selected }, role = props.selectionRole
  const kind = props.selectionKind
  choosing.value = true; selectionError.value = ''; emit('busy', true)
  try {
    const asset = await resolveReferenceInputAsset({...assetReferenceApi, getAsset: repository.getAsset}, expected, kind)
    if (!disposed && kind === props.selectionKind && role === props.selectionRole
      && state.version?.id === expected.id && state.selected?.id === series.id) emit('selected', asset, { version: expected, series })
  } catch (error) { if (!disposed) selectionError.value = apiErrorSummary(error) }
  finally { choosing.value = false; if (!disposed) emit('busy', false) }
}
async function loadOptions() {
  optionsError.value = ''
  try { const items = await assetReferenceApi.roles(); if(!disposed) roles.value = items }
  catch(error) { if(!disposed) optionsError.value = apiErrorSummary(error) }
}
function apply() {
  const values = filters.value
  void flow.search({page: 1, search: values.search.trim(), ...(values.role ? {role: values.role} : {}),
    ...(values.media ? {media_type: values.media} : {}), ...(values.bible ? {bible_linked: values.bible === 'yes'} : {})})
}
function reset() { filters.value = {search: '', role: '', media: props.selectionKind || '', bible: ''}; apply() }
onMounted(() => { void loadOptions(); apply() })
onBeforeUnmount(() => { disposed = true; flow.dispose(); if (choosing.value) emit('busy', false) })
</script>

<template>
  <section class="reference-browser panel" aria-label="主图与变体关系浏览">
    <fieldset class="browser-controls" :disabled="disabled || choosing">
    <header><h3>主图、变体与版本</h3><p>选中具体版本预览原素材。浏览不会选择生产用图、审核采用或启动生成。</p></header>
    <form class="reference-filters" @submit.prevent="apply">
      <label class="field"><span>系列名称</span><input v-model="filters.search" maxlength="160" placeholder="按任意语言名称查询"></label>
      <label class="field"><span>关系角色</span><select v-model="filters.role"><option value="">全部角色</option><option v-for="item in roles" :key="item.value" :value="item.value">{{ item.display_name }}</option></select></label>
      <label v-if="!selectionKind" class="field"><span>包含媒体</span><select v-model="filters.media"><option value="">全部媒体</option><option value="image">图片</option><option value="video">视频</option><option value="audio">音频</option></select></label>
      <label class="field"><span>设定关联</span><select v-model="filters.bible"><option value="">全部</option><option value="yes">已关联 Bible</option><option value="no">未关联 Bible</option></select></label>
      <button class="button primary" type="submit">查询系列</button><button class="button ghost" type="button" @click="reset">清除筛选</button>
    </form>
    <p v-if="optionsError" role="alert">角色选项读取失败：{{ optionsError }} <button class="button ghost small" @click="loadOptions">重读角色选项</button></p>
    <p class="hint">筛选作用于系列；详情显示其全部关系与版本。独立图片、视频、音频可在“全部素材”浏览。</p>
    <AssetReferenceManagement v-if="!selectionKind" :series="state.selected" :version="state.version" @changed="flow.search({...state.query, page: 1})" />
    <p v-if="selectionError" role="alert">{{ selectionError }}</p>
    <div class="reference-layout">
      <section aria-label="系列查询结果" class="series-list">
        <p aria-live="polite">{{ state.catalogLoading ? '查询中…' : state.catalogError ? '查询失败，数量未取得' : `共 ${state.catalog.count} 个系列 · 第 ${state.query.page} 页` }}</p>
        <p v-if="state.catalogError" role="alert">{{ state.catalogError }} <button class="button ghost small" @click="flow.search()">重试系列查询</button></p>
        <template v-else-if="!state.catalogLoading">
          <button v-for="item in state.catalog.results" :key="item.id" class="series-row" :aria-pressed="state.selected?.id === item.id" @click="flow.select(item)">
            <span class="role">{{ roleName(item.role) }}</span><strong>{{ item.name }}</strong><small>{{ item.version_count }} 个版本<span v-if="item.role === 'primary'"> · {{ item.variant_count }} 个变体</span> · {{ item.bible_variant_id ? '已关联 Bible' : '独立系列' }}</small>
          </button>
          <p v-if="!state.catalog.results.length">没有匹配系列。可调整筛选，或用“添加主图 / 变体素材”入库。</p>
        </template>
        <nav class="pager" aria-label="系列分页"><button class="button ghost small" :disabled="state.catalogLoading || !state.catalog.previous" @click="flow.search({...state.query, page: state.query.page - 1})">上一页系列</button><button class="button ghost small" :disabled="state.catalogLoading || !state.catalog.next" @click="flow.search({...state.query, page: state.query.page + 1})">下一页系列</button></nav>
      </section>
      <section class="relationship-detail" aria-label="关系与版本详情">
        <p v-if="state.detailLoading">读取确切来源版本…</p><p v-if="state.detailError" role="alert">{{ state.detailError }}；请重新选择系列。</p>
        <p v-if="!state.selected && !state.detailLoading">选择一个系列查看主图、变体和版本。</p>
        <template v-if="state.selected">
          <h3>{{ roleName(state.selected.role) }} · {{ state.selected.name }}</h3><p class="hint">{{ state.selected.version_count }} 个版本 · 系列 ID：{{ state.selected.id }}</p>
          <p v-if="state.relationsLoading">读取主图与变体…</p>
          <p v-if="state.relationsError" role="alert">关系读取失败：{{ state.relationsError }} <button class="button ghost small" @click="flow.relations(state.variantPage)">重读关系</button></p>
          <section v-if="state.primary" class="relations" aria-label="主图与同组变体">
            <button class="series-row" :aria-pressed="state.selected.id === state.primary.id" @click="flow.select(state.primary)"><span class="role">{{ roleName(state.primary.role) }}</span><strong>{{ state.primary.name }}</strong><small>{{ state.primary.version_count }} 个版本 · 共 {{ state.primary.variant_count }} 个变体</small></button>
            <div class="variant-list"><button v-for="item in state.variants.results" :key="item.id" class="series-row" :aria-pressed="state.selected.id === item.id" @click="flow.select(item)"><span class="role">{{ roleName(item.role) }}</span><strong>{{ item.name }}</strong><small>{{ item.version_count }} 个版本</small></button></div>
            <nav v-if="state.variants.previous || state.variants.next" class="pager" aria-label="同组变体分页"><span>第 {{ state.variantPage }} 页</span><button class="button ghost small" :disabled="state.relationsLoading || !state.variants.previous" @click="flow.relations(state.variantPage - 1)">上一页变体</button><button class="button ghost small" :disabled="state.relationsLoading || !state.variants.next" @click="flow.relations(state.variantPage + 1)">下一页变体</button></nav>
          </section>
          <h4>版本（浏览选择，不改变生产引用）</h4>
          <p v-if="state.versionsLoading">读取版本…</p><p v-if="state.versionsError" role="alert">{{ state.versionsError }} <button class="button ghost small" @click="flow.versions(state.versionPage)">重读版本</button></p>
          <div v-else class="version-list"><button v-for="item in state.versions.results" :key="item.id" class="button ghost small" :aria-pressed="state.version?.id === item.id" @click="flow.preview(item)">v{{ item.version }} · {{ item.asset.name }} · {{ sourceName[item.source] }}</button><p v-if="!state.versionsLoading && !state.versions.results.length">尚无素材版本。</p></div>
          <nav v-if="state.versions.previous || state.versions.next" class="pager" aria-label="版本分页"><span>第 {{ state.versionPage }} 页 / 共 {{ state.versions.count }} 个版本</span><button class="button ghost small" :disabled="state.versionsLoading || !state.versions.previous" @click="flow.versions(state.versionPage - 1)">上一页版本</button><button class="button ghost small" :disabled="state.versionsLoading || !state.versions.next" @click="flow.versions(state.versionPage + 1)">下一页版本</button></nav>
          <section v-if="state.version" class="version-detail" aria-label="确切素材版本">
            <h4>当前预览 v{{ state.version.version }} · {{ state.version.asset.name }}</h4>
            <template v-if="selectionKind">
              <button type="button" class="button primary" :disabled="!canChoose" @click="choose">{{ choosing ? '核对确切版本…' : '选择此版本用于本项' }}</button>
              <p>回填此版本的原素材 ID，不改变系列主图、其他输入或已保存请求。</p>
              <p v-if="selectionRole && state.selected.role !== selectionRole" role="alert">本项只接受{{ roleName(selectionRole) }}系列的版本；不能用兄弟变体替代主图。</p>
              <p v-if="state.asset && !inputAssetCompatible(state.asset, selectionKind)" role="alert">此版本不能用于本项 {{ selectionKind }} 输入。可选择同系列其他版本，不会自动替换。</p>
            </template>
            <p v-if="state.previewLoading">读取原素材…</p><p v-if="state.previewError" role="alert">{{ state.previewError }} <button class="button ghost small" @click="flow.preview(state.version)">重新读取此版本</button></p>
            <div v-if="state.asset" class="media-preview"><AssetMediaPreview :key="state.version.id" :asset="state.asset" controls @source-error="flow.mediaFailed($event.assetId)"/></div>
            <p v-if="state.asset ? !state.asset.isActive : !state.version.asset.is_active" role="status">此素材已停用；展示原关联不代表可用于生成。</p>
            <dl><dt>入库来源</dt><dd>{{ sourceName[state.version.source] }}</dd><dt>媒体类型</dt><dd>{{ state.version.asset.mime_type }}</dd><dt>版本 ID</dt><dd>{{ state.version.id }}</dd><dt>原素材 ID</dt><dd>{{ state.version.asset.id }}</dd><template v-if="state.version.created_at"><dt>关联时间</dt><dd>{{ state.version.created_at }}</dd></template><template v-if="state.version.setting_revision_id"><dt>设定修订</dt><dd>{{ state.version.setting_revision_id }}</dd></template></dl>
            <p v-if="state.asset?.sourceInfo" class="source-text">原素材来源：{{ state.asset.sourceInfo }}</p>
            <div v-if="!selectionKind" class="source-links">
              <RouterLink class="button ghost small" :to="{name: 'assets', query: {asset: state.version.asset.id}}">打开原素材详情</RouterLink>
              <RouterLink v-if="state.version.generation_job?.id" class="button ghost small" :to="taskCenterTarget({category: 'asset', sourceId: state.version.generation_job.id})">素材生成任务 · {{ state.version.generation_job.status }}</RouterLink>
              <span v-else-if="state.version.generation_job" class="hint" role="status">素材生成任务 ID 未记录，无法定位原任务。</span>
              <RouterLink v-if="state.version.generated_artifact?.generation_attempt_id" class="button ghost small" :to="taskCenterTarget({category: 'production', sourceId: state.version.generated_artifact.generation_attempt_id, runId: state.version.generated_artifact.production_run_id || undefined})">查看原生产调用</RouterLink>
              <span v-else-if="state.version.generated_artifact" class="hint" role="status">生成调用 ID 未记录，无法定位原调用。</span>
              <span v-if="state.version.generated_artifact" class="hint" role="status">产物状态：{{ state.version.generated_artifact.status }}</span>
              <button v-if="state.version.generated_from_id" class="button ghost small" @click="flow.openVersion(state.version.generated_from_id)">查看生成依据主图版本</button>
            </div>
            <p v-if="state.version.source !== 'generated'" class="hint">此关联不是模型生成记录，不推断主图参考或生成任务。</p>
            <p v-if="state.version.generated_artifact" class="hint">原产物：{{ state.version.generated_artifact.id }} · 调用：{{ state.version.generated_artifact.generation_attempt_id || '未记录' }}</p>
            <p v-if="state.version.origin_history_cleared" class="hint">原生成历史已清理。素材文件及主图/变体关系保留，可用于新任务；不代表重新生成或重新审核。</p>
          </section>
        </template>
      </section>
    </div>
    </fieldset>
  </section>
</template>

<style scoped>
.browser-controls{border:0;padding:0;margin:0;min-width:0}
.reference-browser{padding:18px;min-width:0}.reference-browser h3,.reference-browser h4{margin:0 0 10px}.reference-browser p{line-height:1.6;font-size:13px}.hint,.series-row small{color:var(--text-muted,#9299a1)}.reference-filters{display:flex;align-items:end;gap:10px;flex-wrap:wrap;margin:18px 0}.reference-filters .field{flex:1;min-width:140px}.reference-filters .field:first-child{flex:2}.reference-layout{display:grid;grid-template-columns:minmax(210px,1fr) minmax(0,3fr);gap:20px}.series-list,.relationship-detail{min-width:0}.relationship-detail{border-left:1px solid var(--border);padding-left:20px}.series-row{display:flex;flex-direction:column;align-items:flex-start;gap:6px;width:100%;padding:12px;margin-bottom:8px;border:1px solid var(--border);border-radius:8px;background:var(--panel,#11151b);color:inherit;text-align:left;cursor:pointer;overflow-wrap:anywhere}.role{font-size:12px;color:var(--accent,#e4a557)}[aria-pressed=true]{border-color:var(--accent,#e4a557);background:rgba(228,165,87,.07)}.relations{padding:12px;border:1px solid var(--border);border-radius:8px;margin-bottom:18px}.variant-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding-left:16px;border-left:2px solid var(--border)}.version-list,.pager,.source-links{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.pager{margin:12px 0}.version-list button{white-space:normal;text-align:left;overflow-wrap:anywhere}.version-detail{margin-top:16px;padding-top:16px;border-top:1px solid var(--border)}.media-preview{height:320px;background:#090c10;border:1px solid var(--border);overflow:hidden;border-radius:8px}.version-detail dl{display:grid;grid-template-columns:90px minmax(0,1fr);gap:8px;font-size:12px;line-height:1.5}.version-detail dd{margin:0;overflow-wrap:anywhere}.version-detail dt{color:var(--text-muted,#9299a1)}.source-text,.hint,[role=alert]{overflow-wrap:anywhere}[role=alert]{color:#ed969c}.reference-browser button:focus-visible,.reference-browser a:focus-visible{outline:2px solid var(--accent);outline-offset:3px}.source-links .button{white-space:normal}
@media(max-width:780px){.reference-browser{padding:12px}.reference-layout{grid-template-columns:minmax(0,1fr)}.relationship-detail{border-left:0;border-top:1px solid var(--border);padding:16px 0 0}.variant-list{grid-template-columns:minmax(0,1fr)}.media-preview{height:240px}}
</style>
