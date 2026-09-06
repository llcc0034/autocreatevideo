import assert from 'node:assert/strict'
import test from 'node:test'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { settingsSectionCatalog } from '../src/content/settingsSectionCatalog.ts'
import {
  artifactRoleLabel, driverLabel, mediaKindLabel, modeLabel, operationLabel,
  reasonCodeLabel, reasonCodeTitle, scopeLabel, stageLabel, valueTypeLabel,
} from '../src/services/presentationLabels.ts'

const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
const guide = JSON.parse(readFileSync(new URL('../src/content/systemSettingsGuide.json', import.meta.url), 'utf8'))

test('system settings guide covers every real settings section with detailed novice actions', () => {
  assert.deepEqual(guide.sections.map((section: { id: string }) => section.id), settingsSectionCatalog.map((section) => section.id))
  assert.equal(guide.sections.length, settingsSectionCatalog.length)
  for (const section of guide.sections) {
    assert.ok(section.summary.length >= 20, `${section.id} needs a summary`)
    assert.ok(section.purpose.length >= 2, `${section.id} needs purpose details`)
    assert.ok(section.before.length >= 3, `${section.id} needs prerequisites`)
    assert.ok(section.steps.length >= 3, `${section.id} needs detailed steps`)
    assert.ok(section.steps.every((step: Record<string, string>) => step.title && step.action && step.expected && step.ifBlocked))
    assert.ok(section.fieldNotes.length >= 4, `${section.id} needs field explanations`)
    assert.ok(section.mistakes.length >= 2, `${section.id} needs beginner recovery advice`)
    assert.match(section.image, /^\/help\/system-settings\/.+\.jpg$/)
  }
})

test('component call graph is complete, ordered and links every dependency to a real node', () => {
  const stages = guide.componentFlow.stages as Array<{
    number: number
    title: string
    summary: string
    success: string
    next: string
    nodes: Array<{ id: string; label: string; english: string; detail: string; sectionId?: string; guideAnchor?: string }>
  }>
  assert.deepEqual(stages.map((stage) => stage.number), stages.map((_, index) => index + 1))
  assert.equal(stages.length, 6)

  const nodes = stages.flatMap((stage) => stage.nodes)
  const nodeIds = nodes.map((node) => node.id)
  assert.equal(new Set(nodeIds).size, nodeIds.length)
  const catalogIds = new Set(settingsSectionCatalog.map((section) => section.id))
  for (const node of nodes) {
    assert.match(node.id, /^[a-z][a-z0-9-]*$/)
    assert.ok(node.label && node.english && node.detail)
    if (node.sectionId) assert.ok(catalogIds.has(node.sectionId), `${node.id} has an unknown settings section`)
    else assert.match(node.guideAnchor || '', /^settings-(real-case|troubleshooting)$/)
  }
  for (const sectionId of catalogIds) {
    assert.ok(nodes.some((node) => node.sectionId === sectionId), `${sectionId} is missing from the component graph`)
  }

  const edges = guide.componentFlow.edges as Array<{ from: string; to: string; label: string; optional: boolean }>
  const stageByNode = new Map(stages.flatMap((stage) => stage.nodes.map((node) => [node.id, stage.number])))
  for (const edge of edges) {
    assert.ok(nodeIds.includes(edge.from), `${edge.from} source node is missing`)
    assert.ok(nodeIds.includes(edge.to), `${edge.to} target node is missing`)
    assert.notEqual(edge.from, edge.to)
    assert.ok(edge.label)
    assert.ok((stageByNode.get(edge.from) || 0) <= (stageByNode.get(edge.to) || 0), `${edge.from} -> ${edge.to} goes backward`)
  }
  const adjacency = new Map(nodeIds.map((id) => [id, edges.filter((edge) => edge.from === id).map((edge) => edge.to)]))
  const visiting = new Set<string>()
  const visited = new Set<string>()
  const visit = (id: string) => {
    assert.equal(visiting.has(id), false, `component graph contains a cycle at ${id}`)
    if (visited.has(id)) return
    visiting.add(id)
    for (const target of adjacency.get(id) || []) visit(target)
    visiting.delete(id)
    visited.add(id)
  }
  for (const id of nodeIds) visit(id)
  const relations = new Set(edges.map((edge) => `${edge.from}->${edge.to}`))
  for (const relation of [
    'provider->model-profile', 'model-profile->deployment', 'deployment->workflow',
    'workflow->recipe', 'recipe->readiness', 'readiness->production-run',
    'security-boundaries->config-revision', 'security-boundaries->readiness', 'security-boundaries->production-run',
    'production-run->execution', 'execution->layer-one-facts',
    'layer-one-facts->layer-two-fields', 'layer-two-fields->prompt-validation',
    'prompt-validation->human-confirmation',
    'layer-one-facts->source-projection', 'prompt-validation->source-projection',
    'human-confirmation->source-projection', 'source-projection->prompt-profile-assembly',
    'prompt-profile-assembly->video-generation', 'video-generation->delivery',
  ]) assert.ok(relations.has(relation), relation)

  const deployment = nodes.find((node) => node.id === 'deployment')!
  assert.match(deployment.detail, /Probe 查健康能力，Smoke 提交最小真实调用/)
  assert.match(JSON.stringify(guide.componentFlow), /Readiness 是汇总检查结果，不是执行器/)
})

