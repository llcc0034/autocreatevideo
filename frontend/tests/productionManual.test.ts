import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { productionManual, searchableIncidentText, searchableManualText } from '../src/content/productionManual.ts'

const requiredStepIds = [
  'create-project', 'write-series-and-episode', 'build-bible', 'qwen-creative-draft',
  'review-translations', 'edit-shots', 'choose-h3-mode', 'submit-production',
  'monitor-tasks', 'revise-single-shot', 'assemble-episode', 'finish-series',
]

test('manual exposes one versioned, ordered end-to-end production path', () => {
  assert.match(productionManual.version, /^\d{4}\.\d{2}\.\d{2}\.\d+$/)
  assert.equal(productionManual.steps.length, requiredStepIds.length)
  assert.deepEqual(productionManual.steps.map((step) => step.id), requiredStepIds)
  assert.deepEqual(productionManual.steps.map((step) => step.number), requiredStepIds.map((_, index) => index + 1))
  assert.equal(new Set(productionManual.steps.map((step) => step.id)).size, productionManual.steps.length)
})

test('every production step tells the operator input, buttons, success and next step', () => {
  for (const step of productionManual.steps) {
    assert.ok(step.location.trim(), `${step.id} needs a location`)
    assert.ok(step.inputs.length, `${step.id} needs an input example`)
    assert.ok(step.inputs.every((item) => item.label && item.example && item.note), `${step.id} has an incomplete input`)
    assert.ok(step.actions.length, `${step.id} needs exact actions`)
    assert.ok(step.success.length, `${step.id} needs success evidence`)
    assert.ok(step.next.trim(), `${step.id} needs a next step`)
    assert.ok(searchableManualText(step).includes(step.title.toLocaleLowerCase('zh-CN')))
  }
})

test('project creation starts from the managed list and redo remains an isolated draft', () => {
  const step = productionManual.steps.find((item) => item.id === 'create-project')
  assert.ok(step)
  const text = searchableManualText(step)
  for (const term of ['项目列表', '新建连续剧', '基于此项目重做', '不复制 bible、剧集、任务或产物', '不会自动触发 gpu']) {
    assert.ok(text.includes(term.toLocaleLowerCase('zh-CN')), `project management guidance missing ${term}`)
  }
})

test('H3 instructions cover text, optional first/last frames and sparse Ref2VA inputs', () => {
  const step = productionManual.steps.find((item) => item.id === 'choose-h3-mode')
  assert.ok(step)
  const text = searchableManualText(step)
  for (const term of ['三类输入模式', 't2va', 'i2va', 'l2va', 'fl2va', 'ref2va', '0–9', '0–3', '均可为空']) assert.ok(text.includes(term), `missing ${term}`)
  assert.ok(!step.title.includes('三类 MiniMax H3 工作流'))
})

test('production instructions cover selectable Ref2VA variants and scoped parameters', () => {
  const step = productionManual.steps.find((item) => item.id === 'submit-production')
  assert.ok(step)
  const text = searchableManualText(step)
  for (const term of ['t8 dual-clock 4-step', 'turbo lora', '固定 20 steps', 'steps 4', 'steps 10', '1–20', 'lora 1.0', '0–2.2', '实验', '只应用到 ref2va', '能力预检', '不代表 gpu 端到端生成通过', '目标环境', 'gpu 全链', '稳定性', '容量', '画质验收']) {
    assert.ok(text.includes(term.toLocaleLowerCase('zh-CN')), `workflow selection missing ${term}`)
  }
  for (const term of ['production_queue_confirmation_required', 'extended_generation_confirmed=true', '取消时不会创建任何任务', '完整保留所有逐镜任务', '不是项目集数、单集镜数或任务总量上限', '不会提高 worker 并发']) {
    assert.ok(text.includes(term.toLocaleLowerCase('zh-CN')), `queue confirmation guidance missing ${term}`)
  }
  for (const term of ['只保存、校验和提交当前镜头', '不会因其他镜头的问题而失败', '先保存并校验整集全部镜头']) {
    assert.ok(text.includes(term.toLocaleLowerCase('zh-CN')), `production scope guidance missing ${term}`)
  }
})

