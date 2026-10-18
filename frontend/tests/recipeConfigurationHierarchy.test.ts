import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'

async function check(fn: (vite: any, data: any, projection: any) => Promise<void>) {
  const vite = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const data = await vite.ssrLoadModule('/tests/fixtures/recipe-hierarchy-data.ts')
    const projection = await vite.ssrLoadModule('/src/services/recipeConfigurationPresentation.ts')
    await fn(vite, data, projection)
  } finally { await vite.close() }
}

test('recipe data flow follows exact declared nested sources and never literal business text or adjacent steps', () => check(async (_, data, m) => {
  const definition = data.recipeHierarchyDefinition()
  definition.stages.push({ key: 'step-independent', kind: 'custom_text', name: '同名步骤', mode: 'ai', approval: 'auto', routing: { routes: [{ input_sources: {
    direct: { kind: 'selected_output', stage_key: 'script_generate', output_key: 'story' },
    custom: { kind: 'structured', template: { kind: 'object', fields: {
      items: { kind: 'array', items: [{ kind: 'each', source: { kind: 'selected_output', stage_key: 'script_generate', output_key: 'rows' }, item: { kind: 'unit_map', value: { kind: 'selected_output', stage_key: 'script_generate', output_key: 'meta' } } }] },
      untouched: { kind: 'literal', value: { kind: 'selected_output', stage_key: 'script_generate', output_key: 'FAKE' } },
    } } },
  } }] } })
  const before = JSON.stringify(definition)
  const result = m.recipeConfigurationSteps(definition, data.recipeHierarchyOptions)
  assert.equal(result[0].consumers.length, 3)
  assert.deepEqual(result[0].consumers.map(row => row.output), ['story', 'rows', 'meta'])
  assert.ok(result[0].consumers.every(row => row.orderValid && row.label === '3 · 同名步骤'))
  assert.equal(result[1].consumers.length, 0)
  assert.equal(JSON.stringify(definition), before)
}))

test('read-only projection preserves invalid bindings, false, zero, empty and source identity without choosing defaults', () => check(async (_, data, m) => {
  const definition = data.recipeHierarchyDefinition()
  definition.stages[0].mode = 'review'
  const route = definition.stages[0].routing.routes[0]
  route.input_sources = { flag: { kind: 'literal', value: false }, zero: { kind: 'literal', value: 0 }, blank: { kind: 'literal', value: '' } }
  const result = m.recipeConfigurationSteps(definition, null)
  assert.deepEqual(result[0].groups[0].routes[0], route)
  assert.match(m.recipeSourceSummary({ kind: 'selected_output', output_key: 'same' }), /步骤未声明/)
  assert.equal(m.recipeSourceSummary({ kind: 'not-cataloged' }), 'not-cataloged')
  definition.stages[0].routing.routes[0].input_sources.bad = { kind: 'selected_output', stage_key: 'script_review', output_key: 'past' }
  assert.equal(m.recipeConfigurationSteps(definition)[1].consumers[0].orderValid, false)
}))

test('assembly and reviewed reference consumers retain exact targets; duplicate step identities are not safe', () => check(async (_, data, m) => {
  const definition = data.recipeHierarchyDefinition()
  definition.stages.push({ key: 'assembly', mode: 'existing_pipeline', approval: 'auto', assembly_output: { source: { kind: 'selected_output', stage_key: 'script_generate', output_key: 'plan' } } })
  definition.stages.push({ key: 'refs', shot_reference_selection: { source: 'upstream', source_stage_key: 'script_generate' } })
  assert.deepEqual(m.recipeConfigurationSteps(definition)[0].consumers.map(row => row.target), ['assembly_output', '逐镜参考素材'])
  definition.stages.push({ key: 'script_generate' })
  assert.ok(m.recipeConfigurationSteps(definition)[0].consumers.every(row => !row.orderValid))
}))

