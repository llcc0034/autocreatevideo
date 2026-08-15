export interface DialogueCharacterOption {
  name: string
  stableSpeakerId?: string
}

export interface DialogueLinePayload {
  speaker_id: string
  text: string
  delivery?: string
  voiceover?: boolean
  start_ms?: number
  end_ms?: number
}

const MAX_DIALOGUE_LINES = 64
const MAX_SPEAKER_ID_LENGTH = 16
const MAX_TEXT_LENGTH = 2_000
const MAX_DELIVERY_LENGTH = 500
const SPEAKER_ID_RE = /^S[1-9]\d*$/
const VOICEOVER_TOKEN_RE = /(^|[^A-Za-z0-9_])voiceover(?=$|[^A-Za-z0-9_])/i
const TIMING_PREFIX_RE = /^\s*[【[]\s*(-?(?:\d+:\d{1,2}(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:ms|毫秒|s|秒)?)\s*(?:-|–|—|~|～|至|到)\s*(-?(?:\d+:\d{1,2}(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:ms|毫秒|s|秒)?)\s*[】\]]\s*/i

const parseTimeMs = (value: string): number | undefined => {
  const normalized = value.trim().toLowerCase().replace(/\s+/g, '')
  let milliseconds: number
  if (normalized.includes(':')) {
    const [minutesText, secondsText] = normalized.split(':', 2)
    const minutes = Number(minutesText)
    const seconds = Number(secondsText.replace(/(?:s|秒)$/i, ''))
    milliseconds = (minutes * 60 + seconds) * 1000
  } else if (/(?:ms|毫秒)$/i.test(normalized)) {
    milliseconds = Number(normalized.replace(/(?:ms|毫秒)$/i, ''))
  } else {
    milliseconds = Number(normalized.replace(/(?:s|秒)$/i, '')) * 1000
  }
  const rounded = Math.round(milliseconds)
  return Number.isSafeInteger(rounded) ? rounded : undefined
}

const formatTimeMs = (value: number): string => Number((value / 1000).toFixed(3)).toString()

export const parseDeliveryAnnotation = (value: string): { delivery?: string; voiceover?: true } => {
  const voiceover = VOICEOVER_TOKEN_RE.test(value)
  const delivery = value
    .replace(new RegExp(VOICEOVER_TOKEN_RE.source, 'ig'), (_match, prefix: string) => prefix || ' ')
    .replace(/^[\s;；,，|/]+|[\s;；,，|/]+$/g, '')
    .replace(/\s*[;；]\s*/g, '; ')
    .trim()
  return {
    ...(delivery ? { delivery } : {}),
    ...(voiceover ? { voiceover: true as const } : {})
  }
}

export const parseDialogueText = (
  value: string,
  characters: DialogueCharacterOption[]
): DialogueLinePayload[] | undefined => {
  if (!value.trim()) return []
  const lines = value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
  if (lines.length > MAX_DIALOGUE_LINES) return undefined
  const result: DialogueLinePayload[] = []
  for (const line of lines) {
    const timing = line.match(TIMING_PREFIX_RE)
    const startMs = timing ? parseTimeMs(timing[1]) : undefined
    const endMs = timing ? parseTimeMs(timing[2]) : undefined
    if (timing && (startMs === undefined || endMs === undefined || startMs < 0 || endMs <= startMs)) {
      return undefined
    }
    const body = timing ? line.slice(timing[0].length) : line
    const match = body.match(/^([^：:（）()\n]{1,160}?)(?:\s*[（(]([^）)\n]{1,500})[）)])?\s*[：:]\s*(.+)$/)
    if (!match) return undefined
    const label = match[1].trim()
    const character = characters.find((item) => item.stableSpeakerId === label || item.name === label)
    const speakerId = SPEAKER_ID_RE.test(label) ? label : character?.stableSpeakerId
    const text = match[3].trim()
    if (!speakerId || speakerId.length > MAX_SPEAKER_ID_LENGTH || !text || text.length > MAX_TEXT_LENGTH) {
      return undefined
    }
    const annotation = parseDeliveryAnnotation((match[2] || '').trim())
    if (annotation.delivery && annotation.delivery.length > MAX_DELIVERY_LENGTH) return undefined
    result.push({
      speaker_id: speakerId,
      text,
      ...annotation,
      ...(startMs !== undefined && endMs !== undefined ? { start_ms: startMs, end_ms: endMs } : {}),
    })
  }
  return result
}

export const formatDialogueLines = (value: unknown): string => {
  if (!Array.isArray(value)) return ''
  return value.map((raw) => {
    const item = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}
    const speakerId = String(item.speaker_id || '')
    const text = String(item.text || '')
    const delivery = typeof item.delivery === 'string' ? item.delivery.trim() : ''
    const annotation = [item.voiceover === true ? 'voiceover' : '', delivery].filter(Boolean).join('; ')
    const startMs = Number(item.start_ms)
    const endMs = Number(item.end_ms)
    const timing = Number.isSafeInteger(startMs) && Number.isSafeInteger(endMs) && startMs >= 0 && endMs > startMs
      ? `[${formatTimeMs(startMs)}–${formatTimeMs(endMs)}秒] `
      : ''
    return `${timing}${speakerId}${annotation ? `（${annotation}）` : ''}：${text}`
  }).join('\n')
}
