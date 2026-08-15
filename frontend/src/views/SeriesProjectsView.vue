<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  AlertTriangle,
  ArrowRight,
  Clapperboard,
  Film,
  Plus,
  RefreshCw,
  RotateCcw,
  Star,
  Trash2,
} from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import ProjectCoverImage from '@/components/ProjectCoverImage.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useStudioStore } from '@/stores/studio'
import type { SeriesProject } from '@/types'
import {
  formatProjectUpdatedAt,
  isProjectDeleteConfirmed,
  projectEpisodeSummary,
  projectCompletion,
  sortProjectsForManagement,
} from '@/services/projectManagement'

const router = useRouter()
const studio = useStudioStore()

const retrying = ref(false)
const retryError = ref('')
const favoritePendingIds = ref(new Set<string>())
const redoTarget = ref<SeriesProject | null>(null)
const deleteTarget = ref<SeriesProject | null>(null)
const deleteConfirmation = ref('')
const redoPending = ref(false)
const deletePending = ref(false)
const redoError = ref('')
const deleteError = ref('')
const unavailableCoverIds = ref(new Set<string>())

const projects = computed(() => sortProjectsForManagement(studio.projects))
const favoriteCount = computed(() => studio.projects.filter((project) => project.isFavorite).length)
const episodeCount = computed(() => studio.projects.reduce((sum, project) => sum + (
  project.detailLoaded ? (projectEpisodeSummary(project).episodeCount ?? 0) : (project.episodeCount ?? 0)
), 0))
const loadingWithoutData = computed(() => (
  studio.loading
  || retrying.value
  || (!studio.ready && !studio.error)
) && !studio.projects.length)
const visibleLoadError = computed(() => retryError.value || studio.error)
const canConfirmDelete = computed(() => Boolean(
  deleteTarget.value
  && isProjectDeleteConfirmed(deleteTarget.value.title, deleteConfirmation.value)
  && !deletePending.value,
))

const styleFor = (styleId: string) => studio.styles.find((style) => style.id === styleId)
const projectHref = (project: SeriesProject) => `/projects/${encodeURIComponent(project.id)}`
const completionFor = projectCompletion
const hasVisibleCover = (project: SeriesProject) => Boolean(
  project.coverUrl && !unavailableCoverIds.value.has(project.id),
)
const markCoverUnavailable = (projectId: string) => {
  unavailableCoverIds.value.add(projectId)
}
const refreshProjectCover = async (projectId: string) => {
  await studio.refreshProjects()
  return studio.projects.find((project) => project.id === projectId)?.coverUrl
}

const retry = async () => {
  retrying.value = true
  retryError.value = ''
  try {
    if (studio.ready) await studio.refreshProjects()
    else await studio.initialize()
    if (studio.error) retryError.value = studio.error
  } catch (error) {
    retryError.value = error instanceof Error ? error.message : '项目列表加载失败，请稍后重试。'
  } finally {
    retrying.value = false
  }
}

const toggleFavorite = async (project: SeriesProject) => {
  if (favoritePendingIds.value.has(project.id)) return
  favoritePendingIds.value.add(project.id)
  try {
    await studio.setProjectFavorite(project, !project.isFavorite)
  } catch (error) {
    studio.notify(error instanceof Error ? error.message : '收藏状态更新失败，请稍后重试。', 'error')
  } finally {
    favoritePendingIds.value.delete(project.id)
  }
}

const openRedo = (project: SeriesProject) => {
  redoTarget.value = project
  redoError.value = ''
}

const closeRedo = () => {
  if (redoPending.value) return
  redoTarget.value = null
  redoError.value = ''
}

const confirmRedo = async () => {
  if (!redoTarget.value || redoPending.value) return
  redoPending.value = true
  redoError.value = ''
  try {
    const created = await studio.duplicateProject(redoTarget.value)
    redoTarget.value = null
    await router.push(projectHref(created))
  } catch (error) {
    redoError.value = error instanceof Error ? error.message : '重做项目创建失败，请稍后重试。'
  } finally {
    redoPending.value = false
  }
}

const openDelete = (project: SeriesProject) => {
  deleteTarget.value = project
  deleteConfirmation.value = ''
  deleteError.value = ''
}

const closeDelete = () => {
  if (deletePending.value) return
  deleteTarget.value = null
  deleteConfirmation.value = ''
  deleteError.value = ''
}

