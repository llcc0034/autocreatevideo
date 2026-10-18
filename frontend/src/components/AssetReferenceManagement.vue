<script setup lang="ts">
import { onBeforeUnmount } from 'vue'
import BaseModal from './BaseModal.vue'
import { assetReferenceApi, type AssetReferenceSeries, type AssetReferenceVersion, type ReferenceBibleOption } from '@/services/assetReferenceApi'
import { createReferenceManagement } from '@/services/assetReferenceManagement'

defineProps<{series: AssetReferenceSeries | null; version: AssetReferenceVersion | null}>()
const emit = defineEmits<{changed: []}>()
const flow = createReferenceManagement(assetReferenceApi, () => emit('changed'))
const {state, patch} = flow
const bibleLabel = (row: ReferenceBibleOption) => `${row.project_title} / ${row.character_name || row.scene_name || row.prop_name || '未记录实体'} / ${row.reference_name}（${row.name}）`
onBeforeUnmount(flow.dispose)
</script>

<template>
  <section class="reference-management" aria-label="素材关系管理">
    <div v-if="series" class="actions">
      <button class="button ghost small" @click="flow.edit(series.id)">编辑名称 / Bible 关联</button>
      <button class="button ghost small" @click="flow.removal('series', series.id, series.name)">删除系列关系…</button>
      <button v-if="version" class="button ghost small" @click="flow.removal('version', version.id, `${series.name} · v${version.version}`)">解除此版本关联…</button>
    </div>
    <p v-if="state.notice" role="status">{{ state.notice }}</p>
    <BaseModal :open="!!state.mode" :dismissible="!state.busy" :title="state.mode === 'edit' ? '编辑系列名称与设定关联' : '确认解除素材关系'" width="720px" @close="flow.close">
      <p v-if="state.loading">正在读取当前对象…</p>
      <p v-if="state.error" role="alert">{{ state.error }}<br>写入失败或结果不明时，先重新读取核对；不会自动重试或改用其他素材。</p>
      <template v-if="state.mode === 'edit'">
        <p class="hint">名称支持任意语言，不是文件身份。只更新当前系列；不改名原素材、不重写设定修订、生成依据或已有请求。</p>
        <button v-if="state.series" class="button ghost small" :disabled="state.busy || state.loading" @click="flow.edit(state.series.id)">重新读取系列</button>
        <form v-if="state.series" @submit.prevent="flow.save">
          <fieldset :disabled="state.busy || state.loading">
            <label class="field"><span>系列引用名</span><input v-model="state.name" maxlength="160" required></label>
            <label class="field"><span>Bible 设定（可选）</span><select v-model="state.bibleId" @change="flow.chooseBible">
              <option value="">不关联 Bible（独立素材系列）</option>
              <option v-if="state.currentBible && !state.options.results.some(row => row.id === state.currentBible?.id)" :value="state.currentBible.id">{{ bibleLabel(state.currentBible) }}</option>
              <option v-for="row in state.options.results" :key="row.id" :value="row.id">{{ bibleLabel(row) }}</option>
            </select></label>
            <p class="hint">只列出系列所有者的同角色设定。已有版本引用确切设定修订时，改绑/解除不会覆盖它，服务端会说明需处理的关联。</p>
            <div class="actions"><label class="field"><span>查找设定：项目、实体或引用名</span><input v-model="state.search" maxlength="160" @keydown.enter.prevent="flow.options()"></label><button class="button ghost small" type="button" @click="flow.options()">查询设定</button></div>
            <p v-if="state.optionsLoading">读取设定选项…</p><p v-else-if="state.optionsError" role="alert">{{ state.optionsError }} <button class="button ghost small" type="button" @click="flow.options(state.page)">重读设定选项</button></p>
            <p v-else>共 {{ state.options.count }} 个匹配设定 · 第 {{ state.page }} 页</p>
            <div class="actions"><button class="button ghost small" type="button" :disabled="state.optionsLoading || !state.options.previous" @click="flow.options(state.page - 1)">上一页设定</button><button class="button ghost small" type="button" :disabled="state.optionsLoading || !state.options.next" @click="flow.options(state.page + 1)">下一页设定</button></div>
          </fieldset>
          <button class="button primary" type="submit" :disabled="state.busy || state.loading || !state.name.trim() || !Object.keys(patch).length">{{ state.busy ? '保存中…' : '保存系列修改' }}</button>
        </form>
      </template>
      <template v-if="state.mode === 'remove' && state.target">
        <h4>{{ state.target.label }}</h4><p class="hint">目标 ID：{{ state.target.id }}</p>
        <p>这里只解除系列/版本关系，保留原 Asset、共享文件、Bible、生成任务和产物。若要删除原素材，请回“全部素材”操作。</p>
        <div v-if="state.impact" class="impact">
          <p>将移除 {{ state.impact.version_count }} 条版本关联；{{ state.impact.asset_count }} 个原素材及其文件保留。{{ state.target.kind === 'series' ? '该系列本身也将删除。' : '其他系列和版本不变。' }}</p>
          <p v-if="!state.impact.can_remove" role="alert">尚有 {{ state.impact.variant_count }} 个子变体系列、{{ state.impact.dependent_version_count }} 条生成版本依赖、{{ state.impact.production_use_count }} 次生产素材选择引用；本次不能解除关系，也不会连带删除这些依赖或运行记录。</p>
          <p v-else>关系删除不可撤销；可重新关联保留的原素材，但不会恢复原关系 ID 或生成来源。</p>
        </div>
        <div class="actions"><button class="button ghost" :disabled="state.busy || state.loading" @click="flow.removal(state.target.kind, state.target.id, state.target.label)">重新读取影响</button><button class="button danger" :disabled="state.busy || state.loading || !state.impact?.can_remove" @click="flow.remove">{{ state.busy ? '解除中…' : '确认解除关系' }}</button></div>
      </template>
      <button class="button ghost" :disabled="state.busy" @click="flow.close">取消 / 关闭</button>
    </BaseModal>
  </section>
</template>

<style scoped>
.actions{display:flex;align-items:end;flex-wrap:wrap;gap:8px;margin:12px 0}.actions .field{flex:1;min-width:180px}.reference-management p{line-height:1.6;overflow-wrap:anywhere;font-size:13px}.hint{color:var(--text-muted)}fieldset{border:0;padding:0;margin:16px 0;min-width:0}.field{margin:12px 0}.field select,.field input{max-width:100%}.impact{border:1px solid var(--border);border-radius:8px;padding:12px;margin:12px 0}[role=alert]{color:#ed969c}form{margin-bottom:12px}
</style>
