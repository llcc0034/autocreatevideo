import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
const server = () => createServer({
  root: frontendRoot,
  logLevel: 'silent',
  appType: 'custom',
  server: { middlewareMode: true },
})

const impactResponse = () => ({
  schema_version: 1,
  requested_action: 'delete',
  target: {
    resource_type: 'model_execution_instance_version',
    object_id: '11111111-1111-4111-8111-111111111111',
    label: 'Local SDXL v2',
    status: 'disabled',
    state_sha256: 'c'.repeat(64),
  },
  target_policy: { allowed: true, reason_code: '', reason: '' },
  complete: true,
  can_execute: false,
  dependencies: [{
    dependency_key: 'a'.repeat(32),
    resource_type: 'production_recipe_version',
    object_id: '22222222-2222-4222-8222-222222222222',
    label: 'Production recipe v3',
    status: 'published',
    relationship: 'recipe_definition_reference',
    relationship_label: '生产配方定义引用',
    disposition: 'blocking_reference',
    reason_code: 'frozen_recipe_reference',
    reason: '请发布替代版本并退役当前路由。',
    reference_paths: ['$.stages[0].routing.routes[0].execution_instance_version_id'],
    state_sha256: 'd'.repeat(64),
  }],
  summary: {
    selectable_count: 0,
    automatic_count: 0,
    blocking_count: 1,
    preserved_history_count: 0,
  },
  confirmation_phrase: '删除 Local SDXL v2',
  advisories: ['已发布版本永不级联删除。'],
  impact_sha256: 'b'.repeat(64),
})

test('registry deletion adapter preserves dependency groups and JSON paths', async () => {
  const vite = await server()
  try {
    const deletion = await vite.ssrLoadModule('/src/services/registryDeletion.ts')
    const impact = deletion.adaptRegistryDeletionImpact(impactResponse())
    assert.equal(impact.target.resourceType, 'model_execution_instance_version')
    assert.equal(impact.summary.blockingCount, 1)
    assert.equal(impact.dependencies[0].disposition, 'blocking_reference')
    assert.deepEqual(
      impact.dependencies[0].referencePaths,
      ['$.stages[0].routing.routes[0].execution_instance_version_id'],
    )
    assert.equal(impact.target.stateSha256, 'c'.repeat(64))
  } finally { await vite.close() }
})

test('registry deletion adapter fails closed on unknown server policy', async () => {
  const vite = await server()
  try {
    const deletion = await vite.ssrLoadModule('/src/services/registryDeletion.ts')
    const response = impactResponse()
    response.dependencies[0].disposition = 'force_delete'
    assert.throws(
      () => deletion.adaptRegistryDeletionImpact(response),
      /未知依赖策略 force_delete.*失败关闭/,
    )
  } finally { await vite.close() }
})

test('registry deletion accepts both current instance resources and rejects retired Binding', async () => {
  const vite = await server()
  try {
    const { adaptRegistryDeletionImpact } = await vite.ssrLoadModule('/src/services/registryDeletion.ts')
    for (const kind of ['model_execution_instance', 'model_execution_instance_version']) {
      const response = impactResponse()
      response.target.resource_type = kind
      assert.equal(adaptRegistryDeletionImpact(response).target.resourceType, kind)
    }
    const retired = impactResponse()
    retired.target.resource_type = 'prompt_profile_binding'
    assert.throws(() => adaptRegistryDeletionImpact(retired), /未知删除资源类型/)
  } finally { await vite.close() }
})

test('registry deletion API submits the reviewed hash, selections and audit reason', async (context) => {
  const vite = await server()
  context.after(async () => { await vite.close() })
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const originalPost = api.post
  context.after(() => { api.post = originalPost })

  api.post = async (path: string, payload: unknown) => {
    assert.equal(path, '/registry-deletion-preview/')
    assert.deepEqual(payload, {
      resource_type: 'model_execution_instance_version',
      object_id: '11111111-1111-4111-8111-111111111111',
    })
    return { data: impactResponse() }
  }
  const impact = await platformApi.previewRegistryDeletion(
    'model_execution_instance_version',
    '11111111-1111-4111-8111-111111111111',
  )
  assert.equal(impact.impactSha256, 'b'.repeat(64))

  let executeCalled = false
  api.post = async (path: string, payload: unknown, options: unknown) => {
    executeCalled = true
    assert.equal(path, '/registry-deletion-execute/')
    assert.deepEqual(payload, {
        resource_type: 'model_execution_instance_version',
        object_id: '11111111-1111-4111-8111-111111111111',
        expected_impact_sha256: 'b'.repeat(64),
        selected_dependency_keys: ['a'.repeat(32)],
        confirmation_phrase: '删除 Local SDXL v2',
    })
    assert.deepEqual(options, {
      headers: { 'X-Audit-Reason-Code': 'planned_change' },
    })
    return { data: { deleted: true, target: impactResponse().target } }
  }
  await platformApi.executeRegistryDeletion(
    'model_execution_instance_version',
    '11111111-1111-4111-8111-111111111111',
    {
      expectedImpactSha256: 'b'.repeat(64),
      selectedDependencyKeys: ['a'.repeat(32)],
      confirmationPhrase: '删除 Local SDXL v2',
      reasonCode: 'planned_change',
    },
  )
  assert.equal(executeCalled, true)
  for (const invalid of [null, {}, { deleted: false, target: impactResponse().target },
    { deleted: true, target: { ...impactResponse().target, object_id: 'other' } },
    { deleted: true, target: { ...impactResponse().target, resource_type: 'model_deployment' } }]) {
    api.post = async () => ({ data: invalid })
    await assert.rejects(() => platformApi.executeRegistryDeletion('model_execution_instance_version', impactResponse().target.object_id,
      { expectedImpactSha256: 'b'.repeat(64), selectedDependencyKeys: [], confirmationPhrase: 'confirm', reasonCode: 'planned_change' }), /尚未确认/)
  }
})
