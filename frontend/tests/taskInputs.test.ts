import assert from 'node:assert/strict'
import test from 'node:test'
import { adaptTaskInputs } from '../src/services/taskInputs.ts'

const media = (item_key: string) => ({ item_key, input_key: 'references', asset_id: 'same-asset', artifact_id: null,
  reference_binding:{position:0,original_name:'同一素材',reference_name:null,name_disambiguated:null,input_slot:null,shot_reference:null},
  display_name: '同一素材', kind: 'image', preview_url: '', purpose: 'appearance', paired_to: null,
  source_sha256: `facts-${item_key}`, checksum: 'bytes', input_label: '<Picture 1>',
  ports: [{ target_key: 'images', port: { kind: 'semantic', semantic_key: 'asset.source' } }], staging_status: 'not_staged' })
const response = () => ({ input_contract_status: 'available', input_contract_code: '', input_parameters: {
  preparation_id: 'prep', attempt_id: 'attempt', execution_instance_version_id: 'version', workflow_version_id: 'workflow',
  request_sha256: 'request', operation: 'image.edit', output_kind: 'image', prompt_mode: 'manual', prompt_profile_version_id: null,
  parameters: [{ key: 'sampling.seed', value: 0, value_source: 'request', rule: { type: 'integer', minimum: 0 } },
    { key: 'audio.enabled', value: false, value_source: 'instance', rule: { type: 'boolean' } }],
  prompt_channels: [{ key: 'positive_prompt', semantic_key: 'prompt.positive', character_count: 12 }],
  media_uses: [media('first'), { ...media('second'), input_label: '<Picture 2>', staging_status: 'staged' }]
} })

test('exact scalar values and distinct uses survive repeated Asset IDs', () => {
  const result = adaptTaskInputs(response())
  assert.equal(result.inputParameters?.preparation_id, 'prep')
  assert.deepEqual(result.referenceAssets.map(row => row.itemKey), ['first', 'second'])
  assert.deepEqual(result.referenceAssets.map(row => row.stagingStatus), ['not_staged', 'staged'])
  assert.deepEqual(result.inputParameters?.parameters, response().input_parameters.parameters)
  assert.equal(result.referenceAssets[0]?.assetId, result.referenceAssets[1]?.assetId)
})

test('context media and precise selected artifacts are not converted into graph or Asset inputs', () => {
  const raw = response()
  const context = { ...media('context'), asset_id: null, artifact_id: 'selected-artifact', ports: [], staging_status: 'not_applicable', input_label: null }
  raw.input_parameters.media_uses.push(context as never)
  const result = adaptTaskInputs(raw)
  assert.equal(result.referenceAssets.length, 2)
  assert.equal(result.contextMediaUses.length, 1)
  assert.equal(result.contextMediaUses[0]?.assetId, undefined)
  assert.equal(result.contextMediaUses[0]?.artifactId, 'selected-artifact')
  assert.equal(result.contextMediaUses[0]?.inputLabel, undefined)
})

test('missing fields and duplicate use keys fail rather than falling back to old summary fields', () => {
  for (const key of ['preparation_id', 'request_sha256', 'parameters', 'media_uses', 'prompt_channels']) {
    const raw = response()
    delete (raw.input_parameters as Record<string, unknown>)[key]
    assert.throws(() => adaptTaskInputs(raw))
  }
  const raw = response()
  raw.input_parameters.media_uses.push(media('first'))
  assert.throws(() => adaptTaskInputs(raw))
  assert.throws(() => adaptTaskInputs({ input_parameters: { reference_assets: [], continuity_assets: [] } }))
})

test('unavailable is explicit and cannot carry a stale input projection', () => {
  const result = adaptTaskInputs({ input_contract_status: 'unavailable', input_contract_code: 'frozen_contract_invalid', input_parameters: null })
  assert.equal(result.inputParameters, undefined)
  assert.deepEqual(result.referenceAssets, [])
  assert.throws(() => adaptTaskInputs({ ...response(), input_contract_status: 'unavailable', input_contract_code: 'frozen_contract_invalid' }))
  assert.throws(() => adaptTaskInputs({ ...response(), input_contract_code: 'legacy_schema_unsupported' }))
})

test('staging and declared ports cannot contradict one another', () => {
  const raw = response()
  raw.input_parameters.media_uses[0]!.staging_status = 'not_applicable'
  assert.throws(() => adaptTaskInputs(raw))
  raw.input_parameters.media_uses[0]!.staging_status = 'staged'
  raw.input_parameters.media_uses[0]!.ports = []
  assert.throws(() => adaptTaskInputs(raw))
})
