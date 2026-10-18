<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { RefreshCw, ShieldCheck } from 'lucide-vue-next'
import PlatformStatusPill from './PlatformStatusPill.vue'
import { deploymentCapabilitySignal } from '@/services/deploymentCapabilitySmoke'
import { operationLabel } from '@/services/presentationLabels'
import { taskCenterTarget } from '@/services/taskCenter'
import type { AcceptanceSmokeDeploymentRow } from '@/services/platformTypes'

const props = defineProps<{ row: AcceptanceSmokeDeploymentRow | null; loading?: boolean; error?: string }>()
defineEmits<{ refresh: [] }>()
const now = ref(Date.now())
const rows = computed(() => props.row?.deploymentSmoke.operations.map(operation => ({
  operation, signal: deploymentCapabilitySignal(props.row!, operation, now.value),
  snapshot: operation.freshPassedSnapshot || operation.latestSnapshot,
})) || [])
const applicable = computed(() => rows.value.filter(item => item.operation.applicable))
const profileRows = (operation: typeof rows.value[number]['operation']) => operation.promptProfiles.map(profile => ({
  ...profile,
  signal: deploymentCapabilitySignal(props.row!, { ...operation, ...profile }, now.value),
  snapshot: profile.freshPassedSnapshot || profile.latestSnapshot,
}))
const validCount = computed(() => applicable.value.filter(item => item.signal.tone === 'linked').length)
const dateTime = (value: string) => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '时间不可用'
let timer: ReturnType<typeof setTimeout> | undefined
let mounted = false
const tick = () => {
  clearTimeout(timer)
  now.value = Date.now()
  if (!mounted) return
  const deadlines = rows.value.flatMap(item => [item.snapshot, ...item.operation.promptProfiles.map(profile => profile.freshPassedSnapshot || profile.latestSnapshot)])
    .map(snapshot => Date.parse(snapshot?.expiresAt || '')).filter(time => time > now.value)
  if (deadlines.length) timer = setTimeout(tick, Math.min(Math.min(...deadlines) - now.value + 1, 2147483647))
}
watch(() => props.row, tick)
onMounted(() => { mounted = true; tick(); window.addEventListener('focus', tick) })
onBeforeUnmount(() => { mounted = false; clearTimeout(timer); window.removeEventListener('focus', tick) })
</script>