test('security boundaries have a superuser-only novice path, fail-closed recovery and a real workflow example', () => {
  const securityCatalog = settingsSectionCatalog.find((section) => section.id === 'security')
  const securitySection = guide.sections.find((section: { id: string }) => section.id === 'security')
  assert.equal(securityCatalog?.writeRequiresSuperuser, true)
  assert.ok(securitySection)
  const text = JSON.stringify(securitySection)
  for (const marker of [
    'Security Boundaries', 'superuser', 'staff', 'configuration_class=security_boundary',
    'allowed_scopes=system', 'fail closed', '未知', '搜索', '已停用/退役',
    '0.2MP / 4 steps', '352×608', '参考图片/视频/音频数量',
  ]) assert.ok(text.includes(marker), marker)
  assert.match(JSON.stringify(guide.permissionNotes), /安全边界[^]*只读[^]*超级管理员/)
  assert.ok(guide.caseStudy.preflight.some((item: { sectionId: string }) => item.sectionId === 'security'))
  assert.match(JSON.stringify(guide.troubleshooting), /configuration_class[^。；]*缺失[^。；]*(?:未知|拼写错误)/)
})

test('production flow B is a complete bilingual sequence from empty project to ReleaseBundle', () => {
  const steps = guide.productionFlow.steps as Array<{
    number: number
    id: string
    label: string
    english: string
    detail: string
    sectionId?: string
    guideAnchor?: string
  }>
  assert.deepEqual(steps.map((step) => step.number), steps.map((_, index) => index + 1))
  assert.equal(steps.length, 14)
  assert.deepEqual(steps.map((step) => step.id), [
    'empty-project', 'ai-script', 'text-bible', 'bible-candidates', 'entity-anchors',
    'shot-bindings', 'prompt-field-generation', 'prompt-field-validation', 'prompt-human-confirmation',
    'prompt-compile', 'h3-ref2va', 'artifact-selection', 'assembly', 'release-bundle',
  ])
  const catalogIds = new Set(settingsSectionCatalog.map((section) => section.id))
  for (const step of steps) {
    assert.ok(step.label && step.english && step.detail)
    if (step.sectionId) assert.ok(catalogIds.has(step.sectionId), `${step.id} has an unknown settings section`)
    else assert.match(step.guideAnchor || '', /^settings-(real-case|troubleshooting)$/)
  }
  const text = JSON.stringify(guide.productionFlow)
  assert.match(text, /z-image-turbo Bible Candidates/)
  assert.match(text, /generation mode[^]*generation source[^]*required[^]*language[^]*length[^]*validators/)
  assert.match(text, /模型专属规则来自冻结 Profile 数据/)
  assert.match(text, /script_breakdown[^]*detailed_description[^]*sound_effects[^]*music/)
  assert.match(text, /原生 Ref2VA 会读取完整参考视频/)
  assert.match(text, /purpose[^。；]*不构成模型级内容隔离/)
  assert.match(text, /fixture_scope=workflow_smoke[^。；]*工作流版本验收页/)
  assert.match(text, /历史 0\.2MP 参数[^。；]*版本化验收案例[^。；]*不是[^。；]*默认值/)
  assert.match(text, /structured_shot_duration_contract/)
  assert.match(JSON.stringify(guide), /structured_shot_duration_contract[^]*1000\/1000\/1000ms/)
  assert.match(text, /已确认内容事实[^]*生成目标字段内容[^]*最终提示词(?:确定性)?装配/)
  assert.doesNotMatch(text, /(?:第一层|第二层|第三层|Layer-?\s*[123])/i)
  assert.match(text, /input_contract\.conditioning_contract\.modes\[\][^]*dynamic_reference_topology\.videos[^]*minimum_items=0[^]*maximum_items=0[^]*minimum_items\.\.maximum_items/)
  assert.match(text, /人工、辅助和全自动[^]*同一 0\.\.N 合同/)
  assert.doesNotMatch(text, /全自动首版每镜最多一条|minimum_items>1 当前不支持/)
  assert.doesNotMatch(text, /input_rules/)
  assert.match(text, /production\.prompt_field_retry_limit/)
  assert.match(text, /只重试失败批/)
  assert.match(text, /重试耗尽进入人工确认/)
  assert.match(text, /uncertain[^]*(?:不自动重投|不走(?:该)?自动重试)/)
  assert.match(text, /PromptProfile schema v6/)
})

