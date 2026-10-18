import assert from 'node:assert/strict'
import { unusedLanguageForm } from './fixtures/executionLanguage.ts'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'

const root = fileURLToPath(new URL('..', import.meta.url))
const sha = 'a'.repeat(64)
const input = { projectId: 'project', planSource: 'manual', idempotencyKey: 'explicit-request',
  allowExternalCalls: false, manualPlan: { stageCatalogKey: 'catalog', expectedStageCatalogSha256: sha,
    scope: { kind: 'project' }, delivery: { kind: 'episode_masters' }, initialContent: null } }
async function modules(fn) {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try { await fn(await vite.ssrLoadModule('/src/services/productionManual.ts'), vite) } finally { await vite.close() }
}

test('guide follows current execution instances and manual actions without removed selectors', () => modules(async (_m, vite) => {
  const { productionManual: guide } = await vite.ssrLoadModule('/src/content/productionManual.ts')
  const step = (id: string) => {
    const value = guide.steps.find(row => row.id === id)
    assert.ok(value, `missing guide step ${id}`)
    return JSON.stringify(value)
  }
  const asset = step('manage-asset-library'), creation = step('create-production-run'), onboarding = step('create-project')
  assert.match(onboarding, /先选制作方式，再选剧本来源/)
  assert.match(onboarding, /两种方式都会创建本项目的生产运行/)
  assert.match(onboarding, /继续本轮制作/)
  assert.match(onboarding, /规划要求和原稿时间原样传入本次请求/)
  assert.match(onboarding, /所选输出结构及语义映射核验/)
  assert.doesNotMatch(onboarding, /逐镜时长之和必须满足单集计划/)
  assert.doesNotMatch(onboarding, /手工模式只建立空项目|ProductionRun（如选择自动生产）|整集统一工作流/)
  const creative = step('review-script-bible'), review = step('handle-run-gates'), monitor = step('monitor-production-run')
  assert.match(asset, /精确执行实例版本/); assert.match(asset, /人工通道/); assert.match(asset, /同一请求/)
  assert.match(asset, /配方与独立生成共用原 Asset 和版本登记/)
  assert.match(asset, /产物状态不等于调用状态/)
  assert.match(asset, /查看本阶段原调用/)
  assert.match(asset, /不提供文本修订或自动重发/)
  assert.match(creation, /无配方人工制作/); assert.match(creation, /完整schema3发布\/创建须通过/)
  assert.match(creative, /参考素材绑定不计入创作采用摘要/)
  assert.match(review, /唯一主图/); assert.match(monitor, /装配完成仍不代表已交付/)
  assert.match(guide.subtitle, /实际服务切换和真实模型验收须另行确认/)
  const material = guide.incidents.find(row => row.id === 'storyboard-reference-selection')
  assert.ok(material)
  assert.match(JSON.stringify(material), /业务用途只是可选创作描述/)
  assert.match(JSON.stringify(material), /输入槽位与来源映射/)
  assert.match(JSON.stringify(material), /自动匹配没有唯一结果.*人工接管/)
  assert.match(JSON.stringify(material), /原对白、发声关系及毫秒区间不变/)
  assert.doesNotMatch(JSON.stringify(guide), /reference_bindings|ref2va_shot_bible_image_missing|ref2va_entity_image|unattended_existing_bible_requires_new_project|每个 Ref2VA Shot 至少/)
  const profiles = step('configure-prompt-profiles')
  assert.match(profiles, /每个策略组装实例只引用一个精确策略/)
  assert.match(profiles, /人工完整通道按原文提交/)
  assert.doesNotMatch(JSON.stringify(guide), /active Binding|默认 resolver|binding ID|完整分镜参考审核尚未接入|完整 schema3 发布\/创建仍在接线|PromptArtifact/)
  assert.doesNotMatch([asset,creation,creative,review,monitor].join('\n'), /workflow_selection|bible_image_prompt_profile_binding_id|Workflow selection|8 个模型候选|每个 Ref2VA Shot 至少/)
}))

test('script creation pages describe configured planning without retired model duration intersection', () => {
  for (const view of ['SeriesStudioView.vue', 'ProductionRunView.vue']) {
    const source = readFileSync(new URL(`../src/views/${view}`, import.meta.url), 'utf8')
    assert.match(source, /计划原样冻结，由配方字段映射传入所选实例/)
    assert.match(source, /实际返回按所选输出结构及语义映射核验/)
    assert.doesNotMatch(source, /时长策略求交|范围冲突会在调用模型前明确阻止/)
  }
})

