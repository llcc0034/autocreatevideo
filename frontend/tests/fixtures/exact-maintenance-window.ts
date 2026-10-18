// Fixture navigation only. Keep the application's exact href, but load its real
// maintenance component in the same guarded fixture document, never the live app.
export function isolatedMaintenanceDestination(href: string, currentHref: string, exactRoute: string): string {
  const current = new URL(currentHref), target = new URL(href, current)
  if (target.origin !== current.origin || target.pathname + target.search !== exactRoute || target.hash) {
    throw Error('隔离维护窗口拒绝非精确资源入口')
  }
  current.searchParams.set('maintenance_route', exactRoute)
  return current.href
}
