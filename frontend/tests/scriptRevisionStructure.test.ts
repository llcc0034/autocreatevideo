import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createRenderer,h,nextTick,reactive,ssrContextKey,createSSRApp} from 'vue'
import {renderToString} from 'vue/server-renderer'

const option={id:'output-1',output_key:'key-1',version:2,name:'布局 A',description:'配置字段'}
const detail=(id='revision-1',hash='a'.repeat(64))=>({document_sha256:'b'.repeat(64),json_schema:{type:'object',properties:{note:{type:'string'},zero:{type:'integer'},flag:{type:'boolean'}}},can_revise:true,document:{schema_version:1,revision:{id,source_id:'source-1',content_sha256:hash},output:{...option},projection:{validation_scope:'structure_mapping_and_relations',source_value:{note:'额外字段',zero:0,flag:false},value:{entities:[],episodes:[]},defaults:[],provenance:[],warnings:[]}}})
async function modules(context:any){
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const service=await vite.ssrLoadModule('/src/services/scriptRevisionStructures.ts')
  const component=(await vite.ssrLoadModule('/src/components/ScriptRevisionStructurePanel.vue')).default
  return {vite,service,api:service.scriptRevisionStructures,component}
}
function setup(context:any,Component:any,props=reactive({revisionId:'revision-1',contentSha256:'a'.repeat(64),status:'preview',disabled:false})){
  let state:any;const events:any[]=[]
  const renderer=createRenderer({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(...args:any[])=>events.push(args)});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({});context.after(()=>app.unmount())
  return {state,props,app,events}
}

test('explicit selection saves only output identity and reads exact detail without approving or rewriting text',async context=>{
  const {api,component}=await modules(context),calls:any[]=[]
  api.read=async()=>null;api.options=async(...args:any[])=>{calls.push(['options',...args]);return{count:1,results:[option]}}
  api.save=async(...args:any[])=>{calls.push(['save',...args]);return detail()}
  const {state}=setup(context,component);await nextTick()
  assert.equal(state.canSave.value,false);await state.save();assert.deepEqual(calls,[])
  await state.loadOptions();assert.equal(state.selected.value,null)
  state.choose(option.id);await state.save()
  assert.deepEqual(calls,[['options',1,''],['save','revision-1','output-1']])
  assert.equal(state.result.value.document.projection.source_value.flag,false)
  assert.equal(state.result.value.document.projection.source_value.zero,0)
  assert.equal(state.canSave.value,false)
  api.read=async()=>detail();await state.read();assert.equal(calls.length,2)
})

test('failure keeps selected version; stale save/read cannot leak across revision changes',async context=>{
  const {api,component}=await modules(context)
  api.read=async()=>null;api.options=async()=>({count:1,results:[option]})
  api.save=async()=>{throw Error('隔离解析错误：entity 未声明')}
  const {state,props}=setup(context,component);await nextTick();await state.loadOptions();state.choose(option.id);await state.save()
  assert.equal(state.selected.value.id,option.id);assert.match(state.error.value,/未声明/);assert.equal(state.result.value,null)
  let finish:any;api.save=()=>new Promise(resolve=>finish=resolve)
  const pending=state.save();assert.equal(state.saving.value,true);props.revisionId='revision-2';await nextTick()
  finish(detail());await pending;assert.equal(state.result.value,null);assert.equal(state.saving.value,false);assert.equal(state.selected.value,null)
  api.read=()=>new Promise(resolve=>finish=resolve);const old=state.read()
  api.read=async()=>null;props.revisionId='revision-3';await nextTick();finish(detail('revision-2'));await old
  assert.equal(state.result.value,null);assert.equal(state.loaded.value,true)
})

test('failed or mismatched reads are explicit, never no-result success; confirmed revisions cannot be parsed',async context=>{
  const {api,component}=await modules(context)
  api.read=async()=>{throw Error('保存详情已损坏')}
  const {state,props}=setup(context,component);await nextTick()
  assert.equal(state.loaded.value,false);assert.match(state.error.value,/损坏/)
  api.read=async()=>detail('wrong-revision');await state.read();assert.equal(state.loaded.value,false);assert.match(state.error.value,/不一致/)
  api.read=async()=>null;props.status='confirmed';await nextTick();state.selected.value=option
  api.save=()=>assert.fail('confirmed revision must not be modified');await state.save();assert.equal(state.canSave.value,false)
})

