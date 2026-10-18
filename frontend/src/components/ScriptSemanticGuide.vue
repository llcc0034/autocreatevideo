<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import type {JsonObject} from '@/services/platformTypes'
import {scriptSemantics,type ScriptSemanticOptions} from '@/services/scriptSemantics'
import {scriptSemanticGuideGroups} from '@/services/scriptSemanticGuide'
import {apiErrorSummary} from '@/services/apiErrorPresentation'

const props=defineProps<{mapping:JsonObject;options?:ScriptSemanticOptions;draft?:boolean}>()
const catalog=ref<ScriptSemanticOptions|null>(null),loading=ref(false),error=ref('')
let serial=0
async function load(){
  const request=++serial;catalog.value=null;error.value='';loading.value=true
  try{const result=props.options??await scriptSemantics.options();if(request===serial)catalog.value=result}
  catch(cause){if(request===serial)error.value=apiErrorSummary(cause,'读取业务语义目录失败；原配置未改变')}
  finally{if(request===serial)loading.value=false}
}
watch(()=>props.options,load,{immediate:true})
onBeforeUnmount(()=>{serial++})
const groups=computed(()=>catalog.value?scriptSemanticGuideGroups(catalog.value,props.mapping):[])
</script>
<template>
  <section class="semantic-guide" aria-label="剧本输出业务逻辑说明" :aria-busy="loading">
    <header><h3>这份输出如何变成剧本与素材需求</h3><p>平台规则只读；返回字段与来源映射可配置。以下不是某次生成结果，也不会调用模型。</p></header>
    <ol class="flow" aria-label="剧本配置与使用流程">
      <li><strong>1 · 创作要求</strong><span>提示词策略 + 本次输入</span></li>
      <li><strong>2 · 返回格式</strong><span>公共响应结构</span></li>
      <li><strong>3 · 对应业务含义</strong><span>本输出版本的语义映射</span></li>
      <li><strong>4 · 解析与采用</strong><span>平台检查 + 本轮采用流程</span></li>
      <li><strong>5 · 下游制作</strong><span>Bible、分镜与素材需求</span></li>
    </ol>
    <details class="explanation" open>
      <summary>哪些可以配置，哪些由平台实现？</summary>
      <dl>
        <div><dt>可以配置 · 写什么</dt><dd>在<strong>提示词策略</strong>填写创作与语言要求；集数、时长等本次要求由执行实例／配方的实际输入提供。业务输出定义本身不填写剧情。</dd></div>
        <div><dt>可以配置 · 返回什么、如何对应</dt><dd>在<strong>公共响应结构</strong>维护字段、层级、类型与必填要求；在<strong>本页的语义映射</strong>声明哪个返回字段对应实体名称、主图、变体、镜头或对白。不要求模型的字段名与平台业务路径相同。</dd></div>
        <div><dt>平台固定 · 如何理解与使用</dt><dd>剧本生成／结构化的原生请求会携带所选响应结构和语义映射，以及平台的业务含义与引用规则；模型返回后再解析和检查。关联依靠稳定 key，不靠显示名称猜测，不能在策略里重新定义这些关联规则。</dd><dd>平台检查结构、重复标识、实体／变体引用和已声明的对白时段等正确性。模型返回字段的必填要求在响应结构配置，平台关联所需的稳定标识仍须满足业务合同；可选内容不凭空补齐。通过解析不等于已采用，更不等于创作质量已通过人工审核。</dd></div>
      </dl>
    </details>
    <details class="explanation">
      <summary>Canonical、主图、变体和对白如何进入下游？</summary>
      <p><strong>统一名称（Canonical）</strong>来自映射后的实体名称，不是另一次翻译。实体 key 标识身份，主图／变体的素材引用名区分参考素材；变体仍属于原实体，不是新人物。</p>
      <p><strong>本镜素材需求</strong>用实体 key 和变体 key 指向声明，变体 key 为 null 表示主图。未知实体或变体不会按同名匹配，也不会静默换成主图。</p>
      <p>结果通过检查并按流程采用后，可用于 Bible 设定、剧集和分镜。声明主图／变体不代表图片已生成；实际素材还需生成或选择，下游执行实例／配方按来源配置传入，不会因为字段存在就全部自动加入视频提示词。</p>
      <p><strong>对白原文、本句发声语言、演绎方式、说话人和时间</strong>分别保留。同一人物可以逐句切换语言；缺少语言不从项目或导演输出语言推断。演绎是否翻译、如何组装提示词由相应策略和输入映射决定；语义映射不代替翻译或改写对白。</p>
    </details>
    <p v-if="loading" role="status">正在读取平台业务字段目录…</p>
    <p v-else-if="error" role="alert">{{error}} <button type="button" @click="load">重读业务说明</button></p>
    <template v-else-if="catalog">
      <p class="scope">{{draft?'当前草稿映射':'本版本保存的映射'}} → 当前平台业务目录 <code>{{catalog.key}}</code>。下列路径说明配置关系，不证明实际返回了该字段；不重新解析历史任务。</p>
      <p v-if="!groups.length" role="alert">目录尚未提供可展示的业务字段；不会按名称补造关联。<button type="button" @click="load">重读业务说明</button></p>
      <details v-for="group in groups" :key="group.key" class="field-group">
        <summary>{{group.label}} · 返回字段与业务含义（{{group.rows.length}}项）</summary>
        <p>“响应”指模型返回的完整内容；[*] 表示列表条目。整体取值会沿原层级读取；固定值和按 key 查找不会伪装成已知返回路径。</p>
        <div class="field-rows">
          <article v-for="row in group.rows" :key="row.path">
            <div><strong>{{row.label}}</strong><code>{{row.path}}</code></div>
            <div><span>配置来源</span><code>{{row.source}}</code></div>
          </article>
        </div>
      </details>
    </template>
    <p class="scope">修改后的输出版本需由执行实例明确引用才会用于新请求；发布不会替换历史冻结配置。实际生成内容请到项目版本或任务详情查看，不能用这里的定义代替产物验收。</p>
  </section>
