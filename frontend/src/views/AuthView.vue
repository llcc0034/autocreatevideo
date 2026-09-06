<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowRight, Check, Eye, EyeOff, Film, Layers3, Sparkles, Workflow } from 'lucide-vue-next'
import BrandMark from '@/components/BrandMark.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { repository } from '@/services/repository'
import { useAuthStore } from '@/stores/auth'

const route = useRoute(); const router = useRouter(); const auth = useAuthStore()
const isRegister = computed(() => route.name === 'register')
const account = ref(''); const email = ref(''); const password = ref(''); const showPassword = ref(false); const error = ref('')
const publicRegistrationEnabled = ref(false); const capabilitiesLoaded = ref(false)
const registrationBlocked = computed(() => isRegister.value && capabilitiesLoaded.value && !publicRegistrationEnabled.value)
onMounted(async () => { try { publicRegistrationEnabled.value = (await repository.getRegistrationCapability()).publicRegistrationEnabled } catch { publicRegistrationEnabled.value = false } finally { capabilitiesLoaded.value = true } })
const submit = async () => {
  error.value = ''
  if (!account.value || !password.value || (isRegister.value && !email.value)) { error.value = '请完整填写账号信息'; return }
  try {
    if (isRegister.value) await auth.register(account.value, email.value, password.value); else await auth.login(account.value, password.value)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    router.replace(auth.user?.mustChangePassword ? '/change-password' : redirect)
  } catch (e) { error.value = apiErrorSummary(e, '登录失败，请稍后重试') }
}
</script>

<template>
  <main class="auth-page">
    <section class="auth-visual">
      <div class="auth-grain"></div><div class="auth-orbit one"></div><div class="auth-orbit two"></div>
      <div class="auth-visual-top"><BrandMark /></div>
      <div class="auth-statement">
        <span class="eyebrow"><Sparkles :size="13"/> 审核式生产流程 / REVIEWED PRODUCTION PIPELINE</span>
        <h1>把一个故事，<br><em>锻造成一整部电影。</em></h1>
        <p>从脚本、Bible 与连续分镜，到语义工作流、受管产物和逐集交付，每个关键阶段都保留人工门禁。</p>
        <div class="auth-capabilities">
          <div><Film :size="18"/><span><strong>ProductionRun 统一入口</strong><small>脚本、路由、成本、产物与交付同一条审计链</small></span></div>
          <div><Layers3 :size="18"/><span><strong>跨集一致性</strong><small>角色、场景、道具与色彩锁定</small></span></div>
          <div><Workflow :size="18"/><span><strong>门禁式生产</strong><small>语义工作流、异步执行与逐集母版均显式验证</small></span></div>
        </div>
      </div>
      <div class="auth-frame-card"><div class="frame-image"><span>场景 07 · 镜头 03 / SCENE · SHOT</span><i></i></div><div><strong>听雨不归人</strong><span>角色一致性 98.4%</span></div></div>
      <footer>AutoPrompt Studio</footer>
    </section>
    <section class="auth-panel">
      <div class="auth-form-wrap">
        <div class="auth-mobile-brand"><BrandMark /></div>
        <span class="eyebrow">{{ isRegister ? 'CREATE YOUR STUDIO' : 'WELCOME BACK' }}</span>
        <h2>{{ registrationBlocked ? '公开注册未开放' : (isRegister ? '创建制作工作室' : '登录制作工作台') }}</h2>
        <p>{{ registrationBlocked ? '此部署由管理员创建账号；如需加入，请联系工作室管理员。' : (isRegister ? '建立你的第一个 AI 连续剧制作空间。' : '继续你的故事，所有素材与进度都在原位。') }}</p>
        <form v-if="!registrationBlocked" @submit.prevent="submit">
          <label class="field"><span>账号</span><input v-model="account" type="text" autocomplete="username" placeholder="输入账号" /></label>
          <label v-if="isRegister" class="field"><span>联系邮箱</span><input v-model="email" type="email" autocomplete="email" placeholder="name@studio.com" /></label>
          <label class="field"><span>密码</span><div class="password-field"><input v-model="password" :type="showPassword ? 'text' : 'password'" :autocomplete="isRegister ? 'new-password' : 'current-password'" placeholder="至少 8 位"><button type="button" @click="showPassword=!showPassword"><component :is="showPassword ? EyeOff : Eye" :size="17"/></button></div></label>
          <div v-if="!isRegister" class="form-options"><label><input type="checkbox" checked><span><Check :size="11"/></span>记住登录状态</label><span>联系管理员重置密码</span></div>
          <p v-if="error" class="form-error">{{ error }}</p>
          <button class="button primary auth-submit" :disabled="auth.loading">{{ auth.loading ? '正在进入工作室…' : (isRegister ? '创建工作室' : '进入工作室') }}<ArrowRight :size="17"/></button>
        </form>
        <div v-if="isRegister" class="auth-switch">已有账号？ <RouterLink to="/login">直接登录</RouterLink></div>
        <div v-else-if="publicRegistrationEnabled" class="auth-switch">还没有账号？ <RouterLink to="/register">免费创建</RouterLink></div>
        <div v-else-if="capabilitiesLoaded" class="auth-switch">新账号由管理员创建</div>
        <div v-if="!isRegister" class="demo-note"><i></i><span>仅隔离本地演示可显式执行 <code>.\scripts\setup.ps1 -WithDemoProject</code>；账号为 demo，随机密码只在首次创建命令中显示一次。</span></div>
      </div>
    </section>
  </main>
</template>
