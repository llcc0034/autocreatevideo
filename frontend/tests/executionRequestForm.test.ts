import assert from 'node:assert/strict'
import { unusedLanguageForm } from './fixtures/executionLanguage.ts'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createSSRApp, createRenderer, h, reactive, nextTick, ssrContextKey, watchEffect } from 'vue'
import { createHash } from 'node:crypto'
import { renderToString } from 'vue/server-renderer'
import { readFileSync } from 'node:fs'

const root = fileURLToPath(new URL('..', import.meta.url))

test('shared request author wraps long contract keys without hiding them and uses configuration typography', () => {
  const source = readFileSync(new URL('../src/components/ExecutionRequestInputEditor.vue', import.meta.url), 'utf8')
  const style = source.split('<style scoped>')[1]!
  assert.match(style, /\.parameter-row>strong\{[^}]*overflow-wrap:anywhere/)
  assert.match(style, /\.input-media-row,\.parameter-row\{[^}]*grid-template-columns:minmax\(0,1fr\)/)
  assert.match(style, /\.execution-request-inputs label\{[^}]*font-size:var\(--config-body-size\)/)
  assert.match(style, /max-width:760px[^\n]*min-height:44px/)
  assert.doesNotMatch(style, /text-overflow:ellipsis|word-break:break-all/)
})

test('execution context picker requests owned projects without widening the management list', async () => {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  let app: any
  try {
    const {api}=await vite.ssrLoadModule('/src/services/api.ts')
    const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
    const calls: any[]=[]
    api.get=async(url,options)=>{calls.push({url,options});return{data:{count:0,next:null,previous:null,results:[]}}}
    await repository.getProjects();assert.equal(calls.at(-1).options.params.owned_only,undefined)
    const {default:Picker}=await vite.ssrLoadModule('/src/components/ExecutionContextFieldPicker.vue')
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({...Picker,setup(props,ctx){Picker.setup(props,ctx);return()=>null}},{modelValue:{selectorKey:'script.content',projectId:'',revisionId:''}})
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick();await nextTick()
    assert.equal(calls.at(-1).url,'/projects/');assert.equal(calls.at(-1).options.params.owned_only,true)
  } finally {app?.unmount();await vite.close()}
})
const input = (key, kind, parent = null) => ({ key, kind, position: 0, required: false, min_items: 0, max_items: 3,
  semantic_contract: { schema_version: 1, allowed_purposes: [], allowed_subject_kinds: ['external'], allowed_scope_kinds: ['request'], paired_with_input_key: parent, allow_repeated_asset: true },
  targets: [{ target_kind: 'workflow_slot', target_key: key, is_primary: true }] })
const raw = { subject_categories: [{value:'character',label:'人物'},{value:'scene',label:'场景'},{value:'prop',label:'道具'}], language_form: unusedLanguageForm(), input_forms: {}, context_fields: [], version_id: 'version', revision: 1, frozen: true, sha256: 'a'.repeat(64), definition: { version_id: 'version',
  configuration: { deployment_id: 'deployment', operation: 'video.reference_to_video', mode: 'mode', workflow_version_id: 'workflow', prompt_mode: 'manual', prompt_profile_version_id: null,
    inputs: [{ ...input('brief', 'text'), max_items: 1, required: true, min_items: 1 }, input('pictures', 'image'), input('clips', 'video'), input('paired', 'audio', 'clips')], parameters: [], auxiliaries: [] },
  interface: { output: { contract: { primary: { kind: 'video' } } }, input_purposes: {}, parameters: {
    frames: { value_source: 'workflow', rule: { type: 'integer', required: true, minimum: 5, maximum: 3600, alignments: [{ modulus: 17, remainder: 5 }], default: 22 } },
    seed: { value_source: null, rule: { type: 'integer', required: true, minimum: 0, maximum: 18446744073709551615, seed_policy: 'generate_once' } },
    gain: { value_source: 'instance', rule: { type: 'number', required: false, minimum: 0, maximum: 2, enum: [0, 0.5, 1], const: 0 } },
  }, prompt: { mode: 'manual', channels: { positive_prompt: { rule: { type: 'string', required: true, max_length: 100 } }, negative_prompt: { rule: { type: 'string', required: false } } } } } } }

test('request form submits original missing-key input for server resolution, never fake supplied defaults',async()=>{
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const m=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const value=structuredClone(raw) as any
    value.definition.configuration.inputs=[{...input('custom','json'),data_structure_version_id:'structure-v1',max_items:1,required:true,min_items:1}]
    const schema={type:'object',properties:{count:{type:'integer',default:4,minimum:0}},required:['count']}
    value.input_forms={custom:{label:'Custom',description:'',suggestions:[],schemas:[schema]}}
    const contract=m.executionRequestFormContract(value),draft=m.emptyExecutionRequestDraft(contract)
    draft.literals.custom.value={};draft.channels.positive_prompt='Explicit prompt'
    const submitted=m.buildExecutionRequestFixture(contract,draft)
    assert.deepEqual(submitted.inputs.custom,{kind:'literal',value:{}})
    assert.deepEqual(draft.literals.custom.value,{})
    draft.literals.custom.value={count:null}
    assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/整数/)
    delete value.definition.configuration.inputs[0].data_structure_version_id
    draft.literals.custom.value={}
    assert.throws(()=>m.buildExecutionRequestFixture(m.executionRequestFormContract(value),draft),/需要添加/)
  } finally {await vite.close()}
})

