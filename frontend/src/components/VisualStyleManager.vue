<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import BaseModal from '@/components/BaseModal.vue'
import StyleCategoryManager from '@/components/StyleCategoryManager.vue'
import { api, fetchAllPages } from '@/services/api'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { styleForm, styleReferenceCount, styleWritePayload, type ManagedStyle, type StyleCategory } from '@/services/styleManagement'
import { useStudioStore } from '@/stores/studio'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: []; changed: [] }>()
const studio = useStudioStore()
const rows = ref<ManagedStyle[]>([]), count = ref(0), page = ref(1), hasNext = ref(false)
const search = ref(''), active = ref(''), scope = ref(''), loading = ref(false), busy = ref(false)
const error = ref(''), message = ref(''), categories = ref<StyleCategory[]>([])
const managingCategories = ref(false), canManageCategories = ref(false), categoryBusy = ref(false), categoriesReady = ref(false), categoryFilter = ref('')
const scopes = ref<Array<{value:string;display_name:string}>>([])
const selected = ref<ManagedStyle|null>(null), mode = ref<'view'|'edit'|'new'>('view'), form = ref(styleForm())
const action = ref<'delete'|'toggle'|''>(''), confirmed = ref(false)
const referenceKind = ref('projects'), referencePage = ref(1), referenceCount = ref(0), referenceNext = ref(false)
const references = ref<Array<{id:string;name:string;kind:string}>>([]), referenceLoading = ref(false), referenceError = ref(''), referencesOpen = ref(false)
let sequence = 0, referenceSequence = 0, catalogSequence = 0
const canSave = computed(() => form.value.name.trim() && categoriesReady.value && (!form.value.category || categories.value.some(item => item.key===form.value.category && (item.is_active || selected.value?.category===item.key))) && scopes.value.length && !busy.value)
const close = () => { if (!busy.value && !categoryBusy.value) emit('close') }
const load = async (reset = false) => {
  const token = ++sequence
  if (reset) page.value = 1
  loading.value = true; error.value = ''
  try {
    const { data } = await api.get('/styles/', { params: { search: search.value || undefined, is_active: active.value || undefined, scope: scope.value || undefined, category: categoryFilter.value || undefined, page: page.value } })
    if (token !== sequence || !props.open) return
    rows.value = data.results; count.value = data.count; hasNext.value = Boolean(data.next)
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e, '风格加载失败，请重试。') }
  finally { if (token === sequence) loading.value = false }
}
const inspect = (row: ManagedStyle) => {
  selected.value = row; form.value = styleForm(row); mode.value = 'view'; action.value = ''; confirmed.value = false
  referencesOpen.value = false; references.value = []; referenceSequence++; referenceError.value = ''
}
const create = () => { selected.value = null; form.value = styleForm(); mode.value = 'new'; action.value = ''; message.value = ''; referencesOpen.value = false; referenceSequence++ }
const refreshChoices = async () => {
  await studio.loadStyles(true)
  emit('changed')
}
const save = async () => {
  if (!canSave.value) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    const payload = styleWritePayload(form.value, selected.value || undefined)
    if (!Object.keys(payload).length) { mode.value = 'view'; return }
    const { data } = selected.value ? await api.patch(`/styles/${selected.value.id}/`, payload) : await api.post('/styles/', payload)
    inspect(data); message.value = '风格已保存。不会触发模型调用，也不会改写已冻结的模型请求。'
    await refreshChoices(); await load(true)
  } catch (e) { error.value = apiErrorSummary(e, '保存或刷新风格失败。') }
  finally { busy.value = false }
}
const mutate = async () => {
  if (!selected.value || !confirmed.value || busy.value) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    if (action.value === 'delete') {
      await api.delete(`/styles/${selected.value.id}/`)
      selected.value = null; message.value = '风格已删除，无法撤销；未删除任何项目或素材。'
    } else {
      const { data } = await api.patch(`/styles/${selected.value.id}/`, { is_active: !selected.value.is_active })
      inspect(data); message.value = data.is_active ? '风格已启用。' : '风格已停用。已有引用保留，新建项目不再提供此选项。'
    }
    action.value = ''; confirmed.value = false
    await refreshChoices(); await load(true)
  } catch (e) { error.value = apiErrorSummary(e, '操作失败。') }
  finally { busy.value = false }
}
const loadReferences = async (reset = false) => {
  if (!selected.value) return
  if (reset) referencePage.value = 1
  const token = ++referenceSequence
  referencesOpen.value = true; referenceLoading.value = true; referenceError.value = ''
  try {
    const {data} = await api.get(`/styles/${selected.value.id}/references/`, {params:{kind:referenceKind.value,page:referencePage.value}})
    if (token !== referenceSequence) return
    references.value = data.results; referenceCount.value = data.count; referenceNext.value = Boolean(data.next)
  } catch (e) { if(token===referenceSequence) referenceError.value = apiErrorSummary(e, '关联读取失败。') }
  finally { if(token===referenceSequence) referenceLoading.value = false }
}
const loadCategories = async () => {
  const token=++catalogSequence;categoriesReady.value=false
  try {
    const [catalog, choices] = await Promise.all([api.get('/styles/catalog/'),fetchAllPages<StyleCategory>('/style-categories/')])
    if(token!==catalogSequence || !props.open)return
    categories.value=choices;scopes.value=catalog.data.scopes;canManageCategories.value=catalog.data.can_manage_categories;categoriesReady.value=true
  } catch(e){if(token===catalogSequence && props.open)error.value=apiErrorSummary(e,'题材分类选项加载失败，请重试。')}
}
const categoriesChanged = async () => {
  try {
    await loadCategories()
    if(categoriesReady.value && form.value.category && !categories.value.some(item=>item.key===form.value.category)) {
      form.value.category=null;message.value='之前选择的分类已删除，已清空未保存的分类选择；其他填写内容保留。'
    }
    if(categoriesReady.value && categoryFilter.value && !categories.value.some(item=>item.key===categoryFilter.value))categoryFilter.value=''
    await load(true);await refreshChoices()
  } catch(e) {
    error.value=apiErrorSummary(e,'分类已保存，但风格选项刷新失败，请重新加载。')
  }
}
const categoryReferences = (key:string) => {
  categoryFilter.value=key;search.value='';active.value='';scope.value='';managingCategories.value=false;load(true)
}
watch(() => props.open, async open => {
  sequence++; referenceSequence++
  catalogSequence++
  if (!open) return
  selected.value = null; mode.value = 'view'; message.value = ''; categories.value = []; scopes.value = []; action.value = '';managingCategories.value=false;categoryFilter.value=''
  await Promise.all([load(true),loadCategories()])
}, {immediate:true})
</script>

