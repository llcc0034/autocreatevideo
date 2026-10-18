import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'

const root = fileURLToPath(new URL('..', import.meta.url))
const hash = 'a'.repeat(64)
function fixture(value:unknown = {count:0,enabled:false,note:null,extra:'实际未映射内容'}) {
  const declarations = ['first','second'].map(key=>({key,kind:'structured_text',name:'相同名称',routing:{evaluator:null}}))
  const stages = declarations.map((row,index)=>({id:row.key,stageKey:row.key,ordinal:index+1,status:'awaiting_input',artifactSelectionDecisions:[]}))
  const run = {id:'run',planSource:'recipe',status:'awaiting_input',executionPlan:{sha256:hash,value:{recipe:{definition:{stages:declarations}}}},stages}
  const output = {id:'output',sha256:hash,data_structure:{id:'structure'}}
  const artifact = {id:'candidate',productionRun:'run',stageRun:'first',role:'structured_text_candidate',status:'available',variantGroup:'group',variantIndex:1,sha256:hash,manifestMetadataSha256:hash,
    textCandidate:{operation:'text.structured',preparation_id:'prep',candidate:{schema_version:1,output_version_id:'output',output_contract_sha256:hash,structure_version_id:'structure',value,defaults:[]},output_contract:output,json_schema:{type:typeof value==='boolean'?'boolean':'object'},structure_warnings:[]}}
  return {run,stage:stages[0],artifact}
}
async function modules(fn) {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true}})
  try { await fn(await vite.ssrLoadModule('/src/services/productionStructuredText.ts'),vite) } finally { await vite.close() }
}
const settle=async()=>{for(let i=0;i<8;i++){await Promise.resolve();await nextTick()}}
async function mount(vite,fixtureData=fixture()) {
  const {platformApi:api}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const {default:Component}=await vite.ssrLoadModule('/src/components/ProductionStructuredTextResults.vue')
  const calls:any[]=[]
  api.listArtifactPage=async params=>{calls.push(['list',params]);return {results:[fixtureData.artifact],count:1,hasNext:false,hasPrevious:false}}
  api.getArtifact=async id=>{calls.push(['get',id]);return structuredClone(fixtureData.artifact)}
  api.selectArtifact=async(run,body,key)=>{calls.push(['select',run,body,key]);return {id:crypto.randomUUID(),action:'select',result_kind:'artifact_selection',target_id:crypto.randomUUID(),created_at:new Date().toISOString()}}
  let state:any
  const props=reactive({...fixtureData,disabled:false})
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(...args)=>calls.push(['emit',...args])});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({});await settle()
  return {api,app,props,state,calls}
}