test('task retry keeps the materialized workflow while new generation exposes selection', () => {
  const step = productionManual.steps.find((item) => item.id === 'monitor-tasks')
  assert.ok(step)
  const text = searchableManualText(step)
  for (const term of ['重试复用原任务的实际工作流与参数', '修改并重新生成此镜头', 'steps', 'lora 强度', 'production_queue_confirmation_required', '确认只继续排队', '不会改变原参数']) {
    assert.ok(text.includes(term.toLocaleLowerCase('zh-CN')), `retry semantics missing ${term}`)
  }
})

test('incident matrix is unique and always includes a recovery confirmation', () => {
  assert.ok(productionManual.incidents.length >= 12)
  assert.equal(new Set(productionManual.incidents.map((incident) => incident.id)).size, productionManual.incidents.length)
  for (const incident of productionManual.incidents) {
    assert.ok(incident.symptom.trim())
    assert.ok(incident.likelyCause.trim())
    assert.ok(incident.actions.length)
    assert.ok(incident.confirmation.trim())
    assert.ok(searchableIncidentText(incident).includes(incident.symptom.toLocaleLowerCase('zh-CN')))
  }
})

test('translation recovery distinguishes project scope and never accepts failed output', () => {
  const translation = productionManual.incidents.find((item) => item.id === 'translation-failed-or-overwritten')
  assert.ok(translation)
  const text = searchableIncidentText(translation)
  for (const term of ['project', 'character', 'scene', 'prop', '重新翻译', '不会用失败输出覆盖原文']) assert.ok(text.includes(term.toLocaleLowerCase('zh-CN')), `missing ${term}`)
  assert.ok(productionManual.incidents.some((item) => item.id === 'residual-cjk'))
  assert.ok(productionManual.incidents.some((item) => item.id === 'qwen-resource-busy' && searchableIncidentText(item).includes('409')))
  const canonical = productionManual.incidents.find((item) => item.id === 'canonical-glossary-autofill-failed')
  assert.ok(canonical)
  for (const term of ['bible 已保存', '补全 n 个缺失术语', '已有人工映射不会被自动生成覆盖']) {
    assert.ok(searchableIncidentText(canonical).includes(term), `canonical recovery missing ${term}`)
  }
  const canonicalCollision = productionManual.incidents.find((item) => item.id === 'canonical-target-collision')
  assert.ok(canonicalCollision)
  for (const term of ['canonical_target_collision', 'nfkc', '人工', '重新翻译生成候选', '一键同步已确认英文', '不能自动猜测']) {
    assert.ok(searchableIncidentText(canonicalCollision).includes(term.toLocaleLowerCase('zh-CN')), `canonical collision recovery missing ${term}`)
  }
  const reviewStep = productionManual.steps.find((item) => item.id === 'review-translations')
  assert.ok(reviewStep)
  for (const term of ['修正为唯一 canonical', '相关译文随后显示失效', '重新翻译生成候选', '确认译文', '一键同步已确认英文']) {
    assert.ok(searchableManualText(reviewStep).includes(term.toLocaleLowerCase('zh-CN')), `canonical review workflow missing ${term}`)
  }
  const sourceChanged = productionManual.incidents.find((item) => item.id === 'translation-source-changed')
  assert.ok(sourceChanged)
  for (const term of ['已失效（只读）', '不会提供确认旧译文', '重新翻译生成候选', '一键翻译待处理', '已确认并默认折叠']) {
    assert.ok(searchableIncidentText(sourceChanged).includes(term.toLocaleLowerCase('zh-CN')), `source-changed recovery missing ${term}`)
  }
})

