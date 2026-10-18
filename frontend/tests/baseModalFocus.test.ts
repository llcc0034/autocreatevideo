import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('preserved modal content is hidden and inert until reopened; other modals still unmount', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { default: Modal } = await vite.ssrLoadModule('/src/components/BaseModal.vue')
  async function markup(preserveContent: boolean) {
    const ctx: any = {}
    await renderToString(createSSRApp({render:()=>h(Modal,{open:false,preserveContent},{default:()=>h('textarea','unfinished local JSON')})}),ctx)
    return ctx.teleports?.body || ''
  }
  const retained=await markup(true)
  assert.match(retained,/inert/);assert.match(retained,/aria-hidden="true"/);assert.match(retained,/display:none/)
  assert.match(retained,/unfinished local JSON/)
  assert.doesNotMatch(await markup(false),/unfinished local JSON/)
})

test('modal focus boundaries exclude hidden ancestors and retain dismissible protection', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  const previousDocument = globalThis.document, previousWindow = globalThis.window
  const doc: any = { activeElement: null }
  globalThis.document = doc
  globalThis.window = { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) } as any
  context.after(async () => { globalThis.document = previousDocument; globalThis.window = previousWindow; await vite.close() })
  const { default: Modal } = await vite.ssrLoadModule('/src/components/BaseModal.vue')
  const props = reactive({ open: false, dismissible: true })
  let state: any, closes = 0
  await renderToString(createSSRApp({ setup() {
    state = Modal.setup(props, { expose() {}, emit: (event: string) => { if (event === 'close') closes++ } })
    return () => h('div')
  } }))
  const element = (name: string, visible = true, hiddenAncestor = false) => ({
    name, getAttribute: () => null, getClientRects: () => visible ? [{}] : [],
    closest: () => hiddenAncestor ? {} : null,
    focus() { doc.activeElement = this },
  })
  const first = element('close'), last = element('last visible'), hidden = element('hidden tab', false), ariaHidden = element('hidden ancestor', true, true)
  let elements = [first, last, hidden, ariaHidden]
  const dialog = { querySelectorAll: () => elements, contains: (node: any) => elements.includes(node), focus() { doc.activeElement = dialog } }
  state.dialogRef.value = dialog
  const key = (key: string, shiftKey = false) => state.onKeydown({ key, shiftKey, preventDefault() {}, stopPropagation() {} })
  doc.activeElement = first; key('Tab', true)
  assert.equal(doc.activeElement, last, 'reverse Tab skips controls under v-show and aria-hidden ancestors')
  key('Tab'); assert.equal(doc.activeElement, first, 'forward Tab wraps from the actual visible last control')
  doc.activeElement = {}; key('Tab'); assert.equal(doc.activeElement, first)
  props.dismissible = false; key('Escape'); assert.equal(closes, 0)
  props.dismissible = true; key('Escape'); assert.equal(closes, 1)
  elements = [hidden, ariaHidden]; key('Tab'); assert.equal(doc.activeElement, dialog)
})