</template>
<style scoped>
.semantic-guide{display:grid;gap:var(--config-space-md);min-width:0;padding:var(--config-space-lg);border:1px solid var(--border);border-radius:var(--config-radius);background:var(--bg-soft);font:var(--config-body-size)/1.65 var(--font)}
.semantic-guide h3,.semantic-guide p{margin:0}.semantic-guide h3{font-size:var(--config-section-size)}.semantic-guide header,.explanation,.field-group{display:grid;gap:var(--config-space-sm)}.semantic-guide p,.semantic-guide dd,.semantic-guide code{overflow-wrap:anywhere}.semantic-guide code{white-space:normal;font-size:var(--config-help-size)}.scope,.semantic-guide header p{color:var(--text-secondary)}
.flow{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:var(--config-space-sm);padding:0;margin:0;list-style:none}.flow li{display:grid;align-content:start;gap:var(--config-space-xs);border-inline-start:2px solid var(--accent);padding-inline-start:var(--config-space-sm);min-width:0}.flow span{color:var(--text-secondary);font-size:var(--config-help-size)}
.semantic-guide summary{cursor:pointer;min-height:var(--config-control-height);align-content:center;font-weight:600}.semantic-guide details{border-top:1px solid var(--border);padding-top:var(--config-space-sm)}.semantic-guide dl{display:grid;gap:var(--config-space-md);margin:var(--config-space-sm) 0}.semantic-guide dt{font-weight:600}.semantic-guide dd{margin:var(--config-space-xs) 0 0;color:var(--text-secondary)}
.field-rows article{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:var(--config-space-md);padding:var(--config-space-md) 0;border-bottom:1px solid var(--border)}.field-rows article>div{display:grid;gap:var(--config-space-xs);min-width:0}.field-rows article span{font-size:var(--config-help-size);color:var(--text-secondary)}.semantic-guide [role=alert]{color:var(--warning)}.semantic-guide button{min-height:var(--config-control-height)}.semantic-guide :focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media(max-width:640px){.semantic-guide{padding:var(--config-space-md);--config-control-height:44px}.flow{grid-template-columns:1fr}.field-rows article{grid-template-columns:1fr}}
</style>