<template>
  <section class="capability-smoke" aria-label="按能力查看烟测">
    <header><strong><ShieldCheck :size="14" aria-hidden="true"/>能力烟测</strong><span v-if="row && !loading && !error && applicable.length">有效通过 {{ validCount }} / {{ applicable.length }} 项</span><button type="button" class="button ghost tiny" :disabled="loading" aria-label="重新读取能力烟测" @click="$emit('refresh')"><RefreshCw :size="12"/></button></header>
    <p v-if="error" role="alert">烟测读取失败：{{ error }}</p>
    <p v-else-if="loading" role="status">正在读取各能力的烟测证据…</p>
    <p v-else-if="!row" role="status">尚未取得部署验收数据，请重新读取；不代表没有烟测记录。</p>
    <template v-else>
      <p v-if="!rows.length">{{ row.deployment.profileContractState!=='current' || row.deployment.targetState!=='current' ? '模型或部署合同不可核验，请查看验收详情。' : '当前合同未声明可直接烟测的能力。' }}</p>
      <ul v-else>
        <li v-for="item in rows" :key="item.operation.operation">
          <div class="capability-heading"><strong>{{ operationLabel(item.operation.operation) }}</strong><span class="evidence-signal" :class="item.signal.tone" :title="item.signal.description">{{ item.signal.label }}</span></div>
          <p v-if="item.snapshot">证据时间 <time :datetime="item.snapshot.capturedAt">{{ dateTime(item.snapshot.capturedAt) }}</time> · 到期时间 <time :datetime="item.snapshot.expiresAt">{{ dateTime(item.snapshot.expiresAt) }}</time><span v-if="Date.parse(item.snapshot.expiresAt) <= now">（已过期）</span></p>
          <p v-else>{{ item.signal.description }}</p>
          <p v-if="item.snapshot?.promptProfile">本次证据策略：{{ item.snapshot.promptProfile.name }} · v{{ item.snapshot.promptProfile.version }}</p>
          <ul v-if="item.operation.promptProfiles.length" aria-label="已提交策略版本的验收记录">
            <li v-for="profile in profileRows(item.operation)" :key="String(profile.promptProfile.id) + ':' + String(profile.promptProfile.execution_mode)">
              <div class="capability-heading"><span>{{ profile.promptProfile.name }} · v{{ profile.promptProfile.version }}</span><span :class="['evidence-signal', profile.signal.tone]">{{ profile.signal.label }}</span></div>
              <p v-if="profile.promptProfile.execution_mode">执行模式：{{ profile.promptProfile.execution_mode }}</p>
              <p v-if="profile.snapshot">到期时间 {{ dateTime(profile.snapshot.expiresAt) }}</p>
            </li>
          </ul>
          <div v-if="item.operation.latestAttempt" class="attempt-line">
            <span>最近任务</span><PlatformStatusPill :status="item.operation.latestAttempt.status" :bilingual="false"/>
            <time :datetime="item.operation.latestAttempt.finishedAt || item.operation.latestAttempt.createdAt">{{item.operation.latestAttempt.finishedAt ? '完成于' : '创建于'}} {{ dateTime(item.operation.latestAttempt.finishedAt || item.operation.latestAttempt.createdAt) }}</time>
            <RouterLink v-if="item.operation.latestAttempt.id" class="button ghost tiny" :aria-label="`${operationLabel(item.operation.operation)}：查看最近任务输入、输出与记录`"
              :to="taskCenterTarget({category:'smoke',sourceId:item.operation.latestAttempt.id})">查看最近任务输入、输出与记录</RouterLink>
            <small>任务标识：{{item.operation.latestAttempt.id || '记录缺少标识，无法定位'}}</small>
            <small v-if="item.operation.latestAttempt.errorCode">任务诊断码：{{item.operation.latestAttempt.errorCode}}</small>
          </div>
          <p v-if="!item.operation.applicable" class="history-note">当前不适用；保留历史记录供查看。</p>
        </li>
      </ul>
      <p v-if="rows.length" class="legend">能力摘要取最近完成的基础验收，不用更早的成功覆盖失败。最近任务可能仍在执行或与上方验收快照不是同一次；历史链接按该任务标识查询，不新建或重跑。已提交的策略版本分别展示，不代表全部策略通过，也不能替代执行实例的组合验收。</p>
    </template>
  </section>
</template>

<style scoped>
.attempt-line>small{flex-basis:100%;overflow-wrap:anywhere}.attempt-line>a{min-height:36px;white-space:normal;overflow-wrap:anywhere}.attempt-line>a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.capability-smoke{display:grid;align-content:start;gap:6px;min-width:0}header,.capability-heading,.attempt-line{display:flex;align-items:center;gap:8px;flex-wrap:wrap}header{min-height:30px}header>strong{display:flex;align-items:center;gap:6px;color:var(--text);font-size:12px}header>span{font-size:11px;color:var(--text-secondary)}header>button{margin-left:auto;min-height:36px;min-width:36px}ul{list-style:none;padding:0;margin:0;display:grid;gap:6px}li{padding:8px 10px;border:1px solid var(--border);border-radius:6px;min-width:0}.capability-heading{justify-content:space-between}.capability-heading>strong{font-size:11px;color:var(--text);overflow-wrap:anywhere}.evidence-signal{font-size:11px;white-space:nowrap;color:var(--text-secondary)}.evidence-signal.linked{color:var(--green)}.evidence-signal.warning{color:var(--accent)}.evidence-signal.danger,p[role=alert]{color:var(--red)}p,.attempt-line{font-size:11px;color:var(--text-secondary);line-height:1.6;overflow-wrap:anywhere}p{margin:4px 0 0}.attempt-line{margin-top:5px}.legend,.history-note{font-size:10px}button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:640px){.capability-heading{align-items:flex-start;flex-direction:column}.attempt-line{gap:5px}}
</style>
