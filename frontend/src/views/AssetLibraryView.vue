<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { AlertTriangle, ArrowUpDown, Box, Download, FileText, Image, Layers3, Music2, Pencil, Plus, RefreshCw, Save, Search, Trash2, UserRound, Video, Workflow as WorkflowIcon, X } from 'lucide-vue-next'
import { useStudioStore } from '@/stores/studio'
import { useAuthStore } from '@/stores/auth'
import type { Asset, AssetGenerationJob, AssetGenerationMediaType, AssetType, BibleDraftEntityType } from '@/types'
import BaseModal from '@/components/BaseModal.vue'
import VisualStyleManager from '@/components/VisualStyleManager.vue'
import AssetMediaPreview from '@/components/AssetMediaPreview.vue'
import AssetExecutionCreateForm from '@/components/AssetExecutionCreateForm.vue'
import AssetReferenceIngest from '@/components/AssetReferenceIngest.vue'
import AssetReferenceBrowser from '@/components/AssetReferenceBrowser.vue'
import AssetBatchDelete from '@/components/AssetBatchDelete.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import ConfigurationAuthoringShell from '@/components/ConfigurationAuthoringShell.vue'
import ConfigurationDiscardNotice from '@/components/ConfigurationDiscardNotice.vue'
import AssetLibraryFacts from '@/components/AssetLibraryFacts.vue'
import { authoringIssues, isExplicitAuthoringRejection, type AuthoringIssue } from '@/services/configurationAuthoring'
import { repository } from '@/services/repository'

type MediaFilter = '' | AssetGenerationMediaType
type ImageCategory = BibleDraftEntityType | 'reference'
const studio = useStudioStore(); const auth = useAuthStore(); const route = useRoute(); const router = useRouter()
const showStyleManager = ref(false)
const showReferenceIngest = ref(false)
const showReferenceBrowser = ref(false)
const referenceBrowserRevision = ref(0)
const selectedStyle = ref(typeof route.query.style === 'string' ? route.query.style : '')
const selectedMedia = ref<MediaFilter>(['image','video','audio','script'].includes(String(route.query.type || '')) ? route.query.type as MediaFilter : '')
const selectedImageCategory = ref<''|ImageCategory>(''); const search = ref(''); const selected = ref<Asset | null>(null)
const loading = ref(false); const loadError = ref(''); const sortNewest = ref(true)
const batchIds = ref<string[]>([])
const toggleBatch = (id: string) => { batchIds.value = batchIds.value.includes(id) ? batchIds.value.filter(value => value !== id) : [...batchIds.value, id] }
const afterBatchDelete = async () => { batchIds.value = []; selected.value = null; studio.notify('所选素材已删除'); await refresh() }
watch([selectedStyle, selectedMedia, selectedImageCategory, search], () => { batchIds.value = [] })
const mediaTabs: Array<{ value: MediaFilter; label: string; icon: typeof Layers3 }> = [
  { value: '', label: '全部', icon: Layers3 }, { value: 'image', label: '图片', icon: Image },
  { value: 'video', label: '视频', icon: Video }, { value: 'audio', label: '音频', icon: Music2 },
  { value: 'script', label: '剧本', icon: FileText },
]
const imageKinds: Array<{value:ImageCategory;label:string}> = [
  {value:'character',label:'人物'}, {value:'scene',label:'场景'}, {value:'prop',label:'道具'}, {value:'reference',label:'通用'},
]
const style = computed(() => studio.styles.find((item) => item.id === selectedStyle.value))
const isImageAsset = (asset:Asset) => ['character','scene','prop','image'].includes(asset.type)
const filteredAssets = computed(() => {
  let rows = [...studio.assets]
  if (selectedMedia.value === 'image') rows = rows.filter(isImageAsset)
  else if (selectedMedia.value) rows = rows.filter((asset) => asset.type === selectedMedia.value)
  if (selectedMedia.value === 'image' && selectedImageCategory.value) {
    rows = rows.filter((asset) => selectedImageCategory.value === 'reference' ? asset.type === 'image' : asset.type === selectedImageCategory.value)
  }
  return rows.sort((left,right) => {
    const value = String(right.updatedAt || '').localeCompare(String(left.updatedAt || ''))
    return sortNewest.value ? value : -value
  })
})
let refreshTimer = 0
const refresh = async () => {
  loading.value = true; loadError.value = ''
  try { await studio.refreshAssets({ style: selectedStyle.value || undefined, search: search.value || undefined }) }
  catch(error){loadError.value=error instanceof Error?error.message:'素材列表加载失败'}
  finally { loading.value = false }
}
watch([selectedStyle, search], () => {
  window.clearTimeout(refreshTimer); refreshTimer = window.setTimeout(refresh, 220)
  router.replace({ query: { ...(selectedStyle.value?{style:selectedStyle.value}:{}), ...(selectedMedia.value?{type:selectedMedia.value}:{}) } })
})
watch(selectedMedia,()=>{selectedImageCategory.value='';router.replace({ query: { ...(selectedStyle.value?{style:selectedStyle.value}:{}), ...(selectedMedia.value?{type:selectedMedia.value}:{}) } })})
onBeforeUnmount(()=>{window.clearTimeout(refreshTimer);if(uploadPreview.value.startsWith('blob:'))URL.revokeObjectURL(uploadPreview.value)})

const typeLabel = (type: AssetType) => ({character:'人物图片',scene:'场景图片',prop:'道具图片',image:'通用图片',video:'视频',audio:'音频',script:'剧本'}[type])
const typeIcon = (type: AssetType) => ({character:UserRound,scene:Image,prop:Box,image:Image,video:Video,audio:Music2,script:FileText}[type]||Layers3)
const assetAccent = (asset: Asset) => studio.styles.find((item) => item.id === asset.styleId)?.accent
const isClassifiedImageAsset = (asset: Asset) => ['character','scene','prop'].includes(asset.type)
const assetDescription = (asset: Asset) => asset.description.trim() || (isClassifiedImageAsset(asset) ? '基础描述缺失，素材信息不完整' : '尚未填写素材说明')
const assetSourceInfo = (asset: Asset) => `${asset.sourceInfo?.trim() || '来源未记录（历史素材）'}${asset.metadata?.cleared_history_origin ? '（原业务来源历史已清理；素材原字节保留）' : ''}`

