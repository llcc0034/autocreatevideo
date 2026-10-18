// Actual standalone creation form; all reads and submits stay in this fixture.
import { createApp, h, ref } from 'vue'
import AssetExecutionCreateForm from '../../src/components/AssetExecutionCreateForm.vue'
import { assetAppearanceApi } from '../../src/services/assetAppearanceTarget'
import { executionRegistryApi } from '../../src/services/executionRegistryApi'
import { repository } from '../../src/services/repository'
import { api } from '../../src/services/api'
import { assetReferenceApi } from '../../src/services/assetReferenceApi'
import { unusedLanguageForm } from './executionLanguage'
import '../../src/styles.css'
import '../../src/platform.css'

api.defaults.adapter = async () => { throw Error('隔离夹具禁止业务网络请求。') }
const option = { versionId: '10000000-0000-0000-0000-000000000001', key: 'fixture', name: '隔离图片实例', version: 1,
  operation: 'image.image_to_image', mode: '', promptMode: 'manual' as const, definitionSha256: 'a'.repeat(64) }
const picture = {id:'60000000-0000-0000-0000-000000000001',name:'隔离主图原素材',type:'image',mimeType:'image/svg+xml',isActive:true,checksum:'b'.repeat(64),
  mediaUrl:'data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200"><rect width="320" height="200" fill="#415367"/><text x="40" y="105" fill="white" font-size="22">ISOLATED PRIMARY</text></svg>')}
const series = {id:'70000000-0000-0000-0000-000000000001',name:'隔离主图系列',role:'primary' as const,owner_id:1,primary_series_id:null,bible_variant_id:null,version_count:1,variant_count:0}
const version = {id:'80000000-0000-0000-0000-000000000001',series_id:series.id,version:1,source:'existing' as const,generated_from_id:null,setting_revision_id:null,
  asset:{id:picture.id,name:picture.name,kind:'image',mime_type:picture.mimeType,is_active:true}}
const toPage = <T>(results:T[]) => ({count:results.length,next:null,previous:null,results})
assetReferenceApi.roles=async()=>[{value:'primary',display_name:'主图'},{value:'variant',display_name:'变体'}]
assetReferenceApi.listSeries=async query=>toPage(query.primary_series_id || query.role==='variant'?[]:[series])
assetReferenceApi.getSeries=async()=>series
assetReferenceApi.listVersions=async()=>toPage([version])
assetReferenceApi.getVersion=async()=>version
repository.getAsset=async()=>picture as any
repository.getAssetPage=async()=>toPage([picture]) as any
const revision = ref(1), failed = ref(false), calls = ref<unknown[]>([]), finished = ref(false)
const missingCatalog = ref(false)
repository.getAssetGenerationOptions = async () => ({ count: 1, next: null, previous: null, results: [option] })
executionRegistryApi.contract = async () => ({ version_id: option.versionId, revision: 1, frozen: true, sha256: option.definitionSha256,
  appearance_inputs: missingCatalog.value ? undefined : { primary_inputs: [{input_key:'reference_1',target_key:'primary_image'}], difference_sources: [{ kind: 'manual_channel', key: 'positive_prompt' }] },
  context_fields: [], input_forms: {}, subject_categories: [{ value: 'character', label: '人物' }], language_form: unusedLanguageForm(),
  definition: { version_id: option.versionId,
    configuration: { deployment_id: 'fixture', operation: option.operation, mode: '', workflow_version_id: null, prompt_mode: 'manual', prompt_profile_version_id: null,
      inputs: [{key:'reference_1',kind:'image',position:0,required:true,min_items:1,max_items:1,
        semantic_contract:{schema_version:1,allowed_purposes:[],allowed_subject_kinds:['external'],allowed_scope_kinds:['request'],paired_with_input_key:null,allow_repeated_asset:true},
        targets:[{target_kind:'workflow_slot',target_key:'primary_image',is_primary:true}]}], parameters: [], auxiliaries: [] },
    interface: { output: { kind: 'image' }, input_purposes: {}, parameters: {}, prompt: { mode: 'manual', channels: { positive_prompt: { rule: { type: 'string', required: true } } } } },
  } }) as any
assetAppearanceApi.options = async (_page, search, role) => {
  if (failed.value) throw Error('隔离模拟：目录读取失败')
  const results = search === '不存在' ? [] : [{ setting_revision_id: `20000000-0000-0000-0000-00000000000${revision.value}`,
    appearance_id: '30000000-0000-0000-0000-000000000001', project_id: '40000000-0000-0000-0000-000000000001',
    project_title: '隔离样例项目', entity_name: '送信人', name: role==='variant'?'蓝色外套变体':'城市日间主图', reference_name: 'Courier.'+role, revision: revision.value }]
  return { count: results.length, next: null, previous: null, results }
}
repository.createAssetGenerationJob = async input => {
  calls.value.push(JSON.parse(JSON.stringify(input)))
  if (calls.value.length === 1) throw Error('隔离模拟：创建回执丢失，请重试同一请求')
  return { executionInstanceVersionId: input.executionInstanceVersionId, jobType: input.jobType } as any
}
createApp({ setup: () => () => h('main', { style: { maxWidth: '900px', margin: '20px auto', padding: '12px' } }, [
  h('h1', '主图与变体目标创建 · 隔离验证'), h('p', '使用原创建表单，所有目录和提交回执都是隔离夹具；不创建业务任务或调用模型。'),
  h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px' } }, [
    h('button', { type: 'button', onClick: () => { revision.value = 2 } }, '模拟目录新增修订'),
    h('button', { type: 'button', onClick: () => { failed.value = !failed.value } }, failed.value ? '恢复目录' : '模拟目录失败'),
    h('button', { type: 'button', onClick: () => { missingCatalog.value = !missingCatalog.value } }, missingCatalog.value ? '恢复外观输入目录' : '模拟外观输入目录缺失'),
  ]),
  h(AssetExecutionCreateForm, { mediaType: 'image', metadata: { name: '隔离主图', description: '', styleId: '', imageCategory: 'reference' },
    onCreated: () => { finished.value = true } }),
  h('p', { role: 'status' }, finished.value ? '隔离回执已确认；不是实际生成成功。' : `隔离提交次数：${calls.value.length}`),
  h('details', [h('summary', '隔离请求记录（无网络提交）'), h('pre', { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } }, JSON.stringify(calls.value, null, 2))]),
]) }).mount('#app')
