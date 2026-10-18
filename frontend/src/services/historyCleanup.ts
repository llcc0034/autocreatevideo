import { computed, ref } from 'vue'
import { apiErrorPresentation, apiErrorSummary } from './apiErrorPresentation'

export interface HistoryCleanupPlan {
  include_maintenance: boolean
  plan_sha256: string
  delete_counts: Record<string, number>
  preserve_counts: Record<string, number>
  blockers: Array<{ model: string; id: string; reason: string; label: string; detail_url: string | null; link_label: string;
    status: string | null; started_at: string | null; heartbeat_at: string | null; progress_marker: string | null }>
  can_execute: boolean
  confirmation: string
}
interface Pause { token: string; expires_at: string; include_maintenance: boolean }
interface PauseStatus { active: boolean; expires_at: string | null; server_time: string; include_maintenance: boolean }
interface Client {
  get: (url: string, options?: {timeout:number}) => Promise<{ data: any }>
  post: (url: string, body: unknown, options?: {timeout:number}) => Promise<{ data: any }>
  delete: (url: string, options: { data: unknown; timeout?:number }) => Promise<unknown>
}
type Phase = 'idle' | 'pausing' | 'checking' | 'blocked' | 'ready' | 'occupied' | 'available' | 'expired' | 'releasing' | 'deleting' | 'done' | 'error' | 'unknown'
const HISTORY = '/maintenance/history/', PAUSE = HISTORY + 'background-pause/'
export const CLEANUP_CONFIRMATION = '清空所选历史并保留素材'
const REQUEST = {timeout:30000}

