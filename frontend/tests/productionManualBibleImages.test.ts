import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createRouter, createMemoryHistory } from 'vue-router'

const root=fileURLToPath(new URL('..',import.meta.url)),sha='a'.repeat(64)
const id=(n:number)=>`10000000-0000-4000-8000-${String(n).padStart(12,'0')}`
const run={id:id(1),project:id(2),planSource:'manual',status:'awaiting_input',executionPlan:{sha256:sha},stages:[{id:id(3),stageKey:'bible_image_generation',status:'pending'}]}
const options={runId:run.id,runPlanSha256:sha,actions:[{stageKey:'bible_image_review',action:'approve'}]}
const candidate={artifact_id:id(5),variant_group:'opaque-reviewed-result',variant_index:1,eligibility:'eligible',sha256:sha,manifest_sha256:sha,
  attempt_id:id(6),preparation_id:id(7),execution_instance_version_id:id(8),request_sha256:sha,prompt_profile_version_id:null,target_snapshot_sha256:sha,
  origin_production_run_id:run.id,historical:false}
const entity={entity_type:'prop',entity_id:id(4),entity_name:'钥匙',target_content_sha256:sha,candidates:[candidate]}
const raw={run_id:run.id,run_plan_sha256:sha,candidates:{generation_stage_id:id(3),generation_stage_attempt_count:0,entities:[entity]},candidates_sha256:sha,
  targets:[{schema_version:2,project_id:run.project,scope:{kind:'prop',id:id(4)},name:'钥匙',description:'铜制钥匙',primary_setting:{appearance_id:id(41),setting_revision_id:id(42),revision:2,content_sha256:sha},anchor_asset_id:null,reference_asset_ids:[]}],
  targets_sha256:sha,content_basis_sha256:sha,content_basis:{script_sha256:sha,bible_sha256:sha,visual_rules_sha256:sha,upstream_reviews_sha256:sha},
  maximum_selected_images:3,approval:{revision:0,decision_id:null,snapshot_sha256:null,status:'not_reviewed',changes:[]}}
