import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('rule editor owns typed fixed/default values and dictionary submits neither', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { default: Settings } = await vite.ssrLoadModule('/src/components/PromptProfileSettings.vue')
  let state: any
  await renderToString(createSSRApp({ setup() { state = Settings.setup({}, { expose() {} }); return () => h('div') } }))
  const field = { id: 'one', key: 'custom_value', valueType: 'boolean', valueOptions: [] }
  state.fields.value = [field]
  const version = { id: 'version-one', field_definition: 'one', field_key: 'custom_value', value_type: 'boolean', value_options: [], status: 'published' }
  state.selectRuleVersion(version)
  Object.assign(state.ruleForm, { fieldDefinition: 'one', fieldDefinitionVersion: version.id, sourceSelector: 'fixed.value', generationSourceSelector: 'fixed.value', fixedBoolean: false })
  assert.equal(state.buildRulePayload().field_definition_version, version.id)
  assert.equal(state.buildRulePayload().fixed_value, false)
  assert.equal(state.buildRulePayload().has_default, false)
  field.valueType = 'number'
  assert.equal(state.buildRulePayload().fixed_value, false, 'changing identity metadata cannot reinterpret the selected version')
  version.value_type = 'integer'
  state.selectRuleVersion({ ...version })
  state.ruleForm.fixedText = '0'
  assert.equal(state.buildRulePayload().fixed_value, 0)
  state.ruleForm.fixedText = '1.5'
  assert.throws(() => state.buildRulePayload(), /整数/)
  Object.assign(state.ruleForm, { sourceSelector: 'request.theme', generationSourceSelector: 'request.theme', hasDefault: true, defaultText: '0' })
  assert.equal(state.buildRulePayload().default_value, 0)
  assert.equal(state.buildRulePayload().fixed_value, null)
  state.ruleForm.hasDefault = false
  assert.equal(state.buildRulePayload().default_value, null)
  assert.equal(state.buildFieldPayload, undefined, 'Profile no longer authors shared field identities')
  assert.equal(state.openField, undefined)
  const { adaptPromptProfileFieldRule, adaptFieldDefinition } = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const rule = adaptPromptProfileFieldRule({ fixed_value: false, has_default: true, default_value: 0 })
  assert.deepEqual([rule.fixedValue, rule.hasDefault, rule.defaultValue], [false, true, 0])
  assert.equal('defaultValue' in adaptFieldDefinition({ id:'field', key:'motion', is_active:true, default_value: 'obsolete' }), false)
})
