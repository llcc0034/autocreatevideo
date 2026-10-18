import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
import {createRenderer, createSSRApp, nextTick, reactive, ssrContextKey} from 'vue'
import {renderToString} from 'vue/server-renderer'

const root=fileURLToPath(new URL('..',import.meta.url)),sha='a'.repeat(64)
const id=(n:number)=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`
const asset={id:id(1),name:'旅人雨衣',type:'image',mimeType:'image/png',checksum:sha,isActive:true,mediaUrl:'/isolated.png'}
const version={id:id(2),series_id:id(3),version:2,source:'generated',asset:{id:asset.id},setting_revision_id:null}
const series={id:id(3),name:'旅人服装',role:'variant'}
const selection={assetId:asset.id,assetSha256:sha,mediaKind:'image',referenceVersionId:version.id,version:2,seriesId:series.id,acquisitionSource:'generated'}
const api=()=>({getAsset:async()=>({...asset}),getVersion:async()=>structuredClone(version),getSeries:async()=>({...series})})
async function modules(fn:any){const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try{await fn(vite,await vite.ssrLoadModule('/src/services/exactReferencePreview.ts'))}finally{await vite.close()}}
const settle=async()=>{await new Promise(resolve=>setTimeout(resolve,0));await nextTick()}
test('task media preview uses frozen asset hash and exact declared reference version only',()=>modules(async(_v,m)=>{
  const item={asset_id:asset.id,source_bytes_sha256:sha,kind:'image',reference_binding:{shot_reference:null}}
  assert.deepEqual(m.taskMediaSelection(item),{assetId:asset.id,assetSha256:sha,mediaKind:'image',referenceVersionId:null})
  assert.equal(m.taskMediaSelection({...item,asset_id:null}),null)
  assert.equal(m.taskMediaSelection({...item,kind:'text'}),null)
  item.reference_binding.shot_reference={source:{kind:'reference_version',reference_version_id:version.id,series_id:series.id,version:2,acquisition_source:'generated'}}
  assert.deepEqual(m.taskMediaSelection(item),selection)
}))
function mount(Component:any,props:any){let state:any
  const renderer=createRenderer({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){}});return()=>null}})
  app.provide(ssrContextKey,{});app.mount({});return {state,unmount:()=>app.unmount()}}

test('exact preview preserves original version and shows series name, never latest',()=>modules(async(_v,m)=>{
  const calls:any[]=[],a=api()
  for(const key of Object.keys(a)){const read=a[key];a[key]=async id=>{calls.push([key,id]);return read()}}
  const value=await m.readExactReferencePreview(a,selection)
  assert.equal(value.label,'旅人服装 · 版本 2 · 旅人雨衣')
  assert.deepEqual(calls,[['getVersion',version.id],['getAsset',asset.id],['getSeries',series.id]])
}))
test('wrong version, series, source or bound asset cannot show another version as the observed selection',()=>modules(async(_v,m)=>{
  for(const patch of [{id:id(9)},{version:3},{series_id:id(9)},{source:'upload'},{asset:{id:id(9)}}]){
    const a=api();let assets=0;a.getVersion=async()=>({...version,...patch});a.getAsset=async()=>{assets++;return asset}
    await assert.rejects(m.readExactReferencePreview(a,selection),/不一致/);assert.equal(assets,0)
  }
  const missing=api();missing.getVersion=async()=>null
  await assert.rejects(m.readExactReferencePreview(missing,selection),/不一致/)
  const absentSeries=api();absentSeries.getSeries=async()=>null
  await assert.rejects(m.readExactReferencePreview(absentSeries,selection),/系列身份/)
}))
test('inactive, wrong kind, wrong hash, missing asset and denied reads stay failures',()=>modules(async(_v,m)=>{
  for(const patch of [{isActive:false},{mimeType:'video/mp4',type:'video'},{checksum:'b'.repeat(64)},{id:id(9)}]){
    const a=api();a.getAsset=async()=>({...asset,...patch});await assert.rejects(m.readExactReferencePreview(a,selection),/失效|指纹/)
  }
  const a=api();a.getAsset=async()=>{throw Error('403 denied')};await assert.rejects(m.readExactReferencePreview(a,selection),/403 denied/)
  await assert.rejects(m.readExactReferencePreview(api(),{...selection,referenceVersionId:null,assetId:null}),/身份/)
  await assert.rejects(m.readExactReferencePreview(api(),{...selection,mediaKind:'script'}),/类型/)
}))
test('direct asset needs no series and does not fabricate a version',()=>modules(async(_v,m)=>{
  const a=api();a.getVersion=async()=>{throw Error('unexpected')};a.getSeries=a.getVersion
  const value=await m.readExactReferencePreview(a,{assetId:asset.id,assetSha256:sha,mediaKind:'image',referenceVersionId:null})
  assert.equal(value.version,null);assert.equal(value.series,null);assert.equal(value.label,asset.name)
}))
async function component(vite:any,fn:any){const {assetReferenceApi}=await vite.ssrLoadModule('/src/services/assetReferenceApi.ts')
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
  const old=[assetReferenceApi.getVersion,assetReferenceApi.getSeries,repository.getAsset],a=api()
  assetReferenceApi.getVersion=a.getVersion;assetReferenceApi.getSeries=a.getSeries;repository.getAsset=a.getAsset
  try{await fn((await vite.ssrLoadModule('/src/components/ExactReferencePreview.vue')).default,repository)}
  finally{[assetReferenceApi.getVersion,assetReferenceApi.getSeries,repository.getAsset]=old}}

test('mounted preview is lazy; close and changed identity discard pending reads',()=>modules(async(vite)=>component(vite,async(C,repository)=>{
  let calls=0;const finish:any[]=[];repository.getAsset=()=>{calls++;return new Promise(resolve=>finish.push(resolve))}
  const props=reactive({selection:{...selection}}),view=mount(C,props)
  try{await settle();assert.equal(calls,0);const read=view.state.read();await settle();assert.equal(calls,1)
    view.state.close();finish[0](asset);await read;assert.equal(view.state.result.value,null)
    const second=view.state.read();await settle();props.selection={...selection,assetSha256:'b'.repeat(64)};await nextTick()
    finish[1](asset);await second;assert.equal(view.state.opened.value,false);assert.equal(view.state.result.value,null)
  }finally{view.unmount()}
})))
test('failed preview can reread original identity; success does not approve or select',()=>modules(async(vite)=>component(vite,async(C,repository)=>{
  repository.getAsset=async()=>{throw Error('404 original missing')};const view=mount(C,reactive({selection:{...selection}}))
  try{await view.state.read();assert.match(view.state.error.value,/original missing/);assert.equal(view.state.result.value,null)
    repository.getAsset=api().getAsset;await view.state.read();assert.equal(view.state.error.value,'');assert.equal(view.state.result.value.asset.id,asset.id)
    view.state.mediaError.value=true;view.state.close();assert.equal(view.state.mediaError.value,false)
  }finally{view.unmount()}
})))
test('missing media address stays explicit and unmount prevents late repaint',()=>modules(async(vite)=>component(vite,async(C,repository)=>{
  repository.getAsset=async()=>({...asset,mediaUrl:''});const view=mount(C,reactive({selection:{...selection}}))
  await view.state.read();assert.match(view.state.error.value,/预览地址/)
  let finish:any;repository.getAsset=()=>new Promise(resolve=>finish=resolve)
  const pending=view.state.read();await settle();view.unmount();finish(asset);await pending;assert.equal(view.state.result.value,null)
})))
test('ready readonly markup names the exact variant and shows media failure without exposing IDs as primary text',()=>modules(async(vite,m)=>component(vite,async(C)=>{
  const value=await m.readExactReferencePreview(api(),selection)
  const html=await renderToString(createSSRApp({...C,setup(p,c){const s=C.setup(p,c);s.opened.value=true;s.result.value=value;s.mediaError.value=true;return s}},{selection}))
  for(const text of ['旅人服装 · 版本 2 · 旅人雨衣','变体','模型生成','原素材预览不可读取','只读预览','原选择身份与文件指纹'])assert.ok(html.includes(text),text)
  assert.doesNotMatch(html,/<select|<input|确认采用|批准/)
})))
test('manual review preview uses canonical exact use, not direct-asset projection or live shot',()=>modules(async(vite)=>{
  const {manualStoryboardReferencePreview:read}=await vite.ssrLoadModule('/src/services/productionManualStoryboardReview.ts')
  const item={reference_key:'shot-use:raincoat',kind:'image',purpose:'外观参考',source:{asset_id:asset.id}}
  const use={use_key:'raincoat',media_kind:'image',purpose:item.purpose,source_mode:'existing',reference_version_id:version.id,asset_id:null,asset_sha256:sha}
  const detail={contract:{assets:[{id:asset.id,checksum:sha}],canonical:{episodes:[{shots:[{id:id(4),reference_uses:[use]}]}]}}}
  const original=JSON.stringify(detail),value=read(detail,id(4),item)
  assert.equal(value.selection.referenceVersionId,version.id);assert.equal(value.selection.assetId,asset.id);assert.equal(value.sourceLabel,'已有素材')
  assert.equal(JSON.stringify(detail),original)
  for(const patch of [{asset_sha256:'b'.repeat(64)},{purpose:'changed'},{reference_version_id:'bad'},{asset_id:id(8)}]){
    const changed=structuredClone(detail);Object.assign(changed.contract.canonical.episodes[0].shots[0].reference_uses[0],patch)
    assert.equal(read(changed,id(4),item).selection,null)
  }
  const missing=structuredClone(detail);missing.contract.canonical.episodes[0].shots[0].reference_uses=[]
  assert.match(read(missing,id(4),item).error,/缺少此独立用途/)
}))
