import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createSSRApp, h, reactive} from 'vue'
import {renderToString} from '@vue/server-renderer'

test('deployment author explains stored sources without resolving or rewriting them', async t => {
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  t.after(()=>vite.close())
  const {deploymentParameterSource:source}=await vite.ssrLoadModule('/src/services/deploymentParameterSource.ts')
  const {default:View}=await vite.ssrLoadModule('/src/components/DeploymentParameterSource.vue')
  const {default:Editor}=await vite.ssrLoadModule('/src/components/RegistryResourceEditor.vue')
  const {resourceEditorFields,buildRegistryResourcePayload}=await vite.ssrLoadModule('/src/services/registryResourceSchema.ts')
  const provider={id:'p',key:'connection',name:'原连接',driverType:'comfyui',driverConfig:{base_url:'http://example.invalid',timeout_seconds:30,pre_submit_resource_release:{schema_version:1,enabled:true,unload_models:true,free_memory:true}},isActive:true}
  const profile={id:'v1',profileKey:'media',version:1,status:'published',provider:'p',modality:'image',specification:{capabilities:['image.generate']}}
  const deps={providers:[provider],modelProfiles:[profile],deployments:[],runtimeHosts:[],workflowVersions:[]}
  const payload={profile_version:'v1',runtime_config:{},priority:0,is_default:false}
  const field=(path='timeout_seconds',kind='number')=>({path:'runtime_config.'+path,label:'参数',kind,description:'',impact:''})
  await t.test('missing, false, zero, null and empty declarations are distinct',()=>{
    assert.equal(source(field(),payload,deps).state,'本部署未填写')
    for(const [value,label] of [[false,'否（false）'],[0,'0'],[null,'空值（null）'],['','空文本']]){
      const result=source(field(),{...payload,runtime_config:{timeout_seconds:value}},deps)
      assert.equal(result.state,'本部署已填写');assert.equal(result.own,label);assert.equal(result.declared,'30')
    }
  })
  await t.test('a partial nested override does not imply per-leaf inheritance',()=>{
    const result=source(field('pre_submit_resource_release.enabled','boolean'),{...payload,runtime_config:{pre_submit_resource_release:{schema_version:1}}},deps)
    assert.equal(result.state,'本部署已填写此参数组，本项未填写');assert.equal(result.declared,'是（true）');assert.equal(result.grouped,true)
  })
  await t.test('uses the exact model and provider IDs, with no latest fallback or missing-data success',()=>{
    const other={...provider,id:'p2',name:'新连接',driverConfig:{timeout_seconds:90}}
    const catalog={...deps,providers:[provider,other],modelProfiles:[profile,{...profile,id:'v2',version:2,provider:'p2'}]}
    assert.equal(source(field(),payload,catalog).declared,'30')
    assert.equal(source(field(),{...payload,profile_version:'v2'},catalog).declared,'90')
    assert.equal(source(field(),{...payload,profile_version:'absent'},catalog).declared,'无法核对')
    assert.equal(source(field(),payload,{...deps,providers:[]}).connection,'连接目录未找到此引用')
  })
  await t.test('does not render connection endpoints, map bodies, unknown extensions or credentials',async()=>{
    const input={...deps,providers:[{...provider,driverConfig:{base_url:'PRIVATE_ENDPOINT',request_field_map:{secret:'DO_NOT_RENDER'},api_key:'DO_NOT_RENDER'}}]}
    for(const item of [field('base_url','text'),field('request_field_map','mapping')]){
      const html=await renderToString(createSSRApp(View,{field:item,payload,dependencies:input}))
      assert.match(html,/已声明（在原维护入口查看）/);assert.doesNotMatch(html,/PRIVATE_ENDPOINT|DO_NOT_RENDER/)
    }
    assert.equal(source({path:'runtime_config',advanced:true},payload,input),null)
    assert.equal(source({path:'profile_version'},payload,input),null)
  })
  await t.test('reactive source changes replace the declaration and never mutate or materialize inherited values',async()=>{
    const props=reactive({field:field(),payload:structuredClone(payload),dependencies:deps})
    let state:any
    await renderToString(createSSRApp({setup(){state=View.setup(props,{expose(){}});return()=>h('div')}}))
    assert.equal(state.source.value.own,'未声明')
    props.payload.runtime_config={timeout_seconds:45};assert.equal(state.source.value.own,'45')
    props.payload.runtime_config={};assert.equal(state.source.value.own,'未声明')
    assert.deepEqual(buildRegistryResourcePayload('deployment',props.payload,deps),payload)
    assert.equal(provider.driverConfig.timeout_seconds,30)
  })
  await t.test('actual hierarchical editor hosts source disclosures once and preserves source JSON for all fields',async()=>{
    const current={...payload,key:'d',name:'部署',status:'active',runtime_host:null,runtime_config:{timeout_seconds:45,pre_submit_resource_release:{schema_version:1,enabled:false,unload_models:false,free_memory:false}}}
    const before=JSON.stringify({current,deps})
    const html=(await renderToString(createSSRApp(Editor,{resource:'deployment',modelValue:current,dependencies:deps,authoring:true}))).replace(/<!--[\s\S]*?-->/g,'')
    const expected=resourceEditorFields('deployment',current,deps).filter(f=>f.path.startsWith('runtime_config.')&&!f.advanced).length
    assert.equal(html.split('class="deployment-parameter-source"').length-1,expected)
    for(const word of ['本部署已填写：45','本部署已填写：否（false）','连接声明','整组覆盖','不是最终请求的生效值'])assert.ok(html.includes(word),word)
    assert.equal(JSON.stringify({current,deps}),before)
    assert.deepEqual(buildRegistryResourcePayload('deployment',current,deps),current)
    const flat=await renderToString(createSSRApp(Editor,{resource:'deployment',modelValue:current,dependencies:deps}))
    assert.doesNotMatch(flat,/class="deployment-parameter-source"/)
  })
})
