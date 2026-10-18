import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('..', import.meta.url))

test('deployment smoke creation identifies preparation and blocks changing operation while pending', async () => {
  const source = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const form = await readFile(new URL('../src/components/ExecutionSmokeCreateForm.vue', import.meta.url), 'utf8')
  const resource = await readFile(new URL('../src/components/ResourceSmokeCreateForm.vue', import.meta.url), 'utf8')
  assert.ok(source.includes('ResourceSmokeCreateForm'))
  assert.ok(source.replace(/\s/g, '').includes("router.push({name:'workflows',query:{category:'smoke',kind:'preparation',id}})"))
  assert.ok(!source.includes('ExecutionPreparationWorkbench'), 'saved Smoke uses the single task detail entry')
  assert.ok(form.includes('正在创建…') && form.includes('重试确认同一请求'))
  assert.ok(form.includes('pending.value.key') && form.includes(':disabled="busy || !!pending"'))
  assert.ok(resource.includes('<fieldset v-if="options?.target" :disabled="busy || !!context">'))
})

const server = () => createServer({
  root: frontendRoot,
  logLevel: 'silent',
  appType: 'custom',
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
})

test('acceptance smoke adapter preserves paginated workflow and deployment evidence', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const payload = {
      count: 2,
      next: null,
      previous: null,
      results: [
        {
          subject: 'workflow',
          workflow: {
            id: 'workflow-1', template_id: 'template-1', template_name: 'H3 Ref2VA', template_slug: 'h3-ref2va', version: 6,
            status: 'validated', is_current: true, output_kind: 'video',
            model_profile_version_id: 'profile-1', provider_id: 'provider-1',
          },
          deployment: {
            id: 'deployment-1', key: 'h3-local', name: 'H3 Local', status: 'active',
            profile_version_id: 'profile-1', provider_id: 'provider-1', provider_driver: 'comfyui', runtime_host_id: 'host-1',
          },
          association: {
            level: 'validated', deployment_id: 'deployment-1', profile_matches: true,
            validated_deployment_matches: true, has_smoke_attempt: true,
          },
          validation: {
            stored_state: 'passed', state: 'drifted', fresh: false, reason_code: 'execution_target_drifted',
            message: 'deployment runtime config changed', evidence_sha256: 'a'.repeat(64),
            stored_target_sha256: 'b'.repeat(64), current_target_sha256: 'c'.repeat(64),
            validated_deployment_id: 'deployment-1',
          },
          smoke: {
            stored_state: 'failed', state: 'failed', fresh: false, reason_code: 'provider_smoke_failed',
            message: 'provider failed', evidence_sha256: '',
            evidence_deployment_id: 'deployment-1',
            latest_attempt: {
              id: 'attempt-1', deployment_id: 'deployment-1', status: 'failed',
              target_sha256: 'c'.repeat(64), graph_sha256: 'd'.repeat(64),
              created_at: '2026-09-01T00:00:00Z', started_at: null,
              finished_at: null, error_code: 'provider_smoke_failed',
              resolution_required: false,
            },
          },
          actions: [{
            kind: 'live_validate', method: 'POST', url: '/api/v1/workflows/workflow-1/validate/',
            ui_url: '/settings/workflows/workflow-1?focus=validate', enabled: true, reason_code: 'ready',
            requires_fixture: false, requires_confirmation: false, may_call_external_service: true,
          }],
        },
        {
          subject: 'deployment',
          deployment: {
            id: 'deployment-2', key: 'deepseek', name: 'DeepSeek', status: 'active',
            profile_version_id: 'profile-2', provider_id: 'provider-2', provider_driver: 'async_rest', runtime_host_id: null,
            current_target_sha256: 'e'.repeat(64),
            profile_contract_state: 'current', profile_contract_reason_code: 'current',
            target_state: 'current', target_reason_code: 'current',
          },
          health: {
            id: 'snapshot-1', probe_kind: 'health', status: 'passed', operations: ['script.generate'],
            target_sha256: 'e'.repeat(64), target_matches: true, fresh: true,
            captured_at: '2026-09-01T00:00:00Z', expires_at: '2026-09-01T01:00:00Z',
          },
          deployment_smoke: {
            applicable: true, reason_code: 'ready', operations: [{
              operation: 'script.generate', prompt_profiles: [], applicable: true, evidence_state: 'fresh', latest_snapshot: null,
              fresh_passed_snapshot: {
                id: 'snapshot-2', probe_kind: 'smoke', status: 'passed', operations: ['script.generate'],
                target_sha256: 'e'.repeat(64), target_matches: true, fresh: true,
                captured_at: '2026-09-01T00:00:00Z', expires_at: '2026-09-01T01:00:00Z',
              },
              latest_attempt: {
                id: 'deployment-attempt-1', operation: 'script.generate', status: 'succeeded',
                request_sha256: 'f'.repeat(64), target_sha256: 'e'.repeat(64), error_code: '',
                resolution_required: false, created_at: '2026-09-01T00:00:00Z',
                submission_started_at: '2026-09-01T00:00:10Z', finished_at: '2026-09-01T00:01:00Z',
              },
              action: {
                kind: 'deployment_smoke', method: 'POST', url: '/api/v1/deployments/deployment-2/smoke-test/',
                ui_url: '/settings?section=models&deployment=deployment-2&focus=smoke', enabled: true,
                reason_code: 'ready', requires_fixture: false, requires_confirmation: true,
                may_call_external_service: true,
              },
            }],
          },
          workflow_association_summary: {
            profile_match_count: 3, compatible_only_count: 1, validated_count: 2, smoke_history_count: 2, fresh_validation_count: 1,
            drifted_validation_count: 1, fresh_smoke_count: 1, unresolved_smoke_count: 0,
          },
          actions: [{
            kind: 'deployment_probe', method: 'POST', url: '/api/v1/deployments/deployment-2/probe/',
            ui_url: '/settings?section=models&deployment=deployment-2&focus=probe', enabled: true,
            reason_code: 'ready', requires_fixture: false, requires_confirmation: false,
            may_call_external_service: true,
          }],
        },
      ],
    }
    const page = adapters.adaptAcceptanceSmokePage(payload)

    assert.equal(page.count, 2)
    assert.equal(page.results[0].subject, 'workflow')
    const workflow = page.results[0]
    if (workflow.subject !== 'workflow') assert.fail('expected workflow row')
    assert.equal(workflow.workflow.templateName, 'H3 Ref2VA')
    assert.equal(workflow.deployment?.providerDriver, 'comfyui')
    assert.equal(workflow.association.validatedDeploymentMatches, true)
    assert.equal(workflow.association.deploymentId, 'deployment-1')
    assert.equal(workflow.validation.state, 'drifted')
    assert.equal(workflow.validation.storedTargetSha256, 'b'.repeat(64))
    assert.equal(workflow.smoke.latestAttempt?.errorCode, 'provider_smoke_failed')
    assert.equal(workflow.smoke.evidenceDeploymentId, 'deployment-1')
    assert.equal(workflow.smoke.latestAttempt?.startedAt, null)

    const deployment = page.results[1]
    if (deployment.subject !== 'deployment') assert.fail('expected deployment row')
    assert.equal(deployment.health?.status, 'passed')
    assert.equal(deployment.deployment.runtimeHostId, null)
    assert.equal(deployment.deployment.profileContractState, 'current')
    assert.equal(deployment.deployment.targetState, 'current')
    assert.equal(deployment.deploymentSmoke.operations[0]?.operation, 'script.generate')
    assert.equal(deployment.deploymentSmoke.operations[0]?.freshPassedSnapshot?.fresh, true)
    assert.equal(deployment.deploymentSmoke.operations[0]?.evidenceState, 'fresh')
    const missingEvidence = structuredClone(payload) as any
    delete missingEvidence.results[1].deployment_smoke.operations[0].evidence_state
    assert.throws(() => adapters.adaptAcceptanceSmokePage(missingEvidence), /evidence_state/)
    const missingHistory = structuredClone(payload) as any
    delete missingHistory.results[1].deployment_smoke.operations[0].latest_snapshot
    assert.throws(() => adapters.adaptAcceptanceSmokePage(missingHistory), /latest_snapshot/)
    assert.equal(deployment.deploymentSmoke.operations[0]?.action.uiUrl, '/settings?section=models&deployment=deployment-2&focus=smoke')
    assert.equal(deployment.workflowAssociationSummary.validatedCount, 2)
    assert.equal(deployment.workflowAssociationSummary.compatibleOnlyCount, 1)
    assert.equal(deployment.workflowAssociationSummary.smokeHistoryCount, 2)
    assert.equal(deployment.workflowAssociationSummary.driftedValidationCount, 1)
    assert.equal(deployment.actions[0]?.mayCallExternalService, true)
    assert.throws(() => adapters.adaptAcceptanceSmokeRow({ subject: 'unknown' }), /未知 subject/)
    assert.throws(() => adapters.adaptAcceptanceSmokePage({ count: 0, next: null, previous: null }), /results/)
    const incomplete = structuredClone(payload) as any
    delete incomplete.results[1].workflow_association_summary.compatible_only_count
    assert.throws(() => adapters.adaptAcceptanceSmokePage(incomplete), /compatible_only_count/)
    const missingTargetState = structuredClone(payload) as any
    delete missingTargetState.results[1].deployment.target_state
    assert.throws(() => adapters.adaptAcceptanceSmokePage(missingTargetState), /target_state/)
  } finally {
    await vite.close()
  }
})