test('output source descriptions distinguish absent, blank, invalid and conflicting declarations without rewriting', () => check(async (_, data, m) => {
  assert.match(m.recipeOutputSourceSummary({}), /来源缺失/)
  assert.match(m.recipeOutputSourceSummary({ source_text: '' }), /原文为空/)
  assert.match(m.recipeOutputSourceSummary({ source_text: null }), /类型无效/)
  const rule = { source_text: ' 原文\r\n🌧 ' }, original = JSON.stringify(rule)
  assert.match(m.recipeOutputSourceSummary(rule), /已填写原文/)
  assert.equal(JSON.stringify(rule), original)
  assert.match(m.recipeOutputSourceSummary({ source_text: '', source: {} }), /来源冲突/)
  assert.match(m.recipeOutputSourceSummary({ source: { kind: 'selected_output', stage_key: 'exact', output_key: 'plan' } }, data.recipeHierarchyOptions), /exact → plan/)
}))

test('recipe view is semantic and lazy: no disabled author, catalog calls, contract loads or writes on display', () => check(async (vite, data) => {
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  let calls = 0
  api.get = api.post = api.patch = () => { calls++; throw Error('network forbidden') }
  const { default: View } = await vite.ssrLoadModule('/src/components/ProductionRecipeFlow.vue')
  const app = createSSRApp(View, { recipe: { definition: data.recipeHierarchyDefinition(), definitionSha256: 'a'.repeat(64) }, recipeEditorOptions: data.recipeHierarchyOptions, showRaw: true })
  app.use(createRouter({ history: createMemoryHistory(), routes: [] }))
  const html = await renderToString(app)
  for (const text of ['本步骤给实例提供什么', '结果去向', '查看执行实例', '阶段审核', '来源原值与映射']) assert.ok(html.includes(text), text)
  assert.doesNotMatch(html, /<select|<input|<textarea|recipe-editor/)
  assert.match(html, /下钻本身不核验摘要或执行准入/)
  assert.equal(calls, 0)
}))

test('author renders four named sections with one set of original controls and semantic review', () => check(async (vite, data) => {
  const { default: Editor } = await vite.ssrLoadModule('/src/components/ProductionRecipeEditor.vue')
  const definition = data.recipeHierarchyDefinition(); delete definition.stages[0].routing
  const before = JSON.stringify(definition), writes: unknown[] = []
  const app = createSSRApp(Editor, { modelValue: definition, options: data.recipeHierarchyOptions, 'onUpdate:modelValue': (value: unknown) => writes.push(value) })
  app.use(createRouter({ history: createMemoryHistory(), routes: [] }))
  const html = await renderToString(app)
  for (const key of ['steps', 'bindings', 'rules', 'review']) assert.match(html, new RegExp(`data-authoring-section="${key}"`))
  assert.doesNotMatch(html, /configuration-authoring-errors/)
  assert.equal((html.match(/>制作方式<select/g) || []).length, 1)
  assert.equal((html.match(/>阶段职责<select/g) || []).length, 2)
  assert.equal((html.match(/>阶段审核<select/g) || []).length, 2)
  assert.equal((html.match(/>生成字段确认<select/g) || []).length, 1)
  assert.match(html, /此执行方式需要实例绑定，当前尚未配置/)
  assert.equal(JSON.stringify(definition), before); assert.equal(writes.length, 0)
}))

test('read-only view distinguishes missing steps and empty steps and renders explicit contract failures', () => check(async (vite, data) => {
  const { default: View } = await vite.ssrLoadModule('/src/components/ProductionRecipeFlow.vue')
  const render = async definition => {
    const app = createSSRApp(View, { recipe: { definition }, readiness: { reasonCodes: [], validationErrors: [{ stageIndex: 0, stageKey: 'script_generate', routingKey: 'routing', routeIndex: 0, fieldPath: 'stages.0.routing', message: '冻结摘要不一致', code: 'invalid' }], capabilities: {} } })
    app.use(createRouter({ history: createMemoryHistory(), routes: [] })); return renderToString(app)
  }
  assert.match(await render({ schema_version: 3 }), /步骤声明缺失/)
  assert.match(await render({ schema_version: 3, stages: [] }), /尚未配置步骤/)
  const html = await render(data.recipeHierarchyDefinition())
  assert.match(html, /冻结摘要不一致/); assert.match(html, /stages.0.routing/)
}))

