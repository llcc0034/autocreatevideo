<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { JsonObject } from '@/services/platformTypes'
import { relationConfigUrl, useRegistryRelations, type RelationRow } from '@/services/registryRelations'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import ExecutionParameterEvidence from './ExecutionParameterEvidence.vue'

const props = defineProps<{ resolvedInputs: JsonObject }>()
const expanded = ref(false)
const object = (value: unknown): JsonObject | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : null
// Read only the persisted preparation. Never reconstruct it from today's
// project, instance defaults, input names, or an unconfirmed local draft.
const inputs = computed(() => object(object(props.resolvedInputs.inputs)?.logical_values))
const parameters = computed(() => object(object(props.resolvedInputs.parameters)?.values))
const parameterContracts = computed(() => object(object(props.resolvedInputs.parameters)?.contracts))
const parameterSources = computed(() => object(object(props.resolvedInputs.parameters)?.sources))
const groups = computed(() => [
  { key: 'inputs', title: '已解析输入', empty: '本次未记录逻辑输入值。', value: inputs.value },
  { key: 'parameters', title: '实际运行参数', empty: '本次没有运行参数。', value: parameters.value },
])
const summary = computed(() => inputs.value === null || parameters.value === null ? '摘要数据不完整'
  : `${Object.keys(inputs.value).length} 项输入 · ${Object.keys(parameters.value).length} 项参数`)

type FieldSource = { unit: string; key: string; label: string; definitionId: string; versionId: string; version: number }
type FieldSources = { error: string; profileId: string; profileSha: string; fields: FieldSource[] }
const sourcesExpanded = ref(false)
const uuid = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
const sha = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const nonempty = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
// This is a display projection of saved identities, not a second field-contract
// validator. The registry supplies navigation only, never replacement facts.
const fieldSources = computed<FieldSources>(() => {
  const fail = (error: string): FieldSources => ({ error, profileId: '', profileSha: '', fields: [] })
  const batches = props.resolvedInputs.field_requests
  if (!Array.isArray(batches)) return fail('本次未记录完整的辅助字段来源，无法定位配置；不代表没有辅助字段。')
  const result: FieldSources = { error: '', profileId: '', profileSha: '', fields: [] }
  const destinations = new Set<string>(), definitions = new Map<string, string>(), identities = new Map<string, string>()
  for (const value of batches) {
    const envelope = object(value), batch = object(envelope?.value), profile = object(batch?.profile)
    if (envelope?.schema_version !== 1 || !sha(envelope.sha256) || batch?.schema_version !== 1
      || !['prompt.fields.generate', 'prompt.fields.translate'].includes(String(batch.operation))
      || !uuid(profile?.version_id) || !sha(profile?.contract_sha256)
      || !Array.isArray(batch.items) || !batch.items.length) return fail('辅助字段的冻结来源缺失或格式异常，无法定位配置。')
    if (result.profileId && (result.profileId !== profile.version_id || result.profileSha !== profile.contract_sha256)) {
      return fail('辅助字段批次的冻结策略身份或摘要矛盾，无法定位配置。')
    }
    result.profileId = profile.version_id; result.profileSha = profile.contract_sha256
    for (const entry of batch.items) {
      const item = object(entry), rule = object(item?.field_contract), definition = object(rule?.field_definition)
      if (!nonempty(item?.unit_key) || !nonempty(rule?.field_key) || definition?.key !== rule.field_key
        || !uuid(definition?.field_definition_id) || !uuid(definition?.field_definition_version_id)
        || !nonempty(definition?.label) || !Number.isSafeInteger(definition?.version) || Number(definition?.version) < 1) {
        return fail('辅助字段的冻结定义身份或版本缺失，无法定位配置。')
      }
      const destination = JSON.stringify([item.unit_key, rule.field_key])
      const signature = JSON.stringify([definition.field_definition_id, rule.field_key, definition.version, definition.label])
      if (destinations.has(destination)
        || (definitions.has(definition.field_definition_version_id) && definitions.get(definition.field_definition_version_id) !== signature)
        || (identities.has(rule.field_key) && identities.get(rule.field_key) !== definition.field_definition_version_id)) {
        return fail('辅助字段存在重复单元字段或矛盾的冻结定义，无法定位配置。')
      }
      destinations.add(destination); definitions.set(definition.field_definition_version_id, signature)
      identities.set(rule.field_key, definition.field_definition_version_id)
      result.fields.push({ unit: item.unit_key, key: rule.field_key, label: definition.label,
        definitionId: definition.field_definition_id, versionId: definition.field_definition_version_id, version: Number(definition.version) })
    }
  }
  const savedProfile = object(object(props.resolvedInputs.profile_sources)?.profile)
  if (result.fields.length && savedProfile
    && (savedProfile.version_id !== result.profileId || savedProfile.contract_sha256 !== result.profileSha)) {
    return fail('辅助字段与本次冻结主策略不一致，无法定位配置。')
  }
  return result
})
// Closing or changing the saved preparation clears the visible links. The
// shared reader cancels requests and discards late responses for the old IDs.
watch(() => props.resolvedInputs, () => { sourcesExpanded.value = false }, { flush: 'sync' })
watch(expanded, value => { if (!value) sourcesExpanded.value = false }, { flush: 'sync' })
const sourcesVisible = computed(() => expanded.value && sourcesExpanded.value && !fieldSources.value.error)
const profileRelations = useRegistryRelations('prompt', () => sourcesVisible.value && fieldSources.value.profileId
  ? [{ id: fieldSources.value.profileId }] : [])
