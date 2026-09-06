<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AlertTriangle, ArrowUpDown, Box, Download, FileText, Image, Layers3, Music2, Pencil, Plus, RefreshCw, Save, Search, Sparkles, Trash2, Upload, UserRound, Video, WandSparkles, Workflow as WorkflowIcon, X } from 'lucide-vue-next'
import { useStudioStore } from '@/stores/studio'
import { useAuthStore } from '@/stores/auth'
import type { Asset, AssetGenerationJob, AssetGenerationMediaType, AssetGenerationRouteOption, AssetType, BibleDraftEntityType } from '@/types'
import BaseModal from '@/components/BaseModal.vue'
import AssetMediaPreview from '@/components/AssetMediaPreview.vue'
import WorkflowExecutionInputEditor from '@/components/WorkflowExecutionInputEditor.vue'
import {
  bindUploadedWorkflowInputAsset,
  buildWorkflowExecutionInput,
  createWorkflowExecutionInputDraft,
  validateWorkflowExecutionInputDraft,
  workflowInputUploadCandidateCompatible,
} from '@/services/workflowExecutionInputs'
import type {
  WorkflowExecutionInputDraft,
  WorkflowInputAssetUploadRequest,
} from '@/services/workflowExecutionInputs'
import type { WorkflowExecutionInput } from '@/services/platformTypes'

type MediaFilter = '' | AssetGenerationMediaType
type ImageCategory = BibleDraftEntityType | 'reference'
type ParameterScalar = string | number | boolean
type ParameterDefinition = {
  key: string
  type: 'string' | 'integer' | 'number' | 'boolean' | 'enum'
  required: boolean
  min?: number
  max?: number
  multipleOf?: number
  minLength?: number
  maxLength?: number
  options: ParameterScalar[]
}

const studio = useStudioStore(); const auth = useAuthStore(); const route = useRoute(); const router = useRouter()
const selectedStyle = ref(typeof route.query.style === 'string' ? route.query.style : '')
const selectedMedia = ref<MediaFilter>(['image','video','audio','script'].includes(String(route.query.type || '')) ? route.query.type as MediaFilter : '')
const selectedImageCategory = ref<''|ImageCategory>(''); const search = ref(''); const selected = ref<Asset | null>(null)
const loading = ref(false); const loadError = ref(''); const sortNewest = ref(true)
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
const assetSourceInfo = (asset: Asset) => asset.sourceInfo?.trim() || '来源未记录（历史素材）'

const assetModalOpen=ref(false);const assetSaving=ref(false);const assetError=ref('');const uploadInput=ref<HTMLInputElement|null>(null);const uploadPreview=ref('');const fullPreview=ref<Asset|null>(null)
const deleteTarget=ref<Asset|null>(null);const deleteConfirmName=ref('');const deletePending=ref(false);const deleteError=ref('')
const generationOptions=ref<AssetGenerationRouteOption[]>([]);const promptOptions=ref<AssetGenerationRouteOption[]>([]);const optionsLoading=ref(false);let optionsLoadSequence=0
const workflowInputAssets=ref<Asset[]>([]);const workflowInputAssetsLoading=ref(false);const workflowInputAssetsError=ref('');let workflowAssetLoadSequence=0
const workflowInputDraft=ref<WorkflowExecutionInputDraft>(createWorkflowExecutionInputDraft(null))
const workflowInputUploadInput=ref<HTMLInputElement|null>(null)
const workflowInputUploadIntent=ref<{request:WorkflowInputAssetUploadRequest;routeKey:string}|null>(null)
const workflowInputUploadPendingKey=ref('');const workflowInputUploadStatus=ref('');let workflowInputUploadSequence=0
const blankAssetForm=()=>({
  entryMode:'local' as 'local'|'generate', mediaType:'image' as AssetGenerationMediaType,
  file:null as File|null,imageCategory:'character' as ImageCategory,name:'',description:'',promptHint:'',styleId:selectedStyle.value,
  brief:'',prompt:'',negativePrompt:'',generationRouteKey:'',promptTargetId:'',parametersText:'{}',allowExternalCalls:false,budgetLimit:'',
  assetIdempotencyKey:crypto.randomUUID(),promptIdempotencyKey:crypto.randomUUID(),
})
const assetForm=ref(blankAssetForm());const editingAssetId=ref('');const editingAsset=ref<Asset|null>(null);const assetMediaTypeFixed=ref(false)
const canEditAsset=(asset:Asset)=>Boolean(asset.ownerId&&(auth.user?.isStaff||String(asset.ownerId)===String(auth.user?.id)))
const assetMediaType=(asset:Asset):AssetGenerationMediaType=>isImageAsset(asset)?'image':asset.type as AssetGenerationMediaType
const editableAssetKind=(asset:Asset):ImageCategory=>['character','scene','prop'].includes(asset.type)?asset.type as BibleDraftEntityType:'reference'
const revokeUploadPreview=()=>{if(uploadPreview.value.startsWith('blob:'))URL.revokeObjectURL(uploadPreview.value);uploadPreview.value=''}
const openCreateAsset=(mode:'local'|'generate'='local',mediaType?:AssetGenerationMediaType)=>{revokeUploadPreview();assetMediaTypeFixed.value=mediaType!==undefined;assetForm.value={...blankAssetForm(),entryMode:mode,mediaType:mediaType||'image'};editingAssetId.value='';editingAsset.value=null;assetError.value='';assetModalOpen.value=true}
const openCreateAssetFromCurrentTab=()=>openCreateAsset('local',selectedMedia.value||undefined)
const openEditAsset=(asset:Asset)=>{if(!canEditAsset(asset))return;revokeUploadPreview();assetMediaTypeFixed.value=true;assetForm.value={...blankAssetForm(),mediaType:assetMediaType(asset),imageCategory:editableAssetKind(asset),name:asset.name,description:asset.description,promptHint:asset.promptHint||'',styleId:asset.styleId};editingAssetId.value=asset.id;editingAsset.value=asset;assetError.value='';uploadPreview.value=asset.thumbnail||'';assetModalOpen.value=true;selected.value=null}
const resetAssetModal=()=>{optionsLoadSequence+=1;workflowAssetLoadSequence+=1;workflowInputUploadSequence+=1;optionsLoading.value=false;workflowInputAssetsLoading.value=false;workflowInputAssets.value=[];workflowInputAssetsError.value='';workflowInputDraft.value=createWorkflowExecutionInputDraft(null);workflowInputUploadIntent.value=null;workflowInputUploadPendingKey.value='';workflowInputUploadStatus.value='';if(workflowInputUploadInput.value)workflowInputUploadInput.value.value='';revokeUploadPreview();assetModalOpen.value=false;editingAssetId.value='';editingAsset.value=null;assetError.value=''}
const closeAssetModal=()=>{if(assetSaving.value)return;resetAssetModal()}
const assetModalTitle=computed(()=>editingAssetId.value?'编辑素材信息':assetMediaTypeFixed.value?`新增${{image:'图片',video:'视频',audio:'音频',script:'剧本'}[assetForm.value.mediaType]}素材`:'新增素材')
const assetUploadAccept={image:'image/jpeg,image/png,image/webp',video:'video/mp4,video/quicktime,video/webm',audio:'audio/wav,audio/mpeg,audio/flac,audio/mp4,audio/ogg',script:'.txt,.md,.markdown,.fountain,.docx,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document'} as const
const acceptForType=computed(()=>assetUploadAccept[assetForm.value.mediaType])
const workflowInputUploadAccept=computed(()=>workflowInputUploadIntent.value?assetUploadAccept[workflowInputUploadIntent.value.request.mediaKind]:'')
const uploadLabel=computed(()=>({image:'JPEG / PNG / WebP',video:'MP4 / MOV / WebM（2–15 秒）',audio:'WAV / MP3 / FLAC / M4A / OGG（2–15 秒）',script:'TXT / Markdown / Fountain / DOCX'}[assetForm.value.mediaType]))
const selectUploadFile=(event:Event)=>{const file=(event.target as HTMLInputElement).files?.[0]||null;assetForm.value.file=file;if(uploadPreview.value.startsWith('blob:'))URL.revokeObjectURL(uploadPreview.value);uploadPreview.value=file?URL.createObjectURL(file):'';if(file&&!assetForm.value.name.trim())assetForm.value.name=file.name.replace(/\.[^.]+$/,'')}
watch(()=>assetForm.value.mediaType,()=>{optionsLoadSequence+=1;workflowAssetLoadSequence+=1;optionsLoading.value=false;workflowInputAssets.value=[];workflowInputDraft.value=createWorkflowExecutionInputDraft(null);workflowInputUploadStatus.value='';assetForm.value.file=null;if(uploadPreview.value.startsWith('blob:'))URL.revokeObjectURL(uploadPreview.value);uploadPreview.value='';generationOptions.value=[];promptOptions.value=[];assetForm.value.generationRouteKey='';assetForm.value.promptTargetId='';if(assetModalOpen.value&&assetForm.value.entryMode==='generate')void loadOptions()})
watch(()=>assetForm.value.entryMode,(mode)=>{workflowInputUploadStatus.value='';if(mode==='generate'&&assetModalOpen.value)void loadOptions();else{optionsLoadSequence+=1;optionsLoading.value=false}})
const routeOptionKey=(item:AssetGenerationRouteOption)=>`${item.targetKind}:${item.targetId}`
const selectedGenerationOption=computed(()=>generationOptions.value.find((item)=>routeOptionKey(item)===assetForm.value.generationRouteKey))
const requiredAssetOperation=computed(()=>({image:'image.text_to_image',video:'video.text_to_video',audio:'audio.text_to_audio',script:'script.generate'}[assetForm.value.mediaType]))
const emptyGenerationGuidance=computed(()=>assetForm.value.mediaType==='script'
  ? '需要已发布且启用的文本 Deployment，声明 script.generate，并保持该 operation 的真实烟测有效。'
  : `需要当前已发布并通过真实烟测的 ComfyUI 工作流，或已发布且通过 ${requiredAssetOperation.value} 真实烟测的异步 API Deployment。`)
