import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'

test('system policy replaces recipe and per-run allowances without external consent', async () => {
  const vite = await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',server:{middlewareMode:true,hmr:false}})
  try {
    const source=readFileSync(new URL('../src/services/productionBudget.ts',import.meta.url),'utf8')
    assert.doesNotMatch(source,/recipeBudgetDefaults|budgetInputError|manualBudgetInputError/)
    const p=await vite.ssrLoadModule('/src/services/productionRunPayload.ts')
    const input={planSource:'recipe',projectId:'p',recipeVersionId:'r',idempotencyKey:'k',scriptMode:'downstream',episodeIds:['e']}
    const payload=p.buildProductionRunRequestPayload(input)
    for(const key of ['budget_limit','budget_currency','max_generation_tasks'])assert.equal(key in payload,false)
    const {productionIssuePresentation}=await vite.ssrLoadModule('/src/services/productionRunPresentation.ts')
    const unauthorized=productionIssuePresentation('external_call_not_authorized','', 'blocked')
    assert.match(unauthorized.summary,/允许本轮外部模型调用/)
    assert.match(unauthorized.summary,/对应执行准备/)
    assert.doesNotMatch(unauthorized.summary,/设置预算|改用本地路由/)
    assert.throws(()=>p.buildProductionRunRequestPayload({...input,budgetLimit:'2',budgetCurrency:'USD'}),/未声明字段/)
  } finally { await vite.close() }
})

test('system resource summary loads authoritative policy, exposes failures and never consents', async () => {
  const vite = await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',server:{middlewareMode:true,hmr:false}})
  let app:any
  try {
    const {api} = await vite.ssrLoadModule('/src/services/api.ts')
    const values={budget_by_currency:{USD:'1.0000',CNY:'10'},call_count:100,output_length:8192}
    let response:any={resource_policy:{values}}, fail=false,calls=0
    api.get=async(path:string)=>{assert.equal(path,'/production-runs/budget-options/');calls++;if(fail)throw Error('策略读取失败');return {data:response}}
    const {default:C}=await vite.ssrLoadModule('/src/components/ProductionBudgetFields.vue')
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    let state:any;const emitted:any[]=[]
    const props={external:false}
    app=renderer.createApp({setup(){state=C.setup(props,{emit:(...args:any[])=>emitted.push(args),expose(){}});return()=>h('div')}})
    app.provide(ssrContextKey,{modules:new Set()});app.mount({})
    for(let n=0;n<10;n++){await Promise.resolve();await nextTick()}
    assert.equal(calls,1);assert.deepEqual(state.policy.value,values);assert.deepEqual(emitted,[])
    fail=true;await state.load();assert.match(state.error.value,/策略读取失败/);assert.equal(state.policy.value,null)
    fail=false;response={resource_policy:{values:{...values,call_count:0}}};await state.load();assert.match(state.error.value,/不完整/);assert.equal(state.policy.value,null)
    response={resource_policy:{values:{...values,budget_by_currency:{}}}};await state.load();assert.equal(state.error.value,'')
    assert.deepEqual(emitted,[]);assert.equal(props.external,false)
    const source=readFileSync(new URL('../src/components/ProductionBudgetFields.vue',import.meta.url),'utf8')
    assert.doesNotMatch(source,/<input :value="(?:budget|currency)"/)
    assert.match(source,/系统设置/);assert.match(source,/超额保留任务/)
  } finally { app?.unmount();await vite.close() }
})

test('system policy editor changes only the draft and obtains currency options without inventing defaults',async()=>{
  const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',server:{middlewareMode:true,hmr:false}})
  let app:any
  try{
    const {api}=await vite.ssrLoadModule('/src/services/api.ts')
    let fail=false
    api.get=async()=>{if(fail)throw Error('币种读取失败');return {data:{currencies:[{value:'USD',label:'美元'},{value:'CNY',label:'人民币'}]}}}
    const {default:C}=await vite.ssrLoadModule('/src/components/SystemResourcePolicyEditor.vue')
    const props=reactive({modelValue:JSON.stringify({budget_by_currency:{},call_count:100,output_length:4096}),disabled:false})
    const changes:any[]=[];let state:any
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({setup(){state=C.setup(props,{emit:(_e,value)=>{changes.push(value);props.modelValue=value},expose(){}});return()=>h('div')}})
    app.provide(ssrContextKey,{modules:new Set()});app.mount({})
    for(let n=0;n<12;n++){await Promise.resolve();await nextTick()}
    assert.equal(changes.length,0);assert.equal(state.selectedCurrency.value,'')
    state.selectedCurrency.value='USD';state.add();await nextTick()
    assert.equal(JSON.parse(props.modelValue).budget_by_currency.USD,'')
    state.amount('USD','20');state.update({output_length:8192});await nextTick()
    assert.deepEqual(JSON.parse(props.modelValue),{budget_by_currency:{USD:'20'},call_count:100,output_length:8192})
    state.remove('USD');await nextTick();assert.deepEqual(JSON.parse(props.modelValue).budget_by_currency,{})
    fail=true;await state.load();assert.match(state.error.value,/币种读取失败/);assert.deepEqual(state.currencies.value,[])
    props.modelValue='broken';await nextTick();assert.equal(state.value.value,null)
  }finally{app?.unmount();await vite.close()}
})

test('creation does not silently pre-authorize and manual run has a non-executing budget entry',()=>{
  const series=readFileSync(new URL('../src/views/SeriesStudioView.vue',import.meta.url),'utf8')
  assert.match(series,/const aiAllowExternalCalls = ref\(false\)/)
  assert.doesNotMatch(series,/ref\('1\.00'\)/)
  assert.match(series,/allowExternalCalls: false/)
  assert.doesNotMatch(series,/budgetLimit:/)
  const run=readFileSync(new URL('../src/views/ProductionRunView.vue',import.meta.url),'utf8')
  assert.match(run,/保存本轮调用设置（不执行模型）/)
  assert.doesNotMatch(run,/允许本阶段外发/)
})

test('creation forms neither expose nor secretly submit a model call limit; run evidence remains visible',()=>{
  for(const file of ['views/SeriesStudioView.vue','views/ProductionRunView.vue','components/ManualProductionCreateForm.vue']){
    const source=readFileSync(new URL(`../src/${file}`,import.meta.url),'utf8')
    assert.doesNotMatch(source,/高级设置 · 模型调用次数|本轮模型调用次数上限|(?:aiMaxGenerationTasks|taskLimit|maxGenerationTasks)\.value|maxGenerationTasks:/)
  }
  const summary=readFileSync(new URL('../src/components/RecipeExecutionSummary.vue',import.meta.url),'utf8')
  assert.doesNotMatch(summary,/模型调用次数上限/)
  const run=readFileSync(new URL('../src/views/ProductionRunView.vue',import.meta.url),'utf8')
  assert.match(run,/currentRun\.generationTaskCount.*额度按当前系统策略检查/)
  const manual=readFileSync(new URL('../src/components/ManualProductionWorkbench.vue',import.meta.url),'utf8')
  assert.match(manual,/run\.generationTaskCount/)
  assert.doesNotMatch(manual,/run\.maxGenerationTasks/)
})
