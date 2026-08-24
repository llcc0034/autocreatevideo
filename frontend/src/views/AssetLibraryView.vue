<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AlertTriangle, Box, ChevronDown, Copy, Image, Layers3, Pencil, Plus, Save, Search, Sparkles, Trash2, Upload, UserRound, X } from 'lucide-vue-next'
import { useStudioStore } from '@/stores/studio'
import { useAuthStore } from '@/stores/auth'
import type { Asset, AssetType, BibleDraftEntityType } from '@/types'
import BaseModal from '@/components/BaseModal.vue'
import AssetImage from '@/components/AssetImage.vue'

const studio = useStudioStore(); const auth = useAuthStore(); const route = useRoute(); const router = useRouter()
const selectedStyle = ref(typeof route.query.style === 'string' ? route.query.style : '')
const selectedType = ref<AssetType | ''>(''); const search = ref(''); const selected = ref<Asset | null>(null); const loading = ref(false)
const types: Array<{ value: AssetType | ''; label: string; icon: typeof Layers3 }> = [{ value: '', label: '全部素材', icon: Layers3 }, { value: 'character', label: '人物', icon: UserRound }, { value: 'scene', label: '场景', icon: Image }, { value: 'prop', label: '道具', icon: Box }, { value: 'image', label: '通用图片', icon: Image }]
const imageKinds: Array<{value:BibleDraftEntityType;label:string}> = [{value:'character',label:'人物参考'}, {value:'scene',label:'场景参考'}, {value:'prop',label:'道具参考'}]
const style = computed(() => studio.styles.find((item) => item.id === selectedStyle.value))
let refreshTimer = 0
const refresh = async () => { loading.value = true; try { await studio.refreshAssets({ style: selectedStyle.value || undefined, type: selectedType.value || undefined, search: search.value || undefined }) } finally { loading.value = false } }
watch([selectedStyle, selectedType, search], () => { window.clearTimeout(refreshTimer); refreshTimer = window.setTimeout(refresh, 220); router.replace({ query: selectedStyle.value ? { style: selectedStyle.value } : {} }) })
onMounted(refresh)
const typeLabel = (type: AssetType) => types.find((item) => item.value === type)?.label || '通用图片'
const typeIcon = (type: AssetType) => types.find((item) => item.value === type)?.icon || Layers3
const assetAccent = (asset: Asset) => studio.styles.find((item) => item.id === asset.styleId)?.accent
const copySeed = async (asset: Asset) => { await navigator.clipboard.writeText(String(asset.seed || '')); studio.notify('Seed 已复制') }

