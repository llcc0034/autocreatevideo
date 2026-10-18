import type { TaskReferenceAssetSummary, WorkflowInputContractCode, WorkflowInputContractStatus } from '../types/index.ts'
import { adaptFrozenReferenceBinding } from './frozenReferenceBinding.ts'

type Row = Record<string, unknown>
const object = (value: unknown): Row => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('任务输入合同必须是对象')
  return value as Row
}
const text = (row: Row, key: string, empty = false): string => {
  const value = row[key]
  if (typeof value !== 'string' || (!empty && !value)) throw new Error(`任务输入缺少 ${key}`)
  return value
}
const nullableText = (row: Row, key: string) => row[key] === null ? undefined : text(row, key)
const array = (row: Row, key: string): unknown[] => {
  if (!Array.isArray(row[key])) throw new Error(`任务输入缺少 ${key}`)
  return row[key]
}

export function adaptTaskInputs(raw: Row): {
  inputContractStatus: WorkflowInputContractStatus; inputContractCode: WorkflowInputContractCode
  inputParameters?: Row; referenceAssets: TaskReferenceAssetSummary[]; contextMediaUses: TaskReferenceAssetSummary[]
} {
  const status = raw.input_contract_status
  if (status === 'unavailable') {
    if (raw.input_parameters !== null || raw.input_contract_code !== 'frozen_contract_invalid') throw new Error('不可用的任务输入必须明确为空并提供原因')
    return { inputContractStatus: status, inputContractCode: raw.input_contract_code, referenceAssets: [], contextMediaUses: [] }
  }
  if (status !== 'available' || raw.input_contract_code !== '') throw new Error('任务输入状态缺失或不一致')
  const source = object(raw.input_parameters)
  for (const key of ['preparation_id', 'attempt_id', 'execution_instance_version_id', 'workflow_version_id', 'request_sha256', 'operation']) text(source, key)
  if (!['image', 'video', 'audio'].includes(String(source.output_kind))) throw new Error('任务产物类型缺失')
  if (!['manual', 'profile', 'none'].includes(String(source.prompt_mode))) throw new Error('任务提示词来源缺失')
  nullableText(source, 'prompt_profile_version_id')
  const parameters = array(source, 'parameters').map(value => {
    const row = object(value)
    text(row, 'key'); text(row, 'value_source'); object(row.rule)
    if (!['string', 'number', 'boolean'].includes(typeof row.value) || (typeof row.value === 'number' && !Number.isFinite(row.value))) throw new Error('任务参数值无效')
    return row
  })
  if (new Set(parameters.map(row => row.key)).size !== parameters.length) throw new Error('任务参数重复')
  array(source, 'prompt_channels').forEach(value => {
    const row = object(value)
    text(row, 'key'); text(row, 'semantic_key')
    if (!Number.isInteger(row.character_count) || (row.character_count as number) < 0) throw new Error('提示词通道摘要无效')
  })
  const media = array(source, 'media_uses').map(value => {
    const row = object(value)
    if (!['image', 'video', 'audio'].includes(String(row.kind))) throw new Error('素材使用类型缺失')
    const ports = array(row, 'ports').map(value => {
      const entry = object(value)
      const port = object(entry.port)
      text(port, 'kind')
      return { targetKey: text(entry, 'target_key'), port }
    })
    if (!['staged', 'not_staged', 'not_applicable'].includes(String(row.staging_status)) || ((ports.length === 0) !== (row.staging_status === 'not_applicable'))) throw new Error('素材暂存状态与槽位不一致')
    return { itemKey: text(row, 'item_key'), inputKey: text(row, 'input_key'), assetId: nullableText(row, 'asset_id'),
      referenceBinding: adaptFrozenReferenceBinding(row.reference_binding),
      artifactId: nullableText(row, 'artifact_id'), displayName: text(row, 'display_name', true),
      mediaKind: row.kind as TaskReferenceAssetSummary['mediaKind'], previewUrl: text(row, 'preview_url', true),
      inputLabel: nullableText(row, 'input_label'), purpose: nullableText(row, 'purpose'), pairedTo: nullableText(row, 'paired_to'),
      checksum: text(row, 'checksum'), sourceSha256: text(row, 'source_sha256'), ports,
      stagingStatus: row.staging_status as TaskReferenceAssetSummary['stagingStatus'] }
  })
  if (new Set(media.map(row => row.itemKey)).size !== media.length) throw new Error('素材使用标识重复')
  return { inputContractStatus: status, inputContractCode: '', inputParameters: source,
    referenceAssets: media.filter(row => row.ports.length > 0), contextMediaUses: media.filter(row => row.ports.length === 0) }
}
