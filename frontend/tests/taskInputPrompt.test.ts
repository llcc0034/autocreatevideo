import assert from 'node:assert/strict'
import test from 'node:test'
import { adaptTaskInputPrompt } from '../src/services/taskInputPrompt.ts'

const response = () => ({ task_id: 'task', attempt_id: 'attempt', preparation_id: 'prep', execution_instance_version_id: 'instance',
  request_sha256: 'sha', prompt_mode: 'manual', prompt_profile_version_id: null, declared_reference_labels: ['<Picture 1>'],
  channels: [{ key: 'positive_prompt', semantic_key: 'prompt.positive', text: '参考 <Picture 1>', character_count: 14, value_source: 'request' },
    { key: 'negative_prompt', semantic_key: 'prompt.negative', text: '', character_count: 0, value_source: 'request' }] })

test('channels preserve exact text including explicit emptiness and server Unicode count', () => {
  const result = adaptTaskInputPrompt(response())
  assert.equal(result.preparationId, 'prep')
  assert.equal(result.channels[1]?.text, '')
  assert.equal(result.channels[1]?.characterCount, 0)
  assert.equal(result.channels[0]?.characterCount, 14)
  assert.equal('negativePromptSubmitted' in result, false)
})

test('profile and no-prompt modes are explicit, not guessed from nonempty text', () => {
  assert.equal(adaptTaskInputPrompt({ ...response(), prompt_mode: 'profile', prompt_profile_version_id: 'profile-version' }).promptProfileVersionId, 'profile-version')
  assert.deepEqual(adaptTaskInputPrompt({ ...response(), prompt_mode: 'none', channels: [], declared_reference_labels: [] }).channels, [])
})

test('missing, duplicate and legacy prompt fields are not repaired with defaults', () => {
  for (const key of ['prompt_mode', 'prompt_profile_version_id', 'channels', 'preparation_id', 'declared_reference_labels']) {
    const raw: Record<string, unknown> = response()
    delete raw[key]
    assert.throws(() => adaptTaskInputPrompt(raw))
  }
  assert.throws(() => adaptTaskInputPrompt({ prompt: 'old', negative_prompt: '' }))
  assert.throws(() => adaptTaskInputPrompt({ ...response(), channels: [response().channels[0], response().channels[0]] }))
})
