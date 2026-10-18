<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { JsonObject } from '@/services/platformTypes'
import { jsonValueTypes, jsonKind, jsonAllowedTypes, jsonChoices, emptyJsonInput, jsonChildSchemas, jsonPropertyOptions, jsonPropertyAllowed, jsonFieldLabel, jsonInputErrors, jsonRules, jsonDefinition, jsonValuePath } from '@/services/structuredInputForm'
import StructureMediaValue from './StructureMediaValue.vue'

// JSON syntax is not a model policy. This editor never evaluates expressions
// or guesses Provider-specific fields; the owning API validates their meaning.
const props = defineProps<{ modelValue: unknown; label: string; disabled?: boolean; readonly?: boolean; fixedObject?: boolean; schemas?: JsonObject[]; defaultSchemas?: JsonObject[]; hideLabel?: boolean; path?: string; fieldErrors?: Record<string,string[]>; selectMedia?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: unknown]; validity: [errors: string[]]; draftValidity: [errors: string[]] }>()
const schemas = computed(() => [...(props.schemas || []), ...(props.fixedObject ? [{type:'object'}] : [])])
const types = computed(() => jsonAllowedTypes(schemas.value))
const kind = computed(() => !props.readonly && types.value.length === 1 ? types.value[0]!.value : jsonKind(props.modelValue))
const rules = computed(() => jsonRules(schemas.value, kind.value))
const definition = computed(() => jsonDefinition(rules.value))
const descriptions = computed(() => [...new Set([definition.value.description, ...rules.value.map(rule => rule.description)].filter((value): value is string => typeof value === 'string' && !!value.trim()))])
const mapped = computed(() => Boolean(definition.value.field_definition_version_id))
const media = computed(() => definition.value.value_type === 'media' || rules.value.some(schema => schema['x-media-types']))
const blocked = computed(() => props.disabled || props.readonly)
const valuePath = computed(() => props.path || 'value')
const childPath = (key: string) => jsonValuePath(valuePath.value,key,kind.value==='array')
const localErrors = computed(() => props.fieldErrors?.[valuePath.value] || [])
const missing = computed(() => props.modelValue === undefined)
const defaultRule = computed(() => schemas.value.find(schema=>Object.prototype.hasOwnProperty.call(schema,'default')))
const entries = computed(() => kind.value === 'object' && jsonKind(props.modelValue)==='object' ? Object.entries(props.modelValue as Record<string,unknown>) : kind.value === 'array' && Array.isArray(props.modelValue) ? props.modelValue.map((value,index)=>[String(index),value] as const) : [])
const choices = computed(() => jsonChoices(rules.value))
const integer = computed(() => rules.value.some(schema=>schema.type==='integer'))
const minimum = computed(() => {const values=rules.value.map(row=>row.minimum).filter((v): v is number=>typeof v==='number');return values.length?Math.max(...values):undefined})
const maximum = computed(() => {const values=rules.value.map(row=>row.maximum).filter((v): v is number=>typeof v==='number');return values.length?Math.min(...values):undefined})
const properties = computed(() => jsonPropertyOptions(schemas.value).filter(key=>!entries.value.some(([name])=>name===key)))
const closedObject = computed(() => mapped.value || rules.value.some(schema=>schema.additionalProperties===false))
const childSchemas = (key:string) => jsonChildSchemas(schemas.value,key,kind.value==='array')
const childLabel = (key:string) => {
  if (kind.value==='array') return `第 ${Number(key)+1} 项`
  const label=jsonFieldLabel(childSchemas(key),key)
  const duplicate=jsonPropertyOptions(schemas.value).some(other=>other!==key && jsonFieldLabel(childSchemas(other),other)===label)
  return duplicate ? `${label} · ${key}` : label
}
const keyDraft = ref('')
const ownError = ref('')
const numberDraft = ref<string|null>(null)
const numberInput = computed(() => numberDraft.value ?? (typeof props.modelValue==='number' ? props.modelValue : ''))
watch(() => props.modelValue, () => {
  if (numberDraft.value !== null) { numberDraft.value=null; ownError.value='' }
})
const childrenErrors = ref<Record<string,string[]>>({})
const childrenDraftErrors = ref<Record<string,string[]>>({})
const draftErrors = computed(() => [ownError.value,...entries.value.flatMap(([key])=>childrenDraftErrors.value[key] || [])].filter(Boolean))
const schemaErrors = computed(() => missing.value ? [] : jsonInputErrors(props.modelValue,schemas.value,props.label,props.defaultSchemas))
const errors = computed(() => [...new Set([ownError.value, ...schemaErrors.value, ...(!types.value.length ? ['输入要求的类型冲突，请检查实例配置'] : []), ...(!jsonValueTypes.some(row=>row.value===jsonKind(props.modelValue)) ? [`${props.label}的数据类型不受支持`] : []), ...entries.value.flatMap(([key])=>childrenErrors.value[key] || [])].filter(Boolean))])
// Parents may project a fresh schema object while handling validity. Emit only
// changed error content; referentially new but equal arrays are not new facts.
watch(() => JSON.stringify(errors.value), value=>emit('validity',JSON.parse(value)),{immediate:true})
watch(() => JSON.stringify(draftErrors.value), value=>emit('draftValidity',JSON.parse(value)),{immediate:true})
function commit(value: unknown) { if (!blocked.value) { numberDraft.value=null; ownError.value=''; emit('update:modelValue',value) } }
function child(key:string,value:unknown) {
  if (blocked.value) return
  commit(kind.value === 'array' ? entries.value.map(([item,current])=>item===key?value:current) : Object.fromEntries(entries.value.map(([item,current])=>[item,item===key?value:current])))
}
function remove(key:string) {
  if (blocked.value) return
  childrenErrors.value={}
  const next=entries.value.filter(([item])=>item!==key)
  commit(kind.value==='array' ? next.map(([,value])=>value) : Object.fromEntries(next))
}
function add() {
  if (blocked.value) return
  if (kind.value === 'array') { commit([...entries.value.map(([,value])=>value),emptyJsonInput(childSchemas(String(entries.value.length)))]); return }
  const key=keyDraft.value.trim()
  if (!key || entries.value.some(([name])=>name===key)) { ownError.value='属性名不能为空或与已有属性重复'; return }
  if (!jsonPropertyAllowed(schemas.value,key)) { ownError.value='输入要求不允许添加此字段'; return }
  if (mapped.value && !properties.value.includes(key)) { ownError.value='请先在结构中定义此字段；已有未映射内容仍保留'; return }
  commit(Object.fromEntries([...entries.value,[key,emptyJsonInput(childSchemas(key))]]))
  keyDraft.value=''
}
function number(raw:string) {
  if (blocked.value) return
  const value=Number(raw)
  if (!raw.trim() || !Number.isFinite(value)) { numberDraft.value=raw; ownError.value=`${props.label}需要有效数字`; return }
  commit(value)
}
function changeType(next:string) {
  if (blocked.value || props.fixedObject || types.value.length===1 || next===kind.value || !types.value.some(row=>row.value===next)) return
  if ((entries.value.length || !['object','array','null'].includes(jsonKind(props.modelValue)) && props.modelValue!=='') && !window.confirm('切换类型会清除此项草稿的现有内容，是否继续？')) return
  childrenErrors.value={}
  commit(next==='string'?'':next==='number'?0:next==='boolean'?false:next==='array'?[]:next==='object'?{}:null)
}
function fill() { commit(emptyJsonInput(schemas.value)) }
const readText = computed(() => missing.value ? '未提供（缺失）' : props.modelValue === null ? '明确空值（null）'
  : props.modelValue === '' ? '空字符串' : typeof props.modelValue === 'boolean' ? props.modelValue ? '是（true）' : '否（false）' : String(props.modelValue))
