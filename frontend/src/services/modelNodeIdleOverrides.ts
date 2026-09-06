export interface NodeIdleLimits { maxSeconds: number; maxOverrides: number }

export const validNodeIdleLimits = (limits?: NodeIdleLimits | null): limits is NodeIdleLimits => Boolean(limits
  && Number.isSafeInteger(limits.maxSeconds) && limits.maxSeconds > 0
  && Number.isSafeInteger(limits.maxOverrides) && limits.maxOverrides > 0)

export const nodeIdleObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)

/** Minute entry is only a view conversion; the persisted contract uses exact integer seconds. */
export function nodeIdleSeconds(minutes: string): number | null {
  if (!/^\+?(?:\d+(?:\.\d*)?|\.\d+)$/.test(minutes.trim())) return null
  const seconds = Number(minutes) * 60
  const rounded = Math.round(seconds)
  return Number.isSafeInteger(rounded) && rounded > 0 && Math.abs(seconds - rounded) < 1e-7 ? rounded : null
}

export function nodeIdleOverrideErrors(value: unknown, limits?: NodeIdleLimits | null): string[] {
  if (value === undefined || value === null) return []
  if (!nodeIdleObject(value)) return ['特定节点等待时限必须是节点类型与秒数的对应表']
  const entries = Object.entries(value)
  const errors: string[] = []
  if (entries.length && !validNodeIdleLimits(limits)) errors.push('等待时限的服务端安全上限尚未读取，请重新读取系统设置')
  if (validNodeIdleLimits(limits) && entries.length > limits.maxOverrides) errors.push(`特定节点等待规则最多 ${limits.maxOverrides} 条`)
  const types = new Set<string>()
  for (const [type, seconds] of entries) {
    const key = type.trim()
    if (!key) errors.push('请选择节点类型')
    else if (key.length > 191 || /[\u0000-\u001f\u007f]/.test(key)) errors.push(`节点类型不能超过 191 个字符或包含控制字符：${type}`)
    if (types.has(key)) errors.push(`节点类型重复：${key}`)
    types.add(key)
    if (typeof seconds !== 'number' || !Number.isSafeInteger(seconds) || seconds <= 0) errors.push(`节点类型 ${type || '(未选择)'} 的等待时长必须换算为正整数秒；0 不表示无限等待`)
    else if (validNodeIdleLimits(limits) && seconds > limits.maxSeconds) errors.push(`节点类型 ${type} 的等待时长超过服务端上限 ${limits.maxSeconds} 秒`)
  }
  return errors
}
