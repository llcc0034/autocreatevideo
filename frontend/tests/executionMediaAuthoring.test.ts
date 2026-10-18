import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createRenderer,h,reactive,ssrContextKey,nextTick} from 'vue'
import {mediaAssets,mediaContractRaw,mediaSources} from './fixtures/execution-media-authoring-data.ts'
async function mount(t:any){
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});t.after(()=>vite.close())
  const form=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
  const {inputSourcePolicies}=await vite.ssrLoadModule('/src/services/inputSourcePolicies.ts');inputSourcePolicies.options=async()=>mediaSources
  const Editor=(await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
  const contract=form.executionRequestFormContract(structuredClone(mediaContractRaw)),draft=form.emptyExecutionRequestDraft(contract)
  const props=reactive({contract,modelValue:draft,disabled:false});let state:any;const events:any[]=[]
  const renderer=createRenderer({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const Component={...Editor,setup(p:any,c:any){state=Editor.setup(p,c);return()=>h('div')}}
  const app=renderer.createApp({render:()=>h(Component,{...props,'onUpdate:modelValue':(v:any)=>props.modelValue=v,onBusy:(v:boolean)=>props.disabled=v,onValidity:(v:string[])=>events.push(v)})})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({});t.after(()=>app.unmount());await nextTick();await nextTick()
  return {state,props,events,form}
}
test('removing a precise video use preserves audio and displays unresolved pairing rather than silently rewiring',async t=>{
  const {state:s,props:p,events}=await mount(t),video=p.contract.inputs.find(r=>r.key==='clips'),audio=p.contract.inputs.find(r=>r.key==='paired')
  s.addMedia(video);s.addMedia(video);s.addMedia(audio)
  const [a,b]=p.modelValue.media.clips,child=p.modelValue.media.paired[0];a.assetId=mediaAssets[2].id;b.assetId=mediaAssets[2].id;child.assetId=mediaAssets[4].id;child.pairedTo=a.itemKey
  s.removeMedia('clips',a.itemKey);await nextTick();assert.match(s.pendingChange.value.message,/配套音轨第 1 项/);assert.ok(events.at(-1).length)
  s.addMedia(video);assert.equal(p.modelValue.media.clips.length,2)
  s.pendingChange.value=null;await nextTick();assert.equal(p.modelValue.media.clips.length,2);assert.deepEqual(events.at(-1),[])
  s.removeMedia('clips',a.itemKey);p.modelValue.media.clips.reverse();s.applyPendingChange()
  assert.equal(p.modelValue.media.clips[0].itemKey,b.itemKey);assert.equal(child.pairedTo,a.itemKey);assert.equal(child.assetId,mediaAssets[4].id);assert.equal(s.missingParent(audio,child.pairedTo),true)
})
test('source change and subject removal require inline confirmation and preserve unrelated selections',async t=>{
  const {state:s,props:p}=await mount(t),spec=p.contract.inputs[0]
  s.addMedia(spec);const row=p.modelValue.media.pictures[0];s.setAsset('pictures',row.itemKey,mediaAssets[0]);s.addSubject();const subject=p.modelValue.subjects[0];subject.name='林岚';row.subjectId=subject.id
  s.removeSubject(subject.id);assert.match(s.pendingChange.value.message,/解除 1 项/);s.applyPendingChange();assert.equal(row.assetId,mediaAssets[0].id);assert.equal(row.subjectId,null)
  const select={value:'asset_upload'};s.changeMediaMode(spec,{target:select});assert.equal(select.value,'asset_library');assert.equal(p.modelValue.media.pictures.length,1)
  s.pendingChange.value=null;s.changeMediaMode(spec,{target:{value:'asset_upload'}});s.applyPendingChange()
  assert.equal(p.modelValue.mediaSourceModes.pictures,'asset_upload');assert.equal(p.modelValue.media.pictures.length,0)
})
test('own upload busy feedback accepts the exact row receipt without unlocking unrelated rows',async t=>{
  const {state:s,props:p}=await mount(t);s.addMedia(p.contract.inputs[0]);s.addMedia(p.contract.inputs[0]);const [a,b]=p.modelValue.media.pictures
  s.inputBusy(`media:${a.itemKey}`,true);await nextTick();assert.equal(p.disabled,true)
  s.setAsset('pictures',b.itemKey,mediaAssets[1]);assert.equal(b.assetId,'')
  s.setAsset('pictures',a.itemKey,mediaAssets[0]);assert.equal(a.assetId,mediaAssets[0].id)
  s.inputBusy(`media:${a.itemKey}`,false);await nextTick();assert.equal(p.disabled,false)
})
