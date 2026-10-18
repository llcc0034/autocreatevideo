import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { visualRuleReviewFixture } from './visualRulesOutputFixture.ts'

const root = fileURLToPath(new URL('..', import.meta.url)), selector = 'reviewed_visual_rules.continuity_summary'
const source = () => ({run_id:'run',project_id:'project',stage_key:'media_generation',run_plan_sha256:'a'.repeat(64),review_contract:visualRuleReviewFixture(),sources:[{
  selector_key:selector,label:'已审核连续性说明',content:'Reviewed continuity.',content_sha256:'b'.repeat(64),
  review:{decision_id:'decision',revision:2,snapshot_sha256:'c'.repeat(64)},
  output:{protocol:'reviewed_visual_rules/v1',schema_version:3,field:'continuity_summary',attempt_id:'attempt',artifact_id:'artifact',reviewed_rules_sha256:'d'.repeat(64),business_output:visualRuleReviewFixture().output}}]})

test('reviewed output reader rejects mismatched identity, duplicate output and unsupported protocol',async()=>{
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const {readReviewedVisualSource:read}=await vite.ssrLoadModule('/src/services/executionReviewedVisualSource.ts')
    assert.equal(read(source(),'run','media_generation','project',selector).content,'Reviewed continuity.')
    for(const mutate of [(v)=>{v.run_id='foreign'},(v)=>{v.sources.push(v.sources[0])},(v)=>{v.sources[0].review.revision=0},
      (v)=>{v.sources[0].output.schema_version=2},(v)=>{v.sources[0].content=null},(v)=>{v.sources[0].output.field='color_script'},
      (v)=>{v.sources[0].output.business_output.id='different-output'},(v)=>{v.review_contract=null},
      (v)=>{v.review_contract.fields[0].source_selector='reviewed_visual_rules.undeclared'}]) {
      const value=source();mutate(value);assert.throws(()=>read(value,'run','media_generation','project',selector))
    }
    const {mappingSourceChoices}=await vite.ssrLoadModule('/src/services/recipeStructureMapping.ts')
    const choices=[{selector_key:selector,kind:'text',scope_kind:'project',revision_kind:'visual_rules_approval',required_stage_key:'visual_rules_review'}]
    assert.equal(mappingSourceChoices(choices,{type:'string'},['project'],[]).length,0)
    assert.equal(mappingSourceChoices(choices,{type:'string'},['project'],['visual_rules_review']).length,1)
    const {emptyExecutionRequestDraft}=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const contract={inputs:[{key:selector,kind:'text',required:true,min_items:1,semantic_contract:{allowed_scope_kinds:['project']}}],
      contextFields:choices,sourceConstraints:{[selector]:{kinds:['context_field'],selectors:[selector]}}}
    assert.deepEqual(emptyExecutionRequestDraft(contract).contexts[selector],{selectorKey:selector,projectId:'',revisionId:''})
    contract.sourceConstraints[selector].selectors.push('another.source')
    assert.equal(emptyExecutionRequestDraft(contract).contexts[selector],null)
  } finally {await vite.close()}
})

test('component previews on demand, confirms only the revision, invalidates on change and discards late responses',async()=>{
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  let app:any
  try {
    const Component=(await vite.ssrLoadModule('/src/components/ExecutionReviewedVisualSource.vue')).default
    const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
    let calls=0,state:any,props:any
    platformApi.getReviewedVisualSources=async()=>{calls++;return source()}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({...Component,setup(p,ctx){props=p;state=Component.setup(p,ctx);return()=>null}},
      {modelValue:{selectorKey:selector,projectId:'project',revisionId:''},runId:'run',stageKey:'media_generation',projectId:'project'})
    app.provide(ssrContextKey,{});app.mount({});assert.equal(calls,0)
    await state.load();assert.equal(calls,1);assert.equal(state.model.value.revisionId,'')
    state.confirm();assert.equal(state.model.value.revisionId,'decision');assert.equal(state.model.value.content,undefined)
    platformApi.getReviewedVisualSources=async()=>{throw new Error('Source changed')}
    await state.load();assert.equal(state.model.value.revisionId,'');assert.equal(state.preview.value,null);assert.match(state.error.value,/Source changed/)
    let resolve:any
    platformApi.getReviewedVisualSources=()=>new Promise(r=>{resolve=r})
    const pending=state.load();state.model.value={...state.model.value,selectorKey:'reviewed_visual_rules.color_script'};await nextTick()
    resolve(source());await pending;assert.equal(state.preview.value,null);assert.equal(state.model.value.revisionId,'')
    const html=await renderToString(createSSRApp({render:()=>h(Component,{modelValue:{selectorKey:selector,projectId:'',revisionId:''}})}))
    assert.match(html,/组合烟测请先选择来源轮次与参考步骤/)
    assert.match(html,/基础资源烟测不提供生产审核上下文/)
  } finally {app?.unmount();await vite.close()}
})
