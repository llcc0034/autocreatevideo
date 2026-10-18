import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const root = fileURLToPath(new URL('..', import.meta.url))
const option = { execution_instance_version_id:'version', key:'one', name:'执行组合', version:2, operation:'image.text_to_image', mode:'', prompt_mode:'manual', definition_sha256:'a'.repeat(64) }
const prep = { id:'prep', stage_run:null, asset_job:'job', deployment_smoke:null, workflow_smoke:null, execution_instance_version:'version', purpose:'production', unit_key:'asset', request_key:'request', revision:1, status:'waiting_fields', intent_sha256:'b'.repeat(64), request_sha256:null, request_frozen_at:null, next_check_at:null, reason_code:'fields_required', reason:'待补充字段', evaluation_revision:1, evaluated_at:'2026-09-08T00:00:00Z', created_by:1, created_at:'2026-09-08T00:00:00Z', updated_at:'2026-09-08T00:00:00Z' }
const job = { id:'job', retry_of:null, retries:[], execution_instance_version:'version', execution_instance_name:'执行组合', execution_version:2, preparation:prep,
  job_type:'asset', media_type:'image', image_category:'reference', name:'测试产物', description:'', style:null,
  execution_kind:'deployment', model_deployment:'resource', model_deployment_name:'执行资源',
  workflow_template_version:null, workflow_name:null, workflow_version:null, output_asset:null, status:'preparing', operation:option.operation,
  is_external:true, allow_external_calls:false, budget_limit:null, estimated_cost:null, actual_cost:null, cost_currency:'',
  result_text:'', remote_status:'', provider_request_id_sha256:'', poll_count:0, error_code:'', error_message:'', ignore_late_result:false,
  submission_started_at:null, cancel_requested_at:null, cancel_submitted_at:null, created_at:prep.created_at, updated_at:prep.updated_at, submitted_at:null, finished_at:null }

async function withModules(fn) {
  const vite = await createServer({ root, envFile:false, logLevel:'silent', appType:'custom', server:{ middlewareMode:true, hmr:false, ws:false, watch:null } })
  try { await fn(await vite.ssrLoadModule('/src/services/assetGenerationAdapters.ts'), vite) } finally { await vite.close() }
}

test('maintenance fixture exposes the same exact media contract to author and registry path',()=>withModules(async(_,vite)=>{
  const {generationOptions,generationMaintenanceContract}=await vite.ssrLoadModule('/tests/fixtures/assetGenerationScenarios.ts')
  const {executionRequestFormContract}=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
  const {executionPath}=await vite.ssrLoadModule('/src/services/executionPath.ts')
  const option=generationOptions.find(row=>row.kind==='video'&&row.promptMode==='manual')
  const raw=generationMaintenanceContract(option)
  const author=executionRequestFormContract(raw)
  const path=executionPath({id:option.versionId,revision:1,status:'published'},raw)
  assert.equal(author.versionId,option.versionId)
  assert.equal(author.inputs[0].key,'pictures')
  assert.equal(author.inputs[0].min_items,1)
  assert.equal(path.inputs[0].key,'pictures')
  assert.equal(path.workflowId,author.workflowVersionId)
  assert.equal(path.outputKind,author.outputKind)
  assert.equal(author.outputKind,'video')
  assert.equal(author.promptMode,'manual')
}))

