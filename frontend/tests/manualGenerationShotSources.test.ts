import assert from 'node:assert/strict'
import test from 'node:test'
import {createHash} from 'node:crypto'
import {fileURLToPath} from 'node:url'
import {createServer} from 'vite'
import {createRenderer,createSSRApp,h,nextTick,reactive,ssrContextKey} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {contract,id,input,options,rawContract,review,run,sha,target} from './fixtures/manualGenerationShotSources.ts'
import {mixedFixture} from './fixtures/manualGenerationMixedSources.ts'
const root=fileURLToPath(new URL('..',import.meta.url))
const settle=async()=>{for(let i=0;i<12;i++){await Promise.resolve();await nextTick()}}
async function modules(fn){const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try{await fn(vite)}finally{await vite.close()}}
function mount(C,props){let state;const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({...C,setup(p,c){state=C.setup(p,c);return()=>null}},props);app.provide(ssrContextKey,{modules:new Set()});app.mount({});return{state,app}}

test('shot-only locked upload retains its reviewed selector and upload source envelope',()=>modules(async vite=>{
  const m=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
  const reviews=await vite.ssrLoadModule('/src/services/productionManualStoryboardReview.ts')
  const C=(await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
  const fixture=mixedFixture(),contract={...fixture.contract,inputs:[fixture.contract.inputs[0]]}
  const sources=reviews.manualGenerationShotSources(reviews.readManualStoryboardReferences(fixture.review,run),id(4))
  const draft=m.emptyExecutionRequestDraft(contract)
  draft.media.pictures=[{itemKey:'picture',assetId:sources[0].assetId,reviewedShot:sources[0],purpose:null,pairedTo:null,subjectId:null}]
  const props={contract,modelValue:draft,reviewedShotSources:sources,runId:run.id,productionScope:target.scope}
  const html=await renderToString(createSSRApp({render:()=>h(C,props)}))
  assert.match(html,/本镜已审核用途<select/)
  assert.match(html,/不允许本次改选/)
  assert.doesNotMatch(html,/上传本地文件并入库|>选择已有素材</)
  const prepared=await m.prepareExecutionSelectedSources(draft)
  const result=m.buildExecutionRequestFixture(contract,prepared,{productionRunId:run.id,productionScope:target.scope})
  assert.equal(result.inputs.pictures.selection.source,'asset_upload')
  assert.equal(result.inputs.pictures.override,undefined)
  assert.deepEqual(result.inputs.pictures.selection.items[0].scope_ref,target.scope)
  prepared.mediaSourceModes.pictures='asset_library'
  assert.throws(()=>m.buildExecutionRequestFixture(contract,prepared,{productionRunId:run.id,productionScope:target.scope}),/不允许/)
}))

test('mixed editor preserves image, two videos and two uses of one audio through reorder and original-audio switching',()=>modules(async vite=>{
  const m=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
  const reviews=await vite.ssrLoadModule('/src/services/productionManualStoryboardReview.ts')
  const manual=await vite.ssrLoadModule('/src/services/productionManual.ts')
  const policy=await vite.ssrLoadModule('/src/services/inputSourcePolicies.ts')
  policy.inputSourcePolicies.options=async()=>({sources:[]})
  const C=(await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
  const fixture=mixedFixture(),contract=fixture.contract
  const sources=reviews.manualGenerationShotSources(reviews.readManualStoryboardReferences(fixture.review,run),id(4))
  const draft=reactive(m.emptyExecutionRequestDraft(contract)),mounted=mount(C,{contract,modelValue:draft,reviewedShotSources:sources,runId:run.id,productionScope:target.scope})
  const build=async(value=draft,c=contract)=>manual.buildManualGeneration({projectId:run.project,options,stageKey:'media_generation',target,contract:c,
    draft:await m.prepareExecutionSelectedSources(value),idempotencyKey:'isolated-mixed'}).payload
  try{
    for(const [key,reference] of [['pictures','picture'],['clips','video-a'],['clips','video-b'],['paired','audio-sync'],['ambient','audio-ambient']]){
      const spec=contract.inputs.find(row=>row.key===key)
      mounted.state.addMedia(spec)
      const row=draft.media[key].at(-1)
      mounted.state.setShotSource(spec,row.itemKey,`shot-use:${reference}`)
      assert.equal(row.purpose,null) // creative descriptions are not model enums
      row.purpose=spec.semantic_contract.allowed_purposes[0]??null
    }
    draft.media.paired[0].pairedTo=draft.media.clips[1].itemKey
    const first=await build(),paired=first.inputs.paired.items[0],ambient=first.inputs.ambient.items[0]
    assert.equal(paired.asset_id,ambient.asset_id);assert.notEqual(paired.item_key,ambient.item_key)
    assert.equal(paired.paired_to,first.inputs.clips.items[1].item_key)
    draft.media.clips.reverse()
    const reordered=await build()
    assert.deepEqual(reordered.inputs.clips.items,[...first.inputs.clips.items].reverse())
    assert.deepEqual(reordered.inputs.paired,first.inputs.paired)
    mounted.state.setVideoAudio('paired',draft.media.paired[0].itemKey,true)
    assert.equal(draft.media.paired[0].reviewedShot,undefined)
    const original=await build(),audio=original.inputs.paired.items[0]
    assert.equal(audio.kind,'video_audio');assert.equal(audio.paired_to,original.inputs.clips.items[0].item_key)
    assert.deepEqual(audio.scope_ref,target.scope)
    assert.ok(!('asset_id' in audio)&&!('subject_ref' in audio)&&!('shot_reference' in audio))
    assert.deepEqual(original.expected_storyboard_review,{decision_id:id(9),snapshot_sha256:sha})
    const audioFirst={...contract,inputs:[...contract.inputs].sort((a,b)=>Number(b.key==='paired')-Number(a.key==='paired'))}
    assert.deepEqual((await build(draft,audioFirst)).inputs,original.inputs)
    const bad=JSON.parse(JSON.stringify(draft));bad.media.paired[0].pairedTo='missing'
    await assert.rejects(()=>build(bad),/父输入|配对/)
    const requestOnly=structuredClone(contract);requestOnly.inputs.find(row=>row.key==='paired').semantic_contract.allowed_scope_kinds=['request']
    await assert.rejects(()=>build(draft,requestOnly),/输入范围/)
    const cross=JSON.parse(JSON.stringify(draft));cross.media.clips[0].reviewedShot.shotId=id(99)
    await assert.rejects(()=>build(cross),/当前制作镜头/)
    const wrongKind=structuredClone(contract);wrongKind.inputs.find(row=>row.key==='paired').semantic_contract.paired_with_input_key='pictures'
    const wrong=JSON.parse(JSON.stringify(draft));wrong.media.paired[0].pairedTo=wrong.media.pictures[0].itemKey
    await assert.rejects(()=>build(wrong,wrongKind),/父视频/)
    mounted.state.setShotSource(contract.inputs.find(row=>row.key==='paired'),draft.media.paired[0].itemKey,'shot-use:audio-sync')
    assert.deepEqual((await build()).inputs.paired,first.inputs.paired)
  }finally{mounted.app.unmount()}
}))

test('current original review projects exact uses, refuses stale or missing versions, never guesses model purpose',()=>modules(async vite=>{
  const m=await vite.ssrLoadModule('/src/services/productionManualStoryboardReview.ts')
  const detail=m.readManualStoryboardReferences(review,run),rows=m.manualGenerationShotSources(detail,id(4))
  assert.deepEqual(rows.map(row=>row.preview.referenceVersionId),[id(20),id(21)])
  assert.deepEqual(rows.map(row=>row.assetId),[id(6),id(6)])
  assert.equal(rows[0].creativePurpose,'外观参考，不是首帧')
  for(const status of ['not_reviewed','changed'])assert.throws(()=>m.manualGenerationShotSources({...detail,approval:{...detail.approval,status}},id(4)),/尚未审核或已变化/)
  assert.throws(()=>m.manualGenerationShotSources(detail,id(99)),/审核范围/)
  const bad=structuredClone(review);bad.contract.canonical.episodes[0].shots[0].reference_uses[0].reference_version_id='missing'
  assert.throws(()=>m.readManualStoryboardReferences(bad,run),/不一致/)
}))

test('real editor and builder retain two use identities for one file and pin the original review without caller provenance',()=>modules(async vite=>{
  const m=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
  assert.equal(m.executionRequestFormContract(rawContract).versionId,contract.versionId)
  const reviews=await vite.ssrLoadModule('/src/services/productionManualStoryboardReview.ts')
  const manual=await vite.ssrLoadModule('/src/services/productionManual.ts')
  const C=(await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
  const rows=reviews.manualGenerationShotSources(reviews.readManualStoryboardReferences(review,run),id(4)), draft=reactive(m.emptyExecutionRequestDraft(contract))
  const props={contract,modelValue:draft,managedInputKeys:[],reviewedShotSources:rows,runId:run.id,productionScope:target.scope}
  const mounted=mount(C,props)
  try{
    mounted.state.addMedia(input);mounted.state.addMedia(input)
    rows.forEach((row,index)=>mounted.state.setShotSource(input,draft.media.pictures[index].itemKey,row.referenceKey))
    assert.deepEqual(draft.media.pictures.map(row=>row.purpose),[null,null])
    const original=JSON.stringify(draft),prepared=await m.prepareExecutionSelectedSources(draft)
    assert.equal(JSON.stringify(draft),original)
    const payload=manual.buildManualGeneration({projectId:run.project,options,stageKey:'media_generation',target,contract,draft:prepared,idempotencyKey:'isolated'}).payload
    assert.deepEqual(payload.expected_storyboard_review,{decision_id:id(9),snapshot_sha256:sha})
    const actual=payload.inputs.pictures.items
    assert.equal(new Set(actual.map(row=>row.item_key)).size,2)
    rows.forEach((row,index)=>assert.equal(actual[index].item_key,'reviewed:'+createHash('sha256').update(JSON.stringify({input_key:'pictures',reference_key:row.referenceKey,shot_id:id(4)})).digest('hex')))
    assert.ok(actual.every(row=>row.asset_id===id(6)&&row.scope_ref.id===id(4)&&row.purpose===null&&!('shot_reference' in row)))
    assert.throws(()=>m.buildExecutionRequestFixture(contract,prepared),/当前制作镜头/)
    assert.throws(()=>manual.buildManualGeneration({projectId:run.project,options,stageKey:'media_generation',target:{scope:{kind:'shot',id:id(99)},unitKey:`shot:${id(99)}`},contract,draft:prepared,idempotencyKey:'wrong'}),/当前制作镜头/)
    const html=await renderToString(createSSRApp({render:()=>h(C,props)}))
    assert.match(html,/本镜已审核用途/);assert.match(html,/原创作用途/);assert.match(html,/查看确切素材/)
    assert.doesNotMatch(html,/代表哪个本次主体/)
    mounted.state.setShotSource(input,draft.media.pictures[0].itemKey,'')
    assert.equal(draft.media.pictures[0].reviewedShot,undefined);assert.equal(draft.media.pictures[0].assetId,'')
  }finally{mounted.app.unmount()}
}))

test('formal form reads only on demand, invalidates old catalog, and submits no stale or cross-shot selection',()=>modules(async vite=>{
  const {platformApi:api}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const {executionRegistryApi:registry}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const m=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
  const C=(await vite.ssrLoadModule('/src/components/ManualGenerationPreparation.vue')).default
  let reads=0,writes=0,body,fail=false,release
  registry.versions=async()=>({results:[],next:null})
  api.listProductionTargets=async()=>({runId:run.id,runPlanSha256:sha,results:[target],count:1,next:null})
  api.getManualStoryboardReview=async()=>{reads++;if(fail)throw Error('isolated read failure');return structuredClone(review)}
  api.performManualProductionAction=async(_id,value)=>{writes++;body=value;throw {response:{status:400,data:{error:{message:'isolated rejection'}}}}}
  const mounted=mount(C,{run,options,initialStageKey:'media_generation'})
  try{
    const s=mounted.state;await settle();assert.equal(reads,0)
    s.contract.value=contract;s.draft.value=m.emptyExecutionRequestDraft(contract)
    await s.loadShotSources();assert.equal(reads,1);assert.equal(s.reviewedShotSources.value.length,2)
    const source=s.reviewedShotSources.value[0]
    s.draft.value.media.pictures=[{itemKey:'chosen',assetId:source.assetId,purpose:null,pairedTo:null,subjectId:null,reviewedShot:source}]
    await s.create();assert.equal(writes,1);assert.deepEqual(body.payload.expected_storyboard_review,source.review)
    fail=true;await s.loadShotSources();assert.equal(s.reviewedShotSources.value.length,0)
    await s.create();assert.equal(writes,1);assert.match(s.error.value,/目录已失效/)
    api.getManualStoryboardReview=()=>new Promise(resolve=>{release=resolve})
    const pending=s.loadShotSources();s.targetKey.value='';await settle();release(structuredClone(review));await pending
    assert.equal(s.reviewedShotSources.value.length,0);assert.equal(s.shotSourceLoading.value,false)
  }finally{mounted.app.unmount()}
}))
