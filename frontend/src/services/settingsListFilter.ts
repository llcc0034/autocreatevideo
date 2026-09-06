export type SettingsListFilterOptions<T> = {
  search: string
  showRetired: boolean
  isRetired: (item: T) => boolean
  searchableValues: (item: T) => unknown[]
}

export type SettingsListFilterResult<T> = {
  items: T[]
  totalCount: number
  hiddenRetiredCount: number
}

const searchableText = (values: unknown[]) => values
  .flatMap((value) => Array.isArray(value) ? value : [value])
  .map((value) => String(value ?? ''))
  .join(' ')
  .toLocaleLowerCase('zh-CN')

export const filterSettingsList = <T>(
  source: readonly T[],
  options: SettingsListFilterOptions<T>,
): SettingsListFilterResult<T> => {
  const query = options.search.trim().toLocaleLowerCase('zh-CN')
  const hiddenRetiredCount = source.filter(options.isRetired).length
  const items = source.filter((item) => {
    if (!options.showRetired && options.isRetired(item)) return false
    return !query || searchableText(options.searchableValues(item)).includes(query)
  })
  return { items, totalCount: source.length, hiddenRetiredCount }
}
