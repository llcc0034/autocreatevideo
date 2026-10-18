import assert from 'node:assert/strict'
import { unusedLanguageForm } from './fixtures/executionLanguage.ts'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { readFileSync } from 'node:fs'

const root = fileURLToPath(new URL('..',import.meta.url)), id = n => `00000000-0000-4000-8000-${String(n).padStart(12,'0')}`
const request = () => ({id:id(1),job_id:id(2),preparation_id:id(3),execution_instance_version_id:id(4),status:'preparing',created_at:'2026-09-12T12:00:00Z',source_count:1})
const source = () => ({id:id(5),position:0,scope_type:'project',scope_id:id(6),field_key:'project.continuity_summary',expected_revision:0,adopted_entry_id:null,adopted_revision:null,adopted_at:null})
const page = row => ({count:1,next:null,previous:null,results:[row]})
const field = () => ({scopeType:'project',scopeId:id(6),fieldKey:'project.continuity_summary',sourceText:'她说 <d>别动。</d>。',label:'连续性'})
async function modules(callback) {
  const vite = await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true}})
  try { await callback(await vite.ssrLoadModule('/src/services/projectTranslationRequests.ts'),vite) } finally { await vite.close() }
}
test('translation sources bind exact saved field, source text and observed manual revision', () => modules(async m => {
  const missing=m.translationRequestEntries([field()],[])[0]
  assert.equal(missing.expected_entry_id,null);assert.equal(missing.expected_revision,0);assert.equal(missing.source_text,field().sourceText)
  const existing={...field(),id:id(7),revision:3}
  assert.equal(m.translationRequestEntries([field()],[existing])[0].expected_revision,3)
  for(const entries of [[{...existing,revision:'3'}],[existing,existing],[{...existing,scopeId:undefined}]]) assert.throws(()=>m.translationRequestEntries([field()],entries))
  assert.throws(()=>m.translationRequestEntries([field(),field()],[]));assert.throws(()=>m.translationRequestEntries([{...field(),scopeId:'unsaved-local'}],[]))
}))
test('candidate and history readers reject missing/coerced identities and partial adoption receipts', () => modules(async m => {
  assert.equal(m.readTranslationRequest(request()).jobId,id(2))
  for(const change of [{source_count:'1'},{status:'completed'},{preparation_id:null},{created_at:'unknown'},{execution_instance_version_id:undefined}]) assert.throws(()=>m.readTranslationRequest({...request(),...change}))
  for(const change of [{adopted_revision:1},{adopted_entry_id:id(8)},{expected_revision:false},{position:'0'}]) assert.throws(()=>m.readTranslationSource({...source(),...change}))
  assert.throws(()=>m.readTranslationPage({...page(request()),results:[request(),request()]},m.readTranslationRequest))
  assert.throws(()=>m.readTranslationCandidate({source:source(),text:'Candidate',source_text:'原文',current:'true',reason:''}))
  assert.equal(m.readTranslationCandidate({source:source(),text:'Candidate',source_text:'原文',current:false,reason:'原文变更'}).current,false)
}))
test('managed input uses configured mapping and never sends fabricated JSON or removes other required inputs', () => modules(async (m,vite) => {
  const form=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
  const input=(key,kind,target)=>({key,kind,required:true,min_items:1,max_items:1,position:0,
    targets:[{target_kind:'operation_input',target_key:target,is_primary:true}],semantic_contract:{allowed_subject_kinds:[],allowed_purposes:[],allowed_scope_kinds:['request'],paired_with_input_key:null}})
  const contract={versionId:id(4),sha256:'a'.repeat(64),operation:'prompt.normalize',mode:'',outputKind:'text',workflowVersionId:null,
    inputs:[input('chosen_sources','json','items'),input('notes','text','notes')],parameters:[],channels:[],serverOwnedInputs:[],contextFields:[],
    promptMode:'none',languageForm:unusedLanguageForm(),purposes:{},sourceConstraints:{chosen_sources:{kinds:['literal'],selectors:null},notes:{kinds:['literal'],selectors:null}}}
  const draft=form.emptyExecutionRequestDraft(contract)
  draft.literals.notes.enabled=false
  assert.throws(()=>m.translationFixture(contract,draft),/notes/)
  draft.literals.notes.enabled=true;draft.literals.notes.value='Explicit instructions'
  const before=JSON.stringify(draft)
  assert.deepEqual(m.translationFixture(contract,draft).inputs,{notes:{kind:'literal',value:'Explicit instructions'}})
  assert.equal(JSON.stringify(draft),before)
  assert.throws(()=>m.translationManagedInput({...contract,inputs:[...contract.inputs,input('second','json','items')]}))
  assert.throws(()=>form.buildExecutionRequestFixture(contract,draft,{managedInputKeys:['notes']}))
  const {default:Editor}=await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')
  const html=await renderToString(createSSRApp({render:()=>h(Editor,{contract,modelValue:draft,managedInputKeys:['chosen_sources']})}))
  assert.match(html,/真实项目字段/);assert.match(html,/Explicit instructions/)
}))
async function component(vite,callback) {
  const {projectTranslationApi:api}=await vite.ssrLoadModule('/src/services/projectTranslationRequests.ts')
  const saved={...api}, {default:Component}=await vite.ssrLoadModule('/src/components/ProjectTranslationRequests.vue')
  const props=reactive({projectId:id(6),fields:[field()],entries:[],blockedReason:''});let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit(){}});return()=>h('div')}})
  try{app.provide(ssrContextKey,{modules:new Set()});app.mount({});await callback(state,props,api)}finally{app.unmount();Object.assign(api,saved)}
}

