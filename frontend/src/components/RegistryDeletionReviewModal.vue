<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { AlertTriangle, CheckSquare, FileClock, Link2, LoaderCircle, RefreshCw, ShieldAlert, Trash2 } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import { apiErrorPresentation } from '@/services/apiErrorPresentation'
import { platformApi } from '@/services/platformApi'
import type {
  RegistryDeletionDependency,
  RegistryDeletionImpact,
  RegistryDeletionResourceType,
} from '@/services/platformTypes'

const props = defineProps<{
  open: boolean
  resourceType: RegistryDeletionResourceType
  objectId: string
  title?: string
}>()
const emit = defineEmits<{ close: []; completed: [target: { resourceType: RegistryDeletionResourceType; objectId: string }] }>()
const route = useRoute()
const auth = useAuthStore()
const scope = () => JSON.stringify([props.open, props.resourceType, props.objectId, route.fullPath,
  auth.user?.id, auth.user?.isStaff, auth.user?.isSuperuser])
let generation = 0, disposed = false
const isCurrent = (serial: number, context: string) => !disposed && serial === generation && context === scope() && props.open
onBeforeUnmount(() => { disposed = true; generation++ })

const loading = ref(false)
const saving = ref(false)
const error = ref('')
const impact = ref<RegistryDeletionImpact | null>(null)
const reviewedScope = ref('')
const stale = ref(false)
const submissionUnknown = ref(false)
const completed = ref(false)
const selectedKeys = ref(new Set<string>())
const confirmation = ref('')
const reasonCode = ref('')

const rowsByDisposition = (disposition: RegistryDeletionDependency['disposition']) => computed(() => (
  impact.value?.dependencies.filter((item) => item.disposition === disposition) || []
))
const selectableRows = rowsByDisposition('selectable_delete')
const automaticRows = rowsByDisposition('automatic_delete')
const blockingRows = rowsByDisposition('blocking_reference')
const historyRows = rowsByDisposition('preserved_history')

const allSelectableConfirmed = computed(() => (
  selectableRows.value.every((item) => selectedKeys.value.has(item.dependencyKey))
))
const canSubmit = computed(() => Boolean(
  !loading.value && !saving.value && !stale.value && !submissionUnknown.value && !completed.value
  && props.open && !disposed && reviewedScope.value === scope()
  && impact.value?.complete
  && impact.value.canExecute
  && allSelectableConfirmed.value
  && confirmation.value === impact.value.confirmationPhrase
  && reasonCode.value,
))
const confirmLabel = computed(() => {
  const selected = selectedKeys.value.size
  return selected ? `删除目标及 ${selected} 项已确认依赖` : '永久删除目标记录'
})

const clearReview = () => {
  selectedKeys.value = new Set()
  confirmation.value = ''
  reasonCode.value = ''
}

const loadImpact = async () => {
  if (!props.open || !props.objectId || loading.value || saving.value || disposed || completed.value) return
  const serial = ++generation, context = scope()
  const resourceType = props.resourceType, objectId = props.objectId
  loading.value = true
  error.value = ''
  stale.value = true
  try {
    const result = await platformApi.previewRegistryDeletion(resourceType, objectId)
    if (!isCurrent(serial, context)) return
    if (result.target.resourceType !== resourceType || result.target.objectId !== objectId) throw Error('删除影响响应与当前目标不一致，已禁止提交。')
    impact.value = result
    reviewedScope.value = context
    stale.value = false
    clearReview()
  } catch (cause) {
    if (isCurrent(serial, context)) error.value = `依赖读取失败；旧清单和输入仅供查看，禁止提交。本次读取不执行删除。${apiErrorPresentation(cause).message}`
  } finally {
    if (isCurrent(serial, context)) loading.value = false
  }
}

watch(
  scope,
  () => {
    generation++; loading.value = false; saving.value = false
    impact.value = null; reviewedScope.value = ''; stale.value = false
    submissionUnknown.value = false; completed.value = false; clearReview(); error.value = ''
    if (props.open) void loadImpact()
  },
  { immediate: true, flush: 'sync' },
)

