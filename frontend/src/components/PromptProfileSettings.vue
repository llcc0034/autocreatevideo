<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { AlertTriangle, Archive, BookOpen, Check, Eye, FileText, Link2, LoaderCircle, Plus, RefreshCw, Save, Settings2, ShieldCheck, Trash2 } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { platformApi } from '@/services/platformApi'
import type {
  JsonObject,
  PromptFieldDefinition,
  PromptFieldValueType,
  PromptProfileBinding,
  PromptProfileFieldRule,
  PromptProfileOptions,
  PromptProfilePreview,
  PromptProfileVersion,
} from '@/services/platformTypes'

type EditorKind = 'field' | 'profile' | 'rule' | 'binding'
type ActionKind = 'delete-field' | 'delete-profile' | 'delete-rule' | 'delete-binding' | 'publish-profile' | 'retire-profile'
type ActionTarget = { kind: ActionKind; id: string; label: string }

const loading = ref(false)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const fields = ref<PromptFieldDefinition[]>([])
const profiles = ref<PromptProfileVersion[]>([])
const rules = ref<PromptProfileFieldRule[]>([])
const bindings = ref<PromptProfileBinding[]>([])
const options = ref<PromptProfileOptions>({
  operations: [], modelFamilies: [], modes: [], sourceSelectors: [], channels: [], labelStyles: [], separators: [], modelProfileVersions: [],
})
const selectedProfileId = ref('')
const fieldSearch = ref('')
const profileSearch = ref('')

const editorOpen = ref(false)
const editorKind = ref<EditorKind>('field')
const editorError = ref('')
const actionTarget = ref<ActionTarget | null>(null)
const actionError = ref('')
const previewOpen = ref(false)
const previewBusy = ref(false)
const previewError = ref('')
const previewResult = ref<PromptProfilePreview | null>(null)
const previewDraft = ref<Record<string, unknown>>({})

const fieldForm = reactive({
  id: '', key: '', label: '', category: '', description: '', impact: '', valueType: 'text' as PromptFieldValueType,
  hasDefault: false, defaultText: '', defaultBoolean: false, valueOptionsText: '', isRequired: false, isActive: true, displayOrder: 0,
})
const profileForm = reactive({
  id: '', profileKey: '', version: 1, name: '', description: '', modelFamily: '', operation: '', mode: '',
  officialReference: '', reviewState: 'needs_review',
})
const ruleForm = reactive({
  id: '', profileVersion: '', fieldDefinition: '', sourceSelector: '', channel: 'positive_prompt', position: 1,
  labelStyle: 'label_colon', separator: 'newline', required: false, omitIfEmpty: true, maxChars: 2000,
})
const bindingForm = reactive({
  id: '', key: '', name: '', promptProfileVersion: '', operation: '', modelFamily: '', mode: '',
  modelProfileVersion: '', priority: 0, isActive: true,
})

const selectedProfile = computed(() => profiles.value.find((item) => item.id === selectedProfileId.value) || null)
const selectedRules = computed(() => rules.value.filter((item) => item.profileVersion === selectedProfileId.value).sort((a, b) => a.position - b.position))
const activeFields = computed(() => fields.value.filter((item) => item.isActive))
const filteredFields = computed(() => {
  const query = fieldSearch.value.trim().toLocaleLowerCase()
  return query ? fields.value.filter((item) => `${item.key} ${item.label} ${item.category}`.toLocaleLowerCase().includes(query)) : fields.value
})
const filteredProfiles = computed(() => {
  const query = profileSearch.value.trim().toLocaleLowerCase()
  return query ? profiles.value.filter((item) => `${item.profileKey} ${item.name} ${item.modelFamily} ${item.operation}`.toLocaleLowerCase().includes(query)) : profiles.value
})
const publishedProfiles = computed(() => profiles.value.filter((item) => item.status === 'published'))
const profileBindings = computed(() => bindings.value.filter((item) => item.promptProfileVersion === selectedProfileId.value))
const sourceLabel = (value: string) => options.value.sourceSelectors.find((item) => item.value === value)?.label || value
const choiceLabel = (rows: Array<{ value: string; label: string }>, value: string) => rows.find((item) => item.value === value)?.label || value
const fieldForRule = (rule: PromptProfileFieldRule) => fields.value.find((item) => item.id === rule.fieldDefinition) || null
const humanBytes = (value: number) => value >= 1024 ? `${(value / 1024).toFixed(1)} KB` : `${value} B`
const message = (cause: unknown) => apiErrorSummary(cause, '请求失败，服务端没有返回可读错误')

const refresh = async () => {
  loading.value = true; error.value = ''
  try {
    const [nextFields, nextProfiles, nextRules, nextBindings, nextOptions] = await Promise.all([
      platformApi.listPromptFieldDefinitions(), platformApi.listPromptProfileVersions(),
      platformApi.listPromptProfileFieldRules(), platformApi.listPromptProfileBindings(), platformApi.getPromptProfileOptions(),
    ])
    fields.value = nextFields; profiles.value = nextProfiles; rules.value = nextRules; bindings.value = nextBindings; options.value = nextOptions
    if (!profiles.value.some((item) => item.id === selectedProfileId.value)) selectedProfileId.value = profiles.value[0]?.id || ''
  } catch (cause) { error.value = message(cause) }
  finally { loading.value = false }
}

