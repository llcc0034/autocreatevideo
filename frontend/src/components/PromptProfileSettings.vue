<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { AlertTriangle, Archive, BookOpen, Check, Copy, Eye, FileText, Link2, LoaderCircle, Plus, RefreshCw, Save, Settings2, ShieldCheck, Trash2 } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import CatalogChoiceField from '@/components/CatalogChoiceField.vue'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import PromptWritingGuide from '@/components/PromptWritingGuide.vue'
import PromptProfileConfiguration from '@/components/PromptProfileConfiguration.vue'
import PromptPrimitiveConfiguration from '@/components/PromptPrimitiveConfiguration.vue'
import PromptRenderingTemplateEditor from '@/components/PromptRenderingTemplateEditor.vue'
import RegistryDeletionReviewModal from '@/components/RegistryDeletionReviewModal.vue'
import SettingsListFilter from '@/components/SettingsListFilter.vue'
import RegistryRelationSummary from '@/components/RegistryRelationSummary.vue'
import { useRegistryRelations } from '@/services/registryRelations'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { platformApi } from '@/services/platformApi'
import { mergePromptProfilePreviewExamples, promptProfilePreviewExampleInputValue } from '@/services/promptProfilePreviewExamples'
import { mediaKindLabel, modeLabel, operationLabel, valueTypeLabel } from '@/services/presentationLabels'
import { filterSettingsList } from '@/services/settingsListFilter'
import { promptBindingModelFamily, promptBindingsForModel } from '@/services/modelPromptBindings'
import { ruleContentMethod, hasRuleTemplates } from '@/services/promptPrimitiveConfiguration'
import type {
  JsonObject,
  ModelProfileVersion,
  PromptFieldDefinition,
  PromptFieldValueType,
  PromptProfileBinding,
  PromptProfileFieldRule,
  PromptProfileOptions,
  PromptProfilePreview,
  PromptProfileVersion,
  RegistryDeletionResourceType,
} from '@/services/platformTypes'

type EditorKind = 'field' | 'profile' | 'rule' | 'binding'
const props = defineProps<{ modelProfileVersionId?: string; promptProfileVersionId?: string; bindingId?: string; includeInactive?: boolean }>()
const modelContext = ref<ModelProfileVersion | null>(null)
const modelContextRequested = computed(() => Boolean(props.modelProfileVersionId))
const modelContextReady = computed(() => !modelContextRequested.value || Boolean(modelContext.value))
const scopedProfiles = computed(() => !modelContextRequested.value ? profiles.value
  : modelContext.value ? profiles.value.filter((item) => item.modelFamily === promptBindingModelFamily(modelContext.value!)) : [])
const scopedBindings = computed(() => !modelContextRequested.value ? bindings.value
  : modelContext.value ? promptBindingsForModel(bindings.value, modelContext.value) : [])
let refreshSerial = 0
type ActionKind = 'delete-field' | 'delete-profile' | 'delete-rule' | 'delete-binding' | 'publish-profile' | 'retire-profile'
type ActionTarget = { kind: ActionKind; id: string; label: string }
type ChannelLimitDraft = { enabled: boolean; maxChars: number | ''; warnAtChars: number | '' }
type PreviewRuleInput = {
  id: string
  fieldDefinition: string
  fieldKey: string
  fieldLabel: string
  sourceSelector: string
  required: boolean
}
type PreviewSourceRow = {
  selector: string
  inputKey: string
  rule: PreviewRuleInput
  field: PromptFieldDefinition | null
}

const loading = ref(false)
const promptOptionsReady = ref(false)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const fields = ref<PromptFieldDefinition[]>([])
const profiles = ref<PromptProfileVersion[]>([])
const rules = ref<PromptProfileFieldRule[]>([])
const bindings = ref<PromptProfileBinding[]>([])
const emptyPromptOptions = (): PromptProfileOptions => ({
  operations: [], modelFamilies: [], modes: [], sourceSelectors: [], channels: [], labelStyles: [], separators: [],
  fieldValueTypes: [], fieldCategories: [], customEntryFields: [], fieldSystemDefaults: {}, ruleSystemDefaults: {},
  generationModes: [], generationSourceSelectors: [], generationContextSelectors: [], generationOutputSemantics: [], sourceProjectionKeys: [], sourceProjectionPrimitives: [], referenceRenderingFields: [], languagePolicies: [], lengthUnits: [], generationValidators: [], modelProfileVersions: [],
})
const options = ref<PromptProfileOptions>(emptyPromptOptions())
const operationCatalog = computed(() => options.value.operations.map((value) => ({ value, label: operationLabel(value) })))
const modeCatalog = computed(() => options.value.modes.map((value) => ({ value, label: modeLabel(value) })))
const selectedProfileId = ref('')
const settingsListSearch = ref('')
const showRetiredSettingsRecords = ref(false)
watch(() => props.includeInactive, (value) => { if (value !== undefined) showRetiredSettingsRecords.value = value }, { immediate: true })

const editorOpen = ref(false)
const editorKind = ref<EditorKind>('field')
const editorError = ref('')
const actionTarget = ref<ActionTarget | null>(null)
const deletionResourceTypes: Partial<Record<ActionKind, RegistryDeletionResourceType>> = {
  'delete-field': 'prompt_field_definition',
  'delete-profile': 'prompt_profile_version',
  'delete-rule': 'prompt_profile_field_rule',
  'delete-binding': 'prompt_profile_binding',
}
const actionDeletionResourceType = computed(() => actionTarget.value ? deletionResourceTypes[actionTarget.value.kind] || null : null)
const actionError = ref('')
const previewOpen = ref(false)
const previewBusy = ref(false)
const previewError = ref('')
const previewResult = ref<PromptProfilePreview | null>(null)
const previewDraft = ref<Record<string, unknown>>({})
const previewStale = ref(false)
const previewReadyMessage = ref('')
const cloneOpen = ref(false)
const cloneName = ref('')
const cloneBusy = ref(false)
const cloneError = ref('')
const configurationSnapshot = ref<{ profile: PromptProfileVersion; rules: PromptProfileFieldRule[]; options: PromptProfileOptions; focusRuleId?: string } | null>(null)
const openConfiguration = (profile: PromptProfileVersion, focusRuleId?: string) => {
  configurationSnapshot.value = JSON.parse(JSON.stringify({
    profile,
    rules: rules.value.filter(rule => rule.profileVersion === profile.id),
    options: options.value,
    focusRuleId,
  }))
}

const fieldForm = reactive({
  id: '', key: '', label: '', category: '', description: '', impact: '', valueType: '' as PromptFieldValueType | '',
  hasDefault: false, defaultText: '', defaultBoolean: false, valueOptionsText: '', isRequired: false, isActive: true, displayOrder: 0,
})
const profileForm = reactive({
  id: '', profileKey: '', version: 1, name: '', description: '', modelFamily: '', operation: '', mode: '',
  sourceProjectionKey: '', officialReference: '', reviewState: 'needs_review',
  renderingTemplates: {} as Record<string, string>, renderingSeparator: '', renderingListPrefix: '', renderingTemplateKey: '',
  renderingTaskSeparator: '', renderingUnreferencedTask: '',
  renderingReferenceRules: [] as { category: string; purpose: string; task: string; relationship: string; usage: string }[],
  overlapEnabled: false, overlapMinimumSharedTokens: '' as number | '', overlapMinimumRatio: '' as number | '',
  densityEnabled: false, densityMaximumDurationMs: '' as number | '', densityMaximumActionCharacters: '' as number | '', densityMinimumClauseSeparators: '' as number | '',
  motionGuidanceEnabled: false, motionMinimumEnglishWords: '' as number | '', motionGenericValuesText: '',
  channelLimits: {} as Record<string, ChannelLimitDraft>,
})
const fieldOriginalPayload = ref<JsonObject>({})
const profileOriginalPayload = ref<JsonObject>({})
const ruleForm = reactive({
  id: '', profileVersion: '', fieldDefinition: '', sourceSelector: '', projectionPrimitive: '', channel: 'positive_prompt', position: 1,
  labelStyle: 'label_colon', separator: 'newline', required: false, omitIfEmpty: true, maxChars: '' as number | '',
  generationMode: '', generationSourceSelector: '', generationInstruction: '', generationOutputSemantics: 'final', generationRequired: false, languagePolicy: '', lengthUnit: '',
  generationMin: '' as number | '', generationMax: '' as number | '', generationValidatorKeys: [] as string[], generationContextSelectors: [] as string[],
})
const bindingForm = reactive({
  id: '', key: '', name: '', promptProfileVersion: '', operation: '', modelFamily: '', mode: '',
  modelProfileVersion: '', priority: 0, isActive: true,
})

const isRetiredLifecycleState = (value: unknown) => ['retired', 'inactive', 'disabled'].includes(String(value || '').toLocaleLowerCase('en-US'))
const selectedProfile = computed(() => profiles.value.find((item) => item.id === selectedProfileId.value && (showRetiredSettingsRecords.value || !isRetiredLifecycleState(item.status))) || null)
const profileProjectionLocked = computed(() => Boolean(
  profileForm.id && rules.value.some((item) => item.profileVersion === profileForm.id),
))
const canCloneSelectedProfile = computed(() => {
  const profile = selectedProfile.value
  return Boolean(
    profile
    && ['published', 'retired'].includes(profile.status)
    && options.value.sourceProjectionKeys.some((item) => item.value === profile.sourceProjectionKey),
  )
})
const selectedRules = computed(() => rules.value.filter((item) => item.profileVersion === selectedProfileId.value).sort((a, b) => a.position - b.position))
const selectedProfileWritingGuide = computed(() => selectedProfile.value ? {
  profileKey: selectedProfile.value.profileKey,
  name: selectedProfile.value.name,
  version: selectedProfile.value.version,
  sourceProjectionKey: selectedProfile.value.sourceProjectionKey,
  officialReference: selectedProfile.value.officialReference,
  fieldRules: selectedRules.value.map((item) => ({
    fieldKey: item.fieldKey,
    fieldLabel: item.fieldLabel,
    sourceSelector: item.sourceSelector,
    channel: item.channel,
    position: item.position,
    labelStyle: item.labelStyle,
    separator: item.separator,
    maxChars: item.maxChars,
    generationMode: item.generationMode,
    generationOutputSemantics: item.generationOutputSemantics,
  })),
} : null)
const activeFields = computed(() => fields.value.filter((item) => item.isActive))
const fieldListState = computed(() => filterSettingsList(fields.value, {
  search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value,
  isRetired: (item) => !item.isActive,
  searchableValues: (item) => [item.key, item.label, item.category, item.description, item.impact, item.valueType],
}))
const profileListState = computed(() => filterSettingsList(scopedProfiles.value, {
  search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value,
  isRetired: (item) => isRetiredLifecycleState(item.status),
  searchableValues: (item) => [item.profileKey, item.name, item.description, item.modelFamily, item.operation, item.mode, item.status],
}))
const bindingListState = computed(() => filterSettingsList(scopedBindings.value, {
  search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value,
  isRetired: (item) => !item.isActive,
  searchableValues: (item) => [item.key, item.name, item.operation, item.modelFamily, item.mode, item.modelIdentifier, item.promptProfileKey, item.priority],
}))
const filteredFields = computed(() => fieldListState.value.items)
const filteredProfiles = computed(() => profileListState.value.items)
const filteredBindings = computed(() => bindingListState.value.items)
const profileRelations = useRegistryRelations('prompt', () => filteredProfiles.value)
const bindingRelations = useRegistryRelations('binding', () => filteredBindings.value)
const settingsListFilterSummary = computed(() => ({
  visibleCount: filteredFields.value.length + filteredProfiles.value.length + filteredBindings.value.length,
  totalCount: fields.value.length + scopedProfiles.value.length + scopedBindings.value.length,
  hiddenRetiredCount: fieldListState.value.hiddenRetiredCount + profileListState.value.hiddenRetiredCount + bindingListState.value.hiddenRetiredCount,
}))
const publishedProfiles = computed(() => scopedProfiles.value.filter((item) => item.status === 'published'))
const profileBindings = computed(() => bindings.value.filter((item) => item.promptProfileVersion === selectedProfileId.value))
const sourceLabel = (value: string) => options.value.sourceSelectors.find((item) => item.value === value)?.label || value
const choiceLabel = (rows: Array<{ value: string; label: string }>, value: string) => rows.find((item) => item.value === value)?.label || value
const primitiveLabel = (value: string) => options.value.sourceProjectionPrimitives.find((item) => item.value === value)?.label || value
const generationSourceLabel = (value: string) => options.value.generationSourceSelectors.find((item) => item.value === value)?.label || (value ? `历史未登记来源 · ${value}` : '未选择')
const modelGenerationModes = ['model_generated', 'translate_only']
const lockedGenerationModes = ['server_locked', 'passthrough']
const modelGenerationOperations = ['image.generate', 'video.generate']
const selectedProfileSupportsModelGeneration = computed(() => modelGenerationOperations.includes(selectedProfile.value?.operation || ''))
const ruleGenerationModeOptions = computed(() => options.value.generationModes.filter((item) => (
  selectedProfileSupportsModelGeneration.value || !modelGenerationModes.includes(item.value)
)))
const generationModeOperationUnavailable = computed(() => (
  modelGenerationModes.includes(ruleForm.generationMode)
  && !selectedProfileSupportsModelGeneration.value
))
const resolvedGenerationSourceLabel = (generationMode: string, generationSource: string, finalSource: string) => (
  lockedGenerationModes.includes(generationMode)
    ? `跟随最终提示词取值 · ${sourceLabel(finalSource)}`
    : generationSourceLabel(generationSource)
)
const ruleGenerationSourceLabel = (rule: PromptProfileFieldRule) => resolvedGenerationSourceLabel(
  rule.generationMode,
  rule.generationSourceSelector,
  rule.sourceSelector,
)
const modelGenerationSourceMode = computed(() => modelGenerationModes.includes(ruleForm.generationMode))
const lockedGenerationSourceMode = computed(() => lockedGenerationModes.includes(ruleForm.generationMode))
const contributionSourceSelectionBlocked = computed(() => (
  ruleForm.generationOutputSemantics === 'contribution'
  && (
    ruleForm.sourceSelector !== 'projection.output'
    || !options.value.sourceProjectionPrimitives.some((item) => item.value === ruleForm.projectionPrimitive)
  )
))
const compatibleGenerationSourceOptions = computed(() => {
  let rows = options.value.generationSourceSelectors.filter((item) => item.status === 'active' && item.generationModes.includes(ruleForm.generationMode))
  if (ruleForm.generationOutputSemantics === 'contribution') {
    const primitive = options.value.sourceProjectionPrimitives.find((item) => item.value === ruleForm.projectionPrimitive)
    if (ruleForm.sourceSelector !== 'projection.output' || !primitive) return []
    rows = rows.filter((item) => primitive.contributionSourceSelectors.includes(item.value))
  }
  return rows
})
const generationSourceGroups = computed(() => {
  const groups = new Map<string, { key: string; label: string; items: PromptProfileOptions['generationSourceSelectors'] }>()
  for (const item of compatibleGenerationSourceOptions.value) {
    const current = groups.get(item.group)
    if (current) current.items.push(item)
    else groups.set(item.group, { key: item.group, label: item.groupLabel, items: [item] })
  }
  return [...groups.values()]
})
const selectedGenerationSourceOption = computed(() => options.value.generationSourceSelectors.find((item) => item.value === ruleForm.generationSourceSelector) || null)
const generationSourceUnknown = computed(() => Boolean(
  modelGenerationSourceMode.value
  && ruleForm.generationSourceSelector
  && !selectedGenerationSourceOption.value,
))
const generationSourceIncompatible = computed(() => Boolean(
  modelGenerationSourceMode.value
  && ruleForm.generationSourceSelector
  && selectedGenerationSourceOption.value
  && !compatibleGenerationSourceOptions.value.some((item) => item.value === ruleForm.generationSourceSelector),
))
const ruleSourceOptions = computed(() => options.value.sourceSelectors.filter((item) => {
  if (selectedProfile.value?.sourceProjectionKey !== 'declarative_v1' && item.value === 'projection.output') return false
  return true
}))
const generationContextSummary = (selectors: string[]) => selectors.length
  ? selectors.map((value) => choiceLabel(options.value.generationContextSelectors, value)).join('、')
  : '仅主要内容来源 / Primary source only'
const generationRangeLabel = (rule: PromptProfileFieldRule) => {
  if (rule.generationMin === null && rule.generationMax === null) return ''
  if (rule.generationMin === null) return `≤ ${rule.generationMax}`
  if (rule.generationMax === null) return `≥ ${rule.generationMin}`
  return `${rule.generationMin}–${rule.generationMax}`
}
const optionalInteger = (value: number | '') => value === '' ? null : Number(value)
const validLanguagePolicy = (value: string) => {
  if (['inherit', 'preserve_source', 'any'].includes(value)) return true
  const tag = /^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,5}$/
  if (tag.test(value)) return true
  if (!value.startsWith('bilingual:')) return false
  const tags = value.slice('bilingual:'.length).split('+')
  return tags.length === 2 && tags[0].toLocaleLowerCase() !== tags[1].toLocaleLowerCase() && tags.every((item) => tag.test(item))
}
const fieldForRule = (rule: { fieldDefinition: string }) => fields.value.find((item) => item.id === rule.fieldDefinition) || null
const humanBytes = (value: number) => value >= 1024 ? `${(value / 1024).toFixed(1)} KB` : `${value} B`
const channelLimitSummary = (limits: PromptProfileVersion['channelLimits']) => {
  const rows = Object.entries(limits)
  if (!rows.length) return '未配置（该 Profile 不限制最终通道长度）'
  return rows.map(([channel, limit]) => `${channel} ≤ ${limit.maxChars}${limit.warnAtChars === null ? '' : `，${limit.warnAtChars} 预警`}`).join('；')
}
const message = (cause: unknown) => apiErrorSummary(cause, '请求失败，服务端没有返回可读错误')

