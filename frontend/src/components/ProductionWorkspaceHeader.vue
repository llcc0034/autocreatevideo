<script setup lang="ts">
import { ArrowLeft, LockKeyhole, PencilLine } from 'lucide-vue-next'
import type { ProductionRun } from '@/services/platformTypes'
import { productionModeLabel } from '@/services/productionRunPresentation'
import { productionDate } from '@/services/projectVersionContents'
import PlatformStatusPill from './PlatformStatusPill.vue'
import ProjectCoverImage from './ProjectCoverImage.vue'
defineProps<{ title: string; mode: 'operate' | 'readonly'; run?: ProductionRun | null; gradient?: string; coverUrl?: string; backLabel?: string }>()
defineEmits<{ back: [] }>()
</script>
<template>
  <header class="series-header production-workspace-header" :style="{background:gradient}">
    <div class="series-pattern"></div>
    <div class="production-header-main">
      <button type="button" class="back-link light" @click="$emit('back')"><ArrowLeft :size="14"/>{{backLabel || '返回制作版本'}}</button>
      <span class="eyebrow">项目制作 / PROJECT PRODUCTION</span>
      <div class="production-header-title">
        <ProjectCoverImage v-if="coverUrl" class="production-header-cover" :src="coverUrl" alt="项目封面"/>
        <div><h2>{{title}}</h2><div class="production-header-meta"><span class="production-mode"><component :is="mode==='readonly'?LockKeyhole:PencilLine" :size="14"/>{{mode==='readonly'?'版本内容 · 只读':'生产工作台 · 可操作'}}</span><template v-if="run"><span>制作版本 {{run.id.slice(0,8)}}</span><span>{{productionModeLabel(run)}}</span><PlatformStatusPill :status="run.status"/></template></div></div>
      </div>
      <p>{{mode==='readonly'?'查看本版本保存的输入与成果，不修改项目或制作记录。':'维护当前输入并推进本轮制作；已保存的版本结果保持独立。'}}</p>
      <p v-if="run" class="production-header-dates">创建 {{productionDate(run.createdAt)}}<template v-if="run.finishedAt"> · 结束 {{productionDate(run.finishedAt)}}</template></p>
      <slot name="context"/>
    </div>
    <div class="production-header-actions"><slot/></div>
  </header>
</template>
<style scoped>
.production-workspace-header{margin:0;min-height:190px;padding:28px;border:1px solid var(--border);border-radius:12px;gap:24px;align-items:center;flex-wrap:wrap}
.production-header-main{position:relative;z-index:1;flex:1;min-width:0;display:grid;gap:12px}
.production-header-actions{position:relative;z-index:1;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.production-workspace-header h2{font-family:var(--font-display,serif);font-size:30px;line-height:1.3;margin:0;overflow-wrap:anywhere}
.production-workspace-header p,.production-header-main :deep(p){color:var(--text-secondary);font-size:14px;line-height:1.7;margin:0;overflow-wrap:anywhere}
.production-header-title{display:flex;align-items:center;gap:18px}.production-header-title>div{min-width:0;display:grid;gap:10px}
.production-header-cover{width:72px;height:96px;object-fit:cover;border-radius:6px}
.production-header-meta{display:flex;align-items:center;flex-wrap:wrap;gap:8px 14px;font-size:14px;color:var(--text-secondary)}
.production-mode{display:inline-flex;align-items:center;gap:6px;color:var(--accent)}
.production-header-dates{font-size:13px!important}.production-header-actions :deep(.button){white-space:normal;min-height:38px}
@media(max-width:640px){.production-workspace-header{padding:20px 16px}.production-workspace-header h2{font-size:24px}.production-header-actions{width:100%}.production-header-actions :deep(.button){flex:1 1 auto}}
</style>
