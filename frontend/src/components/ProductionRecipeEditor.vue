<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useSlots, watch } from 'vue'
import type { AuthoringIssue } from '@/services/configurationAuthoring'
import ProductionRecipeRouteEditor from './ProductionRecipeRouteEditor.vue'
import RecipeShotReferencePolicy from './RecipeShotReferencePolicy.vue'
import RecipeAssemblyOutputEditor from './RecipeAssemblyOutputEditor.vue'
import BaseModal from './BaseModal.vue'
import ConfigurationAuthoringShell from './ConfigurationAuthoringShell.vue'
import ProductionRecipeDefinitionView from './ProductionRecipeDefinitionView.vue'
import { recipeMaterialSource, withRecipeMaterialSource, recipeMaterialSelectionPolicy, withRecipeMaterialSelectionPolicy } from '@/services/productionRecipeAuthoring'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import { executionRequestFormContract } from '@/services/executionRequestForm'
import { moveRecipeStage, recipeRoutingGroups, type RecipeRoutingKey, recipeStageKind, recipeStageOption, recipeStageWithKind, recipeAuthoringErrors, recipeSourceCatalogDefinition, recipeObject as obj, recipeRows as rows, recipeRouteForContract } from '@/services/productionRecipeAuthoring'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { modeLabel, stageLabel } from '@/services/presentationLabels'
import type { ExecutionVersion } from '@/services/executionRegistry'
import type { JsonObject, ProductionRecipeEditorOptions } from '@/services/platformTypes'