test('version directory search is paginated, stale queries discarded and chosen version stays explicit',async context=>{
  const {api,component}=await modules(context)
  api.read=async()=>null;api.options=async()=>({count:21,results:[option]})
  const {state}=setup(context,component);await nextTick();await state.loadOptions();state.choose(option.id)
  const calls:any[]=[];api.options=async(...args:any[])=>{calls.push(args);return {count:21,results:[{...option,id:'output-2'}]}}
  state.search.value='B';await state.loadOptions(2);assert.deepEqual(calls,[[2,'B']]);assert.equal(state.selected.value.id,'output-1')
  let finish:any;api.options=()=>new Promise(resolve=>finish=resolve);const old=state.loadOptions(1)
  api.options=async()=>({count:0,results:[]});await state.loadOptions(2);finish({count:1,results:[option]});await old
  assert.deepEqual(state.rows.value,[]);assert.equal(state.count.value,0);assert.equal(state.page.value,2)
})

test('wire adapter rejects incomplete and null-save responses; request never includes another source text',async context=>{
  const {vite,service,api}=await modules(context),transport=(await vite.ssrLoadModule('/src/services/api.ts')).api
  for(const raw of [null,{}, {structure:{}},{structure:{document:{schema_version:1},document_sha256:'b'.repeat(64)}}])assert.throws(()=>service.adaptScriptRevisionStructure(raw))
  assert.equal(service.adaptScriptRevisionStructure({structure:null}),null)
  for(const key of ['value','source_value','defaults','provenance','warnings']){const missing:any=detail();delete missing.document.projection[key];assert.throws(()=>service.adaptScriptRevisionStructure({structure:missing}))}
  const missingName:any=detail();delete missingName.document.output.name;assert.throws(()=>service.adaptScriptRevisionStructure({structure:missingName}))
  const calls:any[]=[];transport.post=async(...args:any[])=>{calls.push(args);return{data:{structure:detail()}}}
  await api.save('revision-1','output-1');assert.deepEqual(calls,[['/script-revisions/revision-1/structure/',{output_version_id:'output-1'}]])
  transport.post=async()=>({data:{structure:null}});await assert.rejects(api.save('revision-1','output-1'),/未返回/)
  transport.get=async()=>({data:{count:1,results:[{...option,version:0}]}});await assert.rejects(api.options(),/目录不完整/)
})

test('wire directory preserves pagination and exact output identity for real DRF responses',async context=>{
  const {vite,api}=await modules(context),transport=(await vite.ssrLoadModule('/src/services/api.ts')).api
  const page={count:21,next:'/api/v1/script-revisions/structure-options/?page=2',previous:null,results:[option]}
  const calls:any[]=[]
  for(const body of [page,{data:page}]){
    transport.get=async(...args:any[])=>{calls.push(args);return{data:body}}
    assert.deepEqual(await api.options(1,'布局'),page)
    assert.deepEqual(calls.at(-1),['/script-revisions/structure-options/',{params:{page:1,page_size:20,search:'布局'}}])
  }
  transport.get=async()=>({data:{count:0,next:null,previous:null,results:[]}})
  assert.deepEqual((await api.options()).results,[])
  for(const body of [null,[],{results:[option]},{count:1,results:[{...option,id:null}]}]){
    transport.get=async()=>({data:body});await assert.rejects(api.options(),/目录不完整/)
  }
})

test('panel offers read/save actions but never model execution or automatic confirmation',async context=>{
  const {api,component}=await modules(context);api.read=async()=>null
  const html=await renderToString(createSSRApp({render:()=>h(component,{revisionId:'revision-1',contentSha256:'a'.repeat(64),status:'preview'})}))
  assert.match(html,/JSON 结构化结果/);assert.match(html,/重新读取结果/);assert.doesNotMatch(html,/确认该剧本版本|自动采用|触发模型/)
})

test('field edit uses frozen schema, preserves extras and saves through original revision API once',async context=>{
  const {vite,api,component}=await modules(context),platform=(await vite.ssrLoadModule('/src/services/platformApi.ts')).platformApi
  const original=detail();api.read=async()=>original
  const {state,events}=setup(context,component);await nextTick();state.startEdit()
  state.changeDraft({...state.draft.value,note:'人工新描述',extra:{zero:0,flag:false,empty:null}})
  assert.equal(original.document.projection.source_value.note,'额外字段')
  let finish:any;const calls:any[]=[]
  platform.createScriptRevision=(...args:any[])=>{calls.push(args);return new Promise(resolve=>finish=resolve)}
  const pending=state.revise();await state.revise();assert.equal(calls.length,1)
  assert.deepEqual(calls[0],['source-1',JSON.stringify(state.draft.value,null,2),{revision_id:'revision-1',document_sha256:'b'.repeat(64)}])
  const created={id:'revision-2',source:'source-1',status:'preview',content:calls[0][1]}
  finish(created);await pending
  assert.deepEqual(events.find(row=>row[0]==='revision-created'),['revision-created',created,'revision-1'])
  assert.deepEqual(events.filter(row=>row[0]==='edit-busy').map(row=>row[1]),[true,false])
  assert.equal(state.editing.value,false);assert.equal(state.result.value.document.revision.id,'revision-1')
})

