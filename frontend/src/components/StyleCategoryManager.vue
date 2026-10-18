<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '@/services/api'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { categoryForm, type StyleCategory } from '@/services/styleManagement'

const props = defineProps<{ canManage: boolean }>()
const emit = defineEmits<{ back: []; changed: []; busy: [value: boolean]; references: [key: string] }>()
const rows = ref<StyleCategory[]>([]), count = ref(0), page = ref(1), next = ref(false)
const search = ref(''), status = ref(''), loading = ref(false), busy = ref(false), error = ref(''), message = ref('')
const selected = ref<StyleCategory|null>(null), editing = ref(false), form = ref(categoryForm())
const deleting = ref(false), confirmed = ref(false)
const panel = ref<HTMLElement|null>(null), editor = ref<HTMLElement|null>(null)
let sequence = 0
const stateChange = computed(() => selected.value && selected.value.is_active !== form.value.is_active)
const canSave = computed(() => props.canManage && form.value.name.trim() && Number.isInteger(form.value.sort_order) && form.value.sort_order >= 0 && (!stateChange.value || confirmed.value) && !busy.value)
watch(busy, value => emit('busy', value), {flush:'sync'})
const load = async (reset=false) => {
  if(reset) page.value=1
  const token=++sequence;loading.value=true;error.value=''
  try {
    const {data}=await api.get('/style-categories/',{params:{page:page.value,search:search.value || undefined,is_active:status.value || undefined}})
    if(token!==sequence)return
    rows.value=data.results;count.value=data.count;next.value=Boolean(data.next)
  } catch(e){if(token===sequence)error.value=apiErrorSummary(e,'题材分类加载失败。')}
  finally{if(token===sequence)loading.value=false}
}
const edit = async (row?:StyleCategory) => {
  selected.value=row || null;form.value=categoryForm(row);editing.value=true;deleting.value=false;confirmed.value=false;message.value=''
  await nextTick();editor.value?.scrollIntoView({block:'start'})
}
const save = async () => {
  if(!canSave.value)return
  busy.value=true;error.value='';message.value=''
  try{
    const payload={...form.value,name:form.value.name.trim(),description:form.value.description.trim()}
    const changed=selected.value ? Object.fromEntries(Object.entries(payload).filter(([key,value])=>value!==selected.value![key as keyof StyleCategory])) : payload
    if(!Object.keys(changed).length){editing.value=false;return}
    const {data}=selected.value ? await api.patch(`/style-categories/${selected.value.id}/`,changed) : await api.post('/style-categories/',payload)
    selected.value=data;form.value=categoryForm(data);editing.value=false;confirmed.value=false
    message.value='题材分类已保存。已有分类关联保留，不触发模型调用。';emit('changed');await load(true)
  }catch(e){error.value=apiErrorSummary(e,'题材分类保存失败。')}
  finally{busy.value=false}
}
const remove = async () => {
  if(!selected.value || !confirmed.value || !props.canManage || busy.value)return
  busy.value=true;error.value='';message.value=''
  try{
    await api.delete(`/style-categories/${selected.value.id}/`)
    selected.value=null;editing.value=false;deleting.value=false;confirmed.value=false
    message.value='题材分类已删除，无法撤销；没有删除风格、项目或素材。';emit('changed');await load(true)
  }catch(e){error.value=apiErrorSummary(e,'题材分类删除失败。')}
  finally{busy.value=false}
}
onMounted(()=>{panel.value?.scrollIntoView({block:'start'});load()})
onUnmounted(()=>{sequence++})
</script>

