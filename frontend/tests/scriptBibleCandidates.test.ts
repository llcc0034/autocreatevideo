import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

const projectId = '11111111-1111-4111-8111-111111111111'
const existingBibleId = '22222222-2222-4222-8222-222222222222'
const createdBibleId = '33333333-3333-4333-8333-333333333333'
const characterCandidateId = 'a'.repeat(64)
const sceneCandidateId = 'b'.repeat(64)

const rawCandidates = {
  schema_version: 1,
  project_id: projectId,
  source_fingerprint: 'c'.repeat(64),
  has_block: true,
  candidates: [
    {
      candidate_id: characterCandidateId,
      entity_type: 'character',
      name: '沈砚',
      description: '失忆剑客',
      visual_identity_en: 'A lean swordsman in a charcoal robe.',
      declaration_line: 3,
      occurrence: 4,
      evidence: [{ line: 8, kind: 'dialogue_speaker', excerpt: '沈砚：谁在那里？' }],
      recommended: true,
      matched_bible_id: null,
      existing: false,
      confirmed: false,
      conflict_code: null,
    },
    {
      candidate_id: sceneCandidateId,
      entity_type: 'scene',
      name: '听雨客栈',
      description: '雨夜中的旧客栈',
      visual_identity_en: 'An old timber inn under cold rain.',
      declaration_line: 4,
      occurrence: 2,
      evidence: [{ line: 6, kind: 'shot_setting', excerpt: '场景：听雨客栈' }],
      recommended: false,
      matched_bible_id: existingBibleId,
      existing: true,
      confirmed: false,
      conflict_code: null,
    },
  ],
  diagnostics: [{ line: 3, severity: 'info', code: 'script_bible_candidate_parsed', detail: '已解析人物声明。', entity_type: 'character', candidate_id: characterCandidateId }],
}

const bibleRow = (id: string, name: string) => ({
  id,
  slug: name === '沈砚' ? 'shen-yan' : 'ting-yu-ke-zhan',
  name,
  description: `${name}的服务端描述`,
  visual_identity: `${name} server visual identity`,
  reference_assets: [],
  anchor_asset: null,
  is_locked: true,
  metadata: {},
})

const rawApply = {
  schema_version: 1,
  project_id: projectId,
  source_fingerprint: rawCandidates.source_fingerprint,
  updated_at: '2026-08-20T14:45:00Z',
  created: { characters: [bibleRow(createdBibleId, '沈砚·改')], scenes: [], props: [] },
  already_exists: { characters: [], scenes: [], props: [] },
  counts: { requested: 1, created: 1, already_exists: 0 },
}

