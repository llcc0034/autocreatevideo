<script setup lang="ts">
import {computed, reactive} from 'vue'
import type {createShotReferenceBatch, BatchStatus} from '@/services/shotReferenceBatch'
import AssetMediaPreview from './AssetMediaPreview.vue'
const props = defineProps<{batch: ReturnType<typeof createShotReferenceBatch>; disabled: boolean}>()
const labels: Record<BatchStatus,string> = {draft:'待预览',ready:'待确认保存',conflict:'冲突 · 本镜未保存',blocked:'预览失败 · 未提交',saving:'正在保存',confirmed:'原命令已确认',unknown:'结果未知 · 须核对原请求',rejected:'明确拒绝 · 本镜未保存'}
const counts = computed(() => ({confirmed: props.batch.state.shots.filter(s=>s.status==='confirmed').length,
  ready: props.batch.state.shots.filter(s=>s.status==='ready').length,
  problem: props.batch.state.shots.filter(s=>['conflict','blocked','unknown','rejected'].includes(s.status)).length,
  checked: props.batch.state.shots.reduce((n,s)=>n+s.uses.filter(u=>u.checked).length,0)}))
const locked = computed(()=>props.disabled || props.batch.locked())
const mediaErrors = reactive<Record<string,boolean>>({})
</script>
<template>
  <section v-if="batch.state.shots.length" class="batch-preview" aria-label="多镜批量选材影响预览">
    <h3>多镜批量选择与影响预览</h3>
    <p>共 {{ batch.state.shots.length }} 镜，勾选 {{ counts.checked }} 个独立用途。未勾选用途及镜头不变。每镜独立事务，不审核、不恢复调度或调用模型。</p>
    <p role="status">已确认 {{ counts.confirmed }} 镜 · 待保存 {{ counts.ready }} 镜 · 冲突／失败／未知 {{ counts.problem }} 镜。回执不代表当前文件可用或下游请求已使用。</p>
    <p v-if="counts.problem" class="platform-alert warning" role="status">本批存在未完成项，不能视为整批成功。冲突或拒绝需移除该镜头后重读、重新选择；结果未知只核对原请求。</p>
    <p v-if="batch.pending()" class="platform-alert warning" role="status">关闭面板会保留原请求；离开或刷新浏览器会丢失本页请求。请先核对结果未知的原请求，不能重新选材代替核对。</p>
    <p v-if="batch.state.error" class="platform-alert danger" role="alert">{{ batch.state.error }}</p>
    <article v-for="shot in batch.state.shots" :key="shot.shotId" class="batch-shot">
      <header><label><input type="checkbox" :aria-label="`勾选镜头 ${shot.label}`" :checked="shot.uses.every(u=>u.checked)" :indeterminate="shot.uses.some(u=>u.checked) && !shot.uses.every(u=>u.checked)" :disabled="locked || !['draft','ready'].includes(shot.status)" @change="batch.check(shot,null,($event.target as HTMLInputElement).checked)"> {{ shot.label }}</label><strong role="status">{{ labels[shot.status] }}</strong></header>
      <p v-if="shot.error" role="alert">{{ shot.error }}</p>
      <div v-for="row in shot.uses" :key="row.use.id" class="batch-use">
        <label><input type="checkbox" :aria-label="`${shot.label} · ${row.use.use_key}`" :checked="row.checked" :disabled="locked || !['draft','ready'].includes(shot.status)" @change="batch.check(shot,row.use.id,($event.target as HTMLInputElement).checked)"> {{ row.use.position+1 }} · {{ row.use.use_key }} · {{ row.use.purpose || '未填写创作用途（可选）' }}</label>
        <p v-if="!row.checked" role="status">未勾选，不提交此用途。</p>
        <div class="batch-comparison"><p><strong>原选择：</strong>{{ row.beforeLabel }}</p><div><strong>新选择：</strong>{{ row.selection.label }}<p>来源：{{ row.selection.choice.source_mode === 'upload' ? '本地上传' : row.selection.choice.source_mode === 'generated' ? '本轮生成' : '已有素材' }}</p><AssetMediaPreview :asset="row.selection.asset" controls @source-error="mediaErrors[row.selection.asset.id]=true" @source-loaded="mediaErrors[row.selection.asset.id]=false"/><p v-if="mediaErrors[row.selection.asset.id]" role="alert">所选素材预览读取失败，不寻找替代版本；保存时仍由服务端校验实际文件。</p></div></div>
      </div>
      <p v-if="shot.receipt" role="status">本镜正式回执：{{ shot.receipt.id || '已确认原选材命令' }}；仅证明原命令结果，不替代当前选择读取。</p>
      <button v-if="shot.status==='unknown'" class="button ghost small" :disabled="disabled || batch.state.loading || batch.state.submitting" @click="batch.retry(shot)">核对 {{ shot.label }} 的原请求</button>
      <button v-else class="button ghost small" :disabled="locked" @click="batch.remove(shot)">{{ shot.status==='confirmed' ? '移除此镜头的已确认结果（不撤销保存）' : '移除此镜头草稿／结果（不改已保存值）' }}</button>
    </article>
    <div class="batch-actions"><button class="button ghost" :disabled="locked || !counts.checked" @click="batch.preview()">{{ batch.state.loading ? '正在核对逐镜影响…' : '核对批量影响与冲突' }}</button><button class="button primary" :disabled="locked || !batch.state.previewed || !counts.ready" @click="batch.submit()">确认保存已预览的 {{ counts.ready }} 镜</button></div>
    <p>预览仅核对当前基线与来源权限；保存时服务端再次校验确切素材和实际文件。预览后发生的变化会作为该镜头冲突或失败显示，不自动覆盖。</p>
  </section>
</template>
<style scoped>
.batch-preview,.batch-shot,.batch-use{display:grid;gap:12px;min-width:0}.batch-preview{border-top:1px solid var(--border);padding-top:16px}.batch-shot{border-bottom:1px solid var(--border);padding-bottom:12px}.batch-shot header,.batch-actions{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.batch-preview p,.batch-preview h3{margin:0;overflow-wrap:anywhere}.batch-preview h3{font-size:15px}.batch-preview label{display:flex;gap:8px;align-items:center;min-height:36px;overflow-wrap:anywhere}.batch-preview input{flex:0 0 auto;width:18px;height:18px}.batch-preview button{min-height:36px;white-space:normal;overflow-wrap:anywhere}.batch-comparison{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}.batch-comparison>div{min-width:0;overflow-wrap:anywhere}.batch-comparison :deep(img),.batch-comparison :deep(video){max-width:100%;max-height:140px;object-fit:contain}.batch-comparison :deep(audio){max-width:100%}@media(max-width:640px){.batch-comparison{grid-template-columns:minmax(0,1fr)}}
</style>
