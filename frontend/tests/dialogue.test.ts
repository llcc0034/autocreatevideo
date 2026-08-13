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

test('rejects unknown speakers and overlong dialogue text', () => {
  assert.equal(parseDialogueText('陌生人：台词', characters), undefined)
  assert.equal(parseDialogueText(`S1：${'字'.repeat(2001)}`, characters), undefined)
})