test('manual creation has explicit blank contents, source and scope, without a hidden recipe or target', () => modules(async (m, vite) => {
  const p = m.buildManualProductionRunPayload(input)
  assert.equal(p.plan_source, 'manual'); assert.equal(p.manual_plan.initial_content, null)
  assert.equal('budget_currency' in p, false)
  assert.equal('budget_limit' in p, false)
  assert.equal('max_generation_tasks' in p, false)
  assert.deepEqual(p.manual_plan.scope, { kind: 'project' }); assert.equal(p.allow_external_calls, false)
  for (const key of ['recipe_version_id','model_deployment_id','workflow_selection','prompt_profile_binding_id','script_revision_id']) assert.equal(key in p, false)
  const { buildProductionRunRequestPayload } = await vite.ssrLoadModule('/src/services/productionRunPayload.ts')
  assert.deepEqual(buildProductionRunRequestPayload(input), p)
  const { planSource, ...missingSource } = input
  assert.throws(() => buildProductionRunRequestPayload(missingSource), /来源/)
}))

test('manual request rejects implicit all, unknown nested keys, and extra recipe fields', () => modules(m => {
  for (const scope of [{kind:'episodes',episodeIds:[]}, {kind:'episodes',episodeIds:['one','one']}, {kind:'project',episodeIds:['one']}, {kind:'episodes',episodeIds:['one'],route:{}}]) {
    assert.throws(() => m.buildManualProductionRunPayload({...input,manualPlan:{...input.manualPlan,scope}}))
  }
  assert.throws(() => m.buildManualProductionRunPayload({...input,recipeVersionId:'recipe'}))
  assert.throws(() => m.buildManualProductionRunPayload({...input,manualPlan:{...input.manualPlan,delivery:{kind:'episode_masters',fallback:true}}}))
  assert.throws(() => m.buildManualProductionRunPayload({...input,manualPlan:{...input.manualPlan,initialContent:undefined}}))
  const p = m.buildManualProductionRunPayload({...input,manualPlan:{...input.manualPlan,scope:{kind:'episodes',episodeIds:['z','a']}}})
  assert.deepEqual(p.manual_plan.scope.episode_ids,['a','z'])
}))

test('manual options preserve missing project facts as zero and reject incomplete data', () => modules(m => {
  const value = {project_id:'project',stage_catalog:{value:{key:'catalog',stages:[{key:'script_input'}]},schema_version:1,size_bytes:100,sha256:sha},
    scope_kinds:['project','episodes'],delivery_kinds:['episode_masters'],initial_content_kinds:['script_revision'],
    facts:{confirmed_script_revisions:0,episodes:0,shots:0}}
  assert.deepEqual(m.adaptProductionManualOptions(value).facts,value.facts)
  assert.throws(() => m.adaptProductionManualOptions({...value,facts:{episodes:0}}))
  assert.throws(() => m.adaptProductionManualOptions({...value,stage_catalog:{...value.stage_catalog,sha256:''}}))
}))

test('manual adoption uses the observed revision and exact script hash, never just a latest selector', () => modules(m => {
  const opts = m.adaptManualActionOptions({run_id:'run',run_plan_sha256:sha,
    execution_activity:[],content_change_stage_keys:[],script_adoption:{revision:0,decision_id:null,content:null},script_review:{revision:0,decision_id:null,content:null},visual_rules_review:{revision:0,decision_id:null,status:'not_reviewed',reasons:[]},current_script:null,current_script_identity:null,current_script_decision_id:null,actions:[{stage_key:'script_input',action:'adopt',operation:null,output_kinds:[],scope_kinds:['project']}]})
  const p = m.buildManualScriptAdoption({projectId:'project',options:opts,idempotencyKey:'one-adoption',revisionId:'revision',contentSha256:'b'.repeat(64),comment:''})
  assert.equal(p.expected_selection_revision,0); assert.equal(p.expected_run_plan_sha256,sha)
  assert.deepEqual(p.payload.content,{kind:'script_revision',id:'revision',expected_sha256:'b'.repeat(64)})
  assert.throws(() => m.buildManualScriptAdoption({projectId:'project',options:{...opts,actions:[]}}),/未开放/)
}))

