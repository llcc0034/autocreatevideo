import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'

const root=fileURLToPath(new URL('..',import.meta.url)), sha='a'.repeat(64)
const id=(n:number)=>`10000000-0000-0000-0000-${String(n).padStart(12,'0')}`
const run={id:id(1),project:id(2),status:'awaiting_input',planSource:'manual',executionPlan:{sha256:sha}}
const options={runId:run.id,runPlanSha256:sha,actions:[{stageKey:'assembly',action:'assemble',scopeKinds:['episode']}]}
const source={artifact_id:id(6),kind:'image',artifact_sha256:sha,manifest_sha256:sha,decision_id:id(7),
  decision_revision:1,scope:{kind:'shot',id:id(4)}}
const raw={run_id:run.id,run_plan_sha256:sha,contract_sha256:sha,frozen:false,blockers:[],approval:{revision:1},
  authors:{},author_contracts:{},author_schemas:{assembly_output:{type:'object'},assembly_timing:{type:'object'},assembly_media:{type:'object'}},
  episodes:[{episode_id:id(3),number:1,title:'Episode',assembly_link_id:null,job:null,
    shots:[{shot_id:id(4),number:1,title:'Shot',duration_ms:2500,media_kind:'image',source}]}],
  output:{width:1280,height:720,fps:24,media_profile:{schema_version:1},media_profile_key:'delivery.assembly_media_profile',
    media_profile_sha256:sha,max_attempts:2,label_enabled:false,label_text:''}}
async function modules(fn){const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try{await fn(await vite.ssrLoadModule('/src/services/productionManualAssembly.ts'),vite)}finally{await vite.close()}}

test('manual assembly carries exact reviewed contract and explicit failed job without model choices',()=>modules(m=>{
  const detail=m.readManualAssembly(raw,run), body=m.buildManualAssembly(detail,id(3),run,options,'enqueue')
  assert.deepEqual(body.payload,{expected_contract_sha256:sha,expected_assembly_job_id:null})
  assert.deepEqual(body.scope,{kind:'episode',id:id(3)});assert.equal(body.expected_selection_revision,1)
  assert.equal(body.stage_key,'assembly');assert.equal(body.action,'assemble')
  const failed={...raw,frozen:true,episodes:[{...raw.episodes[0],assembly_link_id:id(8),job:{id:id(9),status:'failed',attempts:2,max_attempts:2,error_message:'Encoder failure',updated_at:'2026-09-10T10:00:00Z'}}]}
  const retry=m.readManualAssembly(failed,run)
  assert.equal(retry.episodes[0].job.error,'Encoder failure')
  assert.equal(m.buildManualAssembly(retry,id(3),run,options,'retry').payload.expected_assembly_job_id,id(9))
  assert.equal('workflow_version_id' in body.payload,false);assert.equal('parameters' in body.payload,false)
}))

test('missing, stale, blocked, repeated or in-flight assembly cannot be presented as ready',()=>modules(m=>{
  for(const bad of [{...raw,run_id:id(99)},{...raw,contract_sha256:undefined},{...raw,frozen:undefined},
    {...raw,output:{...raw.output,fps:undefined}}, {...raw,episodes:[raw.episodes[0],raw.episodes[0]]},
    {...raw,episodes:[{...raw.episodes[0],job:undefined}]},
    {...raw,episodes:[{...raw.episodes[0],shots:[{...raw.episodes[0].shots[0],source:{...source,scope:{kind:'shot',id:id(99)}}}]}]}]) assert.throws(()=>m.readManualAssembly(bad,run))
  const blocked=m.readManualAssembly({...raw,blockers:['Review missing']},run)
  assert.throws(()=>m.buildManualAssembly(blocked,id(3),run,options,'blocked'))
  const pending=m.readManualAssembly({...raw,frozen:true,episodes:[{...raw.episodes[0],assembly_link_id:id(8),job:{id:id(9),status:'queued',attempts:0,max_attempts:2,error_message:'',updated_at:'now'}}]},run)
  assert.equal(pending.episodes[0].job.attempts,0)
  assert.throws(()=>m.buildManualAssembly(pending,id(3),run,options,'inflight'))
  assert.throws(()=>m.buildManualAssembly(m.readManualAssembly(raw,run),id(3),run,{...options,actions:[]},'unregistered'))
}))

