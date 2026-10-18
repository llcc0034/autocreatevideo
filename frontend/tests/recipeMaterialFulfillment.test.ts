import test from 'node:test'
import assert from 'node:assert/strict'
import { adaptMaterialFulfillmentContext, adaptMaterialFulfillmentReceipt, materialFulfillmentPayload } from '../src/services/recipeMaterialFulfillment.ts'

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const context = () => ({ source_mode: 'generate', stage_run_id: id(1), unit_key: `appearance:${id(2)}`, requirement_key: `setting:${id(3)}`,
  run_plan_sha256: 'a'.repeat(64), appearance_id: id(2), setting_revision_id: id(3), appearance_role: 'primary',
  reference_name: '林晓 · 主图', primary_appearance_id: null, can_select: true, reason: '', selection: null })
const receipt = () => ({ id: id(4), reference_version_id: id(5), series_id: id(6), asset_id: id(7), source: 'existing',
  target_sha256: 'b'.repeat(64), selection_mode: 'manual', selected_by_id: 6, created_at: '2026-10-02T05:00:00Z' })
const adapt = (value: unknown = context()) => adaptMaterialFulfillmentContext(value, id(1), `appearance:${id(2)}`)

test('payload keeps exact observed requirement, explicit source and no execution or actor fields', () => {
  assert.deepEqual(materialFulfillmentPayload(adapt(), { seriesId: id(6), assetId: id(7), source: 'upload' }), {
    stage_run_id: id(1), unit_key: `appearance:${id(2)}`, requirement_key: `setting:${id(3)}`,
    expected_run_plan_sha256: 'a'.repeat(64), series_id: id(6), asset_id: id(7), source: 'upload',
  })
})

test('declared upload cannot silently become existing or generation', () => {
  const current = adapt({...context(), source_mode: 'upload'})
  assert.equal(current.sourceMode, 'upload')
  assert.throws(()=>materialFulfillmentPayload(current,{seriesId:id(6),assetId:id(7),source:'existing'}))
  for (const source_mode of [undefined,'automatic',null]) assert.throws(()=>adapt({...context(),source_mode}))
})

test('exact-version payload sends only the original version ID and rejects mixed client provenance', () => {
  assert.deepEqual(materialFulfillmentPayload(adapt(), { referenceVersionId: id(5) }), {
    stage_run_id: id(1), unit_key: `appearance:${id(2)}`, requirement_key: `setting:${id(3)}`,
    expected_run_plan_sha256: 'a'.repeat(64), reference_version_id: id(5),
  })
  for (const choice of [
    { referenceVersionId: id(5), source: 'generated' },
    { referenceVersionId: id(5), seriesId: id(6), assetId: id(7), source: 'existing' },
    { referenceVersionId: 'latest' },
  ]) assert.throws(() => materialFulfillmentPayload(adapt(), choice as { referenceVersionId: string }))
})
test('variant pins original primary appearance without assigning a primary image', () => {
  const result = adapt({ ...context(), appearance_role: 'variant', primary_appearance_id: id(9) })
  assert.equal(result.primaryAppearanceId, id(9))
  assert.equal(result.selection, null)
  assert.equal(result.referenceName, '林晓 · 主图')
})
test('saved selection is a receipt, not a candidate or artificial human approval', () => {
  const result = adapt({ ...context(), can_select: false, reason: '已明确选择', selection: receipt() })
  assert.equal(result.selection?.selectedById, 6)
  assert.equal(result.selection?.referenceVersionId, id(5))
  assert.throws(() => materialFulfillmentPayload(result, { seriesId: id(6), assetId: id(7), source: 'existing' }))
})
test('unknown provenance and malformed committed receipts are rejected', () => {
  for (const change of [{ source: 'guessed' }, { selected_by_id: '6' }, { selected_by_id: 0 },
      { selected_by_id: 1.5 }, { target_sha256: 'missing' }, { reference_version_id: undefined }, { created_at: 'invalid' }]) {
    assert.throws(() => adaptMaterialFulfillmentReceipt({ ...receipt(), ...change }))
  }
})

test('reusing a generated version preserves origin without allowing a client to invent generation', () => {
  const saved = {...receipt(), source: 'generated', selection_mode: 'automatic', selected_by_id: null}
  assert.equal(adaptMaterialFulfillmentReceipt(saved).source, 'generated')
  const result = adapt({...context(), source_mode: 'existing', can_select: false, reason: '已有原版本选择', selection: saved})
  assert.equal(result.selection?.source, 'generated')
  assert.equal(result.selection?.selectedById, null)
  assert.throws(() => materialFulfillmentPayload(adapt(), {seriesId: id(6), assetId: id(7), source: 'generated' as 'existing'}))
})

test('automatic receipt keeps null actor; absent or contradictory identity never becomes manual', () => {
  const value = { ...receipt(), selection_mode: 'automatic', selected_by_id: null }
  const result = adaptMaterialFulfillmentReceipt(value)
  assert.equal(result.selectionMode, 'automatic')
  assert.equal(result.selectedById, null)
  for (const change of [{ selection_mode: undefined }, { selection_mode: 'guessed' },
      { selected_by_id: 6 }, { selection_mode: 'manual' }, { selected_by_id: undefined }]) {
    assert.throws(() => adaptMaterialFulfillmentReceipt({ ...value, ...change }))
  }
  assert.equal(adaptMaterialFulfillmentReceipt(receipt()).selectionMode, 'manual')
})
test('different Stage, unit, setting or absent baseline cannot be treated as current context', () => {
  for (const change of [{ stage_run_id: id(9) }, { unit_key: `appearance:${id(9)}` }, { requirement_key: `setting:${id(9)}` },
      { setting_revision_id: id(9) }, { run_plan_sha256: undefined }, { selection: undefined }, { appearance_role: 'guessed' }]) {
    assert.throws(() => adapt({ ...context(), ...change }))
  }
})
test('contradictory readiness and primary dependencies fail closed', () => {
  for (const change of [{ can_select: false }, { can_select: 'true' }, { reason: '已有准备' },
      { selection: receipt() }, { primary_appearance_id: id(9) }, { appearance_role: 'variant' },
      { appearance_role: 'variant', primary_appearance_id: id(2) }]) {
    assert.throws(() => adapt({ ...context(), ...change }))
  }
})
test('existing generation or dependent preparation explains refusal without constructing a submission', () => {
  const result = adapt({ ...context(), can_select: false, reason: '已有变体准备冻结主图' })
  assert.equal(result.reason, '已有变体准备冻结主图')
  assert.throws(() => materialFulfillmentPayload(result, { seriesId: id(6), assetId: id(7), source: 'upload' }))
  assert.throws(() => materialFulfillmentPayload(adapt(), { seriesId: id(6), assetId: 'temporary-file', source: 'upload' }))
})

test('zero-call blocker takeover uses server permission and retains manual receipt semantics', () => {
  const allowed = adapt({...context(), can_select: true, reason: ''})
  assert.equal(materialFulfillmentPayload(allowed, {referenceVersionId: id(5)}).reference_version_id, id(5))
  const claimed = adapt({...context(), can_select: false, reason: '原准备已被工作进程占用，不能接管'})
  assert.throws(() => materialFulfillmentPayload(claimed, {referenceVersionId: id(5)}))
  const stopped = adapt({...context(), can_select: false, reason: '已有明确素材选择', selection: receipt()})
  assert.equal(stopped.selection?.selectionMode, 'manual')
  assert.equal(stopped.selection?.source, 'existing')
})