<template>
  <BaseModal :open="open" title="视觉风格管理" width="1180px" :dismissible="!busy && !categoryBusy" @close="close">
    <StyleCategoryManager v-if="managingCategories" :can-manage="canManageCategories" @back="managingCategories=false" @changed="categoriesChanged" @busy="categoryBusy=$event" @references="categoryReferences" />
    <div v-show="!managingCategories" class="style-management">
      <p class="style-help">简介帮助用户选择；视觉提示词和负向约束供策略按需引用。更改会影响后续读取该风格的制作步骤，不会重跑任务或改写已冻结请求。</p>
      <form class="style-toolbar" @submit.prevent="load(true)">
        <label class="field"><span>搜索风格</span><input v-model="search" placeholder="名称、简介或提示词"></label>
        <label class="field"><span>状态</span><select v-model="active"><option value="">全部状态</option><option value="true">启用</option><option value="false">停用</option></select></label>
        <label class="field"><span>范围</span><select v-model="scope"><option value="">全部可见</option><option value="personal">我的风格</option><option value="global">共享风格</option></select></label>
        <label class="field"><span>题材分类</span><select v-model="categoryFilter"><option value="">全部分类</option><option v-for="item in categories" :key="item.key" :value="item.key">{{ item.name }}{{ item.is_active?'':'（已停用）' }}</option></select></label>
        <button class="button ghost" :disabled="loading || busy">查询</button><button type="button" class="button primary" :disabled="busy" @click="create">新增风格</button>
        <button type="button" class="button ghost" :disabled="busy || !categoriesReady" @click="managingCategories=true">{{ canManageCategories?'管理题材分类':'查看题材分类' }}</button>
      </form>
      <p v-if="error" class="style-error" role="alert">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
      <p v-if="!categoriesReady" class="style-help">题材选项尚未就绪，暂不能保存风格。<button class="button ghost small" @click="loadCategories">重新加载选项</button></p>
      <div class="style-workspace">
        <section aria-label="风格列表" :aria-busy="loading">
          <p v-if="loading" role="status">正在加载风格…</p>
          <p v-else-if="!rows.length && !error">没有匹配风格，可以清空筛选或新增。</p>
          <ul class="style-list"><li v-for="row in rows" :key="row.id"><button type="button" :class="{active:selected?.id===row.id}" :disabled="busy" @click="inspect(row)"><strong>{{ row.name }}</strong><span>{{ row.is_active ? '启用' : '已停用' }} · {{ row.scope==='global' ? '共享' : '个人' }}{{ row.can_manage ? '' : ' · 只读' }}</span><small>{{ row.description || '尚未填写简介' }}</small></button></li></ul>
          <div class="style-pager"><span>共 {{ count }} 项 · 第 {{ page }} 页</span><button class="button ghost small" :disabled="page<=1 || loading || busy" @click="page--;load()">上一页</button><button class="button ghost small" :disabled="!hasNext || loading || busy" @click="page++;load()">下一页</button></div>
        </section>
        <section class="style-detail" aria-label="风格详情">
          <form v-if="mode!=='view'" @submit.prevent="save">
            <h3>{{ mode==='new'?'新增视觉风格':'编辑视觉风格' }}</h3>
            <fieldset :disabled="busy">
              <label class="field"><span>风格名称 *</span><input v-model="form.name" required maxlength="120"></label>
              <label class="field"><span>题材分类（辅助参考，可选）</span><select v-model="form.category"><option :value="null">未分类</option><option v-for="item in categories.filter(c=>c.is_active || c.key===selected?.category)" :key="item.key" :value="item.key" :disabled="!item.is_active">{{ item.name }}{{ item.is_active?'':'（已停用，原关联保留）' }}</option></select><small>用于归类；策略引用时可作题材参考，不决定模型、工作流或配方。</small><small v-if="form.category">{{ categories.find(c=>c.key===form.category)?.description }}</small></label>
              <button v-if="canManageCategories" type="button" class="button ghost" :disabled="!categoriesReady" @click="managingCategories=true">管理题材分类（保留当前填写）</button>
              <label v-if="mode==='new' && scopes.length>1" class="field"><span>共享范围</span><select v-model="form.scope"><option v-for="item in scopes" :key="item.value" :value="item.value">{{ item.display_name }}</option></select></label>
              <label class="field"><span>选择卡片简介 · 供用户阅读</span><textarea v-model="form.description" rows="3" placeholder="描述色彩、光线、材质等差异，不重复平台通用能力。"></textarea></label>
              <label class="field"><span>视觉提示词 · 供模型参考</span><textarea v-model="form.visual_prompt" rows="5" placeholder="填写实际视觉要求；只有策略引用时才进入模型输入。"></textarea></label>
              <label class="field"><span>负向约束 · 供策略按需引用</span><textarea v-model="form.negative_prompt" rows="3" placeholder="希望避免的画面特征；不代表所有模型都支持负向提示词。"></textarea></label>
              <label class="style-check"><input type="checkbox" v-model="form.is_active">启用，可供新建项目选择</label>
            </fieldset>
            <p v-if="selected && styleReferenceCount(selected)">此风格已有引用。请确认修改对后续制作的影响；如需保留原有视觉要求，建议新建风格。</p>
            <div class="style-actions"><button class="button primary" :disabled="!canSave">{{ busy?'正在保存…':'保存风格' }}</button><button type="button" class="button ghost" :disabled="busy" @click="mode='view'">取消编辑</button></div>
          </form>
          <template v-else-if="selected">
            <h3>{{ selected.name }}</h3><p>{{ selected.scope==='global'?'共享风格':'个人风格' }} · {{ selected.is_active?'启用':'已停用' }} · {{ categories.find(c=>c.key===selected?.category)?.name || selected.category_label || '未分类' }}</p>
            <h4>选择卡片简介</h4><p class="style-text">{{ selected.description || '未填写简介' }}</p>
            <h4>视觉提示词</h4><p class="style-text">{{ selected.visual_prompt || '未配置视觉提示词' }}</p>
            <h4>负向约束</h4><p class="style-text">{{ selected.negative_prompt || '未配置负向约束' }}</p>
            <p>关联总数：{{ selected.project_count }} 个项目 · {{ selected.asset_count }} 个素材 · {{ selected.generation_job_count }} 个生成任务</p>
            <div class="style-actions"><button class="button ghost" @click="loadReferences(true)">查看关联</button><template v-if="selected.can_manage"><button class="button ghost" :disabled="busy || !categoriesReady" @click="form=styleForm(selected);mode='edit';action=''">编辑风格</button><button class="button ghost" :disabled="busy" @click="action='toggle';confirmed=false">{{ selected.is_active?'停用':'启用' }}</button><button class="button danger-ghost" :disabled="busy || styleReferenceCount(selected)>0" @click="action='delete';confirmed=false">删除风格</button></template></div>
            <p v-if="!selected.can_manage" class="style-help">此共享风格只读，请联系管理员修改，或新建自己的风格。</p><p v-else-if="styleReferenceCount(selected)>0" class="style-help">已有引用，不能删除；停用不会清空已有项目、素材或任务的关联。</p>
            <div v-if="action" class="style-confirm"><p>{{ action==='delete' ? `将永久删除“${selected.name}”，无法撤销。` : `将${selected.is_active?'停用':'启用'}“${selected.name}”，已有引用会保留。` }}</p><label class="style-check"><input v-model="confirmed" type="checkbox" :disabled="busy">我已确认对象和影响</label><div class="style-actions"><button class="button primary" :disabled="!confirmed || busy" @click="mutate">确认{{ action==='delete'?'删除':selected.is_active?'停用':'启用' }}</button><button class="button ghost" :disabled="busy" @click="action=''">取消</button></div></div>
            <section v-if="referencesOpen" class="style-references"><h4>关联明细</h4><p class="style-help">明细仅列出你有权查看的记录，可能少于关联总数；任务以记录编号显示。</p><label class="field"><span>关联类型</span><select v-model="referenceKind" @change="loadReferences(true)"><option value="projects">项目</option><option value="assets">素材</option><option value="jobs">生成任务</option></select></label><p v-if="referenceError" class="style-error" role="alert">{{ referenceError }} <button class="button ghost small" @click="loadReferences()">重试</button></p><p v-else-if="referenceLoading">正在加载关联…</p><template v-else><ul><li v-for="item in references" :key="item.id"><RouterLink v-if="item.kind==='projects'" :to="`/projects/${item.id}`" @click="close">{{ item.name }}</RouterLink><span v-else>{{ item.name }}</span></li></ul><p v-if="!references.length">此类型暂无可见关联。</p></template><div class="style-pager"><span>可见 {{ referenceCount }} 项</span><button class="button ghost small" :disabled="referencePage<=1 || referenceLoading" @click="referencePage--;loadReferences()">上一页</button><button class="button ghost small" :disabled="!referenceNext || referenceLoading" @click="referencePage++;loadReferences()">下一页</button></div></section>
          </template>
          <p v-else class="style-help">选择风格查看详情，或新增风格。新增不会调用模型。</p>
        </section>
      </div>
    </div>
  </BaseModal>
