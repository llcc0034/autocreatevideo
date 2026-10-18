<script setup lang="ts">
import {computed, nextTick, onBeforeUnmount, ref, watch} from 'vue'
import BusinessOutputManager from './BusinessOutputManager.vue'
import RecipeStructureMappingEditor from './RecipeStructureMappingEditor.vue'
import ConfigurationDiscardNotice from './ConfigurationDiscardNotice.vue'
import RegistryResourceExplorer from './RegistryResourceExplorer.vue'
import {businessOutputs, type BusinessOutputVersion} from '@/services/businessOutputs'
import {dataStructures, type StructureMappingOptions} from '@/services/dataStructures'
import {apiErrorSummary} from '@/services/apiErrorPresentation'
import type {JsonObject,ProductionRecipeEditorOptions} from '@/services/platformTypes'

const props=defineProps<{modelValue?:JsonObject;schema?:JsonObject;disabled?:boolean;purpose?:'output'|'timing'|'delivery'|'media';frozenContract?:JsonObject;sourceOptions?:ProductionRecipeEditorOptions['textOutputOptions']}>()
const emit=defineEmits<{'update:modelValue':[JsonObject|undefined];validity:[string[]]}>()
const chooser=ref(false),busy=ref(false),error=ref(''),label=ref(''),loadedStructureId=ref('')
const sourceSchema=ref<JsonObject|null>(null),options=ref<StructureMappingOptions|null>(null),draftErrors=ref<string[]>([])
const pendingChange=ref<{value:JsonObject|undefined;message:string;actionLabel:string;snapshot:string}|null>(null)
const root=ref<HTMLElement|null>(null)
let returnFocus:HTMLElement|null=null
watch(pendingChange,(value,previous)=>{
  if(!value&&previous)void nextTick(()=>{
    const target=returnFocus?.isConnected?returnFocus:root.value
    target?.focus({preventScroll:true});returnFocus=null
  })
},{flush:'post'})
const changeSnapshot=computed(()=>JSON.stringify([props.modelValue,props.schema,props.sourceOptions,props.purpose,props.frozenContract]))
watch(changeSnapshot,()=>{pendingChange.value=null},{flush:'sync'})
watch(()=>props.disabled,value=>{if(value)pendingChange.value=null},{flush:'sync'})
function requestChange(value:JsonObject|undefined,message:string,actionLabel:string){
  if(props.disabled||pendingChange.value)return
  returnFocus=typeof document!=='undefined'&&document.activeElement instanceof HTMLElement?document.activeElement:null
  pendingChange.value={value:value===undefined?undefined:JSON.parse(JSON.stringify(value)),message,actionLabel,snapshot:changeSnapshot.value}
}
function confirmChange(){
  const pending=pendingChange.value
  if(!pending||props.disabled)return
  pendingChange.value=null
  if(pending.snapshot!==changeSnapshot.value)return
  emit('update:modelValue',pending.value)
}
let serial=0
const isTiming=computed(()=>props.purpose==='timing')
const isDelivery=computed(()=>props.purpose==='delivery')
const isMedia=computed(()=>props.purpose==='media')
const title=computed(()=>isMedia.value?'已采用片段核对':isDelivery.value?'交付标题与说明':isTiming.value?'装配时序核对':'装配输出参数')
const inputLabel=computed(()=>isDelivery.value?'交付':'装配')
const enabled=computed(()=>props.modelValue!==undefined),rule=computed(()=>props.modelValue||{})
const selectedMode=computed(()=>rule.value.source!==undefined)
const upstreamOptions=computed(()=>(props.sourceOptions||[]).filter(row=>row.kind==='json'&&row.scopeKind==='project'&&row.outputVersionId&&row.outputContractSha256&&row.structureVersionId))
const sourceIdentity=(row:{stageKey:string;outputKey:string})=>JSON.stringify([row.stageKey,row.outputKey])
const selectedIdentity=computed(()=>{const source=rule.value.source as JsonObject|undefined;return source?JSON.stringify([source.stage_key,source.output_key]):''})
function requestSourceMode(event:Event){
  const input=event.target as HTMLSelectElement, mode=input.value
  input.value=selectedMode.value?'selected':'manual'
  sourceMode(mode)
}
function sourceMode(mode:string){
  if(props.disabled||pendingChange.value||!['manual','selected'].includes(mode)||selectedMode.value===(mode==='selected'))return
  requestChange({output_version_id:'',expected_contract_sha256:'',mapping:rule.value.mapping!,
    ...(mode==='selected'?{source:{kind:'selected_output',stage_key:'',output_key:'',scope:{kind:'project'},selection_policy:'reviewed_selected'}}:{source_text:''})},
    `切换${inputLabel.value}来源会清除原来源及版本；字段映射保留，请重新核对。仅修改当前草稿，确认后仍需保存。`,'确认切换来源')
}
function chooseSource(value:string){
  if(props.disabled||pendingChange.value)return
  const row=upstreamOptions.value.find(item=>sourceIdentity(item)===value)
  if(!row)return
  emit('update:modelValue',{output_version_id:row.outputVersionId!,expected_contract_sha256:row.outputContractSha256!,mapping:rule.value.mapping!,
    source:{kind:'selected_output',stage_key:row.stageKey,output_key:row.outputKey,scope:{kind:'project'},selection_policy:'reviewed_selected'}})
}
const node=computed(()=>((rule.value.mapping as JsonObject|undefined)?.node||{}) as JsonObject)
const identity=computed(()=>JSON.stringify([rule.value.output_version_id,rule.value.expected_contract_sha256,selectedIdentity.value]))
function update(change:JsonObject){if(!props.disabled&&!pendingChange.value)emit('update:modelValue',{...rule.value,...change})}
function toggle(){
  if(props.disabled||pendingChange.value)return
  if(enabled.value){
    requestChange(undefined,isMedia.value?'移除此步骤的片段核对映射及原始输入；原媒体采用不变。':isDelivery.value?'移除此步骤的交付字段映射及原始输入；之后使用默认交付标题和说明。':isTiming.value?'移除此步骤的装配时序映射及原始输入；之后仅使用本轮分镜时序。':'移除此步骤的装配输出映射及原始输入；之后使用项目几何配置。','确认移除此映射')
    return
  }
  emit('update:modelValue',{output_version_id:'',expected_contract_sha256:'',source_text:'',mapping:{schema_version:1,node:{}}})
}
function choose(row:BusinessOutputVersion){
  if(props.disabled||pendingChange.value||selectedMode.value||row.status!=='published'||row.parser_key!=='structured_json.v1')return
  update({output_version_id:row.id,expected_contract_sha256:row.contract_sha256});chooser.value=false
}
async function load(){
  const request=++serial
  error.value='';label.value='';loadedStructureId.value='';sourceSchema.value=null;options.value=null;busy.value=false
  if(!enabled.value||!rule.value.output_version_id)return
  if(props.frozenContract){
    const reference=props.frozenContract.output_reference as JsonObject|undefined
    if(!props.disabled||!reference||reference.id!==rule.value.output_version_id||reference.sha256!==rule.value.expected_contract_sha256){error.value=`冻结${inputLabel.value}合同与所选版本不一致`;return}
    label.value=`冻结输出版本：${String(reference.id)}`
    return
  }
  const id=String(rule.value.output_version_id),sha=rule.value.expected_contract_sha256
  busy.value=true
  try{
    const row=await businessOutputs.detail(id)
    if(request!==serial)return
    if(row.id!==id||row.contract_sha256!==sha||row.parser_key!=='structured_json.v1'||!row.data_structure_version)throw new Error('输出版本或摘要不一致；原配置保留，请重新选择')
    const option=upstreamOptions.value.find(item=>sourceIdentity(item)===selectedIdentity.value)
    if(selectedMode.value&&option&&row.data_structure_version!==option.structureVersionId)throw new Error('上游目录与确切输出的结构版本不一致')
    if(!props.disabled&&row.status!=='published')throw new Error(`所选输出版本已退役，不能提交新的${inputLabel.value}映射；原配置未替换`)
    const [schema,catalog]=await Promise.all([dataStructures.schema(row.data_structure_version),dataStructures.mappingOptions()])
    if(request!==serial)return
    if(!schema.valid||!schema.json_schema)throw new Error('确切来源结构不可用，不能以空结构编辑')
    sourceSchema.value=schema.json_schema;options.value=catalog;loadedStructureId.value=row.data_structure_version;label.value=`${row.name} · v${row.version}`
  }catch(cause){if(request===serial)error.value=apiErrorSummary(cause)}
  finally{if(request===serial)busy.value=false}
}
const errors=computed(()=>{
  if(!enabled.value||props.disabled)return []
  const result=[...draftErrors.value]
  if(pendingChange.value)result.push(`${title.value}：请确认变更或继续编辑原配置`)
  if(!props.schema)result.push(`${title.value}目录未读取，不能保存映射`)
  if(busy.value)result.push(`正在读取确切${inputLabel.value}输出结构`)
  if(error.value)result.push(error.value)
  if(!rule.value.output_version_id||!rule.value.expected_contract_sha256)result.push('请选择已发布的通用JSON输出版本')
  if(!sourceSchema.value&&!busy.value&&!error.value)result.push('来源结构尚未读取')
  if(selectedMode.value){
    const option=upstreamOptions.value.find(row=>sourceIdentity(row)===selectedIdentity.value)
    if(!option||option.outputVersionId!==rule.value.output_version_id||option.outputContractSha256!==rule.value.expected_contract_sha256)result.push('上游已移除、尚未就绪或确切合同变化；原配置保留，请重新选择')
    else if(loadedStructureId.value&&option.structureVersionId!==loadedStructureId.value)result.push('上游目录与确切输出的结构版本不一致')
  }else if(typeof rule.value.source_text!=='string'||!rule.value.source_text)result.push(`请提供按所选结构编写的${inputLabel.value}输入原文`)
  if(!node.value.kind)result.push(`请配置${inputLabel.value}输出字段映射`)
  return result
})
watch(()=>JSON.stringify(errors.value),value=>emit('validity',JSON.parse(value)),{immediate:true})
watch([identity,enabled,()=>props.disabled,()=>props.frozenContract],()=>{draftErrors.value=[];void load()},{immediate:true})
onBeforeUnmount(()=>{serial++})
</script>

