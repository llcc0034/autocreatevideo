// Explicit isolated data: not a live Run, model response or audit evidence.
export const sha='a'.repeat(64)
export const id=(n:number)=>`10000000-0000-0000-0000-${String(n).padStart(12,'0')}`
export const run={id:id(1),project:id(2),planSource:'manual',status:'awaiting_input',executionPlan:{sha256:sha}}
export const options={runId:run.id,runPlanSha256:sha,actions:[{stageKey:'storyboard',action:'adopt'}]}
export const shot={id:id(4),number:1,title:'已保存镜头',
  duration_ms:6000,media_type:'image',dialogue:[],character_relations:[],prop_ids:[],episode_start_ms:null,episode_end_ms:null}
export const data={run_id:run.id,run_plan_sha256:sha,content_sha256:sha,
  content:{schema_version:3,run_plan_sha256:sha,script:null,script_decision_id:null,episodes:[{id:id(3),number:1,title:'手工分镜',synopsis:'',shots:[shot]}]},
  adoption:{revision:0,decision_id:null,snapshot_sha256:null,content_sha256:null,status:'not_adopted',source:null},
  materialization:{ready:false,reason:'已有分镜，不会覆盖',contract:null,sha256:null}}
export const script={id:id(8),sha256:sha,revision:2}
export const proposed={...data,content:{...data.content,script,script_decision_id:id(9),episodes:[]},materialization:{ready:true,reason:'',sha256:sha,contract:{
  schema_version:3,source_basis:{schema_version:2,script,script_decision_id:id(9),run_plan_sha256:sha,
    script_stages:[{stage_id:id(10),stage_key:'script_input',output_snapshot_sha256:sha}],
    source:{schema_version:3,kind:'script_reference_bible',declaration_sha256:sha,structure_source:{document_sha256:sha}},
    bible_review:{decision_id:id(11),stage_run_id:id(12)},resolved_relations_sha256:sha},run_config_sha256:sha,parameters:{
    default_shot_media_type:'video',default_shot_duration_ms:6000,
    video_shot_min_duration_ms:1000,video_shot_max_duration_ms:10000,
    multi_shot:false,include_audio:true,default_first_frame_continuity:false,default_last_frame_continuity:false}}}}
