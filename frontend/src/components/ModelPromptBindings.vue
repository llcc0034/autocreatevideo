<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { AlertTriangle, RefreshCw } from 'lucide-vue-next'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { modelPromptSettingsRoute, promptBindingModelFamily, promptBindingsForModel } from '@/services/modelPromptBindings'
import { modeLabel, operationLabel } from '@/services/presentationLabels'
import type { ModelDeployment, ModelProfileVersion, PromptProfileBinding } from '@/services/platformTypes'

const props = defineProps<{ profile: ModelProfileVersion; deployment?: ModelDeployment | null }>()
const loading = ref(false)
const error = ref('')
const bindings = ref<PromptProfileBinding[]>([])
const rows = computed(() => promptBindingsForModel(bindings.value, props.profile))
let requestSerial = 0
const refresh = async () => {
  const serial = ++requestSerial
  const profile = props.profile
  bindings.value = []; error.value = ''; loading.value = true
  try {
    const family = promptBindingModelFamily(profile)
    const result = await platformApi.listPromptProfileBindings(family
      ? { model_family: family } : { model_profile_version: profile.id })
    if (serial === requestSerial) bindings.value = result
  } catch (cause) {
    if (serial === requestSerial) error.value = apiErrorSummary(cause, '提示词关联读取失败')
  } finally { if (serial === requestSerial) loading.value = false }
}
watch(() => props.profile.id, refresh, { immediate: true })
onBeforeUnmount(() => { requestSerial += 1 })
</script>

<template>
  <section class="model-prompt-bindings" aria-label="模型提示词策略关联" :aria-busy="loading">
    <div class="binding-context">
      <strong>{{ deployment ? `${deployment.name || deployment.key} → ` : '' }}{{ profile.profileKey }} v{{ profile.version }} → 提示词策略</strong>
      <p>以下为该模型版本的绑定记录；同版本运行实例共享此关联。具体运行仍按操作能力、模式和所选 Binding 校验并冻结策略版本。</p>
      <div class="platform-row-actions">
        <RouterLink class="button primary small" :to="modelPromptSettingsRoute(profile.id, '', deployment?.id)">管理提示词关联</RouterLink>
        <RouterLink v-if="deployment?.providerDriver==='comfyui'" class="button ghost small" :to="{path:'/settings/acceptance-smoke',query:{subject:'workflow',deployment_id:deployment.id}}">查看该实例关联工作流</RouterLink>
        <button class="button ghost small" :disabled="loading" @click="refresh"><RefreshCw :size="13"/>重新读取</button>
      </div>
    </div>
    <div v-if="error" class="platform-alert danger" role="alert"><AlertTriangle :size="16"/><p>{{ error }}</p></div>
    <p v-else-if="loading" class="platform-loading">正在读取提示词关联…</p>
    <template v-else>
      <p v-if="!promptBindingModelFamily(profile)" class="platform-alert warning">该版本未声明模型族，仅展示精确绑定；无法判断模型族通用绑定。</p>
      <p v-if="!rows.length" class="platform-empty">该模型版本尚无提示词绑定，请进入“管理提示词关联”选择已发布策略并新增绑定。</p>
      <div v-else class="binding-table-scroll">
        <table>
          <thead><tr><th>绑定 / 适用范围</th><th>提示词策略版本</th><th>操作能力 / 模式</th><th>状态</th><th>操作</th></tr></thead>
          <tbody><tr v-for="row in rows" :key="row.id">
            <td><strong>{{ row.name }}</strong><small>{{ row.modelProfileVersion ? '精确模型版本绑定' : '模型族通用绑定' }} · 优先级 {{ row.priority }}</small></td>
            <td><strong>{{ row.promptProfileName || row.promptProfileKey }}</strong><small>{{ row.promptProfileVersionNumber ? `v${row.promptProfileVersionNumber}` : '版本号未返回' }} · {{ row.promptProfileKey }}</small><small>合同 SHA {{ row.promptProfileContractSha256.slice(0,12) || '未返回' }}</small></td>
            <td>{{ operationLabel(row.operation) }}<small>{{ row.mode ? modeLabel(row.mode) : '通用模式' }}</small></td>
            <td><PlatformStatusPill :status="row.isActive?'active':'inactive'"/><small>策略：{{ row.promptProfileStatus || '状态未返回' }}</small></td>
            <td><RouterLink class="button ghost tiny" :to="modelPromptSettingsRoute(profile.id,row.promptProfileVersion,deployment?.id)">查看策略与绑定</RouterLink></td>
          </tr></tbody>
        </table>
      </div>
    </template>
  </section>
</template>

<style scoped>
.model-prompt-bindings{display:grid;gap:14px;min-width:0}.binding-context{display:grid;gap:10px}.binding-context p{margin:0;color:#9ba8b5;line-height:1.6}.binding-table-scroll{overflow:auto}table{width:100%;border-collapse:collapse;text-align:left}th,td{padding:12px;vertical-align:top;border-bottom:1px solid #29333d}th{font-size:12px;color:#9ba8b5}td small{display:block;margin-top:5px;color:#92a1af;overflow-wrap:anywhere}td strong{font-size:13px}td:last-child{min-width:130px}
</style>
