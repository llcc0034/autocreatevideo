import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'

const primary={id:'p', name:'主图', role:'primary', primary_series_id:null, bible_variant_id:null, version_count:2, variant_count:2}
const variant={...primary,id:'v', name:'校服',role:'variant',primary_series_id:'p',variant_count:0}
const second={...variant,id:'v2',name:'外套'}
const asset=(id:string,type='image')=>({id,name:id,type,mediaUrl:`fixture:${id}`,isActive:true})
const version=(id:string,series_id='v',number=1)=>({id,series_id,version:number,source:'existing',asset:{id:'a-'+id,name:id,mime_type:'image/png',is_active:true}})
const page=(rows:any[],next:string|null=null,previous:string|null=null)=>({count:rows.length,next,previous,results:rows})
const wait=()=>{let resolve!:(v:any)=>void;const promise=new Promise<any>(r=>{resolve=r});return {promise,resolve}}
async function setup(context:any, overrides:any={}) {
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const {createReferenceBrowser}=await vite.ssrLoadModule('/src/services/assetReferenceBrowser.ts')
  const calls:any[]=[]
  const api={listSeries:async(q:any)=>{calls.push(q);return page(q.primary_series_id?[variant,second]:[primary,variant,second])},
    getSeries:async(id:string)=>[primary,variant,second].find(row=>row.id===id),
    listVersions:async(q:any)=>{calls.push(q);return page([version(q.series_id+'-latest',q.series_id,2)])},
    getVersion:async(id:string)=>version(id,'p'),getAsset:async(id:string)=>asset(id),...overrides}
  return {vite,flow:createReferenceBrowser(api),calls}
}

test('filters go to server, detail shows exact primary/siblings without copying filter to tree',async c=>{
  const {flow,calls}=await setup(c)
  await flow.search({page:2,search:'校服',media_type:'image',bible_linked:false,role:'variant'})
  await flow.select(variant)
  assert.equal(flow.state.primary.id,'p');assert.deepEqual(flow.state.variants.results.map((v:any)=>v.id),['v','v2'])
  assert.deepEqual(calls[0],{page:2,search:'校服',media_type:'image',bible_linked:false,role:'variant'})
  assert.deepEqual(calls[1],{series_id:'v',page:1})
  assert.deepEqual(calls[2],{page:1,search:'',primary_series_id:'p'})
  assert.equal(flow.state.asset.id,'a-v-latest')
})
test('pagination uses exact series and preview does not mutate execution or adoption',async c=>{
  const {flow,calls}=await setup(c,{listVersions:async(q:any)=>{calls.push(q);return page([version('old',q.series_id,1)])}})
  await flow.select(variant);await flow.versions(2);await flow.relations(2)
  assert.deepEqual(calls.find((q:any)=>q.series_id&&q.page===2),{series_id:'v',page:2})
  assert.equal(flow.state.version.id,'old');assert.equal(flow.state.variantPage,2)
})
test('late catalog reply cannot replace newer filters',async c=>{
  const stale=wait();const {flow}=await setup(c,{listSeries:(q:any)=>q.search==='old'?stale.promise:Promise.resolve(page([second]))})
  const pending=flow.search({page:1,search:'old'});await flow.search({page:1,search:'new'})
  stale.resolve(page([primary]));await pending
  assert.equal(flow.state.catalog.results[0].id,'v2');assert.equal(flow.state.query.search,'new')
})
test('switching series ignores late media and clears prior preview immediately',async c=>{
  const stale=wait();const {flow}=await setup(c,{getAsset:(id:string)=>id==='a-v-latest'?stale.promise:Promise.resolve(asset(id))})
  const pending=flow.select(variant)
  await new Promise(r=>setTimeout(r,0));await flow.select(second)
  stale.resolve(asset('a-v-latest'));await pending
  assert.equal(flow.state.asset.id,'a-v2-latest');assert.equal(flow.state.version.series_id,'v2')
})
test('failed preview never falls back to another version and can reread same asset',async c=>{
  let fail=true;const {flow}=await setup(c,{getAsset:async(id:string)=>{if(fail)throw Error('missing');return asset(id)}})
  await flow.select(variant);assert.equal(flow.state.asset,null);assert.match(flow.state.previewError,/missing/)
  assert.equal(flow.state.version.id,'v-latest');fail=false;await flow.preview(flow.state.version)
  flow.mediaFailed('wrong');assert.equal(flow.state.previewError,'')
  flow.mediaFailed(flow.state.asset.id);assert.match(flow.state.previewError,/不会换成其他版本/)
  await flow.preview(flow.state.version);assert.equal(flow.state.previewError,'')
})
test('source navigation pins exact old primary version rather than latest',async c=>{
  const {flow}=await setup(c);await flow.openVersion('base-old')
  assert.equal(flow.state.selected.id,'p');assert.equal(flow.state.version.id,'base-old');assert.equal(flow.state.version.version,1)
  assert.equal(flow.state.versions.results[0].version,2);assert.equal(flow.state.asset.id,'a-base-old')
})
test('empty and failed relations/versions are distinct; query failure cannot keep stale detail',async c=>{
  const {flow}=await setup(c,{getSeries:async()=>{throw Error('关系不可读')},listVersions:async()=>page([])})
  await flow.select(variant);assert.match(flow.state.relationsError,/关系不可读/);assert.equal(flow.state.versionsError,'')
  assert.equal(flow.state.version,null)
  const other=await setup(c,{listSeries:async()=>{throw Error('查询失败')},listVersions:async()=>{throw Error('版本失败')}})
  await other.flow.select(primary);assert.match(other.flow.state.versionsError,/版本失败/)
  await other.flow.search();assert.equal(other.flow.state.selected,null);assert.match(other.flow.state.catalogError,/查询失败/)
})
test('dispose invalidates pending detail fetches; wrong asset identity is not displayed',async c=>{
  const pending=wait();const {flow}=await setup(c,{getVersion:()=>pending.promise})
  const task=flow.openVersion('base');flow.dispose();pending.resolve(version('base','p'));await task
  assert.equal(flow.state.selected,null)
  const other=await setup(c,{getAsset:async()=>asset('wrong')});await other.flow.select(primary)
  assert.equal(other.flow.state.asset,null);assert.match(other.flow.state.previewError,/不一致/)
})
test('image/video/audio original assets all pass through the same exact preview reader',async c=>{
  for(const type of ['image','video','audio']) {
    const {flow}=await setup(c,{getAsset:async(id:string)=>asset(id,type)})
    await flow.select(variant);assert.equal(flow.state.asset.type,type)
  }
})

test('browser API transports typed filters and exact version IDs through read-only endpoints',async c=>{
  const {vite}=await setup(c), {api}=await vite.ssrLoadModule('/src/services/api.ts')
  const calls:any[]=[]
  api.get=async(path:string,options:any)=>{calls.push([path,options]);return {data:{}}}
  const {assetReferenceApi:service}=await vite.ssrLoadModule('/src/services/assetReferenceApi.ts')
  await service.listSeries({page:2,search:'',media_type:'video',bible_linked:false})
  await service.getSeries('series-exact');await service.listVersions({series_id:'series-exact',page:3});await service.getVersion('version-exact')
  assert.deepEqual(calls,[['/asset-reference-series/',{params:{page:2,search:'',media_type:'video',bible_linked:false}}],
    ['/asset-reference-series/series-exact/',undefined],['/asset-reference-versions/',{params:{series_id:'series-exact',page:3}}],['/asset-reference-versions/version-exact/',undefined]])
})
