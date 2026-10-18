import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url)), sha = 'a'.repeat(64)
const id = (n: number) => `10000000-0000-0000-0000-${String(n).padStart(12,'0')}`
const run = {id:id(1),project:id(2),planSource:'manual',executionPlan:{sha256:sha},stages:[{id:id(3),stageKey:'media_generation'}]}
const candidate = {run_id:run.id,stage_key:'media_generation',unit_key:'explicit-unit',scope:{kind:'shot',id:id(4)},
  source_kind:'execution',source_asset_id:null,
  run_plan_sha256:sha,preparation_id:id(5),request_sha256:sha,selection_revision:0,selected_artifact_id:null,selection_decision_id:null}
const artifact = {id:id(6),productionRun:run.id,stageRun:id(3),kind:'image',status:'available',sha256:sha,
  manifestMetadataSha256:sha,manualMediaCandidate:candidate}
const options = {runId:run.id,runPlanSha256:sha,actions:[{stageKey:'media_generation',action:'adopt'}]}
async function modules(fn) {
  const vite = await createServer({ root, logLevel:'silent', appType:'custom', server:{middlewareMode:true} })
  try { await fn(await vite.ssrLoadModule('/src/services/productionManualMedia.ts'),vite) } finally { await vite.close() }
}

test('media adoption preserves the exact scope, revision and both content digests without execution overrides',()=>modules(m=>{
  const request = m.buildManualMediaAdoption(artifact,run,options,'adopt-one')
  assert.deepEqual(request.scope,candidate.scope); assert.equal(request.unit_key,'explicit-unit')
  assert.equal(request.expected_selection_revision,0)
  assert.deepEqual(request.payload,{source:{kind:'artifact',artifact_id:artifact.id,expected_sha256:sha,expected_manifest_sha256:sha}})
  for (const key of ['workflow_version_id','model_deployment_id','prompt_profile_binding_id','parameters']) assert.equal(key in request.payload,false)
  assert.equal(m.manualMediaAdoptionResult({action:'adopt',result_kind:'artifact_selection',target_id:id(7)}),id(7))
  assert.throws(()=>m.manualMediaAdoptionResult({action:'adopt',result_kind:'artifact',target_id:id(7)}))
}))

test('media adoption rejects incomplete, stale, cross-run and unregistered candidates',()=>modules(m=>{
  for(const change of [{manualMediaCandidate:null},{productionRun:id(8)},{stageRun:id(8)},{sha256:''},{status:'selected'},
    {manualMediaCandidate:{...candidate,selection_revision:undefined}},
    {manualMediaCandidate:{...candidate,run_plan_sha256:'b'.repeat(64)}},
    {manualMediaCandidate:{...candidate,scope:{kind:'project',id:id(8)}}}]) {
    assert.throws(()=>m.buildManualMediaAdoption({...artifact,...change},run,options,'key'))
  }
  assert.throws(()=>m.buildManualMediaAdoption(artifact,run,{...options,actions:[]},'key'))
  const selected={...artifact,status:'selected',manualMediaCandidate:{...candidate,selection_revision:1,selected_artifact_id:artifact.id,selection_decision_id:id(7)}}
  assert.equal(m.buildManualMediaAdoption(selected,run,options,'key').expected_selection_revision,1)
}))

test('artifact page API preserves the actual pagination envelope and candidate detail',()=>modules(async(_m,vite)=>{
  const {api}=await vite.ssrLoadModule('/src/services/api.ts'),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const original=api.get
  try{
    api.get=async()=>({data:{count:1,next:null,previous:null,results:[{id:artifact.id,manual_media_candidate:candidate}]}})
    const page=await platformApi.listArtifactPage({production_run:run.id,page:1})
    assert.equal(page.count,1);assert.equal(page.hasNext,false);assert.deepEqual(page.results[0].manualMediaCandidate,candidate)
    api.get=async()=>({data:{count:1,results:[]}})
    await assert.rejects(platformApi.listArtifactPage({production_run:run.id}),/分页/)
  }finally{api.get=original}
}))

const member = {artifact_id:id(6),kind:'image',artifact_sha256:sha,manifest_sha256:sha,decision_id:id(7),
  decision_revision:1,variant_group:'unit-group',scope:{kind:'shot',id:id(4)},unit_key:'explicit-unit'}
const timeline = [{episode_id:id(12),number:1,title:'交付第一集',shots:[{shot_id:id(4),number:1,title:'已核对镜头',duration_ms:2500,media_kind:'image',episode_start_ms:null,episode_end_ms:null}]}]
const basis = {script_sha256:sha,storyboard_sha256:sha,bible_sha256:sha,visual_rules_sha256:sha,upstream_reviews_sha256:sha}
const inventory = {run_id:run.id,run_plan_sha256:sha,selection:{schema_version:4,run_plan_sha256:sha,
  source_stage_id:id(3),items:[member],delivery_timeline:timeline,content_basis:basis},selection_sha256:sha,
  approval:{revision:0,decision_id:null,status:'not_reviewed',selection_sha256:null,changes:[]}}

