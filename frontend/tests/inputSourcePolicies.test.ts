import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createRenderer,createSSRApp,h,reactive,ssrContextKey,nextTick} from 'vue'
import {renderToString} from 'vue/server-renderer'
import * as Vue from 'vue'
import {readFileSync} from 'node:fs'
import {parse,compileTemplate} from '@vue/compiler-sfc'
import ts from 'typescript'

const id='12345678-1234-1234-1234-123456789012'
const asset={id,name:'isolated image',isActive:true,checksum:'a'.repeat(64),mimeType:'image/png',metadata:{}}
const policy=(changes={})=>({schema_version:1,media_kind:'image',allowed_sources:['asset_library','asset_upload'],default_source:'asset_library',allow_override:true,min_items:1,max_items:2,allowed_purposes:['appearance','motion'],order:'provided',...changes})
const selection=(changes={})=>({source:'asset_library',items:[{item_key:'use-1',purpose:'appearance',asset_id:id}],...changes})
const catalog={schema_version:1,configuration_owner:'recipe_instance_or_profile',description:'隔离目录',orders:[{value:'provided',label:'提供顺序'}],sources:[
  {value:'asset_library',label:'素材库已有素材',kinds:['image'],available:true,reader:'asset',requires:['owner'],description:'明确Asset'},
  {value:'asset_upload',label:'本地上传',kinds:['image'],available:true,reader:'asset',requires:['owner'],description:'上传后Asset'},
  {value:'selected_output',label:'已选择产物',kinds:['image'],available:true,reader:'selected_output',requires:['production_run'],description:'确切产物'},
  {value:'bible_reference',label:'Bible主图/变体',kinds:['image'],available:false,reader:null,requires:['asset_reference_version'],description:'尚未接入新版本'},
]}
async function modules(t:any){
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  t.after(()=>vite.close())
  return {vite,service:await vite.ssrLoadModule('/src/services/inputSourcePolicies.ts')}
}
async function setup(t:any,Component:any,props:any){
  let state:any;const events:any[]=[]
  const renderer=createRenderer({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit(event:string,...values:any[]){events.push([event,...values]);if(event==='update:modelValue')props.modelValue=values[0]}});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({});t.after(()=>app.unmount())
  return {state,events,unmount:()=>app.unmount()}
}

test('source policies require explicit available choice and preserve empty/override semantics',async t=>{
  const {service:s}=await modules(t)
  assert.deepEqual(s.sourcePolicyErrors(policy(),catalog),[])
  for(const change of [{allowed_sources:[]},{default_source:'missing'},{allowed_sources:['bible_reference']},{min_items:NaN},{min_items:3},{allow_override:null},{order:'name'}])assert.ok(s.sourcePolicyErrors(policy(change),catalog).length)
  assert.throws(()=>s.selectedMediaSource(policy(),null),/不会自动/)
  assert.throws(()=>s.selectedMediaSource(policy({min_items:NaN}),selection()),/数量上下限/)
  assert.throws(()=>s.selectedMediaSource(policy({allow_override:false}),selection(),selection()),/不允许/)
  assert.throws(()=>s.selectedMediaSource(policy(),selection(),null),/尚未/)
  assert.deepEqual(s.selectedMediaSource(policy({min_items:0}),selection({items:[]})).items,[])
  const alternate=selection({source:'asset_upload'})
  assert.throws(()=>s.selectedMediaSource(policy(),alternate),/默认来源/)
  assert.deepEqual(s.selectedMediaSource(policy(),selection(),alternate),alternate)
  for(const item of [{item_key:'x',purpose:null,asset_id:id},{item_key:'x',purpose:'motion',asset_id:'temp.png'},{item_key:'x',purpose:'motion',asset_id:id,decision_id:id}])assert.throws(()=>s.selectedMediaSource(policy(),selection({items:[item]})))
})

