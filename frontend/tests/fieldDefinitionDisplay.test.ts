import test from 'node:test'
import assert from 'node:assert/strict'
import { fieldDefinitionDisplay } from '../src/services/fieldDefinitionDisplay.ts'

const row = (version:number, status='published', label='色彩设计'):any => ({id:`version-${version}`,field_definition:'field',version,status,label})
test('field directory shows a versioned Chinese label without changing the bound version', () => {
  const versions=[row(1),row(3,'draft','新名称'),row(2,'published','色彩脚本')]
  const original=JSON.stringify(versions), result=fieldDefinitionDisplay('field',versions)
  assert.equal(result?.label,'色彩脚本');assert.equal(result?.version,2);assert.equal(result?.chinese,true)
  assert.equal(JSON.stringify(versions),original)
  assert.equal(fieldDefinitionDisplay('field',[]) ,null)
  assert.equal(fieldDefinitionDisplay('field',[row(1,'draft','Color')])?.chinese,false)
  assert.equal(fieldDefinitionDisplay('field',[row(2,'retired','')])?.basis,'尚无可用定义')
  for(const versions of [[{...row(1),field_definition:'other'}],[row(1),row(1)],[row(0)],[row(1,'invalid')]])assert.throws(()=>fieldDefinitionDisplay('field',versions))
})