const loadWorkflowInputAssets=async()=>{const sequence=++workflowAssetLoadSequence;workflowInputAssetsLoading.value=true;workflowInputAssetsError.value='';try{const rows=await studio.getWorkflowInputAssets();if(sequence===workflowAssetLoadSequence&&assetModalOpen.value)workflowInputAssets.value=rows}catch(error){if(sequence===workflowAssetLoadSequence)workflowInputAssetsError.value=error instanceof Error?error.message:'工作流输入素材读取失败'}finally{if(sequence===workflowAssetLoadSequence)workflowInputAssetsLoading.value=false}}
const workflowInputMediaLabel=(kind:WorkflowInputAssetUploadRequest['mediaKind'])=>({image:'图片',video:'视频',audio:'音频'}[kind])
const workflowInputAssetName=(file:File,kind:WorkflowInputAssetUploadRequest['mediaKind'])=>Array.from(file.name.replace(/\.[^.]+$/,'').trim()||`本地参考${workflowInputMediaLabel(kind)}`).slice(0,160).join('')
const requestWorkflowInputAssetUpload=(request:WorkflowInputAssetUploadRequest)=>{
  if(assetSaving.value)return
  const input=workflowInputUploadInput.value
  if(!input){assetError.value='本地文件选择器不可用，请刷新页面后重试';return}
  workflowInputUploadIntent.value={request,routeKey:assetForm.value.generationRouteKey}
  workflowInputUploadStatus.value='';assetError.value='';input.value='';input.accept=assetUploadAccept[request.mediaKind];input.click()
}
const uploadWorkflowInputAsset=async(event:Event)=>{
  const input=event.target as HTMLInputElement
  const file=input.files?.[0]||null
  const intent=workflowInputUploadIntent.value
  input.value=''
  if(!file||!intent){workflowInputUploadIntent.value=null;return}
  if(assetSaving.value)return
  const sequence=++workflowInputUploadSequence
  const {request,routeKey}=intent
  workflowAssetLoadSequence+=1;workflowInputAssetsLoading.value=false
  workflowInputUploadPendingKey.value=request.key;workflowInputUploadStatus.value='';assetSaving.value=true;assetError.value=''
  try{
    const saved=await studio.createAsset({
      file,mediaType:request.mediaKind,imageCategory:request.mediaKind==='image'?'reference':undefined,
      name:workflowInputAssetName(file,request.mediaKind),description:'',promptHint:'',styleId:assetForm.value.styleId||undefined,
    })
    workflowInputAssets.value=[saved,...workflowInputAssets.value.filter((asset)=>asset.id!==saved.id)]
    void loadWorkflowInputAssets()
    if(sequence!==workflowInputUploadSequence||!assetModalOpen.value||assetForm.value.entryMode!=='generate'||assetForm.value.generationRouteKey!==routeKey){
      assetError.value=`“${saved.name}”已上传并入库，但当前生成方案或表单已变化，未自动绑定；可从素材库重新选择。`;return
    }
    const schema=selectedGenerationOption.value?.workflowInputSchema
    if(!schema||!workflowInputUploadCandidateCompatible(schema,workflowInputDraft.value,request,saved)){
      assetError.value=`“${saved.name}”已上传并入库，但不符合当前工作流的类型、时长或帧率合同，未自动绑定；可在素材库中管理或删除。`;return
    }
    const nextDraft=bindUploadedWorkflowInputAsset(workflowInputDraft.value,request.target,saved.id)
    if(!nextDraft){assetError.value=`“${saved.name}”已上传并入库，但目标输入已变化，未自动绑定；可从素材库重新选择。`;return}
    workflowInputDraft.value=nextDraft
    workflowInputUploadStatus.value=`“${saved.name}”已上传并入库，并自动绑定到当前工作流输入。`
  }catch(error){assetError.value=error instanceof Error?error.message:`参考${workflowInputMediaLabel(request.mediaKind)}上传失败，请稍后重试`}
  finally{if(sequence===workflowInputUploadSequence){workflowInputUploadPendingKey.value='';workflowInputUploadIntent.value=null;assetSaving.value=false}}
}
watch(()=>assetForm.value.generationRouteKey,()=>{workflowInputUploadStatus.value='';const option=selectedGenerationOption.value;if(!option)return;if(option.targetKind==='comfy_workflow'&&option.workflowInputSchema){assetForm.value.parametersText='{}';workflowInputDraft.value=createWorkflowExecutionInputDraft(option.workflowInputSchema);if(option.workflowInputSchema.assetSlots.length||option.workflowInputSchema.dynamicReferences||Object.keys(option.workflowInputSchema.dynamicReferenceModes).length)void loadWorkflowInputAssets()}else{workflowAssetLoadSequence+=1;workflowInputAssets.value=[];workflowInputDraft.value=createWorkflowExecutionInputDraft(null);assetForm.value.parametersText=JSON.stringify(option.parameterDefaults||{},null,2)}})
async function loadOptions(){const sequence=++optionsLoadSequence;const mediaType=assetForm.value.mediaType;optionsLoading.value=true;assetError.value='';try{const [media,prompt]=await Promise.all([studio.getAssetGenerationOptions('asset',mediaType),studio.getAssetGenerationOptions('prompt',mediaType)]);if(sequence!==optionsLoadSequence||!assetModalOpen.value||assetForm.value.entryMode!=='generate'||assetForm.value.mediaType!==mediaType)return;generationOptions.value=media;promptOptions.value=prompt.filter((item)=>item.targetKind==='deployment');if(!assetForm.value.generationRouteKey&&media[0])assetForm.value.generationRouteKey=routeOptionKey(media[0]);if(!assetForm.value.promptTargetId&&promptOptions.value[0])assetForm.value.promptTargetId=promptOptions.value[0].targetId}catch(error){if(sequence===optionsLoadSequence&&assetModalOpen.value&&assetForm.value.mediaType===mediaType)assetError.value=error instanceof Error?error.message:'可用模型加载失败'}finally{if(sequence===optionsLoadSequence)optionsLoading.value=false}}

