<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { LayoutDashboard, Images, Clapperboard, Workflow, Settings, Plus, PanelLeftClose, PanelLeftOpen, LogOut, ChevronRight, BookOpenCheck, ShieldCheck } from 'lucide-vue-next'
import BrandMark from '@/components/BrandMark.vue'
import { useAuthStore } from '@/stores/auth'
import { useStudioStore } from '@/stores/studio'

const route = useRoute(); const router = useRouter(); const auth = useAuthStore(); const studio = useStudioStore()
const collapsed = ref(localStorage.getItem('cineforge_nav_collapsed') === 'true')
const mobileOpen = ref(false)
const nav = computed(() => [
  { label: '制作总览', to: '/', icon: LayoutDashboard, match: (path: string) => path === '/' },
  { label: '连续剧项目', to: '/projects', icon: Clapperboard, match: (path: string) => path.startsWith('/projects') },
  { label: '风格素材库', to: '/assets', icon: Images, match: (path: string) => path.startsWith('/assets') },
  { label: '生产工作流', to: '/workflows', icon: Workflow, match: (path: string) => path.startsWith('/workflows') },
  { label: '操作手册', to: '/manual', icon: BookOpenCheck, match: (path: string) => path.startsWith('/manual') }
])
const pageTitle = computed(() => String(route.meta.title || 'AutoPrompt Studio'))
const toggleCollapse = () => { collapsed.value = !collapsed.value; localStorage.setItem('cineforge_nav_collapsed', String(collapsed.value)) }
const logout = () => { studio.resetSessionState(); auth.logout(); router.replace('/login') }
const unauthorized = () => logout()
const passwordChangeRequired = () => { void router.replace('/change-password') }
onMounted(() => { void studio.initialize().catch(() => undefined); window.addEventListener('cineforge:unauthorized', unauthorized); window.addEventListener('cineforge:password-change-required', passwordChangeRequired) })
onBeforeUnmount(() => { window.removeEventListener('cineforge:unauthorized', unauthorized); window.removeEventListener('cineforge:password-change-required', passwordChangeRequired) })
</script>

<template>
  <div class="app-frame" :class="{ 'nav-collapsed': collapsed }">
    <div v-if="mobileOpen" class="mobile-shade" @click="mobileOpen=false"></div>
    <aside class="sidebar" :class="{ 'mobile-open': mobileOpen }">
      <div class="sidebar-brand"><BrandMark :compact="collapsed"/><button class="collapse-btn" @click="toggleCollapse"><component :is="collapsed ? PanelLeftOpen : PanelLeftClose" :size="16" /></button></div>
      <nav class="primary-nav">
        <RouterLink v-for="item in nav" :key="item.to" :to="item.to" :class="{ active: item.match(route.path) }" @click="mobileOpen=false"><component :is="item.icon" :size="18"/><span>{{ item.label }}</span><ChevronRight class="nav-arrow" :size="14"/></RouterLink>
      </nav>
      <div class="nav-section" v-if="!collapsed"><span>最近项目</span></div>
      <div class="recent-projects">
        <RouterLink v-for="project in studio.projects.slice(0,3)" :key="project.id" :to="`/projects/${project.id}`"><i :style="{background: studio.styles.find(s=>s.id===project.styleId)?.accent}"></i><span>{{ project.title }}</span></RouterLink>
      </div>
      <div class="sidebar-bottom">
        <RouterLink v-if="auth.user?.isSuperuser" to="/permissions" :class="{active: route.path==='/permissions'}"><ShieldCheck :size="18"/><span>权限管理</span></RouterLink>
        <RouterLink v-if="auth.user?.isStaff" to="/settings" :class="{active: route.path.startsWith('/settings')}"><Settings :size="18"/><span>系统设置</span></RouterLink>
        <button @click="logout"><LogOut :size="18"/><span>退出登录</span></button>
      </div>
    </aside>
    <main class="main-area">
      <header class="topbar">
        <button class="mobile-menu" @click="mobileOpen=true"><PanelLeftOpen :size="19"/></button>
        <div class="title-block"><span>AI FILM STUDIO</span><h1>{{ pageTitle }}</h1></div>
        <div class="top-actions">
          <RouterLink v-if="!['projects','dashboard','project-new'].includes(String(route.name))" to="/projects/new" class="button primary small"><Plus :size="16"/>新建连续剧</RouterLink>
          <div class="user-avatar" :title="auth.user?.email">{{ (auth.user?.name || '创').slice(0,1).toUpperCase() }}</div>
        </div>
      </header>
      <section class="page-content"><RouterView /></section>
    </main>
    <Transition name="toast"><div v-if="studio.toast" class="toast" :class="studio.toast.type"><span></span>{{ studio.toast.message }}</div></Transition>
  </div>
</template>
