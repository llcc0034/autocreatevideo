import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createRenderer,h,reactive,ssrContextKey,nextTick} from 'vue'

const input=()=>({key:'images',kind:'image',required:true,min_items:1,max_items:2,
  media_source_policy:{schema_version:1,allowed_sources:['asset_library','asset_upload'],default_source:'asset_library',allow_override:true,order:'provided'},
  semantic_contract:{allowed_purposes:[],allowed_scope_kinds:['request'],paired_with_input_key:null}})
const selection=()=>({source:'asset_library',items:[{item_key:'use-one',purpose:null,asset_id:'12345678-1234-1234-1234-123456789012'}]})
const catalog={schema_version:1,configuration_owner:'instance',orders:[{value:'provided',label:'提供顺序'}],sources:['asset_library','asset_upload','selected_output'].map(value=>({value,label:value,available:true,kinds:['image'],requires:[],reader:'asset',description:''}))}
async function modules(t:any){const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});t.after(()=>vite.close());return vite}
function setup(t:any,Component:any,props:any){
  let state:any;const events:any[]=[]
  const renderer=createRenderer({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit(event:string,...values:any[]){events.push([event,...values]);if(event==='update:modelValue')props.modelValue=values[0]}});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({});t.after(()=>app.unmount());return {state,events}
}
test('recipe fixed-media menu uses instance ownership and declared request scope',async t=>{
  const vite=await modules(t),m=await vite.ssrLoadModule('/src/services/productionRecipeAuthoring.ts')
  const options={sourceKindOptions:['media_selection','reviewed_references'].map(value=>({value,label:value}))}
  assert.deepEqual(m.recipeSourceKindOptions({},input(),options,[]).map((r:any)=>r.value),['media_selection','reviewed_references'])
  for(const spec of [{...input(),media_source_policy:null},{...input(),semantic_contract:{...input().semantic_contract,allowed_scope_kinds:['shot']}},{...input(),semantic_contract:{...input().semantic_contract,paired_with_input_key:'video'}}]){
    assert.deepEqual(m.recipeSourceKindOptions({},spec,options,[]).map((r:any)=>r.value),['reviewed_references'])
  }
})
test('recipe wrapper reports loading, invalid IDs, source changes and readonly without rewriting saved values',async t=>{
  const vite=await modules(t),api=await vite.ssrLoadModule('/src/services/inputSourcePolicies.ts')
  api.inputSourcePolicies.options=async()=>catalog
  const Component=(await vite.ssrLoadModule('/src/components/RecipeMediaSelectionEditor.vue')).default
  const props=reactive({modelValue:selection(),input:input(),disabled:false}),{state,events}=setup(t,Component,props)
  await nextTick();await nextTick()
  assert.deepEqual(state.issues.value,[]);assert.equal(events.filter(row=>row[0]==='update:modelValue').length,0)
  props.modelValue.items[0]!.asset_id='temporary-file';await nextTick();assert.ok(state.issues.value.length)
  props.modelValue=selection();props.modelValue.source='asset_upload';await nextTick();assert.deepEqual(state.issues.value,[])
  props.input.media_source_policy.allow_override=false;await nextTick();assert.match(state.issues.value.join(),/不允许/)
  props.disabled=true;state.update(selection());assert.equal(events.filter(row=>row[0]==='update:modelValue').length,0)
  state.busy.value=true;await nextTick();assert.match(state.issues.value.join(),/上传/)
})
test('shared input mode permits filling default even without override but rejects switching it',async t=>{
  const vite=await modules(t),Component=(await vite.ssrLoadModule('/src/components/MediaSourceSelectionEditor.vue')).default
  const props=reactive({modelValue:{source:'asset_library',items:[]},policy:{...input().media_source_policy,media_kind:'image',min_items:1,max_items:2,allowed_purposes:[],allow_override:false},catalog,mode:'input',supportedSources:['asset_library','asset_upload'],disabled:false})
  const {state}=setup(t,Component,props)
  assert.equal(state.editable.value,true);state.add();assert.equal(props.modelValue.items.length,1)
  state.changeSource('asset_upload');assert.equal(props.modelValue.source,'asset_library')
  props.modelValue.items=[];props.policy.allow_override=true;state.changeSource('asset_upload');assert.equal(props.modelValue.source,'asset_upload')
  props.disabled=true;state.add();assert.equal(props.modelValue.items.length,0)
})
test('catalog failures remain visible and successful retry reuses the same editor',async t=>{
  const vite=await modules(t),api=await vite.ssrLoadModule('/src/services/inputSourcePolicies.ts')
  api.inputSourcePolicies.options=async()=>{throw new Error('isolated catalog unavailable')}
  const Component=(await vite.ssrLoadModule('/src/components/RecipeMediaSelectionEditor.vue')).default
  const props=reactive({modelValue:selection(),input:input(),disabled:false}),{state,events}=setup(t,Component,props)
  await nextTick();await nextTick();assert.match(state.error.value,/isolated catalog unavailable/)
  api.inputSourcePolicies.options=async()=>catalog;await state.load();await nextTick();assert.deepEqual(state.issues.value,[])
  assert.equal(events.filter(row=>row[0]==='update:modelValue').length,0)
})