test('frozen repeatable names preserve exact ordinal and identity; no current catalog fallback',()=>modules(async m=>{
  const {run,stage}=fixture()
  assert.equal(m.runStageLabel(run,stage),'相同名称')
  assert.notEqual(m.structuredTextStep(run,stage).key,m.structuredTextStep(run,run.stages[1]).key)
  assert.throws(()=>m.structuredTextStep(run,{...stage,ordinal:2}),/顺序或身份/)
  assert.throws(()=>m.structuredTextStep({...run,executionPlan:null},stage),/合同/)
  assert.match(m.runStageLabel({...run,executionPlan:null},stage),/读取失败/)
  assert.equal(m.structuredTextStep({...run,planSource:'manual'},stage),null)
}))
test('manual generic selection wait points at the original result panel, not stage approval',()=>modules(async(_,vite)=>{
  const {productionIssuePresentation}=await vite.ssrLoadModule('/src/services/productionRunPresentation.ts')
  const result=productionIssuePresentation('execution_candidate_selection_required','','awaiting_input')
  assert.match(result.summary,/人工选择通用文本候选/)
  assert.match(result.summary,/不会自动重试/)
  assert.equal(result.summary.includes('运行会自动继续'),false)
}))
test('actual root and nested values preserve false zero null lists and unknown fields',()=>modules(async m=>{
  for(const value of [false,0,null,[],{},[false,0,null],{a:false,b:0,c:null,unmapped:'保留'}]) {
    const {artifact}=fixture(value)
    assert.deepEqual(m.readStructuredTextDetail(artifact,'run','first').value,value)
  }
  const {artifact}=fixture()
  delete artifact.textCandidate.candidate.value
  assert.throws(()=>m.readStructuredTextDetail(artifact,'run','first'),/结构不一致/)
}))
test('candidate source schema defaults and warnings must belong to original output',()=>modules(async m=>{
  const {artifact}=fixture()
  artifact.textCandidate.candidate.defaults=[{path:'response.count',value:0,structure_version_id:'structure'}]
  artifact.textCandidate.structure_warnings=[{path:'response.extra',message:'未映射字段保留'}]
  const parsed=m.readStructuredTextDetail(artifact,'run','first')
  assert.equal(parsed.defaults[0].value,0);assert.equal(parsed.warnings[0].message,'未映射字段保留')
  for(const field of ['output_version_id','output_contract_sha256','structure_version_id']) {
    const bad=structuredClone(artifact);bad.textCandidate.candidate[field]='wrong'
    assert.throws(()=>m.readStructuredTextDetail(bad,'run','first'),/结构不一致/)
  }
  assert.throws(()=>m.readStructuredTextDetail(artifact,'other','first'),/归属/)
  artifact.textCandidate.json_schema=null
  assert.throws(()=>m.readStructuredTextDetail(artifact,'run','first'),/合同/)
}))
test('selection modes are original decision facts, never inferred from automatic policy',()=>modules(async(m,vite)=>{
  const {stage,artifact}=fixture()
  stage.artifactSelectionDecisions=[{artifact:'candidate',variantGroup:'group',revision:1,selectionMode:'automatic',selectedBy:null}]
  assert.match(m.candidateSelectionLabel(stage,artifact),/自动选择（不是人工审核）/)
  stage.artifactSelectionDecisions.push({artifact:'candidate',variantGroup:'group',revision:2,selectionMode:'manual',selectedBy:'actor'})
  assert.match(m.candidateSelectionLabel(stage,artifact),/人工选择/)
  const {adaptProductionRun}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const run=adaptProductionRun({plan_source:'recipe',recipe_version:'recipe',stages:[{artifact_selection_decisions:[{id:'fact',artifact:'candidate',variant_group:'group',revision:2,selection_mode:'automatic',selected_by:null}]}]})
  assert.equal(run.stages[0].artifactSelectionDecisions[0].selectedBy,null)
  assert.equal(run.stages[0].artifactSelectionDecisions[0].selectionMode,'automatic')
}))
test('component opens readonly list and detail; only explicit original selection action writes',()=>modules(async(_,vite)=>{
  const ctx=await mount(vite)
  try {
    assert.equal(ctx.calls.filter(c=>c[0]==='select').length,0)
    await ctx.state.inspect(ctx.props.artifact)
    assert.deepEqual(ctx.state.detail.value.value,ctx.props.artifact.textCandidate.candidate.value)
    assert.equal(ctx.state.canSelect.value,true)
    await ctx.state.select();await settle()
    assert.deepEqual(ctx.calls.filter(c=>c[0]==='select').map(c=>c.slice(0,3)),[['select','run',{artifact_id:'candidate'}]])
    assert.match(ctx.state.selectionState.notice,/原选择回执已确认/)
    ctx.props.stage={...ctx.props.stage,artifactSelectionDecisions:[{artifact:'candidate',variantGroup:'group',revision:1,selectionMode:'manual',selectedBy:'actor'}]}
    ctx.props.run={...ctx.props.run};await settle()
    assert.match(ctx.state.selectionState.notice,/原选择回执已确认/)
    assert.ok(ctx.calls.some(c=>c[0]==='emit'&&c[1]==='changed'))
    const busyOff=ctx.calls.findIndex(c=>c[0]==='emit'&&c[1]==='protection'&&c[2].active===false)
    assert.ok(busyOff < ctx.calls.findIndex(c=>c[0]==='emit'&&c[1]==='changed'))
  }finally{ctx.app.unmount()}
}))
test('list failure retains prior list but disables writes; empty list stays explicit',()=>modules(async(_,vite)=>{
  const ctx=await mount(vite)
  try {
    await ctx.state.inspect(ctx.props.artifact)
    ctx.api.listArtifactPage=async()=>{throw Error('隔离读失败')}
    await ctx.state.load()
    assert.equal(ctx.state.rows.value.length,1);assert.equal(ctx.state.stale.value,true)
    assert.match(ctx.state.error.value,/隔离读失败/);assert.equal(ctx.state.canSelect.value,false)
    ctx.api.listArtifactPage=async()=>({results:[],count:0,hasNext:false,hasPrevious:false})
    await ctx.state.load();assert.equal(ctx.state.rows.value.length,0);assert.equal(ctx.state.stale.value,false)
  }finally{ctx.app.unmount()}
}))
test('stale response cannot render another step and ended runs cannot submit',()=>modules(async(_,vite)=>{
  const ctx=await mount(vite)
  try {
    let resolve:any
    ctx.api.getArtifact=()=>new Promise(r=>resolve=r)
    const pending=ctx.state.inspect(ctx.props.artifact)
    const next={...ctx.props.run.stages[1]};ctx.props.stage=next;await settle()
    resolve(structuredClone(fixture().artifact));await pending
    assert.equal(ctx.state.detail.value,null)
    assert.equal(ctx.state.canSelect.value,false)
  }finally{ctx.app.unmount()}
  const ended=await mount(vite)
  try {
    await ended.state.inspect(ended.props.artifact);ended.props.run.status='succeeded';await settle()
    assert.equal(ended.state.canSelect.value,false);await ended.state.select()
    assert.equal(ended.calls.filter(c=>c[0]==='select').length,0)
  }finally{ended.app.unmount()}
}))
test('double selection and detail failure never fabricate a successful result',()=>modules(async(_,vite)=>{
  const ctx=await mount(vite)
  try {
    ctx.api.getArtifact=async()=>({...fixture().artifact,stageRun:'other'})
    await ctx.state.inspect(ctx.props.artifact)
    assert.match(ctx.state.error.value,/归属/);assert.equal(ctx.state.detail.value,null)
    ctx.api.getArtifact=async()=>fixture().artifact
    await ctx.state.inspect(ctx.props.artifact)
    let reject:any;ctx.api.selectArtifact=()=>new Promise((_,r)=>reject=r)
    const pending=ctx.state.select();assert.equal(ctx.state.submitting.value,true)
    await ctx.state.select();reject(Error('隔离响应丢失'));await pending
    assert.ok(ctx.state.selectionState.pending);assert.equal(ctx.state.selectionState.notice,'')
    assert.match(ctx.state.selectionState.error,/隔离响应丢失/)
  }finally{ctx.app.unmount()}
}))

