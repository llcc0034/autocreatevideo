import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'

const root = fileURLToPath(new URL('..', import.meta.url)), sha = 'a'.repeat(64)
const id = (n: number) => `10000000-0000-0000-0000-${String(n).padStart(12,'0')}`
const run = {id:id(1),project:id(2),planSource:'manual',status:'awaiting_input',executionPlan:{sha256:sha},stages:[]}
const content = {kind:'script_revision',id:id(3),sha256:sha,revision:1}
const options = {runId:run.id,runPlanSha256:sha,actions:[{stageKey:'bible_materialize',action:'adopt'},{stageKey:'bible_review',action:'approve'}]}
const initial = {plan_source:'manual',settings:[],run_id:run.id,run_plan_sha256:sha,current_script:content,project_bible_sha256:sha,
  project_bible:{characters:[],scenes:[],props:[]},
  source:{revision:0,decision_id:null,snapshot_sha256:null,status:'not_adopted',content:null,candidate_sha256:null},
  approval:{revision:0,decision_id:null,snapshot_sha256:null,status:'not_reviewed'},candidates:[]}
const ready = {...initial,source:{revision:2,decision_id:id(4),snapshot_sha256:sha,status:'current',content,candidate_sha256:sha},
  candidates:['character','scene','prop'].map((kind,n)=>({id:`candidate-${n}`,entity_type:kind,name:`候选 ${n}`,description:'剧情描述',
    identity_anchor:'Specific identity.',action:'create',conflict_code:null,existing:null}))}
async function modules(fn) {
  const vite = await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try { await fn(await vite.ssrLoadModule('/src/services/productionManualBible.ts'),vite) } finally { await vite.close() }
}

test('Bible source adoption and approval preserve distinct decisions without model calls',()=>modules(m=>{
  const detail=m.readManualBibleReview(initial,run)
  const adopt=m.buildManualBibleAction(detail,run,options,'adopt','source',[],'',false,[])
  assert.equal(adopt.expected_selection_revision,0);assert.equal(adopt.stage_key,'bible_materialize')
  assert.deepEqual(adopt.payload,{expected_current_script:content,comment:''})
  const reviewed=m.readManualBibleReview(ready,run)
  const approve=m.buildManualBibleAction(reviewed,run,options,'approve','review',ready.candidates.map(row=>row.id),'逐项核对',true,[])
  assert.equal(approve.expected_selection_revision,0);assert.equal(approve.stage_key,'bible_review')
  assert.equal(approve.payload.expected_source_snapshot_sha256,sha)
  assert.equal(approve.payload.expected_project_bible_sha256,sha)
  assert.equal(approve.payload.reviewed_candidate_ids.length,3)
  assert.equal(approve.payload.reviewed_project_bible,true)
  assert.equal('execution_instance_version_id' in approve.payload,false)
  assert.throws(()=>m.buildManualBibleAction(reviewed,run,options,'approve','bad',[],'',true,[]))
  assert.throws(()=>m.buildManualBibleAction(reviewed,run,options,'approve','bad',['candidate-0','candidate-0','candidate-2'],'',true,[]))
  assert.throws(()=>m.buildManualBibleAction(reviewed,run,options,'approve','bad',ready.candidates.map(row=>row.id),'',false,[]),/完整创作设定/)
}))

test('Bible review rejects missing or inconsistent facts and distinguishes existing content from candidates',()=>modules(m=>{
  for(const change of [{run_id:id(99)},{run_plan_sha256:'b'.repeat(64)},{current_script:undefined},{project_bible:undefined},
    {project_bible:{characters:[],scenes:[]}},{project_bible:{characters:[],scenes:[],props:null}},
    {source:{...ready.source,content:null}},{source:{...ready.source,content:{...content,id:id(99)}}},
    {candidates:[ready.candidates[0],ready.candidates[0]]},{approval:{...ready.approval,status:'current'}},
    {candidates:[{...ready.candidates[0],action:'reuse'}]}]) assert.throws(()=>m.readManualBibleReview({...ready,...change},run))
  const existing={id:id(5),name:'人工维护名称',description:'保留已修改描述',is_locked:true}
  const reused={...ready,candidates:[{...ready.candidates[0],action:'reuse',existing}]}
  assert.equal(m.readManualBibleReview(reused,run).candidates[0].existing.description,existing.description)
  const conflict={...ready,candidates:[{...ready.candidates[0],conflict_code:'script_bible_content_conflict',existing}]}
  assert.throws(()=>m.buildManualBibleAction(m.readManualBibleReview(conflict,run),run,options,'approve','key',['candidate-0'],'',true,[]))
  assert.throws(()=>m.buildManualBibleAction(m.readManualBibleReview(ready,run),run,{...options,actions:[]},'adopt','key',[],'',false,[]))
}))

