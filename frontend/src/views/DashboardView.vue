<script setup lang="ts">
import { computed, ref } from 'vue'
import { ArrowRight, Clapperboard, Clock3, Film, FolderOpen, Images, MoreHorizontal, Play, Plus, Sparkles, WandSparkles, Workflow } from 'lucide-vue-next'
import { useStudioStore } from '@/stores/studio'
import StatusBadge from '@/components/StatusBadge.vue'
import ProjectCoverImage from '@/components/ProjectCoverImage.vue'
import { workflowProgressPresentation } from '@/services/taskProgress'
import { projectEpisodeSummary } from '@/services/projectManagement'

const studio = useStudioStore()
const episodeSummary = (project: (typeof studio.projects)[number]) => project.detailLoaded ? projectEpisodeSummary(project) : project
const completedEpisodeCountFor = (project: (typeof studio.projects)[number]) => episodeSummary(project).completedEpisodeCount ?? 0
const episodeCountFor = (project: (typeof studio.projects)[number]) => episodeSummary(project).episodeCount ?? 0
const episodeCount = computed(() => studio.projects.reduce((sum, project) => sum + (episodeSummary(project).episodeCount ?? 0), 0))
const runningCount = computed(() => studio.workflows.filter((item) => item.status === 'running' || item.status === 'queued').length)
const estimatedMinutes = computed(() => studio.projects.reduce((sum, project) => sum + (episodeSummary(project).totalDurationSeconds ?? 0), 0) / 60)
const greeting = computed(() => new Date().getHours() < 12 ? '早上好' : new Date().getHours() < 18 ? '下午好' : '晚上好')
const styleFor = (id: string) => studio.styles.find((style) => style.id === id)
const formatDate = (value: string) => new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(new Date(value))
const taskProgress = workflowProgressPresentation
const unavailableCoverIds = ref(new Set<string>())
const hasVisibleCover = (project: (typeof studio.projects)[number]) => Boolean(
  project.coverUrl && !unavailableCoverIds.value.has(project.id),
)
const markCoverUnavailable = (projectId: string) => {
  unavailableCoverIds.value.add(projectId)
}
const refreshProjectCover = async (projectId: string) => {
  await studio.refreshProjects()
  return studio.projects.find((project) => project.id === projectId)?.coverUrl
}
</script>