test('current script is distinct from initial adoption and step review revisions are independent', () => modules(m => {
  const initial = {kind:'script_revision',id:'initial',sha256:sha,revision:1}
  const generated = {...initial,id:'generated',sha256:'b'.repeat(64)}
  const identity = {source_name:'',source_type_label:'AI 生成',source_format_label:'纯文本',decision_stage_key:'script_review',
    candidate:{artifact_id:'candidate',variant_index:1,stage_id:'source',stage_key:'script_generate',attempt_id:'attempt',preparation_id:'prep'}}
  const value = {run_id:'run',run_plan_sha256:sha,execution_activity:[],content_change_stage_keys:[],script_adoption:{revision:1,decision_id:'initial-decision',content:initial},
    script_review:{revision:1,decision_id:'review-decision',content:generated},visual_rules_review:{revision:0,decision_id:null,status:'not_reviewed',reasons:[]},current_script:generated,current_script_identity:identity,current_script_decision_id:'review-decision',
    actions:[{stage_key:'script_review',action:'approve',operation:null,output_kinds:[],scope_kinds:['project'],source_stage_keys:['script_generate']}]}
  const options = m.adaptManualActionOptions(value)
  const changedReview = {revision:1,decision_id:'rules-decision',status:'changed',reasons:['项目中的影视规则已修改']}
  assert.equal(m.adaptManualActionOptions({...value,visual_rules_review:changedReview}).visualRulesReview.status,'changed')
  for (const change of [{status:undefined},{reasons:[]},{status:'current'},{revision:0,decision_id:null}]) {
    assert.throws(()=>m.adaptManualActionOptions({...value,visual_rules_review:{...changedReview,...change}}),/影视规则/)
  }
  assert.equal(options.currentScript.id,'generated'); assert.equal(options.scriptAdoption.content.id,'initial')
  assert.equal(options.currentScriptDecisionId,'review-decision')
  assert.equal(options.currentScriptIdentity.candidate.artifactId,'candidate')
  for (const current_script_identity of [null,undefined,{...identity,candidate:null},{...identity,decision_stage_key:'script_input'}]) {
    assert.throws(()=>m.adaptManualActionOptions({...value,current_script_identity}),/身份|候选|采用/)
  }
  const changed = m.adaptManualActionOptions({...value,current_script:initial,current_script_decision_id:'initial-decision',
    current_script_identity:{...identity,decision_stage_key:'script_input',candidate:null}})
  assert.equal(changed.currentScript.id,'initial')
  assert.equal(changed.scriptReview.content.id,'generated')
  for (const current_script_decision_id of [null,undefined,'unknown']) {
    assert.throws(()=>m.adaptManualActionOptions({...value,current_script_decision_id}),/当前剧本/)
  }
  const input = m.buildManualTextApproval({stageKey:'script_review',projectId:'project',options,idempotencyKey:'review',artifactId:'candidate',
    artifactSha256:sha,manifestSha256:sha,comment:''})
  assert.equal(input.expected_selection_revision,1); assert.deepEqual(input.payload.expected_current_script,generated)
  assert.throws(()=>m.adaptManualActionOptions({...value,visual_rules_review:{revision:0,decision_id:null,status:'not_reviewed',reasons:[]},current_script:initial}),/当前剧本/)
  assert.throws(()=>m.adaptManualActionOptions({...value,script_review:undefined}),/剧本/)
  assert.throws(()=>m.adaptManualActionOptions({...value,actions:[{...value.actions[0],source_stage_keys:[]}]}),/来源步骤/)
}))

