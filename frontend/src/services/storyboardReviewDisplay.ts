export interface ReviewEntity {id: string; name: string | null; status: 'available' | 'unavailable'}
export interface ReviewShotDisplay {
  shot_id: string; episode_id: string; episode_number: number; episode_title: string; number: number; title: string
  scene: ReviewEntity | null
  characters: Array<{role: 'visible' | 'speaker'; position: number; entity: ReviewEntity}>
  props: ReviewEntity[]
}
export interface StoryboardReviewDisplay {name_basis: 'current_bible_by_id'; shots: ReviewShotDisplay[]}
const object = (v: unknown): v is Record<string, any> => !!v && typeof v === 'object' && !Array.isArray(v)
const id = (v: unknown): v is string => typeof v === 'string' && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(v)
const number = (v: unknown, min = 1) => Number.isSafeInteger(v) && Number(v) >= min
const entity = (v: unknown): v is ReviewEntity => object(v) && id(v.id)
  && (v.status === 'available' ? typeof v.name === 'string' && !!v.name.trim() : v.status === 'unavailable' && v.name === null)
/** Display metadata cannot introduce hidden shots or silently drop missing names. */
export function readStoryboardReviewDisplay(raw: unknown, shotIds: string[]): StoryboardReviewDisplay {
  if (!object(raw) || raw.name_basis !== 'current_bible_by_id' || !Array.isArray(raw.shots)) throw Error('原分镜关系展示缺失')
  const expected = new Set(shotIds), seen = new Set<string>(), episodes = new Map<string, string>()
  for (const row of raw.shots) {
    if (!object(row) || !id(row.shot_id) || seen.has(row.shot_id) || !expected.has(row.shot_id)
      || !id(row.episode_id) || !number(row.episode_number) || !number(row.number)
      || typeof row.episode_title !== 'string' || typeof row.title !== 'string'
      || !(row.scene === null || entity(row.scene)) || !Array.isArray(row.characters) || !Array.isArray(row.props))
      throw Error('集/镜排序或原关系身份不完整')
    seen.add(row.shot_id)
    const episode = JSON.stringify([row.episode_number, row.episode_title])
    if (episodes.has(row.episode_id) && episodes.get(row.episode_id) !== episode) throw Error('同一剧集展示身份不一致')
    episodes.set(row.episode_id, episode)
    const positions = new Set<string>(), visible = new Set<string>(), props = new Set<string>()
    for (const relation of row.characters) {
      if (!object(relation) || !['visible', 'speaker'].includes(relation.role) || !number(relation.position, 0)
        || !entity(relation.entity) || positions.has(`${relation.role}:${relation.position}`)
        || relation.role === 'visible' && visible.has(relation.entity.id)) throw Error('出镜与发声关系缺失或重复')
      positions.add(`${relation.role}:${relation.position}`)
      if (relation.role === 'visible') visible.add(relation.entity.id)
    }
    for (const prop of row.props) {
      if (!entity(prop) || props.has(prop.id)) throw Error('原道具关系缺失或重复')
      props.add(prop.id)
    }
  }
  if (seen.size !== shotIds.length) throw Error('原关系展示未覆盖全部审核镜头')
  // Copies only: never reorder the canonical approval contract or its selection.
  return {name_basis: 'current_bible_by_id', shots: [...raw.shots].sort((a, b) =>
    a.episode_number - b.episode_number || a.episode_id.localeCompare(b.episode_id)
    || a.number - b.number || a.shot_id.localeCompare(b.shot_id)).map(row => ({...row,
      characters: [...row.characters].sort((a, b) => a.role.localeCompare(b.role) || a.position - b.position)}))}
}
