import type { Shot } from '../types/index.ts'
import type { DialogueLinePayload } from './dialogue.ts'

export interface ShotCharacterRelation {
  role: 'visible' | 'speaker'
  position: number
  character_id: string
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
export function readShotCharacters(value: unknown, dialogue: DialogueLinePayload[]): ShotCharacterRelation[] {
  if (!Array.isArray(value)) throw new Error('分镜缺少明确出镜/发言关系，请刷新或修正，不从旧人物池推断')
  const positions = new Set<string>(), visible = new Set<string>(), speakers = new Set<number>()
  for (const row of value) {
    if (!row || typeof row !== 'object' || Object.keys(row).sort().join(',') !== 'character_id,position,role'
      || !['visible', 'speaker'].includes(row.role) || !Number.isSafeInteger(row.position) || row.position < 0
      || typeof row.character_id !== 'string' || !uuid.test(row.character_id)) throw new Error('分镜人物关系无效')
    const key = `${row.role}:${row.position}`
    if (positions.has(key) || (row.role === 'visible' && visible.has(row.character_id))) throw new Error('分镜人物关系重复')
    positions.add(key)
    if (row.role === 'visible') visible.add(row.character_id)
    else speakers.add(row.position)
  }
  if (speakers.size !== dialogue.length || dialogue.some((_, index) => !speakers.has(index))) throw new Error('每行对白须有明确发言者，不按名字补齐')
  return value.map((row) => ({ ...row }))
}
export const visibleCharacterIds = (shot: Pick<Shot, 'characterRelations'>): string[] => shot.characterRelations
  .filter((row) => row.role === 'visible').sort((a, b) => a.position - b.position).map((row) => row.character_id)

export function toggleVisibleCharacter(shot: Shot, id: string) {
  const ids = visibleCharacterIds(shot)
  const next = ids.includes(id) ? ids.filter((item) => item !== id) : [...ids, id]
  shot.characterRelations = [...shot.characterRelations.filter((row) => row.role !== 'visible'),
    ...next.map((character_id, position) => ({ role: 'visible' as const, position, character_id }))]
}

export function removeDialogueLine(shot: Shot, position: number) {
  shot.audio.dialogue.splice(position, 1)
  shot.characterRelations = shot.characterRelations.filter((row) => row.role !== 'speaker' || row.position !== position)
    .map((row) => row.role === 'speaker' && row.position > position ? { ...row, position: row.position - 1 } : row)
}
