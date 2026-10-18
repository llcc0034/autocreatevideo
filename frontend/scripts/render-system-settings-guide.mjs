import { readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const frontendDir = resolve(scriptDir, '..')
const projectDir = resolve(frontendDir, '..')
const sourcePath = resolve(frontendDir, 'src/content/systemSettingsGuide.json')
const outputPath = resolve(projectDir, 'docs/system-settings-beginner-guide.md')
const catalogPath = resolve(frontendDir, 'src/content/settingsSectionCatalog.ts')

const guide = JSON.parse(await readFile(sourcePath, 'utf8'))
const catalogSource = await readFile(catalogPath, 'utf8')
const catalog = new Map()
for (const match of catalogSource.matchAll(/id: '([^']+)',[\s\S]*?label: '([^']+)',[\s\S]*?englishLabel: '([^']+)',[\s\S]*?settingsRoute: '([^']+)',[\s\S]*?guideAnchor: '([^']+)'/g)) {
  catalog.set(match[1], { label: match[2], englishLabel: match[3], settingsRoute: match[4], guideAnchor: match[5] })
}

const lines = []
const line = (value = '') => lines.push(value)
const bullets = (items) => items.forEach((item) => line(`- ${item}`))
const numbered = (items) => items.forEach((item, index) => line(`${index + 1}. ${item}`))
const imagePath = (value) => `../frontend/public${value}`
const mermaidId = (value) => `node_${String(value).replace(/[^a-zA-Z0-9_]/g, '_')}`
const mermaidText = (value) => String(value)
  .replaceAll('&', '&amp;')
  .replaceAll('"', '&quot;')
  .replaceAll('|', '&#124;')
  .replace(/\r?\n/g, '<br/>')
const componentNodes = new Map(
  guide.componentFlow.stages.flatMap((stage) => stage.nodes).map((node) => [node.id, node]),
)

line('<!-- 此文件由 frontend/src/content/systemSettingsGuide.json 生成，请勿手工维护正文。 -->')
line(`# ${guide.title}`)
line()
line(`> ${guide.subtitle}`)
line()
line(`版本：${guide.version}　更新日期：${guide.updatedAt}　页面入口：\`/manual/settings\``)
line()
line('## 如何使用本指南')
line()
line('适用对象：')
line()
bullets(guide.audience)
line()
line('权限边界：')
line()
bullets(guide.permissionNotes)
line()
line('### 状态不是一回事')
line()
for (const item of guide.statusSemantics) line(`- **${item.label}**：${item.meaning}`)
line()
line(`![${guide.overviewImage.alt}](${imagePath(guide.overviewImage.image)})`)
line()
line(`_${guide.overviewImage.caption}_`)
line()
line('## 三分钟配置顺序')
line()
for (const item of guide.quickStart) line(`${item.number}. **${item.title}**：${item.detail}`)
line()
line('> 探测（Probe）只确认健康与能力；真实烟测（Smoke Test）会提交真实低成本请求并可能产生费用。两者不能互相替代。')
line()
line(`## ${guide.componentFlow.title}`)
line()
line(guide.componentFlow.description)
line()
guide.componentFlow.legend.forEach((item) => line(`- ${item}`))
line()
line('```mermaid')
line('flowchart TD')
for (const stage of guide.componentFlow.stages) {
  line(`  subgraph stage_${stage.number}["${String(stage.number).padStart(2, '0')}. ${mermaidText(stage.title)}"]`)
  line('    direction LR')
  for (const node of stage.nodes) {
    const optional = node.optional ? '<br/>按需组件' : '<br/>必需组件'
    line(`    ${mermaidId(node.id)}["${mermaidText(node.label)}<br/>${mermaidText(node.english)}${optional}"]`)
  }
  line('  end')
}
for (const edge of guide.componentFlow.edges) {
  const connector = edge.optional ? '-.->' : '-->'
  const label = edge.optional ? `按需：${edge.label}` : edge.label
  line(`  ${mermaidId(edge.from)} ${connector}|${mermaidText(label)}| ${mermaidId(edge.to)}`)
}
line('```')
line()
line(`_${guide.componentFlow.caption}_`)
line()
line('### 不支持关系图时，按阶段阅读')
for (const stage of guide.componentFlow.stages) {
  line()
  line(`#### ${stage.number}. ${stage.title}`)
  line()
  line(stage.summary)
  for (const node of stage.nodes) {
    line(`- **${node.label}（${node.english}）${node.optional ? '〔按需〕' : ''}**：${node.detail}`)
  }
  line(`- **完成标志**：${stage.success}`)
  line(`- **再继续**：${stage.next}`)
}
line()
line('### 关键调用关系')
line()
for (const edge of guide.componentFlow.edges) {
  const source = componentNodes.get(edge.from)
  const target = componentNodes.get(edge.to)
  line(`- **${source?.label || edge.from} → ${target?.label || edge.to}**${edge.optional ? '〔按需〕' : ''}：${edge.label}`)
}
line()
line('### 阅读提示')
line()
bullets(guide.componentFlow.notes)
line()
line(`## ${guide.productionFlow.title}`)
line()
line(guide.productionFlow.description)
line()
line('```mermaid')
line('flowchart TD')
for (const step of guide.productionFlow.steps) {
  line(`  ${mermaidId(`production_${step.id}`)}["${String(step.number).padStart(2, '0')}. ${mermaidText(step.label)}<br/>${mermaidText(step.english)}"]`)
}
for (let index = 0; index < guide.productionFlow.steps.length - 1; index += 1) {
  const current = guide.productionFlow.steps[index]
  const next = guide.productionFlow.steps[index + 1]
  line(`  ${mermaidId(`production_${current.id}`)} -->|读取已确认事实| ${mermaidId(`production_${next.id}`)}`)
}
line('```')
line()
line(`_${guide.productionFlow.caption}_`)
line()
line('### 不支持关系图时，按生产步骤阅读')
line()
for (const step of guide.productionFlow.steps) line(`${step.number}. **${step.label}（${step.english}）**：${step.detail}`)
line()
line('### 生产边界提醒')
line()
bullets(guide.productionFlow.notes)