const confirmDelete = async () => {
  const project = deleteTarget.value
  if (!project || !canConfirmDelete.value) return
  deletePending.value = true
  deleteError.value = ''
  try {
    await studio.deleteProject(project.id)
    deleteTarget.value = null
    deleteConfirmation.value = ''
  } catch (error) {
    deleteError.value = error instanceof Error ? error.message : '项目删除失败，请稍后重试。'
  } finally {
    deletePending.value = false
  }
}
</script>

<template>
  <div class="series-projects page-stack">
    <section class="page-heading split-heading">
      <div>
        <span class="eyebrow">SERIES LIBRARY</span>
        <h2>连续剧项目</h2>
        <p>查看已有连续剧的故事概要与制作进度，继续管理项目，或从新故事开始创作。</p>
      </div>
      <div class="project-heading-side">
        <div class="heading-stats" aria-label="项目统计">
          <div><strong>{{ studio.projects.length }}</strong><span>全部项目</span></div>
          <i aria-hidden="true"></i>
          <div><strong>{{ favoriteCount }}</strong><span>已收藏</span></div>
          <i aria-hidden="true"></i>
          <div><strong>{{ episodeCount }}</strong><span>已规划剧集</span></div>
        </div>
        <RouterLink class="button primary" to="/projects/new"><Plus :size="16" />新建连续剧</RouterLink>
      </div>
    </section>

    <div v-if="visibleLoadError && studio.projects.length" class="load-warning" role="alert">
      <AlertTriangle :size="16" />
      <span>项目列表可能不是最新数据：{{ visibleLoadError }}</span>
      <button type="button" :disabled="retrying" @click="retry"><RefreshCw :size="14" />重新加载</button>
    </div>

    <section v-if="loadingWithoutData" class="project-loading" role="status" aria-label="正在加载连续剧项目">
      <div v-for="index in 4" :key="index" class="project-skeleton" aria-hidden="true"><i></i><span></span><span></span></div>
      <span class="sr-only">正在加载连续剧项目</span>
    </section>

    <section v-else-if="visibleLoadError && !studio.projects.length" class="panel project-state" role="alert">
      <AlertTriangle :size="30" />
      <h3>项目列表加载失败</h3>
      <p>{{ visibleLoadError }}</p>
      <button type="button" class="button ghost" :disabled="retrying" @click="retry">
        <RefreshCw :size="15" :class="{ spinning: retrying }" />{{ retrying ? '正在重试' : '重新加载' }}
      </button>
    </section>

    <section v-else-if="!projects.length" class="panel project-state" aria-labelledby="empty-project-title">
      <div class="empty-project-icon"><Clapperboard :size="30" /></div>
      <h3 id="empty-project-title">还没有连续剧项目</h3>
      <p>通过页面上方的“新建连续剧”创建第一部作品，故事设定、剧集与制作进度会集中显示在这里。</p>
    </section>

    <section v-else class="project-grid" aria-label="连续剧项目列表" :aria-busy="retrying">
      <article v-for="project in projects" :key="project.id" class="project-card" :class="{ favorite: project.isFavorite }">
        <RouterLink class="project-card-main" :to="projectHref(project)" :aria-label="`打开连续剧项目《${project.title}》详情`">
          <div
            class="project-cover"
            :style="{ '--project-cover': styleFor(project.styleId)?.gradient || 'linear-gradient(145deg,#282019,#111820)' }"
          >
            <template v-if="!hasVisibleCover(project)">
              <div class="project-cover-pattern" aria-hidden="true"></div>
              <span aria-hidden="true">{{ styleFor(project.styleId)?.name || '连续剧' }}</span>
              <strong aria-hidden="true">{{ project.title.slice(0, 1) || '剧' }}</strong>
            </template>
            <ProjectCoverImage
              v-else
              class="project-cover-image"
              :src="project.coverUrl"
              :alt="`《${project.title}》连续剧封面`"
              :refresh="() => refreshProjectCover(project.id)"
              @unavailable="markCoverUnavailable(project.id)"
            />
          </div>

          <div class="project-copy">
            <div class="project-title-row">
              <div>
                <Star v-if="project.isFavorite" :size="13" fill="currentColor" aria-hidden="true" />
                <h3>{{ project.title }}</h3>
              </div>
              <StatusBadge :status="project.status" />
            </div>
            <p>{{ project.logline || '尚未填写故事简介。进入项目后可继续完善核心故事。' }}</p>
            <dl class="project-facts">
              <div><dt>剧集</dt><dd><Film :size="13" />{{ project.episodeCount ?? project.episodes.length }} 集</dd></div>
              <div><dt>画面规格</dt><dd>{{ project.aspectRatio }} · {{ project.resolution }}</dd></div>
              <div><dt>更新时间</dt><dd>{{ formatProjectUpdatedAt(project.updatedAt) }}</dd></div>
            </dl>
            <div class="project-completion">
              <div>
                <span>剧集完成度</span>
                <strong>{{ completionFor(project).completed }}/{{ completionFor(project).total }} · {{ completionFor(project).percent }}%</strong>
              </div>
              <div
                class="completion-track"
                role="progressbar"
                aria-label="剧集完成度"
                aria-valuemin="0"
                aria-valuemax="100"
                :aria-valuenow="completionFor(project).percent"
              ><i :style="{ width: `${completionFor(project).percent}%` }"></i></div>
            </div>
            <span class="open-project">进入项目 <ArrowRight :size="14" /></span>
          </div>
        </RouterLink>

        <div class="project-actions" role="group" :aria-label="`管理项目《${project.title}》`" @click.stop @keydown.stop>
          <button
            type="button"
            :class="{ active: project.isFavorite }"
            :aria-label="`${project.isFavorite ? '取消收藏' : '收藏'}项目《${project.title}》`"
            :aria-pressed="project.isFavorite"
            :disabled="favoritePendingIds.has(project.id)"
            @click="toggleFavorite(project)"
          ><Star :size="15" :fill="project.isFavorite ? 'currentColor' : 'none'" /><span>{{ project.isFavorite ? '已收藏' : '收藏' }}</span></button>
          <button type="button" :aria-label="`基于项目《${project.title}》重做`" @click="openRedo(project)"><RotateCcw :size="15" /><span>重做</span></button>
          <button type="button" class="danger" :aria-label="`删除项目《${project.title}》`" @click="openDelete(project)"><Trash2 :size="15" /><span>删除</span></button>
        </div>
      </article>
    </section>

    <BaseModal :open="Boolean(redoTarget)" title="基于此项目重做" eyebrow="CREATE INDEPENDENT DRAFT" width="520px" @close="closeRedo">
      <div v-if="redoTarget" class="confirmation-copy">
        <div class="confirmation-icon"><RotateCcw :size="20" /></div>
        <div>
          <p>将以《{{ redoTarget.title }}》的故事与制作规格创建独立草稿。</p>
          <small>原项目不会改变；剧集、Bible、视觉连续性与生成产物不会复制。</small>
        </div>
      </div>
      <p v-if="redoError" class="modal-error" role="alert">{{ redoError }}</p>
      <template #footer>
        <button type="button" class="button ghost" :disabled="redoPending" @click="closeRedo">取消</button>
        <button type="button" class="button primary" :disabled="redoPending" @click="confirmRedo">
          <RotateCcw :size="15" />{{ redoPending ? '正在创建' : '确认重做' }}
        </button>
      </template>
    </BaseModal>

    <BaseModal :open="Boolean(deleteTarget)" title="删除连续剧项目" eyebrow="IRREVERSIBLE ACTION" width="520px" @close="closeDelete">
      <div v-if="deleteTarget" class="delete-confirmation">
        <div class="delete-warning"><AlertTriangle :size="18" /><span>数据库中的项目、剧集及从属记录将被永久删除，此操作无法撤销；任务与装配指向的物理产物按存储保留和清理策略另行处置。存在活动任务、未终结远端提交或受保护审计与发行证据时，系统会拒绝删除。</span></div>
        <label class="field" for="delete-project-confirmation">
          <span>输入项目名 <strong>{{ deleteTarget.title }}</strong> 以确认</span>
          <input
            id="delete-project-confirmation"
            v-model="deleteConfirmation"
            type="text"
            autocomplete="off"
            :placeholder="deleteTarget.title"
            :aria-invalid="Boolean(deleteConfirmation) && !canConfirmDelete"
            @keyup.enter="confirmDelete"
          />
        </label>
      </div>
      <p v-if="deleteError" class="modal-error" role="alert">{{ deleteError }}</p>
      <template #footer>
        <button type="button" class="button ghost" :disabled="deletePending" @click="closeDelete">取消</button>
        <button type="button" class="button delete-button" :disabled="!canConfirmDelete" @click="confirmDelete">
          <Trash2 :size="15" />{{ deletePending ? '正在删除' : '永久删除' }}
        </button>
      </template>
    </BaseModal>
  </div>
