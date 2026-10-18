import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {asyncMediaFixture as raw} from './helpers/asyncMediaFixture.ts'

async function setup(context:any){const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});context.after(()=>vite.close());return vite}
const key='video.text_to_video'
function specification(){return {schema_version:1,capabilities:[key],other:{keep:false},async_media:{schema_version:1,operations:{[key]:{
  output_kind:'video',request_schema:{type:'object',additionalProperties:false,required:['prompt','width'],properties:{
    prompt:{type:'string',minLength:1,maxLength:100},width:{type:'integer',minimum:64,maximum:512},seed:{type:'integer',enum:[0,1]},audio:{type:'boolean'},
  }},smoke_inputs:{prompt:'example',width:64,seed:0,audio:false},
}}}}}

test('catalog fails closed; exact types, domains and required Smoke samples',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelAsyncMedia.ts'),options=m.adaptAsyncMediaOptions(raw),spec=specification()
  assert.deepEqual(m.asyncMediaErrors(spec,options,true),[])
  assert.deepEqual(m.asyncMediaErrors({capabilities:[key]},null,false),[])
  assert.ok(m.asyncMediaErrors(spec,null,true).length)
  for(const bad of [null,{}, {...raw,fields:[]},{...raw,fields:[...raw.fields,raw.fields[0]]},{...raw,operations:[...raw.operations,raw.operations[0]]},{...raw,fields:[{key:'prompt',label:'Prompt',types:['array'],source_description:'unsupported'}]}])assert.throws(()=>m.adaptAsyncMediaOptions(bad))
  for(const invalid of [{type:'integer',minimum:true},{type:'integer',minimum:100,maximum:64},{type:'integer',minimum:1.5},{type:'integer',minimum:Infinity},{type:'integer',enum:[]},{type:'integer',enum:[64,64]},{type:'integer',enum:[false,64]},{type:'integer',minLength:1},{type:'integer',default:64},{type:'string'}]){
    const broken:any=specification();broken.async_media.operations[key].request_schema.properties.width=invalid;assert.ok(m.asyncMediaErrors(broken,options,true).length,JSON.stringify(invalid))
  }
  const broken:any=specification();broken.async_media.operations[key].smoke_inputs.seed=false;assert.ok(m.asyncMediaErrors(broken,options,true).length)
  broken.async_media.operations[key].smoke_inputs.seed=0;broken.async_media.operations[key].request_schema.required=[{}];assert.ok(m.asyncMediaErrors(broken,options,true).length)
  const unicode:any=specification();unicode.async_media.operations[key].request_schema.properties.prompt={type:'string',maxLength:1};unicode.async_media.operations[key].smoke_inputs.prompt='🎬';assert.deepEqual(m.asyncMediaErrors(unicode,options,true),[])
  const fresh=m.newAsyncOperation('video');assert.equal(fresh.request_schema.properties.prompt.maxLength,undefined);assert.deepEqual(fresh.smoke_inputs,{})
  m.assertAsyncMediaUnchanged(spec,{...spec,other:{keep:true}})
  assert.throws(()=>m.assertAsyncMediaUnchanged(spec,{...spec,async_media:{}}),/固定表单/)
})

test('fixed form edits the same specification, preserves zero/false and blocks readonly writes',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelAsyncMedia.ts'),{default:Editor}=await vite.ssrLoadModule('/src/components/ModelAsyncMediaEditor.vue')
  const {createSSRApp,h,reactive,nextTick}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const props:any=reactive({modelValue:specification(),options:m.adaptAsyncMediaOptions(raw),required:true,requestFieldMap:{prompt:'text',width:'w'},disabled:false})
  let state:any,updates=0
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(event:string,value:any)=>{if(event==='update:modelValue'){props.modelValue=value;updates++}}});return()=>h('div')}}))
  assert.equal(updates,0)
  state.editRule(key,'width','minimum',128);await nextTick();assert.ok(state.errors.value.some((error:string)=>error.includes('Smoke')))
  state.setSmoke(key,'width',128);await nextTick();assert.deepEqual(state.errors.value,[])
  state.setSmoke(key,'audio',false);state.setSmoke(key,'seed',0);assert.equal(props.modelValue.async_media.operations[key].smoke_inputs.audio,false);assert.equal(props.modelValue.async_media.operations[key].smoke_inputs.seed,0)
  state.setSmoke(key,'width',undefined);await nextTick();assert.ok(state.errors.value.length);state.setSmoke(key,'width',128)
  state.selected.value[key]='fps';state.add(key);assert.deepEqual(props.modelValue.async_media.operations[key].request_schema.properties.fps,{})
  state.editRule(key,'fps','type','number');state.editRule(key,'fps','minimum',23.976);state.setSmoke(key,'fps',23.976)
  state.enumValue(key,'seed',1,2);assert.deepEqual(props.modelValue.async_media.operations[key].request_schema.properties.seed.enum,[0,2])
  state.setRequired(key,'fps',true);assert.ok(props.modelValue.async_media.operations[key].request_schema.required.includes('fps'))
  assert.deepEqual(props.modelValue.other,{keep:false})
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}));assert.match(html,/仅测试材料/);assert.match(html,/远程字段/);assert.match(html,/text/);assert.match(html,/只读/)
  const count=updates;props.disabled=true;state.editRule(key,'width','maximum',256);state.setSmoke(key,'audio',true);state.initialize(key);assert.equal(updates,count)
  props.disabled=false;props.options=null;state.setSmoke(key,'audio',true);assert.equal(updates,count)
})

test('scalar control preserves empty string, false and zero; enum indexes resolve exact values',async context=>{
  const vite=await setup(context),{default:Editor}=await vite.ssrLoadModule('/src/components/AsyncScalarInput.vue')
  const {createSSRApp,h,reactive}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  let state:any,values:unknown[]=[];const props:any=reactive({modelValue:undefined,type:'integer',label:'fixture',disabled:false})
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(_event:string,value:any)=>values.push(value)});return()=>h('div')}}))
  state.update('0');state.update('');props.type='boolean';state.update('false');props.type='string';state.update('');props.choices=[false,true];state.update('0');assert.deepEqual(values,[0,undefined,false,'',false])
  props.disabled=true;state.update('1');assert.equal(values.length,5)
})
