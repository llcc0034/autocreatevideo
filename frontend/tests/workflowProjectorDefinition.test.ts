import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { createServer } from 'vite'

const server = () => createServer({
  server: { middlewareMode: true, hmr: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
})

const editorField = (path: string, control: string, readOnly = false) => ({
  path, label: path, help: `Configure ${path}`, control, required: true, read_only: readOnly,
})

const rawOptions = {
  image_resolution: {
    native_adapter: 'comfy.flux_kontext_image_scale/v1', configured_adapter: 'comfy.image_scale_candidates/v1',
    algorithm: '按宽高比选择最近尺寸，Lanczos 居中裁切',
    modes: [{value:'native',label:'跟随原节点'}, {value:'configured',label:'配置尺寸'}],
    native_sizes: [{width:1024,height:1024}],
    presets: [{width:512,height:512,label:'512 × 512'},{width:1024,height:1024,label:'1024 × 1024'}],
    limits: {min_dimension:64,max_dimension:8192,multiple:16,max_pixels:16777216,max_candidates:32},
  },
  definition_schema_version: 1,
  editor_schema_version: 1,
  engines: [{
    key: 'generic.fixed', version: 1, label: 'Generic', description: 'Reviewed generic engine',
    projection_kind: 'generic_fixed',
    adapter_contract: { compiler: 'generic.comfy/v1', output: 'comfy.primary/v1', dynamic: 'none/v1' },
    locked_fields: ['schema_version', 'adapters', 'projection.kind'], projection_fields: [editorField('projection.config.image_resolution','image_resolution')],
    creation_template: {
      provider_driver_type: 'comfyui', operation: 'workflow.import', output_kind: '',
      definition: {
        schema_version: 1, priority: 0,
        detector: {fallback:false, allow_other_nodes:false, node_counts:{}, optional_node_counts:{}, input_equals:[]},
        output_nodes:{}, model_selectors:[], node_roles:{}, required_edges:[], parameter_policies:[], asset_slots:[],
        adapters: {compiler:'generic.comfy/v1',output:'comfy.primary/v1',dynamic:'none/v1'},
        projection: {kind:'generic_fixed',config:{}},
      },
    },
  }],
  provider_driver_types: [{ value: 'comfyui', label: 'Local ComfyUI' }],
  operations: [{ value: 'workflow.import', label: 'Import' }],
  output_kinds: [{ value: '', label: 'Auto' }, { value: 'image', label: 'Image' }],
  adapters: {
    compiler: [{ value: 'generic.comfy/v1', label: 'generic.comfy/v1' }],
    output: [{ value: 'comfy.primary/v1', label: 'comfy.primary/v1' }],
    dynamic: [{ value: 'none/v1', label: 'none/v1' }],
  },
  definition_editor: {
    sections: [{
      key: 'matching', label: 'Matching', description: 'Graph matching',
      fields: [
        editorField('schema_version', 'readonly', true),
        editorField('priority', 'integer'),
        editorField('detector.fallback', 'boolean'),
        editorField('detector.allow_other_nodes', 'boolean'),
        editorField('detector.node_counts', 'node_count_map'),
        editorField('detector.optional_node_counts', 'node_range_map'),
        editorField('detector.input_equals', 'input_equals_list'),
        editorField('output_nodes', 'output_map'),
        editorField('model_selectors', 'model_selector_list'),
        editorField('node_roles', 'node_role_map'),
        editorField('required_edges', 'edge_list'),
        editorField('parameter_policies', 'parameter_policy_list'),
        editorField('asset_slots', 'asset_slot_list'),
        editorField('adapters', 'adapter_contract', true),
        editorField('projection', 'projection_config'),
        editorField('$', 'advanced_json'),
      ],
    }],
    choices: {
      cardinalities: [{ value: 'one', label: 'One' }],
      parameter_policies: [{ value: 'runtime', label: 'Runtime' }],
      artifact_roles: [{ value: 'other', label: 'Other' }],
      media_kinds: [{ value: 'image', label: 'Image' }],
      output_kinds: [{ value: 'image', label: 'Image' }],
      contract_sources: [{ value: 'workflow', label: 'Workflow' }],
      role_selectors: [{ value: 'single', label: 'Single' }],
      scalar_types: [{ value: 'string', label: 'Text' }],
    },
    limits: {
      priority_min: 0, priority_max: 1000, node_count_max: 1000,
      input_equals_max_items: 64, model_selectors_max_items: 128,
      required_edges_max_items: 128, parameter_policies_max_items: 128,
      asset_slots_max_items: 64,
    },
  },
}

test('resolution policy choices validate bounds and share the Projector definition', async context => {
  const vite=await server(); context.after(()=>vite.close())
  const {adaptWorkflowProjectorOptions}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const {workflowProjectorDefinitionErrors}=await vite.ssrLoadModule('/src/services/workflowProjectorDefinition.ts')
  const options=adaptWorkflowProjectorOptions(rawOptions)
  const make=(sizes:unknown[])=>({...definition,projection:{kind:'generic_fixed',config:{image_resolution:{adapter:options.imageResolution.configuredAdapter,sizes}}}})
  assert.deepEqual(workflowProjectorDefinitionErrors(make([{width:512,height:512}]),options,'generic.fixed',1),[])
  for(const sizes of [[],[{width:513,height:512}],[{width:8192,height:8192}],[{width:512,height:512},{width:512,height:512}]]) {
    assert.ok(workflowProjectorDefinitionErrors(make(sizes),options,'generic.fixed',1).length)
  }
  assert.throws(()=>adaptWorkflowProjectorOptions({...rawOptions,image_resolution:undefined}),/尺寸规则目录/)
  assert.throws(()=>adaptWorkflowProjectorOptions({...rawOptions,image_resolution:{...rawOptions.image_resolution,presets:[]}}),/尺寸规则目录/)
})

test('resolution editor selects presets, adds custom sizes, removes overrides, and is read-only for published versions', async context => {
  const vite=await server(); context.after(()=>vite.close())
  const {createSSRApp,h,reactive}=await import('vue'); const {renderToString}=await import('vue/server-renderer')
  const {default:Editor}=await vite.ssrLoadModule('/src/components/WorkflowResolutionPolicyEditor.vue')
  const {adaptWorkflowProjectorOptions}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const options=adaptWorkflowProjectorOptions(rawOptions).imageResolution
  const props=reactive({config:{} as any, options, disabled:false,readOnly:false})
  let state:any; let updates=0
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(_event:string,value:unknown)=>{props.config=value;updates++}});return()=>h('div')}}))
  state.changeMode({target:{value:'configured'}})
  assert.deepEqual(props.config.image_resolution.sizes,[])
  state.toggle({width:512,height:512})
  assert.deepEqual(props.config.image_resolution.sizes,[{width:512,height:512}])
  state.width.value=768;state.height.value=512;state.addCustom()
  assert.equal(props.config.image_resolution.sizes.length,2)
  state.width.value=769;state.height.value=512;state.addCustom()
  assert.ok(state.error.value);assert.equal(props.config.image_resolution.sizes.length,2)
  props.readOnly=true
  const before=updates;state.toggle({width:512,height:512});state.changeMode({target:{value:'native'}})
  assert.equal(updates,before)
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  assert.match(html,/512 × 512/);assert.match(html,/768 × 512/)
  assert.doesNotMatch(html,/添加尺寸|>移除</)
  for(const tag of html.match(/<(?:input|select)\b[^>]*>/g)||[]) assert.match(tag,/disabled/)
  props.readOnly=false;state.changeMode({target:{value:'native'}})
  assert.deepEqual(props.config,{})
})

