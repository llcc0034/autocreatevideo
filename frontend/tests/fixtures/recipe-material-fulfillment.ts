// Real Run panel and shared picker; all business APIs are isolated fixtures.
import {createApp,h,ref} from 'vue'
import {createRouter,createMemoryHistory} from 'vue-router'
import RecipeMaterialFulfillment from '../../src/components/RecipeMaterialFulfillment.vue'
import {platformApi} from '../../src/services/platformApi'
import {assetReferenceApi} from '../../src/services/assetReferenceApi'
import {repository} from '../../src/services/repository'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'
api.interceptors.request.use(()=>{throw Error('隔离夹具禁止真实业务 API')})
const id=(n:number)=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`,sha='a'.repeat(64)
const mode=ref<'existing'|'upload'|'generate'>('existing'),failure=ref(false),posts=ref(0),uploads=ref(0),receipt=ref<any>(null),readsFail=ref(false)
const version=(page=1):any=>({id:id(page===1?7:17),series_id:id(4),version:page,source:'generated',
  asset:{id:id(5),name:'隔离原生成版本',mime_type:'image/svg+xml',kind:'reference',is_active:true},
  setting_revision_id:id(page===1?33:3),generated_from_id:null})
const asset:any={id:id(5),name:'隔离成图（临时示意）',type:'image',isActive:true,mimeType:'image/svg+xml',checksum:sha,metadata:{},
  mediaUrl:'data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="180" height="90"><rect width="180" height="90" fill="#294557"/><text x="25" y="52" fill="white">ISOLATED FIXTURE</text></svg>')}
platformApi.getMaterialFulfillmentContext=async(runId,stageId,unitKey)=>{
  if(readsFail.value)throw Error('夹具：读取失败，请重试')
  return {stageRunId:stageId,unitKey,requirementKey:`setting:${id(3)}`,runPlanSha256:sha,appearanceId:id(2),settingRevisionId:id(3),
    appearanceRole:'primary',referenceName:'林晓 · 正常外观',primaryAppearanceId:null,sourceMode:mode.value,canSelect:!receipt.value,
    reason:receipt.value?'本项已有明确选择，不覆盖':'' ,selection:receipt.value}
}
platformApi.fulfillMaterial=async(_run,ctx,choice)=>{
  posts.value++
  receipt.value={id:id(6),referenceVersionId:'referenceVersionId' in choice?choice.referenceVersionId:id(7),
    seriesId:'referenceVersionId' in choice?id(4):choice.seriesId,assetId:'referenceVersionId' in choice?id(5):choice.assetId,
    source:'referenceVersionId' in choice?'generated':choice.source,targetSha256:sha,selectionMode:'manual',selectedById:1,createdAt:'2026-10-02T06:00:00Z'}
  if(failure.value)throw Error('夹具：服务端已保存但响应中断')
  return receipt.value
}
assetReferenceApi.listSeries=async()=>({count:1,next:null,previous:null,results:[{id:id(4),name:'林晓 正常外观 · 隔离系列',role:'primary',owner_id:1,primary_series_id:null,bible_variant_id:id(2),version_count:2,variant_count:0}]})
assetReferenceApi.listVersions=async({page})=>({count:2,next:page===1?'isolated-next':null,previous:page===2?'isolated-previous':null,results:[version(page)]})
assetReferenceApi.getVersion=async(versionId)=>version(versionId===id(7)?1:2)
repository.getAsset=async()=>asset
repository.getAssetPage=async()=>({count:1,next:null,previous:null,results:[asset]})
repository.createAsset=async()=>{uploads.value++;return asset}
const run:any={id:id(9),planSource:'recipe',status:'running',executionPlan:{sha256:sha}}
const stage:any={id:id(1),stageKey:'bible_image_generation',status:'running',inputSnapshotSha256:sha,inputSnapshot:{
  units:[{unit_key:`appearance:${id(2)}`,requirement_key:`setting:${id(3)}`,appearance_role:'primary'}],
  material_requirements:{run_plan_sha256:sha,targets:[{requirement_key:`setting:${id(3)}`,appearance_role:'primary',reference_name:'林晓 · 正常外观',setting:{appearance_id:id(2),setting_revision_id:id(3)}}]}}}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}},{path:'/assets',name:'assets',component:{render:()=>null}}]})
createApp({setup(){const key=ref(0),disabled=ref(false),busy=ref(false)
  function reset(source:'existing'|'upload'|'generate'){
    mode.value=source;receipt.value=null;posts.value=0;uploads.value=0;key.value++
    run.status=source==='generate'?'awaiting_input':'running';run.blockedCode=source==='generate'?'waiting_material_recovery':''
    run.currentStageKey=stage.stageKey;stage.reasonCode=run.blockedCode
  }
  return()=>h('main',{style:{maxWidth:'1040px',padding:'16px',margin:'0 auto'}},[
    h('h1','运行成图履约 · 隔离验收'),h('p','正式组件与原选材/上传控件，所有业务 HTTP 禁用。未改变现用数据。'),
    h('nav',{style:{display:'flex',flexWrap:'wrap',gap:'12px'}},[
      h('button',{onClick:()=>reset('existing'),disabled:busy.value},'重置已有图场景'),h('button',{onClick:()=>reset('upload'),disabled:busy.value},'重置上传场景'),
      h('button',{onClick:()=>reset('generate'),disabled:busy.value},'全部失败接管场景'),
      h('button',{onClick:()=>{reset('generate');run.status='running';run.blockedCode='';stage.reasonCode='waiting_execution_preparation'},disabled:busy.value},'零调用受阻接管场景'),
      h('button',{onClick:()=>disabled.value=!disabled.value},disabled.value?'恢复编辑':'只读模式'),
      h('button',{onClick:()=>failure.value=!failure.value},failure.value?'关闭响应中断':'启用响应中断'),
      h('button',{onClick:()=>readsFail.value=!readsFail.value},readsFail.value?'恢复读取':'启用读取失败')]),
    h(RecipeMaterialFulfillment,{key:key.value,run,stage,disabled:disabled.value||busy.value,onBusy:(v:boolean)=>busy.value=v}),
    h('output',{style:{display:'block',overflowWrap:'anywhere'}},`隔离 POST 次数 ${posts.value} · 隔离上传次数 ${uploads.value} · 真实 API 次数 0`)
  ])}}).use(router).mount('#app')
