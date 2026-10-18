import test from 'node:test'
import assert from 'node:assert/strict'
import {createServer} from 'vite'
import {createSSRApp,h,ref} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {createPinia,setActivePinia} from 'pinia'
import {matchedRouteKey} from 'vue-router'
import {scriptFixture} from './fixtures/script-semantics-data.ts'
Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=>null,setItem(){},removeItem(){}}})
const base={id:'output-v1',output_key:'family',version:1,revision:3,name:'原输出',description:'不翻译 🟢',parser_key:'structured_json.v1',data_structure_version:'structure-v1',status:'draft',contract_sha256:'a'.repeat(64),quality_advisories:{},parser_contract:{key:'structured_json.v1',format:'json'}}
const copy=(v:any)=>JSON.parse(JSON.stringify(v))
async function setup(t:any){
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});t.after(()=>vite.close());setActivePinia(createPinia())
  const {businessOutputs:api}=await vite.ssrLoadModule('/src/services/businessOutputs.ts'),{default:Manager}=await vite.ssrLoadModule('/src/components/BusinessOutputManager.vue')
  let state:any;const app=createSSRApp({setup(){state=Manager.setup({},{expose(){},emit(){}});return()=>h('div')}});app.provide(matchedRouteKey,ref({leaveGuards:new Set(),updateGuards:new Set()}));await renderToString(app)
  api.list=async()=>({count:1,results:[copy(base)],hasNext:false,hasPrevious:false});api.options=async()=>({parsers:[{key:base.parser_key,label:'JSON',description:'结构',configuration:'structure'}],editor:null,parser:null,allowedValueFields:[],fieldSourceOptions:[],responseKeyPolicy:null})
  await state.load();state.assign(copy(base));return{vite,api,state,Manager}
}
test('output draft uses shared four-section author and published semantic details',async t=>{
  const {vite,Manager,state}=await setup(t)
  assert.deepEqual(state.sections.map((s:any)=>s.key),['identity','structure','rules','review'])
  const {default:Details}=await vite.ssrLoadModule('/src/components/BusinessOutputDefinitionView.vue')
  const html=await renderToString(createSSRApp(Details,{definition:{...base,status:'published'}}))
  assert.match(html,/不翻译 🟢/);assert.match(html,/本版本如何解析/);assert.doesNotMatch(html,/<input|<select|<textarea/)
  assert.ok(Manager)
})
test('unknown update cannot unlock old revision or unrelated concurrent content and never replays',async t=>{
  const {state,api}=await setup(t);let writes=0;state.form.name='期望的修改';api.save=async()=>{writes++;throw{response:{status:504}}}
  await state.save();assert.equal(writes,1);assert.ok(state.pending.value);await state.save();assert.equal(writes,1)
  api.detail=async()=>copy(base);await state.recover();assert.equal(state.recoveryRows.value.length,0)
  api.detail=async()=>({...base,revision:4,name:'其他人的修改'});await state.recover();assert.equal(state.recoveryRows.value.length,0)
  api.detail=async()=>({...base,revision:4,name:'期望的修改'});await state.recover();await state.loadRecovered(state.recoveryRows.value[0]);assert.equal(state.pending.value,null);assert.equal(state.selected.value.name,'期望的修改');assert.equal(writes,1)
})
test('successor accepts only exact next version from same family and content, not newest guesses',async t=>{
  const {state,api}=await setup(t);const source={...base,status:'published'};state.assign(source)
  api.action=async()=>{throw{response:{status:504}}};await state.act('successor')
  for(const change of [{id:source.id},{output_key:'other'},{version:3},{revision:2},{name:'altered'}]){
    const bad={...source,id:'successor',version:2,revision:1,status:'draft',...change};api.detail=async()=>bad;await state.loadRecovered(bad);assert.ok(state.pending.value)
  }
  const good={...source,id:'successor',version:2,revision:1,status:'draft'};api.detail=async()=>good;await state.loadRecovered(good);assert.equal(state.pending.value,null);assert.equal(state.predecessor.value.id,source.id)
})
test('new creation records complete paged identity baseline and excludes old matching drafts',async t=>{
  const {state,api}=await setup(t);state.assign(null);Object.assign(state.form,{name:'新定义',parser_key:base.parser_key,data_structure_version:base.data_structure_version});const pages:number[]=[]
  const old={...base,id:'off-page-old',name:'新定义',description:'',version:1,revision:1}
  api.list=async(page:number)=>{pages.push(page);return{count:2,results:[page===1?base:old],hasNext:page===1,hasPrevious:page>1}}
  api.save=async()=>{throw{response:{status:504}}};await state.save();assert.deepEqual(pages,[1,2]);assert.ok(state.pending.value.known.includes(old.id))
  api.detail=async()=>old;await state.loadRecovered(old);assert.ok(state.pending.value)
  const created={...old,id:'new',output_key:'new-family'};api.detail=async()=>created;await state.loadRecovered(created);assert.equal(state.pending.value,null)
})
test('baseline failure does not claim a submitted or unknown write',async t=>{
  const {state,api}=await setup(t);state.assign(null);Object.assign(state.form,{name:'新定义',parser_key:base.parser_key,data_structure_version:base.data_structure_version});let writes=0
  api.list=async()=>{throw new Error('unavailable')};api.save=async()=>{writes++};await state.save();assert.equal(writes,0);assert.equal(state.pending.value,null);assert.equal(state.form.name,'新定义');assert.ok(state.error.value)
})
test('publish and retire recover target status with unchanged content and SHA',async t=>{
  const {state,api}=await setup(t)
  for(const action of ['publish','retire']){const source={...base,status:action==='publish'?'draft':'published'};state.assign(source);state.confirmed.value=true;api.action=async()=>{throw{response:{status:504}}};await state.act(action)
    api.detail=async()=>({...source,revision:4,status:action==='publish'?'published':'retired',contract_sha256:'wrong'});await state.recover();assert.equal(state.recoveryRows.value.length,0)
    api.detail=async()=>({...source,revision:4,status:action==='publish'?'published':'retired'});await state.recover();await state.loadRecovered(state.recoveryRows.value[0]);assert.equal(state.pending.value,null)
  }
})
test('explicit node/structure errors locate a section and retain input, directory failure blocks save',async t=>{
  const {state,api}=await setup(t);state.form.description='保留原输入';state.notice.value='上一轮已成功';api.save=async()=>{throw{response:{status:400,data:{error:{fields:{data_structure_version:['结构不匹配']}}}}}}
  await state.save();assert.equal(state.pending.value,null);assert.equal(state.issues.value[0].section,'structure');assert.equal(state.form.description,'保留原输入');assert.equal(state.notice.value,'')
  api.options=async()=>{throw new Error('offline')};await state.load();assert.equal(state.catalogReady.value,false);assert.equal(state.form.description,'保留原输入');let writes=0;api.save=async()=>{writes++};await state.save();assert.equal(writes,0)
})
test('content comparison ignores JSON key order but retains zero false and nested array order',async t=>{
  const {vite}=await setup(t);const {outputContentMatches}=await vite.ssrLoadModule('/src/services/businessOutputAuthoring.ts')
  const row={...base,parser_key:'script_semantics.v1',parser_contract:{key:'script_semantics.v1',mapping:{x:0,y:false,items:[1,2]}}}
  assert.ok(outputContentMatches(row,{semantic_mapping:{items:[1,2],y:false,x:0}}));assert.equal(outputContentMatches(row,{semantic_mapping:{x:0,y:false,items:[2,1]}}),false)
})
test('close confirmation retains input and unknown result prevents navigation',async t=>{
  const {state,api}=await setup(t);state.form.name='未保存';const leave=state.canLeave();state.confirmLeave(false);assert.equal(await leave,false);assert.equal(state.form.name,'未保存')
  api.save=async()=>{throw{response:{status:504}}};await state.save();assert.equal(await state.canLeave(),false);let prevented=0;state.beforeUnload({preventDefault(){prevented++}});assert.equal(prevented,1)
})

