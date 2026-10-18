import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { createServer } from 'vite'

async function harness(t:any){
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 const {createHistoryCleanupController,CLEANUP_CONFIRMATION}=await vite.ssrLoadModule('/src/services/historyCleanup.ts')
 const calls:any[]=[]
 let blockers:any[]=[],failPause=false,failClear=false,conflict=false,active=true,failRelease=false,getHook:(()=>Promise<void>)|undefined,postHook:(()=>Promise<void>)|undefined
 const plan=(scope:boolean)=>({include_maintenance:scope,plan_sha256:'a'.repeat(64),confirmation:'CLEAR_ALL_HISTORY_KEEP_ASSETS',can_execute:!blockers.length,blockers,delete_counts:{},preserve_counts:{}})
 const client={
  async get(url:string){calls.push(['GET',url]);if(getHook)await getHook();return {data:url.endsWith('background-pause/')?{active,include_maintenance:true,expires_at:active?new Date(Date.now()+300000).toISOString():null,server_time:new Date().toISOString()}:plan(url.endsWith('true'))}},
  async post(url:string,body:any){calls.push(['POST',url,body]);if(postHook)await postHook();if(url.endsWith('background-pause/')){if(conflict)throw {response:{status:409,data:{code:'history_cleanup_pause_conflict',detail:'another window'}}};if(failPause)throw Error('pause response unknown');return {data:{include_maintenance:body.include_maintenance,token:body.token,expires_at:new Date(Date.now()+300000).toISOString()}}}if(failClear)throw Error('clear response unknown');return {data:{retained_assets:156}}},
  async delete(url:string,body:any){calls.push(['DELETE',url,body]);if(failRelease)throw Error('release unknown')},
 }
 const state=createHistoryCleanupController(client)
 t.after(async()=>{await state.dispose();await vite.close()})
 return {state,calls,CLEANUP_CONFIRMATION,setBlockers:(v:any[])=>{blockers=v},setFailPause:(v:boolean)=>{failPause=v},setFailClear:(v:boolean)=>{failClear=v},setConflict:(v:boolean)=>{conflict=v},setActive:(v:boolean)=>{active=v},setFailRelease:(v:boolean)=>{failRelease=v},setGetHook:(v:any)=>{getHook=v},setPostHook:(v:any)=>{postHook=v}}
}
test('cleanup explicitly pauses then previews; close releases, never deletes',async t=>{
 const h=await harness(t),s=h.state
 assert.equal(h.calls.length,0)
 await s.preview();assert.deepEqual(h.calls.map(c=>c[0]),['POST','GET']);assert.equal(s.canExecute.value,false)
 await s.execute();assert.equal(h.calls.length,2)
 s.confirmation.value=h.CLEANUP_CONFIRMATION;assert.equal(s.canExecute.value,true)
 await s.close();assert.equal(h.calls.at(-1)[0],'DELETE');assert.equal(s.open.value,false)
 assert.equal(h.calls.filter(c=>c[0]==='POST'&&c[1]==='/maintenance/history/').length,0)
})
test('blocked previews poll reads only and never auto confirm or clear',async t=>{
 const h=await harness(t);t.mock.timers.enable({apis:['setTimeout','Date'],now:Date.now()})
 h.setBlockers([{label:'主机探测',id:'p'}]);await h.state.preview()
 h.setBlockers([]);t.mock.timers.tick(10000);for(let i=0;i<6;i++)await Promise.resolve()
 assert.equal(h.calls.filter(c=>c[0]==='GET').length,2);assert.equal(h.state.plan.value.can_execute,true)
 assert.equal(h.state.confirmation.value,'');assert.equal(h.state.canExecute.value,false)
 assert.equal(h.calls.filter(c=>c[0]==='POST').length,1)
 t.mock.timers.tick(300000);assert.equal(h.state.pause.value,null);assert.match(h.state.error.value,/暂停已到期/)
})
test('unknown pause response reuses identity and unknown cleanup is not retried or reported as untouched',async t=>{
 const h=await harness(t);t.mock.timers.enable({apis:['setTimeout','Date'],now:Date.now()})
 h.setFailPause(true);await h.state.preview();const token=h.calls[0][2].token
 h.setFailPause(false);await h.state.preview();assert.equal(h.calls[1][2].token,token)
 h.setFailClear(true);h.state.confirmation.value=h.CLEANUP_CONFIRMATION;await h.state.execute()
 assert.match(h.state.error.value,/结果尚未确认/);assert.equal(h.state.plan.value,null)
 t.mock.timers.tick(600000);await h.state.execute();assert.match(h.state.error.value,/结果尚未确认/)
 await h.state.close();assert.equal(h.state.phase.value,'unknown');assert.match(h.state.notice.value,/清理结果尚未确认/)
 await h.state.preview();assert.equal(h.state.open.value,false)
 assert.equal(h.calls.filter(c=>c[0]==='POST'&&c[1]==='/maintenance/history/').length,1)
})
test('explicit confirmation sends the exact preview and token once; success resumes',async t=>{
 const h=await harness(t);await h.state.preview();h.state.confirmation.value=h.CLEANUP_CONFIRMATION
 await Promise.all([h.state.execute(),h.state.execute()])
 const writes=h.calls.filter(c=>c[0]==='POST'&&c[1]==='/maintenance/history/')
 assert.equal(writes.length,1);assert.equal(writes[0][2].pause_token,h.calls[0][2].token)
 assert.equal(writes[0][2].plan_sha256,'a'.repeat(64));assert.equal(h.state.pause.value,null)
 assert.match(h.state.result.value,/156 项素材/)
})

