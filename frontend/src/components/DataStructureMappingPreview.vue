<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import DataStructureVersionSelect from './DataStructureVersionSelect.vue'
import RecipeStructureMappingEditor from './RecipeStructureMappingEditor.vue'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import { dataStructures, type StructureMappingOptions, type StructureMappingPreview } from '@/services/dataStructures'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import type { JsonObject } from '@/services/platformTypes'

const sourceId = ref(''), targetId = ref(''), sourceText = ref('')
const sourceSchema = ref<JsonObject | null>(null), targetSchema = ref<JsonObject | null>(null)
const options = ref<StructureMappingOptions | null>(null), mapping = ref<JsonObject>({})
const catalogBusy = ref(false), schemaBusy = ref(false), busy = ref(false)
const catalogError = ref(''), schemaError = ref(''), error = ref(''), notice = ref('')
const draftErrors = ref<string[]>([]), result = ref<StructureMappingPreview | null>(null)
const schemaErrors = ref<Record<string, string[]>>({})
let catalogSerial = 0, schemaSerial = 0, previewSerial = 0
onBeforeUnmount(() => {catalogSerial++; schemaSerial++; previewSerial++})
const ready = computed(() => Boolean(options.value && sourceSchema.value && targetSchema.value && !schemaBusy.value && !catalogBusy.value && !draftErrors.value.length))
function invalidate() {
  previewSerial++; busy.value = false; result.value = null; error.value = ''
}
async function loadOptions() {
  const request = ++catalogSerial
  invalidate(); options.value = null; catalogError.value = ''; catalogBusy.value = true
  try {
    const value = await dataStructures.mappingOptions()
    if (request === catalogSerial) options.value = value
  } catch (cause) {if (request === catalogSerial) catalogError.value = apiErrorSummary(cause, '映射方式目录读取失败')}
  finally {if (request === catalogSerial) catalogBusy.value = false}
}
async function loadSchemas() {
  const request = ++schemaSerial
  invalidate(); sourceSchema.value = null; targetSchema.value = null; schemaError.value = ''; schemaErrors.value = {}
  schemaBusy.value = Boolean(sourceId.value && targetId.value)
  if (!schemaBusy.value) return
  try {
    const [source, target] = await Promise.all([dataStructures.schema(sourceId.value), dataStructures.schema(targetId.value)])
    if (request !== schemaSerial) return
    for (const [label, response] of [['来源', source], ['目标', target]] as const) {
      if (!response.valid || !response.json_schema) {
        schemaError.value = '所选结构暂不能解析，请先修正结构配置。'
        Object.assign(schemaErrors.value, Object.fromEntries(Object.entries(response.errors).map(([path, errors]) => [`${label}.${path}`, errors])))
      }
    }
    if (!schemaError.value) {sourceSchema.value = source.json_schema; targetSchema.value = target.json_schema}
  } catch (cause) {if (request === schemaSerial) schemaError.value = apiErrorSummary(cause, '确切结构读取失败，不能使用空结构预览')}
  finally {if (request === schemaSerial) schemaBusy.value = false}
}
function selectStructure(which: 'source' | 'target', id: string) {
  const selected = which === 'source' ? sourceId : targetId
  if (selected.value === id) return
  if (draftErrors.value.length && !window.confirm('更换结构会丢弃尚未完成的局部输入，是否继续？')) return
  selected.value = id; draftErrors.value = []
  notice.value = Object.keys(mapping.value).length ? '结构已更换；现有映射未自动改写，请重新核对路径与目标字段。' : ''
}
function editMapping(value: JsonObject) { mapping.value = value }
async function preview() {
  if (!ready.value || busy.value) return
  const request = ++previewSerial
  busy.value = true; result.value = null; error.value = ''
  try {
    const response = await dataStructures.mappingPreview(targetId.value, sourceId.value, sourceText.value, {schema_version: options.value!.schema_version, node: mapping.value})
    if (request === previewSerial) result.value = response
  } catch (cause) {if (request === previewSerial) error.value = apiErrorSummary(cause, '预览失败；原文和映射仍保留，未保存配置')}
  finally {if (request === previewSerial) busy.value = false}
}
watch([sourceId, targetId], loadSchemas, {flush: 'sync'})
watch([sourceText, mapping, draftErrors], invalidate, {deep: true, flush: 'sync'})
void loadOptions()
</script>