test('manual generation keeps exact input and instance; never inserts a route or approval', () => modules(async (m, vite) => {
  const opts = m.adaptManualActionOptions({run_id:'run',run_plan_sha256:sha,execution_activity:[],content_change_stage_keys:[],script_adoption:{revision:0,decision_id:null,content:null},script_review:{revision:0,decision_id:null,content:null},visual_rules_review:{revision:0,decision_id:null,status:'not_reviewed',reasons:[]},current_script:null,current_script_identity:null,current_script_decision_id:null,
    actions:[{stage_key:'script_generate',action:'generate',operation:'script.generate',output_kinds:[],scope_kinds:['project']}]})
  const form = {languageForm:unusedLanguageForm(),versionId:'version',sha256:sha,operation:'script.generate',mode:'',outputKind:'script',workflowVersionId:null,promptMode:'manual',inputs:[],parameters:[],
    channels:[{key:'positive_prompt',rule:{type:'string',required:true}}],serverOwnedInputs:[],contextFields:[],sourceConstraints:{},purposes:{}}
  const draft = {literals:{},contexts:{},media:{},parameters:{},channels:{positive_prompt:'  人工确定的内容\n'},subjects:[],sourceLanguage:'',targetLanguage:''}
  const input = {projectId:'project',target:{scope:{kind:'project',id:'project'},unitKey:'project:project',label:'本项目'},options:opts,stageKey:'script_generate',idempotencyKey:'same-key',contract:form,draft}
  const result = m.buildManualGeneration(input)
  assert.equal(result.payload.manual_channels.positive_prompt,draft.channels.positive_prompt)
  assert.deepEqual(result.payload.parameters,{})
  assert.equal(result.expected_selection_revision,null)
  assert.equal(result.unit_key,'project:project')
  assert.equal(result.payload.execution_instance_version_id,'version')
  for (const key of ['field_values','approved','route_index','model_deployment_id','workflow_version_id']) assert.equal(key in result.payload,false)
  assert.deepEqual(m.buildManualGeneration(input),result)
  assert.throws(()=>m.buildManualGeneration({...input,contract:{...form,operation:'video.generate'}}),/不一致/)
  assert.throws(()=>m.buildManualGeneration({...input,draft:{...draft,contexts:{body:{projectId:'other'}}}}),/合同之外/)
  const optionalForm={...form,inputs:[{key:'optional_rule',kind:'text',required:false,min_items:0,max_items:1,
    semantic_contract:{allowed_scope_kinds:['project']}}],
    contextFields:[{selector_key:'reviewed_visual_rules.negative_constraints',kind:'text',scope_kind:'project'}],
    sourceConstraints:{optional_rule:{kinds:['context_field'],selectors:['reviewed_visual_rules.negative_constraints']}}}
  const optionalDraft={...draft,literals:{optional_rule:{enabled:false,value:''}},
    contexts:{optional_rule:{selectorKey:'reviewed_visual_rules.negative_constraints',projectId:'',revisionId:''}}}
  const optionalInput={...input,contract:optionalForm,draft:optionalDraft}
  assert.deepEqual(m.buildManualGeneration(optionalInput).payload.inputs,{})
  optionalDraft.literals.optional_rule.enabled=true
  assert.throws(()=>m.buildManualGeneration(optionalInput),/需要完整/)
  optionalDraft.contexts.optional_rule.projectId='other';optionalDraft.contexts.optional_rule.revisionId='review'
  assert.throws(()=>m.buildManualGeneration(optionalInput),/本轮项目/)
  optionalDraft.contexts.optional_rule.projectId='project'
  assert.equal(m.buildManualGeneration(optionalInput).payload.inputs.optional_rule.scope.id,'project')
  const component = (await vite.ssrLoadModule('/src/components/ManualGenerationPreparation.vue')).default
  const html = await renderToString(createSSRApp({render:()=>h(component,{run:{id:'run',project:'project'},options:opts})}))
  assert.match(html,/创建只保存本次准备/)
  assert.match(html,/制作步骤/)
  assert.doesNotMatch(html, /工作流版本<select|提示词策略<select/)
}))

test('manual action options and result require an actual operation and preparation identity', () => modules(m => {
  const base = {run_id:'run',run_plan_sha256:sha,execution_activity:[],content_change_stage_keys:[],script_adoption:{revision:0,decision_id:null,content:null},script_review:{revision:0,decision_id:null,content:null},visual_rules_review:{revision:0,decision_id:null,status:'not_reviewed',reasons:[]},current_script:null,current_script_identity:null,current_script_decision_id:null}
  for (const action of [{stage_key:'script_generate',action:'generate'}, {stage_key:'script_input',action:'adopt'},
    {stage_key:'script_generate',action:'generate',operation:null}]) assert.throws(()=>m.adaptManualActionOptions({...base,actions:[action]}),/能力目录/)
  const target = '12345678-1234-4234-8234-123456789abc'
  assert.equal(m.manualGenerationTarget({action:'generate',result_kind:'preparation',target_id:target}),target)
  for (const result of [{action:'generate',result_kind:'preparation',target_id:''}, {action:'generate',result_kind:'approval_decision',target_id:target},{}]) {
    assert.throws(()=>m.manualGenerationTarget(result),/同一请求/)
  }
}))

