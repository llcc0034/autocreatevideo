<script setup lang="ts">
import { computed, onBeforeUnmount } from 'vue'
import BaseModal from './BaseModal.vue'
import { api } from '@/services/api'
import { createHistoryCleanupController, CLEANUP_CONFIRMATION } from '@/services/historyCleanup'
import PlatformStatusPill from './PlatformStatusPill.vue'
import { taskTime } from '@/services/taskCenter'

const { open,pending,error,confirmation,result,notice,plan,pause,occupied,phase,title,step,elapsed,remaining,lastChecked,nextCheckSeconds,canExecute,includeMaintenance,scopeLocked,preview,refresh,close,execute,dispose } = createHistoryCleanupController(api)
onBeforeUnmount(() => { void dispose() })
const counts=computed(()=>Object.entries(plan.value?.delete_counts||{}).filter(([,count])=>count>0))
const duration=(seconds:number)=>Math.floor(seconds/60)+'分'+String(seconds%60).padStart(2,'0')+'秒'
const previewLabel=computed(()=>pending.value ? (phase.value==='pausing'?'正在暂停…':phase.value==='releasing'?'正在恢复…':phase.value==='deleting'?'正在清理…':'正在核对…') : occupied.value?'立即检查占用状态':pause.value?'立即重新核对':'暂停检查并重新预览')
</script>
<template>
  <section class="cleanup-panel">
    <h3>业务历史清理</h3>
    <p>默认清理所有用户的项目、剧本、分镜、运行、生成 / Smoke 调用、审核、装配、发行及操作审计。保留账号、凭据、模型/工作流/策略/配方配置，以及素材库文件和主图/变体关系。</p>
    <label class="maintenance-choice"><input type="checkbox" v-model="includeMaintenance" :disabled="scopeLocked">同时清理系统维护历史（可选，默认保留）</label>
    <div class="maintenance-scope"><strong>系统维护历史包含以下数据：</strong><ul><li>主机探测任务及其进度、结果。</li><li>主机能力快照：CPU、内存、GPU、磁盘、FFmpeg / ComfyUI 检测结果。</li><li>模型文件可用性观测：文件存在性、大小、校验摘要等观测证据。</li><li>系统级模型 / 工作流推荐快照、候选和采纳 / 覆盖 / 拒绝记录。项目相关推荐随业务历史清理。</li></ul><p>不勾选：探测照常运行，不因探测阻挡业务清理。勾选：暂停新增定时探测，并等待已有探测结束；清除关联文件验证证明，下次能力判断需重新探测。不会删除主机、模型或部署配置，也不会删除模型文件。</p></div>
    <p>生成 / Smoke 历史属于业务执行记录，清理后旧校验 / Smoke 证明重置。配置定义及不可变导入快照保留；仍引用待删历史的配置会明确提示处理。不会删除备份、日志、ComfyUI 或素材文件。</p>
    <p>预览时仅暂停本次范围涉及的新自动检查，最多5分钟；关闭或到期恢复，不取消执行中任务。未选择系统维护历史时不暂停主机探测。</p>
    <button class="button danger-ghost" :disabled="pending||phase==='unknown'" @click="preview">{{pending?previewLabel:phase==='unknown'?'请先核实上次清理结果':'暂停后台检查并预览清理'}}</button>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="phase==='unknown'&&!open"><a href="/projects" target="_blank" rel="noopener">查看项目列表，核对清理结果（新窗口）</a></p>
    <BaseModal :open="open" :dismissible="!pending" title="清理所选历史 · 保留素材库" width="760px" @close="close">
      <p>此操作不可在页面中撤销。需要恢复时依赖清理前的数据库备份。先查看以下范围，再输入确认文字。</p>
      <p><strong>本次范围：{{includeMaintenance?'业务历史 + 系统维护历史':'仅业务历史；主机探测及系统维护记录保留'}}</strong></p>
      <ol class="cleanup-steps" aria-label="清理步骤"><li v-for="(label,index) in ['准备清理窗口','核对范围与在途','确认清理范围','提交与结果']" :key="label" :aria-current="step===index+1?'step':undefined" :class="{current:step===index+1}">{{index+1}} · {{label}}</li></ol>
      <section class="cleanup-status" aria-label="当前清理状态" :aria-busy="pending">
        <strong role="status">{{title}}</strong>
        <p v-if="pending">本次操作已等待 {{duration(elapsed)}}。{{phase==='deleting'?'服务器正在事务内重新核对并删除；未返回完成前不能确定结果，请勿重复提交。':'正在等待服务器响应，请勿重复点击。'}}</p>
        <p v-if="phase==='occupied'">本窗口未开始清理。其他窗口持有后台检查暂停；可关闭原窗口释放，或等到期后重新预览。本窗口不能释放其他窗口的暂停。</p>
        <p v-if="phase==='available'">服务器已确认暂停占用解除；点击“暂停检查并重新预览”继续，不会自动删除。</p>
        <p v-if="phase==='ready'">当前没有阻挡项。尚未删除记录；核对下面范围并输入确认文字后，才可提交清理。</p>
        <p v-if="phase==='blocked'">尚未开始删除。剩余 {{plan?.blockers.length}} 个阻挡项，详见下方真实任务状态。</p>
        <p v-if="remaining!==null">{{pause?'本窗口':'其他窗口'}}暂停剩余 {{duration(remaining)}}，到期后后台检查恢复。<strong>这不是任务预计完成时间。</strong></p>
        <p v-if="lastChecked">最近核对：{{taskTime(new Date(lastChecked).toISOString())}}<span v-if="nextCheckSeconds!==null"> · {{nextCheckSeconds}} 秒后自动核对</span></p>
        <p v-if="phase==='deleting'">删除接口不返回逐条进度，不显示估算百分比；以服务器最终回执为准。</p>
      </section>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-if="result" role="status">{{ result }}</p>
      <template v-if="plan">
        <p>保留 {{ plan.preserve_counts['studio.Asset'] || 0 }} 项素材；素材原来源说明保留，生成历史会明确标记已清理。</p>
        <p v-if="!plan.delete_counts['studio.SeriesProject']&&!plan.delete_counts['studio.Shot']&&!plan.delete_counts['studio.ProductionRun']">项目、镜头和生产运行记录已为空。下方数量是本次仍存在的其他历史，不代表又在删除项目。</p>
        <p v-else>本次范围：{{ plan.delete_counts['studio.SeriesProject'] || 0 }} 个项目、{{ plan.delete_counts['studio.Shot'] || 0 }} 个镜头、{{ plan.delete_counts['studio.ProductionRun'] || 0 }} 轮生产运行及其相关历史。</p>
        <details><summary>查看分类型记录数量</summary><dl class="cleanup-counts"><template v-for="[model,count] in counts" :key="model"><dt>{{ model.replace('studio.','') }}</dt><dd>{{ count }}</dd></template></dl></details>
        <div v-if="plan.blockers.length"><strong>阻挡详情（截至最近核对时间）</strong><ul class="cleanup-blockers"><li v-for="(block,index) in plan.blockers" :key="index"><strong>{{ block.label }}</strong><p>{{ block.reason }}</p><PlatformStatusPill v-if="block.status" :status="block.status"/><p v-if="block.progress_marker">任务阶段：{{block.progress_marker}}</p><p v-if="block.started_at">开始时间：{{taskTime(block.started_at)}}</p><p v-if="block.heartbeat_at">最近心跳：{{taskTime(block.heartbeat_at)}}</p><a v-if="block.detail_url" class="button ghost small" :href="block.detail_url" target="_blank" rel="noopener">{{ block.link_label }}（新窗口）</a><details><summary>记录标识</summary>{{ block.model }} · {{ block.id }}</details></li></ul></div>
        <label v-else class="field"><span>输入“{{CLEANUP_CONFIRMATION}}”</span><input v-model="confirmation" :disabled="pending" autocomplete="off"></label>
      </template>
      <p v-if="phase==='unknown'"><a href="/projects" target="_blank" rel="noopener">查看项目列表，核对清理结果（新窗口）</a></p>
      <template #footer><button class="button ghost" :disabled="pending" @click="close">{{phase==='releasing'?'正在恢复检查…':pause?'关闭并恢复检查':'关闭窗口'}}</button><button v-if="!['done','unknown'].includes(phase)" class="button ghost" :disabled="pending" @click="pause||occupied ? refresh() : preview()">{{previewLabel}}</button><button v-if="!['done','unknown'].includes(phase)" class="button danger-ghost" :disabled="!canExecute" @click="execute">{{phase==='deleting'?'正在清理，请等待…':'清空历史'}}</button></template>
    </BaseModal>
  </section>