const splitValues = (value: string) => value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean)
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
  editorKind.value = 'field'; editorError.value = ''
  Object.assign(fieldForm, {
    id: existing?.id || '', key: existing?.key || '', label: existing?.label || '', category: existing?.category || '',
    description: existing?.description || '', impact: existing?.impact || '', valueType: existing?.valueType || 'text',
    hasDefault: existing ? existing.defaultValue !== null && existing.defaultValue !== undefined : false,
    defaultText: existing ? defaultText(existing) : '', defaultBoolean: existing?.defaultValue === true,
    valueOptionsText: existing?.valueOptions.join('\n') || '', isRequired: existing?.isRequired || false,
    isActive: existing?.isActive ?? true, displayOrder: existing?.displayOrder || 0,
  })
  editorOpen.value = true
}
const openProfile = (existing?: PromptProfileVersion) => {
  editorKind.value = 'profile'; editorError.value = ''
  Object.assign(profileForm, {
    id: existing?.id || '', profileKey: existing?.profileKey || '', version: existing?.version || 1,
    name: existing?.name || '', description: existing?.description || '', modelFamily: existing?.modelFamily || options.value.modelFamilies[0] || '',
    operation: existing?.operation || options.value.operations[0] || '', mode: existing?.mode || '',
    officialReference: existing?.officialReference || '', reviewState: existing?.reviewState || 'needs_review',
  })
  editorOpen.value = true
}
const openRule = (existing?: PromptProfileFieldRule) => {
  const profile = selectedProfile.value
  if (!profile || profile.status !== 'draft') return
  editorKind.value = 'rule'; editorError.value = ''
  const nextPosition = Math.max(0, ...selectedRules.value.map((item) => item.position)) + 1
  Object.assign(ruleForm, {
    id: existing?.id || '', profileVersion: profile.id, fieldDefinition: existing?.fieldDefinition || '',
    sourceSelector: existing?.sourceSelector || options.value.sourceSelectors[0]?.value || '', channel: existing?.channel || 'positive_prompt',
    position: existing?.position || nextPosition, labelStyle: existing?.labelStyle || 'label_colon',
    separator: existing?.separator || 'newline', required: existing?.required || false,
    omitIfEmpty: existing?.omitIfEmpty ?? true, maxChars: existing?.maxChars || 2000,
  })
  editorOpen.value = true
}
const syncBindingProfile = () => {
  const profile = profiles.value.find((item) => item.id === bindingForm.promptProfileVersion)
  if (!profile) return
  bindingForm.operation = profile.operation; bindingForm.modelFamily = profile.modelFamily; bindingForm.mode = profile.mode
}
const openBinding = (existing?: PromptProfileBinding) => {
  editorKind.value = 'binding'; editorError.value = ''
  const profileId = existing?.promptProfileVersion || (selectedProfile.value?.status === 'published' ? selectedProfile.value.id : publishedProfiles.value[0]?.id || '')
  Object.assign(bindingForm, {
    id: existing?.id || '', key: existing?.key || '', name: existing?.name || '', promptProfileVersion: profileId,
    operation: existing?.operation || '', modelFamily: existing?.modelFamily || '', mode: existing?.mode || '',
    modelProfileVersion: existing?.modelProfileVersion || '', priority: existing?.priority || 0, isActive: existing?.isActive ?? true,
  })
  if (!existing) syncBindingProfile()
  editorOpen.value = true
}

watch(() => ruleForm.channel, (channel) => {
  if (channel === 'system_instruction') ruleForm.sourceSelector = 'definition.default'
})
watch(actionTarget, () => { actionError.value = '' })

