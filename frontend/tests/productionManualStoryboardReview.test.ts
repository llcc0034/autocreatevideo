import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import { renderToString } from 'vue/server-renderer'
import {displayFixture} from './storyboardReviewDisplayFixture.ts'

const root=fileURLToPath(new URL('..',import.meta.url)),sha='a'.repeat(64)
const id=(n:number)=>`10000000-0000-0000-0000-${String(n).padStart(12,'0')}`
const run={id:id(1),project:id(2),planSource:'manual',status:'awaiting_input',executionPlan:{sha256:sha}}
const options={runId:run.id,runPlanSha256:sha,actions:[{stageKey:'storyboard_review',action:'approve'}]}
const ready={display:displayFixture(id(3),[id(4),id(5)]),run_id:run.id,run_plan_sha256:sha,ready:true,reason:'',contract_sha256:sha,
  source_options:[{value:'audios',label:'独立音频',kind:'audio'},{value:'images',label:'镜头用途图片',kind:'image'}],
  approval:{revision:0,decision_id:null,snapshot_sha256:null,status:'not_reviewed'},
  contract:{schema_version:1,kind:'manual_storyboard_review',stage_key:'storyboard_review',formal_object_type:'storyboard',canonical_sha256:sha,
    manual_basis:{run_plan_sha256:sha,storyboard_adoption:{decision_id:id(8),revision:1,snapshot_sha256:sha},bible_review:null,bible_image_review:null},
    canonical:{episodes:[{id:id(3),number:1,title:'审核范围',shots:[{id:id(4),number:1,title:'声音参考',
      reference_uses:[{use_key:'voice',media_kind:'audio',purpose:'custom_voice',source_mode:'existing',reference_version_id:null,asset_id:id(6),asset_sha256:sha}]},
      {id:id(5),number:2,title:'无参考',reference_uses:[]}]}]},
    assets:[{id:id(6),name:'声音参考',kind:'reference_audio',mime_type:'audio/wav',checksum:sha}]},
  references:[{shot_id:id(4),items:[{reference_key:'shot-use:voice',source_key:'audios',kind:'audio',source:{kind:'asset',asset_id:id(6)},
    purpose:'custom_voice',subject_ref:null,paired_reference_key:null}]},{shot_id:id(5),items:[]}]}
const blocked={...ready,display:null,ready:false,reason:'请先采用分镜',contract:null,contract_sha256:null,references:[]}
async function modules(fn){const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try{await fn(await vite.ssrLoadModule('/src/services/productionManualStoryboardReview.ts'),vite)}finally{await vite.close()}}

test('manual reference review only submits the exact fully checked original contract',()=>modules(m=>{
  assert.equal(m.readManualStoryboardReferences(blocked,run).ready,false)
  const detail=m.readManualStoryboardReferences(ready,run)
  for(const checked of [[],[id(4)],[id(4),id(4)]])assert.throws(()=>m.buildManualStoryboardReferenceApproval(detail,run,options,'key',checked,''))
  const result=m.buildManualStoryboardReferenceApproval(detail,run,options,'key',[id(5),id(4)],'确认')
  assert.equal(result.stage_key,'storyboard_review');assert.equal(result.expected_selection_revision,0)
  assert.deepEqual(result.payload,{expected_contract_sha256:sha,reviewed_shot_ids:[id(4),id(5)],comment:'确认'})
  assert.throws(()=>m.buildManualStoryboardReferenceApproval(detail,{...run,status:'succeeded'},options,'key',[id(4),id(5)],''))
  assert.throws(()=>m.buildManualStoryboardReferenceApproval(detail,run,{...options,actions:[]},'key',[id(4),id(5)],''))
}))

test('reference review refuses incomplete, duplicate or falsely current snapshots',()=>modules(m=>{
  for(const patch of [{run_id:id(99)},{source_options:[]},{source_options:[ready.source_options[0],ready.source_options[0]]},
    {source_options:[{...ready.source_options[0],kind:'image'}]},
    {references:ready.references.slice(0,1)},{references:[ready.references[0],ready.references[0]]},
    {contract:{...ready.contract,assets:[]}},{approval:{revision:1,decision_id:id(9),snapshot_sha256:'b'.repeat(64),status:'current'}},
    {approval:{revision:1,decision_id:id(9),snapshot_sha256:sha,status:'changed'}},
    {references:[{...ready.references[0],items:[{...ready.references[0].items[0],paired_reference_key:'missing'}]},ready.references[1]]}]){
    assert.throws(()=>m.readManualStoryboardReferences({...ready,...patch},run))
  }
  assert.throws(()=>m.readManualStoryboardReferences({...blocked,contract:ready.contract},run))
}))

