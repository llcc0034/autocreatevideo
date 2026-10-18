<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { executionPreparationApi } from '@/services/executionPreparationApi'
import { bulkFieldReceipts, type BulkFieldPreview } from '@/services/executionFieldBulkReview'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import BaseModal from './BaseModal.vue'
import ExecutionQualityNotices from './ExecutionQualityNotices.vue'

const props = defineProps<{ preparationIds: string[] }>()
const emit = defineEmits<{ close: []; busy: [value: boolean]; confirmed: [count: number] }>()
const items = ref<BulkFieldPreview[]>([]), selected = ref<string[]>([])
const loading = ref(false), submitting = ref(false), error = ref(''), stale = ref(false), acknowledged = ref(false)
const busy = computed(() => loading.value || submitting.value)
const chosen = computed(() => items.value.filter(row => selected.value.includes(row.preparation_id)))
const fieldCount = computed(() => chosen.value.reduce((total, row) => total + row.fields.filter(f => f.state === 'candidate').length, 0))
let serial = 0
onBeforeUnmount(() => { serial++ })
watch(busy, value => emit('busy', value), { flush: 'sync' })
watch(selected, () => { acknowledged.value = false }, { deep: true })
async function load() {
  const request = ++serial
  loading.value = true; error.value = ''; stale.value = false; acknowledged.value = false; items.value = []; selected.value = []
  try {
    const result = await executionPreparationApi.bulkFieldPreview([...props.preparationIds])
    if (request !== serial) return
    items.value = result; selected.value = result.filter(row => row.ready).map(row => row.preparation_id)
  } catch (cause) { if (request === serial) error.value = apiErrorSummary(cause) }
  finally { if (request === serial) loading.value = false }
}
async function confirm() {
  if (busy.value || stale.value || !acknowledged.value || !fieldCount.value) return
  const request = serial
  submitting.value = true; error.value = ''
  try {
    const count = await executionPreparationApi.bulkFieldConfirm(bulkFieldReceipts(items.value, selected.value))
    if (request === serial) { emit('confirmed', count); emit('close') }
  } catch (cause) { if (request === serial) { error.value = apiErrorSummary(cause); stale.value = true; acknowledged.value = false } }
  finally { if (request === serial) submitting.value = false }
}
watch(() => JSON.stringify(props.preparationIds), load, { immediate: true })
</script>

<template>
  <BaseModal :open="true" :title="preparationIds.length === 1 ? '确认本镜头提示词字段' : '批量确认镜头提示词字段'" width="1000px" :dismissible="!busy" @close="emit('close')">
    <div class="bulk-field-review">
      <p>只确认并采用下列已有修订；已采用内容保持不变。不修改正文、不批准整个阶段，本操作不直接提交模型调用。</p>
      <p class="bulk-reason">已获授权的自动运行在字段齐备后可能继续执行并产生费用，仍须通过原资源检查；这里不会新增外发授权。</p>
      <p v-if="loading" role="status">正在核对镜头、字段正文与采用状态…</p>
      <p v-if="error" role="alert" class="platform-alert danger">{{ error }}</p>
      <button v-if="error" type="button" class="button ghost small" :disabled="busy" @click="load">重新读取复核清单</button>
      <template v-if="items.length">
        <p role="status">已读取 {{ items.length }} 个镜头；本次选中 {{ chosen.length }} 个镜头、{{ fieldCount }} 个待采用字段。其余镜头不会变更。</p>
        <article v-for="item in items" :key="item.preparation_id">
          <label class="bulk-shot"><input v-model="selected" type="checkbox" :value="item.preparation_id" :disabled="!item.ready || busy || stale"/>{{ item.label }}<small>准备修订 {{ item.expected_preparation_revision }}</small></label>
          <p v-if="item.reason" class="bulk-reason">{{ item.reason }}</p>
          <ul>
            <li v-for="field in item.fields" :key="`${field.unit_key}:${field.field_key}`">
              <strong>{{ field.label }}</strong> · {{ field.state === 'selected' ? '已采用，保持不变' : field.state === 'candidate' ? `本次采用修订 ${field.revision}` : '需单独处理' }}
              <p v-if="field.reason && field.state !== 'selected'">{{ field.reason }}</p>
              <ExecutionQualityNotices :messages="field.warnings" />
              <details v-if="field.value_id && field.state !== 'selected'"><summary>查看本次正文</summary><pre>{{ field.text }}</pre></details>
            </li>
          </ul>
        </article>
        <label class="bulk-shot"><input v-model="acknowledged" type="checkbox" :disabled="busy || stale || !fieldCount"/>我已核对所选镜头的正文、版本和提醒，确认并采用上述字段。</label>
      </template>
    </div>
    <template #footer>
      <button type="button" class="button ghost" :disabled="busy" @click="emit('close')">取消</button>
      <button type="button" class="button primary" :disabled="busy || stale || !acknowledged || !fieldCount" @click="confirm">{{ submitting ? '正在保存采用决定…' : `确认并采用 ${chosen.length} 镜 / ${fieldCount} 字段` }}</button>
    </template>
  </BaseModal>
</template>

<style scoped>
.bulk-field-review{display:grid;gap:12px;min-width:0}.bulk-field-review article{border-top:1px solid var(--border);padding-top:12px}.bulk-field-review p{margin:6px 0;overflow-wrap:anywhere}.bulk-shot{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-height:36px}.bulk-shot input{width:18px;height:18px;flex:none}.bulk-shot small{color:var(--text-secondary)}.bulk-field-review ul{padding-left:20px}.bulk-field-review li{margin:10px 0;overflow-wrap:anywhere}.bulk-reason{color:var(--accent)}summary{cursor:pointer;min-height:36px;padding-top:6px}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:300px;overflow:auto;background:var(--bg-elevated);padding:12px;font:inherit;line-height:1.7}@media(max-width:640px){.button{white-space:normal;min-height:40px}}
</style>
