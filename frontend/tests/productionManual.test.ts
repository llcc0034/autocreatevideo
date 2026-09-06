import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { productionManual, searchableIncidentText, searchableManualText } from '../src/content/productionManual.ts'

const currentE2EReport = readFileSync(
  new URL('../../docs/e2e-short-drama-ref2va-dual-clock-test-2026-08-28.md', import.meta.url),
  'utf8',
)

const requiredStepIds = [
  'create-project',
  'prepare-content',
  'configure-providers',
  'configure-prompt-profiles',
  'validate-workflows',
  'activate-configuration',
  'probe-host',
  'manage-asset-library',
  'create-production-run',
  'review-script-bible',
  'handle-run-gates',
  'monitor-production-run',
  'inspect-diagnostics',
  'verify-delivery',
  'manual-ref2va-three-lane-acceptance',
]

test('manual has one ordered, searchable ProductionRun path', () => {
  assert.match(productionManual.version, /^\d{4}\.\d{2}\.\d{2}\.\d+$/)
  assert.equal(productionManual.version, '2026.08.31.2')
  assert.deepEqual(productionManual.steps.map((step) => step.id), requiredStepIds)
  assert.deepEqual(productionManual.steps.map((step) => step.number), requiredStepIds.map((_, index) => index + 1))
  assert.equal(new Set(productionManual.steps.map((step) => step.id)).size, requiredStepIds.length)

  for (const step of productionManual.steps) {
    assert.ok(step.location.trim(), step.id + ' needs a location')
    assert.ok(step.inputs.length, step.id + ' needs an input example')
    assert.ok(step.inputs.every((item) => item.label && item.example && item.note), step.id + ' has an incomplete input')
    assert.ok(step.actions.length, step.id + ' needs actions')
    assert.ok(step.success.length, step.id + ' needs success evidence')
    assert.ok(step.next.trim(), step.id + ' needs a next step')
    assert.ok(searchableManualText(step).includes(step.title.toLocaleLowerCase('zh-CN')))
  }
})

test('manual makes ProductionRun the only new production entry', () => {
  const assetLibrary = productionManual.steps.find((step) => step.id === 'manage-asset-library')
  const creation = productionManual.steps.find((step) => step.id === 'create-production-run')
  const diagnostics = productionManual.steps.find((step) => step.id === 'inspect-diagnostics')
  assert.ok(assetLibrary)
  assert.match(searchableManualText(assetLibrary), /图片.*视频.*音频.*剧本/)
  assert.match(searchableManualText(assetLibrary), /不创建 episode、shot/)
  assert.ok(creation)
  assert.ok(diagnostics)
  assert.match(searchableManualText(creation), /productionrun/)
  assert.match(searchableManualText(creation), /所有新生产/)
  assert.match(searchableManualText(diagnostics), /只读诊断/)
  assert.match(searchableManualText(diagnostics), /deferred history/)
  assert.doesNotMatch(searchableManualText(diagnostics), /task retry\/cancel|deferred 例外/)
})

test('manual documents implemented AI, workflow, async, config and delivery boundaries', () => {
  const text = productionManual.steps.map(searchableManualText).join(' ')
  for (const fragment of [
    'ai script',
    '确定性 bible',
    'semantic workflow',
    'promptprofile',
    'download_import',
    '逐镜头',
    't2i',
    't2v',
    'managed artifact',
    '五层配置',
    'boot activation',
    'runtimehost',
    'episode master',
    'releasebundle',
  ]) assert.ok(text.includes(fragment), fragment)

  assert.ok(text.includes('provider_upload_contract_missing'))
  assert.ok(text.includes('没有跨节点或多 gpu scheduler'))
  assert.ok(text.includes('不会自动下载模型'))
  assert.ok(text.includes('needs_review'))
  assert.ok(text.includes('实际通过范围只以对应日期的 e2e 报告'))
  assert.ok(text.includes('structured_shot_duration_contract'))
  assert.match(text, /1 集、1 镜、1 秒[^]*1000\/1000\/1000ms/)
  assert.match(currentE2EReport, /2026-08-28 动态 Ref2VA 双时钟短剧三线实际操作测试/)
  assert.match(currentE2EReport, /\| 纯人工 \|[^]*\| 半自动 \|[^]*\| 全自动目标 \|/)
  assert.match(currentE2EReport, /v22\/v23 的动态参考视频数为 0[^]*不能证明本轮/)
  assert.match(currentE2EReport, /生产手册[^]*第 15 步[^]*依次手动执行/)
  assert.ok(text.includes('不生成整季单条长视频'))
  assert.ok(text.includes('http 202'))
  assert.match(text, /script\.generate[^。；]*script\.structure[^。；]*prompt\.normalize[^。；]*visual_rules\.generate/)
  assert.match(text, /(?:禁止|不得)重复点击重放付费请求/)
})