test('prose review refuses a legacy name target without any write',()=>modules(async(m,vite)=>{
  const {repository:api}=await vite.ssrLoadModule('/src/services/repository.ts')
  const original={get:api.getProjectTranslations,ordinary:api.confirmProjectTranslation}
  const name={scopeType:'prop',scopeId:id(11),fieldKey:'prop.name',sourceText:'玄纹剑',label:'道具名称'}
  const row={...name,id:id(12),projectId:id(6),revision:2,status:'draft_translation',contextCurrent:true,
    translatedText:'Xuanwen Sword',confirmationTarget:'canonical_glossary',canonicalValue:null}
  const props=reactive({projectId:id(6),fields:[name],episodeId:'',shotId:'',refreshKey:0,blockedReason:'',compact:true})
  let state:any,writes=0
  api.getProjectTranslations=async()=>[row]
  api.confirmProjectTranslation=async()=>{writes++;throw Error('must not write')}
  const {default:Component}=await vite.ssrLoadModule('/src/components/TranslationReviewPanel.vue')
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit(){}});return()=>h('div')}})
  try{
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await state.load()
    await state.confirm(name);assert.equal(writes,0);assert.match(state.error.value,/已停止名称翻译/)
    assert.equal(state.entries.value[0].revision,2)
    state.entries.value=[{...row,confirmationTarget:undefined}]
    await state.confirm(name);assert.match(state.error.value,/确认目标缺失/);assert.equal(writes,0)
  }finally{app.unmount();api.getProjectTranslations=original.get;api.confirmProjectTranslation=original.ordinary}
}))
test('actual workbench restores paged original requests only on demand and hides failed/old candidate reads',()=>modules(async(m,vite)=>{
  await component(vite,async(state,props,api)=>{
    let calls=0;api.list=async()=>{calls++;return m.readTranslationPage(page(request()),m.readTranslationRequest)}
    await nextTick();assert.equal(calls,0)
    await state.showHistory();assert.equal(calls,1);assert.equal(state.history.value.results[0].jobId,id(2))
    api.list=async()=>{throw new Error('history unavailable')};await state.loadHistory();assert.equal(state.history.value,null);assert.match(state.historyError.value,/unavailable/)
    state.selected.value=m.readTranslationRequest(request())
    api.candidate=async()=>m.readTranslationCandidate({source:source(),source_text:'原文',text:'Translation',current:true,reason:''})
    await state.readCandidate(m.readTranslationSource(source()));assert.equal(state.candidate.value.text,'Translation')
    api.candidate=async()=>{throw new Error('candidate unavailable')};await state.readCandidate(m.readTranslationSource(source()))
    assert.equal(state.candidate.value,null);assert.match(state.sourceError.value,/unavailable/)
  })
}))
test('uncertain creation retries same frozen request and opens original Preparation without auto-adoption',()=>modules(async(m,vite)=>{
  await component(vite,async(state,props,api)=>{
    const sent:any[]=[];state.pending.value={projectId:props.projectId,key:'same-key',versionId:id(4),body:{entries:[{source_text:'原文'}]}}
    api.create=async(project,body,key)=>{sent.push(JSON.stringify({project,body,key}));throw new Error('response lost')}
    await state.submit();assert.equal(state.pending.value.key,'same-key')
    api.create=async(project,body,key)=>{sent.push(JSON.stringify({project,body,key}));return m.readTranslationRequest(request())}
    api.list=async()=>m.readTranslationPage(page(request()),m.readTranslationRequest)
    api.sources=async()=>m.readTranslationPage(page(source()),m.readTranslationSource)
    api.adopt=async()=>{throw new Error('must not automatically adopt')}
    await state.submit();assert.equal(sent[0],sent[1]);assert.equal(state.pending.value,null);assert.equal(state.selected.value.preparationId,id(3));assert.equal(state.creating.value,false)
  })
}))
test('review panel uses asynchronous exact-instance entry and retains explicit manual confirmation',()=>{
  const view=readFileSync(new URL('../src/components/TranslationReviewPanel.vue',import.meta.url),'utf8')
  assert.match(view,/ProjectTranslationRequests/);assert.match(view,/createManualProjectTranslation/);assert.match(view,/confirmProjectTranslation/)
  assert.doesNotMatch(view,/translateProjectFields|retranslateProjectField|getTranslationCapability|selectedDeploymentId/)
})