<template>
  <RegistryResourceExplorer :label="title" v-slot="{inspect}">
  <section ref="root" class="assembly-output" tabindex="-1" :aria-label="title">
    <h4>{{title}}</h4>
    <ConfigurationDiscardNotice v-if="pendingChange" :title="`确认${title}变更`" :message="pendingChange.message" :action-label="pendingChange.actionLabel" :busy="Boolean(disabled)" @keep="pendingChange=null" @discard="confirmChange" />
    <fieldset class="assembly-controls" :disabled="Boolean(pendingChange)">
    <p v-if="isMedia">可选：把完整集镜片段清单映射到本轮已采用产物。生成候选和已有素材均沿原选片入口明确采用；素材采用后的原产物保留真实Asset来源，不伪造模型调用。这里仅核对确切产物与顺序，不自动采用、换片或补缺项。</p>
    <p v-else-if="isDelivery">可选：将原文或本轮更早步骤的已选择结构化结果映射为交付标题和说明。保留原值、确切版本及来源并纳入签名，不改项目资料、不触发模型或外部分发。未配置时使用默认交付标题和说明。</p>
    <p v-else-if="isTiming">可选：以完整时序原文或配方更早步骤的已选择结构化结果核对本轮分镜。集镜范围、顺序、时长及起止必须一致；不改选片、不覆盖分镜、不自动排序。未配置时仅使用本轮分镜时序。</p>
    <p v-else>未配置映射时，宽、高、帧率在阶段准备时取项目值并冻结。映射只改变输出几何，不选择媒体、不修改镜头时序或编码策略。</p>
    <button v-if="!disabled" type="button" class="button ghost tiny" :disabled="!schema" @click="toggle">{{enabled?(isMedia?'移除片段核对映射':isDelivery?'移除交付字段映射':isTiming?'移除装配时序映射':'移除装配输出映射'):(isMedia?'按字段结构配置片段核对':isDelivery?'按字段结构配置交付字段':isTiming?'按字段结构配置装配时序':'按字段结构配置装配输出')}}</button>
    <template v-if="enabled">
      <label v-if="sourceOptions!==undefined">{{inputLabel}}输入来源<select :value="selectedMode?'selected':'manual'" :disabled="disabled" @change="requestSourceMode"><option value="manual">人工 JSON 原文</option><option value="selected">本轮上游已选择结构化结果</option></select></label>
      <label v-if="selectedMode">确切上游步骤与输出<select :value="selectedIdentity" :disabled="disabled" @change="chooseSource(($event.target as HTMLSelectElement).value)"><option value="">请选择上游</option><option v-if="selectedIdentity&&!upstreamOptions.some(row=>sourceIdentity(row)===selectedIdentity)" :value="selectedIdentity">原上游不可用（保留配置）</option><option v-for="row in upstreamOptions" :key="sourceIdentity(row)" :value="sourceIdentity(row)">{{row.label}}</option></select></label>
      <p>{{label||'确切输出版本：'+String(rule.output_version_id||'尚未选择')}}</p>
      <nav v-if="!frozenContract" class="reference-actions" :aria-label="`${title}公共定义`">
        <button v-if="typeof rule.output_version_id==='string'&&rule.output_version_id" type="button" class="button ghost small" @click="inspect('output',rule.output_version_id,$event)">查看输出定义与使用方</button>
        <button v-if="loadedStructureId" type="button" class="button ghost small" @click="inspect('structure',loadedStructureId,$event)">查看来源字段结构</button>
      </nav>
      <button v-if="!disabled&&!selectedMode" type="button" class="button ghost tiny" @click="chooser=true">选择输出版本</button>
      <p v-if="busy" role="status">读取确切结构中…</p>
      <p v-if="error" role="alert">{{error}} <button type="button" class="button ghost tiny" :disabled="busy" @click="load">重新读取</button></p>
      <label v-if="!selectedMode">{{inputLabel}}输入原文（JSON，保留自定义字段）<textarea :value="typeof rule.source_text==='string'?rule.source_text:''" :readonly="disabled" rows="5" @input="update({source_text:($event.target as HTMLTextAreaElement).value})"/></label>
      <p v-if="selectedMode">保存校验确切结构与映射；阶段准备时读取本轮已完成步骤的正式选择，冻结原值及决定/产物身份。缺少选择或来源变化会报错，不取最新候选，不新增调用。</p>
      <p v-else>保存时由同一后端解析及映射校验。更换输出版本不自动重写原文或映射；缺项和类型错误不会使用项目值补齐。</p>
      <details v-if="frozenContract"><summary>已冻结映射、原值与来源（只读）</summary><pre>{{JSON.stringify({mapping:rule.mapping,contract:frozenContract},null,2)}}</pre></details>
      <p v-if="isTiming">时序映射须完整提供 episodes、集号 number、逐镜 shots，以及每镜 number、duration_ms、episode_start_ms、episode_end_ms。仅时长模式的起止均为 null；明确起止必须连续且差值等于时长。素材及审核身份由系统读取，不在此填写。</p>
      <p v-if="isMedia">完整提供 episodes、集号 number、shots、镜号 number 和原已采用产物 artifact_id；不是Asset ID。确切文件摘要、采用决定及修订仍由原选片事实提供。仅填写映射不会采用新的候选或素材。</p>
      <RecipeStructureMappingEditor v-if="sourceSchema&&options&&schema" :key="identity" :model-value="node" :schema="schema" :label="isMedia?'逐集逐镜已采用产物':isDelivery?'交付标题、说明':isTiming?'逐集逐镜时序':'输出宽、高、帧率'" :nodes="options.nodes" :preview-context="{schema:sourceSchema,scopes:options.scopes}" :fields="[]" :outputs="[]" :scopes="[]" :prior-stages="[]" :disabled="disabled" @update:model-value="update({mapping:{schema_version:1,node:$event}})" @draft-validity="draftErrors=$event"/>
      <ul v-if="errors.length" role="alert"><li v-for="message in errors" :key="message">{{message}}</li></ul>
    </template>
    </fieldset>
    <BusinessOutputManager v-if="chooser&&!disabled" choose :supported-parser-keys="['structured_json.v1']" @select="choose" @close="chooser=false"/>
  </section>
  </RegistryResourceExplorer>
</template>

<style scoped>
.assembly-output,.assembly-controls{min-width:0;display:grid;gap:var(--config-space-md);font:var(--config-body-size)/1.65 var(--font)}
.assembly-controls{border:0;padding:0;margin:0}.assembly-output h4{font-size:var(--config-section-size);font-weight:600;margin:0}.reference-actions{display:flex;gap:var(--config-space-sm);flex-wrap:wrap}.assembly-output button{white-space:normal;overflow-wrap:anywhere}
label{display:grid;gap:6px}textarea{width:100%;min-width:0;box-sizing:border-box;font:inherit;resize:vertical}
p{overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:360px;overflow:auto}
</style>