const assetModalOpen=ref(false);const assetSaving=ref(false);const assetError=ref('');const uploadInput=ref<HTMLInputElement|null>(null);const uploadPreview=ref('');const fullPreview=ref<Asset|null>(null)
const deleteTarget=ref<Asset|null>(null);const deleteConfirmName=ref('');const deletePending=ref(false);const deleteError=ref('')
const generationBusy=ref(false),generationDirty=ref(false),generationLocked=ref(false),promptCandidate=ref('')
const generationUnknown=ref(false)
const assetOutcomeUnknown=ref(false), assetPhase=ref(''), assetIssues=ref<AuthoringIssue[]>([])
const recoveryLoading=ref(false), recoveryRows=ref<Asset[]>([]), recoveryError=ref(''), recoveryPage=ref(1), recoveryNext=ref(false), recoveryPrevious=ref(false)
const editorLocked=computed(()=>assetSaving.value||generationLocked.value||assetOutcomeUnknown.value||recoveryLoading.value||!!discardAction.value)
const baseline=ref(''), discardAction=ref<null|(()=>void)>(null), discardMessage=ref('')
const formStamp=()=>JSON.stringify({...assetForm.value,file:assetForm.value.file ? [assetForm.value.file.name,assetForm.value.file.size,assetForm.value.file.lastModified] : null})
const assetDirty=computed(()=>assetModalOpen.value&&(formStamp()!==baseline.value||generationDirty.value))
const authorStatus=computed(()=>generationUnknown.value?'生成请求结果未知；请在下方只读核对原请求，勿重复创建。':assetPhase.value||(assetDirty.value?'有未提交输入；关闭或刷新前请保留':editingAssetId.value?'编辑素材自有信息；原文件与来源不变':'尚未提交；上传和生成是不同操作'))
const sections=computed(()=>[
  {key:'content',title:'内容与获取方式',description:'上传已有文件或引用执行实例生成；这两条路径不会互相替代。',summary:editingAssetId.value?'保留原文件':assetForm.value.entryMode==='local'?'本地文件入库':'生成任务'},
  {key:'metadata',title:'素材信息',description:'名称、描述与风格属于这份素材，不是全局字段定义。',summary:assetForm.value.name||'尚未填写名称'},
  {key:'result',title:assetForm.value.entryMode==='generate'?'生成输入与结果':'复核与使用',description:assetForm.value.entryMode==='generate'?'复用执行实例的正式创建入口；创建任务不等于生成成功。':'保存后可由新任务引用；不创建配置版本，不改写系统来源。'},
])
function initializeAuthor(){baseline.value=formStamp();generationUnknown.value=false;assetOutcomeUnknown.value=false;assetPhase.value='';assetIssues.value=[];discardAction.value=null;recoveryRows.value=[];recoveryError.value=''}
function requestDiscard(action:()=>void,message:string){if(discardAction.value)return;if(assetDirty.value||assetOutcomeUnknown.value||generationDirty.value){discardMessage.value=message;discardAction.value=action}else action()}
function resolveDiscard(discard:boolean){const action=discardAction.value;discardAction.value=null;if(discard)action?.()}
const beforeUnload=(event:BeforeUnloadEvent)=>{if(assetModalOpen.value&&(assetDirty.value||editorLocked.value)){event.preventDefault();event.returnValue=''}}
onMounted(()=>window.addEventListener('beforeunload',beforeUnload))
onBeforeUnmount(()=>window.removeEventListener('beforeunload',beforeUnload))
let resolveLeave:((value:boolean)=>void)|null=null
function canLeave(){if(!assetModalOpen.value)return true;if(generationUnknown.value){assetPhase.value='生成请求结果未知，请在下方只读核对原请求；当前表单和请求键保留。';return false}if(assetSaving.value||generationBusy.value||recoveryLoading.value||discardAction.value)return false;if(!assetDirty.value&&!assetOutcomeUnknown.value)return true;return new Promise<boolean>(resolve=>{resolveLeave=resolve;requestDiscard(()=>{resolveLeave=null;resetAssetModal();resolve(true)},'离开会丢失当前未提交内容；结果未知的上传请先在素材库核对，不要重复提交。')})}
onBeforeRouteLeave(canLeave)
onBeforeUnmount(()=>{resolveLeave?.(false);resolveLeave=null})
function keepEditing(){resolveDiscard(false);resolveLeave?.(false);resolveLeave=null}
const blankAssetForm=()=>({
  entryMode:'local' as 'local'|'generate',mediaType:'image' as AssetGenerationMediaType,
  file:null as File|null,imageCategory:'character' as ImageCategory,name:'',description:'',promptHint:'',styleId:studio.styles.find(item=>item.id===selectedStyle.value&&item.isActive)?.id||'',
})
const assetForm=ref(blankAssetForm());const editingAssetId=ref('');const editingAsset=ref<Asset|null>(null);const assetMediaTypeFixed=ref(false)
const canEditAsset=(asset:Asset)=>Boolean(asset.ownerId&&(auth.user?.isStaff||String(asset.ownerId)===String(auth.user?.id)))
const canDeleteAsset=(asset:Asset)=>canEditAsset(asset)||auth.user?.isSuperuser===true
const assetMediaType=(asset:Asset):AssetGenerationMediaType=>isImageAsset(asset)?'image':asset.type as AssetGenerationMediaType
const editableAssetKind=(asset:Asset):ImageCategory=>['character','scene','prop'].includes(asset.type)?asset.type as BibleDraftEntityType:'reference'
const revokeUploadPreview=()=>{if(uploadPreview.value.startsWith('blob:'))URL.revokeObjectURL(uploadPreview.value);uploadPreview.value=''}
const openCreateAsset=(mode:'local'|'generate'='local',mediaType?:AssetGenerationMediaType)=>{revokeUploadPreview();assetMediaTypeFixed.value=mediaType!==undefined;assetForm.value={...blankAssetForm(),entryMode:mode,mediaType:mediaType||'image'};editingAssetId.value='';editingAsset.value=null;assetError.value='';promptCandidate.value='';assetModalOpen.value=true;initializeAuthor()}
const openCreateAssetFromCurrentTab=()=>openCreateAsset('local',selectedMedia.value||undefined)
const openEditAsset=(asset:Asset)=>{if(!canEditAsset(asset))return;revokeUploadPreview();assetMediaTypeFixed.value=true;assetForm.value={...blankAssetForm(),mediaType:assetMediaType(asset),imageCategory:editableAssetKind(asset),name:asset.name,description:asset.description,promptHint:asset.promptHint||'',styleId:asset.styleId};editingAssetId.value=asset.id;editingAsset.value=asset;assetError.value='';assetModalOpen.value=true;selected.value=null;initializeAuthor()}
const resetAssetModal=()=>{if(editingAsset.value)selected.value=editingAsset.value;revokeUploadPreview();assetModalOpen.value=false;editingAssetId.value='';editingAsset.value=null;assetError.value='';generationBusy.value=false;generationDirty.value=false;generationLocked.value=false;promptCandidate.value='';assetOutcomeUnknown.value=false;assetPhase.value='';assetIssues.value=[];discardAction.value=null}
const closeAssetModal=()=>{
  if(assetSaving.value||generationBusy.value||recoveryLoading.value)return
  if(generationUnknown.value){assetPhase.value='生成请求结果未知，请在下方只读核对原请求；关闭不会取消任务，当前表单和请求键保留。';return}
  requestDiscard(resetAssetModal,assetOutcomeUnknown.value?'本次结果未知。关闭不会取消已提交请求；请先只读核对素材库，确认前不要重复上传。':'关闭会丢弃未提交的素材信息和生成输入；已保存的文件不会删除。')
}
const changeEntryMode=(mode:'local'|'generate')=>{
  if(editorLocked.value||assetForm.value.entryMode===mode)return
  if(generationDirty.value){requestDiscard(()=>{generationDirty.value=false;assetForm.value.entryMode=mode},'切换获取方式会清空未提交的生成输入，素材信息保留。');return}
  assetForm.value.entryMode=mode
}
const changeAssetMedia=(media:AssetGenerationMediaType)=>{
  if(editorLocked.value||assetForm.value.mediaType===media)return
  const change=()=>{generationDirty.value=false;assetForm.value.mediaType=media}
  if(generationDirty.value||assetForm.value.file){requestDiscard(change,'切换媒体类型会清空已选文件和未提交的生成输入，素材信息保留。');return}change()
}
async function readUnknown(next=1){
  if(recoveryLoading.value||!assetOutcomeUnknown.value)return
  recoveryLoading.value=true;recoveryError.value='';recoveryRows.value=[]
  try{
    if(editingAssetId.value){recoveryRows.value=[await studio.refreshAsset(editingAssetId.value,false)];recoveryNext.value=false;recoveryPrevious.value=false}
    else{const result=await repository.getAssetPage(next,assetForm.value.name.trim());recoveryRows.value=result.results;recoveryNext.value=!!result.next;recoveryPrevious.value=!!result.previous}
    recoveryPage.value=next
  }catch(cause){recoveryError.value=apiErrorSummary(cause)}finally{recoveryLoading.value=false}
}
async function inspectRecovered(asset:Asset){
  if(recoveryLoading.value)return
  recoveryLoading.value=true;recoveryError.value=''
  try{const current=await studio.refreshAsset(asset.id,false);resetAssetModal();selected.value=current}
  catch(cause){recoveryError.value=apiErrorSummary(cause)}finally{recoveryLoading.value=false}
}
const assetModalTitle=computed(()=>editingAssetId.value?'编辑素材信息':assetMediaTypeFixed.value?`新增${{image:'图片',video:'视频',audio:'音频',script:'剧本'}[assetForm.value.mediaType]}素材`:'新增素材')
const assetUploadAccept={image:'image/jpeg,image/png,image/webp',video:'video/mp4,video/quicktime,video/webm',audio:'audio/wav,audio/mpeg,audio/flac,audio/mp4,audio/ogg',script:'.txt,.md,.markdown,.fountain,.docx,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document'} as const
const acceptForType=computed(()=>assetUploadAccept[assetForm.value.mediaType])
const uploadLabel=computed(()=>({image:'JPEG / PNG / WebP',video:'MP4 / MOV / WebM',audio:'WAV / MP3 / FLAC / M4A / OGG',script:'TXT / Markdown / Fountain / DOCX'}[assetForm.value.mediaType]))
const selectUploadFile=(event:Event)=>{if(editorLocked.value)return;const file=(event.target as HTMLInputElement).files?.[0]||null;assetForm.value.file=file;revokeUploadPreview();uploadPreview.value=file?URL.createObjectURL(file):'';if(file&&!assetForm.value.name.trim())assetForm.value.name=file.name.replace(/\.[^.]+$/,'')}
watch(()=>assetForm.value.mediaType,()=>{if(editingAssetId.value)return;assetForm.value.file=null;revokeUploadPreview()})
const generationCreated=async(job:AssetGenerationJob)=>{
  generationUnknown.value=false
  resetAssetModal()
  studio.notify(job.status==='preparing'?'任务已创建，请在准备工作台查看输入与准入状态':'任务已创建，请查看实际执行状态','info')
  await router.push({name:'workflows',query:{category:'asset',source_id:job.id}})
}
const saveAsset=async()=>{
  if(editorLocked.value||assetForm.value.entryMode!=='local')return
  const form=assetForm.value
  assetIssues.value=[];assetError.value='';assetPhase.value=''
  if(!form.name.trim()){assetIssues.value=[{field:'name',message:'请填写素材名称',section:'metadata'}];return}
  if(form.mediaType==='image'&&form.imageCategory!=='reference'&&(!form.description.trim()||!form.promptHint.trim())){assetIssues.value=[{field:!form.description.trim()?'description':'prompt_hint',message:'人物、场景和道具图片必须填写基础描述与视觉锚点',section:'metadata'}];return}
  if(!editingAssetId.value&&!form.file){assetIssues.value=[{field:'file',message:`请选择 ${uploadLabel.value} 文件`,section:'content'}];return}
  assetSaving.value=true;assetPhase.value=editingAssetId.value?'正在保存素材信息…':'正在上传文件并等待入库结果…'
  try{
    let saved:Asset
    if(editingAssetId.value){
      const original=editingAsset.value
      const mutableKind=original&&['character','scene','prop','reference'].includes(original.backendKind||'')?form.imageCategory:undefined
      saved=await studio.updateAssetMetadata(editingAssetId.value,{name:form.name,description:form.description,promptHint:form.promptHint,kind:mutableKind,styleId:form.styleId||undefined})
    }else{
      saved=await studio.createAsset({file:form.file!,mediaType:form.mediaType,imageCategory:form.imageCategory,name:form.name,description:form.description,promptHint:form.promptHint,styleId:form.styleId||undefined})
    }
    resetAssetModal();await refresh();selected.value=studio.assets.find((asset)=>asset.id===saved.id)||saved
  }catch(error){
    assetIssues.value=authoringIssues(error,{file:'content',kind:'metadata',name:'metadata',description:'metadata',prompt_hint:'metadata',style:'metadata'})
    assetOutcomeUnknown.value=!isExplicitAuthoringRejection(error)
    assetError.value=assetOutcomeUnknown.value?'未收到可确认的保存回执。不要重复提交；请先只读核对当前素材库。':assetIssues.value.length?'':apiErrorSummary(error)
    assetPhase.value=assetOutcomeUnknown.value?'提交结果未知，已停止重复提交；请先只读核对。':'保存被拒绝，已填内容保留，可修正后提交。'
  }finally{assetSaving.value=false}
}
const applyPromptResult=(job:AssetGenerationJob)=>{
  if(job.mediaType==='text')throw new Error('字段子任务不能作为完整提示词候选')
  openCreateAsset('generate',job.mediaType);promptCandidate.value=job.resultText;assetForm.value.styleId=job.styleId||selectedStyle.value
}
const queryValue=(value:unknown)=>String(Array.isArray(value)?value[0]||'':value||'')
const baseAssetQuery=()=>({...(selectedStyle.value?{style:selectedStyle.value}:{}),...(selectedMedia.value?{type:selectedMedia.value}:{})})
let contextSequence=0
onBeforeUnmount(()=>{contextSequence++})
const consumeRouteContext=async()=>{
  const serial=++contextSequence
  const promptJobId=queryValue(route.query.promptJob);const assetId=queryValue(route.query.asset)
  if(!promptJobId&&!assetId)return
  const current=()=>serial===contextSequence&&queryValue(route.query.promptJob)===promptJobId&&queryValue(route.query.asset)===assetId
  showReferenceBrowser.value=false
  try{
    if(promptJobId){
      const jobs=await studio.refreshAssetGenerationJobs();const job=jobs.find((item)=>item.id===promptJobId)
      if(!current())return
      if(job?.jobType==='prompt'&&job.status==='succeeded'&&job.resultText){
        if(editorLocked.value){studio.notify('当前表单仍在处理或核对请求，未替换输入；该候选保留在任务中心。','info');return}
        requestDiscard(()=>applyPromptResult(job),'使用此候选将打开对应媒体的生成表单，并放弃当前未提交输入；候选正文仍需明确选择通道后采用。')
      }
      else studio.notify('提示词任务尚未成功，已保留在任务中心继续跟踪。','info')
    }else if(assetId){
      const asset=studio.assets.find((asset)=>asset.id===assetId)||await studio.refreshAsset(assetId,false)
      if(current())selected.value=asset
    }
  }catch(error){if(current())studio.notify(error instanceof Error?error.message:'无法打开指定素材任务结果','error')}
  finally{if(current())await router.replace({name:'assets',query:baseAssetQuery()})}
}
onMounted(async()=>{await refresh();await consumeRouteContext()})
watch(()=>[route.query.asset,route.query.promptJob],()=>{void consumeRouteContext()})
type AssetPreviewEvent={assetId:string;source:string}
const previewRecoveryAttempts=new Map<string,Set<string>>();const previewRecoveryInFlight=new Set<string>()
const assetPreviewSource=(asset:Asset|undefined|null)=>asset?.mediaUrl||asset?.thumbnail||''
const currentPreviewAsset=(assetId:string)=>selected.value?.id===assetId?selected.value:fullPreview.value?.id===assetId?fullPreview.value:editingAsset.value?.id===assetId?editingAsset.value:studio.assets.find((asset)=>asset.id===assetId)
const clearPreviewRecovery=({assetId,source}:AssetPreviewEvent)=>{if(previewRecoveryInFlight.has(assetId)||assetPreviewSource(currentPreviewAsset(assetId))!==source)return;previewRecoveryAttempts.delete(assetId)}
const recoverAssetPreview=async({assetId,source}:AssetPreviewEvent)=>{
  if(!assetId||!source||previewRecoveryInFlight.has(assetId))return
  const currentSource=assetPreviewSource(currentPreviewAsset(assetId));if(currentSource&&currentSource!==source)return
  const attempts=previewRecoveryAttempts.get(assetId)||new Set<string>();if(attempts.has(source))return;attempts.add(source);previewRecoveryAttempts.set(assetId,attempts);previewRecoveryInFlight.add(assetId)
  try{
    const mergeIntoList=studio.assets.some((asset)=>asset.id===assetId);const fresh=await studio.refreshAsset(assetId,mergeIntoList);const freshSource=assetPreviewSource(fresh)
    if(!freshSource||freshSource===source)throw new Error('服务端未返回新的预览地址')
    attempts.add(freshSource)
    if(selected.value?.id===assetId)selected.value=fresh
    if(fullPreview.value?.id===assetId)fullPreview.value=fresh
    if(editingAsset.value?.id===assetId)editingAsset.value=fresh
  }catch(error){studio.notify(`素材预览地址刷新失败：${error instanceof Error?error.message:'请稍后重试'}`,'error')}
  finally{previewRecoveryInFlight.delete(assetId)}
}
const downloadAsset=async(asset:Asset)=>{try{const fresh=await studio.refreshAsset(asset.id,studio.assets.some((item)=>item.id===asset.id));if(!fresh.downloadUrl)throw new Error('该素材没有可下载的受管文件');const link=document.createElement('a');link.href=fresh.downloadUrl;link.rel='noopener';link.download='';document.body.appendChild(link);link.click();link.remove()}catch(error){studio.notify(error instanceof Error?error.message:'下载失败','error')}}

