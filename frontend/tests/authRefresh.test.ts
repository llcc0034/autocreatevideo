import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { AuthSessionChangedError, readAuthSession, refreshAuthSession } from '../src/services/authRefresh.ts'

const fixture = () => {
  const values = new Map<string, string>([
    ['cineforge_token', 'access-before'], ['cineforge_refresh_token', 'refresh-before'],
    ['cineforge_user', JSON.stringify({ id: 'account-a' })],
  ])
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value) }, removeItem: (key: string) => { values.delete(key) } }
  let queue = Promise.resolve()
  const withLock = (work: () => Promise<string>) => {
    const result = queue.then(work)
    queue = result.then(() => undefined, () => undefined)
    return result
  }
  let invalidations = 0
  return { storage, expired: readAuthSession(storage), withLock, isRejected: (e: unknown) => e === 'rejected', onInvalidated: () => { invalidations += 1 }, invalidations: () => invalidations }
}

test('concurrent tabs exchange a rotating refresh token exactly once', async () => {
  const f = fixture()
  let calls = 0
  const exchange = async (refresh: string) => {
    assert.equal(refresh, 'refresh-before')
    calls += 1
    await Promise.resolve()
    return { access: 'access-after', refresh: 'refresh-after' }
  }
  assert.deepEqual(await Promise.all(Array.from({ length: 5 }, () => refreshAuthSession({ ...f, exchange }))), Array(5).fill('access-after'))
  assert.equal(calls, 1)
  assert.equal(f.invalidations(), 0)
  assert.equal(readAuthSession(f.storage).refresh, 'refresh-after')
})

test('delayed 401 uses the already rotated access without another exchange', async () => {
  const f = fixture()
  f.storage.setItem('cineforge_token', 'access-after')
  f.storage.setItem('cineforge_refresh_token', 'refresh-after')
  assert.equal(await refreshAuthSession({ ...f, exchange: async () => { throw Error('must not exchange') } }), 'access-after')
})

test('a different account never receives an old request retry', async () => {
  const f = fixture()
  f.storage.setItem('cineforge_user', JSON.stringify({ id: 'account-b' }))
  f.storage.setItem('cineforge_token', 'access-b')
  await assert.rejects(refreshAuthSession({ ...f, exchange: async () => { throw Error('must not exchange') } }), AuthSessionChangedError)
  assert.equal(readAuthSession(f.storage).access, 'access-b')
  assert.equal(f.invalidations(), 0)
})

for (const failure of ['rejected', 'network', 'server']) {
  test(`refresh ${failure}: only explicit rejection invalidates the matching session`, async () => {
    const f = fixture()
    await assert.rejects(refreshAuthSession({ ...f, exchange: async () => { throw failure } }))
    assert.equal(f.invalidations(), failure === 'rejected' ? 1 : 0)
    assert.equal(Boolean(readAuthSession(f.storage).access), failure !== 'rejected')
  })
}

for (const outcome of ['success', 'rejected']) {
  test(`late refresh ${outcome} cannot overwrite or erase a new login`, async () => {
    const f = fixture()
    await assert.rejects(refreshAuthSession({ ...f, exchange: async () => {
      f.storage.setItem('cineforge_token', 'new-login')
      f.storage.setItem('cineforge_refresh_token', 'new-login-refresh')
      if (outcome === 'rejected') throw 'rejected'
      return { access: 'late-access', refresh: 'late-refresh' }
    } }))
    assert.equal(readAuthSession(f.storage).access, 'new-login')
    assert.equal(f.invalidations(), 0)
  })
}

test('malformed response does not replace usable stored credentials', async () => {
  const f = fixture()
  await assert.rejects(refreshAuthSession({ ...f, exchange: async () => ({ access: '' }) }), /响应不完整/)
  assert.equal(readAuthSession(f.storage).access, 'access-before')
  assert.equal(f.invalidations(), 0)
})

test('logout while queued does not resurrect the prior session', async () => {
  const f = fixture()
  f.storage.removeItem('cineforge_token')
  await assert.rejects(refreshAuthSession({ ...f, exchange: async () => ({ access: 'late' }) }), AuthSessionChangedError)
  assert.equal(readAuthSession(f.storage).access, '')
})

test('missing refresh invalidates once; absent identity cannot authorize replay', async () => {
  const f = fixture()
  f.storage.removeItem('cineforge_refresh_token')
  await assert.rejects(refreshAuthSession({ ...f, expired: readAuthSession(f.storage), exchange: async () => { throw Error('must not exchange') } }))
  assert.equal(f.invalidations(), 1)
  const other = fixture()
  other.storage.setItem('cineforge_user', 'invalid json')
  await assert.rejects(refreshAuthSession({ ...other, expired: readAuthSession(other.storage), exchange: async () => { throw Error('must not exchange') } }), AuthSessionChangedError)
})

test('API and navigation use one invalidation authority and bounded retry', () => {
  const api = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8')
  const router = readFileSync(new URL('../src/router/index.ts', import.meta.url), 'utf8')
  const auth = readFileSync(new URL('../src/stores/auth.ts', import.meta.url), 'utf8')
  assert.match(api, /navigator\.locks\.request\('cineforge:auth-refresh'/)
  assert.match(api, /!config\._cineforgeRetry/)
  assert.doesNotMatch(api, /let refreshRequest/)
  assert.match(router, /status === 401 && !localStorage\.getItem\('cineforge_token'\)/)
  assert.match(auth, /requestUserId !== session\.userId/)
})
