<script setup lang="ts">
import { computed } from 'vue'
import type { JsonObject, ProductionRecipeEditorOptions } from '@/services/platformTypes'
import { recipeObject, recipeRows } from '@/services/productionRecipeAuthoring'
const props=defineProps<{modelValue:JsonObject;input:JsonObject;options:ProductionRecipeEditorOptions;priorStages:JsonObject[];purposeOptions?:{value:string;label:string;description:string}[];disabled?:boolean}>()
const emit=defineEmits<{'update:modelValue':[value:JsonObject]}>()
const semantic=computed(()=>recipeObject(props.input.semantic_contract))
const groups=computed(()=>recipeRows(props.modelValue.groups))
const choices=computed(()=>props.options.referenceSourceOptions.filter(row=>row.kind===props.input.kind
  && row.paired===(semantic.value.paired_with_input_key!==null) && props.priorStages.some(stage=>stage.key===row.stageKey)))
const remaining=computed(()=>choices.value.filter(row=>!groups.value.some(group=>group.source_key===row.value)))
function save(value:JsonObject[]){if(!props.disabled)emit('update:modelValue',{...props.modelValue,groups:value})}
function add(key:string){const row=remaining.value.find(item=>item.value===key);if(row&&!props.disabled)emit('update:modelValue',{
  kind:'reviewed_references',stage_key:row.stageKey,scope:{kind:row.scopeKind},groups:[...groups.value,{source_key:key,purposes:null,subject_kinds:null}]})}
function move(index:number,offset:number){const next=[...groups.value],to=index+offset;if(to<0||to>=next.length)return;[next[index],next[to]]=[next[to],next[index]];save(next)}
function filter(index:number,key:string,event:Event){const selected=Array.from((event.target as HTMLSelectElement).selectedOptions).map(row=>row.value);save(groups.value.map((group,i)=>i===index?{...group,[key]:selected.length?selected:null}:group))}
const values=(key:string)=>Array.isArray(semantic.value[key])?semantic.value[key] as string[]:[]
function purpose(index:number,event:Event){const value=(event.target as HTMLSelectElement).value;save(groups.value.map((group,i)=>{if(i!==index)return group;const next={...group};if(value)next.purpose=value;else delete next.purpose;return next}))}
function descriptions(index:number,event:Event){const value=(event.target as HTMLTextAreaElement).value;save(groups.value.map((group,i)=>i===index?{...group,purposes:value===''?null:value.split('\n')}:group))}
</script>
<template>
  <section class="reference-mapping">
    <p>读取当前镜头已审核素材，保留原创作描述和主体。目标槽位决定输入角色；仅接口要求时设置本次调用的模型用途。</p>
    <p v-if="!choices.length" role="alert">没有可用来源：请核对前置参考审核阶段、素材类型和实例配对合同。</p>
    <ol>
      <li v-for="(group,index) in groups" :key="String(group.source_key)">
        <strong>{{ choices.find(row=>row.value===group.source_key)?.label || `不可用来源：${group.source_key}` }}</strong>
        <div class="mapping-filters">
          <label v-if="values('allowed_purposes').length">本次调用的模型用途
            <select :disabled="disabled" :value="group.purpose||''" @change="purpose(index,$event)"><option value="">请选择接口要求的用途</option><option v-if="group.purpose&&!values('allowed_purposes').includes(String(group.purpose))" :value="group.purpose">原选择不在当前合同：{{group.purpose}}</option><option v-for="value in values('allowed_purposes')" :key="value" :value="value">{{ purposeOptions?.find(row=>row.value===value)?.label || value }}（{{ value }}）</option></select>
          </label>
          <p v-else>此输入无额外用途参数，无需填写模型用途。</p>
          <label v-if="values('allowed_subject_kinds').length">主体类别筛选（不选＝不筛选）
            <select multiple :disabled="disabled" :value="group.subject_kinds||[]" @change="filter(index,'subject_kinds',$event)"><option v-for="value in values('allowed_subject_kinds')" :key="value" :value="value">{{ value }}</option></select>
          </label>
        </div>
        <details><summary>可选：按创作用途描述精确筛选</summary><label>每行一个原描述，留空表示不筛选<textarea :disabled="disabled" :value="Array.isArray(group.purposes)?group.purposes.join('\n'):''" @input="descriptions(index,$event)"/></label><small>只比较原值，不从自由文字推断模型参数；通常按来源与主体选择即可。</small></details>
        <button type="button" class="button ghost tiny" :disabled="disabled||index===0" @click="move(index,-1)">上移来源组</button>
        <button type="button" class="button ghost tiny" :disabled="disabled||index===groups.length-1" @click="move(index,1)">下移来源组</button>
        <button type="button" class="button ghost tiny" :disabled="disabled" @click="save(groups.filter((_,i)=>i!==index))">移除此来源组</button>
      </li>
    </ol>
    <label>添加素材来源组<select :disabled="disabled||!remaining.length" value="" @change="add(($event.target as HTMLSelectElement).value)"><option value="">请选择来源</option><option v-for="row in remaining" :key="row.value" :value="row.value">{{ row.label }}</option></select></label>
    <small>按以上组顺序合并，组内保持审核顺序。数量超限不会自动截断；未匹配的可选输入可为空。</small>
    <p v-if="semantic.paired_with_input_key">配套音频的父输入：{{ semantic.paired_with_input_key }}。按审核记录中的视频身份配对，不按下标配对。</p>
  </section>
</template>
<style scoped>
.reference-mapping{display:grid;gap:8px;min-width:0}.reference-mapping ol{padding-left:20px;margin:0;min-width:0}.reference-mapping li{padding:8px 0;border-bottom:1px solid var(--border);overflow-wrap:anywhere;min-width:0}.mapping-filters{display:flex;gap:10px;flex-wrap:wrap;min-width:0}.reference-mapping label{display:grid;gap:5px;min-width:0;max-width:100%}.mapping-filters label{flex:1 1 240px}.reference-mapping p,.reference-mapping small{margin:0;color:var(--text-secondary)}.reference-mapping select,.reference-mapping textarea{width:100%;min-width:0;min-height:36px;max-width:100%;background:var(--bg-input,#090c10);color:inherit;border:1px solid var(--border,#303743);padding:6px}.reference-mapping button{margin:6px 6px 0 0}
</style>
