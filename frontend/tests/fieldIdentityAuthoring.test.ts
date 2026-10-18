import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createRenderer,h,nextTick,reactive,ssrContextKey} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView} from 'vue-router'
const settle=async()=>{for(let i=0;i<20;i++){await Promise.resolve();await nextTick()}}
const deferred=()=>{let resolve!:(v:any)=>void;const promise=new Promise<any>(r=>resolve=r);return{promise,resolve}}
test('field identity authors keep one stable identity and recover without duplicate writes',async t=>{
  const storage=globalThis.localStorage,win=globalThis.window
  globalThis.localStorage={getItem:()=>null} as any;globalThis.window={addEventListener(){},removeEventListener(){}} as any
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',logLevel:'silent'})
  t.after(async()=>{await vite.close();globalThis.localStorage=storage;globalThis.window=win})
  const {platformApi:api}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const {fieldDefinitionVersions:definitions}=await vite.ssrLoadModule('/src/services/fieldDefinitionVersions.ts')
  definitions.list=async()=>[]
  const {default:Component}=await vite.ssrLoadModule('/src/components/FieldMaintenanceSettings.vue')
  const {useAuthStore}=await vite.ssrLoadModule('/src/stores/auth.ts')
  const original={id:'f1',key:'shot.action',isActive:true,displayOrder:1}
  let calls:any[]=[],saved:any=original
  const reset=()=>{
    calls=[];saved={...original}
    api.fieldDefinitionPage=async(...args:any[])=>{calls.push(['list',...args]);return{count:1,results:[saved],hasNext:false,hasPrevious:false}}
    api.getFieldDefinition=async(id:string)=>{calls.push(['get',id]);return{...saved}}
    api.createFieldDefinition=async(payload:any)=>{calls.push(['create',payload]);saved={...original,id:'new-id',key:payload.key,isActive:false};return saved}
    api.updateFieldDefinition=async(id:string,payload:any)=>{calls.push(['update',id,payload]);saved={...saved,isActive:payload.is_active};return saved}
  }
  const mount=async()=>{
    let state:any
    const props=reactive({fieldDefinitionId:''})
    const Host={setup(){state=Component.setup(props,{expose(){}});return()=>h('div')}}
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/fields',component:Host},{path:'/elsewhere',component:{render:()=>h('div')}}]});await router.push('/fields');await router.isReady()
    const pinia=createPinia(),auth=useAuthStore(pinia);auth.user={id:'owner'}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    const app=renderer.createApp({render:()=>h(RouterView)});app.use(pinia);app.use(router);app.provide(ssrContextKey,{modules:new Set()});app.mount({});await settle()
    return{state,app,auth,router,props}
  }
  await t.test('create sends only trimmed stable key; backend state preserved and opens exact definition context',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();assert.equal(s.form.key,'');s.form.key=' custom.action ';await s.save()
    assert.deepEqual(calls.filter(r=>r[0]==='create'),[['create',{key:'custom.action'}]]);assert.equal(s.versions.value.id,'new-id');assert.equal(s.versions.value.isActive,false);assert.equal(s.editorOpen.value,false);c.app.unmount()
  })
  await t.test('identity update changes only active flag, no version or key rewrite',async()=>{
    reset();const c=await mount(),s=c.state;s.begin(original);await s.save();assert.equal(calls.some(r=>r[0]==='update'),false)
    s.form.isActive=false;s.form.key='not-authoritative';await s.save();assert.deepEqual(calls.filter(r=>r[0]==='update'),[['update','f1',{is_active:false}]]);c.app.unmount()
  })
  await t.test('inline close keep preserves raw value, discard clears and reopened new form is empty',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.key='未保存🟢';s.closeIdentity();assert.ok(s.leavePrompt.value);s.keepIdentity();assert.equal(s.form.key,'未保存🟢');assert.ok(s.editorOpen.value);s.closeIdentity();s.discardIdentity();assert.equal(s.editorOpen.value,false);s.begin();assert.equal(s.form.key,'');c.app.unmount()
  })
  await t.test('route and unload protect unsaved identity; cancel route keeps context',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.key='pending';const leaving=c.router.push('/elsewhere');await settle();assert.ok(s.leavePrompt.value);s.keepIdentity();await leaving;assert.equal(c.router.currentRoute.value.path,'/fields');let prevented=false;s.identityBeforeUnload({preventDefault(){prevented=true},returnValue:null});assert.equal(prevented,true);c.app.unmount()
  })
  await t.test('400 rejection preserves original text and error location without unknown lock',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.key='原值🟢';api.createFieldDefinition=async()=>{throw{response:{status:400,data:{error:{code:'invalid',fields:{key:['格式错误']}}}}}};await s.save();assert.equal(s.form.key,'原值🟢');assert.equal(s.identityIssues.value[0].section,'identity');assert.equal(s.unknownIdentity.value,null);c.app.unmount()
  })
  await t.test('unknown create locks save and preserves exact search across paginated readback including inactive',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.key='exact.key';let writes=0;api.createFieldDefinition=async()=>{writes++;throw{response:{status:504}}};await s.save();await s.save();assert.equal(writes,1)
    api.fieldDefinitionPage=async(...args:any[])=>{calls.push(['recovery',...args]);return{count:15,results:[{...original,key:args[0]===1?'not.exact.key':'exact.key'}],hasNext:args[0]===1,hasPrevious:args[0]===2}};await s.readIdentityResult();assert.deepEqual(calls.at(-1),['recovery',1,'exact.key',true]);assert.equal(s.recovered.value.length,0);assert.ok(s.unknownIdentity.value);assert.equal(s.recoveryChecked.value,true);assert.equal(s.recoveryHasNext.value,true);assert.equal(s.recoveryHasPrevious.value,false);await s.readIdentityResult(2);assert.deepEqual(calls.at(-1),['recovery',2,'exact.key',true]);assert.equal(s.recovered.value[0].key,'exact.key');assert.equal(s.recoveryHasNext.value,false);assert.equal(s.recoveryHasPrevious.value,true);c.app.unmount()
  })
  await t.test('unknown create loads exact existing identity only by explicit action, never calls it successful creation',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.key='new.key';api.createFieldDefinition=async()=>{throw Error('timeout')};await s.save();saved={...original,id:'server-id',key:'new.key',isActive:false};await s.readIdentityResult();assert.equal(s.original.value,null);await s.adoptIdentity('server-id');assert.equal(s.original.value.id,'server-id');assert.equal(s.form.isActive,false);assert.equal(s.unknownIdentity.value,null);assert.match(s.notice.value,/不认定/);assert.equal(s.versions.value,null);c.app.unmount()
  })
  await t.test('unknown update GETs original identity; readback does not overwrite local choice until adopted',async()=>{
    reset();const c=await mount(),s=c.state;s.begin(original);s.form.isActive=false;api.updateFieldDefinition=async()=>{throw Error('timeout')};await s.save();await s.readIdentityResult();assert.deepEqual(calls.at(-1),['get','f1']);assert.equal(s.form.isActive,false);await s.adoptIdentity('f1');assert.equal(s.form.isActive,true);assert.equal(s.identityDirty.value,false);c.app.unmount()
  })
  await t.test('mismatched write receipt stays unknown and cannot open another field versions',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.key='wanted';api.createFieldDefinition=async()=>({...original,key:'foreign'});await s.save();assert.ok(s.unknownIdentity.value);assert.equal(s.versions.value,null);assert.equal(s.editorOpen.value,true);c.app.unmount()
  })
  await t.test('mismatched recovered GET cannot replace original edit or unlock write',async()=>{
    reset();const c=await mount(),s=c.state;s.begin(original);s.form.isActive=false;api.updateFieldDefinition=async()=>{throw Error('timeout')};await s.save();await s.readIdentityResult();saved={...original,id:'foreign'};await s.adoptIdentity('f1');assert.ok(s.unknownIdentity.value);assert.equal(s.form.isActive,false);assert.match(s.editorError.value,/不匹配/);c.app.unmount()
  })
  await t.test('double click during request performs one write and truthful busy phase',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.key='one';const d=deferred();let writes=0;api.createFieldDefinition=()=>{writes++;return d.promise};const action=s.save();await s.save();assert.equal(writes,1);assert.match(s.identityPhase.value,/正在保存/);s.closeIdentity();assert.ok(s.editorOpen.value);d.resolve({...original,id:'one',key:'one'});await action;c.app.unmount()
  })
  await t.test('late write after account change or unmount cannot reopen definition modal',async()=>{
    for(const change of ['account','unmount']){reset();const c=await mount(),s=c.state;s.begin();s.form.key='late';const d=deferred();api.createFieldDefinition=()=>d.promise;const action=s.save();if(change==='account')c.auth.user={id:'next'};else c.app.unmount();d.resolve({...original,id:'late',key:'late'});await action;assert.equal(s.versions.value,null);if(change==='account')c.app.unmount()}
  })
  await t.test('moving from dirty identity to definition versions requires explicit discard, not silent save',async()=>{
    reset();const c=await mount(),s=c.state;s.begin(original);s.form.isActive=false;s.goToIdentityVersions();assert.ok(s.leavePrompt.value);assert.equal(s.versions.value,null);s.keepIdentity();assert.equal(s.form.isActive,false);s.goToIdentityVersions();s.discardIdentity();assert.equal(s.versions.value.id,'f1');assert.equal(calls.some(r=>r[0]==='update'),false);c.app.unmount()
  })
  await t.test('manual identity opening invalidates earlier deep link read',async()=>{
    reset();const c=await mount(),s=c.state;c.props.fieldDefinitionId='old';const d=deferred();api.getFieldDefinition=()=>d.promise;await settle();s.begin();d.resolve({...original,id:'old'});await settle();assert.equal(s.versions.value,null);assert.equal(s.editorOpen.value,true);c.app.unmount()
  })
})