const assets=[{id:id(9),type:'prop',isActive:true,name:'钥匙侧面',mimeType:'image/png',checksum:sha,mediaUrl:'/media/side.png',metadata:{}}]
const history={[id(5)]:{artifactId:id(5),sha256:sha,sourceCurrent:true,integrityStatus:'valid',previewUrl:'/media/candidate.png'}}
const selection={[`prop:${id(4)}`]:{selectedArtifactIds:[id(5)],selectedAssetIds:[id(9)],anchorArtifactId:id(5),anchorAssetId:''}}
async function modules(fn){
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  const {default:Modal}=await vite.ssrLoadModule('/src/components/BaseModal.vue')
  const modalSetup=Modal.setup,modalRender=Modal.ssrRender
  Modal.setup=(props,{slots})=>()=>props.open?h('section',slots.default?.()):null
  delete Modal.ssrRender
  try{await fn(await vite.ssrLoadModule('/src/services/productionManualBibleImages.ts'),vite)}finally{
    Modal.setup=modalSetup;Modal.ssrRender=modalRender;await vite.close()
  }
}
function deferred<T=any>() {
  let resolve!:(value:T)=>void, reject!:(cause:unknown)=>void
  const promise=new Promise<T>((yes,no)=>{resolve=yes;reject=no})
  return {promise,resolve,reject}
}
async function reviewComponent(fn, browserWindow?:any) {
  await modules(async(_m,vite)=>{
    const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
    const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
    const oldApi={getManualBibleImageReview:platformApi.getManualBibleImageReview,performManualProductionAction:platformApi.performManualProductionAction}
    const oldRepo={getBibleImageCandidates:repository.getBibleImageCandidates,getAssets:repository.getAssets}
    const originalWindow=globalThis.window
    if(browserWindow) globalThis.window=browserWindow
    platformApi.getManualBibleImageReview=async()=>structuredClone(raw)
    repository.getAssets=async()=>structuredClone(assets)
    repository.getBibleImageCandidates=async()=>({candidates:[{candidateHistory:Object.values(history)}]})
    const {default:C}=await vite.ssrLoadModule('/src/components/ManualBibleImageReview.vue')
    const props=reactive({run:structuredClone(run),options:structuredClone(options),open:true}),events:any[]=[]
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    let state:any
    const app=renderer.createApp({setup(){state=C.setup(props,{expose(){},emit:(...args)=>events.push(args)});return()=>h('div')}})
    try {
      app.provide(ssrContextKey,{modules:new Set()});app.mount({});await state.load();events.length=0
      await fn({state,props,events,platformApi,repository,unmount:()=>app.unmount()})
    } finally {
      app.unmount();Object.assign(platformApi,oldApi);Object.assign(repository,oldRepo)
      if (originalWindow === undefined) delete globalThis.window
      else globalThis.window=originalWindow
    }
  })
}
test('manual image review uses actual pending producer and exact candidate/target CAS with one anchor',()=>modules(m=>{
  const detail=m.readManualBibleImageReview(raw,run)
  const body=m.buildManualBibleImageApproval(detail,run,options,selection,assets,history,'same-key','reviewed')
  assert.equal(body.stage_key,'bible_image_review');assert.equal(body.expected_selection_revision,0)
  assert.deepEqual(body.payload.bindings[0],{entity_type:'prop',entity_id:id(4),selected_artifact_ids:[id(5)],
    selected_library_assets:[{asset_id:id(9),expected_sha256:sha}],anchor_artifact_id:id(5),anchor_library_asset_id:null})
  assert.equal(body.payload.expected_targets_sha256,sha);assert.equal(body.payload.expected_candidates_sha256,sha)
  assert.equal(body.payload.expected_content_basis_sha256,sha)
  const libraryOnly={...raw,candidates:{...raw.candidates,entities:[{...entity,candidates:[]}]}}
  const librarySelection={[`prop:${id(4)}`]:{selectedArtifactIds:[],selectedAssetIds:[id(9)],anchorArtifactId:'',anchorAssetId:id(9)}}
  assert.equal(m.buildManualBibleImageApproval(m.readManualBibleImageReview(libraryOnly,run),run,options,librarySelection,assets,{},'library','').payload.bindings[0].anchor_library_asset_id,id(9))
}))
test('manual image review refuses incomplete identity, invalid selections, stale previews and absent configured limits',()=>modules(m=>{
  for(const change of [{targets:[]},{targets:[raw.targets[0],raw.targets[0]]},{run_id:id(90)},{targets_sha256:''},{maximum_selected_images:0},
    {content_basis:{}},{content_basis_sha256:''},{content_basis:{...raw.content_basis,script_sha256:null}},
    {approval:{...raw.approval,changes:['本轮采用剧本']}},{approval:{...raw.approval,changes:undefined}},
    {approval:{...raw.approval,status:'current'}},{candidates:{...raw.candidates,generation_stage_id:id(90)}},
    {candidates:{...raw.candidates,entities:[{...entity,candidates:[{...candidate,preparation_id:null}]}]}}]) assert.throws(()=>m.readManualBibleImageReview({...raw,...change},run))
  const detail=m.readManualBibleImageReview(raw,run)
  const build=(choices=selection,lib=assets,previews=history)=>m.buildManualBibleImageApproval(detail,run,options,choices,lib,previews,'x','')
  assert.throws(()=>build({}));assert.throws(()=>build(selection,[],history));assert.throws(()=>build(selection,assets,{}))
  for(const change of [{anchorAssetId:id(9)},{anchorArtifactId:''},{selectedArtifactIds:[id(5),id(5)]},{selectedArtifactIds:[id(95)]}]) {
    assert.throws(()=>build({[`prop:${id(4)}`]:{...selection[`prop:${id(4)}`],...change}}))
  }
  assert.throws(()=>m.buildManualBibleImageApproval(detail,run,{...options,actions:[]},selection,assets,history,'x',''))
}))
test('manual image API is a readonly original Run resource and propagates errors',()=>modules(async(_m,vite)=>{
  const {api}=await vite.ssrLoadModule('/src/services/api.ts'),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),previous=api.get
  try{api.get=async path=>{assert.equal(path,`/production-runs/${run.id}/manual-bible-image-review/`);return {data:raw}}
    assert.deepEqual(await platformApi.getManualBibleImageReview(run.id),raw)
    api.get=async()=>{throw new Error('unavailable')};await assert.rejects(platformApi.getManualBibleImageReview(run.id),/unavailable/)
  }finally{api.get=previous}
}))