const openDeleteAsset=(asset:Asset)=>{if(!canDeleteAsset(asset))return;deleteTarget.value=asset;deleteConfirmName.value='';deleteError.value='';selected.value=null}
const closeDeleteAsset=()=>{if(deletePending.value)return;const target=deleteTarget.value;deleteTarget.value=null;deleteConfirmName.value='';deleteError.value='';if(target)selected.value=target}
const deleteConfirmed=computed(()=>Boolean(deleteTarget.value&&deleteConfirmName.value.trim()===deleteTarget.value.name.trim()))
const confirmDeleteAsset=async()=>{if(deletePending.value||!deleteTarget.value||!deleteConfirmed.value)return;const target=deleteTarget.value;deletePending.value=true;deleteError.value='';try{await studio.deleteAsset(target.id);deleteTarget.value=null;deleteConfirmName.value='';selected.value=null;await refresh()}catch(error){deleteError.value=apiErrorSummary(error)}finally{deletePending.value=false}}
</script>

<template>
  <div class="assets-page page-stack">
    <VisualStyleManager v-if="showStyleManager" :open="showStyleManager" @close="showStyleManager=false"/>
    <section class="page-heading"><span class="eyebrow">多模态素材库 / MULTIMODAL ASSET VAULT</span><h2>风格素材库</h2><p>统一管理可下载、可被短剧正式引用的图片、视频、音频与剧本。</p><button class="button ghost" @click="showStyleManager=true">管理视觉风格</button></section>
    <nav class="asset-tabs" aria-label="素材浏览方式"><button :aria-pressed="!showReferenceBrowser" :class="{active:!showReferenceBrowser}" @click="showReferenceBrowser=false">全部素材</button><button :aria-pressed="showReferenceBrowser" :class="{active:showReferenceBrowser}" @click="showReferenceBrowser=true">主图 / 变体关系</button></nav>
    <section v-if="!showReferenceBrowser" class="asset-command-deck panel" aria-label="素材库固定筛选与操作区">
      <div class="asset-dock-primary"><div class="asset-style-row"><span>风格</span><section class="style-picker asset-style-picker">
        <button class="style-picker-all" :class="{active:!selectedStyle}" :aria-pressed="!selectedStyle" @click="selectedStyle=''">全部<br><strong>全部风格</strong></button>
        <button v-for="item in studio.styles" :key="item.id" :class="{active:selectedStyle===item.id}" :aria-pressed="selectedStyle===item.id" :style="{'--style-accent':item.accent,'--style-bg':item.gradient}" @click="selectedStyle=item.id"><i></i><span>{{ item.englishName }}</span><strong>{{ item.name }}{{ item.isActive?'':'（已停用）' }}</strong></button>
      </section></div><div class="asset-dock-actions"><RouterLink class="button ghost" :to="{name:'workflows',query:{category:'asset'}}"><WorkflowIcon :size="15"/>生成任务</RouterLink><button class="button primary" @click="openCreateAssetFromCurrentTab"><Plus :size="15"/>新增素材</button></div></div>
      <section class="asset-toolbar"><div class="asset-tabs"><button v-for="tab in mediaTabs" :key="tab.value" :class="{active:selectedMedia===tab.value}" :aria-pressed="selectedMedia===tab.value" @click="selectedMedia=tab.value"><component :is="tab.icon" :size="15"/>{{ tab.label }}</button></div><div class="toolbar-right"><label class="search-input"><Search :size="16"/><input v-model="search" placeholder="搜索名称、描述、视觉锚点或来源"><button v-if="search" @click="search=''" aria-label="清除"><X :size="14"/></button></label><button class="icon-btn" :disabled="loading" title="刷新素材" aria-label="刷新素材" @click="refresh"><RefreshCw :size="14"/></button></div></section>
      <div class="asset-filter-footer"><div v-if="selectedMedia==='image'" class="asset-subtabs"><button :class="{active:!selectedImageCategory}" :aria-pressed="!selectedImageCategory" @click="selectedImageCategory=''">全部图片</button><button v-for="kind in imageKinds" :key="kind.value" :class="{active:selectedImageCategory===kind.value}" :aria-pressed="selectedImageCategory===kind.value" @click="selectedImageCategory=kind.value">{{ kind.label }}</button></div><div class="result-bar"><span>显示 <strong>{{ filteredAssets.length }}</strong> 项素材</span><button :aria-label="sortNewest?'切换为较早入库优先':'切换为最新入库优先'" @click="sortNewest=!sortNewest"><ArrowUpDown :size="13"/>{{ sortNewest?'最新入库':'较早入库' }}</button></div></div>
    </section>

    <button v-if="!showReferenceIngest" class="button ghost" @click="showReferenceIngest=true">添加主图 / 变体素材</button>
    <AssetReferenceIngest v-if="showReferenceIngest" @close="showReferenceIngest=false" @changed="referenceBrowserRevision++;refresh()"/>
    <AssetReferenceBrowser v-if="showReferenceBrowser" :key="referenceBrowserRevision"/>

    <template v-if="!showReferenceBrowser">
    <section v-if="style" class="style-feature" :style="{background:style.gradient}"><div class="style-feature-pattern"></div><div><span class="eyebrow">{{ style.englishName }}</span><h3>{{ style.name }}</h3><p>{{ style.description }}</p><div class="tag-row"><span v-for="tag in style.tags" :key="tag">{{ tag }}</span></div></div><div class="style-feature-counts"><div><Layers3 :size="18"/><strong>{{ style.assetCount ?? '—' }}</strong><span>素材总数</span></div></div></section>

    <div v-if="loadError" class="asset-load-error" role="alert"><AlertTriangle :size="15"/><span>{{ loadError }}</span><button @click="refresh">重试</button></div>
    <div v-if="filteredAssets.length && !loading"><button class="button ghost" @click="batchIds=filteredAssets.filter(canDeleteAsset).slice(0,200).map(asset=>asset.id)">选择当前结果（最多200项）</button><AssetBatchDelete :assets="filteredAssets" :selected-ids="batchIds" @cleared="batchIds=[]" @deleted="afterBatchDelete"/></div>
    <section v-if="loading" class="asset-grid"><div v-for="n in 12" :key="n" class="asset-card skeleton-card"></div></section>
    <section v-else-if="filteredAssets.length" class="asset-grid"><article v-for="asset in filteredAssets" :key="asset.id" class="asset-card" @click="selected=asset"><label v-if="canDeleteAsset(asset)" class="batch-check" @click.stop><input type="checkbox" :checked="batchIds.includes(asset.id)" :aria-label="`选择素材 ${asset.name}`" @change="toggleBatch(asset.id)">选择</label><div class="asset-image" :style="{'--asset-accent':assetAccent(asset)}"><AssetMediaPreview :asset="asset" compact @source-error="recoverAssetPreview" @source-loaded="clearPreviewRecovery"/><span class="asset-type"><component :is="typeIcon(asset.type)" :size="12"/>{{ typeLabel(asset.type) }}</span><div class="asset-overlay"><button class="button primary small">查看素材</button></div></div><div class="asset-copy"><span>{{ studio.styles.find(s=>s.id===asset.styleId)?.name || '未限定风格' }}</span><h4>{{ asset.name }}</h4><p>{{ asset.description || asset.promptHint || '暂无说明' }}</p><div><span v-for="tag in asset.tags.slice(0,2)" :key="tag">#{{ tag }}</span></div></div></article></section>
    <section v-else class="empty-state"><Search :size="28"/><h3>没有匹配素材</h3><p>尝试更换风格、素材类型或搜索关键词，也可以立即新增。</p><button class="button primary" @click="openCreateAssetFromCurrentTab"><Plus :size="14"/>新增素材</button></section>
    </template>

    <BaseModal :open="!!selected&&!fullPreview" preserve-content configuration title="素材详情" eyebrow="独立素材 · 内容与来源" width="980px" @close="selected=null">
      <div v-if="selected" class="asset-detail-layout">
        <div class="asset-detail-image"><AssetMediaPreview :asset="selected" controls @source-error="recoverAssetPreview" @source-loaded="clearPreviewRecovery"/></div>
        <div class="asset-detail-content"><p class="asset-help">{{ studio.styles.find(s=>s.id===selected?.styleId)?.name || '未限定风格' }} · {{ typeLabel(selected.type) }}</p><h3>{{ selected.name }}</h3><p :class="{'asset-info-missing':!selected.description.trim()}">{{ assetDescription(selected) }}</p>
          <section v-if="isClassifiedImageAsset(selected)||selected.promptHint"><h4>创作备注 / 视觉锚点</h4><p :class="{'asset-info-missing':!selected.promptHint?.trim()}">{{ selected.promptHint?.trim() || '视觉锚点缺失，素材信息不完整' }}</p></section>
          <AssetLibraryFacts :asset="selected"/>
          <p v-if="!canEditAsset(selected)" class="asset-help">此素材对当前账号只读；可在任务中引用或单独下载。</p>
          <div class="asset-detail-actions"><button class="button primary" @click="downloadAsset(selected)"><Download :size="15"/>下载</button><button class="button ghost" @click="fullPreview=selected">完整预览</button><button v-if="canEditAsset(selected)" class="button ghost" @click="openEditAsset(selected)"><Pencil :size="15"/>编辑信息</button></div>
          <details v-if="canDeleteAsset(selected)"><summary>危险操作</summary><p class="asset-help">删除与来源查询分开；现有引用由服务端校验。</p><button class="button danger-ghost" @click="openDeleteAsset(selected)"><Trash2 :size="15"/>删除</button></details>
        </div>
      </div>
    </BaseModal>

    <BaseModal :open="assetModalOpen" configuration :dismissible="!assetSaving&&!generationBusy&&!recoveryLoading" :title="assetModalTitle" eyebrow="独立素材 · 上传 / 生成 / 编辑" width="1080px" @close="closeAssetModal">
      <ConfigurationDiscardNotice v-if="discardAction" :message="discardMessage" :busy="assetSaving||generationBusy||recoveryLoading" action-label="放弃本地内容并继续" @keep="keepEditing" @discard="resolveDiscard(true)"/>
      <ConfigurationAuthoringShell :inert="!!discardAction" label="素材作者" :sections="sections" :busy="assetSaving||generationBusy||recoveryLoading" :issues="assetIssues" :status="authorStatus">
        <template #feedback>
          <p v-if="assetError" class="asset-author-error" role="alert">{{ assetError }}</p>
          <section v-if="assetOutcomeUnknown" class="asset-recovery"><h3>先核对，不重复提交</h3><p>现有接口不提供请求键回执。上传检索只查启用素材，同名结果不能证明是本次请求创建；请查看当前记录核对文件与信息，页面不会自动认定成功。</p><button class="button ghost" :disabled="recoveryLoading" @click="readUnknown()">{{ recoveryLoading?'正在只读核对…':editingAssetId?'读取当前素材':'查询当前素材库' }}</button><p v-if="recoveryError" role="alert">{{ recoveryError }}</p><ul><li v-for="asset in recoveryRows" :key="asset.id"><strong>{{ asset.name }}</strong> · {{ typeLabel(asset.type) }}<code>{{ asset.id }}</code><button class="button ghost" :disabled="recoveryLoading" @click="inspectRecovered(asset)">结束本地编辑并查看当前记录</button></li></ul><nav v-if="!editingAssetId" aria-label="未知上传结果分页"><button class="button ghost" :disabled="recoveryLoading||!recoveryPrevious" @click="readUnknown(recoveryPage-1)">上一页</button><button class="button ghost" :disabled="recoveryLoading||!recoveryNext" @click="readUnknown(recoveryPage+1)">下一页</button></nav><p class="asset-help">以上仅是库内当前事实，不是原提交回执；未查到也不能证明原请求失败。关闭或刷新后仍需人工核对，不能直接重新上传。</p></section>
        </template>
        <template #content>
          <div v-if="!editingAssetId" class="asset-entry-mode"><button :class="{active:assetForm.entryMode==='local'}" :aria-pressed="assetForm.entryMode==='local'" :disabled="editorLocked" @click="changeEntryMode('local')">本地添加</button><button :class="{active:assetForm.entryMode==='generate'}" :aria-pressed="assetForm.entryMode==='generate'" :disabled="editorLocked" @click="changeEntryMode('generate')">模型生成</button></div>
          <div v-if="!editingAssetId&&!assetMediaTypeFixed" class="asset-kind-picker"><button v-for="tab in mediaTabs.slice(1)" :key="tab.value" :class="{active:assetForm.mediaType===tab.value}" :aria-pressed="assetForm.mediaType===tab.value" :disabled="editorLocked" @click="changeAssetMedia(tab.value as AssetGenerationMediaType)"><component :is="tab.icon" :size="16"/>{{ tab.label }}</button></div>
          <label v-if="assetForm.entryMode==='local'&&!editingAssetId" class="field" data-authoring-field="file"><span>选择本地文件 *</span><input ref="uploadInput" type="file" :accept="acceptForType" :disabled="editorLocked" @change="selectUploadFile"><small>{{ uploadLabel }}；预览只是本地内容，点击“上传并入库”才提交。</small></label>
          <div v-if="editingAsset" class="asset-author-preview"><AssetMediaPreview :asset="editingAsset" controls @source-error="recoverAssetPreview" @source-loaded="clearPreviewRecovery"/></div>
          <div v-else-if="uploadPreview&&assetForm.entryMode==='local'" class="asset-author-preview"><img v-if="assetForm.mediaType==='image'" :src="uploadPreview" alt="素材内容预览"><video v-else-if="assetForm.mediaType==='video'" :src="uploadPreview" controls preload="metadata"></video><audio v-else-if="assetForm.mediaType==='audio'" :src="uploadPreview" controls preload="metadata" aria-label="本地音频预览"></audio><div v-else class="asset-file-summary"><strong>{{ assetForm.file?.name }}</strong><p>剧本将在入库时由服务端提取并规范为 UTF-8 Markdown；此处仅确认所选文件，不代表解析或入库成功。</p></div></div>
          <p v-if="editingAssetId">编辑仅保存素材信息，不替换文件或重新生成。</p>
        </template>
        <template #metadata>
          <p v-if="!editingAssetId && selectedStyle && studio.styles.some(item=>item.id===selectedStyle&&!item.isActive)" class="asset-help">当前筛选风格已停用，未带入新素材；可以选择其他启用风格，或保持不限定风格。</p>
          <div class="form-grid two"><label v-if="assetForm.mediaType==='image'" class="field" data-authoring-field="kind"><span>图片类别</span><select v-model="assetForm.imageCategory" :disabled="editorLocked"><option v-for="kind in imageKinds" :key="kind.value" :value="kind.value">{{ kind.label }}</option></select></label><label class="field" data-authoring-field="style"><span>风格体系</span><select v-model="assetForm.styleId" :disabled="editorLocked"><option value="">不限定风格</option><option v-for="item in studio.styles" :key="item.id" :value="item.id" :disabled="!item.isActive">{{ item.name }}{{ item.isActive?'':'（已停用）' }}</option></select></label></div>
          <label class="field" data-authoring-field="name"><span>名称 *</span><input v-model="assetForm.name" :disabled="editorLocked" maxlength="160" placeholder="清晰、可检索的素材名称"></label>
          <label class="field" data-authoring-field="description"><span>基础描述 <template v-if="assetForm.mediaType==='image'&&assetForm.imageCategory!=='reference'">*</template></span><textarea v-model="assetForm.description" :disabled="editorLocked" rows="3" placeholder="主体、用途和需要保持不变的事实"></textarea></label>
          <label v-if="assetForm.entryMode==='local'||editingAssetId" class="field" data-authoring-field="prompt_hint"><span>{{ assetForm.mediaType==='image'?'视觉锚点':'检索/创作备注' }}</span><textarea v-model="assetForm.promptHint" :disabled="editorLocked" rows="4" placeholder="可选；人物、场景和道具图片必须填写"></textarea></label>
        </template>
        <template #result>
          <AssetExecutionCreateForm v-if="assetForm.entryMode==='generate'&&!editingAssetId" :key="assetForm.mediaType" :media-type="assetForm.mediaType" :metadata="assetForm" :prompt-candidate="promptCandidate" @busy="generationBusy=$event" @dirty="generationDirty=$event" @locked="generationLocked=$event" @unknown="generationUnknown=$event" @created="generationCreated"/>
          <template v-else><p><strong>{{ assetForm.name || '尚未填写名称' }}</strong> · {{ {image:'图片',video:'视频',audio:'音频',script:'剧本'}[assetForm.mediaType] }}</p><p>{{ editingAssetId?'保存信息沿用原素材 ID，已有引用仍指向这份素材。':'上传成功后得到独立素材，可被项目或独立任务选择；不会自动采用到其他任务。' }}</p><p v-if="editingAsset" class="asset-help">来源（只读）：{{ assetSourceInfo(editingAsset) }}</p><p class="asset-help">主图／变体关联在素材库的关系入口单独维护；此表单不修改绑定、不发起模型调用。</p></template>
        </template>
        <template #actions><button class="button ghost" :disabled="assetSaving||generationBusy||recoveryLoading" @click="closeAssetModal">取消</button><button v-if="assetForm.entryMode==='local'" class="button primary" :disabled="editorLocked" @click="saveAsset"><Save :size="15"/>{{ assetSaving?(editingAssetId?'正在保存信息…':'正在上传入库…'):editingAssetId?'保存信息':'上传并入库' }}</button></template>
      </ConfigurationAuthoringShell>
    </BaseModal>

    <BaseModal :open="!!deleteTarget" :dismissible="!deletePending" title="删除素材" eyebrow="删除素材（Delete Asset）" width="520px" @close="closeDeleteAsset"><div v-if="deleteTarget" class="asset-delete-confirm"><div class="asset-delete-warning"><AlertTriangle :size="18"/><span><strong>删除“{{ deleteTarget.name }}”不可撤销</strong><small>删除此 Asset 及不再被其他 Asset 共用的上传文件；不连带删除其他素材、系列或版本。已有主图/变体、任务等引用时，服务端会说明需先处理的关联。仅解除关系请使用“主图 / 变体关系”。</small></span></div><label class="field"><span>输入素材名称以确认</span><input v-model="deleteConfirmName" :disabled="deletePending" :placeholder="deleteTarget.name" @keyup.enter="confirmDeleteAsset"></label><div v-if="deleteError" class="asset-editor-error" role="alert"><AlertTriangle :size="14"/><span>{{ deleteError }}</span></div></div><template #footer><button class="button ghost" :disabled="deletePending" @click="closeDeleteAsset">取消</button><button class="button delete-button" :disabled="deletePending||!deleteConfirmed" @click="confirmDeleteAsset"><Trash2 :size="15"/>{{ deletePending?'删除中…':'永久删除' }}</button></template></BaseModal>
    <BaseModal :open="!!fullPreview" title="素材完整预览" eyebrow="完整预览（Full Preview）" width="1080px" @close="fullPreview=null"><div v-if="fullPreview" class="asset-full-preview"><div><AssetMediaPreview :asset="fullPreview" controls @source-error="recoverAssetPreview" @source-loaded="clearPreviewRecovery"/></div><footer><strong>{{ fullPreview.name }}</strong><button class="button primary small" @click="downloadAsset(fullPreview)"><Download :size="14"/>下载</button></footer></div></BaseModal>
  </div>
