<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { platformApi } from '@/services/platformApi'
import { assetReferenceApi } from '@/services/assetReferenceApi'
import type { AssetReferenceSeries } from '@/services/assetReferenceApi'
import { repository } from '@/services/repository'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { createMaterialFulfillmentForm, materialRequirementOptions, materialFulfillmentRunAvailable, type MaterialRequirementOption } from '@/services/recipeMaterialFulfillmentForm'
import type { ProductionRun, StageRun } from '@/services/platformTypes'
import ExecutionInputAssetPicker from './ExecutionInputAssetPicker.vue'
import AssetMediaPreview from './AssetMediaPreview.vue'

const props = defineProps<{run: ProductionRun; stage: StageRun; disabled?: boolean}>()
const emit = defineEmits<{changed: []; busy: [value: boolean]}>()
const flow = createMaterialFulfillmentForm({...platformApi, ...assetReferenceApi, getAsset: repository.getAsset}), {state} = flow
const sourceNames = {generated: '模型生成', upload: '正式上传', existing: '已有素材关联'}
const pickerBusy = ref(false), requirementPage = ref(1)
const requirements = computed(() => {
  try { return {rows: materialRequirementOptions(props.run, props.stage), error: ''} }
  catch (cause) { return {rows: null, error: apiErrorSummary(cause)} }
})
const pageRows = computed(() => (requirements.value.rows || []).slice((requirementPage.value - 1) * 50, requirementPage.value * 50))
const busy = computed(() => state.submitting || state.choosingVersion || pickerBusy.value)
const locked = computed(() => !!props.disabled || busy.value || state.loading || state.unknown)
const editable = computed(() => !locked.value && materialFulfillmentRunAvailable(props.run, props.stage)
  && !!state.context?.canSelect && !state.receipt && !state.versionsLoading)
watch(busy, value => emit('busy', value), {flush: 'sync'})
watch(() => JSON.stringify([props.run.id, props.stage.id, props.run.executionPlan?.sha256,
  props.stage.inputSnapshotSha256, props.run.status, props.run.blockedCode, props.stage.status, props.stage.reasonCode]), () => {
  flow.reset(); requirementPage.value = 1
})
onBeforeUnmount(() => { flow.reset(); emit('busy', false) })
async function inspect(option: MaterialRequirementOption) {
  if (locked.value) return
  await flow.inspect(props.run.id, props.stage.id, props.run.executionPlan!.sha256, option)
  if (state.context?.canSelect) await flow.loadSeries()
}
async function refresh() {
  if (busy.value || props.disabled) return
  await flow.refresh()
  if (state.context?.canSelect && !state.unknown) await flow.loadSeries()
  if (state.receipt) emit('changed')
}
async function submit() {
  if (props.disabled || busy.value || (!editable.value && !state.unknown)) return
  if (await flow.submit()) emit('changed')
}
async function chooseSeries(series: AssetReferenceSeries) {
  if (!editable.value) return
  flow.chooseSeries(series)
  await flow.loadVersions()
}
</script>

