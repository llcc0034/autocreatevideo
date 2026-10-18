import assert from 'node:assert/strict'
import test from 'node:test'
import { isolatedMaintenanceDestination } from './fixtures/exact-maintenance-window.ts'

const origin = 'http://127.0.0.1:5173'
const fixture = origin + '/tests/fixtures/execution-authoring.html?maintenance=1'
const route = '/settings/workflows/fixture-workflow'
test('isolated maintenance preserves exact original route and guarded document', () => {
  const result = new URL(isolatedMaintenanceDestination(origin + route, fixture, route))
  assert.equal(result.pathname, '/tests/fixtures/execution-authoring.html')
  assert.equal(result.searchParams.get('maintenance'), '1')
  assert.equal(result.searchParams.get('maintenance_route'), route)
})
test('isolated maintenance rejects another identity, origin, query or fragment', () => {
  for (const target of [origin + route + '-new', 'http://example.invalid' + route, origin + route + '?latest=1', origin + route + '#other']) {
    assert.throws(() => isolatedMaintenanceDestination(target, fixture, route))
  }
})
test('the explicitly declared relation context is preserved, never replaced by another resource', () => {
  const exact = route + '?relation_context=workflow%3Afixture-workflow'
  const result = new URL(isolatedMaintenanceDestination(origin + exact, fixture, exact))
  assert.equal(result.searchParams.get('maintenance_route'), exact)
  assert.throws(() => isolatedMaintenanceDestination(origin + route + '?relation_context=workflow%3Aother', fixture, exact))
})
test('asset-generation maintenance keeps the exact execution version and relation context together', () => {
  const exact = '/settings?section=executions&execution_version=20000000-0000-0000-0000-000000000002&relation_context=execution%3A20000000-0000-0000-0000-000000000002'
  const source = origin + '/tests/fixtures/asset-generation-authoring.html?maintenance=1'
  const result = new URL(isolatedMaintenanceDestination(origin + exact, source, exact))
  assert.equal(result.pathname, '/tests/fixtures/asset-generation-authoring.html')
  assert.equal(result.searchParams.get('maintenance_route'), exact)
  assert.throws(() => isolatedMaintenanceDestination(origin + exact.replace('execution_version=20000000', 'execution_version=90000000'), source, exact))
})