test('workflow acceptance fails closed when current validation evidence is not fresh', async () => {
  const vite = await server()
  try {
    const acceptance = await vite.ssrLoadModule('/src/services/workflowAcceptance.ts')
    const version = { status: 'validated', validationState: 'passed', smokeTestState: 'passed' }
    assert.deepEqual(acceptance.workflowAcceptancePhase(version, 'fresh', true), {
      canLiveValidate: true, canConfigureSmoke: true, canStartSmoke: true, canPublish: true,
    })
    assert.deepEqual(acceptance.workflowAcceptancePhase(version, 'fresh', false), {
      canLiveValidate: true, canConfigureSmoke: true, canStartSmoke: true, canPublish: false,
    })
    for (const state of ['drifted', 'missing', 'unavailable']) {
      assert.deepEqual(acceptance.workflowAcceptancePhase(version, state), {
        canLiveValidate: true, canConfigureSmoke: false, canStartSmoke: false, canPublish: false,
      })
    }
    assert.equal(acceptance.workflowAcceptanceEvidenceState({ state: 'current', fresh: true, evidenceSha256: 'a' }), 'fresh')
    assert.equal(acceptance.workflowAcceptanceEvidenceState({ state: 'drifted', fresh: false, evidenceSha256: 'a' }), 'drifted')
    assert.equal(acceptance.workflowAcceptanceEvidenceState({ state: 'not_run', fresh: false, evidenceSha256: '' }), 'missing')
    assert.equal(acceptance.workflowAcceptanceEvidenceState(null), 'unavailable')
    assert.equal(acceptance.workflowAssociationLabel({
      level: 'executed', hasSmokeAttempt: true, validatedDeploymentMatches: true, profileMatches: true,
    }), '已有精确 Workflow Smoke 执行历史')
    assert.equal(acceptance.deploymentHealthDisplayStatus({
      health: { status: 'passed', fresh: false, targetMatches: false },
    }), 'drifted')
    assert.equal(acceptance.deploymentHealthDisplayStatus({
      health: { status: 'passed', fresh: false, targetMatches: true },
    }), 'stale')
    const freshOperation = {
      operation: 'script.generate', applicable: true,
      freshPassedSnapshot: { fresh: true }, latestAttempt: null,
    }
    const missingOperation = {
      operation: 'script.structure', applicable: true,
      freshPassedSnapshot: null, latestAttempt: null,
    }
    assert.equal(acceptance.workflowAcceptanceNeedsAttention({
      validation: { state: 'current', fresh: true, evidenceSha256: 'a' },
      smoke: { fresh: true, latestAttempt: { resolutionRequired: true } },
    }), true)
    assert.equal(acceptance.deploymentAcceptanceNeedsAttention({
      deployment: { status: 'active' },
      health: { fresh: true },
      deploymentSmoke: { applicable: true, operations: [freshOperation, missingOperation] },
      workflowAssociationSummary: { driftedValidationCount: 0, unresolvedSmokeCount: 0 },
    }), true)
  } finally {
    await vite.close()
  }
})

