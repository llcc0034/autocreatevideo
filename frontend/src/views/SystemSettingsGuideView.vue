<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  AlertTriangle, ArrowDown, ArrowLeft, ArrowRight, BookOpenCheck, CheckCircle2,
  CircleHelp, ExternalLink, KeyRound, ListChecks, Search, ShieldCheck,
  Sparkles, X,
} from 'lucide-vue-next'
import { settingsSectionById, settingsSectionCatalog, type SettingsSectionId } from '@/content/settingsSectionCatalog'
import {
  systemSettingsGuide, type GuideComponentNode, type GuideProductionFlowStep, type SystemSettingsGuideSection,
} from '@/content/systemSettingsGuide'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const query = ref('')
const componentNodes = systemSettingsGuide.componentFlow.stages.flatMap((stage) => stage.nodes)
const caseSectionNumber = computed(() => String(settingsSectionCatalog.length + 2).padStart(2, '0'))
const troubleshootingSectionNumber = computed(() => String(settingsSectionCatalog.length + 3).padStart(2, '0'))

const sectionAnchor = (id: SettingsSectionId) => settingsSectionById(id).guideAnchor
const sectionOpenLabel = (id: SettingsSectionId) => settingsSectionById(id).writeRequiresSuperuser && !auth.user?.isSuperuser
  ? '打开只读设置分区'
  : '打开这个设置分区'
const componentNodeById = (id: string) => componentNodes.find((node) => node.id === id)!
const componentNodeAnchor = (node: GuideComponentNode) => node.sectionId ? sectionAnchor(node.sectionId) : node.guideAnchor || 'settings-quick-start'
const componentNodeHref = (node: GuideComponentNode) => `#${componentNodeAnchor(node)}`
const productionFlowStepAnchor = (step: GuideProductionFlowStep) => step.sectionId ? sectionAnchor(step.sectionId) : step.guideAnchor || 'settings-real-case'
const productionFlowStepHref = (step: GuideProductionFlowStep) => `#${productionFlowStepAnchor(step)}`
const sectionMatches = (section: SystemSettingsGuideSection) => {
  const value = query.value.trim().toLocaleLowerCase('zh-CN')
  return !value || JSON.stringify(section).toLocaleLowerCase('zh-CN').includes(value)
}
const troubleshootingMatches = (item: (typeof systemSettingsGuide.troubleshooting)[number]) => {
  const value = query.value.trim().toLocaleLowerCase('zh-CN')
  return !value || JSON.stringify(item).toLocaleLowerCase('zh-CN').includes(value)
}

