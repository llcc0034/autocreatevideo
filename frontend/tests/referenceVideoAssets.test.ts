import assert from 'node:assert/strict'
import test from 'node:test'

import {
  isProductionReferenceAsset,
  isWorkflowSmokeFixtureAsset,
  REFERENCE_VIDEO_CONTENT_SAFETY_NOTICE,
  referenceVideoOptionLabel,
  referenceVideoSourceFps,
  referenceVideoUsableForOwner,
} from '../src/services/referenceVideoAssets.ts'
import type { Asset } from '../src/types/index.ts'

const video = (overrides: Partial<Asset> = {}): Asset => ({
  id: 'video-1',
  styleId: '',
  type: 'video',
  name: '运镜参考',
  description: '',
  tags: [],
  ownerId: '7',
  isActive: true,
  backendKind: 'reference_video',
  mimeType: 'video/mp4',
  checksum: 'a'.repeat(64),
  metadata: { duration_ms: 4_000, source_fps: 24 },
  ...overrides,
})

test('uploaded reference video uses the upload contract source_fps', () => {
  const asset = video()
  assert.equal(referenceVideoSourceFps(asset), 24)
  assert.equal(referenceVideoUsableForOwner(asset, '7'), true)
  assert.equal(referenceVideoOptionLabel(asset), '运镜参考 · 4.0s / 24fps · aaaaaaaaaa')
})

test('unknown source fps is allowed only when selected preprocessing does not require it', () => {
  const asset = video({ metadata: { duration_ms: 4_000, fps: 24 } })
  assert.equal(referenceVideoSourceFps(asset), 0)
  assert.equal(referenceVideoUsableForOwner(asset, '7'), true)
  assert.equal(referenceVideoUsableForOwner(asset, '7', {
    adapterKey: 'fictional_requires_fps_v1',
    minimumDurationMs: 1_000,
    maximumDurationMs: 9_000,
    targetFps: 30,
    requiresSourceFps: true,
  }), false)
})

test('production selector only exposes current-owner or global videos', () => {
  assert.equal(referenceVideoUsableForOwner(video({ ownerId: '7' }), '7'), true)
  assert.equal(referenceVideoUsableForOwner(video({ ownerId: null }), '7'), true)
  assert.equal(referenceVideoUsableForOwner(video({ ownerId: '1' }), '7'), false)
  assert.equal(referenceVideoUsableForOwner(video({ ownerId: '7' }), ''), false)
})

test('production selector excludes workflow smoke fixtures and explains full-video influence', () => {
  const fixture = video({
    ownerId: null,
    metadata: {
      duration_ms: 4_000,
      source_fps: 24,
      fixture_scope: 'workflow_smoke',
    },
  })
  assert.equal(isWorkflowSmokeFixtureAsset(fixture), true)
  assert.equal(isProductionReferenceAsset(fixture), false)
  assert.equal(referenceVideoUsableForOwner(fixture, '7'), false)
  assert.match(REFERENCE_VIDEO_CONTENT_SAFETY_NOTICE, /Ref2VA 会读取完整视频/)
  assert.match(REFERENCE_VIDEO_CONTENT_SAFETY_NOTICE, /不是模型级内容隔离/)
  assert.match(REFERENCE_VIDEO_CONTENT_SAFETY_NOTICE, /人物、场景、道具不冲突/)
})