test('prompt field retry policy is explained as frozen finite retry with narrow human fallback', () => {
  assert.equal(guide.version, '1.3.0')
  const text = JSON.stringify(guide)
  assert.match(text, /production\.prompt_field_retry_limit/)
  assert.match(text, /production\.prompt_language_gate/)
  assert.doesNotMatch(text, /production\.h3_prompt_language_gate/)
  assert.match(text, /0 表示首次[^。；]*(?:转人工|人工确认)/)
  assert.match(text, /security\.production\.max_automatic_retries/)
  assert.match(text, /总尝试数=1\+该值/)
  assert.match(text, /运行创建后冻结|ProductionRun 创建时冻结/)
  assert.match(text, /provider_response_invalid/)
  assert.match(text, /prompt_normalize_contract_failed/)
  assert.match(text, /唯一 KNOWN_RESPONSE_REJECTED/)
  assert.match(text, /uncertain[^。；]*(?:不自动重投|停止自动和人工重复提交|必须先消歧)/)
  assert.match(text, /Provider Smoke[^。；]*(?:硬阻断|失效)/)
  assert.match(text, /配置漂移[^。；]*(?:硬阻断|不走(?:该)?自动重试)/)
  assert.match(text, /pre-submit[^。；]*(?:不计入|门禁)/)
  assert.match(text, /目标合同漂移[^。；]*(?:不计入|按门禁处理|非重试)/)
  assert.match(text, /审计缺失[^。；]*(?:不计入|修复审计链|非重试)/)
  assert.match(text, /awaiting approval/)
  assert.match(text, /失败输出不会成为候选/)
  assert.match(text, /全部冻结字段的空白候选|全部空白冻结候选/)
  assert.match(text, /retry_feedback[^。；]*不是(?:管理员)?配置|retry_feedback 不是设置项/)
  assert.match(text, /reason_code[^。；]*validation_context[^。；]*request_index/)
  assert.match(text, /repair_of_attempt_id[^。；]*retry_feedback_sha256/)
  assert.match(text, /页面只读显示[^。；]*(?:关系|下一 attempt)[^。；]*hash/)
  assert.match(text, /不显示[^。；]*(?:模型原文|反馈正文)/)
})

