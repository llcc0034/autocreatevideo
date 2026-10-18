<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, ref, useId, watch } from 'vue'
import type { Asset } from '@/types'
import type { AssetReferenceSeries, AssetReferenceVersion } from '@/services/assetReferenceApi'
import { repository } from '@/services/repository'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { isDefinitiveCreationRejection } from '@/services/productionOnboarding'
import { mediaKindLabel } from '@/services/presentationLabels'
import { inputAssetCompatible } from '@/services/referenceInputSelection'

const AssetReferenceBrowser = defineAsyncComponent(() => import('./AssetReferenceBrowser.vue'))

const props = withDefaults(defineProps<{ kind: 'image' | 'video' | 'audio'; purpose?: string | null; disabled?: boolean; allowUpload?: boolean; allowLibrary?: boolean }>(), { allowUpload: true, allowLibrary: true })
const emit = defineEmits<{ selected: [asset: Asset, source: 'asset_library'|'asset_upload', reference?: { version: AssetReferenceVersion; series: AssetReferenceSeries }]; busy: [busy: boolean] }>()
const open = ref(false), loading = ref(false), saving = ref(false), search = ref(''), page = ref(1), error = ref('')
const appliedSearch = ref(''), listError = ref(''), uploadNotice = ref('')
const uploadErrorDetail = ref('')
const panelId = useId(), opener = ref<HTMLButtonElement | null>(null), panel = ref<HTMLElement | null>(null)
const result = ref<Awaited<ReturnType<typeof repository.getAssetPage>> | null>(null)
const file = ref<File | null>(null), name = ref(''), fileInput = ref<HTMLInputElement | null>(null), uploadUnknown = ref(false)
const view = ref<'assets'|'series'|'upload'>('assets'), selecting = ref(false)
const notice = ref('')
const busy = computed(() => saving.value || selecting.value)
function selectionBusy(value: boolean) { selecting.value = value; emit('busy', busy.value) }
let sequence = 0
onBeforeUnmount(() => { sequence++; if(busy.value)emit('busy',false) })
watch(() => [props.kind, props.purpose, props.allowUpload, props.allowLibrary], () => {
  if (busy.value) notice.value = saving.value
    ? '选择条件已改变，已提交上传不会取消，也不会回填新条件；请到素材库核对结果。'
    : '选择条件已改变，之前的版本读取不会回填；请按新条件重新选择。'
  sequence++; loading.value=false; result.value=null; file.value=null; name.value=''; listError.value=''
  open.value=false; view.value='assets'
  error.value=saving.value?'选择条件已改变；已提交上传不会取消，返回后不会回填。请到素材库查看结果。':''
  if (fileInput.value) fileInput.value.value=''
})
const compatible = (asset: Asset) => inputAssetCompatible(asset, props.kind)
const matching = computed(() => result.value?.results.filter(compatible) ?? [])
async function load(next = 1, pagination = false) {
  if (props.disabled || busy.value || !props.allowLibrary) return
  const query = pagination ? appliedSearch.value : search.value
  const request = ++sequence; loading.value = true; listError.value = ''
  try { const rows = await repository.getAssetPage(next, query); if (request === sequence) { result.value = rows; page.value = next; appliedSearch.value = query } }
  catch (cause) { if (request === sequence) listError.value = apiErrorSummary(cause) }
  finally { if (request === sequence) loading.value = false }
}
function show() { if (props.disabled || busy.value || (!props.allowLibrary && !props.allowUpload)) return; open.value = true; view.value=props.allowLibrary?'assets':'upload'; void nextTick(()=>panel.value?.focus()); if(props.allowLibrary) void load(1) }
function close() { if (busy.value) return; sequence++; open.value = false; loading.value = false; void nextTick(()=>opener.value?.focus()) }
function select(asset: Asset, reference?: { version: AssetReferenceVersion; series: AssetReferenceSeries }) { if (!props.allowLibrary || (props.disabled && !selecting.value) || saving.value || loading.value || (view.value==='assets' && !!listError.value) || !compatible(asset)) return; notice.value=''; emit('selected', asset, 'asset_library', reference); sequence++; open.value=false; loading.value=false; void nextTick(()=>opener.value?.focus()) }
function chooseFile(event: Event) {
  if (props.disabled || busy.value || uploadUnknown.value) return
  file.value = (event.target as HTMLInputElement).files?.[0] ?? null
  name.value = file.value?.name ?? ''
}
async function upload() {
  if (!props.allowUpload || !file.value || !name.value.trim() || busy.value || props.disabled || uploadUnknown.value) return
  saving.value = true; emit('busy', true); error.value = ''; uploadErrorDetail.value = ''; loading.value = false
  const request=++sequence
  try {
    const asset = await repository.createAsset({ file: file.value, mediaType: props.kind, imageCategory: 'reference',
      name: name.value, description: '', promptHint: '' })
    if (request !== sequence) return
    file.value = null; name.value = ''; if (fileInput.value) fileInput.value.value = ''
    if (!compatible(asset)) { error.value='素材已入库，但返回类型或文件证据不符合当前槽位；未自动采用，请到素材库查看。'; uploadNotice.value=`已入库素材 ${asset.id}，未采用到本次输入。`; return }
    notice.value=''; emit('selected', asset, 'asset_upload'); open.value = false; sequence++
  } catch (cause) {
    if (!isDefinitiveCreationRejection(cause)) {
      uploadUnknown.value = true
      uploadNotice.value = '上传结果尚未确定。请先到素材库核对；当前选择器不会再次上传，也不会按同名文件自动采用。'
    }
    uploadErrorDetail.value = apiErrorSummary(cause, '上传未取得可确认回执')
    if(request === sequence) error.value = uploadUnknown.value ? '上传结果未确定。请先核对原上传结果，不要重复提交。' : `${uploadErrorDetail.value} 上传被明确拒绝；原输入保留，请修正后重试。`
  }
  finally { saving.value = false; emit('busy', false) }
}
</script>

