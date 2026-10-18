<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { RouterLink } from 'vue-router'
import { executionPreparationApi } from '@/services/executionPreparationApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { taskCenterTarget } from '@/services/taskCenter'
import type { ConfigurationReplacementOptions, ConfigurationReplacementPreview, ConfigurationReplacementResult } from '@/services/configurationReplacement'
const props = defineProps<{ preparationId: string; revision: number; disabled?: boolean }>()
const emit = defineEmits<{ busy: [value: boolean]; dirty: [value: boolean]; applied: [] }>()
const options = ref<ConfigurationReplacementOptions | null>(null), preview = ref<ConfigurationReplacementPreview | null>(null)
const receipt = ref<ConfigurationReplacementResult | null>(null), versionId = ref(''), acknowledged = ref(false)
const busy = ref(false), error = ref('')
let serial = 0
watch(busy, value => emit('busy', value))
watch(versionId, value => { preview.value = null; acknowledged.value = false; emit('dirty', !!value) })
watch(() => [props.preparationId, props.revision], () => {
  serial++; busy.value = false; options.value = null; preview.value = null; versionId.value = ''; acknowledged.value = false; error.value = ''
  if (receipt.value?.preparation_id !== props.preparationId) receipt.value = null
})
onBeforeUnmount(() => { serial++ })
async function read() {
  if (props.disabled || busy.value) return
  const request = ++serial
  busy.value = true; error.value = ''; versionId.value = ''; preview.value = null
  try {
    const result = await executionPreparationApi.configurationSuccessors(props.preparationId)
    if (request !== serial) return
    if (result.revision !== props.revision) throw Error('准备修订已变化，请先刷新准备状态')
    options.value = result
  } catch (cause) { if (request === serial) error.value = apiErrorSummary(cause) }
  finally { if (request === serial) busy.value = false }
}
async function inspect() {
  if (props.disabled || busy.value || !options.value?.allowed || !options.value.versions.some(row => row.id === versionId.value)) return
  const request = ++serial, target = versionId.value
  busy.value = true; error.value = ''; preview.value = null; acknowledged.value = false
  try {
    const result = await executionPreparationApi.previewConfigurationReplacement(props.preparationId, target, props.revision)
    if (request === serial && target === versionId.value) preview.value = result
  } catch (cause) { if (request === serial) error.value = apiErrorSummary(cause) }
  finally { if (request === serial) busy.value = false }
}
async function apply() {
  const viewed = preview.value
  if (props.disabled || busy.value || !viewed || !acknowledged.value || viewed.version_id !== versionId.value) return
  const request = ++serial
  busy.value = true; error.value = ''
  try {
    const result = await executionPreparationApi.replaceConfiguration(props.preparationId, viewed.version_id, viewed.revision, viewed.preview_sha256)
    if (request !== serial) return
    receipt.value = result; versionId.value = ''; preview.value = null; emit('applied')
  } catch (cause) { if (request === serial) { error.value = apiErrorSummary(cause); acknowledged.value = false } }
  finally { if (request === serial) busy.value = false }
}
function discard() { if (!busy.value) { versionId.value = ''; preview.value = null; acknowledged.value = false } }
const display = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)
</script>

<template>
  <details class="configuration-replacement">
    <summary>修正未提交准备的提示词策略</summary>
    <p>仅配方媒体准备可用。选择同一实例的已发布策略后继，保持原输入、素材、种子与采样参数；不会重跑成功阶段或改写旧请求。</p>
    <p>主请求已冻结、已有主调用、工作进程占用或辅助结果未决时不可切换。确认后创建关联的新准备，由原流程重新检查准入并继续，可能产生新的辅助调用。</p>
    <button type="button" class="button ghost small" :disabled="disabled || busy" @click="read">{{ busy ? '处理中…' : '读取可用后继' }}</button>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <template v-if="options">
      <p>{{ options.reason }}</p>
      <RouterLink v-if="options.replacement_id" :to="taskCenterTarget({ kind: 'preparation', id: options.replacement_id })">查看已关联的后继准备</RouterLink>
      <template v-else-if="options.allowed">
        <p v-if="!options.versions.length" role="status">暂无符合边界的已发布后继。请先在执行实例中创建策略后继并完成发布；这里不会自动改用最新版本。</p>
        <label v-else>明确选择后继执行版本
          <select v-model="versionId" :disabled="disabled || busy">
            <option value="">请选择，不自动选用</option>
            <option v-for="version in options.versions" :key="version.id" :value="version.id">{{ version.name }} · v{{ version.version }} / {{ version.profile_name }} · v{{ version.profile_version }}</option>
          </select>
        </label>
        <div v-if="versionId" class="replacement-actions">
          <button type="button" class="button ghost small" :disabled="disabled || busy" @click="inspect">预览策略差异</button>
          <button type="button" class="button ghost small" :disabled="busy" @click="discard">取消本次选择</button>
        </div>
      </template>
    </template>
    <section v-if="preview" aria-label="策略切换差异">
      <h4>确认前后差异</h4>
      <article v-for="change in preview.changes" :key="change.path" class="replacement-diff">
        <strong>{{ change.path }}</strong>
        <div><span>原策略</span><pre>{{ display(change.before) }}</pre></div>
        <div><span>后继策略</span><pre>{{ display(change.after) }}</pre></div>
      </article>
      <label class="replacement-confirm"><input v-model="acknowledged" type="checkbox" :disabled="disabled || busy">我已核对差异；保留旧准备及其结果，使用此后继建立新准备并按原流程继续。</label>
      <button type="button" class="button primary" :disabled="disabled || busy || !acknowledged" @click="apply">确认切换并重新准备</button>
    </section>
    <p v-if="receipt" role="status">已创建关联后继准备，尚不代表模型已调用或生成成功。
      <RouterLink :to="taskCenterTarget({ kind: 'preparation', id: receipt.replacement_id })">查看新准备</RouterLink>
    </p>
  </details>
</template>

<style scoped>
.configuration-replacement{padding:16px;border:1px solid var(--border-color,#30343b);border-radius:10px;margin:16px 0;min-width:0}
summary{cursor:pointer;font-weight:600}p{line-height:1.65}label{display:block;margin:12px 0}select{display:block;width:100%;max-width:100%;margin-top:8px}
.replacement-actions{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.replacement-diff{padding:12px;border:1px solid var(--border-color,#30343b);border-radius:8px;margin:8px 0;overflow-wrap:anywhere}
.replacement-diff pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:6px 0 12px}.replacement-diff span{color:var(--text-muted,#aab4c4)}
.replacement-confirm{display:flex;align-items:flex-start;gap:8px;line-height:1.6}.replacement-confirm input{flex:none;margin-top:5px}
</style>