</script>
<template>
  <div class="structured-json-editor" :data-value-path="valuePath">
    <template v-if="readonly">
      <strong v-if="!hideLabel">{{ label }}</strong>
      <template v-if="media && modelValue !== null && !missing"><StructureMediaValue :model-value="modelValue" :schemas="rules" readonly :label="label" /></template>
      <template v-else-if="entries.length">
        <div v-for="([key,value]) in entries" :key="key" class="child-value readonly-value">
          <StructuredJsonEditor :model-value="value" :label="childLabel(key)" :schemas="childSchemas(key)" readonly :path="childPath(key)" :field-errors="fieldErrors" />
          <small v-if="mapped && kind==='object' && !jsonPropertyOptions(schemas).includes(key)">未映射字段 · 原值保留</small>
        </div>
      </template>
      <p v-else class="value-text">{{ Array.isArray(modelValue)?'空列表':jsonKind(modelValue)==='object'?'空对象':readText }}</p>
    </template>
    <template v-else>
    <details v-if="!missing && jsonKind(modelValue)!==kind" class="format-help"><summary>原值类型不符合字段要求（原值保留）</summary><pre>{{ JSON.stringify(modelValue,null,2) }}</pre></details>
    <div v-if="!hideLabel || types.length>1 && (choices===null || types.some(type=>type.value==='null'))" class="value-header"><strong v-if="!hideLabel">{{ label }}</strong><label v-if="types.length>1 && (choices===null || types.some(type=>type.value==='null'))" class="field type-picker">内容格式<select :aria-label="`${label}类型`" :value="kind" :disabled="disabled" @change="changeType(($event.target as HTMLSelectElement).value)"><option v-for="type in types" :key="type.value" :value="type.value">{{ type.label }}</option></select></label></div>
    <details v-if="types.length>1 && choices===null" class="format-help"><summary>这些内容格式有什么区别？</summary><p>这里只决定内容如何组织，不决定制作步骤或模型。请按输入要求选择。</p><ul><li v-for="type in types" :key="type.value"><strong>{{ type.label }}</strong>：{{ type.help }}</li></ul></details>
    <p v-if="missing">未提供（缺失） <button type="button" class="button ghost small" :disabled="disabled" @click="fill">填写此项</button></p>
    <StructureMediaValue v-else-if="media && modelValue !== null" :model-value="modelValue" :schemas="rules" :label="label" :disabled="disabled" :selectable="selectMedia" @update:model-value="commit" />
    <label v-else-if="choices!==null" class="field">选择内容<select :aria-label="label" :value="choices.findIndex(value=>JSON.stringify(value)===JSON.stringify(modelValue))" :disabled="disabled" @change="commit(choices![Number(($event.target as HTMLSelectElement).value)])"><option value="-1" disabled>请选择</option><option v-for="(value,index) in choices" :key="index" :value="index">{{ JSON.stringify(value) }}</option></select></label>
    <label v-else-if="kind==='string'" class="field"><textarea rows="3" :aria-label="label" :value="typeof modelValue==='string'?modelValue:''" :disabled="disabled" @input="commit(($event.target as HTMLTextAreaElement).value)"/></label>
    <label v-else-if="kind==='number'" class="field"><input type="number" :step="integer?1:'any'" :min="minimum" :max="maximum" :aria-label="label" :value="numberInput" :placeholder="integer?'请填写整数':'请填写数字'" :disabled="disabled" @input="number(($event.target as HTMLInputElement).value)"/><span>{{ integer?'整数':'数字' }}<template v-if="minimum!==undefined"> · 最小 {{ minimum }}</template><template v-if="maximum!==undefined"> · 最大 {{ maximum }}</template></span></label>
    <label v-else-if="kind==='boolean'" class="field"><select :aria-label="label" :value="typeof modelValue==='boolean'?String(modelValue):''" :disabled="disabled" @change="commit(($event.target as HTMLSelectElement).value==='true')"><option value="" disabled>请选择</option><option value="true">是（启用）</option><option value="false">否（不启用）</option></select></label>
    <span v-else-if="kind==='null'">明确传空值（null），不等于省略此项。</span>
    <template v-else-if="kind==='object'||kind==='array'">
      <div v-for="([key,value]) in entries" :key="key" class="child-value">
        <StructuredJsonEditor :model-value="value" :label="childLabel(key)" :schemas="childSchemas(key)" :default-schemas="jsonChildSchemas(defaultSchemas || [],key,kind==='array')" :disabled="disabled || (mapped && kind==='object' && !jsonPropertyOptions(schemas).includes(key))" :path="childPath(key)" :field-errors="fieldErrors" :select-media="selectMedia" @update:model-value="child(key,$event)" @validity="childrenErrors[key]=$event" @draft-validity="childrenDraftErrors[key]=$event"/>
        <button type="button" class="button ghost small" :aria-label="`删除${key}`" :disabled="disabled" @click="remove(key)">删除</button>
        <small v-if="mapped && kind==='object' && !jsonPropertyOptions(schemas).includes(key)">未映射字段 · 原值保留，不作为已定义字段编辑</small>
      </div>
      <p>{{ kind==='object'?'逐项填写带名称的字段。':'按顺序添加内容，每项可以单独编辑。' }}<template v-if="!entries.length">当前为空；提供此输入时会提交空{{ kind==='object'?'对象':'列表' }}，不代表省略。</template></p>
      <div class="value-header" v-if="!disabled"><label v-if="kind==='object'" class="field property-picker"><span>添加哪个字段</span><select v-if="closedObject" v-model="keyDraft" :aria-label="`${label}新属性名`"><option value="">请选择字段</option><option v-for="key in properties" :key="key" :value="key">{{ childLabel(key) }}{{ schemas.some(row=>Array.isArray(row.required)&&row.required.includes(key))?'（必填）':'' }}</option></select><input v-else v-model="keyDraft" :aria-label="`${label}新属性名`" placeholder="按输入要求填写字段标识" @keydown.enter.prevent="add"/></label><button type="button" class="button ghost small" :disabled="kind==='object' && closedObject && !properties.length" @click="add">{{ kind==='object'?'添加字段':'添加列表项' }}</button></div>
    </template>
    </template>
    <div v-if="mapped && kind==='object' && jsonKind(modelValue)==='object'" class="missing-fields">
      <div v-for="key in properties" :key="key" :data-value-path="childPath(key)">
        <span>{{ childLabel(key) }}：未提供{{ rules.some(row=>Array.isArray(row.required)&&row.required.includes(key))?'（必填）':'（可选）' }}</span>
        <small v-if="childSchemas(key).some(row=>Object.prototype.hasOwnProperty.call(row,'default'))"> · 配置了缺键默认值，预览后可查看来源</small>
        <p v-for="error in fieldErrors?.[childPath(key)] || []" :key="error" role="alert">{{ error }}</p>
      </div>
    </div>
    <small v-if="definition.unit">单位：{{ definition.unit }}</small>
    <details v-if="descriptions.length || defaultRule" class="format-help"><summary>字段说明与缺失规则</summary><p v-for="description in descriptions" :key="description">{{ description }}</p><p v-if="defaultRule">仅缺键时使用配置默认值：{{ JSON.stringify(defaultRule.default) }}；此表单不自动写入默认值。</p></details>
    <p v-for="error in localErrors" :key="error" role="alert">{{ error }}</p>
    <p v-if="ownError" role="alert">{{ ownError }}</p>
    <p v-for="error in schemaErrors" :key="error" role="alert">{{ error }}</p>
    <p v-if="!types.length" role="alert">输入要求的类型冲突，请检查实例配置；原值未被覆盖。</p>
  </div>
</template>
<style scoped>
.structured-json-editor{display:grid;gap:10px;min-width:0}.value-header{display:flex;align-items:end;flex-wrap:wrap;gap:12px}.value-header strong{font-size:13px;overflow-wrap:anywhere}.child-value{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:start;gap:8px;padding:12px 0 12px 12px;border-left:1px solid var(--border-color,#354350)}input,select,textarea{min-height:40px;max-width:100%;box-sizing:border-box;min-width:0}textarea{resize:vertical}.type-picker{min-width:190px}.property-picker{flex:1;min-width:160px}p,span,label,.format-help{font-size:12px;color:var(--text-secondary,#96a5b3)}p{margin:0}.format-help{line-height:1.8}.format-help summary{cursor:pointer}.format-help ul{padding-left:20px}[role=alert]{color:var(--warning,#e9aa52)}
.readonly-value{grid-template-columns:minmax(0,1fr)}.value-text{white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text-primary)}.missing-fields{display:grid;gap:8px}.structured-json-editor :is(button,input,textarea,select,summary):focus-visible{outline:2px solid var(--accent-color,#e4ad60);outline-offset:2px}@media(max-width:600px){.child-value{padding-left:8px}.type-picker,.property-picker{min-width:0;width:100%}}
</style>
