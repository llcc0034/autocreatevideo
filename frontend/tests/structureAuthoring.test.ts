import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createRenderer,h,nextTick,ssrContextKey,reactive} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView} from 'vue-router'

const settle=async()=>{for(let i=0;i<20;i++){await Promise.resolve();await nextTick()}}
const copy=(v:any)=>JSON.parse(JSON.stringify(v))
const deferred=()=>{let resolve!:(v:any)=>void;const promise=new Promise<any>(r=>resolve=r);return{promise,resolve}}
test('structure authors retain exact definitions and protect uncertain writes',async t=>{
  const storage=globalThis.localStorage,win=globalThis.window
  globalThis.localStorage={getItem:()=>null} as any;globalThis.window={addEventListener(){},removeEventListener(){}} as any
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',logLevel:'silent'})
  t.after(async()=>{await vite.close();globalThis.localStorage=storage;globalThis.window=win})
  const {dataStructures:api}=await vite.ssrLoadModule('/src/services/dataStructures.ts')
  const {api:http}=await vite.ssrLoadModule('/src/services/api.ts')
  const {default:Component}=await vite.ssrLoadModule('/src/components/DataStructureManager.vue')
  const {useAuthStore}=await vite.ssrLoadModule('/src/stores/auth.ts')
  const {structureVersionDetails,readResourceDetails}=await vite.ssrLoadModule('/src/services/registryResourceDetails.ts')
  const originalList=api.list
  const defaults={parent_key:null,field_definition_version:null,property_name:'',position:0,required:false,allow_null:false,allow_blank:true,allow_empty:true,has_default:false,default_value:null,constraints:{}}
  const row=(id='v1',status='published',version=1)=>({id,structure_key:'family-a',version,status,name:'原结构 🟢',description:'中文原值',nodes:[{...defaults,node_key:'root',field_definition_version:'object-v1'},{...defaults,node_key:'motion',parent_key:'root',property_name:'action',position:1,field_definition_version:'text-v3',has_default:true,default_value:false}]})
  let calls:any[]=[],saved:any=row()
  function reset(){
    calls=[];saved=row()
    api.options=async()=>({node_defaults:copy(defaults),statuses:[{value:'draft',label:'草稿'},{value:'published',label:'可用'},{value:'retired',label:'停用'}],constraint_schemas:{},value_schemas:{}})
    api.list=async(...args:any[])=>{calls.push(['list',...args]);return{count:2,results:[row(),row('v7','published',7)],hasNext:false,hasPrevious:false}}
    api.get=async(id:string)=>{calls.push(['get',id]);return copy({...saved,id})}
    api.create=async(content:any,source?:string)=>{calls.push(['create',copy(content),source]);saved={...row('v8','draft',8),...copy(content)};return copy(saved)}
    api.update=async(id:string,content:any)=>{calls.push(['update',id,copy(content)]);saved={...saved,...copy(content)};return copy(saved)}
    api.transition=async(id:string,action:string)=>{calls.push(['transition',id,action]);saved={...saved,status:action==='publish'?'published':'retired'};return copy(saved)}
  }
  async function mount(){
    let state:any;const events:any[]=[]
    const Host={setup(){state=Component.setup({}, {expose(){},emit:(...args:any[])=>events.push(args)});return()=>h('div')}}
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/settings',component:Host},{path:'/elsewhere',component:{render:()=>h('div')}}]});await router.push('/settings');await router.isReady()
    const pinia=createPinia(),auth=useAuthStore(pinia);auth.user={id:'owner'}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    const app=renderer.createApp({render:()=>h(RouterView)});app.use(pinia);app.use(router);app.provide(ssrContextKey,{modules:new Set()});app.mount({});await settle();return{state,app,auth,events,router}
  }
  await t.test('new family saves incomplete empty draft without guessing identity or publishing',async()=>{
    reset();const c=await mount(),s=c.state;assert.equal(s.selected.value,null);s.begin();assert.deepEqual(s.form.nodes,[]);s.form.name='新结构';await s.save();assert.deepEqual(calls.find(c=>c[0]==='create'),['create',{name:'新结构',description:'',nodes:[]},undefined]);assert.equal(s.selected.value.status,'draft');assert.equal(calls.some(c=>c[0]==='transition'),false);c.app.unmount()
  })
  await t.test('old published source creates family successor v8, not source plus one',async()=>{
    reset();const c=await mount(),s=c.state;await s.open('v1');await s.save();assert.equal(calls.some(c=>c[0]==='create'),false);s.begin(true);assert.equal(s.predecessor.value.version,1);s.form.name='后继';await s.save();assert.equal(calls.find(c=>c[0]==='create')[2],'v1');assert.equal(s.selected.value.version,8);assert.equal(s.form.nodes[1].default_value,false);c.app.unmount()
  })
  await t.test('draft PATCH is sparse and nodes remain an explicit whole-list replacement',async()=>{
    reset();saved=row('draft','draft',2);const c=await mount(),s=c.state;await s.open('draft');await s.save();assert.equal(calls.some(c=>c[0]==='update'),false);s.form.description='只改说明';await s.save();assert.deepEqual(calls.find(c=>c[0]==='update')[2],{description:'只改说明'});s.form.nodes[1].default_value=0;await s.save();const patch=calls.filter(c=>c[0]==='update').at(-1)[2];assert.deepEqual(Object.keys(patch),['nodes']);assert.equal(patch.nodes.length,2);assert.equal(patch.nodes[1].default_value,0);c.app.unmount()
  })
  await t.test('publish saves changes first; explicit node-key errors focus the right node',async()=>{
    reset();saved=row('draft','draft',2);const c=await mount(),s=c.state;await s.open('draft');s.form.name='新名字';api.transition=async(...args:any[])=>{calls.push(['transition',...args]);throw{response:{status:400,data:{error:{code:'invalid',fields:{'nodes.motion.property_name':['属性有误']}}}}}};await s.transition('publish');assert.deepEqual(calls.filter(c=>['update','transition'].includes(c[0])).map(c=>c[0]),['update','transition']);assert.equal(s.selected.value.name,'新名字');assert.equal(s.selected.value.status,'draft');assert.equal(s.nodeIndex.value,1);assert.equal(s.issues.value[0].section,'nodes');assert.equal(s.unknown.value,null);c.app.unmount()
  })
  await t.test('unknown create blocks saves and switches, empty readback does not unlock',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.name='原输入';let writes=0;api.create=async()=>{writes++;throw{response:{status:504}}};await s.save();await s.save();s.begin();await s.open('v1');assert.equal(writes,1);assert.equal(s.form.name,'原输入');api.list=async()=>({results:[],count:0,hasNext:false,hasPrevious:false});await s.readback();assert.ok(s.unknown.value);assert.equal(s.recoveryChecked.value,true);c.app.unmount()
  })
  await t.test('successor recovery paginates same family including all statuses and requires explicit load',async()=>{
    reset();const c=await mount(),s=c.state;await s.open('v1');s.begin(true);api.create=async()=>{throw Error('timeout')};await s.save();api.list=async(...args:any[])=>{calls.push(['recovery',...args]);return{results:args[0]===1?[row()]:[row('v8','draft',8)],count:2,hasNext:args[0]===1,hasPrevious:args[0]===2}};await s.readback();assert.equal(s.recoveryNext.value,true);await s.adoptRecovery('v1');assert.ok(s.unknown.value);await s.readback(2);assert.deepEqual(calls.at(-1),['recovery',2,'','','family-a']);assert.equal(s.selected.value,null);saved=row('v8','draft',8);await s.adoptRecovery('v8');assert.equal(s.selected.value.id,'v8');assert.equal(s.unknown.value,null);assert.match(s.notice.value,/不认定/);c.app.unmount()
  })
  await t.test('unknown PATCH and publish read exact target without repeating either writer',async()=>{
    for(const kind of ['update','publish']){reset();saved=row('draft','draft',2);const c=await mount(),s=c.state;await s.open('draft');let writes=0;if(kind==='update'){s.form.description='本地';api.update=async()=>{writes++;throw Error('timeout')};await s.save()}else{api.transition=async()=>{writes++;throw Error('timeout')};await s.transition('publish')};await s.readback();assert.deepEqual(calls.at(-1),['get','draft']);assert.ok(s.unknown.value);await s.adoptRecovery('draft');assert.ok(s.unknown.value,'old content/state is not proof the writer ended');await s.save();await s.transition('publish');assert.equal(writes,1);saved=kind==='update'?{...saved,description:'本地'}:{...saved,status:'published'};await s.readback();await s.adoptRecovery('draft');assert.equal(writes,1);assert.equal(s.unknown.value,null);c.app.unmount()}
  })
  await t.test('wrong receipt locks writer, foreign recovery family cannot replace content',async()=>{
    reset();const c=await mount(),s=c.state;await s.open('v1');s.begin(true);api.create=async()=>({...row('other','draft',8),structure_key:'foreign'});await s.save();assert.ok(s.unknown.value);api.list=async()=>({count:1,results:[{...row('other','draft',8),structure_key:'foreign'}],hasNext:false,hasPrevious:false});await s.readback();assert.match(s.error.value,/其他结构家族/);assert.equal(s.recovery.value.length,0);assert.equal(s.form.name,'原结构 🟢');c.app.unmount()
  })
  await t.test('unknown node PATCH matches serializer property order and node enumeration, never changes input order',async()=>{
    reset();saved=row('draft','draft',2);const c=await mount(),s=c.state;await s.open('draft');s.form.nodes[1].default_value=[0,false,null];const intended=copy(s.form.nodes);api.update=async()=>{throw Error('timeout')};await s.save();saved.nodes=copy(intended).reverse().map((node:any)=>Object.fromEntries(Object.entries(node).reverse()));await s.readback();await s.adoptRecovery('draft');assert.equal(s.unknown.value,null);assert.deepEqual(intended[1].default_value,[0,false,null]);const {sameStructureValue}=await vite.ssrLoadModule('/src/services/structureAuthoring.ts');assert.equal(sameStructureValue('nodes',intended,saved.nodes),true);const different=copy(saved.nodes);different[0].default_value=[false,0,null];assert.equal(sameStructureValue('nodes',intended,different),false);c.app.unmount()
  })
  await t.test('partial node drafts prevent save and removal; catalog failure preserves mounted editor options',async()=>{
    reset();const c=await mount(),s=c.state;await s.open('v1');s.begin(true);s.nodeIndex.value=1;s.draftErrors.value=['未完成数字'];await s.save();s.removeIndex.value=1;s.removeNode();assert.equal(s.form.nodes.length,2);assert.equal(calls.some(c=>c[0]==='create'),false);api.options=async()=>{throw Error('目录失败')};await s.load();assert.ok(s.options.value);assert.equal(s.catalogReady.value,false);assert.equal(s.nodeIndex.value,1);assert.deepEqual(s.draftErrors.value,['未完成数字']);c.app.unmount()
  })
  await t.test('inline close and route guards preserve input until explicit discard',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.name='保留';s.close();assert.ok(s.leavePrompt.value);s.keepEditing();assert.equal(s.form.name,'保留');const leaving=c.router.push('/elsewhere');await settle();assert.ok(s.leavePrompt.value);s.keepEditing();await leaving;assert.equal(c.router.currentRoute.value.path,'/settings');let prevented=false;s.beforeUnload({preventDefault(){prevented=true},returnValue:null});assert.ok(prevented);s.close();s.discardAndLeave();assert.equal(c.events.filter(e=>e[0]==='close').length,1);c.app.unmount()
  })
  await t.test('late writer after account switch or unmount cannot continue publish',async()=>{
    for(const mode of ['account','unmount']){reset();saved=row('draft','draft',2);const c=await mount(),s=c.state;await s.open('draft');s.form.name='迟到';const d=deferred();api.update=()=>d.promise;const pending=s.transition('publish');if(mode==='account')c.auth.user={id:'other'};else c.app.unmount();d.resolve({...saved,name:'回执'});await pending;assert.equal(calls.some(c=>c[0]==='transition'),false);if(mode==='account'){assert.equal(s.selected.value,null);assert.equal(s.form.name,'');c.app.unmount()}}
  })
  await t.test('late reads and incorrect exact IDs cannot silently switch selected version',async()=>{
    reset();const c=await mount(),s=c.state;api.get=async()=>row('wrong');await s.open('v1');assert.equal(s.selected.value,null);assert.match(s.error.value,/身份/);const d=deferred();api.get=()=>d.promise;const pending=s.open('v1');c.auth.user={id:'other'};d.resolve(row());await pending;assert.equal(s.selected.value,null);c.app.unmount()
  })
  await t.test('shared readonly projection preserves defaults and field/position identity',async()=>{
    reset();const r=row();r.nodes[1].default_value=0 as any;const details=structureVersionDetails(r);assert.deepEqual((await readResourceDetails('structure','v1')).sections[0],details.sections[0]);const value=details.sections[1].facts[1].value;assert.equal(value['默认值'],0);assert.equal(value['字段版本'],'text-v3');assert.equal(value['同层排序'],1);assert.equal(value['允许空值'],false)
  })
  await t.test('field reread keeps the node editor definition and unfinished inputs until exact read succeeds',async()=>{
    const {default:Node}=await vite.ssrLoadModule('/src/components/DataStructureNodeEditor.vue')
    let state:any;const events:any[]=[],props=reactive({node:{...copy(defaults),node_key:'motion',field_definition_version:'integer-v1',has_default:true,default_value:7},nodes:[],options:{constraint_schemas:{integer:{type:'object'}},value_schemas:{integer:{type:'integer'}}},disabled:false})
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    const app=renderer.createApp({setup(){state=Node.setup(props,{expose(){},emit:(...args:any[])=>events.push(args)});return()=>h('div')}});app.provide(ssrContextKey,{modules:new Set()});app.mount({})
    const definition={id:'integer-v1',value_type:'integer',value_options:[]};state.receiveDefinition(definition);state.validity('default',['未完成数字']);state.receiveDefinition(null);assert.equal(state.definition.value.id,'integer-v1');assert.equal(state.definitionAvailable.value,false);assert.equal(state.partial.value,true);state.receiveDefinition({...definition});assert.equal(state.definitionAvailable.value,true);assert.deepEqual(state.defaultErrors.value,['未完成数字']);state.receiveDefinition({id:'foreign',value_type:'text'});assert.equal(state.definition.value.id,'integer-v1');assert.equal(state.definitionAvailable.value,false);props.node.field_definition_version='text-v1';await nextTick();assert.equal(state.definition.value,null);app.unmount()
  })
  await t.test('unfinished ordering survives parent rerenders and field rereads without reverting to the stored number',async()=>{
    const {default:Node}=await vite.ssrLoadModule('/src/components/DataStructureNodeEditor.vue')
    let state:any;const events:any[]=[],props=reactive({node:{...copy(defaults),node_key:'motion',position:7,field_definition_version:'text-v1'},nodes:[],options:{constraint_schemas:{},value_schemas:{}},disabled:false})
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    const app=renderer.createApp({setup(){state=Node.setup(props,{expose(){},emit:(...args:any[])=>events.push(args)});return()=>h('div')}});app.provide(ssrContextKey,{modules:new Set()});app.mount({})
    state.updatePosition('');props.node={...props.node,node_key:'renamed-motion',allow_null:true};state.receiveDefinition(null);await nextTick()
    assert.equal(state.positionDraft.value,'');assert.equal(props.node.position,7);assert.equal(state.partial.value,true);assert.equal(events.some(row=>row[0]==='change'),false)
    state.updatePosition('0.5');props.disabled=true;await nextTick();state.updatePosition('9');assert.equal(state.positionDraft.value,'0.5')
    props.disabled=false;state.updatePosition('0');props.node={...events.at(-1)[1]};await nextTick();assert.equal(state.positionDraft.value,'0');assert.equal(state.partial.value,false)
    props.node={...props.node,node_key:'other',position:3};await nextTick();assert.equal(state.positionDraft.value,'3')
    const source=await import('node:fs/promises').then(fs=>fs.readFile(new URL('../src/components/DataStructureNodeEditor.vue',import.meta.url),'utf8'));assert.match(source,/:value="positionDraft"/)
    app.unmount()
  })
  await t.test('list reader forwards existing family filter and honors backend paging, malformed paging rejected',async()=>{
    const calls:any[]=[];http.get=async(url:string,config:any)=>{calls.push([url,config]);return{data:{count:100,results:[],next:null,previous:'?page=1'}}};const result=await originalList(2,'','', 'family');assert.equal(result.hasNext,false);assert.equal(result.hasPrevious,true);assert.equal(calls[0][1].params.structure_key,'family');assert.equal('status' in calls[0][1].params,false);http.get=async()=>({data:{count:1,results:[]}});await assert.rejects(()=>originalList(1,''),/分页/)
  })
})