test('language selectors follow exact policy metadata and preserve English creative and style input', async () => {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const m=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const registry=await vite.ssrLoadModule('/src/services/executionRegistry.ts')
    const Component=(await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
    const value=structuredClone(raw)
    value.definition.configuration.inputs=[{...input('idea','text'),required:true,min_items:1,max_items:1}, {...input('style_hint','text'),required:true,min_items:1,max_items:1}]
    value.language_form.source={required:true,fields:['本轮创意']}
    const contract=m.executionRequestFormContract(value),draft=m.emptyExecutionRequestDraft(contract)
    draft.literals.idea.value='A student uncovers a secret.';draft.literals.style_hint.value='Quiet cinematic suspense.'
    draft.channels.positive_prompt='Explicit prompt'
    assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/请选择输入语言.*本轮创意/)
    draft.sourceLanguage='en'
    const result=m.buildExecutionRequestFixture(contract,draft)
    assert.equal(result.source_language,'en');assert.equal(result.target_language,'')
    assert.equal(result.inputs.idea.value,draft.literals.idea.value);assert.equal(result.inputs.style_hint.value,draft.literals.style_hint.value)
    const html=await renderToString(createSSRApp({render:()=>h(Component,{contract,modelValue:draft})}))
    assert.match(html,/输入语言 · 必填/);assert.match(html,/<select value="en"/)
    assert.match(html,/提示词目标语言 · 无需填写/);assert.match(html,/按原语言保留：本轮创意/)
    assert.match(html,/不是最终剧本/);assert.match(html,/当前共用语言声明不能分别表达/)
    draft.literals.style_hint.value='克制、安静的悬疑风格'
    assert.equal(m.buildExecutionRequestFixture(contract,draft).inputs.style_hint.value,'克制、安静的悬疑风格')
    contract.languageForm.target={required:true,fields:['导演描述']}
    assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/请选择提示词目标语言/)
    draft.targetLanguage='fr';assert.equal(m.buildExecutionRequestFixture(contract,draft).target_language,'fr')
    const customHtml=await renderToString(createSSRApp({render:()=>h(Component,{contract,modelValue:draft})}))
    assert.match(customHtml,/aria-label="其他语言（填写语言代码）"/)
    contract.languageForm.allow_custom=false
    assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/不在服务端可选目录/)
    assert.throws(()=>registry.adaptExecutionLanguageForm(undefined),/响应不完整/)
    assert.throws(()=>registry.adaptExecutionLanguageForm({...unusedLanguageForm(),source:{required:true,fields:[]}}),/作用字段/)
    assert.throws(()=>m.executionRequestFormContract({...value,language_form:undefined}),/响应不完整/)
  } finally {await vite.close()}
})

test('unused and fixed policy languages are read-only explanations, without guessed defaults',async()=>{
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const m=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const Component=(await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
    const value=structuredClone(raw);value.language_form.fixed_fields=[{label:'导演描述',language:'英文'}]
    const contract=m.executionRequestFormContract(value),draft=m.emptyExecutionRequestDraft(contract)
    const html=await renderToString(createSSRApp({render:()=>h(Component,{contract,modelValue:draft})}))
    assert.match(html,/策略已固定语言：导演描述（英文）/)
    assert.equal(draft.sourceLanguage,'');assert.equal(draft.targetLanguage,'')
    assert.doesNotMatch(html,/catalog-choice-field/)
    contract.languageForm.source={required:false,fields:['可选说明']}
    const optional=await renderToString(createSSRApp({render:()=>h(Component,{contract,modelValue:draft})}))
    assert.match(optional,/输入语言 · 使用下列字段时填写/)
  }finally{await vite.close()}
})

test('input form metadata renders numeric business fields without changing the fixture or frozen contract', async () => {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const m=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const Component=(await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
    const contractRaw:any=structuredClone(raw)
    contractRaw.definition.configuration.inputs=[{...input('count','json'),required:true,min_items:1,max_items:1}]
    contractRaw.input_forms={count:{label:'计划集数',description:'填写本次计划',schemas:[{type:'integer',minimum:1}],suggestions:[]}}
    const before=JSON.stringify(contractRaw), contract=m.executionRequestFormContract(contractRaw),draft=m.emptyExecutionRequestDraft(contract)
    assert.equal(draft.literals.count.value,null)
    assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/计划集数需要整数/)
    draft.literals.count.value=2;draft.channels.positive_prompt='Explicit prompt'
    assert.deepEqual(m.buildExecutionRequestFixture(contract,draft).inputs.count,{kind:'literal',value:2})
    const html=await renderToString(createSSRApp({render:()=>h(Component,{contract,modelValue:draft})}))
    assert.match(html,/计划集数/);assert.match(html,/填写本次计划/);assert.match(html,/查看输入要求/)
    assert.match(html,/type="number"/);assert.doesNotMatch(html,/计划集数类型|添加字段/)
    assert.equal(JSON.stringify(contractRaw),before)
    contractRaw.definition.configuration.inputs[0].semantic_contract.allowed_scope_kinds.push('project')
    contractRaw.context_fields=[{selector_key:'example.object',label:'对象来源',kind:'json',scope_kind:'project',revision_kind:'script_revision',description:'',value_schema:{type:'object'}}]
    let htmlWithSources=await renderToString(createSSRApp({render:()=>h(Component,{contract:m.executionRequestFormContract(contractRaw),modelValue:draft})}))
    assert.doesNotMatch(htmlWithSources,/对象来源|内容从哪里来/)
    contractRaw.context_fields[0].value_schema={type:'integer'}
    contractRaw.context_fields[0].label='数量来源'
    htmlWithSources=await renderToString(createSSRApp({render:()=>h(Component,{contract:m.executionRequestFormContract(contractRaw),modelValue:draft})}))
    assert.match(htmlWithSources,/数量来源/)
  } finally {await vite.close()}
})

test('new parameter and manual channel keys reactively open and close their editors', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  let app, stop
  try {
    const m = await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const Component = (await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
    const contract = m.executionRequestFormContract(raw), draft = reactive(m.emptyExecutionRequestDraft(contract))
    let state, visible: boolean[] = []
    app = renderer.createApp({ ...Component, setup(props, context) { state = Component.setup(props, context); return () => null } }, { contract, modelValue: draft, managedInputKeys: [] })
    app.provide(ssrContextKey, {}); app.mount({})
    stop = watchEffect(() => { visible = [state.has(draft.parameters, 'frames'), state.has(draft.channels, 'positive_prompt')] })
    assert.deepEqual(visible, [false, false])
    state.toggleParameter('frames', true); draft.channels.positive_prompt = ''
    await nextTick(); assert.deepEqual(visible, [true, true])
    draft.parameters.frames = 22; draft.channels.positive_prompt = 'An explicit test prompt.'
    await nextTick(); assert.deepEqual(visible, [true, true])
    state.toggleParameter('frames', false); delete draft.channels.positive_prompt
    await nextTick(); assert.deepEqual(visible, [false, false])
    assert.equal(state.has(Object.create({ inherited: true }), 'inherited'), false)
  } finally { stop?.(); app?.unmount(); await vite.close() }
})