const objectValue=(value:unknown):Record<string,unknown>|null=>value&&typeof value==='object'&&!Array.isArray(value)?value as Record<string,unknown>:null
const finiteNumber=(value:unknown)=>typeof value==='number'&&Number.isFinite(value)?value:undefined
const parameterDefinitions=computed<ParameterDefinition[]>(()=>{
  const schema=selectedGenerationOption.value?.requestSchema
  if(!schema)return []
  const properties=objectValue(schema.properties)
  const definitions=properties||schema
  const requiredKeys=new Set(Array.isArray(schema.required)?schema.required.filter((item):item is string=>typeof item==='string'):[])
  return Object.entries(definitions).flatMap(([key,value])=>{
    if(properties&&['prompt','negative_prompt','model'].includes(key))return []
    const definition=objectValue(value);if(!definition)return []
    const options=(Array.isArray(definition.options)?definition.options:Array.isArray(definition.enum)?definition.enum:[])
      .filter((item):item is ParameterScalar=>['string','number','boolean'].includes(typeof item))
    const rawType=String(definition.type||'')
    const type:ParameterDefinition['type']|null=options.length?'enum':['string','integer','number','boolean'].includes(rawType)?rawType as ParameterDefinition['type']:null
    if(!type)return []
    return [{
      key,type,options,required:definition.required===true||requiredKeys.has(key),
      min:finiteNumber(definition.min??definition.minimum),max:finiteNumber(definition.max??definition.maximum),
      multipleOf:finiteNumber(definition.multiple_of??definition.multipleOf),
      minLength:finiteNumber(definition.min_length??definition.minLength),maxLength:finiteNumber(definition.max_length??definition.maxLength),
    }]
  })
})
const parametersRecord=():Record<string,unknown>|null=>{try{return objectValue(JSON.parse(assetForm.value.parametersText||'{}'))}catch{return null}}
const parameterValue=(key:string)=>parametersRecord()?.[key]??''
const updateParameter=(definition:ParameterDefinition,event:Event)=>{
  const parameters=parametersRecord();if(!parameters){assetError.value='高级参数 JSON 无法解析，请先修复后再编辑参数';return}
  const raw=(event.target as HTMLInputElement|HTMLSelectElement).value
  if(raw===''&&!definition.required){delete parameters[definition.key]}
  else if(definition.type==='integer'||definition.type==='number')parameters[definition.key]=raw===''?raw:Number(raw)
  else if(definition.type==='boolean')parameters[definition.key]=raw==='true'
  else if(definition.type==='enum')parameters[definition.key]=definition.options.find((item)=>String(item)===raw)??raw
  else parameters[definition.key]=raw
  assetForm.value.parametersText=JSON.stringify(parameters,null,2)
}
const parameterConstraint=(definition:ParameterDefinition)=>{
  const parts=[definition.required?'必填':'可选',definition.type==='enum'?'枚举':definition.type]
  if(definition.min!==undefined||definition.max!==undefined)parts.push(`${definition.min??'不限'} ～ ${definition.max??'不限'}`)
  if(definition.multipleOf!==undefined)parts.push(`步长 ${definition.multipleOf}`)
  if(definition.minLength!==undefined||definition.maxLength!==undefined)parts.push(`长度 ${definition.minLength??0} ～ ${definition.maxLength??'不限'}`)
  return parts.join(' · ')
}
const validateParameters=(parameters:Record<string,unknown>)=>{
  for(const definition of parameterDefinitions.value){
    const value=parameters[definition.key]
    if(value===undefined||value===null||value===''){
      if(definition.required)return `生成参数 ${definition.key} 为必填项`
      continue
    }
    if(definition.type==='integer'&&(!Number.isInteger(value)||typeof value!=='number'))return `生成参数 ${definition.key} 必须是整数`
    if(definition.type==='number'&&(typeof value!=='number'||!Number.isFinite(value)))return `生成参数 ${definition.key} 必须是数字`
    if(definition.type==='boolean'&&typeof value!=='boolean')return `生成参数 ${definition.key} 必须是布尔值`
    if(definition.type==='string'&&typeof value!=='string')return `生成参数 ${definition.key} 必须是文本`
    if(definition.type==='enum'&&!definition.options.some((option)=>Object.is(option,value)))return `生成参数 ${definition.key} 不在允许选项中`
    if(typeof value==='number'){
      if(definition.min!==undefined&&value<definition.min)return `生成参数 ${definition.key} 不能小于 ${definition.min}`
      if(definition.max!==undefined&&value>definition.max)return `生成参数 ${definition.key} 不能大于 ${definition.max}`
      if(definition.multipleOf!==undefined&&Math.abs(value/definition.multipleOf-Math.round(value/definition.multipleOf))>1e-9)return `生成参数 ${definition.key} 必须是 ${definition.multipleOf} 的倍数`
    }
    if(typeof value==='string'){
      if(definition.minLength!==undefined&&value.length<definition.minLength)return `生成参数 ${definition.key} 长度不能小于 ${definition.minLength}`
      if(definition.maxLength!==undefined&&value.length>definition.maxLength)return `生成参数 ${definition.key} 长度不能大于 ${definition.maxLength}`
    }
  }
  return ''
}
const resetGenerationParameters=()=>{assetForm.value.parametersText=JSON.stringify(selectedGenerationOption.value?.parameterDefaults||{},null,2);assetError.value=''}
const routeOptionLabel=(item:AssetGenerationRouteOption)=>item.targetKind==='comfy_workflow'
  ? `[ComfyUI] ${item.name} · ${item.modelDeploymentName}`
  : `[异步 API] ${item.name}${item.modelIdentifier?` · ${item.modelIdentifier}`:''}`
