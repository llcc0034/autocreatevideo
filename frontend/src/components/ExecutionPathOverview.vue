<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ExecutionContract, ExecutionVersionDetail } from '@/services/executionRegistry'
import { executionPath, contractRecord } from '@/services/executionPath'
import { mediaKindLabel } from '@/services/presentationLabels'
import ExecutionParameterContract from './ExecutionParameterContract.vue'
import ExecutionParameterMappings from './ExecutionParameterMappings.vue'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import RegistryResourceExplorer from './RegistryResourceExplorer.vue'

const props = defineProps<{ version: ExecutionVersionDetail; contract: ExecutionContract }>()
const selected = ref<'inputs' | 'invocation' | 'output'>('inputs')
const state = computed(() => {
  try { return { path: executionPath(props.version, props.contract), error: '' } }
  catch (cause) { return { path: null, error: cause instanceof Error ? cause.message : '链路读取失败' } }
})
const path = computed(() => state.value.path)
const targets = (input: Record<string, unknown>) => (input.targets as unknown[]).map(value => contractRecord(value))
const steps = [
  { key: 'inputs', title: '输入准备', description: '业务字段、参考素材、文本处理与运行参数' },
  { key: 'invocation', title: '模型能力调用', description: '精确模型部署 · 按需下钻实现方式' },
  { key: 'output', title: '输出', description: '解析合同、输出结构与下游引用' },
] as const
</script>

<template>
  <RegistryResourceExplorer label="执行链路" v-slot="{ inspect }">
  <div class="execution-path-overview">
    <p v-if="state.error" class="platform-alert danger" role="alert">{{ state.error }}</p>
    <template v-else-if="path">
      <p class="path-basis">{{ contract.frozen ? '此精确版本的冻结配置' : '此修订的已保存草稿' }} · 修订 {{ contract.revision }}。此图表示配置关系，不是某次任务的实际进度或调用结果。</p>
      <ol class="path-stages" aria-label="执行实例配置路径">
        <li v-for="(step,index) in steps" :key="step.key">
          <button type="button" :aria-pressed="selected===step.key" :aria-controls="`execution-path-${version.id}`" @click="selected=step.key">
            <strong>{{ index+1 }} · {{ step.title }}</strong><small>{{ step.description }}</small>
          </button>
          <span v-if="index<steps.length-1" class="path-arrow" aria-hidden="true">→</span>
        </li>
      </ol>
      <section :id="`execution-path-${version.id}`" class="path-detail" :aria-label="steps.find(step=>step.key===selected)?.title">
        <template v-if="selected==='inputs'">
          <h3>本次调用接收什么</h3>
          <p>这里只定义输入合同；具体值由配方上游、已有素材、上传或人工制作请求提供。项目 Bible 名称与引用事实原样传递，不随创作语言翻译。</p>
          <p v-if="path.inputs===null" role="status">未记录输入声明，不能判定为没有输入。</p>
          <p v-else-if="!path.inputs.length">此合同明确没有实例输入。</p>
          <div v-else class="path-table-wrap"><table><thead><tr><th>输入字段 / 类型</th><th>要求</th><th>交给谁处理</th><th>定义与来源</th></tr></thead><tbody>
            <tr v-for="input in path.inputs" :key="String(input.key)">
              <td><strong>{{ input.key }}</strong><small>{{ input.kind }}</small></td>
              <td>{{ input.required ? '必需' : '可选' }}<small>数量 {{ input.min_items }} – {{ input.max_items }}</small></td>
              <td><ul><li v-for="(target,index) in targets(input)" :key="index">{{ target?.target_kind }} → <code>{{ target?.target_key }}</code><small v-if="target?.is_primary">主要输入</small></li></ul></td>
              <td><button v-if="input.data_structure_version_id" type="button" class="button ghost small" @click="inspect('structure',String(input.data_structure_version_id),$event)">查看确切输入结构</button><details><summary>素材来源与使用规则</summary><ReadonlyDataValue :value="{source:input.media_source_policy ?? '合同未指定媒体来源策略',usage:input.semantic_contract}" label="已保存规则"/></details></td>
            </tr>
          </tbody></table></div>
          <h3>文本输入如何形成</h3>
          <template v-if="path.prompt?.mode==='profile'">
            <p>字段先按所选提示词策略处理，再组装为模型的文本输入；提示词不是与输入并列的另一条主流程。</p>
            <button v-if="path.profileId" type="button" class="button ghost small" @click="inspect('prompt',path.profileId,$event)">查看精确提示词策略与字段依赖</button>
          </template>
          <p v-else-if="path.prompt?.mode==='manual'">此版本直接接收完整文本通道，不引用提示词策略，也不会自动使用辅助 AI。</p>
          <p v-else-if="path.prompt?.mode==='none'">此版本明确不需要提示词处理。</p>
          <p v-else role="status">提示词处理方式未记录。</p>
          <ul v-if="path.auxiliaries?.length"><li v-for="auxiliary in path.auxiliaries" :key="auxiliary.purpose">{{ auxiliary.purpose }} → <button type="button" class="button ghost small" @click="inspect('execution',auxiliary.id,$event)">查看辅助执行实例</button><small>按策略需要调用，不代表每次必定执行。</small></li></ul>
          <p v-else-if="path.auxiliaries">此版本未配置辅助执行实例。</p>
          <p v-else>辅助实例声明未记录。</p>
          <details><summary>文本输入通道与冻结处理合同</summary><ReadonlyDataValue :value="path.prompt" label="提示词通道"/><ReadonlyDataValue :value="path.intf.profile_inputs ?? null" label="策略输入要求（未声明时为空）"/></details>
          <ExecutionParameterContract :interface="path.intf"/>
        </template>
        <template v-else-if="selected==='invocation'">
          <h3>{{ version.operation }}<small>{{ version.mode || '未声明模式' }}</small></h3>
          <p>执行实例固定模型能力与部署；下面的工作流是其实现方式，不是另一条并列的业务调用。</p>
          <div class="path-resource-links"><button v-if="path.modelId" type="button" class="button ghost small" @click="inspect('model',path.modelId,$event)">模型能力版本</button><button v-if="path.deploymentId" type="button" class="button ghost small" @click="inspect('deployment',path.deploymentId,$event)">模型部署 / Provider</button></div>
          <section v-if="path.workflowId" class="path-implementation"><h4>实现方式：工作流</h4><p>节点、内部模型文件与接线由工作流定义；上层使用暴露的输入、参数和输出。</p><button type="button" class="button ghost small" @click="inspect('workflow',path.workflowId,$event)">下钻此工作流的节点与输入输出</button><button v-if="path.projectorId" type="button" class="button ghost small" @click="inspect('projector',path.projectorId,$event)">查看引用的 Projector</button></section>
          <p v-else-if="path.configuration.workflow_version_id===null">实现方式：部署对应的直接接口；此版本不使用工作流。</p>
          <p v-else role="status">实现方式信息不完整，不推断工作流。</p>
          <ExecutionParameterMappings :version-id="contract.version_id" :revision="contract.revision" :sha256="contract.sha256"/>
          <details><summary>精确资源身份与合同摘要</summary><ReadonlyDataValue :value="path.resources" label="资源身份（不是实时状态）"/></details>
        </template>
        <template v-else>
          <h3>模型结果如何交给下游</h3><p>输出按此版本声明解析与校验，再交给业务步骤或素材库。图片、视频、音频也有输出类型和媒体合同，不等同于可任意配置的文本结构。</p>
          <dl class="path-output"><div><dt>输出类型</dt><dd>{{ path.outputKind ? mediaKindLabel(path.outputKind) : '合同未声明媒体类型；查看输出协议' }}</dd></div><div><dt>解析方式</dt><dd>{{ path.outputAdapter || '未记录，不推断解析器' }}</dd></div><div v-if="path.primaryOutput?.node_id"><dt>工作流主输出</dt><dd>节点 {{ path.primaryOutput.node_id }} · {{ path.primaryOutput.required===true?'必需':path.primaryOutput.required===false?'可选':'必要性未记录' }}</dd></div></dl>
          <p v-if="!path.output" role="status">输出合同未记录，不能推断为成功或无输出。</p>
          <ReadonlyDataValue v-else :value="path.output" label="已声明输出与解析合同"/>
          <ReadonlyDataValue v-if="path.businessOutput" :value="path.businessOutput" label="冻结业务输出结构与字段"/>
          <button v-if="path.outputId" type="button" class="button ghost small" @click="inspect('output',path.outputId,$event)">查看确切业务输出版本</button>
          <button type="button" class="button ghost small" @click="inspect('execution',version.id,$event)">查看此版本的引用与使用方</button>
        </template>
      </section>
    </template>
  </div>
  </RegistryResourceExplorer>