test('resolution inspector renders native rules and frozen configured sizes at all evidence entry points', async context => {
  const vite=await server(); context.after(()=>vite.close())
  const {createSSRApp,h}=await import('vue'); const {renderToString}=await import('vue/server-renderer')
  const {default:Inspector}=await vite.ssrLoadModule('/src/components/WorkflowResolutionInspector.vue')
  const {adaptWorkflowProjectorOptions}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const options=adaptWorkflowProjectorOptions(rawOptions).imageResolution
  const strategy={mode:'source_image_derived',adapter:options.nativeAdapter,source_node_id:'1',node_ids:['2'],primary_node_id:'3',size_path:['3','2','1'],asset_semantic_keys:['asset.image.source']}
  const render=(value:unknown)=>renderToString(createSSRApp({render:()=>h(Inspector,{contract:{resolution_strategy:value},options})}))
  const native=await render(strategy)
  assert.match(native,/原生 FluxKontextImageScale/);assert.match(native,/1024 × 1024/);assert.match(native,/3 → 2 → 1/)
  const configured=await render({...strategy,adapter:options.configuredAdapter,sizes:[{width:512,height:512}]})
  assert.match(configured,/Projector 版本配置/);assert.match(configured,/512 × 512/);assert.match(configured,/ImageScale/)
  const settings=await readFile(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
  const version=await readFile(new URL('../src/views/WorkflowVersionEditorView.vue',import.meta.url),'utf8')
  assert.match(settings,/<WorkflowResolutionInspector :contract="savedWorkflowDrafts/)
  assert.match(settings,/<WorkflowResolutionInspector :contract="detailWorkflowInputContract"/)
  assert.match(version,/<WorkflowResolutionInspector :contract="inputContract"/)
})

const definition = {
  schema_version: 1,
  priority: 0,
  detector: { fallback: true, allow_other_nodes: true, node_counts: {}, optional_node_counts: {}, input_equals: [] },
  output_nodes: { SaveImage: 'image' },
  model_selectors: [],
  node_roles: {},
  required_edges: [],
  parameter_policies: [],
  asset_slots: [],
  adapters: { compiler: 'generic.comfy/v1', output: 'comfy.primary/v1', dynamic: 'none/v1' },
  projection: { kind: 'generic_fixed', config: {} },
}

const recommendation = {
  schema_version: 1,
  persistence: 'unsaved_draft',
  requires_human_confirmation: true,
  engine_key: 'generic.fixed',
  engine_version: 1,
  provider_driver_type: 'comfyui',
  operation: 'workflow.import',
  output_kind: 'image',
  definition,
  source_evidence: {
    candidate_source_id: '11111111-1111-4111-8111-111111111111',
    source_scope_schema_version: 1,
    source_scope_sha256: '1'.repeat(64),
    source_sha256: '2'.repeat(64),
    source_path_sha256: '3'.repeat(64),
    object_info_schema_sha256: '4'.repeat(64),
    converted_graph_sha256: '5'.repeat(64),
    primary_canvas_node_id: '123',
    primary_api_node_id: '2',
    primary_node_type: 'SaveImage',
  },
  catalog: {
    node_types: [
      { value: 'LoadImage', count: 1, primary_output: false },
      { value: 'SaveImage', count: 1, primary_output: true },
    ],
    inputs_by_node_type: { LoadImage: ['image'], SaveImage: ['images', 'filename_prefix'] },
    observed_edges: [{
      from_node_type: 'LoadImage', output_index: 0, to_node_type: 'SaveImage',
      input_name: 'images', recommended: true,
    }],
    parameter_targets: [{
      semantic_key: 'output.prefix', node_type: 'SaveImage',
      input_name: 'filename_prefix', value_type: 'string',
    }],
  },
  warnings: ['Single-candidate structure requires review.'],
  recommendation_sha256: '6'.repeat(64),
}

test('Projector options keep editor choices and engine adapter contracts server-owned', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const options = adapters.adaptWorkflowProjectorOptions(rawOptions)
    assert.equal(options.editorSchemaVersion, 1)
    assert.deepEqual(options.engines[0].adapterContract, rawOptions.engines[0].adapter_contract)
    assert.equal(options.definitionEditor.limits.parameterPoliciesMaxItems, 128)
    assert.equal(options.definitionEditor.sections[0].fields[0].readOnly, true)

    const unsupported = structuredClone(rawOptions)
    unsupported.definition_editor.sections[0].fields[1].control = 'future_widget'
    assert.throws(
      () => adapters.adaptWorkflowProjectorOptions(unsupported),
      /editor field is incomplete/,
      'an unknown renderer control must fail the whole options contract closed',
    )
  } finally { await vite.close() }
})

