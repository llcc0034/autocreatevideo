import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { repository } from '@/services/repository'
import type { AuthenticatedUser } from '@/services/authTypes'
import { readAuthSession } from '@/services/authRefresh'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('cineforge_token') || '')
  const user = ref<AuthenticatedUser | null>(JSON.parse(localStorage.getItem('cineforge_user') || 'null'))
  const loading = ref(false)
  const isAuthenticated = computed(() => Boolean(token.value))
  let sessionEpoch = 0
  let currentUserRequest: Promise<void> | null = null

  const syncSession = () => {
    const session = readAuthSession(localStorage)
    if (String(user.value?.id || '') !== session.userId) sessionEpoch += 1
    token.value = session.access
    try { user.value = JSON.parse(localStorage.getItem('cineforge_user') || 'null') }
    catch { user.value = null }
  }

  const persist = (result: { access: string; refresh: string; user: AuthenticatedUser }) => {
    sessionEpoch += 1
    token.value = result.access; user.value = result.user
    localStorage.setItem('cineforge_token', result.access); localStorage.setItem('cineforge_refresh_token', result.refresh); localStorage.setItem('cineforge_user', JSON.stringify(result.user))
  }
  const login = async (account: string, password: string) => { loading.value = true; try { persist(await repository.login(account, password)) } finally { loading.value = false } }
  const register = async (account: string, email: string, password: string) => { loading.value = true; try { persist(await repository.register(account, email, password)) } finally { loading.value = false } }
  const logout = () => { sessionEpoch += 1; token.value = ''; user.value = null; localStorage.removeItem('cineforge_token'); localStorage.removeItem('cineforge_refresh_token'); localStorage.removeItem('cineforge_user') }
  const refreshUser = async () => {
    syncSession()
    if (!token.value) return
    const requestEpoch = sessionEpoch
    const requestUserId = readAuthSession(localStorage).userId
    const current = await repository.getCurrentUser()
    const session = readAuthSession(localStorage)
    if (requestEpoch !== sessionEpoch || requestUserId !== session.userId || !session.access) return
    token.value = session.access
    user.value = current
    localStorage.setItem('cineforge_user', JSON.stringify(current))
  }
  const ensureCurrentUser = async () => {
    if (!token.value) return
    if (!currentUserRequest) currentUserRequest = refreshUser().finally(() => { currentUserRequest = null })
    await currentUserRequest
  }
  return { token, user, loading, isAuthenticated, login, register, logout, refreshUser, ensureCurrentUser, syncSession }
})
