<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { AlertTriangle, AudioLines, CheckCircle2, ChevronRight, ExternalLink, FileCode2, FolderOpen, Gauge, KeyRound, Layers3, Lock, MonitorCog, Play, Save, Server, ShieldCheck, Sparkles, UserRound } from 'lucide-vue-next'
import ToggleSwitch from '@/components/ToggleSwitch.vue'
import { useStudioStore } from '@/stores/studio'
import { useAuthStore } from '@/stores/auth'
import { repository } from '@/services/repository'
import H3PromptOptimizerStatus from '@/components/H3PromptOptimizerStatus.vue'
import LocalTranslationStatus from '@/components/LocalTranslationStatus.vue'
import type { ComfySettings } from '@/types'

const studio=useStudioStore();const auth=useAuthStore();const tab=ref<'comfy'|'model'|'production'|'account'>('comfy');const form=ref<ComfySettings|null>(null);const saving=ref(false);const testing=ref(false);const testResult=ref<{ok:boolean;message:string;latency?:number}|null>(null)
const mockMode = import.meta.env.VITE_USE_MOCK === 'true'
const nav=[{id:'comfy' as const,label:'ComfyUI 连接',desc:'本地生成服务',icon:Server},{id:'model' as const,label:'模型与能力',desc:'H3 与本地翻译',icon:Sparkles},{id:'production' as const,label:'生产偏好',desc:'默认规格与队列',icon:MonitorCog},{id:'account' as const,label:'账户与安全',desc:'工作室资料',icon:ShieldCheck}]
onMounted(async()=>{await studio.initialize();form.value=studio.settings?JSON.parse(JSON.stringify(studio.settings)):null})
const save=async()=>{if(!form.value||!mockMode)return;saving.value=true;try{await studio.saveSettings(form.value)}finally{saving.value=false}}
const test=async()=>{if(!form.value)return;testing.value=true;testResult.value=null;try{testResult.value=await repository.testComfy(form.value.host)}catch(e){testResult.value={ok:false,message:e instanceof Error?e.message:'无法连接 ComfyUI'}}finally{testing.value=false}}
</script>