const toggleDependency = (key: string, checked: boolean) => {
  if (loading.value || saving.value || stale.value || submissionUnknown.value || completed.value) return
  const next = new Set(selectedKeys.value)
  if (checked) next.add(key)
  else next.delete(key)
  selectedKeys.value = next
}

const close = () => { if (!saving.value) emit('close') }

const submit = async () => {
  const current = impact.value
  if (!current || !canSubmit.value) return
  const serial = generation, context = scope()
  const target = { resourceType: props.resourceType, objectId: props.objectId }
  saving.value = true
  error.value = ''
  try {
    await platformApi.executeRegistryDeletion(target.resourceType, target.objectId, {
      expectedImpactSha256: current.impactSha256,
      selectedDependencyKeys: [...selectedKeys.value],
      confirmationPhrase: confirmation.value,
      reasonCode: reasonCode.value,
    })
    if (!isCurrent(serial, context)) return
    completed.value = true
    emit('completed', target)
  } catch (cause) {
    if (!isCurrent(serial, context)) return
    const failure = apiErrorPresentation(cause, '未收到可验证的删除完成回执。')
    const message = `${failure.code ? `[${failure.code}] ` : ''}${failure.message}`
    const status = Number((cause as { response?: { status?: number } })?.response?.status || 0)
    stale.value = true
    // Transport errors, invalid acknowledgements and 5xx do not prove rollback.
    submissionUnknown.value = !(status >= 400 && status < 500)
    error.value = submissionUnknown.value ? `删除结果尚未确认。${message}` : `${message} 请重新读取依赖并重新复核后操作。`
  } finally {
    if (isCurrent(serial, context)) saving.value = false
  }
}
</script>

