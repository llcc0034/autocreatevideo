import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { parse } from '@vue/compiler-sfc'
import { parse as parseTemplate } from '@vue/compiler-dom'

const source = readFileSync(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8')
const template = parseTemplate(parse(source).descriptor.template!.content)
const element = (node: any, tag: string) => node.type === 1 && node.tag === tag
const expression = (node: any, name: string) => node.props?.find((prop: any) => prop.type === 7 && prop.name === name)?.exp?.content

test('creation recovery controls remain outside frozen fields and disclose possible creation', () => {
  for (const [markup, handler] of [[source, 'createRun'], [readFileSync(new URL('../src/components/ManualProductionCreateForm.vue', import.meta.url), 'utf8'), 'create']]) {
    const ast = parseTemplate(parse(markup).descriptor.template!.content)
    const buttons: any[] = []
    const walk = (node: any, parents: any[] = []) => {
      if (element(node, 'button') && expression(node, 'on') === handler) buttons.push({ node, parents })
      for (const child of node.children || []) walk(child, [...parents, node])
    }
    walk(ast)
    const recovery = buttons.find(({node}) => node.loc.source.includes('按原请求恢复创建'))
    assert.ok(recovery); assert.ok(!recovery.parents.some(node => element(node, 'fieldset')))
    assert.match(markup, /不是只读查询/)
    assert.match(markup, /<fieldset[^>]*:disabled="[^"]*creationRequest/)
    assert.match(markup, /不再创建/)
  }
})

test('adoption children are keyed to their exact run so late emits cannot target another run', () => {
  const found = new Map<string, string>()
  const walk = (node: any) => {
    if (['ManualProductionWorkbench', 'ProductionTextCandidateReview', 'GeneratedArtifactCompare'].includes(node.tag)) {
      found.set(node.tag, node.props.find(prop => prop.name === 'bind' && prop.arg?.content === 'key')?.exp?.content)
    }
    for (const child of node.children || []) walk(child)
  }
  walk(template)
  assert.equal(found.size, 3)
  for (const [tag, key] of found) assert.match(key, /currentRun\.id/, tag)
})

test('cancel confirmation is shared by recipe and manual branches', () => {
  const root: any = template.children.find(node => element(node, 'div'))
  const modals = root.children.filter((node: any) => element(node, 'BaseModal'))
  assert.equal(modals.filter((node: any) => node.props.some((prop: any) => prop.exp?.content === 'Boolean(pendingRunConfirmation)')).length, 1)
  assert.match(source, /platformApi\.cancelProductionRun\(pending\.runId\)/)
})

test('manual recovery selects an exact stage without a sequential current-stage assumption', () => {
  const root: any = template.children.find(node => element(node, 'div'))
  const manual = root.children.find((node: any) => expression(node, 'else-if') === "currentRun.planSource==='manual'")
  assert.ok(manual)
  const markup = manual.loc.source
  assert.match(markup, /v-for="stage in currentRun\.stages"/)
  assert.match(markup, /:value="stage\.id"/)
  assert.match(markup, /:stage-id="selectedManualPreparationStage\.id"/)
  assert.doesNotMatch(markup, /v-if="currentStage"/)
  assert.match(source, /stages\?\.find\(stage => stage\.id === manualPreparationStageId\.value\)/)
})

test('read-first receipt warning is shared and submit buttons are guarded without trapping dismissal', () => {
  const root: any = template.children.find(node => element(node, 'div'))
  const warning = root.children.find((node: any) => expression(node, 'if') === 'pendingRunReceipt')
  assert.ok(warning)
  assert.match(warning.loc.source, /原运行提交待核对/)
  assert.match(warning.loc.source, /pendingRunReceipt\.runId/)
  assert.match(warning.loc.source, /重新读取原运行（不重试）/)
  assert.match(warning.loc.source, /@click="recoverRunReceipt"/)
  const buttons: any[] = []
  const walk = (node: any) => { if (element(node, 'button')) buttons.push(node); for (const child of node.children || []) walk(child) }
  walk(root)
  for (const handler of ['confirmRunAction', 'confirmBibleRegeneration', 'submitUncertainResolution', 'saveManualBudget', 'acknowledgeActualCost', 'continueRun', 'decide', 'authorizeCost']) {
    const button = buttons.find(node => expression(node, 'on') === handler)
    const disabled = button?.props.find(prop => prop.type === 7 && prop.name === 'bind' && prop.arg?.content === 'disabled')
    assert.match(disabled?.exp?.content || '', /runActionBlocked/, handler)
  }
  for (const handler of ['closeRunConfirmation', 'closeBibleRegeneration', 'closeUncertainResolution']) {
    const button = buttons.find(node => expression(node, 'on') === handler)
    assert.ok(button); assert.doesNotMatch(button.loc.source, /runActionBlocked|pendingRunReceipt/)
  }
})
