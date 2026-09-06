<script setup lang="ts">
import { computed } from 'vue'
import ModelContractFields from '@/components/ModelContractFields.vue'
import { contractFieldsExcept, isContractObject } from '@/services/modelCapabilityContract'
import { promptConfigurationLabel } from '@/services/promptProfileConfiguration'
import { primitiveConfigGroups, rulePrimitive, savedTemplateRows } from '@/services/promptPrimitiveConfiguration'
import type { PromptProfileFieldRule, PromptProfileOptions, PromptProfileVersion } from '@/services/platformTypes'

const props = defineProps<{ profile: PromptProfileVersion; rules: PromptProfileFieldRule[]; options?: PromptProfileOptions; focusRule?: PromptProfileFieldRule }>()
const label = (key: string) => promptConfigurationLabel(key, props.options)
const hasKey = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key)
const primitive = computed(() => props.focusRule ? rulePrimitive(props.focusRule, props.options) : undefined)
const groups = computed(() => primitiveConfigGroups(props.profile, props.rules, props.options)
  .filter(group => !props.focusRule || group.key === primitive.value?.engineKey))
const projection = computed(() => props.profile.sourceProjectionConfig)
const projectionRules = computed(() => props.rules.filter(rule => rule.profileVersion === props.profile.id && rule.sourceSelector === 'projection.output'))
const unlistedRules = computed(() => projectionRules.value.filter(rule => !rulePrimitive(rule, props.options)))
const templateRows = (group: ReturnType<typeof primitiveConfigGroups>[number]) => savedTemplateRows(group.templates, props.options,
  props.focusRule ? primitive.value?.renderingTemplateKeys : undefined)
const summary = (group: ReturnType<typeof primitiveConfigGroups>[number]) => ({
  prompt_template_key: isContractObject(group.rendering) && hasKey(group.rendering, 'prompt_template_key') ? group.rendering.prompt_template_key : '未保存',
  separator: isContractObject(group.rendering) && hasKey(group.rendering, 'separator') ? group.rendering.separator : '未保存',
  list_prefix: isContractObject(group.rendering) && hasKey(group.rendering, 'list_prefix') ? group.rendering.list_prefix : '未保存',
})
</script>

