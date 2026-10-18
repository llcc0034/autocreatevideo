<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { Asset } from '@/types'
import type { JsonObject } from '@/services/platformTypes'
import { jsonObject } from '@/services/structuredInputForm'
import { repository } from '@/services/repository'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import ExecutionInputAssetPicker from './ExecutionInputAssetPicker.vue'
import AssetMediaPreview from './AssetMediaPreview.vue'

const props = defineProps<{ modelValue: unknown; schemas: JsonObject[]; label: string; disabled?: boolean; readonly?: boolean; selectable?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [unknown] }>()
const id = computed(() => jsonObject(props.modelValue).asset_id)
const malformed = computed(() => props.modelValue !== undefined && (props.modelValue===null || typeof props.modelValue!=='object' || Array.isArray(props.modelValue)
  || Object.keys(jsonObject(props.modelValue)).some(key=>key!=='asset_id')
  || Object.prototype.hasOwnProperty.call(jsonObject(props.modelValue),'asset_id') && (typeof id.value!=='string' || !id.value)))
const kinds = computed(() => ['image','video','audio'].filter(kind => props.schemas.some(schema=>Array.isArray(schema['x-media-types']))
  && props.schemas.every(schema=>!Array.isArray(schema['x-media-types']) || schema['x-media-types'].includes(kind))) as ('image'|'video'|'audio')[])
const kind = ref<'image'|'video'|'audio'|''>('')
const chosenKind = computed(() => kinds.value.length===1 ? kinds.value[0]! : kinds.value.includes(kind.value as 'image') ? kind.value : '')
const usage = computed(() => props.schemas.find(schema=>schema['x-media-usage'])?.['x-media-usage'])
const asset = ref<Asset|null>(null), busy = ref(false), error = ref('')
let selectionContext: string | null = null
const currentContext = () => JSON.stringify([props.modelValue, props.schemas, props.readonly, props.selectable])
function selectionBusy(value: boolean) { selectionContext = value ? currentContext() : null }
let serial = 0
watch(id,()=>{ serial++; asset.value=null; busy.value=false; error.value='' })
onBeforeUnmount(()=>{serial++})
function compatible(value: Asset) { return value.isActive!==false && kinds.value.some(type=>value.mimeType?.startsWith(`${type}/`)) }
async function load() {
  const request = ++serial; asset.value=null; error.value=''
  if (typeof id.value!=='string' || !id.value) { error.value='尚未提供有效素材引用'; return }
  busy.value=true
  try {
    const value=await repository.getAsset(id.value)
    if(request!==serial)return
    if(value.id!==id.value || !compatible(value))throw new Error('素材已失效或类型不符合此字段；原引用保留，请重新选择')
    asset.value=value
  } catch(cause) {if(request===serial)error.value=apiErrorSummary(cause,'素材读取失败')}
  finally {if(request===serial)busy.value=false}
}
function select(value: Asset) {
  if(props.disabled || props.readonly || !props.selectable || !compatible(value))return
  if (selectionContext !== null && selectionContext !== currentContext()) {
    error.value='读取期间此字段已改变，未覆盖当前选择；请重新选择。'; return
  }
  emit('update:modelValue',{asset_id:value.id})
}
</script>
<template>
  <div class="structure-media">
    <p>{{ label }} · {{ kinds.join(' / ') || '未配置媒体类型' }}<template v-if="usage"> · {{ usage }}</template></p>
    <div v-if="malformed" role="alert"><p>素材引用格式不正确，原始内容保留：</p><pre>{{ JSON.stringify(modelValue,null,2) }}</pre></div>
    <p v-else-if="typeof id!=='string' || !id">尚未提供素材引用</p>
    <template v-else>
      <button type="button" class="button ghost small" :disabled="busy" @click="load">{{ busy?'正在读取素材…':'查看引用素材' }}</button>
      <details><summary>确切素材引用</summary><p>{{ id }}</p></details>
    </template>
    <p v-if="error" role="alert">{{ error }}</p>
    <div v-if="asset"><strong>{{ asset.name }}</strong><p>{{ asset.description }}</p>
      <div v-if="asset.mediaUrl" class="media-preview"><AssetMediaPreview :asset="asset" @source-error="error='素材文件不可读取，原引用保留；可重新读取此素材'"/></div>
      <p v-else>此素材没有可播放地址；未用其他素材替代。</p>
    </div>
    <template v-if="!readonly && selectable">
      <label v-if="kinds.length>1">选择媒体类型<select v-model="kind" :disabled="disabled"><option value="">请选择</option><option v-for="item in kinds" :key="item" :value="item">{{ item }}</option></select></label>
      <ExecutionInputAssetPicker v-if="chosenKind" :key="chosenKind" :kind="chosenKind" :purpose="typeof usage==='string'?usage:null" :disabled="disabled" :allow-upload="false" @busy="selectionBusy" @selected="select"/>
    </template>
  </div>
</template>
<style scoped>
.structure-media{display:grid;gap:8px;min-width:0}.structure-media :is(p,strong){margin:0;overflow-wrap:anywhere}.structure-media :is(img,video,audio){display:block;max-width:100%;max-height:260px}.structure-media label{display:grid;gap:6px}.structure-media select{min-height:36px}.structure-media [role=alert]{color:var(--warning,#e9aa52)}
.media-preview{max-width:100%;height:240px;min-width:0}
.structure-media pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:260px;overflow:auto}
</style>