const props = defineProps<{ modelValue: JsonObject; options?: ProductionRecipeEditorOptions | null; disabled?: boolean; authorStatus?: string; authorIssues?: AuthoringIssue[] }>()
const slots = useSlots()
const emit = defineEmits<{ 'update:modelValue': [JsonObject]; validity: [string[]] }>()
const stages = computed(() => rows(props.modelValue.stages))
const versions = ref<ExecutionVersion[]>([]), page = ref(1), next = ref(false), search = ref(''), loading = ref(false), error = ref('')
const routeErrors = ref<Record<string,string[]>>({})
const instancePicker = ref<{index:number; key:RecipeRoutingKey; routeIndex:number|null; definition:string; options:string; label:string}|null>(null)
const pendingSingleInstance = ref<{index:number; key:RecipeRoutingKey; definition:string; options:string; keep:number|null}|null>(null)
const assemblyErrors = ref<string[]>([])
const timingErrors = ref<string[]>([])
const mediaErrors = ref<string[]>([])
const deliveryErrors = ref<string[]>([])
function setAssemblyOutput(index:number, output:JsonObject|undefined, key='assembly_output') {
  if(props.disabled) return
  const value=stages.value.map(row=>({...row}))
  if(output===undefined) delete value[index]![key]
  else value[index]![key]=output
  saveStages(value)
}
const routing = (stage: JsonObject, key: RecipeRoutingKey = 'routing') => obj(stage[key])
const allRoutings = (stage: JsonObject) => recipeRoutingGroups(stage).map(group=>routing(stage,group.key))
const updateRouting = (index:number,key:RecipeRoutingKey,value:JsonObject) => updateStage(index,{[key]:value})
const stageOption = (stage: JsonObject) => recipeStageOption(stage, props.options)
const pendingMaterialSource = ref<{index:number; role:'primary'|'variant'; source:string; stage:string}|null>(null)
const pendingStageEdit = ref<{definition:string; options:string; title:string; label:string; message:string; stages:JsonObject[]}|null>(null)
function requestStageEdit(index:number, value:JsonObject[], title:string, message:string) {
  const stage=stages.value[index]
  if(props.disabled || !props.options || !stage) return
  pendingStageEdit.value={definition:JSON.stringify(props.modelValue),options:JSON.stringify(props.options),title,
    label:`${index+1} · ${stage.name || stageLabel(stage.key) || stage.key}`,message,
    stages:JSON.parse(JSON.stringify(value))}
}
function confirmStageEdit() {
  const pending=pendingStageEdit.value
  pendingStageEdit.value=null
  if(!pending || props.disabled || JSON.stringify(props.modelValue)!==pending.definition || JSON.stringify(props.options)!==pending.options) return
  saveStages(pending.stages)
}
function requestMaterialSource(index:number, role:'primary'|'variant', event:Event) {
  const input=event.target as HTMLSelectElement, source=input.value
  // Keep the displayed value truthful while confirmation is pending/cancelled.
  input.value=recipeMaterialSource(stages.value[index]!,role)
  setMaterialSource(index,role,source)
}
function setMaterialSource(index:number, role:'primary'|'variant', source:string) {
  if (props.disabled || !props.options) return
  const stage=stages.value[index]!
  if (recipeMaterialSource(stage,role)===source) return
  if (source!=='generate' && stage[role==='primary'?'routing':'variant_routing'] !== undefined) {
    pendingMaterialSource.value={index,role,source,stage:JSON.stringify(stage)}
    return
  }
  applyMaterialSource(index,role,source)
}
function confirmMaterialSource() {
  const pending=pendingMaterialSource.value
  pendingMaterialSource.value=null
  if (!pending || props.disabled || JSON.stringify(stages.value[pending.index])!==pending.stage) return
  applyMaterialSource(pending.index,pending.role,pending.source)
}
function applyMaterialSource(index:number, role:'primary'|'variant', source:string) {
  if (props.disabled || !props.options) return
  const value=[...stages.value]
  value[index]=withRecipeMaterialSource(stages.value[index]!,role,source,props.options)
  saveStages(value)
}
function setMaterialSelection(index:number, role:'primary'|'variant', policy:string) {
  if (props.disabled || !props.options) return
  saveStages(stages.value.map((stage, at) => at === index
    ? withRecipeMaterialSelectionPolicy(stage, role, policy, props.options!) : stage))
}
watch([()=>props.disabled,()=>JSON.stringify(props.modelValue)],()=>{pendingMaterialSource.value=null})
watch([()=>props.disabled,()=>JSON.stringify(props.modelValue),()=>JSON.stringify(props.options)],()=>{pendingStageEdit.value=null})
const sourceOutputs = ref<ProductionRecipeEditorOptions['textOutputOptions']>([])
const sourceLoading = ref(false), sourceError = ref('')
let sourceSequence = 0
const sourceSignature = computed(()=>JSON.stringify(recipeSourceCatalogDefinition(props.modelValue)))
const needsSourceCatalog = computed(()=>stages.value.some(stage=>stageOption(stage)?.repeatable && rows(routing(stage).routes).length))
async function loadSourceOptions() {
  const request = ++sourceSequence
  sourceOutputs.value=[]; sourceError.value=''; sourceLoading.value=false
  if (!needsSourceCatalog.value || !props.options) return
  sourceLoading.value=true
  try {
    const outputs=await platformApi.getRecipeSourceOptions(JSON.parse(sourceSignature.value))
    if(request===sourceSequence)sourceOutputs.value=outputs
  } catch(e) {if(request===sourceSequence)sourceError.value=apiErrorSummary(e)}
  finally {if(request===sourceSequence)sourceLoading.value=false}
}
watch([sourceSignature,needsSourceCatalog],()=>{void loadSourceOptions()}, {immediate:true})
onBeforeUnmount(()=>{sourceSequence++;closeInstancePicker()})
function routeOptions(index:number): ProductionRecipeEditorOptions {
  const priorKeys=stages.value.slice(0,index).map(stage=>stage.key)
  return {...props.options!,textOutputOptions:(needsSourceCatalog.value?sourceOutputs.value:props.options!.textOutputOptions)
    .filter(row=>priorKeys.includes(row.stageKey))}
}
function changeKind(index: number, kind: string) {
  if (!props.options || props.disabled) return
  const stage = stages.value[index]
  if(!stage || recipeStageKind(stage)===kind) return
  const value=stages.value.map((row, i) => i === index ? recipeStageWithKind(stage, kind, props.options!, () => `step-${crypto.randomUUID()}`) : row)
  if (stage.routing || stage.variant_routing || stage.assembly_output || stage.assembly_timing || stage.assembly_media || stage.delivery_metadata) {
    requestStageEdit(index,value,'确认更换阶段职责','更换此步骤职责会移除它的执行实例与字段配置；其他步骤的引用不会自动改写。')
  } else saveStages(value)
}
function requestKind(index:number,event:Event) {
  const input=event.target as HTMLSelectElement, kind=input.value
  input.value=recipeStageKind(stages.value[index]!)
  changeKind(index,kind)
}
function requestMode(index:number,event:Event) {
  const input=event.target as HTMLSelectElement, mode=input.value
  input.value=String(stages.value[index]!.mode)
  updateStage(index,{mode})
}
function moveStage(index: number, direction: number) {
  if (!props.options || props.disabled) return
  const value = moveRecipeStage(stages.value, index, direction, props.options)
  if (value) saveStages(value)
}
const canMove = (index: number, direction: number) => Boolean(props.options && moveRecipeStage(stages.value, index, direction, props.options))
function removeStage(index: number) {
  requestStageEdit(index,stages.value.filter((_, i) => i !== index),'确认移除阶段','移除此步骤及其执行实例配置；其他步骤的引用不会自动删除，请随后核对。')
}
const variantLimit = computed(()=>props.options?.effectiveLimits.maxVariants)
const routeLimit = computed(()=>props.options?.effectiveLimits.maxRoutingRoutes)
let sequence = 0
async function loadVersions(target = 1) {
  if (!instancePicker.value || props.disabled || loading.value) return
  const request = ++sequence; loading.value = true; error.value = ''
  versions.value=[]
  try { const result = await executionRegistryApi.versions(target, {status:'published',search:search.value}); if (request !== sequence) return
    versions.value = result.results; page.value = target; next.value = result.next !== null
  } catch (e) { if (request === sequence) error.value = apiErrorSummary(e) }
  finally { if (request === sequence) loading.value = false }
}
function closeInstancePicker() { sequence++; instancePicker.value=null; loading.value=false; error.value=''; versions.value=[] }
function openInstancePicker(index:number,key:RecipeRoutingKey,routeIndex:number|null=null) {
  if(props.disabled || !props.options || loading.value) return
  const stage=stages.value[index]
  if(!stage || !recipeRoutingGroups(stage).some(group=>group.key===key)) return
  instancePicker.value={index,key,routeIndex,definition:JSON.stringify(props.modelValue),options:JSON.stringify(props.options),
    label:`${index+1} · ${stage.name || stageLabel(stage.key) || stage.key} · ${recipeRoutingGroups(stage).find(group=>group.key===key)?.label || '执行'}`}
  search.value='';page.value=1;next.value=false
  void loadVersions()
}
watch([()=>props.disabled,()=>JSON.stringify(props.modelValue),()=>JSON.stringify(props.options)],()=>{
  const pending=instancePicker.value
  if(pending && (props.disabled || pending.definition!==JSON.stringify(props.modelValue) || pending.options!==JSON.stringify(props.options))) closeInstancePicker()
  const single=pendingSingleInstance.value
  if(single && (props.disabled || single.definition!==JSON.stringify(props.modelValue) || single.options!==JSON.stringify(props.options))) pendingSingleInstance.value=null
})
const limitErrors = computed(() => {
  if (props.disabled || !stages.value.some(stage=>stage.routing!==undefined||stage.variant_routing!==undefined)) return []
  if (!Number.isSafeInteger(variantLimit.value) || Number(variantLimit.value)<1 || !Number.isSafeInteger(routeLimit.value) || Number(routeLimit.value)<1) return ['配方目录尚未提供有效的 effective_limits.max_variants / max_routing_routes，不能保存已配置实例']
  return [
    ...(stages.value.some(stage=>allRoutings(stage).some(row=>Number(row.variant_count)>Number(variantLimit.value))) ? [`候选数不能超过服务端上限 ${variantLimit.value}`] : []),
    ...(stages.value.some(stage=>allRoutings(stage).some(row=>rows(row.routes).length>Number(routeLimit.value))) ? [`备选实例数不能超过服务端上限 ${routeLimit.value}`] : []),
  ]
})
const validate = computed(() => [...recipeAuthoringErrors(props.modelValue,props.options), ...limitErrors.value, ...Object.values(routeErrors.value).flat(), ...(stages.value.some(row=>row.key==='assembly'&&row.mode==='existing_pipeline')?[...assemblyErrors.value,...timingErrors.value,...mediaErrors.value]:[]), ...(stages.value.some(row=>row.key==='delivery'&&row.mode==='existing_pipeline')?deliveryErrors.value:[]), ...(sourceLoading.value?['正在读取配方上游输出目录']:[]), ...(sourceError.value?[sourceError.value]:[])])
watch(validate, value => emit('validity',value), {immediate:true})
const saveStages = (value: JsonObject[]) => { emit('update:modelValue',{...props.modelValue,stages:value}) }
function updateStage(index: number, changes: JsonObject) {
  if(props.disabled || !stages.value[index]) return
  const value = stages.value.map(row => ({...row})), stage = {...value[index],...changes}
  const removed:string[]=[]
  if(stage.delivery_metadata!==undefined&&(stage.key!=='delivery'||stage.mode!=='existing_pipeline')) {
    removed.push('交付标题和说明映射及原文')
    delete stage.delivery_metadata
  }
  if((stage.assembly_output !== undefined || stage.assembly_timing !== undefined || stage.assembly_media !== undefined) && (stage.key!=='assembly'||stage.mode!=='existing_pipeline')) {
    removed.push('装配输出、时序和片段核对映射及原文')
    delete stage.assembly_output
    delete stage.assembly_timing
    delete stage.assembly_media
  }
  if ('key' in changes || 'mode' in changes) {
    const option = stageOption(stage)
    if (!option?.modes.includes(String(stage.mode))) stage.mode = ''
    if (!option?.routingModes.includes(String(stage.mode))) { delete stage.routing; delete stage.variant_routing }
    if (recipeStageKind(stage)!=='bible_image_generation' || stage.mode!=='existing_pipeline') {
      delete stage.material_sources; delete stage.material_selection
    }
  }
  if ('approval' in changes && stage.approval !== 'policy') delete stage.decision_policy
  value[index]=stage
  if(removed.length) requestStageEdit(index,value,'确认更换阶段执行方式',`更换执行方式会移除${removed.join('、')}；其他步骤的引用不会自动改写。`)
  else saveStages(value)
}
const routeErrorKey = (stage:JsonObject,key:RecipeRoutingKey,index:number,route:JsonObject) => `${stage.key}:${key}:${index}:${route.execution_instance_version_id}`
watch(stages, value=>{
  const keys=new Set(value.flatMap(stage=>recipeRoutingGroups(stage).flatMap(group=>rows(routing(stage,group.key).routes).map((route,index)=>routeErrorKey(stage,group.key,index,route)))))
  routeErrors.value=Object.fromEntries(Object.entries(routeErrors.value).filter(([key])=>keys.has(key)))
})
async function selectInstance(id:string) {
  const pending=instancePicker.value
  if (!pending || props.disabled || !props.options || loading.value || !versions.value.some(row=>row.id===id)) return
  const request=++sequence; loading.value=true; error.value=''
  try {
    const contract=executionRequestFormContract(await executionRegistryApi.contract(id))
    if(request!==sequence || instancePicker.value!==pending || props.disabled || pending.definition!==JSON.stringify(props.modelValue) || pending.options!==JSON.stringify(props.options)) return
    if(contract.versionId!==id) throw new Error('返回的实例版本与所选版本不一致，请重新读取')
    const previous=stages.value[pending.index]!, current=routing(previous,pending.key)
    const routes=rows(current.routes), replacement=recipeRouteForContract(contract)
    if(!Number.isSafeInteger(routeLimit.value) || Number(routeLimit.value)<1 || !Number.isSafeInteger(variantLimit.value) || Number(variantLimit.value)<1) throw new Error('配方目录缺少有效数量限制，不能选择实例')
    if(pending.routeIndex===null && (routes.length>=Number(routeLimit.value) || current.mode==='fixed'&&routes.length>0)) throw new Error('当前执行方式不能再添加实例，请先配置多实例切换')
    if(pending.routeIndex!==null && !routes[pending.routeIndex]) throw new Error('待更换的执行实例已不存在，请重新选择')
    if(pending.routeIndex!==null && routes[pending.routeIndex]!.execution_instance_version_id===id && routes[pending.routeIndex]!.expected_definition_sha256===contract.sha256) {closeInstancePicker();return}
    if(previous[pending.key]===undefined && !props.options.routingModeOptions.some(row=>row.value==='fixed')) throw new Error('当前目录不支持单一实例执行')
    const value=stages.value.map((stage,index)=>index===pending.index?{...stage,[pending.key]:{
      ...(previous[pending.key]===undefined?{mode:'fixed',variant_count:1,evaluator:null}:current),
      routes:pending.routeIndex===null?[...routes,replacement]:routes.map((route,index)=>index===pending.routeIndex?replacement:route),
    }}:stage)
    closeInstancePicker()
    if(pending.routeIndex===null) saveStages(value)
    else requestStageEdit(pending.index,value,'确认更换执行实例','只更换此项实例并清除它原有的输入来源和步骤参数，请按新实例重新配置；其他实例及步骤保持不变。')
  } catch(e) {if(request===sequence)error.value=apiErrorSummary(e)} finally {if(request===sequence)loading.value=false}
}
function updateRoute(index:number, key:RecipeRoutingKey, routeIndex:number, value:JsonObject|null) {
  if(props.disabled) return
  const stage=stages.value[index], routes=rows(routing(stage,key).routes)
  if(value) updateRouting(index,key,{...routing(stage,key),routes:routes.map((row,at)=>at===routeIndex?value:row)})
  else requestStageEdit(index,stages.value.map((row,at)=>at===index?{...row,[key]:{...routing(stage,key),routes:routes.filter((_,at)=>at!==routeIndex)}}:row),
    '确认移除执行实例','移除此项实例及它的输入来源和步骤参数；其他实例及步骤保持不变。')
}
function requestRoutingMode(index:number,key:RecipeRoutingKey,event:Event) {
  const input=event.target as HTMLSelectElement, mode=input.value, current=routing(stages.value[index]!,key)
  input.value=String(current.mode||'')
  if(props.disabled || !props.options?.routingModeOptions.some(row=>row.value===mode) || props.modelValue.interaction_mode==='unattended'&&mode==='manual_choice') return
  if(mode==='fixed' && rows(current.routes).length>1) {
    pendingSingleInstance.value={index,key,definition:JSON.stringify(props.modelValue),options:JSON.stringify(props.options),keep:null}
    return
  }
  updateRouting(index,key,{...current,mode})
}
function confirmSingleInstance() {
  const pending=pendingSingleInstance.value
  if(!pending || props.disabled || pending.keep===null || pending.definition!==JSON.stringify(props.modelValue) || pending.options!==JSON.stringify(props.options)) return
  const current=routing(stages.value[pending.index]!,pending.key), kept=rows(current.routes)[pending.keep]
  if(!kept) return
  pendingSingleInstance.value=null
  updateRouting(pending.index,pending.key,{...current,mode:'fixed',routes:[kept]})
}
function moveInstance(index:number,key:RecipeRoutingKey,at:number,offset:number) {
  if(props.disabled) return
  const current=routing(stages.value[index]!,key), routes=[...rows(current.routes)], to=at+offset
  if(!routes[at] || !routes[to]) return
  ;[routes[at],routes[to]]=[routes[to]!,routes[at]!]
  updateRouting(index,key,{...current,routes})
}
function setInteraction(value:string) { if(!props.disabled) emit('update:modelValue',{...props.modelValue,interaction_mode:value}) }
function setFieldPolicy(index:number,key:string) {
  const policy=props.options?.fieldReviewPolicyOptions.find(row=>row.key===key)
  updateStage(index,{field_review:policy?{mode:'policy',policy:{key:policy.key,version:policy.version,implementation_sha256:policy.implementation_sha256}}:{mode:'manual'}})
}
const authoringSections = computed(() => [
  ...(slots.identity ? [{ key: 'identity', title: '名称与版本', description: '配方组织步骤和输入来源；模型能力由步骤引用的执行实例提供，不在这里重复配置。' }] : []),
  { key: 'steps', title: '步骤编排', description: '先确定制作方式、步骤职责与顺序。审核和平台处理不必绑定模型。', summary: `已编排 ${stages.value.length} 个步骤` },
  { key: 'bindings', title: '实例与输入来源', description: '生成步骤引用精确执行实例；这里只配置来源和本步骤允许的参数，不重复配置模型、工作流或策略。' },
  { key: 'rules', title: '审核与选择', description: '区分输入字段确认、生成候选选择和阶段审核；自动策略不记作人工审核。' },
  { key: 'review', title: '全链复核', description: '查看当前草稿的数据流；本地复核不等于发布、准入或真实调用。', summary: validate.value.length ? `${validate.value.length} 项待处理` : '现有校验无提示；不代表配置齐全或可发布' },
])
</script>