test('structured Projector editing preserves types and fails closed on engine drift', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const editor = await vite.ssrLoadModule('/src/services/workflowProjectorDefinition.ts')
    const options = adapters.adaptWorkflowProjectorOptions(rawOptions)
    assert.deepEqual(editor.workflowProjectorDefinitionErrors(definition, options, 'generic.fixed', 1), [])

    const changed = editor.setWorkflowProjectorPath(definition, 'detector.allow_other_nodes', false)
    assert.equal(changed.detector.allow_other_nodes, false)
    assert.equal(definition.detector.allow_other_nodes, true, 'the frozen seed is not mutated')

    const reactiveLikeDefinition = new Proxy(structuredClone(definition), {})
    assert.throws(
      () => structuredClone(reactiveLikeDefinition),
      { name: 'DataCloneError' },
      'the browser primitive cannot clone the reactive proxies used by Vue',
    )
    const reactiveClone = editor.cloneWorkflowProjectorDefinition(reactiveLikeDefinition)
    assert.deepEqual(reactiveClone, definition)
    assert.notEqual(reactiveClone, reactiveLikeDefinition, 'editing must detach from Vue reactive state')

    const invalid = structuredClone(definition)
    invalid.adapters.compiler = 'private/v1'
    assert.match(
      editor.workflowProjectorDefinitionErrors(invalid, options, 'generic.fixed', 1).join('\n'),
      /generic\.comfy\/v1/,
    )
    assert.equal(editor.parseWorkflowProjectorJson('[]').value, null)
    assert.match(editor.parseWorkflowProjectorJson('{').error, /JSON/)

    const invalidRow = structuredClone(definition)
    invalidRow.parameter_policies.push({
      semantic_key: '', node_role: '', input_name: '', policy: 'runtime', value: 4,
    })
    assert.match(
      editor.workflowProjectorDefinitionErrors(invalidRow, options, 'generic.fixed', 1).join('\n'),
      /参数策略存在/,
    )

    const nonJsonNumber = structuredClone(definition)
    nonJsonNumber.detector.input_equals.push({ node_type: 'Node', input_name: 'value', value: Number.NaN })
    const cloned = editor.cloneWorkflowProjectorDefinition(nonJsonNumber)
    assert.equal(Number.isNaN(cloned.detector.input_equals[0].value), true, 'clone must not normalize NaN to null')
    assert.match(
      editor.workflowProjectorDefinitionErrors(cloned, options, 'generic.fixed', 1).join('\n'),
      /非 JSON 标量值/,
    )
  } finally { await vite.close() }
})

