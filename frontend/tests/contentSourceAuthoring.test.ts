import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, ssrContextKey, reactive, watch } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory, RouterView, useRoute } from 'vue-router'

const settle=async()=>{for(let i=0;i<20;i++){await Promise.resolve();await nextTick()}}
const deferred=()=>{let resolve!:(x:any)=>void;const promise=new Promise<any>(r=>resolve=r);return{promise,resolve}}
test('content source authoring uses existing writers, exact versions and guarded navigation',async t=>{
  const storage=globalThis.localStorage,win=globalThis.window
  globalThis.localStorage={getItem:()=>null} as any
  globalThis.window={addEventListener(){},removeEventListener(){}} as any
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',logLevel:'silent'})
  const {contentSources:api}=await vite.ssrLoadModule('/src/services/contentSources.ts')
  const originalSave=api.save
  const {api:http}=await vite.ssrLoadModule('/src/services/api.ts')
  const {default:Component}=await vite.ssrLoadModule('/src/components/ContentSourceManager.vue')
  const {useAuthStore}=await vite.ssrLoadModule('/src/stores/auth.ts')
  const {readResourceDetails}=await vite.ssrLoadModule('/src/services/registryResourceDetails.ts')
  const source=(id='source',status='draft',version=1)=>({id,source_key:'family',version,name:'来源',description:'描述',reader_selector:'request.theme',data_structure_version:'structure',status,revision:3,reader:{protocol:'request_text/v1',selector:'request.theme',value_kind:'text',scope_kind:'request',snapshot_policy:'confirmed_request'},contract_sha256:'a'.repeat(64),contract_size_bytes:300})
  let calls:any[]=[],saved:any=source()
  const reset=()=>{
    calls=[];saved=source()
    api.list=async(...args:any[])=>{calls.push(['list',...args]);return{count:1,next:null,results:[saved]}}
    api.readers=async()=>[{value:'request.theme',label:'本次主题',group_label:'本次输入',record_location:'本次填写',description:'本次输入',execution_usage:'冻结原文',reader:source().reader}]
    api.get=async(id:string)=>{calls.push(['get',id]);return{...saved,id}}
    api.save=async(data:any,original:any)=>{calls.push(['save',{...data},original]);saved={...source(),...data};return saved}
    api.action=async(original:any,action:string)=>{calls.push(['action',original,action]);return action==='successor'?source('successor','draft',original.version+1):{...original,status:action==='publish'?'published':'retired',revision:original.revision+1}}
  }
  const mount=async(choose=false)=>{
    let state:any;const events:any[]=[]
    const Host={setup(){const route=useRoute(),props=reactive({choose,initialId:''});watch(()=>route.query.source_version,id=>props.initialId=String(id||''));state=Component.setup(props,{expose(){},emit:(...args:any[])=>events.push(args)});return()=>h('div')}}
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/settings',component:Host}]});await router.push('/settings');await router.isReady()
    const pinia=createPinia(),auth=useAuthStore(pinia);auth.user={id:'operator'}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    const app=renderer.createApp({render:()=>h(RouterView)})
    app.use(pinia);app.use(router);app.provide(ssrContextKey,{modules:new Set()});app.mount({});await settle()
    return{state,app,auth,events,router}
  }
  t.after(async()=>{await vite.close();globalThis.localStorage=storage;globalThis.window=win})
  await t.test('new author has no guessed reader or structure; saves exact raw values and does not publish',async()=>{
    reset();const c=await mount(),s=c.state;s.edit(null);assert.equal(s.form.reader_selector,'');assert.equal(s.form.data_structure_version,'')
    Object.assign(s.form,{name:' 原名 ',description:'中文 🟢 raw',reader_selector:'request.theme',data_structure_version:'structure'});await s.save()
    assert.equal(calls.filter(x=>x[0]==='save').length,1);assert.equal(calls.find(x=>x[0]==='save')[1].name,' 原名 ');assert.equal(s.dirty.value,false);assert.equal(s.selected.value.status,'draft');assert.equal(calls.filter(x=>x[0]==='action').length,0);c.app.unmount()
  })
  await t.test('draft edits retain original ID and revision, require saved values before publish',async()=>{
    reset();const c=await mount(),s=c.state;s.edit(source());s.form.description='changed';s.confirmed.value=true;await s.act('publish');assert.equal(calls.filter(x=>x[0]==='action').length,0)
    await s.save();assert.equal(calls.find(x=>x[0]==='save')[2].revision,3);assert.equal(s.confirmed.value,false);s.confirmed.value=true;await s.act('publish');assert.equal(s.selected.value.status,'published');c.app.unmount()
  })
  await t.test('all in-dialog switches and close preserve dirty inputs until explicit discard',async()=>{
    reset();const c=await mount(),s=c.state;s.edit(source());s.form.name='local';s.close();assert.ok(s.leavePrompt.value);assert.equal(c.events.length,0);s.keepEditing();assert.equal(s.form.name,'local')
    s.edit(null);assert.equal(s.form.name,'local');s.keepEditing();await s.openSource({id:'other'});assert.equal(calls.filter(x=>x[0]==='get').length,0);s.discardAndLeave();await settle();assert.equal(s.selected.value.id,'other');c.app.unmount()
  })
  await t.test('route leave can be rejected or explicitly accepted; browser leave warns',async()=>{
    reset();const c=await mount(),s=c.state;s.edit(source());s.form.name='local';const rejected=s.mayLeave();s.keepEditing();assert.equal(await rejected,false)
    let prevented=false;s.beforeUnload({preventDefault(){prevented=true},returnValue:null});assert.ok(prevented)
    const accepted=s.mayLeave();s.discardAndLeave();assert.equal(await accepted,true);c.app.unmount()
  })
  await t.test('choice mode cannot save, publish, retire or create a successor; choices require exact published readback',async()=>{
    reset();const c=await mount(true),s=c.state;s.edit(source());await s.save();s.confirmed.value=true;await s.act('publish');await s.act('successor');await s.act('retire');assert.equal(calls.filter(x=>['save','action'].includes(x[0])).length,0)
    await s.openSource({id:'source'},true);assert.equal(c.events.length,0);saved=source('source','published');await s.openSource({id:'source'},true);assert.equal(c.events[0][1].id,'source');c.app.unmount()
  })
  await t.test('published view uses frozen reader, source semantic detail shares it, not current reader catalog',async()=>{
    reset();const c=await mount(),s=c.state;s.edit(source('source','published'));s.readers.value=[{value:'request.theme',reader:{protocol:'future/v9'}}]
    assert.equal(s.editable.value,false);assert.equal(s.savedReader.value.protocol,'request_text/v1');const detail=await readResourceDetails('source','source');assert.equal(detail.sections[1].facts[0].value,'request_text/v1')
    api.get=async()=>source('other');await assert.rejects(()=>readResourceDetails('source','source'),/身份不匹配/);c.app.unmount()
  })
  await t.test('successor passes explicitly selected predecessor, rejects mismatched receipt, never selects highest silently',async()=>{
    reset();const c=await mount(),s=c.state;s.edit(source('old','published',2));await s.act('successor');assert.equal(calls.find(x=>x[0]==='action')[1].id,'old');assert.equal(s.predecessor.value.id,'old');assert.equal(s.selected.value.version,3)
    s.edit(source('old','published',2));api.action=async()=>source('wrong','draft',8);await s.act('successor');assert.ok(s.unknown.value);assert.equal(s.selected.value.id,'old');c.app.unmount()
  })
  await t.test('explicit field rejection retains all original inputs and supplies section anchors',async()=>{
    reset();const c=await mount(),s=c.state;s.edit(source());s.form.name='unchanged'
    api.save=async()=>{throw {response:{status:400,data:{error:{code:'invalid',message:'invalid',fields:{name:['名称问题'],data_structure_version:['结构问题']}}}}}}
    await s.save()
    assert.equal(s.form.name,'unchanged');assert.equal(s.unknown.value,null);assert.equal(s.issues.value[0].section,'identity');assert.equal(s.issues.value[1].section,'structure');c.app.unmount()
  })
  await t.test('unknown create blocks repeated writes; empty readback is not failure; explicit exact load is not claimed as receipt',async()=>{
    reset();const c=await mount(),s=c.state;s.edit(null);Object.assign(s.form,{name:'new',reader_selector:'request.theme',data_structure_version:'structure'});let writes=0;api.save=async()=>{writes++;throw Error('timeout')};await s.save();await s.save();assert.equal(writes,1)
    api.list=async()=>({count:0,next:null,results:[]});await s.readback();assert.ok(s.unknown.value);assert.equal(s.recoveryChecked.value,true)
    api.list=async()=>({count:2,next:null,results:[source('a'),source('b')]});await s.readback();assert.equal(s.selected.value,null);await s.adoptRecovery('b');assert.equal(s.selected.value.id,'b');assert.equal(s.unknown.value,null);assert.match(s.notice.value,/不认定/);assert.equal(writes,1);c.app.unmount()
  })
  await t.test('unknown successor uses family pagination and original revision, never retries mutation during readback',async()=>{
    reset();const c=await mount(),s=c.state;s.edit(source('original','published',2));api.action=async()=>{throw{response:{status:504}}};await s.act('successor');s.recoveryPage.value=2;await s.readback();assert.deepEqual(calls.find(x=>x[0]==='list'&&x[1]===2),['list',2,'','','family']);assert.equal(s.unknown.value.source.revision,3)
    api.get=async()=>({...source('source'),source_key:'other'});await s.adoptRecovery('source');assert.ok(s.unknown.value);c.app.unmount()
  })
  await t.test('late save after account change or unmount does not restore former selection or form',async()=>{
    for(const mode of ['account','unmount']){reset();const c=await mount(),s=c.state;s.edit(source());const d=deferred();api.save=()=>d.promise;const pending=s.save();if(mode==='account')c.auth.user={id:'other'};else c.app.unmount();d.resolve(source('receipt'));await pending;if(mode==='account'){assert.equal(s.selected.value,null);assert.equal(s.form.name,'');c.app.unmount()}else assert.equal(s.selected.value.id,'source')}
  })
  await t.test('loading and wrong-ID errors retain original editor; repeated click while pending emits one request',async()=>{
    reset();const c=await mount(),s=c.state;s.edit(source());api.get=async()=>source('wrong');await s.openSource({id:'next'});assert.equal(s.selected.value.id,'source');assert.match(s.error.value,/身份/)
    const d=deferred();let writes=0;api.save=()=>{writes++;return d.promise};const first=s.save();await s.save();assert.equal(s.busy.value,true);assert.match(s.phase.value,/保存/);assert.equal(writes,1);d.resolve(source());await first;assert.equal(s.busy.value,false);c.app.unmount()
  })
  await t.test('original or earlier records cannot resolve an unknown successor',async()=>{
    reset();const c=await mount(),s=c.state;s.edit(source('original','published',2));api.action=async()=>{throw Error('timeout')};await s.act('successor')
    api.list=async()=>({count:2,results:[source('original','published',2),source('next','draft',3)]});await s.readback()
    api.get=async id=>source(id,id==='original'?'published':'draft',id==='original'?2:3);await s.adoptRecovery('original');assert.ok(s.unknown.value);assert.equal(s.predecessor.value,null)
    await s.adoptRecovery('next');assert.equal(s.unknown.value,null);assert.equal(s.selected.value.id,'next');assert.equal(s.predecessor.value.id,'original');c.app.unmount()
  })
  await t.test('same-route source changes require one discard only and keep URL and selected identity aligned',async()=>{
    reset();const c=await mount(),s=c.state;s.edit(source());s.form.name='dirty';const navigation=c.router.push('/settings?source_version=other');await settle();assert.ok(s.leavePrompt.value);s.discardAndLeave();await navigation;await settle();assert.equal(s.leavePrompt.value,'');assert.equal(s.selected.value.id,'other');assert.equal(c.router.currentRoute.value.query.source_version,'other');c.app.unmount()
  })
  await t.test('choice skips author reader options; management keeps a valid version list when options fail',async()=>{
    reset();let readers=0;api.readers=async()=>{readers++;throw Error('directory offline')};const choice=await mount(true);assert.equal(readers,0);assert.equal(choice.state.rows.value.length,1);choice.app.unmount()
    const management=await mount();assert.equal(readers,1);assert.equal(management.state.rows.value.length,1);assert.equal(management.state.readers.value.length,0);assert.match(management.state.error.value,/directory offline/);management.app.unmount()
  })
  await t.test('existing draft PATCH contains only changed fields and exact revision, preserving unchanged structure binding',async()=>{
    reset();let payload:any;const patch=http.patch;http.patch=async(_url,data)=>{payload=data;return{data:source()}}
    try{const original=source();await originalSave({...original,name:'new'},original);assert.deepEqual(payload,{name:'new',expected_revision:3})}finally{http.patch=patch}
  })
  await t.test('an action receipt in a different status remains unknown instead of claiming success',async()=>{
    reset();const c=await mount(),s=c.state;s.edit(source());s.confirmed.value=true;api.action=async()=>source();await s.act('publish');assert.ok(s.unknown.value);assert.equal(s.selected.value.status,'draft');c.app.unmount()
  })
})
