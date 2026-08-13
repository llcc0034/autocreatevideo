<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { AlertTriangle, Check, CheckCircle2, Clipboard, Cpu, HardDrive, Languages, LoaderCircle, RefreshCw, ShieldCheck } from 'lucide-vue-next'
import { repository } from '@/services/repository'
import type { TranslationCapability } from '@/types'

const capability = ref<TranslationCapability | null>(null)
const loading = ref(false)
const error = ref('')
const copied = ref(false)
const fallbackCommand = 'powershell -ExecutionPolicy Bypass -File .\\scripts\\download_local_translation_model.ps1'
const installCommand = computed(() => capability.value?.installCommand || fallbackCommand)
const ready = computed(() => Boolean(capability.value?.enabled && capability.value?.configured && capability.value?.modelDownloaded && capability.value?.modelVerified))
const deepseek = computed(() => capability.value?.providers?.find((provider) => provider.id === 'deepseek_api'))
const load = async () => {
  if (loading.value) return
  loading.value = true; error.value = ''
  try { capability.value = await repository.getTranslationCapability() }
  catch (cause) { capability.value = null; error.value = cause instanceof Error ? cause.message : '无法读取本地翻译能力' }
  finally { loading.value = false }
}
const copyCommand = async () => {
  try { await navigator.clipboard.writeText(installCommand.value) }
  catch {
    const input=document.createElement('textarea');input.value=installCommand.value;input.style.position='fixed';input.style.opacity='0';document.body.appendChild(input);input.select();document.execCommand('copy');input.remove()
  }
  copied.value = true; window.setTimeout(() => { copied.value = false }, 1500)
}
onMounted(load)
</script>

<template>
  <section class="panel local-translation-status">
    <header><div><span class="eyebrow">LOCAL QWEN TRANSLATION REVIEW</span><h3>本地 Qwen 翻译</h3><p>ModelScope 开源 Qwen3-4B 在本机生成英文候选，模型按请求加载并在返回前卸载；译文必须人工确认。</p></div><button class="refresh" :disabled="loading" @click="load"><LoaderCircle v-if="loading" class="spin" :size="13"/><RefreshCw v-else :size="13"/>刷新状态</button></header>
    <div v-if="error" class="load-error"><AlertTriangle :size="16"/><span><strong>能力状态读取失败</strong><small>{{ error }}</small></span></div>
    <div v-else-if="capability" class="translation-status-body">
      <div class="runtime-state" :class="{ready,danger:!ready}"><component :is="ready?CheckCircle2:AlertTriangle" :size="18"/><span><small>RUNTIME STATUS</small><strong>{{ ready?'已安装 · 可按需翻译':capability.enabled?'模型未就绪':'功能未启用' }}</strong></span><b>{{ ready?'READY':'NOT READY' }}</b></div>
      <div class="translation-facts"><article><CheckCircle2 :size="15"/><span><small>功能开关</small><strong>{{ capability.enabled?'已启用':'未启用' }}</strong></span></article><article><CheckCircle2 :size="15"/><span><small>运行配置</small><strong>{{ capability.configured?'已配置':'未配置' }}</strong></span></article><article><HardDrive :size="15"/><span><small>模型文件</small><strong>{{ capability.modelDownloaded?'已下载':'未下载' }}</strong></span></article><article><ShieldCheck :size="15"/><span><small>安装清单</small><strong>{{ capability.modelVerified?'已校验':'未校验' }}</strong></span></article><article><Cpu :size="15"/><span><small>运行设备</small><strong>{{ (capability.device||'cpu').toUpperCase() }}</strong></span></article><article><Languages :size="15"/><span><small>模型</small><strong>{{ capability.model || 'Qwen/Qwen3-4B-GGUF' }}</strong></span></article><article><RefreshCw :size="15"/><span><small>资源策略</small><strong>{{ capability.loadedOnDemand&&capability.unloadedAfterRequest?'按需加载 · 请求后卸载':'读取服务端策略' }}</strong></span></article><article><ShieldCheck :size="15"/><span><small>人工审核</small><strong>{{ capability.reviewRequired?'强制确认':'状态异常' }}</strong></span></article></div>
      <div v-if="deepseek" class="remote-provider-state" :class="{ready:deepseek.ready}"><Languages :size="16"/><div><strong>可选 DeepSeek API · {{ deepseek.model || '服务端模型' }}</strong><p>{{ deepseek.ready?'已由后端启用并配置，可在每次翻译/扩写时主动选择。':'当前未启用或后端密钥未配置完整。' }} 选择远程通道会发送本次提示词及所需项目上下文；浏览器不会读取密钥或完整接口地址。</p></div><b>{{ deepseek.ready?'READY':'NOT READY' }}</b></div>
      <div class="model-distinction"><AlertTriangle :size="15"/><div><strong>不要与官方 Qwen-MT API 混淆</strong><p>{{ capability.message || '当前默认是本机 ModelScope Qwen/Qwen3-4B-GGUF；只有另行配置 DashScope 云端服务时才是官方 Qwen-MT API。' }}</p></div></div>
      <div v-if="!capability.modelDownloaded||!capability.modelVerified" class="install-guide"><div><HardDrive :size="18"/><span><strong>{{ capability.modelDownloaded?'重新校验/安装':'安装' }}本地翻译模型 · 约 {{ (capability.estimatedDownloadGb||2.5).toFixed(2) }} GB</strong><small>在项目根目录的新 PowerShell 中执行；脚本会核对文件大小与 SHA-256 清单。页面不会发起会阻塞 HTTP 数分钟的“假下载”。</small></span></div><pre>{{ installCommand }}</pre><button type="button" @click="copyCommand"><component :is="copied?Check:Clipboard" :size="13"/>{{ copied?'已复制':'复制安装命令' }}</button></div>
    </div>
  </section>