export function createHistoryCleanupController(client: Client) {
  const open = ref(false), pending = ref(false), error = ref(''), confirmation = ref(''), result = ref(''), notice = ref('')
  const plan = ref<HistoryCleanupPlan | null>(null), pause = ref<Pause | null>(null), occupied = ref<PauseStatus | null>(null)
  const phase = ref<Phase>('idle'), now = ref(Date.now()), phaseStarted = ref(Date.now()), lastChecked = ref<number | null>(null), nextCheck = ref<number | null>(null)
  const includeMaintenance = ref(false), unresolvedPause = ref(false)
  const scopeLocked = computed(() => open.value || pending.value || unresolvedPause.value || phase.value === 'unknown')
  let selectedScope = false
  let token = '', disposed = false, serverOffset = 0
  let poll: ReturnType<typeof setTimeout> | undefined, expiry: ReturnType<typeof setTimeout> | undefined, clock: ReturnType<typeof setInterval> | undefined
  const stopTimers = () => { clearTimeout(poll); clearTimeout(expiry); nextCheck.value = null }
  const startClock = () => { clearInterval(clock); now.value = Date.now(); clock = setInterval(() => { now.value = Date.now() }, 1000) }
  const setPhase = (value: Phase) => { phase.value = value; phaseStarted.value = Date.now(); now.value = Date.now() }
  const elapsed = computed(() => Math.max(0, Math.floor((now.value - phaseStarted.value) / 1000)))
  const remaining = computed(() => {
    const end = pause.value?.expires_at || occupied.value?.expires_at
    return end ? Math.max(0, Math.ceil((Date.parse(end) - now.value - (pause.value ? 0 : serverOffset)) / 1000)) : null
  })
  const nextCheckSeconds = computed(() => nextCheck.value === null ? null : Math.max(0, Math.ceil((nextCheck.value - now.value) / 1000)))
  const canExecute = computed(() => phase.value === 'ready' && !pending.value && !error.value && !!pause.value
    && Date.parse(pause.value.expires_at) > now.value && !!plan.value?.can_execute && confirmation.value === CLEANUP_CONFIRMATION)
  const title = computed(() => ({ idle:'尚未开始', pausing:'正在暂停新增后台检查', checking:'正在核对清理范围', blocked:'等待阻挡项解除', ready:'范围核对完成，等待确认', occupied:'等待其他清理窗口释放', available:'占用已解除，可以重新预览', expired:'暂停已到期，请重新预览', releasing:'正在恢复后台检查', deleting:'正在提交清理，等待服务器结果', done:'所选历史已清理', error:'当前操作未完成', unknown:'清理结果尚未确认' })[phase.value])
  const step = computed(() => ['deleting','done','unknown'].includes(phase.value) ? 4 : phase.value === 'ready' ? 3 : ['checking','blocked'].includes(phase.value) ? 2 : 1)
  const expired = () => !pause.value || Date.parse(pause.value.expires_at) <= Date.now()
  const expire = () => {
    stopTimers(); pause.value = null; token = ''; unresolvedPause.value = false; plan.value = null; confirmation.value = ''; setPhase('expired')
    error.value = '暂停已到期，后台检查已自动恢复。请重新暂停并预览；本窗口没有执行清理。'
  }
  const schedule = (action: () => Promise<void>) => {
    clearTimeout(poll); nextCheck.value = Date.now() + 10000
    poll = setTimeout(() => { void action() }, 10000)
  }
  const readPlan = async () => {
    const response = await client.get(HISTORY + '?include_maintenance=' + selectedScope, REQUEST)
    if (disposed) return
    if (response.data.include_maintenance !== selectedScope) throw Error('返回的清理范围与本次选择不一致，请关闭后重新预览。')
    if (expired()) { expire(); return }
    plan.value = response.data; confirmation.value = ''; lastChecked.value = Date.now()
    setPhase(response.data.can_execute ? 'ready' : 'blocked')
    if (response.data.blockers.length) schedule(refresh)
  }
  const inspectOccupation = async () => {
    const data: PauseStatus = (await client.get(PAUSE, REQUEST)).data
    if (disposed) return
    if (typeof data.active !== 'boolean' || !Number.isFinite(Date.parse(data.server_time)) || (data.active && !Number.isFinite(Date.parse(data.expires_at || '')))) throw Error('暂停状态响应不完整，无法确定占用情况。')
    serverOffset = Date.parse(data.server_time) - Date.now(); lastChecked.value = Date.now()
    occupied.value = data.active ? data : null
    setPhase(data.active ? 'occupied' : 'available')
    if (data.active) schedule(refresh)
  }
  const refresh = async () => {
    if (pending.value || disposed || phase.value === 'unknown') return
    const checkingOther = !pause.value && (phase.value === 'occupied' || !!occupied.value)
    if (!checkingOther && expired()) { expire(); return }
    clearTimeout(poll); nextCheck.value = null; pending.value = true; error.value = ''
    // Keep the last blockers and checked-at time visible while reading.
    if (!checkingOther) setPhase('checking')
    try { if (checkingOther) await inspectOccupation(); else await readPlan() }
    catch (cause) { error.value = apiErrorSummary(cause); setPhase('error') }
    finally { pending.value = false }
  }
  const preview = async () => {
    if (pending.value || disposed || phase.value === 'unknown') return
    stopTimers(); startClock(); pending.value = true; error.value = ''; result.value = ''; notice.value = ''; plan.value = null; occupied.value = null
    confirmation.value = ''; lastChecked.value = null; open.value = true; setPhase('pausing')
    if (!token) selectedScope = includeMaintenance.value
    try {
      token ||= crypto.randomUUID() // Reuse after an unknown POST, never silently create a new lease.
      unresolvedPause.value = true
      try {
        const data: Pause = (await client.post(PAUSE, { token, include_maintenance:selectedScope }, REQUEST)).data
        if (data.token !== token || data.include_maintenance !== selectedScope || !Number.isFinite(Date.parse(data.expires_at))) throw Error('暂停响应与本窗口范围不一致，暂停结果尚未确认。')
        pause.value = data
      }
      catch (cause) {
        if (apiErrorPresentation(cause).code !== 'history_cleanup_pause_conflict') throw cause
        // Definite rejection: this window owns nothing and must not release another lease.
        token = ''; pause.value = null; unresolvedPause.value = false; setPhase('occupied'); await inspectOccupation(); return
      }
      if (disposed) return
      if (expired()) { expire(); return }
      expiry = setTimeout(expire, Math.max(0, Date.parse(pause.value!.expires_at) - Date.now()))
      setPhase('checking'); await readPlan()
    } catch (cause) { error.value = apiErrorSummary(cause); setPhase('error') }
    finally {
      pending.value = false
      if (disposed) try { await release() } catch { /* Server expiry recovers an abandoned page. */ }
    }
  }
  const release = async () => {
    stopTimers()
    if (token) await client.delete(PAUSE, { data: { token }, ...REQUEST })
    token = ''; pause.value = null; unresolvedPause.value = false
  }
  const close = async () => {
    if (pending.value) return
    const unknownResult = phase.value === 'unknown'
    pending.value = true
    if (token) setPhase('releasing')
    try { await release() }
    catch (cause) {
      // Closing remains possible; failure is never presented as successful recovery.
      if (apiErrorPresentation(cause).code === 'history_cleanup_pause_conflict') {
        token = ''; pause.value = null; unresolvedPause.value = false
        notice.value = '窗口已关闭。后台暂停由其他窗口持有，本窗口未更改它。'
      } else notice.value = '窗口已关闭，但恢复检查未获确认；暂停会按原有效期自动失效。本次范围保持锁定，重新打开可用原请求核对。'
    } finally {
      if (unknownResult) { setPhase('unknown'); notice.value += ' 清理结果尚未确认，请先查看列表和任务状态，核实后整页刷新；不要重复提交。' }
      clearInterval(clock); open.value = false; plan.value = null; occupied.value = null; confirmation.value = ''; pending.value = false
    }
  }
  const execute = async () => {
    if (!canExecute.value || !plan.value) return
    if (expired()) { expire(); return }
    stopTimers(); pending.value = true; error.value = ''; setPhase('deleting')
    try {
      const response = await client.post(HISTORY, { plan_sha256:plan.value.plan_sha256, confirmation:plan.value.confirmation, pause_token:token, include_maintenance:plan.value.include_maintenance }, REQUEST)
      if (disposed) return
      result.value = '所选' + (selectedScope ? '业务及系统维护历史' : '业务历史') + '已清理，保留 ' + response.data.retained_assets + ' 项素材。文件未删除，后台检查已恢复。请整页刷新，清除旧工作台缓存。'
      token = ''; pause.value = null; unresolvedPause.value = false; setPhase('done')
    } catch (cause) {
      const rejected = ['history_cleanup_conflict','history_cleanup_pause_conflict'].includes(apiErrorPresentation(cause).code || '')
      error.value = apiErrorSummary(cause) + (rejected ? ' 本次未删除记录，请重新核对范围。' : ' 清理结果尚未确认，先查看列表，不要重复清理。')
      setPhase(rejected ? 'error' : 'unknown')
    } finally { plan.value = null; confirmation.value = ''; pending.value = false }
  }
  const dispose = async () => {
    disposed = true; stopTimers(); clearInterval(clock)
    if (!pending.value) try { await release() } catch { /* Server expiry recovers an abandoned page. */ }
  }
  return { open,pending,error,confirmation,result,notice,plan,pause,occupied,phase,title,step,elapsed,remaining,lastChecked,nextCheckSeconds,canExecute,includeMaintenance,scopeLocked,preview,refresh,close,execute,dispose }
}