test('changed creative context is visible and submitted through the existing review action',()=>modules(async(m,vite)=>{
  const changed={...raw,approval:{revision:1,decision_id:id(40),snapshot_sha256:sha,status:'changed',changes:['本轮采用剧本','上游采用或审核修订']}}
  const detail=m.readManualBibleImageReview(changed,run)
  const body=m.buildManualBibleImageApproval(detail,run,options,selection,assets,history,'new-review','explicit reuse')
  assert.equal(body.expected_selection_revision,1)
  assert.equal(body.payload.expected_content_basis_sha256,sha)
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),{repository}=await vite.ssrLoadModule('/src/services/repository.ts')
  platformApi.getManualBibleImageReview=async()=>changed
  repository.getAssets=async()=>assets
  repository.getBibleImageCandidates=async()=>({candidates:[]})
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualBibleImageReview.vue')
  let state:any
  const Wrapped={...C,setup(props,ctx){state=C.setup(props,ctx);return state}}
  const {createSSRApp}=await import('vue')
  const app=createSSRApp({render:()=>h(Wrapped,{run,options,open:true})})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}},{path:'/projects/:projectId',name:'project-detail',component:{render:()=>null}}]})
  app.use(router);await router.push('/');await router.isReady()
  // Initial SSR starts the async reader; the explicit second render exercises
  // the same real component with the validated loaded state.
  await renderToString(app);await state.load()
  const loaded=createSSRApp({render:()=>h({...C,setup:()=>state},{run,options,open:true})});loaded.use(router)
  const html=await renderToString(loaded)
  assert.match(html,/变化项：本轮采用剧本、上游采用或审核修订/)
  assert.match(html,/不会自动重跑/)
  assert.match(html,/主图设定修订 v2（文字依据，不代表图片已就绪）/)
  assert.doesNotMatch(html,/Square handle|visual_identity/)
  assert.match(html,/确认所选参考图与主图并写入/)
}))
test('target v2 distinguishes absent primary from invalid evidence and rejects the old duplicated text contract',()=>modules(m=>{
  const parse=(target)=>m.readManualBibleImageReview({...raw,targets:[target]},run)
  assert.equal(parse({...raw.targets[0],primary_setting:null}).targets[0].primary_setting,null)
  for(const change of [{primary_setting:undefined},{primary_setting:{}},{schema_version:1},{visual_identity:'old'},
    {primary_setting:{...raw.targets[0].primary_setting,revision:0}},
    {primary_setting:{...raw.targets[0].primary_setting,content_sha256:'broken'}},
    {primary_setting:{...raw.targets[0].primary_setting,appearance_id:id(4),identity_anchor:'duplicate'}}]) {
    assert.throws(()=>parse({...raw.targets[0],...change}))
  }
}))
test('shared image editor does not auto-select and keeps mutually exclusive anchors across both image sources',()=>modules(async(m,vite)=>{
  const {default:C}=await vite.ssrLoadModule('/src/components/BibleImageSelectionEditor.vue')
  const props=reactive({entities:m.readManualBibleImageReview(raw,run).review.entities,assets,history,modelValue:{},disabled:false})
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  let state:any;const app=renderer.createApp({setup(){state=C.setup(props,{expose(){},emit(_name,value){props.modelValue=value}});return()=>h('div')}})
  try{app.provide(ssrContextKey,{modules:new Set()});app.mount({})
    assert.deepEqual(props.modelValue,{})
    state.toggleBibleArtifact('prop',id(4),id(5),true);state.setBibleAnchor('prop',id(4),id(5))
    state.toggleBibleLibraryAsset('prop',id(4),id(9),true);state.setBibleLibraryAnchor('prop',id(4),id(9))
    assert.equal(props.modelValue[`prop:${id(4)}`].anchorArtifactId,'');assert.equal(props.modelValue[`prop:${id(4)}`].anchorAssetId,id(9))
    state.toggleBibleLibraryAsset('prop',id(4),id(9),false);assert.equal(props.modelValue[`prop:${id(4)}`].anchorAssetId,'')
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}},{path:'/assets',component:{render:()=>null}}]})
    await router.push('/');await router.isReady()
    const rendered=renderer.createApp({render:()=>h(C,{...props,disabled:true})});rendered.use(router)
    const html=await renderToString(rendered)
    assert.match(html,/fieldset[^>]*disabled/);assert.match(html,/执行实例版本/);assert.match(html,/准备/)
  }finally{app.unmount()}
}))
test('manual image component preserves uncertain request and emits success only for a formal decision',()=>modules(async(m,vite)=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),{repository}=await vite.ssrLoadModule('/src/services/repository.ts')
  const old={getManualBibleImageReview:platformApi.getManualBibleImageReview,performManualProductionAction:platformApi.performManualProductionAction}
  const oldRepo={getBibleImageCandidates:repository.getBibleImageCandidates,getAssets:repository.getAssets}
  const calls:any[]=[],events:any[]=[]
  platformApi.getManualBibleImageReview=async()=>JSON.parse(JSON.stringify(raw));repository.getAssets=async()=>assets
  repository.getBibleImageCandidates=async()=>({candidates:[{candidateHistory:Object.values(history)}]})
  platformApi.performManualProductionAction=async(runId,body)=>{calls.push(JSON.parse(JSON.stringify({runId,body})));if(calls.length===1)throw new Error('uncertain response');return{action:'approve',result_kind:'approval_decision',target_id:id(70)}}
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualBibleImageReview.vue')
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  let state:any;const app=renderer.createApp({setup(){state=C.setup(reactive({run,options,open:true}),{expose(){},emit:(...args)=>events.push(args)});return()=>h('div')}})
  try{app.provide(ssrContextKey,{modules:new Set()});app.mount({});await Promise.resolve();await nextTick();await state.load()
    await state.submit();assert.equal(calls.length,0);assert.match(state.error.value,/选择/)
    state.selections.value=JSON.parse(JSON.stringify(selection));await state.submit();assert.match(state.error.value,/uncertain/)
    const pendingBeforeRefresh=JSON.stringify(state.pending.value),keyBeforeRefresh=state.key.value
    await state.load()
    assert.equal(JSON.stringify(state.pending.value),pendingBeforeRefresh,'refresh must not erase an uncertain approval')
    assert.equal(state.key.value,keyBeforeRefresh,'refresh must retain the original idempotency key')
    const router=createRouter({history:createMemoryHistory(),routes:[{name:'project-detail',path:'/projects/:projectId',component:{render:()=>null}},{path:'/',component:{render:()=>null}}]})
    await router.push('/');await router.isReady()
    const {createSSRApp}=await import('vue')
    const recoveryHtml=await renderToString(createSSRApp({...C,setup:()=>state},{run,options,open:true}).use(router))
    assert.match(recoveryHtml,/不是只读查询/);assert.match(recoveryHtml,/重试同一审核请求/)
    assert.ok(recoveryHtml.includes(keyBeforeRefresh))
    await state.submit();assert.deepEqual(calls[1],calls[0]);assert.equal(events.filter(row=>row[0]==='changed').length,1)
    const rendered=renderer.createApp({render:()=>h(C,{run,options,open:true})});rendered.use(router)
    const html=await renderToString(rendered);assert.match(html,/不要求先调用模型/);assert.match(html,/当前关系/)
  }finally{app.unmount();Object.assign(platformApi,old);Object.assign(repository,oldRepo)}
}))

