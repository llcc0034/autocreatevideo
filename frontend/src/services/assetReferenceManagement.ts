import { computed, reactive } from 'vue'
import type { assetReferenceApi, AssetReferenceSeries, ReferenceBibleOption, ReferenceBiblePage, ReferenceRemovalImpact, ReferenceSeriesPatch } from './assetReferenceApi'
import { apiErrorSummary } from './apiErrorPresentation'

type ManagementApi = Pick<typeof assetReferenceApi, 'getSeries' | 'updateSeries' | 'bibleOptions' | 'removalImpact' | 'remove'>
const emptyOptions = (): ReferenceBiblePage => ({count: 0, next: null, previous: null, results: []})

export function createReferenceManagement(api: ManagementApi, changed: () => void) {
  const state = reactive({mode: '' as '' | 'edit' | 'remove', busy: false, loading: false, error: '', notice: '',
    series: null as AssetReferenceSeries | null, name: '', bibleId: '', currentBible: null as ReferenceBibleOption | null,
    options: emptyOptions(), search: '', page: 1, optionsLoading: false, optionsError: '',
    target: null as {kind: 'series' | 'version'; id: string; label: string} | null,
    impact: null as ReferenceRemovalImpact | null})
  let epoch = 0, optionsEpoch = 0
  const patch = computed(() => {
    const data: ReferenceSeriesPatch = {}, series = state.series
    if (!series) return data
    if (state.name.trim() !== series.name) data.name = state.name.trim()
    if ((state.bibleId || null) !== series.bible_variant_id) data.bible_variant_id = state.bibleId || null
    return data
  })
  function close() {
    if (state.busy) return
    epoch++; optionsEpoch++; state.mode = ''; state.loading = false; state.optionsLoading = false
  }
  async function options(page = 1) {
    if (!state.series || state.busy) return
    const request = ++optionsEpoch, id = state.series.id
    state.optionsLoading = true; state.optionsError = ''; state.options = emptyOptions(); state.page = page
    try {
      const rows = await api.bibleOptions(id, {page, search: state.search.trim()})
      if (request === optionsEpoch) state.options = rows
    } catch (error) { if (request === optionsEpoch) state.optionsError = apiErrorSummary(error) }
    finally { if (request === optionsEpoch) state.optionsLoading = false }
  }
  async function edit(id: string) {
    if (state.busy) return
    close(); const request = epoch
    state.mode = 'edit'; state.loading = true; state.error = ''; state.notice = ''; state.series = null
    state.currentBible = null; state.options = emptyOptions(); state.search = ''
    try {
      const series = await api.getSeries(id)
      if (request !== epoch) return
      state.series = series; state.name = series.name; state.bibleId = series.bible_variant_id || ''
      if (series.bible_variant_id) {
        const selected = await api.bibleOptions(id, {page: 1, id: series.bible_variant_id})
        if (request !== epoch) return
        state.currentBible = selected.results.find(row => row.id === series.bible_variant_id) || null
        if (!state.currentBible) throw Error('当前关联设定未返回，请重新读取；未自动解除关联。')
      }
      await options()
    } catch (error) { if (request === epoch) state.error = apiErrorSummary(error) }
    finally { if (request === epoch) state.loading = false }
  }
  async function save() {
    if (state.busy || state.loading || !state.series || !state.name.trim() || !Object.keys(patch.value).length) return
    const request = epoch, id = state.series.id, data = {...patch.value}
    state.busy = true; state.error = ''
    try {
      const saved = await api.updateSeries(id, data)
      if (request !== epoch) return
      if (saved.id !== id) throw Error('保存响应与目标系列不一致，请重新读取核对。')
      state.notice = `已保存“${saved.name}”；原素材、版本及已有请求未改写。`; state.mode = ''; changed()
    } catch (error) { if (request === epoch) state.error = apiErrorSummary(error) }
    finally { if (request === epoch) state.busy = false }
  }
  function chooseBible() {
    const selected = state.options.results.find(row => row.id === state.bibleId)
    if (selected) state.currentBible = selected
  }
  async function removal(kind: 'series' | 'version', id: string, label: string) {
    if (state.busy) return
    close(); const request = epoch
    state.mode = 'remove'; state.target = {kind, id, label}; state.loading = true; state.error = ''; state.notice = ''; state.impact = null
    try {
      const impact = await api.removalImpact(kind, id)
      if (request !== epoch) return
      if (impact.id !== id || impact.target_type !== kind) throw Error('影响预览与目标不一致，请重新读取。')
      state.impact = impact
    } catch (error) { if (request === epoch) state.error = apiErrorSummary(error) }
    finally { if (request === epoch) state.loading = false }
  }
  async function remove() {
    if (state.busy || state.loading || !state.target || !state.impact?.can_remove) return
    const request = epoch, target = {...state.target}
    state.busy = true; state.error = ''
    try {
      await api.remove(target.kind, target.id)
      if (request !== epoch) return
      state.notice = `已解除“${target.label}”的关系；原素材和文件保留。`; state.mode = ''; changed()
    } catch (error) {
      if (request === epoch) { state.error = apiErrorSummary(error); state.impact = null }
    } finally { if (request === epoch) state.busy = false }
  }
  function dispose() { epoch++; optionsEpoch++ }
  return {state, patch, edit, options, chooseBible, save, removal, remove, close, dispose}
}
