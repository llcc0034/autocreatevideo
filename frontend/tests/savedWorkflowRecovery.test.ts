import test from 'node:test'
import assert from 'node:assert/strict'
import {readSavedWorkflowPending,changeSavedWorkflowPending,savedWorkflowPendingCovers,adaptSavedWorkflowSource,assertSavedWorkflowPreviewReceipt,savedWorkflowSourceMatches,savedWorkflowVersionMatches,useSavedWorkflowRecovery} from '../src/services/savedWorkflowRecovery.ts'
import {recoveredSource,recoveredVersion,recoveredProof,previewReceipt} from './fixtures/workflow-import-data.ts'
import type {ComfySavedWorkflowBatchItemInput} from '../src/services/platformTypes.ts'
const input:ComfySavedWorkflowBatchItemInput={sourcePath:'workflows/video.json',expectedSha256:'1'.repeat(64),expectedSourceScopeSchemaVersion:1,expectedSourceScopeSha256:'2'.repeat(64),importKind:'template_version',contractDraft:'draft-original',successorOf:'version-original',modelProfileVersion:'model-original',template:'template-original',primaryOutputNodeIds:['81'],expectedObjectInfoSchemaSha256:'3'.repeat(64),expectedConvertedGraphSha256:'4'.repeat(64),expectedStructuralContractSha256:'5'.repeat(64),expectedParameterSchemaSha256:'6'.repeat(64)}
const intent={id:'intent',deploymentId:'deployment',startedAt:'2026-10-09T00:00:00Z',input}
function storage(){const map=new Map<string,string>();return{map,getItem:(k:string)=>map.get(k)??null,setItem:(k:string,v:string)=>map.set(k,v),removeItem:(k:string)=>map.delete(k)}}
const defer=()=>{let resolve!:(v:any)=>void;const promise=new Promise<any>(r=>resolve=r);return{promise,resolve}}
function context(){const store=storage();let user='user',scope='scope',active=true;const recovered:any[]=[];const reader={list:async()=>[await recoveredProof('deployment',input)],version:async()=>recoveredVersion(input)}
  let lock=Promise.resolve();const withLock=async<T>(_key:string,work:()=>T|Promise<T>)=>{const result=lock.then(work);lock=result.then(()=>{},()=>{});return result}
  const options={storage:()=>store,userId:()=>user,scope:()=>scope,active:()=>active,list:(...args:any[])=>reader.list(),version:()=>reader.version(),recovered:(...args:any[])=>recovered.push(args),withLock}
  return{store,reader,recovered,options,recovery:useSavedWorkflowRecovery(options),user:(v:string)=>user=v,scope:(v:string)=>scope=v,active:(v:boolean)=>active=v}
}
test('pending import persists exact original intent across refresh and isolates accounts',async()=>{
  const c=context();await c.recovery.begin('deployment',[input]);const reload=useSavedWorkflowRecovery(c.options);reload.reload()
  assert.deepEqual(reload.pending.value[0]!.input,input);assert.equal(reload.pending.value.length,1);assert.deepEqual(readSavedWorkflowPending(c.store,'other'),[])
  assert.equal(savedWorkflowPendingCovers(reload.pending.value[0]!,1,input.sourcePath,input.expectedSourceScopeSha256),true)
  await assert.rejects(()=>reload.begin('different-transport',[{...input,expectedSha256:'9'.repeat(64),successorOf:'new-predecessor'}]),/未确认/)
})
test('unreadable or malformed storage cannot disappear into an empty success state',async()=>{
  const c=context();await c.recovery.begin('deployment',[input]);const k=[...c.store.map.keys()][0]!;c.store.map.set(k,'broken')
  assert.throws(()=>readSavedWorkflowPending(c.store,'user'),/无法读取/);c.recovery.reload();assert.ok(c.recovery.error.value);assert.equal(c.recovery.covers(1,'path','hash'),true)
  const denied={getItem(){throw Error('denied')},setItem(){},removeItem(){}};assert.throws(()=>changeSavedWorkflowPending(denied,'user',()=>[]),/无法读取/)
})
test('source recovery rejects every different identity or frozen contract coordinate',()=>{
  const exact=recoveredSource('deployment',input);assert.equal(savedWorkflowSourceMatches(intent,exact),true)
  assert.equal(savedWorkflowSourceMatches(intent,{...exact,deploymentId:'another-transport'}),true)
  for(const field of ['scopeVersion','scopeSha256','path','sha256','kind','versionId'])assert.equal(savedWorkflowSourceMatches(intent,{...exact,[field]:field==='versionId'?'':'wrong'} as any),false,field)
  for(const field of ['promotion_source','contract_draft_id','successor_of','structural_contract_sha256','source_converted_graph_sha256','object_info_schema_sha256','primary_output_node_id'])assert.equal(savedWorkflowSourceMatches(intent,{...exact,report:{...exact.report,[field]:'wrong'}}),false,field)
  assert.equal(savedWorkflowSourceMatches({...intent,input:{...input,successorOf:null}},exact),false)
})
test('destination readback pins exact model, template and returned version, never highest name match',()=>{
  const source=recoveredSource('deployment',input),version=recoveredVersion(input);assert.ok(savedWorkflowVersionMatches(intent,source,version))
  for(const field of ['id','template','modelProfileVersion'])assert.equal(savedWorkflowVersionMatches(intent,source,{...version,[field]:'other'}),false)
  const draft={...input,template:null,newTemplate:{name:'New',slug:'new',outputKind:'video' as const}},target={...intent,input:draft},v=recoveredVersion(draft)
  assert.ok(savedWorkflowVersionMatches(target,source,v));for(const field of ['templateName','templateSlug','outputKind'])assert.equal(savedWorkflowVersionMatches(target,source,{...v,[field]:'other'}),false)
})
test('readback resolves one exact persisted source without replay and exposes actual version',async()=>{
  const c=context();await c.recovery.begin('deployment',[input]);await c.recovery.readback();assert.equal(c.recovered.length,1);assert.equal(c.recovery.pending.value.length,0)
  assert.equal(readSavedWorkflowPending(c.store,'user').length,0);assert.equal(c.recovery.results.value[0]!.versionId,'fixture-saved-version')
})
test('empty, ambiguous, stale version and reader errors all retain unresolved intents',async()=>{
  for(const mode of ['empty','ambiguous','version','error','parameter']){const c=context();await c.recovery.begin('deployment',[input]);
    if(mode==='empty')c.reader.list=async()=>[];if(mode==='ambiguous')c.reader.list=async()=>[recoveredSource('deployment',input),recoveredSource('deployment',input)]
    if(mode==='version')c.reader.version=async()=>({...recoveredVersion(input),modelProfileVersion:'other'});if(mode==='error')c.reader.list=async()=>{throw Error('503')}
    if(mode==='parameter')c.reader.list=async()=>[await recoveredProof('deployment',{...input,expectedParameterSchemaSha256:'9'.repeat(64)})]
    await c.recovery.readback();assert.equal(c.recovery.pending.value.length,1,mode);assert.equal(c.recovered.length,0);assert.equal(c.recovery.results.value[0]!.versionId,'')
  }
})
test('partial readback only resolves exact rows; unsubmitted sources remain available',async()=>{
  const c=context();await c.recovery.begin('deployment',[input,{...input,sourcePath:'workflows/other.json'}]);await c.recovery.readback()
  assert.equal(c.recovery.pending.value.length,1);assert.equal(c.recovery.pending.value[0]!.input.sourcePath,'workflows/other.json');assert.equal(c.recovered.length,1)
  assert.equal(c.recovery.covers(1,'workflows/unsubmitted.json',input.expectedSourceScopeSha256),false)
})
test('late readback after account, view or unmount never clears the old unresolved request',async()=>{
  for(const kind of ['account','view','unmount']){const c=context(),d=defer();await c.recovery.begin('deployment',[input]);c.reader.list=()=>d.promise
    const pending=c.recovery.readback();if(kind==='account')c.user('other');if(kind==='view')c.scope('other');if(kind==='unmount')c.active(false)
    d.resolve([recoveredSource('deployment',input)]);await pending;assert.equal(readSavedWorkflowPending(c.store,'user').length,1);assert.equal(c.recovered.length,0);assert.equal(c.recovery.busy.value,false)
  }
})
test('an obsolete readback cannot release the busy state of a newer readback',async()=>{
  const c=context(),old=defer(),next=defer();await c.recovery.begin('deployment',[input]);c.reader.list=()=>old.promise
  const oldRequest=c.recovery.readback();c.recovery.reload();c.reader.list=()=>next.promise;const nextRequest=c.recovery.readback()
  old.resolve([]);await oldRequest;assert.equal(c.recovery.busy.value,true)
  next.resolve([]);await nextRequest;assert.equal(c.recovery.busy.value,false);assert.equal(c.recovery.pending.value.length,1)
})
test('confirmed late receipt clears only its original account ticket',async()=>{
  const c=context(),ticket=await c.recovery.begin('deployment',[input]);c.user('other');c.recovery.reload();await c.recovery.begin('deployment',[input]);await c.recovery.settle(ticket)
  assert.equal(readSavedWorkflowPending(c.store,'user').length,0);assert.equal(readSavedWorkflowPending(c.store,'other').length,1);assert.equal(c.recovery.pending.value.length,1)
})
test('cross-tab lock serializes duplicate begin and receipt removal without losing other intents',async()=>{
  const c=context(),other=useSavedWorkflowRecovery(c.options)
  const results=await Promise.allSettled([c.recovery.begin('transport-A',[input]),other.begin('transport-B',[input])])
  assert.equal(results.filter(row=>row.status==='fulfilled').length,1);assert.equal(readSavedWorkflowPending(c.store,'user').length,1)
  const ticket=(results.find(row=>row.status==='fulfilled') as PromiseFulfilledResult<any>).value
  await Promise.all([c.recovery.settle(ticket),other.begin('transport-B',[{...input,sourcePath:'workflows/new.json'}])])
  assert.equal(readSavedWorkflowPending(c.store,'user').length,1);assert.equal(readSavedWorkflowPending(c.store,'user')[0]!.input.sourcePath,'workflows/new.json')
})
test('database source adapter validates identity and never substitutes missing report or workflow',()=>{
  const raw={id:'source',source_deployment:'deployment',source_scope_schema_version:1,source_scope_sha256:input.expectedSourceScopeSha256,source_path:input.sourcePath,source_sha256:input.expectedSha256,import_kind:'template_version',workflow_version:null}
  const parsed=adaptSavedWorkflowSource(raw);assert.equal(savedWorkflowSourceMatches(intent,parsed),false);assert.throws(()=>adaptSavedWorkflowSource({...raw,source_sha256:''}),/缺少精确身份/)
})