test('definite cleanup rejection allows read-only recheck, while every request has a bounded timeout',async t=>{
 const h=await harness(t);await h.state.preview();h.state.confirmation.value=h.CLEANUP_CONFIRMATION
 h.setPostHook(()=>{throw {response:{status:409,data:{code:'history_cleanup_conflict',detail:'scope changed'}}}})
 await h.state.execute();assert.equal(h.state.phase.value,'error');assert.match(h.state.error.value,/未删除记录/)
 const before=h.calls.filter(c=>c[0]==='POST').length
 await h.state.refresh();assert.equal(h.state.phase.value,'ready');assert.equal(h.state.confirmation.value,'')
 assert.equal(h.calls.filter(c=>c[0]==='POST').length,before)
 const source=readFileSync(new URL('../src/services/historyCleanup.ts',import.meta.url),'utf8')
 assert.match(source,/timeout:30000/)
})

test('foreign window has countdown and read-only refresh; close never releases foreign pause',async t=>{
 const h=await harness(t);h.setConflict(true);await h.state.preview()
 assert.equal(h.state.phase.value,'occupied');assert.equal(h.state.remaining.value,300)
 assert.equal(h.state.nextCheckSeconds.value,10);assert.ok(h.state.lastChecked.value)
 const posts=h.calls.filter(c=>c[0]==='POST').length
 h.setActive(false);await h.state.refresh();assert.equal(h.state.phase.value,'available')
 assert.equal(h.calls.filter(c=>c[0]==='POST').length,posts)
 await h.state.close();assert.equal(h.state.open.value,false)
 assert.equal(h.calls.filter(c=>c[0]==='DELETE').length,0)
})

test('buttons have distinct immediate phases and retain previous blockers while refreshing',async t=>{
 const h=await harness(t);let resolvePost!:()=>void,resolveGet!:()=>void
 h.setPostHook(()=>new Promise<void>(resolve=>{resolvePost=resolve}))
 const preview=h.state.preview();assert.equal(h.state.phase.value,'pausing');assert.equal(h.state.pending.value,true)
 h.setGetHook(()=>new Promise<void>(resolve=>{resolveGet=resolve}));resolvePost()
 for(let i=0;i<8;i++)await Promise.resolve()
 assert.equal(h.state.phase.value,'checking');h.setBlockers([{id:'probe',label:'probe'}]);resolveGet();await preview
 assert.equal(h.state.phase.value,'blocked');assert.equal(h.state.plan.value.blockers.length,1)
 const refresh=h.state.refresh();assert.equal(h.state.phase.value,'checking');assert.equal(h.state.plan.value.blockers.length,1)
 h.setBlockers([]);resolveGet();await refresh;assert.equal(h.state.phase.value,'ready')
 h.setPostHook(undefined);h.setGetHook(undefined)
})

test('failed release still closes with truthful notice; scope is explicit through pause preview execute',async t=>{
 const h=await harness(t);assert.equal(h.state.includeMaintenance.value,false)
 h.state.includeMaintenance.value=true;await h.state.preview()
 assert.equal(h.calls[0][2].include_maintenance,true);assert.match(h.calls[1][1],/include_maintenance=true/)
 h.state.confirmation.value=h.CLEANUP_CONFIRMATION;await h.state.execute()
 assert.equal(h.calls.find(c=>c[0]==='POST'&&c[1]==='/maintenance/history/')[2].include_maintenance,true)
 await h.state.preview();h.setFailRelease(true);await h.state.close()
 assert.equal(h.state.open.value,false);assert.match(h.state.notice.value,/未获确认/)
 assert.equal(h.state.scopeLocked.value,true)
 h.setFailRelease(false);await h.state.preview();await h.state.close();assert.equal(h.state.scopeLocked.value,false)
})
test('cleanup entry names blockers and links without leaving the paused window; cancellation is top-level',()=>{
  const page=readFileSync(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
  const view=readFileSync(new URL('../src/components/HistoryCleanupPanel.vue',import.meta.url),'utf8')
  assert.ok(page.includes('<HistoryCleanupPanel v-if="auth.user?.isSuperuser"/>'))
  const detail=readFileSync(new URL('../src/components/TaskCenterDetail.vue',import.meta.url),'utf8')
  assert.match(page,/<RuntimeProbeDetail v-if="probeDetailId" :id="probeDetailId"/)
  assert.match(view,/block.label/);assert.match(view,/:href="block.detail_url" target="_blank" rel="noopener"/)
  const toolbar=detail.slice(detail.indexOf('<div class="detail-actions">'),detail.indexOf('<p v-if="error"'))
  assert.match(toolbar,/@click="openCancelTask"/);assert.match(toolbar,/刷新此任务/)
  assert.equal((detail.match(/@click="openCancelTask"/g)||[]).length,1)
  assert.match(view,/系统维护历史包含以下数据/);assert.match(view,/模型文件可用性观测/)
  assert.match(view,/这不是任务预计完成时间/);assert.match(view,/不显示估算百分比/)
  assert.doesNotMatch(view,/正在核对或执行/)
})