test('suggestions need explicit adoption, preserve edits and freeze the receipt in literal inputs', async () => {
  const vite = await createServer({root, logLevel:'silent', appType:'custom', server:{middlewareMode:true, hmr:false}})
  let app: any
  try {
    const m = await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const helper = await vite.ssrLoadModule('/src/services/executionInputSuggestions.ts')
    const { executionRegistryApi } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
    const Component = (await vite.ssrLoadModule('/src/components/ExecutionInputSuggestions.vue')).default
    const contract = m.executionRequestFormContract(raw), draft = reactive(m.emptyExecutionRequestDraft(contract))
    const choice = {key:'project.logline', label:'使用项目梗概', requires_project:true}
    const response = {input_key:'brief', definition_sha256:contract.sha256, label:choice.label,
      content:'Saved project story.', reference:'signed-preview', origin:{selector:choice.key}}
    const row = draft.literals.brief
    row.value='My existing edits'; draft.channels.positive_prompt='Use exactly my input.'
    let resolve: (value: unknown)=>void = () => {}, calls = 0, state: any
    executionRegistryApi.inputSuggestion=async()=>{calls++;return await new Promise(done=>{resolve=done})}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({...Component,setup(props,context){state=Component.setup(props,context);return()=>null}},
      {versionId:contract.versionId,definitionSha256:contract.sha256,inputKey:'brief',projectId:'project',choices:[choice],modelValue:row})
    app.provide(ssrContextKey,{});app.mount({})
    assert.equal(calls,0)
    const pending=state.show(choice)
    assert.equal(row.value,'My existing edits')
    resolve(response);await pending
    assert.equal(state.preview.value.content,response.content)
    assert.equal(row.value,'My existing edits')
    state.apply()
    assert.equal(row.value,response.content)
    row.value+=' Extra direction.'
    assert.deepEqual(m.buildExecutionRequestFixture(contract,draft).inputs.brief,
      {kind:'literal',value:'Saved project story. Extra direction.',reference:'signed-preview'})
    assert.equal(row.reference.content,response.content)
    const before=JSON.stringify(row)
    assert.throws(()=>helper.applyExecutionInputSuggestion(row,{...response,input_key:'other'},contract,'brief'),/不一致/)
    assert.equal(JSON.stringify(row),before)
    const html=await renderToString(createSSRApp({render:()=>h(Component,{versionId:'v',definitionSha256:contract.sha256,
      inputKey:'brief',choices:[choice],modelValue:row,disabled:true})}))
    assert.match(html,/使用项目梗概/);assert.match(html,/选择项目后/);assert.match(html,/已在引用基础上修改/)
    const late=state.show(choice);state.clearPreview();resolve(response);await late
    assert.equal(state.preview.value,null)
    assert.equal(row.value,'Saved project story. Extra direction.')
    executionRegistryApi.inputSuggestion=async()=>{throw new Error('Source unavailable')}
    await state.show(choice)
    assert.match(state.error.value,/Source unavailable/)
    assert.equal(state.preview.value,null)
    assert.equal(row.value,'Saved project story. Extra direction.')
  } finally {app?.unmount();await vite.close()}
})

test('Bible description prefills only an empty input and keeps source receipt and manual changes', async () => {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  let app:any
  try {
    const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
    const Component=(await vite.ssrLoadModule('/src/components/ExecutionInputSuggestions.vue')).default
    const choice={key:'production.entity.description',label:'使用本轮剧本 Bible 描述',requires_project:true,requires_production_scope:true,prefill:true}
    const row=reactive({enabled:true,value:'',reference:undefined as any})
    const props=reactive({versionId:'version',definitionSha256:'hash',inputKey:'renamed_subject',projectId:'project',choices:[choice],
      production:{runId:'run',scope:{kind:'scene',id:'scene-1'}},disabled:true})
    let state:any, resolve:(value:unknown)=>void=()=>{}, calls:any[]=[]
    executionRegistryApi.inputSuggestion=async(...args:any[])=>{calls.push(args);return await new Promise(done=>{resolve=done})}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    const Wrapped={...Component,setup(p:any,c:any){state=Component.setup(p,c);return()=>null}}
    app=renderer.createApp({render:()=>h(Wrapped,{...props,modelValue:row})});app.provide(ssrContextKey,{});app.mount({})
    assert.equal(calls.length,0)
    props.disabled=false;await nextTick()
    assert.equal(calls.length,1);assert.deepEqual(calls[0][4],props.production)
    // The real input editor creates new context objects during its busy rerender.
    // Equal identities must not cancel the request or start an automatic retry.
    props.disabled=true
    props.production={runId:'run',scope:{kind:'scene',id:'scene-1'}}
    props.choices=[{...choice}]
    await nextTick()
    assert.equal(state.loading.value,true);assert.equal(calls.length,1)
    const response={input_key:'renamed_subject',definition_sha256:'hash',label:'剧本 Bible · 图书馆 · 审核 v1',
      content:'闭馆前的安静阅览室',reference:'signed-bible-receipt',origin:{selector:choice.key}}
    resolve(response);await nextTick();await nextTick()
    assert.equal(row.value,response.content);assert.equal(row.reference.token,response.reference)
    props.production={runId:'run',scope:{kind:'scene',id:'scene-1'}}
    await nextTick()
    assert.equal(row.reference.token,response.reference)
    row.value+='；本次增加一盏台灯'
    props.disabled=true;await nextTick();props.disabled=false;await nextTick()
    assert.equal(calls.length,1)
    const html=await renderToString(createSSRApp({render:()=>h(Component,{...props,disabled:true,modelValue:row})}))
    assert.match(html,/已在引用基础上修改/);assert.match(html,/查看引用原文/);assert.match(html,/闭馆前的安静阅览室/)
    props.production={runId:'run',scope:{kind:'scene',id:'scene-2'}};await nextTick();await nextTick()
    assert.equal(row.reference,undefined);assert.equal(row.value,'闭馆前的安静阅览室；本次增加一盏台灯');assert.equal(calls.length,1)
    assert.match(state.error.value,/保留填写内容/)
    row.value=''
    // Explicit re-read still lets the user review rather than silently replace.
    const read=state.show(choice)
    row.value='在读取过程中人工填写'
    resolve(response);await read
    assert.equal(row.value,'在读取过程中人工填写');assert.equal(state.preview.value.content,response.content)
  } finally {app?.unmount();await vite.close()}
})

