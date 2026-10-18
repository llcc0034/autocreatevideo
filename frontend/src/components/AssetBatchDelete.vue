<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { Asset } from '@/types'
import BaseModal from './BaseModal.vue'
import { repository } from '@/services/repository'
import { apiErrorSummary } from '@/services/apiErrorPresentation'

const props = defineProps<{ assets: Asset[]; selectedIds: string[] }>()
const emit = defineEmits<{ cleared: []; deleted: [ids: string[]] }>()
const open = ref(false), pending = ref(false), confirmation = ref(''), error = ref('')
const targets = ref<Asset[]>([])
const selected = computed(() => props.assets.filter(asset => props.selectedIds.includes(asset.id)))
const show = () => { targets.value = [...selected.value]; confirmation.value = ''; error.value = ''; open.value = true }
watch(() => props.selectedIds, () => { if (!pending.value) open.value = false })
const remove = async () => {
  if (pending.value || confirmation.value !== '删除所选素材' || !targets.value.length) return
  pending.value = true; error.value = ''
  try {
    const result = await repository.deleteAssets(targets.value.map(asset => asset.id))
    open.value = false; emit('deleted', result.deleted_ids)
  } catch (cause) { error.value = apiErrorSummary(cause) }
  finally { pending.value = false }
}
</script>

<template>
  <div v-if="selected.length" class="batch-actions" role="region" aria-label="素材批量操作">
    <span>已选择 {{ selected.length }} 项</span>
    <button class="button ghost" :disabled="pending" @click="emit('cleared')">清空选择</button>
    <button class="button danger-ghost" :disabled="pending || selected.length > 200" @click="show">批量删除</button>
    <span v-if="selected.length > 200">每批最多 200 项</span>
  </div>
  <BaseModal :open="open" :dismissible="!pending" title="批量删除素材" width="600px" @close="open=false">
    <p>永久删除以下 {{ targets.length }} 项素材及不再共用的上传文件。不会删除项目；任一素材仍被引用或无权删除时，整批不删除。</p>
    <ul class="batch-targets"><li v-for="asset in targets" :key="asset.id">{{ asset.name }} <small>{{ asset.id }}</small></li></ul>
    <label class="field"><span>输入“删除所选素材”确认</span><input v-model="confirmation" :disabled="pending"></label>
    <p v-if="error" role="alert">{{ error }} 若响应未知，请先刷新列表核对所选 ID，不要直接再次提交。</p>
    <template #footer><button class="button ghost" :disabled="pending" @click="open=false">取消</button><button class="button danger-ghost" :disabled="pending || confirmation !== '删除所选素材'" @click="remove">{{ pending ? '删除中…' : '永久删除所选素材' }}</button></template>
  </BaseModal>
</template>

<style scoped>
.batch-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 0}
.batch-targets{max-height:240px;overflow:auto;padding-left:20px}.batch-targets small{display:block;overflow-wrap:anywhere;color:var(--text-muted)}
[role=alert]{color:#e4868d}
</style>
