<script setup lang="ts">
import type {TaskMediaTrace} from '@/services/taskCenter'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import FrozenReferenceCorrespondence from './FrozenReferenceCorrespondence.vue'
import ExactReferencePreview from './ExactReferencePreview.vue'
import {taskMediaSelection} from '@/services/exactReferencePreview'
defineProps<{trace:TaskMediaTrace}>()
const modes={workflow_input:'工作流素材输入',embedded_audio:'视频内嵌原音，共用配对视频上传',context_only:'仅提示词上下文，不进入工作流媒体端口'}
const receipts={recorded:'已有经校验的暂存回执',missing:'尚无暂存回执，不表示执行失败',not_applicable:'无需上传回执'}
const path=(parts:(string|number)[])=>parts.map(p=>`[${JSON.stringify(p)}]`).join('')
const wiringStates={matched:'声明端口核对一致',mismatch:'声明端口存在不一致',partial:'部分端口无法核对',unavailable:'端口映射不可核对',omitted:'可选端口未写入'}
const routeStates={connected:'原图连通',disconnected:'原图未连通',unavailable:'目标无法核对'}
const processingStates={not_applicable:'不适用',declared:'仅有处理声明',recorded:'已读取原处理记录',partial:'原处理记录字段不完整',unverifiable:'处理元数据无法完整核对'}
const byteStates={not_applicable:'不适用',unavailable:'尚无法比较',same:'来源与提交字节摘要相同',changed:'来源与提交字节摘要不同'}
const valueStates={missing:'原记录缺字段',unavailable:'原记录未提供确定值',invalid:'原记录字段类型或值无效'}
const nodeOrigins={original:'原图同类型节点',added:'编译请求新增节点',replaced:'节点类型已替换，不推断继承关系',unavailable:'原图基线不可核对'}
const inputChanges={unchanged:'与原图值相同',changed:'编译后值已变化',added:'编译请求新增端口',unavailable:'无可比较的原同类型节点'}
</script>
<template>
 <div class="media-trace">
 <p>{{trace.message}}</p>
 <p class="muted">工作流版本 {{trace.workflow_version_id}}<br/>传输请求摘要（非准备摘要） {{trace.request_sha256}}<br/>本次根任务 {{trace.root_id||'尚未建立'}}</p>
 <p role="status">标量接线：{{wiringStates[trace.scalar_wiring.state]}} · {{trace.scalar_wiring.rows.length}} 项</p>
 <details class="scalar-wiring"><summary>提示词／参数 → 声明节点端口 → 原请求核对</summary>
 <p>{{trace.scalar_wiring.message}}</p><p class="muted">冻结工作流合同摘要 {{trace.scalar_wiring.contract_sha256}}</p>
 <dl><template v-for="row in trace.scalar_wiring.rows" :key="JSON.stringify([row.node_id,row.input_name])">
 <dt>{{row.semantic_key}} · {{row.value_type}}</dt><dd><code>graph[{{JSON.stringify(row.node_id)}}].inputs[{{JSON.stringify(row.input_name)}}]</code><br/>{{row.class_type}}<br/>{{wiringStates[row.state]}} · {{row.message}}</dd>
 </template></dl>
 <p v-if="trace.scalar_wiring.state!=='unavailable' && !trace.scalar_wiring.rows.length">精确合同中无上述范围的标量绑定，不代表存在自动映射。</p>
 </details>
 <p v-if="!trace.items.length">本次明确无已解析媒体用途，不补入模板示例素材。</p>
 <details v-for="item in trace.items" :key="item.item_key" class="media-use">
 <summary>{{item.display_name}} · {{item.input_key}} / {{item.item_key}} · {{modes[item.mode]}}</summary>
 <FrozenReferenceCorrespondence :binding="item.reference_binding"/>
 <div class="source-preview">
 <p>本次冻结的原始参考素材：预览按素材身份及原文件指纹读取，不是描述文字或当前最新版本。</p>
 <ExactReferencePreview v-if="taskMediaSelection(item)" :selection="taskMediaSelection(item)!" :name="item.reference_binding.reference_name || item.display_name"/>
 <p v-else>本项没有可直接预览的素材身份；保留下方上游产物及回执信息，不以其他图片替代。</p>
 <p v-if="item.mode==='context_only'">此项仅作为提示词上下文，不作为图片上传。</p>
 <p v-else-if="item.receipt_state==='recorded'">已保存本次素材暂存回执；{{byteStates[item.processing_summary.byte_identity]}}。暂存成功不等于模型已成功执行。</p>
 <p v-else>尚无已记录的上传回执；预览存在不代表已经传给模型。</p>
 </div>
 <dl>
 <dt>本次来源</dt><dd>{{item.kind}} · 素材 {{item.asset_id||'不适用'}} · 上游产物 {{item.artifact_id||'不适用'}}<ReadonlyDataValue :value="item.origin" label="本次来源定位"/></dd>
 <dt>用途与配对</dt><dd>用途 {{item.purpose||'未指定'}} · 配对使用 {{item.paired_to||'无配对'}} · 提示词标记 {{item.input_label||'未记录'}}</dd>
 <dt>冻结来源摘要</dt><dd>事实 {{item.source_facts_sha256}}<br/>原始字节 {{item.source_bytes_sha256}}</dd>
 <dt>声明目标端口</dt><dd><ReadonlyDataValue :value="item.ports" label="声明目标端口"/></dd>
 <dt>原图实际媒体入口</dt><dd><p v-for="(entry,i) in item.graph_inputs" :key="i"><code>graph[{{JSON.stringify(entry.node_id)}}].inputs{{path(entry.input_path)}}</code><br/>{{entry.class_type}}</p><p v-if="!item.graph_inputs.length">未进入媒体入口</p><p v-if="item.mode==='embedded_audio'">这里显示配对视频共用入口；原音输出端口见声明目标及下方原图连接。</p></dd>
 <dt>素材入口 → 声明消费端口</dt><dd>
 <p v-if="!item.port_routes.length">无声明工作流端口，不补造素材接线。</p>
 <details v-for="(route,index) in item.port_routes" :key="index">
 <summary>{{route.target_key}} · {{routeStates[route.state]}} · {{route.edge_indexes.length}} 条图连接</summary>
 <p>{{route.message}}</p>
 <p v-if="route.destination">声明目标 <code>{{route.destination.node_id}}.{{route.destination.input_name}}</code></p>
 <p v-if="route.entry_node_id">本次入口 <code>{{route.entry_node_id}}</code></p>
 <ol><li v-for="edgeIndex in route.edge_indexes" :key="edgeIndex"><code>{{trace.edges[edgeIndex]?.from_node}} [输出 {{trace.edges[edgeIndex]?.output_index}}] → {{trace.edges[edgeIndex]?.to_node}}.{{trace.edges[edgeIndex]?.input_name}}</code></li></ol>
 </details></dd>
 <dt>上传前处理</dt><dd>{{item.processing_summary.label}} · {{item.adapter||'不适用'}}
 <p role="status">{{processingStates[item.processing_summary.state]}}</p>
 <details><summary>声明参数与原处理结果</summary>
 <p>{{item.processing_summary.description}}</p><p>配置归属：{{item.processing_summary.configuration_source}}</p>
 <p v-if="item.processing_summary.shared_with">共用视频使用 {{item.processing_summary.shared_with}} 的处理记录；不是独立音轨转换或上传。</p>
 <template v-for="section in [{title:'冻结处理声明（非实际结果）',rows:item.processing_summary.declared},{title:'原处理元数据（非当前探测）',rows:item.processing_summary.observed}]" :key="section.title">
 <h4>{{section.title}}</h4><dl v-if="section.rows.length"><template v-for="row in section.rows" :key="row.key">
 <dt>{{row.label}}</dt><dd><template v-if="row.state==='recorded'">{{typeof row.value==='boolean'?(row.value?'是':'否'):row.value}} {{row.unit}}</template><template v-else>{{valueStates[row.state]}}</template><br/><small class="muted">{{row.source}}</small></dd>
 </template></dl><p v-else>无此类记录，不补造数值。</p></template>
 <p>字节核对：{{byteStates[item.processing_summary.byte_identity]}}</p><p>{{item.processing_summary.message}}</p>
 <ReadonlyDataValue v-if="item.processing!==null" :value="item.processing" label="冻结预处理合同"/>
 </details></dd>
 <dt>实际处理回执</dt><dd><p role="status">{{receipts[item.receipt_state]}}</p><template v-if="item.receipt!==null"><ReadonlyDataValue :value="item.receipt" label="来源与提交字节摘要"/><ReadonlyDataValue :value="item.processing_metadata" label="原处理结果元数据"/></template></dd>
 </dl></details>
 <details v-if="trace.media_profile!==null"><summary>冻结媒体处理参数与修订</summary><ReadonlyDataValue :value="trace.media_profile" label="冻结媒体处理配置"/></details>
 <details v-if="trace.nodes.length"><summary>完整冻结图节点与连接 · {{trace.nodes.length}} 节点 / {{trace.edges.length}} 连接</summary>
 <p class="muted">这是原图接线，包含无参考素材请求的节点、上游节点和未连通节点；节点存在不代表已执行。不是节点运行回执或执行顺序，节点名称不保证其内部算法。下列值来自本次冻结请求，只与精确工作流原图比较；不是当前节点目录或模型收到的正文。</p>
 <details v-for="node in trace.nodes" :key="node.node_id">
 <summary>{{node.node_id}} · {{node.class_type}} · {{nodeOrigins[node.origin]}}</summary>
 <p>配置归属：固定节点值随工作流导入冻结；绑定参数／动态接线由原模型、Projector及执行组合共同编译。此处只对账，不修改历史。</p>
 <dl><template v-for="input in node.inputs" :key="input.input_name">
 <dt>{{input.input_name}}</dt><dd><p>{{inputChanges[input.change]}}</p><code>graph[{{JSON.stringify(node.node_id)}}].inputs[{{JSON.stringify(input.input_name)}}]</code>
 <p v-if="input.kind==='redacted'">敏感或路径内容已隐藏，不表示空值。</p>
 <p v-else-if="input.kind==='media'">本次素材占位引用；素材身份与暂存证据见上方对应使用项。</p>
 <template v-else><p v-if="input.kind==='connection'">连接到其他节点的输出（节点ID／输出索引）：</p><ReadonlyDataValue :value="input.value" :label="`${input.input_name} · 原请求值`"/></template>
 </dd></template></dl><p v-if="!node.inputs.length">本节点无输入端口。</p>
 <p v-if="node.removed_inputs.length">相对原同类型节点已移除端口：{{node.removed_inputs.join('、')}}。不回填原值。</p>
 </details>
 <p v-for="(edge,i) in trace.edges" :key="i"><code>{{edge.from_node}} [输出 {{edge.output_index}}] → {{edge.to_node}}.{{edge.input_name}}</code></p>
 </details></div>
</template>
<style scoped>
.media-trace{min-width:0;overflow-wrap:anywhere}.media-trace summary{cursor:pointer;min-height:36px;align-content:center}.media-trace summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.media-use{border-top:1px solid var(--border);padding:8px 0}.media-trace dl{display:grid;grid-template-columns:minmax(120px,1fr) minmax(0,3fr);gap:10px}.media-trace dd{margin:0;min-width:0}.media-trace code{white-space:normal}@media(max-width:640px){.media-trace dl{grid-template-columns:1fr}.media-trace dt{font-weight:600}}
</style>
