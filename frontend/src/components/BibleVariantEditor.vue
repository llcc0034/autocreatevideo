<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { bibleVariantApi } from '@/services/bibleVariantApi'
import TranslationReviewPanel from './TranslationReviewPanel.vue'
import BibleSettingSource from './BibleSettingSource.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { appearanceRoleLabel, emptyVariantSetting, primarySettingTranslationFields, variantDecisionLabel, variantRequestGuard, variantSettingDraft } from '@/services/bibleVariants'
import type { BibleVariantDetail, BibleVariantPage, BibleVariantRevision, BibleVariantScope, BibleVariantSummary } from '@/services/bibleVariants'

const props = defineProps<{ scope: BibleVariantScope; entityName: string }>()
const emit = defineEmits<{ busy: [value: boolean]; dirty: [value: boolean]; translationsChanged: [] }>()
const listing = ref<BibleVariantPage<BibleVariantSummary> | null>(null)
const page = ref(1), listLoading = ref(false), listError = ref('')
const detail = ref<BibleVariantDetail | null>(null), detailLoading = ref(false), detailError = ref('')
const history = ref<BibleVariantPage<BibleVariantRevision> | null>(null)
const historyPage = ref(1), historyLoading = ref(false), historyError = ref(''), historyOpen = ref(false)
const editing = ref(false), creating = ref(false), saving = ref(false), savedMessage = ref(''), mutationError = ref('')
const draft = ref(emptyVariantSetting()), accepted = ref(false), comment = ref('')
const creationPrimary = ref<BibleVariantDetail | null>(null)
const primaryBasis = computed(() => creating.value ? creationPrimary.value?.setting : detail.value?.primary_setting)
const listGate = variantRequestGuard(), detailGate = variantRequestGuard(), historyGate = variantRequestGuard(), writeGate = variantRequestGuard()
const locked = computed(() => saving.value || detailLoading.value)
const isCurrent = computed(() => !!detail.value && detail.value.setting.revision === detail.value.revision)
const canReview = computed(() => isCurrent.value && !detail.value?.setting.review && !editing.value && !detailError.value)
const translationFields = computed(() => detail.value ? primarySettingTranslationFields(detail.value, props.scope) : [])
const dirty = computed(() => editing.value && (creating.value
  ? JSON.stringify(draft.value) !== JSON.stringify(emptyVariantSetting())
  : !!detail.value && JSON.stringify(draft.value) !== JSON.stringify(variantSettingDraft(detail.value))))
watch(saving, v => emit('busy', v), { immediate: true })
watch(dirty, v => emit('dirty', v), { immediate: true })