</template>

<style scoped>
.execution-path-overview{display:grid;gap:var(--config-space-lg);min-width:0;font-size:var(--config-body-size);line-height:1.65}.execution-path-overview p{margin:0;color:var(--text-secondary)}.path-basis{font-size:var(--config-help-size)}.path-output{display:grid;gap:var(--config-space-sm);margin:0}.path-output>div{display:grid;grid-template-columns:minmax(100px,1fr) minmax(0,3fr);gap:var(--config-space-sm)}.path-output dd{margin:0;overflow-wrap:anywhere}.path-stages{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:var(--config-space-xl);padding:0;margin:0;list-style:none}.path-stages li{position:relative;min-width:0}.path-stages button{width:100%;height:100%;min-height:var(--config-control-height);padding:var(--config-space-md);text-align:left;border:1px solid var(--border);border-radius:var(--config-radius);background:var(--bg-secondary,#111820);color:inherit;cursor:pointer}.path-stages button[aria-pressed=true]{border-color:var(--accent);background:rgba(230,167,90,.08)}.path-stages small,.path-detail small{display:block;color:var(--text-secondary);font-size:var(--config-help-size)}.path-arrow{position:absolute;right:-20px;top:35%}.path-detail{border-top:1px solid var(--border);padding-top:var(--config-space-lg);display:grid;gap:var(--config-space-md);min-width:0}.path-detail h3,.path-detail h4{font-size:var(--config-section-size);font-weight:600;margin:0}.path-detail ul{padding-left:var(--config-space-lg);margin:0}.path-detail code,.path-detail small,.path-detail td{overflow-wrap:anywhere}.path-table-wrap{overflow:auto}.path-table-wrap table{width:100%;border-collapse:collapse;text-align:left}.path-table-wrap th,.path-table-wrap td{border-bottom:1px solid var(--border);padding:var(--config-space-sm);vertical-align:top;min-width:110px}.path-resource-links{display:flex;gap:var(--config-space-sm);flex-wrap:wrap}.path-implementation{border-left:2px solid var(--border);padding-left:var(--config-space-lg);display:grid;gap:var(--config-space-sm)}.path-detail summary{cursor:pointer;min-height:var(--config-control-height);align-content:center}.path-stages button:focus-visible,.path-detail summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:640px){.execution-path-overview{--config-control-height:44px}.path-stages{grid-template-columns:1fr}.path-arrow{right:50%;top:auto;bottom:-23px;transform:rotate(90deg)}.path-table-wrap th,.path-table-wrap td{min-width:100px}}
</style>
