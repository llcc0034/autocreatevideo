// Only transport fixtures. No real upload, business selection, approval or model calls.
import {createApp, h, ref} from 'vue'
import ManualShotReferenceSelection from '../../src/components/ManualShotReferenceSelection.vue'
import RecipeShotReferenceSelection from '../../src/components/RecipeShotReferenceSelection.vue'
import {createRouter, createMemoryHistory, RouterView} from 'vue-router'
import {platformApi} from '../../src/services/platformApi'
import {repository} from '../../src/services/repository'
import {assetReferenceApi} from '../../src/services/assetReferenceApi'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'
api.interceptors.request.use(() => {throw Error('隔离夹具禁止真实业务API')})
const id=(n:number)=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`, sha='a'.repeat(64)
const open=ref(true), mode=ref(new URLSearchParams(location.search).get('case') || 'ready'), epoch=ref(0), reads=ref(0), writes=ref(0), uploads=ref(0), requests=ref('尚未提交')
const retained=ref({dirty:false,pending:false}), busy=ref(false), completed=new Map<string,any>()
const picture='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120"><rect width="240" height="120" fill="#315d69"/><text x="20" y="66" fill="white" font-size="20">ISOLATED ASSET</text></svg>')
const asset:any={id:id(5),name:'旅人 · 雨衣参考图（隔离）',type:'image',mimeType:'image/png',checksum:sha,isActive:true,tags:[],mediaUrl:picture}
const uploaded={...asset,id:id(15),name:'本次上传（仅隔离夹具）'}
const series:any={id:id(7),name:'旅人雨衣系列',role:'primary',owner_id:1,primary_series_id:null,bible_variant_id:null,version_count:1,variant_count:0}
const version:any={id:id(6),series_id:id(7),version:2,source:'generated',asset:{id:asset.id,name:asset.name,kind:'image',mime_type:'image/png',is_active:true},generated_from_id:null,setting_revision_id:null}
const saved:Record<string,any>={}
const recipe = new URLSearchParams(location.search).get('plan') === 'recipe'
const run:any={id:id(1),project:id(2),planSource:recipe?'recipe':'manual',status:recipe?'awaiting_approval':'awaiting_input',executionPlan:{sha256:sha}}
const options:any={runId:run.id,runPlanSha256:sha,actions:[{stageKey:'storyboard_review',action:'select',scopeKinds:['shot']}]}
platformApi.listProductionTargets=async()=>({runId:run.id,runPlanSha256:sha,count:3,next:null,previous:null,
  results:[3,30,40].map((n,i)=>({scope:{kind:'shot',id:id(n)},unitKey:`shot:${id(n)}`,label:`第1集 · 镜头${i+1} · ${i?'车内对话':'旅人穿雨衣穿过长街'}`}))}) as any
platformApi.getManualShotReferences=async(_run,shot)=>{
  reads.value++;if(mode.value==='read_error')throw Error('隔离基线读取失败')
  const key=Number(shot.slice(-2)), uses=[{id:id(key+1),shot_id:shot,use_key:'雨中旅人',position:0,purpose:'',media_kind:'image',setting_revision_id:null,
    source_mode:'',reference_version_id:null,asset_id:null,asset_sha256:'',...saved[`${shot}:${id(key+1)}`]},
  {id:id(key+100),shot_id:shot,use_key:'另一处外观参考',position:1,purpose:'同一人物在本镜的独立参考用途',media_kind:'image',setting_revision_id:null,source_mode:'',reference_version_id:null,asset_id:null,asset_sha256:'',...saved[`${shot}:${id(key+100)}`]}]
  return {run_id:run.id,stage_key:'storyboard_review',run_plan_sha256:sha,shot_id:shot,storyboard_adoption_revision:2,
    storyboard_adoption_sha256:'b'.repeat(64),uses_sha256:'c'.repeat(64),state:{schema_version:1,project_id:run.project,shot_id:shot,uses:mode.value==='empty'?[]:uses},
    editability:{allowed:mode.value!=='readonly',code:mode.value==='readonly'?'shot_prepared':'editable',reason:mode.value==='readonly'?'本镜已有冻结准备，只读；其他镜头独立。':'仅修改本镜用途，不等于审核或请求就绪。'}} as any
}
platformApi.getRecipeShotReferences=async(_run,shot)=>{
  const detail:any=await platformApi.getManualShotReferences(_run,shot)
  delete detail.storyboard_adoption_revision;delete detail.storyboard_adoption_sha256
  const source=mode.value==='existing'?'existing':mode.value==='upload'?'upload':'upstream'
  return {...detail,source_snapshot_sha256:'b'.repeat(64),selection_rule:{rule:{source,source_stage_key:source==='upstream'?'bible_image_generation':null,
    selection:source==='upstream'?'unique_setting':'manual',allow_override:mode.value!=='no_override'},
    upstream:source==='upstream'?{stage_run_id:id(10),output_snapshot_sha256:'b'.repeat(64)}:null},upstream_candidates:source==='upstream'?[{
      source:{stage_run_id:id(10),output_snapshot_sha256:'b'.repeat(64),field:'material_generations',reference:{decision_id:id(11),reference_version_id:id(6)}},
      source_mode:'generated',reference_version_id:id(6),version:2,setting_revision_id:null,asset_id:asset.id,asset_sha256:sha,name:asset.name,mime_type:asset.mimeType}]:[]}
}
platformApi.performManualProductionAction=async(_run,request)=>{
  writes.value++;requests.value=JSON.stringify(request)
  const key=String(request.idempotency_key);if(completed.has(key))return completed.get(key)
  const shot=String((request.scope as any).id)
  if(mode.value==='conflict' || (mode.value==='batch_mixed' && shot===id(3)))throw {response:{status:409,data:{detail:'隔离用途基线冲突'}}}
  for (const choice of (request.payload as any).choices) {saved[`${shot}:${choice.use_id}`]={...choice};delete saved[`${shot}:${choice.use_id}`].use_id}
  const result={id:`fixture-receipt-${writes.value}`,action:'select',result_kind:'shot_reference_choices',target_id:shot};completed.set(key,result)
  if(mode.value==='timeout' || (mode.value==='batch_unknown' && shot===id(3)))throw Error('隔离超时：服务端已保存，响应丢失')
  return result
}
repository.getAssetPage=async()=>({count:1,next:null,previous:null,results:[asset]}) as any
repository.getAsset=async(pk)=>{if(mode.value==='preview_error')throw Error('隔离素材预览失效');return pk===uploaded.id?uploaded:asset}
repository.createAsset=async()=>{uploads.value++;return uploaded}
assetReferenceApi.roles=async()=>[{value:'primary',display_name:'主图'},{value:'variant',display_name:'变体'}]
assetReferenceApi.listSeries=async()=>({count:1,next:null,previous:null,results:[series]})
assetReferenceApi.listVersions=async()=>({count:1,next:null,previous:null,results:[version]})
assetReferenceApi.getVersion=async()=>version
assetReferenceApi.getSeries=async()=>series
const fixture={setup(){return()=>h('main',{style:{padding:'16px'}},[
  h('h1',`${recipe?'配方':'人工'}单镜选材 · 隔离验收`),h('p','正式面板和选择器；所有响应均为隔离夹具，无业务API、真实文件入库、审核或模型调用。'),
  h('label',['场景 ',h('select',{value:mode.value,disabled:busy.value,onChange:(e:Event)=>{mode.value=(e.target as HTMLSelectElement).value}},
    ['ready','timeout','conflict','batch_mixed','batch_unknown','readonly','empty','read_error','preview_error',...(recipe?['existing','upload','no_override']:[])].map(value=>h('option',{value},value)))]),
  !recipe?h('button',{onClick:()=>open.value=true},'继续单镜选材'):null,
  h('button',{disabled:busy.value||retained.value.pending,onClick:()=>{epoch.value++;open.value=true}},'重置隔离面板'),
  h('output',{id:'counts'},`基线读取 ${reads.value}；命令 ${writes.value}；隔离上传 ${uploads.value}；草稿 ${retained.value.dirty}；待核对 ${retained.value.pending}`),
  recipe?h(RecipeShotReferenceSelection,{key:epoch.value,run,disabled:false,onContextProtected:(value:boolean)=>busy.value=value})
    :h(ManualShotReferenceSelection,{key:epoch.value,open:open.value,run,options,onClose:()=>open.value=false,onBusy:(value:boolean)=>busy.value=value,onRetained:(value:any)=>retained.value=value}),
  h('details',[h('summary','本次隔离请求'),h('pre',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},requests.value)])
])}}
const fixtureRouter=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:fixture}]})
await fixtureRouter.push('/')
await fixtureRouter.isReady()
createApp({render:()=>h(RouterView)}).use(fixtureRouter).mount('#app')
