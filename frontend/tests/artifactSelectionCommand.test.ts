import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
const root = fileURLToPath(new URL('..', import.meta.url))
const receipt = () => ({id: crypto.randomUUID(), action: 'select', result_kind: 'artifact_selection', target_id: crypto.randomUUID(), created_at: new Date().toISOString()})
async function setup(fn) {
  const vite = await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {await fn(await vite.ssrLoadModule('/src/services/artifactSelectionCommand.ts'), vite)} finally {await vite.close()}
}
test('unknown reply freezes key and exact null/impact body; double submit and replay cannot form a new intent',()=>setup(async m=>{
  let reject:any, reply=receipt(); const calls:any[]=[], protections:any[]=[], confirmed:any[]=[]
  const api=async(...args)=>{calls.push(structuredClone(args)); if(calls.length===1)await new Promise((_,r)=>reject=r); return reply}
  const c=m.createArtifactSelectionCommand(api,()=>({runId:'run',scopeId:'group'}),v=>protections.push(v),v=>confirmed.push(v))
  const body={artifact_id:'A',material_selection:{expected_decision_id:null,expected_dependencies_sha256:'a'.repeat(64)}}
  const first=c.submit(body);await c.submit({artifact_id:'B'});assert.equal(calls.length,1)
  body.material_selection.expected_dependencies_sha256='changed';reject(Error('reply lost'));await first
  assert.ok(c.state.pending);await c.submit({artifact_id:'B'});assert.deepEqual(calls[0],calls[1]);assert.equal(c.state.pending,null)
  assert.deepEqual(confirmed[0].receipt,reply);assert.equal(protections.at(-1).active,false)
  await c.submit({artifact_id:'A'});assert.notEqual(calls[0][2],calls[2][2])
}))
test('after uncertainty, rejection or malformed receipt preserves the original key; first explicit rejection can release',()=>setup(async m=>{
  for(const failure of [400,403,409,'malformed']){
    let phase=0;const c=m.createArtifactSelectionCommand(async()=>{if(!phase++)throw Error('lost');if(failure==='malformed')return {};throw {response:{status:failure}}},()=>({runId:'run',scopeId:'group'}),()=>{},()=>assert.fail())
    await c.submit({artifact_id:'A'});const key=c.state.pending.key;await c.submit();assert.equal(c.state.pending.key,key)
  }
  const c=m.createArtifactSelectionCommand(async()=>{throw {response:{status:409}}},()=>({runId:'run',scopeId:'group'}),()=>{},()=>assert.fail())
  await c.submit({artifact_id:'A'});assert.equal(c.state.pending,null)
}))
test('reset invalidates old success/failure/finally without releasing the next context',()=>setup(async m=>{
  for(const fails of [false,true]){
    let finish:any,ctx={runId:'old',scopeId:'old'},calls=0;const events:any[]=[]
    const c=m.createArtifactSelectionCommand(async()=>{if(!calls++)return new Promise((yes,no)=>finish=fails?no:yes);throw Error('new lost')},()=>ctx,v=>events.push(v),()=>assert.fail('old receipt must be ignored'))
    const old=c.submit({artifact_id:'A'});ctx={runId:'new',scopeId:'new'};c.reset();await c.submit({artifact_id:'B'})
    const key=c.state.pending.key, count=events.length;finish(fails?Error('old'):receipt());await old
    assert.equal(c.state.pending.key,key);assert.equal(events.length,count)
  }
}))
test('receipt validation is strict and target is a decision, not the candidate',()=>setup(async m=>{
  const r=receipt();assert.deepEqual(m.readArtifactSelectionReceipt(r),r)
  for(const changed of [{id:''},{target_id:'A'},{created_at:'bad'},{action:'approve'},{result_kind:'artifact'}])assert.throws(()=>m.readArtifactSelectionReceipt({...r,...changed}),/回执/)
}))
test('public transport uses stable header and original body, returns receipt without Artifact adaptation',()=>setup(async(_,vite)=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),{api}=await vite.ssrLoadModule('/src/services/api.ts')
  const r=receipt(),body={artifact_id:'A',material_selection:{expected_decision_id:null,expected_dependencies_sha256:'a'.repeat(64)}}
  api.post=async(...args)=>{assert.deepEqual(args,['/production-runs/run/select-artifact/',body,{headers:{'Idempotency-Key':'stable'}}]);return{data:r}}
  assert.deepEqual(await platformApi.selectArtifact('run',body,'stable'),r)
}))
