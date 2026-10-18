import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
const id=(n:number)=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`
const sha='a'.repeat(64),run:any={id:id(1),project:id(2),planSource:'manual',status:'awaiting_input',executionPlan:{sha256:sha}}
const options:any={runId:run.id,runPlanSha256:sha,actions:[{stageKey:'storyboard_review',action:'select',scopeKinds:['shot']}]}
const asset:any={id:id(5),name:'明确新图',type:'image',mimeType:'image/png',checksum:sha,isActive:true}
function detail(n=3):any{return {run_id:run.id,stage_key:'storyboard_review',run_plan_sha256:sha,shot_id:id(n),
  uses_sha256:'b'.repeat(64),storyboard_adoption_revision:2,storyboard_adoption_sha256:'c'.repeat(64),
  state:{schema_version:1,project_id:run.project,shot_id:id(n),uses:[0,1].map(i=>({id:id(n*10+i),shot_id:id(n),use_key:`用途${i+1}`,
    position:i,purpose:i?'另一个用途':'',media_kind:'image',setting_revision_id:null,source_mode:'',reference_version_id:null,asset_id:null,asset_sha256:''}))},
  editability:{allowed:true,code:'editable',reason:'本镜可编辑'}}}
const reject=(status:number)=>({response:{status,data:{detail:'测试拒绝'}}})
const receipt=(shot:string)=>({id:id(99),action:'select',result_kind:'shot_reference_choices',target_id:shot})
async function modules(fn:(b:any,m:any)=>Promise<void>|void){
  const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try{await fn(await vite.ssrLoadModule('/src/services/shotReferenceBatch.ts'),await vite.ssrLoadModule('/src/services/productionManualShotReferences.ts'))}finally{await vite.close()}
}
const add=(batch:any,m:any,d=detail(),i=0)=>batch.add(d,`镜头${d.shot_id.slice(-1)}`,m.selectShotReference(d.state.uses[i],asset,'asset_library'),'待选择')
test('one command per selected shot preserves two independent uses and original baseline; unchecked shot unchanged',()=>modules(async(b,m)=>{
  const sent:any[]=[],reads:string[]=[],changed:string[]=[]
  const batch=b.createShotReferenceBatch({read:async(_r:string,s:string)=>{reads.push(s);return detail(Number(s.slice(-1)))},submit:async(_r:string,p:any)=>{sent.push(p);return receipt(p.scope.id)}},()=>run,()=>options,()=>changed.push('yes'))
  add(batch,m);add(batch,m,detail(),1);add(batch,m,detail(4));batch.check(batch.state.shots[1],null,false)
  await batch.submit();assert.equal(sent.length,0)
  await batch.preview();assert.deepEqual(reads,[id(3)]);assert.equal(sent.length,0)
  await batch.submit();assert.equal(sent.length,1);assert.equal(changed.length,1)
  assert.deepEqual(sent[0].payload.choices.map((x:any)=>x.use_id),[id(30),id(31)])
  assert.equal(sent[0].payload.expected_uses_sha256,'b'.repeat(64));assert.equal(batch.state.shots[0].status,'confirmed');assert.equal(batch.state.shots[1].status,'draft')
  await batch.submit();assert.equal(sent.length,1)
}))
test('checkbox change invalidates all ready requests; never submits an unchecked use',()=>modules(async(b,m)=>{
  const sent:any[]=[],batch=b.createShotReferenceBatch({read:async()=>detail(),submit:async(_r:any,p:any)=>{sent.push(p);return receipt(id(3))}},()=>run,()=>options,()=>{})
  add(batch,m);add(batch,m,detail(),1);await batch.preview();const key=batch.state.shots[0].request.idempotency_key
  batch.check(batch.state.shots[0],id(31),false);await batch.submit();assert.equal(sent.length,0)
  await batch.preview();assert.notEqual(batch.state.shots[0].request.idempotency_key,key);await batch.submit()
  assert.deepEqual(sent[0].payload.choices.map((x:any)=>x.use_id),[id(30)])
}))
test('changed baseline/source is a visible pre-submit conflict while independent shot still saves',()=>modules(async(b,m)=>{
  const sent:any[]=[],batch=b.createShotReferenceBatch({read:async(_r:string,s:string)=>{const d=detail(Number(s.slice(-1)));if(s===id(3))d.storyboard_adoption_revision++;return d},submit:async(_r:any,p:any)=>{sent.push(p);return receipt(p.scope.id)}},()=>run,()=>options,()=>{})
  add(batch,m);add(batch,m,detail(4));await batch.preview();assert.equal(batch.state.shots[0].status,'conflict');await batch.submit()
  assert.deepEqual(sent.map(x=>x.scope.id),[id(4)]);assert.deepEqual(batch.state.shots.map((x:any)=>x.status),['conflict','confirmed'])
}))
test('post-preview 409 and bad material 400 do not hide remaining successful shot receipts',()=>modules(async(b,m)=>{
  const sent:any[]=[],batch=b.createShotReferenceBatch({read:async(_r:string,s:string)=>detail(Number(s.slice(-1))),submit:async(_r:any,p:any)=>{
    sent.push(p);if(p.scope.id===id(3))throw reject(409);if(p.scope.id===id(4))throw reject(400);return receipt(p.scope.id)}},()=>run,()=>options,()=>{})
  add(batch,m);add(batch,m,detail(4));add(batch,m,detail(6));await batch.preview();await batch.submit()
  assert.equal(sent.length,3);assert.deepEqual(batch.state.shots.map((x:any)=>x.status),['conflict','rejected','confirmed'])
  await batch.preview();await batch.submit();assert.equal(sent.length,3)
}))
test('unknown request is immutable, prohibits discard/retarget and only explicit same-request retry after terminal state',()=>modules(async(b,m)=>{
  const current={...run},sent:any[]=[],batch=b.createShotReferenceBatch({read:async()=>detail(),submit:async(_r:any,p:any)=>{
    sent.push(p);if(sent.length===1)throw Error('timeout');return receipt(id(3))}},()=>current,()=>options,()=>{})
  add(batch,m);await batch.preview();await batch.submit();const shot=batch.state.shots[0]
  assert.equal(shot.status,'unknown');batch.remove(shot);assert.equal(batch.state.shots.length,1)
  assert.throws(()=>add(batch,m,detail(4)));batch.check(shot,null,false);assert.equal(shot.uses[0].checked,true)
  await batch.preview();await batch.submit();assert.equal(sent.length,1)
  current.status='cancelled';shot.uses[0].selection.choice.asset_id=id(90);await batch.retry(shot)
  assert.deepEqual(sent[0],sent[1]);assert.equal(shot.status,'confirmed')
}))
test('malformed receipt or a later 4xx never clears the original unknown request',()=>modules(async(b,m)=>{
  for(const later of [400,403,409]){
    let calls=0;const batch=b.createShotReferenceBatch({read:async()=>detail(),submit:async()=>{if(++calls===1)return receipt(id(90));throw reject(later)}},()=>run,()=>options,()=>assert.fail())
    add(batch,m);await batch.preview();await batch.submit();const shot=batch.state.shots[0],request=JSON.stringify(shot.request)
    await batch.retry(shot);assert.equal(shot.status,'unknown');assert.equal(JSON.stringify(shot.request),request)
  }
}))
test('pending submit cannot be duplicated or changed; preview failure never builds a writable command',()=>modules(async(b,m)=>{
  let release:any,calls=0;const batch=b.createShotReferenceBatch({read:async(_r:string,s:string)=>{if(s===id(4))throw reject(403);return detail()},submit:async()=>{calls++;return await new Promise(r=>release=r)}},()=>run,()=>options,()=>{})
  add(batch,m);add(batch,m,detail(4));await batch.preview();assert.equal(batch.state.shots[1].status,'blocked')
  const pending=batch.submit();await batch.submit();assert.throws(()=>add(batch,m));release(receipt(id(3)));await pending;assert.equal(calls,1)
}))
test('unseen context change invalidates late preview and prohibits writing drafts into a different run',()=>modules(async(b,m)=>{
  let release:any;const current={...run},batch=b.createShotReferenceBatch({read:async()=>await new Promise(r=>release=r),submit:async()=>assert.fail()},()=>current,()=>options,()=>{})
  add(batch,m);const reading=batch.preview();current.id=id(90);release(detail());await reading
  assert.equal(batch.state.previewed,false);assert.match(batch.state.error,/上下文/);await batch.submit();assert.throws(()=>add(batch,m,detail(4)))
}))
test('draft clones do not follow caller mutation or accept a different same-shot baseline',()=>modules((b,m)=>{
  const batch=b.createShotReferenceBatch({read:async()=>detail(),submit:async()=>assert.fail()},()=>run,()=>options,()=>{})
  const d=detail();add(batch,m,d);d.state.uses[0].purpose='changed';assert.equal(batch.state.shots[0].uses[0].use.purpose,'')
  d.uses_sha256='d'.repeat(64);assert.throws(()=>add(batch,m,d,1),/基线/)
}))
test('shared original selection policy still enforces recipe source and replacement permissions for batch drafts',()=>modules((b,m)=>{
  const current={...run,planSource:'recipe'},d=detail();delete d.storyboard_adoption_revision;delete d.storyboard_adoption_sha256
  d.source_snapshot_sha256='c'.repeat(64);d.selection_rule={rule:{source:'existing',source_stage_key:null,selection:'manual',allow_override:false},upstream:null};d.upstream_candidates=[]
  const batch=b.createShotReferenceBatch({read:async()=>d,submit:async()=>assert.fail()},()=>current,()=>undefined,()=>{})
  add(batch,m,d);assert.throws(()=>batch.add(d,'镜头',m.selectShotReference(d.state.uses[1],asset,'asset_upload'),'待选'),/来源/)
  Object.assign(d.state.uses[1],{source_mode:'existing',asset_id:id(80),asset_sha256:sha});assert.throws(()=>add(batch,m,d,1),/不允许替换/)
}))
test('saved media preview keeps exact identity and exposes failures instead of showing a later file',()=>modules(async(_b,m)=>{
  const use=detail().state.uses[0],api={getVersion:async()=>assert.fail(),getSeries:async()=>assert.fail(),getAsset:async()=>asset}
  assert.equal(await m.readSavedShotReference(api,use),null)
  Object.assign(use,{source_mode:'existing',asset_id:asset.id,asset_sha256:sha});assert.equal((await m.readSavedShotReference(api,use)).label,asset.name)
  use.asset_sha256='f'.repeat(64);await assert.rejects(()=>m.readSavedShotReference(api,use),/指纹/)
}))