test('project-level translation review is separate from Bible entity review', () => {
  const projectStep = productionManual.steps.find((item) => item.id === 'write-series-and-episode')
  const bibleStep = productionManual.steps.find((item) => item.id === 'build-bible')
  const reviewStep = productionManual.steps.find((item) => item.id === 'review-translations')
  assert.ok(projectStep)
  assert.ok(bibleStep)
  assert.ok(reviewStep)

  const projectText = searchableManualText(projectStep)
  const bibleText = searchableManualText(bibleStep)
  const reviewText = searchableManualText(reviewStep)
  for (const term of ['项目总览', '全剧视觉规则', '保存所有更改', 'project.production_settings.color_script', 'project.continuity_summary', '采用并保存三项规则']) {
    assert.ok(projectText.includes(term.toLocaleLowerCase('zh-CN')), `project review missing ${term}`)
  }
  assert.ok(reviewText.includes('项目总览'))
  assert.ok(reviewText.includes('bible'))
  for (const term of ['上方“译文人工审核”', '下方分镜表单', '原子同步', '镜头标题与对白正文保持中文', '已确认条目折叠成摘要', '源文已变更时不匹配的英文只读且没有确认入口']) {
    assert.ok(reviewText.includes(term.toLocaleLowerCase('zh-CN')), `translation review UX guidance missing ${term}`)
  }
  assert.ok(bibleText.includes('只显示当前实体范围'))
  assert.ok(bibleText.includes('项目级全剧字段与 bible 实体字段已分开审核'))
  assert.ok(!bibleText.includes('同时汇总'))

  const studioSource = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  const projectFieldsStart = studioSource.indexOf('const projectTranslationFields')
  const bibleFieldsStart = studioSource.indexOf('const bibleTranslationFields')
  const totalsStart = studioSource.indexOf('const totalShots')
  assert.ok(projectFieldsStart >= 0 && bibleFieldsStart > projectFieldsStart && totalsStart > bibleFieldsStart)
  const projectFieldsSource = studioSource.slice(projectFieldsStart, bibleFieldsStart)
  const bibleFieldsSource = studioSource.slice(bibleFieldsStart, totalsStart)
  for (const fieldKey of ['style.visual_prompt', 'project.production_settings.color_script', 'project.continuity_summary']) {
    assert.ok(projectFieldsSource.includes(fieldKey), `project field missing ${fieldKey}`)
    assert.ok(!bibleFieldsSource.includes(fieldKey), `Bible fields must not include ${fieldKey}`)
  }
  assert.ok(studioSource.includes('项目级手工中文字段 · 本地 Qwen 译文人工审核'))
  assert.ok(studioSource.includes('色彩脚本与连续性摘要是两个独立字段'))
})

test('shot translation promotion and negative recovery are operationally explicit', () => {
  const reviewStep = productionManual.steps.find((item) => item.id === 'review-translations')
  const incident = productionManual.incidents.find((item) => item.id === 'negative-prompt-recovery')
  assert.ok(reviewStep)
  assert.ok(incident)
  const reviewText = searchableManualText(reviewStep)
  for (const term of ['确认译文并同步正式字段', '一键同步已确认英文到正式字段', '对白正文', '撤销译文确认', '不会回滚', '一键重新生成当前提示词']) {
    assert.ok(reviewText.includes(term.toLocaleLowerCase('zh-CN')), `promotion manual missing ${term}`)
  }
  const incidentText = searchableIncidentText(incident)
  for (const term of ['审计并恢复负面约束', 'applied', '逐字匹配', '不会清空人工负面词', 'prompt artifact']) {
    assert.ok(incidentText.includes(term.toLocaleLowerCase('zh-CN')), `negative recovery missing ${term}`)
  }
})

