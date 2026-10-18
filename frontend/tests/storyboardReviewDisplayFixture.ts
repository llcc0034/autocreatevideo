// Explicit isolated response projection, not a runtime source or fallback.
export function displayFixture(episodeId: string, shotIds: string[]) {
  return {name_basis: 'current_bible_by_id', shots: shotIds.map((shot_id, i) => ({
    shot_id, episode_id: episodeId, episode_number: 1, episode_title: '雨中相遇', number: i + 1,
    title: i ? '空镜' : '旅人雨衣', scene: null, characters: [], props: [],
  }))}
}