</template>

<style scoped>
.style-management{display:grid;gap:16px}
.style-help{color:var(--text-secondary);line-height:1.7;margin:0}
.style-toolbar{display:flex;gap:12px;align-items:end;flex-wrap:wrap}
.style-toolbar .field:first-child{flex:1;min-width:180px}
.style-workspace{display:grid;grid-template-columns:minmax(240px,1fr) minmax(0,1.7fr);gap:24px}
.style-list{list-style:none;padding:0;margin:0;display:grid;gap:8px;max-height:560px;overflow:auto}
.style-list button{width:100%;text-align:left;padding:12px;border:1px solid var(--border);border-radius:8px;background:var(--surface);display:grid;gap:6px;color:inherit;overflow-wrap:anywhere;cursor:pointer}
.style-list button.active{border-color:var(--accent)}
.style-list span,.style-list small{color:var(--text-secondary);line-height:1.6}
.style-detail{min-width:0;border-left:1px solid var(--border);padding-left:24px}
.style-detail h3{margin:0 0 12px}
.style-detail h4{margin:18px 0 8px}
.style-detail fieldset{border:0;padding:0;margin:0;display:grid;gap:14px;min-width:0}
.style-text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7}
.style-actions,.style-pager{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-top:14px}
.style-pager{font-size:12px}
.style-check{display:flex;align-items:center;gap:8px;margin:12px 0}
.style-check input{width:auto}
.style-confirm{border:1px solid var(--accent);padding:12px;margin-top:14px;border-radius:8px}
.style-error{color:var(--red);overflow-wrap:anywhere}
.style-references{border-top:1px solid var(--border);margin-top:16px}
.style-references li{overflow-wrap:anywhere;margin:8px 0}
.style-management button:focus-visible,.style-management input:focus-visible,.style-management select:focus-visible,.style-management textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media(max-width:720px){.style-workspace{grid-template-columns:minmax(0,1fr)}.style-detail{border-left:0;border-top:1px solid var(--border);padding:16px 0 0}.style-list{max-height:230px}.style-toolbar .field{flex:1;min-width:120px}.style-management{gap:12px}}
</style>
