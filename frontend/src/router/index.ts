import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import AppShell from '@/layouts/AppShell.vue'
import {
  clearRouteModuleFailure,
  clearRouteModuleRecovery,
  reportRouteModuleFailure,
  resolveRouteRecoveryStorage,
  routeModuleRecoveryAction,
} from '@/services/routeModuleRecovery'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/views/AuthView.vue'), meta: { public: true } },
    { path: '/register', name: 'register', component: () => import('@/views/AuthView.vue'), meta: { public: true } },
    { path: '/', component: AppShell, children: [
      { path: '', name: 'dashboard', component: () => import('@/views/DashboardView.vue'), meta: { title: '制作总览' } },
      { path: 'assets', name: 'assets', component: () => import('@/views/AssetLibraryView.vue'), meta: { title: '素材库' } },
      { path: 'projects', name: 'projects', component: () => import('@/views/SeriesProjectsView.vue'), meta: { title: '连续剧项目' } },
      { path: 'projects/new', name: 'project-new', component: () => import('@/views/SeriesStudioView.vue'), meta: { title: '新建连续剧' } },
      { path: 'projects/:projectId', name: 'project-detail', component: () => import('@/views/SeriesStudioView.vue'), meta: { title: '连续剧工作室' } },
      { path: 'projects/:projectId/episodes/:episodeId/storyboard', name: 'storyboard', component: () => import('@/views/StoryboardView.vue'), meta: { title: '可视化分镜' } },
      { path: 'workflows', name: 'workflows', component: () => import('@/views/WorkflowView.vue'), meta: { title: '生产工作流' } },
      { path: 'manual', name: 'production-manual', component: () => import('@/views/ProductionManualView.vue'), meta: { title: '视频制作操作手册' } },
      { path: 'settings', name: 'settings', component: () => import('@/views/SettingsView.vue'), meta: { title: '系统设置' } }
    ] },
    { path: '/:pathMatch(.*)*', redirect: '/' }
  ],
  scrollBehavior: () => ({ top: 0 })
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.isAuthenticated) return { name: 'login', query: { redirect: to.fullPath } }
  if (to.meta.public && auth.isAuthenticated && !to.meta.allowAuthenticated) return { name: 'dashboard' }
})

router.onError((error, to) => {
  const storage = resolveRouteRecoveryStorage(() => window.sessionStorage)
  const action = routeModuleRecoveryAction(error, to.fullPath, storage)
  if (action === 'reload') {
    window.location.replace(to.fullPath)
    return
  }
  if (action === 'report') reportRouteModuleFailure(to.fullPath)
})

router.afterEach((_to, _from, failure) => {
  if (failure) return
  clearRouteModuleRecovery(resolveRouteRecoveryStorage(() => window.sessionStorage))
  clearRouteModuleFailure()
})

export default router
