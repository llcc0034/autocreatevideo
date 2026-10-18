<script setup lang="ts">
import { computed, ref } from 'vue'
import { Plus, Trash2 } from 'lucide-vue-next'
import ExecutionScalarEditor from './ExecutionScalarEditor.vue'
import ExecutionParameterEvidence from './ExecutionParameterEvidence.vue'
import StructuredJsonEditor from './StructuredJsonEditor.vue'
import PromptSourceHelp from './PromptSourceHelp.vue'
import DataStructureVersionSelect from './DataStructureVersionSelect.vue'
import ExecutionMediaSourcePolicyEditor from './ExecutionMediaSourcePolicyEditor.vue'
import type { JsonObject, PromptSourceOption } from '@/services/platformTypes'
import type { ExecutionAuthoringOptions, ExecutionConfiguration, ExecutionContract, ExecutionInput, ExecutionParameter, ExecutionSelectionOptions, ExecutionVersion } from '@/services/executionRegistry'

const props = defineProps<{ modelValue: ExecutionConfiguration; options: ExecutionAuthoringOptions | null; choices: ExecutionSelectionOptions; auxiliaryVersions: ExecutionVersion[]; sources?: PromptSourceOption[]; profileInputs?: JsonObject; inputForms?: ExecutionContract['input_forms']; readonly?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: ExecutionConfiguration] }>()
const selectedTarget = ref('')
const selectedParameter = ref('')
const selectionError = ref('')
const record = (value: unknown): JsonObject => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}
const targets = computed(() => [...Object.values(props.options?.input_targets || {}), ...Object.values(props.profileInputs ?? props.options?.profile_inputs ?? {})].map(value => record(value)))
const targetIdentity = (row: JsonObject) => JSON.stringify([row.target_kind, row.target_key])
const selectedTargetRow = computed(() => targets.value.find(row => targetIdentity(row) === selectedTarget.value))
const derivedKeys = computed(()=>new Set(props.options?.derived_input_keys ?? []))
const isDerived = (input:ExecutionInput)=>derivedKeys.value.has(input.key)&&input.targets.length===1&&input.targets[0]?.target_kind==='profile_input'
const strategyStructures = (input:ExecutionInput)=>input.targets.filter(mapping=>mapping.target_kind==='profile_input').flatMap(mapping=>{
  const structure=record(targets.value.find(target=>target.target_kind==='profile_input'&&target.target_key===mapping.target_key)?.input_structure)
  return typeof structure.id==='string'?[structure]:[]
})
const connectedTargets = computed(()=>new Set(props.modelValue.inputs.flatMap(input=>input.targets.map(targetIdentity))))
const addableTargets = computed(()=>targets.value.filter(row=>!connectedTargets.value.has(targetIdentity(row))
  &&(row.target_kind!=='profile_input'||!derivedKeys.value.has(String(row.target_key)))))
const sourceTitle = (key: unknown) => props.sources?.find(source => source.value === key)?.label
const usageLabel = (usage: unknown) => ({ assembly_source: '直接装配', generation_source: '辅助生成／翻译的主要输入', generation_context: '辅助生成／翻译的额外上下文', reference_units: '参考原语的单元事实', reference_generation_context: '参考单元的生成上下文' })[String(usage)] || `未登记用途：${String(usage)}`
const targetFields = (target: { target_kind: string; target_key: string }) => {
  const row = targets.value.find(row => row.target_kind === target.target_kind && row.target_key === target.target_key)
  return Array.isArray(row?.fields) ? row.fields.map(record) : []
}
const mappedFields = (target: { target_kind: string; target_key: string }) => {
  return [...new Set(targetFields(target).map(field => field.field_key).filter(value => typeof value === 'string'))].join('、')
}
const parameters = computed(() => Object.entries(props.options?.parameters || {}).map(([key, value]) => ({ key, ...record(value), rule: record(record(value).rule) })))
const addableParameters = computed(()=>parameters.value.filter(row=>!props.modelValue.parameters.some(item=>item.key===row.key)
  &&!Object.prototype.hasOwnProperty.call(row.rule,'const')))
