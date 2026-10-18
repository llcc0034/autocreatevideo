// Original component; all API transport is isolated. No adoption or model calls.
import { createApp, h, ref } from 'vue'
import GeneratedArtifactCompare from '../../src/components/GeneratedArtifactCompare.vue'
import { platformApi } from '../../src/services/platformApi'
import { api } from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'

api.interceptors.request.use(() => { throw new Error('隔离页面禁止业务API') })
const picture = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="320" height="180" fill="#315d69"/><text x="35" y="95" fill="white" font-size="20">ISOLATED FIXTURE</text></svg>')
const mode = ref('media-failure'), reads = ref(0), writes = ref(0), epoch = ref(0)
const artifacts = ref<any[]>(['video','image'].map((kind,index) => ({ id: kind, kind, mimeType: kind === 'video' ? 'video/mp4' : 'image/svg+xml',
  role: 'primary_generation_output', status: 'available', variantGroup: 'same-group', variantIndex: index+1,
  objectKey: `isolated-${kind}`, sha256: 'a'.repeat(64) })))
platformApi.getArtifactPreview = async row => {
  reads.value++
  if (mode.value === 'api-failure') throw new Error('隔离签名API读取失败')
  return { taskId: '', outputIndex: 0, kind: row.kind, filename: row.objectKey,
    previewUrl: mode.value === 'media-failure' ? '/__isolated_missing_media__' : row.kind === 'image' ? picture : '/tests/fixtures/artifact-media-recovery.mp4',
    downloadUrl: '', parameters: {}, backendStatus: mode.value === 'invalid' ? 'invalid' : 'available' }
}
const receipts = new Map<string, any>()
let selectedId = ''
platformApi.selectArtifact = async (_run,body,key) => {
  writes.value++
  if (!receipts.has(key)) {
    selectedId = body.artifact_id
    receipts.set(key, {id:crypto.randomUUID(),action:'select',result_kind:'artifact_selection',target_id:crypto.randomUUID(),created_at:new Date().toISOString()})
    if (mode.value === 'lost-reply') throw Error('隔离：服务端已保存，首次响应丢失；请核对原请求')
  }
  return receipts.get(key)
}
createApp({ setup() { return () => h('main', { style: { maxWidth:'1050px',margin:'16px auto',padding:'16px',background:'#101820',color:'#e6edf3' } }, [
  h('h1','M8候选媒体恢复 · 隔离验证'),
  h('p','正式比较组件；模拟签名/选择响应，本地合成视频。无真实业务API、生成或采用。'),
  h('label',['预览响应场景 ',h('select',{ value:mode.value,onChange:(event:Event) => { mode.value=(event.target as HTMLSelectElement).value } },
    [['media-failure','媒体GET失败'],['ready','媒体恢复'],['lost-reply','选择响应丢失'],['api-failure','签名API失败'],['invalid','候选无效']].map(([value,label])=>h('option',{value},label)))]),
  h('button',{onClick:()=>{epoch.value++}},'切换运行视图'),
  h('output',`签名读取 ${reads.value} 次；选择提交 ${writes.value} 次`),
  h(GeneratedArtifactCompare,{runId:`isolated-${epoch.value}`,artifacts:artifacts.value,
    onSelected:()=>{artifacts.value=artifacts.value.map(row=>({...row,status:row.id===selectedId?'selected':'available'}))}}),
]) } }).mount('#app')
