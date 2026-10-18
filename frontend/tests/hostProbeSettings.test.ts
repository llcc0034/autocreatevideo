import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createSSRApp,h} from 'vue'
import {renderToString} from 'vue/server-renderer'

test('probe modes use catalog labels in a single select, not a parallel JSON editor', async()=>{
  const vite=await createServer({logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false},optimizeDeps:{noDiscovery:true}})
  try{
    const {default:Editor}=await vite.ssrLoadModule('/src/components/ConfigValueEditor.vue')
    const {configEnumLabel,parseConfigValue}=await vite.ssrLoadModule('/src/services/configValueSchema.ts')
    const definition={key:'runtime.host_probe_mode',description:'主机探测模式',valueType:'string',unit:'mode',valueSchema:{enum:['automatic','manual'],enum_labels:{automatic:'自动探测',manual:'手动探测'}}}
    const html=await renderToString(createSSRApp({render:()=>h(Editor,{definition,modelValue:'"manual"'})}))
    assert.match(html,/手动探测/)
    assert.match(html,/自动探测/)
    assert.equal((html.match(/<select/g)||[]).length,1)
    assert.doesNotMatch(html,/<textarea|<input/)
    assert.match(html,/aria-label="主机探测模式"/)
    assert.equal(configEnumLabel(definition,'manual'),'手动探测')
    assert.equal(configEnumLabel({...definition,valueSchema:{enum:['manual']}},'manual'),'manual')
    assert.ok(parseConfigValue(definition,'"off"').error)
    const ttl={...definition,key:'runtime.capability_ttl_seconds',valueType:'integer',valueSchema:{minimum:30,maximum:31536000}}
    assert.equal(parseConfigValue(ttl,'2592000').error,'')
    assert.ok(parseConfigValue(ttl,'0').error)
  }finally{await vite.close()}
})