const label = (items: { value: string; label: string }[], value: string) => items.find(i => i.value === value)?.label || value
const copy = (): ExecutionConfiguration => JSON.parse(JSON.stringify(props.modelValue))
const updateInput = (index: number, changes: Partial<ExecutionInput>) => { if (props.readonly||isDerived(props.modelValue.inputs[index]!)) return; const next = copy(); next.inputs[index] = { ...next.inputs[index]!, ...changes }; emit('update:modelValue', next) }
const updateSemantic = (index: number, changes: Partial<ExecutionInput['semantic_contract']>) => updateInput(index, { semantic_contract: { ...props.modelValue.inputs[index]!.semantic_contract, ...changes } })
const updateParameter = (index: number, changes: Partial<ExecutionParameter>) => { if (props.readonly) return; const next = copy(); next.parameters[index] = { ...next.parameters[index]!, ...changes }; emit('update:modelValue', next) }
function addInput() {
  if (props.readonly) return
  const row = addableTargets.value.find(t => targetIdentity(t) === selectedTarget.value)
  if (!row || typeof row.target_key !== 'string' || typeof row.target_kind !== 'string' || typeof row.kind !== 'string' || typeof row.required !== 'boolean' || typeof row.min_items !== 'number' || typeof row.max_items !== 'number') { selectionError.value = '目标目录缺少类型或数量声明，不能补猜输入'; return }
  const next = copy()
  if (next.inputs.some(i => i.targets.some(t => t.target_key === row.target_key && t.target_kind === row.target_kind))) { selectionError.value = '此目标已有接线，请修改已有输入'; return }
  const position = next.inputs.length ? Math.max(...next.inputs.map(i => i.position)) + 1 : 0
  next.inputs.push({ key: row.target_key, position, kind: row.kind, required: row.required, min_items: row.min_items, max_items: row.max_items,
    semantic_contract: { schema_version: 1, allowed_purposes: [], allowed_subject_kinds: [], allowed_scope_kinds: [], paired_with_input_key: null, allow_repeated_asset: false },
    targets: [{ target_kind: row.target_kind, target_key: row.target_key, is_primary: row.target_kind !== 'profile_input' }] })
  selectionError.value = ''; selectedTarget.value=''; emit('update:modelValue', next)
}
function addParameter() {
  if (props.readonly) return
  const row = addableParameters.value.find(p => p.key === selectedParameter.value)
  if (!row || typeof row.rule.type !== 'string') { selectionError.value = '参数目录缺少类型'; return }
  const next = copy()
  next.parameters.push({ key: row.key, value_type: row.rule.type, policy: 'overridable',
    minimum: null, maximum: null, enum_values: [], step: null, remainder: null, default_value: null,
    locked_value: null, seed_policy: null })
  selectionError.value = ''; selectedParameter.value=''; emit('update:modelValue', next)
}
function remove(kind: 'inputs' | 'parameters', index: number) {
  if(kind==='inputs'&&isDerived(props.modelValue.inputs[index]!))return
  if (props.readonly || !window.confirm(`移除此项草稿${kind === 'inputs' ? '输入及接线' : '参数覆盖'}？保存前不会更改服务端配置。`)) return
  const next = copy(); next[kind].splice(index, 1); emit('update:modelValue', next)
}
function toggle(list: string[], value: string, enabled: boolean) { return enabled ? [...new Set([...list, value])] : list.filter(v => v !== value) }
function attach(index: number, row: JsonObject, enabled: boolean) {
  const existing = props.modelValue.inputs[index]!.targets
  const filtered = existing.filter(t => targetIdentity(t) !== targetIdentity(row))
  if (enabled) filtered.push({ target_kind: String(row.target_kind), target_key: String(row.target_key), is_primary: false })
  updateInput(index, { targets: filtered })
}
function auxiliary(purpose: string, id: string) {
  if (props.readonly) return
  const next = copy(); next.auxiliaries = next.auxiliaries.filter(a => a.purpose !== purpose)
  if (id) next.auxiliaries.push({ purpose, target_version_id: id })
  emit('update:modelValue', next)
}
const sourceEntry = (key: string) => props.options?.parameters?.[key]
const purposeOptions = (kind: string): { value: string; label: string }[] => (props.options?.input_purposes[kind] as unknown as { value: string; label: string }[] | undefined) || []
</script>