test('reordering keeps exact usage identities and distinct uses of same Asset',async t=>{
  const {service:s}=await modules(t)
  const before=selection({items:[{item_key:'z',purpose:'appearance',asset_id:id},{item_key:'a',purpose:'motion',asset_id:id}]})
  const after=s.moveMediaItem(before,1,-1)
  assert.deepEqual(after.items.map((r:any)=>r.item_key),['a','z'])
  assert.deepEqual(before.items.map(r=>r.item_key),['z','a'])
  assert.deepEqual(s.selectedMediaSource(policy(),after),after)
})

test('configuration editor preserves invalid original choices and exposes unavailable modules',async t=>{
  const {vite}=await modules(t),Editor=(await vite.ssrLoadModule('/src/components/InputSourcePolicyEditor.vue')).default
  const props=reactive({modelValue:policy(),catalog,purposes:[{value:'appearance',label:'外观'},{value:'motion',label:'动作'}]})
  const {state,events}=await setup(t,Editor,props)
  assert.equal(events.length,0)
  state.source('asset_library',false)
  assert.equal(props.modelValue.default_source,'asset_library')
  assert.ok(state.errors.value.length)
  state.quantity('min_items','');assert.ok(Number.isNaN(props.modelValue.min_items))
  const html=await renderToString(createSSRApp({render:()=>h(Editor,{...props,modelValue:policy({allowed_sources:['obsolete'],default_source:'obsolete'})})}))
  assert.match(html,/未登记的原来源：obsolete/);assert.match(html,/尚未接入新版本/)
  assert.match(html,/disabled/)
  props.modelValue=policy({allowed_sources:['bible_reference'],default_source:'bible_reference'})
  const unavailable=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  const disabledSourceInput=unavailable.match(/<input[^>]*>(?=<span[^>]*>Bible主图\/变体)/)?.[0]||''
  assert.match(disabledSourceInput,/checked/);assert.doesNotMatch(disabledSourceInput,/disabled/)
  assert.match(unavailable,/原默认来源不可用：bible_reference/)
  state.source('bible_reference',false);assert.deepEqual(props.modelValue.allowed_sources,[])
  state.source('bible_reference',true);assert.deepEqual(props.modelValue.allowed_sources,[])
})

test('selection control does not initialize first option and locks all request changes when configured',async t=>{
  const {vite}=await modules(t),Editor=(await vite.ssrLoadModule('/src/components/MediaSourceSelectionEditor.vue')).default
  const props=reactive({modelValue:null as any,policy:policy(),catalog,mode:'configure',supportedSources:['asset_library','asset_upload']})
  const {state,events}=await setup(t,Editor,props)
  assert.equal(events.length,0);state.changeSource('selected_output');assert.equal(props.modelValue,null)
  state.changeSource('asset_library');state.add();assert.equal(props.modelValue.items.length,1)
  state.select(props.modelValue.items[0].item_key,'asset_library','image',asset)
  assert.equal(props.modelValue.items[0].asset_id,id)
  const original=JSON.parse(JSON.stringify(props.modelValue))
  state.select(props.modelValue.items[0].item_key,'asset_upload','image',{...asset,id:'wrong'})
  assert.deepEqual(props.modelValue,original)
  props.mode='request';props.policy=policy({allow_override:false});await nextTick()
  state.add();state.remove(original.items[0].item_key);state.edit(original.items[0].item_key,{asset_id:'wrong'})
  assert.deepEqual(props.modelValue,original)
})

