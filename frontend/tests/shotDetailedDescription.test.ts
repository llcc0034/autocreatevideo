import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'

test('storyboard edits independent fields, original dialogue and timing without fixed creative controls', () => {
  const source = readFileSync(new URL('../src/views/StoryboardView.vue', import.meta.url), 'utf8')
  for (const name of ['narrative','camera','action','detailedDescription','continuityDirection','negativePrompt','framing']) {
    assert.ok(!source.includes('selectedShot.' + name), name)
  }
  assert.ok(!source.includes('selectedShot.audio.music'))
  assert.ok(!source.includes('selectedShot.audio.soundscape'))
  for (const component of ['ShotSourceFields','ShotFieldRevisions','ShotDialogueEditor','ShotTimingEditor'])
    assert.ok(source.includes('<'+component), component)
})

test('official read/save adapter uses no creative columns or narrative requirement for new shots', async () => {
  const vite = await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const {adaptShot,shotPersistencePayload} = await vite.ssrLoadModule('/src/services/backendAdapters.ts')
    const raw = {id:'new-shot',number:1,title:'新镜头',media_type:'video',duration_ms:1001,
      episode_start_ms:45000,episode_end_ms:46001,include_audio:true,dialogue:[],character_relations:[],props:[],metadata:{}}
    const shot = adaptShot(raw)
    const payload = shotPersistencePayload({bible:{characters:[],scenes:[],props:[]}},shot)
    assert.equal(payload.title,'新镜头')
    assert.equal(payload.duration_ms,1001)
    assert.equal(payload.episode_start_ms,45000)
    assert.equal(payload.episode_end_ms,46001)
    for (const field of ['narrative','camera','action','detailed_description','sound_effects','music','continuity_notes'])
      assert.ok(!(field in payload),field)
    assert.deepEqual(payload.metadata,{})
    assert.deepEqual(payload.dialogue,[])
    assert.deepEqual(shot.audio,{dialogue:[]})
  } finally { await vite.close() }
})
