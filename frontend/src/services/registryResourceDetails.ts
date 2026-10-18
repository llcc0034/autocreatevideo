import { platformApi } from './platformApi'
import { executionRegistryApi } from './executionRegistryApi'
import { businessOutputs, type BusinessOutputVersion } from './businessOutputs'
import { dataStructures, type DataStructureDetail } from './dataStructures'
import { fieldDefinitionVersions, type FieldDefinitionVersion } from './fieldDefinitionVersions'
import { contentSources, type ContentSourceVersion } from './contentSources'
import type { RelationKind } from './registryRelations'
import { ruleContentMethod } from './promptPrimitiveConfiguration'
import { recipeConfigurationSteps } from './recipeConfigurationPresentation'

// A display projection over existing readers. No defaults, resolution or writes.
export interface ResourceFact { label: string; value: unknown }
export interface ResourceSection { title: string; description?: string; facts: ResourceFact[] }
export const runtimeResourceKinds = { provider: '服务连接', host: '运行主机', artifact: '模型文件声明' } as const
export type ResourceKind = RelationKind | keyof typeof runtimeResourceKinds
export interface ResourceLink { kind: ResourceKind; id: string; label: string }
export interface ResourceDetails { id: string; name: string; version?: number; status: string; sections: ResourceSection[]; links?: ResourceLink[]; maintenanceUrl?: string; relationScope?: string }
const reference = (kind: ResourceKind, id: unknown, label: string): ResourceLink[] => typeof id === 'string' && id ? [{kind,id,label}] : []
const unique = <T extends {id:string}>(rows:T[],id:string):T => {
  const matches=rows.filter(row=>row.id===id)
  if(matches.length!==1)throw Error('精确资源不存在或目录身份重复；不使用同名记录代替。')
  return matches[0]!
}
const fact = (label: string, value: unknown): ResourceFact => ({ label, value })
const exact = <T extends { id: string }>(row: T, id: string): T => {
  if (row.id !== id) throw new Error('资源详情身份不匹配；不会使用同名或最新版本代替。')
  return row
}

export function businessOutputDetails(row: BusinessOutputVersion): ResourceDetails {
  const parser=row.parser_contract
  if(!parser||parser.key!==row.parser_key)throw new Error('输出解析合同缺失，不能用当前目录代替。')
  return {id:row.id,name:row.name,version:row.version,status:row.status,sections:[
    {title:'用途与响应结构',description:'公共输出定义约束模型返回，不是某次调用的实际结果。',facts:[fact('使用说明',row.description),fact('精确响应结构版本',row.data_structure_version),fact('解析协议',row.parser_key)]},
    {title:'本版本如何解析',description:'解析规则来自此版本；结构决定字段类型，映射说明业务含义，查看不会执行解析或生成。',facts:row.parser_key==='script_semantics.v1'?[fact('语义映射',parser.mapping)]:row.parser_key==='visual_rules.v2'?[fact('输出字段与转换',parser.fields),fact('响应位置',parser.response_keys),fact('允许取值',parser.allowed_values)]:[fact('返回格式',parser.format)]},
    {title:'质量建议与使用',description:'执行实例显式引用该版本；新版本不会自动替换使用方，配置发布不等于业务生成完成。',facts:[fact('质量建议',row.quality_advisories)]},
  ]}
}

export function contentSourceDetails(row: ContentSourceVersion): ResourceDetails {
  if (!row.reader || typeof row.reader !== 'object' || Array.isArray(row.reader)) throw new Error('版本读取合同缺失，不能用当前目录代替。')
  const reader = row.reader
  return { id: row.id, name: row.name, version: row.version, status: row.status, sections: [
    { title: '用途与身份', description: '这是可复用的读取定义，不是某个项目或任务的具体正文。', facts: [fact('使用说明', row.description), fact('读取位置标识', row.reader_selector)] },
    { title: '本版本如何读取', description: '以下为此精确版本保存的规则，不使用当前目录覆盖旧版本。', facts: [fact('读取协议', reader.protocol), fact('值类型', reader.value_kind), fact('适用范围', reader.scope_kind), fact('确认与冻结方式', reader.snapshot_policy), ...(reader.output_field ? [fact('已审核输出字段', reader.output_field), fact('所属阶段', reader.stage_key), fact('输出结构协议版本', reader.output_schema_version)] : [])] },
    { title: '输出结构与使用方', description: '实际读取的内容按所引用的结构校验，再交给引用此来源的策略；查看定义不会读取正文或调用 AI。', facts: [fact('精确输出结构版本', row.data_structure_version)] },
  ] }
}