test('Projector recommendation adapter binds safe graph catalogs to an unsaved draft', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const adapted = adapters.adaptWorkflowProjectorRecommendation(recommendation)
    assert.equal(adapted.persistence, 'unsaved_draft')
    assert.equal(adapted.sourceEvidence.primaryNodeType, 'SaveImage')
    assert.deepEqual(adapted.catalog.inputsByNodeType.SaveImage, ['images', 'filename_prefix'])
    const dynamic = structuredClone(recommendation)
    dynamic.engine_key = 'generic.references'
    assert.equal(adapters.adaptWorkflowProjectorRecommendation(dynamic).engineKey, 'generic.references')

    const stale = structuredClone(recommendation)
    stale.source_evidence.converted_graph_sha256 = ''
    assert.throws(
      () => adapters.adaptWorkflowProjectorRecommendation(stale),
      /invalid evidence contract/,
    )
  } finally { await vite.close() }
})

test('Settings uses the structured editor and keeps full JSON in the advanced section', async () => {
  const settings = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const editor = await readFile(new URL('../src/components/WorkflowProjectorDefinitionEditor.vue', import.meta.url), 'utf8')
  assert.match(settings, /WorkflowProjectorDefinitionEditor/)
  assert.match(settings, /changeWorkflowProjectorCreateSeed/)
  for (const label of ['从工作流推荐', '复制已有方案', '空白新建']) assert.ok(settings.includes(label))
  assert.doesNotMatch(settings, /复制的受审起点|useGenericReferenceProjectorEngine/)
  assert.match(settings, /workflowProjectorRefresh/)
  assert.match(settings, /revealWorkflowProjectorCatalog/)
  assert.match(settings, /recommendWorkflowProjectorFromCandidate/)
  assert.match(settings, /基于此候选生成专用 Projector 建议/)
  assert.match(settings, /:recommendation="workflowProjectorRecommendation"/)
  assert.match(settings, /:aria-expanded="workflowProjectorCatalogOpen"/)
  assert.doesNotMatch(settings, /href="#workflow-projector-catalog"/)
  assert.match(settings, /它是“图结构解释规则”，不是本地工作流文件目录/)
  assert.doesNotMatch(settings, /完整 Projector definition（JSON 对象）/)
  assert.match(editor, /高级完整视图/)
  assert.match(editor, /parameter_policy_list/)
  assert.match(editor, /model_selector_list/)
  assert.match(editor, /classified_reference_config/)
  assert.match(editor, /WorkflowClassifiedReferenceEditor/)
  assert.doesNotMatch(editor, /dynamic_group_map|frame_alignment|model_target_list/)
  assert.match(editor, /wp-recommendation-note/)
  assert.match(editor, /<datalist/)
})

