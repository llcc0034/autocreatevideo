import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from './platformApi'
import { apiErrorSummary } from './apiErrorPresentation'
import { promptBindingModelFamily, promptBindingsForModel } from './modelPromptBindings'
import type { ModelProfileVersion, PromptProfileBinding } from './platformTypes'

/** One request per visible model version, shared by its runtime instance rows. */
export function useModelPromptBindings(profile: () => ModelProfileVersion | null) {
  const bindings = ref<PromptProfileBinding[]>([]); const loading = ref(false); const error = ref('')
  const rows = computed(() => { const current = profile(); return current ? promptBindingsForModel(bindings.value, current) : [] })
  let serial = 0
  const reload = async () => {
    const request = ++serial; const current = profile()
    bindings.value = []; error.value = ''; loading.value = Boolean(current)
    if (!current) return
    try {
      const family = promptBindingModelFamily(current)
      const result = await platformApi.listPromptProfileBindings(family ? { model_family: family } : { model_profile_version: current.id })
      if (request === serial) bindings.value = result
    } catch (cause) {
      if (request === serial) error.value = apiErrorSummary(cause, '提示词绑定读取失败')
    } finally { if (request === serial) loading.value = false }
  }
  watch(() => { const current = profile(); return current ? `${current.id}:${current.updatedAt}` : '' }, () => { void reload() }, { immediate: true })
  onBeforeUnmount(() => { serial += 1 })
  return { rows, loading, error, reload }
}