test('guide documents operation-scoped chat options from the currently selected model version', () => {
  const text = JSON.stringify(guide)
  for (const marker of [
    'specification.prompt_normalization', 'specification.visual_rules', 'quality_advisories', 'recommended_negative_clauses', 'chat_request_options',
    '当前已选 prompt.normalize 合同', '当前已选 visual_rules.generate 合同',
    'max_input_bytes', 'max_output_tokens', 'timeout_seconds',
    'script.generate', 'script.structure', 'prompt.normalize', 'visual_rules.generate',
  ]) assert.ok(text.includes(marker), marker)
  assert.match(text, /prompt_normalization[^]*四项[^]*(?:可选|配置)[^]*chat_request_options/)
  assert.match(text, /visual_rules[^]*max_input_bytes[^]*max_output_tokens[^]*timeout_seconds[^]*chat_request_options/)
  assert.match(text, /temperature[^]*response_format/)
  for (const reserved of ['model', 'messages', 'max_tokens', 'stream', 'seed', 'timeout_seconds']) {
    assert.ok(text.includes(reserved), reserved)
  }
  assert.match(text, /不得包含凭据|不能包含凭据/)
  assert.match(text, /本次选择的已发布 ModelProfileVersion[^。；]*按 operation 读取/)
  assert.match(text, /max_input_bytes[^。；]*覆盖[^。；]*operation[^。；]*完整冻结输入/)
  assert.match(text, /当前已选 prompt\.normalize 合同[^}]*运行快照[^}]*不要复制退役版本/)
  assert.match(text, /当前已选 visual_rules\.generate 合同[^}]*按 operation 读取冻结规格/)
  assert.match(text, /quality_advisories[^。；]*(?:warning|warnings)[^。；]*advisory audit/)
  assert.match(text, /省略对象或字段[^。；]*不启用隐含建议/)
  assert.match(text, /recommended_negative_clauses[^。；]*(?:人工可移除|可人工移除)/)
  assert.match(text, /(?:不存在|不启用)[^。；]*代码默认清单/)
  assert.match(text, /数组类型[^。；]*允许枚举[^。；]*唯一性[^。；]*绝对 JSON\/请求资源上限[^。；]*硬阻断/)
  assert.match(text, /重跑[^]*真实 Smoke|重跑实时真实烟测/)
  assert.match(text, /建议(?:阈值|警告)[^。]*(?:warning|告警)[^。]*(?:不重试|不阻断|不消费失败重试)/)
  assert.match(text, /主逻辑[^。；]*按 operation[^。；]*不按模型名、版本号/)
  assert.doesNotMatch(text, /ModelProfileVersion v16|当前 v16/)
})

