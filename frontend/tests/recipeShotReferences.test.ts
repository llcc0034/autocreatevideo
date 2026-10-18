import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
import {readFileSync} from 'node:fs'

const id = (n:number) => `00000000-0000-4000-8000-${String(n).padStart(12,'0')}`
const sha = 'a'.repeat(64), sourceSha = 'b'.repeat(64)
const run:any = {id:id(1), project:id(2), planSource:'recipe', status:'awaiting_approval', executionPlan:{sha256:sha}}
const asset:any = {id:id(5), name:'原上游雨衣', type:'image', mimeType:'image/png', checksum:sha, isActive:true}
const candidate = (field='material_generations'):any => ({source:{stage_run_id:id(10),output_snapshot_sha256:sourceSha,field,
  reference:{reference_version_id:id(6),[field==='material_generations'?'decision_id':'fulfillment_id']:id(11)}},
  source_mode:field==='material_generations'?'generated':'existing',reference_version_id:id(6),version:2,setting_revision_id:id(8),
  asset_id:id(5),asset_sha256:sha,name:asset.name,mime_type:asset.mimeType})
const baseline = (source='upstream', allow=true):any => ({run_id:run.id,stage_key:'storyboard_review',run_plan_sha256:sha,shot_id:id(3),
  source_snapshot_sha256:sourceSha,uses_sha256:'c'.repeat(64),selection_rule:{rule:{source,source_stage_key:source==='upstream'?'bible_image_generation':null,
    selection:source==='upstream'?'unique_setting':'manual',allow_override:allow},upstream:source==='upstream'?{stage_run_id:id(10),output_snapshot_sha256:sourceSha}:null},
  upstream_candidates:source==='upstream'?[candidate()]:[],state:{schema_version:1,project_id:run.project,shot_id:id(3),uses:[{
    id:id(4),shot_id:id(3),use_key:'雨衣',position:0,purpose:'',media_kind:'image',setting_revision_id:id(8),source_mode:'',
    reference_version_id:null,asset_id:null,asset_sha256:''}]},editability:{allowed:true,code:'editable',reason:'原配方单镜可选择。'}})
const version:any = {id:id(6),version:2,asset:{id:id(5)},setting_revision_id:id(8),source:'upload'}
const receipt = {action:'select',result_kind:'shot_reference_choices',target_id:id(3)}
async function modules(fn:(m:any)=>Promise<void>|void) {
  const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {await fn(await vite.ssrLoadModule('/src/services/productionManualShotReferences.ts'))} finally {await vite.close()}
}