const refresh = async () => {
  const serial = ++refreshSerial
  loading.value = true; promptOptionsReady.value = false; options.value = emptyPromptOptions(); error.value = ''
  modelContext.value = null
  try {
    const context = props.modelProfileVersionId ? await platformApi.getModelProfile(props.modelProfileVersionId) : null
    if (serial !== refreshSerial) return
    if (context && !promptBindingModelFamily(context)) throw new Error('当前模型版本未声明模型族，无法确定提示词策略适用范围。')
    const [nextFields, nextProfiles, nextRules, nextBindings, nextOptions] = await Promise.all([
      platformApi.listPromptFieldDefinitions(), platformApi.listPromptProfileVersions(),
      platformApi.listPromptProfileFieldRules(), platformApi.listPromptProfileBindings(context ? {model_family:promptBindingModelFamily(context)} : {}), platformApi.getPromptProfileOptions(),
    ])
    if (serial !== refreshSerial) return
    if (context?.status === 'published' && !nextOptions.modelProfileVersions.some((item) => item.id === context.id)) {
      throw new Error('服务端绑定目录未返回当前模型版本，已停止编辑，请重新读取模型与策略目录。')
    }
    const fieldTypeValues = new Set(nextOptions.fieldValueTypes.map((item) => item.value))
    if (!nextOptions.fieldValueTypes.length || fieldTypeValues.size !== nextOptions.fieldValueTypes.length || nextOptions.fieldValueTypes.some((item) => !item.value.trim() || !item.label.trim())) {
      throw new Error('服务端 options API 未返回完整且唯一的通用字段值类型目录，字段编辑已停止。')
    }
    if (!nextOptions.operations.length || !nextOptions.modelFamilies.length || !nextOptions.sourceProjectionKeys.length) {
      throw new Error('服务端 options API 缺少 operation、模型族或来源投影器目录，提示词策略编辑已停止。')
    }
    for (const [name, values] of [['operations', nextOptions.operations], ['modelFamilies', nextOptions.modelFamilies], ['modes', nextOptions.modes]] as const) {
      if (new Set(values).size !== values.length || values.some((value) => !value.trim())) throw new Error(`服务端 options API 的 ${name} 目录包含空值或重复值，表单编辑已停止。`)
    }
    const requiredChoiceCatalogs = [
      ['sourceSelectors', nextOptions.sourceSelectors],
      ['channels', nextOptions.channels],
      ['labelStyles', nextOptions.labelStyles],
      ['separators', nextOptions.separators],
      ['generationModes', nextOptions.generationModes],
      ['generationOutputSemantics', nextOptions.generationOutputSemantics],
      ['sourceProjectionKeys', nextOptions.sourceProjectionKeys],
      ['languagePolicies', nextOptions.languagePolicies],
      ['lengthUnits', nextOptions.lengthUnits],
    ] as const
    for (const [name, rows] of requiredChoiceCatalogs) {
      const values = new Set(rows.map((item) => item.value))
      if (!rows.length || values.size !== rows.length || rows.some((item) => !item.value.trim() || !item.label.trim())) {
        throw new Error(`服务端 options API 的 ${name} 目录为空、重复或缺少标签，表单编辑已停止。`)
      }
    }
    if (!nextOptions.fieldCategories.length && !nextOptions.customEntryFields.includes('field_category')) {
      throw new Error('服务端既未返回字段分类目录，也未授权登记新分类，通用字段编辑已停止。')
    }
    if (!nextOptions.modes.length && !nextOptions.customEntryFields.includes('mode')) {
      throw new Error('服务端既未返回 Mode 目录，也未授权登记新 Mode，提示词策略编辑已停止。')
    }
    for (const key of ['is_required', 'is_active', 'display_order']) {
      if (!Object.prototype.hasOwnProperty.call(nextOptions.fieldSystemDefaults, key)) throw new Error(`服务端字段系统默认缺少 ${key}`)
    }
    for (const key of ['label_style', 'separator', 'required', 'omit_if_empty', 'max_chars', 'generation_output_semantics', 'generation_required', 'language_policy', 'length_unit']) {
      if (!Object.prototype.hasOwnProperty.call(nextOptions.ruleSystemDefaults, key)) throw new Error(`服务端字段规则系统默认缺少 ${key}`)
    }
    const contextOptions = nextOptions.generationContextSelectors
    const contextValues = new Set(contextOptions.map((item) => item.value))
    if (!contextOptions.length || contextValues.size !== contextOptions.length || contextOptions.some((item) => !item.value.trim() || !item.label.trim() || !item.description.trim())) {
      throw new Error('服务端 options API 未返回完整、唯一且带双语说明的 generation_context_selectors 受控目录，字段规则编辑已停止。')
    }
    const generationSourceOptions = nextOptions.generationSourceSelectors
    const generationSourceValues = new Set(generationSourceOptions.map((item) => item.value))
    if (!generationSourceOptions.length || generationSourceValues.size !== generationSourceOptions.length || generationSourceOptions.some((item) => (
      !item.value.trim() || !item.label.trim() || !item.scopeType.trim() || !item.group.trim() || !item.groupLabel.trim()
      || !item.description.trim() || !item.availability.trim() || !item.scope.trim() || item.status !== 'active'
      || !item.generationModes.length || item.generationModes.some((mode) => !modelGenerationModes.includes(mode))
    ))) {
      throw new Error('服务端 options API 未返回完整、唯一且带说明的主要内容来源目录，字段规则编辑已停止。')
    }
    if (nextOptions.sourceProjectionKeys.some((item) => item.value === 'declarative_v1')) {
      const primitiveOptions = nextOptions.sourceProjectionPrimitives
      const renderingFields = nextOptions.referenceRenderingFields
      if (!renderingFields.length || new Set(renderingFields.map((item) => item.key)).size !== renderingFields.length) {
        throw new Error('服务端参考渲染字段目录为空或重复，策略编辑已停止')
      }
      const primitiveValues = new Set(primitiveOptions.map((item) => item.value))
      if (!primitiveOptions.length || primitiveValues.size !== primitiveOptions.length || primitiveOptions.some((item) => !item.value.trim() || !item.label.trim() || !item.description.trim() || !item.engineKey.trim())) {
        throw new Error('服务端 options API 未返回完整且唯一的来源投影原语目录，声明式字段组合编辑已停止。')
      }
      if (!nextOptions.sourceSelectors.some((item) => item.value === 'projection.output')) {
        throw new Error('服务端 options API 缺少 projection.output 受控来源，声明式字段组合编辑已停止。')
      }
    }
    fields.value = nextFields; profiles.value = nextProfiles; rules.value = nextRules; bindings.value = nextBindings; options.value = nextOptions; promptOptionsReady.value = true
    modelContext.value = context
    const requested = props.promptProfileVersionId
    if (requested && !scopedProfiles.value.some((item) => item.id === requested)) throw new Error('指定提示词策略不在当前模型的适用范围内，请返回模型详情重新选择。')
    const visibleProfiles = scopedProfiles.value.filter((item) => !isRetiredLifecycleState(item.status))
    if (requested) {
      selectedProfileId.value = requested
      if (isRetiredLifecycleState(profiles.value.find((item) => item.id === requested)?.status)) showRetiredSettingsRecords.value = true
    } else if (!scopedProfiles.value.some((item) => item.id === selectedProfileId.value) || !selectedProfile.value) selectedProfileId.value = visibleProfiles[0]?.id || ''
  } catch (cause) { if (serial === refreshSerial) { error.value = message(cause); promptOptionsReady.value = false } }
  finally { if (serial === refreshSerial) loading.value = false }
}

const splitValues = (value: string) => value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean)
const recordValue = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const primitiveEngineKeysForSelectedProfile = computed(() => new Set(
  Object.keys(recordValue(selectedProfile.value?.sourceProjectionConfig.primitive_options)),
))
const compatibleProjectionPrimitiveOptions = computed(() => options.value.sourceProjectionPrimitives.filter(
  (item) => primitiveEngineKeysForSelectedProfile.value.has(item.engineKey),
))
const loadDeclarativeProjectionConfig = (value: JsonObject) => {
  const primitiveOptions = recordValue(value.primitive_options)
  const engineOptions = recordValue(primitiveOptions.reference_context_ir_v1)
  const rendering = recordValue(engineOptions.rendering)
  const templates = recordValue(rendering.templates)
  const advisories = recordValue(value.quality_advisories)
  const overlap = recordValue(advisories.positive_negative_token_overlap)
  const density = recordValue(advisories.short_shot_action_density)
  const motion = recordValue(advisories.motion_camera_reference_guidance)
  Object.assign(profileForm, {
    renderingTemplates: Object.fromEntries(options.value.referenceRenderingFields.map((item) => [item.key, typeof templates[item.key] === 'string' ? templates[item.key] : ''])),
    renderingSeparator: typeof rendering.separator === 'string' ? rendering.separator : '',
    renderingListPrefix: typeof rendering.list_prefix === 'string' ? rendering.list_prefix : '',
    renderingTemplateKey: typeof rendering.prompt_template_key === 'string' ? rendering.prompt_template_key : '',
    renderingTaskSeparator: typeof rendering.task_separator === 'string' ? rendering.task_separator : '',
    renderingUnreferencedTask: typeof rendering.unreferenced_task === 'string' ? rendering.unreferenced_task : '',
    renderingReferenceRules: Array.isArray(rendering.reference_rules) ? rendering.reference_rules.map(value => {
      const row = recordValue(value)
      return Object.fromEntries(['category', 'purpose', 'task', 'relationship', 'usage'].map(key => [key, typeof row[key] === 'string' ? row[key] : '']))
    }) : [],
    overlapEnabled: Object.keys(overlap).length > 0,
    overlapMinimumSharedTokens: typeof overlap.minimum_shared_tokens === 'number' ? overlap.minimum_shared_tokens : '',
    overlapMinimumRatio: typeof overlap.minimum_overlap_ratio === 'number' ? overlap.minimum_overlap_ratio : '',
    densityEnabled: Object.keys(density).length > 0,
    densityMaximumDurationMs: typeof density.maximum_duration_ms === 'number' ? density.maximum_duration_ms : '',
    densityMaximumActionCharacters: typeof density.maximum_action_characters === 'number' ? density.maximum_action_characters : '',
    densityMinimumClauseSeparators: typeof density.minimum_clause_separators === 'number' ? density.minimum_clause_separators : '',
    motionGuidanceEnabled: Object.keys(motion).length > 0,
    motionMinimumEnglishWords: typeof motion.minimum_english_words === 'number' ? motion.minimum_english_words : '',
    motionGenericValuesText: Array.isArray(motion.generic_values) ? motion.generic_values.filter((item): item is string => typeof item === 'string').join('\n') : '',
  })
}
const buildProjectionConfig = (): JsonObject => {
  if (profileForm.sourceProjectionKey !== 'declarative_v1') return {}
  const qualityAdvisories: JsonObject = { schema_version: 1 }
  if (profileForm.overlapEnabled) qualityAdvisories.positive_negative_token_overlap = {
    minimum_shared_tokens: Number(profileForm.overlapMinimumSharedTokens), minimum_overlap_ratio: Number(profileForm.overlapMinimumRatio),
  }
  if (profileForm.densityEnabled) qualityAdvisories.short_shot_action_density = {
    maximum_duration_ms: Number(profileForm.densityMaximumDurationMs), maximum_action_characters: Number(profileForm.densityMaximumActionCharacters), minimum_clause_separators: Number(profileForm.densityMinimumClauseSeparators),
  }
  if (profileForm.motionGuidanceEnabled) qualityAdvisories.motion_camera_reference_guidance = {
    minimum_english_words: Number(profileForm.motionMinimumEnglishWords), generic_values: splitValues(profileForm.motionGenericValuesText),
  }
  const result: JsonObject = {
    schema_version: 5,
    primitive_options: { reference_context_ir_v1: { rendering: {
      templates: { ...profileForm.renderingTemplates },
      separator: profileForm.renderingSeparator,
      list_prefix: profileForm.renderingListPrefix,
      prompt_template_key: profileForm.renderingTemplateKey,
      task_separator: profileForm.renderingTaskSeparator,
      unreferenced_task: profileForm.renderingUnreferencedTask,
      reference_rules: profileForm.renderingReferenceRules.map(row => ({ ...row })),
    } } },
  }
  if (Object.keys(qualityAdvisories).length > 1) result.quality_advisories = qualityAdvisories
  return result
}
const defaultText = (field: PromptFieldDefinition) => {
  if (field.defaultValue === null || field.defaultValue === undefined) return ''
  if (Array.isArray(field.defaultValue)) return field.defaultValue.join('\n')
  return String(field.defaultValue)
}
const parseFieldDefault = (): unknown => {
  if (!fieldForm.hasDefault) return null
  if (fieldForm.valueType === 'boolean') return fieldForm.defaultBoolean
  if (fieldForm.valueType === 'number') {
    const value = Number(fieldForm.defaultText)
    if (!Number.isFinite(value)) throw new Error('默认值必须是有限数字')
    return value
  }
  if (fieldForm.valueType === 'string_list') return splitValues(fieldForm.defaultText)
  return fieldForm.defaultText
}

const openField = (existing?: PromptFieldDefinition) => {
  if (!promptOptionsReady.value) {
    error.value = '通用字段 options API 尚未完整读取，不能打开填写表单。'
    return
  }
  editorKind.value = 'field'; editorError.value = ''
  Object.assign(fieldForm, {
    id: existing?.id || '', key: existing?.key || '', label: existing?.label || '', category: existing?.category || '',
    description: existing?.description || '', impact: existing?.impact || '', valueType: existing?.valueType || '',
    hasDefault: existing ? existing.defaultValue !== null && existing.defaultValue !== undefined : false,
    defaultText: existing ? defaultText(existing) : '', defaultBoolean: existing?.defaultValue === true,
    valueOptionsText: existing?.valueOptions.join('\n') || '', isRequired: existing?.isRequired ?? Boolean(options.value.fieldSystemDefaults.is_required),
    isActive: existing?.isActive ?? (options.value.fieldSystemDefaults.is_active !== false), displayOrder: existing?.displayOrder || 0,
  })
  fieldOriginalPayload.value = existing ? buildFieldPayload() : {}
  editorOpen.value = true
}
const openProfile = (existing?: PromptProfileVersion) => {
  if (!promptOptionsReady.value) {
    error.value = '提示词策略 options API 尚未完整读取，不能打开填写表单。'
    return
  }
  editorKind.value = 'profile'; editorError.value = ''
  Object.assign(profileForm, {
    id: existing?.id || '', profileKey: existing?.profileKey || '', version: existing?.version || 1,
    name: existing?.name || '', description: existing?.description || '', modelFamily: existing?.modelFamily || (modelContext.value ? promptBindingModelFamily(modelContext.value) : ''),
    operation: existing?.operation || '', mode: existing?.mode || '',
    sourceProjectionKey: existing?.sourceProjectionKey || '',
    officialReference: existing?.officialReference || '', reviewState: existing?.reviewState || 'needs_review',
    channelLimits: Object.fromEntries(options.value.channels.map((item) => {
      const limit = existing?.channelLimits[item.value]
      return [item.value, {
        enabled: Boolean(limit),
        maxChars: limit?.maxChars ?? '',
        warnAtChars: limit?.warnAtChars ?? '',
      }]
    })),
  })
  loadDeclarativeProjectionConfig(existing?.sourceProjectionConfig || {})
  profileOriginalPayload.value = existing ? buildProfilePayload() : {}
  editorOpen.value = true
}
const openRule = (existing?: PromptProfileFieldRule) => {
  const profile = selectedProfile.value
  if (!profile || profile.status !== 'draft') return
  if (!promptOptionsReady.value) {
    error.value = '字段规则 options API 尚未完整读取，不能使用页面默认值编辑字段规则。'
    return
  }
  editorKind.value = 'rule'; editorError.value = ''
  const nextPosition = Math.max(0, ...selectedRules.value.map((item) => item.position)) + 1
  const defaultSourceSelector = profile.sourceProjectionKey === 'declarative_v1'
    ? 'projection.output'
    : ''
  const defaults = options.value.ruleSystemDefaults
  Object.assign(ruleForm, {
    id: existing?.id || '', profileVersion: profile.id, fieldDefinition: existing?.fieldDefinition || '',
    sourceSelector: existing?.sourceSelector || defaultSourceSelector, projectionPrimitive: existing?.projectionPrimitive || '', channel: existing?.channel || '',
    position: existing?.position || nextPosition, labelStyle: existing?.labelStyle || String(defaults.label_style || ''),
    separator: existing?.separator || String(defaults.separator || ''), required: existing?.required ?? defaults.required === true,
    omitIfEmpty: existing?.omitIfEmpty ?? defaults.omit_if_empty === true, maxChars: existing?.maxChars ?? Number(defaults.max_chars || 0),
    generationMode: existing?.generationMode || '', generationSourceSelector: existing?.generationSourceSelector || '',
    generationContextSelectors: [...(existing
      ? existing.generationContextSelectors
      : Array.isArray(defaults.generation_context_selectors) ? defaults.generation_context_selectors.map(String) : [])],
    generationInstruction: existing?.generationInstruction || '',
    generationOutputSemantics: existing?.generationOutputSemantics || String(defaults.generation_output_semantics || ''),
    generationRequired: existing?.generationRequired ?? defaults.generation_required === true, languagePolicy: existing?.languagePolicy || String(defaults.language_policy || ''),
    lengthUnit: existing?.lengthUnit || String(defaults.length_unit || ''), generationMin: existing?.generationMin ?? '', generationMax: existing?.generationMax ?? '',
    generationValidatorKeys: [...(existing
      ? existing.generationValidatorKeys
      : Array.isArray(defaults.generation_validator_keys) ? defaults.generation_validator_keys.map(String) : [])],
  })
  editorOpen.value = true
}
const syncBindingProfile = () => {
  const profile = profiles.value.find((item) => item.id === bindingForm.promptProfileVersion)
  if (!profile) return
  bindingForm.operation = profile.operation; bindingForm.modelFamily = profile.modelFamily; bindingForm.mode = profile.mode
}
const openBinding = (existing?: PromptProfileBinding) => {
  if (!promptOptionsReady.value || !modelContextReady.value) return
  if (modelContextRequested.value && (modelContext.value?.status !== 'published' || (existing && existing.modelProfileVersion !== modelContext.value?.id))) return
  editorKind.value = 'binding'; editorError.value = ''
  const profileId = existing?.promptProfileVersion || (selectedProfile.value?.status === 'published' ? selectedProfile.value.id : '')
  Object.assign(bindingForm, {
    id: existing?.id || '', key: existing?.key || '', name: existing?.name || '', promptProfileVersion: profileId,
    operation: existing?.operation || '', modelFamily: existing?.modelFamily || '', mode: existing?.mode || '',
    modelProfileVersion: existing?.modelProfileVersion || modelContext.value?.id || '', priority: existing?.priority || 0, isActive: existing?.isActive ?? true,
  })
  if (!existing) syncBindingProfile()
  editorOpen.value = true
}

