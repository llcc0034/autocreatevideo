import assert from 'node:assert/strict'
import test from 'node:test'
import {readFile} from 'node:fs/promises'
import {createServer} from 'vite'
import {createSSRApp,h} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {workflowContractFixture,workflowPreviewFixture} from './fixtures/workflow-public-contract-data.ts'

test('workflow public contract preserves exact declarations without resolving task input', async context => {
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const {default:Component}=await vite.ssrLoadModule('/src/components/WorkflowPublicContract.vue')
  const render=(props:unknown)=>renderToString(createSSRApp({render:()=>h(Component,props)}))
  await context.test('saved version displays simultaneous constraints, zero, false, dynamic roles and output',async()=>{
    const row=workflowContractFixture(); const original=JSON.stringify(row)
    const html=await render({workflow:row})
    for(const text of ['冻结声明','video.frames','5 – 3600','5 + 17k','允许值','联动组','video-length','ref_image_0','Ref2VA','参考类别、用途及配套音频约束','false','0','合同建议值','建议值不是本次任务已填值'])assert.ok(html.includes(text),text)
    assert.doesNotMatch(html,/motion_reference/,'structured diagnostics remain lazy until opened')
    assert.ok(html.includes('视频'));assert.doesNotMatch(html,/<input|<select|<textarea|<button/)
    assert.equal(JSON.stringify(row),original)
  })
  await context.test('missing and explicitly unavailable schemas remain errors, not empty-input success',async()=>{
    for(const patch of [{workflowInputSchema:undefined},{workflowInputSchemaError:'合同读取失败'},{workflowInputSchema:{...workflowContractFixture().workflowInputSchema,available:false,reasonCode:'schema_missing'}}]){
      const html=await render({workflow:{...workflowContractFixture(),...patch}})
      assert.match(html,/role="alert"/);assert.match(html,/不能解释为没有输入/);assert.doesNotMatch(html,/ref_image_0|当前说明未列出可调参数/)
    }
    assert.match(await render({}),/尚未生成合同草稿/)
  })
  await context.test('preview keeps source, default, recommendation separate and uses only explicit output IDs',async()=>{
    const row=workflowPreviewFixture();const original=JSON.stringify(row)
    const html=await render({preview:row})
    for(const text of ['服务端合同草稿','不代表已保存','画布原值','节点目录默认值','服务端建议值','39','22','5','chosen-video'])assert.ok(html.includes(text),text)
    assert.doesNotMatch(html,/first-image/);assert.equal(JSON.stringify(row),original)
    assert.match(await render({preview:{...row,primaryOutputNodeIds:[]}}),/尚未明确主输出/)
    const missing=await render({preview:{...row,primaryOutputNodeIds:['missing']}})
    assert.match(missing,/未在返回目录中找到/);assert.doesNotMatch(missing,/chosen-video|first-image/)
  })
  await context.test('custom names are escaped and empty/false values do not become defaults',async()=>{
    const row=workflowPreviewFixture() as any
    row.configurableParameters[0]={...row.configurableParameters[0],semanticKey:'自定义<script>中文',sourceValue:'',defaultValue:false,recommendedSafeValue:0}
    const html=await render({preview:row});assert.match(html,/自定义&lt;script&gt;中文/);assert.match(html,/false/);assert.match(html,/空文本/)
    assert.doesNotMatch(html,/<script>/)
  })
  await context.test('formal import and saved page share one renderer and default reading does not focus Smoke',async()=>{
    const page=await readFile(new URL('../src/views/WorkflowVersionEditorView.vue',import.meta.url),'utf8')
    const importer=await readFile(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
    assert.ok(page.indexOf('<WorkflowPublicContract :workflow=')<page.indexOf('id="workflow-acceptance-gates"'))
    assert.match(page,/<details class="workflow-contract-summary"><summary>实现详情/)
    assert.match(page,/if \(!requestedFocus\.value\) return/)
    assert.match(importer,/<WorkflowPublicContract[^]*?:preview="savedWorkflowDrafts\[savedWorkflowKey\(item\)\]\.preview!/)
    assert.doesNotMatch(page,/workflowInputParameters|workflowAssetSlots|workflowRuntimeValueCount/)
    const component=await readFile(new URL('../src/components/WorkflowPublicContract.vue',import.meta.url),'utf8')
    assert.doesNotMatch(component,/platformApi|repository|fetch\(|\.sort\(/)
  })
})
