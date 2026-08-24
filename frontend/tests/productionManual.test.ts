import assert from 'node:assert/strict'
import test from 'node:test'
import { productionManual, searchableIncidentText, searchableManualText } from '../src/content/productionManual.ts'

const requiredStepIds = [
  'create-project',
  'prepare-content',
  'configure-providers',
  'configure-prompt-profiles',
  'validate-workflows',
  'activate-configuration',
  'probe-host',
  'create-production-run',
  'review-script-bible',
  'handle-run-gates',
  'monitor-production-run',
  'inspect-diagnostics',
  'verify-delivery',
]

test('manual has one ordered, searchable ProductionRun path', () => {
  assert.match(productionManual.version, /^\d{4}\.\d{2}\.\d{2}\.\d+$/)
  assert.deepEqual(productionManual.steps.map((step) => step.id), requiredStepIds)
  assert.deepEqual(productionManual.steps.map((step) => step.number), requiredStepIds.map((_, index) => index + 1))
  assert.equal(new Set(productionManual.steps.map((step) => step.id)).size, requiredStepIds.length)

  for (const step of productionManual.steps) {
    assert.ok(step.location.trim(), step.id + ' needs a location')
    assert.ok(step.inputs.length, step.id + ' needs an input example')
    assert.ok(step.inputs.every((item) => item.label && item.example && item.note), step.id + ' has an incomplete input')
    assert.ok(step.actions.length, step.id + ' needs actions')
    assert.ok(step.success.length, step.id + ' needs success evidence')
    assert.ok(step.next.trim(), step.id + ' needs a next step')
    assert.ok(searchableManualText(step).includes(step.title.toLocaleLowerCase('zh-CN')))
  }
})

test('manual makes ProductionRun the only new production entry', () => {
  const creation = productionManual.steps.find((step) => step.id === 'create-production-run')
  const diagnostics = productionManual.steps.find((step) => step.id === 'inspect-diagnostics')
  assert.ok(creation)
  assert.ok(diagnostics)
  assert.match(searchableManualText(creation), /productionrun/)
  assert.match(searchableManualText(creation), /所有新生产/)
  assert.match(searchableManualText(diagnostics), /只读诊断/)
  assert.match(searchableManualText(diagnostics), /deferred history/)
  assert.doesNotMatch(searchableManualText(diagnostics), /task retry\/cancel|deferred 例外/)
})

test('manual documents implemented AI, workflow, async, config and delivery boundaries', () => {
  const text = productionManual.steps.map(searchableManualText).join(' ')
  for (const fragment of [
    'ai script',
    '确定性 bible',
    'semantic workflow',
    'promptprofile',
    'download_import',
    '逐镜头',
    't2i',
    't2v',
    'managed artifact',
    '五层配置',
    'boot activation',
    'runtimehost',
    'episode master',
    'releasebundle',
  ]) assert.ok(text.includes(fragment), fragment)

  assert.ok(text.includes('provider_upload_contract_missing'))
  assert.ok(text.includes('没有跨节点或多 gpu scheduler'))
  assert.ok(text.includes('不会自动下载模型'))
  assert.ok(text.includes('needs_review'))
  assert.ok(text.includes('没有真实外部 provider/gpu 完整 e2e 证据'))
  assert.ok(text.includes('不生成整季单条长视频'))
  assert.ok(text.includes('http 202'))
  assert.ok(text.includes('script.generate、script.structure 与 prompt.normalize'))
  assert.ok(text.includes('禁止重复点击重放付费请求'))
})

test('incident matrix covers every operator-visible blocked or recovery state', () => {
  const ids = new Set(productionManual.incidents.map((incident) => incident.id))
  for (const id of [
    'migration-pending',
    'workflow-needs-review',
    'deployment-unavailable',
    'upload-contract-missing',
    'provider-authorization',
    'cost-blocked',
    'script-review-drift',
    'bible-review-conflict',
    'remote-uncertain',
    'artifact-archive-failed',
    'episode-master-incomplete',
    'release-bundle-invalid',
    'config-pending-activation',
    'recommendation-not-applied',
    'no-real-e2e-evidence',
  ]) assert.ok(ids.has(id), id)

  assert.equal(ids.size, productionManual.incidents.length)
  for (const incident of productionManual.incidents) {
    assert.ok(incident.symptom.trim())
    assert.ok(incident.likelyCause.trim())
    assert.ok(incident.actions.length)
    assert.ok(incident.confirmation.trim())
    assert.ok(searchableIncidentText(incident).includes(incident.symptom.toLocaleLowerCase('zh-CN')))
  }
})

test('manual keeps credentials opaque and does not revive retired production routes', () => {
  const text = JSON.stringify(productionManual).toLocaleLowerCase('zh-CN')
  assert.ok(text.includes('credential_id'))
  assert.ok(!text.includes('credential_ref'))
  assert.ok(text.includes('不回显'))
  for (const fragment of [
    'build-production',
    '/prompts/generate',
    '/workflows/generate',
    '生成全部提示词',
    '保存并重新生成',
    '重新生成本镜头',
    '整季单条长视频作为成功',
    '/capabilities/creative-drafts/',
    '/series-concept-drafts/generate/',
  ]) assert.equal(text.includes(fragment), false, fragment)
})

test('manual search includes boundary error codes and recovery actions', () => {
  const upload = productionManual.incidents.find((incident) => incident.id === 'upload-contract-missing')
  const uncertain = productionManual.incidents.find((incident) => incident.id === 'remote-uncertain')
  assert.ok(upload && searchableIncidentText(upload).includes('provider_upload_contract_missing'))
  assert.ok(uncertain && searchableIncidentText(uncertain).includes('resolve-uncertain'))
})
