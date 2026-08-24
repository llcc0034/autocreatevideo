import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

import { buildGeneratedImageAssetPromotionPayload } from '../src/services/outputAssetPromotion.ts'
import { adaptPromptArtifact } from '../src/services/promptArtifactAdapter.ts'
import { adaptTaskProgressTelemetry, workflowProgressPresentation } from '../src/services/taskProgress.ts'
import type { GeneratedImageAssetTarget } from '../src/types/index.ts'

test('image prompt keeps its text-only conditioning and SDXL template metadata', () => {
  const adapted = adaptPromptArtifact({
    prompt_type: 'image',
    model: 'sdxl-1.0',
    prompt_text: 'cinematic wuxia portrait',
    total_duration_ms: 5000,
    include_audio: true,
    structured_payload: {
      effective_conditioning_mode: 'TEXT_ONLY',
      prompt_template_key: 'sdxl_txt2img_bible_v1',
      duration_ms: 5000,
      reference_bindings: [{ asset_id: 'must-not-become-an-image-input' }],
      quality: {
        status: 'ready',
        language_policy: 'English image director prose',
        prompt_character_count: 26,
        prompt_word_count: 3,
        prompt_word_count_is_approximate: true,
        warnings: [],
        fields_requiring_english_rewrite: []
      }
    }
  })

  assert.equal(adapted.mediaType, 'image')
  assert.equal(adapted.effective_conditioning_mode, 'TEXT_ONLY')
  assert.equal(adapted.prompt_template_key, 'sdxl_txt2img_bible_v1')
  assert.equal(adapted.quality?.prompt_word_count, 3)
  assert.equal(adapted.duration, undefined)
  assert.equal(adapted.reference_bindings, undefined)
})

test('video prompt retains mode, duration and reference bindings', () => {
  const bindings = [{ asset_id: 'reference-1' }]
  const adapted = adaptPromptArtifact({
    prompt_type: 'video',
    h3_mode: 'Ref2VA',
    total_duration_ms: 5000,
    prompt_text: 'camera tracks the swordsman',
    structured_payload: {
      effective_conditioning_mode: 'Ref2VA',
      prompt_template_key: 'h3_r2v_classified_v1',
      reference_bindings: bindings
    }
  })

  assert.equal(adapted.mediaType, 'video')
  assert.equal(adapted.mode, 'Ref2VA')
  assert.equal(adapted.duration, 5)
  assert.deepEqual(adapted.reference_bindings, bindings)
})

test('Ref2VA first-frame continuity reference is serialized as a soft picture slot', async (context) => {
  const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
  const server = await createServer({
    root: frontendRoot,
    logLevel: 'silent',
    appType: 'custom',
    server: { middlewareMode: true, hmr: false },
  })
  context.after(async () => { await server.close() })
  const { referenceAssetIdsForShot, referenceBindingsPayload } = await server.ssrLoadModule('/src/services/backendAdapters.ts')
  const bindings = {
    firstFrameReferenceAssetId: 'asset-tail-frame',
    characters: [{ entityId: 'character-1', assetId: 'asset-character' }],
    props: [],
  }
  assert.deepEqual(referenceBindingsPayload(bindings), {
    first_frame_reference_asset_id: 'asset-tail-frame',
    characters: [{ entity_id: 'character-1', asset_id: 'asset-character' }],
  })
  const shot = {
    mode: 'Ref2VA',
    referenceBindings: bindings,
  }
  assert.deepEqual(
    referenceAssetIdsForShot(shot),
    ['asset-tail-frame', 'asset-character'],
  )
})

test('style adapters use only backend facts and preserve unknown server identifiers', async (context) => {
  const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
  const server = await createServer({
    root: frontendRoot,
    logLevel: 'silent',
    appType: 'custom',
    server: { middlewareMode: true, hmr: false },
  })
  context.after(async () => { await server.close() })
  const adapters = await server.ssrLoadModule('/src/services/backendAdapters.ts')
  const backendId = '11111111-1111-4111-8111-111111111111'
  const [style] = adapters.adaptStyles([{
    id: backendId,
    slug: 'wuxia',
    name: '服务端武侠',
    category: '服务端分类',
    description: '服务端说明',
    visual_prompt: 'server-owned visual prompt',
    asset_count: 7,
  }])

  assert.equal(style.name, '服务端武侠')
  assert.equal(style.description, '服务端说明')
  assert.equal(style.promptPrefix, 'server-owned visual prompt')
  assert.deepEqual(style.tags, ['服务端分类'])
  assert.equal(style.assetCount, 7)
  assert.equal(adapters.backendStyleId('wuxia'), backendId)
  assert.equal(adapters.styleSlug(backendId), 'wuxia')
  assert.equal(adapters.styleSlug('future-server-style'), 'future-server-style')
  assert.equal(adapters.styleSlug(), '')

  const project = adapters.adaptProjectSummary({
    id: 'project-1', title: '项目', style: backendId, production_settings: {},
  })
  assert.equal(project.styleId, 'wuxia')
  assert.equal(project.defaultMode, 'T2VA')
  assert.equal(project.bibleImagePromptConfig.stylePrompt, 'server-owned visual prompt')

  const payload = adapters.projectPayload({ title: '新项目' })
  assert.equal(payload.production_settings.default_h3_mode, 'T2VA')

  const [withoutCount] = adapters.adaptStyles([{
    id: '22222222-2222-4222-8222-222222222222',
    slug: 'server-only',
    name: '仅服务端风格',
  }])
  assert.equal(withoutCount.assetCount, null)
})