<template>
  <div class="settings-page page-stack"><section class="page-heading"><span class="eyebrow">STUDIO CONFIGURATION</span><h2>系统设置</h2><p>配置本地生产引擎、MiniMax H3 能力与默认输出规范。</p></section>
    <section class="settings-layout"><aside class="panel settings-nav"><button v-for="item in nav" :key="item.id" :class="{active:tab===item.id}" @click="tab=item.id"><component :is="item.icon" :size="18"/><span><strong>{{ item.label }}</strong><small>{{ item.desc }}</small></span><ChevronRight :size="15"/></button><div class="settings-version"><div><span>CineForge</span><strong>v1.0</strong></div><small>Build 2026.08.08</small></div></aside>
      <main v-if="form" class="settings-content">
        <H3PromptOptimizerStatus v-if="tab==='model'"/>
        <LocalTranslationStatus v-if="tab==='model'"/>
        <template v-if="tab==='comfy'">
          <section class="panel settings-section">
            <header><div><span class="eyebrow">LOCAL RENDER ENGINE</span><h3>ComfyUI 连接</h3><p>生产模式由 Django 服务端固定连接本机 ComfyUI，浏览器不能修改目标地址。</p></div><div class="connection-indicator" :class="{tested:testResult?.ok,error:testResult&&!testResult.ok}"><i></i><span>{{ testResult?.ok?'已连通':testResult?'连接失败':'等待检测' }}</span></div></header>
            <div class="settings-body">
              <div v-if="!mockMode" class="security-row"><ShieldCheck :size="19"/><div><strong>服务端环境配置</strong><p>地址、目录与密钥只从 backend/.env 读取，避免浏览器凭据泄露和任意目标请求。</p></div><span>只读</span></div>
              <label class="field"><span>服务地址</span><div class="field-action"><Server :size="16"/><input v-model="form.host" :readonly="!mockMode" placeholder="由 COMFYUI_BASE_URL 配置"><button :disabled="testing" @click="test">{{ testing?'检测中…':'检测连接' }}</button></div></label>
              <Transition name="fade"><div v-if="testResult" class="test-result" :class="{success:testResult.ok,error:!testResult.ok}"><component :is="testResult.ok?CheckCircle2:AlertTriangle" :size="16"/><span>{{ testResult.message }}</span><strong v-if="testResult.latency">{{ testResult.latency }} ms</strong></div></Transition>
              <label class="field"><span>ComfyUI 安装目录</span><div class="field-action"><FolderOpen :size="16"/><input v-model="form.installPath" :readonly="!mockMode"><button :disabled="!mockMode">浏览</button></div><small>生产模式请编辑 backend/.env；前端不读取或启动任意本地程序。</small></label>
              <label class="field"><span>默认输出目录</span><div class="field-action"><FolderOpen :size="16"/><input v-model="form.outputPath" :readonly="!mockMode"><button :disabled="!mockMode">浏览</button></div></label>
              <label class="field"><span>自定义工作流 JSON</span><div class="field-action"><FileCode2 :size="16"/><input v-model="form.workflowPath" :readonly="!mockMode" placeholder="由后端工作流模板管理"><button :disabled="!mockMode">选择文件</button></div></label>
              <div class="form-grid two"><label class="field"><span>并行任务数</span><select v-model.number="form.concurrency" :disabled="!mockMode"><option :value="1">1 · 本机安全默认</option><option :value="2" disabled>2 · 需管理员显存基准验证</option><option :value="3" disabled>3 · 当前硬件策略禁用</option></select></label><div class="setting-toggle"><div><span>自动启动 ComfyUI</span><small>默认关闭；仅在管理员验证启动脚本与显存余量后启用</small></div><ToggleSwitch v-model="form.autoStart" :disabled="!mockMode"/></div></div>
            </div>
            <footer><span><Lock :size="13"/>{{ mockMode ? 'Mock 偏好仅保存在本机' : '生产配置由 backend/.env 管理' }}</span><button v-if="mockMode" class="button primary" :disabled="saving" @click="save"><Save :size="15"/>{{ saving?'保存中…':'保存 Mock 偏好' }}</button></footer>
          </section>
          <section class="panel launch-guide"><div><Play :size="20"/></div><span><strong>服务端安全配置</strong><code>COMFYUI_BASE_URL=http://127.0.0.1:8188 · TASK_MAX_CONCURRENT=1</code></span><span class="eyebrow">BACKEND/.ENV</span></section>
        </template>
        <template v-else-if="tab==='model'">
          <section class="panel settings-section"><header><div><span class="eyebrow">VIDEO FOUNDATION MODEL</span><h3>MiniMax H3</h3><p>面向多模态视频与原生音频的主生成模型。</p></div><span class="model-ready"><CheckCircle2 :size="14"/>已启用</span></header><div class="model-hero"><div class="model-orb"><Sparkles :size="30"/></div><div><span>PRIMARY VIDEO MODEL</span><h2>MiniMax H3</h2><p>平台提供文生视频、首尾帧生视频、分类参考生视频三类输入模式；参考生视频支持按需选择不同工作流。首尾帧入口按 0/1/2 张图自动执行 T2VA、I2VA/L2VA 或 FL2VA。</p></div><a class="button ghost small" href="https://platform.minimax.io/docs/guides/video-generation" target="_blank" rel="noreferrer">模型文档 <ExternalLink :size="13"/></a></div><div class="mode-capability-grid"><article v-for="mode in [{n:'文生视频',d:'T2VA · 无参考输入'},{n:'首尾帧生视频',d:'0–2 图 · T2VA / I2VA / L2VA / FL2VA'},{n:'参考生视频',d:'0–9 分类图 · 0–3 视频 · 配对/独立音频 · 支持纯音频'}]" :key="mode.n"><CheckCircle2 :size="14"/><strong>{{ mode.n }}</strong><span>{{ mode.d }}</span></article></div><div class="audio-schema"><AudioLines :size="19"/><div><strong>原生音频字段</strong><p>提示词生成器将按所选工作流输出对应的官方 H3 字段。</p></div><code>integrated_multimodal_description</code><code>overall_soundscape</code><code>non_diegetic_music</code></div></section>
          <section class="panel settings-section compact-section"><header><div><span class="eyebrow">CREDENTIAL BOUNDARY</span><h3>服务凭据</h3><p>浏览器不读取或保存模型密钥；本版本通过服务端编排本地 MiniMax H3 工作流。</p></div></header><div class="settings-body"><div class="security-row"><KeyRound :size="19"/><div><strong>后端托管</strong><p>所有敏感值仅允许通过 backend/.env 与操作系统密钥管理配置。</p></div><span>受保护</span></div></div></section>
        </template>
        <template v-else-if="tab==='production'">
          <section class="panel settings-section"><header><div><span class="eyebrow">DEFAULT PRODUCTION PROFILE</span><h3>生产默认值</h3><p>以下是当前本机策略边界，不承诺未经验收的原生 1080p 或 4K 输出。</p></div></header><div class="settings-body"><div class="production-presets"><article class="active"><Gauge :size="20"/><div><strong>H3 标准目标</strong><p>768×1344 · 24 fps · 单任务；GTX 1070 上线前需烟测</p></div><CheckCircle2 :size="16"/></article><article><Layers3 :size="20"/><div><strong>后期超分预留</strong><p>成片后独立超分；不作为 H3 原生 1080p / 4K 承诺</p></div></article></div><div class="setting-toggle"><div><span>提示词版本审计</span><small>后端保存每次生成的结构化提示词与内容哈希</small></div><ToggleSwitch :model-value="true" disabled/></div><div class="setting-toggle"><div><span>任务失败重试</span><small>由后端队列的 max_attempts 策略控制</small></div><ToggleSwitch :model-value="true" disabled/></div><div class="setting-toggle"><div><span>完成后生成工程清单</span><small>组装时导出镜头、素材、Seed 与提示词审计记录</small></div><ToggleSwitch :model-value="true" disabled/></div></div></section>
        </template>
        <template v-else><section class="panel settings-section"><header><div><span class="eyebrow">STUDIO ACCOUNT</span><h3>账户与安全</h3><p>查看当前认证账号与本地数据安全边界。</p></div></header><div class="settings-body"><div class="profile-card"><div><UserRound :size="24"/></div><span><strong>{{ auth.user?.name || '当前工作室' }}</strong><small>{{ auth.user?.email || '已认证账号' }}</small></span><button class="button ghost small" disabled>由账号服务管理</button></div><div class="security-row"><ShieldCheck :size="19"/><div><strong>本地优先数据策略</strong><p>制作圣经、素材索引与工作流配置保存在本地；模型请求由 Django 后端代理。</p></div><span>已启用</span></div></div></section></template>
      </main>
    </section>
  </div>
</template>
