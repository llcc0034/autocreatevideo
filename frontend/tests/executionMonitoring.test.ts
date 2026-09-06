import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
const dependencies = {
  providers: [{ isActive: true }],
  modelProfiles: [],
  deployments: [],
  runtimeHosts: [],
  workflowVersions: [],
  executionMonitoringLimits: { maxSeconds: 10000, maxOverrides: 20 },
}

test('model editor exposes and validates the generic execution monitoring contract', async (context) => {
  const vite = await createServer({
    root: frontendRoot,
    logLevel: 'silent',
    appType: 'custom',
    server: { middlewareMode: true },
  })
  context.after(async () => { await vite.close() })
  const schema = await vite.ssrLoadModule('/src/services/registryResourceSchema.ts')
  const payload = schema.applyResourceDefaults('model', {
    modality: 'video',
    specification: { capabilities: ['video.generate'] },
  }, dependencies)
  const paths = schema.resourceEditorFields('model', payload, dependencies)
    .map((field: { path: string }) => field.path)

  assert.equal(payload.specification.execution_monitoring.schema_version, 1)
  assert.ok(paths.includes('specification.execution_monitoring.default_idle_timeout_seconds'))
  assert.ok(paths.includes('specification.execution_monitoring.node_type_overrides'))

  const invalid = schema.validateRegistryResource('model', {
    ...payload,
    specification: {
      ...payload.specification,
      execution_monitoring: {
        schema_version: 1,
        node_type_overrides: { '\u0001bad node type': 20 },
        node_overrides: { '10': 5400 },
      },
    },
  }, dependencies)
  assert.ok(invalid.errors.some((message: string) => message.includes('未知字段')))
  assert.ok(invalid.errors.some((message: string) => message.includes('包含控制字符')))
  assert.equal(invalid.errors.some((message: string) => message.includes('30–86400')), false)

  const extensionName = schema.validateRegistryResource('model', {
    ...payload,
    specification: {
      ...payload.specification,
      execution_monitoring: {
        schema_version: 1,
        node_type_overrides: { 'KSampler (Efficient)': 3600 },
      },
    },
  }, dependencies)
  assert.ok(!extensionName.errors.some((message: string) => message.includes('节点类型')))

  const textWithHiddenMonitoring = schema.validateRegistryResource('model', {
    ...payload,
    modality: 'text',
  }, dependencies)
  assert.ok(textWithHiddenMonitoring.errors.some((message: string) => message.includes('仅适用于 image、video 或 multimodal')))
})

test('workflow version review does not expose monitoring or node-contract mutation', () => {
  const source = readFileSync(
    fileURLToPath(new URL('../src/views/WorkflowVersionEditorView.vue', import.meta.url)),
    'utf8',
  )
  assert.match(source, /冻结合同概览/)
  assert.match(source, /不能在平台侧人工填写技术字段或覆盖版本/)
  for (const retired of ['execution_monitoring.node_overrides', 'updateNodeIdleTimeout', 'inputContractText', 'WorkflowNodeDrilldown']) {
    assert.equal(source.includes(retired), false, retired)
  }
})

test('novice settings guide explains the future-version migration and telemetry boundary', () => {
  const guide = JSON.parse(readFileSync(
    fileURLToPath(new URL('../src/content/systemSettingsGuide.json', import.meta.url)),
    'utf8',
  ))
  const content = JSON.stringify(guide)
  assert.match(content, /execution_monitoring/)
  assert.match(content, /SamplerCustomAdvanced=5400 秒/)
  assert.match(content, /缺少 GeneratedWorkflow\/GenerationTask 双层合同会直接 fail closed/)
  assert.match(content, /开发环境统一清理旧任务\/产物/)
  assert.match(content, /WebSocket 不报告节点切换/)
  assert.match(content, /hard deadline 始终不续期/)
})