<template>
  <section class="mapping-preview">
    <p>只读试配：选择确切来源与目标结构，用原始 JSON 验证字段路径和装配结果。草稿也可预览，不代表可用于执行；不保存第二份映射，不创建任务或调用模型。正式映射在配方、实例或策略中维护。</p>
    <div class="structures"><fieldset><legend>来源结构</legend><DataStructureVersionSelect :model-value="sourceId" preview-only @update:model-value="selectStructure('source',$event)" /></fieldset><fieldset><legend>目标结构</legend><DataStructureVersionSelect :model-value="targetId" preview-only @update:model-value="selectStructure('target',$event)" /></fieldset></div>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="catalogBusy || schemaBusy" role="status">正在读取映射方式与确切结构…</p>
    <p v-if="catalogError" role="alert">{{ catalogError }} <button type="button" @click="loadOptions">重读映射目录</button></p>
    <p v-if="schemaError" role="alert">{{ schemaError }} <button type="button" @click="loadSchemas">重读结构</button></p>
    <ul v-if="Object.keys(schemaErrors).length" role="alert"><li v-for="(messages,path) in schemaErrors" :key="path"><code>{{ path }}</code>：{{ messages.join('；') }}</li></ul>
    <label>来源原始 JSON 文本<textarea v-model="sourceText" rows="8" spellcheck="false" placeholder="粘贴待解析的原始 JSON；不会自动填充样例或修复原文" /></label>
    <RecipeStructureMappingEditor v-if="options && sourceSchema && targetSchema" :key="sourceId+':'+targetId" :model-value="mapping" :schema="targetSchema" :default-schemas="[targetSchema]" label="目标内容" :nodes="options.nodes" :preview-context="{schema:sourceSchema,scopes:options.scopes}" :fields="[]" :outputs="[]" :scopes="[]" :prior-stages="[]" @update:model-value="editMapping" @draft-validity="draftErrors=$event" />
    <ul v-if="draftErrors.length" role="alert"><li v-for="message in draftErrors" :key="message">{{ message }}</li></ul>
    <ReadonlyDataValue :value="{schema_version:options?.schema_version,node:mapping}" label="当前预览映射（不保存）" />
    <button type="button" class="button primary" :disabled="!ready || busy" @click="preview">{{ busy ? '正在预览…' : '解析并预览映射' }}</button>
    <p v-if="!sourceId || !targetId">请先选择来源和目标结构。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <section v-if="result" class="report" aria-label="映射预览结果">
      <h3>{{ result.valid ? '预览通过（未保存、未执行）' : '预览未通过' }}</h3>
      <p>来源：{{ result.source_structure_version_id }} → 目标：{{ result.target_structure_version_id }}</p>
      <ul v-if="Object.keys(result.errors).length" role="alert"><li v-for="(messages,path) in result.errors" :key="path"><code>{{ path }}</code>：{{ messages.join('；') }}</li></ul>
      <ul v-if="result.warnings.length"><li v-for="(warning,index) in result.warnings" :key="index"><code>{{ warning.path }}</code>：{{ warning.message }}</li></ul>
      <template v-if="result.parsed"><h4>原始解析值</h4><ReadonlyDataValue :value="result.raw_value" label="原始解析值" /><h4>来源结构解析值</h4><ReadonlyDataValue :value="result.source_value" label="来源结构解析值" /><h4>目标结果（未通过时不作为有效输入）</h4><ReadonlyDataValue :value="result.value" label="目标结果" /></template>
      <p v-else>JSON 尚未解析成功；上方原文保留，行列位置见错误信息。</p>
      <ReadonlyDataValue :value="result.defaults" label="默认值及确切版本来源" />
      <ReadonlyDataValue :value="result.provenance" label="字段取值路径及来源" />
    </section>
  </section>
</template>

<style scoped>
.mapping-preview{display:grid;gap:16px;min-width:0}.mapping-preview p,.mapping-preview h3,.mapping-preview h4{margin:0;line-height:1.6;overflow-wrap:anywhere}.structures{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.structures fieldset{min-width:0;border:1px solid var(--border,#354350);padding:12px;border-radius:8px}.mapping-preview label{display:grid;gap:6px}.mapping-preview textarea{box-sizing:border-box;width:100%;min-width:0;resize:vertical;background:var(--bg-input,#0c1015);color:inherit;border:1px solid var(--border,#354350);padding:10px;font:inherit}.mapping-preview .report{display:grid;gap:12px;border-top:1px solid var(--border,#354350);padding-top:16px}.mapping-preview li{overflow-wrap:anywhere}.mapping-preview [role=alert]{color:var(--color-danger,#ed9898)}.mapping-preview button{min-height:36px}.mapping-preview textarea:focus-visible{outline:2px solid var(--accent,#e4ad60);outline-offset:2px}@media(max-width:700px){.structures{grid-template-columns:1fr}}
</style>
