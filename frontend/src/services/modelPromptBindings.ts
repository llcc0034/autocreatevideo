import type { ModelProfileVersion, PromptProfileBinding } from './platformTypes'

export const promptBindingModelFamily = (profile: ModelProfileVersion): string => {
  const value = profile.specification.model_family
  return typeof value === 'string' ? value.trim().toLowerCase() : ''
}

/** Association only: runtime operation/mode selection and validation remain server-owned. */
export const promptBindingsForModel = (
  bindings: readonly PromptProfileBinding[], profile: ModelProfileVersion,
): PromptProfileBinding[] => {
  const family = promptBindingModelFamily(profile)
  return bindings.filter((binding) => binding.modelProfileVersion
    ? binding.modelProfileVersion === profile.id
    : Boolean(family) && binding.modelFamily === family)
    .sort((a, b) => Number(Boolean(b.modelProfileVersion)) - Number(Boolean(a.modelProfileVersion))
      || b.priority - a.priority || a.key.localeCompare(b.key))
}

export const modelPromptSettingsRoute = (profileId: string, promptProfileId = '', deploymentId = '') => ({
  path: '/settings',
  query: {
    section: 'prompts', model_profile_version: profileId,
    ...(promptProfileId ? { prompt_profile_version: promptProfileId } : {}),
    ...(deploymentId ? { deployment: deploymentId } : {}),
  },
})