</template>

<style scoped>
.local-translation-status{overflow:hidden}.local-translation-status>header{padding:20px;display:flex;align-items:flex-start;justify-content:space-between;gap:18px;border-bottom:1px solid var(--border)}.local-translation-status h3{margin:4px 0;font-size:17px}.local-translation-status header p{font-size:10px;line-height:1.55;color:#6e757c}.refresh{height:30px;padding:0 9px;display:flex;align-items:center;gap:6px;border:1px solid var(--border);border-radius:5px;background:#0d1115;color:#7f878f;font-size:9px;cursor:pointer}.translation-status-body{padding:16px 20px 20px;display:grid;gap:10px}.runtime-state{min-height:58px;padding:11px 12px;display:flex;align-items:center;gap:10px;border:1px solid rgba(104,197,167,.2);border-radius:7px;background:rgba(104,197,167,.035);color:var(--green)}.runtime-state.danger{border-color:rgba(239,115,123,.22);background:rgba(239,115,123,.03);color:var(--red)}.runtime-state span{min-width:0;flex:1;display:grid;gap:3px}.runtime-state small{font:8px Consolas,monospace;color:#65717b}.runtime-state strong{font-size:12px;color:#c7d1d7}.runtime-state b{padding:4px 7px;border:1px solid currentColor;border-radius:999px;font-size:7px}.translation-facts{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:7px}.translation-facts article{min-width:0;min-height:62px;padding:10px;display:flex;align-items:center;gap:8px;border:1px solid var(--border);border-radius:7px;background:#0b0e12;color:#6598bb}.translation-facts span{min-width:0;display:grid;gap:4px}.translation-facts small{font-size:8px;color:#626970}.translation-facts strong{overflow:hidden;color:#aeb4b9;font-size:9px;text-overflow:ellipsis;white-space:nowrap}.model-distinction,.load-error{padding:12px;display:flex;align-items:flex-start;gap:9px;border:1px solid rgba(230,167,90,.2);border-radius:7px;background:rgba(230,167,90,.03);color:var(--accent)}.model-distinction strong,.load-error strong{font-size:10px}.model-distinction p,.load-error small{display:block;margin-top:4px;color:#7b7063;font-size:9px;line-height:1.6}.load-error{margin:16px 20px;color:var(--red);border-color:rgba(239,115,123,.22)}.install-guide{display:grid;grid-template-columns:1fr auto;gap:9px;padding:12px;border:1px solid rgba(94,164,218,.2);border-radius:7px;background:rgba(94,164,218,.04)}.install-guide>div{grid-column:1/-1;display:flex;align-items:center;gap:9px;color:#78b6df}.install-guide span{display:grid;gap:3px}.install-guide strong{font-size:10px;color:#b9cad6}.install-guide small{font-size:9px;color:#758590}.install-guide pre{min-width:0;margin:0;padding:9px;overflow:auto;border-radius:5px;background:#07090c;color:#9aa9b3;font:9px/1.5 Consolas,monospace;white-space:pre-wrap;word-break:break-all}.install-guide button{padding:0 11px;display:flex;align-items:center;gap:5px;border:1px solid rgba(94,164,218,.3);border-radius:5px;background:#17344a;color:#9ed0ed;font-size:9px;cursor:pointer}.spin{animation:local-translation-spin 1s linear infinite}@keyframes local-translation-spin{to{transform:rotate(360deg)}}@media(max-width:980px){.translation-facts{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:700px){.local-translation-status>header{flex-direction:column}.translation-facts{grid-template-columns:1fr}.install-guide{grid-template-columns:1fr}.install-guide button{min-height:34px;justify-content:center}}
.translation-facts{grid-template-columns:repeat(auto-fit,minmax(125px,1fr))}
.remote-provider-state{padding:12px;display:flex;align-items:flex-start;gap:9px;border:1px solid rgba(239,115,123,.18);border-radius:7px;background:rgba(239,115,123,.025);color:#a66e74}.remote-provider-state.ready{border-color:rgba(104,197,167,.2);background:rgba(104,197,167,.035);color:var(--green)}.remote-provider-state>div{min-width:0;flex:1}.remote-provider-state strong{font-size:10px;color:#b8c3ca}.remote-provider-state p{margin-top:4px;color:#77828a;font-size:9px;line-height:1.55}.remote-provider-state b{padding:4px 7px;border:1px solid currentColor;border-radius:999px;font-size:7px}
</style>