test('manual keeps model-specific prompt grammar in versioned configuration', () => {
  const promptStep = productionManual.steps.find((step) => step.id === 'configure-prompt-profiles')
  assert.ok(promptStep)
  const text = searchableManualText(promptStep)
  for (const fragment of [
    'source_projection_key', 'direct_v1', 'declarative_v1',
    'classified_reference_context_ir_v1', 'projection.output',
    'projection_primitive', 'reference_context',
    'generation_source_selector', 'generation_source_selectors', 'field_definition.key/profile_field_key',
    'generation_context_selectors', 'target_shot.dialogue_metadata', 'target_shot.dialogue_authored_text',
    'adjacent_shots.core', 'all_shots.core',
    'source_selector', 'generation_mode', 'generation_instruction',
    'generation_output_semantics', 'language', 'length', 'validators',
    'position', 'channel', 'label_style', 'separator', 'max_chars',
    'channel_limits', 'warn_at_chars',
  ]) assert.ok(text.includes(fragment), fragment)
  assert.match(text, /dialogue_metadata[^。；]*不含对白原文/)
  assert.match(text, /dialogue_authored_text[^。；]*(?:对白原文|台词)/)
  assert.match(text, /adjacent_shots\.core[^。；]*all_shots\.core[^。；]*(?:显式|必须)/)
  assert.match(text, /空数组[^。；]*主要内容来源/)
  assert.match(text, /不是文件地址[^。；]*自由点路径/)
  assert.match(text, /model_generated\/translate_only[^。；]*分组[^。；]*读取内容[^。；]*适用条件[^。；]*作用范围/)
  assert.match(text, /server_locked\/passthrough[^。；]*自动跟随 source_selector/)
  assert.match(text, /h3 六段[^。；]*declarative_v1/)
  assert.match(text, /主编排[^。；]*不复制模型专属语法/)
  assert.match(text, /实际运行读取并冻结本次选择的 profile/)
  assert.match(text, /project\.style[^。；]*target_shot\.core[^。；]*target_shot\.dialogue_metadata[^。；]*target_shot\.linked_bible/)
  assert.match(text, /不把已确认台词[^。]*字段生成模型[^。]*服务器确定性投影\/(?:拼装|装配)[^。]*最终 <d>/)
  assert.match(text, /source_projection_key[^。；]*direct_v1[^。；]*declarative_v1/)
  assert.match(text, /positive_prompt[^。；]*7000[^。；]*6200/)
  assert.match(text, /detailed_description[^。；]*350–500/)
  assert.match(text, /新模型[^。；]*schema v6/)
  assert.match(text, /不在生产主编排添加模型分支/)
  assert.match(text, /现有原语无法表达[^。；]*(?:代码开发|修改代码注册)/)
  assert.match(text, /配置不能执行任意代码|不能下放为可执行配置/)
})

test('manual treats Provider modality and operation fields as operational notes only', () => {
  const provider = productionManual.steps.find((step) => step.id === 'configure-providers')
  assert.ok(provider)
  const text = searchableManualText(provider)
  assert.match(text, /支持模态[^。；]*声明操作[^。；]*说明、展示和人工审核用的运营标记/)
  assert.match(text, /不参与服务端执行授权、能力兼容性校验或候选筛选/)
})