<template>
  <section ref="panel" class="category-manager" aria-label="管理题材分类">
    <div class="category-actions"><button class="button ghost" :disabled="busy" @click="emit('back')">返回视觉风格</button><h3>管理题材分类</h3></div>
    <p class="category-help">用于风格归类，可由提示词策略作为题材参考；不决定模型、工作流或配方。名称、说明和排序可调整，排序数字越小越靠前。</p>
    <p v-if="!canManage" class="category-help">分类由管理员统一维护，你可以查看和选择。</p>
    <p class="category-help">关联数量包含全部风格；点击后仅列出你有权查看的风格。</p>
    <form class="category-actions" @submit.prevent="load(true)"><label class="field"><span>搜索分类</span><input v-model="search" placeholder="名称或说明"></label><label class="field"><span>分类状态</span><select v-model="status"><option value="">全部状态</option><option value="true">启用</option><option value="false">停用</option></select></label><button class="button ghost" :disabled="loading || busy">查询</button><button v-if="canManage" type="button" class="button primary" :disabled="busy" @click="edit()">新增题材分类</button></form>
    <p v-if="error" class="category-error" role="alert">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
    <div class="category-table" :aria-busy="loading"><table><thead><tr><th>题材分类 / 说明</th><th>顺序</th><th>状态</th><th>关联风格</th><th>操作</th></tr></thead><tbody><tr v-for="row in rows" :key="row.id"><td><strong>{{ row.name }}</strong><small>{{ row.description || '未填写说明' }}</small></td><td>{{ row.sort_order }}</td><td>{{ row.is_active?'启用':'已停用' }}</td><td><button class="button ghost small" :disabled="busy" @click="emit('references',row.key)">{{ row.style_count }} 个风格</button></td><td><button v-if="canManage" class="button ghost small" :disabled="busy" @click="edit(row)">编辑</button><span v-else>只读</span></td></tr></tbody></table></div>
    <p v-if="loading" role="status">正在加载题材分类…</p><p v-else-if="!rows.length && !error">没有匹配的分类。</p>
    <div class="category-actions"><span>共 {{ count }} 项 · 第 {{ page }} 页</span><button class="button ghost small" :disabled="page<=1 || loading || busy" @click="page--;load()">上一页</button><button class="button ghost small" :disabled="!next || loading || busy" @click="page++;load()">下一页</button></div>
    <form v-if="editing && canManage" ref="editor" class="category-editor" @submit.prevent="save">
      <h4>{{ selected?'编辑题材分类':'新增题材分类' }}</h4>
      <fieldset :disabled="busy"><label class="field"><span>分类名称 *</span><input v-model="form.name" required maxlength="120" placeholder="例如：校园青春"></label><label class="field"><span>说明（仅用于选择）</span><textarea v-model="form.description" rows="2" maxlength="500" placeholder="说明哪些风格适合此分类"></textarea></label><label class="field"><span>显示顺序</span><input v-model.number="form.sort_order" type="number" min="0" max="2147483647" step="1" required></label><label class="category-check"><input v-model="form.is_active" type="checkbox">启用，允许风格选择此分类</label></fieldset>
      <p v-if="selected">当前被 {{ selected.style_count }} 个风格引用。改名会影响后续读取的题材名称，不改写已冻结请求。停用后已有分类保留，但不能新选；有引用时不能删除。</p>
      <label v-if="stateChange || deleting" class="category-check"><input v-model="confirmed" type="checkbox" :disabled="busy">{{ deleting?`确认永久删除“${selected?.name}”，无法撤销`:'确认更改启停状态，已有风格关联保留' }}</label>
      <div class="category-actions"><button v-if="!deleting" class="button primary" :disabled="!canSave">保存分类</button><button v-if="selected && !deleting" type="button" class="button danger-ghost" :disabled="busy || selected.style_count>0" @click="deleting=true;confirmed=false">删除分类</button><button v-if="deleting" type="button" class="button danger-ghost" :disabled="busy || !confirmed" @click="remove">确认删除分类</button><button type="button" class="button ghost" :disabled="busy" @click="editing=false;deleting=false">取消编辑</button></div>
    </form>
  </section>
</template>

<style scoped>
.category-manager{display:grid;gap:16px}.category-actions{display:flex;gap:12px;align-items:end;flex-wrap:wrap}.category-actions h3{margin:0;align-self:center}.category-help{color:var(--text-secondary);line-height:1.6;margin:0}.category-table{overflow-x:auto}table{width:100%;border-collapse:collapse;text-align:left}th,td{padding:12px;border-bottom:1px solid var(--border)}td:first-child{min-width:160px;max-width:380px;overflow-wrap:anywhere}td small{display:block;color:var(--text-secondary);margin-top:6px}.category-editor{border:1px solid var(--border);border-radius:8px;padding:16px}.category-editor h4{margin:0 0 14px}fieldset{display:grid;gap:14px;border:0;padding:0;min-width:0}.category-check{display:flex;gap:8px;align-items:center}.category-check input{width:auto}.category-error{color:var(--red);overflow-wrap:anywhere}.category-manager button:focus-visible,.category-manager input:focus-visible,.category-manager textarea:focus-visible,.category-manager select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:720px){.category-actions .field{flex:1;min-width:120px}.category-editor{padding:12px}th,td{padding:8px}}
</style>
