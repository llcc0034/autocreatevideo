import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
const fields=[{key:'lighting',label:'光线',choices:['low_key','soft_diffuse']},{key:'preserve_natural_skin_exposure',label:'保持自然肤色曝光',choices:[true,false]}]
async function setup(context:any){const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});context.after(()=>vite.close());return vite}

test('allowed values accept only catalog subsets, not paths, empty lists or boolean aliases',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/outputAllowedValues.ts')
  assert.deepEqual(m.adaptOutputAllowedValueFields(fields),fields)
  for(const value of [undefined,[],[fields[0],fields[0]],[{...fields[0],choices:[1]}]])assert.throws(()=>m.adaptOutputAllowedValueFields(value))
  assert.deepEqual(m.outputAllowedValueErrors({},fields),[])
  assert.deepEqual(m.outputAllowedValueErrors({lighting:['low_key'],preserve_natural_skin_exposure:[false]},fields),[])
  for(const value of [{lighting:[]},{lighting:['bad']},{lighting:['low_key','low_key']},{'custom.path':['low_key']},{preserve_natural_skin_exposure:[0]},{preserve_natural_skin_exposure:['false']}])assert.ok(m.outputAllowedValueErrors(value,fields).length)
})

test('typed selection preserves false and invalid empty selections until explicitly corrected',async context=>{
  const vite=await setup(context),{default:Editor}=await vite.ssrLoadModule('/src/components/OutputAllowedValuesEditor.vue')
  const {createSSRApp,h,reactive}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const props=reactive({modelValue:{} as any,fields,disabled:false});let state:any
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{emit:(name:string,value:any)=>{if(name==='update:modelValue')props.modelValue=value},expose:()=>{}});return()=>h('div')}}))
  state.limit(fields[1],true);assert.deepEqual(props.modelValue.preserve_natural_skin_exposure,[true,false])
  state.select(fields[1],true,false);assert.deepEqual(props.modelValue.preserve_natural_skin_exposure,[false])
  state.select(fields[1],false,false);assert.deepEqual(props.modelValue.preserve_natural_skin_exposure,[])
  assert.equal(state.errors.value.length,1)
  state.limit(fields[1],false);assert.deepEqual(props.modelValue,{})
  props.disabled=true;state.limit(fields[0],true);assert.deepEqual(props.modelValue,{})
  const html=await renderToString(createSSRApp({render:()=>h(Editor,{...props,modelValue:{lighting:['low_key']}})}))
  assert.match(html,/fieldset disabled/);assert.match(html,/type="checkbox"/);assert.doesNotMatch(html,/type="text"/)
})
