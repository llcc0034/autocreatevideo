<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Copy, KeyRound, Plus, RefreshCw, ShieldCheck, UserCheck, UserX } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { accessApi, type AccessAuditEvent, type AccessUser } from '@/services/accessApi'
import type { AccessRole } from '@/services/authTypes'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const users = ref<AccessUser[]>([])
const audit = ref<AccessAuditEvent[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const createError = ref('')
const actionError = ref('')
const query = ref('')
const createOpen = ref(false)
const actionOpen = ref(false)
const passwordOpen = ref(false)
const temporaryPassword = ref('')
const action = reactive<{ kind: 'role' | 'status' | 'reset'; user: AccessUser | null; role: AccessRole; isActive: boolean; reason: string }>({
  kind: 'role', user: null, role: 'member', isActive: true, reason: '',
})
const draft = reactive({ username: '', email: '', first_name: '', last_name: '', role: 'member' as AccessRole, reason: '' })
const roleLabels: Record<AccessRole, string> = { member: '成员', platform_admin: '平台管理员', super_admin: '超级管理员' }
const actionLabels: Record<string, string> = { create_user: '创建账号', change_role: '变更角色', change_status: '变更状态', reset_password: '重置临时密码', change_password: '修改密码', bootstrap_superuser: '初始化超级管理员' }

const visibleUsers = computed(() => {
  const needle = query.value.trim().toLowerCase()
  return needle ? users.value.filter((user) => [user.username, user.email, user.firstName, user.lastName].join(' ').toLowerCase().includes(needle)) : users.value
})

const load = async () => {
  loading.value = true; error.value = ''
  try { [users.value, audit.value] = await Promise.all([accessApi.listUsers(), accessApi.listAuditEvents()]) }
  catch (value) { error.value = apiErrorSummary(value, '权限数据读取失败') }
  finally { loading.value = false }
}

const closePassword = () => { temporaryPassword.value = ''; passwordOpen.value = false }
const discardPassword = () => { if (!temporaryPassword.value || window.confirm('关闭后无法再次查看该临时密码，确认已安全保存？')) closePassword() }
const showPassword = (value: string) => { temporaryPassword.value = value; passwordOpen.value = true }
const createUser = async () => {
  saving.value = true; createError.value = ''
  try {
    const result = await accessApi.createUser({ ...draft })
    createOpen.value = false
    Object.assign(draft, { username: '', email: '', first_name: '', last_name: '', role: 'member', reason: '' })
    showPassword(result.temporaryPassword)
    await load()
  } catch (value) { createError.value = apiErrorSummary(value, '账号创建失败') }
  finally { saving.value = false }
}
const openAction = (kind: 'role' | 'status' | 'reset', user: AccessUser) => {
  Object.assign(action, { kind, user, role: user.role, isActive: !user.isActive, reason: '' })
  actionError.value = ''
  actionOpen.value = true
}
const submitAction = async () => {
  if (!action.user) return
  saving.value = true; actionError.value = ''
  try {
    if (action.kind === 'role') await accessApi.changeRole(action.user.id, action.role, action.reason)
    else if (action.kind === 'status') await accessApi.changeStatus(action.user.id, action.isActive, action.reason)
    else showPassword((await accessApi.resetTemporaryPassword(action.user.id, action.reason)).temporaryPassword)
    actionOpen.value = false
    await load()
  } catch (value) { actionError.value = apiErrorSummary(value, '权限操作失败') }
  finally { saving.value = false }
}
const copyPassword = async () => { if (temporaryPassword.value) await navigator.clipboard.writeText(temporaryPassword.value) }
const closeCreate = () => { if (!saving.value) { createOpen.value = false; createError.value = '' } }
const closeAction = () => { if (!saving.value) { actionOpen.value = false; actionError.value = '' } }
const openCreate = () => { createError.value = ''; createOpen.value = true }
const dateTime = (value: string) => value ? new Date(value).toLocaleString('zh-CN') : '从未'
onMounted(load)
</script>

<template>
  <div class="platform-page permission-page">
    <header class="platform-hero"><div><span class="eyebrow">仅限超级管理员 / SUPER ADMIN ONLY</span><h2>权限管理</h2><p>统一管理账号、平台角色、启停状态与临时密码。所有变更均写入不可变审计记录。</p></div><button class="button ghost" :disabled="loading" @click="load"><RefreshCw :size="15" />重新读取</button></header>
    <div v-if="error" class="platform-alert danger">{{ error }}</div>
    <section class="platform-toolbar"><div><strong>账号与角色</strong><span>平台管理员拥有跨项目与系统配置权限；超级管理员额外管理账号和受管凭据。</span></div><div><input v-model="query" class="permission-search" placeholder="搜索账号或联系邮箱" /><button class="button primary small" @click="openCreate"><Plus :size="15" />创建账号</button></div></section>
    <div class="platform-table-wrap"><table class="platform-table"><thead><tr><th>账号</th><th>角色</th><th>状态</th><th>密码</th><th>最近登录</th><th>操作</th></tr></thead><tbody><tr v-for="user in visibleUsers" :key="user.id"><td><strong>{{ user.username }}</strong><small>{{ user.email || '未填邮箱' }}</small></td><td>{{ roleLabels[user.role] }}</td><td><PlatformStatusPill :status="user.isActive?'active':'inactive'" /></td><td>{{ user.mustChangePassword ? '首次改密待完成' : '已设置正式密码' }}</td><td>{{ dateTime(user.lastLogin) }}</td><td><div class="permission-actions"><button class="button ghost tiny" :disabled="user.id===auth.user?.id" @click="openAction('role',user)"><ShieldCheck :size="13" />角色</button><button class="button ghost tiny" :disabled="user.id===auth.user?.id" @click="openAction('status',user)"><component :is="user.isActive?UserX:UserCheck" :size="13" />{{ user.isActive?'停用':'启用' }}</button><button class="button ghost tiny" :disabled="user.id===auth.user?.id" @click="openAction('reset',user)"><KeyRound :size="13" />重置密码</button></div></td></tr><tr v-if="!visibleUsers.length"><td colspan="6" class="platform-empty">{{ loading?'正在读取…':'暂无匹配账号' }}</td></tr></tbody></table></div>
    <section class="platform-card permission-audit"><header><ShieldCheck :size="18" /><div><strong>权限审计</strong><small>不记录密码、Token 或请求正文</small></div></header><div class="platform-dense-list"><article v-for="item in audit.slice(0,100)" :key="item.id"><div><strong>{{ actionLabels[item.action] || item.action }} · {{ item.targetUsername }}</strong><small>{{ item.actorUsername || 'system' }} · user id {{ item.actorId || 'system' }} · {{ item.reason || '未填写说明' }} · {{ dateTime(item.createdAt) }}</small></div></article><p v-if="!audit.length" class="platform-empty">暂无审计记录</p></div></section>

    <BaseModal :open="createOpen" :dismissible="!saving" title="创建账号" eyebrow="一次性密码（One-time Password）" @close="closeCreate"><div class="permission-form"><label>账号<input v-model="draft.username" autocomplete="off" /></label><label>联系邮箱<input v-model="draft.email" type="email" autocomplete="off" /></label><label>名<input v-model="draft.first_name" autocomplete="off" /></label><label>姓<input v-model="draft.last_name" autocomplete="off" /></label><label>角色<select v-model="draft.role"><option v-for="(label,key) in roleLabels" :key="key" :value="key">{{ label }}</option></select></label><label>审计说明<textarea v-model="draft.reason" rows="3" /></label><div v-if="createError" class="platform-alert danger">{{ createError }}</div></div><template #footer><button class="button ghost" :disabled="saving" @click="closeCreate">取消</button><button class="button primary" :disabled="saving||!draft.username" @click="createUser">创建并生成临时密码</button></template></BaseModal>
    <BaseModal :open="actionOpen" :dismissible="!saving" :title="action.kind==='role'?'变更角色':action.kind==='status'?'变更账号状态':'重置临时密码'" eyebrow="受审计操作（Audited Action）" @close="closeAction"><div class="permission-form"><p>目标账号：<strong>{{ action.user?.username }}</strong></p><label v-if="action.kind==='role'">新角色<select v-model="action.role"><option v-for="(label,key) in roleLabels" :key="key" :value="key">{{ label }}</option></select></label><p v-else-if="action.kind==='status'">操作后账号将{{ action.isActive?'恢复登录':'立即失去登录权限' }}。</p><p v-else>现有登录凭据会立即失效，新临时密码要求用户首次登录修改。</p><label>审计说明<textarea v-model="action.reason" rows="3" /></label><div v-if="actionError" class="platform-alert danger">{{ actionError }}</div></div><template #footer><button class="button ghost" :disabled="saving" @click="closeAction">取消</button><button class="button primary" :disabled="saving||!action.reason.trim()" @click="submitAction">确认执行</button></template></BaseModal>
    <BaseModal :open="passwordOpen" title="一次性临时密码" eyebrow="仅显示一次（Show Once）" @close="discardPassword"><div class="one-time-password"><p>关闭后无法再次查看。请通过安全渠道发送给用户；不要粘贴到日志或审计说明。</p><code>{{ temporaryPassword }}</code><button class="button ghost" @click="copyPassword"><Copy :size="14" />复制到剪贴板</button></div><template #footer><button class="button primary" @click="closePassword">我已安全保存</button></template></BaseModal>
  </div>
</template>

<style scoped>
.permission-page{display:grid;gap:18px}.permission-search{min-width:220px}.permission-actions{display:flex;flex-wrap:wrap;gap:6px}.platform-table td:first-child{display:grid;gap:4px}.platform-table td small{color:#7f8996}.permission-audit{margin-top:6px}.permission-form{display:grid;gap:15px}.permission-form label{display:grid;gap:7px;color:#abb4c0}.permission-form input,.permission-form select,.permission-form textarea,.permission-search{border:1px solid #2a3039;border-radius:8px;background:#0b0e12;color:#f7f8fa;padding:10px 12px}.one-time-password{display:grid;gap:16px}.one-time-password code{padding:16px;border:1px solid #d8943e55;border-radius:10px;background:#d8943e12;color:#ffd08a;overflow-wrap:anywhere;font-size:15px}@media(max-width:720px){.platform-table-wrap{overflow:auto}.permission-search{min-width:0;width:100%}}
</style>
