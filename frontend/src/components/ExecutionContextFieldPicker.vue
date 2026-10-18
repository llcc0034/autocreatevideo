<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { repository } from '@/services/repository'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import type { SeriesProject } from '@/types'
import type { ScriptSource, ScriptRevision } from '@/services/platformTypes'
import type { ExecutionRequestDraft } from '@/services/executionRequestForm'

const props = defineProps<{ disabled?: boolean; description?: string; projectId?: string }>()
const model = defineModel<NonNullable<ExecutionRequestDraft['contexts'][string]>>({ required: true })
const emit = defineEmits<{ busy: [value: boolean] }>()
const projects = ref<SeriesProject[]>([]), sources = ref<ScriptSource[]>([]), revisions = ref<ScriptRevision[]>([])
const sourceId = ref(''), busy = ref(false), error = ref('')
let sequence=0
async function load(task:()=>Promise<void>) {
  if (props.disabled || busy.value) return
  const token=++sequence; busy.value=true; emit('busy',true); error.value=''
  try { await task() } catch(e) { if(token===sequence) error.value=apiErrorSummary(e) }
  finally { if(token===sequence) { busy.value=false; emit('busy',false) } }
}
async function loadProjects() { await load(async()=>{const token=sequence; const rows=await repository.getProjects({ownedOnly:true}); if(token===sequence) projects.value=rows}) }
async function projectChanged() {
  sourceId.value=''; sources.value=[]; revisions.value=[]; model.value={...model.value,revisionId:''}
  if(!model.value.projectId) return
  await load(async()=>{const token=sequence; const rows=await platformApi.listScriptSources(model.value.projectId); if(token===sequence) sources.value=rows})
}
async function sourceChanged() {
  revisions.value=[]; model.value={...model.value,revisionId:''}
  if(!sourceId.value) return
  await load(async()=>{const token=sequence; const rows=await platformApi.listConfirmedScriptRevisions(sourceId.value); if(token===sequence) revisions.value=rows.filter(row=>row.status==='confirmed' && row.confirmedAt)})
}
onMounted(async () => {
  if (props.projectId) { model.value = { ...model.value, projectId: props.projectId, revisionId: '' }; await projectChanged() }
  else await loadProjects()
})
onBeforeUnmount(()=>{sequence++; emit('busy',false)})
</script>

<template>
  <fieldset :disabled="disabled || busy" class="context-picker">
    <p>{{ description || '引用已确认剧本全文；只提交精确修订标识，不复制正文、不自动选择最新版本。' }}</p>
    <p v-if="projectId">来源限定为本轮项目；请选择明确的已确认修订。</p>
    <template v-else>
      <label class="field">所属项目<select v-model="model.projectId" @change="projectChanged"><option value="">请选择项目</option><option v-for="row in projects" :key="row.id" :value="row.id">{{ row.title }}</option></select></label>
      <p>仅列出当前账号拥有的项目；管理员可管理的其他项目不作为本次执行来源。</p>
      <p v-if="!projects.length && !busy && !error">当前账号尚无可用项目，请先创建项目并确认剧本。</p>
    </template>
    <label class="field">剧本来源<select v-model="sourceId" @change="sourceChanged"><option value="">请选择剧本来源</option><option v-for="row in sources" :key="row.id" :value="row.id">{{ row.originalName || row.sourceFormat }} · {{ row.id.slice(0,8) }}</option></select></label>
    <label class="field">已确认修订<select v-model="model.revisionId"><option value="">请选择精确修订</option><option v-for="row in revisions" :key="row.id" :value="row.id">修订 {{ row.revision }} · {{ row.confirmedAt }} · {{ row.contentSizeBytes }} B</option></select></label>
    <p v-if="sourceId && !revisions.length && !busy && !error">此来源尚无可选的已确认修订，请先到项目剧本管理中确认。</p>
    <p v-if="busy" role="status">正在读取来源…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <button v-if="error" type="button" @click="sourceId ? sourceChanged() : model.projectId ? projectChanged() : loadProjects()">重新读取</button>
  </fieldset>
</template>

<style scoped>
.context-picker{display:grid;gap:10px;border:1px solid var(--border);border-radius:8px;padding:12px}.context-picker label{display:grid;gap:6px}.context-picker select{width:100%}.context-picker p{margin:0;font-size:12px}
</style>