test('reference review rejects legacy pools, implicit direct bindings and unused attachments',()=>modules(m=>{
  for(const key of ['character_ids','visual_reference_character_ids','reference_asset_ids','reference_bindings']){
    const data=structuredClone(ready)
    data.contract.canonical.episodes[0].shots[0][key]=[]
    assert.throws(()=>m.readManualStoryboardReferences(data,run),/旧混合关系/)
  }
  const oldBinding=structuredClone(ready)
  oldBinding.references[0].items[0].reference_key='audios:0'
  assert.throws(()=>m.readManualStoryboardReferences(oldBinding,run),/原独立用途身份/)
  const extraAsset=structuredClone(ready)
  extraAsset.contract.assets.push({...ready.contract.assets[0],id:id(7),name:'未使用附件'})
  assert.throws(()=>m.readManualStoryboardReferences(extraAsset,run),/未被用途选定的附件/)
}))

test('formal API uses the review endpoint and does not absorb failures',()=>modules(async(_m,vite)=>{
  const {api}=await vite.ssrLoadModule('/src/services/api.ts'),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const old=api.get
  try{api.get=async path=>{assert.equal(path,`/production-runs/${run.id}/manual-storyboard-review/`);return {data:ready}}
    assert.deepEqual(await platformApi.getManualStoryboardReview(run.id),ready)
    api.get=async()=>{throw Error('unavailable')};await assert.rejects(platformApi.getManualStoryboardReview(run.id),/unavailable/)
  }finally{api.get=old}
}))

test('reference review component requires every shot and retries an uncertain receipt without generating',()=>modules(async(_m,vite)=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),old={getManualStoryboardReview:platformApi.getManualStoryboardReview,performManualProductionAction:platformApi.performManualProductionAction}
  const calls:any[]=[],events:any[]=[];let reads=0
  platformApi.getManualStoryboardReview=async()=>{reads++;return JSON.parse(JSON.stringify(ready))}
  platformApi.performManualProductionAction=async(runId,body)=>{calls.push(JSON.parse(JSON.stringify({runId,body})))
    if(calls.length===1)throw Error('uncertain');return {action:'approve',result_kind:'approval_decision',target_id:id(9)}}
  const {default:Component}=await vite.ssrLoadModule('/src/components/ManualStoryboardReferenceReview.vue')
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  let state:any;const props=reactive({run,options})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(...args)=>events.push(args)});return()=>h('div')}})
  try{app.provide(ssrContextKey,{modules:new Set()});app.mount({});await Promise.resolve();await nextTick()
    assert.equal(calls.length,0);state.checked.value=[id(4)];await state.approve();assert.equal(calls.length,0)
    state.checked.value=[id(4),id(5)];await state.approve();assert.match(state.error.value,/uncertain/)
    const originalPending=JSON.stringify(state.pending.value),originalKey=state.key.value
    assert.deepEqual(events.at(-1),['busy',true])
    await state.load();assert.equal(reads,1);assert.equal(state.key.value,originalKey)
    assert.equal(JSON.stringify(state.pending.value),originalPending)
    await state.approve();assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1])
    assert.equal(events.filter(row=>row[0]==='changed').length,1);assert.equal(state.checked.value.length,0)
    assert.deepEqual(events.at(-1),['busy',false]);assert.equal(reads,2)
    const html=await renderToString(h(Component,props));assert.match(html,/不代表烟测通过/)
    assert.match(html,/用途与配对关系/)
  }finally{app.unmount();Object.assign(platformApi,old)}
}))

