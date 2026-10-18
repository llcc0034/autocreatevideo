import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'

const root=fileURLToPath(new URL('..',import.meta.url)),sha='a'.repeat(64)
const id=(n:number)=>`10000000-0000-0000-0000-${String(n).padStart(12,'0')}`
const run={id:id(1),project:id(2),status:'awaiting_input',planSource:'manual',executionPlan:{sha256:sha}}
const options={runId:run.id,runPlanSha256:sha,actions:[{stageKey:'delivery',action:'deliver',scopeKinds:['project']}]}
const raw={run_id:run.id,run_plan_sha256:sha,contract_sha256:sha,blockers:[],bundle:null,
  contract:{schema_version:1,kind:'manual_delivery',run_plan_sha256:sha,assembly_contract_sha256:sha,selection_revision:1,
    episodes:[{episode_id:id(3),number:1,title:'First',assembly_job_id:id(4),artifact_id:id(5),sha256:sha,manifest_sha256:sha,size_bytes:100,mime_type:'video/mp4'}]}}
const master=()=>({id:id(5),productionRun:run.id,kind:'assembly',status:'available',sha256:sha,manifestMetadataSha256:sha,
  sizeBytes:100,mimeType:'video/mp4',objectKey:'isolated/master.mp4',previewUrl:'/fresh-preview',downloadUrl:'/fresh-download'})
async function modules(fn){const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try{await fn(await vite.ssrLoadModule('/src/services/productionManualDelivery.ts'),vite)}finally{await vite.close()}}

test('explicit delivery uses exact metadata contract and original command, without media overrides',()=>modules(m=>{
  const detail=m.readManualDelivery(raw,run),body=m.buildManualDelivery(detail,run,options,'seal-once','Reviewed')
  assert.equal(body.action,'deliver');assert.equal(body.stage_key,'delivery')
  assert.deepEqual(body.scope,{kind:'project',id:run.project});assert.equal(body.expected_selection_revision,1)
  assert.deepEqual(body.payload,{expected_contract_sha256:sha,comment:'Reviewed'})
  const sealed=m.readManualDelivery({...raw,bundle:{id:id(8),status:'sealed',manifest_sha256:sha,sealed_at:'2026-09-10T10:00:00Z'}},run)
  assert.equal(sealed.bundle.id,id(8));assert.throws(()=>m.buildManualDelivery(sealed,run,options,'again',''))
}))

test('invalid, foreign, missing and blocked deliveries do not appear ready',()=>modules(m=>{
  for(const bad of [{...raw,run_id:id(99)},{...raw,contract_sha256:undefined},{...raw,bundle:undefined},
    {...raw,contract:{...raw.contract,kind:'old_delivery'}},
    {...raw,contract:{...raw.contract,episodes:[raw.contract.episodes[0],raw.contract.episodes[0]]}},
    {...raw,contract:{...raw.contract,episodes:[{...raw.contract.episodes[0],sha256:undefined}]}}]) assert.throws(()=>m.readManualDelivery(bad,run))
  const blocked=m.readManualDelivery({...raw,blockers:['Assembly incomplete']},run)
  assert.throws(()=>m.buildManualDelivery(blocked,run,options,'bad',''))
  const ready=m.readManualDelivery(raw,run)
  assert.throws(()=>m.buildManualDelivery(ready,{...run,status:'succeeded'},options,'bad',''))
  assert.throws(()=>m.buildManualDelivery(ready,run,{...options,actions:[]},'bad',''))
  assert.throws(()=>m.buildManualDelivery(ready,run,{...options,runId:id(99)},'bad',''))
}))

test('formal read propagates errors and screen has explicit confirmation, preview and idempotent recovery',()=>modules(async(_m,vite)=>{
  const {api}=await vite.ssrLoadModule('/src/services/api.ts'),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const original=api.get,calls=[]
  try{api.get=async path=>{calls.push(path);return {data:raw}}
    assert.deepEqual(await platformApi.getManualDelivery(run.id),raw)
    assert.deepEqual(calls,[`/production-runs/${run.id}/manual-delivery/`])
    api.get=async()=>{throw new Error('Unavailable')};await assert.rejects(platformApi.getManualDelivery(run.id),/Unavailable/)
  }finally{api.get=original}
  const view=await readFile(new URL('../src/components/ManualDeliveryReview.vue',import.meta.url),'utf8')
  const workbench=await readFile(new URL('../src/components/ManualProductionWorkbench.vue',import.meta.url),'utf8')
  for(const token of ['pending.value','token !== sequence','window.confirm','重试同一封存请求','确认成片并封存','getArtifact(episode.artifactId)','verifyAssemblyMedia(artifact']) assert.ok(view.includes(token),token)
  assert.ok(workbench.includes('ManualDeliveryReview'));assert.ok(workbench.includes('查看成片与封存'))
  assert.equal(view.includes('createProductionRun('),false)
  await vite.ssrLoadModule('/src/components/ManualDeliveryReview.vue')
}))