test('blank templates fail closed until completed and reject stale engine identity', async (context) => {
  const vite = await server()
  context.after(() => vite.close())
  const {adaptWorkflowProjectorOptions} = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const {workflowProjectorDefinitionErrors,cloneWorkflowProjectorDefinition} = await vite.ssrLoadModule('/src/services/workflowProjectorDefinition.ts')
  const options = adaptWorkflowProjectorOptions(rawOptions)
  const template = options.engines[0].creationTemplate
  assert.ok(workflowProjectorDefinitionErrors(template.definition,options,'generic.fixed',1).length)
  const draft = cloneWorkflowProjectorDefinition(template.definition)
  draft.output_nodes = {SaveImage:'image'}
  draft.detector.node_counts = {SaveImage:1}
  assert.deepEqual(workflowProjectorDefinitionErrors(draft,options,'generic.fixed',1), [])
  assert.deepEqual(template.definition.output_nodes,{})
  const missing = structuredClone(rawOptions) as any
  delete missing.engines[0].creation_template
  assert.throws(()=>adaptWorkflowProjectorOptions(missing))
  const drift = structuredClone(rawOptions)
  drift.engines[0].creation_template.definition.adapters.compiler = 'not.registered/v1'
  assert.throws(()=>adaptWorkflowProjectorOptions(drift), /creation template disagrees/)
})