test('guide explains the generic configurable prompt contract without making H3 the orchestrator', () => {
  const promptSection = guide.sections.find((section: { id: string }) => section.id === 'prompts')
  assert.ok(promptSection)
  const text = JSON.stringify(promptSection)
  for (const field of [
    'source_projection_key', 'direct_v1', 'declarative_v1',
    'classified_reference_context_ir_v1', 'projection.output',
    'projection_primitive', 'reference_context',
    'field_definition.key', 'profile_field_key', 'generation_source_selector', 'generation_source_selectors',
    'generation_context_selectors', 'target_shot.dialogue_metadata', 'target_shot.dialogue_authored_text',
    'adjacent_shots.core', 'all_shots.core',
    'source_selector', 'generation_mode', 'generation_instruction',
    'generation_output_semantics', 'language_policy', 'length_unit',
    'generation_validator_keys', 'position', 'channel', 'label_style',
    'separator', 'max_chars',
  ]) assert.ok(text.includes(field), field)
  assert.match(text, /generation_source_selector[^。；]*主要内容来源/)
  assert.match(text, /generation_source_selectors[^。；]*(?:服务端|目录)/)
  assert.match(text, /不是文件地址[^。；]*(?:数据库位置|可手写表达式)/)
  assert.match(text, /读取什么[^。；]*什么时候可用[^。；]*作用范围/)
  assert.match(text, /generation_context_selectors[^。；]*(?:额外|上下文)/)
  assert.match(text, /dialogue_metadata[^。；]*不含对白(?:原文|正文)/)
  assert.match(text, /dialogue_authored_text[^。；]*(?:对白原文|台词)/)
  assert.match(text, /adjacent_shots\.core[^。；]*all_shots\.core[^。；]*显式/)
  assert.match(text, /空数组[^。；]*主要内容来源/)
  assert.match(text, /field_definition\.key\/profile_field_key[^。；]*(?:目标字段|destination)/)
  assert.match(text, /source_selector[^。；]*(?:最终提示词取值|最终拼装)/)
  assert.match(text, /final[^。]*目标字段/)
  assert.match(text, /contribution[^。]*(?:原语|投影)/)
  assert.match(text, /H3 六段[^。；]*(?:可复制|配置实例|配置案例)/)
  assert.match(text, /(?:主编排[^。；]*(?:不识别 H3|不在主编排)|不是主编排硬编码)/)
  assert.match(text, /纯配置接入[^。；]*复用[^。；]*已登记原语/)
  assert.match(text, /不修改 ProductionRun 主编排/)
  assert.match(text, /现有原语无法表达[^。；]*(?:新确定性转换|新增[^。；]*代码原语|需要代码)/)
  assert.match(text, /配置[^。；]*(?:不能|不)[^。；]*(?:执行脚本|执行代码|任意代码)/)
  assert.match(text, /Operation 首次登记/)
  assert.match(text, /options[^。；]*建议/)
  assert.match(text, /operation evidence missing/)
  assert.match(text, /非内置 operation[^。；]*active Binding[^。；]*精确[^。；]*模型/)
  assert.match(text, /没有 Deployment\/Workflow[^。；]*(?:fail closed|阻断)/)
  assert.match(text, /实时查看最终提示词[^]*当前未保存的完整候选快照[^。；]*各通道实际组合文本/)
  assert.match(text, /预览不保存示例[^。；]*不调用模型[^。；]*只执行[^。；]*最终排版/)
  assert.match(text, /字段生成[^。；]*声明式 H3 原语[^。；]*没有在此执行/)
  assert.match(text, /旧响应不得覆盖新配置|不要把上次有效预览当成当前结果/)
  assert.doesNotMatch(text, /(?:第二层|第三层|Layer-?\s*[23])/i)
  assert.doesNotMatch(text, /instruction 最多 4000|generation_instruction[^。；]*4000/)
})

test('provider declarations are bilingual operational notes and never server capability inputs', () => {
  const schema = readFileSync(new URL('../src/services/registryResourceSchema.ts', import.meta.url), 'utf8')
  assert.match(schema, /path: 'modalities'[^\n]*支持模态 \/ Supported modalities/)
  assert.match(schema, /path: 'operations'[^\n]*声明操作 \/ Declared operations/)
  assert.match(schema, /path: 'modalities'[^\n]*说明性运营标记[^\n]*不使用本字段做执行授权、能力兼容性校验或候选筛选[^\n]*注册驱动[^\n]*模型版本[^\n]*模型部署\/工作流合同[^\n]*真实烟测/)
  assert.match(schema, /path: 'operations'[^\n]*说明性运营标记[^\n]*不使用本字段做执行授权、能力兼容性校验或候选筛选[^\n]*注册驱动[^\n]*模型版本[^\n]*模型部署\/工作流合同[^\n]*真实烟测/)
  const guideText = JSON.stringify(guide)
  assert.match(guideText, /Supported modalities[^]*Declared operations/)
  assert.match(guideText, /只是说明、展示和人工审核用的运营标记[^。；]*不参与服务端执行授权、能力兼容性校验或候选筛选/)
  assert.match(guideText, /真实能力来自注册驱动 capabilities[^。；]*ModelProfileVersion[^。；]*Deployment\/Workflow[^。；]*新鲜真实 Smoke/)
  assert.match(guideText, /任意输入文本不会[^。；]*(?:增加驱动能力|注册驱动)/)
})

