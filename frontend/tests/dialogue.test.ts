import assert from 'node:assert/strict'
import test from 'node:test'

import { formatDialogueLines, parseDialogueText } from '../src/services/dialogue.ts'

const characters = [{ name: '沈砚', stableSpeakerId: 'S1' }]

test('parses delivery and voiceover token without changing dialogue text', () => {
  const result = parseDialogueText(
    '沈砚（voiceover; in a low, restrained voice）：谁在等我！？\nS1（softly）：别动。',
    characters
  )
  assert.deepEqual(result, [
    { speaker_id: 'S1', text: '谁在等我！？', delivery: 'in a low, restrained voice', voiceover: true },
    { speaker_id: 'S1', text: '别动。', delivery: 'softly' }
  ])
})

test('voiceover must be an independent token and formatting is reversible', () => {
  const parsed = parseDialogueText('S1（voiceoverish）：原文。', characters)
  assert.deepEqual(parsed, [{ speaker_id: 'S1', text: '原文。', delivery: 'voiceoverish' }])
  assert.equal(formatDialogueLines(parsed), 'S1（voiceoverish）：原文。')

  const voiceover = [{ speaker_id: 'S1', text: '原文。', delivery: 'softly', voiceover: true }]
  assert.deepEqual(parseDialogueText(formatDialogueLines(voiceover), characters), voiceover)
})

test('timed dialogue round-trips in milliseconds without changing the text', () => {
  const parsed = parseDialogueText(
    '[1–3.5秒] 沈砚（softly）：原文。\n[00:03.500-5000ms] S1：第二句。',
    characters,
  )
  assert.deepEqual(parsed, [
    { speaker_id: 'S1', text: '原文。', delivery: 'softly', start_ms: 1000, end_ms: 3500 },
    { speaker_id: 'S1', text: '第二句。', start_ms: 3500, end_ms: 5000 },
  ])
  assert.equal(
    formatDialogueLines(parsed),
    '[1–3.5秒] S1（softly）：原文。\n[3.5–5秒] S1：第二句。',
  )
  assert.deepEqual(parseDialogueText(formatDialogueLines(parsed), characters), parsed)
  assert.equal(parseDialogueText('[4-2秒] S1：倒序。', characters), undefined)
})

test('rejects unknown speakers but delegates content limits to the server', () => {
  assert.equal(parseDialogueText('陌生人：台词', characters), undefined)
  assert.deepEqual(parseDialogueText(`S1：${'字'.repeat(2001)}`, characters), [{
    speaker_id: 'S1',
    text: '字'.repeat(2001),
  }])
})