test('assembly HTTP inventory is a read, errors propagate, workbench exposes formal action and stable retry',()=>modules(async(_m,vite)=>{
  const {api}=await vite.ssrLoadModule('/src/services/api.ts'),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const original=api.get, calls=[]
  try{api.get=async path=>{calls.push(path);return {data:raw}}
    assert.deepEqual(await platformApi.getManualAssembly(run.id),raw)
    assert.deepEqual(calls,[`/production-runs/${run.id}/manual-assembly/`])
    api.get=async()=>{throw new Error('Unavailable')};await assert.rejects(platformApi.getManualAssembly(run.id),/Unavailable/)
  }finally{api.get=original}
  const view=await readFile(new URL('../src/components/ManualAssemblyReview.vue',import.meta.url),'utf8')
  const workbench=await readFile(new URL('../src/components/ManualProductionWorkbench.vue',import.meta.url),'utf8')
  for(const token of ['pending.value.body','token !== sequence','确认提交本集装配','重试同一请求','尚未批准交付']) assert.ok(view.includes(token),token)
  assert.equal(view.includes('window.confirm'), false)
  assert.ok(workbench.includes('ManualAssemblyReview'));assert.ok(workbench.includes(':dismissible="!assemblyBusy && !assemblyPending"'))
  assert.equal(view.includes('createProductionRun('),false)
}))

async function panel(vite, fn) {
  const {platformApi:api}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const {default:Component}=await vite.ssrLoadModule('/src/components/ManualAssemblyReview.vue')
  const originals={get:api.getManualAssembly,action:api.performManualProductionAction,preview:api.previewManualAssembly}
  const props=reactive({run:{...run},options})
  let state:any
  api.getManualAssembly=async()=>structuredClone(raw)
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{emit(){},expose(){}});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick();await nextTick()
  try{await fn(state,api,props)}finally{app.unmount();api.getManualAssembly=originals.get;api.performManualProductionAction=originals.action;api.previewManualAssembly=originals.preview}
}

test('unresolved mapping change blocks assembly preview without disabling or resetting the author',()=>modules(async(_m,vite)=>{
  await panel(vite,async(s,api)=>{
    let calls=0;api.previewManualAssembly=async()=>{calls++;return structuredClone(raw)}
    s.authorErrors.value={assembly_output:['请先确认映射变更或继续编辑原配置']}
    const original=s.authors.value;await s.preview()
    assert.equal(calls,0);assert.equal(s.loading.value,false);assert.equal(s.authors.value,original);assert.equal(s.previewReady.value,false)
    s.authorErrors.value={};await s.preview();assert.equal(calls,1)
  })
}))

test('inline assembly confirmation never submits before confirmation and invalidates on refresh or run change',()=>modules(async(_m,vite)=>{
  await panel(vite,async(s,api,props)=>{
    const calls=[]
    api.performManualProductionAction=async(_id,body)=>{calls.push(body);return {action:'assemble',result_kind:'assembly_link',target_id:id(8)}}
    s.checked.value=true;await s.submit(id(3));assert.equal(calls.length,0)
    assert.equal(s.confirmation.value.episodeId,id(3))
    s.confirmation.value=null;await s.submit(id(3),true);assert.equal(calls.length,0)
    await s.submit(id(3));await s.load();s.checked.value=true
    await s.submit(id(3),true);assert.equal(calls.length,0)
    await s.submit(id(3));await s.submit(id(99),true);assert.equal(calls.length,0)
    await s.submit(id(3),true);assert.equal(calls.length,1)
    assert.equal(calls[0].payload.expected_contract_sha256,sha)
    await s.submit(id(3),true);assert.equal(calls.length,1)
    s.checked.value=true;await s.submit(id(3));props.run={...run,id:id(90)};await nextTick()
    await s.submit(id(3),true);assert.equal(calls.length,1);assert.equal(s.confirmation.value,null)
  })
}))