<template>
  <section class="recipe-editor">
    <p v-if="!options" role="alert">尚未读取服务端配方目录。</p>
    <template v-else>
      <p v-if="modelValue.schema_version!==3" role="alert">此记录不是 schema3 配方，不提供旧合同编辑或自动转换。请按清理计划处理后新建。</p>
      <template v-else>
      <ConfigurationAuthoringShell label="配方作者" :sections="authoringSections" :busy="disabled"
        :issues="authorIssues"
        :status="authorStatus || (disabled ? '当前不可编辑；保存、发布与运行是不同动作。' : '正在编辑当前配方草稿；修改仅在提交到服务端后保存，不会自动发布或运行。')">
        <template v-if="slots.identity" #identity><slot name="identity" /></template>
        <template v-if="slots.feedback" #feedback><slot name="feedback" /></template>
        <template v-if="slots.actions" #actions><slot name="actions" /></template>
        <template #steps>
        <label>制作方式<select :disabled="disabled" :value="modelValue.interaction_mode||''" @change="setInteraction(($event.target as HTMLSelectElement).value)"><option value="">请选择</option><option v-for="option in options.interactionModeOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
        <p>每个生成步骤直接选择精确执行实例版本，再配置输入来源和步骤参数；部署、工作流与提示词策略沿用实例配置。保存草稿不代表可以发布或运行。</p>
        <div v-if="!disabled" class="toolbar"><button v-for="preset in options.presets" :key="preset.value" type="button" class="button ghost tiny" :disabled="stages.length>0" @click="saveStages(preset.stages)">{{ preset.label }}</button><small v-if="stages.length">预设仅用于空草稿，不覆盖已编辑阶段。</small><button type="button" class="button ghost tiny" @click="saveStages([...stages,{key:'',mode:'',approval:''}])">添加阶段</button></div>
        <article v-for="(stage,index) in stages" :key="`${index}:${stage.key}`">
          <header><strong>{{ index+1 }} · {{ stageOption(stage)?.repeatable ? stage.name : stageLabel(stage.key)||'未选择阶段' }}</strong><div v-if="!disabled" class="toolbar"><button type="button" class="button ghost tiny" :disabled="!canMove(index,-1)" @click="moveStage(index,-1)">上移步骤</button><button type="button" class="button ghost tiny" :disabled="!canMove(index,1)" @click="moveStage(index,1)">下移步骤</button><button type="button" class="button ghost tiny" @click="removeStage(index)">移除阶段</button></div></header>
          <div class="grid">
            <label>阶段职责<select :disabled="disabled" :value="recipeStageKind(stage)" @change="requestKind(index,$event)"><option value="">请选择职责</option><option v-for="option in options.stageOptions" :key="option.value" :value="option.value">{{ option.repeatable ? option.label : stageLabel(option.value) }}</option></select></label>
            <label>执行方式<select :disabled="disabled" :value="stage.mode" @change="requestMode(index,$event)"><option value="">请选择方式</option><option v-for="mode in stageOption(stage)?.modes||[]" :key="mode" :value="mode">{{ modeLabel(mode) }}</option></select></label>
            <label v-if="stageOption(stage)?.repeatable">步骤显示名称<input :disabled="disabled" :value="stage.name" maxlength="160" @input="updateStage(index,{name:($event.target as HTMLInputElement).value})"/></label>
          </div>
          <template v-if="stageOption(stage)?.repeatable"><p>按配置位置顺序执行；改名不改变步骤身份。候选选择后推进，不另设阶段审核；移动后请核对上游依赖。</p><details><summary>步骤身份（同名步骤以此区分）</summary><small>{{ stage.key }}</small></details></template>
        </article>
        </template>
        <template #bindings>
        <p v-if="!stages.length">先在步骤编排中添加阶段，再配置其输入。</p>
        <p v-if="sourceLoading" role="status">正在按确切实例读取上游输出与字段目录…</p>
        <p v-if="sourceError" role="alert">上游输出目录读取失败：{{ sourceError }}。已保存映射未修改，旧目录不再用于选择。<button type="button" class="button ghost" @click="loadSourceOptions">重新读取来源目录</button></p>
        <article v-for="(stage,index) in stages" :key="`${index}:${stage.key}`" :data-authoring-field="`stages.${index}`">
          <header><strong>{{ index+1 }} · {{ stageOption(stage)?.repeatable ? stage.name : stageLabel(stage.key)||'未选择阶段' }}</strong><span>{{ modeLabel(stage.mode) }}</span></header>
          <p v-if="!stageOption(stage)?.routingModes.includes(String(stage.mode)) && !['assembly','delivery'].includes(String(stage.key))">此执行方式没有模型调用绑定；只按阶段职责处理。审核与选材规则在下一分段配置。</p>
          <RecipeAssemblyOutputEditor v-if="stage.key==='assembly'&&stage.mode==='existing_pipeline'" :model-value="stage.assembly_output as JsonObject|undefined" :schema="options.assemblyOutputSchema" :source-options="routeOptions(index).textOutputOptions" :disabled="disabled" @update:model-value="setAssemblyOutput(index,$event)" @validity="assemblyErrors=$event"/>
          <RecipeAssemblyOutputEditor v-if="stage.key==='assembly'&&stage.mode==='existing_pipeline'" purpose="timing" :model-value="stage.assembly_timing as JsonObject|undefined" :schema="options.assemblyTimingSchema" :source-options="routeOptions(index).textOutputOptions" :disabled="disabled" @update:model-value="setAssemblyOutput(index,$event,'assembly_timing')" @validity="timingErrors=$event"/>
          <RecipeAssemblyOutputEditor v-if="stage.key==='assembly'&&stage.mode==='existing_pipeline'" purpose="media" :model-value="stage.assembly_media as JsonObject|undefined" :schema="options.assemblyMediaSchema" :source-options="routeOptions(index).textOutputOptions" :disabled="disabled" @update:model-value="setAssemblyOutput(index,$event,'assembly_media')" @validity="mediaErrors=$event"/>
          <RecipeAssemblyOutputEditor v-if="stage.key==='delivery'&&stage.mode==='existing_pipeline'" purpose="delivery" :model-value="stage.delivery_metadata as JsonObject|undefined" :schema="options.deliveryMetadataSchema" :source-options="routeOptions(index).textOutputOptions" :disabled="disabled" @update:model-value="setAssemblyOutput(index,$event,'delivery_metadata')" @validity="deliveryErrors=$event"/>
          <section v-if="recipeStageKind(stage)==='bible_image_generation' && stage.mode==='existing_pipeline'">
            <strong>本轮成图来源</strong>
            <p>按本轮 Bible 需求展开；未配置时沿本阶段的模型生成职责。已有/上传按各角色的选材策略履约，不是模型参考输入，也不等于审核采用。</p>
            <div class="grid"><label v-for="role in (['primary','variant'] as const)" :key="role">{{ role==='primary'?'主图':'变体' }}成图来源<select :disabled="disabled || !options.materialSourceOptions?.length" :value="recipeMaterialSource(stage,role)" @change="requestMaterialSource(index,role,$event)"><option v-if="!options.materialSourceOptions?.some(row=>row.value===recipeMaterialSource(stage,role))" :value="recipeMaterialSource(stage,role)">来源未接通或目录缺失</option><option v-for="option in options.materialSourceOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label></div>
            <div class="grid"><template v-for="role in (['primary','variant'] as const)" :key="role"><label v-if="recipeMaterialSource(stage,role)!=='generate'">{{role==='primary'?'主图':'变体'}}成图选材策略<select :disabled="disabled || !options.materialSelectionOptions?.length" :value="recipeMaterialSelectionPolicy(stage,role)" @change="setMaterialSelection(index,role,($event.target as HTMLSelectElement).value)"><option v-if="!options.materialSelectionOptions?.some(row=>row.value===recipeMaterialSelectionPolicy(stage,role))" :value="recipeMaterialSelectionPolicy(stage,role)">策略未接通或目录缺失</option><option v-for="option in options.materialSelectionOptions" :key="option.value" :value="option.value">{{option.label}}</option></select></label></template></div>
            <p v-if="(['primary','variant'] as const).some(role=>recipeMaterialSelectionPolicy(stage,role)==='unique_version')">自动选材只用阶段开始时已关联确切设定的唯一素材版本；无匹配、多版本或文件不可用时转本轮人工选材，不取最新图、不改走生成。后续新增版本不会自动替换冻结来源。</p>
            <p>已有/上传单元缺素材时保持待来源，不创建模型准备；在运行详情的本轮素材选材表单中可人工接管。生成调用全部明确失败后可改用已有/上传成图；在途或结果未知时不能重复调用或伪装成功。</p>
          </section>
          <section v-if="stageOption(stage)?.routingModes.includes(String(stage.mode)) && recipeRoutingGroups(stage).length">
            <section v-for="group in recipeRoutingGroups(stage)" :key="group.key">
              <strong>{{ group.label==='执行'?'':group.label }}执行实例</strong>
              <p v-if="group.key==='variant_routing'">变体单独选择执行实例，不继承主图的实例和候选设置；执行时使用本轮确切主图及变体差异。</p>
              <p v-if="!rows(routing(stage,group.key).routes).length">尚未选择执行实例。</p>
              <button v-if="!disabled&&!rows(routing(stage,group.key).routes).length" type="button" class="button ghost tiny" @click="openInstancePicker(index,group.key)">选择{{ group.label==='执行'?'':group.label }}执行实例</button>
            <template v-if="stage[group.key]">
              <label>每个制作单元的候选数<input type="number" min="1" :max="variantLimit||undefined" step="1" :disabled="disabled" :value="routing(stage,group.key).variant_count" @input="updateRouting(index,group.key,{...routing(stage,group.key),variant_count:Number(($event.target as HTMLInputElement).value)})"/></label>
              <p>执行方式：{{ modeLabel(routing(stage,group.key).mode) }} · 已配置 {{ rows(routing(stage,group.key).routes).length }} 个实例</p>
              <details :open="routing(stage,group.key).mode!=='fixed'||rows(routing(stage,group.key).routes).length>1">
                <summary>高级设置：多实例切换</summary>
                <label>实例选择方式<select :disabled="disabled" :value="routing(stage,group.key).mode" @change="requestRoutingMode(index,group.key,$event)"><option value="">请选择</option><option v-for="option in options.routingModeOptions" :key="option.value" :value="option.value" :disabled="modelValue.interaction_mode==='unattended'&&option.value==='manual_choice'">{{ modeLabel(option.value) }}</option></select></label>
                <p>单一实例不自动换目标；按顺序切换仅在确定失败且符合原重试规则时尝试下一实例。输入缺失、待审核或结果未知不会自动换模型。</p>
                <button v-if="!disabled&&['ordered_fallback','manual_choice'].includes(String(routing(stage,group.key).mode))" type="button" class="button ghost tiny" :disabled="!routeLimit||rows(routing(stage,group.key).routes).length>=routeLimit" @click="openInstancePicker(index,group.key)">添加备选执行实例</button>
              </details>
              <div v-for="(route,routeIndex) in rows(routing(stage,group.key).routes)" :key="`${routeIndex}:${route.execution_instance_version_id}`" class="route">
                <header><strong>{{ rows(routing(stage,group.key).routes).length===1?'所选执行实例':`执行实例 ${routeIndex+1}` }}</strong><div v-if="!disabled" class="toolbar">
                  <template v-if="rows(routing(stage,group.key).routes).length>1"><button type="button" class="button ghost tiny" :disabled="routeIndex===0" @click="moveInstance(index,group.key,routeIndex,-1)">上移实例</button><button type="button" class="button ghost tiny" :disabled="routeIndex===rows(routing(stage,group.key).routes).length-1" @click="moveInstance(index,group.key,routeIndex,1)">下移实例</button></template>
                  <button type="button" class="button ghost tiny" @click="openInstancePicker(index,group.key,routeIndex)">更换执行实例</button><button type="button" class="button ghost tiny" @click="updateRoute(index,group.key,routeIndex,null)">移除执行实例</button>
                </div></header>
                <ProductionRecipeRouteEditor :model-value="route" :options="routeOptions(index)" :prior-stages="stages.slice(0,index)" :stage-key="String(stage.key)" :appearance-role="group.key==='variant_routing'?'variant':'primary'" :disabled="disabled" @update:model-value="updateRoute(index,group.key,routeIndex,$event)" @validity="routeErrors[routeErrorKey(stage,group.key,routeIndex,route)]=$event"/>
              </div>
            </template>
            </section>
          </section>
        </article>
        </template>
        <template #rules>
          <p v-if="!stages.length">尚未添加步骤，不预设审核或选择规则。</p>
          <article v-for="(stage,index) in stages" :key="`${index}:${stage.key}`">
            <header><strong>{{ index+1 }} · {{ stageOption(stage)?.repeatable ? stage.name : stageLabel(stage.key)||'未选择阶段' }}</strong></header>
            <p v-if="stageOption(stage)?.repeatable">此步骤按候选选择推进，不另设阶段审核。</p>
            <label v-else>阶段审核<select :disabled="disabled" :value="stage.approval" @change="updateStage(index,{approval:($event.target as HTMLSelectElement).value})"><option value="">请选择</option><option v-for="option in options.approvalOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
            <label v-if="stage.approval==='policy'">自动审核策略<select :disabled="disabled" :value="stage.decision_policy||''" @change="updateStage(index,{decision_policy:($event.target as HTMLSelectElement).value})"><option value="">请选择此阶段已注册策略</option><option v-if="stageOption(stage)?.decisionPolicy" :value="stageOption(stage)?.decisionPolicy||''">{{ stageOption(stage)?.decisionPolicy }}</option></select></label>
            <RecipeShotReferencePolicy v-if="stage.key === 'storyboard_review' && stage.mode === 'review'" :model-value="stage.shot_reference_selection" :prior="stages.slice(0,index)" :options="options" :disabled="disabled" @update:model-value="updateStage(index,{shot_reference_selection:$event})" />
            <template v-if="stageOption(stage)?.routingModes.includes(String(stage.mode)) && recipeRoutingGroups(stage).length">
              <label>生成字段确认<select :disabled="disabled" :value="obj(obj(stage.field_review).policy).key||''" @change="setFieldPolicy(index,($event.target as HTMLSelectElement).value)"><option value="">人工确认</option><option v-for="policy in options.fieldReviewPolicyOptions" :key="String(policy.key)" :value="String(policy.key)" :disabled="modelValue.interaction_mode!=='unattended'">{{ policy.label }}</option></select><small>只控制辅助生成的输入字段，不替代阶段审核或输出候选选择。</small></label>
              <template v-for="group in recipeRoutingGroups(stage)" :key="group.key">
                <label v-if="stage[group.key] && stageOption(stage)?.candidateEvaluatorOptions.length">{{ group.label }}候选选择方式<select :disabled="disabled" :value="routing(stage,group.key).evaluator===null ? '' : routing(stage,group.key).evaluator" @change="updateRouting(index,group.key,{...routing(stage,group.key),evaluator:($event.target as HTMLSelectElement).value||null})"><option v-for="option in stageOption(stage)?.candidateEvaluatorOptions" :key="String(option.value)" :value="option.value===null ? '' : option.value" :disabled="modelValue.interaction_mode==='unattended'&&option.value===null">{{ option.label }}</option></select><small>选择本步骤／本角色生成的候选，不等于字段确认或 Bible 审核采用；成功项不会因迟到结果自动替换。</small></label>
              </template>
            </template>
          </article>
        </template>
        <template #review>
          <div v-if="validate.length" role="status"><strong>本地配置待处理（不会打断当前填写）</strong><ul><li v-for="(message,index) in validate" :key="index">{{ message }}</li></ul></div>
          <ProductionRecipeDefinitionView :definition="modelValue" :options="options"/>
        </template>
      </ConfigurationAuthoringShell>
      </template>
    </template>
  </section>
  <BaseModal :open="Boolean(instancePicker)" title="选择执行实例" width="760px" @close="closeInstancePicker">
    <section class="instance-picker">
      <p>{{ instancePicker?.label }}</p>
      <p>只选择已发布的精确版本，不重新组合模型、工作流或策略。选择只修改草稿，不发布、不运行。</p>
      <div class="toolbar"><label>搜索已发布实例<input v-model="search" :disabled="loading" @keydown.enter.prevent="loadVersions()"/></label><button type="button" class="button ghost" :disabled="loading||disabled" @click="loadVersions()">搜索实例</button></div>
      <p v-if="loading" role="status">正在读取执行实例…</p>
      <p v-if="error" role="alert">{{ error }} <button type="button" class="button ghost" :disabled="loading||disabled" @click="loadVersions(page)">重新读取</button></p>
      <p v-if="!loading&&!error&&!versions.length">没有匹配的已发布实例，请调整搜索或在执行实例管理中配置。</p>
      <article v-for="version in versions" :key="version.id"><strong>{{ version.instance_name }} v{{ version.version }}</strong><small>{{ version.operation }} · {{ version.prompt_mode }} · {{ version.id }}</small><button type="button" class="button primary" :disabled="loading||disabled" @click="selectInstance(version.id)">选择此版本</button></article>
      <div class="toolbar"><button type="button" class="button ghost" :disabled="loading||page===1||disabled" @click="loadVersions(page-1)">上一页</button><span>第 {{ page }} 页</span><button type="button" class="button ghost" :disabled="loading||!next||disabled" @click="loadVersions(page+1)">下一页</button></div>
    </section>
    <template #footer><button type="button" class="button ghost" @click="closeInstancePicker">取消选择</button></template>
  </BaseModal>
  <BaseModal :open="Boolean(pendingSingleInstance)" title="改为单一执行实例" @close="pendingSingleInstance=null">
    <p>请选择保留的实例。确认后移除其余实例及其输入映射、步骤参数；保留项的配置、候选数和选择方式不变。</p>
    <label v-if="pendingSingleInstance" class="retained-instance">保留的执行实例<select v-model="pendingSingleInstance.keep" :disabled="disabled"><option :value="null">请选择，不自动保留第一项</option><option v-for="(route,index) in rows(routing(stages[pendingSingleInstance.index]!,pendingSingleInstance.key).routes)" :key="index" :value="index">实例 {{ index+1 }} · {{ route.execution_instance_version_id }}</option></select></label>
    <template #footer><button type="button" class="button ghost" @click="pendingSingleInstance=null">保留多实例</button><button type="button" class="button primary" :disabled="disabled||pendingSingleInstance?.keep==null" @click="confirmSingleInstance">确认保留所选实例</button></template>
  </BaseModal>
  <BaseModal :open="Boolean(pendingMaterialSource)" title="确认切换成图来源" @close="pendingMaterialSource=null">
    <p>切换{{ pendingMaterialSource?.role==='primary'?'主图':'变体' }}为已有/上传成图将移除该角色的执行实例及输入配置；其他角色不变。取消会保留原配置。</p>
    <template #footer><button type="button" class="button ghost" @click="pendingMaterialSource=null">保留原配置</button><button type="button" class="button" :disabled="disabled" @click="confirmMaterialSource">确认切换来源</button></template>
  </BaseModal>
  <BaseModal :open="Boolean(pendingStageEdit)" :title="pendingStageEdit?.title || '确认阶段变更'" @close="pendingStageEdit=null">
    <p>{{ pendingStageEdit?.label }}</p>
    <p>{{ pendingStageEdit?.message }}</p>
    <p>仅修改当前编辑草稿，确认后仍需保存；取消会保留原配置。</p>
    <template #footer><button type="button" class="button ghost" @click="pendingStageEdit=null">保留原配置</button><button type="button" class="button" :disabled="disabled" @click="confirmStageEdit">确认阶段变更</button></template>
  </BaseModal>
