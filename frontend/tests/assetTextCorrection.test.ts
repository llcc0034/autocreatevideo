import assert from 'node:assert/strict'
import test from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer} from 'vite'
import {createRenderer,h,nextTick,reactive,ssrContextKey} from 'vue'
import {assetTextCorrectionData} from './fixtures/asset-text-correction-data.ts'

const root=fileURLToPath(new URL('..',import.meta.url))
test('asset response owner and complete human provenance are mandatory and exact',async()=>{
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try{
    const {adaptExecutionPreparationDetail:adapt}=await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const data=assetTextCorrectionData()
    assert.equal(adapt(data).textResponseReview.assetJobId,'fixture-job')
    assert.equal(adapt(data).textResponseReview.attemptId,null)
    for(const change of [{attempt_id:'fake-attempt'},{asset_job_id:null},{asset_job_id:'foreign'},
      {corrected_text:'fake'},{confirmed_at:'2026-10-02T01:02:00Z'},{asset_job_id:undefined}]){
      assert.throws(()=>adapt({...data,text_response_review:{...data.text_response_review,...change}}))
    }
    const corrected={...data.text_response_review,editable:false,confirmed_at:'2026-10-02T01:02:00Z',confirmed_by:'1',corrected_text:'人工',corrected_sha256:'d'.repeat(64)}
    assert.equal(adapt({...data,text_response_review:corrected}).textResponseReview.correctedText,'人工')
    assert.throws(()=>adapt({...data,text_response_review:{...corrected,editable:true}}))
  }finally{await vite.close()}
})

test('shared editor requires confirmation, suppresses duplicates, retains errors and rejects mismatched/late receipts',async()=>{
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  let app:any
  try{
    const {adaptExecutionPreparationDetail:adapt}=await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const {executionPreparationApi:api}=await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel=(await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')).default
    let data=adapt(assetTextCorrectionData()),writes=0,state:any,resolve:any
    api.get=async()=>structuredClone(data)
    api.continue=async()=>{throw Error('must never invoke')}
    api.completeTextResponse=async(...args:any[])=>{
      writes++;assert.deepEqual(args,['fixture-prep',3,'fixture-review','c'.repeat(64),'人工正文'])
      return new Promise(r=>resolve=r)
    }
    const props=reactive({preparationId:'fixture-prep',versionId:'fixture-version',purpose:'production'})
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({setup(){state=Panel.setup(props,{emit(){},expose(){}});return()=>h('div')}})
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick()
    state.recoveryText.value='人工正文'
    await state.completeResponse();assert.equal(writes,0)
    state.recoveryAcknowledged.value=true
    const pending=state.completeResponse();await state.completeResponse();assert.equal(writes,1)
    resolve({...data,revision:4,textResponseReview:{...data.textResponseReview,editable:false,confirmedAt:'2026-10-02T01:02:00Z',confirmedBy:'1',correctedText:'人工正文',correctedSha256:'d'.repeat(64)}})
    await pending
    assert.equal(state.recoveryText.value,'');assert.match(state.notice.value,/本任务结果.*未调用模型/)
    assert.equal(state.preparation.value.textResponseReview.originalText,data.textResponseReview.originalText)
    state.preparation.value=structuredClone(data);state.recoveryText.value='人工正文';state.recoveryAcknowledged.value=true
    api.completeTextResponse=async()=>{throw Error('隔离拒绝保存')}
    await state.completeResponse();assert.equal(state.recoveryText.value,'人工正文');assert.match(state.error.value,/拒绝/)
    api.completeTextResponse=async()=>({...data,textResponseReview:{...data.textResponseReview,confirmedAt:'yes',correctedText:'错误正文'}})
    await state.completeResponse();assert.equal(state.needsRefresh.value,true);assert.equal(state.recoveryText.value,'人工正文');assert.match(state.error.value,/回执/)
    state.needsRefresh.value=false
    api.completeTextResponse=async()=>new Promise(r=>resolve=r)
    const late=state.completeResponse();props.preparationId='other';data={...data,id:'other'};await nextTick()
    resolve({...data,id:'fixture-prep'});await late
    assert.notEqual(state.preparation.value?.id,'fixture-prep')
  }finally{app?.unmount();await vite.close()}
})