test('uncertain assembly response retries the same frozen request and reread requires explicit confirmation',()=>modules(async(_m,vite)=>{
  await panel(vite,async(s,api)=>{
    const bodies=[];let reads=0, release:any
    api.getManualAssembly=async()=>{reads++;return structuredClone(raw)}
    api.performManualProductionAction=async(_id,body)=>{bodies.push(body);await new Promise(resolve=>{release=resolve});throw new Error('Response lost')}
    s.checked.value=true;await s.submit(id(3));const first=s.submit(id(3),true)
    await s.submit(id(3),true);assert.equal(bodies.length,1)
    release();await first;assert.match(s.error.value,/Response lost/)
    const retry=s.submit(id(3));assert.equal(bodies.length,2);assert.equal(bodies[0],bodies[1]);release();await retry
    await s.reread();assert.equal(reads,0);assert.equal(s.rereadConfirmation.value,true);assert.ok(s.pending.value)
    s.rereadConfirmation.value=false;await s.reread(true);assert.equal(reads,0)
    await s.reread();await s.reread(true);assert.equal(reads,1);assert.ok(s.pending.value)
  })
}))

test('unknown request survives failed inventory and resolves only its original command, not a new intent',()=>modules(async(_m,vite)=>{
  await panel(vite,async(s,api)=>{
    const bodies:any[]=[]
    api.performManualProductionAction=async(_id,body)=>{bodies.push(body);throw Error('Response lost')}
    s.checked.value=true;await s.submit(id(3));await s.submit(id(3),true)
    const pending=s.pending.value, old=s.detail.value
    let prevented=0
    s.beforeUnload({preventDefault(){prevented++},returnValue:undefined})
    assert.equal(prevented,1)
    api.getManualAssembly=async()=>{throw Error('Inventory unavailable')}
    await s.reread();await s.reread(true)
    assert.equal(s.pending.value,pending);assert.equal(s.detail.value,old);assert.equal(s.detailFresh.value,false)
    assert.match(s.error.value,/Inventory unavailable/);assert.equal(s.previewReady.value,false)
    s.detail.value=null
    api.performManualProductionAction=async(_id,body)=>{bodies.push(body);return {action:'assemble',result_kind:'assembly_link',target_id:id(8)}}
    await s.submit(id(3))
    assert.equal(bodies.length,2);assert.equal(bodies[0],bodies[1]);assert.equal(s.pending.value,null)
    s.beforeUnload({preventDefault(){prevented++},returnValue:undefined})
    assert.equal(prevented,1)
    assert.match(s.error.value,/Inventory unavailable/)
    const source=await readFile(new URL('../src/components/ManualAssemblyReview.vue',import.meta.url),'utf8')
    assert.ok(source.indexOf('aria-label="装配请求结果未知"')<source.indexOf('<template v-if="detail">'))
  })
}))

test('a later client rejection cannot discard an earlier unknown assembly command',()=>modules(async(_m,vite)=>{
  await panel(vite,async(s,api)=>{
    const bodies:any[]=[]
    api.performManualProductionAction=async(_id,body)=>{bodies.push(body);throw Error('Response lost')}
    s.checked.value=true;await s.submit(id(3));await s.submit(id(3),true)
    const pending=s.pending.value
    for(const status of [400,401,403,404,409,429]) {
      api.performManualProductionAction=async(_id,body)=>{bodies.push(body);throw {response:{status,data:{detail:'Rejected now'}}}}
      await s.submit(id(3));assert.equal(s.pending.value,pending)
    }
    assert.equal(bodies.length,7);assert.ok(bodies.every(body=>body===bodies[0]))
    api.performManualProductionAction=async()=>({action:'assemble',result_kind:'assembly_link',target_id:id(8)})
    await s.submit(id(3));assert.equal(s.pending.value,null)
  })
}))

