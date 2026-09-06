import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
const server = () => createServer({
  root: frontendRoot,
  logLevel: 'silent',
  appType: 'custom',
  server: { middlewareMode: true },
})
const sha = (letter: string) => letter.repeat(64)

test('workflow input draft preserves every scalar type and multiple fixed media slots', async () => {
  const vite = await server()
  try {
    const inputs = await vite.ssrLoadModule('/src/services/workflowExecutionInputs.ts')
    const schema = {
      schemaVersion: 2,
      available: true,
      reasonCode: '',
      parameters: [
        { semanticKey: 'prompt.positive', type: 'string', required: true, min: null, max: null, multipleOf: null, alignmentModulus: null, alignmentRemainder: null, minLength: 1, maxLength: 2000, options: [], selectionGroup: '', recommendedValue: '雨夜街道' },
        { semanticKey: 'sampling.seed', type: 'integer', required: true, min: 0, max: 9999, multipleOf: 1, alignmentModulus: null, alignmentRemainder: null, minLength: null, maxLength: null, options: [], selectionGroup: '', recommendedValue: 7 },
        { semanticKey: 'sampling.cfg_scale', type: 'number', required: true, min: 0, max: 20, multipleOf: 0.5, alignmentModulus: null, alignmentRemainder: null, minLength: null, maxLength: null, options: [], selectionGroup: '', recommendedValue: 6.5 },
        { semanticKey: 'video.frames', type: 'integer', required: true, min: 22, max: 3600, multipleOf: null, alignmentModulus: 17, alignmentRemainder: 5, minLength: null, maxLength: null, options: [], selectionGroup: '', recommendedValue: 56 },
        { semanticKey: 'audio.enabled', type: 'boolean', required: true, min: null, max: null, multipleOf: null, alignmentModulus: null, alignmentRemainder: null, minLength: null, maxLength: null, options: [], selectionGroup: '', recommendedValue: false },
        { semanticKey: 'sampling.scheduler', type: 'enum', required: true, min: null, max: null, multipleOf: null, alignmentModulus: null, alignmentRemainder: null, minLength: null, maxLength: null, options: ['normal', 'karras'], selectionGroup: '', recommendedValue: 'karras' },
        { semanticKey: 'prompt.negative', type: 'string', required: false, min: null, max: null, multipleOf: null, alignmentModulus: null, alignmentRemainder: null, minLength: null, maxLength: 500, options: [], selectionGroup: '', recommendedValue: null },
      ],
      assetSlots: [
        ['asset.image.first', 'image', true],
        ['asset.image.style', 'image', true],
        ['asset.image.character', 'image', true],
        ['asset.video.motion', 'video', false],
        ['asset.audio.voice', 'audio', true],
      ].map(([semanticKey, mediaKind, required], index) => ({
        semanticKey, mediaKind, required, inputSlot: index + 1,
        category: mediaKind, minItems: required ? 1 : 0, maxItems: 1,
      })),
      dynamicReferences: null,
      dynamicReferenceModes: {},
    }
    const assets = [
      { id: 'image-1', name: '首帧', type: 'image', mimeType: 'image/png', checksum: sha('a'), isActive: true },
      { id: 'image-2', name: '风格', type: 'image', mimeType: 'image/webp', checksum: sha('b'), isActive: true },
      { id: 'image-3', name: '人物', type: 'image', mimeType: 'image/jpeg', checksum: sha('c'), isActive: true },
      { id: 'video-1', name: '动作', type: 'video', mimeType: 'video/mp4', checksum: sha('d'), isActive: true },
      { id: 'audio-1', name: '声音', type: 'audio', mimeType: 'audio/wav', checksum: sha('e'), isActive: true },
    ]
    const draft = inputs.createWorkflowExecutionInputDraft(schema)
    draft.assetIds = {
      'asset.image.first': 'image-1',
      'asset.image.style': 'image-2',
      'asset.image.character': 'image-3',
      'asset.video.motion': 'video-1',
      'asset.audio.voice': 'audio-1',
    }
    assert.equal(inputs.validateWorkflowExecutionInputDraft(schema, draft, assets), '')
    const payload = inputs.buildWorkflowExecutionInput(schema, draft)
    assert.equal(payload.schemaVersion, 2)
    assert.deepEqual(payload.values, {
      'prompt.positive': '雨夜街道',
      'sampling.seed': 7,
      'sampling.cfg_scale': 6.5,
      'video.frames': 56,
      'audio.enabled': false,
      'sampling.scheduler': 'karras',
    })
    assert.deepEqual(payload.assets, [
      { semanticKey: 'asset.image.first', assetId: 'image-1' },
      { semanticKey: 'asset.image.style', assetId: 'image-2' },
      { semanticKey: 'asset.image.character', assetId: 'image-3' },
      { semanticKey: 'asset.video.motion', assetId: 'video-1' },
      { semanticKey: 'asset.audio.voice', assetId: 'audio-1' },
    ])
    draft.values['video.frames'] = 57
    assert.match(inputs.validateWorkflowExecutionInputDraft(schema, draft, assets), /5 \+ 17k/)
    draft.values['video.frames'] = 56
    draft.values['sampling.seed'] = 7.5
    assert.match(inputs.validateWorkflowExecutionInputDraft(schema, draft, assets), /必须是整数/)
  } finally {
    await vite.close()
  }
})

