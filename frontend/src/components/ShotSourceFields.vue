<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { shotSourceFieldsApi } from '@/services/shotSourceFieldsApi'
import { isSavedShotId, readShotSourceFields, type ShotSourceFields } from '@/services/shotSourceFields'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import ReadonlyDataValue from './ReadonlyDataValue.vue'

const props = defineProps<{ projectId: string; shotId: string }>()
const open = ref(false), loading = ref(false), error = ref('')
const detail = ref<ShotSourceFields | null>(null)
const saved = computed(() => isSavedShotId(props.shotId))
let sequence = 0
async function load() {
  if (loading.value || !saved.value) return
  const token = ++sequence, shotId = props.shotId
  open.value = true; loading.value = true; error.value = ''; detail.value = null
  try {
    const result = readShotSourceFields(await shotSourceFieldsApi.read(shotId), shotId)
    if (token === sequence) detail.value = result
  } catch (cause) { if (token === sequence) error.value = apiErrorSummary(cause) }
  finally { if (token === sequence) loading.value = false }
}
watch([() => props.projectId, () => props.shotId], () => {
  sequence++; open.value = false; loading.value = false; error.value = ''; detail.value = null
})
onBeforeUnmount(() => { sequence++ })
</script>

<template>
  <section class="panel shot-source-fields" aria-label="原结构化字段">
    <header><div><h3>原结构化字段 · 只读</h3><p>本镜创建时绑定的原值、字段定义和结构版本，不随当前配置或镜头编号变化。人工修改通过独立字段修订另存；这里不代表某次模型已收到的输入。</p></div>
      <button type="button" class="button ghost small" :disabled="loading || !saved" @click="load">{{ open ? '重新读取原结构化字段' : '查看原结构化字段' }}</button>
    </header>
    <p v-if="!saved">请先保存镜头。手工创建且没有结构化来源的镜头，不会自动生成来源记录。</p>
    <template v-if="open">
      <p v-if="loading" role="status">正在读取本镜的原结构化字段…</p>
      <p v-if="error" role="alert">{{ error }}</p>
      <template v-if="detail">
        <p>原坐标：第 {{ detail.source.episode_number }} 集 / 第 {{ detail.source.shot_number }} 镜 · {{ detail.fields.length }} 个字段</p>
        <details><summary>原修订与落库依据</summary><dl>
          <dt>结构化修订</dt><dd>{{ detail.source.structure_id }}</dd>
          <dt>创建阶段</dt><dd>{{ detail.source.stage_id }}</dd>
          <dt>原文档 SHA256</dt><dd>{{ detail.source.document_sha256 }}</dd>
          <dt>落库合同 SHA256</dt><dd>{{ detail.source.materialization_sha256 }}</dd>
        </dl></details>
        <p v-if="!detail.fields.length">原结构中没有绑定到本镜的独立字段定义；不自动推造字段身份。</p>
        <article v-for="(field, index) in detail.fields" :key="`${field.source_path}:${field.node_key}:${index}`">
          <h4>{{ field.field_definition.label }} <small>{{ field.field_definition.key }} · v{{ field.field_definition.version }}</small></h4>
          <p>{{ !field.present ? '原字段缺失' : field.source_present ? '原输入提供' : '配置默认值' }}<span v-if="field.default_paths.length && field.source_present"> · 包含配置默认值</span></p>
          <ReadonlyDataValue :value="field.value" :label="field.field_definition.label" />
          <details><summary>确切字段与来源</summary><dl>
            <dt>字段身份</dt><dd>{{ field.field_definition.field_definition_id }}</dd>
            <dt>字段定义版本</dt><dd>{{ field.field_definition.field_definition_version_id }}</dd>
            <dt>数据结构版本</dt><dd>{{ field.structure_version_id }}</dd>
            <dt>结构节点</dt><dd>{{ field.node_key }}</dd>
            <dt>原值路径</dt><dd>{{ field.source_path }}</dd>
            <template v-if="field.default_paths.length"><dt>应用默认值的路径</dt><dd>{{ field.default_paths.join('\n') }}</dd></template>
          </dl></details>
        </article>
      </template>
    </template>
  </section>
</template>

<style scoped>
.shot-source-fields{padding:16px;display:grid;gap:12px;min-width:0}.shot-source-fields header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.shot-source-fields header>div{flex:1 1 240px;min-width:0}.shot-source-fields h3,.shot-source-fields h4,.shot-source-fields p{margin:0}.shot-source-fields p,.shot-source-fields small{color:var(--text-secondary);line-height:1.7}.shot-source-fields article{display:grid;gap:8px;min-width:0;border-top:1px solid var(--border);padding-top:12px}.shot-source-fields h4 small{font-weight:400}.shot-source-fields summary{cursor:pointer;padding:8px 0;min-height:36px}.shot-source-fields dl{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,3fr);gap:8px;margin:0}.shot-source-fields dd{margin:0;white-space:pre-wrap}.shot-source-fields :is(p,h4,dt,dd,small){overflow-wrap:anywhere}.shot-source-fields summary:focus-visible{outline:2px solid var(--accent)}@media(max-width:540px){.shot-source-fields dl{grid-template-columns:minmax(0,1fr)}.shot-source-fields dt{font-weight:600}.shot-source-fields h4 small{display:block}}
</style>