</template>

<style scoped>
.instance-picker input,.retained-instance select{width:100%;min-width:0;min-height:36px;padding:8px;border:1px solid var(--border,#303743);border-radius:6px;background:var(--bg-input,#090c10);color:inherit}.retained-instance{display:grid;gap:6px;min-width:0}
.instance-picker{display:grid;gap:12px;min-width:0}.instance-picker article{display:grid;gap:8px;border-top:1px solid var(--border,#2b323c);padding:12px 0;min-width:0}.instance-picker label{display:grid;gap:6px;min-width:0;flex:1}.instance-picker input{width:100%;min-width:0;min-height:36px}.instance-picker p,.instance-picker small,.instance-picker strong{overflow-wrap:anywhere;margin:0}.instance-picker [role=alert]{color:#e7b669}
.recipe-editor{display:grid;gap:var(--config-space-md);min-width:0;font:var(--config-body-size)/1.65 var(--font)}.recipe-editor article{padding:var(--config-space-md) 0;border-top:1px solid var(--border-strong);display:grid;gap:var(--config-space-md)}.recipe-editor header,.toolbar{display:flex;gap:var(--config-space-sm);align-items:center;justify-content:space-between;flex-wrap:wrap}.recipe-editor label{display:grid;gap:var(--config-space-sm);min-width:0}.recipe-editor input,.recipe-editor select{width:100%;min-width:0}.recipe-editor p,.recipe-editor small{margin:0;color:var(--text-secondary);overflow-wrap:anywhere}.recipe-editor small{font-size:var(--config-help-size)}.recipe-editor section{display:grid;gap:var(--config-space-md)}.recipe-editor [role=alert]{color:#e7b669}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:var(--config-space-sm)}.route{border-left:2px solid var(--border-strong);padding-left:var(--config-space-md);display:grid;gap:var(--config-space-md)}.recipe-editor summary{cursor:pointer;padding:var(--config-space-sm) 0;min-height:var(--config-control-height)}@media(max-width:760px){.grid{grid-template-columns:1fr}}
</style>
