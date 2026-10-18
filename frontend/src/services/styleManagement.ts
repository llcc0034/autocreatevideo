export interface ManagedStyle {
  id: string
  slug: string
  name: string
  category: string | null
  category_label: string
  category_active: boolean | null
  description: string
  visual_prompt: string
  negative_prompt: string
  scope: 'personal' | 'global'
  is_active: boolean
  is_system: boolean
  can_manage: boolean
  asset_count: number
  project_count: number
  generation_job_count: number
  updated_at: string
}
export type StyleForm = Pick<ManagedStyle, 'name' | 'category' | 'description' | 'visual_prompt' | 'negative_prompt' | 'scope' | 'is_active'>
export const styleForm = (row?: ManagedStyle): StyleForm => ({
  name: row?.name || '', category: row?.category || null, description: row?.description || '',
  visual_prompt: row?.visual_prompt || '', negative_prompt: row?.negative_prompt || '',
  scope: row?.scope || 'personal', is_active: row?.is_active ?? true,
})
export const styleWritePayload = (form: StyleForm, original?: ManagedStyle): Partial<StyleForm> => {
  const value = { ...form, name: form.name.trim(), description: form.description.trim(), visual_prompt: form.visual_prompt.trim(), negative_prompt: form.negative_prompt.trim() }
  if (!original) return value
  return Object.fromEntries(Object.entries(value).filter(([key, item]) => key !== 'scope' && item !== original[key as keyof StyleForm]))
}
export const styleReferenceCount = (style: ManagedStyle) => style.project_count + style.asset_count + style.generation_job_count

export interface StyleCategory {
  id: string
  key: string
  name: string
  description: string
  sort_order: number
  is_active: boolean
  style_count: number
  can_manage: boolean
}
export const categoryForm = (row?: StyleCategory) => ({name:row?.name || '',description:row?.description || '',sort_order:row?.sort_order ?? 0,is_active:row?.is_active ?? true})
