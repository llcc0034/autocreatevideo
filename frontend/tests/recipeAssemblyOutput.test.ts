import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
import {createRenderer,h,nextTick,reactive,ssrContextKey} from 'vue'

const root=fileURLToPath(new URL('..',import.meta.url)), sha='a'.repeat(64)
const definition={type:'object',properties:{w:{type:'integer'},h:{type:'integer'},f:{type:'integer'}}}
const target={type:'object',properties:{width:{type:'integer'},height:{type:'integer'},fps:{type:'integer'}}}
const initial=()=>({output_version_id:'output-one',expected_contract_sha256:sha,source_text:' {"w":640,"h":360,"f":30} ',mapping:{schema_version:1,node:{kind:'object',fields:{}}}})
async function component(work:(ctx:any)=>Promise<void>){
  const vite=await createServer({root,server:{middlewareMode:true},appType:'custom',logLevel:'silent'})
  let app:any
  try{
    const {businessOutputs}=await vite.ssrLoadModule('/src/services/businessOutputs.ts')
    const {dataStructures}=await vite.ssrLoadModule('/src/services/dataStructures.ts')
    const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeAssemblyOutputEditor.vue')
    businessOutputs.detail=async(id:string)=>({id,name:'自定义输出',version:1,status:'published',parser_key:'structured_json.v1',contract_sha256:sha,data_structure_version:'structure-one'})
    dataStructures.schema=async()=>({valid:true,json_schema:definition,errors:{}})
    dataStructures.mappingOptions=async()=>({schema_version:1,nodes:[],scopes:[]})
    const props=reactive({modelValue:undefined as any,schema:target,disabled:false,purpose:'output'}),writes:any[]=[],validity:any[]=[]
    let state:any
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit(event:string,value:any){if(event==='validity')validity.push([...value]);else{writes.push(JSON.parse(JSON.stringify(value??null)));props.modelValue=value}}});return()=>h('div')}})
    app.provide(ssrContextKey,{modules:new Set()});app.mount({})
    const flush=async()=>{for(let i=0;i<8;i++)await nextTick()}
    await work({props,writes,validity,state,businessOutputs,dataStructures,flush,vite})
  }finally{app?.unmount();await vite.close()}
}

test('exact contract is read without rewriting raw input or mapping; readonly does not write',()=>component(async({props,writes,state,flush})=>{
  props.modelValue=initial();await flush()
  assert.deepEqual(state.sourceSchema.value,definition);assert.equal(writes.length,0)
  assert.equal(state.label.value,'自定义输出 · v1')
  props.disabled=true;await flush()
  state.update({source_text:'overwritten'});state.choose({id:'other',status:'published',parser_key:'structured_json.v1',contract_sha256:sha})
  assert.equal(writes.length,0);assert.equal(props.modelValue.source_text,initial().source_text)
}))

test('delivery fields reuse exact author editor and keep whitespace, failed reads and readonly state',()=>component(async({props,writes,state,businessOutputs,flush})=>{
  props.purpose='delivery'
  props.schema={type:'object',required:['title','notes'],properties:{title:{type:'string'},notes:{type:'string'}}}
  const raw=' {"标题":"  雨夜🌧  ","说明":"原文\\r\\n"} '
  props.modelValue={...initial(),source_text:raw};await flush()
  assert.equal(state.title.value,'交付标题与说明');assert.equal(state.inputLabel.value,'交付')
  assert.deepEqual(state.errors.value,[]);assert.equal(writes.length,0)
  const original=businessOutputs.detail
  businessOutputs.detail=async()=>{throw new Error('offline exact output')}
  await state.load();assert.match(state.errors.value.join(' '),/offline/)
  assert.equal(props.modelValue.source_text,raw);assert.equal(writes.length,0)
  businessOutputs.detail=original;await state.load();await flush();assert.deepEqual(state.errors.value,[])
  props.disabled=true;await flush();state.update({source_text:'must not overwrite'})
  assert.equal(props.modelValue.source_text,raw);assert.equal(writes.length,0)
}))

