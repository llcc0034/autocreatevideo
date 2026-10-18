<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { resourceSmokeApi, type ResourceSmokeTarget, type ResourceSmokeContext, type ResourceSmokeSelection } from '@/services/executionResourceSmoke'
import type { ExecutionSelectionOptions } from '@/services/executionRegistry'
import type { PromptProfileVersion } from '@/services/platformTypes'
import ExecutionSmokeCreateForm from './ExecutionSmokeCreateForm.vue'
import BaseModal from './BaseModal.vue'
import BusinessOutputManager from './BusinessOutputManager.vue'
import type { BusinessOutputVersion } from '@/services/businessOutputs'
import { readAuthSession } from '@/services/authRefresh'
const props = defineProps<{ target: ResourceSmokeTarget }>()
const emit = defineEmits<{ busy: [value: boolean]; dirty: [value: boolean]; locked: [value: boolean]; created: [preparationId: string] }>()
const options = ref<ExecutionSelectionOptions|null>(null), profiles = ref<PromptProfileVersion[]>([])
const operation = ref(''), mode = ref(''), promptMode = ref(''), profileId = ref('')
const outputsOpen = ref(false), selectedOutput = ref<BusinessOutputVersion|null>(null)
const outputParsers = computed(() => options.value?.target?.operations.find(row => row.value === operation.value)?.output_parser_keys ?? [])
watch(operation, () => { selectedOutput.value = null; outputsOpen.value = false })
function chooseOutput(row: BusinessOutputVersion) {
  if (busy.value || context.value || row.status !== 'published' || !outputParsers.value.includes(row.parser_key)) return
  selectedOutput.value = row; outputsOpen.value = false
}
const context = ref<ResourceSmokeContext|null>(null), loading = ref(false), childBusy = ref(false), childDirty = ref(false), error = ref('')
const busy = computed(() => loading.value || childBusy.value)
const childLocked = ref(false), selectionDiscard = ref<{ fingerprint: string; targetLabel: string } | null>(null)
const selectionFingerprint = () => JSON.stringify([props.target, context.value, childDirty.value, busy.value, childLocked.value,
  typeof localStorage === 'undefined' ? '' : readAuthSession(localStorage).userId])
watch(selectionFingerprint, () => { selectionDiscard.value = null }, { flush: 'sync' })
watch(() => childLocked.value || !!selectionDiscard.value, value => emit('locked', value), { flush: 'sync' })
const dirty = computed(() => childDirty.value || !!operation.value || !!promptMode.value || !!mode.value || !!profileId.value)
const eligibleProfiles = computed(() => profiles.value.filter(row => row.status === 'published'
  && row.modelFamily === options.value?.target?.model_family && (!row.mode || row.mode === mode.value)))
