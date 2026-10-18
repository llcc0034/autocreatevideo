import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { readFile } from 'node:fs/promises'

const series = {id:'series-1',name:'沈念·校服',role:'variant',primary_series_id:'primary-1',bible_variant_id:null,owner_id:1,version_count:0,variant_count:0}
const asset = {id:'asset-1',name:'已上传主图',type:'image',mimeType:'image/png',isActive:true,checksum:'a'.repeat(64)}
const version = {id:'version-1',series_id:series.id,version:1,source:'upload',asset:{id:asset.id,name:asset.name,kind:'reference',mime_type:'image/png',is_active:true},generated_from_id:null,setting_revision_id:null}
async function modules(context:any) {
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  return {vite, flow:await vite.ssrLoadModule('/src/services/assetReferenceIngest.ts')}
}
const deferred = () => { let resolve!:(value:any)=>void;const promise=new Promise<any>(done=>{resolve=done});return {promise,resolve} }

test('uploaded asset joins exact series with no new upload or generated provenance',async context=>{
  const {flow}=await modules(context);const calls:any[]=[]
  const input=flow.createReferenceIngest({createSeries:async()=>{throw Error('must not create')},attach:async(value:any)=>{calls.push(value);return version}})
  input.selectSeries(series);input.selectAsset(asset,'asset_upload');await input.attach()
  assert.deepEqual(calls,[{series_id:series.id,asset_id:asset.id,source:'upload'}])
  assert.equal(input.state.result.id,version.id);assert.equal(input.state.asset.id,asset.id)
})

test('failed association preserves asset and target; retry only repeats idempotent relation',async context=>{
  const {flow}=await modules(context);let calls=0
  const input=flow.createReferenceIngest({attach:async()=>{if(++calls===1)throw Error('关联失败');return version}})
  input.selectSeries(series);input.selectAsset(asset,'asset_upload');await input.attach()
  assert.equal(input.state.result,null);assert.match(input.state.error,/无需重新上传/)
  assert.equal(input.state.asset.id,asset.id);assert.equal(input.state.target.id,series.id)
  await input.attach();assert.equal(calls,2);assert.equal(input.state.result.id,version.id)
})

test('library selection uses existing source and switching selection clears prior success',async context=>{
  const {flow}=await modules(context);let payload:any
  const input=flow.createReferenceIngest({attach:async(value:any)=>{payload=value;return version}})
  input.selectSeries(series);input.selectAsset(asset,'asset_library');await input.attach()
  assert.equal(payload.source,'existing')
  input.selectAsset({...asset,id:'asset-2'},'asset_library');assert.equal(input.state.result,null)
  input.selectSeries({...series,id:'series-2'});assert.equal(input.state.result,null)
})

test('duplicate submit and target changes while association is pending do not race',async context=>{
  const {flow}=await modules(context), wait=deferred();let count=0
  const input=flow.createReferenceIngest({attach:async()=>{count++;return wait.promise}})
  input.selectSeries(series);input.selectAsset(asset,'asset_upload');const first=input.attach()
  await input.attach();input.selectSeries({...series,id:'wrong'});input.selectAsset({...asset,id:'wrong'},'asset_library')
  assert.equal(count,1);assert.equal(input.state.target.id,series.id);assert.equal(input.state.asset.id,asset.id)
  wait.resolve(version);await first;assert.equal(input.state.busy,false)
})

test('uncertain series creation is not automatically retried; choosing existing resolves uncertainty',async context=>{
  const {flow}=await modules(context);let calls=0
  const input=flow.createReferenceIngest({createSeries:async()=>{calls++;throw Error('网络未返回')}})
  await input.create({name:'主图',role:'primary',primary_series_id:null});await input.create({name:'主图',role:'primary',primary_series_id:null})
  assert.equal(calls,1);assert.equal(input.state.creationUnknown,true);assert.match(input.state.error,/查询已有系列/)
  input.selectSeries(series);assert.equal(input.state.creationUnknown,false)
})

test('validation error may be corrected; creating variant passes exact parent with no Bible requirement',async context=>{
  const {flow}=await modules(context);const calls:any[]=[]
  const input=flow.createReferenceIngest({createSeries:async(value:any)=>{calls.push(value);if(calls.length===1)throw {response:{status:400,data:{detail:'name required'}}};return series}})
  const value={name:'校服',role:'variant',primary_series_id:'primary-1'}
  await input.create(value);assert.equal(input.state.creationUnknown,false)
  await input.create(value);assert.deepEqual(calls,[value,value]);assert.equal(input.state.target.id,series.id)
})

test('unmounted controller ignores late write response instead of presenting stale success',async context=>{
  const {flow}=await modules(context),wait=deferred()
  const input=flow.createReferenceIngest({attach:()=>wait.promise})
  input.selectSeries(series);input.selectAsset(asset,'asset_upload');const task=input.attach()
  input.dispose();wait.resolve(version);await task;assert.equal(input.state.result,null)
})

test('API uses server roles and paginated references without any generation endpoint',async context=>{
  const {vite}=await modules(context), {api}=await vite.ssrLoadModule('/src/services/api.ts')
  const calls:any[]=[]
  api.options=async(path:string)=>{calls.push(path);return {data:{actions:{POST:{role:{choices:[{value:'primary',display_name:'主图'}]}}}}}}
  api.get=async(path:string,params:any)=>{calls.push([path,params]);return {data:{count:1,next:null,previous:null,results:[series]}}}
  api.post=async(path:string,payload:any)=>{calls.push([path,payload]);return {data:path.includes('versions')?version:series}}
  const {assetReferenceApi:service}=await vite.ssrLoadModule('/src/services/assetReferenceApi.ts')
  assert.equal((await service.roles())[0].display_name,'主图')
  await service.listSeries({page:2,search:'校服',role:'primary'});await service.createSeries({name:'校服',role:'variant',primary_series_id:'primary-1'})
  await service.attach({series_id:series.id,asset_id:asset.id,source:'existing'})
  assert.match(JSON.stringify(calls),/"page":2/);assert.doesNotMatch(JSON.stringify(calls),/asset-generation|production-run|generated_from/)
  api.options=async()=>({data:{}});await assert.rejects(service.roles(),/角色选项未返回/)
})

test('ingest page is lazy in library and states upload is a relationship, not model generation',async context=>{
  const {vite}=await modules(context)
  const Component=(await vite.ssrLoadModule('/src/components/AssetReferenceIngest.vue')).default
  const html=await renderToString(createSSRApp({render:()=>h(Component)}))
  assert.match(html,/主图与变体入库/);assert.match(html,/系列关联不会生成图片/)
  assert.match(html,/查询并选择确切系列/);assert.doesNotMatch(html,/已加入 .*版本/)
  const view=await readFile(new URL('../src/views/AssetLibraryView.vue',import.meta.url),'utf8')
  assert.match(view,/<AssetReferenceIngest v-if="showReferenceIngest"/)
})
