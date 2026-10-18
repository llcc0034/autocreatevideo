<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { Server, Boxes, GitBranch, Ellipsis } from 'lucide-vue-next'
import ConfigurationRelationButton from './ConfigurationRelationButton.vue'
import PlatformStatusPill from './PlatformStatusPill.vue'
import { relationOverviewUrl } from '@/services/registryRelations'
import { workflowModelSettingsRoute, workflowExecutionSettingsRoute, workflowExecutionsRoute, workflowProjectorSettingsRoute, workflowRelationItems } from '@/services/workflowConfigurationLinks'
import { operationLabel, modeLabel } from '@/services/presentationLabels'
import type { WorkflowTemplateVersion } from '@/services/platformTypes'

const props = defineProps<{ workflow: WorkflowTemplateVersion; compact?: boolean }>()
const emit = defineEmits<{ navigate: [] }>()
const links = computed(() => props.workflow.configurationLinks)
const relationItems = computed(() => links.value ? workflowRelationItems(props.workflow.id, links.value) : [])
const relationIcons = { model: Server, execution: Boxes, projector: GitBranch }
const navigate = () => emit('navigate')
</script>

<template>
  <div v-if="compact && links" class="workflow-links-compact" aria-label="工作流关联配置">
    <ConfigurationRelationButton
      v-for="item in relationItems" :key="item.key"
      :label="item.label" :name="item.name" :status="item.status" :tone="item.tone"
      :description="item.description" :to="item.to || relationOverviewUrl('workflow',workflow.id)" :icon="relationIcons[item.key]"
      @activate="navigate"
    />
    <RouterLink class="button ghost tiny relation-more" :to="relationOverviewUrl('workflow',workflow.id)" :aria-label="`${workflow.templateName || workflow.templateSlug || '工作流'} · 更多关联`" @click="navigate">
      <Ellipsis :size="15" aria-hidden="true"/>更多关联
      <span v-if="links.executionInstances.total > links.executionInstances.activeTotal" class="relation-extra-count">其他版本 {{ links.executionInstances.total-links.executionInstances.activeTotal }}</span>
    </RouterLink>
  </div>
  <div v-else-if="workflow.configurationLinks" class="workflow-links" aria-label="工作流关联配置">
    <section>
      <strong>模型部署</strong>
      <template v-if="workflow.configurationLinks.validatedDeployment">
        <RouterLink :to="workflowModelSettingsRoute(workflow.id, workflow.configurationLinks.validatedDeployment.modelProfileVersionId, workflow.configurationLinks.validatedDeployment.id)" @click="emit('navigate')">{{ workflow.configurationLinks.validatedDeployment.name }}</RouterLink>
        <small>已校验目标 <PlatformStatusPill :status="workflow.configurationLinks.validatedDeployment.status"/></small>
        <small v-if="workflow.configurationLinks.validatedDeployment.modelProfileVersionId!==workflow.configurationLinks.model?.id" class="warning">校验目标与绑定模型版本不一致，请查看证据。</small>
      </template>
      <small v-else>尚未绑定校验部署</small>
      <RouterLink v-if="workflow.configurationLinks.model" :to="workflowModelSettingsRoute(workflow.id, workflow.configurationLinks.model.id)" @click="emit('navigate')">{{ workflow.configurationLinks.model.key }} v{{ workflow.configurationLinks.model.version }} · 查看部署（{{ workflow.configurationLinks.model.instanceCount }}）</RouterLink>
      <small v-else class="warning">尚未绑定精确模型版本</small>
      <small v-if="!compact">同模型的其他部署仅供管理，不代表已经通过此工作流验收。</small>
    </section>
    <section>
      <strong>执行实例 · 启用 {{ workflow.configurationLinks.executionInstances.activeTotal }} / 全部 {{ workflow.configurationLinks.executionInstances.total }}</strong>
      <div v-for="execution in workflow.configurationLinks.executionInstances.preview" :key="execution.id" class="prompt-link">
        <RouterLink :to="workflowExecutionSettingsRoute(workflow.id, execution.id)" @click="emit('navigate')">{{ execution.name }} v{{ execution.version }}</RouterLink>
        <small>{{ operationLabel(execution.operation) }}<template v-if="execution.mode"> · {{ modeLabel(execution.mode) }}</template> · {{ execution.promptMode==='profile'?'固定策略组装':execution.promptMode==='manual'?'人工完整提示词':'无需提示词' }}</small>
      </div>
      <small v-if="!workflow.configurationLinks.executionInstances.total">尚无执行实例引用此工作流，请在独立执行实例分区创建执行版本。</small>
      <small v-else-if="!workflow.configurationLinks.executionInstances.activeTotal">没有同时已发布且实例启用的版本。</small>
      <small v-if="workflow.configurationLinks.executionInstances.activeTotal > workflow.configurationLinks.executionInstances.preview.length">仅预览 {{ workflow.configurationLinks.executionInstances.preview.length }} 个启用版本，更多请查看全部。</small>
      <small v-if="workflow.configurationLinks.executionInstances.total > workflow.configurationLinks.executionInstances.activeTotal">其他 {{ workflow.configurationLinks.executionInstances.total-workflow.configurationLinks.executionInstances.activeTotal }} 个草稿、退役或停用版本收在全部关联中。</small>
      <RouterLink :to="workflowExecutionsRoute(workflow.id)" @click="emit('navigate')">查看全部 {{ workflow.configurationLinks.executionInstances.total }} 个执行版本</RouterLink>
      <small>具体提示词策略、参数和组合烟测均在执行实例中查看，不按模型族推测。</small>
    </section>
    <section>
      <strong>Workflow Projector</strong>
      <template v-if="workflow.configurationLinks.projector.state==='linked'">
        <RouterLink :to="workflowProjectorSettingsRoute(workflow.id, workflow.configurationLinks.projector.versionId!)" @click="emit('navigate')">{{ workflow.configurationLinks.projector.name }} v{{ workflow.configurationLinks.projector.version }} · 查看配置</RouterLink>
        <small>工作流冻结版本<template v-if="!workflow.configurationLinks.projector.isCurrent"> · 非当前版</template><template v-if="!workflow.configurationLinks.projector.isActive"> · 方案族停用</template><template v-if="workflow.configurationLinks.projector.status==='retired'"> · 已退役</template></small>
        <small v-if="!compact">{{ workflow.configurationLinks.projector.key }} · SHA {{ workflow.configurationLinks.projector.definitionSha256.slice(0,12) }}。调整须创建后继草稿并重新生成工作流，不会修改此版本。</small>
      </template>
      <small v-else-if="workflow.configurationLinks.projector.state==='invalid'" class="warning">冻结引用缺失或不一致，请查看合同证据；不会替换为当前版本。</small>
      <small v-else>此工作流未记录 Projector 引用</small>
    </section>
  </div>
  <p v-else class="warning">关联配置尚未读取，请刷新工作流列表或详情。</p>
