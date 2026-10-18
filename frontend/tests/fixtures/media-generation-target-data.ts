// Synthetic identities only; no current business records or model results.
export const versionId = '11111111-1111-4111-8111-111111111111'
const uuid = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`
export function targetFixture(role: 'primary' | 'variant' | null = null, withSetting = false) {
  const setting = withSetting ? { appearance_id: uuid(1), setting_revision_id: uuid(2),
    revision: 3, content_sha256: 'a'.repeat(64) } : null
  return { schema_version: 1, source_mode: 'generate', media_type: 'image',
    execution_instance_version_id: versionId, definition_sha256: 'b'.repeat(64), inputs_sha256: 'c'.repeat(64),
    candidates: { count: 2, evaluator: null }, setting,
    ...(role ? { appearance: { role, setting,
      primary: role === 'variant' ? { version_id: uuid(3), series_id: uuid(4), asset_id: uuid(5), sha256: 'd'.repeat(64) } : null,
      primary_input: role === 'variant' ? { input_key: 'selected_primary_image', item_key: 'exact-primary-item' } : null,
      ...(role === 'variant' ? { difference: { kind: 'manual_channel', key: 'positive_prompt', value_sha256: 'e'.repeat(64) } } : {}) } } : {}) }
}