test('single-shot and episode high-detail expansion document review, atomic application and recovery', () => {
  const editStep = productionManual.steps.find((item) => item.id === 'edit-shots')
  assert.ok(editStep)
  const text = searchableManualText(editStep)
  for (const term of [
    '逐镜高细节扩写', '生成高细节候选', '保存并校验', '确认候选',
    '采用已审中英', '采用中文，转上方翻译审核', 'shot.detailed_description',
    '整集扩写', '整集应用', '原子应用', 'capability policy', 'Ref2VA', '上下文已变化', '资源暂时占用',
    '只保存并校验当前镜头', '其他镜头的问题不会阻断', '生成全部提示词', '保存并校验整集',
  ]) assert.ok(text.includes(term.toLocaleLowerCase('zh-CN')), `high-detail manual missing ${term}`)

  for (const incidentId of [
    'shot-detail-draft-source-changed', 'shot-detail-draft-resource-busy',
    'shot-detail-draft-english-review', 'shot-detail-draft-length-guidance',
  ]) assert.ok(productionManual.incidents.some((item) => item.id === incidentId), `missing ${incidentId}`)

  const storyboardSource = readFileSync(new URL('../src/views/StoryboardView.vue', import.meta.url), 'utf8')
  const panelSource = readFileSync(new URL('../src/components/ShotDetailDraftPanel.vue', import.meta.url), 'utf8')
  const modalSource = readFileSync(new URL('../src/components/EpisodeDetailDraftModal.vue', import.meta.url), 'utf8')
  const comparisonSource = readFileSync(new URL('../src/components/ShotDetailDraftComparison.vue', import.meta.url), 'utf8')
  assert.ok(storyboardSource.includes('<ShotDetailDraftPanel'))
  assert.ok(storyboardSource.includes('@applied="applyShotDetailDraft"'))
  assert.ok(storyboardSource.includes('shot.detailed_description'))
  assert.ok(panelSource.includes('requestSequence === loadSequence'))
  assert.ok(panelSource.includes('采用已审中英'))
  assert.ok(panelSource.includes('采用中文，转上方翻译审核'))
  assert.ok(comparisonSource.includes('扩写前'))
  assert.ok(comparisonSource.includes('扩写后'))
  assert.ok(modalSource.includes('原子应用整集'))
})

test('global visual rules use an isolated English candidate and fingerprinted apply', () => {
  const step = productionManual.steps.find((item) => item.id === 'write-series-and-episode')
  assert.ok(step)
  const text = searchableManualText(step)
  for (const term of ['生成英文候选', '当前正式值', '确定性保护项', '本地 qwen 只提议结构化色彩设计', '三段英文文本全部只读', '1–3', '采用并保存三项规则', '故事指纹', '不会读取剧集剧本或分镜', '不会生成或覆盖 canonical 术语表']) {
    assert.ok(text.includes(term.toLocaleLowerCase('zh-CN')), `global visual rules guidance missing ${term}`)
  }
  const incident = productionManual.incidents.find((item) => item.id === 'visual-rules-draft-failed')
  assert.ok(incident)
  const incidentText = searchableIncidentText(incident)
  for (const term of ['visual_rules_resource_busy', 'visual_rules_source_conflict', 'visual_rules_runtime_unavailable', '放弃候选']) {
    assert.ok(incidentText.includes(term), `global visual incident missing ${term}`)
  }

  const studioSource = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  const componentSource = readFileSync(new URL('../src/components/GlobalVisualRulesDraft.vue', import.meta.url), 'utf8')
  const adapterSource = readFileSync(new URL('../src/services/backendAdapters.ts', import.meta.url), 'utf8')
  const draftServiceSource = readFileSync(new URL('../src/services/visualRulesDraft.ts', import.meta.url), 'utf8')
  assert.ok(studioSource.includes('<GlobalVisualRulesDraft'))
  assert.ok(studioSource.includes('v-model="project.continuitySummary"'))
  assert.ok(componentSource.includes('采用并保存三项规则'))
  assert.ok(componentSource.includes('服务端英文候选 · 只读'))
  assert.ok(componentSource.includes('STRUCTURED COLOR DESIGN'))
  assert.ok(componentSource.includes('visualRulesColorDesignSummary(colorDesign)'))
  assert.ok(componentSource.includes('colorDesign: colorDesign.value'))
  assert.ok(!componentSource.includes('draft: candidate.value'))
  assert.ok(componentSource.includes('repository.applyVisualRulesDraft'))
  assert.ok(adapterSource.includes('visualRulesProjectPersistenceFields(project)'))
  assert.ok(adapterSource.includes('continuity_summary: visualRules.continuitySummary'))
  assert.ok(draftServiceSource.includes("continuitySummary: project.continuitySummary || ''"))
  assert.ok(!adapterSource.includes('continuity_summary: visualRules.colorScript'))
  assert.ok(!adapterSource.includes('color_script || raw.continuity_summary'))
})

