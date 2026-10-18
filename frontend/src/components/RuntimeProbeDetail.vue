<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import type { RuntimeHost, RuntimeHostProbeAttempt } from '@/services/platformTypes'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { useAuthStore } from '@/stores/auth'
import { taskTime } from '@/services/taskCenter'
import PlatformStatusPill from './PlatformStatusPill.vue'

const props=defineProps<{id:string;hosts?:Pick<RuntimeHost,'id'|'name'>[]}>(),auth=useAuthStore()
const attempt=ref<RuntimeHostProbeAttempt|null>(null),loading=ref(false),error=ref('')
const hostName=computed(()=>props.hosts?.find(host=>host.id===attempt.value?.host)?.name||'主机名称尚未读取')
let serial=0
async function refresh(){
 const id=props.id,n=++serial;loading.value=true;error.value='';attempt.value=null
 try{
  if(!/^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(id))throw Error('探测记录标识无效')
  const result=await platformApi.getRuntimeHostProbeAttempt(id)
  if(n!==serial)return
  if(result.id!==id)throw Error('返回的探测记录与指定任务不一致')
  attempt.value=result
 }catch(cause){if(n===serial)error.value=apiErrorSummary(cause)}finally{if(n===serial)loading.value=false}
}
watch(()=>[props.id,auth.user?.id],()=>{void refresh()},{immediate:true})
onBeforeUnmount(()=>{serial++})
</script>
<template>
 <section class="probe-detail" :aria-busy="loading">
  <button class="button ghost small" :disabled="loading" @click="refresh">刷新此探测</button>
  <p>当前只读取链接指定的探测记录，不替换为最新探测、不重发任务。未结束或结果未知时不能跳过清理保护。</p>
  <p v-if="loading" role="status">正在读取指定探测…</p>
  <p v-if="error" class="platform-alert danger" role="alert">{{error}}</p>
  <template v-if="attempt"><PlatformStatusPill :status="attempt.status"/>
   <dl><dt>主机</dt><dd>{{hostName}}</dd><dt>触发方式</dt><dd>{{attempt.trigger}}</dd><dt>探测类型</dt><dd>{{attempt.probeKind}}</dd><dt>开始 / 完成</dt><dd>{{taskTime(attempt.startedAt)}} / {{taskTime(attempt.finishedAt)}}</dd><dt>最后心跳</dt><dd>{{taskTime(attempt.heartbeatAt)}}</dd><dt>当前进度</dt><dd>{{attempt.progressMarker||'尚无记录'}}</dd></dl>
   <details><summary>记录标识</summary><p>探测：{{attempt.id}}</p><p>主机：{{attempt.host}}</p></details>
   <p v-if="attempt.errorCode||attempt.errorMessage" role="alert">{{attempt.errorCode}} · {{attempt.errorMessage}}</p>
  </template>
 </section>
</template>
<style scoped>
.probe-detail{display:grid;gap:12px;min-width:0;font-size:13px}.probe-detail p{line-height:1.7;margin:0;overflow-wrap:anywhere}.probe-detail dl{display:grid;grid-template-columns:100px minmax(0,1fr);gap:12px}.probe-detail dd{margin:0;overflow-wrap:anywhere}.probe-detail button{justify-self:start}@media(max-width:500px){.probe-detail dl{grid-template-columns:1fr}}
</style>
