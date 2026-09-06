import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('typed node waiting rules preserve the seconds contract and guarded catalog', async context => {
  const vite = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(() => vite.close())
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis,'localStorage')
  Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=>null}})
  context.after(()=>{if(originalStorage) Object.defineProperty(globalThis,'localStorage',originalStorage);else Reflect.deleteProperty(globalThis,'localStorage')})
  const service = await vite.ssrLoadModule('/src/services/modelNodeIdleOverrides.ts')
  const { default: Editor } = await vite.ssrLoadModule('/src/components/ModelNodeIdleOverridesEditor.vue')
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const limits = {maxSeconds:7200,maxOverrides:2}
  const sources = [
    {id:'one',name:'Comfy one',providerProfileId:'provider',providerDriver:'comfyui',status:'active'},
    {id:'two',name:'Comfy two',providerProfileId:'provider',providerDriver:'comfyui',status:'active'},
    {id:'other',providerProfileId:'elsewhere',providerDriver:'comfyui',status:'active'},
    {id:'disabled',providerProfileId:'provider',providerDriver:'comfyui',status:'disabled'},
    {id:'rest',providerProfileId:'provider',providerDriver:'async_rest',status:'active'},
  ]
  const catalog = {schemaVersion:2,totalCount:3,catalogSha256:'a'.repeat(64),items:[
    {classType:'SamplerCustomAdvanced',displayName:'高级采样',category:'sampling'},
    {classType:'LoadImage',displayName:'加载图片',category:'image'},
    {classType:'KSampler (Efficient)',displayName:'高效采样',category:'sampling'},
  ]}
  const renderer = createRenderer<any,any>({patchProp(){},insert(){},remove(){},createElement:()=>({}),createText:()=>({}),createComment:()=>({}),setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null})
  const mount = (value:unknown = {SamplerCustomAdvanced:5400}) => {
    const props = reactive<any>({modelValue:value,providerId:'provider',deployments:sources,limits,disabled:false})
    const updates:any[]=[]; let validity:string[]=[]; let state:any
    const app=renderer.createApp({setup(){state=Editor.setup(props,{expose(){},emit(event:string,value:any){if(event==='update:modelValue'){updates.push(value);props.modelValue=value}else if(event==='validity')validity=value}});return()=>h('div')}})
    app.provide(ssrContextKey,{})
    app.mount({});context.after(()=>{if(app._instance)app.unmount()})
    return {props,state,updates,validity:()=>validity,app}
  }
  await context.test('minute conversion preserves all second precision and never rounds invalid input',()=>{
    assert.equal(service.nodeIdleSeconds('90'),5400)
    assert.equal(service.nodeIdleSeconds('1.5'),90)
    for(const seconds of [1,59,61,3599,7200]) assert.equal(service.nodeIdleSeconds(String(seconds/60)),seconds)
    for(const raw of ['', '0','-1','Infinity','abc','0.3333','0x10','1e2']) assert.equal(service.nodeIdleSeconds(raw),null,raw)
    for(const absent of [undefined,null,{}]) assert.deepEqual(service.nodeIdleOverrideErrors(absent,null),[])
    assert.ok(service.nodeIdleOverrideErrors({A:1},null).length)
    assert.ok(service.nodeIdleOverrideErrors({A:7201},limits).length)
    assert.ok(service.nodeIdleOverrideErrors({A:1,B:1,C:1},limits).length)
    assert.ok(service.nodeIdleOverrideErrors({' A ':1,A:1},limits).some((e:string)=>e.includes('重复')))
    assert.ok(service.nodeIdleOverrideErrors({'bad\nnode':1},limits).length)
    assert.ok(service.nodeIdleOverrideErrors({A:true},limits).length)
  })
  await context.test('initial view is readonly until edited, has no JSON input and does not fetch automatically',async()=>{
    let calls=0;platformApi.getAllNodeMetadata=async()=>{calls++;return catalog}
    const fixture=mount()
    const html=await renderToString(createSSRApp({render:()=>h(Editor,fixture.props)}))
    assert.equal(calls,0);assert.equal(fixture.updates.length,0)
    for(const text of ['通常无需设置','等待时长（分钟）','将保存 5400 秒','尚未读取节点目录，原值保留','只读取节点类型'])assert.ok(html.includes(text),text)
    assert.doesNotMatch(html,/<textarea/)
    assert.deepEqual(fixture.state.sources.value.map((s:any)=>s.id),['one','two'])
  })
  await context.test('edits write seconds, reject duplicates and incomplete rows, and preserve raw JSON synchronization',async()=>{
    platformApi.getAllNodeMetadata=async()=>catalog
    const f=mount();f.state.sourceId.value='one';await nextTick();await f.state.loadCatalog()
    f.state.add();await nextTick();assert.ok(f.validity().length);assert.equal(f.updates.length,0)
    const row=f.state.rows.value[1]
    f.state.changeType(row.id,'SamplerCustomAdvanced');assert.equal(row.nodeType,'')
    f.state.changeType(row.id,'not-in-catalog');assert.equal(row.nodeType,'')
    f.state.changeType(row.id,'LoadImage');f.state.changeMinutes(row.id,'1.5');await nextTick()
    assert.deepEqual(f.props.modelValue,{SamplerCustomAdvanced:5400,LoadImage:90})
    assert.deepEqual(f.validity(),[])
    f.state.add();assert.equal(f.state.rows.value.length,2)
    f.state.changeMinutes(row.id,'');await nextTick();assert.ok(f.validity().length)
    assert.equal(f.props.modelValue.LoadImage,90)
    f.state.changeMinutes(row.id,'121');await nextTick();assert.ok(f.validity().some(e=>e.includes('上限')))
    f.state.changeMinutes(row.id,'2');await nextTick();assert.equal(f.props.modelValue.LoadImage,120)
    f.props.modelValue={OnlyInSavedContract:61};await nextTick();assert.equal(f.state.rows.value[0].nodeType,'OnlyInSavedContract')
    assert.equal(service.nodeIdleSeconds(f.state.rows.value[0].minutes),61)
    f.props.modelValue={SamplerCustomAdvanced:5400,LoadImage:120};await nextTick();assert.equal(f.state.rows.value.length,2)
    f.state.remove(f.state.rows.value[1].id);f.state.remove(f.state.rows.value[0].id);await nextTick()
    assert.deepEqual(f.props.modelValue,{});assert.deepEqual(f.validity(),[])
  })
  await context.test('stale and failed directory reads cannot overwrite current catalog or saved rules',async()=>{
    const pending:any[]=[];platformApi.getAllNodeMetadata=(id:string)=>new Promise((resolve,reject)=>pending.push({id,resolve,reject}))
    const f=mount();f.state.sourceId.value='one';await nextTick();const old=f.state.loadCatalog()
    f.state.sourceId.value='two';await nextTick();const current=f.state.loadCatalog()
    pending[1].resolve(catalog);await current;pending[0].resolve({...catalog,items:[]});await old
    assert.equal(f.state.catalog.value.items.length,3)
    const failed=f.state.loadCatalog();pending[2].reject(new Error('directory unavailable'));await failed
    assert.equal(f.state.catalog.value,null);assert.ok(f.state.loadError.value)
    assert.deepEqual(f.props.modelValue,{SamplerCustomAdvanced:5400});assert.equal(f.updates.length,0)
    f.props.providerId='elsewhere';await nextTick();assert.equal(f.state.sourceId.value,'');assert.equal(f.state.catalog.value,null)
  })
  await context.test('unmount and disabled transitions invalidate requests and editing; malformed data is never erased',async()=>{
    let resolve:any;platformApi.getAllNodeMetadata=()=>new Promise(done=>{resolve=done})
    const f=mount();f.state.sourceId.value='one';await nextTick();const request=f.state.loadCatalog()
    f.props.disabled=true;await nextTick();resolve(catalog);await request
    assert.equal(f.state.catalog.value,null)
    f.state.changeMinutes(f.state.rows.value[0].id,'1');f.state.remove(f.state.rows.value[0].id);f.state.add()
    assert.equal(f.updates.length,0)
    f.props.disabled=false;await nextTick();const afterUnmount=f.state.loadCatalog();f.app.unmount();resolve(catalog);await afterUnmount
    assert.equal(f.state.catalog.value,null)
    const bad=mount(['invalid']);bad.state.add();bad.state.remove(1);await nextTick();assert.ok(bad.validity().length);assert.deepEqual(bad.props.modelValue,['invalid'])
  })
  await context.test('no sources or no limits does not require optional rules and does not manufacture a catalog',async()=>{
    const f=mount(null);f.props.deployments=[];f.props.limits=null;await nextTick()
    f.state.add();assert.equal(f.state.rows.value.length,0);assert.deepEqual(f.validity(),[])
    const html=await renderToString(createSSRApp({render:()=>h(Editor,f.props)}))
    assert.match(html,/可先保持不设置/);assert.match(html,/未设置特殊规则/)
  })
  await context.test('registry saves the same map, validates upper limits, and replaces only the dedicated JSON field',async()=>{
    const schema=await vite.ssrLoadModule('/src/services/registryResourceSchema.ts')
    const deps={providers:[],modelProfiles:[],deployments:[],runtimeHosts:[],workflowVersions:[],executionMonitoringLimits:limits}
    const payload={modality:'video',specification:{execution_monitoring:{schema_version:1,node_type_overrides:{SamplerCustomAdvanced:5400}}}}
    const field=schema.resourceEditorFields('model',payload,deps).find((f:any)=>f.path==='specification.execution_monitoring.node_type_overrides')
    assert.equal(field.kind,'node-idle-overrides');assert.equal(field.advanced,true)
    const changed=schema.setPath(payload,field.path,{SamplerCustomAdvanced:90})
    assert.equal(changed.specification.execution_monitoring.node_type_overrides.SamplerCustomAdvanced,90)
    assert.equal(payload.specification.execution_monitoring.node_type_overrides.SamplerCustomAdvanced,5400)
    changed.specification.execution_monitoring.node_type_overrides.SamplerCustomAdvanced=7201
    assert.ok(schema.validateRegistryResource('model',changed,deps).errors.some((e:string)=>e.includes('服务端上限')))
    const source=await readFile(new URL('../src/components/RegistryResourceEditor.vue',import.meta.url),'utf8')
    assert.match(source,/ModelNodeIdleOverridesEditor v-if="field.kind==='node-idle-overrides'"/)
    assert.match(source,/errors.push\(\.\.\.nodeIdleErrors.values\)/)
    assert.match(source,/<details v-for="field in advancedFields"[^>]*>/)
    assert.doesNotMatch(source,/<details v-for="field in advancedFields"[^>]*\sopen[\s>]/)
    const editorSource=await readFile(new URL('../src/components/ModelNodeIdleOverridesEditor.vue',import.meta.url),'utf8')
    assert.match(editorSource,/<input type="text" inputmode="decimal"/)
    assert.doesNotMatch(editorSource,/<input type="number"/)
  })
})
