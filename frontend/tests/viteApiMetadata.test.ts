import assert from 'node:assert/strict'
import { createServer as createHttpServer } from 'node:http'
import { once } from 'node:events'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, loadConfigFromFile } from 'vite'

test('development proxy preserves authenticated OPTIONS metadata and API failures', async () => {
  const loaded = await loadConfigFromFile({ command: 'serve', mode: 'development' },
    fileURLToPath(new URL('../vite.config.ts', import.meta.url)))
  assert.ok(loaded)
  const requests: Array<{ method: string | undefined; url: string | undefined }> = []
  const upstream = createHttpServer((request, response) => {
    requests.push({ method: request.method, url: request.url })
    response.setHeader('Content-Type', 'application/json')
    if (request.headers.authorization !== 'Bearer isolated-fixture') {
      response.writeHead(401).end(JSON.stringify({ detail: 'Authentication required' }))
      return
    }
    response.end(JSON.stringify({ actions: { POST: { role: { choices: [
      { value: 'primary', display_name: '主图' }, { value: 'variant', display_name: '变体' },
    ] } } } }))
  })
  upstream.listen(0, '127.0.0.1')
  await once(upstream, 'listening')
  const address = upstream.address()
  assert.ok(address && typeof address !== 'string')
  const vite = await createServer({
    configFile: false, envFile: false, appType: 'custom', logLevel: 'silent',
    root: fileURLToPath(new URL('..', import.meta.url)),
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, ws: false, watch: null,
      cors: loaded.config.server?.cors,
      proxy: { '/api': { target: `http://127.0.0.1:${address.port}`, changeOrigin: true } },
    },
  })
  const frontend = createHttpServer(vite.middlewares)
  try {
    frontend.listen(0, '127.0.0.1')
    await once(frontend, 'listening')
    const front = frontend.address()
    assert.ok(front && typeof front !== 'string')
    const url = `http://127.0.0.1:${front.port}/api/v1/asset-reference-series/`
    const metadata = await fetch(url, { method: 'OPTIONS', headers: {
      Authorization: 'Bearer isolated-fixture', Origin: 'http://127.0.0.1:5173',
    } })
    assert.equal(metadata.status, 200, 'OPTIONS must reach Django, not end as an empty CORS 204')
    assert.deepEqual((await metadata.json()).actions.POST.role.choices.map(row => row.value), ['primary', 'variant'])
    assert.equal(metadata.headers.get('access-control-allow-origin'), 'http://127.0.0.1:5173')
    const denied = await fetch(url, { method: 'OPTIONS', headers: { Origin: 'https://untrusted.invalid' } })
    assert.equal(denied.status, 401, 'The proxy must not hide API authentication errors')
    assert.equal(denied.headers.get('access-control-allow-origin'), null, 'Do not widen Vite origin permissions')
    assert.equal((await denied.json()).detail, 'Authentication required')
    assert.deepEqual(requests, [
      { method: 'OPTIONS', url: '/api/v1/asset-reference-series/' },
      { method: 'OPTIONS', url: '/api/v1/asset-reference-series/' },
    ])
  } finally {
    frontend.closeAllConnections()
    await new Promise<void>(resolve => frontend.close(() => resolve()))
    await vite.close()
    upstream.closeAllConnections()
    await new Promise<void>(resolve => upstream.close(() => resolve()))
  }
})
