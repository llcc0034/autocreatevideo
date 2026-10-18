import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'

import { sha, id, run, options, shot, data, proposed } from './fixtures/manualStoryboardMaterialization.ts'
const root=fileURLToPath(new URL('..',import.meta.url))
async function modules(fn){
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try{await fn(await vite.ssrLoadModule('/src/services/productionManualStoryboard.ts'),vite)}finally{await vite.close()}
}

test('authored storyboard adoption freezes explicit whole content without fake script or execution selection',()=>modules(m=>{
  const detail=m.readManualStoryboard(data,run),request=m.buildManualStoryboardAdoption(detail,run,options,'adopt',[shot.id],'checked')
  assert.deepEqual(request.payload,{source:{kind:'existing_storyboard'},expected_content_sha256:sha,reviewed_shot_ids:[shot.id],comment:'checked'})
  assert.equal(request.expected_selection_revision,0);assert.equal(request.stage_key,'storyboard')
  assert.equal(detail.content.script,null)
  assert.throws(()=>m.buildManualStoryboardAdoption(detail,run,options,'adopt',[],''))
  assert.throws(()=>m.buildManualStoryboardAdoption(detail,run,options,'adopt',[shot.id,shot.id],''))
  assert.throws(()=>m.buildManualStoryboardAdoption(detail,run,{...options,actions:[]},'adopt',[shot.id],''))
}))

test('storyboard content rejects incomplete or cross-run facts and distinguishes changed from current',()=>modules(m=>{
  for(const patch of [{run_id:id(99)},{content_sha256:''},{content:{...data.content,script:undefined}},
    {content:{...data.content,schema_version:2}}, {content:{...data.content,script_decision_id:undefined}},
    {content:{...data.content,script_decision_id:id(9)}},
    ...['reference_asset_ids','reference_bindings','character_ids','visual_reference_character_ids'].map(field=>({content:{...data.content,episodes:[{
      ...data.content.episodes[0],shots:[{...shot,[field]:[]}]}]}})),
    {content:{...data.content,episodes:[data.content.episodes[0],data.content.episodes[0]]}},
    {content:{...data.content,episodes:[{...data.content.episodes[0],shots:[shot,shot]}]}},
    {adoption:{...data.adoption,status:'current'}}]) assert.throws(()=>m.readManualStoryboard({...data,...patch},run))
  const current={...data,adoption:{revision:1,decision_id:id(5),snapshot_sha256:sha,content_sha256:sha,status:'current',source:{kind:'existing_storyboard'}}}
  assert.equal(m.readManualStoryboard(current,run).adoption.status,'current')
  assert.throws(()=>m.readManualStoryboard({...current,adoption:{...current.adoption,status:'changed'}},run))
  assert.equal(m.readManualStoryboard({...current,content_sha256:'b'.repeat(64),adoption:{...current.adoption,status:'changed'}},run).adoption.status,'changed')
}))