function reset() {
  listGate.cancel(); detailGate.cancel(); historyGate.cancel(); writeGate.cancel()
  listing.value = null; detail.value = null; history.value = null
  creationPrimary.value = null
  listError.value = ''; detailError.value = ''; historyError.value = ''; mutationError.value = ''; savedMessage.value = ''
  page.value = 1; historyPage.value = 1; historyOpen.value = false; editing.value = false; creating.value = false
  saving.value = false; detailLoading.value = false; historyLoading.value = false; accepted.value = false; comment.value = ''
}
watch(() => [props.scope.project_id, props.scope.entity_kind, props.scope.entity_id], () => { reset(); void loadList(1) }, { immediate: true })
onBeforeUnmount(() => { listGate.cancel(); detailGate.cancel(); historyGate.cancel(); writeGate.cancel() })
function discard() { return !dirty.value || window.confirm('放弃尚未保存的变体设定编辑？已保存修订和审核不受影响。') }
async function loadList(target = page.value) {
  const ticket = listGate.next(), scope = { ...props.scope }
  listLoading.value = true; listError.value = ''
  try {
    const result = await bibleVariantApi.list(scope, target)
    if (!listGate.current(ticket)) return
    listing.value = result; page.value = target
  } catch (error) { if (listGate.current(ticket)) listError.value = apiErrorSummary(error, '变体清单读取失败；不能判断是否已有设定。') }
  finally { if (listGate.current(ticket)) listLoading.value = false }
}
async function openDetail(id: string, revision?: number) {
  if (saving.value || !discard()) return
  const ticket = detailGate.next()
  editing.value = false; creating.value = false; accepted.value = false; comment.value = ''; mutationError.value = ''; savedMessage.value = ''
  detailLoading.value = true; detailError.value = ''; detail.value = null
  historyGate.cancel(); historyOpen.value = false; history.value = null; historyLoading.value = false; historyError.value = ''
  try {
    const result = await bibleVariantApi.detail(id, revision)
    if (detailGate.current(ticket)) detail.value = result
  } catch (error) { if (detailGate.current(ticket)) detailError.value = apiErrorSummary(error, '设定修订读取失败；请从清单重新打开。') }
  finally { if (detailGate.current(ticket)) detailLoading.value = false }
}
function startCreate(primary: BibleVariantDetail | null = null) {
  if (locked.value || !discard()) return
  if (primary && primary.appearance_role !== 'primary') return
  creationPrimary.value = primary
  detailGate.cancel(); detail.value = null; detailError.value = ''; mutationError.value = ''; savedMessage.value = ''
  historyGate.cancel(); historyOpen.value = false; history.value = null; historyLoading.value = false
  draft.value = emptyVariantSetting(); creating.value = true; editing.value = true
}
function startEdit() {
  if (locked.value || !detail.value || !isCurrent.value) return
  draft.value = variantSettingDraft(detail.value); editing.value = true; creating.value = false; accepted.value = false
  savedMessage.value = ''; mutationError.value = ''
}
function cancelEdit() { if (!saving.value && discard()) { editing.value = false; creating.value = false } }
async function save() {
  if (locked.value || !editing.value || !dirty.value) return
  const ticket = writeGate.next(), scope = { ...props.scope }, payload = { ...draft.value }, basis = detail.value
  const isNew = creating.value
  saving.value = true; mutationError.value = ''; savedMessage.value = ''
  try {
    const result = isNew ? await bibleVariantApi.create(scope, payload, creationPrimary.value) : await bibleVariantApi.revise(basis!, payload)
    if (!writeGate.current(ticket)) return
    detail.value = result; editing.value = false; creating.value = false; accepted.value = false
    historyGate.cancel(); history.value = null; historyOpen.value = false; historyLoading.value = false
    savedMessage.value = `已保存设定修订 ${result.setting.revision}，尚未人工确认；没有生成或采用图片。`
    await loadList()
  } catch (error) {
    if (writeGate.current(ticket)) mutationError.value = apiErrorSummary(error, '保存结果未确认。请先刷新清单核对，勿盲目重复提交。')
  } finally { if (writeGate.current(ticket)) saving.value = false }
}
async function review(decision: 'approve' | 'reject') {
  if (locked.value || !canReview.value || !accepted.value || !detail.value) return
  const ticket = writeGate.next(), basis = detail.value
  saving.value = true; mutationError.value = ''; savedMessage.value = ''
  try {
    const result = await bibleVariantApi.review(basis, decision, comment.value)
    if (!writeGate.current(ticket)) return
    detail.value = result; accepted.value = false
    historyGate.cancel(); history.value = null; historyOpen.value = false; historyLoading.value = false
    savedMessage.value = `${variantDecisionLabel(result.setting.review!.decision)}；记录已保存，不代表任何图片已采用。`
    await loadList()
  } catch (error) { if (writeGate.current(ticket)) mutationError.value = apiErrorSummary(error, '审核未确认；请重新读取当前修订核对。') }
  finally { if (writeGate.current(ticket)) saving.value = false }
}
async function loadHistory(target = 1) {
  if (!detail.value || locked.value) return
  const ticket = historyGate.next(), id = detail.value.id
  historyOpen.value = true; historyLoading.value = true; historyError.value = ''
  try {
    const result = await bibleVariantApi.history(id, target)
    if (historyGate.current(ticket)) { history.value = result; historyPage.value = target }
  } catch (error) { if (historyGate.current(ticket)) historyError.value = apiErrorSummary(error, '修订历史读取失败。') }
  finally { if (historyGate.current(ticket)) historyLoading.value = false }
}
</script>

