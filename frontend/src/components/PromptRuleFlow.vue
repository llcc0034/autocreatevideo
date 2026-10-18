<script setup lang="ts">
import { computed } from 'vue'
import type { PromptProfileOptions, PromptProfileVersion } from '@/services/platformTypes'
import { ruleContentMethod, ruleTemplatePath, type PromptRulePath } from '@/services/promptPrimitiveConfiguration'
import { promptConfigurationLabel } from '@/services/promptProfileConfiguration'
import ReadonlyDataValue from '@/components/ReadonlyDataValue.vue'
import { structuredContextLabel } from '@/services/promptGenerationContexts'

const props = defineProps<{ rule: PromptRulePath; profile: Pick<PromptProfileVersion, 'sourceProjectionConfig'> & Partial<Pick<PromptProfileVersion, 'inputStructureVersion'>>; options?: PromptProfileOptions; draft?: boolean }>()
const path = computed(() => ruleTemplatePath(props.profile, props.rule, props.options))
const assisted = computed(() => ['model_generated', 'translate_only'].includes(props.rule.generationMode))
const source = computed(() => assisted.value ? props.rule.generationSourceSelector : props.rule.sourceSelector)
const sourceName = computed(() => (assisted.value ? props.options?.generationSourceSelectors : props.options?.sourceSelectors)?.find(item => item.value === source.value)?.label)
const languageName = computed(() => props.options?.languagePolicies?.find(item => item.value === props.rule.languagePolicy)?.label)
</script>