test('confirmed script materialization requires explicit review and frozen server parameters',()=>modules(m=>{
  const detail=m.readManualStoryboard(proposed,run)
  assert.throws(()=>m.buildManualStoryboardFromScript(detail,run,options,'script',false,''))
  const request=m.buildManualStoryboardFromScript(detail,run,options,'script',true,'已核对')
  assert.deepEqual(request.payload,{source:{kind:'confirmed_script',expected_materialization_sha256:sha},expected_content_sha256:sha,comment:'已核对'})
  assert.equal(request.action,'adopt');assert.equal(request.payload.parameters,undefined)
  for(const decision of [null,undefined,id(99)]) {
    assert.throws(()=>m.readManualStoryboard({...proposed,materialization:{...proposed.materialization,contract:{
      ...proposed.materialization.contract,source_basis:{...proposed.materialization.contract.source_basis,script_decision_id:decision}}}},run))
  }
  assert.throws(()=>m.buildManualStoryboardFromScript(m.readManualStoryboard(data,run),run,options,'script',true,''))
  for(const parameterPatch of [{default_conditioning_mode:null},{default_shot_duration_ms:undefined},{include_audio:'yes'},
    {episode_count_hint:1},{auto_split_trigger_chars:12000},{auto_split_target_chars:6000},{prop_alias_policy:{}}]){
    assert.throws(()=>m.readManualStoryboard({...proposed,materialization:{...proposed.materialization,contract:{
      ...proposed.materialization.contract,parameters:{...proposed.materialization.contract.parameters,...parameterPatch}}}},run))
  }
  assert.equal(m.readManualStoryboard(proposed,run).materialization.ready,true)
  for (const basisPatch of [{schema_version:1},{run_plan_sha256:'b'.repeat(64)}, {script_stages:[]},
    {bible_review:{}},{source:{}},{resolved_relations_sha256:null}]) {
    assert.throws(()=>m.readManualStoryboard({...proposed,materialization:{...proposed.materialization,contract:{
      ...proposed.materialization.contract,source_basis:{...proposed.materialization.contract.source_basis,...basisPatch}}}},run))
  }
  assert.throws(()=>m.readManualStoryboard({...proposed,materialization:{...proposed.materialization,contract:{
    ...proposed.materialization.contract,schema_version:2}}},run))
}))

test('typed cast and exact dialogue positions never merge a voice-only speaker into visible cast',()=>modules(m=>{
  const row={...shot,dialogue:[{speaker_id:'S1',text:'  原对白，不补写。 ',start_ms:125,end_ms:900}],
    character_relations:[{role:'speaker',position:0,character_id:id(40)},{role:'visible',position:0,character_id:id(41)}]}
  const payload=(value)=>({...data,content:{...data.content,episodes:[{...data.content.episodes[0],shots:[value]}]}})
  assert.deepEqual(m.readManualStoryboard(payload(row),run).content.episodes[0].shots[0],row)
  for(const relations of [undefined,[],[row.character_relations[1]],[...row.character_relations,row.character_relations[0]],
    [{...row.character_relations[0],position:1}], [{...row.character_relations[0],position:true}],
    [{...row.character_relations[0],role:'mixed'}],[{...row.character_relations[0],role:['speaker']}],
    [{...row.character_relations[0],character_id:'姓名'}]]) {
    assert.throws(()=>m.readManualStoryboard(payload({...row,character_relations:relations}),run))
  }
}))

test('adopted structured source cannot masquerade as a current unrelated script or legacy receipt',()=>modules(m=>{
  const adopted={...data,content:{...data.content,script:proposed.content.script,script_decision_id:proposed.content.script_decision_id},
    adoption:{revision:1,decision_id:id(20),snapshot_sha256:sha,content_sha256:sha,status:'current',
      source:{kind:'confirmed_script',sha256:sha,contract:proposed.materialization.contract}}}
  assert.equal(m.readManualStoryboard(adopted,run).adoption.status,'current')
  for(const content of [{...adopted.content,script:null,script_decision_id:null},
    {...adopted.content,script:{...proposed.content.script,id:id(99)}},
    {...adopted.content,script_decision_id:id(99)}]) assert.throws(()=>m.readManualStoryboard({...adopted,content},run))
  for(const change of [{warnings:[]},{method:'canonical'},{sha256:null},
    {contract:{...proposed.materialization.contract,schema_version:2}}]) {
    assert.throws(()=>m.readManualStoryboard({...adopted,adoption:{...adopted.adoption,source:{...adopted.adoption.source,...change}}},run))
  }
  const changed={...adopted,content_sha256:'b'.repeat(64),content:{...adopted.content,script_decision_id:id(99)},
    adoption:{...adopted.adoption,status:'changed'}}
  assert.equal(m.readManualStoryboard(changed,run).adoption.status,'changed')
}))

