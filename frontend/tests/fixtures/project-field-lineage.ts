import { createApp, h, reactive, ref } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import ProjectFieldLineage from '../../src/components/ProjectFieldLineage.vue'
import { fieldLineageFixture } from './project-field-lineage-data'
import { api } from '../../src/services/api'
import { platformApi } from '../../src/services/platformApi'
import '../../src/styles.css'
import '../../src/series.css'
import '../../src/platform.css'
import '../../src/responsive.css'
api.interceptors.request.use(() => { throw Error('隔离页面禁止真实请求') })
const f = fieldLineageFixture(), draft = reactive(structuredClone(f.project)), failed = ref(false), run = ref(f.run)
platformApi.getArtifact = async () => { if (failed.value) throw Error('隔离模拟：来源暂不可用'); return f.artifact }
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:all(.*)*', component: { render: () => null } }] })
await router.push('/')
createApp({ setup: () => () => h('main', { style: 'padding:16px;max-width:1320px;margin:auto' }, [
  h('h1', '视觉与术语 · 字段流向'), h('p', '隔离样例，所有 HTTP 禁止；不写数据库、不调用模型。'),
  h('div', { class: 'actions' }, [h('button', { class: 'button ghost', onClick: () => { failed.value = !failed.value } }, failed.value ? '恢复读取' : '模拟读取失败'),
    h('button', { class: 'button ghost', onClick: () => { run.value = { ...f.run, id: 'manual', planSource: 'manual', stages: [] } } }, '切换无来源人工版本')]),
  h(ProjectFieldLineage, { run: run.value, project: draft, savedProject: f.project }),
  h('label', { class: 'field' }, ['色彩脚本（仅本地编辑）', h('textarea', { value: draft.bible.colorScript, onInput: (event: Event) => { draft.bible.colorScript = (event.target as HTMLTextAreaElement).value } })]),
]) }).use(router).mount('#app')