test('manual media uses explicit paginated targets and frozen output type, never an operation-name guess', () => modules(m => {
  const id = '12345678-1234-4234-8234-123456789abc'
  const raw = {run_id:'run',run_plan_sha256:sha,count:2,next:'/next',previous:null,
    results:[{scope:{kind:'shot',id},unit_key:`shot:${id}`,label:'第1集 / 镜头1'}]}
  const page = m.adaptManualTargetPage(raw)
  assert.equal(page.count,2); assert.equal(page.next,'/next'); assert.equal(page.results.length,1)
  for (const bad of [{...raw,next:undefined}, {...raw,count:0}, {...raw,results:[raw.results[0],raw.results[0]]},
    {...raw,results:[{...raw.results[0],unit_key:'guessed-shot'}]}]) assert.throws(()=>m.adaptManualTargetPage(bad))
  const options = m.adaptManualActionOptions({run_id:'run',run_plan_sha256:sha,execution_activity:[],content_change_stage_keys:[],script_adoption:{revision:0,decision_id:null,content:null},script_review:{revision:0,decision_id:null,content:null},visual_rules_review:{revision:0,decision_id:null,status:'not_reviewed',reasons:[]},current_script:null,current_script_identity:null,current_script_decision_id:null,
    actions:[{stage_key:'media_generation',action:'generate',operation:null,output_kinds:['image','video','audio'],scope_kinds:['project','episode','shot']}]})
  const form = {versionId:'version',sha256:sha,operation:'arbitrary.registered.operation',outputKind:'image',workflowVersionId:'workflow',
    mode:'',promptMode:'manual',languageForm:unusedLanguageForm(),inputs:[],parameters:[],channels:[{key:'positive_prompt',rule:{type:'string',required:true}}],serverOwnedInputs:[],contextFields:[],sourceConstraints:{},purposes:{}}
  const draft = {literals:{},contexts:{},media:{},parameters:{},channels:{positive_prompt:'Exact user channels.'},subjects:[],sourceLanguage:'',targetLanguage:''}
  const input = {projectId:'project',options,stageKey:'media_generation',target:page.results[0],contract:form,draft,idempotencyKey:'one-candidate'}
  const request = m.buildManualGeneration(input)
  assert.deepEqual(request.scope,{kind:'shot',id}); assert.equal(request.unit_key,`shot:${id}`)
  assert.equal(request.payload.manual_channels.positive_prompt,draft.channels.positive_prompt)
  assert.equal(request.payload.execution_instance_version_id,'version')
  for (const kind of ['image','audio','video']) {
    assert.equal(m.buildManualGeneration({...input,contract:{...form,outputKind:kind}}).unit_key,request.unit_key)
  }
  assert.throws(()=>m.buildManualGeneration({...input,target:undefined}),/明确选择/)
  assert.throws(()=>m.buildManualGeneration({...input,contract:{...form,operation:'video.generate',outputKind:'text'}}),/输出类型/)
  assert.throws(()=>m.buildManualGeneration({...input,target:{scope:{kind:'project',id:'foreign'},unitKey:'project:foreign'}}),/明确选择/)
  assert.throws(()=>m.adaptManualActionOptions({run_id:'run',run_plan_sha256:sha,execution_activity:[],content_change_stage_keys:[],script_adoption:{revision:0,decision_id:null,content:null},script_review:{revision:0,decision_id:null,content:null},visual_rules_review:{revision:0,decision_id:null,status:'not_reviewed',reasons:[]},current_script:null,current_script_identity:null,current_script_decision_id:null,
    actions:[{stage_key:'media_generation',action:'generate',operation:'guessed',output_kinds:['video'],scope_kinds:['shot']}]}),/能力目录/)
}))