test('schema and field-path failures keep draft and do not fake a new revision',async context=>{
  const {vite,api,component}=await modules(context),platform=(await vite.ssrLoadModule('/src/services/platformApi.ts')).platformApi
  api.read=async()=>detail();const {state,events}=setup(context,component);await nextTick();state.startEdit()
  state.changeDraft({...state.draft.value,zero:'wrong'});assert.equal(state.canRevise.value,false)
  platform.createScriptRevision=()=>assert.fail('invalid schema cannot submit');await state.revise()
  state.changeDraft({...state.draft.value,zero:0,note:'保持草稿'})
  platform.createScriptRevision=async()=>{throw {response:{status:400,data:{'source.note':['隔离关联错误']}}}}
  await state.revise();assert.equal(state.draft.value.note,'保持草稿');assert.equal(state.editing.value,true)
  assert.equal(state.editUncertain.value,false);assert.deepEqual(state.fieldErrors.value,{'source.note':['隔离关联错误']})
  assert.equal(events.some(row=>row[0]==='revision-created'),false)
  const html=await renderToString(createSSRApp({...component,setup:()=>state},{revisionId:'revision-1',contentSha256:'a'.repeat(64),status:'preview'}))
  assert.match(html,/structured-json-editor/);assert.match(html,/隔离关联错误/);assert.match(html,/另存预览修订并解析/)
  await state.read();assert.equal(state.draft.value.note,'保持草稿')
})

test('uncertain save and mismatched receipt never automatically duplicate a revision',async context=>{
  const {vite,api,component}=await modules(context),platform=(await vite.ssrLoadModule('/src/services/platformApi.ts')).platformApi
  api.read=async()=>detail();const {state,events}=setup(context,component);await nextTick();state.startEdit()
  let calls=0;platform.createScriptRevision=async()=>{calls++;throw Error('network offline')}
  await state.revise();await state.revise();assert.equal(calls,1);assert.equal(state.editUncertain.value,true)
  assert.equal(state.draft.value.zero,0);assert.equal(events.some(row=>row[0]==='revision-created'),false)
  state.cancelEdit();state.startEdit()
  platform.createScriptRevision=async()=>({id:'revision-2',source:'other-source',status:'preview',content:'wrong'})
  await state.revise();assert.equal(state.editUncertain.value,true);assert.match(state.editError.value,/回执/)
})

test('late field-save receipt cannot replace another source selection',async context=>{
  const {vite,api,component}=await modules(context),platform=(await vite.ssrLoadModule('/src/services/platformApi.ts')).platformApi
  api.read=async()=>detail();const {state,props,events}=setup(context,component);await nextTick();state.startEdit()
  let finish:any;platform.createScriptRevision=()=>new Promise(resolve=>finish=resolve)
  const pending=state.revise(),content=JSON.stringify(state.draft.value,null,2)
  api.read=async()=>detail('revision-other');props.revisionId='revision-other';await nextTick()
  finish({id:'revision-new',source:'source-1',status:'preview',content});await pending
  assert.equal(events.some(row=>row[0]==='revision-created'),false);assert.equal(state.editSaving.value,false)
  assert.equal(state.result.value.document.revision.id,'revision-other');assert.equal(state.editing.value,false)
})

test('root arrays use shared field editor, while generated or malformed contracts cannot edit',async context=>{
  const {api,component,service}=await modules(context)
  const value:any=detail();value.document.projection.source_value=[{note:'数组内容',zero:0,flag:false}]
  value.json_schema={type:'array',items:detail().json_schema};api.read=async()=>value
  const {state}=setup(context,component);await nextTick();state.startEdit();assert.equal(state.canRevise.value,true)
  const html=await renderToString(createSSRApp({...component,setup:()=>state},{revisionId:'revision-1',contentSha256:'a'.repeat(64),status:'preview'}))
  assert.match(html,/第 1 项/);assert.match(html,/数组内容/)
  state.cancelEdit();api.read=async()=>({...value,can_revise:false});await state.read();state.startEdit()
  assert.equal(state.editing.value,false)
  for(const key of ['json_schema','can_revise']){const bad:any=detail();delete bad[key];assert.throws(()=>service.adaptScriptRevisionStructure({structure:bad}))}
})

test('revision API includes exact optional structure basis without inventing approval inputs',async context=>{
  const {vite}=await modules(context),transport=(await vite.ssrLoadModule('/src/services/api.ts')).api
  const platform=(await vite.ssrLoadModule('/src/services/platformApi.ts')).platformApi,calls:any[]=[]
  transport.post=async(...args:any[])=>{calls.push(args);return {data:{id:'revision-2',source:'source-1',status:'preview',content:'{}'}}}
  const basis={revision_id:'revision-1',document_sha256:'b'.repeat(64)}
  await platform.createScriptRevision('source-1','{}',basis)
  assert.deepEqual(calls,[['/script-revisions/',{source_id:'source-1',content:'{}',structure_basis:basis}]])
})
