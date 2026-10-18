import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'

const id='10000000-0000-4000-8000-000000000001'
const asset=(kind='image')=>({id,name:'原素材',mimeType:kind+'/fixture',checksum:'a'.repeat(64),isActive:true})
const version={id:'version-old',series_id:'uniform',version:1,asset:{id},source:'existing'}
async function setup(c:any){
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  c.after(()=>vite.close())
  return {...await vite.ssrLoadModule('/src/services/referenceInputSelection.ts'),
    ...await vite.ssrLoadModule('/src/services/inputSourcePolicies.ts')}
}

test('an old exact version resolves only its Asset, never the latest or a name match',async c=>{
  const {resolveReferenceInputAsset}=await setup(c),calls:any[]=[]
  const value=await resolveReferenceInputAsset({getVersion:async(key:string)=>{calls.push(['version',key]);return version},
    getAsset:async(key:string)=>{calls.push(['asset',key]);return asset()}},version,'image')
  assert.equal(value.id,id);assert.deepEqual(calls,[['version','version-old'],['asset',id]])
  assert.equal(version.version,1)
})
test('all three independent media types are eligible without Bible or series properties',async c=>{
  const {inputAssetCompatible,resolveReferenceInputAsset}=await setup(c)
  for(const kind of ['image','video','audio']){
    assert.equal(inputAssetCompatible(asset(kind),kind),true)
    const result=await resolveReferenceInputAsset({getVersion:async()=>version,getAsset:async()=>asset(kind)},version,kind)
    assert.equal(result.id,id)
  }
})
test('mismatched version id, series, number or original asset stops before reading another asset',async c=>{
  const {resolveReferenceInputAsset}=await setup(c)
  for(const changes of [{id:'different'},{series_id:'coat'},{version:2},{asset:{id:'different'}}]){
    await assert.rejects(()=>resolveReferenceInputAsset({getVersion:async()=>({...version,...changes}),
      getAsset:async()=>{assert.fail('must not read substituted asset')}},version,'image'),/关联已变化/)
  }
})
test('deleted or unauthorized version errors remain errors, without library or latest fallback',async c=>{
  const {resolveReferenceInputAsset}=await setup(c)
  const error={response:{status:404}}
  await assert.rejects(()=>resolveReferenceInputAsset({getVersion:async()=>{throw error},getAsset:async()=>{assert.fail()}},version,'image'),e=>e===error)
})
test('inactive, wrong type, absent digest and project cover do not become valid input',async c=>{
  const {resolveReferenceInputAsset,inputAssetCompatible}=await setup(c)
  for(const changes of [{isActive:false},{mimeType:'audio/wav'},{checksum:''},{metadata:{usage:'project_cover'}}]){
    const bad={...asset(),...changes};assert.equal(inputAssetCompatible(bad,'image'),false)
    await assert.rejects(()=>resolveReferenceInputAsset({getVersion:async()=>version,getAsset:async()=>bad},version,'image'),/不能|不符合|已失效/)
  }
})
test('asset detail identity mismatch and detail network failure cannot choose anything',async c=>{
  const {resolveReferenceInputAsset}=await setup(c)
  await assert.rejects(()=>resolveReferenceInputAsset({getVersion:async()=>version,getAsset:async()=>({...asset(),id:'other'})},version,'image'),/不符合/)
  await assert.rejects(()=>resolveReferenceInputAsset({getVersion:async()=>version,getAsset:async()=>{throw Error('offline')}},version,'image'),/offline/)
})
test('selection preserves configured purpose, stable item key and order through the M1 contract',async c=>{
  const {resolveReferenceInputAsset,selectedMediaSource}=await setup(c)
  const original={source:'asset_library',items:[{item_key:'first',purpose:'custom-purpose',asset_id:'10000000-0000-4000-8000-000000000002'}]}
  const chosen=await resolveReferenceInputAsset({getVersion:async()=>version,getAsset:async()=>asset()},version,'image')
  const input={...original,items:original.items.map(row=>({...row,asset_id:chosen.id}))}
  const result=selectedMediaSource({schema_version:1,media_kind:'image',allowed_sources:['asset_library'],default_source:'asset_library',allow_override:true,min_items:1,max_items:2,allowed_purposes:['custom-purpose'],order:'provided'},input)
  assert.deepEqual(result,input);assert.notEqual(original.items[0].asset_id,result.items[0].asset_id)
  assert.deepEqual(Object.keys(result.items[0]).sort(),['asset_id','item_key','purpose'])
  assert.equal(version.asset.id,id)
})