test('failed image-review refresh preserves the visible draft but blocks stale approval until a complete read',()=>reviewComponent(async({state,platformApi,repository})=>{
  state.selections.value=structuredClone(selection);state.comment.value='keep my review'
  const oldDetail=state.detail.value,oldHistory=state.history.value,oldKey=state.key.value
  let posts=0
  platformApi.performManualProductionAction=async()=>{posts++;throw new Error('must not write')}
  repository.getAssets=async()=>{throw new Error('library unavailable')}
  await state.load()
  assert.equal(state.detail.value,oldDetail);assert.equal(state.history.value,oldHistory)
  assert.deepEqual(state.selections.value,selection);assert.equal(state.comment.value,'keep my review')
  assert.equal(state.key.value,oldKey);assert.equal(state.current.value,false)
  await state.submit();assert.equal(posts,0)
  repository.getAssets=async()=>structuredClone(assets)
  await state.load()
  assert.equal(state.current.value,true);assert.deepEqual(state.selections.value,{})
  assert.equal(state.comment.value,'');assert.equal(state.error.value,'')
}))

test('explicit image-review refresh confirms draft discard and pending requests cannot refresh',()=>reviewComponent(async({state,platformApi})=>{
  let reads=0,confirms=0
  platformApi.getManualBibleImageReview=async()=>{reads++;return structuredClone(raw)}
  state.selections.value=structuredClone(selection);state.comment.value='manual note'
  globalThis.window={confirm:()=>{confirms++;return false}} as any
  await state.refresh()
  assert.equal(reads,0);assert.equal(confirms,1);assert.deepEqual(state.selections.value,selection)
  globalThis.window.confirm=()=>true
  await state.refresh();assert.equal(reads,1);assert.deepEqual(state.selections.value,{})
  assert.equal(state.comment.value,'')
  state.pending.value={idempotency_key:'original-request'}
  await state.refresh();await state.load();assert.equal(reads,1)
  assert.deepEqual(state.pending.value,{idempotency_key:'original-request'})
}))

