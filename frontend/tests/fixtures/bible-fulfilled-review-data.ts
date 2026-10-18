// Synthetic contract data only; no business records or model calls.
export const id = (n: number) => `20000000-0000-4000-8000-${String(n).padStart(12, '0')}`
export const sha = 'a'.repeat(64)
export const materials = (count = 3) => Array.from({ length: count }, (_, i) => ({
  asset_id: id(10 + i), asset_name: i ? `隔离变体 ${i}` : '隔离上传主图', checksum: sha, mime_type: 'image/svg+xml',
  appearance_role: i ? 'variant' : 'primary',
  reference: { unit_key: `appearance:${id(200 + i)}`, requirement_key: `setting:${id(300 + i)}`, reference_version_id: id(400 + i),
    ...(i ? { decision_id: id(500 + i), artifact_id: id(600 + i), generated_from_id: id(400), source: 'generated' }
      : { fulfillment_id: id(500), target_sha256: sha, source: 'upload' }) },
}))
export const canonical = (count = 3) => ({ generation_stage_id: id(3),
  entities: [{ entity_type: 'character', entity_id: id(4), entity_name: '林晓 · 隔离试用', target_content_sha256: sha, candidates: [], fulfilled_materials: materials(count) }],
  material_fulfillments: materials(count).filter(m => m.appearance_role === 'primary').map(m => m.reference),
  material_generations: materials(count).filter(m => m.appearance_role === 'variant').map(m => m.reference),
})
export const assets = (count = 3): any[] => materials(count).map(m => ({ id: m.asset_id, name: m.asset_name,
  type: 'image', backendKind: 'reference', isActive: true, mimeType: m.mime_type, checksum: m.checksum, metadata: {},
  mediaUrl: 'data:image/svg+xml,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="200" height="160"><rect width="200" height="160" fill="${m.appearance_role === 'primary' ? '#294c65' : '#514469'}"/><text x="16" y="80" fill="white">ISOLATED ${m.appearance_role.toUpperCase()}</text></svg>`),
}))
