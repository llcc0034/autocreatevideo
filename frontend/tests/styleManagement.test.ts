import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { createPinia } from 'pinia'
import { styleForm, styleWritePayload, styleReferenceCount, type ManagedStyle } from '../src/services/styleManagement.ts'

const row = { id:'one',slug:'stable',name:'武侠',category:'wuxia',category_label:'武侠',category_active:true,description:'简介',visual_prompt:'visual',negative_prompt:'negative',scope:'global',is_active:true,is_system:true,can_manage:true,asset_count:1,project_count:2,generation_job_count:3,updated_at:'now' } satisfies ManagedStyle
test('style edit submits only changed business fields, never identity or scope', () => {
  const form = styleForm(row)
  assert.deepEqual(styleWritePayload(form,row), {})
  form.name = ' 新名称 '; form.scope = 'personal'
  assert.deepEqual(styleWritePayload(form,row), {name:'新名称'})
  assert.equal(styleReferenceCount(row),6)
})
test('style creation separates display description from model-facing instructions', () => {
  const form = styleForm()
  assert.equal(form.category,null)
  form.name='我的风格';form.description='用于选择';form.visual_prompt='soft light'
  const payload=styleWritePayload(form)
  assert.equal(payload.description,'用于选择');assert.equal(payload.visual_prompt,'soft light')
  assert.ok(!('slug' in payload));assert.ok(!('defaults' in payload))
})
test('both entries reuse one manager without leaving the project creation form', () => {
  for(const name of ['AssetLibraryView','SeriesStudioView']){
    const source=readFileSync(new URL(`../src/views/${name}.vue`,import.meta.url),'utf8')
    assert.match(source, /VisualStyleManager/);assert.match(source,/管理视觉风格/)
  }
  const source=readFileSync(new URL('../src/views/SeriesStudioView.vue',import.meta.url),'utf8')
  assert.match(source,/v-for="style in selectableStyles"/)
  const manager=readFileSync(new URL('../src/components/VisualStyleManager.vue',import.meta.url),'utf8')
  assert.match(manager,/api.get\('\/styles\/catalog\/'\)/)
  assert.match(manager,/styleReferenceCount\(selected\)>0/)
  assert.match(manager,/!confirmed \|\| busy/)
})

