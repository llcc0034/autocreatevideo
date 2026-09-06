import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('template editor explains server-owned facts without injecting examples into a strategy', async context => {
  const vite = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const {default:Editor} = await vite.ssrLoadModule('/src/components/PromptRenderingTemplateEditor.vue')
  const item = {key:'reference',label:'参考素材声明',placeholders:['label','subject'],description:'每个素材一段。',assembly:'参考主体定义',requirement:'保留 {label}。',
    placeholderHelp:{label:{label:'素材标签',description:'绑定中的精确标签。',example:'<Picture 1>'},subject:{label:'主体标识',description:'主体绑定。',example:'<Subject 1>'}},
    example:{template:'{label}: {subject}',result:'<Picture 1>: <Subject 1>'}}
  const props = reactive({item,modelValue:'original',disabled:false})
  let state:any
  const updates:string[] = []
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(_key:string,value:string)=>updates.push(value)});return()=>h('div')}}))
  const render = (p:any=props)=>renderToString(createSSRApp({render:()=>h(Editor,p)}))
  const html = await render()
  for (const text of ['插入素材标签 {label}','绑定中的精确标签','&lt;Picture 1&gt;','不是当前模板预览','不会填入配置','普通句子','最终提示词预览']) assert.ok(html.includes(text),text)
  assert.match(html,/>original<\/textarea>/)
  assert.equal(updates.length,0)
  assert.doesNotMatch(html,/<Picture 1>/)
  let focus=0,selection:any
  state.textarea.value={selectionStart:1,selectionEnd:3,focus:()=>focus++,setSelectionRange:(...args:any[])=>selection=args}
  await state.insert('label')
  assert.deepEqual(updates,['o{label}ginal'])
  assert.equal(focus,1);assert.deepEqual(selection,[8,8])
  await state.insert('not_allowed'); assert.equal(updates.length,1)
  props.disabled=true
  await state.insert('subject'); assert.equal(updates.length,1)
  assert.match(await render(),/disabled/)
  props.modelValue='changed'
  assert.match(await render(),/>changed<\/textarea>/)
  const empty = await render({...props,item:{...item,key:'empty',placeholders:[],placeholderHelp:{}}})
  assert.match(empty,/没有可插入的占位符/)
  const source = await readFile(new URL('../src/components/PromptRenderingTemplateEditor.vue',import.meta.url),'utf8')
  assert.doesNotMatch(source,/platformApi|fetch\(|\.replace\(|v-html/)
  const parent = await readFile(new URL('../src/components/PromptProfileSettings.vue',import.meta.url),'utf8')
  assert.match(parent,/<PromptRenderingTemplateEditor/)
  assert.match(parent,/字段组合规则 → 正向提示词/)
  assert.doesNotMatch(parent,/点击占位符追加到模板/)
})
