<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { Asset } from '@/types'
import type { JsonObject, ProductionRun } from '@/services/platformTypes'
import { manualScopeLabels as scopeLabels, type ManualActionOptions, type ManualScopeKind, type ManualTarget } from '@/services/productionManual'
import { buildManualAssetAdoption, manualMediaAdoptionResult, readManualMediaReview, type ManualMediaReview } from '@/services/productionManualMedia'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import ExecutionInputAssetPicker from './ExecutionInputAssetPicker.vue'
import AssetMediaPreview from './AssetMediaPreview.vue'

const props = defineProps<{run:ProductionRun;options:ManualActionOptions;disabled?:boolean}>()
const emit = defineEmits<{busy:[value:boolean];changed:[]}>()
const mediaKind = ref<'image'|'video'|'audio'>('image'), scopeKind = ref<ManualScopeKind>('project')
const targetKey = ref(''), targets = ref<ManualTarget[]>([]), page = ref(1), hasNext = ref(false)
const target = computed(()=>targets.value.find(row=>row.unitKey===targetKey.value))
const asset = ref<Asset|null>(null), selection = ref<ManualMediaReview|null>(null), reviewed = ref(false)
const loading = ref(false), submitting = ref(false), uploadBusy = ref(false), error = ref(''), notice = ref('')
const busy = computed(()=>loading.value || submitting.value || uploadBusy.value)
const pending = ref<JsonObject|null>(null), key = ref(crypto.randomUUID())
const previewError = ref('')
const locked = computed(()=>Boolean(props.disabled) || busy.value || Boolean(pending.value))
const current = computed(()=>selection.value?.items.find(row=>row.unitKey===target.value?.unitKey))
let sequence = 0
watch(busy,value=>emit('busy',value))
onBeforeUnmount(()=>{sequence++;emit('busy',false)})
function changedInput(){reviewed.value=false;notice.value=''}
function chooseAsset(value:Asset){asset.value=value;previewError.value='';changedInput()}
async function load(next=1){
  if(props.disabled || busy.value)return
  const token=++sequence;loading.value=true;error.value='';selection.value=null;reviewed.value=false;targets.value=[];targetKey.value=''
  try{
    const [directory,raw]=await Promise.all([platformApi.listProductionTargets(props.run.id,scopeKind.value,next),platformApi.getManualMediaReview(props.run.id)])
    if(token!==sequence)return
    if(directory.runId!==props.run.id || directory.runPlanSha256!==props.options.runPlanSha256 || directory.results.some(row=>row.scope.kind!==scopeKind.value))throw new Error('制作对象目录与本轮计划不一致')
    selection.value=readManualMediaReview(raw,props.run);targets.value=directory.results;page.value=next;hasNext.value=directory.next!==null
    pending.value=null;key.value=crypto.randomUUID()
  }catch(cause){if(token===sequence)error.value=apiErrorSummary(cause)}
  finally{if(token===sequence)loading.value=false}
}
async function adopt(){
  if(props.disabled || busy.value || (!pending.value && (!asset.value || !target.value || !selection.value || !reviewed.value)))return
  submitting.value=true;error.value='';notice.value=''
  const token=sequence
  try{
    pending.value??=buildManualAssetAdoption(asset.value!,mediaKind.value,target.value!,selection.value!,props.run,props.options,key.value)
    const result=await platformApi.performManualProductionAction(props.run.id,pending.value)
    manualMediaAdoptionResult(result)
    if(token!==sequence)return
    pending.value=null;selection.value=null;reviewed.value=false;key.value=crypto.randomUUID()
    notice.value='已记录素材来源并采用；没有模型调用，也没有自动审核或装配。再次操作请重新读取状态。';emit('changed')
  }catch(cause){if(token===sequence)error.value=apiErrorSummary(cause)}
  finally{submitting.value=false}
}
</script>

<template>
  <section class="asset-adoption">
    <h4>从素材库直接采用</h4>
    <p>不需要先生成。先选本轮制作对象，再选择已有媒体或上传入库；采用后保留独立文件副本和原素材来源。用于成片镜头的素材请选择对应镜头，不会按名称猜测归属。</p>
    <div v-if="error" class="platform-alert danger" role="alert">{{error}}</div><p v-if="notice" role="status">{{notice}}</p>
    <fieldset :disabled="locked">
      <label>制作对象类型<select v-model="scopeKind" @change="selection=null;targets=[];targetKey='';changedInput()"><option v-for="kind in options.actions.find(row=>row.stageKey==='media_generation' && row.action==='adopt')?.scopeKinds" :key="kind" :value="kind">{{scopeLabels[kind]}}</option></select></label>
      <label>媒体类型<select v-model="mediaKind" @change="asset=null;changedInput()"><option value="image">图片</option><option value="video">视频</option><option value="audio">音频</option></select></label>
    </fieldset>
    <div class="asset-actions"><button type="button" class="button ghost small" :disabled="disabled || busy" @click="load(1)">重新读取对象与采用状态</button><button type="button" class="button ghost small" :disabled="locked || page<=1" @click="load(page-1)">上一页</button><button type="button" class="button ghost small" :disabled="locked || !hasNext" @click="load(page+1)">下一页</button></div>
    <label v-if="selection">明确制作对象 · 第 {{page}} 页<select v-model="targetKey" :disabled="locked" @change="changedInput"><option value="">请选择</option><option v-for="row in targets" :key="row.unitKey" :value="row.unitKey">{{row.label}}</option></select></label>
    <p v-if="target && selection">{{current ? `将替换此对象的当前采用：${current.id}（修订 ${current.revision}）` : '此对象尚未采用媒体'}}。旧候选保留，已有审核会显示需重新核对。</p>
    <ExecutionInputAssetPicker :kind="mediaKind" :disabled="locked || !target || !selection" @selected="chooseAsset" @busy="uploadBusy=$event"/>
    <p v-if="asset">待采用：{{asset.name}} · {{asset.mimeType}} · 素材 {{asset.id}}</p>
    <div v-if="asset && (asset.mediaUrl || asset.thumbnail)" class="source-preview"><AssetMediaPreview :asset="asset" controls @source-error="previewError='当前素材预览失败，请重新选择素材或到素材库核对文件';reviewed=false" @source-loaded="previewError=''"/></div>
    <p v-else-if="asset">当前素材没有可用的预览链接，请先到素材库核对内容；缺少预览不代表文件已验收。</p>
    <p v-if="previewError" class="platform-alert danger" role="alert">{{previewError}}</p>
    <label><input v-model="reviewed" type="checkbox" :disabled="locked || !asset || !target || !selection">我已核对素材内容、使用权限及对应制作对象</label>
    <button type="button" class="button primary small" :disabled="disabled || busy || (!pending && (!reviewed || !asset || !target || !selection))" @click="adopt">{{submitting ? '正在归档并采用…' : pending ? '重试同一采用请求' : '采用此素材'}}</button>
  </section>
</template>

<style scoped>
.asset-adoption{display:grid;gap:12px;border-top:1px solid var(--border);padding-top:16px}.asset-adoption p{font-size:13px;line-height:1.6;overflow-wrap:anywhere}.asset-adoption fieldset{border:0;padding:0;display:flex;gap:12px;flex-wrap:wrap}.asset-adoption label{display:grid;gap:8px}.asset-actions{display:flex;gap:8px;flex-wrap:wrap}.source-preview{max-height:420px;overflow:auto}.source-preview :deep(img),.source-preview :deep(video){max-width:100%;max-height:400px;object-fit:contain}
</style>
