import type { JsonObject, NodeMetadataItem } from './platformTypes'

export interface ComfyApiNode extends JsonObject {
  class_type: string
  inputs: JsonObject
}
export type ComfyApiGraph = Record<string, ComfyApiNode>
export interface GraphProjectionNode {
  id: string
  classType: string
  label: string
  position: { x: number; y: number }
  inputs: JsonObject
  outputCount: number
  source: JsonObject
}
export interface GraphProjectionEdge { id: string; source: string; target: string; sourceOutput: number; targetInput: string }
export interface WorkflowGraphAnalysis {
  format: 'api' | 'ui' | 'unknown'
  graph: ComfyApiGraph
  nodes: GraphProjectionNode[]
  edges: GraphProjectionEdge[]
  blockers: string[]
  warnings: string[]
}

const record = (value: unknown): JsonObject => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}
const text = (value: unknown) => value === null || value === undefined ? '' : String(value)
const finite = (value: unknown, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback
const primitive = (value: unknown) => value === null || ['string', 'number', 'boolean'].includes(typeof value)
const apiNode = (value: unknown): value is ComfyApiNode => { const item = record(value); return typeof item.class_type === 'string' && Boolean(item.class_type) && item.inputs !== null && typeof item.inputs === 'object' && !Array.isArray(item.inputs) }
export const isComfyApiGraph = (value: unknown): value is ComfyApiGraph => { const graph = record(value); const nodes = Object.values(graph); return nodes.length > 0 && nodes.every(apiNode) }
const connection = (value: unknown, nodeIds: Set<string>): { nodeId: string; output: number } | null => Array.isArray(value) && value.length === 2 && nodeIds.has(text(value[0])) && Number.isInteger(Number(value[1])) ? { nodeId: text(value[0]), output: finite(value[1]) } : null

const projectApi = (graph: ComfyApiGraph, metadata: NodeMetadataItem[]): WorkflowGraphAnalysis => {
  const nodeIds = new Set(Object.keys(graph)); const metadataMap = new Map(metadata.map((item) => [item.classType, item])); const blockers: string[] = []; const warnings: string[] = []
  const nodes = Object.entries(graph).map(([id, raw], index) => { const meta = metadataMap.get(raw.class_type); if (metadata.length && !meta) blockers.push(`节点 ${id} 的 class_type “${raw.class_type}” 不在当前部署 node-metadata 中`); for (const [name, value] of Object.entries(record(raw.inputs))) { if (connection(value, nodeIds) || primitive(value)) continue; blockers.push(`节点 ${id}.${name} 是无法安全编辑的复杂控件值，已原样保留`) } const metaInfo = record(raw._meta); return { id, classType: raw.class_type, label: text(metaInfo.title) || meta?.displayName || raw.class_type, position: { x: (index % 4) * 280, y: Math.floor(index / 4) * 210 }, inputs: { ...record(raw.inputs) }, outputCount: Math.max(1, meta?.outputCount || 1), source: { ...raw } } })
  const edges: GraphProjectionEdge[] = []
  for (const [targetId, raw] of Object.entries(graph)) for (const [inputName, value] of Object.entries(record(raw.inputs))) { const linked = connection(value, nodeIds); if (linked) edges.push({ id: `${linked.nodeId}:${linked.output}->${targetId}:${inputName}`, source: linked.nodeId, target: targetId, sourceOutput: linked.output, targetInput: inputName }) }
  if (!metadata.length) warnings.push('尚未读取目标部署 node-metadata，不能确认节点和端口兼容性。')
  return { format: 'api', graph, nodes, edges, blockers: [...new Set(blockers)], warnings }
}

const uiToApi = (value: JsonObject, metadata: NodeMetadataItem[]): WorkflowGraphAnalysis => {
  const rawNodes = Array.isArray(value.nodes) ? value.nodes.map(record) : []; const blockers: string[] = []; const warnings = ['这是 ComfyUI UI 画布 JSON 的安全投影；后端只接受 API graph。']
  if (value.definitions || value.subgraphs || value.extra && record(value.extra).subgraphs) blockers.push('UI JSON 包含子图/definitions，当前裁剪契约无法无损展开')
  const graph: ComfyApiGraph = {}; const positions = new Map<string, { x: number; y: number }>()
  for (const [index, node] of rawNodes.entries()) { const id = text(node.id); const classType = text(node.type || node.class_type); if (!id || !classType) { blockers.push(`UI 节点 ${index + 1} 缺少 id/type`); continue } if (/subgraph/i.test(classType)) blockers.push(`UI 节点 ${id} 是子图节点`); const widgets = node.widgets_values; const literals: JsonObject = {}; if (Array.isArray(widgets) && widgets.length) blockers.push(`UI 节点 ${id} 使用按位置编码的 widgets_values，node-metadata 不含控件类型/顺序，不能安全转换`); else if (widgets && typeof widgets === 'object') Object.assign(literals, record(widgets)); const rawInputs = Array.isArray(node.inputs) ? node.inputs.map(record) : []; for (const input of rawInputs) if ('value' in input && primitive(input.value)) literals[text(input.name)] = input.value; const source: ComfyApiNode = { class_type: classType, inputs: literals }; graph[id] = source; const pos = Array.isArray(node.pos) ? node.pos : []; positions.set(id, { x: finite(pos[0], (index % 4) * 280), y: finite(pos[1], Math.floor(index / 4) * 210) }) }
  const links = Array.isArray(value.links) ? value.links : []
  for (const rawLink of links) { if (!Array.isArray(rawLink) || rawLink.length < 6) { blockers.push('UI links 中包含无法识别的连接记录'); continue } const sourceId = text(rawLink[1]); const sourceSlot = finite(rawLink[2]); const targetId = text(rawLink[3]); const targetSlot = finite(rawLink[4]); const targetNode = rawNodes.find((item) => text(item.id) === targetId); const inputs = Array.isArray(targetNode?.inputs) ? targetNode?.inputs.map(record) : []; const inputName = text(inputs[targetSlot]?.name); if (!graph[sourceId] || !graph[targetId] || !inputName) { blockers.push(`UI 连接 ${text(rawLink[0])} 无法映射目标输入名`); continue } graph[targetId].inputs[inputName] = [sourceId, sourceSlot] }
  const projected = projectApi(graph, metadata); projected.format = 'ui'; projected.blockers = [...new Set([...blockers, ...projected.blockers])]; projected.warnings = [...warnings, ...projected.warnings]; projected.nodes = projected.nodes.map((item) => ({ ...item, position: positions.get(item.id) || item.position })); return projected
}

export const analyzeComfyWorkflow = (value: unknown, metadata: NodeMetadataItem[] = []): WorkflowGraphAnalysis => {
  if (isComfyApiGraph(value)) return projectApi(value, metadata)
  const raw = record(value); if (Array.isArray(raw.nodes)) return uiToApi(raw, metadata)
  return { format: 'unknown', graph: {}, nodes: [], edges: [], blockers: ['JSON 既不是 ComfyUI API graph，也不是包含 nodes/links 的 UI workflow'], warnings: [] }
}

export const buildApiGraph = (nodes: GraphProjectionNode[], edges: GraphProjectionEdge[]): ComfyApiGraph => {
  const nodeIds = new Set(nodes.map((item) => item.id)); const graph: ComfyApiGraph = {}
  for (const node of nodes) { const inputs: JsonObject = {}; for (const [name, value] of Object.entries(node.inputs)) if (!connection(value, nodeIds)) inputs[name] = value; graph[node.id] = { ...node.source, class_type: node.classType, inputs } }
  for (const edge of edges) if (graph[edge.source] && graph[edge.target] && edge.targetInput) graph[edge.target].inputs[edge.targetInput] = [edge.source, edge.sourceOutput]
  return graph
}

export const parseComfyLiteralInput = (raw: string): unknown => {
  try { return JSON.parse(raw) } catch { return raw }
}

export const updateGraphLiteralInput = (
  nodes: GraphProjectionNode[],
  nodeId: string,
  inputName: string,
  raw: string,
): GraphProjectionNode[] => nodes.map((node) => node.id === nodeId
  ? { ...node, inputs: { ...node.inputs, [inputName]: parseComfyLiteralInput(raw) } }
  : node)
