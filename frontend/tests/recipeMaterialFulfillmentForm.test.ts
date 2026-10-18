import test, {before, after} from 'node:test'
import assert from 'node:assert/strict'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
import {createSSRApp} from 'vue'
import {renderToString} from '@vue/server-renderer'
let vite, module
before(async()=>{vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),server:{middlewareMode:true},appType:'custom'});module=await vite.ssrLoadModule('/src/services/recipeMaterialFulfillmentForm.ts')})
after(async()=>{await vite?.close()})
const id=n=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`, sha='a'.repeat(64)
const option={unitKey:`appearance:${id(2)}`,requirementKey:`setting:${id(3)}`,name:'林晓 主图',role:'primary'}
const context=()=>({sourceMode:'existing',stageRunId:id(1),unitKey:option.unitKey,requirementKey:option.requirementKey,runPlanSha256:sha,
  appearanceId:id(2),settingRevisionId:id(3),appearanceRole:'primary',referenceName:option.name,primaryAppearanceId:null,canSelect:true,reason:'',selection:null})
const series=()=>({id:id(4),name:'明确关联系列',role:'primary',owner_id:1,primary_series_id:null,bible_variant_id:id(2),version_count:1,variant_count:0})
const asset=()=>({id:id(5),name:'成图',type:'image',isActive:true,mimeType:'image/png',checksum:sha,metadata:{}})
const version=()=>({id:id(7),series_id:id(4),version:1,source:'generated',asset:{id:id(5),name:'原生成成图',is_active:true,mime_type:'image/png'},setting_revision_id:id(3),generated_from_id:id(8)})
const receipt=()=>({id:id(6),referenceVersionId:id(7),seriesId:id(4),assetId:id(5),source:'existing',targetSha256:sha,selectionMode:'manual',selectedById:1,createdAt:'2026-10-02T06:00:00Z'})
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r});return {promise,resolve}}
function setup(overrides={}) {
  const calls=[]
  const api={getMaterialFulfillmentContext:async()=>context(),listSeries:async query=>{calls.push(query);return {count:1,next:null,previous:null,results:[series()]}},
    listVersions:async()=>({count:1,next:null,previous:null,results:[version()]}),getVersion:async()=>version(),getAsset:async()=>asset(),
    fulfillMaterial:async(...args)=>{calls.push(args);return receipt()},...overrides}
  return {flow:module.createMaterialFulfillmentForm(api),api,calls}
}
async function ready(flow) {await flow.inspect(id(9),id(1),sha,option);await flow.loadSeries();flow.chooseSeries(series());flow.chooseAsset(asset(),'asset_library')}
function stage() {return {id:id(1),stageKey:'bible_image_generation',status:'running',inputSnapshotSha256:sha,inputSnapshot:{
  units:[{unit_key:option.unitKey,requirement_key:option.requirementKey,appearance_role:'primary'}],
  material_requirements:{run_plan_sha256:sha,targets:[{requirement_key:option.requirementKey,appearance_role:'primary',reference_name:option.name,
    setting:{appearance_id:id(2),setting_revision_id:id(3)}}]}}}}
const run=()=>({id:id(9),planSource:'recipe',status:'running',executionPlan:{sha256:sha}})
test('requirements are only original frozen unit references, including empty and unavailable states',()=>{
  assert.deepEqual(module.materialRequirementOptions(run(),stage()),[option])
  assert.equal(module.materialRequirementOptions({...run(),planSource:'manual'},stage()),null)
  const empty=stage();empty.inputSnapshot.units=[];empty.inputSnapshot.material_requirements.targets=[]
  assert.deepEqual(module.materialRequirementOptions(run(),empty),[])
  assert.throws(()=>module.materialRequirementOptions(run(),{...stage(),inputSnapshotSha256:''}),/尚未冻结/)
  const wrong=stage();wrong.inputSnapshot.units[0].requirement_key=`setting:${id(33)}`
  assert.throws(()=>module.materialRequirementOptions(run(),wrong))
  assert.throws(()=>module.materialRequirementOptions({...run(),executionPlan:{sha256:'b'.repeat(64)}},stage()))
})
test('only original material recovery pause allows editing alongside running status',()=>{
  const recovering={...run(),status:'awaiting_input',currentStageKey:'bible_image_generation',blockedCode:'waiting_material_recovery'}
  const current={...stage(),reasonCode:'waiting_material_recovery'}
  assert.equal(module.materialFulfillmentRunAvailable(recovering,current),true)
  assert.equal(module.materialFulfillmentRunAvailable(run(),stage()),true)
  for(const change of [{blockedCode:'execution_preparation_review_required'},{status:'blocked'},{status:'cancelled'},
    {status:'succeeded'},{currentStageKey:'bible_image_review'}]) {
    assert.equal(module.materialFulfillmentRunAvailable({...recovering,...change},current),false)
  }
  assert.equal(module.materialFulfillmentRunAvailable(recovering,{...current,status:'cancelled'}),false)
  assert.equal(module.materialFulfillmentRunAvailable(recovering,{...current,reasonCode:'waiting_external_task'}),false)
})

test('explicit series and Asset submit one exact HTTP choice, without auto selection or review',async()=>{
  const {flow,calls}=setup();await flow.inspect(id(9),id(1),sha,option)
  assert.equal(flow.state.series,null);assert.equal(flow.state.asset,null);assert.equal(calls.length,0)
  await flow.loadSeries();assert.deepEqual(calls[0],{page:1,search:'',role:'primary',bible_variant_id:id(2)})
  flow.chooseSeries(series());flow.chooseAsset(asset(),'asset_library');assert.equal(await flow.submit(),true)
  assert.deepEqual(calls[1],[id(9),context(),{seriesId:id(4),assetId:id(5),source:'existing'}])
  assert.deepEqual(flow.state.receipt,receipt());assert.equal(await flow.submit(),false)
})

test('generated reuse receipt is read without choosing or submitting again',async()=>{
  const saved={...receipt(),source:'generated',selectionMode:'automatic',selectedById:null}
  const {flow,calls}=setup({getMaterialFulfillmentContext:async()=>({...context(),canSelect:false,reason:'已有确切版本',selection:saved})})
  await flow.inspect(id(9),id(1),sha,option);await flow.loadSeries()
  assert.equal(flow.state.receipt.source,'generated')
  assert.equal(flow.state.receipt.selectedById,null)
  assert.equal(flow.state.context.sourceMode,'existing')
  assert.equal(await flow.submit(),false)
  assert.deepEqual(calls,[])
})

test('manual original generated version submits only immutable identity, preserves origin and recovers unknown reply',async()=>{
  let saved=null,posts=0
  const {flow}=setup({fulfillMaterial:async(_run,_ctx,choice)=>{
    posts++;assert.deepEqual(choice,{referenceVersionId:id(7)})
    saved={...receipt(),source:'generated'};throw Error('reply lost')
  },getMaterialFulfillmentContext:async()=>saved?{...context(),canSelect:false,reason:'已保存',selection:saved}:context()})
  await flow.inspect(id(9),id(1),sha,option);await flow.loadSeries();flow.chooseSeries(series());await flow.loadVersions()
  assert.equal(flow.state.version,null);assert.equal(await flow.submit(),false)
  await flow.chooseVersion(version());assert.equal(flow.state.version.generated_from_id,id(8))
  assert.equal(flow.state.source,null);assert.equal(await flow.submit(),false);assert.equal(flow.state.unknown,true)
  flow.chooseAsset(asset(),'asset_library');await flow.loadVersions(2)
  assert.equal(flow.state.version.id,id(7));assert.equal(posts,1)
  await flow.refresh();assert.equal(flow.state.unknown,false);assert.equal(flow.state.receipt.source,'generated');assert.equal(posts,1)
})

test('version paging is explicit, shows rejected revisions, and drops late directory or preview replies',async()=>{
  const pages=[], {flow,api}=setup({listVersions:async query=>{pages.push(query);return {count:2,next:query.page===1?'next':null,previous:query.page===2?'previous':null,
    results:[query.page===1?{...version(),setting_revision_id:id(22)}:version()]}}})
  await flow.inspect(id(9),id(1),sha,option);await flow.loadSeries();flow.chooseSeries(series());await flow.loadVersions()
  assert.match(flow.versionReason(flow.state.versions.results[0]),/修订/)
  await flow.chooseVersion(flow.state.versions.results[0]);assert.equal(flow.state.asset,null)
  await flow.loadVersions(2);assert.deepEqual(pages,[{series_id:id(4),page:1},{series_id:id(4),page:2}])
  const pending=deferred();api.getVersion=()=>pending.promise
  const selecting=flow.chooseVersion(version());assert.equal(flow.state.choosingVersion,true)
  flow.reset();pending.resolve(version());await selecting;assert.equal(flow.state.version,null);assert.equal(flow.state.asset,null)
  await flow.inspect(id(9),id(1),sha,option);await flow.loadSeries();flow.chooseSeries(series())
  const listing=deferred();api.listVersions=()=>listing.promise
  const reading=flow.loadVersions();flow.reset();listing.resolve({count:1,results:[version()]});await reading;assert.equal(flow.state.versions,null)
})

test('upload policy refuses generated versions and failed original-asset reads cannot submit',async()=>{
  const {flow,api}=setup({getMaterialFulfillmentContext:async()=>({...context(),sourceMode:'upload'})})
  await flow.inspect(id(9),id(1),sha,option);await flow.loadSeries();flow.chooseSeries(series());await flow.loadVersions()
  assert.match(flow.versionReason(version()),/上传/);await flow.chooseVersion(version());assert.equal(flow.state.version,null)
  api.getMaterialFulfillmentContext=async()=>context();await flow.refresh();await flow.loadSeries();flow.chooseSeries(series());await flow.loadVersions()
  api.getAsset=async()=>{throw Error('original asset unavailable')};await flow.chooseVersion(version())
  assert.match(flow.state.versionsError,/unavailable/);assert.equal(await flow.submit(),false)
  api.getAsset=async()=>asset();await flow.chooseVersion(version());assert.equal(flow.state.version.id,id(7))
  flow.chooseAsset(asset(),'asset_library');assert.equal(flow.state.version,null);assert.equal(flow.state.source,'existing')
})
test('wrong plan context is refused and no stale context survives reread failure',async()=>{
  const {flow,api}=setup();await ready(flow)
  api.getMaterialFulfillmentContext=async()=>({...context(),runPlanSha256:'b'.repeat(64)})
  await flow.refresh();assert.equal(flow.state.context,null);assert.equal(flow.state.asset.id,id(5));assert.match(flow.state.error,/变化/)
  assert.equal(await flow.submit(),false)
})
test('late context and series reads cannot overwrite a different requirement or disposed form',async()=>{
  const wait=deferred(), {flow}=setup({getMaterialFulfillmentContext:()=>wait.promise})
  const reading=flow.inspect(id(9),id(1),sha,option);flow.reset();wait.resolve(context());await reading
  assert.equal(flow.state.context,null)
  const seriesWait=deferred(), other=setup({listSeries:()=>seriesWait.promise}).flow
  await other.inspect(id(9),id(1),sha,option);const loading=other.loadSeries();other.reset()
  seriesWait.resolve({results:[series()],count:1});await loading;assert.equal(other.state.directory,null)
})
test('foreign or failed series directory is not actionable and does not choose first result',async()=>{
  const {flow,api}=setup();await flow.inspect(id(9),id(1),sha,option);await flow.loadSeries();assert.equal(flow.state.series,null)
  api.listSeries=async()=>({count:1,results:[{...series(),bible_variant_id:id(20)}]})
  await flow.loadSeries();flow.chooseSeries(series());assert.equal(flow.state.directory,null);assert.equal(flow.state.series,null)
  assert.match(flow.state.error,/不一致/)
})
test('upload and existing respect source policy and do not bypass file/type checks',async()=>{
  const {flow}=setup({getMaterialFulfillmentContext:async()=>({...context(),sourceMode:'upload'}),fulfillMaterial:async()=>({...receipt(),source:'upload'})})
  await flow.inspect(id(9),id(1),sha,option);await flow.loadSeries();flow.chooseSeries(series())
  flow.chooseAsset(asset(),'asset_library');assert.equal(flow.state.asset,null)
  flow.chooseAsset({...asset(),checksum:''},'asset_upload');assert.equal(flow.state.asset,null)
  flow.chooseAsset(asset(),'asset_upload');assert.equal(await flow.submit(),true);assert.equal(flow.state.receipt.source,'upload')
})
test('invalid replacement clears an earlier draft instead of silently using the previous image',async()=>{
  const {flow}=setup();await ready(flow);flow.chooseAsset({...asset(),mimeType:'video/mp4'},'asset_library')
  assert.equal(flow.state.asset,null);assert.equal(await flow.submit(),false)
})
test('variant context queries its own series and all frozen requirements remain reachable by paging',async()=>{
  const variant={...option,role:'variant'},queries=[]
  const {flow}=setup({getMaterialFulfillmentContext:async()=>({...context(),appearanceRole:'variant',primaryAppearanceId:id(20)}),
    listSeries:async query=>{queries.push(query);return {count:60,next:'?page=2',previous:null,results:[]}}})
  await flow.inspect(id(9),id(1),sha,variant);await flow.loadSeries(2)
  assert.equal(queries[0].role,'variant');assert.equal(queries[0].bible_variant_id,id(2));assert.equal(queries[0].page,2)
  const {default:component}=await vite.ssrLoadModule('/src/components/RecipeMaterialFulfillment.vue'), many=stage()
  many.inputSnapshot.units=[];many.inputSnapshot.material_requirements.targets=[]
  for(let n=100;n<151;n++) {
    many.inputSnapshot.units.push({unit_key:`appearance:${id(n)}`,requirement_key:`setting:${id(n+100)}`,appearance_role:'primary'})
    many.inputSnapshot.material_requirements.targets.push({requirement_key:`setting:${id(n+100)}`,appearance_role:'primary',reference_name:`外观${n}`,setting:{appearance_id:id(n),setting_revision_id:id(n+100)}})
  }
  assert.equal(module.materialRequirementOptions(run(),many).length,51)
  const html=await renderToString(createSSRApp(component,{run:run(),stage:many}))
  assert.match(html,/共 51 项/);assert.match(html,/下一页需求/);assert.doesNotMatch(html,/外观150/)
})
test('unknown response locks choice and retries identical request, including after empty reread',async()=>{
  let count=0;const posts=[]
  const {flow}=setup({fulfillMaterial:async(...args)=>{posts.push(args);if(!count++)throw Error('网络中断');return receipt()}})
  await ready(flow);assert.equal(await flow.submit(),false);assert.equal(flow.state.unknown,true)
  flow.chooseAsset({...asset(),id:id(20)},'asset_library');assert.equal(flow.state.asset.id,id(5))
  await flow.inspect(id(10),id(1),sha,option);await flow.refresh();assert.equal(flow.state.unknown,true)
  assert.equal(await flow.submit(),true);assert.deepEqual(posts[0],posts[1]);assert.equal(flow.state.unknown,false)
})
test('reread saved receipt recovers unknown outcome without another POST',async()=>{
  let posts=0;const {flow,api}=setup({fulfillMaterial:async()=>{posts++;throw Error('timeout')}})
  await ready(flow);await flow.submit();api.getMaterialFulfillmentContext=async()=>({...context(),canSelect:false,reason:'已保存',selection:receipt()})
  await flow.refresh();assert.equal(flow.state.unknown,false);assert.deepEqual(flow.state.receipt,receipt());assert.equal(posts,1)
})
test('definitive rejection requires recheck; unknown retry rejection cannot release original request',async()=>{
  const reject={response:{status:400,data:{detail:'需求已变化'}}};const {flow,api}=setup({fulfillMaterial:async()=>{throw reject}})
  await ready(flow);await flow.submit();assert.equal(flow.state.unknown,false);assert.equal(flow.state.context,null)
  await ready(flow);api.fulfillMaterial=async()=>{throw Error('lost')};await flow.submit();api.fulfillMaterial=async()=>{throw reject}
  await flow.submit();assert.equal(flow.state.unknown,true)
})
test('rejected upload association retains the original Asset when rechecking, never uploads again',async()=>{
  const {flow,api}=setup({getMaterialFulfillmentContext:async()=>({...context(),sourceMode:'upload'}),fulfillMaterial:async()=>{throw {response:{status:400}}}})
  await flow.inspect(id(9),id(1),sha,option);await flow.loadSeries();flow.chooseSeries(series());flow.chooseAsset(asset(),'asset_upload')
  await flow.submit();await flow.refresh();await flow.loadSeries();flow.chooseSeries(series())
  assert.equal(flow.state.asset.id,id(5));assert.equal(flow.state.source,'upload')
  api.fulfillMaterial=async()=>({...receipt(),source:'upload'});assert.equal(await flow.submit(),true)
})
test('duplicate clicks and delayed POST on old view cannot save a new view receipt',async()=>{
  const wait=deferred();let posts=0;const {flow}=setup({fulfillMaterial:()=>{posts++;return wait.promise}})
  await ready(flow);const sending=flow.submit();assert.equal(await flow.submit(),false);flow.reset()
  wait.resolve(receipt());assert.equal(await sending,false);assert.equal(flow.state.receipt,null);assert.equal(posts,1)
})
test('server refusal and mismatched receipt never display new fulfillment success',async()=>{
  const {flow}=setup({getMaterialFulfillmentContext:async()=>({...context(),canSelect:false,reason:'已有生成准备'})})
  await ready(flow);assert.equal(await flow.submit(),false);assert.equal(flow.state.receipt,null)
  const mismatch=setup({fulfillMaterial:async()=>({...receipt(),assetId:id(20)})}).flow
  await ready(mismatch);await mismatch.submit();assert.equal(mismatch.state.unknown,true);assert.equal(mismatch.state.receipt,null)
})
test('formal component renders frozen identities, non-approval semantics, missing/empty and readonly states',async()=>{
  const {default:component}=await vite.ssrLoadModule('/src/components/RecipeMaterialFulfillment.vue')
  const html=await renderToString(createSSRApp(component,{run:run(),stage:stage(),disabled:true}))
  assert.match(html,/本轮素材需求/);assert.match(html,/不是模型参考输入/);assert.match(html,/disabled/);assert.match(html,/林晓/)
  const missing=await renderToString(createSSRApp(component,{run:run(),stage:{...stage(),inputSnapshotSha256:''}}))
  assert.match(missing,/尚未冻结/)
})