test('primary setting suggestions stay separate, require explicit apply and preserve their revision receipt', async () => {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  let app:any
  try {
    const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
    const Component=(await vite.ssrLoadModule('/src/components/ExecutionInputSuggestions.vue')).default
    const choices=[
      {key:'production.entity.primary.identity_anchor',label:'使用本轮主图共同身份',requires_project:true,requires_production_scope:true,prefill:false},
      {key:'production.entity.primary.appearance_anchor',label:'使用本轮主图基础外观',requires_project:true,requires_production_scope:true,prefill:false},
    ]
    const row=reactive({enabled:true,value:'已有本次填写',reference:undefined as any})
    const props={versionId:'v',definitionSha256:'sha',inputKey:'custom_subject',projectId:'p',choices,
      production:{runId:'run',scope:{kind:'scene',id:'scene'}}}
    let state:any, calls:any[]=[]
    executionRegistryApi.inputSuggestion=async(...args:any[])=>{
      calls.push(args)
      const identity=args[2]===choices[0]!.key
      return {input_key:'custom_subject',definition_sha256:'sha',label:choices[identity?0:1]!.label+' · 主图设定 v2',
        content:identity?'确切共同身份':'确切基础外观',reference:identity?'identity-receipt':'appearance-receipt',
        origin:{selector:args[2],setting:{setting_revision_id:'revision-id',revision:2,content_sha256:'setting-sha'}}}
    }
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    const Wrapped={...Component,setup(p:any,c:any){state=Component.setup(p,c);return()=>null}}
    app=renderer.createApp({render:()=>h(Wrapped,{...props,modelValue:row})});app.provide(ssrContextKey,{});app.mount({})
    assert.equal(calls.length,0)
    await state.show(choices[0]);assert.equal(row.value,'已有本次填写')
    assert.equal(state.preview.value.content,'确切共同身份');assert.equal(state.preview.value.origin.setting.revision,2);state.apply()
    assert.equal(row.value,'确切共同身份');assert.equal(row.reference.token,'identity-receipt')
    assert.match(row.reference.label,/主图设定 v2/)
    await state.show(choices[1]);assert.equal(row.value,'确切共同身份')
    assert.equal(state.preview.value.content,'确切基础外观');state.apply()
    assert.equal(row.value,'确切基础外观');assert.equal(row.reference.token,'appearance-receipt')
    const html=await renderToString(createSSRApp({render:()=>h(Component,{...props,disabled:true,modelValue:row})}))
    assert.match(html,/使用本轮主图共同身份/);assert.match(html,/使用本轮主图基础外观/)
    assert.match(html,/主图设定 v2/);assert.doesNotMatch(html,/使用本轮 Bible 视觉设定/)
    assert.equal(calls.length,2);assert.deepEqual(calls[1][4],props.production)
  } finally {app?.unmount();await vite.close()}
})

test('Bible automatic prefill failures and late responses never overwrite user input or retry themselves', async () => {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  let app:any
  try {
    const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
    const Component=(await vite.ssrLoadModule('/src/components/ExecutionInputSuggestions.vue')).default
    const row=reactive({enabled:true,value:''}), props=reactive({versionId:'v',definitionSha256:'sha',inputKey:'subject',projectId:'p',
      choices:[{key:'production.entity.description',label:'Bible 描述',requires_project:true,requires_production_scope:true,prefill:true}],
      production:{runId:'run',scope:{kind:'scene',id:'one'}},disabled:false})
    let state:any, calls=0, resolve:(value:unknown)=>void=()=>{}
    executionRegistryApi.inputSuggestion=async()=>{calls++;return await new Promise(done=>{resolve=done})}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    const Wrapped={...Component,setup(p:any,c:any){state=Component.setup(p,c);return()=>null}}
    app=renderer.createApp({render:()=>h(Wrapped,{...props,modelValue:row})})
    app.provide(ssrContextKey,{});app.mount({});assert.equal(calls,1)
    row.value='人工先填'
    resolve({input_key:'subject',definition_sha256:'sha',label:'Bible 描述',content:'原场景描述',reference:'receipt',origin:{}})
    await nextTick();await nextTick();assert.equal(row.value,'人工先填');assert.equal(state.preview.value.content,'原场景描述')
    row.value='';executionRegistryApi.inputSuggestion=async()=>{calls++;throw new Error('Bible 审核已变化')}
    props.production={runId:'run',scope:{kind:'scene',id:'two'}};await nextTick();await nextTick();await nextTick()
    assert.equal(calls,2);assert.match(state.error.value,/Bible 审核已变化/);assert.equal(row.value,'')
    props.disabled=true;await nextTick();props.disabled=false;await nextTick();assert.equal(calls,2)
  } finally {app?.unmount();await vite.close()}
})