test('manual treats chat request options as a generic controlled model contract', () => {
  const provider = productionManual.steps.find((step) => step.id === 'configure-providers')
  const gates = productionManual.steps.find((step) => step.id === 'handle-run-gates')
  assert.ok(provider && gates)
  const providerText = searchableManualText(provider)
  const gateText = searchableManualText(gates)
  for (const marker of [
    'chat_request_options', 'current published model version', 'operation-scoped contract',
    'max_input_bytes', 'max_output_tokens',
    'script.generate', 'script.structure', 'prompt.normalize', 'visual_rules.generate',
  ]) assert.ok(providerText.includes(marker), marker)
  assert.match(providerText, /四项必填硬容量[^]*chat_request_options/)
  assert.match(providerText, /temperature[^]*response_format/)
  for (const reserved of ['model', 'messages', 'max_tokens', 'stream', 'seed', 'timeout_seconds']) {
    assert.ok(providerText.includes(reserved), reserved)
  }
  assert.match(providerText, /不得包含凭据/)
  assert.match(providerText, /字节和结构上限[^。；]*当前安全配置/)
  assert.match(providerText, /本次选择的已发布 modelprofileversion[^。；]*按 operation 读取并冻结/)
  assert.match(providerText, /不要复制退役版本或旧验收案例的数值/)
  assert.match(providerText, /主逻辑[^。；]*不按 h3、deepseek 或版本号分支/)
  assert.match(providerText, /支持模态[^。；]*声明操作[^。；]*说明、展示和人工审核用的运营标记/)
  assert.match(providerText, /不参与服务端执行授权、能力兼容性校验或候选筛选/)
  assert.match(gateText, /chat_request_options[^]*不消费字段重试[^]*不降级为人工填写/)
  assert.match(gateText, /provider_response_invalid[^]*prompt_normalize_contract_failed[^]*known_response_rejected[^]*耗尽后进入人工/)
  assert.match(gateText, /production\.prompt_field_retry_limit[^]*(?:当前安全边界|安全边界)/)
  assert.match(gateText, /retry_feedback[^]*不是配置项[^]*服务端[^]*length_out_of_range/)
  assert.match(gateText, /只读显示[^]*(?:下一 attempt|是否使用)[^]*hash/)
  assert.match(gateText, /不显示模型原文|不显示[^]*反馈正文/)
})

test('manual explains Deployment and ComfyUI asset route eligibility and UI recovery', () => {
  const provider = productionManual.steps.find((step) => step.id === 'configure-providers')
  const assetLibrary = productionManual.steps.find((step) => step.id === 'manage-asset-library')
  const unavailable = productionManual.incidents.find((incident) => incident.id === 'deployment-unavailable')
  assert.ok(provider)
  assert.ok(assetLibrary)
  assert.ok(unavailable)

  const providerText = searchableManualText(provider)
  const assetText = searchableManualText(assetLibrary)
  const incidentText = searchableIncidentText(unavailable)
  for (const [scope, text] of [
    ['provider', providerText],
    ['asset library', assetText],
    ['deployment incident', incidentText],
  ] as const) {
    for (const fragment of [
      'script.generate',
      'async_rest',
      'image.text_to_image',
      'video.text_to_video',
      'audio.text_to_audio',
      'comfyui workflow',
    ]) assert.ok(text.includes(fragment), `${scope} needs ${fragment}`)
  }

  assert.ok(providerText.includes('系统设置 → 模型'))
  assert.ok(providerText.includes('模型卡片内'))
  assert.ok(incidentText.includes('对应模型版本 → 运行实例 → 详情与证据'))
  assert.ok(assetText.includes('风格素材库 → 新增素材 → 模型生成'))
  assert.match(assetText, /外发授权.*预算上限/)
  assert.match(assetText, /uncertain.*禁止普通重试/)
  assert.match(incidentText, /真实请求可能产生费用/)
  assert.match(incidentText, /管理员消歧.*新烟测.*禁止普通重试/)
  assert.match(providerText, /fresh script\.generate smoke/)
  assert.match(assetText, /import → live validate → real workflow smoke → publish/)
  assert.match(assetText, /submission token.*受控输出前缀/)
  assert.match(incidentText, /current\/published.*持久 save 主输出/)
  assert.match(incidentText, /deployment\/comfyui workflow/)
  assert.doesNotMatch(
    `${providerText} ${assetText} ${incidentText}`,
    /只接受 async_rest|不会进入独立素材|独立素材下拉的正常边界/,
  )
})

test('manual treats Ref2VA video as a full-content optional contract input', () => {
  const text = productionManual.steps.map(searchableManualText).join(' ')
  assert.match(text, /原生 ref2va 会读取完整视频/)
  assert.match(text, /purpose[^。；]*不会把内容隔离成纯动作或纯运镜信号/)
  assert.match(text, /同故事[^。；]*人物、场景、道具不冲突/)
  assert.match(text, /h3 动态参考拓扑[^。；]*版本化验收配置案例/)
  assert.match(text, /实际操作必须读取本次选择并冻结的 workflow/)
  assert.match(text, /bible 图片与视频 workflow[^。；]*(?:分别|各自)/)
  assert.doesNotMatch(text, /h3 workflow v5|z-image-turbo workflow v4/)
  assert.doesNotMatch(text, /动态参考视频只能提供动作与运镜条件/)
})

