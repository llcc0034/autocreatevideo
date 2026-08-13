<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowRight, Check, Eye, EyeOff, Film, Layers3, Sparkles, Workflow } from 'lucide-vue-next'
import BrandMark from '@/components/BrandMark.vue'
import { repository } from '@/services/repository'
import { useAuthStore } from '@/stores/auth'

const route = useRoute(); const router = useRouter(); const auth = useAuthStore()
const isRegister = computed(() => route.name === 'register')
const name = ref(''); const email = ref(''); const password = ref(''); const showPassword = ref(false); const error = ref('')
const publicRegistrationEnabled = ref(false); const capabilitiesLoaded = ref(false)
const registrationBlocked = computed(() => isRegister.value && capabilitiesLoaded.value && !publicRegistrationEnabled.value)
onMounted(async () => { try { publicRegistrationEnabled.value = (await repository.getRegistrationCapability()).publicRegistrationEnabled } catch { publicRegistrationEnabled.value = false } finally { capabilitiesLoaded.value = true } })
const submit = async () => {
  error.value = ''
  if (!email.value || !password.value || (isRegister.value && !name.value)) { error.value = '请完整填写账户信息'; return }
  try {
    if (isRegister.value) await auth.register(name.value, email.value, password.value); else await auth.login(email.value, password.value)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'; router.replace(redirect)
  } catch (e) { error.value = e instanceof Error ? e.message : '登录失败，请稍后重试' }
}
</script>

<template>
  <main class="auth-page">
    <section class="auth-visual">
      <div class="auth-grain"></div><div class="auth-orbit one"></div><div class="auth-orbit two"></div>
      <div class="auth-visual-top"><BrandMark /></div>
      <div class="auth-statement">
        <span class="eyebrow"><Sparkles :size="13"/> MINI​MAX H3 NATIVE AUDIO</span>
        <h1>把一个故事，<br><em>锻造成一整部电影。</em></h1>
        <p>从角色圣经、连续分镜到 ComfyUI 自动制作，让每一集都保持同一张脸、同一个世界。</p>
        <div class="auth-capabilities">
          <div><Film :size="18"/><span><strong>三种 H3 生产工作流</strong><small>文生 · 0/1/2 首尾帧 · 分类参考生视频</small></span></div>
          <div><Layers3 :size="18"/><span><strong>跨集一致性</strong><small>角色、场景、道具与色彩锁定</small></span></div>
          <div><Workflow :size="18"/><span><strong>无人值守生产</strong><small>提示词到合成成片的完整工作流</small></span></div>
        </div>
      </div>
      <div class="auth-frame-card"><div class="frame-image"><span>SCENE 07 · SHOT 03</span><i></i></div><div><strong>听雨不归人</strong><span>角色一致性 98.4%</span></div></div>
      <footer>© 2026 CineForge Studio</footer>
    </section>
    <section class="auth-panel">
      <div class="auth-form-wrap">
        <div class="auth-mobile-brand"><BrandMark /></div>
        <span class="eyebrow">{{ isRegister ? 'CREATE YOUR STUDIO' : 'WELCOME BACK' }}</span>
        <h2>{{ registrationBlocked ? '公开注册未开放' : (isRegister ? '创建制作工作室' : '登录制作工作台') }}</h2>
        <p>{{ registrationBlocked ? '此部署由管理员创建账号；如需加入，请联系工作室管理员。' : (isRegister ? '建立你的第一个 AI 连续剧制作空间。' : '继续你的故事，所有素材与进度都在原位。') }}</p>
        <form v-if="!registrationBlocked" @submit.prevent="submit">
          <label v-if="isRegister" class="field"><span>工作室昵称</span><input v-model="name" autocomplete="name" placeholder="例如：云墨影业" /></label>
          <label class="field"><span>{{ isRegister ? '邮箱地址' : '账号或邮箱' }}</span><input v-model="email" :type="isRegister ? 'email' : 'text'" :autocomplete="isRegister ? 'email' : 'username'" :placeholder="isRegister ? 'name@studio.com' : '输入账号或邮箱'" /></label>
          <label class="field"><span>密码</span><div class="password-field"><input v-model="password" :type="showPassword ? 'text' : 'password'" :autocomplete="isRegister ? 'new-password' : 'current-password'" placeholder="至少 8 位"><button type="button" @click="showPassword=!showPassword"><component :is="showPassword ? EyeOff : Eye" :size="17"/></button></div></label>
          <div v-if="!isRegister" class="form-options"><label><input type="checkbox" checked><span><Check :size="11"/></span>记住登录状态</label><span>联系管理员重置密码</span></div>
          <p v-if="error" class="form-error">{{ error }}</p>
          <button class="button primary auth-submit" :disabled="auth.loading">{{ auth.loading ? '正在进入工作室…' : (isRegister ? '创建工作室' : '进入工作室') }}<ArrowRight :size="17"/></button>
        </form>
        <div v-if="isRegister" class="auth-switch">已有账户？ <RouterLink to="/login">直接登录</RouterLink></div>
        <div v-else-if="publicRegistrationEnabled" class="auth-switch">还没有账户？ <RouterLink to="/register">免费创建</RouterLink></div>
        <div v-else-if="capabilitiesLoaded" class="auth-switch">新账号由管理员创建</div>
        <div v-if="!isRegister" class="demo-note"><i></i><span>仅隔离本地演示可显式执行 <code>.\scripts\setup.ps1 -WithDemoProject</code>；用户名为 demo，随机密码只在首次创建命令中显示一次。</span></div>
      </div>
    </section>
  </main>
</template>