watch(() => ruleForm.channel, (channel) => {
  if (channel === 'system_instruction') ruleForm.sourceSelector = 'definition.default'
})
watch(() => ruleForm.sourceSelector, (source) => {
  if (source !== 'projection.output') ruleForm.projectionPrimitive = ''
  if (lockedGenerationModes.includes(ruleForm.generationMode)) ruleForm.generationSourceSelector = source
})
watch(() => ruleForm.generationMode, (mode) => {
  if (lockedGenerationModes.includes(mode)) ruleForm.generationSourceSelector = ruleForm.sourceSelector
  if (mode === 'translate_only' && ['preserve_source', 'any'].includes(ruleForm.languagePolicy)) ruleForm.languagePolicy = ''
})
const generationModeBeforeUserChange = ref('')
const rememberGenerationModeBeforeUserChange = () => {
  generationModeBeforeUserChange.value = ruleForm.generationMode
}
const onRuleGenerationModeChange = () => {
  if (
    lockedGenerationModes.includes(generationModeBeforeUserChange.value)
    && modelGenerationModes.includes(ruleForm.generationMode)
  ) {
    ruleForm.generationSourceSelector = ''
  } else if (lockedGenerationModes.includes(ruleForm.generationMode)) {
    ruleForm.generationSourceSelector = ruleForm.sourceSelector
  }
  generationModeBeforeUserChange.value = ruleForm.generationMode
}
watch(() => ruleForm.lengthUnit, (unit) => {
  if (unit === 'none') {
    ruleForm.generationMin = ''
    ruleForm.generationMax = ''
  }
})
watch(actionTarget, () => { actionError.value = '' })