test('initial explicit review rejection unlocks rereading, but rejection while reconciling retains the unknown request',()=>modules(async(_m,vite)=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const old={getManualStoryboardReview:platformApi.getManualStoryboardReview,performManualProductionAction:platformApi.performManualProductionAction}
  const events:any[]=[],calls:any[]=[];let reads=0,unknown=false
  platformApi.getManualStoryboardReview=async()=>{reads++;return structuredClone(ready)}
  platformApi.performManualProductionAction=async(_id,body)=>{
    calls.push(JSON.parse(JSON.stringify(body)));if(unknown){unknown=false;throw Error('uncertain')}
    throw {response:{status:409,data:{code:'manual_selection_conflict',message:'内容已变化'}}}
  }
  const {default:Component}=await vite.ssrLoadModule('/src/components/ManualStoryboardReferenceReview.vue')
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  let state:any
  const app=renderer.createApp({setup(){state=Component.setup({run,options},{expose(){},emit:(...args)=>events.push(args)});return()=>h('div')}})
  try{
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await Promise.resolve();await nextTick()
    state.checked.value=[id(4),id(5)];await state.approve()
    assert.equal(state.pending.value,null);assert.equal(state.detail.value,null);assert.deepEqual(events.at(-1),['busy',false])
    await state.load();assert.equal(reads,2);unknown=true
    state.checked.value=[id(4),id(5)];await state.approve();const original=JSON.parse(JSON.stringify(state.pending.value))
    await state.approve();assert.deepEqual(state.pending.value,original);assert.deepEqual(calls[1],calls[2])
    await state.load();assert.equal(reads,2);assert.deepEqual(events.at(-1),['busy',true])
    assert.equal(events.some(row=>row[0]==='changed'),false)
  }finally{app.unmount();Object.assign(platformApi,old)}
}))

test('ready view shows exact audio uses and a no-reference shot without the old attachment pool',()=>modules(async(m,vite)=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),old=platformApi.getManualStoryboardReview
  platformApi.getManualStoryboardReview=async()=>JSON.parse(JSON.stringify(ready))
  try{
    const {default:Component}=await vite.ssrLoadModule('/src/components/ManualStoryboardReferenceReview.vue')
    const Seeded={...Component,setup(props,context){const state=Component.setup(props,context)
      state.detail.value=m.readManualStoryboardReferences(ready,run);return state}}
    const app=createSSRApp(Seeded,{run,options})
    app.use(createRouter({history:createMemoryHistory(),routes:[{path:'/projects/:projectId/episodes/:episodeId',name:'storyboard',component:{render:()=>h('div')}}]}))
    const html=await renderToString(app)
    for(const text of ['独立音频','custom_voice','当前没有绑定参考素材','确认已逐镜核对参考素材'])assert.ok(html.includes(text),text)
    assert.ok(!html.includes('已附加但未绑定用途'))
    assert.ok(html.includes('尚未审核'))
    assert.ok(!html.includes('已审核的独立音频'))
    assert.equal((html.match(/type="checkbox"/g)||[]).length,2)
  }finally{platformApi.getManualStoryboardReview=old}
}))

test('new shot image uses display the server source label and retain the authored purpose',()=>modules(async(m,vite)=>{
  const data=JSON.parse(JSON.stringify(ready))
  data.contract.assets=[{id:id(7),name:'确切图片',kind:'reference',mime_type:'image/png',checksum:sha}]
  data.source_options[1].label='服务端图片来源名称'
  data.references[0].items=[{reference_key:'shot-use:costume',source_key:'images',kind:'image',
    source:{kind:'asset',asset_id:id(7)},purpose:'校服与雨衣不可混用',subject_ref:null,paired_reference_key:null}]
  data.contract.canonical.episodes[0].shots[0].reference_uses=[{use_key:'costume',media_kind:'image',purpose:'校服与雨衣不可混用',
    source_mode:'existing',reference_version_id:null,asset_id:id(7),asset_sha256:sha}]
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),old=platformApi.getManualStoryboardReview
  platformApi.getManualStoryboardReview=async()=>data
  try{
    const {default:Component}=await vite.ssrLoadModule('/src/components/ManualStoryboardReferenceReview.vue')
    const Seeded={...Component,setup(props,context){const state=Component.setup(props,context)
      state.detail.value=m.readManualStoryboardReferences(data,run);return state}}
    const app=createSSRApp(Seeded,{run,options})
    app.use(createRouter({history:createMemoryHistory(),routes:[{path:'/projects/:projectId/episodes/:episodeId',name:'storyboard',component:{render:()=>h('div')}}]}))
    const html=await renderToString(app)
    assert.ok(html.includes('服务端图片来源名称'));assert.ok(html.includes('校服与雨衣不可混用'))
    assert.ok(html.includes('查看确切素材'))
    const broken=structuredClone(data);broken.contract.canonical.episodes[0].shots[0].reference_uses=[]
    assert.throws(()=>m.readManualStoryboardReferences(broken,run),/缺少此独立用途/)
    assert.throws(()=>m.readManualStoryboardReferences({...data,source_options:undefined},run))
  }finally{platformApi.getManualStoryboardReview=old}
}))
