<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { bibleVariantApi } from '@/services/bibleVariantApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { sourceOriginLabel, variantRequestGuard } from '@/services/bibleVariants'
import type { BibleSettingSource, BibleVariantDetail } from '@/services/bibleVariants'

const props = defineProps<{ detail: BibleVariantDetail }>()
const result = ref<BibleSettingSource | null>(null), loading = ref(false), error = ref(''), opened = ref(false)
const gate = variantRequestGuard()
watch(() => [props.detail.id, props.detail.setting.id, props.detail.setting.content_sha256], () => {
  gate.cancel(); result.value = null; loading.value = false; error.value = ''; opened.value = false
})
onBeforeUnmount(() => gate.cancel())
async function load() {
  if (loading.value) return
  const ticket = gate.next(), basis = props.detail
  opened.value = true; loading.value = true; error.value = ''; result.value = null
  try {
    const value = await bibleVariantApi.source(basis)
    if (gate.current(ticket)) result.value = value
  } catch (reason) {
    if (gate.current(ticket)) error.value = apiErrorSummary(reason, '来源对照读取失败；不能判断初始声明与人工补充。')
  } finally { if (gate.current(ticket)) loading.value = false }
}
</script>

<template>
  <section class="setting-source" aria-label="设定来源与修改对照">
    <button type="button" class="button ghost" :disabled="loading" :aria-expanded="opened" @click="load">
      {{ opened ? '重新读取来源对照' : '查看初始声明与本修订对照' }}
    </button>
    <p v-if="loading" role="status">正在读取确切来源…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="result">
      <p v-if="result.source">对照初次创建时的结构化声明；它不等同于原稿逐句事实。配置常量、默认值及本修订改动分别标明，后续剧本复用不改写初始来源。</p>
      <p v-else>此设定由人工登记，没有初始剧本声明；不会从当前剧本补造来源。</p>
      <ul class="source-fields">
        <li v-for="field in result.fields" :key="field.key">
          <strong>{{ field.label }} · {{ sourceOriginLabel(field.origin) }}</strong>
          <dl>
            <template v-if="result.source"><dt>初始声明值</dt><dd>{{ field.declaration_value === null ? '未提供' : field.declaration_value === '' ? '（明确空字符串）' : field.declaration_value }}</dd></template>
            <dt>所选修订值</dt><dd>{{ field.setting_value === '' ? '（空字符串）' : field.setting_value }}</dd>
          </dl>
          <p v-if="field.changed_from_declaration === true">本修订人工设计修改 / 补充；未覆盖初始声明。</p>
          <p v-else-if="field.changed_from_declaration === false">与初始声明一致；不表示图片已生成或采用。</p>
          <details v-if="field.source_path"><summary>查看输出字段路径</summary><code>{{ field.source_path }}</code></details>
        </li>
      </ul>
      <details v-if="result.source"><summary>初始来源标识（只读）</summary>
        <p>剧本修订：{{ result.source.script_revision_id }}</p>
        <p>实体键：{{ result.source.entity_key }} · {{ result.source.variant_key ? `变体键：${result.source.variant_key}` : '主图声明' }}</p>
        <p>来源绑定：{{ result.source.binding_id }}</p>
        <p>实体声明 SHA256：{{ result.source.declaration_sha256 }}</p>
        <p>结构化详情 SHA256：{{ result.document_sha256 }}</p>
      </details>
    </template>
  </section>
</template>

<style scoped>
.setting-source{margin:16px 0;min-width:0;overflow-wrap:anywhere}.source-fields{list-style:none;margin:8px 0;padding:0}
.source-fields li{padding:12px 0;border-bottom:1px solid var(--border)}.source-fields dl{margin:8px 0}
.source-fields dt{color:var(--text-secondary)}.source-fields dd{margin:4px 0 10px;white-space:pre-wrap}
.setting-source code{white-space:pre-wrap;overflow-wrap:anywhere}.setting-source [role=alert]{color:var(--danger,#ef8a88)}
.setting-source button{min-height:36px;white-space:normal}
</style>
