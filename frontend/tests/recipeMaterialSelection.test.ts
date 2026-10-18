import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { adaptRecipeMaterialSelectionContext, materialDependencyActionLabel, materialSelectionPayload } from '../src/services/recipeMaterialSelection.ts'

const digest = 'a'.repeat(64)
const dependency = (action = 'reprepare') => ({ id: 'prep', unit_key: 'appearance:two', reference_name: '雨衣',
  status: 'awaiting_review', revision: 3, action, reason: '按明确影响处理，不改旧快照' })

const context = () => ({ artifact_id: 'candidate', stage_run_id: 'stage', unit_key: 'appearance:one',
  appearance_role: 'primary', reference_name: '旅人主图', candidate_number: 2,
  expected_decision_id: null, selected_artifact_id: null, can_select: true, reason: '', dependent_preparations: [], dependencies_sha256: digest })

test('explicit empty baseline stays null, not inferred from candidate status', () => {
  const actual = adaptRecipeMaterialSelectionContext(context(), 'candidate')
  assert.deepEqual(materialSelectionPayload(actual, 'candidate'), { expected_decision_id: null, expected_dependencies_sha256: digest })
  assert.equal(actual.referenceName, '旅人主图')
})
test('existing decision identity is submitted unchanged', () => {
  const actual = adaptRecipeMaterialSelectionContext({ ...context(), expected_decision_id: 'observed', selected_artifact_id: 'old' }, 'candidate')
  assert.deepEqual(materialSelectionPayload(actual, 'candidate'), { expected_decision_id: 'observed', expected_dependencies_sha256: digest })
})
test('explicit append can reconfirm the current image using server permission, not selected status', () => {
  const actual = adaptRecipeMaterialSelectionContext({ ...context(), expected_decision_id: 'observed', selected_artifact_id: 'candidate' }, 'candidate')
  assert.equal(materialSelectionPayload(actual, 'candidate').expected_decision_id, 'observed')
  const denied = adaptRecipeMaterialSelectionContext({ ...context(), expected_decision_id: 'observed', selected_artifact_id: 'candidate',
    can_select: false, reason: '无需重复提交' }, 'candidate')
  assert.throws(() => materialSelectionPayload(denied, 'candidate'))
  const component = readFileSync(new URL('../src/components/GeneratedArtifactCompare.vue', import.meta.url), 'utf8')
  assert.match(component, /artifact.status!=='selected'\|\|materialSelection/)
  assert.match(component, /确认保留当前图/)
})
test('dependency impact disables choice and preserves preparation status', () => {
  const actual = adaptRecipeMaterialSelectionContext({ ...context(), can_select: false, reason: '需要重新准备',
    dependent_preparations: [dependency('blocked')] }, 'candidate')
  assert.equal(actual.dependentPreparations[0]?.status, 'awaiting_review')
  assert.throws(() => materialSelectionPayload(actual, 'candidate'))
})
test('wrong candidate, missing baseline, unknown state and contradictory permissions fail closed', () => {
  for (const change of [null, { ...context(), artifact_id: 'other' }, { ...context(), expected_decision_id: undefined },
    { ...context(), can_select: 'true' }, { ...context(), can_select: false }, { ...context(), appearance_role: 'guessed' },
    { ...context(), candidate_number: 0 }, { ...context(), dependent_preparations: [{}] },
    { ...context(), dependencies_sha256: undefined }, { ...context(), dependencies_sha256: 'guessed' },
    { ...context(), dependent_preparations: [dependency('unknown')] },
    { ...context(), dependent_preparations: [dependency('blocked')] },
    { ...context(), dependent_preparations: [dependency('retain')] },
    { ...context(), dependent_preparations: [{ ...dependency(), revision: 0 }] },
    { ...context(), dependent_preparations: [dependency(), dependency()] },
    { ...context(), appearance_role: 'variant', dependent_preparations: [dependency()] },
    { ...context(), dependent_preparations: [{ id: 'p', unit_key: 'u', reference_name: 'n', status: 'succeeded' }] },
    { ...context(), dependent_preparations: [{ id: 'p', unit_key: 'u', reference_name: 'n', status: 'ready' }] }]) {
    assert.throws(() => adaptRecipeMaterialSelectionContext(change, 'candidate'))
  }
})

test('confirmed mixed impact carries server digest and distinguishes retained history from new preparation', () => {
  const actual = adaptRecipeMaterialSelectionContext({ ...context(), dependent_preparations: [dependency(),
    { ...dependency('retain'), id: 'completed', status: 'dispatched' }] }, 'candidate')
  assert.equal(actual.canSelect, true)
  assert.deepEqual(actual.dependentPreparations.map(item => materialDependencyActionLabel(item.action)), ['按新主图重新准备', '保留原主图依据'])
  assert.equal(materialSelectionPayload(actual, 'candidate').expected_dependencies_sha256, digest)
})
test('variant selection uses its own exact context and cannot be transferred to another candidate', () => {
  const actual = adaptRecipeMaterialSelectionContext({ ...context(), appearance_role: 'variant' }, 'candidate')
  assert.equal(actual.appearanceRole, 'variant')
  assert.throws(() => materialSelectionPayload(actual, 'other'))
})
test('original comparison has separate inspect/confirm and invalidates stale or failed context', () => {
  const component = readFileSync(new URL('../src/components/GeneratedArtifactCompare.vue', import.meta.url), 'utf8')
  assert.match(component, /if \(props.materialSelection && !confirmed\) return inspectMaterial\(artifact\)/)
  assert.match(component, /if \(selectionState.error && !selectionState.pending\) materialContext.value = null/)
  assert.match(component, /核对原选择请求/)
  assert.match(component, /token === sequence/)
  assert.match(component, /onBeforeUnmount/)
  assert.match(component, /apiErrorSummary/)
  assert.match(component, /主调用尚未开始/)
  assert.match(component, /确定未提交的辅助任务/)
  assert.match(component, /已结束的辅助结果保留，不移植到新准备/)
  const view = readFileSync(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8')
  assert.match(view, /planSource !== 'recipe'/)
  assert.match(view, /:material-selection="group.materialSelection"/)
})