const dateTime=(value:string|null)=>value?new Date(value).toLocaleString('zh-CN'):'—'
const saveAsset=async()=>{
  if(assetSaving.value)return;const form=assetForm.value
  if(!form.name.trim()){assetError.value='请填写素材名称';return}
  const classified=form.mediaType==='image'&&form.imageCategory!=='reference'
  if(classified&&(!form.description.trim()||!(form.entryMode==='generate'?form.prompt:form.promptHint).trim())){assetError.value='人物、场景和道具图片必须填写基础描述与视觉锚点/最终提示词';return}
  assetSaving.value=true;assetError.value=''
  try{
    if(editingAssetId.value){
      const original=editingAsset.value
      const mutableKind=original&&['character','scene','prop','reference'].includes(original.backendKind||'')?form.imageCategory:undefined
      const saved=await studio.updateAssetMetadata(editingAssetId.value,{name:form.name,description:form.description,promptHint:form.promptHint,kind:mutableKind,styleId:form.styleId||undefined});resetAssetModal();await refresh();selected.value=studio.assets.find((asset)=>asset.id===saved.id)||saved;return
    }
    if(form.entryMode==='local'){
      if(!form.file){assetError.value=`请选择 ${uploadLabel.value} 文件`;return}
      const saved=await studio.createAsset({file:form.file,mediaType:form.mediaType,imageCategory:form.imageCategory,name:form.name,description:form.description,promptHint:form.promptHint,styleId:form.styleId||undefined});resetAssetModal();await refresh();selected.value=studio.assets.find((asset)=>asset.id===saved.id)||saved
    }else{
      if(!form.prompt.trim()){assetError.value='请填写或先生成最终提示词';return}
      if(!form.generationRouteKey){assetError.value='当前类型没有已发布且通过真实烟测的可用生成方案';return}
      const option=selectedGenerationOption.value
      if(!option){assetError.value='所选生成方案已失效，请刷新后重试';return}
      let parameters:Record<string,unknown>={};let workflowInputs:WorkflowExecutionInput|undefined
      if(option.targetKind==='comfy_workflow'){
        if(!option.workflowInputSchema){assetError.value='所选工作流没有可用的输入合同';return}
        const inputError=validateWorkflowExecutionInputDraft(option.workflowInputSchema,workflowInputDraft.value,workflowInputAssets.value);if(inputError){assetError.value=inputError;return}
        workflowInputs=buildWorkflowExecutionInput(option.workflowInputSchema,workflowInputDraft.value)
      }else{try{parameters=JSON.parse(form.parametersText||'{}')}catch{assetError.value='生成参数必须是合法 JSON 对象';return}if(!parameters||Array.isArray(parameters)||typeof parameters!=='object'){assetError.value='生成参数必须是对象';return}const parameterError=validateParameters(parameters);if(parameterError){assetError.value=parameterError;return}}
      if(option.isExternal&&(!form.allowExternalCalls||!form.budgetLimit)){assetError.value='外部模型需要勾选数据外发授权并填写预算上限';return}
      const job=await studio.createAssetGenerationJob({jobType:'asset',mediaType:form.mediaType,executionKind:option.targetKind,generationTargetId:option.targetId,styleId:form.styleId||undefined,imageCategory:form.mediaType==='image'?form.imageCategory:undefined,name:form.name,description:form.description,prompt:form.prompt,negativePrompt:form.negativePrompt,parameters,workflowInputs,allowExternalCalls:form.allowExternalCalls,budgetLimit:form.budgetLimit||null,idempotencyKey:form.assetIdempotencyKey});resetAssetModal();await router.push({name:'workflows',query:{scope:'asset',task:job.id}})
    }
  }catch(error){assetError.value=error instanceof Error?error.message:'素材保存失败，请稍后重试'}finally{assetSaving.value=false}
}
const generatePrompt=async()=>{
  const form=assetForm.value
  if(!form.brief.trim()){assetError.value='请先填写创作需求';return}
  if(!form.promptTargetId){assetError.value='没有可用的已烟测文本模型';return}
  const option=promptOptions.value.find((item)=>item.targetId===form.promptTargetId)
  if(!option){assetError.value='所选文本模型已失效，请刷新后重试';return}
  if(option.isExternal&&(!form.allowExternalCalls||!form.budgetLimit)){assetError.value='外部文本模型需要数据外发授权和预算上限';return}
  assetSaving.value=true;assetError.value=''
  try{
    const job=await studio.createAssetGenerationJob({jobType:'prompt',mediaType:form.mediaType,executionKind:'deployment',generationTargetId:option.targetId,styleId:form.styleId||undefined,prompt:form.brief,allowExternalCalls:form.allowExternalCalls,budgetLimit:form.budgetLimit||null,idempotencyKey:form.promptIdempotencyKey})
    resetAssetModal();await router.push({name:'workflows',query:{scope:'asset',task:job.id}})
  }catch(error){assetError.value=error instanceof Error?error.message:'提示词任务创建失败'}finally{assetSaving.value=false}
}
const applyPromptResult=(job:AssetGenerationJob)=>{openCreateAsset('generate',job.mediaType);assetForm.value.brief=job.prompt;assetForm.value.prompt=job.resultText;assetForm.value.styleId=job.styleId||selectedStyle.value;void loadOptions()}
const queryValue=(value:unknown)=>String(Array.isArray(value)?value[0]||'':value||'')
const baseAssetQuery=()=>({...(selectedStyle.value?{style:selectedStyle.value}:{}),...(selectedMedia.value?{type:selectedMedia.value}:{})})
const consumeRouteContext=async()=>{
  const promptJobId=queryValue(route.query.promptJob);const assetId=queryValue(route.query.asset)
  if(!promptJobId&&!assetId)return
  try{
    if(promptJobId){
      const jobs=await studio.refreshAssetGenerationJobs();const job=jobs.find((item)=>item.id===promptJobId)
      if(job?.jobType==='prompt'&&job.status==='succeeded'&&job.resultText)applyPromptResult(job)
      else studio.notify('提示词任务尚未成功，已保留在生产工作流页继续跟踪。','info')
    }else if(assetId){
      selected.value=studio.assets.find((asset)=>asset.id===assetId)||await studio.refreshAsset(assetId,false)
    }
  }catch(error){studio.notify(error instanceof Error?error.message:'无法打开指定素材任务结果','error')}
  finally{await router.replace({name:'assets',query:baseAssetQuery()})}
}
onMounted(async()=>{await refresh();await consumeRouteContext()})
type AssetPreviewEvent={assetId:string;source:string}
const previewRecoveryAttempts=new Map<string,Set<string>>();const previewRecoveryInFlight=new Set<string>()
const assetPreviewSource=(asset:Asset|undefined|null)=>asset?.mediaUrl||asset?.thumbnail||''
const currentPreviewAsset=(assetId:string)=>selected.value?.id===assetId?selected.value:fullPreview.value?.id===assetId?fullPreview.value:studio.assets.find((asset)=>asset.id===assetId)
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
  }catch(error){studio.notify(`素材预览地址刷新失败：${error instanceof Error?error.message:'请稍后重试'}`,'error')}
  finally{previewRecoveryInFlight.delete(assetId)}
}
const downloadAsset=async(asset:Asset)=>{try{const fresh=await studio.refreshAsset(asset.id,studio.assets.some((item)=>item.id===asset.id));if(!fresh.downloadUrl)throw new Error('该素材没有可下载的受管文件');const link=document.createElement('a');link.href=fresh.downloadUrl;link.rel='noopener';link.download='';document.body.appendChild(link);link.click();link.remove()}catch(error){studio.notify(error instanceof Error?error.message:'下载失败','error')}}