test('prompt artifact adapter keeps backend-authoritative compiler freshness metadata', () => {
  const adapted = adaptPromptArtifact({
    id: 'artifact-1', revision: 7, created_at: '2026-08-10T09:30:00Z',
    compiler_current: false, prompt_type: 'video', prompt_text: 'legacy prompt',
    structured_payload: {},
  })

  assert.equal(adapted.compiler_current, false)
  assert.equal(adapted.artifact_id, 'artifact-1')
  assert.equal(adapted.artifact_revision, 7)
  assert.equal(adapted.artifact_created_at, '2026-08-10T09:30:00Z')
})

test('generated image promotion payload keeps the optional Ref2VA entity target typed', () => {
  const target: GeneratedImageAssetTarget = {
    referenceCategory: 'character',
    entityId: '11111111-1111-4111-8111-111111111111'
  }
  assert.deepEqual(
    buildGeneratedImageAssetPromotionPayload(2, '  林默新定妆  ', target),
    {
      output_index: 2,
      name: '林默新定妆',
      reference_category: 'character',
      entity_id: target.entityId
    }
  )
  assert.deepEqual(buildGeneratedImageAssetPromotionPayload(0), { output_index: 0 })
})

test('legacy running progress never guesses that the submission marker is quantifiable', () => {
  const adapted = adaptTaskProgressTelemetry({
    progress: 10,
    submission_state: 'submitted',
    remote_state: 'running',
  }, 'running')

  assert.equal(adapted.progressSource, 'none')
  assert.equal(adapted.comfyProgress, undefined)
  assert.equal(adapted.remoteState, 'running')
  assert.equal(adapted.progressIndeterminate, true)
  const presentation = workflowProgressPresentation({
    status: 'running',
    submissionState: 'submitted',
    remoteState: adapted.remoteState,
    ...adapted,
  })
  assert.equal(presentation.indeterminate, true)
  assert.equal(presentation.percent, undefined)
  assert.match(presentation.summary, /尚未收到量化进度/)
  assert.doesNotMatch(presentation.valueLabel, /10%/)
})

test('submission progress is presented as initialization without a fake percentage', () => {
  const adapted = adaptTaskProgressTelemetry({
    progress_source: 'submission',
    progress_telemetry_state: 'connected',
    submission_state: 'submitted',
    remote_state: 'submitted',
  }, 'running')
  const presentation = workflowProgressPresentation({
    status: 'running',
    submissionState: 'submitted',
    ...adapted,
  })

  assert.equal(adapted.progressIndeterminate, true)
  assert.equal(presentation.mode, 'indeterminate')
  assert.equal(presentation.percent, undefined)
  assert.equal(presentation.compactLabel, '模型初始化')
  assert.equal(presentation.summary, '已提交，正在模型初始化')
})

test('ComfyUI progress uses only raw current-node telemetry and labels its scope', () => {
  const adapted = adaptTaskProgressTelemetry({
    progress: 62,
    progress_source: 'comfyui',
    comfy_progress: 37.6,
    progress_node_id: 'sampler-10',
    progress_updated_at: '2026-08-13T01:30:00+08:00',
    progress_telemetry_state: 'receiving',
    remote_state: 'running',
  }, 'running')
  const presentation = workflowProgressPresentation({ status: 'running', ...adapted })

  assert.equal(adapted.comfyProgress, 38)
  assert.equal(adapted.progressNodeId, 'sampler-10')
  assert.equal(adapted.progressUpdatedAt, '2026-08-13T01:30:00+08:00')
  assert.equal(adapted.progressTelemetryState, 'receiving')
  assert.equal(adapted.progressIndeterminate, false)
  assert.equal(presentation.mode, 'comfyui')
  assert.equal(presentation.percent, 38)
  assert.equal(presentation.valueLabel, '38%')
  assert.match(presentation.heading, /当前节点采样进度/)
  assert.match(presentation.note || '', /不等于整项任务完成度/)
  assert.doesNotMatch(presentation.valueLabel, /62/)
})

test('telemetry failure remains honest and keeps the generation running', () => {
  const adapted = adaptTaskProgressTelemetry({
    progress_source: 'submission',
    progress_telemetry_state: 'unavailable',
    progress_telemetry_error: 'WebSocket handshake failed',
  }, 'running')
  const presentation = workflowProgressPresentation({ status: 'running', ...adapted })

  assert.equal(adapted.progressTelemetryError, 'WebSocket handshake failed')
  assert.equal(presentation.indeterminate, true)
  assert.match(presentation.note || '', /任务仍会继续执行/)
  assert.match(presentation.note || '', /WebSocket handshake failed/)
})

test('terminal progress is always a determinate 100 percent', () => {
  const adapted = adaptTaskProgressTelemetry({
    progress_source: 'terminal',
    comfy_progress: null,
    progress_telemetry_state: 'connected',
  }, 'completed')
  const presentation = workflowProgressPresentation({ status: 'completed', ...adapted })

  assert.equal(adapted.progressSource, 'terminal')
  assert.equal(adapted.progressIndeterminate, false)
  assert.equal(presentation.mode, 'terminal')
  assert.equal(presentation.percent, 100)
  assert.equal(presentation.valueLabel, '100%')
})
