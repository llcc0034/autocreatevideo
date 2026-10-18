import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'

const root = fileURLToPath(new URL('..', import.meta.url))
const raw = { run_id:'run', current_script:{id:'revision',sha256:'a'.repeat(64)}, current_script_decision_id:'decision',
  status:'structured',reason_code:'',reason:'本次采用修订已有保存的结构化结果',episode_count:2,shot_count:12 }
const checked = () => ({runId:raw.run_id,currentScript:{...raw.current_script},currentScriptDecisionId:raw.current_script_decision_id,
  status:raw.status,reasonCode:raw.reason_code,reason:raw.reason,episodeCount:raw.episode_count,shotCount:raw.shot_count})
const options = () => ({runId:'run',currentScript:{...raw.current_script},currentScriptDecisionId:'decision'})
const settle = async () => { for(let i=0;i<8;i++){await Promise.resolve();await nextTick()} }

test('parser transport rejects unknown states, absent basis and incompatible counts', async () => {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
  const {adaptManualScriptStructureCheck}=await vite.ssrLoadModule('/src/services/productionManual.ts')
  assert.equal(adaptManualScriptStructureCheck(raw).shotCount,12)
  for(const change of [{status:'succeeded'},{status:'canonical'},{status:'not_canonical'},{current_script:null},{current_script_decision_id:null},
    {reason_code:'unexpected'},{episode_count:0},{episode_count:-1},{shot_count:-1},{shot_count:null},{shot_count:0.5},{reason:''},
    {current_script:{id:'revision',sha256:'bad'}}]) assert.throws(()=>adaptManualScriptStructureCheck({...raw,...change}))
  assert.equal(adaptManualScriptStructureCheck({...raw,episode_count:0,shot_count:0}).episodeCount,0)
  assert.equal(adaptManualScriptStructureCheck({...raw,shot_count:0}).shotCount,0)
  assert.equal(adaptManualScriptStructureCheck({...raw,shot_count:1}).shotCount,1)
  const plain={...raw,status:'unstructured',reason_code:'script_structure_required',episode_count:null,shot_count:null}
  assert.equal(adaptManualScriptStructureCheck(plain).status,'unstructured')
  assert.throws(()=>adaptManualScriptStructureCheck({...plain,reason_code:''}))
  assert.throws(()=>adaptManualScriptStructureCheck({...plain,reason_code:'script_structure_not_canonical'}))
  assert.throws(()=>adaptManualScriptStructureCheck({...plain,shot_count:12}))
  assert.equal(adaptManualScriptStructureCheck({...plain,status:'no_script',reason_code:'',current_script:null,current_script_decision_id:null}).status,'no_script')
  } finally { await vite.close() }
})

async function withForm(fn:(state:any,props:any,api:any)=>Promise<void>) {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  const {platformApi:api}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const old={...api}
  api.performManualProductionAction=async()=>{throw Error('must not create or approve anything')}
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualScriptStructureCheck.vue')
  const props=reactive({runId:'run',options:options()}), renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  let state:any
  const app=renderer.createApp({setup(){state=C.setup(props,{emit(){},expose(){}});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({})
  try{await fn(state,props,api)}finally{app.unmount();Object.assign(api,old);await vite.close()}
}

test('only explicit checks read saved structure, routine polling preserves evidence, changed adoption marks it stale',()=>withForm(async(state,props,api)=>{
  let calls=0
  api.getManualScriptStructure=async()=>{calls++;return checked()}
  await settle();assert.equal(calls,0)
  await state.check();assert.equal(calls,1);assert.equal(state.result.value.shotCount,12)
  props.options=null;await settle();assert.equal(state.result.value.shotCount,12);assert.equal(state.current.value,false)
  props.options=options();await settle();assert.equal(state.stale.value,false);assert.equal(state.current.value,true);assert.equal(calls,1)
  props.options={...options(),currentScriptDecisionId:'another-decision'};await settle()
  assert.equal(state.stale.value,true);assert.equal(calls,1)
  props.options={...options(),currentScript:{...raw.current_script,sha256:'b'.repeat(64)}};await settle()
  assert.equal(state.stale.value,true)
}))

test('late response cannot cross run identity or replace failure with an old pass',()=>withForm(async(state,props,api)=>{
  let resolve:any
  api.getManualScriptStructure=()=>new Promise(r=>{resolve=r})
  const pending=state.check();assert.equal(state.busy.value,true)
  props.runId='other-run';props.options={...options(),runId:'other-run'};await settle()
  resolve(checked());await pending;assert.equal(state.result.value,null)
  api.getManualScriptStructure=async()=>checked()
  await state.check();assert.match(state.error.value,/不属于当前轮次/);assert.equal(state.result.value,null)
  props.runId='run';props.options=options();await settle()
  await state.check();assert.equal(state.result.value.status,'structured')
  api.getManualScriptStructure=async()=>{throw Error('connection unavailable')}
  await state.check();assert.equal(state.result.value,null);assert.ok(state.error.value)
}))
