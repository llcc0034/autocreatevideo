import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { readFileSync } from 'node:fs'
const root=fileURLToPath(new URL('..',import.meta.url))
test('field Smoke API uses exact profile identity and factual items without client-authored rules',async()=>{
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true}})
  try{
    const m=await vite.ssrLoadModule('/src/services/executionFieldSmoke.ts')
    const {api}=await vite.ssrLoadModule('/src/services/api.ts')
    const source={execution_instance_version_id:'leaf',definition_sha256:'a'.repeat(64),input_key:'fields',
      profile_version_id:'published-profile',profile_contract_sha256:'b'.repeat(64),
      rules:[{field_key:'direction',generation_instruction:'Describe only supplied facts',language_policy:'en',generation_context_selectors:[],max_chars:1000}]}
    const calls=[]
    api.get=async(...args)=>{calls.push(args);return {data:source}}
    api.post=async(...args)=>{calls.push(args);return {data:{...source,source:{kind:'literal',value:{items:[]}}}}}
    const read=await m.executionFieldSmokeApi.read('leaf','published-profile')
    const items=[{field_key:'direction',source_text:'A person waves.',source_language:'English',target_language:'English',context_facts:{},protected_tokens:[]}]
    const before=JSON.stringify(items)
    await m.executionFieldSmokeApi.build(read,items)
    assert.equal(calls.length,2);assert.equal(calls[0][0],'/model-execution-instance-versions/leaf/field-smoke-source/')
    assert.deepEqual(calls[0][1],{params:{profile_version_id:'published-profile'}})
    assert.deepEqual(calls[1][1],{profile_version_id:'published-profile',expected_definition_sha256:source.definition_sha256,expected_profile_contract_sha256:source.profile_contract_sha256,items})
    assert.equal(JSON.stringify(items),before);assert.equal(Object.hasOwn(calls[1][1],'rules'),false)
    assert.throws(()=>m.adaptFieldSmokeSource({...source,rules:[]}),/不完整/)
    assert.throws(()=>m.adaptFieldSmokeSource(source,true),/不完整/)
  }finally{await vite.close()}
})
test('field source changes invalidate generated fixture and creation remains separately authorized',()=>{
  const form=readFileSync(new URL('../src/components/ExecutionSmokeCreateForm.vue',import.meta.url),'utf8')
  const source=readFileSync(new URL('../src/components/ExecutionFieldSmokeSource.vue',import.meta.url),'utf8')
  const editor=readFileSync(new URL('../src/components/ExecutionRequestInputEditor.vue',import.meta.url),'utf8')
  assert.ok(source.includes("watch(fields,()=>emit('invalidated'),{deep:true})"))
  assert.ok(form.includes('@invalidated="invalidateFieldSource"'))
  assert.ok(form.includes('isFieldLeaf&&!fieldSourceReady'))
  assert.ok(form.includes('source.definition_sha256!==contract.value.sha256'))
  assert.ok(editor.includes('!contract.serverOwnedInputs.includes(spec.key)'))
  assert.equal(source.includes('executionRegistryApi.smoke'),false)
  assert.equal(source.includes('field_contract:'),false)
})