test('script Bible candidate contract, review payload, failures, and incremental merge stay explicit', async (context) => {
  const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
  const server = await createServer({
    root: frontendRoot,
    logLevel: 'silent',
    appType: 'custom',
    server: { middlewareMode: true, hmr: false },
  })
  context.after(async () => { await server.close() })

  const service = await server.ssrLoadModule('/src/services/scriptBibleCandidates.ts')
  const { repository } = await server.ssrLoadModule('/src/services/repository.ts')
  const { api } = await server.ssrLoadModule('/src/services/api.ts')
  const originalGet = api.get
  const originalPost = api.post
  context.after(() => { api.get = originalGet; api.post = originalPost })

  await context.test('GET and local edits perform zero writes; existing and confirmed remain separate', async () => {
    let writes = 0
    api.get = async (url: string) => {
      assert.equal(url, `/projects/${projectId}/script-bible-candidates/`)
      return { data: rawCandidates }
    }
    api.post = async () => { writes += 1; throw new Error('unexpected write') }

    const candidates = await repository.getScriptBibleCandidates(projectId)
    const drafts = service.createScriptBibleCandidateDrafts(candidates.candidates)
    drafts[characterCandidateId].name = '沈砚·改'
    drafts[characterCandidateId].description = '人工补充且必须保留的描述'

    assert.equal(writes, 0)
    assert.equal(candidates.candidates[1].existing, true)
    assert.equal(candidates.candidates[1].confirmed, false)
    assert.equal(drafts[characterCandidateId].description, '人工补充且必须保留的描述')
  })

  await context.test('recommended selection is seeded once; clear then refresh stays clear', () => {
    const candidates = service.adaptScriptBibleCandidates(rawCandidates).candidates
    const first = service.reconcileScriptBibleSelectionAfterRefresh(candidates, [], false)
    assert.deepEqual([...first.selectedIds], [characterCandidateId])
    assert.equal(first.recommendedSeeded, true)
    assert.equal(service.scriptBibleCandidateSelectable(candidates[1]), true)

    const afterClearAndRefresh = service.reconcileScriptBibleSelectionAfterRefresh(
      candidates,
      [],
      first.recommendedSeeded,
    )
    assert.deepEqual([...afterClearAndRefresh.selectedIds], [])
    assert.equal(afterClearAndRefresh.recommendedSeeded, true)
    assert.equal(service.masterScriptNeedsScriptBibleRefresh('新增 SCRIPT_BIBLE_V1 声明', '旧母稿'), true)
    assert.equal(service.masterScriptNeedsScriptBibleRefresh('同一母稿', '同一母稿'), false)
  })

  await context.test('selected-only payload preserves exact human edits and rejects aliases', () => {
    const candidates = service.adaptScriptBibleCandidates(rawCandidates)
    const drafts = service.createScriptBibleCandidateDrafts(candidates.candidates)
    drafts[characterCandidateId] = {
      name: '沈砚·改',
      description: '  人工保留空格的描述  ',
      visualIdentityEn: 'Human-reviewed immutable visual identity.',
    }
    const selections = service.buildScriptBibleSelections(candidates.candidates, [characterCandidateId], drafts)
    const payload = service.scriptBibleApplyPayload(candidates.sourceFingerprint, selections)
    const existingSelection = service.buildScriptBibleSelections(candidates.candidates, [sceneCandidateId], drafts)

    assert.deepEqual(payload, {
      source_fingerprint: rawCandidates.source_fingerprint,
      selections: [{
        candidate_id: characterCandidateId,
        reviewed: true,
        name: '沈砚·改',
        description: '  人工保留空格的描述  ',
        visual_identity_en: 'Human-reviewed immutable visual identity.',
      }],
    })
    assert.equal(payload.selections.length, 1)
    assert.equal(existingSelection[0].candidateId, sceneCandidateId)
    assert.equal(existingSelection[0].name, '听雨客栈')
    assert.deepEqual(Object.keys(payload.selections[0]).sort(), ['candidate_id', 'description', 'name', 'reviewed', 'visual_identity_en'])
    assert.throws(() => service.adaptScriptBibleCandidates({ ...rawCandidates, schemaVersion: 1, schema_version: undefined }), /契约无效/)
  })

  await context.test('409 stale/apply conflicts preserve drafts and expose authoritative codes', async () => {
    const candidates = service.adaptScriptBibleCandidates(rawCandidates)
    const drafts = service.createScriptBibleCandidateDrafts(candidates.candidates)
    drafts[characterCandidateId].name = '不会因失败丢失的人工名称'
    const snapshot = structuredClone(drafts)
    const stale = Object.assign(new Error('unsafe backend detail'), {
      response: { status: 409, data: { error: { status_code: 409, code: 'script_bible_candidates_stale', details: { detail: 'stale', pending_count: 2 } } } },
    })
    api.post = async () => { throw stale }

    await assert.rejects(repository.applyScriptBibleCandidates(
      projectId,
      candidates.sourceFingerprint,
      service.buildScriptBibleSelections(candidates.candidates, [characterCandidateId], drafts),
    ))
    assert.deepEqual(drafts, snapshot)
    assert.equal(service.scriptBibleErrorCode(stale), 'script_bible_candidates_stale')
    assert.equal(service.scriptBiblePendingCountFromError(stale), 2)
    assert.match(service.scriptBibleErrorMessage(stale, 'fallback'), /已过期/)

    const conflict = Object.assign(new Error('conflict'), {
      response: { status: 409, data: { error: { code: 'script_bible_apply_conflict', details: { detail: 'conflict' } } } },
    })
    assert.match(service.scriptBibleErrorMessage(conflict, 'fallback'), /未写入任何内容/)

    const confirmation = Object.assign(new Error('confirmation'), {
      response: { status: 409, data: { error: { code: 'script_bible_confirmation_required', details: { detail: 'confirmation required', pending_count: 3 } } } },
    })
    assert.equal(service.scriptBibleErrorCode(confirmation), 'script_bible_confirmation_required')
    assert.equal(service.scriptBiblePendingCountFromError(confirmation), 3)
    assert.match(service.scriptBibleErrorMessage(confirmation, 'fallback'), /明确跳过/)
  })

  await context.test('apply uses the exact endpoint and grouped response appends without deleting old Bible rows', async () => {
    let postedUrl = ''
    let postedPayload: unknown
    api.post = async (url: string, payload: unknown) => {
      postedUrl = url
      postedPayload = payload
      return { data: rawApply }
    }
    const candidates = service.adaptScriptBibleCandidates(rawCandidates)
    const drafts = service.createScriptBibleCandidateDrafts(candidates.candidates)
    drafts[characterCandidateId].name = '沈砚·改'
    const characterSelection = service.buildScriptBibleSelections(candidates.candidates, [characterCandidateId], drafts)
    const result = await repository.applyScriptBibleCandidates(projectId, candidates.sourceFingerprint, characterSelection)

    assert.equal(postedUrl, `/projects/${projectId}/script-bible-candidates/apply/`)
    assert.deepEqual(postedPayload, service.scriptBibleApplyPayload(candidates.sourceFingerprint, characterSelection))
    assert.equal(result.updatedAt, '2026-08-20T14:45:00Z')
    assert.equal(result.created.characters[0].name, '沈砚·改')

    const oldCharacter = { id: 'old-character', name: '旧角色', description: '保留', visualPrompt: 'keep', referenceAssetIds: [], locked: true }
    const oldScene = { id: existingBibleId, name: '本地未保存场景编辑', description: '不要覆盖', visualPrompt: 'local', referenceAssetIds: [], locked: true }
    const bible = { characters: [oldCharacter], scenes: [oldScene], props: [], negativePrompt: 'keep-negative', colorScript: 'keep-color' }
    const appended = service.mergeScriptBibleApplyIntoBible(bible, {
      ...result,
      alreadyExists: { ...result.alreadyExists, scenes: [service.adaptScriptBibleApplyResult({
        ...rawApply,
        created: { characters: [], scenes: [], props: [] },
        already_exists: { characters: [], scenes: [bibleRow(existingBibleId, '听雨客栈')], props: [] },
        counts: { requested: 1, created: 0, already_exists: 1 },
      }).alreadyExists.scenes[0]] },
    })

    assert.equal(appended, 1)
    assert.deepEqual(bible.characters.map((item: { id: string }) => item.id), ['old-character', createdBibleId])
    assert.equal(bible.scenes.length, 1)
    assert.equal(bible.scenes[0].name, '本地未保存场景编辑')
    assert.equal(bible.negativePrompt, 'keep-negative')
  })
})

