/** Authored JSON rows are the value; never round-trip them through display text. */
export interface DialogueLinePayload {
  speaker_id: string
  text: string
  language?: string
  delivery?: string
  voiceover?: boolean
  start_ms?: number
  end_ms?: number
}
export function readDialogue(value: unknown): DialogueLinePayload[] {
  if (!Array.isArray(value)) throw new Error('对白必须是逐行结构化数据')
  for (const row of value) {
    if (!row || typeof row !== 'object' || Array.isArray(row)
      || Object.keys(row).some((key) => !['speaker_id', 'text', 'language', 'delivery', 'voiceover', 'start_ms', 'end_ms'].includes(key))
      || typeof row.speaker_id !== 'string' || !/^S[1-9]\d*$/.test(row.speaker_id)
      || typeof row.text !== 'string' || !row.text.trim()
      || ('language' in row && (typeof row.language !== 'string' || /[\x00-\x1f\x7f<>\[\]{}]/.test(row.language)))
      || ('delivery' in row && (typeof row.delivery !== 'string' || !row.delivery.trim()))
      || ('voiceover' in row && typeof row.voiceover !== 'boolean')) throw new Error('对白行的原文、发言标识或可选字段无效')
    if ('start_ms' in row || 'end_ms' in row) {
      if (!Number.isSafeInteger(row.start_ms) || !Number.isSafeInteger(row.end_ms)
        || row.start_ms < 0 || row.end_ms <= row.start_ms) throw new Error('对白时间须为成对的整数毫秒且结束大于开始')
    }
  }
  return value.map((row) => ({ ...row }))
}
