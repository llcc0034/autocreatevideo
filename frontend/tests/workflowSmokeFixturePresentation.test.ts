import assert from 'node:assert/strict'
import test from 'node:test'

import type { WorkflowSmokeFixtureParameter } from '../src/services/platformTypes.ts'
import {
  workflowSmokeFixtureParameterControl,
  workflowSmokeFixtureValueFromControl,
} from '../src/services/workflowSmokeFixturePresentation.ts'

const parameter = (
  semanticKey: string,
  type = 'string',
): WorkflowSmokeFixtureParameter => ({
  semanticKey,
  type,
  required: true,
  min: null,
  max: null,
  multipleOf: null,
  alignmentModulus: null,
  alignmentRemainder: null,
  options: [],
  selectionGroup: '',
  recommendedValue: '',
})

test('prompt semantic string fields use a multiline control and preserve section line breaks', () => {
  const prompt = parameter('prompt.positive')
  const sixSections = [
    'subject_definitions: detective, pocket watch',
    'summary: a warning at the station',
    'retention_analysis: retain identity and amber light',
    'detailed_description: the detective says "Do not turn around."',
    'overall_soundscape: rain, footsteps, distant train',
    'non_diegetic_music: restrained cello pulse',
  ].join('\n')

  assert.equal(workflowSmokeFixtureParameterControl(prompt), 'textarea')
  assert.equal(workflowSmokeFixtureValueFromControl(prompt, sixSections), sixSections)
  assert.equal(String(workflowSmokeFixtureValueFromControl(prompt, sixSections)).split('\n').length, 6)
  assert.equal(workflowSmokeFixtureParameterControl(parameter('generation.negative_prompt')), 'textarea')
})

test('ordinary short string fields remain single-line text controls', () => {
  const prefix = parameter('output.prefix')

  assert.equal(workflowSmokeFixtureParameterControl(prefix), 'text')
  assert.equal(workflowSmokeFixtureValueFromControl(prefix, 'smoke-h3-001'), 'smoke-h3-001')
})

test('a single required value is fixed, while optional enums retain the unset choice', () => {
  for (const [type, value] of [['number', 4], ['boolean', true], ['enum', 'ReferenceMode']] as const) {
    const row = { ...parameter('fixed.value', type), options: [value], recommendedValue: value }
    assert.equal(workflowSmokeFixtureParameterControl(row), 'fixed')
    assert.equal(workflowSmokeFixtureParameterControl({ ...row, required: false }), 'select')
  }
})