test('media author preserves clip identity and raw input; removal does not imply undoing adoption',()=>component(async({props,state,writes,businessOutputs,flush})=>{
  props.purpose='media';const raw=' {"片段":{"episodes":[{"number":1,"shots":[{"number":1,"artifact_id":"00000000-0000-4000-8000-000000000003"}]}]},"说明":"  原文  "} '
  props.modelValue={...initial(),source_text:raw};await flush()
  assert.equal(state.title.value,'已采用片段核对');assert.equal(writes.length,0)
  const original=businessOutputs.detail
  businessOutputs.detail=async()=>{throw new Error('media offline')};await state.load()
  assert.match(state.errors.value.join(' '),/media offline/);assert.equal(props.modelValue.source_text,raw)
  businessOutputs.detail=original;await state.load();await flush();assert.deepEqual(state.errors.value,[])
  state.toggle();assert.equal(writes.length,0);assert.match(state.pendingChange.value.message,/原媒体采用不变/)
  props.disabled=true;await flush();state.confirmChange();state.update({source_text:'changed'});state.toggle()
  assert.equal(props.modelValue.source_text,raw);assert.equal(writes.length,0)
}))

test('frozen manual author displays saved contract without current registry or catalog reads',()=>component(async({props,state,businessOutputs,dataStructures,flush})=>{
  businessOutputs.detail=async()=>{throw new Error('No registry')}
  dataStructures.schema=async()=>{throw new Error('No current structure')}
  dataStructures.mappingOptions=async()=>{throw new Error('No current catalog')}
  props.disabled=true;props.modelValue=initial()
  props.frozenContract={output_reference:{id:'output-one',sha256:sha},projection:{valid:true}}
  await flush();assert.equal(state.error.value,'');assert.match(state.label.value,/冻结/)
  assert.equal(state.sourceSchema.value,null)
  props.frozenContract={output_reference:{id:'wrong',sha256:sha}}
  await flush();assert.match(state.error.value,/不一致/)
}))
test('hash drift is an explicit retained-draft error and recovery reloads exact metadata',()=>component(async({props,writes,state,businessOutputs,flush})=>{
  const original=businessOutputs.detail
  businessOutputs.detail=async(id:string)=>({...await original(id),contract_sha256:'b'.repeat(64)})
  props.modelValue=initial();await flush()
  assert.match(state.error.value,/摘要不一致/);assert.equal(state.sourceSchema.value,null);assert.equal(writes.length,0)
  businessOutputs.detail=original;await state.load();await flush()
  assert.equal(state.error.value,'');assert.deepEqual(state.sourceSchema.value,definition)
}))
test('changing selected output preserves authored text and mapping, ignores late old response',()=>component(async({props,state,businessOutputs,flush})=>{
  let release:any
  const original=businessOutputs.detail
  businessOutputs.detail=(id:string)=>id==='output-one'?new Promise(resolve=>{release=resolve}):original(id)
  props.modelValue=initial();await flush()
  state.choose({id:'output-two',status:'published',parser_key:'structured_json.v1',contract_sha256:sha});await flush()
  release({...await original('output-one'),name:'Late old output'});await flush()
  assert.equal(props.modelValue.output_version_id,'output-two');assert.equal(props.modelValue.source_text,initial().source_text)
  assert.deepEqual(props.modelValue.mapping,initial().mapping);assert.equal(state.label.value,'自定义输出 · v1')
}))
test('optional mapping removal requires confirmation; failed metadata remains visible',()=>component(async({props,state,writes,businessOutputs,flush})=>{
  businessOutputs.detail=async()=>{throw new Error('isolated offline')}
  props.modelValue=initial();await flush()
  assert.match(state.error.value,/isolated offline/);assert.ok(state.errors.value.length)
  state.toggle();assert.equal(writes.length,0)
  state.pendingChange.value=null;assert.equal(props.modelValue.source_text,initial().source_text)
  state.toggle();state.confirmChange();await flush()
  assert.equal(props.modelValue,undefined);assert.deepEqual(state.errors.value,[])
}))

