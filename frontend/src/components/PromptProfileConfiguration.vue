<script setup lang="ts">
import { computed } from 'vue'
import ModelContractFields from '@/components/ModelContractFields.vue'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import PromptPrimitiveConfiguration from '@/components/PromptPrimitiveConfiguration.vue'
import { contractFieldsExcept, isContractObject } from '@/services/modelCapabilityContract'
import { promptConfigurationLabel } from '@/services/promptProfileConfiguration'
import { operationLabel, modeLabel } from '@/services/presentationLabels'
import type { PromptProfileFieldRule, PromptProfileOptions, PromptProfileVersion } from '@/services/platformTypes'

const props = defineProps<{ profile: PromptProfileVersion; rules: PromptProfileFieldRule[]; options?: PromptProfileOptions; focusRuleId?: string }>()
const label = (key: string) => promptConfigurationLabel(key, props.options)
const versionRules = computed(() => props.rules.filter(rule => rule.profileVersion === props.profile.id).sort((a, b) => a.position - b.position))
const focusRule = computed(() => versionRules.value.find(rule => rule.id === props.focusRuleId))
const projection = computed(() => props.profile.sourceProjectionConfig)
const emptyProjection = computed(() => isContractObject(projection.value) && !Object.keys(projection.value).length)
const metadata = computed(() => contractFieldsExcept(props.profile, ['sourceProjectionConfig', 'channelLimits']))
const sourceName = computed(() => props.options?.sourceProjectionKeys.find(item => item.value === props.profile.sourceProjectionKey)?.label)
const technicalData = computed(() => JSON.stringify({ profile: props.profile, fieldRules: versionRules.value }, null, 2))
const ruleIdentityKeys = ['id', 'createdAt', 'updatedAt', 'profileVersion', 'fieldDefinition']
const ruleIdentity = (rule: PromptProfileFieldRule) => Object.fromEntries(Object.entries(rule).filter(([key]) => ruleIdentityKeys.includes(key)))
</script>

<template>
  <div class="profile-configuration">
    <div class="configuration-heading">
      <div><strong>{{ profile.profileKey }} · v{{ profile.version }}</strong><p>{{ profile.modelFamily }} · {{ operationLabel(profile.operation) }}<template v-if="profile.mode"> · {{ modeLabel(profile.mode) }}</template></p></div>
      <PlatformStatusPill :status="profile.status" />
    </div>
    <p class="configuration-note">只读展示此版本已保存的配置，不补入新版默认值，不执行预览或模型调用。查看历史配置不代表当前可执行；当时依赖的内置代码文案不属于已保存配置，不能在这里还原。</p>
    <p v-if="profile.status === 'draft'" class="configuration-note">此版本尚未发布，展示的是已保存草稿，不包含编辑器中未保存的修改。</p>
    <PromptPrimitiveConfiguration v-if="focusRule" :profile="profile" :rules="versionRules" :options="options" :focus-rule="focusRule" />
    <p v-else-if="focusRuleId" role="alert">未读取到指定字段规则，请重新读取页面。以下仅展示当前版本的配置。</p>
    <details :open="!focusRule" aria-label="来源投影配置">
      <summary>来源投影配置与模板</summary>
      <p>{{ sourceName ? `${sourceName} · ` : '' }}<code>{{ profile.sourceProjectionKey }}</code></p>
      <p v-if="emptyProjection" class="configuration-note">此版本未保存额外来源投影配置。</p>
      <template v-else>
        <p v-if="!isContractObject(projection)" role="alert">来源投影配置格式异常，以下保留返回值。</p>
        <ModelContractFields :value="projection" :label-for-key="label" />
      </template>
    </details>
    <section aria-label="通道长度限制">
      <h4>通道长度限制</h4>
      <p v-if="isContractObject(profile.channelLimits) && !Object.keys(profile.channelLimits).length" class="configuration-note">此版本未设置通道长度限制。</p>
      <ModelContractFields v-else :value="profile.channelLimits" :label-for-key="label" />
    </section>
    <section aria-label="字段规则配置">
      <h4>字段规则 · {{ versionRules.length }} 条</h4>
      <p v-if="versionRules.length !== profile.fieldRuleCount" role="alert">字段规则数据不完整：版本声明 {{ profile.fieldRuleCount }} 条，当前读取 {{ versionRules.length }} 条。请关闭后重新读取页面。</p>
      <p v-if="!versionRules.length" class="configuration-note">当前未读取到字段规则。</p>
      <details v-for="rule in versionRules" :key="rule.id" class="rule-configuration">
        <summary>{{ rule.position }} · {{ rule.fieldLabel || rule.fieldKey }} <small>{{ rule.fieldKey }} · {{ label(rule.channel) }}</small></summary>
        <ModelContractFields :value="contractFieldsExcept(rule, ruleIdentityKeys)" :label-for-key="label" />
        <details><summary>规则标识与记录时间</summary><ModelContractFields :value="ruleIdentity(rule)" :label-for-key="label" /></details>
      </details>
    </section>
    <details>
      <summary>版本信息与合同标识</summary>
      <ModelContractFields :value="metadata" :label-for-key="label" />
    </details>
    <details>
      <summary>接口配置数据（JSON）</summary>
      <p class="configuration-note">与上方同一版本和字段规则；页面字段名已转换，不是用于重新计算合同 SHA-256 的规范化字节。</p>
      <pre>{{ technicalData }}</pre>
    </details>
  </div>
</template>

<style scoped>
.profile-configuration{display:grid;gap:16px;min-width:0}.configuration-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.configuration-heading p{margin:6px 0 0}.configuration-note,p,summary small{color:var(--text-secondary)}p{margin:0;line-height:1.6;overflow-wrap:anywhere}
section{min-width:0;border-top:1px solid var(--border);padding-top:14px;display:grid;gap:12px}h4{margin:0}
summary{cursor:pointer;min-height:36px;line-height:1.6;padding:8px 0;overflow-wrap:anywhere}summary small{display:block;margin-left:16px}
details{min-width:0}details[open]>summary{margin-bottom:10px}.rule-configuration{border-bottom:1px solid var(--border)}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-height:420px;overflow:auto;margin:0}code{overflow-wrap:anywhere}[role=alert]{color:var(--warning)}
</style>