test('old image approval cannot announce success or release busy state after switching run',()=>reviewComponent(async({state,props,events,platformApi})=>{
  const approval=deferred(),newRead=deferred()
  let reads=0
  platformApi.performManualProductionAction=()=>approval.promise
  platformApi.getManualBibleImageReview=async()=>{reads++;return newRead.promise}
  state.selections.value=structuredClone(selection)
  const submitting=state.submit()
  assert.equal(state.submitting.value,true)
  props.run={...props.run,id:id(81)};props.options={...props.options,runId:id(81)}
  assert.equal(state.detail.value,null);assert.equal(state.pending.value,null)
  assert.equal(state.loading.value,true);assert.equal(state.submitting.value,false)
  const eventCount=events.length
  approval.resolve({action:'approve',result_kind:'approval_decision',target_id:id(70)})
  await submitting
  assert.equal(events.length,eventCount);assert.equal(state.notice.value,'')
  assert.equal(state.loading.value,true);assert.equal(reads,1)
  newRead.resolve({...structuredClone(raw),run_id:id(81)})
  await new Promise(resolve=>setImmediate(resolve))
  assert.equal(state.detail.value.run_id,id(81));assert.equal(state.loading.value,false)
  assert.equal(events.filter(row=>row[0]==='changed').length,0)
}))

