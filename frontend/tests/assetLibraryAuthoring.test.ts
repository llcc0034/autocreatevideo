import test from 'node:test'
import assert from 'node:assert/strict'
import {createServer} from 'vite'
import {createSSRApp,h,ref,nextTick} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {createPinia,setActivePinia} from 'pinia'
import {createRouter,createMemoryHistory,matchedRouteKey} from 'vue-router'
Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=>null,setItem(){},removeItem(){}}})
const base={id:'asset-1',type:'image',backendKind:'reference',name:'原素材 🟢',description:'原事实',promptHint:'',sourceInfo:'原生成来源已清理',styleId:'',ownerId:'9',tags:[],checksum:'a'.repeat(64),metadata:{cleared_history_origin:true},isActive:true}
async function setup(t:any){
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});t.after(()=>vite.close())
 const pinia=createPinia();setActivePinia(pinia)
 const {useAuthStore}=await vite.ssrLoadModule('/src/stores/auth.ts'),{useStudioStore}=await vite.ssrLoadModule('/src/stores/studio.ts'),{repository}=await vite.ssrLoadModule('/src/services/repository.ts'),{default:View}=await vite.ssrLoadModule('/src/views/AssetLibraryView.vue')
 useAuthStore(pinia).user={id:9,isStaff:true};const studio=useStudioStore(pinia)
 const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',name:'assets',component:{render:()=>null}}]});await router.push('/');await router.isReady()
 let state:any;const app=createSSRApp({setup(){state=View.setup({},{expose(){}});return()=>h('div')}}).use(pinia).use(router);app.provide(matchedRouteKey,ref({leaveGuards:new Set(),updateGuards:new Set()}));await renderToString(app)
 studio.refreshAssets=async()=>[];return{vite,studio,repository,state,router}
}
test('asset edit has own metadata sections and closing preserves unsaved multilingual facts',async t=>{
 const {state}=await setup(t);state.openEditAsset({...base});assert.deepEqual(state.sections.value.map((s:any)=>s.key),['content','metadata','result']);assert.equal(state.assetDirty.value,false)
 state.assetForm.value.description='原事实 中文 🟠';state.closeAssetModal();assert.ok(state.discardAction.value);state.keepEditing();assert.equal(state.assetForm.value.description,'原事实 中文 🟠');assert.equal(state.assetModalOpen.value,true)
 state.closeAssetModal();state.resolveDiscard(true);assert.equal(state.assetModalOpen.value,false)
})
test('metadata edit uses original writer, clears style and never sends source or file',async t=>{
 const {state,studio}=await setup(t);state.openEditAsset({...base,styleId:'old'});state.assetForm.value.styleId='';let captured:any
 studio.updateAssetMetadata=async(id:any,payload:any)=>{captured={id,payload};return{...base,...payload}};await state.saveAsset()
 assert.equal(captured.id,base.id);assert.equal(captured.payload.styleId,undefined);assert.equal(Object.hasOwn(captured.payload,'sourceInfo'),false);assert.equal(Object.hasOwn(captured.payload,'file'),false);assert.equal(state.selected.value.id,base.id)
})
test('media editing keeps the original preview and upload labels do not invent usage duration limits',async t=>{
 const {state}=await setup(t);state.openEditAsset({...base,type:'video',backendKind:'video',thumbnail:'/media/poster.png',mediaUrl:'/media/original-video.mp4'});await nextTick();assert.equal(state.editingAsset.value.mediaUrl,'/media/original-video.mp4');assert.equal(state.uploadPreview.value,'');assert.equal(state.uploadLabel.value,'MP4 / MOV / WebM')
 state.resetAssetModal();state.openCreateAsset('local','audio');await nextTick();assert.equal(state.uploadPreview.value,'');assert.equal(state.uploadLabel.value,'WAV / MP3 / FLAC / M4A / OGG')
})