const openDeleteAsset=(asset:Asset)=>{if(!canEditAsset(asset))return;deleteTarget.value=asset;deleteConfirmName.value='';deleteError.value='';selected.value=null}
const closeDeleteAsset=()=>{if(deletePending.value)return;const target=deleteTarget.value;deleteTarget.value=null;deleteConfirmName.value='';deleteError.value='';if(target)selected.value=target}
const deleteConfirmed=computed(()=>Boolean(deleteTarget.value&&deleteConfirmName.value.trim()===deleteTarget.value.name.trim()))
const confirmDeleteAsset=async()=>{if(deletePending.value||!deleteTarget.value||!deleteConfirmed.value)return;const target=deleteTarget.value;deletePending.value=true;deleteError.value='';try{await studio.deleteAsset(target.id);deleteTarget.value=null;deleteConfirmName.value='';selected.value=null;await refresh()}catch(error){deleteError.value=error instanceof Error?error.message:'素材删除失败'}finally{deletePending.value=false}}
</script>

<template>
  <div class="assets-page page-stack">
    <section class="page-heading"><span class="eyebrow">多模态素材库 / MULTIMODAL ASSET VAULT</span><h2>风格素材库</h2><p>统一管理可下载、可被短剧正式引用的图片、视频、音频与剧本。</p></section>
    <section class="asset-command-deck panel" aria-label="素材库固定筛选与操作区">
      <div class="asset-dock-primary"><div class="asset-style-row"><span>风格</span><section class="style-picker asset-style-picker">
        <button class="style-picker-all" :class="{active:!selectedStyle}" :aria-pressed="!selectedStyle" @click="selectedStyle=''">全部<br><strong>全部风格</strong></button>
        <button v-for="item in studio.styles" :key="item.id" :class="{active:selectedStyle===item.id}" :aria-pressed="selectedStyle===item.id" :style="{'--style-accent':item.accent,'--style-bg':item.gradient}" @click="selectedStyle=item.id"><i></i><span>{{ item.englishName }}</span><strong>{{ item.name }}</strong></button>
      </section></div><div class="asset-dock-actions"><RouterLink class="button ghost" :to="{name:'workflows',query:{scope:'asset'}}"><WorkflowIcon :size="15"/>生成任务</RouterLink><button class="button primary" @click="openCreateAssetFromCurrentTab"><Plus :size="15"/>新增素材</button></div></div>
      <section class="asset-toolbar"><div class="asset-tabs"><button v-for="tab in mediaTabs" :key="tab.value" :class="{active:selectedMedia===tab.value}" :aria-pressed="selectedMedia===tab.value" @click="selectedMedia=tab.value"><component :is="tab.icon" :size="15"/>{{ tab.label }}</button></div><div class="toolbar-right"><label class="search-input"><Search :size="16"/><input v-model="search" placeholder="搜索名称、描述、视觉锚点或来源"><button v-if="search" @click="search=''" aria-label="清除"><X :size="14"/></button></label><button class="icon-btn" :disabled="loading" title="刷新素材" aria-label="刷新素材" @click="refresh"><RefreshCw :size="14"/></button></div></section>
      <div class="asset-filter-footer"><div v-if="selectedMedia==='image'" class="asset-subtabs"><button :class="{active:!selectedImageCategory}" :aria-pressed="!selectedImageCategory" @click="selectedImageCategory=''">全部图片</button><button v-for="kind in imageKinds" :key="kind.value" :class="{active:selectedImageCategory===kind.value}" :aria-pressed="selectedImageCategory===kind.value" @click="selectedImageCategory=kind.value">{{ kind.label }}</button></div><div class="result-bar"><span>显示 <strong>{{ filteredAssets.length }}</strong> 项素材</span><button :aria-label="sortNewest?'切换为较早入库优先':'切换为最新入库优先'" @click="sortNewest=!sortNewest"><ArrowUpDown :size="13"/>{{ sortNewest?'最新入库':'较早入库' }}</button></div></div>
    </section>

    <section v-if="style" class="style-feature" :style="{background:style.gradient}"><div class="style-feature-pattern"></div><div><span class="eyebrow">{{ style.englishName }}</span><h3>{{ style.name }}</h3><p>{{ style.description }}</p><div class="tag-row"><span v-for="tag in style.tags" :key="tag">{{ tag }}</span></div></div><div class="style-feature-counts"><div><Layers3 :size="18"/><strong>{{ style.assetCount ?? '—' }}</strong><span>素材总数</span></div></div></section>

    <div v-if="loadError" class="asset-load-error" role="alert"><AlertTriangle :size="15"/><span>{{ loadError }}</span><button @click="refresh">重试</button></div>
    <section v-if="loading" class="asset-grid"><div v-for="n in 12" :key="n" class="asset-card skeleton-card"></div></section>
    <section v-else-if="filteredAssets.length" class="asset-grid"><article v-for="asset in filteredAssets" :key="asset.id" class="asset-card" @click="selected=asset"><div class="asset-image" :style="{'--asset-accent':assetAccent(asset)}"><AssetMediaPreview :asset="asset" compact @source-error="recoverAssetPreview" @source-loaded="clearPreviewRecovery"/><span class="asset-type"><component :is="typeIcon(asset.type)" :size="12"/>{{ typeLabel(asset.type) }}</span><div class="asset-overlay"><button class="button primary small">查看素材</button></div></div><div class="asset-copy"><span>{{ studio.styles.find(s=>s.id===asset.styleId)?.name || '未限定风格' }}</span><h4>{{ asset.name }}</h4><p>{{ asset.description || asset.promptHint || '暂无说明' }}</p><div><span v-for="tag in asset.tags.slice(0,2)" :key="tag">#{{ tag }}</span></div></div></article></section>
    <section v-else class="empty-state"><Search :size="28"/><h3>没有匹配素材</h3><p>尝试更换风格、素材类型或搜索关键词，也可以立即新增。</p><button class="button primary" @click="openCreateAssetFromCurrentTab"><Plus :size="14"/>新增素材</button></section>

    <BaseModal :open="!!selected" title="素材详情" eyebrow="素材库资产（Library Asset）" width="900px" @close="selected=null"><div v-if="selected" class="asset-detail"><div class="asset-detail-image"><AssetMediaPreview :asset="selected" controls @source-error="recoverAssetPreview" @source-loaded="clearPreviewRecovery"/><span>{{ typeLabel(selected.type) }} · {{ selected.id.toUpperCase() }}</span></div><div class="asset-detail-copy"><span class="eyebrow">{{ studio.styles.find(s=>s.id===selected?.styleId)?.name || '未限定风格' }} · {{ typeLabel(selected.type) }}</span><h2>{{ selected.name }}</h2><p :class="{'asset-info-missing':!selected.description.trim()}">{{ assetDescription(selected) }}</p><div v-if="isClassifiedImageAsset(selected)||selected.promptHint" class="detail-block asset-prompt-hint" :class="{'asset-info-missing':!selected.promptHint?.trim()}"><label>生成提示词 / 视觉锚点</label><p>{{ selected.promptHint?.trim() || '视觉锚点缺失，素材信息不完整' }}</p></div><div class="detail-block asset-source-info"><label>来源信息</label><p>{{ assetSourceInfo(selected) }}</p></div><div class="detail-block"><label>完整性</label><div class="seed-box"><code>{{ selected.checksum?.slice(0,24) || '未提供摘要' }}</code><span>{{ selected.mimeType }}</span></div></div><div class="asset-detail-actions"><button class="button primary" @click="downloadAsset(selected)"><Download :size="15"/>下载</button><button v-if="canEditAsset(selected)" class="button ghost" @click="openEditAsset(selected)"><Pencil :size="15"/>编辑信息</button><button v-if="canEditAsset(selected)" class="button danger-ghost" @click="openDeleteAsset(selected)"><Trash2 :size="15"/>删除</button></div><div v-if="!canEditAsset(selected)" class="asset-readonly"><AlertTriangle :size="14"/><span>全局素材为只读，但仍可在短剧制作中引用和单独下载。</span></div><button class="button ghost full" @click="fullPreview=selected">完整预览</button></div></div></BaseModal>

    <BaseModal :open="assetModalOpen" :dismissible="!assetSaving" :title="assetModalTitle" eyebrow="素材入库与生成（Ingest & Generation）" width="800px" @close="closeAssetModal"><div class="asset-editor">
      <div v-if="!editingAssetId" class="segmented asset-entry-mode"><button :class="{active:assetForm.entryMode==='local'}" :disabled="assetSaving" @click="assetForm.entryMode='local'">本地添加</button><button :class="{active:assetForm.entryMode==='generate'}" :disabled="assetSaving" @click="assetForm.entryMode='generate'">模型生成</button></div>
      <div v-if="!editingAssetId&&!assetMediaTypeFixed" class="asset-kind-picker"><button v-for="tab in mediaTabs.slice(1)" :key="tab.value" :class="{active:assetForm.mediaType===tab.value}" :disabled="assetSaving" @click="assetForm.mediaType=tab.value as AssetGenerationMediaType"><component :is="tab.icon" :size="16"/>{{ tab.label }}</button></div>
      <div v-if="assetForm.entryMode==='local'&&!editingAssetId" class="asset-upload-box" :class="{filled:uploadPreview}" @click="uploadInput?.click()"><img v-if="uploadPreview&&assetForm.mediaType==='image'" :src="uploadPreview" alt="本地预览"><video v-else-if="uploadPreview&&assetForm.mediaType==='video'" :src="uploadPreview" muted preload="metadata"></video><template v-else><Upload :size="24"/><strong>{{ assetForm.file?.name || `选择 ${uploadLabel}` }}</strong></template><input ref="uploadInput" type="file" :accept="acceptForType" hidden @change="selectUploadFile"></div>
      <div v-if="assetForm.entryMode==='generate'&&!editingAssetId" class="asset-editor-evidence"><Sparkles :size="17"/><span><strong>生成任务由后端持久化执行</strong><small>只列出已发布且通过新鲜真实烟测的模型。外部模型必须显式授权并设置预算；提交不确定时不会自动重试。</small></span></div>
      <div class="form-grid two"><label v-if="assetForm.mediaType==='image'" class="field"><span>图片类别</span><select v-model="assetForm.imageCategory" :disabled="assetSaving"><option v-for="kind in imageKinds" :key="kind.value" :value="kind.value">{{ kind.label }}</option></select></label><label class="field"><span>风格体系</span><select v-model="assetForm.styleId" :disabled="assetSaving"><option value="">不限定风格</option><option v-for="item in studio.styles" :key="item.id" :value="item.id">{{ item.name }}</option></select></label></div>
      <label class="field"><span>名称 *</span><input v-model="assetForm.name" :disabled="assetSaving" maxlength="160" placeholder="清晰、可检索的素材名称"></label>
      <label class="field"><span>基础描述 <template v-if="assetForm.mediaType==='image'&&assetForm.imageCategory!=='reference'">*</template></span><textarea v-model="assetForm.description" :disabled="assetSaving" rows="3" placeholder="主体、用途和需要保持不变的事实"></textarea></label>
      <template v-if="assetForm.entryMode==='local'||editingAssetId"><label class="field"><span>{{ assetForm.mediaType==='image'?'视觉锚点':'检索/创作备注' }}</span><textarea v-model="assetForm.promptHint" :disabled="assetSaving" rows="4" placeholder="可选；人物、场景和道具图片必须填写"></textarea></label></template>
      <template v-else>
        <label class="field"><span>创作需求</span><textarea v-model="assetForm.brief" rows="3" placeholder="描述要生成的素材，可交给文本模型扩写为提示词"></textarea></label>
        <div class="form-grid prompt-helper-grid"><label class="field"><span>提示词文本模型</span><select v-model="assetForm.promptTargetId" :disabled="optionsLoading"><option value="">{{ optionsLoading?'加载中…':'请选择' }}</option><option v-for="item in promptOptions" :key="item.targetId" :value="item.targetId">{{ item.name }}{{ item.isExternal?' · 外部':'' }}</option></select></label><button class="button ghost" :disabled="assetSaving||!assetForm.brief.trim()||!assetForm.promptTargetId" @click="generatePrompt"><WandSparkles :size="14"/>文本模型生成提示词</button></div>
        <label class="field"><span>最终提示词 *</span><textarea v-model="assetForm.prompt" rows="6" placeholder="可直接编辑；只有这里确认的文本才会提交给素材生成方案"></textarea></label>
        <label v-if="assetForm.mediaType!=='script'&&(!selectedGenerationOption||selectedGenerationOption.acceptsNegativePrompt)" class="field"><span>负面提示词</span><textarea v-model="assetForm.negativePrompt" rows="2"></textarea></label>
        <label class="field"><span>素材生成方案 *</span><select v-model="assetForm.generationRouteKey" :disabled="assetSaving||optionsLoading"><option value="">{{ optionsLoading?'加载中…':generationOptions.length?'请选择':'没有可用方案' }}</option><option v-for="item in generationOptions" :key="routeOptionKey(item)" :value="routeOptionKey(item)">{{ routeOptionLabel(item) }}{{ item.isExternal?' · 外部':'' }}</option></select><small v-if="selectedGenerationOption?.targetKind==='comfy_workflow'">工作流 {{ selectedGenerationOption.workflowName }} v{{ selectedGenerationOption.workflowVersion }} · 运行部署 {{ selectedGenerationOption.modelDeploymentName }} · 烟测证据（Smoke）{{ selectedGenerationOption.smokeEvidenceId.slice(0,8) }}</small><small v-else-if="selectedGenerationOption">异步接口运行部署（API Deployment）· 烟测有效至 {{ dateTime(selectedGenerationOption.smokeExpiresAt) }}</small></label>
        <WorkflowExecutionInputEditor v-if="selectedGenerationOption?.targetKind==='comfy_workflow'&&selectedGenerationOption.workflowInputSchema" v-model="workflowInputDraft" :schema="selectedGenerationOption.workflowInputSchema" :assets="workflowInputAssets" :assets-loading="workflowInputAssetsLoading" :assets-error="workflowInputAssetsError" :asset-upload-pending-key="workflowInputUploadPendingKey" :disabled="assetSaving" allow-asset-upload @refresh-assets="loadWorkflowInputAssets" @reset="workflowInputUploadStatus=''" @request-asset-upload="requestWorkflowInputAssetUpload"/>
        <section v-else-if="parameterDefinitions.length" class="asset-parameter-panel"><header><span><strong>生成参数</strong><small>只展示该方案声明的可编辑参数；提示词和模型绑定由系统单独管理。</small></span><button type="button" class="button ghost tiny" @click="resetGenerationParameters">恢复默认</button></header><div class="asset-parameter-grid"><label v-for="definition in parameterDefinitions" :key="definition.key" class="field"><span>{{ definition.key }}<template v-if="definition.required"> *</template></span><select v-if="definition.type==='enum'" :value="String(parameterValue(definition.key))" @change="updateParameter(definition,$event)"><option v-if="!definition.required" value="">不设置</option><option v-for="option in definition.options" :key="String(option)" :value="String(option)">{{ option }}</option></select><select v-else-if="definition.type==='boolean'" :value="String(parameterValue(definition.key))" @change="updateParameter(definition,$event)"><option v-if="!definition.required" value="">不设置</option><option value="true">是</option><option value="false">否</option></select><input v-else :type="definition.type==='string'?'text':'number'" :value="String(parameterValue(definition.key))" :min="definition.min" :max="definition.max" :step="definition.type==='integer'?(definition.multipleOf||1):(definition.multipleOf||'any')" @change="updateParameter(definition,$event)"><small>{{ parameterConstraint(definition) }}</small></label></div></section>
        <div v-if="selectedGenerationOption?.isExternal||promptOptions.find(item=>item.targetId===assetForm.promptTargetId)?.isExternal" class="external-consent"><label><input v-model="assetForm.allowExternalCalls" type="checkbox">允许将上述创作文本发送到所选外部模型</label><label class="field"><span>预算上限 *</span><input v-model="assetForm.budgetLimit" inputmode="decimal" placeholder="例如 1.000000"></label></div>
      </template>
      <input ref="workflowInputUploadInput" type="file" :accept="workflowInputUploadAccept" hidden @change="uploadWorkflowInputAsset">
      <p v-if="workflowInputUploadStatus" class="workflow-upload-status" role="status">{{ workflowInputUploadStatus }}</p>
      <div v-if="editingAsset" class="asset-editor-source"><span>来源信息</span><p>{{ assetSourceInfo(editingAsset) }}</p><small>来源由系统记录，不能在素材信息编辑中改写。</small></div>
      <div v-if="assetForm.entryMode==='generate'&&!editingAssetId&&!optionsLoading&&(!promptOptions.length||!generationOptions.length)" class="asset-option-guidance"><AlertTriangle :size="15"/><span><small v-if="!promptOptions.length">提示词文本模型：需要已发布且启用的文本运行部署（Deployment），声明剧本生成能力（script.generate），并保持该操作能力（operation）的真实烟测有效。</small><small v-if="!generationOptions.length">素材生成模型：{{ emptyGenerationGuidance }}</small></span></div>
      <div v-if="assetError" class="asset-editor-error" role="alert"><AlertTriangle :size="14"/><span>{{ assetError }}</span><button type="button" @click="assetError=''">关闭</button></div>
    </div><template #footer><button class="button ghost" :disabled="assetSaving" @click="closeAssetModal">取消</button><button class="button primary" :disabled="assetSaving" @click="saveAsset"><Save :size="15"/>{{ workflowInputUploadPendingKey?'上传参考素材中…':assetSaving?'处理中…':editingAssetId?'保存信息':assetForm.entryMode==='local'?'上传并入库':'创建生成任务' }}</button></template></BaseModal>

    <BaseModal :open="!!deleteTarget" title="删除素材" eyebrow="删除素材（Delete Asset）" width="520px" @close="closeDeleteAsset"><div v-if="deleteTarget" class="asset-delete-confirm"><div class="asset-delete-warning"><AlertTriangle :size="18"/><span><strong>此操作无法撤销</strong><small>若素材已被短剧、剧本来源或生成审计引用，服务端会明确阻止删除。</small></span></div><label class="field"><span>输入素材名称以确认</span><input v-model="deleteConfirmName" :disabled="deletePending" :placeholder="deleteTarget.name" @keyup.enter="confirmDeleteAsset"></label><div v-if="deleteError" class="asset-editor-error"><AlertTriangle :size="14"/><span>{{ deleteError }}</span></div></div><template #footer><button class="button ghost" :disabled="deletePending" @click="closeDeleteAsset">取消</button><button class="button delete-button" :disabled="deletePending||!deleteConfirmed" @click="confirmDeleteAsset"><Trash2 :size="15"/>{{ deletePending?'删除中…':'永久删除' }}</button></template></BaseModal>
    <BaseModal :open="!!fullPreview" title="素材完整预览" eyebrow="完整预览（Full Preview）" width="1080px" @close="fullPreview=null"><div v-if="fullPreview" class="asset-full-preview"><div><AssetMediaPreview :asset="fullPreview" controls @source-error="recoverAssetPreview" @source-loaded="clearPreviewRecovery"/></div><footer><strong>{{ fullPreview.name }}</strong><button class="button primary small" @click="downloadAsset(fullPreview)"><Download :size="14"/>下载</button></footer></div></BaseModal>
  </div>