async function mounted(vite, fn) {
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const originals={getManualDelivery:platformApi.getManualDelivery,getArtifact:platformApi.getArtifact,
    performManualProductionAction:platformApi.performManualProductionAction}
  const oldWindow=Object.getOwnPropertyDescriptor(globalThis,'window')
  Object.defineProperty(globalThis,'window',{configurable:true,value:{confirm:()=>true}})
  const reads=[],writes=[],events=[]
  platformApi.getManualDelivery=async runId=>{reads.push(runId);return structuredClone({...raw,run_id:runId})}
  platformApi.performManualProductionAction=async(runId,body)=>{writes.push(structuredClone({runId,body}));return {action:'deliver',result_kind:'release_bundle',target_id:id(8)}}
  const {default:Component}=await vite.ssrLoadModule('/src/components/ManualDeliveryReview.vue')
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const props=reactive({run:{...run},options:{...options}});let state:any
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(...args)=>events.push(args)});return()=>h('div')}})
  try{
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await Promise.resolve();await nextTick()
    await fn({state,props,platformApi,reads,writes,events,unmount:()=>app.unmount()})
  }finally{app.unmount();Object.assign(platformApi,originals)
    if(oldWindow)Object.defineProperty(globalThis,'window',oldWindow);else delete globalThis.window}
}

test('delivery confirmation is explicit and rereading never discards an uncertain command',()=>modules(async(_m,vite)=>mounted(vite,async({state,platformApi,writes,events})=>{
  await state.submit();assert.equal(writes.length,0)
  let confirmations=0;window.confirm=()=>{confirmations++;return false}
  state.checked.value=true;await state.submit();assert.equal(writes.length,0)
  window.confirm=()=>{confirmations++;return true}
  platformApi.performManualProductionAction=async(runId,body)=>{
    writes.push(JSON.parse(JSON.stringify({runId,body})))
    if(writes.length===1)throw new Error('uncertain response')
    return {action:'deliver',result_kind:'release_bundle',target_id:id(8)}
  }
  await state.submit();assert.match(state.error.value,/uncertain/)
  const original=JSON.parse(JSON.stringify(state.pending.value))
  await state.reread();assert.deepEqual(JSON.parse(JSON.stringify(state.pending.value)),original)
  assert.equal(state.checked.value,false)
  await state.submit();assert.deepEqual(writes[0],writes[1])
  assert.equal(confirmations,3);assert.equal(events.filter(row=>row[0]==='changed').length,1)
  assert.equal(state.pending.value,null);assert.match(state.notice.value,/已核验并封存/)
})))

test('delivery preview verifies the exact master and invalid preview clears human confirmation',()=>modules(async(_m,vite)=>mounted(vite,async({state,platformApi,writes})=>{
  const episode=state.detail.value.episodes[0];let complete
  platformApi.getArtifact=()=>new Promise(resolve=>{complete=resolve})
  state.checked.value=true
  const loading=state.preview(episode);await state.submit();assert.equal(writes.length,0)
  complete({id:episode.artifactId,productionRun:id(99),kind:'assembly',sha256:sha,manifestMetadataSha256:sha,previewUrl:'/wrong'})
  await loading;assert.equal(state.checked.value,false);assert.deepEqual(state.previews.value,{})
  assert.match(state.error.value,/不一致/)
  platformApi.getArtifact=async artifactId=>({id:artifactId,productionRun:run.id,kind:'assembly',sha256:sha,
    manifestMetadataSha256:sha,status:'available',sizeBytes:100,mimeType:'video/mp4',objectKey:'isolated/master.mp4',previewUrl:'/verified-preview',downloadUrl:'/signed-download'})
  await state.preview(episode);assert.equal(state.previews.value[episode.id].url,'/verified-preview')
  assert.equal(writes.length,0)
})))

test('late delivery response cannot mark a different Run successful or reread after unmount',()=>modules(async(_m,vite)=>mounted(vite,async({state,props,platformApi,events,reads,unmount})=>{
  let complete
  platformApi.performManualProductionAction=()=>new Promise(resolve=>{complete=resolve})
  state.checked.value=true;const submitting=state.submit()
  props.run={...run,id:id(10)};props.options={...options,runId:id(10)};await nextTick()
  assert.equal(state.detail.value,null)
  complete({action:'deliver',result_kind:'release_bundle',target_id:id(8)});await submitting
  assert.equal(events.filter(row=>row[0]==='changed').length,0);assert.equal(state.notice.value,'')
  assert.equal(reads.at(-1),id(10));assert.ok(state.detail.value)
  state.checked.value=true;const detached=state.submit();unmount();const count=reads.length
  complete({action:'deliver',result_kind:'release_bundle',target_id:id(8)});await detached
  assert.equal(reads.length,count);assert.equal(events.filter(row=>row[0]==='changed').length,0)
})))

