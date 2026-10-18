// Captured isolated Django API receipts, replayed ONLY in the Node host renderer.
// Confirm POSTs return declared receipts; no live backend or business writes.
import assert from 'node:assert/strict'
import { h } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import ExecutionPreparationWorkbench from '../../src/components/ExecutionPreparationWorkbench.vue'
import { api } from '../../src/services/api'
import captured from './m6-confirmation-journey-receipts.json'

export const receipts = captured
export function createFixture() {
  const stages = [captured.candidate, ...captured.confirmations.map(row => row.after)]
  const preparation = captured.candidate.preparation
  const base = `/execution-preparations/${preparation.id}/`
  let stage = 0
  const requests: any[] = [], denied: string[] = []
  const originalAdapter = api.defaults.adapter
  const deny = (message: string): never => { denied.push(message); throw Error(message) }
  function validate(config: any) {
    const allowedGet = config.url === base || ['prompt-display/', 'storage-metrics/'].some(s => config.url === base + s)
      || captured.fields.some(field => config.url === `/execution-field-values/${field.id}/`)
    if (config.method === 'get' && allowedGet) {
      assert.equal(config.data, undefined)
      if (config.url === base || config.url.startsWith('/execution-field-values/')) assert.equal(config.params, undefined)
      else assert.deepEqual(config.params, { expected_preparation_revision: stages[stage]!.preparation.revision })
      return
    }
    const field = captured.fields[stage]
    if (field && config.method === 'post' && config.url === `/execution-field-values/${field.id}/confirm/`) {
      assert.equal(config.params, undefined)
      assert.deepEqual(JSON.parse(config.data), { expected_text_sha256: field.text_sha256,
        expected_source_sha256: field.source_sha256, expected_preparation_revision: stages[stage]!.preparation.revision,
        select_for_execution: true, replace: false, expected_selection_revision: null })
      return
    }
    deny(`Undeclared ${config.method} ${config.url}`)
  }
  const defaultReply = (config: any): unknown => {
    if (config.method === 'post') return structuredClone(captured.confirmations[stage++]!.response)
    const current = stages[stage]!
    if (config.url === base) return structuredClone(current.preparation)
    if (config.url === base + 'prompt-display/') return structuredClone(current.prompt)
    if (config.url === base + 'storage-metrics/') return structuredClone(current.metrics)
    const index = captured.fields.findIndex(field => config.url === `/execution-field-values/${field.id}/`)
    assert.ok(index >= 0)
    return structuredClone(index < stage ? captured.confirmations[index]!.response.field : captured.fields[index])
  }
  let reply = defaultReply
  const adapter = async (config: any) => {
    try { validate(config) } catch (error) { denied.push(String(error)); throw error }
    requests.push({ method: config.method, url: config.url, params: structuredClone(config.params), data: config.data })
    return { config, data: await reply(config), status: 200, statusText: 'OK', headers: {} }
  }
  api.interceptors.request.clear()
  api.defaults.adapter = adapter
  const interceptor = api.interceptors.request.use(config => {
    config.adapter = adapter; config.headers.delete('Authorization'); return config
  })
  const pinia = createPinia()
  const Root = { setup: () => () => h(ExecutionPreparationWorkbench, { preparationId: preparation.id,
    versionId: preparation.execution_instance_version, purpose: preparation.purpose }) }
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/fixture/preparation', component: Root }] })
  return { Root, router, pinia, base, requests, denied, stages, defaultReply,
    setReply(value: typeof reply) { reply = value },
    restore() { api.defaults.adapter = originalAdapter; api.interceptors.request.eject(interceptor); assert.deepEqual(denied, []) },
  }
}