test('Settings creation handlers work with no existing versions and preserve cancelled edits', async (context) => {
  const vite = await server()
  context.after(() => vite.close())
  const {createSSRApp,h} = await import('vue')
  const {renderToString} = await import('vue/server-renderer')
  const {createPinia} = await import('pinia')
  const {routeLocationKey,routerKey} = await import('vue-router')
  let approve = false
  let confirmations = 0
  const oldStorage=Object.getOwnPropertyDescriptor(globalThis,'localStorage')
  const oldWindow=Object.getOwnPropertyDescriptor(globalThis,'window')
  const oldDocument=Object.getOwnPropertyDescriptor(globalThis,'document')
  context.after(()=>{
    for (const [key,descriptor] of [['localStorage',oldStorage],['window',oldWindow],['document',oldDocument]] as const) {
      if(descriptor) Object.defineProperty(globalThis,key,descriptor)
      else Reflect.deleteProperty(globalThis,key)
    }
  })
  Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}}})
  Object.defineProperty(globalThis,'window',{configurable:true,value:{confirm:()=>{confirmations++;return approve},localStorage:globalThis.localStorage}})
  const {default: Settings} = await vite.ssrLoadModule('/src/views/SettingsView.vue')
  const {adaptWorkflowProjectorOptions} = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const {platformApi} = await vite.ssrLoadModule('/src/services/platformApi.ts')
  context.mock.method(platformApi,'createWorkflowProjector',async()=>{throw new Error('uninitialized form must never write')})
  let state: any
  const app = createSSRApp({
    setup(){state=Settings.setup({}, {expose:()=>{}});return ()=>h('div')},
  })
  app.use(createPinia())
  app.provide(routeLocationKey,{query:{section:'workflows'},fullPath:'/settings?section=workflows'} as any)
  app.provide(routerKey,{replace:async()=>{}} as any)
  await renderToString(app)
  state.auth.user={isSuperuser:true}
  state.workflowProjectorOptions.value=adaptWorkflowProjectorOptions(rawOptions)
  state.openWorkflowProjectorCreate()
  assert.equal(state.workflowProjectorCreateOpen.value,true,'no seed versions are required')
  assert.equal(state.workflowProjectorCreateMode.value,'recommendation')
  assert.equal(state.workflowProjectorCreateInitialized.value,false)
  await state.submitWorkflowProjectorCreate()
  assert.match(state.workflowProjectorError.value,/初始化/)
  state.changeWorkflowProjectorCreateMode('blank')
  state.initializeBlankWorkflowProjector({target:{value:'generic.fixed\u00001'}})
  assert.equal(state.workflowProjectorCreateInitialized.value,true)
  assert.deepEqual(state.workflowProjectorCreateDefinition.value.output_nodes,{})
  state.workflowProjectorCreateFamily.name='Independent family'
  state.workflowProjectorCreateDefinition.value.output_nodes={SaveImage:'image'}
  state.changeWorkflowProjectorCreateMode('copy')
  assert.equal(confirmations,1)
  assert.equal(state.workflowProjectorCreateMode.value,'blank')
  assert.deepEqual(state.workflowProjectorCreateDefinition.value.output_nodes,{SaveImage:'image'})
  approve=true
  state.changeWorkflowProjectorCreateMode('copy')
  assert.equal(state.workflowProjectorCreateMode.value,'copy')
  assert.equal(state.workflowProjectorCreateInitialized.value,false)
  assert.equal(state.workflowProjectorCreateFamily.name,'Independent family')
  state.changeWorkflowProjectorCreateMode('blank')
  state.initializeBlankWorkflowProjector({target:{value:'generic.fixed\u00001'}})
  assert.deepEqual(state.workflowProjectorCreateDefinition.value.output_nodes,{})
  assert.deepEqual(state.workflowProjectorOptions.value.engines[0].creationTemplate.definition.output_nodes,{})
  const seed={id:'seed-one',projectorKey:'existing',version:1,status:'published',isCurrent:true,engineKey:'generic.fixed',engineVersion:1,providerDriverType:'comfyui',operation:'workflow.import',outputKind:'image',definition:structuredClone(definition)}
  state.workflowProjectorVersions.value=[seed]
  state.changeWorkflowProjectorCreateMode('copy')
  state.changeWorkflowProjectorCreateSeed({target:{value:seed.id}})
  assert.equal(state.workflowProjectorCreateInitialized.value,true)
  assert.deepEqual(state.workflowProjectorCreateDefinition.value,definition)
  state.workflowProjectorCreateDefinition.value.priority=7
  assert.equal(seed.definition.priority,0,'copied configuration is detached from the published source')

  state.workflowProjectorVersions.value=[]
  state.changeWorkflowProjectorCreateMode('recommendation')
  state.workflowModelContextCatalogLoaded.value=true
  state.savedWorkflowScanState.value='fresh'
  const evidence=recommendation.source_evidence
  const candidate={sourceKey:'candidate-key',sourcePath:'example.json',sourceSha256:evidence.source_sha256,sourceScopeSchemaVersion:1,sourceScopeSha256:evidence.source_scope_sha256,candidateManaged:true,candidateSourceId:evidence.candidate_source_id,suggestedImportKind:'template_version',errorCode:''}
  state.savedWorkflowItems.value=[candidate]
  state.savedWorkflowDrafts['candidate-key']={importKind:'template_version',primaryOutputNodeId:'123',outputKind:'image',previewError:''}
  state.workflowProjectorCreateCandidateKey.value='candidate-key'
  const {adaptWorkflowProjectorRecommendation}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const recommendMock=context.mock.method(platformApi,'recommendWorkflowProjector',async()=>adaptWorkflowProjectorRecommendation(recommendation))
  await state.requestProjectorRecommendationFromCreate()
  assert.equal(recommendMock.mock.callCount(),1,'recommendation no longer needs a generic seed version')
  assert.equal(state.workflowProjectorCreateInitialized.value,true)
  assert.equal(state.workflowProjectorCreateFamily.name,'Independent family')
  state.savedWorkflowDrafts['candidate-key'].primaryOutputNodeId='other-output'
  assert.equal(state.workflowProjectorCreateInitialized.value,false,'changing the main output invalidates the recommendation')
  state.savedWorkflowDrafts['candidate-key'].primaryOutputNodeId='123'
  state.savedWorkflowScanState.value='failed'
  assert.equal(state.workflowProjectorCreateInitialized.value,false,'failed scans cannot silently reuse the old recommendation')

  let importer: any = null
  Object.defineProperty(globalThis,'document',{configurable:true,value:{getElementById:(id:string)=>id==='saved-workflow-importer'?importer:null}})
  const familyBeforeScan=JSON.stringify(state.workflowProjectorCreateFamily)
  const definitionBeforeScan=JSON.stringify(state.workflowProjectorCreateDefinition.value)
  await state.goToProjectorCandidateScan()
  assert.equal(state.workflowProjectorCreateOpen.value,true,'missing scan area must not hide the form')
  assert.match(state.workflowProjectorError.value,/扫描区暂不可用/)
  assert.equal(state.workflowProjectorResumeFromScan.value,false)
  const navigationCalls: string[]=[]
  importer={open:false,scrollIntoView:()=>navigationCalls.push('scroll'),querySelector:(selector:string)=>{
    assert.equal(selector,'[data-projector-scan-action]')
    return {focus:(options:any)=>{assert.equal(options.preventScroll,true);navigationCalls.push('focus')}}
  }}
  state.savedWorkflowBusy.value='scan'
  await state.goToProjectorCandidateScan()
  assert.equal(state.workflowProjectorCreateOpen.value,true,'pending work blocks navigation')
  state.savedWorkflowBusy.value=''
  await state.goToProjectorCandidateScan()
  assert.equal(state.workflowProjectorCreateOpen.value,false)
  assert.equal(state.workflowProjectorResumeFromScan.value,true)
  assert.equal(importer.open,true)
  assert.deepEqual(navigationCalls,['scroll','focus'])
  state.openWorkflowProjectorCreate()
  assert.equal(state.workflowProjectorCreateOpen.value,true)
  assert.equal(JSON.stringify(state.workflowProjectorCreateFamily),familyBeforeScan)
  assert.equal(JSON.stringify(state.workflowProjectorCreateDefinition.value),definitionBeforeScan)
  assert.equal(state.workflowProjectorCreateCandidateKey.value,'candidate-key')
  assert.equal(recommendMock.mock.callCount(),1,'scan navigation and return must not request recommendations')
})

