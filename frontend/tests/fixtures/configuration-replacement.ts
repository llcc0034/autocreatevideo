import { createApp, h, ref } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import Component from '../../src/components/ExecutionConfigurationReplacement.vue'
import { executionPreparationApi as api } from '../../src/services/executionPreparationApi'
import { api as transport } from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'
transport.defaults.adapter = async () => { throw Error('隔离夹具禁止业务网络请求') }
const sha = 'a'.repeat(64), status = ref('尚未确认'), busy = ref(false)
api.configurationSuccessors = async () => ({ preparation_id: 'isolated-prep', revision: 3, allowed: true, reason: '只切换策略后继；原输入、素材、种子与采样参数保持不变。', replacement_id: null,
  versions: [{ id: 'isolated-next', version: 6, name: '主体参考主图', profile_name: '通用主图策略 · 保留来源语言', profile_version: 4, definition_sha256: sha }] })
api.previewConfigurationReplacement = async () => ({ preparation_id: 'isolated-prep', revision: 3, version_id: 'isolated-next', preview_sha256: sha,
  changes: [{ path: '/rules/2/language_policy', before: 'inherit', after: 'preserve_source' }] })
api.replaceConfiguration = async () => ({ preparation_id: 'isolated-prep', replacement_id: 'isolated-replacement', version_id: 'isolated-next' })
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/workflows', name: 'workflows', component: { render: () => null } }, { path: '/:pathMatch(.*)*', component: { render: () => null } }] })
createApp({ setup: () => () => h('main', { style: 'max-width:980px;margin:20px auto;padding:16px;min-width:0' }, [
  h('h1', '隔离验收：提示词策略后继'), h('p', '此页只使用夹具，不访问正式API、不调用模型，也不修改当前Run。'),
  h(Component, { preparationId: 'isolated-prep', revision: 3, disabled: busy.value,
    onBusy: (value: boolean) => { busy.value = value }, onApplied: () => { status.value = '隔离确认已完成，未创建正式任务' } }),
  h('p', { role: 'status' }, status.value),
]) }).use(router).mount('#app')