test('Bible API uses the original Run detail endpoint and propagates errors',()=>modules(async(_m,vite)=>{
  const {api}=await vite.ssrLoadModule('/src/services/api.ts'),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const previous=api.get
  try {
    api.get=async path=>{assert.equal(path,`/production-runs/${run.id}/bible-review/`);return {data:ready}}
    assert.deepEqual(await platformApi.getBibleReview(run.id),ready)
    api.get=async()=>{throw new Error('source unavailable')}
    await assert.rejects(platformApi.getBibleReview(run.id),/source unavailable/)
  } finally {api.get=previous}
}))

test('Bible component requires each check and keeps the exact uncertain request for retry',()=>modules(async(_m,vite)=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const old={getBibleReview:platformApi.getBibleReview,performManualProductionAction:platformApi.performManualProductionAction}
  const calls:any[]=[],events:any[]=[]
  platformApi.getBibleReview=async()=>JSON.parse(JSON.stringify(ready))
  platformApi.performManualProductionAction=async(runId,body)=>{
    calls.push(JSON.parse(JSON.stringify({runId,body})))
    if(calls.length===1)throw new Error('uncertain response')
    return {action:'approve',result_kind:'approval_decision',target_id:id(6)}
  }
  const {default:Component}=await vite.ssrLoadModule('/src/components/ManualBibleReview.vue')
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const props=reactive({run,options});let state:any
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(...args)=>events.push(args)});return()=>h('div')}})
  try {
    app.provide(ssrContextKey,{modules:new Set()});app.mount({})
    await Promise.resolve();await nextTick()
    assert.equal(calls.length,0);assert.equal(state.checked.value.length,0)
    state.checked.value=['candidate-0','candidate-1'];await state.submit('approve');assert.equal(calls.length,0)
    state.checked.value=ready.candidates.map(row=>row.id);await state.submit('approve');assert.equal(calls.length,0)
    state.reviewedProjectBible.value=true;await state.submit('approve')
    assert.match(state.error.value,/uncertain/);assert.equal(state.pending.value.action,'approve')
    await state.submit('adopt');assert.equal(calls.length,1)
    await state.submit('approve');assert.equal(calls.length,2);assert.deepEqual(calls[1],calls[0])
    assert.equal(state.reviewedProjectBible.value,false)
    assert.equal(events.filter(row=>row[0]==='changed').length,1)
    const html=await renderToString(h(Component,props))
    assert.match(html,/解析候选不调用模型/);assert.match(html,/尚未自动|不会自动/)
  }finally{app.unmount();Object.assign(platformApi,old)}
}))

test('full creative review shows speaker voice and rejects omitted fields without guessing',()=>modules(async(m,vite)=>{
  const character={id:id(20),name:'旁白',is_locked:true,metadata:{creative_note:'固定音色'},
    ...Object.fromEntries(Object.keys(m.bibleCreativeFields.characters).map(key=>[key,'']))}
  Object.assign(character,{slug:'narrator',stable_speaker_id:'narrator',voice_profile:'Warm measured voice.',wardrobe:'Grey coat.'})
  const bible={characters:[character],scenes:[],props:[]}
  assert.equal(m.readManualBibleReview({...ready,project_bible:bible},run).project_bible.characters[0].voice_profile,character.voice_profile)
  for(const key of ['voice_profile','wardrobe','metadata']) {
    const bad=JSON.parse(JSON.stringify(bible));delete bad.characters[0][key]
    assert.throws(()=>m.readManualBibleReview({...ready,project_bible:bad},run))
  }
  const {default:Component}=await vite.ssrLoadModule('/src/components/BibleCreativeReview.vue')
  const html=await renderToString(h(Component,{content:bible}))
  for(const text of ['说话人标识','声音特征','Warm measured voice.','Grey coat.','固定音色','未填写','主图和参考图另行审核']) assert.ok(html.includes(text),text)
  assert.doesNotMatch(html,/<input|<textarea/)
  assert.doesNotMatch(html,/外观描述|visual_identity/)
  assert.match(html,/确切设定修订/)
  assert.throws(()=>m.readManualBibleReview({...ready,project_bible:{...bible,
    characters:[{...character,visual_identity:'retired duplicate'}]}},run))
}))