test('whole recipe author uses catalog identity and single original editor with labelled controls',()=>check(async(vite,data)=>{
  const { default: Author }=await vite.ssrLoadModule('/src/components/ProductionRecipeAuthorForm.vue')
  const definition={schema_version:3,interaction_mode:'assisted',stages:[{key:'script_generate',mode:'manual',approval:'auto'}]}
  const props={modelValue:{recipe_key:'fixture',name:' 保留 🌙 ',definition},dependencies:{providers:[],modelProfiles:[],deployments:[],runtimeHosts:[],recipes:[],configDefinitions:[],recipeEditorOptions:data.recipeHierarchyOptions,lockedPaths:['version','definition.schema_version']},status:'新建配方 · 尚未保存',issues:[]}
  const app=createSSRApp(Author,props);app.use(createRouter({history:createMemoryHistory(),routes:[]}))
  const html=await renderToString(app)
  for(const section of ['identity','steps','bindings','rules','review'])assert.match(html,new RegExp(`data-authoring-section="${section}"`))
  assert.equal((html.match(/<form /g)||[]).length,1);assert.match(html,/<select[^>]*aria-label="新配方族标识/)
  assert.match(html,/value=" 保留 🌙 "/);assert.match(html,/保存草稿不会发布配方/)
  assert.doesNotMatch(html,/<input[^>]*aria-label="版本号"/)
}))

test('complex recipe keeps repeated generators distinct and all four assembly delivery sources exact',()=>check(async(vite,_,projection)=>{
  const data=await vite.ssrLoadModule('/tests/fixtures/recipe-complex-data.ts')
  const {recipeAuthoringErrors}=await vite.ssrLoadModule('/src/services/productionRecipeAuthoring.ts')
  const definition=data.complexDefinition(),before=JSON.stringify(definition)
  assert.deepEqual(recipeAuthoringErrors(definition,data.complexRecipeOptions),[])
  const {default:View}=await vite.ssrLoadModule('/src/components/ProductionRecipeDefinitionView.vue')
  const html=await renderToString(createSSRApp(View,{definition,options:data.complexRecipeOptions}))
  for(const label of ['制作计划 1','制作计划 2','装配输出','装配时序','已采用片段','交付标题与说明','plan_b'])assert.ok(html.includes(label),label)
  assert.equal(JSON.stringify(definition),before)
  assert.equal(definition.stages[2].assembly_output.source_text,data.complexRaw)
  assert.equal(definition.stages[2].assembly_media.source_text,data.complexRaw)
  for(const rule of [definition.stages[2].assembly_timing,definition.stages[3].delivery_metadata]){
    assert.equal(rule.source.stage_key,'plan_b');assert.equal(rule.output_version_id,data.complexOutputId)
    assert.equal(rule.expected_contract_sha256,data.complexSha);assert.ok(!('source_text' in rule))
  }
}))

test('complex whole author exposes four public output drilldowns without copying model configuration',()=>check(async(vite)=>{
  const data=await vite.ssrLoadModule('/tests/fixtures/recipe-complex-data.ts')
  const {default:Author}=await vite.ssrLoadModule('/src/components/ProductionRecipeAuthorForm.vue')
  const props={modelValue:{recipe_key:'complex-new',name:'新制作方案',definition:data.complexDefinition()},dependencies:{providers:[],modelProfiles:[],deployments:[],runtimeHosts:[],recipes:[],configDefinitions:[],recipeEditorOptions:data.complexRecipeOptions,lockedPaths:['version','definition.schema_version']},status:'隔离草稿',issues:[]}
  const app=createSSRApp(Author,props);app.use(createRouter({history:createMemoryHistory(),routes:[]}))
  const html=await renderToString(app)
  assert.equal((html.match(/查看输出定义与使用方/g)||[]).length,4)
  for(const name of ['移除装配输出映射','移除装配时序映射','移除片段核对映射','移除交付字段映射'])assert.ok(html.includes(name))
  assert.equal((html.match(/<form /g)||[]).length,1)
  assert.doesNotMatch(html,/<select[^>]*aria-label="模型部署"/)
}))
