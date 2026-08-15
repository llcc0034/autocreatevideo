<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { AlertTriangle, CheckCircle2, KeyRound, LoaderCircle, RefreshCw, Server, ShieldCheck } from 'lucide-vue-next'
import { repository } from '@/services/repository'
import type { H3PromptOptimizerCapability } from '@/types'

const capability = ref<H3PromptOptimizerCapability | null>(null)
const loading = ref(false)
const failed = ref(false)

const state = computed(() => {
  const value = capability.value
  if (!value) return { tone: 'neutral', label: '状态未知' }
  if (value.enabled && value.configured) return { tone: 'ready', label: '已启用 · 配置完整' }
  if (value.enabled) return { tone: 'danger', label: '已启用 · 配置不完整' }
  if (value.configured) return { tone: 'staged', label: '配置已就绪 · 尚未启用' }
  return { tone: 'neutral', label: '未启用 · 未配置' }
})

const transportLabel = computed(() => ({
  local: '本地 OpenAI-compatible 服务',
  remote: '显式授权的远程服务',
  unconfigured: '尚未选择传输方式',
})[capability.value?.transport || 'unconfigured'])

const failureLabel = computed(() => capability.value?.failureMode === 'block'
  ? 'block：优化失败会阻断提示词生成'
  : 'warn：优化失败会保留原提示词并告警')

const gateMessage = computed(() => {
  const value = capability.value
  if (!value) return '尚未读取生产语言门禁状态。'
  if (value.languageGate === 'block') {
    if (!value.enabled || !value.configured) {
      return '生产语言门禁为 block：视频导演描述仍含待改写中文时，提示词预览仍可生成，但生产排队会被后端拒绝。请按质量面板列出的字段手工改写为英文；中文对白、歌词和真实屏显文字保留原语言。'
    }
    return '生产语言门禁为 block：只有优化器成功完成英文改写，或质量面板已无待改写导演字段，视频任务才可进入生产队列。中文对白不触发门禁。'
  }
  return '生产语言门禁为 warn：待英文改写的视频导演字段会标记 needs_attention，但仍允许排队。该模式仅用于非阻断诊断，正式生产前仍应完成英文改写；中文对白不受影响。'
})