test('creative draft failure guidance treats zero shots as a retry record, never a savable draft', () => {
  const step = productionManual.steps.find((item) => item.id === 'qwen-creative-draft')
  const incident = productionManual.incidents.find((item) => item.id === 'creative-draft-zero-shots')
  assert.ok(step)
  assert.ok(incident)
  const stepText = searchableManualText(step)
  const incidentText = searchableIncidentText(incident)
  for (const term of ['生成失败 · 0 镜', '失败审计记录', '一键重试整稿', '超时不等于服务端已取消']) {
    assert.ok(stepText.includes(term.toLocaleLowerCase('zh-CN')), `creative step missing ${term}`)
  }
  for (const term of ['能力接口', '推荐镜数', '单批规划值', '页面不应写死秒数', '初次整稿与整稿重生成会沿剧本连续原文边界自动分批', '只有选镜重生成保留携带完整旧草稿的单次协议']) {
    assert.ok(stepText.includes(term.toLocaleLowerCase('zh-CN')), `creative timeout/capability guidance missing ${term}`)
  }
  for (const term of ['镜数 + 权威对白 + provider 输出余量', '取消不会发送请求', '不会静默压缩']) {
    assert.ok(stepText.includes(term.toLocaleLowerCase('zh-CN')), `creative capacity guidance missing ${term}`)
  }
  for (const term of ['读取能力接口中的环境配置建议', '超过建议只提示人工复核', '不截断编辑或丢弃草稿']) {
    assert.ok(stepText.includes(term.toLocaleLowerCase('zh-CN')), `creative field-limit guidance missing ${term}`)
  }
  for (const term of ['已生成 · 待人工修正', '跳到字段', '保存并重新检查', '已阅读并承担风险', '数据完整性问题', '正式应用前必须处理']) {
    assert.ok(stepText.includes(term.toLocaleLowerCase('zh-CN')), `creative review guidance missing ${term}`)
  }
  for (const term of ['不是可保存的空草稿', '一键重试整稿', '不显示保存、确认或应用空稿入口']) {
    assert.ok(incidentText.includes(term), `creative incident missing ${term}`)
  }
  for (const term of ['能力接口', '环境配置', '页面当前显示的等待上限', '检查 Django 服务连接']) {
    assert.ok(incidentText.includes(term.toLocaleLowerCase('zh-CN')), `creative incident timeout guidance missing ${term}`)
  }

  const extendedIncident = productionManual.incidents.find((item) => item.id === 'creative-extended-generation-confirmation')
  assert.ok(extendedIncident)
  const extendedText = searchableIncidentText(extendedIncident)
  for (const term of ['extended_generation_confirmed=true', 'creative_generation_confirmation_required', '不表示任务必然失败', '保留原目标镜数', 'batch_shots']) {
    assert.ok(extendedText.includes(term.toLocaleLowerCase('zh-CN')), `extended generation guidance missing ${term}`)
  }

  const budgetIncident = productionManual.incidents.find((item) => item.id === 'creative-output-budget')
  assert.ok(budgetIncident)
  const budgetText = searchableIncidentText(budgetIncident)
  for (const term of ['creative_batch_planning_failed', 'creative_output_budget_exceeded', 'deepseek_output_budget_exceeded', 'creative_output_budget_exhausted', 'creative_repair_not_started', '初次/整稿重生成批次规划', '选镜重生成', '缩短“本次创作要求”', '不会写入正式分镜']) {
    assert.ok(budgetText.includes(term.toLocaleLowerCase('zh-CN')), `creative budget recovery missing ${term}`)
  }
})

test('Bible creation documents advisory visual-anchor language and candidate review', () => {
  const bibleStep = productionManual.steps.find((item) => item.id === 'build-bible')
  assert.ok(bibleStep)
  const text = searchableManualText(bibleStep)
  for (const term of ['中文视觉锚点', '全中文或全英文', '不做素材库硬拦截', '确认字段并创建设定', '中文原文不会被覆盖', '不会自动确认', '名称 canonical 与视觉锚点候选分别报告']) {
    assert.ok(text.includes(term.toLocaleLowerCase('zh-CN')), `Bible translation guidance missing ${term}`)
  }
  const incident = productionManual.incidents.find((item) => item.id === 'bible-visual-candidate-failed')
  assert.ok(incident)
  assert.ok(searchableIncidentText(incident).includes('名称 canonical'))
  assert.ok(searchableIncidentText(incident).includes('确认译文'))

  const studioSource = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  const panelSource = readFileSync(new URL('../src/components/TranslationReviewPanel.vue', import.meta.url), 'utf8')
  assert.ok(studioSource.includes('ensureVisualIdentityTranslation: true'))
  assert.ok(studioSource.includes(':refresh-key="translationReviewRefreshKey"'))
  assert.ok(studioSource.includes('视觉锚点（中文或英文）'))
  assert.ok(!studioSource.includes('placeholder="Use English: immutable appearance'))
  assert.ok(panelSource.includes('watch(() => props.refreshKey, load)'))
  assert.ok(panelSource.includes('const requestSequence = loadGate.begin()'))
  assert.ok(panelSource.includes('if (!loadGate.isCurrent(requestSequence)) return'))
})