</template>

<style scoped>
.workflow-links-compact {
  display: grid;
  gap: 5px;
  width: clamp(320px, 27vw, 430px);
  font-size: 11px;
  line-height: 1.5;
  white-space: normal;
}
.relation-more:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.relation-more { justify-self: start; display: inline-flex; align-items: center; gap: 6px; min-height: 36px; }
.relation-extra-count { padding-left: 7px; border-left: 1px solid var(--border, #29323a); color: var(--text-muted, #94a3b8); font-size: 10px; }
.relation-legend { margin: 0 0 12px; color: var(--text-muted, #94a3b8); font-size: 12px; line-height: 1.5; }
.workflow-links{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:12px 0;min-width:0}.workflow-links section{display:grid;align-content:start;gap:6px;border:1px solid var(--border-color,#29323a);border-radius:8px;padding:12px;min-width:0}.workflow-links strong{font-size:12px}.workflow-links small{display:block;font-size:11px;color:var(--text-muted,#94a3b8);line-height:1.5}.workflow-links a{color:var(--accent-color,#e8ab60);font-size:12px;overflow-wrap:anywhere;text-decoration:underline;text-underline-offset:3px}.workflow-links a:focus-visible{outline:2px solid currentColor;outline-offset:3px}.warning{color:#e8ab60;font-size:12px;white-space:normal}.prompt-link{display:grid;gap:2px}@media(max-width:900px){.workflow-links{grid-template-columns:1fr}}
</style>