test('workflow input alignment uses the same normalized remainder as the server', async () => {
  const vite = await server()
  try {
    const inputs = await vite.ssrLoadModule('/src/services/workflowExecutionInputs.ts')
    const schema = {
      schemaVersion: 2, available: true, reasonCode: '', assetSlots: [],
      dynamicReferences: null, dynamicReferenceModes: {},
      parameters: [{
        semanticKey: 'video.frames', type: 'integer', required: true,
        min: -12, max: 90, multipleOf: null,
        alignmentModulus: 17, alignmentRemainder: 5,
        minLength: null, maxLength: null, options: [], selectionGroup: '',
        recommendedValue: -12,
      }],
    }
    const draft = inputs.createWorkflowExecutionInputDraft(schema)
    assert.equal(inputs.validateWorkflowExecutionInputDraft(schema, draft, []), '')
    draft.values['video.frames'] = -11
    assert.match(inputs.validateWorkflowExecutionInputDraft(schema, draft, []), /5 \+ 17k/)
  } finally {
    await vite.close()
  }
})

test('CONDITIONING_MODE selects its own dynamic video and paired-audio contract', async () => {
  const vite = await server()
  try {
    const inputs = await vite.ssrLoadModule('/src/services/workflowExecutionInputs.ts')
    const zero = { minimumItems: 0, maximumItems: 0 }
    const videoMode = {
      key: 'REFERENCE_VIDEO', label: '参考视频', description: '', inputKind: 'classified_references',
      requiresEntityReferenceBible: false, supportsNativeAudio: true,
      inputs: {
        totalImages: zero, firstFrame: zero, lastFrame: zero, entityImages: zero,
        referenceVideos: { minimumItems: 1, maximumItems: 2 },
        pairedVideoAudios: { minimumItems: 0, maximumItems: 1 },
        referenceAudios: { minimumItems: 0, maximumItems: 2 },
      },
      dynamicReferenceTopology: {
        schemaVersion: 1,
        videos: { minimumItems: 1, maximumItems: 2, purposes: [{ key: 'motion', label: '动作', description: '', minimumItems: 1, maximumItems: 2 }], defaultPurposeKey: 'motion' },
        audios: { minimumItems: 0, maximumItems: 2, purposes: [
          { key: 'voice', label: '声音', description: '', minimumItems: 0, maximumItems: 1 },
          { key: 'ambience', label: '环境声', description: '', minimumItems: 0, maximumItems: 2 },
        ], defaultPurposeKey: 'ambience' },
        preprocessing: {
          videos: { adapterKey: 'video-normalize', minimumDurationMs: 1000, maximumDurationMs: 10000, targetFps: 24, requiresSourceFps: true },
          pairedVideoAudios: { adapterKey: 'audio-normalize', minimumDurationMs: 1000, maximumDurationMs: 10000 },
          referenceAudios: { adapterKey: 'audio-passthrough', minimumDurationMs: 1000, maximumDurationMs: 10000 },
        },
        pairingRules: [{
          key: 'motion-audio', label: '动作配音', description: '', sourceMediaKind: 'video', targetMediaKind: 'audio',
          sourcePurposeKeys: ['motion'], targetPurposeKeys: ['voice'], defaultTargetPurposeKey: 'voice',
          minimumTargetsPerSource: 0, maximumTargetsPerSource: 1,
          allowEmbeddedSource: true, allowExternalTarget: true, sourceAndExternalMutuallyExclusive: true,
        }],
        mutualExclusionRules: [],
      },
    }
    const pictureMode = {
      ...videoMode,
      key: 'REFERENCE_IMAGES', label: '参考图片', supportsNativeAudio: false,
      inputs: { totalImages: { minimumItems: 1, maximumItems: 2 }, firstFrame: { minimumItems: 0, maximumItems: 1 }, lastFrame: zero, entityImages: { minimumItems: 0, maximumItems: 1 }, referenceVideos: zero, pairedVideoAudios: zero, referenceAudios: zero },
      dynamicReferenceTopology: {
        schemaVersion: 1,
        videos: { minimumItems: 0, maximumItems: 0, purposes: [], defaultPurposeKey: null },
        audios: { minimumItems: 0, maximumItems: 0, purposes: [], defaultPurposeKey: null },
        preprocessing: { videos: null, pairedVideoAudios: null, referenceAudios: null },
        pairingRules: [], mutualExclusionRules: [],
      },
    }
    const dynamic = (mode: any, pictureMin: number) => ({
      mode: mode.key, conditioningMode: mode,
      pictures: { minItems: pictureMin, maxItems: 2, categories: ['character', 'continuity_frame'] },
      videos: {
        minItems: mode.dynamicReferenceTopology.videos.minimumItems,
        maxItems: mode.dynamicReferenceTopology.videos.maximumItems,
        purposes: mode.dynamicReferenceTopology.videos.purposes.map((item: any) => item.key),
        defaultPurposeKey: mode.dynamicReferenceTopology.videos.defaultPurposeKey || '',
        supportsEmbeddedAudio: mode.dynamicReferenceTopology.pairingRules.length > 0,
        supportsExternalAudio: mode.dynamicReferenceTopology.pairingRules.length > 0,
      },
      audios: {
        minItems: mode.dynamicReferenceTopology.audios.minimumItems,
        maxItems: mode.dynamicReferenceTopology.audios.maximumItems,
        purposes: mode.dynamicReferenceTopology.audios.purposes.map((item: any) => item.key),
        defaultPurposeKey: mode.dynamicReferenceTopology.audios.defaultPurposeKey || '',
      },
      maximumPhysicalAssets: 6,
    })
    const schema = {
      schemaVersion: 2, available: true, reasonCode: '', assetSlots: [],
      parameters: [{ semanticKey: 'CONDITIONING_MODE', type: 'enum', required: true, min: null, max: null, multipleOf: null, alignmentModulus: null, alignmentRemainder: null, minLength: null, maxLength: null, options: ['REFERENCE_IMAGES', 'REFERENCE_VIDEO'], selectionGroup: '', recommendedValue: null }],
      dynamicReferences: null,
      dynamicReferenceModes: {
        REFERENCE_IMAGES: dynamic(pictureMode, 1),
        REFERENCE_VIDEO: dynamic(videoMode, 0),
      },
    }
    const assets = [
      { id: 'image-1', name: '人物', type: 'image', mimeType: 'image/png', checksum: sha('a'), isActive: true },
      { id: 'video-1', name: '动作', type: 'video', backendKind: 'reference_video', mimeType: 'video/mp4', checksum: sha('b'), isActive: true, durationMs: 4000, sourceFps: 24, hasEmbeddedAudio: false },
      { id: 'audio-1', name: '配音', type: 'audio', backendKind: 'reference_audio', mimeType: 'audio/wav', checksum: sha('c'), isActive: true, durationMs: 4000 },
      { id: 'audio-2', name: '环境声', type: 'audio', backendKind: 'reference_audio', mimeType: 'audio/wav', checksum: sha('d'), isActive: true, durationMs: 4000 },
    ]
    const draft = inputs.createWorkflowExecutionInputDraft(schema)
    draft.values.CONDITIONING_MODE = 'REFERENCE_IMAGES'
    assert.match(inputs.validateWorkflowExecutionInputDraft(schema, draft, assets), /参考图片数量/)
    draft.pictures.push({ key: 'picture-1', assetId: 'image-1', category: 'character' })
    assert.equal(inputs.validateWorkflowExecutionInputDraft(schema, draft, assets), '')

    draft.values.CONDITIONING_MODE = 'REFERENCE_VIDEO'
    draft.pictures = []
    draft.videos.push({ key: 'video-1', assetId: 'video-1', purpose: 'motion', useEmbeddedAudio: false, audioAssetId: 'audio-1', audioPurpose: 'voice' })
    draft.audios.push({ key: 'audio-1', assetId: 'audio-2', purpose: 'ambience' })
    assert.equal(inputs.validateWorkflowExecutionInputDraft(schema, draft, assets), '')
    const payload = inputs.buildWorkflowExecutionInput(schema, draft)
    assert.deepEqual(payload.references.videos, [{
      assetId: 'video-1', purpose: 'motion', useEmbeddedAudio: false,
      audioAssetId: 'audio-1', audioPurpose: 'voice',
    }])
    assert.deepEqual(payload.references.audios, [{
      assetId: 'audio-2', purpose: 'ambience',
    }])
  } finally {
    await vite.close()
  }
})