<template>
  <BaseModal
    :open="open"
    :dismissible="!saving"
    :title="title || impact?.target.label || '删除资源复核'"
    eyebrow="删除影响复核（DELETE IMPACT REVIEW）"
    width="980px"
    @close="close"
  >
    <div class="deletion-review">
      <div v-if="loading" class="deletion-loading"><LoaderCircle :size="18" class="spin"/><span>正在读取完整关联依赖，尚未执行删除…</span></div>
      <div v-if="impact" class="deletion-content">
        <p v-if="stale" role="status">依赖清单已失效或尚未重新读取；当前内容仅供查看，不能用于删除。</p>
        <section class="deletion-target">
          <div><span>删除目标</span><strong>{{ impact.target.label }}</strong><small>{{ impact.target.resourceType }} · {{ impact.target.status }} · {{ impact.target.objectId }}</small></div>
          <div class="deletion-counts">
            <span><b>{{ impact.summary.selectableCount }}</b> 可选草稿</span>
            <span><b>{{ impact.summary.automaticCount }}</b> 自有子项</span>
            <span class="danger"><b>{{ impact.summary.blockingCount }}</b> 阻断引用</span>
            <span class="history"><b>{{ impact.summary.preservedHistoryCount }}</b> 历史事实</span>
          </div>
        </section>

        <div v-if="!impact.targetPolicy.allowed" class="platform-alert danger"><ShieldAlert :size="16"/><p><strong>目标当前不允许删除</strong><br/>{{ impact.targetPolicy.reason }}</p></div>
        <div v-if="impact.advisories.length" class="deletion-advisories"><p v-for="item in impact.advisories" :key="item">{{ item }}</p></div>

        <section v-if="blockingRows.length" class="dependency-group blocking">
          <header><ShieldAlert :size="16"/><div><strong>必须先处理的阻断引用</strong><small>这些版本不能在此勾选强删；请替换、退役或在对应对象中单独复核。</small></div><b>{{ blockingRows.length }}</b></header>
          <div class="dependency-list">
            <article v-for="item in blockingRows" :key="item.dependencyKey">
              <div><strong>{{ item.label }}</strong><small>{{ item.relationshipLabel }} · {{ item.resourceType }} · {{ item.status }}</small><code>{{ item.objectId }}</code></div>
              <p>{{ item.reason }}<small v-if="item.referencePaths.length">引用位置：{{ item.referencePaths.join('；') }}</small></p>
            </article>
          </div>
        </section>

        <section v-if="historyRows.length" class="dependency-group history">
          <details open>
            <summary><FileClock :size="16"/><span><strong>必须保留的运行与审计事实</strong><small>任务、生产运行、烟测、快照、主机观测和决策不会成为级联删除选项。</small></span><b>{{ historyRows.length }}</b></summary>
            <div class="dependency-list history-list">
              <article v-for="item in historyRows" :key="item.dependencyKey">
                <div><strong>{{ item.label }}</strong><small>{{ item.relationshipLabel }} · {{ item.resourceType }} · {{ item.status }}</small><code>{{ item.objectId }}</code></div>
                <p>{{ item.reason }}<small v-if="item.referencePaths.length">引用位置：{{ item.referencePaths.join('；') }}</small></p>
              </article>
            </div>
          </details>
        </section>

        <section v-if="selectableRows.length" class="dependency-group selectable">
          <header><CheckSquare :size="16"/><div><strong>可由人工确认删除的安全草稿依赖</strong><small>默认全部不选；要删除目标，必须逐项确认所有直接依赖。</small></div><b>{{ selectableRows.length }}</b></header>
          <div class="dependency-list">
            <label v-for="item in selectableRows" :key="item.dependencyKey">
              <input type="checkbox" :checked="selectedKeys.has(item.dependencyKey)" :disabled="!impact.canExecute || loading || saving || stale || submissionUnknown || completed" @change="toggleDependency(item.dependencyKey, ($event.target as HTMLInputElement).checked)"/>
              <span><strong>{{ item.label }}</strong><small>{{ item.relationshipLabel }} · {{ item.resourceType }} · {{ item.status }}</small><code>{{ item.objectId }}</code><em>{{ item.reason }}</em></span>
            </label>
          </div>
        </section>

        <section v-if="automaticRows.length" class="dependency-group automatic">
          <header><Link2 :size="16"/><div><strong>目标自有的草稿结构</strong><small>这些子项不能脱离目标存在；输入目标确认短语即同时确认它们随目标删除。</small></div><b>{{ automaticRows.length }}</b></header>
          <div class="dependency-list compact">
            <article v-for="item in automaticRows" :key="item.dependencyKey"><div><strong>{{ item.label }}</strong><small>{{ item.relationshipLabel }} · {{ item.resourceType }}</small></div><p>{{ item.reason }}</p></article>
          </div>
        </section>

        <div v-if="impact.canExecute" class="deletion-confirmation">
          <p><Trash2 :size="15"/>此操作只删除平台注册记录，不会移动或删除磁盘上的模型或媒体文件；操作不可撤销。</p>
          <label>审计原因代码<select v-model="reasonCode" :disabled="loading || saving || stale || submissionUnknown || completed"><option value="">请选择稳定原因代码</option><option value="lifecycle_cleanup">生命周期清理</option><option value="configuration_correction">配置纠正</option><option value="planned_change">计划变更</option><option value="incident_response">故障处置</option></select></label>
          <label>输入完整确认短语 <code>{{ impact.confirmationPhrase }}</code><input v-model="confirmation" :disabled="loading || saving || stale || submissionUnknown || completed" autocomplete="off" :placeholder="impact.confirmationPhrase"/></label>
        </div>
      </div>

      <div v-if="error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ error }}</p></div>
      <p v-if="submissionUnknown" role="alert">未确认的目标：{{ resourceType }} · {{ objectId }}。请关闭后到资源目录和生命周期审计核对实际结果；重新读取依赖只更新清单，不证明原请求失败，也不会解锁本窗口或自动重发删除。</p>
      <p v-if="completed" role="status">服务端已确认删除完成，请在资源目录查看刷新结果；不要重复提交。</p>
    </div>

    <template #footer>
      <button class="button ghost" :disabled="saving" @click="close">取消</button>
      <button class="button ghost" :disabled="loading||saving||completed" @click="loadImpact"><RefreshCw :size="13"/>重新读取依赖</button>
      <button class="button danger" :disabled="saving||!canSubmit" @click="submit"><Trash2 :size="13"/>{{ saving?'事务校验与删除中…':confirmLabel }}</button>
    </template>
  </BaseModal>
</template>