const assetModalOpen=ref(false);const assetSaving=ref(false);const assetError=ref('');const uploadInput=ref<HTMLInputElement|null>(null);const uploadPreview=ref('');const fullPreview=ref<Asset|null>(null)
const deleteTarget=ref<Asset|null>(null);const deleteConfirmName=ref('');const deletePending=ref(false);const deleteError=ref('')
const blankAssetForm=()=>({file:null as File|null,kind:'character' as BibleDraftEntityType|'reference',name:'',description:'',promptHint:'',styleId:selectedStyle.value})
const assetForm=ref(blankAssetForm());const editingAssetId=ref('')
const canEditAsset=(asset:Asset)=>Boolean(asset.ownerId&&(auth.user?.isStaff||String(asset.ownerId)===String(auth.user?.id)))
const openCreateAsset=()=>{assetForm.value=blankAssetForm();editingAssetId.value='';assetError.value='';uploadPreview.value='';assetModalOpen.value=true}
const editableAssetKind=(asset:Asset):BibleDraftEntityType|'reference'=>{
  if(['character','scene','prop'].includes(asset.backendKind||''))return asset.backendKind as BibleDraftEntityType
  if(asset.backendKind==='reference'&&['character','scene','prop'].includes(asset.type))return asset.type as BibleDraftEntityType
  return 'reference'
}
const openEditAsset=(asset:Asset)=>{if(!canEditAsset(asset))return;assetForm.value={file:null,kind:editableAssetKind(asset),name:asset.name,description:asset.description,promptHint:asset.promptHint||'',styleId:asset.styleId};editingAssetId.value=asset.id;assetError.value='';uploadPreview.value=asset.thumbnail||'';assetModalOpen.value=true;selected.value=null}
const closeAssetModal=()=>{if(assetSaving.value)return;assetModalOpen.value=false;assetError.value=''}
const selectUploadFile=(event:Event)=>{const file=(event.target as HTMLInputElement).files?.[0]||null;assetForm.value.file=file;if(uploadPreview.value.startsWith('blob:'))URL.revokeObjectURL(uploadPreview.value);uploadPreview.value=file?URL.createObjectURL(file):'';if(file&&!assetForm.value.name.trim())assetForm.value.name=file.name.replace(/\.[^.]+$/,'')}
const saveAsset=async()=>{
  if(assetSaving.value)return
  const form=assetForm.value
  if(!form.name.trim()){assetError.value='请填写素材名称';return}
  if(!form.description.trim()||!form.promptHint.trim()){assetError.value='请填写基础描述和视觉锚点，Bible 快速创建会严格依据这两项元数据';return}
  if(!editingAssetId.value&&!form.file){assetError.value='请选择 JPEG、PNG 或 WebP 图片';return}
  assetSaving.value=true;assetError.value=''
  try{
    const saved=editingAssetId.value
      ? await studio.updateAssetMetadata(editingAssetId.value,{name:form.name,description:form.description,promptHint:form.promptHint,kind:form.kind,styleId:form.styleId||undefined})
      : await studio.createImageAsset({file:form.file!,kind:form.kind,name:form.name,description:form.description,promptHint:form.promptHint,styleId:form.styleId||undefined})
    assetModalOpen.value=false
    try{await refresh()}catch{studio.notify('素材已保存，但列表刷新失败；请稍后手动刷新。')}
    selected.value=studio.assets.find((asset)=>asset.id===saved.id)||null
  }catch(error){assetError.value=error instanceof Error?error.message:'素材保存失败，请稍后重试'}finally{assetSaving.value=false}
}
const openDeleteAsset=(asset:Asset)=>{if(!canEditAsset(asset))return;deleteTarget.value=asset;deleteConfirmName.value='';deleteError.value='';selected.value=null}
const closeDeleteAsset=()=>{if(deletePending.value)return;const target=deleteTarget.value;deleteTarget.value=null;deleteConfirmName.value='';deleteError.value='';if(target)selected.value=target}
const deleteConfirmed=computed(()=>Boolean(deleteTarget.value&&deleteConfirmName.value.trim()===deleteTarget.value.name.trim()))
const confirmDeleteAsset=async()=>{
  if(deletePending.value||!deleteTarget.value||!deleteConfirmed.value)return
  const target=deleteTarget.value;deletePending.value=true;deleteError.value=''
  try{
    await studio.deleteAsset(target.id);deleteTarget.value=null;deleteConfirmName.value='';selected.value=null
    try{await refresh()}catch{studio.notify('素材已删除，但列表刷新失败；请稍后手动刷新。')}
  }catch(error){deleteError.value=error instanceof Error?error.message:'素材删除失败，请稍后重试'}finally{deletePending.value=false}
}
</script>