test('contract pending shares source lock with import across refresh, accounts and changed transports',async()=>{
  const c=context(),request={...input,contractDraft:undefined,expectedStructuralContractSha256:undefined,expectedParameterSchemaSha256:undefined}
  await c.recovery.begin('deployment',[request],{candidateSourceId:'candidate'})
  const other=useSavedWorkflowRecovery(c.options);other.reload()
  assert.equal(other.pending.value[0]!.contractRequest?.candidateSourceId,'candidate')
  await assert.rejects(()=>other.begin('other-deployment',[input]),/未确认/)
  assert.equal(readSavedWorkflowPending(c.store,'other').length,0)
  const key=[...c.store.map.keys()][0]!;c.store.map.set(key,JSON.stringify({schemaVersion:1,userId:'user',items:[{...intent,contractRequest:{candidateSourceId:''}}]}))
  assert.throws(()=>readSavedWorkflowPending(c.store,'user'),/无法读取/)
})

test('contract receipt restores only exact request and never treats missing or incomplete proof as success',async()=>{
  for(const kind of ['missing','mismatch','found','incomplete','error','changed-form']){
    const c=context();let applied=0
    const recovery=useSavedWorkflowRecovery({...c.options,contractReceipt:async(deploymentId,key)=>{
      if(kind==='error'||kind==='missing')throw Error(kind==='missing'?'尚未查到原请求回执，不代表未保存':'503')
      const result=previewReceipt(deploymentId,{...input,requestKey:key},'candidate')
      if(kind==='mismatch')result.requestReceipt!.requestKey='other'
      if(kind==='incomplete')result.suggestedInputContract={}
      return result
    },recoveredContract:()=>{applied++;return kind!=='changed-form'}})
    await recovery.begin('deployment',[input],{candidateSourceId:'candidate'});await recovery.readback()
    assert.equal(recovery.pending.value.length,kind==='found'?0:1,kind);assert.equal(c.recovered.length,0)
    assert.equal(applied,['found','changed-form'].includes(kind)?1:0)
    assert.equal(Boolean(recovery.results.value[0]!.preview),['found','changed-form'].includes(kind))
  }
})