test('script materialization component displays source/defaults and never submits on read',()=>modules(async(_m,vite)=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const old={getManualStoryboard:platformApi.getManualStoryboard,performManualProductionAction:platformApi.performManualProductionAction}
  const calls:any[]=[]
  platformApi.getManualStoryboard=async()=>JSON.parse(JSON.stringify(proposed))
  platformApi.performManualProductionAction=async(runId,body)=>{
    calls.push(JSON.parse(JSON.stringify({runId,body})));throw new Error('connection lost')
  }
  const {default:Component}=await vite.ssrLoadModule('/src/components/ManualStoryboardAdoption.vue')
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const props=reactive({run,options});let state:any
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit(){}});return()=>h('div')}})
  try{
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await Promise.resolve();await nextTick()
    await state.adopt(true);assert.equal(calls.length,0)
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>h('div')}}]})
    await router.push('/');await router.isReady()
    const rendered=createSSRApp({...Component,setup:()=>state},props);rendered.use(router)
    const html=await renderToString(rendered)
    assert.match(html,/生成时由所选执行实例确定/);assert.match(html,/不会使用后来编辑的母稿/);assert.match(html,/采用该剧本并创建分镜/)
    assert.match(html,/不推测集数、不自动切段/);assert.match(html,/独立创作字段保留原值及版本/)
    assert.doesNotMatch(html,/预期集数|auto_split_trigger_chars|prop_alias_policy/)
    state.scriptConfirmed.value=true;await state.adopt(true);assert.match(state.error.value,/connection lost/)
    await state.adopt(true);assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1])
    assert.equal(calls[0].body.payload.source.kind,'confirmed_script')
  }finally{app.unmount();Object.assign(platformApi,old)}
}))

test('storyboard component requires each explicit check and safely retries the original receipt',()=>modules(async(_m,vite)=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const old={getManualStoryboard:platformApi.getManualStoryboard,performManualProductionAction:platformApi.performManualProductionAction}
  const calls:any[]=[],events:any[]=[]
  platformApi.getManualStoryboard=async()=>JSON.parse(JSON.stringify(data))
  platformApi.performManualProductionAction=async(runId,body)=>{
    calls.push(JSON.parse(JSON.stringify({runId,body})))
    if(calls.length===1)throw new Error('uncertain response')
    return {action:'adopt',result_kind:'approval_decision',target_id:id(7)}
  }
  const {default:Component}=await vite.ssrLoadModule('/src/components/ManualStoryboardAdoption.vue')
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const props=reactive({run,options});let state:any
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(...args)=>events.push(args)});return()=>h('div')}})
  try{
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await Promise.resolve();await nextTick()
    assert.equal(calls.length,0);await state.adopt();assert.equal(calls.length,0)
    state.checked.value=[shot.id];await state.adopt();assert.match(state.error.value,/uncertain/)
    await state.adopt();assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1])
    assert.equal(events.filter(row=>row[0]==='changed').length,1)
    const router=createRouter({history:createMemoryHistory(),routes:[
      {path:'/',component:{render:()=>h('div')}},
      {path:'/projects/:projectId/episodes/:episodeId/storyboard',name:'storyboard',component:{render:()=>h('div')}}]})
    await router.push('/');await router.isReady()
    // Render the actual loaded reactive component state. A fresh SSR mount
    // correctly shows loading because this view reads only after opening.
    const rendered=createSSRApp({...Component,setup:()=>state},props);rendered.use(router)
    const html=await renderToString(rendered)
    assert.match(html,/不自动拆稿或调用模型/);assert.match(html,/采用已核对的分镜内容/)
    assert.match(html,/参考素材及槽位需单独审核/)
    assert.match(html,/创作原值与人工修订请在分镜页的独立字段中核对/)
    assert.doesNotMatch(html,/未填写；不会自动补写|镜头调度|详细描述|声景/)
    assert.match(html,new RegExp(`/projects/${run.project}/episodes/${id(3)}/storyboard`))
  }finally{app.unmount();Object.assign(platformApi,old)}
}))