<template>
  <div class="execution-config">
    <p v-if="selectionError" class="platform-inline-error" role="alert">{{ selectionError }}</p>
    <section>
      <h4>素材与字段输入</h4>
      <p v-if="readonly">当前展示的输入、范围与接线暂不可编辑，不会根据当前目录静默改写。如需改变已发布版本，请创建独立后继草稿。</p>
      <p v-else-if="modelValue.prompt_mode==='profile'">策略声明的本次输入、项目梗概、项目风格和已审核影视规则输入由服务端自动派生，无需重复添加。实例只需处理物理槽位及需要明确选择的上下文接线；派生不会读取项目、选择上游结果或调用模型。</p>
      <p v-else>从目标目录选择输入位置，再配置本次请求或业务上下文的来源。这里只声明取值方式，不选择实际素材或调用模型。</p>
      <p v-if="!readonly&&modelValue.operation==='visual_rules.generate'">在策略中选择 request.theme（一句话梗概）和 request.style（视觉风格），保存实例后即生成对应输入。创建任务时可预览复制项目内容或自行填写；确认后冻结本次内容，不附加整份剧本或人物库。</p>
      <p v-if="!readonly&&modelValue.operation==='visual_rules.generate'">若希望直接引用已保存事实，策略选择 project.logline 和 style.visual_prompt；创建准备时预览确认项目来源，不需再建同名接线。</p>
      <div v-if="!readonly" class="execution-config-toolbar">
        <label>从已声明目标添加<select v-model="selectedTarget"><option value="">请选择需要接线的目标</option><option v-for="row in addableTargets" :key="targetIdentity(row)" :value="targetIdentity(row)">{{ sourceTitle(row.target_key) || row.target_key }} · {{ row.target_key }} · {{ row.kind }} · {{ row.min_items }}–{{ row.max_items }}</option></select></label>
        <button type="button" class="button ghost small" :disabled="!addableTargets.some(row=>targetIdentity(row)===selectedTarget)||!options" @click="addInput"><Plus :size="14"/>添加输入</button>
      </div>
      <PromptSourceHelp v-if="selectedTargetRow?.target_kind==='profile_input'" :selector="String(selectedTargetRow.target_key)" :sources="sources" />
      <p v-if="!modelValue.inputs.length">尚无已保存输入。<template v-if="modelValue.prompt_mode==='profile'">保存草稿后会按所选策略生成本次输入；其他来源与物理槽位仍需接线。</template><template v-else>按目标目录配置需要的来源与物理槽位。</template>发布时检查完整性。</p>
      <fieldset v-for="(input,index) in modelValue.inputs" :key="index" :data-authoring-field="`inputs.${index}`" tabindex="-1" :disabled="readonly||isDerived(input)">
        <legend>{{ input.key }} · {{ label(choices.input_kinds,input.kind) }}</legend>
        <p v-if="isDerived(input)">来自所选策略 · 自动派生，只读。类型、数量、必填及来源范围随精确策略确定；修改取值来源请创建策略后继版本。</p>
        <p v-if="isDerived(input)">{{ input.required?'必填':'选填' }} · {{ label(choices.input_kinds,input.kind) }} · 数量 {{ input.min_items }}–{{ input.max_items }} · {{ input.semantic_contract.allowed_scope_kinds.includes('project')?'项目来源或已审核输出，需核对精确来源':'本次请求内容' }}</p>
        <div v-else class="execution-config-grid">
          <label :data-authoring-field="`inputs.${index}.key`">输入标识<input :value="input.key" maxlength="100" required @input="updateInput(index,{key:($event.target as HTMLInputElement).value})"/></label>
          <label :data-authoring-field="`inputs.${index}.position`">顺序<input type="number" min="0" step="1" :value="input.position" required @change="updateInput(index,{position:($event.target as HTMLInputElement).valueAsNumber})"/></label>
          <label :data-authoring-field="`inputs.${index}.min_items`">最少数量<input type="number" min="0" step="1" :value="input.min_items" required @change="updateInput(index,{min_items:($event.target as HTMLInputElement).valueAsNumber})"/></label>
          <label :data-authoring-field="`inputs.${index}.max_items`">最多数量<input type="number" min="0" step="1" :value="input.max_items" required @change="updateInput(index,{max_items:($event.target as HTMLInputElement).valueAsNumber})"/></label>
        </div>
        <label v-if="!isDerived(input)" class="execution-check"><input type="checkbox" :checked="input.required" @change="updateInput(index,{required:($event.target as HTMLInputElement).checked})"/>本次必须提供</label>
        <div v-if="strategyStructures(input).length">
          <strong>输入结构 · 继承所接策略</strong>
          <p v-for="structure in strategyStructures(input)" :key="String(structure.id)">{{ structure.name }} v{{ structure.version }} · {{ structure.id }}</p>
          <p>物理槽位与策略共用这一份结构，保存时由服务端派生；无需在执行实例重复选择。模型目标的独立接口约束仍然生效。</p>
          <p v-if="input.data_structure_version_id&&strategyStructures(input).some(structure=>structure.id!==input.data_structure_version_id)" role="alert">已有输入结构与策略不同，请先解除冲突接线或修改原结构；不会静默覆盖。</p>
        </div>
        <div v-else-if="!isDerived(input)&&['text','json'].includes(input.kind)">
          <strong>通用输入结构（可选）</strong>
          <p>选择已发布的确切结构版本，复用字段含义、表单和缺键默认。未选择时不增加业务结构约束，模型目标原有要求仍生效。文本输入须选择不允许 null 的文本结构。</p>
          <DataStructureVersionSelect :model-value="input.data_structure_version_id || ''" :disabled="readonly" @update:model-value="updateInput(index,{data_structure_version_id:$event || null})" />
          <button v-if="input.data_structure_version_id&&!readonly" type="button" class="btn btn-secondary btn-sm" @click="updateInput(index,{data_structure_version_id:null})">移除结构引用</button>
        </div>
        <ul><li v-for="target in input.targets" :key="targetIdentity(target)">{{ label(choices.target_kinds,target.target_kind) }}：{{ target.target_key }} · {{ target.is_primary?'主执行槽位':target.target_kind==='profile_input'?'策略输入':'复用输入' }}</li></ul>
        <details v-for="target in input.targets.filter(target=>target.target_kind==='profile_input')" :key="`source-${target.target_key}`">
          <summary>查看填写位置与字段对应：{{ sourceTitle(target.target_key) || target.target_key }}</summary>
          <p>创建准备时的输入「{{ inputForms?.[input.key]?.label || input.key }}」（标识：{{ input.key }}） → 策略来源「{{ target.target_key }}」 → 策略字段「{{ mappedFields(target) || '字段关系未读取' }}」。{{ isDerived(input)?'对应关系来自精确策略，不需要再配置。':'以此处明确接线为准。' }}</p>
          <ul><li v-for="(field,fieldIndex) in targetFields(target)" :key="fieldIndex">{{ field.field_key }}：{{ usageLabel(field.usage) }}<template v-if="field.usage!=='assembly_source'">；不是直接作为该字段的最终正文</template></li></ul>
          <PromptSourceHelp :selector="target.target_key" :sources="sources" />
          <p>如果内容来自另一任务，请在正式输入选择器中明确选择支持的输出类型及来源记录；不能把任务 ID 或文件路径粘贴为来源标识，也不会自动选最新结果。</p>
        </details>
        <div v-if="!readonly&&!isDerived(input)" class="execution-config-grid">
          <label v-for="row in targets.filter(t=>t.kind===input.kind&&t.target_kind==='profile_input')" :key="targetIdentity(row)" class="execution-check">
            <input type="checkbox" :checked="input.targets.some(t=>targetIdentity(t)===targetIdentity(row))" @change="attach(index,row,($event.target as HTMLInputElement).checked)"/>同时用于策略：{{ row.target_key }}
          </label>
        </div>
        <template v-if="['image','video','audio'].includes(input.kind)">
          <ExecutionMediaSourcePolicyEditor :input="input" :purposes="purposeOptions(input.kind)" :disabled="readonly||isDerived(input)" @update:policy="updateInput(index,{media_source_policy:$event})"/>
          <details :open="readonly"><summary>素材用途、主体、范围与配对</summary>
            <div v-for="field in (['allowed_subject_kinds','allowed_scope_kinds'] as const)" :key="field">
              <strong>{{ field==='allowed_subject_kinds'?'允许的主体类型':'允许的使用范围' }}</strong>
              <label v-for="item in choices.identity_options[field==='allowed_subject_kinds'?'subject_kinds':'scope_kinds']" :key="item.value" class="execution-check"><input type="checkbox" :checked="input.semantic_contract[field].includes(item.value)" @change="updateSemantic(index,{[field]:toggle(input.semantic_contract[field],item.value,($event.target as HTMLInputElement).checked)})"/>{{ item.label }}</label>
            </div>
            <div v-if="Array.isArray(options?.input_purposes[input.kind])"><strong>允许的用途</strong><label v-for="purpose in purposeOptions(input.kind)" :key="purpose.value" class="execution-check"><input type="checkbox" :checked="input.semantic_contract.allowed_purposes.includes(purpose.value)" @change="updateSemantic(index,{allowed_purposes:toggle(input.semantic_contract.allowed_purposes,purpose.value,($event.target as HTMLInputElement).checked)})"/>{{ purpose.label }}</label></div>
            <p v-else>已声明用途：{{ input.semantic_contract.allowed_purposes.join('、') || '未声明' }}</p>
            <label>配对的逻辑输入<select :value="input.semantic_contract.paired_with_input_key || ''" @change="updateSemantic(index,{paired_with_input_key:($event.target as HTMLSelectElement).value||null})"><option value="">不配对</option><option v-for="other in modelValue.inputs.filter(i=>i.key!==input.key)" :key="other.key" :value="other.key">{{ other.key }} · {{ other.kind }}</option></select></label>
            <label class="execution-check"><input type="checkbox" :checked="input.semantic_contract.allow_repeated_asset" @change="updateSemantic(index,{allow_repeated_asset:($event.target as HTMLInputElement).checked})"/>允许同一素材用于多次独立使用</label>
          </details>
        </template>
        <details v-else-if="!isDerived(input)" :open="readonly">
          <summary>上下文来源与范围</summary>
          <p>允许从哪些真实业务范围读取内容。选中范围不自动选项目、剧本或镜头；调用时仍须提供对应来源和审核版本。仅接受已确认上下文的目标不能用本次手填内容替代。</p>
          <strong>允许的使用范围</strong>
          <label v-for="item in choices.identity_options.scope_kinds" :key="item.value" class="execution-check"><input type="checkbox" :checked="input.semantic_contract.allowed_scope_kinds.includes(item.value)" @change="updateSemantic(index,{allowed_scope_kinds:toggle(input.semantic_contract.allowed_scope_kinds,item.value,($event.target as HTMLInputElement).checked)})"/>{{ item.label }}</label>
        </details>
        <button v-if="!readonly&&!isDerived(input)" type="button" class="button danger tiny" @click="remove('inputs',index)"><Trash2 :size="12"/>移除输入</button>
      </fieldset>
    </section>
    <section>
      <h4>运行参数（可选覆盖）</h4><p>默认继承模型／工作流合同，只添加需要收紧、设置默认值或固定值的参数。已配置项和目标固定参数不再重复添加；不在这里重新定义业务字段。</p>
      <div v-if="!readonly" class="execution-config-toolbar">
        <label>选择需要覆盖的参数<select v-model="selectedParameter"><option value="">请选择未覆盖的可调参数</option><option v-for="row in addableParameters" :key="row.key" :value="row.key">{{ row.key }} · {{ row.rule.type }}</option></select></label>
        <button type="button" class="button ghost small" :disabled="!addableParameters.some(row=>row.key===selectedParameter)||!options" @click="addParameter"><Plus :size="14"/>添加覆盖</button>
      </div>
      <p v-if="!modelValue.parameters.length">没有实例级参数覆盖；请在复核合同中查看最终参数和来源。</p>
      <section v-if="!readonly&&selectedParameter&&sourceEntry(selectedParameter)" aria-label="目标参数约束">
        <p>所选目标的来源约束（尚未合并本表单未保存的覆盖）。保存后请在“复核完整合同”核对最终交集；冲突不能覆盖掉下层规则。</p>
        <ExecutionParameterEvidence :entry="sourceEntry(selectedParameter)"/>
      </section>
      <fieldset v-for="(parameter,index) in modelValue.parameters" :key="parameter.key" :data-authoring-field="`parameters.${index}`" tabindex="-1" :disabled="readonly">
        <legend>{{ parameter.key }} · {{ label(choices.parameter_types,parameter.value_type) }}</legend>
        <details v-if="sourceEntry(parameter.key)"><summary>查看目标来源约束（未合并本表单覆盖）</summary><ExecutionParameterEvidence :entry="sourceEntry(parameter.key)"/></details>
        <div class="execution-config-grid">
          <label>值的使用方式<select :value="parameter.policy" @change="updateParameter(index,{policy:($event.target as HTMLSelectElement).value,default_value:null,locked_value:null})"><option v-for="choice in choices.parameter_policies" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
          <ExecutionScalarEditor :data-authoring-field="`parameters.${index}.locked_value`" v-if="parameter.policy==='locked'" :model-value="parameter.locked_value" :value-type="parameter.value_type" label="固定值" :disabled="readonly" @update:model-value="updateParameter(index,{locked_value:$event})"/>
          <ExecutionScalarEditor :data-authoring-field="`parameters.${index}.default_value`" v-else :model-value="parameter.default_value" :value-type="parameter.value_type" label="默认值" optional :disabled="readonly" @update:model-value="updateParameter(index,{default_value:$event})"/>
          <template v-if="['integer','number'].includes(parameter.value_type)">
            <ExecutionScalarEditor :data-authoring-field="`parameters.${index}.${field}`" v-for="field in (['minimum','maximum','step','remainder'] as const)" :key="field" :model-value="parameter[field]" :value-type="parameter.value_type" :label="({minimum:'最小值',maximum:'最大值',step:'额外步长',remainder:'步长余数'})[field]" optional :disabled="readonly" @update:model-value="updateParameter(index,{[field]:$event})"/>
          </template>
          <label v-if="parameter.value_type==='integer'">种子使用规则<select :value="parameter.seed_policy||''" @change="updateParameter(index,{seed_policy:($event.target as HTMLSelectElement).value||null})"><option value="">不是种子覆盖</option><option v-for="choice in choices.seed_policies" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
        </div>
        <details><summary>限定可选值（{{ parameter.enum_values.length }} 项；空列表表示不额外限制）</summary><StructuredJsonEditor :model-value="parameter.enum_values" label="可选值" :disabled="readonly" @update:model-value="Array.isArray($event)&&updateParameter(index,{enum_values:$event})"/></details>
        <button v-if="!readonly" type="button" class="button danger tiny" @click="remove('parameters',index)"><Trash2 :size="12"/>移除覆盖</button>
      </fieldset>
    </section>
    <section v-if="modelValue.prompt_mode==='profile'||modelValue.auxiliaries.length" data-authoring-field="auxiliaries" tabindex="-1">
      <h4>按需辅助文字生成与翻译</h4><p>这是文本输入准备的一部分。辅助用途来自策略，只能选择对应能力的已发布叶子版本；选择不代表自动批准生成的字段，也不立即调用模型。</p>
      <template v-if="readonly"><p v-for="item in modelValue.auxiliaries" :key="item.purpose">{{ item.purpose }} → {{ item.target_version_id }}</p><p v-if="!modelValue.auxiliaries.length">未配置辅助实例</p></template>
      <template v-else><label v-for="item in options?.auxiliary_purposes" :key="item.purpose">{{ item.purpose }} · {{ item.operation }}<select :value="modelValue.auxiliaries.find(a=>a.purpose===item.purpose)?.target_version_id||''" @change="auxiliary(item.purpose,($event.target as HTMLSelectElement).value)"><option value="">请选择辅助版本</option><option v-for="version in auxiliaryVersions.filter(v=>v.operation===item.operation&&v.status==='published')" :key="version.id" :value="version.id">{{ version.instance_name }} v{{ version.version }}</option></select></label><p v-if="!options?.auxiliary_purposes.length">此方式没有声明辅助用途</p></template>
    </section>
  </div>