test('old receipt reloads current B and failed read recovers with GET only',()=>modules(async(_,vite)=>{
  const ctx=await mount(vite)
  try {
    await ctx.state.inspect(ctx.props.artifact)
    ctx.api.listArtifactPage=async()=>{throw Error('after receipt read failed')}
    await ctx.state.select();await settle()
    assert.equal(ctx.state.selectionState.pending,null);assert.match(ctx.state.selectionState.notice,/回执已确认/)
    assert.equal(ctx.state.stale.value,true);assert.match(ctx.state.error.value,/after receipt/)
    const writes=ctx.calls.filter(row=>row[0]==='select').length
    ctx.api.listArtifactPage=async()=>({results:[{...fixture().artifact,id:'B',status:'selected'}],count:1,hasNext:false,hasPrevious:false})
    await ctx.state.load();assert.equal(ctx.state.rows.value[0].id,'B');assert.equal(ctx.state.rows.value[0].status,'selected')
    assert.equal(ctx.calls.filter(row=>row[0]==='select').length,writes)
  }finally{ctx.app.unmount()}
}))

test('late selection success and failure cannot affect a new step pending command',()=>modules(async(_,vite)=>{
  for(const fails of [false,true]){
    const ctx=await mount(vite)
    try {
      await ctx.state.inspect(ctx.props.artifact);let finish:any
      ctx.api.selectArtifact=()=>new Promise((yes,no)=>finish=fails?no:yes)
      const pending=ctx.state.select();ctx.props.stage={...ctx.props.run.stages[1]};await settle()
      const eventCount=ctx.calls.length
      finish(fails?Error('late'):{id:crypto.randomUUID(),action:'select',result_kind:'artifact_selection',target_id:crypto.randomUUID(),created_at:new Date().toISOString()});await pending;await settle()
      assert.equal(ctx.calls.length,eventCount);assert.equal(ctx.state.selectionState.notice,'');assert.equal(ctx.state.selectionState.error,'')
    }finally{ctx.app.unmount()}
  }
}))

test('pagination uses server page facts and does not accumulate duplicate candidates',()=>modules(async(_,vite)=>{
  const ctx=await mount(vite)
  try {
    ctx.api.listArtifactPage=async params=>{assert.equal(params.page,2);return{results:[{...fixture().artifact,id:'page-two'}],count:21,hasNext:false,hasPrevious:true}}
    await ctx.state.load(2)
    assert.equal(ctx.state.page.value,2);assert.equal(ctx.state.count.value,21)
    assert.equal(ctx.state.hasPrevious.value,true);assert.equal(ctx.state.hasNext.value,false)
    assert.deepEqual(ctx.state.rows.value.map(row=>row.id),['page-two'])
    assert.equal(ctx.state.selected.value,null)
  }finally{ctx.app.unmount()}
}))