</template>
<style scoped>
.cleanup-blockers{padding-left:22px}.cleanup-blockers li{margin:14px 0}.cleanup-blockers p{margin:5px 0}.cleanup-blockers details{font-size:12px;margin-top:8px}.cleanup-blockers a{color:var(--text-primary)}
.maintenance-choice{display:flex;align-items:center;gap:8px;min-height:40px;font-size:14px}.maintenance-scope{font-size:13px;border-left:2px solid var(--border);padding:8px 12px}.maintenance-scope ul{padding-left:20px;line-height:1.8}
.cleanup-steps{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;padding:0;list-style:none;font-size:12px}.cleanup-steps li{padding:8px;border-bottom:2px solid var(--border);color:var(--text-muted)}.cleanup-steps .current{color:var(--text-primary);border-color:var(--accent)}.cleanup-status{border:1px solid var(--border);border-radius:8px;padding:12px;margin:12px 0}.cleanup-status p{margin:6px 0}@media(max-width:600px){.cleanup-steps{grid-template-columns:repeat(2,minmax(0,1fr))}}
.cleanup-panel{padding:16px;border:1px solid var(--border);border-radius:8px;margin-bottom:18px}.cleanup-panel p{font-size:13px;line-height:1.6;overflow-wrap:anywhere}.cleanup-counts{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px;max-height:250px;overflow:auto;font-size:12px}.cleanup-counts dt{overflow-wrap:anywhere}.cleanup-counts dd{margin:0}[role=alert]{color:#e4868d;overflow-wrap:anywhere}
</style>