test('batch selection is explicit, preserves source order, resets review and cannot change an uncertain request',()=>modules(async(m,vite)=>{
  const form=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
  await component(vite,async(state,props,api)=>{
    const second={...field(),fieldKey:'project.visual_style',sourceText:'夜雨中的街道'}
    props.fields=[field(),second]
    api.options=async()=>({count:0,next:null,previous:null,results:[],sourceLang:'zh-CN',targetLang:'en',maxEntries:1})
    await state.begin(props.fields)
    assert.equal(state.selectedSources.value.length,2);assert.match(state.sourceSelectionError.value,/最多 1/)
    state.selectSource(0,false);assert.equal(state.selectedSources.value[0].source_text,second.sourceText)
    state.reviewed.value=true;state.selectSource(0,true);await nextTick()
    assert.equal(state.reviewed.value,false)
    assert.deepEqual(state.selectedSources.value.map(row=>row.source_text),[field().sourceText,second.sourceText])
    state.selectAllSources(false);assert.match(state.sourceSelectionError.value,/至少选择/)
    state.selectSource(1,true)
    const input={key:'selected_fields',kind:'json',required:true,min_items:1,max_items:1,position:0,
      targets:[{target_kind:'operation_input',target_key:'items',is_primary:true}],
      semantic_contract:{allowed_subject_kinds:[],allowed_purposes:[],allowed_scope_kinds:['request'],paired_with_input_key:null}}
    state.contract.value={versionId:id(4),sha256:'a'.repeat(64),operation:'prompt.normalize',mode:'',outputKind:'text',workflowVersionId:null,
      inputs:[input],parameters:[],channels:[],serverOwnedInputs:[],contextFields:[],promptMode:'none',languageForm:unusedLanguageForm(),purposes:{},sourceConstraints:{selected_fields:{kinds:['literal'],selectors:null}}}
    state.draft.value=form.emptyExecutionRequestDraft(state.contract.value)
    await nextTick();state.reviewed.value=true
    let sent:any;api.create=async(project,body,key)=>{sent={project,body,key};throw new Error('response unknown')}
    await state.submit()
    assert.equal(sent.body.entries.length,1);assert.equal(sent.body.entries[0].source_text,second.sourceText)
    assert.equal(state.sources.value.length,2);assert.equal(state.pending.value.key,sent.key)
    state.selectAllSources(true);state.selectSource(0,true)
    assert.equal(state.selectedSources.value.length,1);assert.equal(state.pending.value.body.entries.length,1)
  })
}))