watch(busy, value => emit('busy', value)); watch(dirty, value => emit('dirty', value))
let serial = 0
let disposed = false
onBeforeUnmount(() => { disposed = true; serial++; selectionDiscard.value = null })
async function load() {
  if (busy.value || context.value) return
  const request = ++serial; loading.value = true; error.value = ''
  try {
    const [choices, rows] = await Promise.all([resourceSmokeApi.choices(props.target), platformApi.listPromptProfileVersions()])
    if (request !== serial) return
    if (!choices.target) throw new Error('服务端未返回资源能力目录')
    options.value = choices; profiles.value = rows
  } catch (cause) { if (request === serial) error.value = apiErrorSummary(cause) }
  finally { if (request === serial) loading.value = false }
}
function choose() {
  if (busy.value || context.value || !options.value?.target) return
  error.value = ''
  if (!options.value.target.operations.some(row => row.value === operation.value)
    || !options.value.prompt_modes.some(row => row.value === promptMode.value)
    || (options.value.target.modes.length ? !options.value.target.modes.some(row => row.value === mode.value) : mode.value !== '')
    || (promptMode.value === 'profile' && !eligibleProfiles.value.some(row => row.id === profileId.value))
    || (outputParsers.value.length && (!selectedOutput.value || !outputParsers.value.includes(selectedOutput.value.parser_key)))) {
    error.value = '请选择合同声明的能力、模式、提示词来源与所需的确切输出版本'; return
  }
  context.value = { target: {...props.target}, selection: { operation: operation.value, mode: mode.value,
    ...(selectedOutput.value ? {output_version_id: selectedOutput.value.id} : {}),
    prompt_mode: promptMode.value as ResourceSmokeSelection['prompt_mode'], prompt_profile_version_id: promptMode.value === 'profile' ? profileId.value : null } }
  childDirty.value = false
}
function changeSelection() {
  if (disposed || busy.value || childLocked.value || selectionDiscard.value || !context.value) return
  if (childDirty.value) {
    selectionDiscard.value = { fingerprint: selectionFingerprint(), targetLabel: `${context.value.target.kind} · ${context.value.target.id} · ${context.value.selection.operation}` }
    return
  }
  context.value = null; childDirty.value = false
}
function confirmSelectionDiscard() {
  const pending = selectionDiscard.value; selectionDiscard.value = null
  if (!pending || disposed || pending.fingerprint !== selectionFingerprint()) return
  context.value = null; childDirty.value = false
}
onMounted(() => { void load() })
</script>
<template>
  <section class="resource-smoke-form" :aria-busy="busy">
    <p>选择本次验收的能力和提示词来源，再按实际合同填写测试输入。不会自动挑选旧策略绑定，也不会自动填入业务样例。</p>
    <p v-if="loading" role="status">正在读取基础资源能力…</p><p v-if="error" role="alert" class="platform-alert danger">{{ error }}</p>
    <button type="button" class="button ghost small" :disabled="busy || !!context" @click="load">刷新能力与策略目录</button>
    <fieldset v-if="options?.target" :disabled="busy || !!context">
      <label>执行能力<select v-model="operation"><option value="">请选择能力</option><option v-for="row in options.target.operations" :key="row.value" :value="row.value">{{ row.label }} · {{ row.value }}</option></select></label>
      <label v-if="options.target.modes.length">执行模式<select v-model="mode"><option value="">请选择模式</option><option v-for="row in options.target.modes" :key="row.value" :value="row.value">{{ row.label }}</option></select></label>
      <label>提示词来源<select v-model="promptMode"><option value="">请选择来源</option><option v-for="row in options.prompt_modes" :key="row.value" :value="row.value">{{ row.label }}</option></select></label>
      <label v-if="promptMode==='profile'">已发布策略<select v-model="profileId"><option value="">请选择精确版本</option><option v-for="row in eligibleProfiles" :key="row.id" :value="row.id">{{ row.name }} · v{{ row.version }}</option></select></label>
      <p v-if="promptMode==='profile'&&!options.target.model_family">模型合同尚未声明模型族，无法匹配策略；请先补齐模型合同。</p>
      <p v-else-if="promptMode==='profile'&&!eligibleProfiles.length">当前模型族和模式没有可选的已发布策略。</p>
      <div v-if="outputParsers.length"><strong>确切业务输出版本</strong><p>{{ selectedOutput ? `${selectedOutput.name} · v${selectedOutput.version}` : '尚未选择。输出结构随本次合同冻结。' }}</p><button type="button" class="button ghost" @click="outputsOpen=true">选择／管理输出版本</button></div>
      <button type="button" class="button ghost" @click="choose">按此选择读取输入合同</button>
    </fieldset>
    <BusinessOutputManager v-if="outputsOpen" choose :supported-parser-keys="outputParsers" @select="chooseOutput" @close="outputsOpen=false"/>
    <p v-if="context">下方输入固定使用上面已选的能力与提示词来源；更换选择需重新读取合同。</p>
    <button v-if="context" type="button" class="button ghost" :disabled="busy || childLocked || !!selectionDiscard" @click="changeSelection">更换能力与提示词来源</button>
    <ExecutionSmokeCreateForm v-if="context" :key="JSON.stringify(context)" :resource="context" @busy="childBusy=$event" @dirty="childDirty=$event" @locked="childLocked=$event" @created="emit('created',$event)"/>
    <BaseModal :open="!!selectionDiscard" title="更换能力与提示词来源？" eyebrow="仅未提交输入 · 不创建或取消任务" width="600px" @close="selectionDiscard=null">
      <p class="selection-target">{{ selectionDiscard?.targetLabel }}</p>
      <p>将清空下方未提交的测试输入、素材选择和调用授权，返回上方重新选择。当前能力与来源选项会保留供修改，不自动读取合同或调用模型。</p>
      <template #footer><button type="button" class="button ghost" @click="selectionDiscard=null">返回继续填写</button><button type="button" class="button danger" :disabled="busy || childLocked" @click="confirmSelectionDiscard">确认清空并更换选择</button></template>
    </BaseModal>
  </section>
</template>
<style scoped>
.resource-smoke-form,.resource-smoke-form fieldset{display:grid;gap:12px;min-width:0}.resource-smoke-form label{display:grid;gap:6px}.resource-smoke-form p{font-size:12px;line-height:1.7}
.selection-target{overflow-wrap:anywhere}
</style>
