import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { repository } from '@/services/repository'

interface User { id: string; name: string; email: string; isStaff: boolean }

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('cineforge_token') || '')
  const user = ref<User | null>(JSON.parse(localStorage.getItem('cineforge_user') || 'null'))
  const loading = ref(false)
  const isAuthenticated = computed(() => Boolean(token.value))
  let sessionEpoch = 0

  const persist = (result: { access: string; refresh: string; user: User }) => {
    sessionEpoch += 1
    token.value = result.access; user.value = result.user
    localStorage.setItem('cineforge_token', result.access); localStorage.setItem('cineforge_refresh_token', result.refresh); localStorage.setItem('cineforge_user', JSON.stringify(result.user))
  }
  const login = async (email: string, password: string) => { loading.value = true; try { persist(await repository.login(email, password)) } finally { loading.value = false } }
  const register = async (name: string, email: string, password: string) => { loading.value = true; try { persist(await repository.register(name, email, password)) } finally { loading.value = false } }
  const logout = () => { sessionEpoch += 1; token.value = ''; user.value = null; localStorage.removeItem('cineforge_token'); localStorage.removeItem('cineforge_refresh_token'); localStorage.removeItem('cineforge_user') }
  const refreshUser = async () => {
    if (!token.value) return
    const requestEpoch = sessionEpoch
    const requestToken = token.value
    const current = await repository.getCurrentUser()
    if (requestEpoch !== sessionEpoch || requestToken !== token.value) return
    user.value = current
    localStorage.setItem('cineforge_user', JSON.stringify(current))
  }
  return { token, user, loading, isAuthenticated, login, register, logout, refreshUser }
})