test('studio wiring uses a two-layer one-time skip and never routes candidate apply through syncBible', () => {
  const view = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  const component = readFileSync(new URL('../src/components/ScriptBibleCandidatesPanel.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  const types = readFileSync(new URL('../src/types/index.ts', import.meta.url), 'utf8')
  const candidateMethods = repository.slice(
    repository.indexOf('async getScriptBibleCandidates'),
    repository.indexOf('async generateVisualRulesDraft'),
  )
  const breakdownOptions = types.slice(
    types.indexOf('export interface ScriptBreakdownOptions'),
    types.indexOf('export interface AssemblyResult'),
  )
  const applySelected = component.slice(
    component.indexOf('const applySelected = async'),
    component.indexOf('const focus = async'),
  )

  assert.ok(view.includes('先确认建议'))
  assert.ok(view.includes('跳过并继续'))
  assert.ok(view.includes('再次确认跳过 Bible 建议'))
  assert.ok(view.includes('确认跳过并继续'))
  assert.ok(view.includes('studio.breakdownScript(project.value, breakdownOptions.value, continueWithoutScriptBible)'))
  assert.ok(view.includes('mergeScriptBibleApplyIntoBible(currentProject.bible, result)'))
  assert.ok(view.includes('scriptBibleCanonicalEntries(result)'))
  assert.ok(view.includes('studio.ensureCanonicalGlossary(currentProject, { entries: canonicalEntries })'))
  assert.ok(view.includes('currentProject.updatedAt = refreshed.updatedAt || currentProject.updatedAt'))
  assert.ok(view.includes("productionErrorCode(error) === 'script_bible_confirmation_required'"))
  assert.ok(view.includes('<ScriptBibleCandidatesPanel ref="scriptBiblePanel" v-show="tab===\'bible\'"'))
  assert.ok(view.includes(':source-dirty="scriptBibleSourceDirty"'))
  assert.ok(view.includes('@apply-busy="scriptBibleApplying=$event"'))
  assert.ok(view.includes('if (scriptBibleApplying.value)'))
  assert.ok(component.includes('if (props.sourceDirty)'))
  assert.ok(component.includes('const version = ++applyVersion'))
  assert.ok(component.includes('projectId !== props.projectId'))
  assert.ok(component.includes("applyError.value = ''"))
  assert.ok(applySelected.indexOf('await repository.applyScriptBibleCandidates') < applySelected.indexOf('selectedIds.value = new Set()'))
  assert.ok(!applySelected.slice(applySelected.indexOf('catch (error)')).includes('drafts.value ='))
  assert.ok(!candidateMethods.includes('syncBible('))
  assert.ok(candidateMethods.includes('/script-bible-candidates/'))
  assert.ok(candidateMethods.includes('/script-bible-candidates/apply/'))
  assert.ok(!breakdownOptions.includes('continueWithoutScriptBible'))
  assert.ok(!breakdownOptions.includes('continue_without_script_bible'))
  assert.ok(repository.includes('continue_without_script_bible: continueWithoutScriptBible'))
  const breakdownStart = view.indexOf('const breakdown = async () => {')
  const breakdownEnd = view.indexOf('\nconst confirmProjectRebuild', breakdownStart)
  const breakdownMethod = view.slice(breakdownStart, breakdownEnd)
  assert.ok(breakdownMethod.indexOf('resetBreakdownBypassState()') < breakdownMethod.indexOf('masterScriptNeedsScriptBibleRefresh('))
  assert.ok(breakdownMethod.indexOf('const saved = await save(false)') < breakdownMethod.indexOf('await scriptBiblePanel.value?.refresh()'))
  assert.ok(breakdownMethod.indexOf('await scriptBiblePanel.value?.refresh()') < breakdownMethod.indexOf('if (scriptBiblePendingCount.value > 0)'))
  assert.ok(!breakdownMethod.includes('scriptBibleHasBlock.value || scriptBiblePendingCount.value > 0'))
  assert.ok(component.includes('!item.confirmed).length'))
  assert.ok(view.includes('@click="closeRebuildConfirmation">取消</button>'))
  assert.ok(view.includes("const resetBreakdownBypassState = () => {\n  closeScriptBibleGate()\n  closeRebuildConfirmation()"))
  assert.ok(view.includes('@click="save(true)"'))
})