test('library-only and upload-only picker modes call the existing API and never substitute sources',async t=>{
  const {vite}=await modules(t),Picker=(await vite.ssrLoadModule('/src/components/ExecutionInputAssetPicker.vue')).default
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
  let lists=0,uploads=0
  repository.getAssetPage=async()=>{lists++;return {count:0,next:null,previous:null,results:[]}}
  repository.createAsset=async()=>{uploads++;return asset}
  const props=reactive({kind:'image',allowLibrary:false,allowUpload:true,disabled:false})
  const {state,events}=await setup(t,Picker,props)
  state.show();await nextTick();assert.equal(lists,0)
  state.select(asset);assert.equal(events.filter(e=>e[0]==='selected').length,0)
  state.file.value={name:'fixture.png'};state.name.value='fixture';await state.upload()
  assert.equal(uploads,1);assert.deepEqual(events.find(e=>e[0]==='selected'),['selected',asset,'asset_upload'])
  props.allowLibrary=true;props.allowUpload=false;await nextTick()
  state.file.value={name:'fixture.png'};state.name.value='fixture';await state.upload();assert.equal(uploads,1)
  state.select(asset);assert.equal(events.filter(e=>e[0]==='selected').at(-1)?.[2],'asset_library')
})

test('late uploads after type or source permission change never fill the new selection',async t=>{
  const {vite}=await modules(t),Picker=(await vite.ssrLoadModule('/src/components/ExecutionInputAssetPicker.vue')).default
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
  let finish:any;repository.createAsset=()=>new Promise(resolve=>finish=resolve)
  for(const change of [{kind:'video'},{allowUpload:false},{allowLibrary:true},{purpose:'new-purpose'}]){
    const props=reactive({kind:'image',allowLibrary:false,allowUpload:true,disabled:false})
    const {state,events}=await setup(t,Picker,props)
    state.file.value={name:'fixture.png'};state.name.value='fixture';const request=state.upload()
    Object.assign(props,change);await nextTick();finish(asset);await request
    assert.equal(events.filter(e=>e[0]==='selected').length,0)
    assert.equal(state.saving.value,false)
  }
})

test('failed upload keeps explicit error and does not retry or choose a library asset',async t=>{
  const {vite}=await modules(t),Picker=(await vite.ssrLoadModule('/src/components/ExecutionInputAssetPicker.vue')).default
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
  let calls=0;repository.createAsset=async()=>{calls++;throw new Error('isolated upload failure')}
  const {state,events}=await setup(t,Picker,reactive({kind:'image',allowLibrary:true,allowUpload:true,disabled:false}))
  state.file.value={name:'fixture.png'};state.name.value='fixture';await state.upload();await state.upload()
  assert.equal(calls,1);assert.equal(events.filter(e=>e[0]==='selected').length,0)
  assert.match(state.uploadErrorDetail.value,/isolated upload failure/);assert.match(state.error.value,/不要重复提交/);assert.equal(state.uploadUnknown.value,true)
})

test('unknown upload stays locked across file, purpose and panel changes; definitive rejection is editable',async t=>{
  const {vite}=await modules(t),Picker=(await vite.ssrLoadModule('/src/components/ExecutionInputAssetPicker.vue')).default
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
  let calls=0;repository.createAsset=async()=>{calls++;throw {response:{status:504}}}
  const props=reactive({kind:'image',purpose:null,allowLibrary:true,allowUpload:true,disabled:false})
  const {state:s}=await setup(t,Picker,props)
  s.file.value={name:'original.png'};s.name.value='原图';await s.upload()
  s.chooseFile({target:{files:[{name:'another.png'}]}});assert.equal(s.file.value.name,'original.png')
  s.close();props.purpose='another';await nextTick()
  assert.equal(s.uploadUnknown.value,true);assert.match(s.uploadNotice.value,/不会再次上传/)
  s.file.value={name:'different.png'};s.name.value='different';await s.upload();assert.equal(calls,1)
  const {state:rejected}=await setup(t,Picker,reactive({...props}))
  repository.createAsset=async()=>{calls++;throw {isAxiosError:true,response:{status:400,data:{detail:'invalid name'}}}}
  rejected.file.value={name:'fix.png'};rejected.name.value='fix';await rejected.upload()
  assert.equal(rejected.uploadUnknown.value,false);assert.equal(rejected.file.value.name,'fix.png')
  assert.match(rejected.error.value,/明确拒绝/)
  repository.createAsset=async()=>{calls++;return asset};await rejected.upload();assert.equal(calls,3)
})