<template>
  <section class="variant-editor" :aria-label="`${entityName}的主图与变体设定`">
    <p class="variant-boundary">设定保存和审核不调用模型或采用素材；当前主图文字可在详情中另行生成及审核英文候选。主图采用 → 参考主图生成变体的生产链尚未接入。</p>
    <div class="variant-toolbar">
      <strong>{{ entityName }} · 主图 / 变体</strong>
      <button type="button" class="button ghost" :disabled="listLoading || saving" @click="loadList()">刷新清单</button>
      <button type="button" class="button primary" :disabled="locked" @click="startCreate()">新增主图设定</button>
    </div>
    <p v-if="listLoading" role="status">正在读取变体清单…</p>
    <p v-if="listError" role="alert">{{ listError }}<span v-if="listing"> 下方为上次读取结果，可能已过期。</span></p>
    <template v-if="listing">
      <p v-if="!listing.count && !listLoading && !listError">先登记主图设定，再从主图详情新增变体；不需要先生成图片。</p>
      <ul class="variant-list">
        <li v-for="row in listing.results" :key="row.id">
          <div><strong>{{ row.name }} · {{ appearanceRoleLabel(row.appearance_role) }}</strong><p>引用名称：{{ row.reference_name }}</p><small>修订 {{ row.revision }} · {{ variantDecisionLabel(row.current_decision) }} · {{ row.source ? '剧本声明来源' : '人工登记' }}</small></div>
          <button type="button" class="button ghost" :disabled="saving" @click="openDetail(row.id)">查看设定 / 审核</button>
        </li>
      </ul>
      <nav class="variant-toolbar" aria-label="变体分页">
        <button type="button" class="button ghost" :disabled="!listing.previous || listLoading || saving" @click="loadList(page - 1)">上一页</button>
        <span>第 {{ page }} 页 · 共 {{ listing.count }} 项设定</span>
        <button type="button" class="button ghost" :disabled="!listing.next || listLoading || saving" @click="loadList(page + 1)">下一页</button>
      </nav>
    </template>
    <p v-if="detailLoading" role="status">正在读取完整设定修订…</p>
    <p v-if="detailError" role="alert">{{ detailError }}</p>
    <p v-if="savedMessage" role="status">{{ savedMessage }}</p>
    <p v-if="mutationError" role="alert">{{ mutationError }} 草稿仍保留；冲突时请重新读取，系统不会覆盖已有修订或审核。</p>

    <form v-if="editing" class="form-stack variant-form" @submit.prevent="save">
      <h3>{{ creating ? (creationPrimary ? '基于主图新增变体设定' : '人工新增主图设定') : '另存设定修订' }}</h3>
      <label class="field"><span>造型 / 状态名称</span><input v-model="draft.name" required :disabled="saving"></label>
      <label class="field"><span>素材引用名称（不限语言）</span><input v-model="draft.reference_name" required :disabled="saving"><small>请区分同一实体的不同造型；不是固定文件名，也不是模型槽位编号。</small></label>
      <p v-if="primaryBasis">身份依据固定为「{{ primaryBasis.reference_name }}」修订 {{ primaryBasis.revision }}；本次只保存差异，不会切换主图设定或复制其文字。</p>
      <label v-else class="field"><span>本修订的共同身份锚点（可留空）</span><textarea v-model="draft.identity_anchor" rows="3" :disabled="saving"></textarea><small>共同身份由主图设定维护；不会从项目当前字段补齐或反向覆盖原 Bible。</small></label>
      <label class="field"><span>{{ primaryBasis ? '变体差异 / 状态（可留空）' : '主图外观 / 状态（可留空）' }}</span><textarea v-model="draft.appearance_anchor" rows="5" :disabled="saving"></textarea><small>{{ primaryBasis ? '只写相对固定主图的变化，不重复共同身份。' : '描述主图的基础造型。' }}用于参考图造型生成，不是视频动作提示词。</small></label>
      <p v-if="(!primaryBasis && !draft.identity_anchor) || !draft.appearance_anchor" role="status">未填写的文字按空值保存，可稍后补充；生成时再按所选执行合同检查输入，保存不代表图片已就绪。</p>
      <p v-if="draft.basis_asset_id">此修订保留已有身份参考素材来源。它不等于“已采用主图”，不会解除后续生产依赖。</p>
      <aside class="setting-impact" aria-label="保存影响">
        <strong>保存影响</strong>
        <p v-if="primaryBasis">只新增此变体的设定修订，不改主图、其他变体或固定的主图修订引用。</p>
        <p v-else>共同身份和主图外观只保存到此主图的新修订；已有变体仍引用原来的确切主图修订，不会被批量改写。</p>
        <p>旧修订、已保存的审核及冻结请求保留原样；依赖旧设定的后续使用会核对基线，发生漂移时须重新核对。保存不等于审核通过，也不生成、采用或重试任何图片。</p>
      </aside>
      <div class="variant-toolbar"><button type="button" class="button ghost" :disabled="saving" @click="cancelEdit">取消编辑</button><button type="submit" class="button primary" :disabled="saving || !dirty">{{ saving ? '正在保存…' : '保存新修订' }}</button></div>
    </form>
    <section v-else-if="detail && !detailLoading" class="variant-detail">
      <h3>{{ detail.setting.name }} · {{ appearanceRoleLabel(detail.appearance_role) }} · 修订 {{ detail.setting.revision }}</h3>
      <p>{{ variantDecisionLabel(detail.setting.review?.decision ?? null) }} · {{ isCurrent ? '当前修订' : `历史只读；当前为修订 ${detail.revision}` }}</p>
      <p class="variant-boundary">此处只显示本修订的人工审核记录；没有人审记录不等于生产流程未通过。配方自动决定请查看对应 ProductionRun 的审批审计事实。</p>
      <dl><dt>素材引用名称</dt><dd>{{ detail.setting.reference_name }}</dd></dl>
      <div class="setting-sections">
        <section aria-label="共同身份" class="setting-section">
          <h4>共同身份</h4>
          <p v-if="detail.primary_setting">只读来源：「{{ detail.primary_setting.reference_name }}」修订 {{ detail.primary_setting.revision }}。不在变体中重复保存。</p>
          <p v-else>由此主图修订维护，不从实体基础描述补齐。</p>
          <p class="setting-text">{{ (detail.primary_setting ? detail.primary_setting.identity_anchor : detail.setting.identity_anchor) || '此修订未填写共同身份文字。' }}</p>
        </section>
        <section aria-label="主图外观" class="setting-section">
          <h4>{{ detail.primary_setting ? '固定主图外观（只读）' : '主图外观' }}</h4>
          <p class="setting-text">{{ (detail.primary_setting ? detail.primary_setting.appearance_anchor : detail.setting.appearance_anchor) || '此修订未填写主图外观文字。' }}</p>
        </section>
        <section v-if="detail.primary_setting" aria-label="变体差异" class="setting-section">
          <h4>变体差异 / 状态</h4>
          <p class="setting-text">{{ detail.setting.appearance_anchor || '此修订未填写差异文字，可另存修订补充。' }}</p>
        </section>
      </div>
      <section v-if="detail.primary_setting" class="primary-dependency" aria-label="固定主图设定依赖">
        <strong>身份依据：{{ detail.primary_setting.reference_name }} · 修订 {{ detail.primary_setting.revision }}</strong>
        <p>{{ variantDecisionLabel(detail.primary_setting.review?.decision ?? null) }}。这是本变体保存的确切设定，不随主图的新修订自动变化，也不代表主图图片已采用。</p>
        <details><summary>固定主图修订标识（只读）</summary><p>修订 ID：{{ detail.primary_setting.id }}</p><p>内容 SHA256：{{ detail.primary_setting.content_sha256 }}</p></details>
      </section>
      <BibleSettingSource :detail="detail" />
      <p v-if="detail.setting.basis_asset_id">已有身份参考来源不代表主图采用；本页不会变更该素材。</p>
      <details><summary>修订与来源标识（只读）</summary><p>修订 ID：{{ detail.setting.id }}</p><p>内容 SHA256：{{ detail.setting.content_sha256 }}</p><p>保存者：{{ detail.setting.created_by_id }} · {{ new Date(detail.setting.created_at).toLocaleString() }}</p><template v-if="detail.setting.basis_asset_id"><p>身份参考素材：{{ detail.setting.basis_asset_id }}</p><p>参考素材 SHA256：{{ detail.setting.basis_asset_sha256 }}</p></template></details>
      <div v-if="detail.setting.review"><p>审核者：{{ detail.setting.review.decided_by_id }} · {{ new Date(detail.setting.review.created_at).toLocaleString() }}</p><p>审核说明：{{ detail.setting.review.comment || '未填写' }}</p></div>
      <div class="variant-toolbar"><button v-if="isCurrent" type="button" class="button ghost" :disabled="locked" @click="startEdit">修改并另存修订</button><button v-else type="button" class="button ghost" :disabled="locked" @click="openDetail(detail.id)">打开当前修订</button><button v-if="detail.appearance_role === 'primary'" type="button" class="button primary" :disabled="locked" @click="startCreate(detail)">基于此修订新增变体</button><button type="button" class="button ghost" :disabled="locked || historyLoading" @click="loadHistory()">查看修订历史</button></div>
      <div v-if="canReview" class="form-stack variant-review">
        <label class="field"><span>本次审核说明（可不填）</span><textarea v-model="comment" rows="2" :disabled="saving"></textarea></label>
        <label><input v-model="accepted" type="checkbox" :disabled="saving">我已核对本修订的名称、共同身份和外观设定；本次仅作设定决定，不采用图片。</label>
        <div class="variant-toolbar"><button type="button" class="button primary" :disabled="saving || !accepted" @click="review('approve')">确认此设定修订</button><button type="button" class="button ghost" :disabled="saving || !accepted" @click="review('reject')">拒绝此设定修订</button></div>
      </div>
      <TranslationReviewPanel v-if="translationFields.length && !detailError" :key="detail.setting.id"
        :project-id="scope.project_id" :fields="translationFields" :blocked-reason="saving ? '正在保存设定，请等待完成。' : ''"
        compact title="当前主图设定 · 英文候选审核" @changed="emit('translationsChanged')" />
    </section>
    <section v-if="historyOpen" class="variant-history">
      <h3>修订历史</h3><p v-if="historyLoading" role="status">正在读取历史…</p><p v-if="historyError" role="alert">{{ historyError }} 下方已读取记录不代表当前状态。</p>
      <ul v-if="history" class="variant-list"><li v-for="row in history.results" :key="row.id"><span>修订 {{ row.revision }} · {{ row.reference_name }} · {{ variantDecisionLabel(row.review?.decision ?? null) }}</span><button type="button" class="button ghost" :disabled="locked" @click="detail && openDetail(detail.id, row.revision)">查看此修订</button></li></ul>
      <nav v-if="history" class="variant-toolbar" aria-label="修订历史分页"><button type="button" class="button ghost" :disabled="!history.previous || historyLoading || locked" @click="loadHistory(historyPage - 1)">上一页</button><span>第 {{ historyPage }} 页 · 共 {{ history.count }} 个修订</span><button type="button" class="button ghost" :disabled="!history.next || historyLoading || locked" @click="loadHistory(historyPage + 1)">下一页</button></nav>
    </section>
  </section>
