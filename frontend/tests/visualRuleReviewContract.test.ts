import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { visualRuleReviewFixture } from './visualRulesOutputFixture.ts'

test('review fields and candidates require the original complete contract, never inferred defaults', async t => {
  const vite = await createServer({ root:fileURLToPath(new URL('..',import.meta.url)), logLevel:'silent', appType:'custom',server:{middlewareMode:true} })
  t.after(()=>vite.close())
  const {readVisualRuleReviewContract:read} = await vite.ssrLoadModule('/src/services/visualRuleReviewContract.ts')
  const contract=visualRuleReviewFixture()
  contract.fields[0]!.label='Frozen custom label';contract.fields[0]!.response_path='rules.0.summary'
  assert.deepEqual(read(contract),contract)
  for(const mutate of [(v:any)=>{v.schema_version=2},(v:any)=>{v.output.sha256='invalid'},(v:any)=>{v.output.version=0},
    (v:any)=>{v.fields.push(v.fields[0])},(v:any)=>{v.fields[0].key='constructor'},(v:any)=>{v.fields[0].allow_blank=null},
    (v:any)=>{v.fields[0].value_type='object'},(v:any)=>{v.fields[0].source_selector='foreign.source'}]) {
    const value=structuredClone(contract);mutate(value);assert.throws(()=>read(value))
  }
  const {readTextCandidateDetail:candidate} = await vite.ssrLoadModule('/src/services/productionTextCandidates.ts')
  const row:any={productionRun:'run',stageRun:'source',status:'available',sha256:'a'.repeat(64),manifestMetadataSha256:'b'.repeat(64),textCandidate:{
    operation:'visual_rules.generate',warnings:[],review_contract:contract,
    candidate:{negative_prompt:'',color_script:'Muted palette.',continuity_summary:'Keep continuity.'}}}
  assert.equal(candidate(row,'run','source').reviewContract.fields[0].label,'Frozen custom label')
  const extended=structuredClone(row)
  extended.textCandidate.review_contract.fields.push({key:'camera_guidance',label:'已审核镜头指导',value_type:'text',allow_blank:false,
    source_selector:'reviewed_visual_rules.camera_guidance',response_path:'rules.3',response_format:'camera_guidance',transformation:'Keep reviewed camera guidance.'})
  extended.textCandidate.candidate.camera_guidance='Keep eye-level framing.'
  assert.equal(candidate(extended,'run','source').reviewContract.fields.length,4)
  delete extended.textCandidate.candidate.camera_guidance
  assert.throws(()=>candidate(extended,'run','source'))
  for(const mutate of [(v:any)=>{delete v.manifestMetadataSha256},(v:any)=>{delete v.textCandidate.review_contract},(v:any)=>{delete v.textCandidate.candidate.color_script},
    (v:any)=>{v.textCandidate.candidate.extra='undeclared'},(v:any)=>{v.textCandidate.candidate.color_script=''},
    (v:any)=>{v.textCandidate.candidate.color_script=[]}]) {
    const value=structuredClone(row);mutate(value);assert.throws(()=>candidate(value,'run','source'))
  }
  row.textCandidate.review_contract=null;row.textCandidate.adoption_block_reason='Historical read-only result'
  assert.equal(candidate(row,'run','source').reviewContract,null)
  assert.equal(candidate(row,'run','source').adoptionBlockReason,'Historical read-only result')
})