</template>

<style scoped>
.asset-command-deck{position:sticky;top:0;z-index:12;display:flex;flex-direction:column;gap:8px;padding:10px 12px;background:#0d1014;box-shadow:0 14px 30px rgba(0,0,0,.28)}
.asset-dock-primary{display:flex;align-items:center;gap:10px;min-width:0}.asset-style-row{min-width:0;flex:1;display:flex;align-items:center;gap:8px}.asset-style-row>span{flex:0 0 auto;color:#656b72;font-size:9px;font-weight:700}.asset-style-picker{min-width:0;flex:1;display:flex;grid-template-columns:none;gap:6px;overflow-x:auto;overflow-y:hidden;padding:0 0 3px;scrollbar-width:thin}.asset-style-picker button{flex:0 0 118px;min-width:118px;height:45px;padding:6px 8px}.asset-style-picker .style-picker-all{min-width:96px!important;flex-basis:96px}.asset-style-picker button span{font-size:5px}.asset-style-picker button strong{font-size:9px}.asset-dock-actions{display:flex;align-items:center;gap:6px;flex:0 0 auto}.asset-dock-actions .button{white-space:nowrap}
.asset-command-deck .asset-toolbar{padding:7px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border);border-radius:0;background:transparent}.asset-command-deck .asset-tabs{min-width:0;overflow-x:auto}.asset-command-deck .asset-tabs button{flex:0 0 auto}.asset-command-deck .toolbar-right{flex:0 0 auto}.asset-filter-footer{min-height:27px;display:flex;align-items:center;justify-content:space-between;gap:10px}.asset-filter-footer .result-bar{margin-left:auto;gap:16px}.asset-subtabs{display:flex;gap:6px;min-width:0;overflow-x:auto}.asset-subtabs button,.asset-kind-picker button{padding:7px 11px;border:1px solid var(--border);border-radius:6px;background:#101318;color:#737980;font-size:10px;cursor:pointer;white-space:nowrap}.asset-subtabs button.active,.asset-kind-picker button.active{border-color:rgba(104,197,167,.45);background:rgba(104,197,167,.08);color:#77cdb2}.asset-command-deck button:focus-visible,.asset-command-deck a:focus-visible{outline:1px solid var(--accent);outline-offset:2px}
.asset-load-error{display:flex;align-items:center;gap:8px;padding:10px;border:1px solid rgba(239,115,123,.3);border-radius:7px;color:#e4868d}.asset-load-error span{flex:1;font-size:10px}.asset-load-error button{border:0;background:transparent;color:inherit;cursor:pointer}.asset-detail-image{padding:0;border:0}.asset-prompt-hint p{padding:10px;border:1px solid rgba(104,197,167,.18);border-radius:7px;background:rgba(104,197,167,.04);color:#90ada3;font-size:10px;line-height:1.65}.asset-readonly{display:flex;gap:8px;margin:12px 0;padding:9px;border:1px solid rgba(218,154,70,.2);border-radius:7px;color:#b28a54}.asset-readonly span{font-size:9px;line-height:1.5}.asset-detail-actions{display:flex;gap:8px;margin-top:18px}.asset-detail-actions .button{flex:1}.button.danger-ghost{border-color:rgba(239,115,123,.35);color:#e4868d;background:rgba(239,115,123,.04)}.delete-button{border-color:#c2555f;background:#a94049;color:#fff}.asset-editor{display:flex;flex-direction:column;gap:12px}.asset-entry-mode{align-self:stretch}.asset-kind-picker{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}.asset-kind-picker button{display:flex;align-items:center;justify-content:center;gap:6px}.asset-editor-evidence{display:flex;gap:10px;padding:12px;border:1px solid rgba(104,197,167,.23);border-radius:8px;background:rgba(104,197,167,.05);color:#65c3a5}.asset-editor-evidence span{display:flex;flex-direction:column;gap:4px}.asset-editor-evidence strong{font-size:11px;color:#a9d9ca}.asset-editor-evidence small{font-size:9px;line-height:1.55;color:#71958a}.asset-upload-box{height:190px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:7px;overflow:hidden;border:1px dashed #38414a;border-radius:8px;background:#0b0e12;color:#707880;cursor:pointer}.asset-upload-box img,.asset-upload-box video{width:100%;height:100%;object-fit:contain}.asset-upload-box strong{font-size:11px;color:#b8bec4}.prompt-helper-grid{grid-template-columns:1fr auto;align-items:end}.prompt-helper-grid .button{height:38px;margin-bottom:1px}.external-consent{display:grid;grid-template-columns:1fr 220px;align-items:center;gap:12px;padding:11px;border:1px solid rgba(218,154,70,.25);border-radius:7px;background:rgba(218,154,70,.04)}.external-consent>label:first-child{display:flex;align-items:center;gap:8px;color:#b89462;font-size:9px}.asset-editor-error{display:flex;align-items:flex-start;gap:8px;padding:9px 10px;border:1px solid rgba(239,115,123,.28);border-radius:7px;background:rgba(239,115,123,.06);color:#e4868d}.asset-editor-error span{flex:1;font-size:10px;line-height:1.5}.asset-editor-error button{border:0;background:transparent;color:inherit;cursor:pointer}.asset-delete-confirm{display:flex;flex-direction:column;gap:16px}.asset-delete-warning{display:flex;gap:10px;padding:13px;border:1px solid rgba(239,115,123,.28);border-radius:8px;background:rgba(239,115,123,.06);color:#e4868d}.asset-delete-warning span{display:flex;flex-direction:column;gap:5px}.asset-delete-warning small{font-size:9px;line-height:1.6}.asset-full-preview{display:flex;flex-direction:column;gap:10px}.asset-full-preview>div{width:100%;height:min(76vh,760px);border-radius:8px;overflow:hidden;background:#07090c}.asset-full-preview footer{display:flex;align-items:center;justify-content:space-between}.asset-full-preview strong{font-size:12px}
.asset-detail-copy,.asset-editor-source{min-width:0}.asset-source-info p,.asset-editor-source p{margin:0;overflow-wrap:anywhere;word-break:break-word;color:#9aa1a8;font-size:10px;line-height:1.6}.asset-info-missing,.asset-prompt-hint.asset-info-missing p{color:#d99c55}.asset-prompt-hint.asset-info-missing p{border-color:rgba(218,154,70,.24);background:rgba(218,154,70,.05)}.asset-editor-source{display:flex;flex-direction:column;gap:5px;padding:10px 12px;border:1px solid var(--border);border-radius:7px;background:#0b0e12}.asset-editor-source>span{color:#777e85;font-size:9px;font-weight:700}.asset-editor-source small{color:#656c73;font-size:8px}
.workflow-upload-status{margin:0;padding:9px 10px;border:1px solid rgba(104,197,167,.28);border-radius:7px;background:rgba(104,197,167,.06);color:#8fcab7;font-size:10px;line-height:1.5}
@media(max-width:980px){.asset-dock-primary{align-items:stretch;flex-direction:column}.asset-dock-actions{justify-content:flex-end}.asset-command-deck .asset-toolbar{align-items:stretch;flex-direction:row}.asset-command-deck .search-input{width:210px}}
@media(max-width:780px){.asset-command-deck{margin-inline:-6px;padding:8px}.asset-dock-actions .button{flex:1}.asset-command-deck .asset-toolbar{flex-direction:column}.asset-command-deck .toolbar-right{width:100%}.asset-command-deck .search-input{width:auto;flex:1}.asset-filter-footer{align-items:stretch;flex-direction:column}.asset-filter-footer .result-bar{width:100%;margin-left:0}.asset-kind-picker{grid-template-columns:repeat(2,1fr)}.asset-editor .form-grid,.external-consent{grid-template-columns:1fr}.asset-detail{grid-template-columns:1fr}.asset-detail-image{height:330px}.asset-detail-actions{flex-wrap:wrap}}
.asset-option-guidance{display:flex;align-items:flex-start;gap:9px;padding:10px 11px;border:1px solid rgba(218,154,70,.28);border-radius:7px;background:rgba(218,154,70,.05);color:#d39a52}.asset-option-guidance span{display:flex;flex-direction:column;gap:5px}.asset-option-guidance small{font-size:9px;line-height:1.55;color:#b89462}
.asset-parameter-panel{display:flex;flex-direction:column;gap:10px;padding:11px;border:1px solid rgba(104,197,167,.2);border-radius:8px;background:rgba(104,197,167,.035)}.asset-parameter-panel>header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.asset-parameter-panel>header span{display:flex;flex-direction:column;gap:3px}.asset-parameter-panel>header strong{font-size:10px;color:#b8d6cc}.asset-parameter-panel>header small,.asset-parameter-grid .field>small{font-size:8px;line-height:1.5;color:#6f827c}.asset-parameter-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
@media(max-width:780px){.asset-parameter-grid{grid-template-columns:1fr}}
</style>