test('manual keeps the new dynamic Ref2VA three-lane acceptance operator-owned', () => {
  const step = productionManual.steps.find((item) => item.id === 'manual-ref2va-three-lane-acceptance')
  assert.ok(step)
  const text = searchableManualText(step)
  for (const fragment of [
    '纯人工', '半自动', '无人值守', '三个全新的独立项目',
    'live validate', 'real workflow smoke', 'classified_references_last_video_audio',
    'script.generate', 'script.structure', 'prompt.normalize', 'visual_rules.generate',
    'motion_camera_reference', 'reference_video_count >= 1', 'workflow_smoke fixture',
    'recipe_policy', 'human approval=0', 'episode master', 'sealed releasebundle',
    '/settings?section=workflows', '/assets', '/projects/new', '/workflows',
    'visual_rules_review', 'before_rules_sha256', 'after_rules_sha256',
    '继续 / 检查阶段', 'ffprobe', 'codec_name', 'signatureverified=true', 'liveverified=true',
    '0127', 'required_dimension_multiple', '新不可变版本',
  ]) assert.ok(text.includes(fragment), fragment)
  assert.match(text, /纯人工 → 半自动 → 无人值守[^。；]*依次执行/)
  assert.match(text, /三个新 run[^。]*两个 h3 task[^。；]*同一生产动态参考视频/)
  assert.match(text, /v22\/v23[^。；]*零动态视频[^。；]*不能替代/)
  assert.match(text, /代理或自动化检查不启动服务[^。；]*不代跑真实模型/)
  assert.match(text, /uncertain[^。；]*禁止普通重试/)
  assert.match(text, /workflow_smoke fixture[^。；]*(?:不得混用|禁止混用|绝不能)/)
  assert.match(text, /完整 stage\/route 合同[^。；]*不猜高级 json/)
})

test('incident matrix covers every operator-visible blocked or recovery state', () => {
  const ids = new Set(productionManual.incidents.map((incident) => incident.id))
  for (const id of [
    'migration-pending',
    'workflow-needs-review',
    'deployment-unavailable',
    'upload-contract-missing',
    'provider-authorization',
    'cost-blocked',
    'script-review-drift',
    'bible-review-conflict',
    'prompt-profile-source-projection-invalid',
    'remote-uncertain',
    'artifact-archive-failed',
    'episode-master-incomplete',
    'release-bundle-invalid',
    'config-pending-activation',
    'recommendation-not-applied',
    'no-real-e2e-evidence',
  ]) assert.ok(ids.has(id), id)

  assert.equal(ids.size, productionManual.incidents.length)
  for (const incident of productionManual.incidents) {
    assert.ok(incident.symptom.trim())
    assert.ok(incident.likelyCause.trim())
    assert.ok(incident.actions.length)
    assert.ok(incident.confirmation.trim())
    assert.ok(searchableIncidentText(incident).includes(incident.symptom.toLocaleLowerCase('zh-CN')))
  }
})

test('manual keeps credentials opaque and does not revive retired production routes', () => {
  const text = JSON.stringify(productionManual).toLocaleLowerCase('zh-CN')
  assert.ok(text.includes('credential_id'))
  assert.ok(!text.includes('credential_ref'))
  assert.ok(text.includes('不回显'))
  for (const fragment of [
    'build-production',
    '/prompts/generate',
    '/workflows/generate',
    '生成全部提示词',
    '保存并重新生成',
    '重新生成本镜头',
    '整季单条长视频作为成功',
    '/series-concept-drafts/generate/',
  ]) assert.equal(text.includes(fragment), false, fragment)
})

test('manual search includes boundary error codes and recovery actions', () => {
  const upload = productionManual.incidents.find((incident) => incident.id === 'upload-contract-missing')
  const uncertain = productionManual.incidents.find((incident) => incident.id === 'remote-uncertain')
  assert.ok(upload && searchableIncidentText(upload).includes('provider_upload_contract_missing'))
  assert.ok(uncertain && searchableIncidentText(uncertain).includes('resolve-uncertain'))
})
