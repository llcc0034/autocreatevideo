import type { JsonObject } from './platformTypes'
import { getPath, type RegistryEditorDependencies, type RegistryFieldSchema } from './registryResourceSchema'

// A presentation of two stored declarations, never a second effective-config resolver.
export function deploymentParameterSource(field: RegistryFieldSchema, payload: JsonObject, dependencies: RegistryEditorDependencies) {
  if (!field.path.startsWith('runtime_config.') || field.advanced) return null
  const profile = dependencies.modelProfiles.find(row => row.id === payload.profile_version)
  const provider = dependencies.providers.find(row => row.id === profile?.provider)
  const path = field.path.slice('runtime_config.'.length)
  const root = path.split('.')[0]!
  const ownValue = getPath(payload, field.path)
  const group = getPath(payload, `runtime_config.${root}`)
  const state = ownValue !== undefined ? '本部署已填写' : path.includes('.') && group !== undefined
    ? '本部署已填写此参数组，本项未填写' : '本部署未填写'
  const inherited = provider ? getPath({ config: provider.driverConfig }, `config.${path}`) : undefined
  // Only typed scalar declarations are echoed. Do not dump connection maps, endpoints or extensions.
  const display = (value: unknown) => value === undefined ? '未声明' : value === null ? '空值（null）'
    : value === '' ? '空文本' : typeof value === 'boolean' ? `${value ? '是' : '否'}（${value}）`
      : typeof value === 'number' ? String(value)
        : field.kind === 'select' ? field.options?.find(row => row.value === value)?.label || '已声明，当前目录未识别'
          : '已声明（在原维护入口查看）'
  return {
    state,
    own: display(ownValue),
    connection: provider ? `${provider.name} · ${provider.key}` : profile ? '连接目录未找到此引用' : '请先选择确切模型版本',
    declared: provider ? display(inherited) : '无法核对',
    grouped: path.includes('.'),
  }
}
