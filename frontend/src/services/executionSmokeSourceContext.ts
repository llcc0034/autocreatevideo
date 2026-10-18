import { adaptExecutionPage } from './executionRegistry'

export interface SmokeSourceContext {
  run_id: string; stage_key: string; run_plan_sha256: string
  project_id: string; project_title: string; run_created_at: string
}
export function adaptSmokeSourceContexts(value: unknown) {
  return adaptExecutionPage(value, row => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) throw new Error('审核来源上下文不完整')
    const r = row as Record<string, unknown>
    const keys = ['run_id','stage_key','run_plan_sha256','project_id','project_title','run_created_at']
    if (keys.some(key => typeof r[key] !== 'string' || !r[key])
      || !/^[a-f0-9]{64}$/.test(String(r.run_plan_sha256))) throw new Error('审核来源上下文缺少明确身份')
    return r as unknown as SmokeSourceContext
  })
}