test('read-only Projector renders a copyable JSON view without edit actions', async (context) => {
  const vite = await server()
  context.after(() => vite.close())
  const {createSSRApp, h} = await import('vue')
  const {renderToString} = await import('vue/server-renderer')
  const {default: Editor} = await vite.ssrLoadModule('/src/components/WorkflowProjectorDefinitionEditor.vue')
  const {adaptWorkflowProjectorOptions} = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const props = {modelValue:definition,options:adaptWorkflowProjectorOptions(rawOptions),engineKey:'generic.fixed',engineVersion:1,readOnly:true}
  const html = await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  assert.match(html, /textarea[^>]*readonly/)
  for (const control of html.match(/<(?:input|select)\b[^>]*>/g) || []) assert.match(control, /disabled/)
  assert.match(html, /此版本配置只读/)
  assert.doesNotMatch(html, /应用到参数表单|放弃文本修改/)
  assert.match(html, /generic_fixed/)
})

test('generic dynamic groups have one editor; asset slots are a linked read-only summary', async (context) => {
  const vite=await server();context.after(()=>vite.close())
  const {createSSRApp,h}=await import('vue')
  const {renderToString}=await import('vue/server-renderer')
  const {default:Editor}=await vite.ssrLoadModule('/src/components/WorkflowProjectorDefinitionEditor.vue')
  const {adaptWorkflowProjectorOptions}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const options=adaptWorkflowProjectorOptions(rawOptions)
  options.engines[0]={...options.engines[0],key:'generic.references',projectionKind:'generic_references',adapterContract:{compiler:'generic.comfy/v2',output:'comfy.primary/v1',dynamic:'generic.asset_groups/v1'},projectionFields:[{path:'projection.config.groups',label:'动态素材组接线',help:'',control:'asset_group_list',required:true,readOnly:false}]}
  options.definitionEditor.choices.assetGroupWirings=[{value:'ordered_ports',label:'顺序端口'}]
  const draft={...definition,asset_slots:[{key:'refs',media_kind:'image',contract_source:'workflow',dynamic:true}],adapters:options.engines[0].adapterContract,projection:{kind:'generic_references',config:{groups:[{key:'refs',source_role:'',branch_roles:[],output_role:'',output_index:0,target_roles:[],input_names:[],wiring:'ordered_ports',reducer_role:'',reducer_inputs:[],reducer_output_index:0,minimum_items:1,maximum_items:3,conditioning_mode:'',model_input:''}]}}}
  const html=await renderToString(createSSRApp({render:()=>h(Editor,{modelValue:draft,options,engineKey:'generic.references',engineVersion:1})}))
  assert.match(html,/只读摘要/)
  assert.match(html,/编辑动态素材组/)
  assert.doesNotMatch(html,/添加素材槽|aria-label="删除素材槽"/)
  assert.equal((html.match(/数量合同来源/g)||[]).length,2,'one source selector plus its detail button')
  assert.match(html,/查看数量合同来源/)
})