test('video audio and script upload then edit only own facts through original writers',async t=>{
 const {state,studio}=await setup(t)
 for(const [mediaType,mime,ext,kind] of [['video','video/mp4','mp4','reference_video'],['audio','audio/wav','wav','reference_audio'],['script','text/markdown','md','script']]){
  state.resetAssetModal();state.openCreateAsset('local',mediaType);await nextTick()
  const file=new File(['仅交互夹具，不代表媒体探测通过'],`原文件.${ext}`,{type:mime});state.selectUploadFile({target:{files:[file]}})
  Object.assign(state.assetForm.value,{name:`原名 ${mediaType} 🟢`,description:'',promptHint:'原文备注'})
  let create:any,update:any;const original={...base,id:mediaType,type:mediaType,backendKind:kind,mediaUrl:`/media/${mediaType}`,thumbnail:'',metadata:{...base.metadata,duration_ms:4200}}
  studio.createAsset=async(payload:any)=>{create=payload;return original};await state.saveAsset()
  assert.equal(create.file,file);assert.equal(create.mediaType,mediaType);assert.equal(create.description,'');assert.equal(create.promptHint,'原文备注');assert.equal(Object.hasOwn(create,'sourceInfo'),false)
  state.openEditAsset(original);await nextTick();state.assetForm.value.description='编辑中文 🟠'
  studio.updateAssetMetadata=async(id:any,payload:any)=>{update={id,payload};return{...original,...payload}};await state.saveAsset()
  assert.equal(update.id,mediaType);assert.equal(update.payload.kind,undefined);assert.equal(Object.hasOwn(update.payload,'file'),false);assert.equal(Object.hasOwn(update.payload,'metadata'),false);assert.equal(Object.hasOwn(update.payload,'sourceInfo'),false)
  assert.equal(state.selected.value.mediaUrl,original.mediaUrl);assert.deepEqual(state.selected.value.metadata,original.metadata);assert.equal(state.selected.value.sourceInfo,original.sourceInfo)
 }
})

test('editing signed-media refresh keeps unsaved metadata and only updates original asset preview',async t=>{
 const {state,studio}=await setup(t);const original={...base,type:'audio',mediaUrl:'/old.wav',thumbnail:''};state.openEditAsset(original);state.assetForm.value.name='未保存 🟢'
 studio.refreshAsset=async(id:any)=>{assert.equal(id,base.id);return{...original,mediaUrl:'/fresh.wav'}}
 await state.recoverAssetPreview({assetId:base.id,source:'/old.wav'});assert.equal(state.editingAsset.value.mediaUrl,'/fresh.wav');assert.equal(state.assetForm.value.name,'未保存 🟢');assert.equal(state.assetDirty.value,true)
})

test('media facts show recorded values and separate missing, zero, false and malformed without guessed fallback',async t=>{
 const {vite}=await setup(t);const {default:Facts}=await vite.ssrLoadModule('/src/components/AssetLibraryFacts.vue')
 const render=(asset:any)=>renderToString(createSSRApp(Facts,{asset:{...base,...asset}}))
 const video=await render({type:'video',mimeType:'video/mp4',metadata:{duration_ms:4200,source_fps:24,has_audio:false,size_bytes:0}})
 for(const value of ['4200 毫秒','24 fps','0 字节'])assert.ok(video.includes(value),value)
 assert.match(video,/内嵌音频<\/dt><dd[^>]*>无/)
 const missing=await render({type:'video',metadata:{fps:30,has_audio:'false',duration_ms:'4200'}});assert.match(missing,/来源帧率<\/dt><dd[^>]*>未记录/);assert.match(missing,/记录格式异常/);assert.doesNotMatch(missing,/30 fps|4200 毫秒/)
 const script=await render({type:'script',metadata:{script_source_format:'docx',script_normalization:'utf8_markdown_v1',cleared_history_origin:true}})
 for(const value of ['docx','UTF-8 Markdown 规范化','原业务来源历史已清理','来源由系统记录'])assert.ok(script.includes(value),value)
 assert.doesNotMatch(script,/执行实例版本|创建后继|<input|<textarea/)
})
test('pending navigation confirmation cannot be overwritten or bypassed by save',async t=>{
 const {state,studio}=await setup(t);state.openEditAsset({...base});state.assetForm.value.name='未保存';let writes=0;studio.updateAssetMetadata=async()=>{writes++;return base}
 const leaving=state.canLeave(),action=state.discardAction.value;assert.equal(state.canLeave(),false);state.closeAssetModal();assert.equal(state.discardAction.value,action);await state.saveAsset();assert.equal(writes,0)
 state.keepEditing();assert.equal(await leaving,false);assert.equal(state.assetModalOpen.value,true)
 const next=state.canLeave();state.resolveDiscard(true);assert.equal(await next,true);assert.equal(state.assetModalOpen.value,false)
})
test('required values and explicit API fields are located without dropping author values',async t=>{
 const {state,studio}=await setup(t);state.openCreateAsset();await state.saveAsset();assert.equal(state.assetIssues.value[0].section,'metadata')
 state.openEditAsset({...base});state.assetForm.value.description='保留';studio.updateAssetMetadata=async()=>{throw{response:{status:400,data:{error:{fields:{description:['服务端错误']}}}}}};await state.saveAsset();assert.equal(state.assetOutcomeUnknown.value,false);assert.equal(state.assetIssues.value[0].section,'metadata');assert.equal(state.assetForm.value.description,'保留')
})
test('unknown upload cannot replay and same-name readback never silently confirms it',async t=>{
 const {state,studio,repository}=await setup(t);state.openCreateAsset();Object.assign(state.assetForm.value,{imageCategory:'reference',name:base.name,file:new File(['x'],'fixture.png',{type:'image/png'})});let writes=0
 studio.createAsset=async()=>{writes++;throw{response:{status:504}}};await state.saveAsset();await state.saveAsset();assert.equal(writes,1);assert.equal(state.assetOutcomeUnknown.value,true);assert.match(state.assetError.value,/不要重复提交/);assert.doesNotMatch(state.assetError.value,/请重试/)
 repository.getAssetPage=async()=>({count:1,next:'page2',previous:null,results:[base]});await state.readUnknown();assert.equal(state.assetOutcomeUnknown.value,true);assert.equal(state.recoveryNext.value,true);assert.equal(state.selected.value,null)
 state.changeEntryMode('generate');assert.equal(state.assetForm.value.entryMode,'local');state.changeAssetMedia('video');assert.equal(state.assetForm.value.mediaType,'image')
 studio.refreshAsset=async(id:any)=>({...base,id});await state.inspectRecovered(base);assert.equal(writes,1);assert.equal(state.assetModalOpen.value,false);assert.equal(state.selected.value.id,base.id)
})
test('unknown update reads exact ID and read failure does not permit another write',async t=>{
 const {state,studio}=await setup(t);state.openEditAsset({...base});let writes=0;studio.updateAssetMetadata=async()=>{writes++;throw{response:{status:502}}};await state.saveAsset();studio.refreshAsset=async(id:any)=>{assert.equal(id,base.id);throw Error('offline')};await state.readUnknown();await state.saveAsset();assert.equal(writes,1);assert.equal(state.recoveryError.value,'offline');assert.equal(state.assetOutcomeUnknown.value,true)
})
test('relation query scopes exact asset, honours paging and ignores stale responses',async t=>{
 const {vite}=await setup(t);const {assetReferenceApi}=await vite.ssrLoadModule('/src/services/assetReferenceApi.ts'),{default:Facts}=await vite.ssrLoadModule('/src/components/AssetLibraryFacts.vue');let state:any
 const app=createSSRApp({setup(){state=Facts.setup({asset:base},{expose(){}});return()=>h('div')}});await renderToString(app);let finish:any;assetReferenceApi.listSeries=async()=>new Promise(r=>finish=r);const old=state.loadRelations()
 assetReferenceApi.listSeries=async(q:any)=>{assert.equal(q.asset_id,base.id);assert.equal(q.page,2);return{count:1,next:null,previous:'p1',results:[]}};await state.loadRelations(2);finish({count:99,results:[],next:null,previous:null});await old;assert.equal(state.relations.value.count,1);assert.equal(state.page.value,2)
 const html=await renderToString(createSSRApp(Facts,{asset:base}));assert.match(html,/原业务来源历史已清理/);assert.match(html,/不代表全部项目或任务引用/);assert.doesNotMatch(html,/<input|<textarea|<select/)
})