<template>
  <div class="assets-page page-stack">
    <section class="page-heading split-heading">
      <div><span class="eyebrow">CONSISTENCY ASSET VAULT</span><h2>风格素材库</h2><p>为人物、场景和道具参考图维护名称、基础描述与视觉锚点；Bible 快速创建只读取这些已保存元数据，不进行图片像素识别。</p></div>
      <div class="asset-heading-actions"><div class="heading-stats"><div><strong>{{ studio.styles.length }}</strong><span>风格体系</span></div><i></i><div><strong>{{ studio.assets.length }}</strong><span>当前筛选结果</span></div></div><button class="button primary" @click="openCreateAsset"><Plus :size="15"/>新增图片素材</button></div>
    </section>

    <section class="style-picker">
      <button class="style-picker-all" :class="{active:!selectedStyle}" @click="selectedStyle=''">ALL<br><strong>全部风格</strong></button>
      <button v-for="item in studio.styles" :key="item.id" :class="{active:selectedStyle===item.id}" :style="{'--style-accent':item.accent,'--style-bg':item.gradient}" @click="selectedStyle=item.id"><i></i><span>{{ item.englishName }}</span><strong>{{ item.name }}</strong></button>
    </section>

    <section v-if="style" class="style-feature" :style="{background:style.gradient}">
      <div class="style-feature-pattern"></div><div><span class="eyebrow">{{ style.englishName }}</span><h3>{{ style.name }}</h3><p>{{ style.description }}</p><div class="tag-row"><span v-for="tag in style.tags" :key="tag">{{ tag }}</span></div></div>
      <div class="style-feature-counts"><div><Layers3 :size="18"/><strong>{{ style.assetCount ?? '—' }}</strong><span>素材总数</span></div></div>
    </section>

    <section class="asset-toolbar panel">
      <div class="asset-tabs"><button v-for="type in types" :key="type.value" :class="{active:selectedType===type.value}" @click="selectedType=type.value"><component :is="type.icon" :size="15"/>{{ type.label }}</button></div>
      <div class="toolbar-right"><label class="search-input"><Search :size="16"/><input v-model="search" placeholder="搜索名称、标签、描述、视觉锚点"><button v-if="search" @click="search=''" aria-label="清除"><X :size="14"/></button></label></div>
    </section>

    <div class="result-bar"><span>显示 <strong>{{ studio.assets.length }}</strong> 项素材</span><button>最新入库 <ChevronDown :size="14"/></button></div>
    <section v-if="loading" class="asset-grid"><div v-for="n in 12" :key="n" class="asset-card skeleton-card"></div></section>
    <section v-else-if="studio.assets.length" class="asset-grid">
      <article v-for="asset in studio.assets" :key="asset.id" class="asset-card" @click="selected=asset">
        <div class="asset-image"><AssetImage :src="asset.thumbnail" :alt="asset.name" :label="asset.name" :accent="assetAccent(asset)"/><span class="asset-type"><component :is="typeIcon(asset.type)" :size="12"/>{{ typeLabel(asset.type) }}</span><div class="asset-overlay"><button class="button primary small">查看参考</button></div></div>
        <div class="asset-copy"><span>{{ studio.styles.find(s=>s.id===asset.styleId)?.name }}</span><h4>{{ asset.name }}</h4><p>{{ asset.description }}</p><div><span v-for="tag in asset.tags.slice(1)" :key="tag">#{{ tag }}</span></div></div>
      </article>
    </section>
    <section v-else class="empty-state"><Search :size="28"/><h3>没有匹配素材</h3><p>尝试更换风格、素材类型或搜索关键词。</p><button class="button ghost" @click="selectedStyle='';selectedType='';search=''">清空筛选</button></section>

    <BaseModal :open="!!selected" title="素材参考详情" eyebrow="REFERENCE ASSET" width="880px" @close="selected=null">
      <div v-if="selected" class="asset-detail">
        <button type="button" class="asset-detail-image" title="点击查看完整图片" @click="fullPreview=selected"><AssetImage :src="selected.thumbnail" :alt="selected.name" :label="selected.name" :accent="assetAccent(selected)"/><span>REF · {{ selected.id.toUpperCase() }} · 点击完整预览</span></button>
        <div class="asset-detail-copy"><span class="eyebrow">{{ studio.styles.find(s=>s.id===selected?.styleId)?.name }} · {{ typeLabel(selected.type) }}</span><h2>{{ selected.name }}</h2><p>{{ selected.description || '尚未填写基础描述' }}</p>
          <div class="detail-block asset-prompt-hint"><label>视觉锚点 · Bible 草稿依据</label><p>{{ selected.promptHint || '尚未填写；请由素材所有者补充不可变化的外观、结构、材质和颜色。' }}</p></div>
          <div class="asset-language-note"><AlertTriangle :size="14"/><span>建议整段使用中文或整段使用英文，避免中英混杂；这是质量提醒，不会阻止保存。</span></div>
          <div v-if="selected.seed" class="detail-block"><label>一致性 Seed</label><div class="seed-box"><code>{{ selected.seed }}</code><button @click="copySeed(selected)"><Copy :size="14"/>复制</button></div></div>
          <div class="detail-block"><label>参考标签</label><div class="tag-row"><span v-for="tag in selected.tags" :key="tag">{{ tag }}</span></div></div>
          <div v-if="canEditAsset(selected)" class="asset-detail-actions">
            <button class="button ghost full" @click="openEditAsset(selected)"><Pencil :size="15"/>编辑素材信息</button>
            <button class="button danger-ghost full" @click="openDeleteAsset(selected)"><Trash2 :size="15"/>删除素材</button>
          </div>
          <div v-else class="asset-readonly"><AlertTriangle :size="14"/><span>全局素材或其他用户素材为只读；可用于 Bible 草稿，但不能修改其元数据。</span></div>
          <button class="button primary full" @click="fullPreview=selected"><Image :size="16"/>查看完整图片</button>
        </div>
      </div>
    </BaseModal>

    <BaseModal :open="assetModalOpen" :title="editingAssetId?'编辑图片素材':'新增图片素材'" eyebrow="ASSET METADATA" width="760px" @close="closeAssetModal">
      <div class="asset-editor">
        <div class="asset-editor-evidence"><Sparkles :size="17"/><span><strong>这些字段会成为 Bible 快速创建的依据</strong><small>系统根据名称、基础描述和视觉锚点生成设定草稿，不分析图片像素。请准确描述参考图中应保持不变的事实；语言一致性只做提醒，不做硬拦截。</small></span></div>
        <div v-if="!editingAssetId" class="asset-upload-box" :class="{filled:uploadPreview}" @click="uploadInput?.click()"><AssetImage v-if="uploadPreview" :src="uploadPreview" :label="assetForm.name||'待上传素材'"/><template v-else><Upload :size="24"/><strong>选择 JPEG / PNG / WebP</strong></template><input ref="uploadInput" type="file" accept="image/jpeg,image/png,image/webp" hidden @change="selectUploadFile"></div>
        <div class="form-grid two"><label class="field"><span>素材类别 *</span><select v-model="assetForm.kind" :disabled="assetSaving"><option v-if="editingAssetId&&assetForm.kind==='reference'" value="reference">通用参考</option><option v-for="kind in imageKinds" :key="kind.value" :value="kind.value">{{ kind.label }}</option></select><small>{{ editingAssetId?'可更正人物、场景或道具类别；若素材已用于 Bible、分镜或发行，需先解除引用。':'必须选择人物、场景或道具；Bible 草稿严格按同类选择。' }}</small></label><label class="field"><span>风格体系</span><select v-model="assetForm.styleId" :disabled="assetSaving"><option value="">不限定风格</option><option v-for="item in studio.styles" :key="item.id" :value="item.id">{{ item.name }}</option></select></label></div>
        <label class="field"><span>名称 *</span><input v-model="assetForm.name" :disabled="assetSaving" maxlength="200" placeholder="例如：沈砚雨夜定妆正面图"></label>
        <label class="field"><span>基础描述 *</span><textarea v-model="assetForm.description" :disabled="assetSaving" rows="3" maxlength="4000" placeholder="说明素材主体、用途和创作背景；会用于快速创建 Bible 的基础描述。"></textarea></label>
        <label class="field"><span>视觉锚点 *</span><textarea v-model="assetForm.promptHint" :disabled="assetSaving" rows="4" maxlength="8000" placeholder="只写可由此素材确定且跨镜头不可变化的外观、结构、材质、颜色和固定标记；不要写临时动作或运镜。"></textarea><small>Bible 草稿会合并所选素材的视觉锚点；建议一条锚点整段全中文或全英文，避免中英混杂。系统只提醒，不会因语言混写阻止保存。</small></label>
        <div v-if="assetError" class="asset-editor-error"><AlertTriangle :size="14"/><span>{{ assetError }}</span><button type="button" @click="assetError=''">关闭</button></div>
      </div>
      <template #footer><button class="button ghost" :disabled="assetSaving" @click="closeAssetModal">取消</button><button class="button primary" :disabled="assetSaving" @click="saveAsset"><Save :size="15"/>{{ assetSaving?'保存中…':editingAssetId?'保存素材说明':'上传并保存素材' }}</button></template>
    </BaseModal>
    <BaseModal :open="!!deleteTarget" title="删除素材" eyebrow="DELETE ASSET" width="520px" @close="closeDeleteAsset">
      <div v-if="deleteTarget" class="asset-delete-confirm" :aria-busy="deletePending">
        <div class="asset-delete-warning"><AlertTriangle :size="18"/><span><strong>此操作无法撤销</strong><small>将永久删除“{{ deleteTarget.name }}”及其上传文件。若素材已用于 Bible、分镜或发行记录，系统会阻止删除并保留原数据。</small></span></div>
        <label class="field"><span>输入素材名称以确认</span><input v-model="deleteConfirmName" :disabled="deletePending" autocomplete="off" :placeholder="deleteTarget.name" @keyup.enter="confirmDeleteAsset"></label>
        <div v-if="deleteError" class="asset-editor-error" role="alert"><AlertTriangle :size="14"/><span>{{ deleteError }}</span><button type="button" @click="deleteError=''">关闭</button></div>
      </div>
      <template #footer><button class="button ghost" :disabled="deletePending" @click="closeDeleteAsset">取消</button><button class="button delete-button" :disabled="deletePending||!deleteConfirmed" @click="confirmDeleteAsset"><Trash2 :size="15"/>{{ deletePending?'删除中…':'永久删除' }}</button></template>
    </BaseModal>
    <BaseModal :open="!!fullPreview" title="素材完整预览" eyebrow="FULL IMAGE PREVIEW" width="1080px" @close="fullPreview=null"><div v-if="fullPreview" class="asset-full-preview"><AssetImage :src="fullPreview.mediaUrl||fullPreview.thumbnail" :alt="fullPreview.name" :label="fullPreview.name"/><footer><strong>{{ fullPreview.name }}</strong><span>{{ typeLabel(fullPreview.type) }} · {{ fullPreview.description || '未填写基础描述' }}</span></footer></div></BaseModal>
  </div>