test('manual target API preserves its page envelope and never falls back on read failure', () => modules(async (m, vite) => {
  const {api} = await vite.ssrLoadModule('/src/services/api.ts')
  const {platformApi} = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const original = api.get, calls = []
  const raw = {run_id:'run',run_plan_sha256:sha,count:0,next:null,previous:null,results:[]}
  try {
    api.get = async (...args) => {calls.push(args);return {data:raw}}
    const result = await platformApi.listProductionTargets('run','shot',2)
    assert.deepEqual(result,m.adaptManualTargetPage(raw))
    assert.deepEqual(calls,[['/production-runs/run/targets/',{params:{kind:'shot',page:2}}]])
    api.get = async () => {throw new Error('target list unavailable')}
    await assert.rejects(platformApi.listProductionTargets('run','shot'),/unavailable/)
  } finally {api.get=original}
}))

test('Bible image preparation chooses an actual character, scene or prop without a placeholder shot', () => modules(m => {
  const id = '12345678-1234-4234-8234-123456789abc'
  const options = m.adaptManualActionOptions({run_id:'run',run_plan_sha256:sha,
    execution_activity:[],content_change_stage_keys:[],script_adoption:{revision:0,decision_id:null,content:null},script_review:{revision:0,decision_id:null,content:null},
    visual_rules_review:{revision:0,decision_id:null,status:'not_reviewed',reasons:[]},current_script:null,current_script_identity:null,current_script_decision_id:null,
    actions:[{stage_key:'bible_image_generation',action:'generate',operation:null,output_kinds:['image'],scope_kinds:['character','scene','prop']}]})
  const contract = {versionId:'version',sha256:sha,operation:'arbitrary.registered.operation',outputKind:'image',workflowVersionId:'workflow',
    mode:'',promptMode:'manual',languageForm:unusedLanguageForm(),inputs:[],parameters:[],channels:[{key:'positive_prompt',rule:{type:'string',required:true}}],
    serverOwnedInputs:[],contextFields:[],sourceConstraints:{},purposes:{}}
  const draft = {literals:{},contexts:{},media:{},parameters:{},channels:{positive_prompt:'用户确认的素材描述'},subjects:[],sourceLanguage:'',targetLanguage:''}
  for (const kind of ['character','scene','prop']) {
    const page = m.adaptManualTargetPage({run_id:'run',run_plan_sha256:sha,count:1,next:null,previous:null,
      results:[{scope:{kind,id},unit_key:`${kind}:${id}`,label:'实际对象'}]})
    assert.ok(m.manualScopeLabels[kind])
    const input = {projectId:'project',options,stageKey:'bible_image_generation',target:page.results[0],contract,draft,idempotencyKey:`target-${kind}`}
    const request = m.buildManualGeneration(input)
    assert.deepEqual(request.scope,{kind,id})
    assert.equal(request.unit_key,`${kind}:${id}`)
    assert.equal(request.payload.manual_channels.positive_prompt,draft.channels.positive_prompt)
    assert.throws(()=>m.buildManualGeneration({...input,contract:{...contract,outputKind:'video'}}),/输出类型/)
    assert.throws(()=>m.buildManualGeneration({...input,target:{scope:{kind:'shot',id},unitKey:`shot:${id}`,label:'错误镜头'}}),/制作对象/)
  }
}))

test('production source adapter cannot infer manual or recipe and preserves waiting instead of completed', () => modules(async (_m,vite) => {
  const { adaptProductionRun, presentPlatformStatus } = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const row = adaptProductionRun({id:'run',project:'project',plan_source:'manual',recipe_version:null,status:'awaiting_input'})
  assert.equal(row.planSource,'manual'); assert.equal(row.status,'awaiting_input')
  assert.equal(presentPlatformStatus(row.status).label,'等待人工操作')
  assert.throws(() => adaptProductionRun({id:'run',recipe_version:'recipe'}), /计划来源/)
  assert.throws(() => adaptProductionRun({id:'run',plan_source:'manual',recipe_version:'recipe'}))
  assert.throws(() => adaptProductionRun({id:'run',plan_source:'recipe',recipe_version:null}))
  const { productionRunUnstartedLabel } = await vite.ssrLoadModule('/src/services/productionRunPresentation.ts')
  assert.equal(productionRunUnstartedLabel(row),'等待选择人工制作步骤')
  assert.equal(productionRunUnstartedLabel({...row,status:'succeeded'}),'本轮成片已封存')
  assert.equal(productionRunUnstartedLabel({...row,status:'succeeded',planSource:'recipe'}),'已完成全部阶段')
  assert.equal(productionRunUnstartedLabel({...row,status:'cancelled'}),'本轮已取消')
}))
