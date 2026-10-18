import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

import {
  promptWritingGuideContract,
} from '../src/services/promptWritingGuide.ts'

const fictionalRules = [
  {
    fieldKey: 'fictional.exclusions', sourceSelector: 'fixed.value', channel: 'negative_prompt',
    position: 3, labelStyle: 'none', separator: 'newline', maxChars: 600,
  },
  {
    fieldKey: 'fictional.camera', sourceSelector: 'shot.camera', channel: 'positive_prompt',
    position: 2, labelStyle: 'bracketed', separator: 'semicolon', maxChars: 900,
  },
  {
    fieldKey: 'fictional.story', sourceSelector: 'request.text', channel: 'positive_prompt',
    position: 1, labelStyle: 'label_colon', separator: 'space', maxChars: 2400,
  },
]

test('fictional direct profile guide is driven by frozen configuration and shows no H3 example', () => {
  const guide = promptWritingGuideContract({
    profileKey: 'fictional.cinema.v1',
    name: 'Fictional Cinema Writer',
    version: 7,
    sourceProjectionKey: 'direct_v1',
    officialReference: 'https://example.test/fictional-model',
    fieldRules: fictionalRules,
  })

  assert.equal(guide.adapterKey, 'direct_v1')
  assert.match(guide.adapterDescription, /冻结的字段来源/)
  assert.deepEqual(guide.fieldRules.map((rule) => rule.fieldKey), [
    'fictional.story', 'fictional.camera', 'fictional.exclusions',
  ])
  assert.deepEqual(guide.fieldRules.map((rule) => rule.channel), [
    'positive_prompt', 'positive_prompt', 'negative_prompt',
  ])
})

test('declarative and unknown adapters never infer model-specific writing rules', () => {
  const declarative = promptWritingGuideContract({
    profileKey: 'configured.reference.context',
    name: 'Configured reference context',
    version: 1,
    sourceProjectionKey: 'declarative_v1',
    fieldRules: [],
  })
  const unknown = promptWritingGuideContract({
    profileKey: 'fictional.adapter',
    name: 'Fictional adapter',
    version: 1,
    sourceProjectionKey: 'fictional_projection_v9',
    fieldRules: [],
  })

  assert.equal(declarative.adapterKey, 'declarative_v1')
  assert.equal(unknown.adapterKey, 'fictional_projection_v9')
  assert.match(declarative.adapterDescription, /不会根据模型名称猜测/)
  assert.match(unknown.adapterDescription, /不会根据模型名称猜测/)
  assert.deepEqual(declarative.fieldRules, [])
})

test('authoring help uses exact instance contracts without legacy H3 examples or implicit model calls', () => {
  const component = readFileSync(
    new URL('../src/components/PromptWritingGuide.vue', import.meta.url),
    'utf8',
  )
  assert.doesNotMatch(component, /classifiedReference|showClassifiedReferenceExample|classified_reference_context_ir_v1|17k\+5/)
  assert.doesNotMatch(component, /字段写作模板与 H3 导演描述规则/)
  assert.match(component, /当前策略 \/ CURRENT PROMPT PROFILE/)
  assert.match(component, /参考视频会被模型读取完整内容/)
  assert.match(component, /purpose 仅表达希望侧重的动作、运镜或节奏意图/)
  assert.match(component, /不提供模型级内容隔离/)
  assert.match(component, /不能替代 Bible 锚点/)
  assert.match(component, /保存不会自动调用模型/)
  assert.match(component, /独立翻译使用明确选择的/)
  assert.match(component, /不自动寻找本地模型或旧绑定/)
  assert.match(component, /不会静默换成默认值/)
  assert.match(component, /45–50 秒代表一个 5 秒镜头/)
  assert.doesNotMatch(component, /只会提示并回退默认|必须用英文|中文 visualPrompt 会自动生成/)
  assert.doesNotMatch(component, /参考视频只负责声明的动作、运镜或节奏/)
  const storyboard = readFileSync(
    new URL('../src/views/StoryboardView.vue', import.meta.url),
    'utf8',
  )
  assert.match(storyboard, /<PromptWritingGuide scope="shot"/)
  assert.doesNotMatch(storyboard, /generatedPrompt|prompt_profile_guide/)
})

test('no execution version means generic authoring help without a guessed strategy', () => {
  const guide = promptWritingGuideContract(null)
  assert.equal(guide.profile, null)
  assert.equal(guide.adapterKey, '')
  assert.equal(guide.adapterLabel, '尚未读取策略配置')
  assert.deepEqual(guide.fieldRules, [])
})

test('an incomplete profile does not silently become direct assembly', () => {
  const guide = promptWritingGuideContract({ profileKey: 'incomplete', name: 'Missing projection',
    version: 1, sourceProjectionKey: ' ', fieldRules: fictionalRules })
  assert.equal(guide.adapterKey, '')
  assert.equal(guide.adapterLabel, '尚未读取策略配置')
  assert.equal(guide.fieldRules.length, fictionalRules.length)
})