test('timing author uses the same exact source loader, preserves raw layout and has separate removal semantics',()=>component(async({props,state,writes,flush})=>{
  props.purpose='timing';props.modelValue={...initial(),source_text:' {"时序":{"episodes":[]},"说明":"原值"} '};await flush()
  assert.equal(state.title.value,'装配时序核对');assert.deepEqual(state.sourceSchema.value,definition)
  assert.equal(writes.length,0)
  const original=props.modelValue.source_text
  state.choose({id:'output-two',status:'published',parser_key:'structured_json.v1',contract_sha256:sha});await flush()
  assert.equal(props.modelValue.source_text,original)
  state.toggle();assert.equal(props.modelValue.source_text,original)
  assert.match(state.pendingChange.value.message,/本轮分镜时序/)
}))

test('upstream choice retains exact step/output identities without authored text; catalog drift blocks without rewrite',()=>component(async({props,state,writes,flush,vite})=>{
  const {adaptRecipeTextOutputOptions}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const raw={stage_key:'step-a',output_key:'structured_text.output-one',kind:'json',scope_kind:'project',label:'第一步 · 输出v1',reader:'structured_text',
    output_version_id:'output-one',output_contract_sha256:sha,structure_version_id:'structure-one',value_schema:definition}
  const options=adaptRecipeTextOutputOptions({text_output_options:[raw,{...raw,stage_key:'step-b',label:'第二步 · 同一输出v1'}]})
  assert.equal(options[0].outputVersionId,'output-one');assert.equal(options[0].structureVersionId,'structure-one')
  assert.throws(()=>adaptRecipeTextOutputOptions({text_output_options:[{...raw,structure_version_id:undefined}]}),/确切/)
  props.modelValue=initial();props.sourceOptions=options;await flush()
  state.chooseSource(JSON.stringify(['step-b',raw.output_key]));await flush()
  assert.equal(props.modelValue.source.stage_key,'step-b');assert.equal(props.modelValue.output_version_id,'output-one')
  assert.ok(!('source_text' in props.modelValue));assert.deepEqual(props.modelValue.mapping,initial().mapping)
  assert.equal(state.selectedMode.value,true);assert.deepEqual(state.errors.value,[])
  const count=writes.length, saved=JSON.stringify(props.modelValue)
  props.sourceOptions=[options[0]];await flush()
  assert.match(state.errors.value.join(' '),/上游已移除/);assert.equal(JSON.stringify(props.modelValue),saved);assert.equal(writes.length,count)
  props.sourceOptions=options;await flush();assert.deepEqual(state.errors.value,[])
  props.disabled=true;await flush();state.chooseSource(JSON.stringify(['step-a',raw.output_key]))
  assert.equal(JSON.stringify(props.modelValue),saved)
}))

test('source switch can be cancelled and removes the mutually exclusive source only on confirmation',()=>component(async({props,state,flush})=>{
  props.modelValue=initial();props.sourceOptions=[];await flush()
  state.sourceMode('selected');assert.equal(props.modelValue.source_text,initial().source_text)
  state.pendingChange.value=null
  state.sourceMode('selected');state.confirmChange();await flush()
  assert.ok(!('source_text' in props.modelValue));assert.equal(props.modelValue.source.kind,'selected_output')
  state.sourceMode('manual');state.confirmChange();await flush();assert.ok(!('source' in props.modelValue));assert.equal(props.modelValue.source_text,'')
}))