const load = async () => {
  if (loading.value) return
  loading.value = true
  failed.value = false
  try {
    capability.value = await repository.getH3PromptOptimizerCapability()
  } catch {
    capability.value = null
    failed.value = true
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="panel optimizer-capability">
    <header>
      <div>
        <span class="eyebrow">H3 DIRECTOR PROSE OPTIMIZER</span>
        <h3>英文语义优化器</h3>
        <p>只读显示 Django 服务端配置状态；此页面不读取、接收或保存 API Key 与服务地址。</p>
      </div>
      <button class="optimizer-refresh" :disabled="loading" aria-label="刷新优化器状态" @click="load">
        <LoaderCircle v-if="loading" :size="14" class="spin"/>
        <RefreshCw v-else :size="14"/>
        刷新状态
      </button>
    </header>

    <div v-if="failed" class="optimizer-load-error">
      <AlertTriangle :size="17"/>
      <div><strong>无法读取能力状态</strong><p>请确认已登录且 Django 服务可用；页面不会回显后端连接详情。</p></div>
    </div>
    <div v-else-if="capability" class="optimizer-body">
      <div class="optimizer-state" :class="state.tone">
        <component :is="capability.enabled && capability.configured ? CheckCircle2 : AlertTriangle" :size="19"/>
        <div><small>RUNTIME STATUS</small><strong>{{ state.label }}</strong></div>
        <span>{{ capability.enabled ? 'ENABLED' : 'DISABLED' }}</span>
      </div>

      <div class="optimizer-facts">
        <article><Server :size="15"/><span><small>传输方式</small><strong>{{ transportLabel }}</strong></span></article>
        <article><ShieldCheck :size="15"/><span><small>失败策略</small><strong>{{ failureLabel }}</strong></span></article>
        <article><KeyRound :size="15"/><span><small>文本模型</small><strong>{{ capability.model || '尚未配置' }}</strong></span></article>
      </div>

      <div class="optimizer-gate" :class="{ danger: capability.languageGate==='block' && (!capability.enabled || !capability.configured) }">
        <AlertTriangle :size="16"/>
        <div><strong>语言质量门禁 · {{ capability.languageGate.toUpperCase() }}</strong><p>{{ gateMessage }}</p></div>
      </div>

      <div class="optimizer-guides">
        <details>
          <summary>启用本地优化器（推荐，剧本不出本机）</summary>
          <p>在 <code>backend/.env</code> 连接已由管理员部署的 OpenAI-compatible 本地文本模型，然后重启服务。推荐“优化器 warn + 生产语言门禁 block”：服务异常时仍可查看原提示词与错误，但混合语言视频不会进入生产队列。不要在浏览器填写密钥。</p>
          <pre>H3_PROMPT_OPTIMIZER_ENABLED=true
H3_PROMPT_OPTIMIZER_BASE_URL=http://127.0.0.1:&lt;port&gt;/v1
H3_PROMPT_OPTIMIZER_MODEL=&lt;local-model-name&gt;
H3_PROMPT_OPTIMIZER_FAILURE_MODE=warn
H3_PROMPT_LANGUAGE_GATE=block</pre>
        </details>
        <details>
          <summary>启用远程优化器（会发送提示词）</summary>
          <p>仅在获得数据外发授权后，由管理员通过 <code>backend/.env</code> 配置 HTTPS 端点和服务端密钥。API Key 永不进入前端。</p>
          <pre>H3_PROMPT_OPTIMIZER_ENABLED=true
H3_PROMPT_OPTIMIZER_BASE_URL=https://&lt;approved-host&gt;/v1
H3_PROMPT_OPTIMIZER_MODEL=&lt;approved-model&gt;
H3_PROMPT_OPTIMIZER_ALLOW_NON_LOOPBACK=true
H3_PROMPT_OPTIMIZER_API_KEY=&lt;server-secret&gt;
H3_PROMPT_OPTIMIZER_FAILURE_MODE=warn
H3_PROMPT_LANGUAGE_GATE=block</pre>
        </details>
      </div>
    </div>
  </section>
</template>

<style scoped>
.optimizer-capability{overflow:hidden}.optimizer-capability>header{padding:20px;display:flex;align-items:flex-start;justify-content:space-between;gap:18px;border-bottom:1px solid var(--border)}.optimizer-capability h3{margin:4px 0;font-size:17px}.optimizer-capability header p{font-size:10px;line-height:1.55;color:#6e757c}.optimizer-refresh{height:30px;padding:0 9px;display:flex;align-items:center;gap:6px;flex:0 0 auto;border:1px solid var(--border);border-radius:5px;background:#0d1115;color:#7f878f;font-size:9px;cursor:pointer}.optimizer-refresh:hover:not(:disabled){border-color:rgba(230,167,90,.28);color:var(--accent)}.optimizer-refresh:disabled{opacity:.6;cursor:wait}.spin{animation:spin 1s linear infinite}.optimizer-body{padding:16px 20px 20px;display:flex;flex-direction:column;gap:10px}.optimizer-state{min-height:58px;padding:11px 12px;display:flex;align-items:center;gap:10px;border:1px solid var(--border);border-radius:7px;color:#7c838a;background:#0b0e12}.optimizer-state>div{min-width:0;flex:1;display:flex;flex-direction:column;gap:3px}.optimizer-state small{font:8px Consolas,monospace;letter-spacing:.09em;color:#60676e}.optimizer-state strong{font-size:12px;color:#bdc2c6}.optimizer-state>span{padding:4px 7px;border:1px solid currentColor;border-radius:999px;font-size:7px}.optimizer-state.ready{color:var(--green);border-color:rgba(104,197,167,.2);background:rgba(104,197,167,.035)}.optimizer-state.staged{color:var(--accent);border-color:rgba(230,167,90,.2);background:rgba(230,167,90,.03)}.optimizer-state.danger{color:var(--red);border-color:rgba(239,115,123,.24);background:rgba(239,115,123,.035)}.optimizer-facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}.optimizer-facts article{min-width:0;min-height:62px;padding:10px;display:flex;align-items:center;gap:9px;border:1px solid var(--border);border-radius:7px;color:#776f96;background:#0b0e12}.optimizer-facts span{min-width:0;display:flex;flex-direction:column;gap:4px}.optimizer-facts small{font-size:8px;color:#626970}.optimizer-facts strong{overflow:hidden;text-overflow:ellipsis;font-size:10px;color:#aeb4b9;white-space:nowrap}.optimizer-gate,.optimizer-load-error{padding:12px;display:flex;align-items:flex-start;gap:9px;border:1px solid rgba(230,167,90,.2);border-radius:7px;background:rgba(230,167,90,.03);color:var(--accent)}.optimizer-gate>div,.optimizer-load-error>div{min-width:0;flex:1}.optimizer-gate strong,.optimizer-load-error strong{font-size:10px;color:#bd9a70}.optimizer-gate p,.optimizer-load-error p{margin-top:4px;font-size:9px;line-height:1.6;color:#7b7063}.optimizer-gate.danger,.optimizer-load-error{border-color:rgba(239,115,123,.22);background:rgba(239,115,123,.03);color:var(--red)}.optimizer-gate.danger strong,.optimizer-load-error strong{color:#c78388}.optimizer-gate.danger p,.optimizer-load-error p{color:#86676a}.optimizer-load-error{margin:16px 20px}.optimizer-guides{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.optimizer-guides details{padding:11px;border:1px solid var(--border);border-radius:7px;background:#0b0e12}.optimizer-guides summary{color:#aab0b5;font-size:10px;cursor:pointer}.optimizer-guides p{margin:9px 0 7px;font-size:9px;line-height:1.6;color:#686f76}.optimizer-guides code{color:#a88358}.optimizer-guides pre{margin:0;padding:9px;overflow:auto;border-radius:5px;background:#07090c;color:#858d91;font:8px/1.65 Consolas,monospace;white-space:pre-wrap;word-break:break-word}@media(max-width:800px){.optimizer-facts,.optimizer-guides{grid-template-columns:1fr}.optimizer-capability>header{flex-direction:column}.optimizer-refresh{align-self:flex-start}}
</style>
