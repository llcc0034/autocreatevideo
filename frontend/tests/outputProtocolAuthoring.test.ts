import test from 'node:test'
import assert from 'node:assert/strict'
import {createServer} from 'vite'
import {createSSRApp,h,reactive} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {visualRulesOutputRaw} from './visualRulesOutputFixture.ts'
async function setup(t:any,component:string,props:any){
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});t.after(()=>vite.close())
 const {default:Editor}=await vite.ssrLoadModule('/src/components/'+component+'.vue');let state:any;const events:any[]=[]
 await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose(){},emit:(event:string,value:any)=>{events.push([event,value]);if(event==='update:modelValue')props.modelValue=value}});return()=>h('div')}}))
 return{vite,state,events,Editor}
}
test('quality suggestions inline clear cancels unchanged, guards readonly and preserves unfinished clause',async t=>{
 const props=reactive({modelValue:{schema_version:1,negative_constraints_max_items:0} as any,options:null as any,disabled:false})
 const {vite,state,events}=await setup(t,'ModelVisualRulesOutputEditor',props)
 props.options=(await vite.ssrLoadModule('/src/services/visualRulesOutput.ts')).adaptVisualRulesOutputOptions(visualRulesOutputRaw)
 state.clauseDraft.value='identity drift';assert.ok(state.errors.value.some((s:string)=>s.includes('尚未添加')))
 state.clear();assert.equal(state.clearOpen.value,true);state.number('negative_constraints_max_items','2');assert.equal(events.filter(e=>e[0]==='update:modelValue').length,0)
 state.clearOpen.value=false;assert.equal(props.modelValue.negative_constraints_max_items,0);assert.equal(state.clauseDraft.value,'identity drift')
 state.addClause();assert.equal(state.errors.value.length,0);state.clear();props.disabled=true;state.confirmClear();assert.ok(props.modelValue)
 props.disabled=false;state.confirmClear();assert.equal(props.modelValue,undefined);assert.equal(state.clearOpen.value,false)
})
test('format example replacement is local explicit and blocks preview until resolved',async t=>{
 const props=reactive({options:{} as any,disabled:false})
 const {vite,state}=await setup(t,'VisualRulesOutputPreview',props)
 props.options=(await vite.ssrLoadModule('/src/services/visualRulesOutput.ts')).adaptVisualRulesOutputOptions(visualRulesOutputRaw)
 const {api}=await vite.ssrLoadModule('/src/services/api.ts');let calls=0;api.post=async()=>{calls++;throw new Error('unexpected')}
 state.responseText.value='原中文 🟢';state.example();assert.equal(state.replaceOpen.value,true);assert.equal(state.responseText.value,'原中文 🟢')
 await state.validateSample();assert.equal(calls,0);state.replaceOpen.value=false;assert.equal(state.responseText.value,'原中文 🟢')
 state.example();props.disabled=true;state.applyExample();assert.equal(state.responseText.value,'原中文 🟢')
 props.disabled=false;state.applyExample();assert.deepEqual(JSON.parse(state.responseText.value),visualRulesOutputRaw.response_example);assert.equal(calls,0)
})
test('semantic overview uses catalog titles and exact mapping without evaluating or rewriting source values',async t=>{
 const original={schema_version:1,node:{kind:'object',fields:{custom:{kind:'each',source:{kind:'path',scope:'root',path:['world','cast']},item:{kind:'path',scope:'item',path:[]}},extra:{kind:'unknown',value:false}}}}
 const props=reactive({modelValue:structuredClone(original),structureId:'',version:null})
 const {vite,events}=await setup(t,'ScriptSemanticOutputEditor',props)
 const {scriptSemanticGuideGroups}=await vite.ssrLoadModule('/src/services/scriptSemanticGuide.ts')
 const groups=scriptSemanticGuideGroups({json_schema:{properties:{custom:{title:'自定义实体清单',type:'array',items:{type:'object',properties:{name:{type:'string',title:'显示名称'}}}},extra:{type:'boolean'}}},mapping:{nodes:[]}},props.modelValue)
 assert.equal(groups[0].label,'自定义实体清单');assert.equal(groups[0].rows[0].source,'（响应.world.cast）[*].name')
 assert.match(groups[1].rows[0].source,/目录未声明/)
 assert.deepEqual(props.modelValue,original);assert.equal(events.filter(e=>e[0]==='update:modelValue').length,0)
})