<template>
  <section v-if="requirements.rows !== null || requirements.error" class="material-fulfillment" aria-label="本轮素材需求履约">
    <h3>本轮素材需求 · 选择已有图 / 上传成图</h3>
    <p>这里选择本需求的成图结果，不是模型参考输入或生成候选审核。确认后仅记录确切素材关联；原调度会按配方继续，后续生成仍受原运行授权约束。</p>
    <p v-if="requirements.error" role="alert">{{ requirements.error }}</p>
    <template v-else-if="requirements.rows">
      <p v-if="!requirements.rows.length">本轮已冻结需求为空，没有可选素材目标。</p>
      <template v-else>
        <p>共 {{requirements.rows.length}} 项冻结需求 · 第 {{requirementPage}} 页。选择一项后核对其确切设定与当前可操作状态。</p>
        <ul class="requirements"><li v-for="item in pageRows" :key="item.unitKey"><strong>{{item.name}} · {{item.role === 'primary' ? '主图' : '变体'}}</strong><button type="button" class="button ghost small" :disabled="locked" :aria-pressed="state.context?.unitKey === item.unitKey" @click="inspect(item)">查看本项选材</button></li></ul>
        <nav v-if="requirements.rows.length > 50" aria-label="素材需求分页"><button class="button ghost small" :disabled="locked || requirementPage===1" @click="requirementPage--">上一页需求</button><button class="button ghost small" :disabled="locked || requirementPage*50 >= requirements.rows.length" @click="requirementPage++">下一页需求</button></nav>
      </template>
    </template>
    <p v-if="state.loading" role="status">正在核对本轮需求…</p>
    <p v-if="state.error" role="alert">{{state.error}}</p>
    <button v-if="state.context || state.error" type="button" class="button ghost small" :disabled="busy || disabled || state.loading" @click="refresh">重新核对本项履约</button>
    <p v-if="state.unknown" role="status">提交结果尚未确认，不能换图或切换需求。可重新核对，或仅重试相同选择；不重复上传。上传素材仍保留在素材库。</p>
    <button v-if="state.unknown" type="button" class="button primary small" :disabled="busy || disabled || state.loading" @click="submit">核对 / 重试相同选择</button>
    <template v-if="state.context">
      <h4>{{state.context.referenceName}}</h4>
      <p>本项来源：{{state.context.sourceMode === 'existing' ? '已有成图' : state.context.sourceMode === 'upload' ? '上传成图' : '默认模型生成；创建准备前、未冻结且主/辅助均无调用的受阻准备，或全部调用明确失败后，可选择已有图或上传'}}。</p>
      <p v-if="state.context.sourceMode === 'generate' && state.context.canSelect && !state.receipt">确认成图时会在同一事务内停止本单元可接管的未提交受阻准备及其辅助准备，保留原输入与阻断记录；不会取消在途调用或把此选择记为图片审核。</p>
      <p v-if="run.blockedCode === 'waiting_material_recovery'">失败接管仅替换本轮成图来源，原失败请求与费用保留；不会重发失败调用，也不是人工审核。保存后原调度按已有授权继续，其他待处理项仍须处理。</p>
      <details><summary>确切设定与需求依据</summary><p>设定修订：{{state.context.settingRevisionId}}</p><p>需求：{{state.context.requirementKey}}</p><p>运行计划摘要：{{state.context.runPlanSha256}}</p></details>
      <p v-if="state.context.reason" role="status">{{state.context.reason}}</p>
      <template v-if="state.context.canSelect && !state.receipt && !state.unknown">
        <h4>1. 选择本外观的素材系列</h4>
        <p>只列已关联本外观的系列，不按名称或最新版本自动选择。变体父系列会在提交时核对。</p>
        <button type="button" class="button ghost small" :disabled="!editable || state.seriesLoading" @click="flow.loadSeries()">刷新匹配系列</button>
        <p v-if="state.seriesLoading" role="status">正在读取匹配系列…</p>
        <template v-if="state.directory">
          <p>匹配系列 {{state.directory.count}} 个 · 第 {{state.page}} 页</p>
          <ul><li v-for="series in state.directory.results" :key="series.id"><span>{{series.name}} · {{series.version_count}} 个版本</span><button type="button" class="button ghost small" :disabled="!editable || state.seriesLoading" :aria-pressed="state.series?.id===series.id" @click="chooseSeries(series)">选用此系列</button></li></ul>
          <p v-if="!state.directory.results.length">尚无匹配系列。请到素材库“主图、变体与版本”创建系列，并在关系管理中关联本外观，再返回刷新；不会暗建或改绑其他系列。</p>
          <nav aria-label="履约系列分页"><button class="button ghost small" :disabled="!editable || state.seriesLoading || !state.directory.previous" @click="flow.loadSeries(state.page-1)">上一页系列</button><button class="button ghost small" :disabled="!editable || state.seriesLoading || !state.directory.next" @click="flow.loadSeries(state.page+1)">下一页系列</button></nav>
        </template>
        <RouterLink :to="{name:'assets'}" target="_blank" rel="noopener">打开素材库管理系列与 Bible 关联（新页）</RouterLink>
        <template v-if="state.series">
          <h4>2. 选择成图</h4><p>目标系列：{{state.series.name}}</p>
          <section aria-label="选择原素材版本">
            <h5>复用此系列的确切版本</h5>
            <p>保留原来源、设定与生成依据，不重新关联素材。选择后仍需确认本轮需求。</p>
            <button type="button" class="button ghost small" :disabled="!editable" @click="flow.loadVersions(state.versionPage)">刷新版本目录</button>
            <p v-if="state.versionsLoading" role="status">正在读取版本目录…</p>
            <p v-if="state.choosingVersion" role="status">正在核对确切版本与原素材…</p>
            <p v-if="state.versionsError" role="alert">{{state.versionsError}}</p>
            <template v-if="state.versions">
              <p>共 {{state.versions.count}} 个版本 · 第 {{state.versionPage}} 页；不匹配本轮需求的版本不能选用。</p>
              <ul><li v-for="version in state.versions.results" :key="version.id"><span>v{{version.version}} · {{version.asset.name}} · {{sourceNames[version.source]}}<small v-if="flow.versionReason(version)"> · {{flow.versionReason(version)}}</small></span><button type="button" class="button ghost small" :disabled="!editable || !!flow.versionReason(version)" :aria-pressed="state.version?.id===version.id" @click="flow.chooseVersion(version)">选用版本 v{{version.version}}</button></li></ul>
              <p v-if="!state.versions.results.length">本系列尚无版本；可以使用下方原素材选择或上传入口。</p>
              <nav aria-label="履约版本分页"><button type="button" class="button ghost small" :disabled="!editable || !state.versions.previous" @click="flow.loadVersions(state.versionPage-1)">上一页版本</button><button type="button" class="button ghost small" :disabled="!editable || !state.versions.next" @click="flow.loadVersions(state.versionPage+1)">下一页版本</button></nav>
            </template>
          </section>
          <h5>或选择素材建立新关联</h5>
          <ExecutionInputAssetPicker :key="state.context.unitKey + ':' + state.context.sourceMode" kind="image" purpose="本轮需求成图（不提交模型参考输入）"
            :disabled="!editable" :allow-library="state.context.sourceMode !== 'upload'" :allow-upload="state.context.sourceMode !== 'existing'"
            @selected="flow.chooseAsset" @busy="pickerBusy=$event"/>
          <template v-if="state.asset"><p>已选素材：{{state.asset.name}} · {{state.version ? `原版本 v${state.version.version} / ${sourceNames[state.version.source]}` : state.source === 'upload' ? '本次上传' : '已有素材'}}。尚未记录为本轮结果。</p><p v-if="state.version">确切版本：{{state.version.id}}<span v-if="state.version.generated_from_id"> · 原生成主图依据：{{state.version.generated_from_id}}</span></p><div class="selected-preview"><AssetMediaPreview :asset="state.asset"/></div></template>
          <button type="button" class="button primary" :disabled="!editable || !state.asset || (!state.version && !state.source) || state.seriesLoading" @click="submit">确认此图满足本轮需求（不是审核）</button>
        </template>
      </template>
    </template>
    <p v-if="state.submitting" role="status">正在保存本轮选择…</p>
    <section v-if="state.receipt" aria-label="素材履约已保存" role="status"><strong>服务端已保存本轮选择，不等于阶段审核通过。</strong><p>素材：{{state.receipt.assetId}}</p><p>确切版本：{{state.receipt.referenceVersionId}}</p><RouterLink :to="{name:'assets',query:{asset:state.receipt.assetId}}">查看原素材</RouterLink></section>
  </section>
</template>
<style scoped>
.material-fulfillment{display:grid;gap:12px;padding:16px;border:1px solid var(--border);border-radius:8px;min-width:0}.material-fulfillment h3,.material-fulfillment h4,.material-fulfillment p{margin:0}.material-fulfillment p,.material-fulfillment li{font-size:12px;line-height:1.7;overflow-wrap:anywhere}.material-fulfillment ul{list-style:none;padding:0;margin:0}.material-fulfillment li,.material-fulfillment nav{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}.material-fulfillment li{padding:8px 0;border-bottom:1px solid var(--border)}.material-fulfillment button{min-height:36px;white-space:normal}.material-fulfillment summary{cursor:pointer}.selected-preview{max-width:240px;max-height:180px;overflow:hidden}.material-fulfillment [role=alert]{color:var(--danger)}@media(max-width:640px){.material-fulfillment{padding:10px}.material-fulfillment li{align-items:start;flex-direction:column}}
</style>