test('project source contracts require a confirmed receipt and show a read-only snapshot, not a duplicate editor', async () => {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const m=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const Component=(await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
    const value=structuredClone(raw)
    value.definition.configuration.inputs=[{...input('project.logline','text'),required:true,min_items:1,max_items:1,
      semantic_contract:{...input('p','text').semantic_contract,allowed_scope_kinds:['project']},
      targets:[{target_kind:'profile_input',target_key:'project.logline',is_primary:false}]}]
    value.definition.interface.profile_inputs={'project.logline':{kind:'text',value_domain:'string',
      source_kinds:['project_snapshot'],source_reader:{protocol:'project_snapshot/v1',selector:'project.logline'}}}
    value.input_forms={'project.logline':{label:'项目梗概（已保存）',description:'预览确认后冻结',schemas:[],
      suggestions:[{key:'project.logline',label:'预览并引用项目梗概',requires_project:true}]}}
    const contract=m.executionRequestFormContract(value),draft=m.emptyExecutionRequestDraft(contract)
    draft.channels.positive_prompt='Explicit channel'
    assert.deepEqual(contract.sourceConstraints['project.logline'].kinds,['project_snapshot'])
    assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/预览并确认/)
    draft.literals['project.logline']={enabled:true,value:'Previewed story',reference:{token:'signed',content:'Previewed story',label:'项目梗概'}}
    assert.deepEqual(m.buildExecutionRequestFixture(contract,draft).inputs['project.logline'],{kind:'project_snapshot',reference:'signed'})
    const html=await renderToString(createSSRApp({render:()=>h(Component,{contract,modelValue:draft,projectId:'actual-project'})}))
    assert.match(html,/已确认项目输入/);assert.match(html,/Previewed story/);assert.match(html,/预览并引用项目梗概/)
    assert.doesNotMatch(html,/请填写项目梗概/);assert.doesNotMatch(html,/请选择真实上下文来源/)
    draft.literals['project.logline'].value='Injected edit'
    assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/不能手工替换/)
    value.definition.configuration.inputs[0].targets.push({target_kind:'operation_input',target_key:'script',is_primary:true})
    value.definition.interface.input_targets={script:{source_kinds:['context_field']}}
    assert.throws(()=>m.executionRequestFormContract(value),/来源限制没有交集/)
  } finally {await vite.close()}
})

test('versioned request text uses literal input without requiring a project or exposing output selectors', async () => {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const m=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const Component=(await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
    const value:any=structuredClone(raw)
    value.definition.configuration.inputs=[{...input('request.theme','text'),required:true,min_items:1,max_items:1,
      targets:[{target_kind:'profile_input',target_key:'request.theme',is_primary:false}]}]
    value.definition.interface.profile_inputs={'request.theme':{kind:'text',value_domain:'string',source_kinds:['literal'],
      source_reader:{protocol:'request_text/v1',selector:'request.theme',scope_kind:'request',value_kind:'text',snapshot_policy:'confirmed_request'}}}
    value.input_forms={'request.theme':{label:'本次故事构想',description:'本次填写并确认',schemas:[],suggestions:[]}}
    const contract=m.executionRequestFormContract(value),draft=m.emptyExecutionRequestDraft(contract)
    assert.deepEqual(contract.sourceConstraints['request.theme'].kinds,['literal'])
    draft.channels.positive_prompt='Explicit prompt'
    draft.literals['request.theme'].value='A short story. 一句话构想'
    const fixture=m.buildExecutionRequestFixture(contract,draft)
    assert.deepEqual(fixture.inputs['request.theme'],{kind:'literal',value:'A short story. 一句话构想'})
    const html=await renderToString(createSSRApp({render:()=>h(Component,{contract,modelValue:draft})}))
    assert.match(html,/本次故事构想/);assert.match(html,/A short story/)
    assert.doesNotMatch(html,/请选择真实上下文来源|预览并引用项目梗概/)
  } finally {await vite.close()}
})

test('project snapshot selection loads owned projects only on demand and invalidates receipts when scope changes', async () => {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  let app:any
  try {
    const Component=(await vite.ssrLoadModule('/src/components/ExecutionInputSuggestions.vue')).default
    const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
    const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
    const row=reactive({enabled:true,value:''}),choice={key:'project.logline',label:'项目梗概',requires_project:true}
    let state:any,calls=0,selected:any
    repository.getProjects=async(options)=>{calls++;assert.equal(options.ownedOnly,true);return[{id:'p',title:'Project'}]}
    executionRegistryApi.inputSuggestion=async(_v,_i,_s,project)=>{selected=project;return{input_key:'project.logline',
      definition_sha256:'a'.repeat(64),label:'项目梗概',content:'Stored story',reference:'signed',origin:{project_id:project}}}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({...Component,setup(props,ctx){state=Component.setup(props,ctx);return()=>null}},
      {modelValue:row,snapshot:true,versionId:'v',definitionSha256:'a'.repeat(64),inputKey:'project.logline',choices:[choice]})
    app.provide(ssrContextKey,{});app.mount({})
    assert.equal(calls,0)
    await state.loadProjects();assert.equal(calls,1)
    state.selectedProject.value='p';await nextTick();await state.show(choice)
    assert.equal(selected,'p');assert.equal(row.value,'')
    state.apply();assert.equal(row.value,'Stored story');assert.ok(row.reference)
    state.selectedProject.value='other';await nextTick()
    assert.equal(row.reference,undefined);assert.equal(row.value,'Stored story')
    assert.match(state.error.value,/已保留填写内容/)
  } finally {app?.unmount();await vite.close()}
})

test('frozen request editor renders typed slots, paired media and server limits without modifying defaults', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const component = (await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
    const contract = m.executionRequestFormContract(raw), draft = m.emptyExecutionRequestDraft(contract)
    assert.deepEqual(contract.parameters[0].evidence, raw.definition.interface.parameters.frames)
    draft.media.pictures = [{ itemKey:'picture', assetId:'saved', purpose:null, pairedTo:null, subjectId:null }]
    draft.media.paired = [{ itemKey:'paired', assetId:'audio', purpose:null, pairedTo:null, subjectId:null }]
    const before = JSON.stringify(draft)
    const html = await renderToString(createSSRApp({ render: () => h(component, { contract, modelValue: draft, disabled:true }) }))
    for (const text of ['图片', '音频', '选择素材 / 上传本地文件', 'positive_prompt', 'frames', '17', '首次准备', 'disabled']) assert.ok(html.includes(text),text)
    assert.equal(JSON.stringify(draft), before)
    assert.doesNotMatch(html, /工作流版本<select|提示词策略<select/)
  } finally { await vite.close() }
})