test('model artifact inventory selectors explain default folders and custom object_info pairs', () => {
  const text = JSON.stringify(guide)
  for (const marker of [
    'Inventory folder', 'Inventory node type', 'Inventory input name',
    'checkpoints', 'diffusion_models', 'text_encoders', 'vae', 'loras',
  ]) assert.ok(text.includes(marker), marker)
  assert.match(text, /标准角色[^。；]*全部留空[^。；]*按角色/)
  assert.match(text, /自定义[^。；]*inventory_folder[^。；]*inventory_node_type[^。；]*inventory_input_name[^。；]*成对/)
  assert.match(text, /已有探测任务[^。；]*等待[^。；]*不要重复点击/)
  assert.match(text, /几十 GB[^。；]*数分钟/)
})

test('conditioning, reference and execution-monitoring contracts have one novice H3 example', () => {
  const text = JSON.stringify(guide)
  for (const marker of [
    'input_contract.conditioning_contract', 'conditioning_contract.modes[]',
    'dynamic_reference_topology', 'default_purpose_key', 'pairing_rules',
    'mutual_exclusion_rules', 'preprocessing', 'conditioning_mode',
    'execution_monitoring', 'default_idle_timeout_seconds', 'node_type_overrides',
    'SamplerCustomAdvanced', '注册 projector', '页面只读证据',
  ]) assert.ok(text.includes(marker), marker)
  const workflowExamples = guide.sections
    .find((section: { id: string }) => section.id === 'workflows')
    ?.codeExamples || []
  assert.equal(workflowExamples.some((example: { code: string }) => example.code.includes('node_overrides')), false)
  assert.equal(workflowExamples.some((example: { code: string }) => example.code.includes('"10": 5400')), false)
  assert.match(text, /H3[^]*只读[^]*cfr_video_v1[^]*aligned_audio_v1[^]*passthrough_audio_v1/)
  assert.match(text, /条件输入合同（Conditioning Contract）/)
  assert.match(text, /动态参考拓扑（Dynamic Reference Topology）/)
  assert.doesNotMatch(text, /input_contract\.dynamic_reference_topology/)
})

test('guide records the completed Ref2VA dual-clock case without mixing fixture or historical evidence', () => {
  const text = JSON.stringify(guide)
  const caseText = JSON.stringify(guide.caseStudy)
  for (const fact of [
    '352×608', '0.2MP', '24fps', '4 steps', '22 frames / 917ms',
    '7c06c10b-4dd8-4a19-ac78-7921371bcde7',
    'aadbf9be-9a33-4507-a6d2-9629d5c3cfd0',
    'minimax_h3.video.ref2va',
    'minimax-h3-ref2va-dynamic-v3',
    '983f2cd4-37eb-4914-869e-1b7b467ca6ae',
    '17 个阶段', '2 个 Shot', '17/17 阶段成功', '实际费用 0.03 USD',
  ]) assert.ok(text.includes(fact), fact)
  assert.match(caseText, /Deployment UUID 不写入操作手册/)
  assert.doesNotMatch(caseText, /(?:H3|z-image-turbo) Deployment[： ]+[0-9a-f]{8}-/)
  assert.doesNotMatch(caseText, /minimax-h3-ref2va-official-v4/)
  assert.match(caseText, /PromptProfile v3/)
  assert.match(caseText, /f2cce7ef-ca4f-4928-98dd-d8586e8fbadc[^]*PromptProfile v4[^]*没有动态参考视频/)
  assert.match(caseText, /没有 production\.prompt_field_retry_limit/)
  assert.match(caseText, /没有 target_prompt_contract|请求没有 target_prompt_contract/)
  assert.match(caseText, /不能证明 v8、动态视频、容量分批和新逐字段合同的联合真实执行/)
  assert.match(text, /56 (?:帧|frames)[^。；]*单元测试 fixture/)
  assert.equal([...guide.caseStudy.facts, ...guide.caseStudy.result].some((item: string) => /56 帧/.test(item)), false)
  assert.equal(text.includes('待补齐'), false)
  assert.equal(guide.caseStudy.result.some((item: string) => /ReleaseBundle[^。；]*已 sealed/.test(item)), true)
  assert.equal(guide.caseStudy.result.some((item: string) => /总耗时约 19 分 27 秒/.test(item)), true)
  assert.equal(guide.caseStudy.result.some((item: string) => /实际费用 0\.03 USD/.test(item)), true)
  assert.match(text, /当前可视结构化编辑器没有 interaction_mode=unattended/)
  assert.match(text, /shot\.narrative 是[^。；]*上游镜头事实[^。；]*不能互相替代/)
  assert.match(caseText, /本案例结论以该 Run 持久化的 Stage、PromptArtifact、Artifact 和 ReleaseBundle 证据为准/)
})

