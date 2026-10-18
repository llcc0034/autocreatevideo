import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import { fieldLineageFixture } from './fixtures/project-field-lineage-data.ts'

test('field lineage connects frozen definitions, adoption and explicit bindings without guessing', async t => {
  const vite = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  t.after(() => vite.close())
  const m = await vite.ssrLoadModule('/src/services/projectFieldLineage.ts')
  assert.deepEqual(m.projectVisualFields.map(row => row.key), ['negative_prompt', 'color_script', 'continuity_summary'])
  await t.test('reads original fields and distinguishes current values', () => {
    const f = fieldLineageFixture()
    assert.equal(m.visualLineageContract(f.run, 'project', f.artifact).fields[1].response_path, 'color_design')
    assert.equal(m.visualReviewEvidence(f.run, 'project').rules.color_script, 'Cool blue palette.')
    f.project.bible.colorScript = 'Changed'
    assert.equal(m.visualReviewEvidence(f.run, 'project').rules.color_script, 'Cool blue palette.')
    assert.throws(() => m.visualReviewEvidence(f.run, 'other'), /项目不一致/)
  })
  await t.test('foreign, missing, ambiguous and mismatched evidence stays explicit', () => {
    for (const mutate of [(f:any) => { f.artifact.productionRun = 'foreign' }, (f:any) => { f.artifact.sha256 = 'bad' },
      (f:any) => { f.artifact.generationAttempt = 'different' }, (f:any) => { f.artifact.textCandidate.preparation_id = 'different' },
      (f:any) => { f.artifact.status = 'available' }, (f:any) => { f.artifact.textCandidate.review_contract = null },
      (f:any) => { f.run.stages.push(f.run.stages[1]) }, (f:any) => { f.run.stages[1].outputSnapshot.canonical.rules = {} }]) {
      const f = fieldLineageFixture(); mutate(f); assert.throws(() => m.visualLineageContract(f.run, 'project', f.artifact))
    }
    const f = fieldLineageFixture(); f.run.stages[1].status = 'pending'; assert.equal(m.visualReviewEvidence(f.run, 'project'), null)
  })
  await t.test('manual adoption and extra output fields use their saved contract', () => {
    const f = fieldLineageFixture()
    f.run.planSource = 'manual'; f.run.stages[1].outputSnapshot = f.review
    f.artifact.textCandidate.review_contract.fields.push({key:'camera_guidance',label:'镜头指导',value_type:'text',allow_blank:false,
      source_selector:'reviewed_visual_rules.camera_guidance',response_path:'camera',response_format:'text',transformation:'保留原文'})
    f.artifact.textCandidate.candidate.camera_guidance='Eye-level framing.'
    assert.equal(m.visualLineageContract(f.run,'project',f.artifact).fields.at(-1).label,'镜头指导')
    assert.equal(m.projectVisualFields.some(row=>row.key==='camera_guidance'),false,'extra fields must not pretend to be project columns')
  })
  await t.test('nested mapping is an edge but literal objects and matching input names are not', () => {
    const f = fieldLineageFixture(), inputs:any = f.definition.stages[0]!.routing.routes[0]!.input_sources
    inputs.color_script = { kind: 'literal', value: f.source }
    let result = m.fieldConsumers(f.run, f.source.selector_key)
    assert.equal(result.rows.length, 1); assert.equal(result.rows[0].target, 'scene["color"]')
    assert.match(result.note, /不代表模型实际已读取/)
    inputs.scene = { kind: 'structured', template: { kind: 'array', items: [{ kind: 'each', source: f.source, item: { kind: 'literal', value: f.source } }] } }
    assert.equal(m.fieldConsumers(f.run, f.source.selector_key).rows.length, 1)
    f.run.stages[2].selectedRouteIndex = 1; assert.match(m.fieldConsumers(f.run, f.source.selector_key).rows[0].route, /未选/)
    f.run.planSource = 'manual'; assert.match(m.fieldConsumers(f.run, f.source.selector_key).note, /每次执行准备/)
    f.run.planSource = 'recipe'; f.run.executionPlan = {}; assert.match(m.fieldConsumers(f.run, f.source.selector_key).note, /未能读取/)
  })
  await t.test('panel loads on demand, retries errors and discards late reads on Run change', async () => {
    const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts'), old = platformApi.getArtifact
    const { default: C } = await vite.ssrLoadModule('/src/components/ProjectFieldLineage.vue')
    const f = fieldLineageFixture(), props = reactive({ run: f.run, project: structuredClone(f.project), savedProject: f.project })
    let count = 0, reject = false
    platformApi.getArtifact = async () => { count++; if (reject) throw Error('fixture read failed'); return f.artifact }
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:all(.*)*', component: { render: () => null } }] })
    await router.push('/'); await router.isReady()
    const renderer = createRenderer<any,any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    let instance:any
    const app = renderer.createApp({ render: () => h(C, { ...props, ref: (v:any) => { instance = v } }) }).use(router)
    app.provide(ssrContextKey, { modules: new Set() }); app.mount({})
    const settle = async () => { for (let i = 0; i < 20; i++) { await Promise.resolve(); await nextTick() } }
    try {
      await settle(); const s = instance.$.devtoolsRawSetupState
      assert.equal(count, 0); s.open.value = true; await settle(); assert.equal(count, 1); assert.ok(s.contract.value)
      props.run = { ...props.run }; await settle(); assert.equal(count, 1, 'unchanged polls must not reread')
      s.selected.value = 'color_script'; props.project.bible.colorScript = 'Unsaved'; await settle(); assert.match(s.comparison.value, /未保存/)
      reject = true; await s.load(); assert.equal(s.contract.value, null); assert.match(s.error.value, /fixture read failed/)
      reject = false; await s.load(); assert.ok(s.contract.value)
      let resolve!: (v:any) => void; platformApi.getArtifact = () => new Promise(r => { resolve = r })
      const pending = s.load(); props.run = { ...props.run, id: 'other', stages: [] }; await settle()
      resolve(f.artifact); await pending; assert.equal(s.contract.value, null)
      assert.equal(s.evidence.value.value, null)
    } finally { app.unmount(); platformApi.getArtifact = old }
  })
})