test('paired original audio keeps the exact video use and does not invent an audio Asset', async () => {
  const vite = await createServer({ root, logLevel:'silent', appType:'custom', server:{middlewareMode:true} })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const Component = (await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
    const contract=m.executionRequestFormContract(raw),draft=m.emptyExecutionRequestDraft(contract)
    draft.literals.brief.value='Explicit fixture';draft.channels.positive_prompt='Use the declared reference.'
    draft.media.clips=[{itemKey:'video',assetId:'video-asset',purpose:null,pairedTo:null,subjectId:null}]
    draft.media.paired=[{itemKey:'sound',assetId:'',purpose:null,pairedTo:'video',subjectId:null,videoAudio:true}]
    const body=m.buildExecutionRequestFixture(contract,draft),audio=body.inputs.paired.items[0]
    assert.equal(audio.kind,'video_audio');assert.equal(audio.paired_to,'video');assert.equal('asset_id' in audio,false)
    const html=await renderToString(createSSRApp({render:()=>h(Component,{contract,modelValue:draft,disabled:true})}))
    assert.ok(html.includes('使用配对视频的原音'));assert.ok(html.includes('不会在预览时提取'))
    for(const change of [{pairedTo:null},{pairedTo:'another-request'},{assetId:'audio-asset'},{subjectId:'invented'}]) {
      const invalid=structuredClone(draft);Object.assign(invalid.media.paired[0],change)
      assert.throws(()=>m.buildExecutionRequestFixture(contract,invalid))
    }
  } finally { await vite.close() }
})

test('resource smoke reuses input rendering with null identity and cannot masquerade as a published instance', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const registry = await vite.ssrLoadModule('/src/services/executionRegistry.ts')
    const component = (await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
    const { frozen, revision, ...definition } = structuredClone(raw)
    const value = { ...definition, schema_version: 1, size_bytes: 100, target_fingerprint: 'b'.repeat(64), scope: 'resource_smoke', version_id: null }
    value.definition.version_id = null
    const response = registry.adaptResourceSmokeContract(value)
    const contract = m.resourceSmokeRequestFormContract(response), draft = m.emptyExecutionRequestDraft(contract)
    assert.equal(contract.versionId, null)
    const manualPrompt = '  第一行：素材与主体。\n第二行：保留原始间距。  '
    draft.literals.brief.value = 'Explicit fixture'; draft.channels.positive_prompt = manualPrompt
    const fixture = m.buildExecutionRequestFixture(contract, draft)
    assert.equal(fixture.manual_channels.positive_prompt, manualPrompt)
    assert.equal(fixture.inputs.brief.value, 'Explicit fixture')
    draft.media.pictures = [{ itemKey: 'resource-picture', assetId: 'saved', purpose: null, pairedTo: null, subjectId: null }]
    const html = await renderToString(createSSRApp({ render: () => h(component, { contract, modelValue: draft, disabled: true }) }))
    for (const text of ['选择素材 / 上传本地文件', 'positive_prompt', 'frames', '17']) assert.ok(html.includes(text), text)
    assert.match(html, /<textarea[^>]*>\s*第一行：素材与主体。\n第二行：保留原始间距。\s*<\/textarea>/)
    assert.throws(() => m.executionRequestFormContract(response), /已发布/)
    assert.throws(() => m.resourceSmokeRequestFormContract(raw), /基础资源/)
    assert.throws(() => registry.adaptResourceSmokeContract({ ...value, frozen: true }), /冒充/)
    assert.throws(() => m.resourceSmokeRequestFormContract({ ...response, definition: raw.definition }), /精确版本/)
    assert.equal(m.executionRequestFormContract(raw).versionId, 'version')
  } finally { await vite.close() }
})

test('reviewed output uses keep exact decisions, UTF-8 use identity and pairing without mutating reactive drafts', async () => {
  const vite = await createServer({ root, logLevel:'silent', appType:'custom', server:{middlewareMode:true} })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const contract = m.executionRequestFormContract(raw), draft = reactive(m.emptyExecutionRequestDraft(contract))
    const id = n => `00000000-0000-4000-8000-${String(n).padStart(12,'0')}`
    const source = {runId:id(1),artifactId:id(2),decisionId:id(3),kind:'video',unitKey:`shot:${id(4)}`,revision:2}
    draft.literals.brief.value='Use the selected reference'; draft.channels.positive_prompt='Manual text'
    draft.media.clips=[{itemKey:'original-parent',assetId:'',purpose:null,pairedTo:null,subjectId:null,selectedOutput:source}]
    draft.media.paired=[{itemKey:'audio',assetId:id(5),purpose:null,pairedTo:'original-parent',subjectId:null}]
    const before=JSON.stringify(draft), prepared=await m.prepareExecutionSelectedSources(draft)
    const expected='selected:'+createHash('sha256').update(JSON.stringify({artifact_id:id(2),decision_id:id(3),input_key:'clips',purpose:null})).digest('hex')
    assert.equal(prepared.media.clips[0].itemKey,expected);assert.equal(prepared.media.paired[0].pairedTo,expected)
    assert.equal(JSON.stringify(draft),before)
    const duplicate={...draft,media:{clips:[draft.media.clips[0],{...draft.media.clips[0],selectedOutput:{...source,artifactId:id(9)}}]}}
    await assert.rejects(m.prepareExecutionSelectedSources(duplicate),/素材使用标识重复/)
    const request=m.buildExecutionRequestFixture(contract,prepared,{productionRunId:id(1)})
    assert.deepEqual(request.inputs.clips.items[0],{kind:'selected_output',decision_id:id(3),artifact_id:id(2),item_key:expected,purpose:null,scope_ref:{kind:'request'}})
    assert.equal(request.inputs.paired.items[0].paired_to,expected)
    assert.throws(()=>m.buildExecutionRequestFixture(contract,prepared),/本轮明确已审核/)
    assert.throws(()=>m.buildExecutionRequestFixture(contract,prepared,{productionRunId:id(99)}),/本轮明确已审核/)
    prepared.media.clips[0].assetId=id(7)
    assert.throws(()=>m.buildExecutionRequestFixture(contract,prepared,{productionRunId:id(1)}),/混用素材来源/)
    draft.media.clips[0].purpose='人物"参考'
    const changed=await m.prepareExecutionSelectedSources(draft)
    assert.notEqual(changed.media.clips[0].itemKey,expected)
    assert.equal(changed.media.clips[0].itemKey,'selected:'+createHash('sha256').update(JSON.stringify({artifact_id:id(2),decision_id:id(3),input_key:'clips',purpose:'人物"参考'})).digest('hex'))
    const component=(await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
    const html=await renderToString(createSSRApp({render:()=>h(component,{contract,modelValue:draft,selectedSources:[source],disabled:true})}))
    for(const text of ['本轮已审核','采用 v2','素材来源',source.artifactId]) assert.ok(html.includes(text),text)
    const stale=await renderToString(createSSRApp({render:()=>h(component,{contract,modelValue:draft,selectedSources:[],disabled:true})}))
    assert.match(stale,/原选择已不在当前审核清单/)
  } finally { await vite.close() }
})