export function fieldVersionDetails(row: FieldDefinitionVersion): ResourceDetails {
  return { id:row.id, name:row.label, version:row.version, status:row.status, sections:[
    {title:'字段语义', description:'公共定义描述值的含义，不是某个项目或镜头的实际内容。', facts:[fact('稳定字段键',row.field_key),fact('分类',row.category),fact('含义说明',row.description),fact('影响说明',row.impact)]},
    {title:'值的形态与约束', description:'必填、默认值及使用处的额外约束由结构或调用配置维护，此处不复制。', facts:[fact('值类型',row.value_type),fact('单位',row.unit),...(row.value_type==='enum'?[fact('枚举选项',row.value_options)]:[])]},
  ] }
}

export function structureVersionDetails(row: DataStructureDetail): ResourceDetails {
  if (!Array.isArray(row.nodes)) throw new Error('结构节点未返回，不能解释为空结构。')
  return {id:row.id,name:row.name,version:row.version,status:row.status,sections:[
    {title:'结构含义',description:'公共定义组织字段，不保存本次任务的具体内容。',facts:[fact('结构家族',row.structure_key),fact('说明',row.description)]},
    {title:'字段组织与约束',description:'精确引用公共字段版本；必填、默认值和局部约束只作用于此结构。',facts:row.nodes.map(node=>fact(node.property_name||node.node_key,{
      '节点键':node.node_key,'父节点':node.parent_key,'同层排序':node.position,'字段版本':node.field_definition_version,
      '必需':node.required,'允许空值':node.allow_null,'允许空文本':node.allow_blank,'允许空集合':node.allow_empty,
      '有默认值':node.has_default,...(node.has_default?{'默认值':node.default_value}:{}),'约束':node.constraints,
    }))},
  ]}
}

