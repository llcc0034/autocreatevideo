import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createRenderer,h,nextTick,reactive,ssrContextKey,watch} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView,useRoute} from 'vue-router'

const settle=async()=>{for(let i=0;i<20;i++){await Promise.resolve();await nextTick()}}
const deferred=()=>{let resolve!:(value:any)=>void;const promise=new Promise<any>(r=>resolve=r);return{promise,resolve}}
test('field definition authors preserve exact contracts and user context',async t=>{
  const storage=globalThis.localStorage,win=globalThis.window
  globalThis.localStorage={getItem:()=>null} as any;globalThis.window={addEventListener(){},removeEventListener(){}} as any
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',logLevel:'silent'})
  t.after(async()=>{await vite.close();globalThis.localStorage=storage;globalThis.window=win})
  const {fieldDefinitionVersions:api}=await vite.ssrLoadModule('/src/services/fieldDefinitionVersions.ts')
  const {default:Component}=await vite.ssrLoadModule('/src/components/FieldDefinitionVersions.vue')
  const {default:Parent}=await vite.ssrLoadModule('/src/components/FieldMaintenanceSettings.vue')
  const {useAuthStore}=await vite.ssrLoadModule('/src/stores/auth.ts')
  const {readResourceDetails,fieldVersionDetails}=await vite.ssrLoadModule('/src/services/registryResourceDetails.ts')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const row=(id='v1',status='published',version=1)=>({id,field_definition:'f1',field_key:'scene.action',version,status,label:'动作原名',category:'创作',value_type:'text',unit:'',description:'原值 🟢',impact:'用于镜头',value_options:[],preview_example:'示例不是默认值'})
  let calls:any[]=[],saved:any=row()
  const reset=()=>{
    calls=[];saved=row()
    api.options=async()=>({value_types:[{value:'text',label:'文本'},{value:'enum',label:'枚举'},{value:'integer',label:'整数'}],statuses:[{value:'published',label:'已启用'},{value:'draft',label:'草稿'},{value:'retired',label:'停用'}]})
    api.list=async(field:string)=>{calls.push(['list',field]);return[row()]}
    api.get=async(id:string)=>{calls.push(['get',id]);return{...saved,id}}
    api.create=async(field:string,content:any,source:string)=>{calls.push(['create',field,structuredClone(content),source]);saved={...row('v8','draft',8),...content};return saved}
    api.update=async(id:string,content:any)=>{calls.push(['update',id,structuredClone(content)]);saved={...saved,id,status:'draft',...content};return saved}
    api.transition=async(id:string,action:string)=>{calls.push(['transition',id,action]);saved={...saved,id,status:action==='publish'?'published':'retired'};return saved}
  }
  const mount=async(target=Component)=>{
    let state:any;const events:any[]=[],exposed:any={}
    const Host={setup(){const route=useRoute(),props=reactive({fieldDefinition:'f1',initialId:'',fieldDefinitionId:''});watch(()=>route.query.field_version,id=>props.initialId=String(id||''));state=target.setup(props,{expose:(value:any)=>Object.assign(exposed,value),emit:(...args:any[])=>events.push(args)});return()=>h('div')}}
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/settings',component:Host},{path:'/elsewhere',component:{render:()=>h('div')}}]});await router.push('/settings');await router.isReady()
    const pinia=createPinia(),auth=useAuthStore(pinia);auth.user={id:'owner'}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    const app=renderer.createApp({render:()=>h(RouterView)});app.use(pinia);app.use(router);app.provide(ssrContextKey,{modules:new Set()});app.mount({});await settle()
    return{state,app,auth,events,exposed,router}
  }
  await t.test('no automatic version or type selection; new values retained and not auto-published',async()=>{
    reset();const c=await mount(),s=c.state;assert.equal(s.selected.value,null);s.begin();assert.equal(s.form.value_type,'');assert.equal(s.dirty.value,false)
    Object.assign(s.form,{label:' 原中文🟢 ',description:'原描述',value_type:'enum'});s.enumText.value='A\nB';await s.save()
    assert.deepEqual(calls.find(c=>c[0]==='create').slice(1),['f1',{label:' 原中文🟢 ',category:'',value_type:'enum',unit:'',description:'原描述',impact:'',value_options:['A','B']},undefined]);assert.equal(s.selected.value.status,'draft');assert.equal(s.dirty.value,false);assert.equal(calls.some(c=>c[0]==='transition'),false);c.app.unmount()
  })
  await t.test('published version readonly; successor uses original selected version not latest and server assigns version',async()=>{
    reset();const c=await mount(),s=c.state;await s.open('v1');await s.save();assert.equal(calls.some(c=>c[0]==='create'),false)
    s.rows.value.push(row('v7','published',7));s.begin(true);assert.equal(s.sourceVersion.value,'v1');assert.equal(s.form.description,'原值 🟢');s.form.label='后继';await s.save();assert.equal(calls.find(c=>c[0]==='create')[3],'v1');assert.equal(s.selected.value.version,8);c.app.unmount()
  })
  await t.test('unchanged draft has no PATCH; changed draft sends only changed fields, save before publish',async()=>{
    reset();saved=row('draft','draft',2);const c=await mount(),s=c.state;await s.open('draft');await s.save();assert.equal(calls.some(c=>c[0]==='update'),false)
    s.form.description='本次修改';await s.transition('publish');assert.deepEqual(calls.filter(c=>['update','transition'].includes(c[0])),[['update','draft',{description:'本次修改'}],['transition','draft','publish']]);assert.equal(s.readonly.value,true);c.app.unmount()
  })
  await t.test('explicit field rejection retains content with correct section and stops publish',async()=>{
    reset();saved=row('draft','draft');const c=await mount(),s=c.state;await s.open('draft');s.form.label='保留原值';api.update=async()=>{throw{response:{status:400,data:{error:{code:'invalid',fields:{label:['名称有误'],value_type:['类型有误']}}}}}}
    await s.transition('publish');assert.equal(s.form.label,'保留原值');assert.equal(s.unknown.value,null);assert.deepEqual(s.issues.value.map((i:any)=>i.section),['meaning','type']);assert.equal(calls.some(c=>c[0]==='transition'),false);c.app.unmount()
  })
  await t.test('unknown create blocks duplicate save, transitions and switching; empty readback stays unknown',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.label='保留';let writes=0;api.create=async()=>{writes++;throw Error('timeout')};await s.save();await s.save();await s.open('v1');s.begin();await s.transition('publish');assert.equal(writes,1);assert.equal(s.form.label,'保留')
    api.list=async()=>[];await s.readback();assert.equal(s.recoveryChecked.value,true);assert.ok(s.unknown.value);assert.equal(s.selected.value,null);c.app.unmount()
  })
  await t.test('unknown successor cannot be cleared by predecessor or known version; explicit new-record load is not claimed as a receipt',async()=>{
    reset();const c=await mount(),s=c.state;await s.open('v1');s.begin(true);api.create=async()=>{throw Error('timeout')};await s.save();api.list=async()=>[row(),row('new','draft',9)];await s.readback();await s.adoptRecovery('v1');assert.ok(s.unknown.value);assert.match(s.error.value,/原请求前/)
    saved=row('new','draft',9);await s.adoptRecovery('new');assert.equal(s.selected.value.id,'new');assert.equal(s.rows.value[0].id,'new');assert.equal(s.rows.value.filter(item=>item.id==='new').length,1);assert.equal(s.unknown.value,null);assert.match(s.notice.value,/不认定/);c.app.unmount()
  })
  await t.test('unknown update reads exact original ID and loads explicitly without retry',async()=>{
    reset();saved=row('draft','draft');const c=await mount(),s=c.state;await s.open('draft');s.form.label='本地';let writes=0;api.update=async()=>{writes++;throw{response:{status:504}}};await s.save();saved={...saved,label:'服务器原值'};await s.readback();assert.equal(s.form.label,'本地');await s.adoptRecovery('draft');assert.equal(s.form.label,'服务器原值');assert.equal(writes,1);c.app.unmount()
  })
  await t.test('wrong receipt identity/state locks writer, wrong GET cannot overwrite editor',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.label='本地';api.create=async()=>row('other','published');await s.save();assert.ok(s.unknown.value);assert.equal(s.selected.value,null)
    api.list=async()=>[row('new','draft')];await s.readback();api.get=async()=>({...row('new','draft'),field_definition:'foreign'});await s.adoptRecovery('new');assert.ok(s.unknown.value);assert.equal(s.form.label,'本地');c.app.unmount()
  })
  await t.test('parent close and version switch require inline discard; keep restores local form',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.label='原值';c.exposed.requestClose();assert.ok(s.leavePrompt.value);assert.equal(c.events.some(c=>c[0]==='close'),false);s.keepEditing();assert.equal(s.form.label,'原值')
    await s.open('v1');assert.equal(s.selected.value,null);s.discardAndLeave();await settle();assert.equal(s.selected.value.id,'v1');c.exposed.requestClose();assert.equal(c.events.filter(c=>c[0]==='close').length,1);c.app.unmount()
  })
  await t.test('route update asks once, explicit discard enters requested exact version',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.label='路由原值';const attempt=c.router.push('/settings?field_version=v1');await settle();assert.ok(s.leavePrompt.value);s.discardAndLeave();await attempt;await settle();assert.equal(s.selected.value.id,'v1');assert.equal(s.leavePrompt.value,'');c.app.unmount()
  })
  await t.test('route cancellation and browser unload preserve local state',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();s.form.label='保留';const leaving=s.mayLeave();s.keepEditing();assert.equal(await leaving,false);let prevented=false;s.beforeUnload({preventDefault(){prevented=true},returnValue:null});assert.ok(prevented);assert.equal(s.form.label,'保留');c.app.unmount()
  })
  await t.test('late writer after account change or unmount never applies or continues publish',async()=>{
    for(const mode of ['account','unmount']){reset();saved=row('draft','draft');const c=await mount(),s=c.state;await s.open('draft');s.form.label='迟到';const d=deferred();api.update=()=>d.promise;const writing=s.transition('publish');if(mode==='account')c.auth.user={id:'other'};else c.app.unmount();d.resolve({...row('draft','draft'),label:'receipt'});await writing;assert.equal(calls.some(c=>c[0]==='transition'),false);if(mode==='account'){assert.equal(s.selected.value,null);assert.equal(s.form.label,'');c.app.unmount()}else assert.equal(s.form.label,'迟到')}
  })
  await t.test('catalog failure stops new editing/writes but not saved semantic view',async()=>{
    reset();api.options=async()=>{throw{response:{status:503}}};const c=await mount(),s=c.state;assert.equal(s.options.value,null);assert.equal(s.rows.value.length,1);await s.open('v1');assert.equal(s.selected.value.id,'v1');s.begin(true);assert.equal(s.selected.value.id,'v1');await s.save();assert.equal(calls.some(c=>c[0]==='create'),false);c.app.unmount()
  })
  await t.test('failed version directory cannot create from incomplete baseline; failure remains visible after detail read',async()=>{
    reset();api.list=async()=>{throw Error('目录暂不可用')};const c=await mount(),s=c.state;await s.open('v1');assert.match(s.directoryError.value,/目录暂不可用/);assert.equal(s.directoryComplete.value,false);s.begin(true);assert.equal(s.selected.value.id,'v1');s.selected.value=null;await s.save();assert.equal(calls.some(c=>c[0]==='create'),false);c.app.unmount()
  })
  await t.test('unknown create recovery checks original maximum, not only IDs and not source plus one',async()=>{
    reset();api.list=async()=>[row(),row('v7','published',7)];const c=await mount(),s=c.state;await s.open('v1');s.begin(true);api.create=async()=>{throw Error('timeout')};await s.save();api.list=async()=>[row('previous-unlisted','draft',4),row('v9','draft',9)];await s.readback();saved=row('previous-unlisted','draft',4);await s.adoptRecovery('previous-unlisted');assert.ok(s.unknown.value);saved=row('v9','draft',9);await s.adoptRecovery('v9');assert.equal(s.selected.value.version,9);c.app.unmount()
  })
  await t.test('duplicate click while pending sends one request with truthful busy phase',async()=>{
    reset();const c=await mount(),s=c.state;s.begin();const d=deferred();let writes=0;api.create=()=>{writes++;return d.promise};const writing=s.save();await s.save();assert.equal(writes,1);assert.equal(s.busy.value,true);assert.match(s.phase.value,/保存/);d.resolve(row('new','draft',8));await writing;assert.equal(s.busy.value,false);c.app.unmount()
  })
  await t.test('field identity and version semantic readers preserve false, original type and exact identity',async()=>{
    reset();platformApi.getFieldDefinition=async(id:string)=>({id,key:'scene.action',isActive:false});const identity=await readResourceDetails('field','f1');assert.equal(identity.status,'inactive');assert.equal(identity.sections[0].facts[1].value,false)
    const detail=fieldVersionDetails(row());assert.equal(detail.sections[0].facts[2].value,'原值 🟢');assert.equal(detail.sections[1].facts[0].value,'text');platformApi.getFieldDefinition=async()=>({id:'foreign'});await assert.rejects(()=>readResourceDetails('field','f1'),/身份不匹配/)
  })
  await t.test('parent delayed deep link cannot replace manual selection or reopen after close',async()=>{
    for(const action of ['select','close']){reset();platformApi.fieldDefinitionPage=async()=>({count:0,results:[]});const c=await mount(Parent),s=c.state;s.props.fieldDefinitionId='f1';await settle();const d=deferred();platformApi.getFieldDefinition=()=>d.promise;const reading=s.openDeepLink();if(action==='select')s.openVersions({id:'f2',key:'other'});else s.closeVersions();d.resolve({id:'f1',key:'first'});await reading;assert.equal(s.versions.value?.id,action==='select'?'f2':undefined);c.app.unmount()}
  })
  await t.test('parent account change discards old catalog and late field read without reopening modal',async()=>{
    reset();platformApi.fieldDefinitionPage=async()=>({count:0,results:[]});const c=await mount(Parent),s=c.state;s.props.fieldDefinitionId='f1';await settle();const d=deferred();platformApi.getFieldDefinition=()=>d.promise;const reading=s.openDeepLink();c.auth.user={id:'next'};d.resolve({id:'f1',key:'old'});await reading;assert.equal(s.versions.value,null);assert.equal(s.rows.value.length,0);c.app.unmount()
  })
})