test('picker pagination uses the submitted query including return to page one and rejects stale selection on read failure',async t=>{
  const {vite}=await modules(t),Picker=(await vite.ssrLoadModule('/src/components/ExecutionInputAssetPicker.vue')).default
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts');const calls:any[]=[]
  repository.getAssetPage=async(page,query)=>{calls.push([page,query]);return {count:4,next:'next',previous:'previous',results:[asset]}}
  const {state:s,events}=await setup(t,Picker,reactive({kind:'image',allowLibrary:true,allowUpload:true,disabled:false}))
  s.search.value='原查询';await s.load(1);s.search.value='未提交';await s.load(2,true);await s.load(1,true)
  assert.deepEqual(calls,[[1,'原查询'],[2,'原查询'],[1,'原查询']]);assert.equal(s.appliedSearch.value,'原查询')
  await s.load(1);assert.deepEqual(calls.at(-1),[1,'未提交'])
  repository.getAssetPage=async()=>{throw Error('read failed')};await s.load(1);s.select(asset)
  assert.equal(events.filter(e=>e[0]==='selected').length,0);assert.equal(s.result.value.results[0].id,asset.id)
})

test('upload supersedes directory loading without leaving it busy and keeps late directory results out',async t=>{
  const {vite}=await modules(t),Picker=(await vite.ssrLoadModule('/src/components/ExecutionInputAssetPicker.vue')).default
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts');let finish:any
  repository.getAssetPage=()=>new Promise(resolve=>finish=resolve)
  repository.createAsset=async()=>{throw {response:{status:400}}}
  const {state:s}=await setup(t,Picker,reactive({kind:'image',allowLibrary:true,allowUpload:true,disabled:false}))
  const pending=s.load(1);assert.equal(s.loading.value,true)
  s.file.value={name:'fixture.png'};s.name.value='fixture';await s.upload()
  assert.equal(s.loading.value,false);assert.equal(s.saving.value,false)
  finish({count:1,results:[asset]});await pending;assert.equal(s.result.value,null)
})