test('standalone text candidates never open an invented text Asset category',()=>withModules(async(_,vite)=>{
  const {assetPromptContinuationTarget:target}=await vite.ssrLoadModule('/src/services/assetGenerationPayload.ts')
  const completed={id:'job',jobType:'prompt',mediaType:'text',status:'succeeded',resultText:'Translated content'}
  assert.equal(target(completed),null)
  for(const mediaType of ['image','video','audio','script']) {
    assert.deepEqual(target({...completed,mediaType}),{name:'assets',query:{type:mediaType,promptJob:'job'}})
    assert.equal(target({...completed,mediaType,status:'failed'}),null)
    assert.equal(target({...completed,mediaType,jobType:'field'}),null)
    assert.equal(target({...completed,mediaType,resultText:''}),null)
  }
}))
test('asset adapter preserves preparing, absent budget and precise composition and rejects missing facts', () => withModules(m => {
  const row = m.adaptAssetGenerationJob(job, () => assert.fail('No output was recorded'), value => value)
  assert.equal(row.status,'preparing'); assert.equal(row.actualCost,null); assert.equal(row.budgetLimit,null)
  assert.equal(row.executionInstanceVersionId,'version'); assert.equal(row.executionVersion,2)
  assert.equal(row.preparation.reason,'待补充字段'); assert.equal(row.submittedAt,'')
  assert.equal(Object.hasOwn(row,'prompt'),false)
  for (const key of ['status','preparation','execution_instance_version','allow_external_calls','output_asset','retry_of','retries']) {
    const missing = { ...job }; delete missing[key]
    assert.throws(() => m.adaptAssetGenerationJob(missing, () => assert.fail('No output'), value=>value), /不完整|不一致/)
  }
  assert.throws(() => m.adaptAssetGenerationJob({ ...job, preparation:{ ...prep,asset_job:'other' } },()=>null,value=>value), /preparation/)
  const chained = m.adaptAssetGenerationJob({ ...job, retry_of:'ancestor', retries:[{id:'successor',status:'succeeded'}] },()=>null,value=>value)
  assert.equal(chained.retryOf, 'ancestor'); assert.deepEqual(chained.retries,[{id:'successor',status:'succeeded'}])
  for (const retries of [null,[{id:'job',status:'failed'}],[{id:'child',status:'unknown'}],[{id:'child',status:'failed'},{id:'child',status:'failed'}]]) {
    assert.throws(() => m.adaptAssetGenerationJob({ ...job,retries },()=>null,value=>value), /不完整|不一致/)
  }
  assert.throws(() => m.adaptAssetGenerationOptions({ routes:[] }), /results/)
  assert.throws(() => m.adaptAssetGenerationOptions({ count:1,next:null,previous:null,results:[{ ...option,definition_sha256:'' }] }), /definition_sha256/)
}))
test('asset option pagination and creation use only version identity and the explicit frozen fixture', () => withModules(async (_m,vite) => {
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const { repository } = await vite.ssrLoadModule('/src/services/repository.ts')
  const calls = []
  api.get = async (...args) => { calls.push(args); return { data:{ count:48,next:'?page=3',previous:'?page=1',results:[option] } } }
  const options = await repository.getAssetGenerationOptions('asset','image',2)
  assert.equal(options.count,48); assert.equal(options.next,'?page=3')
  assert.equal(options.results[0].versionId,'version')
  assert.deepEqual(calls[0],['/asset-generation-jobs/options/',{ params:{ job_type:'asset',media_type:'image',page:2 } }])
  api.post = async (...args) => { calls.push(args); return { data:job } }
  const fixture = { inputs:{},parameters:{scale:0},manual_channels:{positive_prompt:'原文'},subject_facts:[],source_language:'',target_language:'' }
  const input = { jobType:'asset',mediaType:'image',executionInstanceVersionId:'version',expectedDefinitionSha256:option.definition_sha256,
    fixture,name:'测试产物',description:'',imageCategory:'reference',allowExternalCalls:false,budgetLimit:null,idempotencyKey:'stable' }
  const before = JSON.stringify(input)
  await repository.createAssetGenerationJob(input); await repository.createAssetGenerationJob(input)
  assert.deepEqual(calls[1],calls[2]); assert.equal(JSON.stringify(input),before)
  assert.deepEqual(calls[1][2],{ headers:{ 'Idempotency-Key':'stable' } })
  assert.equal(calls[1][1].execution_instance_version_id,'version')
  assert.equal(calls[1][1].expected_definition_sha256,option.definition_sha256)
  assert.deepEqual(calls[1][1].manual_channels,fixture.manual_channels)
  assert.equal(Object.hasOwn(calls[1][1],'model_deployment_id'),false)
  assert.equal(Object.hasOwn(calls[1][1],'workflow_template_version_id'),false)
  assert.equal(Object.hasOwn(calls[1][1],'appearance_target'),false)
  const appearanceTarget = {role:'primary',setting_revision_id:'10000000-0000-0000-0000-000000000001',primary_version_id:null,primary_input:null}
  await repository.createAssetGenerationJob({...input,appearanceTarget})
  assert.deepEqual(calls[3][1].appearance_target,appearanceTarget)
  assert.notEqual(calls[3][1].appearance_target,appearanceTarget)
}))

test('asset retry keeps the same source, empty payload and idempotency key after a lost reply', () => withModules(async (_m,vite) => {
  const {api}=await vite.ssrLoadModule('/src/services/api.ts')
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
  const calls:any[]=[]
  api.post=async (...args:any[])=>{calls.push(args);if(calls.length===1)throw Error('isolated lost reply');return {data:job}}
  await assert.rejects(repository.retryAssetGenerationJob('original-job'),/isolated lost reply/)
  await repository.retryAssetGenerationJob('original-job')
  assert.deepEqual(calls,[
    ['/asset-generation-jobs/original-job/retry/',{},{headers:{'Idempotency-Key':'asset-generation-retry:original-job'}}],
    ['/asset-generation-jobs/original-job/retry/',{},{headers:{'Idempotency-Key':'asset-generation-retry:original-job'}}],
  ])
}))
