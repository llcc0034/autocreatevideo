import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
import {createRenderer, createSSRApp, nextTick, reactive, ssrContextKey} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {readFileSync} from 'node:fs'
import {displayFixture} from './storyboardReviewDisplayFixture.ts'

const root=fileURLToPath(new URL('..',import.meta.url)), sha='a'.repeat(64), selectionSha='b'.repeat(64)
const id=(n:number)=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`
function response(){return {display:displayFixture(id(20),[id(3),id(10)]),source_snapshot_sha256:sha,selection_sha256:selectionSha,shot_count:2,
  selection:{schema_version:1,project_id:id(2),shots:[{shot_id:id(3),uses:[{use_id:id(4),use_key:'clothes',position:0,
    media_kind:'image',purpose:'同一旅人的雨衣外观',source_mode:'existing',setting:null,
    source:{kind:'reference_version',reference_version_id:id(5),version:2,series_id:id(6),acquisition_source:'generated'},
    asset:{asset_id:id(7),sha256:sha,mime_type:'image/png'}},{use_id:id(8),use_key:'voice',position:1,media_kind:'audio',purpose:'',
    source_mode:'upload',setting:null,source:{kind:'asset',asset_id:id(9)},asset:{asset_id:id(9),sha256:sha,mime_type:'audio/wav'}}]},
  {shot_id:id(10),uses:[]}]}}}
const props=()=>reactive({run:{id:id(1),project:id(2),stages:[]},stageId:id(11),refreshEpoch:0,disabled:false})
async function modules(fn:any){const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true}});try{await fn(vite)}finally{await vite.close()}}
const settle=async()=>{await new Promise(resolve=>setTimeout(resolve,0));await nextTick()}
function mount(Component:any,p:any,emit:any=()=>{}){
  let state:any
  const renderer=createRenderer({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(p,{expose(){},emit});return()=>null}})
  app.provide(ssrContextKey,{});app.mount({});return {state,unmount:()=>app.unmount()}
}

test('review display retains independent business prose, optional empty prose, exact versions and empty uses',()=>modules(async(vite:any)=>{
  const {recipeStoryboardReferenceReview:parse}=await vite.ssrLoadModule('/src/services/recipeStoryboardReferenceReview.ts')
  const value=parse(response(),id(2))
  assert.equal(value.selectionSha256,selectionSha);assert.equal(value.shots[0].uses[0].purpose,'同一旅人的雨衣外观')
  assert.equal(value.shots[0].uses[0].version,2);assert.equal(value.shots[0].uses[1].purpose,'');assert.deepEqual(value.shots[1].uses,[])
  for(const changes of [{shot_count:1},{selection_sha256:''},{source_snapshot_sha256:'bad'},{selection:{}}])assert.throws(()=>parse({...response(),...changes},id(2)))
  assert.throws(()=>parse(response(),id(99)))
  for(const change of [{position:-1},{use_id:'bad'},{purpose:null},{source_mode:''},{source:{kind:'asset',asset_id:id(99)}},
    {source:{...response().selection.shots[0]!.uses[0]!.source,series_id:'bad'}},
    {source:{...response().selection.shots[0]!.uses[0]!.source,acquisition_source:'unknown'}}]){
    const value=response();Object.assign(value.selection.shots[0]!.uses[0]!,change);assert.throws(()=>parse(value,id(2)))
  }
}))

test('actual review reads once on entry, does not poll-replace seen hashes and refreshes explicitly',()=>modules(async(vite:any)=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeStoryboardReferences.vue')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),before=platformApi.getStoryboardReferenceSelection
  let calls=0;platformApi.getStoryboardReferenceSelection=async()=>{calls++;return response()}
  const p=props(),events:any[]=[],mounted=mount(Component,p,(name:any,v:any)=>events.push([name,v]))
  try{await settle();assert.equal(calls,1);assert.equal(mounted.state.result.value.selectionSha256,selectionSha)
    p.run=JSON.parse(JSON.stringify(p.run));await nextTick();assert.equal(calls,1)
    await mounted.state.read();assert.equal(calls,2)
    p.refreshEpoch++;await settle();assert.equal(calls,3)
    assert.equal(events.filter(([name,value])=>name==='ready'&&value).length,3)
    p.disabled=true;await mounted.state.read();assert.equal(calls,3)
  }finally{platformApi.getStoryboardReferenceSelection=before;mounted.unmount()}
}))

test('read error clears approval basis, stays visible and recovers only with a successful read',()=>modules(async(vite:any)=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeStoryboardReferences.vue')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),before=platformApi.getStoryboardReferenceSelection
  let failed=false;platformApi.getStoryboardReferenceSelection=async()=>{if(failed)throw Error('isolated missing file');return response()}
  const mounted=mount(Component,props())
  try{await settle();failed=true;await mounted.state.read();assert.equal(mounted.state.result.value,null);assert.match(mounted.state.error.value,/missing file/)
    failed=false;await mounted.state.read();assert.equal(mounted.state.error.value,'');assert.ok(mounted.state.result.value)
  }finally{platformApi.getStoryboardReferenceSelection=before;mounted.unmount()}
}))

test('an initially disabled review reads once when unlocked without replacing an already seen selection',()=>modules(async(vite:any)=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeStoryboardReferences.vue')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),before=platformApi.getStoryboardReferenceSelection
  let calls=0;platformApi.getStoryboardReferenceSelection=async()=>{calls++;return response()}
  const p=props();p.disabled=true;const mounted=mount(Component,p)
  try{await settle();assert.equal(calls,0);p.disabled=false;await settle();assert.equal(calls,1)
    p.disabled=true;await nextTick();p.disabled=false;await settle();assert.equal(calls,1)
  }finally{platformApi.getStoryboardReferenceSelection=before;mounted.unmount()}
}))

test('new stage and unmount discard late read results without retaining a previous approval hash',()=>modules(async(vite:any)=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeStoryboardReferences.vue')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),before=platformApi.getStoryboardReferenceSelection
  const finish:any[]=[];platformApi.getStoryboardReferenceSelection=()=>new Promise(resolve=>finish.push(resolve))
  const p=props(),mounted=mount(Component,p)
  try{p.stageId=id(20);await nextTick();finish[0](response());await settle();assert.equal(mounted.state.result.value,null)
    mounted.unmount();finish[1](response());await settle();assert.equal(mounted.state.result.value,null)
  }finally{platformApi.getStoryboardReferenceSelection=before}
}))

test('actual readonly markup shows exact independent uses and has no legacy primary editor',()=>modules(async(vite:any)=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeStoryboardReferences.vue')
  const {recipeStoryboardReferenceReview:parse}=await vite.ssrLoadModule('/src/services/recipeStoryboardReferenceReview.ts')
  const p=props();p.disabled=true
  const html=await renderToString(createSSRApp({...Component,setup(p:any,c:any){const s=Component.setup(p,c);s.result.value=parse(response(),id(2));return s}},p))
  assert.match(html,/同一旅人的雨衣外观/);assert.match(html,/未填写（可选）/);assert.match(html,/素材版本 v2/)
  assert.match(html,/本镜未声明参考用途/);assert.match(html,/重新读取并核对选择/);assert.doesNotMatch(html,/<select|<input|主图下拉/)
}))

test('transport sends only observed hashes and reads one explicit readonly review endpoint',()=>modules(async(vite:any)=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),{api}=await vite.ssrLoadModule('/src/services/api.ts')
  const before=[api.get,api.post],calls:any[]=[]
  api.get=async(path:any)=>{calls.push(path);return {data:response()}}
  api.post=async(path:any,body:any)=>{calls.push(body);return {data:{id:id(1),project:id(2),plan_source:'recipe',recipe_version:id(12),stages:[]}}}
  try{await platformApi.getStoryboardReferenceSelection(id(1))
    await platformApi.approveProductionRun(id(1),{stageKey:'storyboard_review',decision:'approve',expectedSourceSnapshotSha256:sha,expectedSelectionSha256:selectionSha})
    assert.match(calls[0],/storyboard-reference-selection\/$/)
    assert.deepEqual(calls[1],{stage_key:'storyboard_review',decision:'approve',comment:'',expected_source_snapshot_sha256:sha,expected_selection_sha256:selectionSha})
  }finally{api.get=before[0];api.post=before[1]}
}))

test('parent invalidates seen hashes when selection opens and rechecks on release; known conflict does not retry',()=>{
  const source=readFileSync(new URL('../src/views/ProductionRunView.vue',import.meta.url),'utf8')
  assert.match(source,/if \(value\) storyboardReferenceReview.value = null/)
  assert.match(source,/if \(released\) storyboardReviewRefreshEpoch.value\+\+/)
  assert.match(source,/storyboard_reference_selection_changed/);assert.match(source,/expectedSelectionSha256/)
  assert.doesNotMatch(source,/shotReferenceBindings|storyboardReferenceDrafts|loadStoryboardApprovedAssets/)
})
