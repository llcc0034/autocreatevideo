export interface PromptProfilePreviewExampleRow {
  inputKey: string
  previewExample: unknown
}

export const promptProfilePreviewExampleInputValue = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map((item) => String(item)).join('\n')
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') return JSON.stringify(value, null, 2)
  return value
}

export const mergePromptProfilePreviewExamples = (
  draft: Record<string, unknown>,
  rows: PromptProfilePreviewExampleRow[],
): Record<string, unknown> => {
  const next = { ...draft }
  for (const row of rows) {
    if (Object.prototype.hasOwnProperty.call(next, row.inputKey)) continue
    next[row.inputKey] = promptProfilePreviewExampleInputValue(row.previewExample)
  }
  return next
}