<template>
  <div class="dashboard page-stack">
    <section class="hero-banner">
      <div class="hero-copy">
        <span class="eyebrow"><Sparkles :size="13"/> REVIEWED PRODUCTION PIPELINE</span>
        <h2>{{ greeting }}，导演。</h2>
        <p>今天继续把故事变成镜头。实际模型、工作流与执行能力以 ProductionRun 的已审核路由和运行快照为准。</p>
        <div class="hero-actions"><RouterLink to="/projects/new" class="button primary"><Plus :size="17"/>创建连续剧</RouterLink><RouterLink to="/workflows" class="button ghost"><Workflow :size="17"/>查看生产队列</RouterLink></div>
      </div>
      <div class="hero-reel" aria-hidden="true"><div class="reel-ring outer"><i v-for="n in 12" :key="n"></i></div><div class="reel-ring inner"><span></span><span></span><span></span><span></span></div><div class="reel-core"><Play fill="currentColor" :size="18"/></div></div>
      <div class="hero-grid"></div>
    </section>

    <section class="metric-grid">
      <article class="metric-card"><div class="metric-icon amber"><Clapperboard :size="20"/></div><div><span>连续剧项目</span><strong>{{ studio.projects.length }}</strong><small>其中 {{ studio.projects.filter(p=>p.status==='producing').length }} 个制作中</small></div><div class="metric-spark amber"><i v-for="h in [35,48,40,67,62,85,75]" :key="h" :style="{height:`${h}%`}"></i></div></article>
      <article class="metric-card"><div class="metric-icon cyan"><Film :size="20"/></div><div><span>已规划剧集</span><strong>{{ episodeCount }}</strong><small>支持无限新增集数</small></div><div class="metric-spark cyan"><i v-for="h in [22,45,51,46,66,71,89]" :key="h" :style="{height:`${h}%`}"></i></div></article>
      <article class="metric-card"><div class="metric-icon violet"><WandSparkles :size="20"/></div><div><span>工作流任务</span><strong>{{ runningCount }}</strong><small>当前等待或运行</small></div><div class="metric-spark violet"><i v-for="h in [61,46,73,55,80,70,92]" :key="h" :style="{height:`${h}%`}"></i></div></article>
      <article class="metric-card"><div class="metric-icon green"><Clock3 :size="20"/></div><div><span>成片总时长</span><strong>{{ estimatedMinutes.toFixed(1) }}<em> min</em></strong><small>已编排镜头估算</small></div><div class="metric-spark green"><i v-for="h in [30,32,41,55,59,77,83]" :key="h" :style="{height:`${h}%`}"></i></div></article>
    </section>

    <section class="content-grid wide-left">
      <div class="panel project-panel">
        <header class="panel-header"><div><span class="eyebrow">SERIES IN PRODUCTION</span><h3>最近连续剧</h3></div><RouterLink to="/projects" class="text-link">查看全部 <ArrowRight :size="14"/></RouterLink></header>
        <div v-if="studio.loading" class="skeleton-list"><i v-for="n in 3" :key="n"></i></div>
        <div v-else class="project-list">
          <RouterLink v-for="project in studio.projects.slice(0,4)" :key="project.id" :to="`/projects/${project.id}`" class="project-row">
            <div class="project-cover" :style="{background:styleFor(project.styleId)?.gradient}">
              <template v-if="!hasVisibleCover(project)"><div class="cover-lines"></div><span>{{ styleFor(project.styleId)?.name }}</span></template>
              <ProjectCoverImage v-else class="project-cover-image" :src="project.coverUrl" :alt="`《${project.title}》连续剧封面`" :refresh="() => refreshProjectCover(project.id)" @unavailable="markCoverUnavailable(project.id)" />
            </div>
            <div class="project-main"><div><h4>{{ project.title }}</h4><StatusBadge :status="project.status"/></div><p>{{ project.logline || '暂无简介' }}</p><div class="project-meta"><span><Film :size="13"/> {{ episodeCountFor(project) }} 集</span><span>{{ project.aspectRatio }} · {{ project.resolution }}</span><span>更新于 {{ formatDate(project.updatedAt) }}</span></div></div>
            <div class="project-progress"><span>{{ completedEpisodeCountFor(project) }} / {{ episodeCountFor(project) }}</span><div><i :style="{width:`${Math.max(8,completedEpisodeCountFor(project)/Math.max(1,episodeCountFor(project))*100)}%`}"></i></div><small>剧集完成度</small></div>
            <button class="icon-btn" @click.prevent><MoreHorizontal :size="18"/></button>
          </RouterLink>
          <RouterLink v-if="!studio.projects.length" to="/projects" class="empty-inline"><FolderOpen :size="25"/><span>查看连续剧项目</span></RouterLink>
        </div>
      </div>
      <div class="panel workflow-mini-panel">
        <header class="panel-header"><div><span class="eyebrow">COMFYUI QUEUE</span><h3>生产进度</h3></div><RouterLink to="/workflows" class="icon-btn"><ArrowRight :size="16"/></RouterLink></header>
        <div v-for="workflow in studio.workflows.slice(0,3)" :key="workflow.id" class="workflow-mini">
          <div class="workflow-mini-top"><div><span class="pulse-dot" :class="workflow.status"></span><strong>{{ workflow.projectTitle }}</strong></div><span>{{ taskProgress(workflow).compactLabel }}</span></div>
          <p>{{ taskProgress(workflow).summary }}</p><div class="progress-track" :class="{indeterminate:taskProgress(workflow).indeterminate}"><i :style="{width:taskProgress(workflow).indeterminate?'8%':`${taskProgress(workflow).percent || 0}%`}"></i></div><small v-if="workflow.eta">{{ workflow.status==='completed' ? '已归档' : `预计剩余 ${workflow.eta}` }}</small>
        </div>
        <div v-if="!studio.workflows.length" class="empty-compact"><Workflow :size="25"/><p>还没有生产任务</p></div>
      </div>
    </section>

    <section class="panel style-strip-panel">
      <header class="panel-header"><div><span class="eyebrow">CURATED VISUAL WORLDS</span><h3>10 种热门视觉风格</h3></div><RouterLink to="/assets" class="text-link">浏览全部素材 <ArrowRight :size="14"/></RouterLink></header>
      <div class="style-strip">
        <RouterLink v-for="style in studio.styles" :key="style.id" :to="`/assets?style=${style.id}`" class="style-mini-card" :style="{background:style.gradient}">
          <div class="style-pattern"></div><span class="style-count">{{ style.assetCount ?? '—' }} ITEMS</span><div><small>{{ style.englishName }}</small><strong>{{ style.name }}</strong><p>{{ style.tags.join(' · ') }}</p></div>
        </RouterLink>
      </div>
    </section>
  </div>
</template>

<style scoped>
.project-cover-image{position:absolute;inset:0;z-index:1;width:100%;height:100%;object-fit:cover}
</style>