const selectedRuleField = computed(() => fields.value.find((item) => item.id === ruleForm.fieldDefinition) || null)
const editorValidation = computed(() => {
  const errors: string[] = []
  const keyPattern = /^[a-z][a-z0-9_.-]{1,119}$/
  if (editorKind.value === 'field') {
    if (!promptOptionsReady.value) errors.push('通用字段 options API 未就绪，不能提交')
    if (!keyPattern.test(fieldForm.key)) errors.push('字段 Key 必须为小写稳定标识')
    if (!fieldForm.label.trim()) errors.push('显示名称必填')
    if (!keyPattern.test(fieldForm.category)) errors.push('分类必须为小写稳定标识')
    if (!options.value.fieldCategories.some((item) => item.value === fieldForm.category) && !options.value.customEntryFields.includes('field_category')) errors.push('字段分类必须来自服务端目录')
    if (!fieldForm.description.trim()) errors.push('必须说明字段含义')
    if (!fieldForm.impact.trim()) errors.push('必须说明字段影响')
    if (!options.value.fieldValueTypes.some((item) => item.value === fieldForm.valueType)) errors.push('值类型必须来自服务端受控目录')
    const values = splitValues(fieldForm.valueOptionsText)
    if (fieldForm.valueType === 'enum' && !values.length) errors.push('枚举字段必须配置可选值')
    if (fieldForm.valueType !== 'enum' && values.length) errors.push('只有枚举字段可配置可选值')
    if (fieldForm.hasDefault && fieldForm.valueType === 'enum' && !values.includes(fieldForm.defaultText)) errors.push('枚举默认值必须来自可选值')
    if (fieldForm.hasDefault && fieldForm.valueType === 'number' && !Number.isFinite(Number(fieldForm.defaultText))) errors.push('数值默认值无效')
  } else if (editorKind.value === 'profile') {
    if (!promptOptionsReady.value) errors.push('提示词策略 options API 未就绪，不能提交')
    if (!keyPattern.test(profileForm.profileKey)) errors.push('Profile Key 格式不正确')
    if (!profileForm.name.trim() || !profileForm.modelFamily || !profileForm.operation) errors.push('名称、模型族和 operation 必填')
    else if (!/^[a-z][a-z0-9_.-]{1,99}$/.test(profileForm.operation)) errors.push('Operation 必须为 2–100 位小写受控 key，仅包含字母、数字、点、下划线或短横线')
    else if (!options.value.operations.includes(profileForm.operation) && !options.value.customEntryFields.includes('operation')) errors.push('Operation 必须来自服务端目录')
    if (profileForm.modelFamily && !options.value.modelFamilies.includes(profileForm.modelFamily)) errors.push('模型族必须来自服务端目录')
    if (profileForm.mode && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$/.test(profileForm.mode)) errors.push('Mode 只能包含字母、数字、点、下划线或短横线，最多 40 字符')
    else if (profileForm.mode && !options.value.modes.includes(profileForm.mode) && !options.value.customEntryFields.includes('mode')) errors.push('Mode 必须来自服务端目录')
    if (!options.value.sourceProjectionKeys.some((item) => item.value === profileForm.sourceProjectionKey)) errors.push('必须选择已注册的来源投影器')
    const persistedProfile = profiles.value.find((item) => item.id === profileForm.id)
    if (profileProjectionLocked.value && persistedProfile && persistedProfile.sourceProjectionKey !== profileForm.sourceProjectionKey) errors.push('已有字段规则的 Profile 不能切换来源投影器；请保留当前投影器或创建新的无规则草稿')
    if (profileForm.sourceProjectionKey === 'declarative_v1') {
      if (!profileForm.renderingTemplateKey.trim()) errors.push('提示词模板标识必填')
      if (!profileForm.renderingSeparator) errors.push('段内分隔符必填')
      if (!profileForm.renderingTaskSeparator) errors.push('任务类型分隔符必填')
      if (!profileForm.renderingUnreferencedTask.trim()) errors.push('无参考素材时的任务名称必填')
      const referenceRuleKeys = new Set<string>()
      for (const [index, rule] of profileForm.renderingReferenceRules.entries()) {
        if (![rule.category, rule.task, rule.relationship, rule.usage].every((value) => value.trim())) errors.push(`用途规则 ${index + 1} 的类别、任务、保留方式和使用说明必填`)
        const key = JSON.stringify([rule.category, rule.purpose])
        if (referenceRuleKeys.has(key)) errors.push(`用途规则 ${index + 1} 的类别与用途重复`)
        referenceRuleKeys.add(key)
      }
      for (const key of ['reference', 'entity_reference', 'retention', 'reference_usage', 'shot', 'reviewed_shot', 'summary', 'shot_opening', 'shot_transition']) {
        if (!profileForm.renderingTemplates[key]?.trim()) errors.push(`必填渲染模板尚未配置：${key}`)
      }
      const positive = (value: number | '') => Number.isInteger(value) && Number(value) > 0
      if (profileForm.overlapEnabled && (!positive(profileForm.overlapMinimumSharedTokens) || !(Number(profileForm.overlapMinimumRatio) > 0 && Number(profileForm.overlapMinimumRatio) <= 1))) errors.push('正负词重叠建议需要正整数词数与 0–1 比例')
      if (profileForm.densityEnabled && ![profileForm.densityMaximumDurationMs, profileForm.densityMaximumActionCharacters, profileForm.densityMinimumClauseSeparators].every(positive)) errors.push('短镜头动作密度建议的三个阈值都必须为正整数')
      if (profileForm.motionGuidanceEnabled && (!positive(profileForm.motionMinimumEnglishWords) || !splitValues(profileForm.motionGenericValuesText).length)) errors.push('动态参考说明建议需要最小英文词数和至少一个通用占位值')
    }
    if (profileForm.officialReference && !/^https?:\/\//i.test(profileForm.officialReference)) errors.push('官方参考必须是 HTTP(S) URL')
    for (const [channel, limit] of Object.entries(profileForm.channelLimits)) {
      if (!limit.enabled) continue
      if (!Number.isInteger(limit.maxChars) || Number(limit.maxChars) < 1) errors.push(`${channel} 的通道上限必须为正整数`)
      if (limit.warnAtChars !== '' && (!Number.isInteger(limit.warnAtChars) || Number(limit.warnAtChars) < 1 || Number(limit.warnAtChars) > Number(limit.maxChars))) errors.push(`${channel} 的预警阈值必须留空或为 1–通道上限的整数`)
    }
  } else if (editorKind.value === 'rule') {
    if (!promptOptionsReady.value) errors.push('字段规则 options API 未就绪，不能提交字段规则')
    const allowedContextSelectors = new Set(options.value.generationContextSelectors.map((item) => item.value))
    const allowedGenerationSources = new Set(options.value.generationSourceSelectors.map((item) => item.value))
    if (ruleForm.generationContextSelectors.some((item) => !allowedContextSelectors.has(item))) errors.push('生成上下文包含服务端受控目录之外的值，请刷新后重新选择')
    if (!selectedProfile.value || selectedProfile.value.status !== 'draft') errors.push('只能编辑草稿 Profile 规则')
    if (!selectedRuleField.value?.isActive) errors.push('必须选择已启用字段')
    if (!ruleForm.sourceSelector || !ruleForm.channel) errors.push('数据来源和输出通道必填')
    if (ruleForm.sourceSelector === 'projection.output') {
      const registeredPrimitive = options.value.sourceProjectionPrimitives.find((item) => item.value === ruleForm.projectionPrimitive)
      const primitive = compatibleProjectionPrimitiveOptions.value.find((item) => item.value === ruleForm.projectionPrimitive)
      if (selectedProfile.value?.sourceProjectionKey !== 'declarative_v1') errors.push('projection.output 只能用于声明式投影策略')
      if (!registeredPrimitive) errors.push('必须选择服务端登记的投影原语')
      else if (!primitive) errors.push('所选投影原语的引擎未在当前 Profile 的 primitive_options 中配置')
      if (ruleForm.generationOutputSemantics === 'contribution' && modelGenerationModes.includes(ruleForm.generationMode) && primitive && !primitive.contributionSourceSelectors.includes(ruleForm.generationSourceSelector)) errors.push('该投影原语不支持当前贡献来源')
    } else if (ruleForm.projectionPrimitive) errors.push('只有 projection.output 来源可以选择投影原语')
    if (ruleForm.channel === 'system_instruction' && ruleForm.sourceSelector !== 'definition.default') errors.push('系统约束只能使用管理员字段默认值')
    if (ruleForm.sourceSelector === 'definition.default' && selectedRuleField.value?.defaultValue == null) errors.push('引用字段默认值时，字段必须已配置默认值')
    if (!Number.isInteger(ruleForm.position) || ruleForm.position < 1) errors.push('装配顺序必须从 1 开始')
    if (!Number.isInteger(ruleForm.maxChars) || Number(ruleForm.maxChars) < 1) errors.push('最大字符数必须为正整数')
    if (!ruleForm.generationMode) errors.push('字段内容生成方式必填')
    if (generationModeOperationUnavailable.value) errors.push('模型生成和仅翻译目前只适用于图像或视频生产策略')
    if (modelGenerationModes.includes(ruleForm.generationMode)) {
      if (!ruleForm.generationSourceSelector) errors.push('主要内容来源必填')
      else if (!allowedGenerationSources.has(ruleForm.generationSourceSelector)) errors.push('主要内容来源是历史未登记值；请从服务端当前目录重新选择')
      else if (!compatibleGenerationSourceOptions.value.some((item) => item.value === ruleForm.generationSourceSelector)) errors.push('主要内容来源不适用于当前生成方式或投影原语')
    } else if (lockedGenerationModes.includes(ruleForm.generationMode) && ruleForm.generationSourceSelector !== ruleForm.sourceSelector) {
      errors.push('服务端锁定和原值透传会自动使用最终提示词取值，不能配置第二个内容来源')
    }
    if (['model_generated', 'translate_only'].includes(ruleForm.generationMode) && !ruleForm.generationInstruction.trim()) errors.push('模型生成或仅翻译必须填写版本化字段生成说明')
    if (/\{\{|\}\}|\$\{|<%|%>|\{%|%\}/.test(ruleForm.generationInstruction)) errors.push('字段生成说明不能包含模板表达式')
    if (!ruleForm.generationOutputSemantics) errors.push('输出语义（output semantics）必填')
    if (selectedProfile.value?.sourceProjectionKey === 'direct_v1' && ruleForm.generationOutputSemantics === 'contribution') errors.push('direct_v1 只接受 final；贡献值必须使用已配置兼容原语的 declarative_v1')
    if (ruleForm.generationOutputSemantics === 'contribution' && modelGenerationModes.includes(ruleForm.generationMode) && ruleForm.sourceSelector !== 'projection.output') errors.push('投影贡献必须选择“声明式投影输出”作为最终提示词取值，并由明确原语消费')
    if (ruleForm.generationOutputSemantics === 'contribution' && !['model_generated', 'translate_only'].includes(ruleForm.generationMode)) errors.push('贡献值（contribution）只允许模型生成或仅翻译')
    if (!validLanguagePolicy(ruleForm.languagePolicy)) errors.push('语言策略必须是受控关键字、BCP 47 标签或 bilingual:<tag>+<tag>')
    else if (!options.value.languagePolicies.some((item) => item.value === ruleForm.languagePolicy) && !options.value.customEntryFields.includes('language_policy')) errors.push('语言策略必须来自服务端目录')
    if (ruleForm.generationMode === 'translate_only' && ['preserve_source', 'any'].includes(ruleForm.languagePolicy)) errors.push('仅翻译（translate only）必须指定目标语言，不能保留来源或不限语言')
    if (!ruleForm.lengthUnit) errors.push('长度单位（length unit）必填')
    const minimum = optionalInteger(ruleForm.generationMin); const maximum = optionalInteger(ruleForm.generationMax)
    if (minimum !== null && (!Number.isInteger(minimum) || minimum < 1)) errors.push('生成下限必须为空或正整数')
    if (maximum !== null && (!Number.isInteger(maximum) || maximum < 1)) errors.push('生成上限必须为空或正整数')
    if (ruleForm.lengthUnit === 'none' && (minimum !== null || maximum !== null)) errors.push('不限制长度（none）时上下限必须留空')
    if (ruleForm.lengthUnit && ruleForm.lengthUnit !== 'none' && minimum === null && maximum === null) errors.push('选择长度单位后至少填写一个上下限')
    if (minimum !== null && maximum !== null && minimum > maximum) errors.push('生成下限不能大于生成上限')
    if (ruleForm.generationValidatorKeys.includes('non_empty') && !ruleForm.generationRequired) errors.push('non_empty 校验要求勾选“字段内容必须生成成功”')
    if (ruleForm.generationValidatorKeys.includes('json_object') && ruleForm.generationValidatorKeys.includes('json_array')) errors.push('json_object 与 json_array 不能同时选择')
    if ((ruleForm.generationValidatorKeys.includes('json_object') || ruleForm.generationValidatorKeys.includes('json_array')) && !ruleForm.generationValidatorKeys.includes('valid_json')) errors.push('json_object/json_array 必须同时选择 valid_json')
    if (ruleForm.generationValidatorKeys.includes('unique_items') && ruleForm.lengthUnit !== 'items') errors.push('unique_items 要求长度单位为 items')
    if (ruleForm.generationValidatorKeys.includes('length_advisory') && ruleForm.lengthUnit === 'none') errors.push('长度仅建议（length_advisory）要求选择长度单位并填写至少一个建议边界')
    if (ruleForm.generationValidatorKeys.includes('protected_tokens_preserve_count') && ruleForm.generationValidatorKeys.includes('protected_tokens_at_least_once')) errors.push('受保护内容的“保持来源次数”与“至少出现一次”策略不能同时选择')
  } else {
    const profile = profiles.value.find((item) => item.id === bindingForm.promptProfileVersion)
    if (!keyPattern.test(bindingForm.key) || !bindingForm.name.trim()) errors.push('Binding Key 和名称必填')
    if (!profile || profile.status !== 'published') errors.push('必须选择已发布 PromptProfile')
    if (profile && (bindingForm.operation !== profile.operation || bindingForm.modelFamily !== profile.modelFamily || bindingForm.mode !== profile.mode)) errors.push('Binding selector 必须与 PromptProfile 一致')
    if (!Number.isInteger(bindingForm.priority)) errors.push('优先级必须为整数')
    if (modelContextRequested.value && (!modelContextReady.value || bindingForm.modelProfileVersion !== modelContext.value?.id)) errors.push('绑定必须使用当前页面的精确模型版本')
  }
  return errors
})

const buildChannelLimitsPayload = (): JsonObject => Object.fromEntries(
  Object.entries(profileForm.channelLimits).flatMap(([channel, limit]) => (
    limit.enabled
      ? [[channel, {
          max_chars: Number(limit.maxChars),
          warn_at_chars: limit.warnAtChars === '' ? null : Number(limit.warnAtChars),
        }]]
      : []
  )),
)

const buildFieldPayload = (): JsonObject => ({
  key: fieldForm.key,
  label: fieldForm.label,
  category: fieldForm.category,
  description: fieldForm.description,
  impact: fieldForm.impact,
  value_type: fieldForm.valueType,
  default_value: parseFieldDefault(),
  value_options: fieldForm.valueType === 'enum' ? splitValues(fieldForm.valueOptionsText) : [],
  is_required: fieldForm.isRequired,
  ...(fieldForm.id ? { is_active: fieldForm.isActive } : {}),
})

const buildProfilePayload = (): JsonObject => ({
  profile_key: profileForm.profileKey,
  name: profileForm.name,
  description: profileForm.description,
  model_family: profileForm.modelFamily,
  operation: profileForm.operation,
  mode: profileForm.mode,
  source_projection_key: profileForm.sourceProjectionKey,
  source_projection_config: buildProjectionConfig(),
  official_reference: profileForm.officialReference,
  channel_limits: buildChannelLimitsPayload(),
  review_state: profileForm.reviewState,
})

const changedTopLevelPayload = (current: JsonObject, original: JsonObject): JsonObject => (
  Object.fromEntries(Object.entries(current).filter(([key, value]) => (
    JSON.stringify(value) !== JSON.stringify(original[key])
  ))) as JsonObject
)

const buildRulePayload = (): JsonObject => ({
  profile_version: ruleForm.profileVersion,
  field_definition: ruleForm.fieldDefinition,
  source_selector: ruleForm.sourceSelector,
  projection_primitive: ruleForm.projectionPrimitive,
  channel: ruleForm.channel,
  position: ruleForm.position,
  label_style: ruleForm.labelStyle,
  separator: ruleForm.separator,
  required: ruleForm.required,
  omit_if_empty: ruleForm.omitIfEmpty,
  max_chars: ruleForm.maxChars,
  generation_mode: ruleForm.generationMode,
  generation_source_selector: ruleForm.generationSourceSelector,
  generation_context_selectors: [...ruleForm.generationContextSelectors],
  generation_instruction: ruleForm.generationInstruction,
  generation_output_semantics: ruleForm.generationOutputSemantics,
  generation_required: ruleForm.generationRequired,
  language_policy: ruleForm.languagePolicy,
  length_unit: ruleForm.lengthUnit,
  generation_min: optionalInteger(ruleForm.generationMin),
  generation_max: optionalInteger(ruleForm.generationMax),
  generation_validator_keys: [...ruleForm.generationValidatorKeys],
})

const persistedRuleCandidatePayload = (rule: PromptProfileFieldRule): JsonObject => ({
  id: rule.id,
  field_definition: rule.fieldDefinition,
  source_selector: rule.sourceSelector,
  projection_primitive: rule.projectionPrimitive,
  channel: rule.channel,
  position: rule.position,
  label_style: rule.labelStyle,
  separator: rule.separator,
  required: rule.required,
  omit_if_empty: rule.omitIfEmpty,
  max_chars: rule.maxChars,
  generation_mode: rule.generationMode,
  generation_source_selector: rule.generationSourceSelector,
  generation_context_selectors: [...rule.generationContextSelectors],
  generation_instruction: rule.generationInstruction,
  generation_output_semantics: rule.generationOutputSemantics,
  generation_required: rule.generationRequired,
  language_policy: rule.languagePolicy,
  length_unit: rule.lengthUnit,
  generation_min: rule.generationMin,
  generation_max: rule.generationMax,
  generation_validator_keys: [...rule.generationValidatorKeys],
})

const submitEditor = async () => {
  if (editorValidation.value.length) return
  if (['field', 'profile', 'rule'].includes(editorKind.value) && !promptOptionsReady.value) {
    editorError.value = '服务端填写选项目录已失效，请重新读取后再提交。'
    return
  }
  saving.value = true; editorError.value = ''
  try {
    const reason = editorKind.value === 'field' ? 'prompt_field_config' : editorKind.value === 'profile' ? 'prompt_profile_config' : editorKind.value === 'rule' ? 'prompt_rule_config' : 'prompt_binding_config'
    if (editorKind.value === 'field') {
      let payload = buildFieldPayload()
      if (fieldForm.id) {
        delete payload.key
        const original = { ...fieldOriginalPayload.value }
        delete original.key
        payload = changedTopLevelPayload(payload, original)
        if (!Object.keys(payload).length) throw new Error('未检测到需要提交的通用字段变更。')
        await platformApi.updatePromptFieldDefinition(fieldForm.id, payload, reason)
      } else await platformApi.createPromptFieldDefinition(payload, reason)
    } else if (editorKind.value === 'profile') {
      let payload = buildProfilePayload()
      let saved: PromptProfileVersion
      if (profileForm.id) {
        delete payload.profile_key
        const original = { ...profileOriginalPayload.value }
        delete original.profile_key
        payload = changedTopLevelPayload(payload, original)
        if (!Object.keys(payload).length) throw new Error('未检测到需要提交的提示词策略变更。')
        saved = await platformApi.updatePromptProfileVersion(profileForm.id, payload, reason)
      } else {
        delete payload.review_state
        saved = await platformApi.createPromptProfileVersion(payload, reason)
      }
      selectedProfileId.value = saved.id
    } else if (editorKind.value === 'rule') {
      const payload = buildRulePayload()
      ruleForm.id ? await platformApi.updatePromptProfileFieldRule(ruleForm.id, payload, reason) : await platformApi.createPromptProfileFieldRule(payload, reason)
    } else {
      const payload: JsonObject = {
        key: bindingForm.key, name: bindingForm.name, operation: bindingForm.operation, model_family: bindingForm.modelFamily,
        mode: bindingForm.mode, model_profile_version: bindingForm.modelProfileVersion || null,
        prompt_profile_version: bindingForm.promptProfileVersion, priority: bindingForm.priority, is_active: bindingForm.isActive,
      }
      bindingForm.id ? await platformApi.updatePromptProfileBinding(bindingForm.id, payload, reason) : await platformApi.createPromptProfileBinding(payload, reason)
    }
    editorOpen.value = false; await refresh(); notice.value = '服务端已保存并重新读取 PromptProfile 事实。'
  } catch (cause) { editorError.value = message(cause) }
  finally { saving.value = false }
}

const openCloneSuccessor = () => {
  if (!selectedProfile.value) return
  cloneName.value = `${selectedProfile.value.name} · 后继草稿`
  cloneError.value = ''
  cloneOpen.value = true
}

const cloneSuccessor = async () => {
  const source = selectedProfile.value
  if (!source || !cloneName.value.trim()) return
  cloneBusy.value = true
  cloneError.value = ''
  try {
    const created = await platformApi.clonePromptProfileSuccessor(
      source.id,
      source.contractSha256,
      cloneName.value.trim(),
    )
    cloneOpen.value = false
    await refresh()
    selectedProfileId.value = created.id
    notice.value = `已从 v${source.version} 的精确合同创建 v${created.version} 草稿，并复制全部字段组合规则。`
  } catch (cause) { cloneError.value = message(cause) }
  finally { cloneBusy.value = false }
}

const runAction = async () => {
  const target = actionTarget.value
  if (!target) return
  saving.value = true; actionError.value = ''
  try {
    if (target.kind === 'publish-profile') await platformApi.publishPromptProfileVersion(target.id, 'prompt_profile_publish')
    else await platformApi.retirePromptProfileVersion(target.id, 'prompt_profile_retire')
    actionTarget.value = null; await refresh(); notice.value = '操作已由服务端校验并完成。'
  } catch (cause) { actionError.value = message(cause) }
  finally { saving.value = false }
}

const onRegistryDeletionCompleted = async () => {
  actionTarget.value = null
  await refresh()
  notice.value = '删除已按完整依赖清单原子完成。'
}

const inlinePreviewActive = computed(() => Boolean(
  editorOpen.value
  && (
    editorKind.value === 'rule'
    || (editorKind.value === 'profile' && profileForm.id)
  ),
))
const previewActive = computed(() => previewOpen.value || inlinePreviewActive.value)
const previewRulesForInputs = computed<PreviewRuleInput[]>(() => {
  const persisted: PreviewRuleInput[] = selectedRules.value.map((rule) => ({
    id: rule.id,
    fieldDefinition: rule.fieldDefinition,
    fieldKey: rule.fieldKey,
    fieldLabel: rule.fieldLabel,
    sourceSelector: rule.sourceSelector,
    required: rule.required,
  }))
  if (!inlinePreviewActive.value || editorKind.value !== 'rule' || !selectedRuleField.value) return persisted
  const candidate: PreviewRuleInput = {
    id: ruleForm.id || '__unsaved_candidate__',
    fieldDefinition: ruleForm.fieldDefinition,
    fieldKey: selectedRuleField.value.key,
    fieldLabel: selectedRuleField.value.label,
    sourceSelector: ruleForm.sourceSelector,
    required: ruleForm.required,
  }
  const replaced = persisted.map((rule) => rule.id === ruleForm.id ? candidate : rule)
  return ruleForm.id ? replaced : [...replaced, candidate]
})
const previewSourceRows = computed<PreviewSourceRow[]>(() => {
  const seen = new Set<string>()
  return previewRulesForInputs.value.flatMap((rule) => {
    if (rule.sourceSelector === 'definition.default') return []
    if (rule.sourceSelector === 'projection.output') return [{ selector: rule.sourceSelector, inputKey: `${rule.sourceSelector}.${rule.fieldKey}`, rule, field: fieldForRule(rule) }]
    if (seen.has(rule.sourceSelector)) return []
    seen.add(rule.sourceSelector)
    return [{ selector: rule.sourceSelector, inputKey: rule.sourceSelector, rule, field: fieldForRule(rule) }]
  })
})
const seedPreviewExamples = (rows: PreviewSourceRow[]) => {
  previewDraft.value = mergePromptProfilePreviewExamples(
    previewDraft.value,
    rows.map((row) => ({
      inputKey: row.inputKey,
      previewExample: row.field?.previewExample,
    })),
  )
}
const openPreview = () => {
  seedPreviewExamples(previewSourceRows.value)
  previewResult.value = null; previewError.value = ''; previewReadyMessage.value = ''; previewStale.value = false; previewOpen.value = true
}
const previewInputValue = (inputKey: string) => previewDraft.value[inputKey]
const previewInputDisplayValue = (inputKey: string) => {
  const value = previewInputValue(inputKey)
  return value === null || value === undefined ? '' : String(value)
}
const setPreviewInput = (inputKey: string, value: unknown) => { previewDraft.value = { ...previewDraft.value, [inputKey]: value } }
const resetPreviewInput = (row: PreviewSourceRow) => {
  setPreviewInput(
    row.inputKey,
    promptProfilePreviewExampleInputValue(row.field?.previewExample),
  )
}
const buildCandidateRulePayloads = (): JsonObject[] => {
  const persisted = selectedRules.value.map(persistedRuleCandidatePayload)
  if (!inlinePreviewActive.value || editorKind.value !== 'rule') return persisted
  const candidate = { ...buildRulePayload() }
  delete candidate.profile_version
  if (ruleForm.id) candidate.id = ruleForm.id
  const next = persisted.map((item) => item.id === ruleForm.id ? candidate : item)
  if (!ruleForm.id) next.push(candidate)
  return next.sort((left, right) => Number(left.position) - Number(right.position))
}
const buildPreviewCandidate = (): JsonObject | undefined => {
  const profile = selectedProfile.value
  if (!inlinePreviewActive.value || !profile) return undefined
  if (editorKind.value === 'profile') {
    const payload = buildProfilePayload()
    return {
      model_family: payload.model_family,
      operation: payload.operation,
      mode: payload.mode,
      source_projection_key: payload.source_projection_key,
      source_projection_config: payload.source_projection_config,
      channel_limits: payload.channel_limits,
      field_rules: buildCandidateRulePayloads(),
    }
  }
  return {
    model_family: profile.modelFamily,
    operation: profile.operation,
    mode: profile.mode,
    source_projection_key: profile.sourceProjectionKey,
    source_projection_config: profile.sourceProjectionConfig,
    channel_limits: Object.fromEntries(Object.entries(profile.channelLimits).map(([channel, limit]) => [channel, {
      max_chars: limit.maxChars,
      warn_at_chars: limit.warnAtChars,
    }])),
    field_rules: buildCandidateRulePayloads(),
  }
}
const buildPreviewSources = (): JsonObject => {
  const sources: JsonObject = {}
  const projectedValues: JsonObject = {}
  for (const row of previewSourceRows.value) {
    const raw = previewDraft.value[row.inputKey]
    if (raw === '' || raw === null || raw === undefined) continue
    if (row.selector === 'projection.output') {
      projectedValues[row.rule.fieldKey] = raw
      continue
    }
    if (row.field?.valueType === 'number') {
      const value = Number(raw)
      if (!Number.isFinite(value)) throw new Error(`${sourceLabel(row.selector)} 必须是数字`)
      sources[row.selector] = value
    } else if (row.field?.valueType === 'string_list') sources[row.selector] = splitValues(String(raw))
    else sources[row.selector] = raw
  }
  if (Object.keys(projectedValues).length) sources['projection.output'] = projectedValues
  return sources
}
const previewBlockedMessage = computed(() => {
  if (!inlinePreviewActive.value) return ''
  if (!selectedProfile.value) return '请先选择一个提示词策略版本。'
  if (editorValidation.value.length) return '当前配置尚未完整，修复左侧“保存前必须修复”项目后会自动重新组合。'
  return ''
})
let previewTimer: ReturnType<typeof setTimeout> | undefined
let previewRequestSerial = 0
const runPreview = async (requestSerial = ++previewRequestSerial) => {
  const profile = selectedProfile.value
  if (!profile || previewBlockedMessage.value) return
  previewBusy.value = true; previewError.value = ''; previewReadyMessage.value = '正在按当前设置重新组合…'
  try {
    const candidate = buildPreviewCandidate()
    const result = await platformApi.previewPromptProfileVersion(profile.id, {
      sources: buildPreviewSources(),
      ...(candidate ? { expectedBaseContractSha256: profile.contractSha256, candidate } : {}),
    })
    if (requestSerial !== previewRequestSerial) return
    previewResult.value = result
    previewStale.value = false
    previewReadyMessage.value = candidate ? '已按当前未保存设置组合' : '已按当前已保存合同组合'
  } catch (cause) {
    if (requestSerial !== previewRequestSerial) return
    previewError.value = message(cause)
    previewStale.value = Boolean(previewResult.value)
    previewReadyMessage.value = previewResult.value ? '上次有效预览；当前设置尚未应用' : ''
  } finally {
    if (requestSerial === previewRequestSerial) previewBusy.value = false
  }
}

const schedulePreview = () => {
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = undefined
  const requestSerial = ++previewRequestSerial
  if (!previewActive.value) {
    previewBusy.value = false
    previewReadyMessage.value = ''
    return
  }
  previewStale.value = Boolean(previewResult.value)
  if (previewStale.value) previewReadyMessage.value = '上次有效预览；等待当前设置重新组合'
  if (previewBlockedMessage.value) {
    previewBusy.value = false
    previewError.value = ''
    previewReadyMessage.value = previewResult.value ? '上次有效预览；当前设置尚未应用' : ''
    return
  }
  previewTimer = setTimeout(() => {
    previewTimer = undefined
    void runPreview(requestSerial)
  }, 400)
}

watch(previewSourceRows, (rows) => {
  seedPreviewExamples(rows)
}, { deep: true, immediate: true })
watch(selectedProfileId, () => {
  previewDraft.value = {}
  previewResult.value = null
  previewError.value = ''
  previewReadyMessage.value = ''
  previewStale.value = false
})
watch(() => ({
  active: previewActive.value,
  inputs: previewDraft.value,
  profile: inlinePreviewActive.value && editorKind.value === 'profile' ? { ...profileForm } : null,
  rule: inlinePreviewActive.value && editorKind.value === 'rule' ? { ...ruleForm } : null,
  persistedRules: selectedRules.value.map((rule) => `${rule.id}:${rule.updatedAt}`),
}), schedulePreview, { deep: true })

onMounted(refresh)
watch([() => props.bindingId, bindings, loading], async () => {
  if (!props.bindingId || loading.value) return
  const binding = bindings.value.find(item => item.id === props.bindingId)
  if (!binding) { error.value = '指定提示词绑定不存在或当前目录读取失败。'; return }
  showRetiredSettingsRecords.value = true
  await nextTick()
  document.getElementById(`prompt-binding-${binding.id}`)?.scrollIntoView({behavior:'smooth',block:'center'})
})
onBeforeUnmount(() => {
  refreshSerial += 1
  if (previewTimer) clearTimeout(previewTimer)
  previewRequestSerial += 1
})
</script>

<template>
  <div class="prompt-profile-settings">
    <section v-if="modelContextRequested" class="platform-toolbar">
      <div><strong>{{ modelContext ? `${modelContext.profileKey} v${modelContext.version} · 提示词关联` : '正在核对模型上下文' }}</strong><span>展示当前模型的精确绑定与模型族通用绑定；新增绑定会固定当前模型版本。策略目录显示同模型族候选，能力与模式由服务端保存时复核。</span><span v-if="modelContext&&modelContext.status!=='published'">当前模型未发布，暂不能新增绑定。</span></div>
      <div><RouterLink class="button ghost small" :to="{path:'/settings',query:{section:'models',model_profile_version:modelProfileVersionId,...($route.query.deployment?{deployment:$route.query.deployment}:{})}}">返回模型实例</RouterLink><RouterLink class="button ghost small" :to="{path:'/settings',query:{section:'prompts'}}">查看全部提示词策略</RouterLink></div>
    </section>
    <section class="platform-toolbar"><div><strong>提示词组装策略（PromptProfile）</strong><span>字段字典 → 精确版本后继草稿 → 页面业务组合规则 → 预览/审核 → 发布 → 模型绑定。投影引擎原语由代码登记；字段、顺序、贡献来源、格式和通道由当前版本页面配置并进入合同 SHA-256。</span></div><div><button class="button ghost small" :disabled="loading" @click="refresh"><RefreshCw :size="13" :class="{spin:loading}"/>重新读取</button><button class="button ghost small" :disabled="!promptOptionsReady" :title="promptOptionsReady?'':'字段目录读取失败'" @click="openField()"><Plus :size="13"/>字段</button><button class="button primary small" :disabled="!promptOptionsReady" :title="promptOptionsReady?'':'策略目录读取失败'" @click="openProfile()"><Plus :size="13"/>提示词策略（Profile）</button></div></section>
    <SettingsListFilter
      v-model:search="settingsListSearch"
      v-model:show-retired="showRetiredSettingsRecords"
      :visible-count="settingsListFilterSummary.visibleCount"
      :total-count="settingsListFilterSummary.totalCount"
      :hidden-retired-count="settingsListFilterSummary.hiddenRetiredCount"
      placeholder="搜索字段、策略、绑定、模型族、操作能力或状态"
    />
    <div v-if="error" class="platform-alert danger"><AlertTriangle :size="16"/><div><strong>提示词策略（PromptProfile）配置未完整读取</strong><p>{{ error }}</p></div></div>
    <div v-if="notice" class="platform-alert success"><Check :size="16"/><p>{{ notice }}</p></div>
    <div v-if="loading" class="platform-loading"><LoaderCircle class="spin" :size="20"/>正在读取字段、版本、规则与绑定…</div>

    <template v-else>
      <div class="prompt-catalog-grid">
        <section class="prompt-catalog-card">
          <header><div><FileText :size="16"/><span><strong>通用字段字典</strong><small>{{ filteredFields.length }} 个当前可见服务端字段</small></span></div></header>
          <div class="prompt-scroll-list"><article v-for="item in filteredFields" :key="item.id"><div><strong>{{ item.label }}</strong><small>{{ item.key }} · {{ item.category }} · {{ valueTypeLabel(item.valueType) }}{{ item.isRequired?' · 必填':'' }}</small><small>{{ item.description || '未填写含义' }}</small><small>影响：{{ item.impact || '未填写影响' }} · 默认 {{ item.defaultValue===null||item.defaultValue===undefined?'无':JSON.stringify(item.defaultValue) }}</small></div><PlatformStatusPill :status="item.isActive?'active':'inactive'"/><span><button class="button ghost tiny" :disabled="!promptOptionsReady" @click="openField(item)">编辑</button><button class="button danger tiny" @click="actionTarget={kind:'delete-field',id:item.id,label:`字段 ${item.label}`}"><Trash2 :size="11"/></button></span></article><p v-if="!filteredFields.length" class="platform-empty">没有匹配字段。</p></div>
        </section>

        <section class="prompt-catalog-card profile-list">
          <header><div><Settings2 :size="16"/><span><strong>提示词策略版本（Profile Version）</strong><small>{{ filteredProfiles.length }} 个当前可见不可变策略版本</small></span></div></header>
          <div class="prompt-scroll-list"><div v-for="item in filteredProfiles" :key="item.id"><button class="profile-select-row" :class="{active:selectedProfileId===item.id}" @click="selectedProfileId=item.id"><span><strong>{{ item.name }} · v{{ item.version }}</strong><small>{{ item.modelFamily }} · {{ operationLabel(item.operation) }}<template v-if="item.mode"> · {{ modeLabel(item.mode) }}</template></small></span><span><PlatformStatusPill :status="item.status"/><small>{{ item.fieldRuleCount }} 条规则（rules）</small></span></button><button class="button ghost tiny" :aria-label="`查看 ${item.name} v${item.version} 配置`" @click="openConfiguration(item)"><Eye :size="12"/>查看配置</button><RegistryRelationSummary :row="profileRelations.rows.value[item.id]" :loading="profileRelations.loading.value" :error="profileRelations.error.value" @retry="profileRelations.reload"/></div><p v-if="!filteredProfiles.length" class="platform-empty">尚无提示词策略（Profile）。</p></div>
        </section>

        <section class="prompt-catalog-card">
          <header><div><Link2 :size="16"/><span><strong>运行时绑定</strong><small>{{ filteredBindings.length }} 条当前可见选择规则</small></span></div><button class="button ghost tiny" :disabled="!promptOptionsReady||!publishedProfiles.length||(modelContextRequested&&modelContext?.status!=='published')" @click="openBinding()"><Plus :size="11"/>绑定</button></header>
          <div class="prompt-scroll-list">
            <div v-if="!publishedProfiles.length" class="prompt-prerequisite"><AlertTriangle :size="14"/><span>前置依赖缺失：请先发布适用的提示词策略，再新增绑定。</span></div>
            <article v-for="item in filteredBindings" :key="item.id" :id="`prompt-binding-${item.id}`"><div><strong>{{ item.name }}</strong><small>{{ operationLabel(item.operation) }} / {{ item.modelFamily }}<template v-if="item.mode"> / {{ modeLabel(item.mode) }}</template></small><small>{{ item.modelIdentifier || '模型族通用' }} · {{ item.promptProfileKey }} · 优先级 {{ item.priority }}</small><RegistryRelationSummary :row="bindingRelations.rows.value[item.id]" :loading="bindingRelations.loading.value" :error="bindingRelations.error.value" @retry="bindingRelations.reload"/></div><PlatformStatusPill :status="item.isActive?'active':'inactive'"/>
              <span v-if="!modelContextRequested||(item.modelProfileVersion===modelContext?.id&&modelContext?.status==='published')"><button class="button ghost tiny" :disabled="!promptOptionsReady" @click="openBinding(item)">编辑</button><button class="button danger tiny" :disabled="!promptOptionsReady" @click="actionTarget={kind:'delete-binding',id:item.id,label:`绑定 ${item.name}`}"><Trash2 :size="11"/></button></span>
              <small v-else>通用或历史绑定只读；可在全部策略中管理</small>
            </article>
            <p v-if="!filteredBindings.length" class="platform-empty">当前筛选下没有运行时绑定。</p>
          </div>
        </section>
      </div>

      <section v-if="selectedProfile" class="prompt-profile-workbench">
        <header><div><span class="eyebrow">提示词策略版本 / PROMPT PROFILE VERSION</span><h3>{{ selectedProfile.name }} · v{{ selectedProfile.version }}</h3><p>{{ selectedProfile.description || '未填写策略说明。' }}</p></div><div class="profile-actions"><button class="button ghost small" @click="openConfiguration(selectedProfile)"><Eye :size="12"/>查看配置</button><button class="button ghost small" @click="openPreview"><Eye :size="12"/>最终提示词预览</button><button v-if="canCloneSelectedProfile" class="button ghost small" @click="openCloneSuccessor"><Copy :size="12"/>创建后继草稿</button><button v-if="selectedProfile.status==='draft'" class="button ghost small" :disabled="!promptOptionsReady" @click="openProfile(selectedProfile)">编辑</button><button v-if="selectedProfile.status==='draft'" class="button primary small" :disabled="selectedProfile.reviewState!=='ready'||!selectedRules.length" @click="actionTarget={kind:'publish-profile',id:selectedProfile.id,label:`发布 ${selectedProfile.name} v${selectedProfile.version}`}"><ShieldCheck :size="12"/>发布</button><button v-if="selectedProfile.status==='published'" class="button ghost small" :disabled="profileBindings.some(item=>item.isActive)" @click="actionTarget={kind:'retire-profile',id:selectedProfile.id,label:`退役 ${selectedProfile.name} v${selectedProfile.version}`}"><Archive :size="12"/>退役</button><button v-if="selectedProfile.status==='draft'" class="button danger small" @click="actionTarget={kind:'delete-profile',id:selectedProfile.id,label:`Profile ${selectedProfile.name} v${selectedProfile.version}`}"><Trash2 :size="12"/>删除</button></div></header>
        <div class="profile-facts"><div><span>模型族</span><strong>{{ selectedProfile.modelFamily }}</strong></div><div><span>操作能力 / 模式（Operation / Mode）</span><strong>{{ operationLabel(selectedProfile.operation) }}{{ selectedProfile.mode?` / ${modeLabel(selectedProfile.mode)}`:'' }}</strong></div><div><span>来源投影器 / Source projection</span><strong>{{ choiceLabel(options.sourceProjectionKeys,selectedProfile.sourceProjectionKey) }} · {{ selectedProfile.sourceProjectionKey }}</strong></div><div><span>来源投影配置 / Projection config</span><strong>{{ Object.keys(selectedProfile.sourceProjectionConfig).length ? `schema v${selectedProfile.sourceProjectionConfig.schema_version || '?'}` : '未配置（无额外投影）' }}</strong></div><div><span>通道长度合同 / Channel limits</span><strong>{{ channelLimitSummary(selectedProfile.channelLimits) }}</strong></div><div><span>审核状态</span><PlatformStatusPill :status="selectedProfile.reviewState"/></div><div><span>冻结合同 / Frozen contract</span><strong>schema v{{ selectedProfile.contractSchemaVersion }} · {{ humanBytes(selectedProfile.contractSizeBytes) }} · {{ selectedProfile.contractSha256.slice(0,12) || '等待生成（pending）' }}</strong></div><div class="official-source"><span>官方来源（管理员登记）</span><a v-if="selectedProfile.officialReference" :href="selectedProfile.officialReference" target="_blank" rel="noopener noreferrer"><BookOpen :size="12"/>打开登记的官方说明</a><strong v-else>未录入；当前为内部通用规则</strong></div></div>

        <PromptPrimitiveConfiguration :key="selectedProfile.id" :profile="selectedProfile" :rules="selectedRules" :options="options" />
        <div class="rule-builder-header"><div><strong>业务组合规则：字段、顺序、格式与通道</strong><small>声明式规则从服务端登记的确定性原语取值；更换字段、顺序、标签、分隔符或通道都只生成新合同版本，不改写引擎代码。</small></div><button v-if="selectedProfile.status==='draft'" class="button primary small" :disabled="!activeFields.length||!promptOptionsReady" :title="promptOptionsReady?'':'生成上下文 options API 未就绪'" @click="openRule()"><Plus :size="12"/>添加字段规则</button></div>
        <div class="profile-rule-list">
          <article v-for="item in selectedRules" :key="item.id">
            <b>{{ item.position }}</b>
            <div>
              <strong>{{ item.fieldLabel || item.fieldKey }}</strong>
              <div class="rule-content-source">
                <span>{{ ruleContentMethod(item,hasRuleTemplates(selectedProfile,item,options)) }} → {{ item.fieldLabel || item.fieldKey }} → {{ choiceLabel(options.channels,item.channel) }}</span>
                <button v-if="item.sourceSelector==='projection.output'" type="button" class="button ghost small" :aria-label="`${item.fieldLabel || item.fieldKey}：查看相关模板与原语配置`" @click="openConfiguration(selectedProfile,item.id)"><Eye :size="12"/>{{ hasRuleTemplates(selectedProfile,item,options) ? '查看相关模板' : '查看原语配置' }}</button>
              </div>
              <small>{{ sourceLabel(item.sourceSelector) }}<template v-if="item.projectionPrimitive"> · {{ primitiveLabel(item.projectionPrimitive) }}</template> → {{ choiceLabel(options.channels,item.channel) }}</small>
              <small>{{ choiceLabel(options.labelStyles,item.labelStyle) }} · {{ choiceLabel(options.separators,item.separator) }} · 最大字符数 {{ item.maxChars }}<template v-if="item.required"> · 必填</template><template v-if="item.omitIfEmpty"> · 空值省略</template></small>
              <small>字段内容：{{ choiceLabel(options.generationModes,item.generationMode) }} · {{ choiceLabel(options.generationOutputSemantics,item.generationOutputSemantics) }} · {{ ruleGenerationSourceLabel(item) }} · {{ item.languagePolicy }} · {{ choiceLabel(options.lengthUnits,item.lengthUnit) }}<template v-if="generationRangeLabel(item)"> {{ generationRangeLabel(item) }}{{ item.generationValidatorKeys.includes('length_advisory') ? '（建议值）' : '（硬约束）' }}</template><template v-if="item.generationRequired"> · 内容必须生成成功</template></small>
              <small>生成上下文 / Generation context：{{ generationContextSummary(item.generationContextSelectors) }}</small>
              <small v-if="item.generationValidatorKeys.length">校验器 / Validators：{{ item.generationValidatorKeys.join(', ') }}</small>
            </div>
            <span v-if="selectedProfile.status==='draft'"><button class="button ghost tiny" :disabled="!promptOptionsReady" :title="promptOptionsReady?'':'生成上下文 options API 未就绪'" @click="openRule(item)">编辑</button><button class="button danger tiny" @click="actionTarget={kind:'delete-rule',id:item.id,label:`规则 ${item.fieldLabel || item.fieldKey}`}"><Trash2 :size="11"/></button></span>
          </article>
          <p v-if="!selectedRules.length" class="platform-empty">尚无字段规则。空的提示词策略（Profile）不能发布。</p>
        </div>
        <PromptWritingGuide scope="shot" :profile="selectedProfileWritingGuide"/>
      </section>
    </template>

    <BaseModal :open="editorOpen" :dismissible="!saving" :title="editorKind==='field'?(fieldForm.id?'编辑通用字段':'新增通用字段'):editorKind==='profile'?(profileForm.id?'编辑提示词策略（PromptProfile）':'新增提示词策略（PromptProfile）'):editorKind==='rule'?(ruleForm.id?'编辑字段组合规则':'添加字段组合规则'):(bindingForm.id?'编辑运行时绑定':'新增运行时绑定')" eyebrow="页面业务规则 · 服务端校验" :width="inlinePreviewActive?'1180px':'860px'" @close="editorOpen=false">
      <div :class="{'prompt-editor-live-layout':inlinePreviewActive}">
        <div class="prompt-editor-config-pane">
      <div v-if="editorKind==='field'" class="prompt-editor-form">
        <p>通用字段只描述类型与含义，不执行模板代码。被已发布的提示词策略（Profile）引用后不可原地改写。</p>
        <div class="prompt-form-grid">
          <label>唯一键（Key）<i>必填</i><input v-model="fieldForm.key" :disabled="Boolean(fieldForm.id)" placeholder="camera.movement"/><small>稳定机器标识，小写字母开头；创建后由系统锁定。</small></label>
          <label>显示名称 <i>必填</i><input v-model="fieldForm.label" placeholder="镜头运动"/></label>
          <label>分类 <i>必填</i><CatalogChoiceField v-model="fieldForm.category" :options="options.fieldCategories" :allow-custom="options.customEntryFields.includes('field_category')" custom-label="登记新分类（高级）" custom-placeholder="例如 camera" pattern="^[a-z][a-z0-9_.-]{1,79}$"/></label>
          <label>值类型 <i>必填</i><select v-model="fieldForm.valueType"><option value="">请选择，不自动猜测</option><option v-for="item in options.fieldValueTypes" :key="item.value" :value="item.value">{{ item.label }}</option></select></label>
          <label class="wide">含义说明 <i>必填</i><textarea v-model="fieldForm.description" rows="3" placeholder="说明输入内容、空值语义和填写方式"/></label>
          <label class="wide">影响说明 <i>必填</i><textarea v-model="fieldForm.impact" rows="3" placeholder="说明它会影响哪些模型输出、通道或任务"/></label>
          <label v-if="fieldForm.valueType==='enum'" class="wide">枚举选项 <i>必填</i><textarea v-model="fieldForm.valueOptionsText" rows="3" placeholder="每行一项"/></label>
          <label class="check wide"><input v-model="fieldForm.hasDefault" type="checkbox"/><span>配置管理员默认值（可作为 definition.default 安全来源）</span></label>
          <label v-if="fieldForm.hasDefault&&fieldForm.valueType==='boolean'">默认值<select v-model="fieldForm.defaultBoolean"><option :value="true">是（true）</option><option :value="false">否（false）</option></select></label>
          <label v-else-if="fieldForm.hasDefault" class="wide">默认值<textarea v-if="fieldForm.valueType==='long_text'||fieldForm.valueType==='string_list'" v-model="fieldForm.defaultText" rows="3"/><select v-else-if="fieldForm.valueType==='enum'" v-model="fieldForm.defaultText"><option value="">请选择</option><option v-for="value in splitValues(fieldForm.valueOptionsText)" :key="value" :value="value">{{ value }}</option></select><input v-else v-model="fieldForm.defaultText" :type="fieldForm.valueType==='number'?'number':'text'"/></label>
          <label>排序<output>{{ fieldForm.id ? fieldForm.displayOrder : '保存时由服务端追加' }}</output></label>
          <label class="check"><input v-model="fieldForm.isRequired" type="checkbox"/><span>字段必填</span></label>
          <label v-if="fieldForm.id" class="check"><input v-model="fieldForm.isActive" type="checkbox"/><span>启用字段</span></label>
          <label v-else>生命周期<output>创建后自动启用</output></label>
        </div>
      </div>

      <div v-else-if="editorKind==='profile'" class="prompt-editor-form">
        <p>提示词策略（Profile）发布后合同冻结；模型官方文档可以作为审核来源，没有时必须明确使用内部通用规则。</p>
        <div class="prompt-form-grid">
          <label>策略唯一键（Profile Key）<i>必填</i><input v-model="profileForm.profileKey" :disabled="Boolean(profileForm.id)" placeholder="reference-video-policy"/></label>
          <label>版本<output>{{ profileForm.id ? `v${profileForm.version}` : '保存时由服务端分配 v1' }}</output><small>后续版本只通过“创建后继草稿”产生，不能人工填写。</small></label>
          <label>名称 <i>必填</i><input v-model="profileForm.name"/></label>
          <label>模型族 <i>必填</i><select v-model="profileForm.modelFamily" :disabled="modelContextRequested"><option value="">请选择</option><option v-for="value in options.modelFamilies" :key="value" :value="value">{{ value }}</option></select></label>
          <label>操作能力（Operation）<i>必填</i><CatalogChoiceField v-model="profileForm.operation" :options="operationCatalog" :allow-custom="options.customEntryFields.includes('operation')" custom-label="登记新 Operation（高级）" custom-placeholder="例如 video.generate" pattern="^[a-z][a-z0-9_.-]{1,99}$"/><small>日常配置从服务端目录选择；只有服务端明确授权登记平台尚未出现的新能力时才显示高级入口。发布仍要求受控 registry、模型 capability 或 Provider driver 证据。</small></label>
          <label>生成模式（Mode）<CatalogChoiceField v-model="profileForm.mode" :options="modeCatalog" :allow-custom="options.customEntryFields.includes('mode')" empty-label="通用（不限定 Mode）" custom-label="登记新 Mode（高级）" custom-placeholder="例如 Ref2VA" pattern="^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$"/><small>已有模式从服务端目录选择；留空表示通用，新模式只有在服务端授权时才能显式登记。</small></label>
          <label class="wide">来源投影器 / Source projection <i>必填</i><select v-model="profileForm.sourceProjectionKey" :disabled="profileProjectionLocked"><option value="">请选择，不自动猜测</option><option v-for="item in options.sourceProjectionKeys" :key="item.value" :value="item.value">{{ item.label }} · {{ item.value }}</option></select><small v-if="profileProjectionLocked">当前草稿已有字段规则，来源投影器已锁定，避免遗留不兼容的配置或规则；如需更换，请创建新的无规则草稿。</small><small v-else>普通字段直装配选择 direct_v1；需要把确定性原语按业务字段组合时选择 declarative_v1。旧内置渲染策略仅供历史查看；执行前需要复核发布已配置模板的后继版本。</small></label>
          <fieldset v-if="profileForm.sourceProjectionKey==='declarative_v1'" class="projection-config-fieldset wide">
            <legend>参考素材与镜头的文案模板</legend>
            <p class="rendering-intro">占位符就是“留给实际数据的位置”：例如 {label} 会换成素材标签，括号外的文字原样保留。平台按固定标识取值，不会猜测你写的句子是什么意思。</p>
            <details class="rendering-guide">
              <summary>第一次配置？查看操作步骤与组装关系</summary>
              <ol>
                <li>先找相近的已发布策略，用“查看配置”参考，再“创建后继草稿”；不需要从零编写。</li>
                <li>按每段的用途写普通句子，点击中文按钮插入动态内容。例如先写“Reference: ”，再插入“素材标签”，得到“Reference: {label}”。不要把示例中的具体素材标签写死。</li>
                <li>模板只负责生成段落。在版本详情的“业务组合规则”中添加或编辑字段：“最终提示词取值”选择“声明式投影输出（projection.output）”，再选择“用于得到该字段的确定性原语”，配置排序、标签、分隔符与输出通道。模板本身不决定段落在最终提示词里的位置。</li>
                <li>保存草稿，点击“最终提示词预览”核对字段排版和通道。该预览使用示例字段值，不验证真实素材替换；还需结合真实项目和镜头验收。审核发布后，显式切换运行时绑定，才会被后续任务选用。</li>
              </ol>
              <p>以参考素材声明为例：实际素材绑定 → 替换 reference 模板 → “参考主体定义”原语 → 字段组合规则 → 正向提示词等输出通道。</p>
              <p>这里只支持本段按钮列出的 {英文标识}，区分大小写。不支持表达式、脚本、函数或自定义正则，也不能做条件判断或 {subject.name} 这类属性访问。要原样输出花括号，请写 {{ '{' + '{' }} 和 {{ '}' + '}' }}。</p>
              <p>模板替换本身不翻译文字；实际取值可能来自已确认译文。固定文字不会随空值自动隐藏，例如没有配对视频时 {parent_label} 为空，但“paired video=”仍会保留；不需要的固定说明请自行删去。完整模板与字段规则随发布版本冻结。</p>
            </details>
            <div class="projection-config-grid">
              <label>提示词模板标识 <i>必填</i><input v-model="profileForm.renderingTemplateKey"/><small>随策略版本冻结；需满足工作流允许的模板标识。</small></label>
              <label>段内分隔符 <i>必填</i><select v-model="profileForm.renderingSeparator"><option value="">请选择</option><option :value="'\n'">换行</option><option :value="' '">空格</option><option :value="'; '">分号与空格</option><option v-if="profileForm.renderingSeparator && !['\n', ' ', '; '].includes(profileForm.renderingSeparator)" :value="profileForm.renderingSeparator">保留当前自定义值</option></select></label>
              <label>列表前缀<input v-model="profileForm.renderingListPrefix"/><small>可留空；例如横线与空格。</small></label>
              <label>任务名称分隔符<input v-model="profileForm.renderingTaskSeparator"/><small>按真实用途去重后连接，例如“ + ”。</small></label>
              <label>无素材时的任务名称<input v-model="profileForm.renderingUnreferencedTask"/><small>明确声明不传参考素材时的任务名称，不会按模型猜测。</small></label>
              <fieldset class="wide"><legend>参考用途规则</legend>
                <p>类别与用途必须精确匹配；用途留空仅匹配未声明用途。主体图片通常没有用途，视频和音频的用途请与模型合同一致。未匹配会阻止编译，不会套用任意默认值。</p>
                <div v-for="(row, index) in profileForm.renderingReferenceRules" :key="index" class="projection-config-grid">
                  <label>规则 {{ index + 1 }} · 素材类别<input v-model="row.category" :disabled="saving"/></label>
                  <label>规则 {{ index + 1 }} · 参考用途<input v-model="row.purpose" :disabled="saving"/></label>
                  <label>规则 {{ index + 1 }} · 任务名称<input v-model="row.task" :disabled="saving"/></label>
                  <label>规则 {{ index + 1 }} · 保留方式<input v-model="row.relationship" :disabled="saving"/></label>
                  <label class="wide">规则 {{ index + 1 }} · 使用说明<textarea v-model="row.usage" :disabled="saving" rows="2"/></label>
                  <button type="button" class="button ghost tiny" :disabled="saving" @click="profileForm.renderingReferenceRules.splice(index, 1)">移除规则 {{ index + 1 }}</button>
                </div>
                <button type="button" class="button ghost" :disabled="saving" @click="profileForm.renderingReferenceRules.push({ category: '', purpose: '', task: '', relationship: '', usage: '' })">添加参考用途规则</button>
              </fieldset>
              <PromptRenderingTemplateEditor v-for="item in options.referenceRenderingFields" :key="item.key" v-model="profileForm.renderingTemplates[item.key]" :item="item" :disabled="saving" />
            </div>
            <details class="projection-quality-details">
              <summary>质量建议（仅告警，可选）</summary>
              <div class="projection-config-grid">
                <label class="check"><input v-model="profileForm.overlapEnabled" type="checkbox"/><span>检查正负词重叠</span></label>
                <template v-if="profileForm.overlapEnabled"><label>最少共享词数<input v-model.number="profileForm.overlapMinimumSharedTokens" type="number" min="1"/></label><label>最小重叠比例<input v-model.number="profileForm.overlapMinimumRatio" type="number" min="0.01" max="1" step="0.01"/></label></template>
                <label class="check"><input v-model="profileForm.densityEnabled" type="checkbox"/><span>检查短镜头动作密度</span></label>
                <template v-if="profileForm.densityEnabled"><label>最大镜头时长（ms）<input v-model.number="profileForm.densityMaximumDurationMs" type="number" min="1"/></label><label>最大动作字符数<input v-model.number="profileForm.densityMaximumActionCharacters" type="number" min="1"/></label><label>最少分句符数量<input v-model.number="profileForm.densityMinimumClauseSeparators" type="number" min="1"/></label></template>
                <label class="check"><input v-model="profileForm.motionGuidanceEnabled" type="checkbox"/><span>检查动态参考说明完整度</span></label>
                <template v-if="profileForm.motionGuidanceEnabled"><label>最少英文词数<input v-model.number="profileForm.motionMinimumEnglishWords" type="number" min="1"/></label><label class="wide">视为通用占位的值<textarea v-model="profileForm.motionGenericValuesText" rows="4" placeholder="每行一项"/></label></template>
              </div>
            </details>
          </fieldset>
          <details class="channel-limit-fieldset profile-advanced wide">
            <summary>最终通道长度合同 / Final Channel Limits（高级，可选）</summary>
            <small>按通道启用；未启用表示该 Profile 对该通道不设隐藏上限。max_chars 会阻断超限装配，warn_at_chars 只生成审核预警。全部进入当前合同 schema 与 SHA-256。</small>
            <div v-for="item in options.channels" :key="item.value" class="channel-limit-row">
              <label class="check"><input v-model="profileForm.channelLimits[item.value].enabled" type="checkbox"/><span>{{ item.label }} · {{ item.value }}</span></label>
              <label>最大字符数 / max_chars<input v-model.number="profileForm.channelLimits[item.value].maxChars" :disabled="!profileForm.channelLimits[item.value].enabled" type="number" min="1" step="1"/></label>
              <label>预警字符数 / warn_at_chars<input v-model.number="profileForm.channelLimits[item.value].warnAtChars" :disabled="!profileForm.channelLimits[item.value].enabled" type="number" min="1" step="1" placeholder="可留空"/></label>
            </div>
          </details>
          <label class="wide">策略说明<textarea v-model="profileForm.description" rows="3"/></label>
          <label class="wide">官方参考地址（URL，管理员核验）<input v-model="profileForm.officialReference" type="url" placeholder="https://..."/><small>请确认链接确属模型官方文档；留空时页面会标记为“内部通用规则”，不伪造官方来源。</small></label>
          <label v-if="profileForm.id">审核状态<select v-model="profileForm.reviewState"><option value="needs_review">待审核</option><option value="ready">已审核，可发布</option></select></label>
          <label v-else>审核状态<output>待审核（needs_review）</output><small>新策略由系统固定为待审核，保存后再完成审核。</small></label>
        </div>
      </div>

      <div v-else-if="editorKind==='rule'" class="prompt-editor-form">
        <p>先说明这个字段的内容从哪里来、如何生成；再决定它在最终提示词中的顺序和格式。原语能力由服务端代码登记，业务组合规则由这里配置。</p>
        <div class="prompt-form-grid">
          <label>字段 / Field <i>必填</i><select v-model="ruleForm.fieldDefinition" :disabled="Boolean(ruleForm.id)"><option value="">请先选择已启用字段</option><option v-for="item in activeFields" :key="item.id" :value="item.id">{{ item.label }} · {{ item.key }}</option></select><small>{{ selectedRuleField?.description || '选择字段后显示含义' }}</small></label>
          <div class="generation-contract-heading wide"><strong>字段内容如何获得</strong><small>设置内容生成方式、主要内容来源、可参考的已确认信息、语言、长度和校验规则。这里不决定最终段落顺序与格式。</small></div>
          <label>内容生成方式 <i>必填</i><select v-model="ruleForm.generationMode" @focus="rememberGenerationModeBeforeUserChange" @change="onRuleGenerationModeChange"><option value="">请选择，不自动猜测</option><option v-if="generationModeOperationUnavailable" :value="ruleForm.generationMode" disabled>当前 operation 不执行此历史生成方式 · {{ ruleForm.generationMode }}</option><option v-for="item in ruleGenerationModeOptions" :key="item.value" :value="item.value">{{ item.label }} · {{ item.value }}</option></select><small v-if="selectedProfileSupportsModelGeneration">图像或视频生产策略可选择服务端锁定、模型生成、仅翻译或原值透传。</small><small v-else>当前 operation 只提供服务端锁定或原值透传；字段级模型生成目前由图像/视频 ProductionRun 执行。</small></label>
          <label>生成结果如何使用 <i>必填</i><select v-model="ruleForm.generationOutputSemantics"><option v-for="item in options.generationOutputSemantics" :key="item.value" :value="item.value" :disabled="selectedProfile?.sourceProjectionKey==='direct_v1'&&item.value==='contribution'">{{ item.label }} · {{ item.value }}</option></select><small v-if="selectedProfile?.sourceProjectionKey==='direct_v1'">直接装配只允许最终字段值；贡献值需要声明式投影原语合成。</small><small v-else>“最终字段值”可直接排版；“投影贡献”会先交给所选确定性原语合成。</small></label>
          <label v-if="modelGenerationSourceMode">主要内容来源 <i>必填</i><select v-model="ruleForm.generationSourceSelector" :disabled="contributionSourceSelectionBlocked"><option value="">{{ contributionSourceSelectionBlocked ? '请先在下方选择声明式投影输出与确定性原语' : '请选择服务端已登记的业务事实' }}</option><option v-if="generationSourceUnknown" :value="ruleForm.generationSourceSelector" disabled>历史未登记来源 · {{ ruleForm.generationSourceSelector }}</option><option v-else-if="generationSourceIncompatible" :value="ruleForm.generationSourceSelector" disabled>当前组合不兼容 · {{ ruleForm.generationSourceSelector }}</option><optgroup v-for="group in generationSourceGroups" :key="group.key" :label="group.label"><option v-for="item in group.items" :key="item.value" :value="item.value">{{ item.label }} · {{ item.value }}</option></optgroup></select><small v-if="contributionSourceSelectionBlocked">投影贡献必须先明确由哪个原语消费；完成下方“最终提示词取值”和“确定性原语”后，这里只显示该原语支持的来源。</small><small v-else>这不是文件地址或数据库路径。它决定模型生成当前字段时首先读取哪一项已确认业务事实；相邻镜头、全剧和对白正文在“额外参考信息”中另行选择。</small></label>
          <label v-else-if="lockedGenerationSourceMode">主要内容来源<select :value="ruleForm.generationSourceSelector" disabled><option :value="ruleForm.generationSourceSelector">{{ sourceLabel(ruleForm.generationSourceSelector) }} · {{ ruleForm.generationSourceSelector }}</option></select><small>当前生成方式不会启动字段生成任务，因此无需第二次选择；服务端会自动使用下方“最终提示词取值”。</small></label>
          <label v-else>主要内容来源<select disabled><option>请先选择内容生成方式</option></select><small>选择“模型生成”或“仅翻译”后可从受控业务事实中选择；其他方式会自动跟随最终提示词取值。</small></label>
          <div v-if="modelGenerationSourceMode && selectedGenerationSourceOption && !generationSourceIncompatible" class="generation-source-explanation wide"><header><strong>{{ selectedGenerationSourceOption.label }}</strong><code>{{ selectedGenerationSourceOption.value }}</code></header><dl><div><dt>读取什么</dt><dd>{{ selectedGenerationSourceOption.description }}</dd></div><div><dt>什么时候可用</dt><dd>{{ selectedGenerationSourceOption.availability }}</dd></div><div><dt>作用范围</dt><dd>{{ selectedGenerationSourceOption.scope }}</dd></div></dl></div>
          <div v-else-if="generationSourceUnknown" class="generation-source-warning wide"><AlertTriangle :size="14"/><span>此规则保留了当前目录已不再登记的历史来源，仅供审计；当前草稿必须从下拉目录重新选择后才能保存或预览。</span></div>
          <div v-else-if="generationSourceIncompatible" class="generation-source-warning wide"><AlertTriangle :size="14"/><span>这个来源仍在当前目录中，但不适用于所选生成方式或投影原语；请调整当前组合。</span></div>
          <details class="rule-advanced wide" :open="!ruleForm.id||!ruleForm.languagePolicy||!ruleForm.lengthUnit||['model_generated','translate_only'].includes(ruleForm.generationMode)"><summary>生成时可参考的信息、语言、长度与校验</summary><div class="projection-config-grid">
          <fieldset class="generation-context-fieldset wide"><legend>生成时可参考的额外信息（多选）</legend><small>不勾选表示只使用上方选定的主要内容来源。相邻镜头、全剧或已确认对白不会自动加入，必须显式选择；对白元数据不含对白正文。候选值及说明全部来自服务端 options API。</small><div v-if="options.generationContextSelectors.length"><label v-for="item in options.generationContextSelectors" :key="item.value" class="check"><input v-model="ruleForm.generationContextSelectors" type="checkbox" :value="item.value"/><span><strong>{{ item.label }}</strong><small>{{ item.description || '服务端未提供该选项说明 / Description unavailable' }}</small></span></label></div><p v-else class="platform-empty">服务端未返回候选信息；请刷新配置，不能用自由 JSON 绕过受控目录。</p></fieldset>
          <label class="wide">字段生成说明 / Generation instruction <i v-if="['model_generated','translate_only'].includes(ruleForm.generationMode)">必填</i><textarea v-model="ruleForm.generationInstruction" rows="4" placeholder="说明该目标字段应从已冻结来源提取或生成什么，不要填写模板、脚本或变量表达式。"/><small>这是进入 schema v6 合同与 SHA-256 的不可执行说明文本；整个版本合同仍受服务端绝对安全包络保护。</small></label>
          <label>语言策略 / Language policy <i>必填</i><CatalogChoiceField v-model="ruleForm.languagePolicy" :options="options.languagePolicies" :allow-custom="options.customEntryFields.includes('language_policy')" custom-label="填写 BCP 47 / bilingual（高级）" custom-placeholder="例如 zh-CN 或 bilingual:zh-CN+en"/><small>日常规则从服务端目录选择；服务端授权时，BCP 47 与 bilingual 开放标准可通过显式高级入口填写。</small></label>
          <label>长度单位 / Length unit <i>必填</i><select v-model="ruleForm.lengthUnit"><option value="">请选择，不自动猜测</option><option v-for="item in options.lengthUnits" :key="item.value" :value="item.value">{{ item.label }} · {{ item.value }}</option></select><small>字符、英文词数、列表项，或明确选择不限制（none）。</small></label>
          <label>生成下限 / Minimum<input v-model.number="ruleForm.generationMin" :disabled="!ruleForm.lengthUnit||ruleForm.lengthUnit==='none'" type="number" min="1" step="1" placeholder="可留空"/></label>
          <label>生成上限 / Maximum<input v-model.number="ruleForm.generationMax" :disabled="!ruleForm.lengthUnit||ruleForm.lengthUnit==='none'" type="number" min="1" step="1" placeholder="可留空"/></label>
          <label class="check wide"><input v-model="ruleForm.generationRequired" type="checkbox"/><span>字段内容必须生成成功</span><small>这是内容生成阶段的约束，与下方“缺少字段时阻止最终拼装”分开记录。</small></label>
          <fieldset class="generation-validator-fieldset wide"><legend>受控校验器 / Controlled validators</legend><small>只能勾选服务端登记的通用校验器；不能输入代码或自定义正则。长度仅建议（length_advisory）会把上下限变成审计警告，不阻断生成；必填、语言、JSON、受保护内容、控制字符和未解析占位符仍是硬门禁。受保护内容的两种计数策略不能同时选择。</small><div><label v-for="item in options.generationValidators" :key="item.value" class="check"><input v-model="ruleForm.generationValidatorKeys" type="checkbox" :value="item.value"/><span>{{ item.label }} · {{ item.value }}</span></label></div></fieldset>
          </div></details>

          <div class="assembly-contract-heading wide"><strong>最终提示词如何排版</strong><small>把已经得到的字段内容按指定通道、顺序、标签和分隔方式拼成模型实际接收的提示词。</small></div>
          <label>最终提示词取值 <i>必填</i><select v-model="ruleForm.sourceSelector"><option v-for="item in ruleSourceOptions.filter(row=>ruleForm.channel!=='system_instruction'||row.value==='definition.default')" :key="item.value" :value="item.value">{{ item.label }} · {{ item.value }}</option></select><small>声明式派生字段选择“声明式投影输出”；普通字段可直接选择已确认来源。</small></label>
          <label v-if="ruleForm.sourceSelector==='projection.output'">用于得到该字段的确定性原语 <i>必填</i><select v-model="ruleForm.projectionPrimitive"><option value="">{{ compatibleProjectionPrimitiveOptions.length ? '请选择原语' : '当前策略未配置可用原语引擎' }}</option><option v-for="item in compatibleProjectionPrimitiveOptions" :key="item.value" :value="item.value">{{ item.label }} · {{ item.value }}</option></select><small>{{ compatibleProjectionPrimitiveOptions.find(item=>item.value===ruleForm.projectionPrimitive)?.description || '这里只能选择服务端已登记、且当前策略已启用的原语。' }}</small></label>
          <label>输出到哪个通道 <i>必填</i><select v-model="ruleForm.channel"><option v-for="item in options.channels" :key="item.value" :value="item.value">{{ item.label }} · {{ item.value }}</option></select></label>
          <label>在最终提示词中的顺序 <i>必填</i><input v-model.number="ruleForm.position" type="number" min="1" step="1"/></label>
          <label>字段名显示方式<select v-model="ruleForm.labelStyle"><option v-for="item in options.labelStyles" :key="item.value" :value="item.value">{{ item.label }} · {{ item.value }}</option></select></label>
          <label>与上一段如何分隔<select v-model="ruleForm.separator"><option v-for="item in options.separators" :key="item.value" :value="item.value">{{ item.label }} · {{ item.value }}</option></select></label>
          <label>这一段最多字符数<input v-model.number="ruleForm.maxChars" type="number" min="1" step="1"/></label>
          <label class="check"><input v-model="ruleForm.required" type="checkbox"/><span>缺少此字段时阻止生成最终提示词</span></label>
          <label class="check"><input v-model="ruleForm.omitIfEmpty" type="checkbox"/><span>字段为空时不输出这一段</span></label>
        </div>
      </div>

      <div v-else class="prompt-editor-form">
        <p>绑定（Binding）只能引用已发布（published）的提示词策略（Profile）。选择策略后，操作能力、模型族和模式由该冻结版本派生，不再伪装成可填写输入。</p>
        <div class="prompt-form-grid">
          <label>绑定唯一键（Binding Key）<i>必填</i><input v-model="bindingForm.key" :disabled="Boolean(bindingForm.id)" placeholder="reference-video-default"/><small v-if="bindingForm.id">Binding Key 创建后不可修改；如需新 Key，请新建绑定并停用旧绑定。</small></label>
          <label>名称 <i>必填</i><input v-model="bindingForm.name"/></label>
          <label class="wide">已发布提示词策略（Published PromptProfile）<i>必填</i><select v-model="bindingForm.promptProfileVersion" @change="syncBindingProfile"><option value="">请选择，不自动猜测</option><option v-for="item in publishedProfiles" :key="item.id" :value="item.id">{{ item.name }} v{{ item.version }} · {{ item.modelFamily }} / {{ operationLabel(item.operation) }}{{ item.mode?` / ${modeLabel(item.mode)}`:'' }}</option></select></label>
          <label>操作能力（Operation）<output>{{ bindingForm.operation ? operationLabel(bindingForm.operation) : '选择策略后由系统带入' }}</output></label>
          <label>模型族<output>{{ bindingForm.modelFamily || '选择策略后由系统带入' }}</output></label>
          <label>生成模式（Mode）<output>{{ bindingForm.mode ? modeLabel(bindingForm.mode) : bindingForm.promptProfileVersion ? '通用' : '选择策略后由系统带入' }}</output></label>
          <label class="wide">精确模型版本<select v-model="bindingForm.modelProfileVersion" :disabled="modelContextRequested"><option v-if="!modelContextRequested" value="">不限实际模型，按模型族匹配</option><option v-for="item in options.modelProfileVersions.filter(item=>!modelContextRequested||item.id===modelContext?.id)" :key="item.id" :value="item.id">{{ item.profileKey }} v{{ item.version }} · {{ item.modelIdentifier }} · {{ mediaKindLabel(item.modality) }}</option></select><small>{{ modelContextRequested?'已固定为进入页面时的模型版本。':'服务端会复核实际模型族；不匹配不会保存。' }}</small></label>
          <label>优先级<input v-model.number="bindingForm.priority" type="number" step="1"/></label>
          <label class="check"><input v-model="bindingForm.isActive" type="checkbox"/><span>启用绑定</span></label>
        </div>
      </div>
        </div>

        <aside v-if="inlinePreviewActive" class="prompt-live-preview">
          <header><Eye :size="16"/><div><strong>实时最终提示词</strong><small>当前未保存设置 · 400ms 后自动组合</small></div></header>
          <p>服务端已自动填入通用示例并按当前未保存配置即时拼装；示例不会保存，可以修改，也不会调用模型。</p>
          <div class="preview-scope-note"><AlertTriangle :size="14"/><span><template v-if="editorKind==='rule' && ruleForm.generationSourceSelector">当前候选主要内容来源：{{ resolvedGenerationSourceLabel(ruleForm.generationMode, ruleForm.generationSourceSelector, ruleForm.sourceSelector) }}。</template> 这里只执行“最终提示词如何排版”；通用示例代表字段或声明式原语已经得到的结果，不会伪装成真实字段生成。参考模板的真实替换与原语参数仍需结合实际项目和镜头验收。</span></div>
          <details class="preview-samples" :open="!previewResult">
            <summary>服务端通用示例（可按需修改）</summary>
            <div class="preview-source-grid single-column">
              <div v-for="row in previewSourceRows" :key="row.inputKey" class="preview-sample-item">
                <label>
                  <span>{{ row.selector==='projection.output' ? `假设「${row.rule.fieldLabel || row.rule.fieldKey}」已经得到` : `假设「${sourceLabel(row.selector)}」为` }}</span>
                  <select v-if="row.field?.valueType==='boolean'" :value="previewInputDisplayValue(row.inputKey)" @change="setPreviewInput(row.inputKey,($event.target as HTMLSelectElement).value===''?'':($event.target as HTMLSelectElement).value==='true')"><option value="">留空</option><option value="false">否（false）</option><option value="true">是（true）</option></select>
                  <textarea v-else-if="row.field?.valueType==='long_text'||row.field?.valueType==='string_list'" :value="previewInputDisplayValue(row.inputKey)" rows="3" @input="setPreviewInput(row.inputKey,($event.target as HTMLTextAreaElement).value)"/>
                  <select v-else-if="row.field?.valueType==='enum'" :value="previewInputDisplayValue(row.inputKey)" @change="setPreviewInput(row.inputKey,($event.target as HTMLSelectElement).value)"><option value="">留空</option><option v-for="value in row.field.valueOptions" :key="value" :value="value">{{ value }}</option></select>
                  <input v-else :type="row.field?.valueType==='number'?'number':'text'" :value="previewInputDisplayValue(row.inputKey)" @input="setPreviewInput(row.inputKey,($event.target as HTMLInputElement).value)"/>
                </label>
                <button type="button" class="button ghost small preview-example-reset" @click="resetPreviewInput(row)">恢复通用示例</button>
              </div>
              <p v-if="!previewSourceRows.length" class="platform-empty">当前规则只使用管理员默认值，无需示例输入。</p>
            </div>
          </details>
          <div v-if="previewBlockedMessage" class="preview-state paused" aria-live="polite" aria-atomic="true"><AlertTriangle :size="14"/><span>{{ previewBlockedMessage }}</span></div>
          <div v-else-if="previewBusy" class="preview-state" aria-live="polite" aria-atomic="true"><LoaderCircle class="spin" :size="14"/><span>{{ previewReadyMessage }}</span></div>
          <div v-else-if="previewReadyMessage" class="preview-state" :class="previewStale?'paused':'ready'" aria-live="polite" aria-atomic="true"><AlertTriangle v-if="previewStale" :size="14"/><Check v-else :size="14"/><span>{{ previewReadyMessage }}</span></div>
          <div v-if="previewError" class="platform-alert danger" role="alert"><AlertTriangle :size="15"/><p>{{ previewError }}</p><button class="button ghost small" :disabled="previewBusy||Boolean(previewBlockedMessage)" @click="schedulePreview">重新组合</button></div>
          <section v-if="previewResult" class="preview-result" :class="{stale:previewStale}">
            <div class="preview-channels single-column"><article v-for="(value,key) in previewResult.channels" :key="key"><strong>{{ choiceLabel(options.channels,String(key)) }} <small>{{ previewResult.channelAudit[String(key)]?.character_count ?? String(value).length }} 字符</small></strong><pre>{{ value || '（空）' }}</pre></article></div>
            <details><summary>诊断详情</summary><p>候选合同：<code>{{ previewResult.contractSha256 }}</code></p><p>实际执行：字段生成 {{ previewResult.executedStages.field_generation?'是':'否' }} · 来源投影 {{ previewResult.executedStages.source_projection?'是':'否' }} · 最终拼装 {{ previewResult.executedStages.final_assembly?'是':'否' }}</p></details>
          </section>
        </aside>
      </div>

      <div v-if="editorValidation.length" class="prompt-validation"><AlertTriangle :size="15"/><div><strong>保存前必须修复</strong><ul><li v-for="item in editorValidation" :key="item">{{ item }}</li></ul></div></div>
      <div v-if="editorError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ editorError }}</p></div>
      <template #footer><button class="button ghost" :disabled="saving" @click="editorOpen=false">取消</button><button class="button primary" :disabled="saving||Boolean(editorValidation.length)" @click="submitEditor"><Save :size="13"/>{{ saving?'服务端保存中…':'保存' }}</button></template>
    </BaseModal>

    <BaseModal :open="Boolean(configurationSnapshot)" :title="`查看配置 · ${configurationSnapshot?.profile.name || ''} · v${configurationSnapshot?.profile.version ?? ''}`" eyebrow="已保存版本 · 只读" width="1050px" @close="configurationSnapshot=null">
      <PromptProfileConfiguration v-if="configurationSnapshot" :key="configurationSnapshot.profile.id" v-bind="configurationSnapshot" />
      <template #footer><button class="button ghost" @click="configurationSnapshot=null">关闭</button></template>
    </BaseModal>

    <BaseModal :open="cloneOpen" :dismissible="!cloneBusy" title="创建后继草稿" eyebrow="精确版本复制 · 原子创建" width="680px" @close="cloneOpen=false">
      <div v-if="selectedProfile" class="prompt-clone-review">
        <p>从这一份不可变合同复制 Profile 配置与全部字段组合规则。服务端会再次核对来源 ID、版本和 SHA-256，并自动分配同一 Profile Key 的下一个版本；来源已变化时不会创建半成品。</p>
        <dl>
          <div><dt>来源</dt><dd>{{ selectedProfile.name }} · v{{ selectedProfile.version }}</dd></div>
          <div><dt>Profile Key</dt><dd><code>{{ selectedProfile.profileKey }}</code></dd></div>
          <div><dt>合同 SHA-256</dt><dd><code>{{ selectedProfile.contractSha256 }}</code></dd></div>
          <div><dt>复制规则</dt><dd>{{ selectedProfile.fieldRuleCount }} 条</dd></div>
        </dl>
        <label>新草稿名称 <i>必填</i><input v-model.trim="cloneName" maxlength="160" autocomplete="off"/></label>
        <div v-if="cloneError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ cloneError }}</p></div>
      </div>
      <template #footer><button class="button ghost" :disabled="cloneBusy" @click="cloneOpen=false">取消</button><button class="button primary" :disabled="cloneBusy||!cloneName.trim()" @click="cloneSuccessor"><Copy :size="13"/>{{ cloneBusy?'正在创建…':'创建并打开草稿' }}</button></template>
    </BaseModal>

    <BaseModal :open="previewOpen" :dismissible="!previewBusy" :title="`最终提示词预览 · ${selectedProfile?.name || ''}`" eyebrow="示例内容 · 服务端实时拼装" width="1050px" @close="previewOpen=false">
      <div class="prompt-preview">
        <p>系统已自动填入服务端通用示例并显示模型最终接收的各通道提示词；仅在需要时修改。修改只影响本次预览，不保存、不调用模型；声明式来源原语仍需用真实项目上下文另行验收。</p>
        <div class="preview-source-grid">
          <div v-for="row in previewSourceRows" :key="row.inputKey" class="preview-sample-item">
            <label>
              <span>{{ row.selector==='projection.output' ? `假设「${row.rule.fieldLabel || row.rule.fieldKey}」已经得到` : `假设「${sourceLabel(row.selector)}」为` }}</span>
              <select v-if="row.field?.valueType==='boolean'" :value="previewInputDisplayValue(row.inputKey)" @change="setPreviewInput(row.inputKey,($event.target as HTMLSelectElement).value===''?'':($event.target as HTMLSelectElement).value==='true')"><option value="">留空（用于测试缺失）</option><option value="false">否（false）</option><option value="true">是（true）</option></select>
              <textarea v-else-if="row.field?.valueType==='long_text'||row.field?.valueType==='string_list'" :value="previewInputDisplayValue(row.inputKey)" rows="3" @input="setPreviewInput(row.inputKey,($event.target as HTMLTextAreaElement).value)"/>
              <select v-else-if="row.field?.valueType==='enum'" :value="previewInputDisplayValue(row.inputKey)" @change="setPreviewInput(row.inputKey,($event.target as HTMLSelectElement).value)"><option value="">留空（用于测试缺失）</option><option v-for="value in row.field.valueOptions" :key="value" :value="value">{{ value }}</option></select>
              <input v-else :type="row.field?.valueType==='number'?'number':'text'" :value="previewInputDisplayValue(row.inputKey)" @input="setPreviewInput(row.inputKey,($event.target as HTMLInputElement).value)"/>
              <small>{{ row.field?.description }}<template v-if="row.rule.required"> · 该规则必填</template></small>
            </label>
            <button type="button" class="button ghost small preview-example-reset" @click="resetPreviewInput(row)">恢复通用示例</button>
          </div>
          <p v-if="!previewSourceRows.length" class="platform-empty">所有规则均使用管理员默认值，无需输入请求数据。</p>
        </div>
        <div v-if="previewBusy" class="preview-state" aria-live="polite" aria-atomic="true"><LoaderCircle class="spin" :size="14"/><span>{{ previewReadyMessage }}</span></div><div v-else-if="previewReadyMessage" class="preview-state" :class="previewStale?'paused':'ready'" aria-live="polite" aria-atomic="true"><AlertTriangle v-if="previewStale" :size="14"/><Check v-else :size="14"/><span>{{ previewReadyMessage }}</span></div>
        <div v-if="previewError" class="platform-alert danger" role="alert"><AlertTriangle :size="15"/><p>{{ previewError }}</p><button class="button ghost small" :disabled="previewBusy" @click="schedulePreview">重新组合</button></div>
        <section v-if="previewResult" class="preview-result" :class="{stale:previewStale}"><header><AlertTriangle v-if="previewStale" :size="15"/><Check v-else :size="15"/><div><strong>{{ previewStale?'上次有效组合文本':'实际组合文本' }}</strong><small>合同摘要 {{ previewResult.contractSha256 }}</small></div></header><div v-if="previewResult.warnings.length" class="platform-alert warning"><AlertTriangle :size="15"/><p>{{ previewResult.warnings.join('；') }}</p></div><div class="preview-channels"><article v-for="(value,key) in previewResult.channels" :key="key"><strong>{{ choiceLabel(options.channels,String(key)) }}</strong><pre>{{ value || '（空）' }}</pre></article></div><details><summary>诊断详情</summary><p>实际执行：字段生成 {{ previewResult.executedStages.field_generation?'是':'否' }} · 来源投影 {{ previewResult.executedStages.source_projection?'是':'否' }} · 最终拼装 {{ previewResult.executedStages.final_assembly?'是':'否' }}</p></details></section>
      </div>
      <template #footer><button class="button ghost" :disabled="previewBusy" @click="previewOpen=false">关闭</button></template>
    </BaseModal>

    <RegistryDeletionReviewModal
      v-if="actionTarget && actionDeletionResourceType"
      :open="true"
      :resource-type="actionDeletionResourceType"
      :object-id="actionTarget.id"
      :title="actionTarget.label"
      @close="actionTarget=null"
      @completed="onRegistryDeletionCompleted"
    />

    <BaseModal :open="Boolean(actionTarget && !actionDeletionResourceType)" :dismissible="!saving" :title="actionTarget?.label || '确认操作'" eyebrow="提交前复核（Review Before Commit）" @close="actionTarget=null">
      <div class="prompt-action-review"><p v-if="actionTarget?.kind==='publish-profile'">发布前服务端会复核审核状态、字段规则、通道要求、冻结合同与 operation 能力证据。新 operation 没有已发布模型 capability 或受控 registry/driver 证据时会拒绝；发布也不会自动创建可执行 route。</p><p v-else-if="actionTarget?.kind==='retire-profile'">仍有启用中的绑定（active binding）时退役会被服务端拒绝；历史生产引用保留。</p><p v-else>删除只适用于无引用的可变资源。已发布或已进入运行事实的对象会被服务端拒绝。</p><div v-if="actionError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ actionError }}</p></div></div>
      <template #footer><button class="button ghost" :disabled="saving" @click="actionTarget=null">取消</button><button class="button" :class="actionTarget?.kind.startsWith('delete')?'danger':'primary'" :disabled="saving" @click="runAction">{{ saving?'服务端处理中…':'确认提交' }}</button></template>
    </BaseModal>
  </div>
</template>

<style scoped>
.rule-content-source{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:5px 0;color:var(--text);font-size:12px;line-height:1.6;overflow-wrap:anywhere}.rule-content-source .button{min-height:36px}.profile-rule-list article>div{min-width:0}
.preview-sample-item{position:relative;min-width:0}.preview-sample-item>label>span{padding-right:96px}.preview-example-reset{position:absolute;top:7px;right:7px;z-index:1}
.prompt-profile-settings{display:grid;gap:14px}.prompt-catalog-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.prompt-catalog-card,.prompt-profile-workbench{min-width:0;border:1px solid #252b34;border-radius:10px;background:#0b0e12;overflow:hidden}.prompt-catalog-card>header{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:11px;border-bottom:1px solid #252b34}.prompt-catalog-card>header>div{display:flex;align-items:center;gap:8px}.prompt-catalog-card>header span{display:grid;gap:2px}.prompt-catalog-card>header strong{color:#c8d0da;font-size:11px}.prompt-catalog-card>header small{color:#6f7985;font-size:8px}.prompt-scroll-list{max-height:330px;overflow:auto}.prompt-scroll-list article{display:grid;grid-template-columns:minmax(0,1fr) auto auto;align-items:center;gap:7px;padding:9px 10px;border-bottom:1px solid rgba(255,255,255,.045)}.prompt-scroll-list article>div,.profile-select-row>span{min-width:0;display:grid;gap:3px}.prompt-scroll-list article strong,.profile-select-row strong{overflow:hidden;color:#b4bdc8;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.prompt-scroll-list article small,.profile-select-row small{overflow:hidden;color:#68737f;font-size:8px;text-overflow:ellipsis;white-space:nowrap}.prompt-scroll-list article>span:last-child{display:flex;gap:4px}.prompt-prerequisite{display:flex;gap:7px;align-items:flex-start;margin:8px;padding:8px;border:1px solid rgba(210,145,63,.3);border-radius:6px;background:rgba(210,145,63,.07);color:#cfa86f;font-size:9px;line-height:1.5}.profile-select-row{width:100%;padding:10px;display:flex;align-items:center;justify-content:space-between;gap:10px;border:0;border-bottom:1px solid rgba(255,255,255,.045);background:transparent;text-align:left;cursor:pointer}.profile-select-row:hover,.profile-select-row.active{background:rgba(210,145,63,.08)}.profile-select-row.active{box-shadow:inset 2px 0 #d08f3d}.profile-select-row>span:last-child{justify-items:end}.prompt-profile-workbench>header{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:16px;border-bottom:1px solid #252b34}.prompt-profile-workbench h3{margin:4px 0;color:#e5e9ee;font:20px Georgia,serif}.prompt-profile-workbench p{margin:0;color:#85909c;font-size:10px}.profile-actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:5px}.profile-facts{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:1px;background:#242b34}.profile-facts>div{min-width:0;padding:10px;display:grid;gap:4px;background:#0c1015}.profile-facts span{color:#697481;font-size:8px}.profile-facts strong,.profile-facts a{overflow-wrap:anywhere;color:#b9c2cc;font-size:9px}.profile-facts a{display:flex;align-items:center;gap:5px;color:#d59a50}.rule-builder-header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px}.rule-builder-header>div{display:grid;gap:3px}.rule-builder-header strong{color:#c3cbd5;font-size:11px}.rule-builder-header small{color:#6f7985;font-size:8px}.profile-rule-list{display:grid;gap:6px;padding:0 12px 12px}.profile-rule-list article{display:grid;grid-template-columns:30px minmax(0,1fr) auto;align-items:center;gap:9px;padding:9px;border:1px solid #252c35;border-radius:7px;background:#090c10}.profile-rule-list article>b{display:grid;place-items:center;width:26px;height:26px;border:1px solid #3a342b;border-radius:50%;color:#d69a51;font-size:9px}.profile-rule-list article>div{display:grid;gap:3px}.profile-rule-list article strong{color:#bac3ce;font-size:10px}.profile-rule-list article small{color:#6f7985;font-size:8px}.profile-rule-list article>span{display:flex;gap:4px}.prompt-editor-form{display:grid;gap:12px}.prompt-editor-form>p,.prompt-preview>p{margin:0;color:#94a0ad;line-height:1.6}.prompt-form-grid,.preview-source-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.prompt-form-grid label,.preview-source-grid label{display:grid;align-content:start;gap:6px;padding:10px;border:1px solid #272e37;border-radius:7px;background:#090c10;color:#9ca7b4;font-size:10px}.prompt-form-grid label.wide,.prompt-form-grid>.wide{grid-column:1/-1}.prompt-form-grid label.check{display:flex;align-items:center}.prompt-form-grid label.check input{width:auto}.prompt-form-grid i{float:right;color:#d69a51;font-size:8px;font-style:normal}.prompt-form-grid input,.prompt-form-grid select,.prompt-form-grid textarea,.preview-source-grid input,.preview-source-grid select,.preview-source-grid textarea{min-width:0;width:100%;padding:9px;border:1px solid #303743;border-radius:6px;background:#070a0e;color:#e0e5eb}.prompt-form-grid small,.preview-source-grid small{color:#697481;font-size:8px;line-height:1.5}.generation-contract-heading{display:grid;gap:5px;padding:10px;border:1px solid rgba(83,145,197,.35);border-radius:7px;background:rgba(83,145,197,.08)}.generation-contract-heading strong{color:#aad2ee;font-size:10px}.generation-validator-fieldset,.channel-limit-fieldset{display:grid;gap:8px;margin:0;padding:10px;border:1px solid #272e37;border-radius:7px;background:#090c10}.generation-validator-fieldset legend,.channel-limit-fieldset legend{padding:0 5px;color:#9ca7b4;font-size:10px}.generation-validator-fieldset>div{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.generation-validator-fieldset label.check{padding:7px}.channel-limit-row{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr) minmax(0,1fr);gap:6px}.channel-limit-row label{padding:7px}.prompt-validation{display:flex;gap:8px;margin-top:12px;padding:10px;border:1px solid rgba(206,76,76,.35);border-radius:7px;background:rgba(206,76,76,.08);color:#ef9999}.prompt-validation ul{margin:5px 0 0;padding-left:18px}.prompt-preview{display:grid;gap:13px}.preview-source-grid label>span{display:flex;align-items:center;justify-content:space-between;gap:8px}.preview-source-grid code{color:#6e7986;font-size:8px}.preview-result{display:grid;gap:10px;border-top:1px solid #282f39;padding-top:12px}.preview-result>header{display:flex;align-items:center;gap:8px;color:#73d2a0}.preview-result>header div{display:grid;gap:2px}.preview-result>header small{color:#6e7986}.preview-channels{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.preview-channels article,.preview-result details{border:1px solid #282f39;border-radius:7px;background:#090c10;overflow:hidden}.preview-channels strong,.preview-result summary{display:block;padding:8px 10px;color:#b8c1cc;font-size:9px}.preview-channels pre{min-height:70px;margin:0;padding:10px;border-top:1px solid #282f39;color:#d2dae3;font:10px/1.6 ui-monospace,Consolas,monospace;white-space:pre-wrap}.preview-result details summary{cursor:pointer}.preview-result dl{display:grid;gap:1px;margin:0;background:#252c35}.preview-result dl div{display:grid;grid-template-columns:180px 1fr;gap:10px;padding:8px;background:#0a0d11}.preview-result dt{color:#798591}.preview-result dd{margin:0;color:#c5ced8;overflow-wrap:anywhere}.preview-result table{width:100%;border-collapse:collapse}.preview-result th,.preview-result td{padding:7px;border-top:1px solid #252c35;color:#8d98a5;text-align:left;font-size:8px}.prompt-action-review{display:grid;gap:12px}.prompt-action-review>p{margin:0;color:#a5afbb;line-height:1.65}
.generation-source-explanation{padding:10px;border:1px solid rgba(83,145,197,.32);border-radius:7px;background:rgba(83,145,197,.06)}.generation-source-explanation header{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}.generation-source-explanation strong{color:#afd4ee;font-size:10px}.generation-source-explanation code{color:#7894a8;font-size:8px}.generation-source-explanation dl{display:grid;gap:1px;margin:0;background:#27313b}.generation-source-explanation dl>div{display:grid;grid-template-columns:78px minmax(0,1fr);gap:9px;padding:7px;background:#090d12}.generation-source-explanation dt{color:#7e9bb0;font-size:8px}.generation-source-explanation dd{margin:0;color:#9daab7;font-size:8px;line-height:1.55}.generation-source-warning{display:flex;align-items:flex-start;gap:7px;padding:9px;border:1px solid rgba(210,145,63,.35);border-radius:7px;background:rgba(210,145,63,.08);color:#d4ad75;font-size:8px;line-height:1.55}
.generation-context-fieldset{display:grid;gap:8px;margin:0;padding:10px;border:1px solid #272e37;border-radius:7px;background:#090c10}.generation-context-fieldset legend{padding:0 5px;color:#9ca7b4;font-size:10px}.generation-context-fieldset>div{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.generation-context-fieldset label.check{padding:7px}.generation-context-fieldset label.check span{display:grid;gap:3px}.generation-context-fieldset label.check strong{color:#b7c1cc;font-size:9px}.generation-context-fieldset label.check small{font-size:8px}.generation-context-fieldset>small{color:#788491;font-size:8px;line-height:1.55}
.projection-config-fieldset{display:grid;gap:10px;margin:0;padding:11px;border:1px solid rgba(83,145,197,.35);border-radius:7px;background:rgba(83,145,197,.06)}.projection-config-fieldset>legend{padding:0 5px;color:#aad2ee;font-size:10px}.projection-config-fieldset>small{color:#7f95a8;font-size:8px;line-height:1.55}.projection-config-fieldset>summary,.projection-quality-details>summary,.rule-advanced>summary{cursor:pointer;color:#aeb9c5;font-size:10px}.projection-config-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.projection-config-grid>label{display:grid;align-content:start;gap:6px;padding:9px;border:1px solid #28313b;border-radius:6px;background:#090c10}.projection-config-grid>label.wide,.projection-config-grid>.wide{grid-column:1/-1}.projection-config-grid label.check{display:flex;align-items:center}.projection-config-grid label.check input{width:auto}.projection-quality-details,.rule-advanced{padding:9px;border:1px solid #28313b;border-radius:6px;background:#080b0f}.projection-quality-details[open],.rule-advanced[open]{display:grid;gap:9px}.prompt-clone-review{display:grid;gap:12px}.prompt-clone-review>p{margin:0;color:#9ca7b4;line-height:1.65}.prompt-clone-review dl{display:grid;gap:1px;margin:0;background:#252c35}.prompt-clone-review dl div{display:grid;grid-template-columns:120px minmax(0,1fr);gap:10px;padding:8px;background:#090c10}.prompt-clone-review dt{color:#74808c}.prompt-clone-review dd{margin:0;color:#c2cbd5;overflow-wrap:anywhere}.prompt-clone-review label{display:grid;gap:6px;color:#9ca7b4}.prompt-clone-review input{width:100%;padding:9px;border:1px solid #303743;border-radius:6px;background:#070a0e;color:#e0e5eb}.prompt-clone-review i{color:#d69a51;font-size:8px;font-style:normal}
.assembly-contract-heading{display:grid;gap:5px;padding:10px;border:1px solid rgba(208,143,61,.38);border-radius:7px;background:rgba(208,143,61,.08)}.assembly-contract-heading strong{color:#efc489;font-size:10px}.assembly-contract-heading small{color:#917e65;font-size:8px;line-height:1.55}.prompt-editor-live-layout{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(360px,.75fr);gap:14px;align-items:start}.prompt-editor-config-pane{min-width:0;max-height:68vh;padding-right:5px;overflow:auto}.prompt-live-preview{position:sticky;top:0;display:grid;gap:10px;max-height:68vh;padding:12px;border:1px solid #303947;border-radius:9px;background:#080b0f;overflow:auto}.prompt-live-preview>header{display:flex;align-items:center;gap:8px;color:#d79a50}.prompt-live-preview>header div{display:grid;gap:2px}.prompt-live-preview>header strong{font-size:11px}.prompt-live-preview>header small{color:#77828e;font-size:8px}.prompt-live-preview>p{margin:0;color:#929eaa;font-size:9px;line-height:1.6}.preview-scope-note,.preview-state{display:flex;align-items:flex-start;gap:7px;padding:8px;border:1px solid rgba(83,145,197,.3);border-radius:6px;background:rgba(83,145,197,.07);color:#91b8d5;font-size:8px;line-height:1.55}.preview-state{align-items:center;color:#aeb9c5}.preview-state.ready{border-color:rgba(74,170,116,.3);background:rgba(74,170,116,.07);color:#87d5a8}.preview-state.paused{border-color:rgba(210,145,63,.3);background:rgba(210,145,63,.07);color:#d4ad75}.preview-samples{border:1px solid #282f39;border-radius:7px;background:#090c10}.preview-samples>summary{padding:8px 10px;cursor:pointer;color:#b8c1cc;font-size:9px}.preview-samples>.preview-source-grid{padding:8px;border-top:1px solid #282f39}.single-column{grid-template-columns:1fr!important}.preview-channels strong{display:flex;align-items:center;justify-content:space-between;gap:8px}.preview-channels strong small{color:#6f7985;font-weight:400}.preview-result.stale{border-color:#705a3a;opacity:.66}.preview-result details>p{margin:0;padding:8px 10px;border-top:1px solid #252c35;color:#8d98a5;font-size:8px;overflow-wrap:anywhere}
.prompt-form-grid output{min-width:0;padding:9px;border:1px solid #252c35;border-radius:6px;background:#0d1117;color:#aeb8c4;overflow-wrap:anywhere}
@media(max-width:1180px){.prompt-catalog-grid{grid-template-columns:1fr 1fr}.prompt-catalog-card:last-child{grid-column:1/-1}.profile-facts{grid-template-columns:repeat(3,1fr)}}
@media(max-width:900px){.prompt-editor-live-layout{grid-template-columns:1fr}.prompt-editor-config-pane,.prompt-live-preview{max-height:none;overflow:visible}.prompt-live-preview{position:static}}
@media(max-width:760px){.prompt-catalog-grid,.prompt-form-grid,.preview-source-grid,.preview-channels,.generation-validator-fieldset>div,.generation-context-fieldset>div,.channel-limit-row,.projection-config-grid{grid-template-columns:1fr}.prompt-catalog-card:last-child,.prompt-form-grid label.wide,.prompt-form-grid>.wide,.projection-config-grid>label.wide,.projection-config-grid>.wide{grid-column:auto}.prompt-profile-workbench>header{flex-direction:column}.profile-actions{justify-content:flex-start}.profile-facts{grid-template-columns:1fr}.preview-result dl div,.prompt-clone-review dl div{grid-template-columns:1fr}}
.rendering-intro,.rendering-guide{font-size:12px;line-height:1.7;color:var(--text-secondary)}.rendering-intro{margin:0}.rendering-guide summary{cursor:pointer;min-height:36px;line-height:36px;color:var(--text)}.rendering-guide ol{padding-left:22px}.rendering-guide li{margin:8px 0}.rendering-guide p{overflow-wrap:anywhere}
</style>