for (const section of guide.sections) {
  const item = catalog.get(section.id)
  line()
  line(`## ${item?.label || section.id}（${item?.englishLabel || section.id}）`)
  line()
  line(section.summary)
  line()
  line(`设置深链：\`${item?.settingsRoute || `/settings?section=${section.id}`}\``)
  line()
  line(`![${section.imageAlt}](${imagePath(section.image)})`)
  line()
  line(`_${section.imageCaption}_`)
  line()
  line('### 这个部分具体做什么')
  line()
  bullets(section.purpose)
  line()
  line('### 操作前准备')
  line()
  bullets(section.before)
  line()
  line('### 逐步操作')
  for (const [index, step] of section.steps.entries()) {
    line()
    line(`#### ${index + 1}. ${step.title}`)
    line()
    line(`- **怎么做**：${step.action}`)
    line(`- **成功标志**：${step.expected}`)
    line(`- **卡住时**：${step.ifBlocked}`)
  }
  for (const example of section.codeExamples || []) {
    line()
    line(`### ${example.title}`)
    line()
    line(`\`\`\`${example.language || 'text'}`)
    line(example.code)
    line('```')
  }
  line()
  line('### 字段怎么理解')
  line()
  for (const note of section.fieldNotes) line(`- **${note.term}**：${note.meaning}`)
  line()
  line('### 新手最常见卡点')
  line()
  for (const mistake of section.mistakes) line(`- **${mistake.symptom}**：${mistake.fix}`)
}

line()
line(`## ${guide.caseStudy.title}`)
line()
line(guide.caseStudy.objective)
line()
line(`![${guide.caseStudy.imageAlt}](${imagePath(guide.caseStudy.image)})`)
line()
line(`_${guide.caseStudy.imageCaption}_`)
line()
line('### 已核实的固定事实')
line()
bullets(guide.caseStudy.facts)
line()
line('### 设置页预检')
for (const preflight of guide.caseStudy.preflight) {
  line()
  line(`#### ${preflight.title}`)
  line()
  bullets(preflight.checks)
}
line()
line('### 创建真实 Run')
line()
numbered(guide.caseStudy.runSteps)
line()
line('### 本轮验证状态（CURRENT VERIFICATION STATUS）')
line()
bullets(guide.caseStudy.result)
line()
line('### 质量与口径提示')
line()
bullets(guide.caseStudy.caveats)

line()
line('## 故障排查')
for (const issue of guide.troubleshooting) {
  line()
  line(`### ${issue.symptom}`)
  line()
  line(`常见原因：${issue.cause}`)
  line()
  numbered(issue.actions)
}

line()
line('## 最终检查单')
line()
guide.finalChecklist.forEach((item) => line(`- [ ] ${item}`))
line()
line('## 常用术语速查')
line()
for (const item of guide.glossary) line(`- **${item.chinese}（${item.english}）**：${item.meaning}`)
line()
line('---')
line()
line('待优化计划与当前能力分开维护。最新范围与决策见[待办复核清单](optimization/backlog-review-2026-09-28.md)；Provider 受控目录的已完成范围与历史证据见原台账 `OPT-SETTINGS-001`，不重复列为待开发。')
line()
line('正文唯一来源：`frontend/src/content/systemSettingsGuide.json`。网页路由 `/manual/settings` 与本 Markdown 使用同一份结构化数据。')
line()

const output = lines.join('\n')
if (process.argv.includes('--check')) {
  let current = ''
  try { current = await readFile(outputPath, 'utf8') } catch {}
  if (current !== output) {
    console.error('系统设置指南 Markdown 与结构化内容源不一致，请运行 npm run docs:settings-guide。')
    process.exitCode = 1
  } else {
    console.log('系统设置指南 Markdown 与结构化内容源一致。')
  }
} else {
  await writeFile(outputPath, output, 'utf8')
  console.log(`已生成 ${outputPath}`)
}