test('changing semantic structure warns before discarding invalid local input and keeps exact mapping',async t=>{
 const {state,api}=await setup(t);state.assign({...base,parser_key:'script_semantics.v1',parser_contract:{key:'script_semantics.v1',format:'json',mapping:{schema_version:1,node:{kind:'literal',value:0}}}})
 state.semanticErrors.value=['需要有效数字'];state.changeStructure('structure-v2');assert.equal(state.form.data_structure_version,'structure-v1');assert.equal(state.structureChange.value,'structure-v2')
 let writes=0;api.save=async()=>{writes++};await state.save();assert.equal(writes,0);assert.equal(await state.canLeave(),false)
 state.structureChange.value=null;assert.deepEqual(state.semanticErrors.value,['需要有效数字']);assert.equal(state.form.semantic_mapping.node.value,0)
 state.changeStructure('structure-v2');state.confirmStructureChange();assert.equal(state.form.data_structure_version,'structure-v2');assert.equal(state.form.semantic_mapping.node.value,0);assert.equal(state.structureChange.value,null)
})

test('trimmed author labels recover lost create and update receipts without rewriting form',async t=>{
  const {state,api}=await setup(t)
  for(const fresh of [false,true]){state.assign(fresh?null:copy(base));Object.assign(state.form,{name:' 输出A ',description:' 原说明 🟢 ',parser_key:base.parser_key,data_structure_version:base.data_structure_version});api.save=async()=>{throw{response:{status:504}}};await state.save();assert.equal(state.form.name,' 输出A ')
    const row={...base,id:fresh?'created':base.id,output_key:fresh?'created-family':base.output_key,revision:fresh?1:4,name:'输出A',description:'原说明 🟢'};api.detail=async()=>row;await state.loadRecovered(row);assert.equal(state.pending.value,null);assert.equal(state.selected.value.name,'输出A')
  }
})