</template>

<style scoped>
.project-cover-image{position:absolute;inset:0;z-index:1;width:100%;height:100%;object-fit:cover}
.project-heading-side{display:flex;align-items:flex-end;gap:25px}.project-heading-side .heading-stats{padding:0}.project-heading-side .button{min-height:40px}.load-warning{display:flex;align-items:center;gap:10px;padding:11px 13px;border:1px solid rgba(230,167,90,.25);border-radius:8px;background:rgba(230,167,90,.055);color:#c89a62;font-size:11px}.load-warning span{flex:1}.load-warning button{display:inline-flex;align-items:center;gap:6px;padding:5px 8px;border-radius:5px;background:transparent;color:#deb379;cursor:pointer}.load-warning button:hover{background:rgba(255,255,255,.04)}.project-loading,.project-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.project-skeleton{min-height:285px;padding:20px;display:flex;flex-direction:column;gap:12px;border:1px solid var(--border);border-radius:11px;background:linear-gradient(100deg,#101318 10%,#171b20 40%,#101318 70%);background-size:200%;animation:project-shimmer 1.6s infinite}.project-skeleton i{width:90px;height:105px;border-radius:8px;background:rgba(255,255,255,.035)}.project-skeleton span{display:block;height:12px;border-radius:4px;background:rgba(255,255,255,.035)}.project-skeleton span:last-child{width:62%}@keyframes project-shimmer{to{background-position:-200%}}.project-state{min-height:390px;padding:40px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;color:#666d75}.project-state>svg{color:#d48258}.project-state h3{margin:15px 0 7px;font-size:17px;color:#d1d3d6}.project-state p{max-width:520px;margin-bottom:18px;font-size:11px;line-height:1.65}.empty-project-icon{width:62px;height:62px;display:flex;align-items:center;justify-content:center;border:1px solid rgba(225,160,79,.22);border-radius:50%;background:rgba(225,160,79,.06);color:var(--accent)}.project-card{min-width:0;display:grid;grid-template-columns:minmax(0,1fr) 86px;overflow:hidden;border:1px solid var(--border);border-radius:11px;background:linear-gradient(150deg,#13171c,#0e1115);transition:border-color .2s,box-shadow .2s,transform .2s}.project-card:hover{transform:translateY(-2px);border-color:rgba(255,255,255,.17);box-shadow:0 16px 35px rgba(0,0,0,.22)}.project-card.favorite{border-color:rgba(225,160,79,.23)}.project-card-main{min-width:0;display:grid;grid-template-columns:124px minmax(0,1fr);gap:17px;padding:17px}.project-card-main:focus-visible{outline:2px solid var(--accent);outline-offset:-3px}.project-cover{position:relative;min-height:208px;overflow:hidden;display:flex;flex-direction:column;align-items:flex-start;justify-content:space-between;padding:12px;border:1px solid rgba(255,255,255,.1);border-radius:8px;background:var(--project-cover)}.project-cover:after{content:'';position:absolute;inset:0;background:linear-gradient(180deg,transparent 35%,rgba(4,6,8,.75))}.project-cover-pattern{position:absolute;inset:0;background:repeating-linear-gradient(140deg,transparent 0 18px,rgba(255,255,255,.035) 19px 20px)}.project-cover span,.project-cover strong{position:relative;z-index:2}.project-cover span{padding:4px 6px;border:1px solid rgba(255,255,255,.13);border-radius:4px;background:rgba(6,8,10,.34);font-size:7px;letter-spacing:.12em;color:#d6d3ce}.project-cover strong{align-self:center;margin:auto;font-family:var(--font-display);font-size:43px;color:rgba(255,255,255,.76);text-shadow:0 5px 18px rgba(0,0,0,.4)}.project-copy{min-width:0;position:relative;display:flex;flex-direction:column;padding:2px 0}.project-title-row{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.project-title-row>div{min-width:0;display:flex;align-items:center;gap:7px}.project-title-row svg{flex:0 0 auto;color:var(--accent)}.project-title-row h3{overflow:hidden;font-family:var(--font-display);font-size:20px;line-height:1.25;text-overflow:ellipsis;white-space:nowrap}.project-copy>p{min-height:46px;margin:10px 0 13px;color:#7b8188;font-size:10px;line-height:1.65;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden}.project-facts{display:grid;grid-template-columns:.7fr 1fr 1.25fr;gap:9px;margin:0}.project-facts>div{min-width:0;padding:8px 9px;border:1px solid rgba(255,255,255,.045);border-radius:6px;background:rgba(255,255,255,.018)}.project-facts dt{margin-bottom:4px;font-size:7px;color:#515860}.project-facts dd{min-width:0;margin:0;display:flex;align-items:center;gap:4px;overflow:hidden;color:#a0a5aa;font-size:9px;text-overflow:ellipsis;white-space:nowrap}.project-completion{margin-top:13px}.project-completion>div:first-child{display:flex;align-items:center;justify-content:space-between;color:#60666e;font-size:8px}.project-completion strong{color:#9ba0a6;font-size:9px}.completion-track{height:4px;margin-top:7px;overflow:hidden;border-radius:4px;background:#272c32}.completion-track i{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#bd7733,var(--accent))}.open-project{display:inline-flex;align-items:center;gap:4px;align-self:flex-end;margin-top:auto;padding-top:12px;color:#bd8a50;font-size:9px}.project-actions{display:flex;flex-direction:column;border-left:1px solid var(--border);background:rgba(7,9,12,.22)}.project-actions button{min-height:58px;flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;border-bottom:1px solid var(--border);background:transparent;color:#707780;font-size:9px;cursor:pointer}.project-actions button:last-child{border-bottom:0}.project-actions button:hover,.project-actions button:focus-visible{background:rgba(255,255,255,.035);color:#c8cbd0}.project-actions button.active{color:var(--accent)}.project-actions button.danger:hover,.project-actions button.danger:focus-visible{background:rgba(237,105,113,.06);color:#e17880}.project-actions button:disabled{opacity:.45;cursor:wait}.confirmation-copy{display:flex;align-items:flex-start;gap:13px}.confirmation-icon{width:40px;height:40px;display:flex;align-items:center;justify-content:center;flex:0 0 auto;border-radius:8px;background:rgba(224,160,79,.08);color:var(--accent)}.confirmation-copy p{margin:2px 0 6px;color:#c9cbcf;font-size:12px;line-height:1.6}.confirmation-copy small{color:#71777f;font-size:10px;line-height:1.6}.delete-confirmation{display:flex;flex-direction:column;gap:18px}.delete-warning{display:flex;align-items:flex-start;gap:9px;padding:12px;border:1px solid rgba(237,105,113,.24);border-radius:7px;background:rgba(237,105,113,.055);color:#d9787f}.delete-warning span{font-size:10px;line-height:1.6}.delete-confirmation .field strong{color:#e8b270}.modal-error{margin-top:16px;padding:9px 10px;border:1px solid rgba(237,105,113,.25);border-radius:6px;background:rgba(237,105,113,.055);color:#e4868d;font-size:10px}.delete-button{background:#a94049;border-color:#ca5962;color:#fff}.delete-button:hover:not(:disabled){background:#bd4b55}.spinning{animation:project-spin .8s linear infinite}@keyframes project-spin{to{transform:rotate(360deg)}}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}

@media(max-width:1180px){.project-grid,.project-loading{grid-template-columns:1fr}.project-heading-side{align-items:flex-end;flex-direction:column;gap:12px}}
@media(max-width:760px){.split-heading{align-items:stretch}.project-heading-side{align-items:stretch}.project-heading-side .heading-stats{justify-content:space-between}.project-heading-side .heading-stats>div{align-items:center}.project-card{grid-template-columns:1fr}.project-card-main{grid-template-columns:96px minmax(0,1fr);padding:13px}.project-cover{min-height:190px}.project-actions{min-height:56px;display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid var(--border);border-left:0}.project-actions button{min-height:56px;border-right:1px solid var(--border);border-bottom:0}.project-actions button:last-child{border-right:0}.project-facts{grid-template-columns:repeat(2,minmax(0,1fr))}.project-facts>div:last-child{grid-column:1/-1}.open-project{display:none}}
@media(max-width:480px){.project-heading-side .heading-stats{gap:8px}.project-heading-side .heading-stats>i{display:none}.project-heading-side .button{width:100%}.load-warning{align-items:flex-start;flex-wrap:wrap}.load-warning span{min-width:calc(100% - 30px)}.project-card-main{grid-template-columns:1fr}.project-cover{min-height:116px}.project-cover strong{font-size:34px}.project-title-row h3{font-size:18px}.project-copy>p{min-height:0}.project-facts{grid-template-columns:1fr 1fr}.project-actions button{flex-direction:row}.project-state{min-height:330px;padding:25px 18px}}
</style>