test('contract receipt pins all request coordinates and preserves candidate-only null model',()=>{
  const pending={...intent,contractRequest:{candidateSourceId:'candidate'}},exact=previewReceipt('deployment',{...input,requestKey:intent.id},'candidate')
  assert.doesNotThrow(()=>assertSavedWorkflowPreviewReceipt(pending,exact))
  for(const field of ['deploymentId','sourcePath','expectedSha256','expectedSourceScopeSchemaVersion','expectedSourceScopeSha256','primaryOutputNodeIds','modelProfileVersion']){
    const broken=structuredClone(exact);(broken.requestReceipt!.request as any)[field]=field==='primaryOutputNodeIds'?['wrong']:'wrong'
    assert.throws(()=>assertSavedWorkflowPreviewReceipt(pending,broken),/不一致/,field)
  }
  const candidate=previewReceipt('deployment',{...input,requestKey:intent.id,modelProfileVersion:null},'candidate');candidate.actionability='candidate_only';candidate.suggestedInputContract={}
  assert.doesNotThrow(()=>assertSavedWorkflowPreviewReceipt({...pending,input:{...input,modelProfileVersion:null}},candidate))
})

test('late contract receipt leaves original account and changed view pending',async()=>{
  for(const kind of ['account','view','unmount']){
    const c=context(),d=defer();let applied=0
    const r=useSavedWorkflowRecovery({...c.options,contractReceipt:()=>d.promise,recoveredContract:()=>{applied++;return true}})
    const ticket=await r.begin('deployment',[input],{candidateSourceId:'candidate'}),reading=r.readback()
    if(kind==='account')c.user('other');if(kind==='view')c.scope('other');if(kind==='unmount')c.active(false)
    d.resolve(previewReceipt('deployment',{...input,requestKey:ticket.ids[0]},'candidate'));await reading
    assert.equal(readSavedWorkflowPending(c.store,'user').length,1);assert.equal(applied,0)
  }
})