test('unknown generation blocks close, local-mode switch and navigation without discarding the request owner',async t=>{
 const {state}=await setup(t);state.openCreateAsset('generate','video');state.generationUnknown.value=true;state.generationLocked.value=true;state.generationDirty.value=true
 assert.match(state.authorStatus.value,/结果未知/);assert.doesNotMatch(state.authorStatus.value,/有未提交输入/)
 state.closeAssetModal();assert.equal(state.assetModalOpen.value,true);assert.equal(state.discardAction.value,null);assert.match(state.assetPhase.value,/原请求/)
 assert.equal(state.canLeave(),false);state.changeEntryMode('local');state.changeAssetMedia('image');assert.equal(state.assetForm.value.mediaType,'video');assert.equal(state.assetForm.value.entryMode,'generate')
})

test('late prompt-job response cannot reset another route or filled author; explicit discard is required',async t=>{
 const {state,studio,router}=await setup(t);let finish:any
 await router.replace({query:{promptJob:'old'}});studio.refreshAssetGenerationJobs=()=>new Promise(r=>finish=r);const old=state.consumeRouteContext()
 await router.replace({query:{promptJob:'new'}});finish([{id:'old',jobType:'prompt',status:'succeeded',mediaType:'video',resultText:'old'}]);await old;assert.equal(state.assetModalOpen.value,false);assert.equal(router.currentRoute.value.query.promptJob,'new')
 state.openCreateAsset('generate','image');state.assetForm.value.name='原名';state.generationDirty.value=true
 studio.refreshAssetGenerationJobs=async()=>[{id:'new',jobType:'prompt',status:'succeeded',mediaType:'video',resultText:'候选 中文 🟢'}]
 await state.consumeRouteContext();assert.equal(state.assetForm.value.name,'原名');assert.equal(state.assetForm.value.mediaType,'image');assert.ok(state.discardAction.value)
 state.keepEditing();assert.equal(state.assetForm.value.name,'原名');assert.equal(state.promptCandidate.value,'')
})