test('asset library page route is not swallowed by the development asset proxy', () => {
  const viteConfig = readFileSync(new URL('../vite.config.ts', import.meta.url), 'utf8')
  assert.ok(viteConfig.includes("'/assets/library'"), 'dev proxy should expose only the raw reference library prefix')
  assert.ok(!viteConfig.includes("'/assets':"), 'dev proxy must not capture the SPA /assets page route')
})

test('Bible quick creation is single-reference metadata-driven and protects manual edits', () => {
  const bibleStep = productionManual.steps.find((item) => item.id === 'build-bible')
  assert.ok(bibleStep)
  const text = searchableManualText(bibleStep)
  for (const term of ['素材元数据', '单张参考图', '不识别图片像素', '自动补充三个字段', '人工编辑不覆盖', 'checksum']) {
    assert.ok(text.includes(term.toLocaleLowerCase('zh-CN')), `Bible asset draft guidance missing ${term}`)
  }
  const incident = productionManual.incidents.find((item) => item.id === 'bible-asset-draft-failed')
  assert.ok(incident)
  assert.ok(searchableIncidentText(incident).includes('草稿依据已变化'))

  const studioSource = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  const assetSource = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')
  const adapterSource = readFileSync(new URL('../src/services/backendAdapters.ts', import.meta.url), 'utf8')
  const repositorySource = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  assert.ok(studioSource.includes('repository.getAssets({})'))
  assert.ok(studioSource.includes("reference_mode:'single_reference'"))
  assert.ok(studioSource.includes('pixel_vision_used:false'))
  assert.ok(studioSource.includes('asset_draft_provenance'))
  assert.ok(studioSource.includes('asset_metadata_sha256:sourceSnapshot.metadataSha256'))
  assert.ok(studioSource.includes('source_metadata_sha256:bibleDraftGenerateResult.value.sourceMetadataSha256'))
  assert.ok(studioSource.includes('所选素材缺少最新校验快照'))
  assert.ok(studioSource.includes('applyBibleDraftCandidateField'))
  assert.ok(studioSource.includes('切换到“${asset.name}”会用该素材已保存'))
  assert.ok(assetSource.includes('Bible 快速创建只读取这些已保存元数据，不进行图片像素识别'))
  assert.ok(assetSource.includes("asset.ownerId&&(auth.user?.isStaff"))
  assert.ok(adapterSource.includes("description: raw.description || '', promptHint:String(raw.prompt_hint||'')"))
  assert.ok(adapterSource.includes("metadataSha256:String(raw.metadata_sha256||'')"))
  assert.ok(repositorySource.includes("rows.filter((asset)=>asset.type===params.type)"))
  assert.ok(!repositorySource.includes("kind: params?.type"))
})

test('runtime limits are bound to authenticated capability endpoints', () => {
  assert.equal(productionManual.capabilityEndpoints.creative, '/capabilities/creative-drafts/')
  assert.equal(productionManual.capabilityEndpoints.translation, '/capabilities/prompt-translation/')
  const manualView = readFileSync(new URL('../src/views/ProductionManualView.vue', import.meta.url), 'utf8')
  assert.ok(manualView.includes('adaptPromptProviders'))
  assert.ok(manualView.includes('adapted.defaultProvider'))
  assert.ok(manualView.includes('baselineMaxNewTokens'))
  assert.ok(manualView.includes('configuredMaxNewTokens'))
  assert.ok(manualView.includes('单批 ${batch} 镜'))
  assert.ok(!manualView.includes('max_shots'))
  assert.ok(!manualView.includes('|| 4'))
})