test('acceptance center attention summary names every counted state family', async () => {
  const source = await readFile(new URL('../src/views/AcceptanceSmokeCenterView.vue', import.meta.url), 'utf8')
  assert.match(source, /未完成、失效、失败或待消歧/)
  assert.doesNotMatch(source, /<small>漂移、失败或待消歧<\/small>/)
  assert.ok(
    source.indexOf("if (row.smoke.latestAttempt?.resolutionRequired) return 'smoke'")
      < source.indexOf("if (!row.association.validatedDeploymentMatches) return 'validate'"),
    '待消歧 attempt 必须优先跳到 Smoke 消歧区，而不是 Live Validate',
  )
})

test('acceptance smoke is a shared first-level settings destination without duplicate section buttons', async () => {
  const vite = await server()
  try {
    const { settingsPrimaryNavigation, settingsSectionCatalog } = await vite.ssrLoadModule('/src/content/settingsSectionCatalog.ts')
    assert.deepEqual(settingsPrimaryNavigation.map((item: { id: string }) => item.id), [
      'models', 'workflows', 'acceptance-smoke', 'fields', 'prompts', 'routing', 'defaults', 'security', 'runtime', 'governance',
    ])
    assert.equal(settingsSectionCatalog.some((item: { id: string }) => item.id === 'acceptance-smoke'), false)
  } finally {
    await vite.close()
  }

  const settings = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const center = await readFile(new URL('../src/views/AcceptanceSmokeCenterView.vue', import.meta.url), 'utf8')
  const navigation = await readFile(new URL('../src/components/SettingsPrimaryNav.vue', import.meta.url), 'utf8')
  const router = await readFile(new URL('../src/router/index.ts', import.meta.url), 'utf8')

  assert.ok(settings.includes('<SettingsPrimaryNav/>'))
  assert.ok(center.includes('<SettingsPrimaryNav/>'))
  assert.equal(settings.includes('验收与烟测中心'), false, '模型/工作流标题栏不应保留重复通用入口')
  assert.match(settings, /:to="workflowSmokeUrlForDeployment\(item\)"[^>]*><ShieldCheck[^>]*\/>工作流验收<\/RouterLink>/, '简短验收按钮仍指向精确实例的 Workflow Smoke 深链')
  assert.ok(settings.includes('查看该部署验收'), '可直接 Smoke 的实例仍需保留精确验收深链')
  assert.ok(settings.includes("['probe','smoke'].includes(String(route.query.focus||''))"), '实例 Probe/Smoke 深链都应高亮精确目标')
  assert.ok(settings.includes('() => route.query.focus'), '同一实例切换 Probe/Smoke 焦点时应重新聚焦')
  assert.equal(center.includes('返回系统设置'), false, '一级导航已提供返回路径，不应重复返回按钮')
  assert.ok(navigation.includes('settingsPrimaryNavigation'))
  assert.ok(navigation.includes(':aria-current="isActive(item) ? \'page\' : undefined"'))
  assert.ok(navigation.includes(':to="item.settingsRoute"'))
  assert.ok(router.includes("name: 'acceptance-smoke-center'"))
  assert.ok(router.includes('requiresStaff: true'))
})
