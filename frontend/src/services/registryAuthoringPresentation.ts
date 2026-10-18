import type { RegistryResource, RegistryFieldSchema } from './registryResourceSchema'

// Presentation only: schema fields, defaults, validation and writers stay in their original owners.
export const isModelResource = (resource: RegistryResource) => ['model', 'deployment', 'provider', 'artifact'].includes(resource)
export const registryAuthorSections = (resource: RegistryResource) => {
  const titles = resource === 'model'
    ? ['模型身份与连接', '能力与输入输出', '运行约束', '实现详情与复核']
    : resource === 'deployment' ? ['部署身份', '模型与运行位置', '部署自有覆盖', '实现详情与复核']
    : resource === 'provider' ? ['连接身份', '驱动与服务能力', '连接参数', '实现详情与复核']
    : ['文件声明身份', '所属模型版本', '文件定位与核验', '实现详情与复核']
  const descriptions = resource === 'model'
    ? ['选择已有服务连接；此处定义模型能力，不填写工作流节点。', '声明接口接受和产生的内容；业务值在任务中提供。', '只配置模型允许的运行边界，不代表已经通过实际验收。', '完整合同与专有扩展按需核对；保存草稿不会发布或调用模型。']
    : resource === 'deployment' ? ['部署是可修改的运行资源，不是执行实例版本。', '引用精确模型版本；Provider由模型推导，主机按实际运行位置选择。', '只填写本部署需要覆盖的参数；空项的继承含义沿原合同。', '核对同一运行配置；保存不发起探测或Smoke。']
    : resource === 'provider' ? ['可复用服务连接；模型和部署引用它，不复制连接定义。', '选择平台已支持的驱动与能力，不创建新的执行协议。', '维护连接自有参数，凭据仅引用受管记录。', '高级扩展仍属于同一连接对象；保存不调用远端。']
    : ['维护文件声明，不上传或移动真实模型文件。', '只能绑定可维护的草稿模型版本，不自动使用最新版。', '填写实际文件位置及可选摘要；声明不是已核验事实。', '特殊库存映射按需配置；保存不执行主机探测。']
  return ['identity', 'dependencies', 'configuration', 'implementation'].map((key,index)=>({key,title:titles[index]!,description:descriptions[index]!}))
}
export function registryAuthorFieldSection(resource: RegistryResource, field: Pick<RegistryFieldSchema,'path'|'advanced'>): string {
  if(field.advanced) return 'implementation'
  const path=field.path
  if(resource==='model') {
    if(!path.startsWith('specification.') || path==='specification.model_family'||path==='specification.schema_version') return 'identity'
    if(path==='specification.capabilities') return 'dependencies'
    return 'configuration'
  }
  if(resource==='deployment') {
    if(['key','name'].includes(path)) return 'identity'
    if(['profile_version','runtime_host'].includes(path)) return 'dependencies'
    return 'configuration'
  }
  if(resource==='provider') {
    if(['key','name','is_active'].includes(path)) return 'identity'
    if(['driver_type','modalities','operations'].includes(path)) return 'dependencies'
    return 'configuration'
  }
  if(resource==='artifact') return path==='profile_version'?'dependencies': ['role','logical_name'].includes(path)?'identity':'configuration'
  return 'all'
}
