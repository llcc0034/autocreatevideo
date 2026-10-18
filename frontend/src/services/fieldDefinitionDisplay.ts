import type { FieldDefinitionVersionSummary } from './fieldDefinitionVersions'

// Directory display only. This must never select a version for a business input.
export function fieldDefinitionDisplay(fieldId: string, versions: FieldDefinitionVersionSummary[]) {
  const ids = new Set<string>(), numbers = new Set<number>()
  for (const row of versions) {
    if (row.field_definition !== fieldId || !row.id || ids.has(row.id) || numbers.has(row.version)
      || !Number.isSafeInteger(row.version) || row.version < 1 || typeof row.label !== 'string'
      || !['draft', 'published', 'retired'].includes(row.status)) throw Error('字段定义目录不完整或归属不一致')
    ids.add(row.id); numbers.add(row.version)
  }
  const published = versions.filter(row => row.status === 'published')
  const row = [...(published.length ? published : versions)].sort((a, b) => b.version - a.version)[0]
  return row ? { id: row.id, label: row.label.trim(), version: row.version, status: row.status,
    chinese: /[\u3400-\u9fff]/u.test(row.label), basis: published.length ? '最新可用定义' : '尚无可用定义' } : null
}