test('guide keeps H3 as a configurable example and reads current frozen workflows', () => {
  const text = JSON.stringify(guide)
  assert.match(text, /H3 六段声明式配置案例[^]*schema v6/)
  assert.match(text, /实际运行读取本次选择并冻结的 Profile 版本/)
  assert.match(text, /实际操作必须分别读取本次选择并冻结的视频 Workflow 和 Bible 图片 Workflow/)
  assert.match(text, /0\.2MP 验收案例[^]*352×608 \/ 4 steps[^]*只证明对应的精确 Workflow/)
  assert.match(text, /detailed_description[^]*350–500[^]*(?:英文词|english_words)/)
  assert.match(text, /declarative_v1[^]*projection\.output[^]*reference_context\./)
  assert.match(text, /project\.style[^]*target_shot\.core[^]*target_shot\.dialogue_metadata[^]*target_shot\.linked_bible/)
  assert.match(text, /对白原文[^。]*服务器确定性(?:投影\/装配|投影或装配)/)
  assert.match(text, /purpose[^。；]*不构成模型级内容隔离/)
  assert.match(text, /同一故事|同故事/)
  assert.doesNotMatch(text, /动态参考视频只能提供动作与运镜|动态参考视频不承担剧情语义/)
  assert.doesNotMatch(text, /H3 Workflow v5|z-image-turbo Workflow v4|H3 PromptProfile v16/)
})

