<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { TaskReferenceAssetSummary, Workflow } from '@/types'
import { mediaKindLabel } from '@/services/presentationLabels'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import FrozenReferenceCorrespondence from './FrozenReferenceCorrespondence.vue'

const props = defineProps<{ task: Workflow }>()
const expanded = ref<string[]>([]), failed = ref<string[]>([])
const groups = computed(() => [
  { title: '模型媒体输入', rows: props.task.referenceAssets },
  { title: '仅提示词上下文 · 不占模型媒体槽位', rows: props.task.contextMediaUses },
])
const rows = computed(() => [...props.task.referenceAssets, ...props.task.contextMediaUses])
const pairedName = (key: string) => rows.value.find(row => row.itemKey === key)?.displayName || '原配对素材名称未记录'
// Preview URLs are issued by the original task reader after ownership/hash
// checks. Never look up today's Shot, latest version or a replacement Asset.
function previewUrl(row: TaskReferenceAssetSummary) {
  const value = row.previewUrl
  if (value.startsWith('/') && !value.startsWith('//') && !value.includes('\\')) return value
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : '' } catch { return '' }
}
function toggle(key: string) {
  expanded.value = expanded.value.includes(key) ? expanded.value.filter(value => value !== key) : [...expanded.value, key]
}
watch(() => props.task, () => { expanded.value = []; failed.value = [] })
</script>

<template>
  <section class="frozen-task-references" aria-label="原请求素材绑定（只读）">
    <h4>原请求素材绑定 · 只读</h4>
    <p>来自所选任务的原执行准备与冻结请求，不随当前分镜改选。暂存不等于远端收到或生成成功；此处不能改选、审核或重发调用。</p>
    <p v-if="task.inputContractStatus !== 'available'" class="platform-alert warning" role="alert">原输入合同不可核对，不能将缺失记录显示为“未使用素材”。请重新读取该任务。</p>
    <template v-else>
      <p v-if="!rows.length">原请求明确没有媒体素材使用记录。</p>
      <section v-for="group in groups.filter(group => group.rows.length)" :key="group.title">
        <h5>{{group.title}}</h5>
        <ol><li v-for="(row, index) in group.rows" :key="row.itemKey">
          <strong>{{index + 1}}. {{row.displayName || '原素材名称未记录'}} · {{mediaKindLabel(row.mediaKind)}}</strong>
          <p>原输入：{{row.inputKey}}<span v-if="row.inputLabel"> · 标签 {{row.inputLabel}}</span></p>
          <p v-if="row.ports.length">目标槽位：{{row.ports.map(port => port.targetKey).join('、')}}</p>
          <p>本次用途参数：{{row.purpose ?? '此槽位无额外用途值'}} · {{ {staged:'已暂存',not_staged:'尚未暂存',not_applicable:'仅作上下文'}[row.stagingStatus] }}</p>
          <p v-if="row.pairedTo">配对来源：{{pairedName(row.pairedTo)}}</p>
          <p>来源：{{row.artifactId ? '原已选产物' : row.assetId ? '原确切素材' : row.pairedTo ? '原配对视频音轨' : '原来源身份未记录'}}</p>
          <FrozenReferenceCorrespondence :binding="row.referenceBinding"/>
          <button v-if="previewUrl(row) && !failed.includes(row.itemKey)" type="button" class="button ghost small" :aria-expanded="expanded.includes(row.itemKey)" @click="toggle(row.itemKey)">{{expanded.includes(row.itemKey) ? '收起原素材预览' : '预览原素材'}}</button>
          <p v-else role="status" class="platform-alert warning">原素材预览不可用或已失效；保留原绑定，不寻找替代文件。可重新读取此任务核对访问地址。</p>
          <div v-if="expanded.includes(row.itemKey) && previewUrl(row) && !failed.includes(row.itemKey)" class="frozen-media">
            <img v-if="row.mediaKind==='image'" :src="previewUrl(row)" :alt="row.displayName || '原请求图片'" @error="failed.push(row.itemKey)"/>
            <video v-else-if="row.mediaKind==='video'" :src="previewUrl(row)" controls preload="none" playsinline @error="failed.push(row.itemKey)"/>
            <audio v-else :src="previewUrl(row)" controls preload="none" @error="failed.push(row.itemKey)"/>
          </div>
          <ReadonlyDataValue :value="{item_key:row.itemKey,asset_id:row.assetId,artifact_id:row.artifactId,checksum:row.checksum,source_sha256:row.sourceSha256,ports:row.ports,paired_to:row.pairedTo}" label="原绑定身份、指纹及端口（技术信息）"/>
        </li></ol>
      </section>
    </template>
  </section>
</template>

<style scoped>
.frozen-task-references,.frozen-task-references section{display:grid;gap:10px;min-width:0}.frozen-task-references h4,.frozen-task-references h5,.frozen-task-references p{margin:0}.frozen-task-references h5{font-size:inherit}.frozen-task-references ol{list-style:none;padding:0;margin:0;display:grid;gap:14px}.frozen-task-references li{display:grid;gap:8px;min-width:0;border-top:1px solid var(--border);padding-top:12px;overflow-wrap:anywhere}.frozen-task-references p{font-size:12px;line-height:1.7}.frozen-task-references button{justify-self:start;min-height:36px;white-space:normal}.frozen-task-references button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.frozen-media{width:min(100%,560px);min-width:0}.frozen-media img,.frozen-media video{display:block;max-width:100%;max-height:320px;object-fit:contain}.frozen-media audio{width:100%}
</style>