test('same-run polling preserves preview and review draft; actual plan change invalidates them',()=>modules(async(_m,vite)=>mounted(vite,async({state,props,platformApi,reads,writes})=>{
  const episode=state.detail.value.episodes[0]
  platformApi.getArtifact=async artifactId=>({id:artifactId,productionRun:run.id,kind:'assembly',sha256:sha,
    manifestMetadataSha256:sha,status:'available',sizeBytes:100,mimeType:'video/mp4',objectKey:'isolated/master.mp4',previewUrl:'/verified-preview',downloadUrl:'/signed-download'})
  await state.preview(episode)
  state.comment.value='Review in progress';state.checked.value=true
  const count=reads.length
  for(let i=0;i<3;i++) {
    props.run={...run,executionPlan:{sha256:sha},status:'running'}
    await nextTick();await nextTick()
  }
  assert.equal(reads.length,count)
  assert.equal(state.previews.value[episode.id].url,'/verified-preview')
  assert.equal(state.comment.value,'Review in progress');assert.equal(state.checked.value,true)
  assert.equal(writes.length,0)
  props.run={...run,executionPlan:{sha256:'b'.repeat(64)}};await nextTick();await nextTick()
  assert.equal(reads.length,count+1);assert.deepEqual(state.previews.value,{})
  assert.equal(state.comment.value,'');assert.equal(state.checked.value,false);assert.equal(writes.length,0)
})))

test('manual and sealed entry share the exact master validator without requiring model provenance',()=>modules(async(_m,vite)=>{
  const {verifyAssemblyMedia}=await vite.ssrLoadModule('/src/services/assemblyMedia.ts')
  const expected={artifactId:id(5),sha256:sha}, manual={...expected,manifestSha256:sha,sizeBytes:100}
  for(const identity of [expected,manual]) {
    // No generation attempt or recipe source is manufactured for an existing-media master.
    const artifact=master();assert.equal(verifyAssemblyMedia(artifact,identity,run.id),artifact)
    for(const patch of [{status:'failed'},{sizeBytes:0},{mimeType:'image/png'},{objectKey:''},{downloadUrl:''},{previewUrl:''},
      {productionRun:id(99)},{id:id(99)},{sha256:'b'.repeat(64)}])
      assert.throws(()=>verifyAssemblyMedia({...artifact,...patch},identity,run.id))
  }
  assert.throws(()=>verifyAssemblyMedia({...master(),manifestMetadataSha256:'b'.repeat(64)},manual,run.id))
  assert.throws(()=>verifyAssemblyMedia({...master(),sizeBytes:101},manual,run.id))
}))

test('manual failed reread clears previous playable master and blocks confirmation',()=>modules(async(_m,vite)=>mounted(vite,async({state,platformApi,writes})=>{
  const episode=state.detail.value.episodes[0];platformApi.getArtifact=async()=>master()
  await state.preview(episode);assert.ok(state.previews.value[episode.id]);state.checked.value=true
  platformApi.getArtifact=async()=>({...master(),sizeBytes:0})
  await state.preview(episode);assert.deepEqual(state.previews.value,{});assert.equal(state.checked.value,false)
  await state.submit();assert.equal(writes.length,0);assert.match(state.error.value,/不可用/)
})))

test('manual download rereads exact master; expired response and changed Run never use old links',()=>modules(async(_m,vite)=>mounted(vite,async({state,props,platformApi,writes})=>{
  const episode=state.detail.value.episodes[0],saved=[],reads=[]
  const original=Object.getOwnPropertyDescriptor(globalThis,'document')
  Object.defineProperty(globalThis,'document',{configurable:true,value:{body:{appendChild(){}},createElement(){return{
    href:'',download:'',click(){saved.push(this.href)},remove(){}}}}})
  try {
    platformApi.getArtifact=async key=>{reads.push(key);return master()}
    await state.preview(episode);await state.preview(episode,true)
    assert.deepEqual(reads,[episode.artifactId,episode.artifactId]);assert.deepEqual(saved,['/fresh-download'])
    platformApi.getArtifact=async()=>{throw Error('403 unavailable')}
    await state.preview(episode,true);assert.equal(saved.length,1);assert.deepEqual(state.previews.value,{})
    let finish;platformApi.getArtifact=()=>new Promise(resolve=>{finish=resolve})
    const pending=state.preview(episode,true);props.run={...run,id:id(99)};await nextTick()
    finish(master());await pending;assert.equal(saved.length,1);assert.deepEqual(state.previews.value,{})
    assert.equal(writes.length,0)
  } finally {if(original)Object.defineProperty(globalThis,'document',original);else delete globalThis.document}
})))