test('mounted parent preserves busy uploads, reports failures and rejects changed usage policy',async t=>{
  const {vite}=await modules(t),Editor=(await vite.ssrLoadModule('/src/components/MediaSourceSelectionEditor.vue')).default
  const Picker=(await vite.ssrLoadModule('/src/components/ExecutionInputAssetPicker.vue')).default
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
  // Compile the actual parent template (including its v-if), not a recreated harness branch.
  const filename=new URL('../src/components/MediaSourceSelectionEditor.vue',import.meta.url)
  const {descriptor}=parse(readFileSync(filename,'utf8'))
  const compiled=compileTemplate({source:descriptor.template!.content,filename:filename.pathname,id:'mounted-source-test',compilerOptions:{expressionPlugins:['typescript']}})
  assert.deepEqual(compiled.errors,[])
  const exports:any={}
  new Function('require','exports',ts.transpileModule(compiled.code,{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText)(()=>Vue,exports)
  for(const outcome of ['success','failure','changed-policy']){
    const failed=outcome==='failure',changed=outcome==='changed-policy'
    let finish:any,reject:any,pickerState:any,editorState:any,mounts=0
    repository.createAsset=()=>new Promise((resolve,fail)=>{finish=resolve;reject=fail})
    const props=reactive({modelValue:selection({source:'asset_upload',items:[{item_key:'upload-use',purpose:'appearance',asset_id:''}]}),policy:policy(),catalog,mode:'request',supportedSources:['asset_library','asset_upload'],disabled:false})
    const NestedPicker={...Picker,ssrRender:undefined,render:()=>h('div'),setup(p:any,c:any){mounts++;pickerState=Picker.setup(p,c);return pickerState}}
    const MountedEditor={...Editor,ssrRender:undefined,render:exports.render,components:{ExecutionInputAssetPicker:NestedPicker,StructureMediaValue:()=>h('div')},setup(p:any,c:any){editorState=Editor.setup(p,c);return {...editorState}}}
    const renderer=createRenderer({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    const app=renderer.createApp({render:()=>h(MountedEditor,{...props,'onUpdate:modelValue':(value:any)=>props.modelValue=value,onBusy:(value:boolean)=>props.disabled=value})})
    app.provide(ssrContextKey,{modules:new Set()});app.mount({})
    try{
      pickerState.file.value={name:'fixture.png'};pickerState.name.value='fixture'
      const original=pickerState,request=pickerState.upload();await nextTick()
      assert.equal(props.disabled,true);assert.equal(mounts,1)
      if(changed){props.policy=policy({allowed_purposes:['motion']});await nextTick()}
      if(failed)reject(new Error('isolated delayed failure'));else finish(asset)
      await request;await nextTick()
      assert.equal(props.disabled,false);assert.equal(mounts,1);assert.equal(pickerState,original)
      assert.equal(props.modelValue.items[0].asset_id,failed||changed?'':id)
      if(failed)assert.match(pickerState.uploadErrorDetail.value,/isolated delayed failure/)
      if(changed)assert.match(editorState.uploadNotice.value,/已入库.*未回填/)
    }finally{app.unmount()}
  }
})

test('series selection only emits the exact Asset once; unmount and type changes discard late reads',async t=>{
  const {vite}=await modules(t),Browser=(await vite.ssrLoadModule('/src/components/AssetReferenceBrowser.vue')).default
  const {assetReferenceApi}=await vite.ssrLoadModule('/src/services/assetReferenceApi.ts')
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
  assetReferenceApi.roles=async()=>[]
  assetReferenceApi.listSeries=async()=>({count:0,next:null,previous:null,results:[]})
  repository.getAsset=async()=>asset
  for(const outcome of ['success','unmount','kind-change','version-change']){
    let finish:any,calls=0
    const version={id:'v1',series_id:'series',version:1,asset:{id}}
    assetReferenceApi.getVersion=()=>{calls++;return new Promise(resolve=>finish=resolve)}
    const props=reactive({selectionKind:'image',disabled:false})
    const mounted=await setup(t,Browser,props),s=mounted.state
    const series={id:'series',role:'primary'}
    await nextTick();s.state.selected=series;s.state.version=version;s.state.asset=asset
    const pending=s.choose();await s.choose();assert.equal(calls,1)
    if(outcome==='unmount')mounted.unmount()
    if(outcome==='kind-change')props.selectionKind='video'
    if(outcome==='version-change')s.state.version={...version,id:'v2'}
    await nextTick();finish(version);await pending
    const selected=mounted.events.filter(e=>e[0]==='selected')
    assert.equal(selected.length,outcome==='success'?1:0)
    if(selected.length)assert.deepEqual(selected[0],['selected',asset,{version,series}])
    assert.equal(mounted.events.filter(e=>e[0]==='busy').at(-1)?.[1],false)
  }
})

test('structure media selections cannot overwrite a field changed during the request',async t=>{
  const {vite}=await modules(t),Editor=(await vite.ssrLoadModule('/src/components/StructureMediaValue.vue')).default
  const props=reactive({modelValue:{asset_id:id},schemas:[{'x-media-types':['image'],'x-media-usage':'用途'}],label:'素材',selectable:true,disabled:false,readonly:false})
  const {state,events}=await setup(t,Editor,props)
  state.selectionBusy(true);props.modelValue={asset_id:'another'};await nextTick();state.select(asset)
  assert.equal(events.filter(e=>e[0]==='update:modelValue').length,0);assert.match(state.error.value,/未覆盖/)
  state.selectionBusy(false);state.select(asset)
  assert.deepEqual(props.modelValue,{asset_id:id})
})
