import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createSSRApp,h} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {referenceFixture} from './fixtures/projector-reference-data.ts'

for(const kind of ['references','classified'] as const){
  test(`${kind}: public quantity precedes collapsed implementation; readonly renders identical facts`,async context=>{
    const vite=await createServer({logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
    context.after(()=>vite.close())
    const {adaptWorkflowProjectorOptions}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const {workflowProjectorDefinitionErrors}=await vite.ssrLoadModule('/src/services/workflowProjectorDefinition.ts')
    const fixture=referenceFixture(kind), options=adaptWorkflowProjectorOptions(fixture.options),before=JSON.stringify(fixture.definition)
    assert.deepEqual(workflowProjectorDefinitionErrors(fixture.definition,options,fixture.engineKey,1),[])
    const name=kind==='references'?'WorkflowAssetGroupConfigEditor':'WorkflowClassifiedReferenceEditor'
    const {default:Editor}=await vite.ssrLoadModule(`/src/components/${name}.vue`)
    for(const readOnly of [false,true]){
      const html=await renderToString(createSSRApp({render:()=>h(Editor,{modelValue:fixture.definition,options,readOnly,disabled:readOnly,modelProfiles:[fixture.model]})}))
      assert.match(html,/projector-reference-editor/)
      const articles=[...html.matchAll(/<article[\s\S]*?<\/article>/g)].map(row=>row[0])
      assert.equal(articles.length,kind==='references'?2:4)
      for(const article of articles){
        assert.ok(article.indexOf('数量合同来源')<article.indexOf('reference-implementation'))
        assert.match(article,/<details class="reference-implementation"><summary>/)
        assert.doesNotMatch(article,/<details[^>]*reference-implementation[^>]*\bopen\b/)
      }
      assert.match(html,kind==='references'?/逐项合并/:/视频配套音频/)
      if(readOnly)assert.match(html,/disabled/)
    }
    assert.equal(JSON.stringify(fixture.definition),before)
  })
}

test('reference hierarchy uses shared typography and touch targets without another data writer',async context=>{
  const {readFile}=await import('node:fs/promises')
  const css=await readFile('src/components/projectorReferenceEditor.css','utf8')
  assert.match(css,/font-size:var\(--config-body-size\)/)
  assert.match(css,/min-height:44px/)
  for(const name of ['WorkflowAssetGroupConfigEditor','WorkflowClassifiedReferenceEditor']){
    const source=await readFile(`src/components/${name}.vue`,'utf8')
    assert.match(source,/<style src="\.\/projectorReferenceEditor.css"><\/style>/)
    assert.doesNotMatch(source,/font-size:\s*(?:10|11)px/)
  }
})

test('classified implementation edits keep per-media identity and port exclusivity; readonly does not write',async context=>{
  const vite=await createServer({logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  context.after(()=>vite.close())
  const {default:Editor}=await vite.ssrLoadModule('/src/components/WorkflowClassifiedReferenceEditor.vue')
  const {createRenderer,reactive,nextTick,ssrContextKey}=await import('vue')
  const fixture=referenceFixture('classified'),props:any=reactive({modelValue:fixture.definition,readOnly:false,modelProfiles:[],options:{}})
  let state:any,writes=0
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Editor.setup(props,{expose(){},emit(event:string,value:any){if(event==='update:modelValue'){props.modelValue=value;writes++}}});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount())
  const other=JSON.stringify(props.modelValue.projection.config.dynamic_groups.videos)
  state.changeGroup('images','input_names',['appearance_0','appearance_1']);await nextTick()
  assert.equal(props.modelValue.projection.config.dynamic_groups.images.prefix,'')
  assert.deepEqual([...props.modelValue.projection.config.dynamic_groups.images.input_names],['appearance_0','appearance_1'])
  assert.equal(JSON.stringify(props.modelValue.projection.config.dynamic_groups.videos),other)
  state.changeGroup('images','prefix','reference.');await nextTick()
  assert.deepEqual([...props.modelValue.projection.config.dynamic_groups.images.input_names],[])
  assert.equal(props.modelValue.projection.config.static_semantics['audio.enabled'],false)
  props.readOnly=true;const before=JSON.stringify(props.modelValue)
  state.changeGroup('images','input_names',['forbidden']);state.setAudio('true');await nextTick()
  assert.equal(writes,2);assert.equal(JSON.stringify(props.modelValue),before)
})
