import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
import {readFileSync} from 'node:fs'

const id = (n:number) => `00000000-0000-4000-8000-${String(n).padStart(12,'0')}`
const hash = 'a'.repeat(64), run:any = {id:id(1),project:id(2),planSource:'manual',status:'awaiting_input',executionPlan:{sha256:hash}}
const options:any = {runId:run.id,runPlanSha256:hash,actions:[{stageKey:'storyboard_review',action:'select',scopeKinds:['shot']}]}
const asset:any = {id:id(5),name:'雨衣主图',type:'image',mimeType:'image/png',checksum:hash,isActive:true,tags:[]}
const baseline = (shot=id(3)):any => ({run_id:run.id,stage_key:'storyboard_review',run_plan_sha256:hash,shot_id:shot,
  storyboard_adoption_revision:2,storyboard_adoption_sha256:'b'.repeat(64),uses_sha256:'c'.repeat(64),
  state:{schema_version:1,project_id:run.project,shot_id:shot,uses:[{id:id(4),shot_id:shot,use_key:'雨中旅人',position:0,
    purpose:'character_reference',media_kind:'image',setting_revision_id:id(8),source_mode:'',reference_version_id:null,asset_id:null,asset_sha256:''}]},
  editability:{allowed:true,code:'editable',reason:'只改本镜，不等于审核。'}})
