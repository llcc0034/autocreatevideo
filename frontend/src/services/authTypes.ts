export type AccessRole = 'member' | 'platform_admin' | 'super_admin'

export interface AuthenticatedUser {
  id: string
  username: string
  name: string
  email: string
  isStaff: boolean
  isSuperuser: boolean
  role: AccessRole
  mustChangePassword: boolean
}

export const adaptAuthenticatedUser = (
  value: Record<string, unknown>,
  fallbackName = '用户',
): AuthenticatedUser => {
  const username = String(value.username || value.email || '')
  const role = ['member', 'platform_admin', 'super_admin'].includes(String(value.role))
    ? String(value.role) as AccessRole
    : value.is_superuser === true
      ? 'super_admin'
      : value.is_staff === true
        ? 'platform_admin'
        : 'member'
  return {
    id: String(value.id || ''),
    username,
    name: String(value.first_name || username || fallbackName),
    email: String(value.email || username),
    isStaff: value.is_staff === true,
    isSuperuser: value.is_superuser === true,
    role,
    mustChangePassword: value.must_change_password === true,
  }
}
