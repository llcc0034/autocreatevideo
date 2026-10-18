<script setup lang="ts">
import {computed, onBeforeUnmount, ref, watch} from 'vue'
import {onBeforeRouteLeave, onBeforeRouteUpdate} from 'vue-router'
import type {ProductionRun} from '@/services/platformTypes'
import ManualShotReferenceSelection from './ManualShotReferenceSelection.vue'

const props = defineProps<{run: ProductionRun; disabled: boolean}>()
const emit = defineEmits<{contextProtected: [boolean]; changed: []}>()
const open = ref(false), initialized = ref(false), busy = ref(false), changed = ref(false)
const retained = ref({dirty: false, pending: false})
const protectedContext = computed(() => open.value || busy.value || retained.value.dirty || retained.value.pending)
watch(protectedContext, value => emit('contextProtected', value), {flush: 'sync'})
function close() {
  if (busy.value) return
  open.value = false
  if (!protectedContext.value && changed.value) {changed.value = false; emit('changed')}
}
function show() {
  if (!initialized.value && props.disabled) return
  initialized.value = true; open.value = true
}
const canLeave = () => !busy.value && (!(retained.value.dirty || retained.value.pending) || window.confirm(retained.value.pending
  ? '配方选材请求结果未知，可能已经保存。离开会丢失原请求，请先继续单镜选材核对；仍要离开吗？'
  : '本镜有未提交选择，离开将丢弃草稿；已上传入库的文件保留。确定离开？'))
onBeforeRouteLeave(canLeave)
onBeforeRouteUpdate(canLeave)
onBeforeUnmount(() => emit('contextProtected', false))
</script>

<template>
  <section class="recipe-shot-selection" aria-label="配方单镜选材">
    <strong>本轮素材选择（单镜 / 多镜）</strong>
    <p>自动规则明确时无需逐镜重选。这里只保存本镜明确用途，不审核、不恢复调度或调用模型。</p>
    <p v-if="protectedContext" class="platform-alert warning" role="status">选材打开或有未提交／结果未知内容，整页轮询与其他运行操作已暂停。关闭且无待处理内容后恢复。</p>
    <button class="button ghost small" :disabled="busy || (!initialized && disabled)" @click="show">{{ initialized ? '继续单镜 / 多镜素材选择及核对原请求' : '选择素材（单镜 / 多镜）' }}</button>
    <ManualShotReferenceSelection v-if="initialized" :open="open" :run="run" @close="close" @busy="busy=$event" @retained="retained=$event" @changed="changed=true"/>
  </section>
</template>

<style scoped>
.recipe-shot-selection{display:grid;gap:12px;min-width:0}.recipe-shot-selection p{margin:0;overflow-wrap:anywhere}.recipe-shot-selection button{justify-self:start;min-height:36px;white-space:normal}
</style>