test('equivalent refreshed catalogs do not feed back validity events; exact structure drift remains blocking',()=>component(async({props,state,validity,flush})=>{
  props.modelValue={...initial(),source:{kind:'selected_output',stage_key:'prior',output_key:'structured_text.output-one',scope:{kind:'project'},selection_policy:'reviewed_selected'}}
  delete props.modelValue.source_text
  props.sourceOptions=[];await flush()
  const count=validity.length
  for(let i=0;i<5;i++){props.sourceOptions=[];await flush()}
  assert.equal(validity.length,count)
  const option={stageKey:'prior',outputKey:'structured_text.output-one',kind:'json',scopeKind:'project',label:'上游',outputVersionId:'output-one',outputContractSha256:sha,structureVersionId:'structure-one'}
  props.sourceOptions=[option];await flush();assert.deepEqual(state.errors.value,[])
  props.sourceOptions=[{...option,structureVersionId:'different'}];await flush()
  assert.match(state.errors.value.join(' '),/结构版本不一致/)
  props.sourceOptions=[option];await flush();assert.deepEqual(state.errors.value,[])
}))

test('pending assembly changes block edits and saving, cancel on changed source or purpose without writes',()=>component(async({props,state,writes,flush})=>{
  props.modelValue=initial();props.sourceOptions=[];await flush()
  const before=structuredClone(JSON.parse(JSON.stringify(props.modelValue)))
  state.sourceMode('selected');await flush()
  assert.match(state.errors.value.join(' '),/请确认变更/)
  state.update({source_text:'must not write'});state.chooseSource('not-current')
  state.choose({id:'other',status:'published',parser_key:'structured_json.v1',contract_sha256:sha});state.toggle()
  assert.equal(writes.length,0);assert.deepEqual(props.modelValue,before)
  props.sourceOptions=[{stageKey:'different'}];state.confirmChange()
  assert.equal(writes.length,0);assert.equal(state.pendingChange.value,null)
  state.toggle();props.purpose='delivery';state.confirmChange();assert.equal(writes.length,0)
  state.sourceMode('selected');props.modelValue={...before,source_text:'new fact'};state.confirmChange()
  assert.equal(writes.length,0);assert.equal(props.modelValue.source_text,'new fact')
  state.sourceMode('unknown');assert.equal(state.pendingChange.value,null)
}))

test('all four mapping purposes retain unrelated data and require explicit removal once',()=>component(async({props,state,writes,flush})=>{
  for(const purpose of ['output','timing','media','delivery']){
    props.purpose=purpose;props.modelValue={...initial(),extra:false};await flush()
    const count=writes.length
    state.toggle();assert.equal(writes.length,count)
    state.pendingChange.value=null;assert.equal(props.modelValue.extra,false)
    state.toggle();state.confirmChange();state.confirmChange();await flush()
    assert.equal(writes.length,count+1);assert.equal(props.modelValue,undefined)
  }
}))

test('assembly public references share the exact resource explorer and keep frozen facts separate',()=>component(async({vite})=>{
  const {readFileSync}=await import('node:fs')
  const source=readFileSync(new URL('../src/components/RecipeAssemblyOutputEditor.vue',import.meta.url),'utf8')
  assert.doesNotMatch(source,/window\.confirm/)
  assert.match(source,/RegistryResourceExplorer :label="title"/)
  assert.match(source,/inspect\('output',rule.output_version_id,\$event\)/)
  assert.match(source,/inspect\('structure',loadedStructureId,\$event\)/)
  assert.match(source,/v-if="!frozenContract"/)
  assert.match(source,/watch\(pendingChange/)
  assert.match(source,/returnFocus\?\.isConnected\?returnFocus:root.value/)
  const {createSSRApp}=await import('vue'),{renderToString}=await import('@vue/server-renderer')
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeAssemblyOutputEditor.vue')
  const html=await renderToString(createSSRApp(Component,{modelValue:initial(),schema:target,disabled:true}))
  assert.match(html,/查看输出定义与使用方/);assert.match(html,/readonly/)
  assert.match(html,/assembly-controls/)
}))
