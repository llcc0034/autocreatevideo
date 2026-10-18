import {unusedLanguageForm} from './executionLanguage.ts'
import {displayFixture} from '../storyboardReviewDisplayFixture.ts'
export const id = (n:number)=>`10000000-0000-4000-8000-${String(n).padStart(12,'0')}`
export const sha = 'a'.repeat(64)
export const run:any = {id:id(1),project:id(2),planSource:'manual',status:'awaiting_input',executionPlan:{sha256:sha}}
export const target = {scope:{kind:'shot',id:id(4)},unitKey:`shot:${id(4)}`,label:'隔离样例 · 第一镜'}
export const options:any = {runId:run.id,runPlanSha256:sha,actions:[{stageKey:'media_generation',action:'generate',operation:null,outputKinds:['image','video','audio'],scopeKinds:['shot']}]}
export const review:any = {display:displayFixture(id(3),[id(4)]),run_id:run.id,run_plan_sha256:sha,ready:true,reason:'',contract_sha256:sha,
  source_options:[{value:'images',label:'镜头用途图片',kind:'image'}],approval:{revision:1,decision_id:id(9),snapshot_sha256:sha,status:'current'},
  contract:{schema_version:1,kind:'manual_storyboard_review',stage_key:'storyboard_review',formal_object_type:'storyboard',canonical_sha256:sha,
    manual_basis:{run_plan_sha256:sha},canonical:{episodes:[{id:id(3),number:1,title:'隔离剧集',shots:[{id:id(4),number:1,title:'同文件不同确切用途',
      reference_uses:[0,1].map(n=>({use_key:['雨衣正面','雨衣侧面'][n],media_kind:'image',purpose:n?'侧面轮廓':'外观参考，不是首帧',
        source_mode:'existing',reference_version_id:id(20+n),asset_id:null,asset_sha256:sha}))}]}]},
    assets:[{id:id(6),name:'同一雨衣文件',kind:'reference',mime_type:'image/png',checksum:sha}]},
  references:[{shot_id:id(4),items:[0,1].map(n=>({reference_key:'shot-use:'+['雨衣正面','雨衣侧面'][n],source_key:'images',kind:'image',
    source:{kind:'asset',asset_id:id(6)},purpose:n?'侧面轮廓':'外观参考，不是首帧',subject_ref:null,paired_reference_key:null}))}]}
export const input:any = {key:'pictures',kind:'image',position:0,required:true,min_items:1,max_items:2,
  semantic_contract:{schema_version:1,allowed_purposes:[],allowed_subject_kinds:[],allowed_scope_kinds:['shot'],paired_with_input_key:null,allow_repeated_asset:true},
  targets:[{target_kind:'workflow_slot',target_key:'pictures',is_primary:true}]}
export const contract:any = {versionId:id(10),sha256:sha,operation:'video.reference_to_video',outputKind:'video',mode:'',workflowVersionId:id(11),
  inputs:[input],parameters:[],promptMode:'none',channels:[],serverOwnedInputs:[],contextFields:[],sourceConstraints:{pictures:{kinds:['asset'],selectors:null}},
  purposes:{},languageForm:unusedLanguageForm(),subjectCategories:[]}
export const rawContract:any = {version_id:contract.versionId,revision:1,frozen:true,sha256:sha,context_fields:[],subject_categories:[{value:'character',label:'人物'}],language_form:unusedLanguageForm(),input_forms:{},
  definition:{version_id:contract.versionId,configuration:{deployment_id:id(12),operation:contract.operation,mode:'',workflow_version_id:id(11),prompt_mode:'none',prompt_profile_version_id:null,
    inputs:[input],parameters:[],auxiliaries:[]},interface:{output:{contract:{primary:{kind:'video'}}},input_purposes:{},parameters:{},prompt:{mode:'none',channels:{}}}}}