const scrollTo = async (id: string) => {
  await router.replace({ hash: `#${id}` })
  await nextTick()
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

const syncHash = async () => {
  const id = String(route.hash || '').replace(/^#/, '')
  if (!id) return
  await nextTick()
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

watch(() => route.hash, () => void syncHash())
onMounted(() => void syncHash())
</script>

<template>
  <div class="settings-guide page-stack">
    <section class="settings-guide-hero panel">
      <div>
        <RouterLink class="settings-guide-back" to="/manual"><ArrowLeft :size="14"/>返回完整生产操作手册</RouterLink>
        <span class="eyebrow"><BookOpenCheck :size="13"/>小白图文指南 / BEGINNER GUIDE</span>
        <h2>{{ systemSettingsGuide.title }}</h2>
        <p>{{ systemSettingsGuide.subtitle }}</p>
        <div class="settings-guide-meta"><span>版本 {{ systemSettingsGuide.version }}</span><span>更新 {{ systemSettingsGuide.updatedAt }}</span><span>{{ settingsSectionCatalog.length }} 个设置分区</span><span>1 个真实案例</span></div>
      </div>
      <div class="settings-guide-hero-actions">
        <button class="button primary" @click="scrollTo('settings-quick-start')"><Sparkles :size="15"/>从三分钟顺序开始</button>
        <button class="button ghost" @click="scrollTo('settings-real-case')"><CheckCircle2 :size="15"/>查看真实案例</button>
      </div>
    </section>

    <section class="settings-guide-search panel">
      <Search :size="17"/>
      <input v-model="query" aria-label="搜索系统设置指南" placeholder="搜索：烟测、服务提供方（Provider）、参考生视频（Ref2VA）、待消歧（uncertain）、配置修订…">
      <button v-if="query" title="清空搜索" @click="query='' "><X :size="15"/></button>
    </section>

    <div class="settings-guide-layout">
      <aside class="settings-guide-toc panel">
        <header><span>本页目录</span><strong>按真实操作顺序阅读</strong></header>
        <nav>
          <button @click="scrollTo('settings-how-to-use')"><b>00</b><span>权限与状态语义</span></button>
          <button @click="scrollTo('settings-quick-start')"><b>01</b><span>配置顺序与组件关系</span></button>
          <button v-for="(item,index) in settingsSectionCatalog" :key="item.id" @click="scrollTo(item.guideAnchor)"><b>{{ String(index+2).padStart(2,'0') }}</b><span>{{ item.label }}<small>{{ item.englishLabel }}</small></span></button>
          <button @click="scrollTo('settings-real-case')"><b>{{ caseSectionNumber }}</b><span>真实参考生视频（Ref2VA）案例</span></button>
          <button @click="scrollTo('settings-troubleshooting')"><b>{{ troubleshootingSectionNumber }}</b><span>故障排查与检查单</span></button>
        </nav>
      </aside>

      <main class="settings-guide-main">
        <section id="settings-how-to-use" class="settings-guide-section panel">
          <header class="settings-guide-section-title"><span>00</span><div><small>开始前先读</small><h3>权限、阅读方法与状态语义</h3><p>中文是操作说明，括号里的英文或机器值用于对照 API、日志和错误代码。不要翻译或改写机器值。</p></div></header>
          <div class="settings-guide-intro-grid">
            <article><h4>适合谁</h4><ul><li v-for="item in systemSettingsGuide.audience" :key="item">{{ item }}</li></ul></article>
            <article><h4><KeyRound :size="14"/>权限边界</h4><ul><li v-for="item in systemSettingsGuide.permissionNotes" :key="item">{{ item }}</li></ul><p class="settings-guide-role">当前账号：<strong>{{ auth.user?.isSuperuser ? '超级管理员（superuser）' : auth.user?.isStaff ? '平台管理员（staff）' : '普通用户' }}</strong></p></article>
          </div>
          <div class="settings-guide-status-grid"><article v-for="item in systemSettingsGuide.statusSemantics" :key="item.label"><strong>{{ item.label }}</strong><p>{{ item.meaning }}</p></article></div>
          <figure class="settings-guide-figure overview"><img :src="systemSettingsGuide.overviewImage.image" :alt="systemSettingsGuide.overviewImage.alt"><figcaption>{{ systemSettingsGuide.overviewImage.caption }}</figcaption></figure>
        </section>

        <section id="settings-quick-start" class="settings-guide-section panel">
          <header class="settings-guide-section-title"><span>01</span><div><small>三分钟先看懂</small><h3>第一次配置按这个顺序</h3><p>这是“先验证事实、再发布合同”的顺序。每一步看到成功标志后再继续。</p></div></header>
          <div class="settings-guide-quick-path"><button v-for="item in systemSettingsGuide.quickStart" :key="item.number" @click="scrollTo(sectionAnchor(item.sectionId))"><b>{{ item.number }}</b><span><strong>{{ item.title }}</strong><small>{{ item.detail }}</small></span><ArrowRight :size="15"/></button></div>
          <figure id="settings-component-flow" class="settings-guide-component-flow" aria-labelledby="settings-component-flow-title">
            <header class="settings-guide-flow-header">
              <div>
                <small>组件调用图 / COMPONENT CALL GRAPH</small>
                <h4 id="settings-component-flow-title">{{ systemSettingsGuide.componentFlow.title }}</h4>
                <p>{{ systemSettingsGuide.componentFlow.description }}</p>
              </div>
              <ul class="settings-guide-flow-legend" aria-label="关系图图例"><li v-for="item in systemSettingsGuide.componentFlow.legend" :key="item">{{ item }}</li></ul>
            </header>

            <ol class="settings-guide-flow-stages">
              <li v-for="(stage, stageIndex) in systemSettingsGuide.componentFlow.stages" :key="stage.number">
                <article class="settings-guide-flow-stage">
                  <header><b>{{ String(stage.number).padStart(2, '0') }}</b><div><h5>{{ stage.title }}</h5><p>{{ stage.summary }}</p></div></header>
                  <ul class="settings-guide-flow-nodes">
                    <li v-for="node in stage.nodes" :key="node.id" :class="{ optional: node.optional }">
                      <a :href="componentNodeHref(node)" @click.prevent="scrollTo(componentNodeAnchor(node))">
                        <span><strong>{{ node.label }}</strong><small>{{ node.english }}</small></span>
                        <p>{{ node.detail }}</p>
                        <em>{{ node.optional ? '按需组件' : '必需组件' }}</em>
                      </a>
                    </li>
                  </ul>
                  <div class="settings-guide-flow-success"><CheckCircle2 :size="15"/><p><strong>完成标志：</strong>{{ stage.success }}</p></div>
                </article>
                <div v-if="stageIndex < systemSettingsGuide.componentFlow.stages.length - 1" class="settings-guide-flow-next">
                  <ArrowDown :size="16" aria-hidden="true"/><span>{{ stage.next }}</span>
                </div>
              </li>
            </ol>

            <section class="settings-guide-flow-relations" aria-labelledby="settings-component-relations-title">
              <h5 id="settings-component-relations-title">关键调用关系</h5>
              <ul>
                <li v-for="edge in systemSettingsGuide.componentFlow.edges" :key="`${edge.from}-${edge.to}-${edge.label}`" :class="{ optional: edge.optional }">
                  <a :href="componentNodeHref(componentNodeById(edge.from))" @click.prevent="scrollTo(componentNodeAnchor(componentNodeById(edge.from)))">{{ componentNodeById(edge.from).label }}<small>{{ componentNodeById(edge.from).english }}</small></a>
                  <span><em>{{ edge.optional ? '按需关系' : '必需关系' }}</em><b>{{ edge.label }}</b><ArrowRight :size="14" aria-hidden="true"/></span>
                  <a :href="componentNodeHref(componentNodeById(edge.to))" @click.prevent="scrollTo(componentNodeAnchor(componentNodeById(edge.to)))">{{ componentNodeById(edge.to).label }}<small>{{ componentNodeById(edge.to).english }}</small></a>
                </li>
              </ul>
            </section>

            <div class="settings-guide-flow-notes"><strong>阅读提示</strong><ul><li v-for="item in systemSettingsGuide.componentFlow.notes" :key="item">{{ item }}</li></ul></div>
            <figcaption>{{ systemSettingsGuide.componentFlow.caption }}</figcaption>
          </figure>
          <figure id="settings-production-flow" class="settings-guide-component-flow settings-guide-production-flow" aria-labelledby="settings-production-flow-title">
            <header class="settings-guide-flow-header">
              <div>
                <small>生产顺序图 / PRODUCTION SEQUENCE</small>
                <h4 id="settings-production-flow-title">{{ systemSettingsGuide.productionFlow.title }}</h4>
                <p>{{ systemSettingsGuide.productionFlow.description }}</p>
              </div>
              <p class="settings-guide-production-summary"><strong>先固定事实，再逐步生成</strong><span>每个箭头都表示下一步必须读取上一环节已确认的数据。</span></p>
            </header>
            <ol class="settings-guide-production-steps">
              <li v-for="(step, index) in systemSettingsGuide.productionFlow.steps" :key="step.id">
                <a :href="productionFlowStepHref(step)" @click.prevent="scrollTo(productionFlowStepAnchor(step))">
                  <b>{{ String(step.number).padStart(2, '0') }}</b>
                  <span><strong>{{ step.label }}</strong><small>{{ step.english }}</small><p>{{ step.detail }}</p></span>
                  <ArrowRight :size="16" aria-hidden="true"/>
                </a>
                <div v-if="index < systemSettingsGuide.productionFlow.steps.length - 1" class="settings-guide-production-arrow"><ArrowDown :size="16" aria-hidden="true"/><span>读取已确认事实后继续</span></div>
              </li>
            </ol>
            <div class="settings-guide-flow-notes"><strong>边界提醒</strong><ul><li v-for="item in systemSettingsGuide.productionFlow.notes" :key="item">{{ item }}</li></ul></div>
            <figcaption>{{ systemSettingsGuide.productionFlow.caption }}</figcaption>
          </figure>
          <div class="settings-guide-warning"><AlertTriangle :size="17"/><p><strong>两个最容易混淆的动作：</strong>主机/部署“探测（Probe）”只确认健康与能力；“真实烟测（Smoke Test）”会提交真实低成本请求，可能产生费用。两者都通过后才有完整执行证据。</p></div>
        </section>

        <section v-for="(section,index) in systemSettingsGuide.sections" v-show="sectionMatches(section)" :id="sectionAnchor(section.id)" :key="section.id" class="settings-guide-section panel">
          <header class="settings-guide-section-title"><span>{{ String(index+2).padStart(2,'0') }}</span><div><small>{{ settingsSectionById(section.id).englishLabel }}</small><h3>{{ settingsSectionById(section.id).label }}</h3><p>{{ section.summary }}</p></div><RouterLink v-if="auth.user?.isStaff" class="button primary small" :to="settingsSectionById(section.id).settingsRoute" target="_blank">{{ sectionOpenLabel(section.id) }} <ExternalLink :size="13"/></RouterLink></header>
          <figure class="settings-guide-figure"><img :src="section.image" :alt="section.imageAlt" loading="lazy"><figcaption>{{ section.imageCaption }}</figcaption></figure>
          <div class="settings-guide-purpose"><article><h4>这个部分具体做什么</h4><ul><li v-for="item in section.purpose" :key="item">{{ item }}</li></ul></article><article><h4>操作前准备</h4><ul><li v-for="item in section.before" :key="item">{{ item }}</li></ul></article></div>
          <div class="settings-guide-steps">
            <article v-for="(step,stepIndex) in section.steps" :key="step.title">
              <header><b>{{ stepIndex+1 }}</b><h4>{{ step.title }}</h4></header>
              <dl><div><dt>怎么做</dt><dd>{{ step.action }}</dd></div><div class="success"><dt>成功标志</dt><dd>{{ step.expected }}</dd></div><div class="blocked"><dt>卡住时</dt><dd>{{ step.ifBlocked }}</dd></div></dl>
            </article>
          </div>
          <section v-if="section.codeExamples?.length" class="settings-guide-code-examples">
            <article v-for="example in section.codeExamples" :key="example.title">
              <header><strong>{{ example.title }}</strong><small>{{ example.language }}</small></header>
              <pre><code>{{ example.code }}</code></pre>
            </article>
          </section>
          <details class="settings-guide-detail" open><summary><ListChecks :size="15"/>字段怎么理解</summary><div><article v-for="item in section.fieldNotes" :key="item.term"><strong>{{ item.term }}</strong><p>{{ item.meaning }}</p></article></div></details>
          <details class="settings-guide-detail mistakes"><summary><CircleHelp :size="15"/>新手最常见卡点</summary><div><article v-for="item in section.mistakes" :key="item.symptom"><strong>{{ item.symptom }}</strong><p>{{ item.fix }}</p></article></div></details>
        </section>

        <section v-if="query && !systemSettingsGuide.sections.some(sectionMatches)" class="settings-guide-empty panel"><Search :size="24"/><strong>设置分区中没有匹配内容</strong><p>可以尝试机器值、中文术语或错误原因代码。</p></section>

        <section id="settings-real-case" class="settings-guide-section settings-guide-case panel">
          <header class="settings-guide-section-title"><span>{{ caseSectionNumber }}</span><div><small>实际操作案例 / ACTUAL OPERATION CASE</small><h3>{{ systemSettingsGuide.caseStudy.title }}</h3><p>{{ systemSettingsGuide.caseStudy.objective }}</p></div></header>
          <figure class="settings-guide-figure"><img :src="systemSettingsGuide.caseStudy.image" :alt="systemSettingsGuide.caseStudy.imageAlt" loading="lazy"><figcaption>{{ systemSettingsGuide.caseStudy.imageCaption }}</figcaption></figure>
          <div class="settings-guide-case-facts"><h4>已核实的固定事实</h4><ul><li v-for="item in systemSettingsGuide.caseStudy.facts" :key="item">{{ item }}</li></ul></div>
          <h4 class="settings-guide-subtitle">设置页预检</h4>
          <div class="settings-guide-preflight"><article v-for="item in systemSettingsGuide.caseStudy.preflight" :key="item.title"><button @click="scrollTo(sectionAnchor(item.sectionId))">{{ item.title }}<ArrowRight :size="13"/></button><ul><li v-for="check in item.checks" :key="check">{{ check }}</li></ul></article></div>
          <div class="settings-guide-case-columns"><article><h4>创建真实 Run / CREATE PRODUCTION RUN</h4><ol><li v-for="item in systemSettingsGuide.caseStudy.runSteps" :key="item">{{ item }}</li></ol></article><article class="result"><h4>本轮验证状态 / CURRENT VERIFICATION STATUS</h4><ul><li v-for="item in systemSettingsGuide.caseStudy.result" :key="item">{{ item }}</li></ul></article></div>
          <div class="settings-guide-warning danger"><AlertTriangle :size="17"/><div><strong>质量与口径提示</strong><ul><li v-for="item in systemSettingsGuide.caseStudy.caveats" :key="item">{{ item }}</li></ul></div></div>
        </section>

        <section id="settings-troubleshooting" class="settings-guide-section panel">
          <header class="settings-guide-section-title"><span>{{ troubleshootingSectionNumber }}</span><div><small>故障排查 / TROUBLESHOOTING</small><h3>卡住时按症状处理</h3><p>先定位原因，再执行恢复动作；不要通过默认值、旧快照或重复提交掩盖失败。</p></div></header>
          <div class="settings-guide-troubleshooting"><article v-for="item in systemSettingsGuide.troubleshooting.filter(troubleshootingMatches)" :key="item.symptom"><header><AlertTriangle :size="15"/><strong>{{ item.symptom }}</strong></header><p><b>常见原因：</b>{{ item.cause }}</p><ol><li v-for="action in item.actions" :key="action">{{ action }}</li></ol></article></div>
          <h4 class="settings-guide-subtitle">最终检查单</h4>
          <div class="settings-guide-checklist"><label v-for="item in systemSettingsGuide.finalChecklist" :key="item"><input type="checkbox"><span>{{ item }}</span></label></div>
          <h4 class="settings-guide-subtitle">常用术语速查</h4>
          <div class="settings-guide-glossary"><article v-for="item in systemSettingsGuide.glossary" :key="item.english"><strong>{{ item.chinese }}<small>{{ item.english }}</small></strong><p>{{ item.meaning }}</p></article></div>
          <footer class="settings-guide-footer"><ShieldCheck :size="17"/><p>详细配置正文来自同一份结构化指南数据；网页与可打印 Markdown 不分别手工维护，避免版本漂移。</p></footer>
        </section>
      </main>
    </div>
  </div>
</template>