<style scoped>
.deletion-review,.deletion-content{display:grid;gap:12px}.deletion-loading{min-height:180px;display:flex;align-items:center;justify-content:center;gap:9px;color:#9ca7b4}.deletion-target{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:12px;border:1px solid #2d3540;border-radius:8px;background:#090c10}.deletion-target>div:first-child{min-width:0;display:grid;gap:4px}.deletion-target span,.deletion-target small{color:#74808d;font-size:9px}.deletion-target strong{color:#e0e5eb;font-size:13px}.deletion-target small{overflow-wrap:anywhere}.deletion-counts{display:grid;grid-template-columns:repeat(4,auto);gap:6px}.deletion-counts span{display:grid;gap:2px;min-width:70px;padding:7px;border:1px solid #29313a;border-radius:6px;text-align:center}.deletion-counts b{color:#c9d1da;font-size:14px}.deletion-counts .danger b{color:#ee8f8f}.deletion-counts .history b{color:#d4aa70}.deletion-advisories{display:grid;gap:4px;padding:9px 11px;border:1px solid rgba(210,145,63,.3);border-radius:7px;background:rgba(210,145,63,.07)}.deletion-advisories p{margin:0;color:#c7a471;font-size:9px;line-height:1.5}.dependency-group{border:1px solid #29313a;border-radius:8px;overflow:hidden;background:#090c10}.dependency-group>header,.dependency-group summary{display:flex;align-items:flex-start;gap:8px;padding:10px;color:#aeb8c3}.dependency-group>header>div,.dependency-group summary>span{flex:1;display:grid;gap:3px}.dependency-group header strong,.dependency-group summary strong{font-size:10px}.dependency-group header small,.dependency-group summary small{color:#74808c;font-size:8px}.dependency-group header>b,.dependency-group summary>b{color:#95a0ac}.dependency-group.blocking{border-color:rgba(204,75,75,.38)}.dependency-group.blocking>header{background:rgba(204,75,75,.08);color:#ec9696}.dependency-group.history{border-color:rgba(197,145,75,.3)}.dependency-group.history summary{cursor:pointer;background:rgba(197,145,75,.07);color:#d5af78}.dependency-group.selectable{border-color:rgba(76,177,128,.35)}.dependency-group.selectable>header{background:rgba(76,177,128,.07);color:#79d0a4}.dependency-list{display:grid;border-top:1px solid #252c35}.dependency-list article,.dependency-list label{display:grid;grid-template-columns:minmax(0,1fr) minmax(240px,.8fr);gap:12px;padding:9px 11px;border-bottom:1px solid rgba(255,255,255,.045)}.dependency-list article:last-child,.dependency-list label:last-child{border-bottom:0}.dependency-list article>div,.dependency-list label>span{min-width:0;display:grid;gap:3px}.dependency-list strong{color:#bdc6d0;font-size:9px}.dependency-list small{color:#727e8a;font-size:8px}.dependency-list code{overflow-wrap:anywhere;color:#66727f;font-size:8px}.dependency-list p,.dependency-list em{display:grid;align-content:start;gap:4px;margin:0;color:#8e99a5;font-size:8px;font-style:normal;line-height:1.55}.dependency-list label{grid-template-columns:auto minmax(0,1fr);cursor:pointer}.dependency-list label input{margin-top:3px}.history-list{max-height:330px;overflow:auto}.dependency-list.compact{max-height:230px;overflow:auto}.deletion-confirmation{display:grid;gap:10px;padding:12px;border:1px solid rgba(204,75,75,.35);border-radius:8px;background:rgba(204,75,75,.06)}.deletion-confirmation>p{display:flex;align-items:center;gap:7px;margin:0;color:#e09292;font-size:9px}.deletion-confirmation label{display:grid;gap:6px;color:#aab4bf;font-size:9px}.deletion-confirmation select,.deletion-confirmation input{width:100%;padding:9px;border:1px solid #343b45;border-radius:6px;background:#070a0e;color:#e0e5eb}.deletion-confirmation code{color:#e9a1a1;overflow-wrap:anywhere}.spin{animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:760px){.deletion-target{display:grid}.deletion-counts{grid-template-columns:repeat(2,1fr)}.dependency-list article{grid-template-columns:1fr}}
</style>