const fieldRelations = useRegistryRelations('field_version', () => sourcesVisible.value
  ? [...new Set(fieldSources.value.fields.map(field => field.versionId))].map(id => ({ id })) : [])
function configurationLink(row: RelationRow | undefined, field?: FieldSource): { url: string; error: string } {
  if (!sourcesVisible.value || !row) return { url: '', error: '' }
  const id = field?.versionId ?? fieldSources.value.profileId
  const kind = field ? 'field_version' : 'prompt'
  // Verify the returned navigation still targets the saved identity; do not
  // invent a replacement route if the directory returns a conflicting one.
  const target = new URL(row.config_url, 'https://configuration.invalid')
  const expected = field ? { section: 'fields', field_definition: field.definitionId, field_version: id }
    : { section: 'prompts', prompt_profile_version: id }
  if (row.id !== id || row.kind !== kind || target.origin !== 'https://configuration.invalid' || target.pathname !== '/settings'
    || Object.entries(expected).some(([key, value]) => target.searchParams.getAll(key).length !== 1 || target.searchParams.get(key) !== value)) {
    return { url: '', error: '配置目录返回的目标与冻结身份不一致，无法跳转。' }
  }
  return { url: relationConfigUrl(row), error: '' }
}
const profileLink = computed(() => configurationLink(profileRelations.rows.value[fieldSources.value.profileId]))
const sourceRows = computed(() => fieldSources.value.fields.map(field => ({ ...field,
  link: configurationLink(fieldRelations.rows.value[field.versionId], field) })))
</script>