test('same-context run polling preserves draft and uncertain image approval but updates terminal state',()=>reviewComponent(async({state,props,platformApi})=>{
  let reads=0
  platformApi.getManualBibleImageReview=async()=>{reads++;return structuredClone(raw)}
  state.selections.value=structuredClone(selection);state.comment.value='keep my review'
  state.pending.value={idempotency_key:'original-request'}
  const detail=state.detail.value,key=state.key.value,pending=state.pending.value
  props.run={...props.run};await nextTick()
  assert.equal(reads,0);assert.equal(state.detail.value,detail);assert.equal(state.key.value,key)
  assert.equal(state.pending.value,pending);assert.deepEqual(state.selections.value,selection)
  assert.equal(state.comment.value,'keep my review')
  props.run={...props.run,status:'cancelled'};await nextTick()
  assert.equal(state.terminal.value,true);assert.equal(state.pending.value,pending)
  await state.submit();assert.equal(reads,0)
}))

test('out-of-order image reads and plan changes never restore the old context',()=>reviewComponent(async({state,props,platformApi})=>{
  const oldRead=deferred(),newRead=deferred()
  let reads=0
  platformApi.getManualBibleImageReview=()=>++reads===1?oldRead.promise:newRead.promise
  const reading=state.load()
  props.run={...props.run,executionPlan:{sha256:'b'.repeat(64)}}
  assert.equal(state.detail.value,null)
  oldRead.resolve(structuredClone(raw));await reading
  assert.equal(state.detail.value,null);assert.equal(state.loading.value,true)
  newRead.resolve({...structuredClone(raw),run_plan_sha256:'b'.repeat(64)})
  await new Promise(resolve=>setImmediate(resolve))
  assert.equal(state.detail.value.run_plan_sha256,'b'.repeat(64));assert.equal(state.current.value,true)
  platformApi.getManualBibleImageReview=async()=>{throw new Error('new context unreadable')}
  props.run={...props.run,id:id(83)};await new Promise(resolve=>setImmediate(resolve))
  props.open=false;await nextTick();props.open=true;await nextTick()
  assert.equal(state.detail.value,null);assert.equal(state.readAt.value,'')
  assert.equal(state.reopened.value,false,'a failed first read must not claim a previous read basis was restored')
}))

test('unmounted image review ignores late errors and duplicate clicks never resubmit in flight',()=>reviewComponent(async({state,events,platformApi,unmount})=>{
  const response=deferred();let posts=0
  platformApi.performManualProductionAction=()=>{posts++;return response.promise}
  state.selections.value=structuredClone(selection)
  const first=state.submit();await state.submit();assert.equal(posts,1)
  unmount();const eventCount=events.length
  response.reject(new Error('late failure'));await first
  assert.equal(events.length,eventCount);assert.equal(state.error.value,'')
}))

test('initial explicit rejection allows re-reading instead of trapping a stale approval forever',()=>reviewComponent(async({state,platformApi})=>{
  for (const response of [{status:400,data:{detail:'invalid'}},{status:409,data:{code:'manual_selection_conflict',message:'review changed'}}]) {
    await state.load();state.selections.value=structuredClone(selection)
    platformApi.performManualProductionAction=async()=>{throw {response}}
    await state.submit()
    assert.equal(state.pending.value,null);assert.equal(state.current.value,false)
    assert.deepEqual(state.selections.value,selection)
    await state.load();assert.equal(state.current.value,true)
  }
}))

test('an explicit retry rejection cannot erase an earlier unknown approval result',()=>reviewComponent(async({state,platformApi})=>{
  state.selections.value=structuredClone(selection)
  platformApi.performManualProductionAction=async()=>{throw new Error('unknown transport result')}
  await state.submit();const pending=state.pending.value,key=state.key.value
  platformApi.performManualProductionAction=async()=>{throw {response:{status:403,data:{detail:'permission changed'}}}}
  await state.submit();await state.load()
  assert.equal(state.pending.value,pending);assert.equal(state.key.value,key)
}))