export async function readResourceDetails(kind: ResourceKind, id: string): Promise<ResourceDetails | null> {
  // These resources have existing readers but are not kinds in the relation API.
  // Keep the relation contract unchanged and label the narrower read scope.
  if (kind === 'provider') {
    const row=unique(await platformApi.listProviders(),id)
    const models=(await platformApi.listModelProfiles()).filter(model=>model.provider===id)
    return {id,name:row.name,status:row.isActive?'active':'inactive',maintenanceUrl:'/settings?'+new URLSearchParams({section:'models',provider:id}),relationScope:'仅列当前模型目录中明确引用此连接的版本；不代表所有工作流或历史调用。',sections:[
      {title:'连接职责',description:'提供协议与连接配置，不定义创作字段；不是一次模型调用。地址、驱动配置与凭据不在公共详情展示。',facts:[fact('驱动方式',row.driverType),fact('支持模态',row.modalities),fact('支持操作',row.operations),fact('是否配置凭据',row.credentialConfigured)]},
    ],links:models.map(model=>({kind:'model',id:model.id,label:`使用方：${model.profileKey} · v${model.version}`}))}
  }
  if (kind === 'host') {
    const row=unique(await platformApi.listRuntimeHosts(),id)
    const deployments=(await platformApi.listDeployments()).filter(deployment=>deployment.runtimeHost===id)
    return {id,name:row.name,status:row.status,maintenanceUrl:'/settings?'+new URLSearchParams({section:'runtime',host:id}),relationScope:'仅列当前部署目录中明确绑定此主机的记录；不是探测结果、运行中任务或全部历史消费者。',sections:[
      {title:'运行位置',description:'主机登记与可运行证据分开；此处只读登记，不发起探测、启停或模型调用。',facts:[fact('主机标识',row.key),fact('最近报告时间',row.lastSeenAt)]},
    ],links:deployments.map(deployment=>({kind:'deployment',id:deployment.id,label:`绑定部署：${deployment.name}`}))}
  }
  if (kind === 'artifact') {
    const row=unique(await platformApi.listModelArtifacts(),id)
    return {id,name:row.logicalName,status:row.status,maintenanceUrl:'/settings?'+new URLSearchParams({section:'models',model_profile_version:row.profileVersion,artifact:id}),relationScope:'文件声明属于确切模型版本，不据此推断工作流内部节点或真实任务已消费此文件。',sections:[
      {title:'文件声明',description:'登记文件身份，不是上传或搬移文件；最新主机可用性与核验另在维护页查看。',facts:[fact('文件角色',row.role),fact('文件名',row.filename),fact('声明字节数',row.sizeBytes),fact('声明SHA256',row.sha256)]},
      {title:'已保存核验记录',facts:[fact('核验快照标识',row.verifiedSnapshot),fact('核验时间',row.verifiedAt)]},
    ],links:reference('model',row.profileVersion,'所属模型版本')}
  }
  if (kind === 'recipe') {
    const row = exact(await platformApi.getRecipe(id), id)
    const definition = row.definition
    if (!definition || !Array.isArray(definition.stages) || definition.stages.some(stage => !stage || typeof stage !== 'object' || Array.isArray(stage))) throw new Error('配方步骤声明缺失或格式异常，不能解释为空配方。')
    return { id, name: row.name, version: row.version, status: row.status, sections: [
      { title: '编排用途', description: '配方引用执行实例，不重复维护模型、工作流或策略；下面是配置路径，不是实际生产进度。', facts: [fact('制作方式', definition.interaction_mode), fact('合同版本', definition.schema_version)] },
      ...recipeConfigurationSteps(definition).map(step => ({ title: step.label, facts: [
        fact('执行方式', step.mode), fact('阶段审核', step.approval),
        fact('输入、绑定与处理规则', step.stage),
        fact('已声明的下游使用', step.consumers.map(consumer => ({ '步骤': consumer.label, '目标位置': consumer.target, '上游输出': consumer.output, '引用顺序正确': consumer.orderValid }))),
      ] })),
    ] }
  }
  if (kind === 'source') return contentSourceDetails(exact(await contentSources.get(id), id))
  if (kind === 'field') {
    const row=exact(await platformApi.getFieldDefinition(id),id)
    return {id,name:row.key,status:row.isActive?'active':'inactive',sections:[
      {title:'稳定身份',description:'身份是公共引用的根，不是模型调用，也不保存项目字段值。',facts:[fact('唯一键',row.key),fact('允许新使用',row.isActive)]},
      {title:'定义版本与使用',description:'名称、类型、单位、含义和枚举在字段定义版本维护。结构与策略显式引用精确版本；身份启停不会改写已有版本。',facts:[fact('维护入口','字段维护 → 定义版本')]},
    ]}
  }
  if (kind === 'deployment') {
    const row = exact(await platformApi.getDeployment(id), id)
    return { id, name: row.name, status: row.status, links:[...reference('provider',row.providerProfileId,'服务连接'),...reference('host',row.runtimeHost,'运行主机')], sections: [
      { title: '部署职责', facts: [fact('服务提供方式', row.providerDriver), fact('优先级', row.priority), fact('是否默认部署', row.isDefault)] },
      { title: '引用的运行资源', description: '部署不重复定义模型输入输出。连接与主机在其唯一维护页管理；此处不展示凭据或连接参数。', facts: [fact('模型能力版本', row.profileVersion), fact('服务连接', row.providerProfileId), fact('运行主机', row.runtimeHost), fact('是否配置凭据', row.credentialConfigured)] },
    ] }
  }
  if (kind === 'model') {
    const row = exact(await platformApi.getModelProfile(id), id)
    return { id, name: row.profileKey, version: row.version, status: row.status, links:reference('provider',row.provider,'服务连接'), sections: [
      { title: '模型能力', facts: [fact('模型标识', row.modelIdentifier), fact('模态', row.modality), fact('Provider', row.providerKey)] },
      { title: '能力与接口声明', description: '以下为这个版本保存的声明；部署负责连接，工作流内部接线不在此重新维护。', facts: [fact('模型规范', row.specification)] },
    ] }
  }
  if (kind === 'workflow') {
    const row = exact(await platformApi.getWorkflowVersion(id), id)
    return { id, name: row.templateName, version: row.version, status: row.status, sections: [
      { title: '对外输入与参数', facts: [fact('输入合同', row.inputContract), ...(row.workflowInputSchemaError ? [fact('输入说明异常', row.workflowInputSchemaError)] : [])] },
      { title: '输出及识别状态', description: '配置发布、结构校验与真实调用是不同状态；查看不会启动校验或生成。', facts: [fact('输出类型', row.outputKind), fact('校验状态', row.validationState), fact('校验说明', row.validationMessage), fact('Smoke状态', row.smokeTestState), fact('Smoke说明', row.smokeTestMessage)] },
      { title: '实现详情', facts: [fact('语义接线', row.semanticBindings), fact('内部资源声明', row.modelRequirements), fact('原始节点图', row.graph)] },
    ] }
  }
  if (kind === 'prompt') {
    const row = exact(await platformApi.getPromptProfileVersion(id), id)
    const rules = await platformApi.listPromptProfileFieldRules(id)
    if (rules.some(rule => rule.profileVersion !== id)) throw new Error('字段规则属于其他策略，未合并展示。')
    if (rules.length !== row.fieldRuleCount || new Set(rules.map(rule => rule.id)).size !== rules.length) throw new Error('字段规则数量或身份与策略不一致，请重新读取。')
    rules.sort((a, b) => a.position - b.position)
    return { id, name: row.name, version: row.version, status: row.status, sections: [
      { title: '用途与输入来源', facts: [fact('用途', row.operation), fact('说明', row.description), fact('来源读取方式', row.sourceProjectionKey), fact('输入结构版本', row.inputStructureVersion), fact('来源规则', row.sourceProjectionConfig)] },
      { title: '字段如何进入文本通道', description: '按规则读取、保留、生成或翻译。辅助目标在使用此策略的执行实例中绑定，不是每项字段都调用 AI。', facts: rules.map(rule => fact(`${rule.position} · ${rule.fieldLabel} → ${rule.channel}`, {
        '字段键': rule.fieldKey, '字段定义版本': rule.fieldDefinitionVersion, '来源版本': rule.sourceVersion,
        '取值来源': rule.sourceSelector, '路径': rule.sourcePath, '处理方式': rule.generationMode, '内容处理路径': ruleContentMethod(rule),
        ...(rule.sourceSelector === 'fixed.value' ? { '固定值': rule.fixedValue } : {}),
        '投影规则': rule.projectionPrimitive, '语言规则': rule.languagePolicy, '必需': rule.required,
        '有默认值': rule.hasDefault, ...(rule.hasDefault ? { '默认值': rule.defaultValue } : {}),
        '通道组装': { '标签样式': rule.labelStyle, '分隔符': rule.separator, '空内容省略': rule.omitIfEmpty, '最大字符数': rule.maxChars },
        '生成／翻译配置（按处理方式生效）': {
          '生成来源': rule.generationSourceSelector, '上下文来源': rule.generationContextSelectors,
          '生成指令': rule.generationInstruction, '输出含义': rule.generationOutputSemantics,
          '必须生成': rule.generationRequired, '长度单位': rule.lengthUnit,
          '最小长度': rule.generationMin, '最大长度': rule.generationMax, '校验规则': rule.generationValidatorKeys,
        },
      })) },
      { title: '输出到提示词通道', facts: [fact('通道约束', row.channelLimits)] },
    ] }
  }
  if (kind === 'execution') {
    const row = exact(await executionRegistryApi.version(id), id)
    return { id, name: row.instance_name, version: row.version, status: row.status, sections: [
      { title: '能力与实现', facts: [fact('能力', row.configuration.operation), fact('模式', row.configuration.mode), fact('部署', row.configuration.deployment_id), fact('工作流版本', row.configuration.workflow_version_id)] },
      { title: '输入准备', facts: [fact('输入定义与接线', row.configuration.inputs), fact('提示词方式', row.configuration.prompt_mode), fact('提示词策略版本', row.configuration.prompt_profile_version_id), fact('按需辅助实例', row.configuration.auxiliaries), fact('参数策略', row.configuration.parameters)] },
      { title: '输出约定', description: '此处读取已保存配置，不进行请求准入或 Smoke；完整目标合同在实例维护页复核。', facts: [fact('业务输出版本', row.configuration.output_version_id)] },
    ] }
  }
  if (kind === 'output') {
    return businessOutputDetails(exact(await businessOutputs.detail(id), id))
  }
  if (kind === 'structure') {
    return structureVersionDetails(exact(await dataStructures.get(id), id))
  }
  if (kind === 'field_version') {
    const row = exact(await fieldDefinitionVersions.get(id), id)
    return fieldVersionDetails(row)
  }
  if (kind === 'projector') {
    const row = exact(await platformApi.getWorkflowProjectorVersion(id), id)
    return { id, name: row.projectorKey, version: row.version, status: row.status, sections: [
      { title: '适用实现', facts: [fact('引擎', row.engineKey), fact('引擎版本', row.engineVersion), fact('提供方式', row.providerDriverType), fact('能力', row.operation), fact('输出类型', row.outputKind)] },
      { title: '识别与映射规则', description: '工作流解释方案，不是另一轮模型调用；不代表任意节点均已支持。', facts: [fact('方案定义', row.definition)] },
    ] }
  }
  return null // Other kinds retain their existing exact maintenance page, explicitly labelled.
}
