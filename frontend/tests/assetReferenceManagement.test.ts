import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'

const series = {id:'p',name:'主图',role:'primary',owner_id:1,primary_series_id:null,bible_variant_id:null,version_count:2,variant_count:0}
const option = {id:'b',name:'主图设定',reference_name:'Reference',project_id:'project',project_title:'项目',character_name:'人物',scene_name:null,prop_name:null}
const page = (results:any[],next:string|null=null,previous:string|null=null) => ({results,count:results.length,next,previous})
const impact = {id:'p',target_type:'series',name:'主图',version_count:2,asset_count:1,variant_count:0,dependent_version_count:0,production_use_count:0,can_remove:true}
const deferred=()=>{let resolve!:(v:any)=>void;const promise=new Promise<any>(r=>resolve=r);return {resolve,promise}}
async function setup(c:any, overrides:any={}) {
  const vite = await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  c.after(()=>vite.close())
  const {createReferenceManagement}=await vite.ssrLoadModule('/src/services/assetReferenceManagement.ts')
  const calls:any[]=[];let changes=0
  const api={getSeries:async()=>({...series}),bibleOptions:async(id:string,q:any)=>{calls.push(['options',id,q]);return page([option])},
    updateSeries:async(id:string,p:any)=>{calls.push(['patch',id,p]);return {...series,...p}},
    removalImpact:async()=>({...impact}),remove:async(...args:any[])=>{calls.push(['delete',...args])},...overrides}
  return {flow:createReferenceManagement(api,()=>changes++),calls,vite,changes:()=>changes}
}
test('arbitrary names and optional Bible use dirty PATCH on exact series without rewriting versions',async c=>{
  const {flow,calls,changes}=await setup(c)
  await flow.edit('p');await flow.save();assert.equal(changes(),0)
  flow.state.name=' 人物 / tenue ';flow.state.bibleId='b';flow.chooseBible();await flow.save()
  assert.deepEqual(calls.at(-1),['patch','p',{name:'人物 / tenue',bible_variant_id:'b'}])
  assert.equal(changes(),1);assert.equal(flow.state.mode,'');assert.match(flow.state.notice,/已有请求未改写/)
})
test('optional unlink is explicit null, current choice retained beyond searched page',async c=>{
  const {flow,calls}=await setup(c,{getSeries:async()=>({...series,bible_variant_id:'b'}),bibleOptions:async(_id:string,q:any)=>q.id?page([option]):page([])})
  await flow.edit('p');assert.equal(flow.state.currentBible.id,'b');assert.equal(flow.state.bibleId,'b')
  flow.state.bibleId='';await flow.save()
  assert.deepEqual(calls.at(-1),['patch','p',{bible_variant_id:null}])
})
test('missing current Bible is explicit error and never silently clears it',async c=>{
  const {flow,changes}=await setup(c,{getSeries:async()=>({...series,bible_variant_id:'b'}),bibleOptions:async()=>page([])})
  await flow.edit('p');assert.match(flow.state.error,/未自动解除/);assert.equal(flow.state.bibleId,'b');assert.equal(changes(),0)
})
test('paged selected option survives subsequent empty query and late queries cannot overwrite',async c=>{
  const stale=deferred();const {flow}=await setup(c,{bibleOptions:async(_id:string,q:any)=>q.search==='old'?stale.promise:q.search==='empty'?page([]):page([option],'next')})
  await flow.edit('p');flow.state.bibleId='b';flow.chooseBible()
  flow.state.search='old';const pending=flow.options(2)
  flow.state.search='empty';await flow.options();stale.resolve(page([{...option,id:'late'}]));await pending
  assert.equal(flow.state.options.count,0);assert.equal(flow.state.currentBible.id,'b');assert.equal(flow.state.bibleId,'b')
})
test('cancel and close ignore stale reads, disposal never reports success',async c=>{
  const late=deferred();const {flow,changes}=await setup(c,{getSeries:()=>late.promise})
  const pending=flow.edit('p');flow.close();late.resolve(series);await pending
  assert.equal(flow.state.mode,'');assert.equal(flow.state.series,null);assert.equal(changes(),0)
})
test('duplicate submission blocked and dialog cannot close during exact write',async c=>{
  const pending=deferred();let writes=0
  const {flow,changes}=await setup(c,{updateSeries:async()=>{writes++;return pending.promise}})
  await flow.edit('p');flow.state.name='new';const saving=flow.save();await flow.save();flow.close()
  assert.equal(writes,1);assert.equal(flow.state.mode,'edit');assert.equal(flow.state.busy,true)
  pending.resolve({...series,name:'new'});await saving;assert.equal(changes(),1);assert.equal(flow.state.busy,false)
})
test('backend binding failure preserves edits and raw detail without false success',async c=>{
  const {flow,changes}=await setup(c,{updateSeries:async()=>{throw Error('已有版本引用原设定修订')}})
  await flow.edit('p');flow.state.bibleId='b';await flow.save()
  assert.match(flow.state.error,/原设定修订/);assert.equal(flow.state.bibleId,'b');assert.equal(flow.state.mode,'edit');assert.equal(changes(),0)
})
test('impact read and cancellation do not delete; confirmed deletion is scoped',async c=>{
  const {flow,calls,changes}=await setup(c)
  await flow.removal('series','p','主图');flow.close();assert.equal(calls.length,0)
  await flow.removal('series','p','主图');await flow.remove()
  assert.deepEqual(calls,[['delete','series','p']]);assert.equal(changes(),1);assert.match(flow.state.notice,/原素材和文件保留/)
})
test('blocked or mismatched impact never enables deletion, backend changed dependencies demand fresh preview',async c=>{
  const {flow,calls}=await setup(c,{removalImpact:async()=>({...impact,can_remove:false,variant_count:1})})
  await flow.removal('series','p','主图');await flow.remove();assert.equal(calls.length,0)
  const other=await setup(c,{removalImpact:async()=>({...impact,id:'other'})})
  await other.flow.removal('series','p','主图');assert.equal(other.flow.state.impact,null);assert.match(other.flow.state.error,/不一致/)
  const failed=await setup(c,{remove:async()=>{throw Error('新增依赖，请重读')}})
  await failed.flow.removal('series','p','主图');await failed.flow.remove()
  assert.equal(failed.flow.state.impact,null);assert.equal(failed.changes(),0);assert.match(failed.flow.state.error,/新增依赖/)
})
test('typed wrappers target original PATCH/DELETE and read-only option/impact actions',async c=>{
  const {vite}=await setup(c);const {api}=await vite.ssrLoadModule('/src/services/api.ts');const calls:any[]=[]
  api.get=async(...args:any[])=>{calls.push(['get',...args]);return {data:impact}}
  api.patch=async(...args:any[])=>{calls.push(['patch',...args]);return {data:series}}
  api.delete=async(...args:any[])=>{calls.push(['delete',...args]);return {}}
  const {assetReferenceApi:a}=await vite.ssrLoadModule('/src/services/assetReferenceApi.ts')
  await a.bibleOptions('p',{page:2,search:'x'});await a.updateSeries('p',{name:'x'})
  await a.removalImpact('version','v');await a.remove('series','p')
  assert.deepEqual(calls,[['get','/asset-reference-series/p/bible-options/',{params:{page:2,search:'x'}}],['patch','/asset-reference-series/p/',{name:'x'}],['get','/asset-reference-versions/v/removal-impact/'],['delete','/asset-reference-series/p/']])
})
test('production choice use independently blocks removal without pretending to be a generated dependency',async c=>{
  const {flow,calls,changes}=await setup(c,{removalImpact:async()=>({...impact,production_use_count:2,can_remove:false})})
  await flow.removal('series','p','主图');await flow.remove()
  assert.equal(flow.state.impact.production_use_count,2)
  assert.equal(flow.state.impact.dependent_version_count,0)
  assert.equal(flow.state.mode,'remove');assert.equal(changes(),0);assert.deepEqual(calls,[])
})