test('closing and reopening retains draft and unknown request without reading or posting, exact retry clears only after receipt',()=>reviewComponent(async({state,props,events,platformApi})=>{
  let reads=0;const requests:any[]=[]
  platformApi.getManualBibleImageReview=async()=>{reads++;throw new Error('post-approval refresh unavailable')}
  platformApi.performManualProductionAction=async(_run,body)=>{
    requests.push(JSON.parse(JSON.stringify(body)))
    if(requests.length===1) throw new Error('response lost')
    return {action:'approve',result_kind:'approval_decision',target_id:id(70)}
  }
  state.selections.value=structuredClone(selection);state.comment.value='keep across close'
  const detail=state.detail.value,key=state.key.value,readAt=state.readAt.value
  state.close();assert.deepEqual(events.at(-1),['close'])
  props.open=false;await nextTick();props.open=true;await nextTick()
  assert.equal(reads,0);assert.equal(requests.length,0);assert.equal(state.reopened.value,true)
  assert.equal(state.readAt.value,readAt);assert.equal(state.detail.value,detail);assert.equal(state.key.value,key)
  assert.deepEqual(state.selections.value,selection);assert.equal(state.comment.value,'keep across close')
  await state.submit();const pending=state.pending.value
  assert.ok(pending);assert.deepEqual(events.findLast(row=>row[0]==='retained'),['retained',{dirty:true,pending:true}])
  state.close();props.open=false;await nextTick();props.open=true;await nextTick()
  assert.equal(reads,0);assert.equal(requests.length,1);assert.equal(state.pending.value,pending)
  assert.equal(state.key.value,key);assert.equal(state.readAt.value,readAt)
  await state.submit()
  assert.deepEqual(requests[1],requests[0]);assert.equal(events.filter(row=>row[0]==='changed').length,1)
  assert.equal(reads,1);assert.equal(state.current.value,false)
  assert.equal(state.pending.value,null);assert.equal(state.dirty.value,false)
  assert.deepEqual(state.selections.value,{});assert.equal(state.comment.value,'')
  assert.deepEqual(events.findLast(row=>row[0]==='retained'),['retained',{dirty:false,pending:false}])
}))

test('modal closure is blocked during reads and writes, and retained draft prevents browser unload until cleared',async()=>{
  const listeners=new Map<string,any>(),removed:any[]=[]
  const browserWindow={addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:(name,fn)=>{removed.push([name,fn]);listeners.delete(name)}}
  await reviewComponent(async({state,events,platformApi,unmount})=>{
    const beforeUnload=listeners.get('beforeunload');assert.equal(typeof beforeUnload,'function')
    const event={prevented:false,returnValue:undefined,preventDefault(){this.prevented=true}}
    state.selections.value={[`prop:${id(4)}`]:{selectedArtifactIds:[],selectedAssetIds:[],anchorArtifactId:'',anchorAssetId:''}}
    assert.equal(state.dirty.value,false);beforeUnload(event);assert.equal(event.prevented,false)
    state.comment.value='manual note';beforeUnload(event);assert.equal(event.prevented,true);assert.equal(event.returnValue,'')
    assert.deepEqual(events.at(-1),['retained',{dirty:true,pending:false}])
    const read=deferred();platformApi.getManualBibleImageReview=()=>read.promise
    const reading=state.load(),closeCount=events.filter(row=>row[0]==='close').length
    state.close();assert.equal(events.filter(row=>row[0]==='close').length,closeCount)
    read.resolve(structuredClone(raw));await reading
    assert.equal(state.dirty.value,false)
    state.selections.value=structuredClone(selection)
    const result=deferred();platformApi.performManualProductionAction=()=>result.promise
    const submitting=state.submit();state.close()
    assert.equal(events.filter(row=>row[0]==='close').length,closeCount)
    result.reject(new Error('unknown outcome'));await submitting
    state.comment.value='';state.selections.value={};event.prevented=false
    beforeUnload(event);assert.equal(event.prevented,true,'unknown result alone is enough to guard unloading')
    state.close();assert.equal(events.filter(row=>row[0]==='close').length,closeCount+1)
    unmount();assert.deepEqual(removed,[['beforeunload',beforeUnload]]);assert.equal(listeners.size,0)
  },browserWindow)
})