<template>
  <p v-if="notice" role="status">{{ notice }}</p>
  <p v-if="uploadNotice" role="status" class="platform-alert warning">{{ uploadNotice }} <a href="/assets" target="_blank" rel="noopener noreferrer">查看素材库（新窗口）</a></p>
  <details v-if="uploadUnknown && uploadErrorDetail"><summary>查看原错误信息</summary><p>{{ uploadErrorDetail }}</p></details>
  <button ref="opener" type="button" class="button ghost small" :aria-expanded="open" :aria-controls="panelId" :disabled="disabled || busy || (!allowLibrary && !allowUpload)" @click="show">{{ allowLibrary ? (allowUpload ? '选择素材 / 上传本地文件' : '选择已有素材') : (allowUpload ? '上传本地文件并入库' : '此处没有允许的素材来源') }}</button>
    <section v-if="open" :id="panelId" ref="panel" class="execution-asset-picker" tabindex="-1" aria-label="选择输入素材">
      <header class="picker-actions"><h4>为此项选择{{ mediaKindLabel(kind) }}</h4><button type="button" class="button ghost small" :disabled="busy" @click="close">收起选材</button></header>
      <p>输入类型：{{ mediaKindLabel(kind) }} · 用途：{{ purpose || '未声明（由本项字段配置决定）' }}。只更新本次输入，不改写素材库属性。</p>
      <nav class="picker-actions" aria-label="选材方式"><button v-if="allowLibrary" type="button" class="button ghost small" :aria-pressed="view==='assets'" :disabled="busy || disabled" @click="view='assets'">选择已有素材</button><button v-if="allowLibrary" type="button" class="button ghost small" :aria-pressed="view==='series'" :disabled="busy || disabled" @click="view='series'">按系列与版本选择</button><button v-if="allowUpload" type="button" class="button ghost small" :aria-pressed="view==='upload'" :disabled="busy || disabled" @click="view='upload'">上传本地文件并入库</button></nav>
      <AssetReferenceBrowser v-if="open && allowLibrary && view==='series'" :key="kind" :selection-kind="kind" :disabled="disabled" @selected="select" @busy="selectionBusy"/>
      <form v-if="allowLibrary && view==='assets'" class="picker-actions" @submit.prevent="load(1)"><label>名称或描述<input v-model="search" :disabled="busy || disabled"/></label><button class="button ghost small" :disabled="loading || busy || disabled">搜索</button></form>
      <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
      <p v-if="listError && view==='assets'" class="platform-alert danger" role="alert">{{ listError }}<span v-if="result">保留上次目录供查看，不能采用；请重新搜索。</span></p>
      <p v-if="loading && view==='assets'" role="status">正在读取素材目录…</p>
      <template v-if="allowLibrary && view==='assets' && result">
        <p>已查询「{{ appliedSearch || '全部' }}」· 共 {{ result.count }} 项素材 · 第 {{ page }} 页 · 本页 {{ matching.length }} 项符合{{ mediaKindLabel(kind) }}类型且具有文件摘要。实际数量、用途和媒体规格将在提交时校验。</p>
        <p v-if="search!==appliedSearch">搜索词已修改，点击搜索后生效；翻页仍使用上次查询条件。</p>
        <ul><li v-for="asset in matching" :key="asset.id"><span>{{ asset.name }}<small>{{ asset.mimeType }}</small></span><button type="button" class="button ghost small" :disabled="busy || loading || disabled || !!listError" @click="select(asset)">选择此素材</button></li></ul>
        <p v-if="!matching.length">本页没有匹配素材；可以翻页、修改搜索{{ allowUpload?'或上传文件':'' }}。</p>
        <nav class="picker-actions" aria-label="输入素材目录分页"><button type="button" class="button ghost small" :disabled="busy || disabled || loading || !result.previous" @click="load(page - 1,true)">上一页</button><button type="button" class="button ghost small" :disabled="busy || disabled || loading || !result.next" @click="load(page + 1,true)">下一页</button></nav>
      </template>
      <div v-if="allowUpload && view==='upload'" class="picker-upload"><h4>上传入库 → 采用到此项输入</h4><p>先创建正式素材，再采用其素材 ID；不把临时文件路径传给执行器。收起选材不会删除已入库素材。</p><label>文件<input ref="fileInput" type="file" :accept="`${kind}/*`" :disabled="busy || disabled || uploadUnknown" @change="chooseFile"/></label><p v-if="file">已选文件：{{ file.name }}</p><label>素材名称<input v-model="name" :disabled="busy || disabled || uploadUnknown"/></label><button type="button" class="button primary small" :disabled="busy || disabled || !file || !name.trim() || uploadUnknown" @click="upload">{{ saving ? '正在上传并核对返回素材…' : '上传并采用' }}</button></div>
    </section>
