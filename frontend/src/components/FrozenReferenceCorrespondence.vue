<script setup lang="ts">
import type {FrozenReferenceBinding} from '@/services/frozenReferenceBinding'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
defineProps<{binding:FrozenReferenceBinding}>()
const sources:Record<string,string>={existing:'已有素材',upload:'上传素材',generated:'生成素材'}
</script>
<template>
 <dl class="reference-correspondence" aria-label="原名、版本与输入顺序对应">
  <dt>冻结原名</dt><dd>{{binding.original_name || '原名未记录'}}</dd>
  <dt>本次策略引用名</dt><dd v-if="binding.reference_name!==null">{{binding.reference_name || '无名称'}}<span v-if="binding.name_disambiguated"> · 已按使用 ID 消歧</span></dd><dd v-else>未记录策略引用名，不重新消歧</dd>
  <dt>本次输入顺序</dt><dd>该逻辑输入第 {{binding.position+1}} 项<span v-if="binding.input_slot!==null"> · 模型标记序号 {{binding.input_slot}}</span></dd>
  <template v-if="binding.shot_reference">
   <dt>原分镜使用</dt><dd>{{binding.shot_reference.use_key}} · 原选择第 {{binding.shot_reference.position+1}} 项</dd>
   <dt>创作描述（可选）</dt><dd class="creative-note">{{binding.shot_reference.purpose || '未填写'}}<small>原值保留，不作为模型用途参数。</small></dd>
   <dt>确切来源</dt><dd>{{sources[binding.shot_reference.source_mode]}} · <template v-if="binding.shot_reference.source.kind==='reference_version'">系列版本 v{{binding.shot_reference.source.version}}<br/>系列 {{binding.shot_reference.source.series_id}}<br/>版本 {{binding.shot_reference.source.reference_version_id}}</template><template v-else>直接素材 · 未选择系列版本</template></dd>
   <dt>确切设定修订</dt><dd v-if="binding.shot_reference.setting">{{binding.shot_reference.setting.reference_name}} · r{{binding.shot_reference.setting.revision}}<br/>{{binding.shot_reference.setting.setting_revision_id}}</dd><dd v-else>未绑定设定修订</dd>
   <dt>原始身份</dt><dd><ReadonlyDataValue :value="binding.shot_reference" label="原使用、版本和设定身份（技术信息）"/></dd>
  </template>
  <template v-else><dt>分镜版本来源</dt><dd>此请求未记录分镜选材版本；不读取当前版本补齐。</dd></template>
 </dl>
</template>
<style scoped>
.reference-correspondence{display:grid;grid-template-columns:minmax(100px,1fr) minmax(0,3fr);gap:6px 12px;margin:0;font-size:12px;line-height:1.7;min-width:0;overflow-wrap:anywhere}.reference-correspondence dt{color:var(--text-muted)}.reference-correspondence dd{margin:0;min-width:0}.reference-correspondence small{display:block}.creative-note{white-space:pre-wrap}@media(max-width:640px){.reference-correspondence{grid-template-columns:1fr}.reference-correspondence dt{font-weight:600}}
</style>