<template>
  <section class="prompt-rule-flow" aria-label="字段内容流转">
    <header><strong>字段内容流转</strong><small>{{ draft ? '当前未保存设置' : '此规则已保存的设置' }} · 配置路径，不是执行结果</small></header>
    <ol>
      <li><span>1 · {{ assisted ? '主要生成／翻译来源' : '内容来源' }}</span><strong>{{ sourceName || source || '未声明来源' }}</strong><code v-if="sourceName">{{ source }}</code></li>
      <li><span>2 · 内容处理</span><strong>{{ ruleContentMethod(rule, path.active && path.templates.length > 0) }}</strong><code>{{ rule.generationMode || '未选择' }}<template v-if="assisted"> · {{ rule.generationOutputSemantics || '未选择' }}</template></code></li>
      <li v-if="path.active"><span>3 · 原语合成</span><strong>{{ path.primitive?.label || rule.projectionPrimitive || '未选择原语' }}</strong><code v-if="path.primitive?.label">{{ rule.projectionPrimitive }}</code></li>
      <li><span>{{ path.active ? 4 : 3 }} · 字段 → 输出通道</span><strong>{{ rule.fieldLabel || rule.fieldKey || '未选择字段' }}</strong><span>{{ rule.channel ? promptConfigurationLabel(rule.channel, options) : '未选择通道' }} · 顺序 {{ rule.position ?? '未声明' }}</span></li>
    </ol>
    <dl class="processing-facts">
      <template v-if="source === 'request.structured_input'">
        <div><dt>输入结构版本</dt><dd><ReadonlyDataValue :value="profile.inputStructureVersion" label="输入结构版本" /></dd></div>
        <div><dt>确切字段路径</dt><dd><ReadonlyDataValue :value="rule.sourcePath" label="确切字段路径" /></dd></div>
      </template>
      <div v-if="rule.languagePolicy !== undefined"><dt>{{ rule.generationMode === 'translate_only' ? '翻译目标语言' : '创作内容语言约束' }}</dt><dd>{{ languageName || rule.languagePolicy || '未选择语言策略' }}<code v-if="languageName">{{ rule.languagePolicy }}</code></dd></div>
    </dl>
    <details v-if="assisted" class="processing-inputs">
      <summary>辅助处理会收到什么</summary>
      <p>仅所选主要来源及明确配置的额外信息；不自动加入整份结构或相邻内容，不把此处配置当成已发送请求。</p>
      <strong>字段生成／翻译说明</strong><ReadonlyDataValue :value="rule.generationInstruction" label="字段生成／翻译说明" />
      <strong>额外参考信息</strong>
      <ul v-if="rule.generationContextSelectors?.length"><li v-for="(value,index) in rule.generationContextSelectors" :key="`${index}:${value}`">{{ options?.generationContextSelectors?.find(item => item.value === value)?.label || structuredContextLabel(value) }}<code>{{ value }}</code></li></ul>
      <p v-else>{{ rule.generationContextSelectors ? '未选择额外信息' : '额外信息字段未提供' }}</p>
    </details>
    <p v-else-if="['passthrough','server_locked'].includes(rule.generationMode)">本方式不派发字段辅助任务；已保存的生成说明和额外参考信息不代表当前方式会调用模型。</p>
    <p v-if="rule.languagePolicy !== undefined">语言约束作用于创作内容；冻结的 Bible／素材参考原名、描述与输入槽位保留原值，不因语言设置而翻译。</p>
    <p v-if="assisted && rule.generationOutputSemantics === 'final'">采用的生成／翻译结果直接作为字段值进入通道排版；不会再作为贡献交给原语套模板。生成说明、语言与校验仍以本规则为准。</p>
    <p v-if="assisted && rule.generationOutputSemantics === 'contribution'">采用的生成／翻译结果是贡献，不是最终段落；由明确配置的原语合成完整字段后，再按字段顺序、标签与分隔符进入通道。</p>
    <template v-if="path.active">
      <p v-if="!path.primitive" role="alert">目录未提供此原语的模板关系，不能按字段名猜测。</p>
      <details v-else-if="path.templates.length">
        <summary>此路径关联的文案模板 · {{ path.templates.length }} 项</summary>
        <p>关系来自原语目录；是否在本次调用用到每段，取决于实际输入。这里保留配置原值，不填入示例或默认文案。</p>
        <div v-for="row in path.templates" :key="row.key" class="flow-template">
          <strong>{{ row.label }} · {{ row.state }}</strong><code>{{ row.key }}</code>
          <ReadonlyDataValue v-if="row.state !== '未保存'" :value="row.value" />
        </div>
      </details>
      <p v-else>目录未声明此原语使用文案模板，不推断模板关系。</p>
    </template>
    <p v-if="assisted">此处只声明辅助处理需求。是否需要辅助调用及采用哪个结果由正式准备链决定；实际辅助执行实例在使用此策略的主执行实例中绑定。</p>
  </section>
</template>

<style scoped>
.prompt-rule-flow{display:grid;gap:12px;min-width:0;padding:16px;border:1px solid var(--border);border-radius:var(--radius-sm,6px);font-size:var(--config-body-size,14px);line-height:1.6}
header{display:grid;gap:4px}header>strong{font-size:var(--config-section-size,16px)}small,li>span,p{color:var(--text-secondary)}small,code{font-size:var(--config-help-size,12px)}
ol{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,200px),1fr));gap:12px;list-style:none;padding:0;margin:0}li{display:grid;align-content:start;gap:6px;min-width:0;border-left:2px solid var(--border);padding-left:12px}
p{margin:0}strong,code,span,p{overflow-wrap:anywhere}details{min-width:0}summary{cursor:pointer;min-height:36px;padding:6px 0}details>p{margin:8px 0}.flow-template{display:grid;gap:6px;padding:12px 0;border-top:1px solid var(--border);min-width:0}[role=alert]{color:var(--warning)}
.processing-facts{margin:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:var(--config-space-md)}.processing-facts>div{min-width:0}.processing-facts dt{font-weight:600}.processing-facts dd{margin:4px 0 0;overflow-wrap:anywhere}.processing-facts code,.processing-inputs code{display:block}.processing-inputs>strong{display:block;margin-top:12px}.processing-inputs ul{padding-left:20px}.processing-inputs li{display:list-item;border:0;padding:0}.processing-inputs strong{font-weight:600}
</style>