</template>

<style scoped>
.asset-heading-actions{display:flex;align-items:center;gap:18px}.asset-heading-actions .heading-stats{display:flex}.asset-detail-image{padding:0;border:0;cursor:zoom-in}.asset-detail-image img{object-fit:contain!important}.asset-prompt-hint{margin-bottom:8px}.asset-prompt-hint p{padding:10px;border:1px solid rgba(104,197,167,.18);border-radius:7px;background:rgba(104,197,167,.04);color:#90ada3;font-size:10px;line-height:1.65}.asset-language-note{display:flex;align-items:flex-start;gap:8px;margin:8px 0 12px;padding:9px 10px;border:1px solid rgba(218,154,70,.22);border-radius:7px;background:rgba(218,154,70,.045);color:#c59658}.asset-language-note svg{flex:0 0 auto;margin-top:1px}.asset-language-note span{font-size:9px;line-height:1.55;color:#b89462;font-weight:600}.asset-readonly{display:flex;gap:8px;margin:12px 0;padding:9px;border:1px solid rgba(218,154,70,.2);border-radius:7px;color:#b28a54}.asset-readonly span{font-size:9px;line-height:1.5}.asset-detail-actions{display:flex;gap:8px}.asset-detail-actions .button{flex:1}.button.danger-ghost{border-color:rgba(239,115,123,.35);color:#e4868d;background:rgba(239,115,123,.04)}.button.danger-ghost:hover{border-color:rgba(239,115,123,.65);background:rgba(239,115,123,.09)}.delete-button{border-color:#c2555f;background:#a94049;color:#fff}.delete-button:hover:not(:disabled){background:#bd4b55}.asset-editor{display:flex;flex-direction:column;gap:12px}.asset-editor-evidence{display:flex;gap:10px;padding:12px;border:1px solid rgba(104,197,167,.23);border-radius:8px;background:rgba(104,197,167,.05);color:#65c3a5}.asset-editor-evidence span{display:flex;flex-direction:column;gap:4px}.asset-editor-evidence strong{font-size:11px;color:#a9d9ca}.asset-editor-evidence small{font-size:9px;line-height:1.55;color:#71958a}.asset-upload-box{height:190px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:7px;overflow:hidden;border:1px dashed #38414a;border-radius:8px;background:#0b0e12;color:#707880;cursor:pointer}.asset-upload-box.filled{border-style:solid}.asset-upload-box img{width:100%;height:100%;object-fit:contain}.asset-upload-box strong{font-size:11px;color:#b8bec4}.asset-editor-error{display:flex;align-items:flex-start;gap:8px;padding:9px 10px;border:1px solid rgba(239,115,123,.28);border-radius:7px;background:rgba(239,115,123,.06);color:#e4868d}.asset-editor-error span{flex:1;font-size:10px;line-height:1.5}.asset-editor-error button{border:0;background:transparent;color:inherit;cursor:pointer}.asset-delete-confirm{display:flex;flex-direction:column;gap:16px}.asset-delete-warning{display:flex;gap:10px;padding:13px;border:1px solid rgba(239,115,123,.28);border-radius:8px;background:rgba(239,115,123,.06);color:#e4868d}.asset-delete-warning svg{flex:0 0 auto}.asset-delete-warning span{display:flex;flex-direction:column;gap:5px}.asset-delete-warning strong{font-size:11px}.asset-delete-warning small{color:#a78185;font-size:9px;line-height:1.6}.asset-full-preview{display:flex;flex-direction:column;gap:10px}.asset-full-preview>img{width:100%;height:min(76vh,760px);object-fit:contain;border-radius:8px;background:#07090c}.asset-full-preview footer{display:flex;justify-content:space-between;gap:12px}.asset-full-preview strong{font-size:12px}.asset-full-preview span{font-size:9px;color:#737b82}@media(max-width:780px){.asset-heading-actions{align-items:stretch;flex-direction:column}.asset-editor .form-grid{grid-template-columns:1fr}.asset-detail{grid-template-columns:1fr}.asset-detail-image{height:330px}.asset-detail-actions{flex-direction:column}}
</style>