test('original identity declaration preserves absent and empty values and refuses the retired alias',()=>modules(async(m,vite)=>{
  const candidates=ready.candidates.map((row,index)=>({...row,identity_anchor:[null,'','源共同身份'][index]}))
  const detail={...ready,candidates}
  m.readManualBibleReview(detail,run)
  for(const candidate of [{...candidates[0],identity_anchor:undefined}, {...candidates[0],identity_anchor:{}},
    {...candidates[0],visual_identity:'old'}, {...candidates[0],action:'reuse',
      existing:{id:id(6),name:'已有',description:'原说明',is_locked:true,visual_identity:'old'}}]) {
    assert.throws(()=>m.readManualBibleReview({...detail,candidates:[candidate]},run))
  }
  const {default:Component}=await vite.ssrLoadModule('/src/components/BibleContentReview.vue')
  const html=await renderToString(h(Component,{content:detail,candidates:[],settings:[],project:false}))
  for(const text of ['原始共同身份声明：未声明此字段','原始共同身份声明：声明为空文本','原始共同身份声明：源共同身份']) assert.ok(html.includes(text),text)
  assert.doesNotMatch(html,/visual_identity|外观描述/)
}))

const structureSource = {schema_version:1,kind:'confirmed_script_structure',script:content,
  document_sha256:sha,configuration_sha256:sha,output:{id:id(20),version:1,sha256:sha}}
const declaration = {review_key:'declaration:one',kind:'declaration',entity_type:'character',entity_name:'旅人',entity_key:'traveler',
  appearance_role:'variant',variant_key:'raincoat',name:'雨衣',reference_name:'旅人·雨衣',identity_anchor:'',appearance_anchor:'黄色雨衣',
  source_facts:{script_revision_id:id(3),already_materialized:false,primary_reference_name:'旅人·主图',description:'旅人',
    structure_source:structureSource,semantic_path:'value.entities[0]',identity_anchor:'短发',appearance_anchor:'黄色雨衣'},setting:null,primary_setting:null}

test('saved semantic fields distinguish absent and empty and never invent old line origins',()=>modules(async(m,vite)=>{
  const facts={...declaration.source_facts,description:null,identity_anchor:''}
  const detail={...ready,settings:[{...declaration,source_facts:facts}]}
  m.readManualBibleReview(detail,run)
  for(const broken of [{...facts,description:{text:'old',origin:'source',source_lines:[1]}},
    {...facts,structure_source:{...structureSource,document_sha256:'bad'}}, {...facts,semantic_path:'invented'}]) {
    assert.throws(()=>m.readManualBibleReview({...detail,settings:[{...declaration,source_facts:broken}]},run))
  }
  const {default:Component}=await vite.ssrLoadModule('/src/components/BibleContentReview.vue')
  const html=await renderToString(h(Component,{content:detail,candidates:[],settings:[],project:false}))
  for(const value of ['未声明此字段','声明为空文本','value.entities[0]','已保存的字段映射结果',structureSource.document_sha256]) assert.ok(html.includes(value),value)
  assert.match(html,/未填写；不会自动补齐/)
  assert.doesNotMatch(html,/原文行|source_lines|原冻结规则|继承下述明确主图/)
}))
test('manual and recipe approval require the exact displayed declaration and setting set',()=>modules(m=>{
  const detail={...ready,settings:[declaration]}, ids=detail.candidates.map(row=>row.id)
  for(const selected of [[],['declaration:one','declaration:one'],['declaration:other']]) {
    assert.throws(()=>m.buildManualBibleAction(detail,run,options,'approve','key',ids,'',true,selected))
    assert.throws(()=>m.buildBibleConfirmation(detail,sha,ids,selected,true))
  }
  const receipt=m.buildBibleConfirmation(detail,sha,ids,['declaration:one'],true)
  assert.deepEqual(receipt.reviewed_setting_keys,['declaration:one'])
  const recipe={...detail,plan_source:'recipe',source_snapshot_sha256:sha}
  assert.equal(m.readRecipeBibleReview(recipe,{...run,planSource:'recipe'}).settings[0].reference_name,'旅人·雨衣')
  for(const settings of [undefined,[{...declaration,source_facts:null}],[declaration,declaration]]) {
    assert.throws(()=>m.readRecipeBibleReview({...recipe,settings},{...run,planSource:'recipe'}))
  }
  assert.deepEqual(m.buildBibleConfirmation({...initial},null,[],[],true).reviewed_setting_keys,[])
}))

