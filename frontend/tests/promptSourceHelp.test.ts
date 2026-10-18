import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('source help connects page facts, source and dictionary field without mutating configuration', async context => {
  const vite = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(() => vite.close())
  const {default: Help} = await vite.ssrLoadModule('/src/components/PromptSourceHelp.vue')
  const sources = [{value:'request.theme',label:'请求主题',groupLabel:'本次请求输入',description:'确认填入后才引用项目内容',recordLocation:'创建执行准备 → 一句话梗概',executionUsage:'显式接线，不自动读取最新结果'}]
  const props = {selector:'request.theme',sources,fieldKey:'custom_theme',fieldLabel:'故事目标',channel:'正向提示',generationMode:'passthrough'}
  const before = JSON.stringify(props)
  const render = async (value = props) => (await renderToString(createSSRApp({render:()=>h(Help,value)}))).replace(/<!--[\s\S]*?-->/g, '')
  const html = await render()
  for (const text of ['请求主题 → 故事目标（custom_theme）','正向提示','创建执行准备 → 一句话梗概','显式接线','确认填入后才引用']) assert.ok(html.includes(text),text)
  assert.equal(JSON.stringify(props),before)
  assert.doesNotMatch(html,/<(?:input|select|textarea|button)\b/)
  assert.match(await render({...props,selector:'unknown'}),/不据名称推断实际取值/)
  assert.match(await render({...props,generationMode:'model_generated'}),/先生成或翻译字段/)
  assert.match(await render({...props,generationMode:'model_generated'}),/已采用的辅助生成／翻译结果/)
  assert.match(await render({...props,generationMode:'model_generated'}),/生成结果用途尚未明确/)
  assert.match(await render({...props,generationMode:'translate_only',generationOutputSemantics:'contribution'} as any),/原语合成/)
  assert.match(await render({...props,generationMode:'translate_only',generationOutputSemantics:'final'} as any),/直接作为最终字段值排版/)
  assert.doesNotMatch(await render({...props,generationMode:'translate_only'}),/请求主题 → 故事目标/)
  assert.doesNotMatch(html,/先生成或翻译字段/)
  const escaped = await render({...props,fieldLabel:'<script>unsafe</script>'})
  assert.doesNotMatch(escaped,/<script>/)

  const {default: Settings} = await vite.ssrLoadModule('/src/components/PromptProfileSettings.vue')
  let state: any
  await renderToString(createSSRApp({setup(){state=Settings.setup({}, {expose(){}});return()=>h('div')}}))
  state.fields.value = [{id:'field-id',key:'theme',label:'主题'}]
  state.profiles.value = [{id:'profile-1',name:'策略一',version:1,status:'published'},{id:'profile-2',name:'策略二',version:2,status:'draft'}]
  state.rules.value = [
    {id:'a',fieldDefinition:'field-id',fieldKey:'theme',profileVersion:'profile-1',sourceSelector:'request.theme'},
    {id:'b',fieldDefinition:'field-id',fieldKey:'theme',profileVersion:'profile-2',sourceSelector:'project.logline'},
    {id:'c',fieldDefinition:'different-id',fieldKey:'theme',profileVersion:'profile-2',sourceSelector:'fixed.value'},
  ]
  state.openConfiguration(state.profiles.value[1], 'b')
  assert.equal(state.configurationSnapshot.value.profile.id,'profile-2')
  assert.equal(state.configurationSnapshot.value.focusRuleId,'b')
  assert.deepEqual(state.configurationSnapshot.value.rules.map((row:any)=>row.id),['b','c'],'snapshot follows exact profile identity, not a shared field name')
  state.rules.value[1].sourceSelector='request.text'
  assert.equal(state.configurationSnapshot.value.rules[0].sourceSelector,'project.logline','later editor changes do not mutate the readonly snapshot')
  state.options.value.sourceSelectors = sources
  state.sourceSearch.value = '一句话梗概'
  assert.equal(state.sourceMatches.value.length,1)
  state.sourceSearch.value = 'does-not-exist'
  assert.equal(state.sourceMatches.value.length,0)
})
