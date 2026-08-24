import { api, fetchAllPages, unwrap } from './api'
import type { AccessRole } from './authTypes'

export interface AccessUser {
  id: string
  username: string
  email: string
  firstName: string
  lastName: string
  role: AccessRole
  isActive: boolean
  mustChangePassword: boolean
  passwordChangedAt: string
  lastLogin: string
  dateJoined: string
}

export interface AccessAuditEvent {
  id: string
  actorId: string
  actorUsername: string
  targetUsername: string
  action: string
  beforeRole: string
  afterRole: string
  beforeActive: boolean | null
  afterActive: boolean | null
  reason: string
  createdAt: string
}

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object'
  ? value as Record<string, unknown> : {}

const adaptUser = (value: unknown): AccessUser => {
  const row = record(value)
  return {
    id: String(row.id || ''), username: String(row.username || ''), email: String(row.email || ''),
    firstName: String(row.first_name || ''), lastName: String(row.last_name || ''),
    role: String(row.role || 'member') as AccessRole, isActive: row.is_active === true,
    mustChangePassword: row.must_change_password === true,
    passwordChangedAt: String(row.password_changed_at || ''), lastLogin: String(row.last_login || ''),
    dateJoined: String(row.date_joined || ''),
  }
}

const adaptAudit = (value: unknown): AccessAuditEvent => {
  const row = record(value)
  return {
    id: String(row.id || ''), actorId: String(row.actor || ''), actorUsername: String(row.actor_username || 'system'),
    targetUsername: String(row.target_username || ''), action: String(row.action || ''),
    beforeRole: String(row.before_role || ''), afterRole: String(row.after_role || ''),
    beforeActive: typeof row.before_active === 'boolean' ? row.before_active : null,
    afterActive: typeof row.after_active === 'boolean' ? row.after_active : null,
    reason: String(row.reason || ''), createdAt: String(row.created_at || ''),
  }
}

const body = <T>(value: unknown): T => unwrap<T>(value)

export const accessApi = {
  async listUsers(): Promise<AccessUser[]> {
    return (await fetchAllPages('/access/users/', { page_size: 500 })).map(adaptUser)
  },
  async createUser(payload: {
    username: string; email: string; first_name?: string; last_name?: string; role: AccessRole; reason: string
  }): Promise<{ user: AccessUser; temporaryPassword: string }> {
    const row = record(body((await api.post('/access/users/', payload)).data))
    return { user: adaptUser(row.user), temporaryPassword: String(row.temporary_password || '') }
  },
  async changeRole(id: string, role: AccessRole, reason: string): Promise<AccessUser> {
    return adaptUser(body((await api.post(`/access/users/${id}/role/`, { role, reason })).data))
  },
  async changeStatus(id: string, isActive: boolean, reason: string): Promise<AccessUser> {
    return adaptUser(body((await api.post(`/access/users/${id}/status/`, { is_active: isActive, reason })).data))
  },
  async resetTemporaryPassword(id: string, reason: string): Promise<{ user: AccessUser; temporaryPassword: string }> {
    const row = record(body((await api.post(`/access/users/${id}/temporary-password/`, { reason })).data))
    return { user: adaptUser(row.user), temporaryPassword: String(row.temporary_password || '') }
  },
  async listAuditEvents(): Promise<AccessAuditEvent[]> {
    return (await fetchAllPages('/access/audit-events/', { page_size: 500 })).map(adaptAudit)
  },
}