<template>
  <section class="primitive-configuration" :aria-label="focusRule ? '字段相关模板' : '内容生成与模板配置'">
    <header><strong>{{ focusRule ? `${focusRule.fieldLabel || focusRule.fieldKey} · 相关模板` : '内容生成与模板配置' }}</strong>
      <span>v{{ profile.version }} · {{ profile.status === 'draft' ? '已保存草稿' : '已保存版本' }}</span></header>
    <p>此处是版本配置，不是实际执行结果。模板正文为版本原值，用途说明来自当前原语目录；具体使用取决于素材、音频及已审核内容。</p>
    <p v-if="focusRule"><code>{{ focusRule.projectionPrimitive }}</code><template v-if="primitive"> · {{ primitive.description }}</template></p>
    <p v-if="!isContractObject(projection)" class="warning" role="alert">来源投影配置格式异常，请查看完整配置。</p>
    <p v-else-if="hasKey(projection, 'primitive_options') && !isContractObject(projection.primitive_options)" class="warning" role="alert">原语配置格式异常，请查看完整配置。</p>
    <p v-if="focusRule && !primitive" class="warning">当前目录未提供此原语的模板对应关系；下方完整配置仍保留原值。</p>
    <p v-else-if="!groups.length">{{ projectionRules.length ? '未读取到可展示的原语配置，请查看完整配置。' : '此版本没有引用投影原语；内容按各字段指定的来源读取或生成。' }}</p>
    <p v-if="!focusRule && unlistedRules.length" class="warning">{{ unlistedRules.length }} 条字段规则的原语不在当前目录中，无法确定模板对应关系；可通过“查看配置”核对原值。</p>
    <div v-for="group in groups" :key="group.key" class="engine-configuration">
      <div class="engine-heading"><code>{{ label(group.key) }} · {{ group.key }}</code><span v-if="!focusRule">关联 {{ group.rules.length }} 条字段规则 · 保存 {{ Object.keys(group.templates ?? {}).length }} 项模板</span></div>
      <p v-if="!group.saved" class="warning">此版本未保存该原语引擎的配置，不会补入默认值。</p>
      <p v-else-if="!isContractObject(group.config)" class="warning">原语引擎配置格式异常，请展开原始配置核对。</p>
      <template v-else-if="group.rendering !== undefined">
        <template v-if="isContractObject(group.rendering)">
          <ModelContractFields :value="summary(group)" :label-for-key="label" />
          <p v-if="!group.templates" class="warning">文案模板缺失或格式异常，请查看完整配置。</p>
          <details v-else :open="Boolean(focusRule)">
            <summary>{{ focusRule ? '相关文案模板' : '展开文案模板' }}（{{ templateRows(group).length }} 项）</summary>
            <p v-if="!templateRows(group).length">未保存文案模板。</p>
            <div v-for="row in templateRows(group)" :key="row.key" class="saved-template">
              <header><strong>{{ row.label }} <code>{{ row.key }}</code></strong><span :class="{'warning': row.state === '未保存' || row.state === '格式异常'}">{{ row.state }}</span></header>
              <p v-if="row.help">{{ row.help.description }}</p>
              <ModelContractFields v-if="row.state !== '未保存'" :value="row.value" :label-for-key="label" />
              <p v-if="row.help" class="template-requirement">{{ row.help.requirement }}</p>
            </div>
          </details>
          <details v-if="Object.keys(contractFieldsExcept(group.rendering, ['templates', 'separator', 'list_prefix', 'prompt_template_key'])).length">
            <summary>其他已保存的渲染配置</summary><ModelContractFields :value="contractFieldsExcept(group.rendering, ['templates', 'separator', 'list_prefix', 'prompt_template_key'])" :label-for-key="label" />
          </details>
        </template>
        <template v-else><p class="warning">渲染配置格式异常，以下保留返回值。</p><ModelContractFields :value="group.rendering" :label-for-key="label" /></template>
      </template>
      <p v-else>此版本未保存显式文案模板；不能据此还原历史内置文案，也不会套用新版模板。</p>
      <details v-if="group.saved && (!isContractObject(group.config) || Object.keys(contractFieldsExcept(group.config, ['rendering'])).length)">
        <summary>其他已保存的原语配置</summary><ModelContractFields :value="isContractObject(group.config) ? contractFieldsExcept(group.config, ['rendering']) : group.config" :label-for-key="label" />
      </details>
    </div>
    <details v-if="!focusRule && isContractObject(projection) && hasKey(projection, 'quality_advisories')">
      <summary>已保存的质量建议规则</summary><ModelContractFields :value="projection.quality_advisories" :label-for-key="label" />
    </details>
  </section>
</template>

<style scoped>
.primitive-configuration{min-width:0;display:grid;gap:10px;padding:14px;border-top:1px solid var(--border);border-bottom:1px solid var(--border);font-size:12px}
header,.engine-heading{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px}header>span,.engine-heading>span,p{color:var(--text-secondary)}
p{margin:0;line-height:1.6;overflow-wrap:anywhere}code{font-size:11px;overflow-wrap:anywhere}strong{color:var(--text)}.warning{color:var(--warning)}
.engine-configuration{display:grid;gap:10px;min-width:0}.engine-configuration+.engine-configuration{border-top:1px solid var(--border);padding-top:12px}
summary{cursor:pointer;min-height:36px;display:list-item;align-content:center;line-height:1.6}details{min-width:0}details[open]>summary{margin-bottom:8px}
.saved-template{display:grid;gap:8px;min-width:0;padding:12px 0;border-top:1px solid var(--border)}.saved-template :deep(.contract-value){font-family:ui-monospace,Consolas,monospace;line-height:1.6}.template-requirement{font-size:11px}
</style>
