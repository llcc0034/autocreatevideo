<script setup lang="ts">
import BibleCreativeReview from './BibleCreativeReview.vue'
import type { BibleReviewContent } from '@/services/productionManualBible'

defineProps<{ content: BibleReviewContent; disabled?: boolean }>()
const candidates = defineModel<string[]>('candidates', { required: true })
const settings = defineModel<string[]>('settings', { required: true })
const project = defineModel<boolean>('project', { required: true })
const types = { character: '人物', scene: '场景', prop: '道具' }
const authoredText = (value: unknown) => value === null ? '未声明此字段' : value === '' ? '声明为空文本' : String(value)
</script>

<template>
  <section class="bible-content" aria-label="完整 Bible 声明与设定审核">
    <h5>原剧本实体声明 · {{ content.candidates.length }} 项</h5>
    <p v-if="!content.candidates.length">本次没有实体声明；不会为补齐数量而生成条目。</p>
    <details v-for="item in content.candidates" :key="item.id">
      <summary>{{ types[item.entity_type] }} · {{ item.name }} · {{ item.action === 'reuse' ? '已入库，保留当前设定' : '待创建' }}</summary>
      <p>{{ item.description || '未声明描述' }}</p><p>原始共同身份声明：{{ authoredText(item.identity_anchor) }}</p>
      <p v-if="item.conflict_code" role="alert">内容冲突：{{ item.conflict_code }}</p>
      <p v-if="item.existing">对应已有条目：{{ item.existing.name }}。当前完整内容见下方项目设定；不会用原声明覆盖人工修订。</p>
      <label><input v-model="candidates" type="checkbox" :value="item.id" :disabled="disabled || Boolean(item.conflict_code)">我已核对实体声明及其创建／复用对象</label>
    </details>
    <h5>主图与变体设定 · {{ content.settings.length }} 项</h5>
    <p>确认的是设定内容，不是图片生成或图片采用。原声明与当前修订分别展示；变体依赖明确的主图设定修订，不自动替换为最新版本。</p>
    <p v-if="!content.settings.length">当前没有主图或变体设定。</p>
    <details v-for="item in content.settings" :key="item.review_key">
      <summary>{{ item.entity_name }} · {{ item.name }} · {{ item.appearance_role === 'primary' ? '主图' : '变体' }} · {{ item.kind === 'declaration' ? '原剧本声明' : `当前设定修订 ${item.setting?.revision}` }}</summary>
      <dl><dt>素材引用名称</dt><dd>{{ item.reference_name }}</dd><dt>身份锚点</dt><dd>{{ item.identity_anchor || (item.primary_setting ? '未单独填写；身份依据见下述确切主图设定' : '未填写；不会自动补齐') }}</dd>
        <dt>外观锚点</dt><dd>{{ item.appearance_anchor || '未填写；不会自动补齐' }}</dd></dl>
      <div v-if="item.source_facts" class="basis">
        <p>{{ item.source_facts.already_materialized ? '原声明已入库；仅核对来源，不覆盖当前修订。' : '确认后按稳定 key 复用已有设定，仅为缺失的 key 创建新设定；本声明不会覆盖已有名称和正文。' }}</p>
        <p>声明的主图引用名称：{{ item.source_facts.primary_reference_name }}</p>
        <details><summary>已保存的映射字段与来源</summary><dl>
          <template v-for="(label, key) in {description:'描述',identity_anchor:'身份锚点',appearance_anchor:'外观锚点'}" :key="key">
            <dt>{{ label }}</dt><dd>{{ authoredText(item.source_facts[key]) }}</dd>
          </template></dl>
          <p>语义位置：{{ item.source_facts.semantic_path }}。内容来自采用时已保存的字段映射结果，不按当前配置重新解析。</p>
          <details><summary>结构化依据（技术信息）</summary><pre>{{ JSON.stringify(item.source_facts.structure_source, null, 2) }}</pre></details>
        </details>
      </div>
      <details v-if="item.primary_setting" class="basis"><summary>所依赖的主图完整设定 · 修订 {{ item.primary_setting.revision }} · {{ item.primary_setting.reference_name }}</summary>
        <dl><dt>身份锚点</dt><dd>{{ item.primary_setting.identity_anchor || '未声明' }}</dd><dt>外观锚点</dt><dd>{{ item.primary_setting.appearance_anchor || '未声明' }}</dd></dl>
        <p>这是该变体已绑定的明确修订；可能是历史修订。本次不改写它的审核决定。</p>
        <details><summary>主图完整证据（技术信息）</summary><pre>{{ JSON.stringify(item.primary_setting, null, 2) }}</pre></details>
      </details>
      <details v-if="item.setting"><summary>当前修订、原审核和来源证据（技术信息）</summary><pre>{{ JSON.stringify(item.setting, null, 2) }}</pre></details>
      <label><input v-model="settings" type="checkbox" :value="item.review_key" :disabled="disabled">我已核对本项完整设定及其来源／主图依赖</label>
    </details>
    <details><summary>项目现有完整创作设定（含声音等字段）</summary><BibleCreativeReview :content="content.project_bible" /></details>
    <label><input v-model="project" type="checkbox" :disabled="disabled">我已核对项目完整创作设定；未填写内容不会自动补全。</label>
  </section>
</template>

<style scoped>
.bible-content{display:grid;gap:12px;min-width:0}.bible-content>details{border:1px solid var(--border);border-radius:8px;padding:12px}.bible-content summary{cursor:pointer;overflow-wrap:anywhere}.bible-content h5,.bible-content p{margin:8px 0}.bible-content p,.bible-content dd,.bible-content pre{white-space:pre-wrap;overflow-wrap:anywhere}.bible-content dl{display:grid;grid-template-columns:8em minmax(0,1fr);gap:8px}.bible-content dd{margin:0}.bible-content label{display:flex;align-items:flex-start;gap:8px;margin-top:10px}.basis{border-left:2px solid var(--border);padding-left:12px}@media(max-width:600px){.bible-content dl{grid-template-columns:1fr}}
</style>