test('an immediately rejected new command clears only that unexecuted intent',()=>modules(async(_m,vite)=>{
  await panel(vite,async(s,api)=>{
    api.performManualProductionAction=async()=>{throw {response:{status:400,data:{detail:'Contract rejected'}}}}
    s.checked.value=true;await s.submit(id(3));await s.submit(id(3),true)
    assert.equal(s.pending.value,null);assert.equal(s.checked.value,false);assert.equal(s.detailFresh.value,false)
  })
}))

const author=()=>({output_version_id:id(20),expected_contract_sha256:sha,source_text:' {"width":640,"height":360,"fps":30} ',mapping:{schema_version:1,node:{kind:'path',scope:'root',path:[]}}})
const projected=(authors:any,frozen=false)=>({...structuredClone(raw),frozen,authors:structuredClone(authors),
  author_contracts:Object.fromEntries(Object.entries(authors).map(([key,value]:any)=>[key,{output_reference:{id:value.output_version_id,sha256:value.expected_contract_sha256},projection:{valid:true,output:{width:640,height:360,fps:30}}}]))})

test('two-episode frozen author reload preserves completed first job and retries only the uncertain second request',()=>modules(async(_m,vite)=>{
  await panel(vite,async(s,api)=>{
    const authors={assembly_output:author(),assembly_timing:{...author(),output_version_id:id(21)},assembly_media:{...author(),output_version_id:id(22)}}
    const inventory:any=projected(authors,true)
    inventory.episodes[0].assembly_link_id=id(8)
    inventory.episodes[0].job={id:id(9),status:'succeeded',attempts:1,max_attempts:2,error_message:'',updated_at:'now'}
    inventory.episodes.push({...structuredClone(raw.episodes[0]),episode_id:id(13),number:2,title:'Episode 2',
      shots:[{...structuredClone(raw.episodes[0].shots[0]),shot_id:id(14),source:{...source,artifact_id:id(16),decision_id:id(17),scope:{kind:'shot',id:id(14)}}}]})
    api.getManualAssembly=async()=>structuredClone(inventory)
    await s.load();assert.deepEqual(JSON.parse(JSON.stringify(s.authors.value)),authors)
    s.checked.value=true;await s.submit(id(3));assert.equal(s.confirmation.value,null)
    const bodies:any[]=[]
    api.performManualProductionAction=async(_run,body)=>{
      bodies.push(body)
      if(bodies.length===1)throw new Error('Isolated response lost')
      inventory.episodes[1].assembly_link_id=id(18)
      inventory.episodes[1].job={id:id(19),status:'queued',attempts:0,max_attempts:2,error_message:'',updated_at:'now'}
      return {action:'assemble',result_kind:'assembly_link',target_id:id(18)}
    }
    await s.submit(id(13));await s.submit(id(13),true)
    assert.equal(s.pending.value.episodeId,id(13));assert.equal(bodies.length,1)
    await s.submit(id(3));assert.equal(bodies.length,1)
    s.setAuthor('assembly_media',undefined);assert.ok(s.authors.value.assembly_media)
    await s.submit(id(13));assert.equal(bodies.length,2);assert.equal(bodies[0],bodies[1])
    assert.deepEqual(JSON.parse(JSON.stringify(bodies[0].payload.authors)),authors)
    assert.deepEqual(bodies[0].scope,{kind:'episode',id:id(13)})
    assert.equal(s.pending.value,null);assert.equal(s.detail.value.episodes[0].job.id,id(9))
    assert.equal(s.detail.value.episodes[1].job.id,id(19));assert.equal(s.detail.value.episodes[1].job.status,'queued')
    assert.match(s.notice.value,/尚未批准交付/)
    s.checked.value=true;await s.submit(id(13));assert.equal(bodies.length,2)
  })
}))

