import { createApp, h, ref } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import ProductionRecipeEditor from '../../src/components/ProductionRecipeEditor.vue'
import ProductionRecipeFlow from '../../src/components/ProductionRecipeFlow.vue'
import { executionRegistryApi } from '../../src/services/executionRegistryApi'
import { platformApi } from '../../src/services/platformApi'
import { api } from '../../src/services/api'
import { recipeHierarchyContract, recipeHierarchyDefinition, recipeHierarchyOptions } from './recipe-hierarchy-data'
import '../../src/styles.css'
import '../../src/platform.css'

// Original components, local definition events only; NOT a server persistence fixture.
const copy = (value: any) => JSON.parse(JSON.stringify(value)), blocked = ref(0), edits = ref(0), reads = ref(0)
const mode = ref('author'), fail = ref(false), key = ref(0), definition = ref<any>(recipeHierarchyDefinition())
function deny(): never { blocked.value++; throw Error('隔离夹具禁止真实网络或配置写入') }
api.interceptors.request.use(() => deny()); api.defaults.adapter = async () => deny()
window.fetch = async () => deny(); XMLHttpRequest.prototype.send = () => deny(); navigator.sendBeacon = () => deny()
const detail = () => ({ id: 'fixture-structure', instance_id: 'fixture-family', instance_name: '隔离结构化文本能力', version: 1, revision: 1, status: 'published', configuration: recipeHierarchyContract().definition.configuration })
executionRegistryApi.versions = async () => { reads.value++; if (fail.value) throw Error('隔离目录读取失败'); return { count: 1, next: null, previous: null, results: [detail()] } as any }
executionRegistryApi.contract = async id => { reads.value++; if (id !== 'fixture-structure') return deny(); if (fail.value) throw Error('隔离合同读取失败'); return copy(recipeHierarchyContract()) }
executionRegistryApi.version = async id => { reads.value++; if (id !== 'fixture-structure') return deny(); return copy(detail()) }
platformApi.getRecipeSourceOptions = async () => { reads.value++; return [] }
api.get = async (url: string, request: any) => {
  if (url !== '/registry-relations/' || request.params.kind !== 'execution' || request.params.ids !== 'fixture-structure') return deny()
  reads.value++
  return { data: { count: 1, next: null, previous: null, generated_at: '2026-10-09T00:00:00Z', context_label: '隔离关系', results: [{ id: 'fixture-structure', kind: 'execution', label: '隔离结构化文本能力 v1', status: 'published', config_url: '/settings?section=executions&execution_version=fixture-structure', overview_url: '/settings/acceptance-smoke?view=relations&kind=execution&ids=fixture-structure', acceptance_url: '', capabilities: [], notices: [], groups: [] }] } } as any
}
createApp({ setup() { return () => h('main', { style: 'max-width:1100px;margin:auto;padding:16px;min-width:0' }, [
  h('h1', { style: 'font:600 var(--config-title-size) var(--font);margin:0 0 16px' }, '配方层级作者 · 隔离验证'),
  h('p', '仅验证组件交互与本地草稿，不保存配置、不调用模型；重新载入即恢复夹具。'),
  h('div', { style: 'display:flex;flex-wrap:wrap;gap:8px;margin:16px 0' }, [
    h('button', { class: 'button', onClick: () => { mode.value = mode.value === 'author' ? 'view' : 'author' } }, mode.value === 'author' ? '查看当前草稿' : '返回作者'),
    h('button', { class: 'button', onClick: () => { definition.value = { schema_version: 3, interaction_mode: '', stages: [] }; key.value++; mode.value = 'author' } }, '开始空白配方'),
    h('label', [h('input', { type: 'checkbox', checked: fail.value, onChange: (event: Event) => { fail.value = (event.target as HTMLInputElement).checked } }), '隔离目录失败']),
  ]),
  h('p', { role: 'status', 'data-fixture-counters': '' }, `本地修改 ${edits.value} · 隔离读取 ${reads.value} · 禁止网络/写入 ${blocked.value}`),
  mode.value === 'author' ? h(ProductionRecipeEditor, { key: key.value, modelValue: definition.value, options: recipeHierarchyOptions as any, 'onUpdate:modelValue': value => { definition.value = copy(value); edits.value++ } })
    : h(ProductionRecipeFlow, { recipe: { definition: definition.value, definitionSha256: '隔离未发布草稿' } as any, recipeEditorOptions: recipeHierarchyOptions as any, showRaw: true }),
]) } }).use(createRouter({ history: createMemoryHistory(), routes: [] })).mount('#app')