</template>

<style scoped>
.execution-asset-picker,.picker-upload{display:grid;grid-template-columns:minmax(0,1fr);gap:12px;min-width:0}.execution-asset-picker{padding:12px;border:1px solid var(--border);border-radius:6px;background:var(--bg-panel);font:var(--config-body-size)/1.7 var(--font)}.picker-actions{display:flex;gap:8px;align-items:end;flex-wrap:wrap;min-width:0}.execution-asset-picker h4{font-size:var(--config-section-size);font-weight:600;margin:0;overflow-wrap:anywhere}.execution-asset-picker label{display:grid;gap:6px;min-width:0}.execution-asset-picker input{min-width:0;max-width:100%}.execution-asset-picker p{font-size:var(--config-body-size);line-height:1.7;margin:0;overflow-wrap:anywhere}.execution-asset-picker ul{list-style:none;margin:0;padding:0;max-height:360px;overflow:auto}.execution-asset-picker li{display:flex;gap:10px;align-items:center;justify-content:space-between;padding:8px;border-bottom:1px solid var(--border);flex-wrap:wrap;overflow-wrap:anywhere}.execution-asset-picker li>span{min-width:0;flex:1 1 140px}.execution-asset-picker small{display:block;font-size:var(--config-help-size);color:var(--text-muted)}.execution-asset-picker .button[aria-pressed=true]{border-color:var(--accent);color:var(--accent)}
</style>