test('local workflow uploads bind every fixed and dynamic target by stable key', async () => {
  const vite = await server()
  try {
    const inputs = await vite.ssrLoadModule('/src/services/workflowExecutionInputs.ts')
    const preprocessing = {
      videos: { adapterKey: 'video-normalize', minimumDurationMs: 1000, maximumDurationMs: 10000, targetFps: 24, requiresSourceFps: true },
      pairedVideoAudios: { adapterKey: 'audio-normalize', minimumDurationMs: 1000, maximumDurationMs: 10000 },
      referenceAudios: { adapterKey: 'audio-passthrough', minimumDurationMs: 1000, maximumDurationMs: 10000 },
    }
    const schema = {
      schemaVersion: 2, available: true, reasonCode: '', parameters: [],
      assetSlots: [{ semanticKey: 'asset.image.style', mediaKind: 'image', required: true, inputSlot: 1, category: 'style', minItems: 1, maxItems: 1 }],
      dynamicReferenceModes: {},
      dynamicReferences: {
        mode: 'REFERENCE_MEDIA',
        conditioningMode: {
          key: 'REFERENCE_MEDIA', inputKind: 'classified_references',
          dynamicReferenceTopology: { preprocessing },
        },
      },
    }
    const draft = {
      values: {}, assetIds: { 'asset.image.style': '' },
      pictures: [{ key: 'picture-row', assetId: '', category: 'character' }],
      videos: [{ key: 'video-row', assetId: '', purpose: 'motion', useEmbeddedAudio: true, audioAssetId: '', audioPurpose: 'voice' }],
      audios: [{ key: 'audio-row', assetId: '', purpose: 'ambience' }],
    }
    const image = { id: 'image-upload', name: '本地图', type: 'image', backendKind: 'reference', mimeType: 'image/png', checksum: sha('a'), isActive: true }
    const video = { id: 'video-upload', name: '本地视频', type: 'video', backendKind: 'reference_video', mimeType: 'video/mp4', checksum: sha('b'), isActive: true, metadata: { duration_ms: 4000, source_fps: 24 } }
    const audio = { id: 'audio-upload', name: '本地音频', type: 'audio', backendKind: 'reference_audio', mimeType: 'audio/wav', checksum: sha('c'), isActive: true, metadata: { duration_ms: 4000 } }
    const requests = [
      { key: 'asset_slot:asset.image.style', mediaKind: 'image', target: { kind: 'asset_slot', semanticKey: 'asset.image.style' }, asset: image },
      { key: 'picture:picture-row', mediaKind: 'image', target: { kind: 'picture', rowKey: 'picture-row' }, asset: image },
      { key: 'video:video-row', mediaKind: 'video', target: { kind: 'video', rowKey: 'video-row' }, asset: video },
      { key: 'paired_audio:video-row', mediaKind: 'audio', target: { kind: 'paired_audio', rowKey: 'video-row' }, asset: audio },
      { key: 'audio:audio-row', mediaKind: 'audio', target: { kind: 'audio', rowKey: 'audio-row' }, asset: audio },
    ]
    let next = draft
    for (const request of requests) {
      assert.equal(inputs.workflowInputUploadCandidateCompatible(schema, next, request, request.asset), true, request.key)
      next = inputs.bindUploadedWorkflowInputAsset(next, request.target, request.asset.id)
      assert.ok(next, request.key)
    }
    assert.equal(draft.assetIds['asset.image.style'], '')
    assert.equal(next.assetIds['asset.image.style'], image.id)
    assert.equal(next.pictures[0].assetId, image.id)
    assert.equal(next.videos[0].assetId, video.id)
    assert.equal(next.videos[0].audioAssetId, audio.id)
    assert.equal(next.videos[0].useEmbeddedAudio, false)
    assert.equal(next.audios[0].assetId, audio.id)

    const tooLong = { ...video, id: 'too-long', metadata: { duration_ms: 11000, source_fps: 24 } }
    assert.equal(inputs.workflowInputUploadCandidateCompatible(schema, next, requests[2], tooLong), false)
    assert.equal(inputs.bindUploadedWorkflowInputAsset(next, { kind: 'video', rowKey: 'removed-row' }, video.id), null)
  } finally {
    await vite.close()
  }
})
