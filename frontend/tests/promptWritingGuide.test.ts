import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

import {
  CLASSIFIED_REFERENCE_CONTEXT_PROJECTION_KEY,
  promptWritingGuideContract,
} from '../src/services/promptWritingGuide.ts'
import { adaptPromptArtifact } from '../src/services/promptArtifactAdapter.ts'

const fictionalRules = [
  {
    fieldKey: 'fictional.exclusions', sourceSelector: 'definition.default', channel: 'negative_prompt',
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
  assert.equal(guide.showClassifiedReferenceExample, false)
  assert.deepEqual(guide.fieldRules.map((rule) => rule.fieldKey), [
    'fictional.story', 'fictional.camera', 'fictional.exclusions',
  ])
  assert.deepEqual(guide.fieldRules.map((rule) => rule.channel), [
    'positive_prompt', 'positive_prompt', 'negative_prompt',
  ])
})

test('six-section H3 example is enabled only by the controlled projection key', () => {
  const classified = promptWritingGuideContract({
    profileKey: 'configured.reference.context',
    name: 'Configured reference context',
    version: 1,
    sourceProjectionKey: CLASSIFIED_REFERENCE_CONTEXT_PROJECTION_KEY,
    fieldRules: [],
  })
  const unknown = promptWritingGuideContract({
    profileKey: 'fictional.adapter',
    name: 'Fictional adapter',
    version: 1,
    sourceProjectionKey: 'fictional_projection_v9',
    fieldRules: [],
  })

  assert.equal(classified.showClassifiedReferenceExample, true)
  assert.equal(unknown.showClassifiedReferenceExample, false)
})

test('PromptWritingGuide gates every H3 six-section block on adapter configuration', () => {
  const component = readFileSync(
    new URL('../src/components/PromptWritingGuide.vue', import.meta.url),
    'utf8',
  )
  assert.match(component, /guide\.showClassifiedReferenceExample/)
  assert.match(component, /H3 分类参考上下文六段案例/)
  assert.doesNotMatch(component, /字段写作模板与 H3 导演描述规则/)
  assert.match(component, /当前策略 \/ CURRENT PROMPT PROFILE/)
  assert.match(component, /参考视频会被模型读取完整内容/)
  assert.match(component, /purpose 仅表达希望侧重的动作、运镜或节奏意图/)
  assert.match(component, /不提供模型级内容隔离/)
  assert.match(component, /不能替代 Bible 锚点/)
  assert.doesNotMatch(component, /参考视频只负责声明的动作、运镜或节奏/)
  const storyboard = readFileSync(
    new URL('../src/views/StoryboardView.vue', import.meta.url),
    'utf8',
  )
  assert.match(storyboard, /generatedPrompt\?\.prompt_profile_guide/)
})

test('persisted frozen Profile assembly becomes the Storyboard guide input', () => {
  const payload = adaptPromptArtifact({
    id: 'artifact-1',
    prompt_type: 'video',
    model: 'fictional-video-v9',
    conditioning_mode: 'fictional.text',
    prompt_text: 'Story: move forward',
    include_audio: false,
    is_multi_shot: false,
    structured_payload: {
      effective_conditioning_mode: 'fictional.text',
      prompt_profile: {
        profile_key: 'fictional.cinema.v1',
        version: 7,
        source_projection_key: 'direct_v1',
        official_reference: 'https://example.test/fictional-model',
        field_rules: fictionalRules.map((rule) => ({
          field_key: rule.fieldKey,
          source_selector: rule.sourceSelector,
          channel: rule.channel,
          position: rule.position,
          label_style: rule.labelStyle,
          separator: rule.separator,
          max_chars: rule.maxChars,
        })),
      },
    },
  })

  assert.equal(payload.prompt_profile_guide?.profileKey, 'fictional.cinema.v1')
  assert.equal(payload.prompt_profile_guide?.sourceProjectionKey, 'direct_v1')
  assert.deepEqual(
    promptWritingGuideContract(payload.prompt_profile_guide).fieldRules.map((rule) => rule.fieldKey),
    ['fictional.story', 'fictional.camera', 'fictional.exclusions'],
  )
})
