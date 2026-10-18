import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { referenceFixture } from './fixtures/projector-reference-data.ts'

async function setup(t:any) {
  const vite=await createServer({logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  t.after(()=>vite.close())
  const {default:Component}=await vite.ssrLoadModule('/src/components/WorkflowProjectorPresentation.vue')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  for(const key of Object.keys(platformApi)) platformApi[key]=()=>assert.fail(`unexpected request ${key}`)
  const fixture=referenceFixture('classified')
  const version={id:'original-v1',version:1,status:'published',projectorKey:'example',definitionSha256:'a'.repeat(64),definition:fixture.definition,operation:'workflow.import',outputKind:'video'}
  const render=(readOnly:boolean,context?:any)=>renderToString(createSSRApp({render:()=>h(context?{...Component,setup(props:any,ctx:any){const state=Component.setup(props,ctx);state.context.value=context;return state}}:Component,{version,readOnly},{default:()=>h('input',{'data-original-author':'',value:'原作者尚未保存 🙂'})})}))
  return {vite,fixture,version,render}
}

test('readonly overview is semantic, lazy and immutable; draft preserves the original author',async t=>{
  const {version,render}=await setup(t),before=JSON.stringify(version)
  const html=await render(true)
  assert.match(html,/工作流解释方案/);assert.match(html,/方案声明的输入/);assert.match(html,/参数如何提供/);assert.match(html,/输出与使用方/)
  assert.doesNotMatch(html,/data-original-author|<input|<select|<textarea/)
  assert.match(html,/实现详情 · 节点、接线与完整配置（只读）/)
  assert.match(html,/未指定工作流/);assert.match(html,/否（false）/)
  assert.match(html,/>0<\/pre>/);assert.equal(JSON.stringify(version),before)
  const draft=await render(false);assert.match(draft,/data-original-author/);assert.match(draft,/原作者尚未保存/)
  assert.doesNotMatch(draft,/工作流解释方案|公共资源只读详情/)
})

test('missing declarations are not reported as empty inputs; fixed false and zero survive',async t=>{
  const {version,render}=await setup(t)
  delete (version.definition as any).asset_slots;delete (version.definition as any).parameter_policies
  let html=await render(true);assert.match(html,/未返回素材槽声明/);assert.match(html,/未返回参数规则/)
  ;(version.definition as any).asset_slots=[null]
  html=await render(true);assert.match(html,/条目格式异常/);assert.doesNotMatch(html,/此版本未声明素材槽/)
  version.definition.parameter_policies=[{semantic_key:'custom.flag',policy:'fixed',value:false},{semantic_key:'custom.zero',policy:'fixed',value:0}]
  html=await render(true);assert.match(html,/custom.flag/);assert.match(html,/否（false）/);assert.match(html,/custom.zero/);assert.match(html,/>0<\/pre>/)
})

test('verified workflow links exact model; context errors retain standalone definition without false model link',async t=>{
  const {render}=await setup(t)
  const ready={state:'ready',model:{id:'model-old',profileKey:'同名模型',version:2},workflow:{id:'workflow-old',templateName:'独立工作流',version:4,outputKind:'video',workflowInputSchemaError:'完整输入说明暂缺',inputContract:{classified_references:{groups:{videos:{min_items:0,max_items:3}}}}}}
  const html=await render(true,ready)
  assert.match(html,/独立工作流 v4/);assert.match(html,/查看绑定模型能力 · v2/);assert.match(html,/不是 Projector 的独占依赖/)
  assert.match(html,/完整输入说明暂缺/)
  assert.match(html,/0–3 份/)
  const failure=await render(true,{state:'error',message:'冻结 Projector SHA 不一致'})
  assert.match(failure,/重新核对工作流上下文/);assert.match(failure,/方案声明的输入/);assert.doesNotMatch(failure,/查看绑定模型能力/)
  const loading=await render(true,{state:'loading'});assert.match(loading,/正在核对工作流冻结引用/)
})

test('advanced author consumes supplied frozen context without another context read',async t=>{
  const {vite,fixture,version}=await setup(t)
  const {adaptWorkflowProjectorOptions}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const {default:Editor}=await vite.ssrLoadModule('/src/components/WorkflowProjectorDefinitionEditor.vue')
  const html=await renderToString(createSSRApp({render:()=>h(Editor,{modelValue:fixture.definition,options:adaptWorkflowProjectorOptions(fixture.options),engineKey:fixture.engineKey,engineVersion:1,readOnly:true,projectorVersion:version,workflowContextId:'00000000-0000-4000-8000-000000000001',workflowContext:{state:'error',message:'共享冻结上下文失败'}})}))
  assert.match(html,/共享冻结上下文失败/)
})