test('review shows exact historical primary body separately from original declarations',()=>modules(async(m,vite)=>{
  const primary={setting_revision_id:id(22),content_sha256:sha,revision:1,reference_name:'旅人·主图',
    identity_anchor:'原主图黑发',appearance_anchor:'原主图灰衣'}
  const setting={...declaration,review_key:'setting:current',kind:'stored',appearance_anchor:'当前人工修订蓝衣',source_facts:null,
    setting:{setting_revision_id:id(23),appearance_id:id(24),content_sha256:sha,revision:2},primary_setting:primary}
  const detail={...ready,settings:[declaration,setting]}
  m.readManualBibleReview(detail,run)
  const {default:Component}=await vite.ssrLoadModule('/src/components/BibleContentReview.vue')
  const html=await renderToString(h(Component,{content:detail,candidates:[],settings:[],project:false}))
  for(const text of ['原剧本声明','当前设定修订 2','黄色雨衣','当前人工修订蓝衣','原主图黑发','原主图灰衣','不改写它的审核决定']) assert.ok(html.includes(text),text)
  assert.match(html,/按稳定 key 复用已有设定，仅为缺失的 key 创建新设定/)
  assert.match(html,/本声明不会覆盖已有名称和正文/)
  assert.match(html,/未单独填写；身份依据见下述确切主图设定/)
  assert.doesNotMatch(html,/确认后将按本声明创建设定/)
  assert.throws(()=>m.readManualBibleReview({...detail,settings:[{...setting,primary_setting:null}]},run))
}))

test('recipe reads clear confirmation and late responses cannot cross runs',()=>modules(async(_m,vite)=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),old=platformApi.getBibleReview
  const waiting:any[]=[],events:any[]=[]
  platformApi.getBibleReview=()=>new Promise(resolve=>waiting.push(resolve))
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeBibleReview.vue')
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const props=reactive({run:{...run,planSource:'recipe'},disabled:false});let state:any
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(...args)=>events.push(args)});return()=>h('div')}})
  try {
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick()
    props.run={...props.run,id:id(99)};await nextTick()
    waiting[0]({...ready,plan_source:'recipe',source_snapshot_sha256:sha});await Promise.resolve();await nextTick()
    assert.equal(state.detail.value,null);assert.ok(events.every(row=>row[1]===null))
    waiting[1]({...ready,run_id:id(99),plan_source:'recipe',source_snapshot_sha256:sha,settings:[declaration]})
    await Promise.resolve();await nextTick();state.candidates.value=ready.candidates.map(row=>row.id);state.project.value=true
    await nextTick();assert.equal(state.confirmation.value,null)
    state.settings.value=['declaration:one'];await nextTick();assert.ok(state.confirmation.value)
    const reload=state.load();assert.equal(state.confirmation.value,null)
    waiting[2]({...ready,run_id:id(99),plan_source:'recipe',source_snapshot_sha256:sha});await reload
    assert.equal(state.project.value,false);assert.equal(state.confirmation.value,null)
  } finally {app.unmount();platformApi.getBibleReview=old}
}))
