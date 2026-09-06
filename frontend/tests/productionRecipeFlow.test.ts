import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('..', import.meta.url))

test('production recipe flow projects workflow inputs, model binding, output and declared next stage', async (context) => {
  const vite = await createServer({ root: frontendRoot, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  context.after(async () => { await vite.close() })
  const service = await vite.ssrLoadModule('/src/services/productionRecipeFlow.ts')
  const flow = service.buildProductionRecipeFlow({
    schema_version: 2,
    interaction_mode: 'unattended',
    stages: [
      { key: 'script_generate', mode: 'ai', approval: 'auto', routing: { mode: 'fixed', variant_count: 1, routes: [{ model_deployment_id: 'deployment-text', workflow_version_id: null }] } },
      { key: 'script_review', mode: 'review', approval: 'policy', decision_policy: 'approve_frozen_script_v1' },
      { key: 'media_generation', mode: 'existing_pipeline', approval: 'auto', routing: { mode: 'fixed', variant_count: 1, routes: [{ model_deployment_id: 'deployment-video', workflow_version_id: 'workflow-video' }] } },
      { key: 'artifact_selection', mode: 'review', approval: 'required' },
      { key: 'delivery', mode: 'existing_pipeline', approval: 'auto' },
    ],
  }, {
    deployments: [
      { id: 'deployment-text', name: 'DeepSeek 文本部署', key: 'deepseek-text', profileVersion: 'model-text', providerDriver: 'async_rest', status: 'active' },
      { id: 'deployment-video', name: 'H3 视频部署', key: 'h3-video', profileVersion: 'model-video', providerDriver: 'comfyui', status: 'active' },
    ],
    modelProfiles: [
      { id: 'model-text', profileKey: 'deepseek', version: 3, providerKey: 'deepseek', modelIdentifier: 'deepseek-chat', modality: 'text', status: 'published' },
      { id: 'model-video', profileKey: 'minimax-h3', version: 5, providerKey: 'comfyui-local', modelIdentifier: 'minimax-h3-ref2va', modality: 'video', status: 'published' },
    ],
    workflows: [{
      id: 'workflow-video',
      templateName: 'H3 Ref2VA',
      templateSlug: 'h3-ref2va',
      version: 5,
      status: 'retired',
      outputKind: 'video',
      modelProfileVersion: 'model-video',
      inputContractSha256: 'input-contract-sha',
      semanticBindingCount: 2,
      inputContract: {
        schema_version: 3,
        parameters: {
          'prompt.positive': { type: 'text', required: true },
          'video.width': { type: 'integer', required: true, min: 768, max: 768, multiple_of: 64 },
          'model.diffusion_model_1.filename': { type: 'string', required: true },
        },
        conditioning_contract: {
          modes: [{ key: 'dual_clock', label: '双时钟参考', inputs: { 'reference.images': { minimum_items: 1, maximum_items: 3 } } }],
        },
        outputs: { primary: { node_id: '13', kind: 'video', required: true } },
      },
      semanticBindings: [
        { id: 'binding-prompt', semanticKey: 'prompt.positive', nodeId: '7', inputName: 'text', valueType: 'text', isRequired: true },
        { id: 'binding-width', semanticKey: 'video.width', nodeId: '14', inputName: 'width', valueType: 'integer', isRequired: true },
      ],
      modelRequirements: [{ id: 'requirement-1', role: 'diffusion_model', alias: 'h3', artifactId: 'artifact-1', expectedFilename: 'h3.safetensors', expectedSha256: 'model-sha', isRequired: true }],
    }],
    workflowDetailStates: { 'workflow-video': 'loaded' },
  })

  assert.equal(flow.stageCount, 5)
  assert.equal(flow.routeCount, 2)
  assert.equal(flow.reviewGateCount, 2)
  assert.equal('edges' in flow, false)
  const mediaStage = flow.stages[2]
  const mediaRoute = mediaStage.routes[0]
  assert.equal(mediaRoute.modelIdentifier, 'minimax-h3-ref2va')
  assert.equal(mediaRoute.modelProfileStatus, 'published')
  assert.equal(mediaRoute.workflowStatus, 'retired')
  assert.equal(mediaRoute.workflowDetailState, 'loaded')
  assert.deepEqual(mediaRoute.inputs.map((item: { semanticKey: string }) => item.semanticKey), ['prompt.positive', 'video.width'])
  assert.deepEqual(mediaRoute.inputs[1].constraints, ['范围 768–768', '步长 64'])
  assert.equal(mediaRoute.dynamicInputs[0].semanticKey, 'reference.images')
  assert.equal(mediaRoute.dynamicInputs[0].maximumItems, 3)
  assert.equal(mediaRoute.modelRequirements[0].expectedFilename, 'h3.safetensors')
  assert.deepEqual(mediaRoute.outputs[0], { key: 'primary', kind: 'video', nodeId: '13', required: true })
  assert.deepEqual(mediaStage.nextDeclaration, { index: 3, key: 'artifact_selection', label: '产物选片（artifact_selection）' })
  assert.equal(flow.stages[0].routes[0].workflowDetailState, 'not_applicable')
  assert.deepEqual(flow.issues, [])
})

test('production recipe flow fails closed on empty or malformed stage declarations', async (context) => {
  const vite = await createServer({ root: frontendRoot, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  context.after(async () => { await vite.close() })
  const service = await vite.ssrLoadModule('/src/services/productionRecipeFlow.ts')
  const empty = service.buildProductionRecipeFlow({ schema_version: 1, stages: [] })
  assert.equal(empty.stageCount, 0)
  assert.ok(empty.issues.some((issue: string) => issue.includes('stages 为空')))

  const malformed = service.buildProductionRecipeFlow({
    schema_version: 1,
    stages: [
      { key: 'script_input', mode: 'manual', approval: 'required' },
      { key: '', mode: '', approval: '', routing: { routes: [{ workflow_version_id: 'workflow-only' }] } },
    ],
  })
  assert.equal(malformed.stageCount, 2)
  assert.ok(malformed.issues.some((issue: string) => issue.includes('缺少 key')))
  assert.ok(malformed.issues.some((issue: string) => issue.includes('缺少冻结的模型部署 ID')))
  assert.ok(malformed.issues.some((issue: string) => issue.includes('不是 delivery')))
  assert.deepEqual(malformed.stages[0].previousDeclaration, null)
  assert.equal(malformed.stages[0].nextDeclaration?.index, 1)
})

test('production recipe flow marks a ComfyUI route without a declared workflow as a projection issue', async (context) => {
  const vite = await createServer({ root: frontendRoot, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  context.after(async () => { await vite.close() })
  const service = await vite.ssrLoadModule('/src/services/productionRecipeFlow.ts')
  const flow = service.buildProductionRecipeFlow({
    schema_version: 1,
    stages: [
      { key: 'media_generation', mode: 'existing_pipeline', approval: 'auto', routing: { mode: 'fixed', variant_count: 1, routes: [{ model_deployment_id: 'deployment-video' }] } },
      { key: 'delivery', mode: 'existing_pipeline', approval: 'auto' },
    ],
  }, {
    deployments: [{ id: 'deployment-video', name: 'H3 视频部署', key: 'h3-video', profileVersion: 'model-video', providerDriver: 'comfyui', status: 'active' }],
  })

  assert.equal(flow.stages[0].routes[0].workflowVersionLabel, '缺少工作流版本')
  assert.ok(flow.issues.some((issue: string) => issue.includes('ComfyUI 路由缺少冻结的工作流版本 ID')))
  assert.ok(flow.stages[0].issues.some((issue: string) => issue.includes('ComfyUI 路由缺少冻结的工作流版本 ID')))
})

test('recipe workflow detail identifiers are deduplicated before lazy loading', async (context) => {
  const vite = await createServer({ root: frontendRoot, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  context.after(async () => { await vite.close() })
  const service = await vite.ssrLoadModule('/src/services/productionRecipeFlow.ts')
  assert.deepEqual(service.recipeWorkflowVersionIds({
    stages: [
      { routing: { routes: [{ workflow_version_id: 'workflow-1' }, { workflow_version_id: 'workflow-1' }] } },
      { routing: { routes: [{ workflow_version_id: 'workflow-2' }] } },
    ],
  }), ['workflow-1', 'workflow-2'])
})

test('settings recipe cards stay compact and audit details use data-first drilldown before raw JSON', () => {
  const settings = readFileSync(fileURLToPath(new URL('../src/views/SettingsView.vue', import.meta.url)), 'utf8')
  const component = readFileSync(fileURLToPath(new URL('../src/components/ProductionRecipeFlow.vue', import.meta.url)), 'utf8')
  const recipeSection = settings.indexOf('生产配方（Production Recipe）')
  const cardFlow = settings.indexOf('<ProductionRecipeFlow v-if="expandedRecipeIds.has(item.id)"', recipeSection)
  const cardActions = settings.indexOf('showRecipeDetail(item)', cardFlow)
  const modalFlow = settings.indexOf('<ProductionRecipeFlow v-if="detailRecipe"')
  const inputPanel = component.indexOf('输入合同')
  const executionPanel = component.indexOf('模型与执行绑定')
  const outputPanel = component.indexOf('输出与后续声明')
  const rawJson = component.indexOf('原始配方 JSON · 高级审计')

  assert.ok(recipeSection >= 0 && cardFlow > recipeSection && cardActions > cardFlow)
  assert.ok(modalFlow > cardActions)
  assert.ok(inputPanel >= 0 && executionPanel > inputPanel && outputPanel > executionPanel && rawJson > outputPanel)
  assert.match(component, /v-if="compact"/)
  assert.match(component, /aria-label="阶段声明顺序"/)
  assert.match(component, /role="tablist"/)
  assert.match(component, /下一声明阶段（非依赖）/)
  assert.match(component, /不能从前一声明阶段推断输入/)
  assert.doesNotMatch(component, /配方节点流程图|箭头仅表示|<ArrowRight/)
  assert.match(settings, /recipeWorkflowVersionIds/)
  assert.match(settings, /platformApi\.getWorkflowVersionForAudit\(id\)/)
  assert.match(settings, /:model-profiles="modelProfiles"/)
  assert.match(settings, /:workflows="recipeWorkflowCatalog"/)
  assert.match(settings, /:workflow-detail-states="recipeWorkflowDetailStates"/)
})