test('bilingual presenters keep raw machine values while adding Chinese explanations', () => {
  const examples = [
    [driverLabel('async_rest'), '受限异步接口', 'async_rest'],
    [mediaKindLabel('video'), '视频', 'video'],
    [artifactRoleLabel('diffusion_model'), '扩散模型', 'diffusion_model'],
    [artifactRoleLabel('episode_master'), '分集成片母版', 'episode_master'],
    [artifactRoleLabel('primary_generation_output'), '主生成产物', 'primary_generation_output'],
    [operationLabel('video.reference_to_video'), '参考生视频', 'video.reference_to_video'],
    [stageLabel('media_generation'), '媒体生成', 'media_generation'],
    [modeLabel('Ref2VA'), '参考生视频', 'Ref2VA'],
    [valueTypeLabel('boolean'), '布尔值', 'boolean'],
    [scopeLabel('system'), '系统级', 'system'],
    [reasonCodeLabel('published_recipe_missing'), '缺少已发布生产配方', 'published_recipe_missing'],
  ]
  for (const [actual, chinese, raw] of examples) {
    assert.match(actual, new RegExp(chinese))
    assert.match(actual, new RegExp(raw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
  }
  assert.equal(operationLabel('custom.operation'), '自定义操作（custom.operation）')

  const workflowImportReasons = [
    ['required_dimension_multiple_unresolved', '画面尺寸倍数约束无法证明'],
    ['resolution_execution_adapter_unavailable', '当前执行适配器不支持该尺寸策略'],
    ['unsupported_dynamic_workflow_family', '工作流动态结构尚无受审合同方案'],
    ['workflow_profile_projection_failed', '工作流与模型合同投影失败'],
    ['workflow_projector_contract_mismatch', '工作流合同方案与图结构不兼容'],
    ['workflow_projector_engine_unavailable', '工作流合同引擎不可用'],
  ] as const
  for (const [code, title] of workflowImportReasons) {
    assert.equal(reasonCodeTitle(code), title)
    assert.match(reasonCodeLabel(code), new RegExp(title))
    assert.match(reasonCodeLabel(code), new RegExp(code))
  }
  assert.equal(reasonCodeTitle('unknown_workflow_reason'), '未识别的阻断原因')
})

test('in-app guide route, settings deep links and generated printable guide stay discoverable', () => {
  const router = readFileSync(new URL('../src/router/index.ts', import.meta.url), 'utf8')
  const settingsView = readFileSync(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const manualView = readFileSync(new URL('../src/views/ProductionManualView.vue', import.meta.url), 'utf8')
  const guideView = readFileSync(new URL('../src/views/SystemSettingsGuideView.vue', import.meta.url), 'utf8')
  const markdown = readFileSync(new URL('../../docs/system-settings-beginner-guide.md', import.meta.url), 'utf8')
  const optimizationLedger = readFileSync(new URL('../../docs/optimization/README.md', import.meta.url), 'utf8')
  const assetConfiguration = readFileSync(new URL('../../docs/asset-generation-model-configuration.md', import.meta.url), 'utf8')
  assert.match(router, /path: 'manual\/settings'/)
  assert.match(settingsView, /当前分区图文指南/)
  assert.match(manualView, /系统设置新手图文指南/)
  assert.match(guideView, /systemSettingsGuide\.componentFlow\.stages/)
  assert.match(guideView, /systemSettingsGuide\.componentFlow\.edges/)
  assert.match(guideView, /systemSettingsGuide\.productionFlow\.steps/)
  assert.match(guideView, /<figure[^>]+settings-component-flow/)
  assert.match(guideView, /<figure[^>]+settings-production-flow/)
  assert.match(markdown, /正文唯一来源：`frontend\/src\/content\/systemSettingsGuide\.json`/)
  assert.match(markdown, /## 核心组件调用关系与推荐配置顺序/)
  assert.match(markdown, /```mermaid[\s\S]*flowchart TD[\s\S]*```/)
  for (const stage of guide.componentFlow.stages) {
    assert.ok(markdown.includes(stage.title), stage.title)
    for (const node of stage.nodes) assert.ok(markdown.includes(node.label), node.label)
  }
  assert.match(markdown, /## 关系图 B：从空项目到交付/)
  for (const step of guide.productionFlow.steps) {
    assert.ok(markdown.includes(step.label), step.label)
    assert.ok(markdown.includes(step.english), step.english)
  }
  assert.match(markdown, /## 历史案例：动态 Ref2VA 双时钟全自动短剧（PromptProfile v3）/)
  assert.match(markdown, /OPT-SETTINGS-001/)
  assert.match(optimizationLedger, /OPT-SETTINGS-001 Provider 与模型录入收敛为服务端选项目录/)
  assert.match(optimizationLedger, /\| `OPT-SETTINGS-001` \|[^\n]+\| 已完成 \|/)
  assert.match(optimizationLedger, /输入新值不会给驱动增加能力/)
  assert.doesNotMatch(assetConfiguration, /受控操作[^\n]+页面和烟测候选来源/)
  assert.match(assetConfiguration, /烟测候选来自模型版本 capability/)
})

test('every guide screenshot is a real JPEG public asset with a matching extension', () => {
  const images = [guide.overviewImage.image, ...guide.sections.map((section: { image: string }) => section.image), guide.caseStudy.image]
  assert.equal(new Set(images).size, settingsSectionCatalog.length + 2)
  for (const image of images) {
    const absolutePath = `${frontendRoot}/public${image}`
    assert.ok(existsSync(absolutePath), image)
    assert.deepEqual([...readFileSync(absolutePath).subarray(0, 3)], [0xff, 0xd8, 0xff], `${image} must be JPEG encoded`)
  }
})
