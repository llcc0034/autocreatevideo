import { shallowRef } from 'vue'

export const ROUTE_MODULE_RECOVERY_KEY = 'cineforge_route_module_recovery_path'

type RecoveryStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export type RouteModuleRecoveryAction = 'ignore' | 'reload' | 'report'

export interface RouteModuleFailure {
  path: string
}

const dynamicImportFailurePatterns = [
  /failed to fetch dynamically imported module/iu,
  /error loading dynamically imported module/iu,
  /importing a module script failed/iu,
  /failed to load module script/iu,
]

const errorMessage = (error: unknown) => {
  if (error instanceof Error) return error.message
  return typeof error === 'string' ? error : ''
}

export const isRouteModuleLoadFailure = (error: unknown) => {
  const message = errorMessage(error)
  return dynamicImportFailurePatterns.some((pattern) => pattern.test(message))
}

export const routeModuleRecoveryAction = (
  error: unknown,
  fullPath: string,
  storage: RecoveryStorage | undefined,
): RouteModuleRecoveryAction => {
  if (!isRouteModuleLoadFailure(error)) return 'ignore'
  if (!storage) return 'report'

  try {
    if (storage.getItem(ROUTE_MODULE_RECOVERY_KEY) === fullPath) return 'report'
    storage.setItem(ROUTE_MODULE_RECOVERY_KEY, fullPath)
    return 'reload'
  } catch {
    // Without durable state a reload could loop indefinitely, so surface the failure instead.
    return 'report'
  }
}

export const resolveRouteRecoveryStorage = (resolve: () => RecoveryStorage) => {
  try {
    return resolve()
  } catch {
    return undefined
  }
}

export const clearRouteModuleRecovery = (storage: RecoveryStorage | undefined) => {
  if (!storage) return
  try {
    storage.removeItem(ROUTE_MODULE_RECOVERY_KEY)
  } catch {
    // Storage can be unavailable in hardened browser modes; successful navigation is enough.
  }
}

export const routeModuleFailure = shallowRef<RouteModuleFailure | null>(null)

export const reportRouteModuleFailure = (path: string) => {
  routeModuleFailure.value = { path }
}

export const clearRouteModuleFailure = () => {
  routeModuleFailure.value = null
}