test('successful-looking save must match attempted content and exclude baseline identities',async t=>{
  const {state,api}=await setup(t);state.form.name='新内容';let writes=0;api.save=async()=>{writes++;return{...base,revision:4,name:'别人内容'}};await state.save();assert.ok(state.pending.value);assert.equal(state.form.name,'新内容');await state.save();assert.equal(writes,1)
  state.pending.value=null;state.assign(null);Object.assign(state.form,{name:base.name,description:base.description,parser_key:base.parser_key,data_structure_version:base.data_structure_version});const old={...base,revision:1};api.list=async()=>({count:1,results:[old],hasNext:false,hasPrevious:false});api.save=async()=>old;await state.save();assert.ok(state.pending.value);assert.equal(state.selected.value,null)
})

test('pending semantic mapping blocks publishing the old saved output before any API write',async t=>{
  const {state,api}=await setup(t)
  state.assign({...base,parser_key:'script_semantics.v1',parser_contract:{key:'script_semantics.v1',format:'json',mapping:{schema_version:1,node:{kind:'literal',value:0}}}})
  state.confirmed.value=true;assert.equal(state.dirty.value,false)
  state.semanticErrors.value=['请先确认映射变更或继续编辑原配置'];let writes=0;api.action=async()=>{writes++;throw Error('not called')}
  await state.act('publish');assert.equal(writes,0);assert.equal(state.busy.value,false)
  state.semanticErrors.value=[];state.structureChange.value='other-structure';await state.act('publish');assert.equal(writes,0)
})

test('complex semantic successor saves the entire edited tree without mutating its published predecessor',async t=>{
  const {state,api}=await setup(t)
  const source={...copy(base),status:'published',parser_key:'script_semantics.v1',parser_contract:{key:'script_semantics.v1',format:'json',mapping:copy(scriptFixture.layouts.b.mapping)}}
  const original=copy(source);state.assign(source)
  state.parsers.value=[{key:source.parser_key,label:'剧本语义',configuration:'script_semantics'}]
  const options=api.options;api.options=async()=>({...await options(),parsers:[{key:source.parser_key,label:'剧本语义',configuration:'script_semantics'}]})
  api.action=async()=>({...copy(source),id:'successor',revision:1,version:2,status:'draft'})
  await state.act('successor')
  const mapping=state.form.semantic_mapping
  mapping.node.fields.entities.item.fields.description={kind:'literal',value:'原始设定 🟢，不得翻译'}
  mapping.node.fields.episodes.item.fields.shots.item.fields.duration_ms={kind:'literal',value:3400}
  state.form.description='跨维护返回后保存';state.changeStructure('structure-v4')
  const expected=copy(state.payload());let writes=0
  api.save=async(payload:any,current:any)=>{writes++;assert.deepEqual(payload,expected);assert.equal(current.id,'successor');return{...copy(current),...copy(payload),revision:2,parser_contract:{key:source.parser_key,format:'json',mapping:copy(payload.semantic_mapping)}}}
  await state.save()
  assert.equal(writes,1);assert.equal(state.pending.value,null);assert.equal(state.dirty.value,false)
  assert.deepEqual(state.selected.value.parser_contract.mapping,expected.semantic_mapping)
  assert.equal(state.selected.value.data_structure_version,'structure-v4')
  assert.deepEqual(source,original);assert.deepEqual(state.predecessor.value,original)
})

test('returning to output author and refreshing catalogs never upgrades a structure or drops unsaved mapping',async t=>{
  const {state,api}=await setup(t)
  state.assign({...copy(base),parser_key:'script_semantics.v1',parser_contract:{key:'script_semantics.v1',mapping:copy(scriptFixture.layouts.b.mapping)}})
  state.form.description='未保存原文 🟢';state.form.semantic_mapping.node.fields.episodes.item.fields.shots.item.fields.duration_ms={kind:'literal',value:3400}
  const before=copy(state.payload())
  api.list=async()=>({count:1,results:[{...copy(base),data_structure_version:'unrelated-newest'}],hasNext:false,hasPrevious:false})
  await state.load();assert.deepEqual(state.payload(),before)
  state.changeStructure('explicit-v4')
  assert.deepEqual(state.payload(),{...before,data_structure_version:'explicit-v4'})
  assert.equal(state.selected.value.data_structure_version,'structure-v1','saved record is unchanged until save')
})