test('media review submits the explicitly read whole selection and its original revision',()=>modules(m=>{
  const original=JSON.stringify(inventory),detail=m.readManualMediaReview(inventory,run)
  const reviewOptions={...options,actions:[{stageKey:'artifact_selection',action:'approve'}]}
  const request=m.buildManualMediaReview(detail,run,reviewOptions,'review-one','已逐项核对')
  assert.equal(detail.timeline[0].shots[0].durationMs,2500)
  assert.deepEqual(request.payload,{selection:inventory.selection,expected_selection_sha256:sha,comment:'已逐项核对'})
  assert.equal(request.expected_selection_revision,0);assert.equal(request.stage_key,'artifact_selection')
  assert.equal(request.scope.id,run.project);assert.equal(JSON.stringify(inventory),original)
  assert.equal('workflow_version_id' in request.payload,false)
  detail.selection.items.length=0
  assert.equal(inventory.selection.items.length,1)
}))

test('delivery timeline cannot be omitted, duplicated, defaulted or mixed with an old schema',()=>modules(m=>{
  for (const selection of [
    {...inventory.selection,schema_version:1}, {...inventory.selection,delivery_timeline:undefined},
    {...inventory.selection,delivery_timeline:[...timeline,...timeline]},
    {...inventory.selection,delivery_timeline:[{...timeline[0],shots:[...timeline[0].shots,...timeline[0].shots]}]},
    {...inventory.selection,delivery_timeline:[{...timeline[0],shots:[{...timeline[0].shots[0],duration_ms:undefined}]}]},
  ]) assert.throws(()=>m.readManualMediaReview({...inventory,selection},run))
  const incomplete=m.readManualMediaReview({...inventory,selection:{...inventory.selection,delivery_timeline:[{
    ...timeline[0],shots:[{...timeline[0].shots[0],duration_ms:null,media_kind:''}]}]}},run)
  assert.equal(incomplete.timeline[0].shots[0].durationMs,null)
  assert.equal(incomplete.timeline[0].shots[0].mediaKind,'')
}))

test('media review distinguishes changed selections and rejects malformed or empty approval',()=>modules(m=>{
  const changed={...inventory,approval:{revision:1,decision_id:id(8),status:'changed',selection_sha256:'b'.repeat(64),changes:[]}}
  assert.equal(m.readManualMediaReview(changed,run).approval.status,'changed')
  const current={...changed,approval:{...changed.approval,status:'current',selection_sha256:sha}}
  assert.equal(m.readManualMediaReview(current,run).approval.status,'current')
  for(const bad of [{...inventory,run_id:id(99)}, {...inventory,selection:{...inventory.selection,items:[member,member]}},
    {...inventory,selection:{...inventory.selection,items:[{...member,decision_revision:0}]}},
    {...current,approval:{...current.approval,status:'changed'}},
    {...current,approval:{...current.approval,revision:0}},
    {...inventory,selection:{...inventory.selection,source_stage_id:id(99)}},
    {...inventory,selection:{...inventory.selection,items:[{...member,scope:{kind:'project',id:id(99)}}]}}]) {
    assert.throws(()=>m.readManualMediaReview(bad,run))
  }
  const empty=m.readManualMediaReview({...inventory,selection:{...inventory.selection,items:[]}},run)
  assert.throws(()=>m.buildManualMediaReview(empty,run,{...options,actions:[{stageKey:'artifact_selection',action:'approve'}]},'empty',''))
  assert.throws(()=>m.buildManualMediaReview(m.readManualMediaReview(inventory,run),run,options,'wrong-action',''))
}))

test('reuse review requires current content basis and shows server-owned change reasons',()=>modules(m=>{
  for (const selection of [
    {...inventory.selection,schema_version:3}, {...inventory.selection,content_basis:undefined},
    {...inventory.selection,content_basis:{...basis,bible_sha256:'invalid'}},
    {...inventory.selection,content_basis:{...basis,guess:'old source'}},
  ]) assert.throws(()=>m.readManualMediaReview({...inventory,selection},run))
  const detail=m.readManualMediaReview({...inventory,approval:{revision:1,decision_id:id(8),status:'changed',
    selection_sha256:'b'.repeat(64),changes:['人物、场景或道具设定','上游采用或审核修订']}},run)
  assert.deepEqual(detail.approval.changes,['人物、场景或道具设定','上游采用或审核修订'])
  assert.deepEqual(detail.selection.content_basis,basis)
  assert.throws(()=>m.readManualMediaReview({...inventory,approval:{...inventory.approval,changes:['不能伪装未变化']}},run))
}))

test('media review API reads the original run endpoint and propagates request failures',()=>modules(async(_m,vite)=>{
  const {api}=await vite.ssrLoadModule('/src/services/api.ts'),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const original=api.get
  try{
    api.get=async(path)=>{assert.equal(path,`/production-runs/${run.id}/manual-media-review/`);return {data:inventory}}
    assert.deepEqual(await platformApi.getManualMediaReview(run.id),inventory)
    api.get=async()=>{throw new Error('selection unavailable')}
    await assert.rejects(platformApi.getManualMediaReview(run.id),/selection unavailable/)
  }finally{api.get=original}
}))

