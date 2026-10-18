// Real management components with in-memory fixtures; no live API or database access.
import { createApp, h } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import FieldMaintenanceSettings from '../../src/components/FieldMaintenanceSettings.vue'
import SettingsPrimaryNav from '../../src/components/SettingsPrimaryNav.vue'
import { api } from '../../src/services/api'
import { platformApi } from '../../src/services/platformApi'
import { fieldDefinitionVersions } from '../../src/services/fieldDefinitionVersions'
import '../../src/styles.css'
import '../../src/platform.css'

api.defaults.adapter = async config => { throw Error('隔离页面禁止真实 API：' + config.url) }
const identities = ['camera.movement', 'subject.reference', 'scene.mood'].map((key, index) => ({ id: `fixture-${index}`, key, isActive: true, displayOrder: index, createdAt: '', updatedAt: '' }))
platformApi.fieldDefinitionPage = async (_page, search, includeInactive) => {
  const results = identities.filter(row => row.key.includes(search) && (includeInactive || row.isActive))
  return { count: results.length, results, hasNext:false, hasPrevious:false }
}
platformApi.getFieldDefinition = async id => { const row = identities.find(item => item.id === id); if (!row) throw Error('隔离记录不存在'); return row }
platformApi.createFieldDefinition = async payload => { const row = { ...identities[0]!, id: 'new-fixture', key: String(payload.key) }; identities.push(row); return row }
platformApi.updateFieldDefinition = async (id, payload) => { const row = await platformApi.getFieldDefinition(id); row.isActive = Boolean(payload.is_active); return row }
fieldDefinitionVersions.options = async () => ({ value_types: [{ value: 'text', label: '文字' }, { value: 'object', label: '对象' }], statuses: [{ value: 'published', label: '已启用' }, { value: 'draft', label: '草稿' }] })
const version = { id: 'fixture-version', field_definition: identities[0]!.id, field_key: identities[0]!.key, version: 1, status: 'published' as const, label: '镜头变化', category: 'camera', value_type: 'text' as const, unit: '', description: '描述运动方向和变化，不重复参考图外观。', impact: '用于视频镜头描述', value_options: [], preview_example: '镜头缓慢推近' }
fieldDefinitionVersions.list = async id => id === version.field_definition ? [version] : []
fieldDefinitionVersions.get = async () => version
fieldDefinitionVersions.create = fieldDefinitionVersions.update = fieldDefinitionVersions.transition = async () => { throw Error('隔离保存错误示例：此夹具不发布定义') }
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', name: 'settings', component: { render: () => null } }] })
await router.push('/settings?section=fields')
createApp({ setup: () => () => h('main', { style: { maxWidth: '1180px', margin: '16px auto', padding: '12px' } }, [
  h('p', { style: { marginBottom: '16px' } }, '隔离验证：真实组件 + 模拟目录。不会修改业务配置或调用模型。'),
  h(SettingsPrimaryNav), h(FieldMaintenanceSettings),
]) }).use(router).mount('#app')