test('request output kind comes from the exact interface and cannot be inferred from operation', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    assert.equal(m.executionRequestFormContract(raw).outputKind, 'video')
    const direct = structuredClone(raw)
    direct.definition.configuration.workflow_version_id = null
    direct.definition.interface.output = { kind:'audio' }
    assert.equal(m.executionRequestFormContract(direct).outputKind, 'audio')
    assert.equal(m.executionRequestFormContract(direct).workflowVersionId, null)
    delete direct.definition.interface.output
    assert.throws(()=>m.executionRequestFormContract(direct), /output/)
  } finally { await vite.close() }
})

test('request form preserves source defaults, locked zero, generated seed and exact channels', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const contract = m.executionRequestFormContract(raw), draft = m.emptyExecutionRequestDraft(contract)
    assert.deepEqual(draft.parameters, {})
    draft.literals.brief.value = '  A parcel.\n'
    assert.throws(() => m.buildExecutionRequestFixture(contract, draft), /positive_prompt/)
    draft.channels.positive_prompt = 'Exact manual text'
    const fixture = m.buildExecutionRequestFixture(contract, draft)
    assert.deepEqual(fixture.parameters, {})
    assert.deepEqual(fixture.inputs, { brief: { kind: 'literal', value: '  A parcel.\n' } })
    assert.deepEqual(fixture.manual_channels, { positive_prompt: 'Exact manual text' })
    assert.match(m.executionRuleDescription(contract.parameters[0].rule), /17/)
    assert.match(m.executionRuleDescription(contract.parameters[1].rule), /超出页面精确整数范围/)
    assert.match(m.executionRuleDescription(contract.parameters[2].rule), /固定值 0/)
    draft.parameters.seed = 1
    assert.throws(() => m.buildExecutionRequestFixture(contract, draft), /首次准备生成/)
    delete draft.parameters.seed; draft.parameters.frames = 5; draft.parameters.gain = 0
    assert.deepEqual(m.buildExecutionRequestFixture(contract, draft).parameters, { frames: 5, gain: 0 })
    draft.parameters.gain = 0.5
    assert.throws(() => m.buildExecutionRequestFixture(contract, draft), /固定值/)
    delete draft.parameters.gain; draft.parameters.frames = true
    assert.throws(() => m.buildExecutionRequestFixture(contract, draft), /integer/)
    for (const source of [{ ...raw, frozen: false }, { ...raw, version_id: 'other' }]) assert.throws(() => m.executionRequestFormContract(source), /精确版本/)
  } finally { await vite.close() }
})

test('media items keep explicit identity and order and do not guess pairing, purpose or subjects', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const contract = m.executionRequestFormContract(raw), draft = m.emptyExecutionRequestDraft(contract)
    draft.channels.positive_prompt = 'Motion only'
    draft.subjects = [{ id: 'subject', category: '', name: '蓝盒', description: '蓝色纸盒' }]
    assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/主体的语义类别/)
    draft.subjects[0].category='prop'
    const item = (itemKey, assetId, pairedTo = null, subjectId = null) => ({ itemKey, assetId, pairedTo, subjectId, purpose: null })
    draft.media.pictures = [item('first', 'image', null, 'subject'), item('second', 'image')]
    draft.media.clips = [item('clip-a', 'video-a'), item('clip-b', 'video-b')]
    draft.media.paired = [item('audio', 'audio-asset', 'clip-b')]
    const fixture = m.buildExecutionRequestFixture(contract, draft)
    assert.deepEqual(fixture.inputs.pictures.items.map(row => row.item_key), ['first', 'second'])
    assert.equal(fixture.inputs.paired.items[0].paired_to, 'clip-b')
    assert.deepEqual(fixture.inputs.pictures.items[0].subject_ref, { kind: 'external', id: 'subject' })
    assert.equal(fixture.subject_facts[0].category,'prop')
    const Component=(await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
    const html=await renderToString(createSSRApp({render:()=>h(Component,{contract,modelValue:draft})}))
    assert.match(html,/主体语义类别/);assert.match(html,/请选择类别/);assert.match(html,/不创建或采用项目 Bible/)
    const registry=await vite.ssrLoadModule('/src/services/executionRegistry.ts')
    for(const missing of [undefined, [], [{value:'prop',label:'道具'},{value:'prop',label:'重复'}]]) assert.throws(()=>registry.adaptExecutionSubjectCategories(missing))
    draft.subjects[0].category='external'
    assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/主体的语义类别/)
    draft.subjects[0].category='prop'
    assert.equal(fixture.inputs.pictures.items[0].purpose, null)
    draft.media.paired[0].pairedTo = 'outside'
    assert.throws(() => m.buildExecutionRequestFixture(contract, draft), /配对/)
    draft.media.paired[0].pairedTo = 'clip-a'; draft.media.pictures[0].purpose = 'guessed'
    assert.throws(() => m.buildExecutionRequestFixture(contract, draft), /用途/)
    draft.media.pictures[0].purpose = null; draft.media.pictures[0].subjectId = 'unknown'
    assert.throws(() => m.buildExecutionRequestFixture(contract, draft), /主体/)
  } finally { await vite.close() }
})

