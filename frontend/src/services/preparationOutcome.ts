/** Read-only server explanation shared by task center and preparation views. */
export interface PreparationOutcome {
  message: string
  artifactCount: number
  selectedCount: number
}
export function adaptPreparationOutcome(value: unknown): PreparationOutcome {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('任务状态说明缺失')
  const r = value as Record<string, unknown>
  if (typeof r.message !== 'string' || !r.message.trim()
    || !Number.isSafeInteger(r.artifact_count) || Number(r.artifact_count) < 0
    || !Number.isSafeInteger(r.selected_count) || Number(r.selected_count) < 0
    || Number(r.selected_count) > Number(r.artifact_count)) throw Error('任务状态说明不完整')
  return { message: r.message, artifactCount: Number(r.artifact_count), selectedCount: Number(r.selected_count) }
}