const receipt = {action:'select',result_kind:'shot_reference_choices',target_id:id(3)}
const rejection = (status:number) => ({response:{status,data:{detail:'测试拒绝'}}})
async function modules(fn:(m:any,vite:any)=>Promise<void>|void) {
  const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {await fn(await vite.ssrLoadModule('/src/services/productionManualShotReferences.ts'),vite)} finally {await vite.close()}
}
test('strict baseline accepts declared pending uses and blocked zero adoption, never infers targets',()=>modules(m=>{
  const raw=baseline(), detail=m.readManualShotReferences(raw,run,id(3)); assert.equal(detail.state.uses[0].source_mode,'')
  const blank=baseline();blank.state.uses[0].purpose='';assert.equal(m.readManualShotReferences(blank,run,id(3)).state.uses[0].purpose,'')
  raw.state.uses[0].purpose='changed';assert.notEqual(detail.state.uses[0].purpose,'changed')
  const missing=baseline();missing.storyboard_adoption_revision=0;missing.storyboard_adoption_sha256=null;missing.editability={allowed:false,code:'storyboard_not_current',reason:'尚未采用'}
  assert.equal(m.readManualShotReferences(missing,run,id(3)).editability.allowed,false)
}))
test('wrong run/project/shot/plan, missing states, duplicated uses and invented generated source are rejected',()=>modules(m=>{
  const mutations=[(v:any)=>v.run_id=id(9),(v:any)=>v.state.project_id=id(9),(v:any)=>v.shot_id=id(9),(v:any)=>v.run_plan_sha256='z',
    (v:any)=>v.editability.allowed=null,(v:any)=>v.storyboard_adoption_sha256=null,(v:any)=>v.state.uses.push({...v.state.uses[0]}),
    (v:any)=>v.state.uses[0].asset_id=id(5),(v:any)=>Object.assign(v.state.uses[0],{source_mode:'generated',asset_id:id(5),asset_sha256:hash})]
  for(const mutate of mutations){const raw=baseline();mutate(raw);assert.throws(()=>m.readManualShotReferences(raw,run,id(3)))}
}))
test('library version selection preserves exact version, while direct upload stays original Asset and source',()=>modules(m=>{
  const use=baseline().state.uses[0], reference={series:{id:id(7),name:'雨衣'},version:{id:id(6),series_id:id(7),version:2,asset:{id:asset.id},setting_revision_id:id(8),source:'generated'}}
  const selected=m.selectShotReference(use,asset,'asset_library',reference)
  assert.equal(selected.choice.reference_version_id,id(6));assert.equal(selected.choice.asset_id,null);assert.equal(selected.choice.source_mode,'existing')
  assert.match(selected.label,/版本 2/)
  const upload=m.selectShotReference(use,asset,'asset_upload');assert.equal(upload.choice.asset_id,asset.id);assert.equal(upload.choice.source_mode,'upload')
  assert.throws(()=>m.selectShotReference(use,asset,'asset_upload',reference))
  reference.version.setting_revision_id=id(99);assert.throws(()=>m.selectShotReference(use,asset,'asset_library',reference),/设定/)
}))
test('bad media type, inactive asset and missing digest never become a draft',()=>modules(m=>{
  for(const patch of [{mimeType:'video/mp4'},{isActive:false},{checksum:''}])assert.throws(()=>m.selectShotReference(baseline().state.uses[0],{...asset,...patch},'asset_library'))
}))
test('original select envelope carries observed revisions/SHA and only the chosen use, not approval or generation',()=>modules(m=>{
  const detail=baseline();detail.state.uses.push({...detail.state.uses[0],id:id(20),use_key:'第二用途',position:1})
  const choice=m.selectShotReference(detail.state.uses[0],asset,'asset_library')
  const request=m.buildShotReferenceSelection(detail,run,options,'request-key',choice)
  assert.equal(request.action,'select');assert.equal(request.expected_selection_revision,2);assert.equal(request.unit_key,`shot:${id(3)}`)
  assert.deepEqual(Object.keys(request.payload).sort(),['choices','expected_storyboard_adoption_sha256','expected_uses_sha256'])
  assert.equal(request.payload.choices.length,1);assert.equal(request.payload.choices[0].use_id,id(4))
  choice.choice.asset_sha256='d'.repeat(64);assert.equal(request.payload.choices[0].asset_sha256,hash)
}))
test('closed run, absent frozen select, wrong options or server read-only cannot create new command',()=>modules(m=>{
  const detail=baseline(), choice=m.selectShotReference(detail.state.uses[0],asset,'asset_library')
  for(const [r,o,d] of [[{...run,status:'cancelled'},options,detail],[run,{...options,actions:[]},detail],
    [run,{...options,runId:id(90)},detail],[run,options,{...detail,editability:{allowed:false,code:'shot_prepared',reason:'已有准备'}}]])
    assert.throws(()=>m.buildShotReferenceSelection(d,r,o,'key',choice))
}))
test('unknown command retries byte-identical request even after terminal status and later selection; no duplicate submit',()=>modules(async m=>{
  const current={...run}, requests:any[]=[];let changes=0, release:any
  const editor=m.createManualShotReferenceEditor({read:async()=>baseline(),submit:async(_r:any,p:any)=>{
    requests.push(p);if(requests.length===1)throw Error('timeout');await new Promise(r=>release=r);return receipt}},()=>current,()=>options,()=>changes++)
  await editor.load(id(3));editor.state.selection=m.selectShotReference(editor.state.detail.state.uses[0],asset,'asset_library')
  await editor.submit();assert.ok(editor.state.pending)
  current.status='cancelled';editor.state.selection.choice.asset_sha256='f'.repeat(64)
  const retry=editor.submit();await editor.submit();await editor.load(id(30));assert.equal(requests.length,2);release();await retry
  assert.deepEqual(requests[0],requests[1]);assert.equal(changes,1);assert.equal(editor.state.pending,null);assert.equal(editor.state.shotId,id(3))
}))
test('unknown response stays pending when replay returns a definitive rejection or malformed receipt',()=>modules(async m=>{
  for(const later of [rejection(400),rejection(403),rejection(409),'malformed']){
    let calls=0;const editor=m.createManualShotReferenceEditor({read:async()=>baseline(),submit:async()=>{
      if(++calls===1)throw Error('offline');if(later==='malformed')return {...receipt,target_id:id(99)};throw later}},()=>run,()=>options,()=>assert.fail())
    await editor.load(id(3));editor.state.selection=m.selectShotReference(editor.state.detail.state.uses[0],asset,'asset_library')
    await editor.submit();const original=JSON.stringify(editor.state.pending);await editor.submit();assert.equal(JSON.stringify(editor.state.pending),original)
  }
}))
test('initial 400 preserves draft and unlocks; initial conflict requires successful reread before any new write',()=>modules(async m=>{
  for(const status of [400,409]){
    let calls=0;const editor=m.createManualShotReferenceEditor({read:async()=>baseline(),submit:async()=>{calls++;throw rejection(status)}},()=>run,()=>options,()=>assert.fail())
    await editor.load(id(3));editor.state.selection=m.selectShotReference(editor.state.detail.state.uses[0],asset,'asset_upload')
    await editor.submit();assert.equal(editor.state.pending,null);assert.ok(editor.state.selection)
    assert.equal(editor.state.conflict,status===409)
    if(status===409){await editor.submit();assert.equal(calls,1);await editor.load(id(3));assert.equal(editor.state.conflict,false);assert.equal(editor.state.selection,null)}
  }
}))
test('successful receipt plus failed refresh stays confirmed but stale, never invites repeat write',()=>modules(async m=>{
  let reads=0,calls=0;const editor=m.createManualShotReferenceEditor({read:async()=>{if(++reads>1)throw Error('refresh offline');return baseline()},submit:async()=>{calls++;return receipt}},()=>run,()=>options,()=>{})
  await editor.load(id(3));editor.state.selection=m.selectShotReference(editor.state.detail.state.uses[0],asset,'asset_library');await editor.submit()
  assert.equal(editor.state.pending,null);assert.equal(editor.state.selection,null);assert.equal(editor.state.stale,true);assert.match(editor.state.notice,/已确认/);assert.match(editor.state.error,/offline/)
  await editor.submit();assert.equal(calls,1)
}))
test('late baseline never overwrites a different shot; dispose invalidates pending reads',()=>modules(async m=>{
  let release:any;const editor=m.createManualShotReferenceEditor({read:async(_r:any,s:any)=>s===id(3)?await new Promise(r=>release=r):baseline(s),submit:async()=>assert.fail()},()=>run,()=>options,()=>{})
  const first=editor.load(id(3));await editor.load(id(30));release(baseline());await first;assert.equal(editor.state.detail.shot_id,id(30))
  const late=editor.load(id(3));editor.dispose();release(baseline());await late;assert.equal(editor.state.detail,null)
}))
test('original workbench retains editor through modal closure and pauses polling; picker forwards selected version identity',()=>{
  const source=readFileSync(new URL('../src/components/ManualProductionWorkbench.vue',import.meta.url),'utf8')
  assert.match(source,/ManualShotReferenceSelection v-if="shotSelectionContext" :open="shotSelectionOpen"/)
  assert.match(source,/generationOpen.value \|\| bibleImageProtected.value \|\| shotSelectionProtected.value/)
  assert.match(source,/shotSelectionRetained.value.pending/);assert.match(source,/!shotSelectionBusy.value/)
  const picker=readFileSync(new URL('../src/components/ExecutionInputAssetPicker.vue',import.meta.url),'utf8')
  assert.match(picker,/emit\('selected', asset, 'asset_library', reference\)/)
})