test('resource parameters keep explicit scalar types, untouched defaults and server alignment without rounding',async t=>{
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true}})
  t.after(()=>vite.close())
  const m=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
  const {frozen,revision,...value}=structuredClone(raw)
  value.version_id=null;value.scope='resource_smoke';value.definition.version_id=null
  const parameters=value.definition.interface.parameters
  parameters.enabled={value_source:null,rule:{type:'boolean',required:true}}
  parameters.label={value_source:null,rule:{type:'string',required:false,max_length:6}}
  parameters.scheduler={value_source:null,rule:{type:'string',required:true,enum:['normal','karras']}}
  const contract=m.resourceSmokeRequestFormContract(value),draft=m.emptyExecutionRequestDraft(contract)
  draft.literals.brief.value='Input';draft.channels.positive_prompt='Prompt'
  draft.parameters={frames:57,enabled:false,label:'猫',scheduler:'karras'}
  assert.deepEqual(m.buildExecutionRequestFixture(contract,draft).parameters,draft.parameters)
  assert.match(m.executionRuleDescription(contract.parameters[0].rule),/17/)
  for(const change of [{enabled:'false'},{scheduler:'unknown'},{label:'1234567'},{frames:7.5},{frames:4000},{unknown:1}])
    assert.throws(()=>m.buildExecutionRequestFixture(contract,{...draft,parameters:{...draft.parameters,...change}}))
  assert.equal(draft.parameters.frames,57) // Server checks exact rational alignment; client never rounds the input.
})

test('context input sends only a selected confirmed revision identity and rejects incomplete sources', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const source = structuredClone(raw)
    source.context_fields = [{ selector_key:'script.content', label:'已确认剧本全文', kind:'text', scope_kind:'project', revision_kind:'script_revision', description:'精确修订' }]
    source.definition.configuration.inputs[0].semantic_contract.allowed_scope_kinds.push('project')
    const contract = m.executionRequestFormContract(source), draft = m.emptyExecutionRequestDraft(contract)
    draft.channels.positive_prompt = 'Preserve source facts'
    draft.literals.brief.value = 'stale copied body must not be submitted'
    draft.contexts.brief = { selectorKey:'script.content', projectId:'project-id', revisionId:'revision-id' }
    assert.deepEqual(m.buildExecutionRequestFixture(contract, draft).inputs.brief, {
      kind:'context_field', selector_key:'script.content', scope:{kind:'project',id:'project-id'}, content_revision_id:'revision-id',
    })
    for (const patch of [{selectorKey:'script.latest'}, {projectId:''}, {revisionId:''}]) {
      draft.contexts.brief = { selectorKey:'script.content', projectId:'project-id', revisionId:'revision-id', ...patch }
      assert.throws(()=>m.buildExecutionRequestFixture(contract,draft), /项目内容修订/)
    }
    draft.contexts.brief = null
    assert.deepEqual(m.buildExecutionRequestFixture(contract,draft).inputs.brief, {kind:'literal', value:draft.literals.brief.value})
    const picker = (await vite.ssrLoadModule('/src/components/ExecutionContextFieldPicker.vue')).default
    const html = await renderToString(createSSRApp({ render:()=>h(picker,{disabled:true,modelValue:{selectorKey:'script.content',projectId:'',revisionId:''}}) }))
    for (const phrase of ['不自动选择最新版本','请选择项目','请选择剧本来源','请选择精确修订']) assert.ok(html.includes(phrase),phrase)
  } finally { await vite.close() }
})

test('source-kind contract hides literal editing and requires the exact registered context selector', async () => {
  const vite = await createServer({ root, logLevel:'silent', appType:'custom', server:{middlewareMode:true} })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
    const source = structuredClone(raw)
    source.context_fields = [{selector_key:'script.visual_context',label:'已确认项目上下文',kind:'json',scope_kind:'project',revision_kind:'script_revision',description:'真实剧本与项目事实'}]
    const spec = source.definition.configuration.inputs[0]
    spec.kind='json'; spec.targets=[{target_kind:'operation_input',target_key:'source',is_primary:true}]
    spec.semantic_contract.allowed_scope_kinds=['project']
    source.definition.interface.input_targets={source:{source_kinds:['context_field'],context_selectors:['script.visual_context']}}
    const contract=m.executionRequestFormContract(source), draft=m.emptyExecutionRequestDraft(contract)
    draft.channels.positive_prompt='Use confirmed facts'
    draft.literals.brief.value={source_text:'forged snapshot'}
    assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/真实上下文来源/)
    const component=(await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
    const html=await renderToString(createSSRApp({render:()=>h(component,{disabled:true,contract,modelValue:draft})}))
    assert.match(html,/请选择真实上下文来源/)
    assert.doesNotMatch(html,/forged snapshot|<option[^>]*>本次填写<\/option>/)
    draft.contexts.brief={selectorKey:'script.visual_context',projectId:'project',revisionId:'revision'}
    assert.equal(m.buildExecutionRequestFixture(contract,draft).inputs.brief.kind,'context_field')
    draft.contexts.brief.selectorKey='script.content'
    assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/项目内容修订/)
  } finally { await vite.close() }
})