test('recipe baseline preserves optional creative description and original upstream receipt without manual adoption',()=>modules(m=>{
  const raw=baseline(), value=m.readRecipeShotReferences(raw,run,id(3)); raw.upstream_candidates[0].name='changed'
  assert.equal(value.state.uses[0].purpose,'');assert.equal(value.upstream_candidates[0].name,asset.name)
  for(const source of ['existing','upload'])assert.equal(m.readRecipeShotReferences(baseline(source),run,id(3)).upstream_candidates.length,0)
  assert.throws(()=>m.readManualShotReferences(value,run,id(3)))
}))
test('recipe context rejects fabricated adoption, missing permissions, inconsistent source and upstream catalog',()=>modules(m=>{
  for(const mutate of [(v:any)=>v.storyboard_adoption_revision=1,(v:any)=>delete v.source_snapshot_sha256,
    (v:any)=>v.selection_rule.rule.allow_override=null,(v:any)=>v.selection_rule.rule.extra=true,(v:any)=>v.selection_rule.rule.source_stage_key='other',
    (v:any)=>v.selection_rule.upstream.stage_run_id=id(9),(v:any)=>v.upstream_candidates[0].source_mode='existing',
    (v:any)=>v.upstream_candidates[0].source.reference.reference_version_id=id(9),(v:any)=>v.upstream_candidates.push(v.upstream_candidates[0]),
    (v:any)=>v.upstream_candidates[0].version=0,(v:any)=>v.upstream_candidates[0].asset_sha256='',(v:any)=>v.state.uses[0].purpose=null]) {
    const raw=baseline();mutate(raw);assert.throws(()=>m.readRecipeShotReferences(raw,run,id(3)))
  }
  const direct=baseline('existing');direct.upstream_candidates=[candidate()];assert.throws(()=>m.readRecipeShotReferences(direct,run,id(3)))
}))
test('upstream selector rereads exact version and Asset, round source is not acquisition provenance',()=>modules(async m=>{
  const detail=baseline(), calls:string[]=[]
  const selection=await m.selectRecipeUpstreamReference({getVersion:async(pk:string)=>{calls.push(pk);return version},
    getAsset:async(pk:string)=>{calls.push(pk);return asset}},detail,detail.state.uses[0],detail.upstream_candidates[0])
  assert.deepEqual(calls,[id(6),id(5)]);assert.equal(selection.choice.source_mode,'generated');assert.equal(selection.choice.asset_id,null)
  const fulfilled=baseline();fulfilled.upstream_candidates=[candidate('material_fulfillments')]
  const reused=await m.selectRecipeUpstreamReference({getVersion:async()=>({...version,source:'generated'}),getAsset:async()=>asset},
    fulfilled,fulfilled.state.uses[0],fulfilled.upstream_candidates[0]);assert.equal(reused.choice.source_mode,'existing')
}))
test('changed version, setting, original file or metadata never falls back to another asset',()=>modules(async m=>{
  const d=baseline(), use=d.state.uses[0], row=d.upstream_candidates[0]
  for(const patch of [{id:id(90)},{version:3},{asset:{id:id(90)}},{setting_revision_id:id(90)}])
    await assert.rejects(()=>m.selectRecipeUpstreamReference({getVersion:async()=>({...version,...patch}),getAsset:async()=>assert.fail()},d,use,row))
  for(const patch of [{id:id(90)},{checksum:'d'.repeat(64)},{mimeType:'video/mp4'},{isActive:false}])
    await assert.rejects(()=>m.selectRecipeUpstreamReference({getVersion:async()=>version,getAsset:async()=>({...asset,...patch})},d,use,row))
  assert.match(m.recipeReferenceCandidateReason({...use,media_kind:'audio'},row),/类型/)
  await assert.rejects(()=>m.selectRecipeUpstreamReference({getVersion:async()=>assert.fail(),getAsset:async()=>assert.fail()},d,use,{...row,asset_id:id(90)}))
}))
test('recipe select sends source/use SHA and null revision, saves one use and does not approve/resume',()=>modules(async m=>{
  const d=baseline(), s=await m.selectRecipeUpstreamReference({getVersion:async()=>version,getAsset:async()=>asset},d,d.state.uses[0],d.upstream_candidates[0])
  const request=m.buildShotReferenceSelection(d,run,undefined,'key',s)
  assert.equal(request.expected_selection_revision,null);assert.equal(request.action,'select');assert.equal(request.scope.kind,'shot')
  assert.deepEqual(Object.keys(request.payload).sort(),['choices','expected_source_snapshot_sha256','expected_uses_sha256'])
  assert.equal(request.payload.choices.length,1);assert.equal(request.payload.choices[0].reference_version_id,id(6))
  assert.equal(request.payload.expected_source_snapshot_sha256,sourceSha)
}))
test('only frozen existing/upload source can be submitted and upstream cannot use direct/library replacement',()=>modules(m=>{
  for(const source of ['existing','upload']) {
    const d=baseline(source), use=d.state.uses[0]
    const selection=m.selectShotReference(use,asset,source==='existing'?'asset_library':'asset_upload')
    assert.equal(m.buildShotReferenceSelection(d,run,undefined,'key',selection).payload.choices[0].source_mode,source)
    selection.choice.source_mode=source==='existing'?'upload':'existing';assert.throws(()=>m.buildShotReferenceSelection(d,run,undefined,'key',selection))
  }
  const d=baseline();assert.throws(()=>m.buildShotReferenceSelection(d,run,undefined,'key',m.selectShotReference(d.state.uses[0],asset,'asset_library')))
}))
test('no override allows first selection and same choice, rejects replacement but does not lock sibling pending use',()=>modules(m=>{
  const d=baseline('existing',false), use=d.state.uses[0], selection=m.selectShotReference(use,asset,'asset_library')
  assert.equal(m.shotReferenceReplacementReason(d,use),'');m.buildShotReferenceSelection(d,run,undefined,'key',selection)
  Object.assign(use,{source_mode:'existing',asset_id:asset.id,asset_sha256:sha})
  assert.match(m.shotReferenceReplacementReason(d,use),/不允许替换/);m.buildShotReferenceSelection(d,run,undefined,'key',selection)
  const changed=m.selectShotReference(use,{...asset,id:id(90)},'asset_library');assert.throws(()=>m.buildShotReferenceSelection(d,run,undefined,'key',changed))
  const sibling={...use,id:id(40),position:1,use_key:'另一用途',source_mode:'',asset_id:null,asset_sha256:''};d.state.uses.push(sibling)
  m.buildShotReferenceSelection(d,run,undefined,'key',m.selectShotReference(sibling,asset,'asset_library'))
}))
test('read-only or ended recipe cannot issue a fresh selection',()=>modules(m=>{
  const d=baseline('existing'), selected=m.selectShotReference(d.state.uses[0],asset,'asset_library')
  assert.throws(()=>m.buildShotReferenceSelection(d,{...run,status:'cancelled'},undefined,'key',selected))
  d.editability={allowed:false,code:'shot_prepared',reason:'已有准备'};assert.throws(()=>m.buildShotReferenceSelection(d,run,undefined,'key',selected))
}))
test('unknown recipe select retains byte-identical original command after modal closure and terminal state',()=>modules(async m=>{
  const current={...run}, sent:any[]=[];let changes=0
  const editor=m.createManualShotReferenceEditor({read:async()=>baseline('existing'),submit:async(_r:any,p:any)=>{
    sent.push(p);if(sent.length===1)throw Error('timeout');return receipt}},()=>current,()=>undefined,()=>changes++)
  await editor.load(id(3));editor.state.selection=m.selectShotReference(editor.state.detail.state.uses[0],asset,'asset_library')
  await editor.submit();assert.ok(editor.state.pending);current.status='cancelled';editor.state.selection.choice.asset_id=id(90)
  await editor.load(id(30));await editor.submit();assert.deepEqual(sent[0],sent[1]);assert.equal(changes,1);assert.equal(editor.state.pending,null)
}))
test('recipe original request conflicts remain pending after unknown result; fresh conflict requires reread',()=>modules(async m=>{
  for(const unknown of [true,false]) {
    let calls=0;const editor=m.createManualShotReferenceEditor({read:async()=>baseline('existing'),submit:async()=>{
      if(++calls===1 && unknown)throw Error('offline');throw {response:{status:409,data:{detail:'基线冲突'}}}}},()=>run,()=>undefined,()=>assert.fail())
    await editor.load(id(3));editor.state.selection=m.selectShotReference(editor.state.detail.state.uses[0],asset,'asset_library')
    await editor.submit();if(unknown){const original=JSON.stringify(editor.state.pending);await editor.submit();assert.equal(JSON.stringify(editor.state.pending),original)}
    else {assert.equal(editor.state.pending,null);assert.equal(editor.state.conflict,true);await editor.submit();assert.equal(calls,1)}
  }
}))
test('real workbench uses common modal, source-restricted picker, route protection and no recipe manual-options fiction',()=>{
  const read=(path:string)=>readFileSync(new URL(path,import.meta.url),'utf8')
  const host=read('../src/components/RecipeShotReferenceSelection.vue'), modal=read('../src/components/ManualShotReferenceSelection.vue')
  assert.match(host,/ManualShotReferenceSelection v-if="initialized" :open="open" :run="run"/)
  assert.match(host,/retained.value.pending/);assert.match(host,/onBeforeRouteLeave\(canLeave\)/);assert.match(host,/onBeforeRouteUpdate\(canLeave\)/)
  assert.doesNotMatch(host,/getManualProductionActions|performManualProductionAction|approveProductionRun|resumeProduction/)
  assert.match(modal,/getRecipeShotReferences/);assert.match(modal,/:allow-library="!sourceRule \|\| sourceRule.source === 'existing'"/)
  assert.match(modal,/:allow-upload="!sourceRule \|\| sourceRule.source === 'upload'"/)
  const view=read('../src/views/ProductionRunView.vue');assert.match(view,/manualWorkbenchProtected.value \|\| recipeShotSelectionProtected.value/)
  assert.match(view,/@context-protected="protectRecipeShotSelection"/)
})
