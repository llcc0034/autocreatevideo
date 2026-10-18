import { createApp, h, ref } from 'vue'
import BibleContentReview from '../../src/components/BibleContentReview.vue'
import { bibleCreativeFields, type BibleReviewContent } from '../../src/services/productionManualBible'
import { api } from '../../src/services/api'
import '../../src/styles.css'

api.interceptors.request.use(() => { throw Error('隔离样例禁止真实API请求') })
const hash = 'a'.repeat(64), revisionId = '10000000-0000-0000-0000-000000000001'
const content: BibleReviewContent = {
  project_bible_sha256: hash, project_bible: { characters: [{
    ...Object.fromEntries(Object.keys(bibleCreativeFields.characters).map(key => [key, ''])),
    id: revisionId, name: '独立登记的旁白', is_locked: false, voice_profile: '温和、缓慢的音色',
    metadata: { note: '明确未建立主图；不从旧实体字段补身份' }, appearance_settings: [],
  }], scenes: [], props: [] },
  candidates: [{ id: 'isolated-character', entity_type: 'character', name: '海遥 / Haruka', description: '',
    identity_anchor: '', action: 'create', conflict_code: null, existing: null }],
  settings: [{ review_key: 'isolated-primary', kind: 'declaration', entity_type: 'character', entity_name: '海遥 / Haruka',
    entity_key: 'person_a', appearance_role: 'primary', variant_key: null, name: '海遥 / Haruka', reference_name: '海遥·主图',
    identity_anchor: '', appearance_anchor: '白色外套；保留原语言。',
    source_facts: { script_revision_id: revisionId, already_materialized: false, primary_reference_name: '海遥·主图',
      description: null, identity_anchor: '', appearance_anchor: '白色外套；保留原语言。', semantic_path: 'value.entities[0]',
      structure_source: { schema_version: 1, kind: 'confirmed_script_structure',
        script: { kind: 'script_revision', id: revisionId, sha256: hash }, document_sha256: hash,
        output: { id: '10000000-0000-0000-0000-000000000002', version: 1, sha256: hash }, configuration_sha256: hash } },
    setting: null, primary_setting: null }],
}
createApp({ setup() {
  const candidates = ref<string[]>([]), settings = ref<string[]>([]), project = ref(false)
  return () => h('main', { style: 'max-width:900px;margin:16px auto;padding:16px;min-width:0' }, [
    h('h1', 'Bible 已保存字段 · 隔离样例'), h('p', '真实审核组件；仅内存内容，不调用业务API、不审核真实记录。'),
    h(BibleContentReview, { content, candidates: candidates.value, settings: settings.value, project: project.value,
      'onUpdate:candidates': (value: string[]) => { candidates.value = value },
      'onUpdate:settings': (value: string[]) => { settings.value = value },
      'onUpdate:project': (value: boolean) => { project.value = value } }),
    h('p', { role: 'status' }, `样例勾选：实体 ${candidates.value.length}，设定 ${settings.value.length}，项目 ${project.value ? '已核对' : '未核对'}`),
  ])
} }).mount('#app')
