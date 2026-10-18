import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import test from 'node:test'

test('the release check discovers every frontend test, including newly added files', () => {
  const scripts = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).scripts
  assert.equal(scripts.test, 'node --test --test-concurrency=1 tests/*.test.ts')
  const discovered = readdirSync(new URL('.', import.meta.url)).filter(name => name.endsWith('.test.ts'))
  for (const critical of ['testDiscovery.test.ts', 'taskCenter.test.ts']) assert.ok(discovered.includes(critical), critical)
  const check = readFileSync(new URL('../../scripts/check.ps1', import.meta.url), 'utf8')
  assert.match(check, /& \$NpmPath run test\r?\n/)
  assert.match(check, /& \$NpmPath run check:settings-guide/)
  assert.doesNotMatch(check, /& \$NpmPath run test:/)
  assert.equal(scripts['check:settings-guide'], 'node scripts/render-system-settings-guide.mjs --check')
})
