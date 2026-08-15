<script setup lang="ts">
import { computed } from 'vue'
import { AlertTriangle, Check, Clock3, Image } from 'lucide-vue-next'
import { shotDetailDraftReviewIssueTitle } from '@/services/shotDetailDraft'
import type { Shot, ShotDetailDraftBatchItem } from '@/types'

const props = defineProps<{ item: ShotDetailDraftBatchItem; shot?: Shot }>()
const source = computed(() => props.item.beforeSnapshot.targetShot)
const beforeText = computed(() => String(
  source.value.detailed_description || source.value.detailedDescription
  || [source.value.camera, source.value.action, source.value.narrative].filter(Boolean).join('\n')
  || '冻结快照中没有可展示的扩写源文本。',
))
const statusCopy = computed(() => ({
  pending: '排队中', running: '扩写中', succeeded: '候选已生成', failed: '生成失败',
  stale: '源镜头已变化', skipped: '图片镜头已跳过',
}[props.item.status]))
</script>

<template>
<article class="draft-comparison" :class="item.status">
  <header>
    <span><b>{{ String(item.ordinal).padStart(2, '0') }}</b><strong>{{ shot?.title || `分镜 ${item.ordinal}` }}</strong><small>{{ shot?.mediaType === 'image' ? '图片' : '视频' }}</small></span>
    <em><Check v-if="item.status==='succeeded'" :size="13"/><Image v-else-if="item.status==='skipped'" :size="13"/><AlertTriangle v-else-if="['failed','stale'].includes(item.status)" :size="13"/><Clock3 v-else :size="13"/>{{ statusCopy }}</em>
  </header>
  <p v-if="item.errorMessage" class="item-error" role="alert">{{ item.errorMessage }}</p>
  <div class="comparison-grid">
    <section><h4>扩写前 <small>创建批次时冻结</small></h4><p>{{ beforeText }}</p></section>
    <section><h4>扩写后 <small>{{ item.draft ? `候选 V${item.draft.generationNo} · 修订 ${item.draft.revision}` : '尚无候选' }}</small></h4><template v-if="item.draft"><p>{{ item.draft.draftData.detailZh || '中文候选为空' }}</p><p class="english">{{ item.draft.draftData.detailEn || 'English candidate is empty.' }}</p></template><p v-else class="empty">{{ item.status==='skipped' ? '非视频镜头不参与高细节扩写。' : '等待模型返回候选。' }}</p></section>
  </div>
  <ul v-if="item.draft?.reviewIssues.length" class="review-list" aria-label="候选审阅问题">
    <li v-for="(issue,index) in item.draft.reviewIssues" :key="`${issue.code}-${index}`" :class="issue.severity">{{ shotDetailDraftReviewIssueTitle(issue) }}</li>
  </ul>
</article>
</template>

<style scoped>
.draft-comparison{display:grid;gap:8px;padding:10px;border:1px solid #29333d;border-radius:7px;background:#0b1015}.draft-comparison>header{display:flex;align-items:center;justify-content:space-between;gap:10px}.draft-comparison>header>span{display:flex;align-items:center;gap:7px;min-width:0}.draft-comparison b{font:10px Consolas,monospace;color:#d5a354}.draft-comparison strong{font-size:11px;overflow-wrap:anywhere}.draft-comparison small{color:#6f7a84;font-size:8px}.draft-comparison em{display:flex;align-items:center;gap:4px;color:#aab4bd;font-size:9px;font-style:normal;white-space:nowrap}.draft-comparison.succeeded em{color:#72c9ab}.draft-comparison.failed em,.draft-comparison.stale em{color:#df8177}.comparison-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.comparison-grid section{min-width:0;padding:8px;border:1px solid #202a33;border-radius:6px;background:#080c10}.comparison-grid h4{display:flex;justify-content:space-between;gap:8px;margin:0 0 6px;color:#aeb8c0;font-size:9px}.comparison-grid p{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;color:#929da6;font-size:9px;line-height:1.55}.comparison-grid p.english{margin-top:7px;padding-top:7px;border-top:1px solid #202a33;color:#a99ad3}.comparison-grid p.empty{color:#65717a}.item-error{margin:0;padding:7px;border:1px solid rgba(223,101,90,.25);border-radius:5px;background:rgba(223,101,90,.06);color:#df8177;font-size:9px;overflow-wrap:anywhere}.review-list{display:grid;gap:4px;margin:0;padding:0;list-style:none}.review-list li{padding:6px 7px;border-radius:5px;background:rgba(213,163,84,.07);color:#c9a56a;font-size:8px}.review-list li.requires_edit{background:rgba(223,101,90,.07);color:#df8177}@media(max-width:720px){.draft-comparison>header{align-items:flex-start;flex-direction:column}.comparison-grid{grid-template-columns:1fr}}
</style>
