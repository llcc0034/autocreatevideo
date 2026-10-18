export interface ExecutionParameterMappings {
  version_id: string; revision: number; definition_sha256: string
  status: 'verified' | 'unavailable' | 'not_recorded'; code: string; message: string
  basis_kind: 'workflow' | 'deployment' | 'none'; basis_id: string | null; basis_sha256: string | null
  rows: { key: string; destinations: { node_id: string | null; field: string }[] }[]
}
const invalid = (): never => { throw new Error('参数目标映射响应不完整或身份不一致，请重新读取合同。') }
const object = (v: unknown): Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : invalid()
const text = (v: unknown): string => typeof v === 'string' && v.length > 0 ? v : invalid()
const sha = (v: unknown): string => /^[a-f0-9]{64}$/.test(text(v)) ? v as string : invalid()
const nullable = (v: unknown) => v === null ? null : text(v)

export function adaptExecutionParameterMappings(value: unknown): ExecutionParameterMappings {
  const r = object(value)
  if (!['verified', 'unavailable', 'not_recorded'].includes(text(r.status)) || !['workflow', 'deployment', 'none'].includes(text(r.basis_kind))
    || !Number.isSafeInteger(r.revision) || (r.revision as number) < 1 || !Array.isArray(r.rows)) invalid()
  const rows = (r.rows as unknown[]).map(value => {
    const row = object(value)
    if (!Array.isArray(row.destinations)) invalid()
    const destinations = (row.destinations as unknown[]).map(value => {
      const destination = object(value), node_id = nullable(destination.node_id), field = text(destination.field)
      if ((r.basis_kind === 'workflow') !== (node_id !== null)) invalid()
      return { node_id, field }
    })
    if (new Set(destinations.map(d => JSON.stringify([d.node_id, d.field]))).size !== destinations.length) invalid()
    return { key: text(row.key), destinations }
  })
  const basis_id = nullable(r.basis_id), basis_sha256 = r.basis_sha256 === null ? null : sha(r.basis_sha256)
  if (new Set(rows.map(row => row.key)).size !== rows.length || (r.status !== 'verified' && rows.length)
    || (r.basis_kind === 'none') !== (basis_id === null && basis_sha256 === null)
    || (r.basis_kind !== 'none' && (basis_id === null || basis_sha256 === null))
    || (r.status === 'verified' && r.basis_kind === 'none')) invalid()
  return { version_id: text(r.version_id), revision: r.revision as number, definition_sha256: sha(r.definition_sha256),
    status: r.status as ExecutionParameterMappings['status'], code: text(r.code), message: text(r.message),
    basis_kind: r.basis_kind as ExecutionParameterMappings['basis_kind'], basis_id, basis_sha256, rows }
}