const selectedRuleField = computed(() => fields.value.find((item) => item.id === ruleForm.fieldDefinition) || null)
const editorValidation = computed(() => {
  const errors: string[] = []
  const keyPattern = /^[a-z][a-z0-9_.-]{1,119}$/
  if (editorKind.value === 'field') {
    if (!keyPattern.test(fieldForm.key)) errors.push('字段 Key 必须为小写稳定标识')
    if (!fieldForm.label.trim()) errors.push('显示名称必填')
    if (!keyPattern.test(fieldForm.category)) errors.push('分类必须为小写稳定标识')
    if (!fieldForm.description.trim()) errors.push('必须说明字段含义')
    if (!fieldForm.impact.trim()) errors.push('必须说明字段影响')
    const values = splitValues(fieldForm.valueOptionsText)
    if (fieldForm.valueType === 'enum' && !values.length) errors.push('枚举字段必须配置可选值')
    if (fieldForm.valueType !== 'enum' && values.length) errors.push('只有枚举字段可配置可选值')
    if (fieldForm.hasDefault && fieldForm.valueType === 'enum' && !values.includes(fieldForm.defaultText)) errors.push('枚举默认值必须来自可选值')
    if (fieldForm.hasDefault && fieldForm.valueType === 'number' && !Number.isFinite(Number(fieldForm.defaultText))) errors.push('数值默认值无效')
  } else if (editorKind.value === 'profile') {
    if (!keyPattern.test(profileForm.profileKey)) errors.push('Profile Key 格式不正确')
    if (!profileForm.name.trim() || !profileForm.modelFamily || !profileForm.operation) errors.push('名称、模型族和 operation 必填')
    if (!Number.isInteger(profileForm.version) || profileForm.version < 1) errors.push('版本号必须为正整数')
    if (profileForm.officialReference && !/^https?:\/\//i.test(profileForm.officialReference)) errors.push('官方参考必须是 HTTP(S) URL')
  } else if (editorKind.value === 'rule') {
    if (!selectedProfile.value || selectedProfile.value.status !== 'draft') errors.push('只能编辑草稿 Profile 规则')
    if (!selectedRuleField.value?.isActive) errors.push('必须选择已启用字段')
    if (!ruleForm.sourceSelector || !ruleForm.channel) errors.push('数据来源和输出通道必填')
    if (ruleForm.channel === 'system_instruction' && ruleForm.sourceSelector !== 'definition.default') errors.push('系统约束只能使用管理员字段默认值')
    if (ruleForm.sourceSelector === 'definition.default' && selectedRuleField.value?.defaultValue == null) errors.push('引用字段默认值时，字段必须已配置默认值')
    if (!Number.isInteger(ruleForm.position) || ruleForm.position < 1) errors.push('装配顺序必须从 1 开始')
    if (!Number.isInteger(ruleForm.maxChars) || ruleForm.maxChars < 1 || ruleForm.maxChars > 30000) errors.push('最大字符数必须为 1–30000')
  } else {
    const profile = profiles.value.find((item) => item.id === bindingForm.promptProfileVersion)
    if (!keyPattern.test(bindingForm.key) || !bindingForm.name.trim()) errors.push('Binding Key 和名称必填')
    if (!profile || profile.status !== 'published') errors.push('必须选择已发布 PromptProfile')
    if (profile && (bindingForm.operation !== profile.operation || bindingForm.modelFamily !== profile.modelFamily || bindingForm.mode !== profile.mode)) errors.push('Binding selector 必须与 PromptProfile 一致')
    if (!Number.isInteger(bindingForm.priority) || bindingForm.priority < -1000 || bindingForm.priority > 1000) errors.push('优先级必须为 -1000–1000 的整数')
  }
  return errors
})

const submitEditor = async () => {
  if (editorValidation.value.length) return
  saving.value = true; editorError.value = ''
  try {
    const reason = editorKind.value === 'field' ? 'prompt_field_config' : editorKind.value === 'profile' ? 'prompt_profile_config' : editorKind.value === 'rule' ? 'prompt_rule_config' : 'prompt_binding_config'
    if (editorKind.value === 'field') {
      const payload: JsonObject = {
        key: fieldForm.key, label: fieldForm.label, category: fieldForm.category, description: fieldForm.description,
        impact: fieldForm.impact, value_type: fieldForm.valueType, default_value: parseFieldDefault(),
        value_options: fieldForm.valueType === 'enum' ? splitValues(fieldForm.valueOptionsText) : [],
        is_required: fieldForm.isRequired, is_active: fieldForm.isActive, display_order: fieldForm.displayOrder,
      }
      fieldForm.id ? await platformApi.updatePromptFieldDefinition(fieldForm.id, payload, reason) : await platformApi.createPromptFieldDefinition(payload, reason)
    } else if (editorKind.value === 'profile') {
      const payload: JsonObject = {
        profile_key: profileForm.profileKey, version: profileForm.version, name: profileForm.name, description: profileForm.description,
        model_family: profileForm.modelFamily, operation: profileForm.operation, mode: profileForm.mode,
        official_reference: profileForm.officialReference, review_state: profileForm.reviewState,
      }
      const saved = profileForm.id ? await platformApi.updatePromptProfileVersion(profileForm.id, payload, reason) : await platformApi.createPromptProfileVersion(payload, reason)
      selectedProfileId.value = saved.id
    } else if (editorKind.value === 'rule') {
      const payload: JsonObject = {
        profile_version: ruleForm.profileVersion, field_definition: ruleForm.fieldDefinition, source_selector: ruleForm.sourceSelector,
        channel: ruleForm.channel, position: ruleForm.position, label_style: ruleForm.labelStyle, separator: ruleForm.separator,
        required: ruleForm.required, omit_if_empty: ruleForm.omitIfEmpty, max_chars: ruleForm.maxChars,
      }
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

const runAction = async () => {
  const target = actionTarget.value
  if (!target) return
  saving.value = true; actionError.value = ''
  try {
    if (target.kind === 'delete-field') await platformApi.deletePromptFieldDefinition(target.id, 'prompt_field_delete')
    else if (target.kind === 'delete-profile') await platformApi.deletePromptProfileVersion(target.id, 'prompt_profile_delete')
    else if (target.kind === 'delete-rule') await platformApi.deletePromptProfileFieldRule(target.id, 'prompt_rule_delete')
    else if (target.kind === 'delete-binding') await platformApi.deletePromptProfileBinding(target.id, 'prompt_binding_delete')
    else if (target.kind === 'publish-profile') await platformApi.publishPromptProfileVersion(target.id, 'prompt_profile_publish')
    else await platformApi.retirePromptProfileVersion(target.id, 'prompt_profile_retire')
    actionTarget.value = null; await refresh(); notice.value = '操作已由服务端校验并完成。'
  } catch (cause) { actionError.value = message(cause) }
  finally { saving.value = false }
}

const previewSourceRows = computed(() => {
  const seen = new Set<string>()
  return selectedRules.value.flatMap((rule) => {
    if (rule.sourceSelector === 'definition.default' || seen.has(rule.sourceSelector)) return []
    seen.add(rule.sourceSelector)
    return [{ selector: rule.sourceSelector, rule, field: fieldForRule(rule) }]
  })
})
const openPreview = () => {
  previewDraft.value = Object.fromEntries(previewSourceRows.value.map((row) => [row.selector, '']))
  previewResult.value = null; previewError.value = ''; previewOpen.value = true
}
const previewInputValue = (selector: string) => previewDraft.value[selector]
const setPreviewInput = (selector: string, value: unknown) => { previewDraft.value = { ...previewDraft.value, [selector]: value } }
const runPreview = async () => {
  const profile = selectedProfile.value
  if (!profile) return
  previewBusy.value = true; previewError.value = ''; previewResult.value = null
  try {
    const sources: JsonObject = {}
    for (const row of previewSourceRows.value) {
      const raw = previewDraft.value[row.selector]
      if (raw === '' || raw === null || raw === undefined) continue
      if (row.field?.valueType === 'number') {
        const value = Number(raw); if (!Number.isFinite(value)) throw new Error(`${sourceLabel(row.selector)} 必须是数字`); sources[row.selector] = value
      } else if (row.field?.valueType === 'string_list') sources[row.selector] = splitValues(String(raw))
      else sources[row.selector] = raw
    }
    previewResult.value = await platformApi.previewPromptProfileVersion(profile.id, sources)
  } catch (cause) { previewError.value = message(cause) }
  finally { previewBusy.value = false }
}

onMounted(refresh)
</script>

<template>
  <div class="prompt-profile-settings">
    <section class="platform-toolbar"><div><strong>PromptProfile 组装策略</strong><span>字段字典 → 草稿 Profile → 字段规则 → 预览/审核 → 发布 → 模型绑定。生产运行只解析已发布的不可变版本。</span></div><div><button class="button ghost small" :disabled="loading" @click="refresh"><RefreshCw :size="13" :class="{spin:loading}"/>重新读取</button><button class="button ghost small" @click="openField()"><Plus :size="13"/>字段</button><button class="button primary small" @click="openProfile()"><Plus :size="13"/>Profile</button></div></section>
    <div v-if="error" class="platform-alert danger"><AlertTriangle :size="16"/><div><strong>PromptProfile 配置未完整读取</strong><p>{{ error }}</p></div></div>
    <div v-if="notice" class="platform-alert success"><Check :size="16"/><p>{{ notice }}</p></div>
    <div v-if="loading" class="platform-loading"><LoaderCircle class="spin" :size="20"/>正在读取字段、版本、规则与绑定…</div>

    <template v-else>
      <div class="prompt-catalog-grid">
        <section class="prompt-catalog-card">
          <header><div><FileText :size="16"/><span><strong>通用字段字典</strong><small>{{ fields.length }} 个服务端字段</small></span></div><input v-model="fieldSearch" placeholder="搜索字段 / 分类"/></header>
          <div class="prompt-scroll-list"><article v-for="item in filteredFields" :key="item.id"><div><strong>{{ item.label }}</strong><small>{{ item.key }} · {{ item.category }} · {{ item.valueType }}{{ item.isRequired?' · 必填':'' }}</small><small>{{ item.description || '未填写含义' }}</small><small>影响：{{ item.impact || '未填写影响' }} · 默认 {{ item.defaultValue===null||item.defaultValue===undefined?'无':JSON.stringify(item.defaultValue) }}</small></div><PlatformStatusPill :status="item.isActive?'active':'inactive'"/><span><button class="button ghost tiny" @click="openField(item)">编辑</button><button class="button danger tiny" @click="actionTarget={kind:'delete-field',id:item.id,label:`字段 ${item.label}`}"><Trash2 :size="11"/></button></span></article><p v-if="!filteredFields.length" class="platform-empty">没有匹配字段。</p></div>
        </section>

        <section class="prompt-catalog-card profile-list">
          <header><div><Settings2 :size="16"/><span><strong>Profile 版本</strong><small>{{ profiles.length }} 个不可变策略版本</small></span></div><input v-model="profileSearch" placeholder="搜索模型族 / operation"/></header>
          <div class="prompt-scroll-list"><button v-for="item in filteredProfiles" :key="item.id" class="profile-select-row" :class="{active:selectedProfileId===item.id}" @click="selectedProfileId=item.id"><span><strong>{{ item.name }} · v{{ item.version }}</strong><small>{{ item.modelFamily }} · {{ item.operation }}<template v-if="item.mode"> · {{ item.mode }}</template></small></span><span><PlatformStatusPill :status="item.status"/><small>{{ item.fieldRuleCount }} rules</small></span></button><p v-if="!filteredProfiles.length" class="platform-empty">尚无 Profile。</p></div>
        </section>

        <section class="prompt-catalog-card">
          <header><div><Link2 :size="16"/><span><strong>运行时绑定</strong><small>{{ bindings.length }} 条选择规则</small></span></div><button class="button ghost tiny" :disabled="!publishedProfiles.length" @click="openBinding()"><Plus :size="11"/>绑定</button></header>
          <div class="prompt-scroll-list"><div v-if="!publishedProfiles.length" class="prompt-prerequisite"><AlertTriangle :size="14"/><span>前置依赖缺失：请先完成规则审核并发布至少一个 PromptProfile，再新增绑定。</span></div><article v-for="item in bindings" :key="item.id"><div><strong>{{ item.name }}</strong><small>{{ item.operation }} / {{ item.modelFamily }}<template v-if="item.mode"> / {{ item.mode }}</template></small><small>{{ item.modelIdentifier || '模型族通用' }} · {{ item.promptProfileKey }} · priority {{ item.priority }}</small></div><PlatformStatusPill :status="item.isActive?'active':'inactive'"/><span><button class="button ghost tiny" @click="openBinding(item)">编辑</button><button class="button danger tiny" @click="actionTarget={kind:'delete-binding',id:item.id,label:`绑定 ${item.name}`}"><Trash2 :size="11"/></button></span></article><p v-if="!bindings.length" class="platform-empty">尚无运行时绑定；生产将无法解析策略。</p></div>
        </section>
      </div>

      <section v-if="selectedProfile" class="prompt-profile-workbench">
        <header><div><span class="eyebrow">PROMPT PROFILE VERSION</span><h3>{{ selectedProfile.name }} · v{{ selectedProfile.version }}</h3><p>{{ selectedProfile.description || '未填写策略说明。' }}</p></div><div class="profile-actions"><button class="button ghost small" @click="openPreview"><Eye :size="12"/>类型化预览</button><button v-if="selectedProfile.status==='draft'" class="button ghost small" @click="openProfile(selectedProfile)">编辑</button><button v-if="selectedProfile.status==='draft'" class="button primary small" :disabled="selectedProfile.reviewState!=='ready'||!selectedRules.length" @click="actionTarget={kind:'publish-profile',id:selectedProfile.id,label:`发布 ${selectedProfile.name} v${selectedProfile.version}`}"><ShieldCheck :size="12"/>发布</button><button v-if="selectedProfile.status==='published'" class="button ghost small" :disabled="profileBindings.some(item=>item.isActive)" @click="actionTarget={kind:'retire-profile',id:selectedProfile.id,label:`退役 ${selectedProfile.name} v${selectedProfile.version}`}"><Archive :size="12"/>退役</button><button v-if="selectedProfile.status==='draft'" class="button danger small" @click="actionTarget={kind:'delete-profile',id:selectedProfile.id,label:`Profile ${selectedProfile.name} v${selectedProfile.version}`}"><Trash2 :size="12"/>删除</button></div></header>
        <div class="profile-facts"><div><span>模型族</span><strong>{{ selectedProfile.modelFamily }}</strong></div><div><span>Operation / mode</span><strong>{{ selectedProfile.operation }}{{ selectedProfile.mode?` / ${selectedProfile.mode}`:'' }}</strong></div><div><span>审核状态</span><PlatformStatusPill :status="selectedProfile.reviewState"/></div><div><span>冻结合同</span><strong>{{ humanBytes(selectedProfile.contractSizeBytes) }} · {{ selectedProfile.contractSha256.slice(0,12) || 'pending' }}</strong></div><div class="official-source"><span>官方来源（管理员登记）</span><a v-if="selectedProfile.officialReference" :href="selectedProfile.officialReference" target="_blank" rel="noopener noreferrer"><BookOpen :size="12"/>打开登记的官方说明</a><strong v-else>未录入；当前为内部通用规则</strong></div></div>

        <div class="rule-builder-header"><div><strong>字段组装顺序</strong><small>每条规则只能从白名单 source selector 取值；system_instruction 不允许提升项目或请求内容。</small></div><button v-if="selectedProfile.status==='draft'" class="button primary small" :disabled="!activeFields.length" @click="openRule()"><Plus :size="12"/>添加字段规则</button></div>
        <div class="profile-rule-list"><article v-for="item in selectedRules" :key="item.id"><b>{{ item.position }}</b><div><strong>{{ item.fieldLabel || item.fieldKey }}</strong><small>{{ sourceLabel(item.sourceSelector) }} → {{ choiceLabel(options.channels,item.channel) }}</small><small>{{ choiceLabel(options.labelStyles,item.labelStyle) }} · {{ choiceLabel(options.separators,item.separator) }} · max {{ item.maxChars }}<template v-if="item.required"> · 必填</template><template v-if="item.omitIfEmpty"> · 空值省略</template></small></div><span v-if="selectedProfile.status==='draft'"><button class="button ghost tiny" @click="openRule(item)">编辑</button><button class="button danger tiny" @click="actionTarget={kind:'delete-rule',id:item.id,label:`规则 ${item.fieldLabel || item.fieldKey}`}"><Trash2 :size="11"/></button></span></article><p v-if="!selectedRules.length" class="platform-empty">尚无字段规则。空 Profile 不能发布。</p></div>
      </section>
    </template>

    <BaseModal :open="editorOpen" :dismissible="!saving" :title="editorKind==='field'?(fieldForm.id?'编辑通用字段':'新增通用字段'):editorKind==='profile'?(profileForm.id?'编辑 PromptProfile':'新增 PromptProfile'):editorKind==='rule'?(ruleForm.id?'编辑组装规则':'添加组装规则'):(bindingForm.id?'编辑运行时绑定':'新增运行时绑定')" eyebrow="TYPED BUILDER · SERVER VALIDATED" width="860px" @close="editorOpen=false">
      <div v-if="editorKind==='field'" class="prompt-editor-form"><p>通用字段只描述类型与含义，不执行模板代码。被已发布 Profile 引用后不可原地改写。</p><div class="prompt-form-grid"><label>Key <i>必填</i><input v-model="fieldForm.key" :disabled="Boolean(fieldForm.id)" placeholder="camera.movement"/><small>稳定机器标识，小写字母开头。</small></label><label>显示名称 <i>必填</i><input v-model="fieldForm.label" placeholder="镜头运动"/></label><label>分类 <i>必填</i><input v-model="fieldForm.category" placeholder="camera"/></label><label>值类型 <i>必填</i><select v-model="fieldForm.valueType"><option v-for="value in ['text','long_text','string_list','boolean','number','enum']" :key="value" :value="value">{{ value }}</option></select></label><label class="wide">含义说明 <i>必填</i><textarea v-model="fieldForm.description" rows="3" placeholder="说明输入内容、空值语义和填写方式"/></label><label class="wide">影响说明 <i>必填</i><textarea v-model="fieldForm.impact" rows="3" placeholder="说明它会影响哪些模型输出、通道或任务"/></label><label v-if="fieldForm.valueType==='enum'" class="wide">枚举选项 <i>必填</i><textarea v-model="fieldForm.valueOptionsText" rows="3" placeholder="每行一项"/></label><label class="check wide"><input v-model="fieldForm.hasDefault" type="checkbox"/><span>配置管理员默认值（可作为 definition.default 安全来源）</span></label><label v-if="fieldForm.hasDefault&&fieldForm.valueType==='boolean'">默认值<select v-model="fieldForm.defaultBoolean"><option :value="true">true</option><option :value="false">false</option></select></label><label v-else-if="fieldForm.hasDefault" class="wide">默认值<textarea v-if="fieldForm.valueType==='long_text'||fieldForm.valueType==='string_list'" v-model="fieldForm.defaultText" rows="3"/><select v-else-if="fieldForm.valueType==='enum'" v-model="fieldForm.defaultText"><option value="">请选择</option><option v-for="value in splitValues(fieldForm.valueOptionsText)" :key="value" :value="value">{{ value }}</option></select><input v-else v-model="fieldForm.defaultText" :type="fieldForm.valueType==='number'?'number':'text'"/></label><label>排序<input v-model.number="fieldForm.displayOrder" type="number" min="0" step="1"/></label><label class="check"><input v-model="fieldForm.isRequired" type="checkbox"/><span>字段必填</span></label><label class="check"><input v-model="fieldForm.isActive" type="checkbox"/><span>启用字段</span></label></div></div>

      <div v-else-if="editorKind==='profile'" class="prompt-editor-form"><p>Profile 发布后合同冻结；模型官方文档可以作为审核来源，没有时必须明确使用内部通用规则。</p><div class="prompt-form-grid"><label>Profile Key <i>必填</i><input v-model="profileForm.profileKey" :disabled="Boolean(profileForm.id)" placeholder="minimax-h3-t2v"/></label><label>版本 <i>必填</i><input v-model.number="profileForm.version" :disabled="Boolean(profileForm.id)" type="number" min="1" step="1"/></label><label>名称 <i>必填</i><input v-model="profileForm.name"/></label><label>模型族 <i>必填</i><select v-model="profileForm.modelFamily"><option value="">请选择</option><option v-for="value in options.modelFamilies" :key="value" :value="value">{{ value }}</option></select></label><label>Operation <i>必填</i><select v-model="profileForm.operation"><option value="">请选择</option><option v-for="value in options.operations" :key="value" :value="value">{{ value }}</option></select></label><label>Mode<select v-model="profileForm.mode"><option value="">通用 / 不限模式</option><option v-for="value in options.modes" :key="value" :value="value">{{ value }}</option></select></label><label class="wide">策略说明<textarea v-model="profileForm.description" rows="3"/></label><label class="wide">官方参考 URL（管理员核验）<input v-model="profileForm.officialReference" type="url" placeholder="https://..."/><small>请确认链接确属模型官方文档；留空时页面会标记为“内部通用规则”，不伪造官方来源。</small></label><label>审核状态<select v-model="profileForm.reviewState"><option value="needs_review">待审核</option><option value="ready">已审核，可发布</option></select></label></div></div>

      <div v-else-if="editorKind==='rule'" class="prompt-editor-form"><p>数据源、通道、标签与分隔符全部来自服务端白名单，不接受可执行模板。</p><div class="prompt-form-grid"><label>字段 <i>必填</i><select v-model="ruleForm.fieldDefinition" :disabled="Boolean(ruleForm.id)"><option value="">请先选择已启用字段</option><option v-for="item in activeFields" :key="item.id" :value="item.id">{{ item.label }} · {{ item.key }}</option></select><small>{{ selectedRuleField?.description || '选择字段后显示含义' }}</small></label><label>数据来源 <i>必填</i><select v-model="ruleForm.sourceSelector"><option v-for="item in options.sourceSelectors.filter(row=>ruleForm.channel!=='system_instruction'||row.value==='definition.default')" :key="item.value" :value="item.value">{{ item.label }} · {{ item.value }}</option></select></label><label>输出通道 <i>必填</i><select v-model="ruleForm.channel"><option v-for="item in options.channels" :key="item.value" :value="item.value">{{ item.label }}</option></select></label><label>装配位置 <i>必填</i><input v-model.number="ruleForm.position" type="number" min="1" step="1"/></label><label>标签样式<select v-model="ruleForm.labelStyle"><option v-for="item in options.labelStyles" :key="item.value" :value="item.value">{{ item.label }}</option></select></label><label>分隔符<select v-model="ruleForm.separator"><option v-for="item in options.separators" :key="item.value" :value="item.value">{{ item.label }}</option></select></label><label>最大字符数<input v-model.number="ruleForm.maxChars" type="number" min="1" max="30000" step="1"/></label><label class="check"><input v-model="ruleForm.required" type="checkbox"/><span>缺失时阻断装配</span></label><label class="check"><input v-model="ruleForm.omitIfEmpty" type="checkbox"/><span>空值时省略</span></label></div></div>

      <div v-else class="prompt-editor-form"><p>Binding 只能引用 published Profile。选择 Profile 后 operation、model family 和 mode 自动锁定；相同选择器与优先级冲突会被服务端拒绝。</p><div class="prompt-form-grid"><label>Binding Key <i>必填</i><input v-model="bindingForm.key" placeholder="minimax-h3-t2v-default"/></label><label>名称 <i>必填</i><input v-model="bindingForm.name"/></label><label class="wide">Published PromptProfile <i>必填</i><select v-model="bindingForm.promptProfileVersion" @change="syncBindingProfile"><option value="">请先发布 Profile</option><option v-for="item in publishedProfiles" :key="item.id" :value="item.id">{{ item.name }} v{{ item.version }} · {{ item.modelFamily }} / {{ item.operation }}{{ item.mode?` / ${item.mode}`:'' }}</option></select></label><label>Operation<input v-model="bindingForm.operation" disabled/></label><label>模型族<input v-model="bindingForm.modelFamily" disabled/></label><label>Mode<input v-model="bindingForm.mode" disabled placeholder="通用"/></label><label class="wide">精确模型版本<select v-model="bindingForm.modelProfileVersion"><option value="">不限实际模型，按模型族匹配</option><option v-for="item in options.modelProfileVersions" :key="item.id" :value="item.id">{{ item.profileKey }} v{{ item.version }} · {{ item.modelIdentifier }} · {{ item.modality }}</option></select><small>服务端会复核实际模型族；不匹配不会保存。</small></label><label>优先级<input v-model.number="bindingForm.priority" type="number" min="-1000" max="1000" step="1"/></label><label class="check"><input v-model="bindingForm.isActive" type="checkbox"/><span>启用绑定</span></label></div></div>

      <div v-if="editorValidation.length" class="prompt-validation"><AlertTriangle :size="15"/><div><strong>保存前必须修复</strong><ul><li v-for="item in editorValidation" :key="item">{{ item }}</li></ul></div></div>
      <div v-if="editorError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ editorError }}</p></div>
      <template #footer><button class="button ghost" :disabled="saving" @click="editorOpen=false">取消</button><button class="button primary" :disabled="saving||Boolean(editorValidation.length)" @click="submitEditor"><Save :size="13"/>{{ saving?'服务端保存中…':'保存' }}</button></template>
    </BaseModal>

    <BaseModal :open="previewOpen" :dismissible="!previewBusy" :title="`类型化预览 · ${selectedProfile?.name || ''}`" eyebrow="DETERMINISTIC ASSEMBLY PREVIEW" width="1050px" @close="previewOpen=false">
      <div class="prompt-preview"><p>仅可输入 Profile 已白名单的 source selector。预览使用与生产相同的确定性组装器，不调用文本模型。</p><div class="preview-source-grid"><label v-for="row in previewSourceRows" :key="row.selector"><span>{{ sourceLabel(row.selector) }} <code>{{ row.selector }}</code></span><select v-if="row.field?.valueType==='boolean'" :value="previewInputValue(row.selector)" @change="setPreviewInput(row.selector,($event.target as HTMLSelectElement).value===''?'':($event.target as HTMLSelectElement).value==='true')"><option value="">使用默认值 / 留空</option><option value="false">false</option><option value="true">true</option></select><textarea v-else-if="row.field?.valueType==='long_text'||row.field?.valueType==='string_list'" :value="String(previewInputValue(row.selector) || '')" rows="3" @input="setPreviewInput(row.selector,($event.target as HTMLTextAreaElement).value)"/><select v-else-if="row.field?.valueType==='enum'" :value="String(previewInputValue(row.selector) || '')" @change="setPreviewInput(row.selector,($event.target as HTMLSelectElement).value)"><option value="">使用默认值 / 留空</option><option v-for="value in row.field.valueOptions" :key="value" :value="value">{{ value }}</option></select><input v-else :type="row.field?.valueType==='number'?'number':'text'" :value="String(previewInputValue(row.selector) || '')" @input="setPreviewInput(row.selector,($event.target as HTMLInputElement).value)"/><small>{{ row.field?.description }}<template v-if="row.rule.required"> · 该规则必填</template></small></label><p v-if="!previewSourceRows.length" class="platform-empty">所有规则均使用管理员默认值，无需输入请求数据。</p></div><div v-if="previewError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ previewError }}</p></div><section v-if="previewResult" class="preview-result"><header><Check :size="15"/><div><strong>组装完成</strong><small>contract {{ previewResult.contractSha256 }}</small></div></header><div class="preview-channels"><article v-for="(value,key) in previewResult.channels" :key="key"><strong>{{ key }}</strong><pre>{{ value || '空' }}</pre></article></div><details><summary>结构化字段 · {{ Object.keys(previewResult.structuredFields).length }}</summary><dl><div v-for="(value,key) in previewResult.structuredFields" :key="key"><dt>{{ key }}</dt><dd>{{ Array.isArray(value)?value.join(', '):String(value) }}</dd></div></dl></details><details><summary>组装追踪 · {{ previewResult.trace.length }}</summary><table><thead><tr><th>#</th><th>字段</th><th>来源</th><th>通道</th><th>默认</th></tr></thead><tbody><tr v-for="(row,index) in previewResult.trace" :key="index"><td>{{ index+1 }}</td><td>{{ row.field_key }}</td><td>{{ row.source_selector }}</td><td>{{ row.channel }}</td><td>{{ row.defaulted===true?'是':'否' }}</td></tr></tbody></table></details></section></div>
      <template #footer><button class="button ghost" :disabled="previewBusy" @click="previewOpen=false">关闭</button><button class="button primary" :disabled="previewBusy" @click="runPreview"><Eye :size="13"/>{{ previewBusy?'组装中…':'运行服务端预览' }}</button></template>
    </BaseModal>

    <BaseModal :open="Boolean(actionTarget)" :dismissible="!saving" :title="actionTarget?.label || '确认操作'" eyebrow="REVIEW BEFORE COMMIT" @close="actionTarget=null">
      <div class="prompt-action-review"><p v-if="actionTarget?.kind==='publish-profile'">发布前服务端会复核审核状态、字段规则、通道要求与冻结合同。成功后不可原地修改。</p><p v-else-if="actionTarget?.kind==='retire-profile'">仍有 active binding 时退役会被服务端拒绝；历史生产引用保留。</p><p v-else>删除只适用于无引用的可变资源。已发布或已进入运行事实的对象会被服务端拒绝。</p><div v-if="actionError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ actionError }}</p></div></div>
      <template #footer><button class="button ghost" :disabled="saving" @click="actionTarget=null">取消</button><button class="button" :class="actionTarget?.kind.startsWith('delete')?'danger':'primary'" :disabled="saving" @click="runAction">{{ saving?'服务端处理中…':'确认提交' }}</button></template>
    </BaseModal>
  </div>
</template>

<style scoped>
.prompt-profile-settings{display:grid;gap:14px}.prompt-catalog-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.prompt-catalog-card,.prompt-profile-workbench{min-width:0;border:1px solid #252b34;border-radius:10px;background:#0b0e12;overflow:hidden}.prompt-catalog-card>header{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:11px;border-bottom:1px solid #252b34}.prompt-catalog-card>header>div{display:flex;align-items:center;gap:8px}.prompt-catalog-card>header span{display:grid;gap:2px}.prompt-catalog-card>header strong{color:#c8d0da;font-size:11px}.prompt-catalog-card>header small{color:#6f7985;font-size:8px}.prompt-catalog-card>header input{min-width:0;max-width:150px;padding:7px 8px;border:1px solid #2b323c;border-radius:6px;background:#080b0f;color:#d9e0e8;font-size:9px}.prompt-scroll-list{max-height:330px;overflow:auto}.prompt-scroll-list article{display:grid;grid-template-columns:minmax(0,1fr) auto auto;align-items:center;gap:7px;padding:9px 10px;border-bottom:1px solid rgba(255,255,255,.045)}.prompt-scroll-list article>div,.profile-select-row>span{min-width:0;display:grid;gap:3px}.prompt-scroll-list article strong,.profile-select-row strong{overflow:hidden;color:#b4bdc8;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.prompt-scroll-list article small,.profile-select-row small{overflow:hidden;color:#68737f;font-size:8px;text-overflow:ellipsis;white-space:nowrap}.prompt-scroll-list article>span:last-child{display:flex;gap:4px}.prompt-prerequisite{display:flex;gap:7px;align-items:flex-start;margin:8px;padding:8px;border:1px solid rgba(210,145,63,.3);border-radius:6px;background:rgba(210,145,63,.07);color:#cfa86f;font-size:9px;line-height:1.5}.profile-select-row{width:100%;padding:10px;display:flex;align-items:center;justify-content:space-between;gap:10px;border:0;border-bottom:1px solid rgba(255,255,255,.045);background:transparent;text-align:left;cursor:pointer}.profile-select-row:hover,.profile-select-row.active{background:rgba(210,145,63,.08)}.profile-select-row.active{box-shadow:inset 2px 0 #d08f3d}.profile-select-row>span:last-child{justify-items:end}.prompt-profile-workbench>header{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:16px;border-bottom:1px solid #252b34}.prompt-profile-workbench h3{margin:4px 0;color:#e5e9ee;font:20px Georgia,serif}.prompt-profile-workbench p{margin:0;color:#85909c;font-size:10px}.profile-actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:5px}.profile-facts{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:1px;background:#242b34}.profile-facts>div{min-width:0;padding:10px;display:grid;gap:4px;background:#0c1015}.profile-facts span{color:#697481;font-size:8px}.profile-facts strong,.profile-facts a{overflow-wrap:anywhere;color:#b9c2cc;font-size:9px}.profile-facts a{display:flex;align-items:center;gap:5px;color:#d59a50}.rule-builder-header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px}.rule-builder-header>div{display:grid;gap:3px}.rule-builder-header strong{color:#c3cbd5;font-size:11px}.rule-builder-header small{color:#6f7985;font-size:8px}.profile-rule-list{display:grid;gap:6px;padding:0 12px 12px}.profile-rule-list article{display:grid;grid-template-columns:30px minmax(0,1fr) auto;align-items:center;gap:9px;padding:9px;border:1px solid #252c35;border-radius:7px;background:#090c10}.profile-rule-list article>b{display:grid;place-items:center;width:26px;height:26px;border:1px solid #3a342b;border-radius:50%;color:#d69a51;font-size:9px}.profile-rule-list article>div{display:grid;gap:3px}.profile-rule-list article strong{color:#bac3ce;font-size:10px}.profile-rule-list article small{color:#6f7985;font-size:8px}.profile-rule-list article>span{display:flex;gap:4px}.prompt-editor-form{display:grid;gap:12px}.prompt-editor-form>p,.prompt-preview>p{margin:0;color:#94a0ad;line-height:1.6}.prompt-form-grid,.preview-source-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.prompt-form-grid label,.preview-source-grid label{display:grid;align-content:start;gap:6px;padding:10px;border:1px solid #272e37;border-radius:7px;background:#090c10;color:#9ca7b4;font-size:10px}.prompt-form-grid label.wide{grid-column:1/-1}.prompt-form-grid label.check{display:flex;align-items:center}.prompt-form-grid label.check input{width:auto}.prompt-form-grid i{float:right;color:#d69a51;font-size:8px;font-style:normal}.prompt-form-grid input,.prompt-form-grid select,.prompt-form-grid textarea,.preview-source-grid input,.preview-source-grid select,.preview-source-grid textarea{min-width:0;width:100%;padding:9px;border:1px solid #303743;border-radius:6px;background:#070a0e;color:#e0e5eb}.prompt-form-grid small,.preview-source-grid small{color:#697481;font-size:8px;line-height:1.5}.prompt-validation{display:flex;gap:8px;margin-top:12px;padding:10px;border:1px solid rgba(206,76,76,.35);border-radius:7px;background:rgba(206,76,76,.08);color:#ef9999}.prompt-validation ul{margin:5px 0 0;padding-left:18px}.prompt-preview{display:grid;gap:13px}.preview-source-grid label>span{display:flex;align-items:center;justify-content:space-between;gap:8px}.preview-source-grid code{color:#6e7986;font-size:8px}.preview-result{display:grid;gap:10px;border-top:1px solid #282f39;padding-top:12px}.preview-result>header{display:flex;align-items:center;gap:8px;color:#73d2a0}.preview-result>header div{display:grid;gap:2px}.preview-result>header small{color:#6e7986}.preview-channels{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.preview-channels article,.preview-result details{border:1px solid #282f39;border-radius:7px;background:#090c10;overflow:hidden}.preview-channels strong,.preview-result summary{display:block;padding:8px 10px;color:#b8c1cc;font-size:9px}.preview-channels pre{min-height:70px;margin:0;padding:10px;border-top:1px solid #282f39;color:#d2dae3;font:10px/1.6 ui-monospace,Consolas,monospace;white-space:pre-wrap}.preview-result details summary{cursor:pointer}.preview-result dl{display:grid;gap:1px;margin:0;background:#252c35}.preview-result dl div{display:grid;grid-template-columns:180px 1fr;gap:10px;padding:8px;background:#0a0d11}.preview-result dt{color:#798591}.preview-result dd{margin:0;color:#c5ced8;overflow-wrap:anywhere}.preview-result table{width:100%;border-collapse:collapse}.preview-result th,.preview-result td{padding:7px;border-top:1px solid #252c35;color:#8d98a5;text-align:left;font-size:8px}.prompt-action-review{display:grid;gap:12px}.prompt-action-review>p{margin:0;color:#a5afbb;line-height:1.65}
@media(max-width:1180px){.prompt-catalog-grid{grid-template-columns:1fr 1fr}.prompt-catalog-card:last-child{grid-column:1/-1}.profile-facts{grid-template-columns:repeat(3,1fr)}}
@media(max-width:760px){.prompt-catalog-grid,.prompt-form-grid,.preview-source-grid,.preview-channels{grid-template-columns:1fr}.prompt-catalog-card:last-child,.prompt-form-grid label.wide{grid-column:auto}.prompt-profile-workbench>header{flex-direction:column}.profile-actions{justify-content:flex-start}.profile-facts{grid-template-columns:1fr}.preview-result dl div{grid-template-columns:1fr}}
</style>
