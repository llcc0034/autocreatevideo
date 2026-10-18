export function referenceTopologyFixture(){return {
  schema_version:1,maximum_rules:32,maximum_duration_ms:60000,maximum_fps:60,label_max_characters:80,text_max_characters:1024,maximum_targets_per_source:1,
  media_groups:[{value:'videos',label:'视频用途',media_kind:'video',input_slot:'reference_videos'},{value:'audios',label:'音频用途',media_kind:'audio',input_slot:'reference_audios'}],
  preprocessing_slots:[{value:'videos',label:'参考视频',input_slot:'reference_videos',include_target_fps:true,adapters:[{value:'cfr_video_v1',label:'恒定帧率',description:'任务副本',target_fps:'required'},{value:'passthrough_video_v1',label:'原样视频',description:'保留原视频',target_fps:'forbidden'}]},
    {value:'paired_video_audios',label:'视频配对音频',input_slot:'paired_video_audios',include_target_fps:false,adapters:[{value:'aligned_audio_v1',label:'对齐音轨',description:'对齐视频',target_fps:'not_applicable'}]},
    {value:'reference_audios',label:'独立参考音频',input_slot:'reference_audios',include_target_fps:false,adapters:[{value:'passthrough_audio_v1',label:'原样音频',description:'保留音频',target_fps:'not_applicable'}]}],
  pairing_direction:{source_media_kind:'video',target_media_kind:'audio'},
}}
export function conditioningOptionsFixture(){const slots=['total_images','first_frame','last_frame','entity_images','reference_videos','paired_video_audios','reference_audios'];return {
  schemaVersion:1,adapterKey:'media_conditioning_v1',inputKinds:[{value:'classified_references',label:'多参考素材'}],inputSlots:slots.map(value=>({value,label:value})),maximumItems:32,maximumModes:10,identifierPattern:'^[A-Za-z][A-Za-z0-9_.-]{0,63}$',
  modeTemplate:{key:'',label:'',description:'',input_kind:'',requires_entity_reference_bible:false,supports_native_audio:false,inputs:Object.fromEntries(slots.map(key=>[key,{minimum_items:0,maximum_items:0}]))},
  dynamicReferenceTopology:referenceTopologyFixture(),
}}
export function referenceModeFixture(){const options=conditioningOptionsFixture();const purpose=(key:string)=>({key,label:key,description:'test purpose',minimum_items:0,maximum_items:2});return {
  ...options.modeTemplate,key:'test_mode',label:'测试模式',input_kind:'classified_references',inputs:{...options.modeTemplate.inputs,reference_videos:{minimum_items:0,maximum_items:2},paired_video_audios:{minimum_items:0,maximum_items:2},reference_audios:{minimum_items:0,maximum_items:2}},
  dynamic_reference_topology:{schema_version:1,videos:{minimum_items:0,maximum_items:2,purposes:[purpose('editing'),purpose('motion')],default_purpose_key:'editing'},audios:{minimum_items:0,maximum_items:2,purposes:[purpose('voice'),purpose('music')],default_purpose_key:'voice'},
    preprocessing:{videos:{adapter_key:'cfr_video_v1',minimum_duration_ms:1000,maximum_duration_ms:10000,target_fps:24,requires_source_fps:true},paired_video_audios:{adapter_key:'aligned_audio_v1',minimum_duration_ms:1000,maximum_duration_ms:10000},reference_audios:{adapter_key:'passthrough_audio_v1',minimum_duration_ms:1000,maximum_duration_ms:10000}},
    pairing_rules:[{key:'pair',label:'配对',description:'test',source_media_kind:'video',target_media_kind:'audio',source_purpose_keys:['editing'],target_purpose_keys:['voice'],default_target_purpose_key:'voice',minimum_targets_per_source:0,maximum_targets_per_source:1,allow_embedded_source:true,allow_external_target:true,source_and_external_mutually_exclusive:true}],
    mutual_exclusion_rules:[{key:'exclusion',label:'互斥',description:'test',media_kind:'video',purpose_keys:['editing','motion'],maximum_distinct_purposes:1}],
  },
}}