test('author draft invalidates confirmation; failed preview preserves draft and cannot enqueue',()=>modules(async(_m,vite)=>{
  await panel(vite,async(s,api)=>{
    let calls=0
    api.performManualProductionAction=async()=>{calls++;throw new Error('must not submit')}
    s.checked.value=true;await s.submit(id(3));assert.ok(s.confirmation.value)
    s.setAuthor('assembly_output',author());assert.equal(s.confirmation.value,null);assert.equal(s.checked.value,false)
    s.checked.value=true;await s.submit(id(3));assert.equal(calls,0);assert.equal(s.confirmation.value,null)
    api.previewManualAssembly=async()=>{throw new Error('Invalid mapping')}
    await s.preview();assert.match(s.error.value,/Invalid mapping/);assert.equal(s.authors.value.assembly_output.source_text,author().source_text)
    assert.equal(s.previewReady.value,false);await s.submit(id(3),true);assert.equal(calls,0)
    api.previewManualAssembly=async(_run,_plan,draft)=>projected(draft)
    await s.preview();assert.equal(s.previewReady.value,true);assert.equal(calls,0)
    s.checked.value=true;await s.submit(id(3));assert.ok(s.confirmation.value)
    s.setAuthor('assembly_output',undefined);assert.equal(s.confirmation.value,null);assert.equal(s.previewReady.value,false)
  })
}))

test('previewed authors freeze into the retry body; uncertain result prohibits author edits',()=>modules(async(_m,vite)=>{
  await panel(vite,async(s,api)=>{
    const requests=[];api.previewManualAssembly=async(_run,_plan,draft)=>projected(draft)
    s.setAuthor('assembly_output',author());await s.preview();s.checked.value=true
    api.performManualProductionAction=async(_run,body)=>{requests.push(body);throw new Error('Response lost')}
    await s.submit(id(3));await s.submit(id(3),true)
    assert.equal(requests[0].payload.authors.assembly_output.source_text,author().source_text)
    s.setAuthor('assembly_output',{...author(),source_text:'changed'})
    assert.equal(s.authors.value.assembly_output.source_text,author().source_text)
    await s.submit(id(3));assert.equal(requests[0],requests[1])
    api.getManualAssembly=async()=>projected({assembly_output:author()},true)
    await s.reread();await s.reread(true);assert.equal(s.detail.value.frozen,true)
    s.setAuthor('assembly_output',undefined);assert.ok(s.authors.value.assembly_output)
  })
}))

test('media author joins original preview and request without turning asset identity into an adoption',()=>modules(async(m,vite)=>{
  const clip={...author(),source_text:' {"片段":{"episodes":[{"number":1,"shots":[{"number":1,"artifact_id":"00000000-0000-4000-8000-000000000099"}]}]}} '}
  await panel(vite,async(s,api)=>{
    let requests=0;api.performManualProductionAction=async()=>{requests++;throw new Error('No enqueue in this preview test')}
    api.previewManualAssembly=async(_run,_plan,draft)=>projected(draft)
    s.setAuthor('assembly_media',clip);assert.equal(s.previewReady.value,false)
    await s.preview();assert.equal(s.previewReady.value,true)
    const body=m.buildManualAssembly(s.detail.value,id(3),run,options,'isolated-media-author')
    assert.deepEqual(body.payload.authors.assembly_media,clip);assert.equal(body.action,'assemble')
    assert.equal(requests,0);s.setAuthor('assembly_media',undefined);assert.equal(s.previewReady.value,false)
  })
}))

test('late preview cannot replace another Run and missing author contracts fail explicitly',()=>modules(async(m,vite)=>{
  assert.throws(()=>m.readManualAssembly({...raw,authors:undefined},run))
  assert.throws(()=>m.readManualAssembly({...raw,authors:{assembly_output:author()}},run))
  const bad=projected({assembly_output:author()});bad.author_contracts.assembly_output.projection.valid=false
  assert.throws(()=>m.readManualAssembly(bad,run))
  await panel(vite,async(s,api,props)=>{
    let release:any
    api.previewManualAssembly=()=>new Promise(resolve=>{release=resolve})
    s.setAuthor('assembly_output',author());const pending=s.preview()
    props.run={...run,id:id(90)};api.getManualAssembly=async()=>({...structuredClone(raw),run_id:id(90)})
    await nextTick();await nextTick();release(projected({assembly_output:author()}));await pending
    assert.deepEqual(s.authors.value,{});assert.deepEqual(s.detail.value.authors,{})
  })
}))