<template>
  <details class="preparation-facts" @toggle="expanded = ($event.target as HTMLDetailsElement).open">
    <summary><strong>输入与运行参数</strong><span>{{ summary }} · 只读</span></summary>
    <div v-if="expanded" class="facts-body">
      <p>来自本次已保存的准备，不随项目修改变化；这些是生成输入，不是模型结果。字段标识沿用实例定义，来源与映射可在底部查看。</p>
      <section v-for="group in groups" :key="group.key" :aria-label="group.title">
        <h4>{{ group.title }}</h4>
        <p v-if="group.value === null" class="facts-missing" role="status">{{ group.title }}数据缺失或格式不完整，无法展示摘要；请查看原始记录并重新读取准备。</p>
        <p v-else-if="!Object.keys(group.value).length">{{ group.empty }}</p>
        <dl v-else class="fact-list">
          <div v-for="(value, key) in group.value" :key="key"><dt>{{ key }}</dt><dd><ReadonlyDataValue :value="value" :label="`${group.title} · ${key}`"/>
            <template v-if="group.key==='parameters'">
              <p v-if="parameterSources?.[key]==='workflow.static_semantics'">来自工作流冻结静态事实，不是可调参数。</p>
              <ExecutionParameterEvidence v-else :entry="parameterContracts?.[key]" resolved :actual-source="parameterSources?.[key]"/>
            </template>
          </dd></div>
        </dl>
      </section>
      <details class="field-sources" :open="sourcesExpanded" @toggle="sourcesExpanded = ($event.target as HTMLDetailsElement).open">
        <summary>辅助字段配置来源</summary>
        <div v-if="sourcesExpanded" class="field-source-body">
          <p v-if="fieldSources.error" class="facts-missing" role="alert">{{ fieldSources.error }}</p>
          <p v-else-if="!fieldSources.fields.length">本次冻结记录没有辅助字段批次。</p>
          <template v-else>
            <p>以下身份与名称来自本次冻结记录。配置链接在新标签页打开；查看配置不会生成、确认或采用字段，也不会改变本次请求。</p>
            <section aria-label="冻结提示词策略来源">
              <h4>本次提示词策略</h4>
              <p>版本 ID：<code>{{ fieldSources.profileId }}</code></p>
              <p>冻结摘要：<code>{{ fieldSources.profileSha }}</code></p>
              <p v-if="profileRelations.loading.value" role="status">正在读取策略配置入口…</p>
              <p v-else-if="profileRelations.error.value" class="facts-missing" role="alert">策略配置入口读取失败：{{ profileRelations.error.value }}
                <button type="button" class="button ghost small" @click="profileRelations.reload">重新读取策略入口</button></p>
              <p v-else-if="profileLink.error" class="facts-missing" role="alert">{{ profileLink.error }}</p>
              <a v-else-if="profileLink.url" :href="profileLink.url" target="_blank" rel="noopener noreferrer">查看本次 Profile 字段规则（新标签页）</a>
              <p v-else role="status">策略配置入口尚未读取。</p>
            </section>
            <p v-if="fieldRelations.loading.value" role="status">正在读取字段定义配置入口…</p>
            <p v-else-if="fieldRelations.error.value" class="facts-missing" role="alert">字段定义入口读取失败：{{ fieldRelations.error.value }}
              <button type="button" class="button ghost small" @click="fieldRelations.reload">重新读取字段入口</button></p>
            <ul class="field-source-list">
              <li v-for="row in sourceRows" :key="JSON.stringify([row.unit, row.key])">
                <strong>{{ row.label }} · 定义 v{{ row.version }}</strong>
                <p>单元：{{ row.unit }} · 字段：{{ row.key }}</p>
                <p>定义版本 ID：<code>{{ row.versionId }}</code></p>
                <p v-if="row.link.error" class="facts-missing" role="alert">{{ row.link.error }}</p>
                <a v-else-if="!fieldRelations.loading.value && !fieldRelations.error.value && row.link.url"
                  :href="row.link.url" target="_blank" rel="noopener noreferrer">查看此字段定义版本（新标签页）</a>
                <p v-else-if="!fieldRelations.loading.value && !fieldRelations.error.value" role="status">字段配置入口尚未读取。</p>
              </li>
            </ul>
          </template>
        </div>
      </details>
      <ReadonlyDataValue :value="resolvedInputs" label="查看来源、映射与完整记录（技术信息）"/>
    </div>
  </details>
</template>

<style scoped>
.field-sources{border-top:1px solid var(--border);padding-top:10px;min-width:0}.field-sources>summary{cursor:pointer;min-height:36px;line-height:36px;font-size:13px}.field-source-body{display:grid;gap:12px;min-width:0;padding-top:8px}.field-source-body section{display:grid;gap:6px}.field-source-list{list-style:none;margin:0;padding:0;display:grid;gap:12px}.field-source-list li{min-width:0;border-top:1px solid var(--border);padding-top:10px;display:grid;gap:6px}.field-source-body strong,.field-source-body code,.field-source-body a{overflow-wrap:anywhere}.field-source-body a{font-size:13px;display:inline-block;min-height:36px;line-height:1.7;padding:6px 0}.field-source-body .button{white-space:normal;min-height:36px;margin:6px 0}.field-sources summary:focus-visible,.field-source-body a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.preparation-facts{min-width:0;border:1px solid var(--border);border-radius:8px}.preparation-facts>summary{cursor:pointer;padding:12px 14px;font-size:13px;line-height:1.8;overflow-wrap:anywhere}.preparation-facts>summary span{margin-left:12px;font-size:12px;color:var(--text-secondary)}.preparation-facts>summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.facts-body{padding:0 14px 14px;display:grid;gap:16px;min-width:0}.facts-body h4,.facts-body p{margin:0;font-size:13px;line-height:1.7}.facts-body p{color:var(--text-secondary)}.facts-body .facts-missing{color:var(--warning,var(--accent))}.facts-body section{min-width:0}.fact-list{margin:8px 0 0}.fact-list>div{display:grid;grid-template-columns:minmax(100px,180px) minmax(0,1fr);gap:12px;padding:10px 0;border-top:1px solid var(--border);min-width:0}.fact-list dt{font-size:12px;color:var(--text-secondary);overflow-wrap:anywhere}.fact-list dd{margin:0;min-width:0}@media(max-width:640px){.fact-list>div{grid-template-columns:minmax(0,1fr);gap:5px}.preparation-facts>summary span{display:block;margin-left:16px}}
</style>