</template>

<style scoped>
.variant-editor{display:flex;flex-direction:column;gap:14px;min-width:0;color:var(--text)}
.variant-editor p,.variant-editor dd,.variant-editor small{overflow-wrap:anywhere;line-height:1.6}
.variant-boundary{color:var(--text-secondary);padding-bottom:12px;border-bottom:1px solid var(--border)}
.variant-toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:10px}
.variant-list{list-style:none;padding:0;margin:0}.variant-list li{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:14px 0;border-bottom:1px solid var(--border)}
.variant-list li>div,.variant-list li>span{min-width:0;overflow-wrap:anywhere}.variant-list p{margin:4px 0}.variant-list small{color:var(--text-secondary)}
.variant-form,.variant-detail,.variant-history{border-top:1px solid var(--border);padding-top:18px}.variant-detail dt{color:var(--text-secondary);margin-top:14px}.variant-detail dd{margin:5px 0 18px;white-space:pre-wrap}.variant-detail details{margin:16px 0}.variant-review{margin-top:18px}
.setting-sections{display:grid;gap:12px}.setting-section,.setting-impact{border:1px solid var(--border);border-radius:var(--radius-md,8px);padding:14px;min-width:0}.setting-section h4{margin:0}.setting-text{white-space:pre-wrap}.setting-impact p:last-child{margin-bottom:0}
.variant-editor [role=alert]{color:var(--danger,#ef8a88)}.variant-editor [role=status]{color:var(--text-secondary)}.variant-editor input:not([type=checkbox]),.variant-editor textarea{width:100%;max-width:100%;box-sizing:border-box}.variant-editor button{min-height:36px;white-space:normal}.variant-review>label{line-height:1.6}
@media(max-width:600px){.variant-list li{align-items:flex-start;flex-direction:column}.variant-toolbar{align-items:flex-start}.variant-toolbar button{flex:1 1 auto}.variant-editor h3{overflow-wrap:anywhere}}
</style>