</template>

<style scoped>
.asset-entry-mode{display:flex;gap:8px;flex-wrap:wrap}.asset-entry-mode button{padding:8px 16px;min-height:var(--config-control-height);font:600 var(--config-body-size)/1.4 var(--font);border:1px solid var(--border);border-radius:var(--config-radius);background:var(--surface);color:var(--text-secondary);cursor:pointer}.asset-entry-mode button.active{border-color:var(--accent);color:var(--accent)}.field>span{font-size:var(--config-body-size)}.field>small{font-size:var(--config-help-size)}
.batch-check{display:flex;align-items:center;gap:8px;padding:12px;font-size:var(--config-help-size);cursor:pointer}.batch-check input{width:16px;height:16px;margin:0}
.asset-command-deck{position:sticky;top:0;z-index:12;display:grid;gap:8px;padding:12px;background:var(--surface)}
.asset-dock-primary{display:flex;align-items:center;gap:12px;min-width:0}.asset-style-row{min-width:0;flex:1;display:flex;align-items:center;gap:8px}.asset-style-row>span{font-size:var(--config-help-size);color:var(--text-secondary)}
.asset-style-picker{min-width:0;flex:1;display:flex;grid-template-columns:none;gap:8px;overflow-x:auto;padding-bottom:4px}.asset-style-picker button{flex:0 0 144px;min-width:144px;height:auto;min-height:64px;padding:8px 12px}.asset-style-picker button span{font-size:var(--config-help-size)}.asset-style-picker button strong{font-size:var(--config-body-size)}.asset-dock-actions{display:flex;gap:8px;flex-wrap:wrap}
.asset-command-deck .asset-toolbar{padding:8px 0;border-block:1px solid var(--border);border-radius:0;background:transparent}.asset-command-deck .asset-tabs{min-width:0;overflow-x:auto}.asset-command-deck .asset-tabs button{flex:0 0 auto}.asset-filter-footer{display:flex;align-items:center;justify-content:space-between;gap:12px}.asset-filter-footer .result-bar{margin-left:auto;gap:16px}.asset-subtabs{display:flex;gap:8px;min-width:0;overflow-x:auto}.asset-subtabs button,.asset-kind-picker button{padding:8px 12px;border:1px solid var(--border);border-radius:var(--config-radius);background:var(--surface);color:var(--text-secondary);font-size:var(--config-body-size);cursor:pointer;white-space:nowrap}.asset-subtabs button.active,.asset-kind-picker button.active{border-color:var(--accent);color:var(--accent)}
.asset-load-error{display:flex;align-items:center;gap:8px;padding:12px;color:var(--red)}.asset-load-error span{flex:1}.asset-load-error button{background:transparent;color:inherit;cursor:pointer}
.asset-detail-layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:24px}.asset-detail-content{min-width:0;display:grid;gap:16px;font:var(--config-body-size)/1.6 var(--font)}.asset-detail-content h3{font-size:var(--config-title-size);font-weight:600}.asset-detail-content h4{font-size:var(--config-section-size);font-weight:600}.asset-detail-content p,.asset-detail-content h3{overflow-wrap:anywhere;white-space:pre-wrap}.asset-detail-image{padding:0;border:0}
.asset-info-missing{color:var(--accent)}.asset-help{font-size:var(--config-help-size);color:var(--text-secondary);line-height:1.6;overflow-wrap:anywhere}.asset-detail-actions{display:flex;gap:8px;flex-wrap:wrap}.asset-detail-actions .button{font-size:var(--config-body-size);min-height:var(--config-control-height)}.button.danger-ghost{color:var(--red);border-color:var(--red);background:transparent}.delete-button{background:var(--red);color:var(--text)}
.asset-kind-picker{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.asset-kind-picker button{display:flex;align-items:center;justify-content:center;gap:8px}.asset-author-preview{height:200px;background:var(--bg-soft);border-radius:8px;overflow:auto;display:grid;align-items:center;min-width:0}.asset-author-preview img,.asset-author-preview video{width:100%;height:100%;object-fit:contain}.asset-author-preview audio{width:100%;min-width:0}.asset-file-summary{padding:16px;font-size:var(--config-body-size);overflow-wrap:anywhere}.asset-file-summary p{font-size:var(--config-help-size);color:var(--text-secondary);margin-top:8px}
.asset-author-error,.asset-recovery [role=alert]{color:var(--red);overflow-wrap:anywhere}.asset-recovery{display:grid;gap:12px;border:1px solid var(--accent);padding:16px;border-radius:8px}.asset-recovery h3{font-size:var(--config-section-size)}.asset-recovery li{display:grid;gap:8px;margin:8px 0;overflow-wrap:anywhere}.asset-recovery ul{padding:0;list-style:none}.asset-recovery nav{display:flex;gap:8px;flex-wrap:wrap}
.asset-delete-confirm{display:grid;gap:16px}.asset-delete-warning{display:flex;gap:12px;padding:16px;border:1px solid var(--red);border-radius:8px;color:var(--red)}.asset-delete-warning span{display:grid;gap:8px}.asset-delete-warning small{font-size:var(--config-help-size);line-height:1.6}.asset-editor-error{display:flex;gap:8px;color:var(--red)}
.asset-full-preview{display:grid;gap:12px}.asset-full-preview>div{height:min(76vh,760px);background:var(--bg-soft);overflow:hidden}.asset-full-preview footer{display:flex;gap:12px;align-items:center;justify-content:space-between}.asset-full-preview strong{font-size:var(--config-body-size);overflow-wrap:anywhere}
@media(max-width:980px){.asset-dock-primary{align-items:stretch;flex-direction:column}.asset-dock-actions{justify-content:flex-end}}
@media(max-width:780px){.asset-command-deck .asset-toolbar,.asset-filter-footer{align-items:stretch;flex-direction:column}.asset-command-deck .toolbar-right{width:100%}.asset-command-deck .search-input{width:auto;flex:1}.asset-filter-footer .result-bar{width:100%;margin-left:0}.asset-kind-picker{grid-template-columns:repeat(2,minmax(0,1fr))}.form-grid.two{grid-template-columns:1fr}.asset-detail-layout{grid-template-columns:1fr}.asset-detail-image{height:260px}.asset-detail-actions{--config-control-height:44px}}
</style>