</template>

<style scoped>
.execution-config{display:grid;gap:var(--config-space-xl)}.execution-config section{border-top:1px solid var(--border);padding-top:var(--config-space-md)}.execution-config h4{font-size:var(--config-body-size);font-weight:600}.execution-config p{color:var(--text-secondary);font-size:var(--config-help-size);line-height:1.6;overflow-wrap:anywhere}.execution-config fieldset{border:1px solid var(--border);border-radius:var(--config-radius);padding:var(--config-space-md);margin:var(--config-space-md) 0;min-width:0}.execution-config legend{font-size:var(--config-body-size);overflow-wrap:anywhere}.execution-config-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--config-space-md)}.execution-config label{display:grid;gap:var(--config-space-sm);font-size:var(--config-body-size);min-width:0}.execution-config .execution-check{display:flex;align-items:center;gap:var(--config-space-sm);margin:var(--config-space-sm) 0}.execution-check input{width:auto}.execution-config-toolbar{display:flex;align-items:end;gap:var(--config-space-sm);flex-wrap:wrap}.execution-config-toolbar label{flex:1;min-width:180px}.execution-config ul{font-size:var(--config-help-size);padding-left:20px;overflow-wrap:anywhere}.execution-config details{margin:var(--config-space-md) 0}.execution-config details>div{margin:var(--config-space-sm) 0}.execution-config :deep(input),.execution-config select{max-width:100%;min-height:var(--config-control-height)}.execution-config summary{cursor:pointer}.execution-config .platform-inline-error{color:var(--red);font-size:var(--config-body-size)}@media(max-width:640px){.execution-config-grid{grid-template-columns:1fr}}
</style>