test('existing asset adoption sends only the explicit asset, target and observed selection revision',()=>modules(m=>{
  const target={scope:{kind:'shot',id:id(4)},unitKey:`shot:${id(4)}`,label:'第一镜头'}
  const asset={id:id(10),isActive:true,mimeType:'image/png',checksum:sha,metadata:{}}
  const detail=m.readManualMediaReview({...inventory,selection:{...inventory.selection,items:[{...member,unit_key:target.unitKey}]}},run)
  const allowed={...options,actions:[{stageKey:'media_generation',action:'adopt',scopeKinds:['project','episode','shot']}]}
  const original=JSON.stringify({asset,detail,target})
  const request=m.buildManualAssetAdoption(asset,'image',target,detail,run,allowed,'asset-one')
  assert.equal(request.expected_selection_revision,1);assert.equal(request.unit_key,target.unitKey)
  assert.deepEqual(request.payload,{source:{kind:'asset',asset_id:asset.id,expected_sha256:sha,media_kind:'image'}})
  assert.equal(JSON.stringify({asset,detail,target}),original)
  for(const changed of [{...asset,checksum:''},{...asset,isActive:false},{...asset,mimeType:'audio/wav'},{...asset,metadata:{usage:'project_cover'}}]) {
    assert.throws(()=>m.buildManualAssetAdoption(changed,'image',target,detail,run,allowed,'bad'))
  }
  assert.throws(()=>m.buildManualAssetAdoption(asset,'image',{...target,unitKey:'guessed'},detail,run,allowed,'bad'))
  const empty=m.readManualMediaReview({...inventory,selection:{...inventory.selection,items:[]}},run)
  assert.equal(m.buildManualAssetAdoption(asset,'image',target,empty,run,allowed,'first').expected_selection_revision,0)
}))

test('adopted candidate reports an asset source and rejects any fabricated execution identity',()=>modules(m=>{
  const source={...candidate,source_kind:'asset',source_asset_id:id(10),preparation_id:null,request_sha256:null}
  const adopted={...artifact,manualMediaCandidate:source}
  const result=m.readManualMediaCandidate(adopted,run)
  assert.equal(result.sourceKind,'asset');assert.equal(result.sourceAssetId,id(10));assert.equal(result.preparationId,null)
  assert.equal(m.buildManualMediaAdoption(adopted,run,options,'choose-adopted').payload.source.kind,'artifact')
  for(const change of [{preparation_id:id(5)},{request_sha256:sha},{source_asset_id:null},{source_kind:'unknown'}]) {
    assert.throws(()=>m.readManualMediaCandidate({...adopted,manualMediaCandidate:{...source,...change}},run))
  }
}))

test('asset adoption page offers formal asset upload and explicit target/review controls without automatic requests',()=>modules(async(_m,vite)=>{
  const {renderToString}=await import('@vue/server-renderer'),{createSSRApp}=await import('vue')
  const component=(await vite.ssrLoadModule('/src/components/ManualAssetAdoption.vue')).default
  const html=await renderToString(createSSRApp(component,{run,options:{...options,actions:[{stageKey:'media_generation',action:'adopt',scopeKinds:['project','shot']}]}}))
  assert.match(html,/从素材库直接采用/);assert.match(html,/重新读取对象与采用状态/)
  assert.match(html,/选择素材 \/ 上传本地文件/);assert.match(html,/我已核对素材内容/)
}))

test('selection review visibly includes delivery order, real duration and incomplete values without implying regeneration',()=>modules(async(m,vite)=>{
  const {renderToString}=await import('@vue/server-renderer'),{createSSRApp}=await import('vue')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),old=platformApi.getManualMediaReview
  const raw={...inventory,approval:{revision:1,decision_id:id(8),status:'changed',selection_sha256:'b'.repeat(64),
    changes:['人物、场景或道具设定','上游采用或审核修订']},selection:{...inventory.selection,delivery_timeline:[{...timeline[0],shots:[...timeline[0].shots,
    {shot_id:id(13),number:2,title:'待补参数',duration_ms:null,media_kind:'',episode_start_ms:null,episode_end_ms:null}]}]}}
  platformApi.getManualMediaReview=async()=>raw
  try {
    const Component=(await vite.ssrLoadModule('/src/components/ManualMediaSelectionReview.vue')).default
    const Seeded={...Component,setup(props,context){const state=Component.setup(props,context)
      state.detail.value=m.readManualMediaReview(raw,run);return state}}
    const html=await renderToString(createSSRApp(Seeded,{run,options}))
    for(const text of ['按装配顺序','交付第一集','已核对镜头','2.5 秒','待补参数','未配置有效时长','未配置媒体类型','不会自动重生成',
      '已变化：人物、场景或道具设定、上游采用或审核修订','不代表重新运行过模型','确认上述候选仍适合本次交付'])assert.ok(html.includes(text),text)
  } finally {platformApi.getManualMediaReview=old}
}))