test('manager loads server choices, writes changes, refreshes selectors and reports failed saves honestly', async () => {
  const vite = await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  const {api}=await vite.ssrLoadModule('/src/services/api.ts')
  const {useStudioStore}=await vite.ssrLoadModule('/src/stores/studio.ts')
  const {default:Component}=await vite.ssrLoadModule('/src/components/VisualStyleManager.vue')
  const original={get:api.get,options:api.options,post:api.post,patch:api.patch,delete:api.delete}
  const storageDescriptor=Object.getOwnPropertyDescriptor(globalThis,'localStorage')
  Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=>null,setItem(){},removeItem(){}}})
  const calls:any[]=[];let refreshed=0,state:any, app:any,fail=false
  const settle=async()=>{for(let i=0;i<14;i++){await Promise.resolve();await nextTick()}}
  try {
    api.get=async(url:any)=>({data:url.includes('catalog')?{can_manage_categories:true,scopes:[{value:'personal',display_name:'我的风格'}]}:url.includes('style-categories')?{results:[{key:'wuxia',name:'武侠',is_active:true}],next:null}:url.includes('references')?{count:0,results:[],next:null}:{count:1,results:[row],next:null}})
    api.post=async(url:any,payload:any)=>{calls.push(['post',payload]);return{data:{...row,...payload,id:'created'}}}
    api.patch=async(url:any,payload:any)=>{if(fail)throw Error('save refused');calls.push(['patch',payload]);return{data:{...row,...payload}}}
    api.delete=async()=>{calls.push(['delete']);return{}}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    const pinia=createPinia();useStudioStore(pinia).loadStyles=async(force:boolean)=>{assert.equal(force,true);refreshed++}
    app=renderer.createApp({setup(){state=Component.setup(reactive({open:true}),{expose(){},emit(){}});return()=>h('div')}})
    app.use(pinia);app.provide(ssrContextKey,{modules:new Set()});app.mount({});await settle()
    assert.equal(state.rows.value.length,1);assert.equal(state.categories.value[0].key,'wuxia')
    state.create();state.form.value.name='测试';state.form.value.category='wuxia';await state.save()
    assert.equal(calls[0][0],'post');assert.equal(state.mode.value,'view');assert.equal(refreshed,1)
    state.inspect(row);state.mode.value='edit';state.form.value.description='新简介';await state.save()
    assert.deepEqual(calls[1],['patch',{description:'新简介'}]);assert.equal(refreshed,2)
    state.action.value='toggle';state.confirmed.value=false;await state.mutate();assert.equal(calls.length,2)
    state.confirmed.value=true;await state.mutate();assert.deepEqual(calls[2],['patch',{is_active:false}])
    state.inspect(row);state.mode.value='edit';state.form.value.name='不应假保存';fail=true;await state.save()
    assert.match(state.error.value,/save refused/);assert.equal(state.mode.value,'edit');assert.equal(state.message.value,'');assert.equal(refreshed,3)
    fail=false;state.inspect({...row,asset_count:0,project_count:0,generation_job_count:0});state.action.value='delete';state.confirmed.value=true;await state.mutate()
    assert.equal(calls.at(-1)[0],'delete');assert.equal(state.selected.value,null);assert.equal(refreshed,4)
    state.create();state.form.value.name='未保存风格';state.form.value.visual_prompt='preserve this';state.form.value.category='removed'
    await state.categoriesChanged()
    assert.equal(state.form.value.category,null);assert.equal(state.form.value.name,'未保存风格');assert.equal(state.form.value.visual_prompt,'preserve this')
    assert.equal(state.canSave.value,true)
    state.categories.value[0].is_active=false;state.form.value.category='wuxia';assert.equal(state.canSave.value,false)
    state.inspect({...row,category_active:false});assert.equal(state.canSave.value,true)
    state.categoryReferences('wuxia');assert.equal(state.categoryFilter.value,'wuxia');assert.equal(state.managingCategories.value,false)
  } finally {app?.unmount();Object.assign(api,original);if(storageDescriptor)Object.defineProperty(globalThis,'localStorage',storageDescriptor);else delete globalThis.localStorage;await vite.close()}
})

test('category manager CRUD uses server data, confirms changes, and retains rejected edits', async () => {
  const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  const {api}=await vite.ssrLoadModule('/src/services/api.ts')
  const {default:Component}=await vite.ssrLoadModule('/src/components/StyleCategoryManager.vue')
  const original={get:api.get,post:api.post,patch:api.patch,delete:api.delete}
  let state:any,app:any,fail=false;const calls:any[]=[],events:any[]=[]
  const category={id:'category-one',key:'server-key',name:'校园',description:'学校生活',sort_order:0,is_active:true,style_count:0,can_manage:true}
  try{
    api.get=async()=>({data:{results:[category],count:1,next:null}})
    api.post=async(url:any,data:any)=>{calls.push(['create',data]);return{data:{...category,...data}}}
    api.patch=async(url:any,data:any)=>{if(fail)throw Error('category rejected');calls.push(['update',data]);return{data:{...category,...data}}}
    api.delete=async()=>{calls.push(['delete']);return{}}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({setup(){state=Component.setup(reactive({canManage:true}),{expose(){},emit:(...args:any[])=>events.push(args)});return()=>h('div')}})
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await state.load()
    assert.equal(state.rows.value[0].name,'校园')
    state.edit();state.form.value.name=' 新类别 ';await state.save();assert.equal(calls[0][1].name,'新类别');assert.ok(!('key' in calls[0][1]))
    state.edit(category);state.form.value.is_active=false;await state.save();assert.equal(calls.length,1)
    state.confirmed.value=true;await state.save();assert.deepEqual(calls[1],['update',{is_active:false}])
    state.edit(category);state.form.value.name='保持输入';fail=true;await state.save();assert.match(state.error.value,/category rejected/);assert.equal(state.form.value.name,'保持输入');assert.equal(state.editing.value,true)
    fail=false;state.edit(category);await state.remove();assert.equal(calls.length,2)
    state.confirmed.value=true;await state.remove();assert.deepEqual(calls.at(-1),['delete']);assert.equal(state.selected.value,null);assert.equal(events.filter(e=>e[0]==='changed').length,3)
  } finally {app?.unmount();Object.assign(api,original);await vite.close()}
})
