<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { KeyRound, LogOut } from 'lucide-vue-next'
import BrandMark from '@/components/BrandMark.vue'
import { api } from '@/services/api'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()
const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const saving = ref(false)
const error = ref('')

const submit = async () => {
  error.value = ''
  if (!currentPassword.value || !newPassword.value || !confirmPassword.value) {
    error.value = '请完整填写当前密码和两次新密码。'
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    error.value = '两次输入的新密码不一致。'
    return
  }
  saving.value = true
  try {
    await api.post('/auth/password/change/', {
      current_password: currentPassword.value,
      new_password: newPassword.value,
      confirm_password: confirmPassword.value,
    })
    auth.logout()
    await router.replace({ name: 'login', query: { password_changed: '1' } })
  } catch (value) {
    error.value = value instanceof Error ? value.message : '密码修改失败，请重试。'
  } finally {
    saving.value = false
  }
}

const logout = async () => { auth.logout(); await router.replace('/login') }
</script>

<template>
  <main class="password-change-page">
    <section class="password-change-card">
      <BrandMark />
      <div class="password-change-icon"><KeyRound :size="24" /></div>
      <span class="eyebrow">FIRST LOGIN SECURITY</span>
      <h1>首次登录，请先修改临时密码</h1>
      <p>临时密码只用于本次身份确认。修改成功后，当前登录凭据会立即失效，请使用新密码重新登录。</p>
      <form @submit.prevent="submit">
        <label class="field"><span>当前临时密码</span><input v-model="currentPassword" type="password" autocomplete="current-password" /></label>
        <label class="field"><span>新密码</span><input v-model="newPassword" type="password" autocomplete="new-password" /></label>
        <label class="field"><span>确认新密码</span><input v-model="confirmPassword" type="password" autocomplete="new-password" /></label>
        <p v-if="error" class="form-error">{{ error }}</p>
        <button class="button primary" :disabled="saving">{{ saving ? '正在修改…' : '修改密码并重新登录' }}</button>
      </form>
      <button class="button ghost" @click="logout"><LogOut :size="15" />退出登录</button>
    </section>
  </main>
</template>

<style scoped>
.password-change-page{min-height:100vh;display:grid;place-items:center;padding:24px;background:#080a0d;color:#f8fafc}
.password-change-card{width:min(100%,520px);display:grid;gap:18px;padding:36px;border:1px solid #252a32;border-radius:18px;background:#0e1116;box-shadow:0 28px 80px #0008}
.password-change-icon{width:48px;height:48px;display:grid;place-items:center;border-radius:14px;background:#d98d2b1a;color:#e9a84b}
h1{font-size:28px;line-height:1.2;margin:0}p{margin:0;color:#9aa4b2;line-height:1.65}form{display:grid;gap:16px}.button{justify-content:center}.eyebrow{color:#e9a84b}
</style>
