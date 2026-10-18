import assert from 'node:assert/strict'
import test from 'node:test'
import { readDialogue } from '../src/services/dialogue.ts'
import { readShotCharacters, removeDialogueLine, toggleVisibleCharacter, visibleCharacterIds } from '../src/services/shotCharacters.ts'
const visibleId = '11111111-1111-4111-8111-111111111111'
const speakerId = '22222222-2222-4222-8222-222222222222'
const lines = [{speaker_id:'S2',text:'  原文\n不裁剪。  ',language:'Chinese',delivery:' softly ',voiceover:true,start_ms:123,end_ms:987}]
const relations = [{role:'visible',position:0,character_id:visibleId},{role:'speaker',position:0,character_id:speakerId}]

test('structured dialogue round trips original whitespace, explicit false and integer milliseconds', () => {
  const value = [...lines, {speaker_id:'S1',text:'另一行',voiceover:false}]
  const result = readDialogue(value)
  assert.deepEqual(result, value)
  assert.notEqual(result[0],value[0])
  assert.deepEqual(readShotCharacters(relations, lines),relations)
})
test('no display text parsing, name inference, coercion or incomplete timing', () => {
  for (const input of ['同名：文字',null,[{speaker_id:'同名',text:'原文'}],
    [{...lines[0],end_ms:undefined}],[{...lines[0],start_ms:true}],[{...lines[0],voiceover:1}],
    [{...lines[0],unexpected:'ignored'}],[{...lines[0],text:' '}], [{...lines[0],end_ms:100}]]) {
    assert.throws(()=>readDialogue(input))
  }
})
test('each speech line keeps its language independently, missing remains missing', () => {
  const value = [...lines, {...lines[0], text: 'Hello.', language: 'English'}, {speaker_id:'S2', text:'Unknown language'}]
  assert.deepEqual(readDialogue(value), value)
  for (const language of [false, null, 1, 'English\n', '{language}', '<d>', '[Chinese]']) {
    assert.throws(() => readDialogue([{...lines[0], language}]))
  }
})
test('roles require exact typed positions and complete speaker identity', () => {
  for (const input of [undefined,[],relations.slice(0,1),[...relations,relations[1]],
    [{...relations[1],position:true}],[{...relations[1],role:['speaker']}],
    [{...relations[1],position:1}],[{...relations[1],character_id:'name'}]]) {
    assert.throws(()=>readShotCharacters(input,lines))
  }
})
test('visibility changes do not create or delete a speaker; line removal shifts only speaker positions', () => {
  const shot = {characterRelations:structuredClone(relations),audio:{dialogue:structuredClone(lines)}} as never
  toggleVisibleCharacter(shot,visibleId)
  assert.deepEqual(visibleCharacterIds(shot),[])
  assert.deepEqual(shot.characterRelations,[relations[1]])
  toggleVisibleCharacter(shot,speakerId)
  assert.deepEqual(visibleCharacterIds(shot),[speakerId])
  shot.audio.dialogue.push({speaker_id:'S1',text:'第二行'})
  shot.characterRelations.push({role:'speaker',position:1,character_id:visibleId})
  removeDialogueLine(shot,0)
  assert.deepEqual(shot.audio.dialogue,[{speaker_id:'S1',text:'第二行'}])
  assert.deepEqual(shot.characterRelations.filter(row=>row.role==='speaker'),[{role:'speaker',position:0,character_id:visibleId}])
  assert.deepEqual(visibleCharacterIds(shot),[speakerId])
})
